"""Portable static-asset and managed-process gateway for app-engine."""

from __future__ import annotations

import asyncio
import errno
import hashlib
import ipaddress
import mimetypes
import os
import stat
import uuid
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from urllib.parse import urlencode, urlparse

import httpx

from .contracts import (
    AppCatalog,
    AppGateway,
    AppManifest,
    AppSession,
    AppSessionId,
    AssetRequest,
    AssetResponse,
    CatalogKey,
    LaunchId,
    OpenAppRequest,
    ProxyRequest,
    ProxyResponse,
    ProcessScope,
    RuntimeState,
    ScopeKey,
    StopReason,
    TargetEndpoint,
    TargetProviderError,
    TargetSpec,
)


_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)


@dataclass(frozen=True)
class _SessionRecord:
    public: AppSession
    manifest: AppManifest
    target: TargetSpec
    endpoint: TargetEndpoint | None
    scope_key: ScopeKey | None


@dataclass
class _ScopeState:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    launch_ids: set[LaunchId] = field(default_factory=set)


class DefaultAppGateway(AppGateway):
    """Serve static apps and proxy managed web apps through one safe surface."""

    def __init__(
        self,
        *,
        catalog: AppCatalog,
        lifecycle,
        catalog_key: CatalogKey,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._catalog = catalog
        self._lifecycle = lifecycle
        self._catalog_key = catalog_key
        self._client = httpx.AsyncClient(transport=transport, trust_env=False)
        self._sessions: dict[AppSessionId, _SessionRecord] = {}
        self._scopes: dict[ScopeKey, _ScopeState] = {}
        self._session_lock = asyncio.Lock()
        self._map_lock = asyncio.Lock()
        self._shutdown = False

    async def open(self, request: OpenAppRequest) -> AppSession:
        manifest, target = self._resolve_target(request)
        if target.kind != "web":
            raise TargetProviderError(
                "unsupported_gateway_target",
                "The default application gateway only serves web targets.",
                remediation="Use the registered provider for this target kind.",
                context=(("kind", target.kind),),
            )

        runtime = target.runtime
        if target.entry_point is not None:
            endpoint = _entry_point_endpoint(target.entry_point)
            async with self._map_lock:
                self._require_open_gateway()
                return await self._register_session(
                    manifest, target, None, endpoint, None
                )
        if runtime is None:
            async with self._map_lock:
                self._require_open_gateway()
                return await self._register_session(
                    manifest, target, None, None, None
                )

        scope_key = _scope_key(request, target, runtime.scope)
        async with self._map_lock:
            self._require_open_gateway()
            state = self._scopes.setdefault(scope_key, _ScopeState())
        async with state.lock:
            self._require_open_gateway()
            handle = await self._lifecycle.launch(request.launch, request.approval)
            launch_id = handle.launch_id
            state.launch_ids.add(launch_id)
            try:
                status = await self._lifecycle.status(launch_id)
            except BaseException:
                await self._cleanup_rejected_launch(state, launch_id)
                raise
            if status.state is not RuntimeState.READY or status.endpoint is None:
                await self._cleanup_rejected_launch(state, launch_id)
                raise TargetProviderError(
                    "target_not_ready",
                    "The managed target did not provide a ready endpoint.",
                    remediation="Inspect the target status and process logs.",
                    context=(("launch_id", str(launch_id)),),
                )
            try:
                endpoint = _normalize_loopback_endpoint(status.endpoint)
            except TargetProviderError:
                await self._cleanup_rejected_launch(state, launch_id)
                raise

            await self._lifecycle.retain(launch_id)
            try:
                return await self._register_session(
                    manifest, target, launch_id, endpoint, scope_key
                )
            except BaseException:
                await self._lifecycle.release(launch_id)
                raise

    async def close(self, session_id: AppSessionId) -> None:
        async with self._map_lock:
            async with self._session_lock:
                record = self._sessions.get(session_id)
            state = (
                self._scopes.get(record.scope_key)
                if record is not None and record.scope_key is not None
                else None
            )
        if record is None:
            return
        if record.scope_key is None:
            async with self._session_lock:
                self._sessions.pop(session_id, None)
            return

        if state is None:
            # Shutdown has already cleared this session and its exact lease.
            async with self._session_lock:
                self._sessions.pop(session_id, None)
            return
        async with state.lock:
            async with self._session_lock:
                removed = self._sessions.pop(session_id, None)
            if removed is None:
                return
            assert removed.public.launch_id is not None
            await self._lifecycle.release(removed.public.launch_id)

    async def serve_asset(
        self, session: AppSession, request: AssetRequest
    ) -> AssetResponse:
        record = await self._session_record(session)
        if record.endpoint is not None:
            raise TargetProviderError(
                "managed_target_asset",
                "Backend targets must serve content through the application proxy.",
            )

        root = record.manifest.root.resolve(strict=True)
        relative = PurePosixPath(request.relative_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise _asset_escape(request.relative_path)

        parts = tuple(part for part in relative.parts if part not in {"", "."})
        body = await asyncio.to_thread(
            _read_contained_regular, root, parts, request.relative_path
        )
        asset_name = parts[-1] if parts else "index.html"
        if body is None:
            if relative.suffix:
                return _asset_not_found()
            body = await asyncio.to_thread(
                _read_contained_regular, root, ("index.html",), "index.html"
            )
            if body is None:
                return _asset_not_found()
            asset_name = "index.html"

        etag = f'"{hashlib.sha256(body).hexdigest()}"'
        media_type = mimetypes.guess_type(asset_name)[0] or "application/octet-stream"
        headers = (
            ("X-Content-Type-Options", "nosniff"),
            ("ETag", etag),
        )
        if request.if_none_match == etag:
            return AssetResponse(304, media_type, headers, b"")
        return AssetResponse(200, media_type, headers, body)

    async def proxy(
        self, session: AppSession, request: ProxyRequest
    ) -> ProxyResponse:
        record = await self._session_record(session)
        endpoint = record.endpoint
        if endpoint is None:
            raise TargetProviderError(
                "static_target_proxy",
                "Static targets do not expose a managed proxy endpoint.",
            )
        if not _is_loopback(endpoint.host):
            raise TargetProviderError(
                "non_loopback_endpoint",
                "Managed targets may only be proxied from a loopback endpoint.",
                context=(("host", endpoint.host),),
            )

        if _is_websocket_upgrade(request.headers):
            raise TargetProviderError(
                "websocket_route_required",
                "WebSocket upgrades require the host's bidirectional route adapter.",
                remediation=(
                    "Use websocket_endpoint() to obtain the pinned loopback target "
                    "for the reusable WebSocket route bridge."
                ),
            )

        url = _proxy_url(endpoint, request.path, request.query)
        headers = _strip_hop_headers(request.headers, remove_host=True)
        upstream_request = self._client.build_request(
            request.method,
            url,
            headers=headers,
            content=request.body,
        )
        try:
            upstream = await self._client.send(upstream_request, stream=True)
        except httpx.HTTPError as exc:
            raise TargetProviderError(
                "upstream_unavailable",
                "The managed application endpoint could not be reached.",
                remediation="Inspect the application process and retry.",
                context=(("detail", str(exc)),),
            ) from exc

        response_headers = _strip_hop_headers(upstream.headers.multi_items())

        async def body():
            try:
                async for chunk in upstream.aiter_raw():
                    yield chunk
            finally:
                await upstream.aclose()

        return ProxyResponse(upstream.status_code, response_headers, body())

    async def websocket_endpoint(self, session: AppSession) -> TargetEndpoint:
        """Return the pinned target for a host-owned WebSocket route bridge.

        ``ProxyRequest`` is an HTTP byte-stream DTO and cannot represent
        bidirectional WebSocket frames.  Route adapters must authenticate the
        public session, call this seam, then bridge frames to the returned
        loopback endpoint without performing another DNS lookup.
        """
        record = await self._session_record(session)
        if record.endpoint is None:
            raise TargetProviderError(
                "static_target_websocket",
                "Static targets do not expose a WebSocket endpoint.",
            )
        return record.endpoint

    async def shutdown(self) -> tuple[LaunchId, ...]:
        """Stop retained launches and close the gateway's HTTP resources.

        This concrete composition seam complements the frozen per-session
        ``AppGateway.close`` contract.  ``AppEngineRuntime.close`` should call
        it so zero-idle targets remain warm only for the runtime's lifetime.
        """
        async with self._map_lock:
            self._shutdown = True
            scopes = tuple(self._scopes.items())

        stopped: list[LaunchId] = []
        async with AsyncExitStack() as stack:
            for _, state in scopes:
                await stack.enter_async_context(state.lock)

            async with self._session_lock:
                sessions = tuple(self._sessions.values())
                self._sessions.clear()
            for record in sessions:
                if record.public.launch_id is not None:
                    await self._lifecycle.release(record.public.launch_id)

            for _, state in scopes:
                for launch_id in tuple(state.launch_ids):
                    result = await self._lifecycle.stop(
                        launch_id, StopReason.SHUTDOWN
                    )
                    if result.stopped:
                        state.launch_ids.discard(launch_id)
                        stopped.append(launch_id)

        async with self._map_lock:
            for scope_key, state in scopes:
                if not state.launch_ids and self._scopes.get(scope_key) is state:
                    self._scopes.pop(scope_key, None)
        await self._client.aclose()
        return tuple(stopped)

    async def _register_session(
        self,
        manifest: AppManifest,
        target: TargetSpec,
        launch_id: LaunchId | None,
        endpoint: TargetEndpoint | None,
        scope_key: ScopeKey | None,
    ) -> AppSession:
        session_id = AppSessionId(uuid.uuid4().hex)
        public = AppSession(
            session_id=session_id,
            app_id=manifest.app_id,
            launch_id=launch_id,
            origin=f"app-engine://{session_id}",
            entry_url=f"/apps/{manifest.app_id}/sessions/{session_id}/",
            capabilities=(),
        )
        async with self._session_lock:
            self._sessions[session_id] = _SessionRecord(
                public, manifest, target, endpoint, scope_key
            )
        return public

    async def _cleanup_rejected_launch(
        self, state: _ScopeState, launch_id: LaunchId
    ) -> None:
        result = await self._lifecycle.stop(launch_id, StopReason.FAILURE)
        if result.stopped:
            state.launch_ids.discard(launch_id)

    def _require_open_gateway(self) -> None:
        if self._shutdown:
            raise TargetProviderError(
                "gateway_shutdown",
                "The application gateway has already shut down.",
            )

    def _resolve_target(self, request: OpenAppRequest) -> tuple[AppManifest, TargetSpec]:
        snapshot = self._catalog.snapshot(self._catalog_key)
        manifest = next(
            (
                app.manifest
                for app in snapshot.apps
                if app.manifest.app_id == request.launch.app_id
            ),
            None,
        )
        if manifest is None:
            raise TargetProviderError(
                "app_not_found",
                f"App {request.launch.app_id!s} is not present in the catalog.",
                remediation="Refresh the app catalog and try again.",
            )
        target_id = request.launch.target_id or manifest.default_target
        target = next(
            (target for target in manifest.targets if target.target_id == target_id),
            None,
        )
        if target is None:
            raise TargetProviderError(
                "target_not_found",
                f"Target {target_id!s} is not declared by the app.",
                context=(("app_id", str(manifest.app_id)),),
            )
        return manifest, target

    async def _session_record(self, session: AppSession) -> _SessionRecord:
        async with self._session_lock:
            record = self._sessions.get(session.session_id)
        if record is None or record.public != session:
            raise TargetProviderError(
                "invalid_app_session",
                "The app session is closed or does not belong to this gateway.",
            )
        return record


def _read_contained_regular(
    root: Path, parts: tuple[str, ...], display_path: str
) -> bytes | None:
    """Read an asset through no-follow descriptors rooted at ``root``.

    Validation and reading use the same final descriptor, closing the usual
    resolve/check/reopen race.  Each intermediate component is opened relative
    to its already-verified parent descriptor.
    """
    if not parts:
        return None
    common_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    directory_flags = (
        common_flags
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    file_flags = (
        common_flags
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    descriptors: list[int] = []
    try:
        descriptors.append(os.open(root, directory_flags))
        parent_fd = descriptors[-1]
        for component in parts[:-1]:
            descriptors.append(
                os.open(component, directory_flags, dir_fd=parent_fd)
            )
            parent_fd = descriptors[-1]
        descriptors.append(os.open(parts[-1], file_flags, dir_fd=parent_fd))
        asset_fd = descriptors[-1]
        if not stat.S_ISREG(os.fstat(asset_fd).st_mode):
            return None
        chunks: list[bytes] = []
        while chunk := os.read(asset_fd, 1024 * 1024):
            chunks.append(chunk)
        return b"".join(chunks)
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            return None
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise _asset_escape(display_path) from exc
        raise TargetProviderError(
            "asset_unavailable",
            "The requested asset could not be opened safely.",
            context=(("path", display_path), ("detail", str(exc))),
        ) from exc
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _asset_escape(relative_path: str) -> TargetProviderError:
    return TargetProviderError(
        "asset_path_escape",
        "The requested asset resolves outside the application root.",
        context=(("path", relative_path),),
    )


def _asset_not_found() -> AssetResponse:
    return AssetResponse(
        404,
        "text/plain; charset=utf-8",
        (("X-Content-Type-Options", "nosniff"),),
        b"Not Found",
    )


def _is_loopback(host: str) -> bool:
    normalized = host.strip().strip("[]").lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _entry_point_endpoint(value: str) -> TargetEndpoint:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
        raise TargetProviderError(
            "invalid_entry_point", "The legacy entry point is not an HTTP URL."
        )
    if parsed.port is None:
        port = 443 if parsed.scheme == "https" else 80
    else:
        port = parsed.port
    endpoint = TargetEndpoint(
        scheme=parsed.scheme,
        host=parsed.hostname,
        port=port,
        base_path=parsed.path or "/",
    )
    return _normalize_loopback_endpoint(endpoint)


def _normalize_loopback_endpoint(endpoint: TargetEndpoint) -> TargetEndpoint:
    normalized = endpoint.host.strip().strip("[]").lower()
    if normalized == "localhost":
        host = "127.0.0.1"
    else:
        try:
            address = ipaddress.ip_address(normalized)
        except ValueError:
            address = None
        if address is None or not address.is_loopback:
            raise TargetProviderError(
                "non_loopback_endpoint",
                "Managed targets may only be proxied from a loopback endpoint.",
                remediation="Bind the application to the assigned loopback host.",
                context=(("host", endpoint.host),),
            )
        host = address.compressed
    return TargetEndpoint(
        endpoint.scheme, host, endpoint.port, endpoint.base_path
    )


def _scope_key(
    request: OpenAppRequest, target: TargetSpec, scope: ProcessScope
) -> ScopeKey:
    launch = request.launch
    if scope is ProcessScope.SHARED:
        return ScopeKey(launch.app_id, target.target_id, None, None)
    if scope is ProcessScope.PER_USER:
        return ScopeKey(
            launch.app_id, target.target_id, launch.subject.subject_id, None
        )
    if launch.instance_id is None:
        raise TargetProviderError(
            "instance_required",
            "This app target requires an instance identifier.",
        )
    return ScopeKey(
        launch.app_id,
        target.target_id,
        launch.subject.subject_id,
        launch.instance_id,
    )


def _is_websocket_upgrade(headers) -> bool:
    upgrade = False
    connection_tokens: set[str] = set()
    for key, value in headers:
        lowered = key.lower()
        if lowered == "upgrade" and value.strip().lower() == "websocket":
            upgrade = True
        elif lowered == "connection":
            connection_tokens.update(
                token.strip().lower() for token in value.split(",") if token.strip()
            )
    return upgrade or "upgrade" in connection_tokens


def _proxy_url(
    endpoint: TargetEndpoint,
    request_path: str,
    query: tuple[tuple[str, str], ...],
) -> str:
    if endpoint.scheme not in {"http", "https"}:
        raise TargetProviderError(
            "unsupported_endpoint_scheme",
            "The web gateway only proxies HTTP and HTTPS endpoints.",
            context=(("scheme", endpoint.scheme),),
        )
    host = endpoint.host.strip("[]")
    authority_host = f"[{host}]" if ":" in host else host
    base = "/" + endpoint.base_path.strip("/") if endpoint.base_path.strip("/") else ""
    path = "/" + request_path.lstrip("/")
    encoded_query = urlencode(query)
    suffix = f"?{encoded_query}" if encoded_query else ""
    return f"{endpoint.scheme}://{authority_host}:{endpoint.port}{base}{path}{suffix}"


def _strip_hop_headers(
    headers, *, remove_host: bool = False
) -> tuple[tuple[str, str], ...]:
    materialized = tuple((str(key), str(value)) for key, value in headers)
    connection_tokens: set[str] = set()
    for key, value in materialized:
        if key.lower() == "connection":
            connection_tokens.update(
                token.strip().lower() for token in value.split(",") if token.strip()
            )
    blocked = set(_HOP_BY_HOP) | connection_tokens
    if remove_host:
        blocked.add("host")
    return tuple(
        (key, value) for key, value in materialized if key.lower() not in blocked
    )
