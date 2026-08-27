"""Behavioral tests for app-engine's reusable application gateway."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from app_engine.contracts import (
    AppCatalog,
    AppId,
    AppManifest,
    AppMetadata,
    AppSessionId,
    AppSource,
    AssetRequest,
    BrowserPolicy,
    CatalogApp,
    CatalogEvent,
    CatalogKey,
    CatalogSnapshot,
    CommandStep,
    HealthSpec,
    HostSubject,
    InstanceId,
    LaunchHandle,
    LaunchId,
    LaunchRequest,
    LaunchStatus,
    LifecycleManager,
    ManifestInspection,
    OpenAppRequest,
    ProcessScope,
    ProxyRequest,
    RuntimeSpec,
    RuntimeState,
    ScopeKey,
    StopReason,
    StopResult,
    TargetEndpoint,
    TargetId,
    TargetProviderError,
    TargetSpec,
)


def _gateway_type():
    try:
        from app_engine.gateway import DefaultAppGateway
    except ImportError as exc:
        pytest.fail(
            "app_engine.gateway.DefaultAppGateway is not implemented: "
            f"{exc}"
        )
    return DefaultAppGateway


class _Catalog(AppCatalog):
    def __init__(self, manifest: AppManifest) -> None:
        self.key = CatalogKey("test")
        self.current = CatalogSnapshot(
            key=self.key,
            generation=1,
            apps=(
                CatalogApp(
                    manifest=manifest,
                    source=AppSource("test", manifest.root.parent, 0),
                    compatible=True,
                ),
            ),
            rejections=(),
            created_at=datetime.now(timezone.utc),
        )

    async def configure(self, catalog_key, sources):
        return self.current

    def snapshot(self, catalog_key):
        assert catalog_key == self.key
        return self.current

    async def inspect(self, app_root: Path) -> ManifestInspection:
        raise AssertionError("the gateway must use the immutable catalog snapshot")

    async def refresh(self, catalog_key):
        return self.current

    async def events(self, catalog_key):
        if False:
            yield CatalogEvent(catalog_key, 1, ())


class _Lifecycle(LifecycleManager):
    def __init__(
        self,
        endpoint: TargetEndpoint | None = None,
        *,
        state: RuntimeState = RuntimeState.READY,
        stop_succeeds: bool = True,
    ) -> None:
        self.endpoint = endpoint or TargetEndpoint("http", "127.0.0.1", 8912)
        self.state = state
        self.stop_succeeds = stop_succeeds
        self.launch_requests: list[LaunchRequest] = []
        self.stop_calls: list[tuple[LaunchId, StopReason]] = []
        self.retain_calls: list[LaunchId] = []
        self.release_calls: list[LaunchId] = []
        self.leases: dict[LaunchId, int] = {}
        self.launch_id = LaunchId("launch-1")

    async def preview(self, request):
        raise AssertionError("gateway open should delegate launch authorization")

    async def launch(self, request, approval):
        self.launch_requests.append(request)
        return LaunchHandle(
            launch_id=self.launch_id,
            scope_key=ScopeKey(
                app_id=request.app_id,
                target_id=request.target_id or TargetId("web"),
                subject_id=request.subject.subject_id,
                instance_id=None,
            ),
            initial_state=RuntimeState.READY,
        )

    async def stop(self, launch_id, reason):
        self.stop_calls.append((launch_id, reason))
        return StopResult(launch_id, self.stop_succeeds, 0 if self.stop_succeeds else None)

    async def retain(self, launch_id):
        self.retain_calls.append(launch_id)
        self.leases[launch_id] = self.leases.get(launch_id, 0) + 1

    async def release(self, launch_id):
        self.release_calls.append(launch_id)
        self.leases[launch_id] = max(0, self.leases.get(launch_id, 0) - 1)

    async def status(self, launch_id):
        assert launch_id == self.launch_id
        return LaunchStatus(
            launch_id=launch_id,
            state=self.state,
            endpoint=self.endpoint,
            started_at=datetime.now(timezone.utc),
        )

    async def events(self, launch_id, after=None):
        if False:
            yield

    async def logs(self, launch_id, after, limit):
        raise AssertionError("gateway does not read logs while opening an app")


class _BlockingReopenLifecycle(_Lifecycle):
    def __init__(self) -> None:
        super().__init__()
        self.reopen_started = asyncio.Event()
        self.release_reopen = asyncio.Event()

    async def launch(self, request, approval):
        if self.launch_requests:
            self.reopen_started.set()
            await self.release_reopen.wait()
        return await super().launch(request, approval)


class _BlockingFirstLifecycle(_Lifecycle):
    def __init__(self) -> None:
        super().__init__()
        self.launch_started = asyncio.Event()
        self.release_launch = asyncio.Event()

    async def launch(self, request, approval):
        self.launch_started.set()
        await self.release_launch.wait()
        return await super().launch(request, approval)


def _manifest(
    root: Path, *, managed: bool, idle_timeout_seconds: float = 60
) -> AppManifest:
    runtime = None
    if managed:
        runtime = RuntimeSpec(
            driver="host",
            scope=ProcessScope.PER_USER,
            install=(),
            build=(),
            start=CommandStep(("python", "server.py")),
            health=HealthSpec(),
            idle_timeout_seconds=idle_timeout_seconds,
            autostart=False,
        )
    return AppManifest(
        manifest_version=2,
        app_id=AppId("demo"),
        label="Demo",
        icon="🧪",
        root=root,
        default_target=TargetId("web"),
        targets=(TargetSpec(TargetId("web"), "web", runtime),),
        configuration=(),
        metadata=AppMetadata(),
        browser=BrowserPolicy(),
    )


def _open_request() -> OpenAppRequest:
    return OpenAppRequest(
        launch=LaunchRequest(
            app_id=AppId("demo"),
            target_id=None,
            subject=HostSubject("alice", "user"),
            instance_id=InstanceId("window-1"),
        )
    )


async def _body(*chunks: bytes):
    for chunk in chunks:
        yield chunk


@pytest.mark.asyncio
async def test_legacy_entry_point_uses_the_same_safe_loopback_proxy(tmp_path):
    root = tmp_path / "demo"
    root.mkdir()
    manifest = _manifest(root, managed=False)
    manifest = replace(
        manifest,
        targets=(
            TargetSpec(
                TargetId("web"),
                "web",
                None,
                "http://127.0.0.1:8912/base",
            ),
        ),
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "http://127.0.0.1:8912/base/hello"
        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b"legacy backend"

        return httpx.Response(200, stream=Stream())

    gateway = _gateway_type()(
        catalog=_Catalog(manifest),
        lifecycle=_Lifecycle(),
        catalog_key=CatalogKey("test"),
        transport=httpx.MockTransport(handler),
    )
    session = await gateway.open(_open_request())

    response = await gateway.proxy(
        session, ProxyRequest("GET", "hello", (), (), _body())
    )
    assert b"".join([chunk async for chunk in response.body]) == b"legacy backend"


@pytest.mark.asyncio
async def test_static_open_and_assets_are_contained_with_spa_fallback(tmp_path):
    root = tmp_path / "demo"
    root.mkdir()
    (root / "index.html").write_text("<h1>home</h1>", encoding="utf-8")
    (root / "site.css").write_text("body{}", encoding="utf-8")
    lifecycle = _Lifecycle()
    gateway = _gateway_type()(
        catalog=_Catalog(_manifest(root, managed=False)),
        lifecycle=lifecycle,
        catalog_key=CatalogKey("test"),
    )

    session = await gateway.open(_open_request())
    assert session.launch_id is None
    assert lifecycle.launch_requests == []

    index = await gateway.serve_asset(session, AssetRequest(""))
    fallback = await gateway.serve_asset(
        session, AssetRequest("settings/profile")
    )
    stylesheet = await gateway.serve_asset(session, AssetRequest("site.css"))

    assert index.status_code == 200
    assert index.media_type.startswith("text/html")
    assert index.body == b"<h1>home</h1>"
    assert fallback.body == index.body
    assert stylesheet.media_type.startswith("text/css")
    index_headers = {key.lower(): value for key, value in index.headers}
    stylesheet_headers = {key.lower(): value for key, value in stylesheet.headers}
    assert index_headers["x-content-type-options"] == "nosniff"
    assert stylesheet_headers["x-content-type-options"] == "nosniff"


@pytest.mark.asyncio
async def test_asset_service_rejects_traversal_and_symlink_escape(tmp_path):
    root = tmp_path / "demo"
    root.mkdir()
    (root / "index.html").write_text("home", encoding="utf-8")
    secret = tmp_path / "secret.txt"
    secret.write_text("do not expose", encoding="utf-8")
    (root / "linked.txt").symlink_to(secret)
    gateway = _gateway_type()(
        catalog=_Catalog(_manifest(root, managed=False)),
        lifecycle=_Lifecycle(),
        catalog_key=CatalogKey("test"),
    )
    session = await gateway.open(_open_request())

    for relative_path in ("../secret.txt", "linked.txt"):
        with pytest.raises(TargetProviderError) as raised:
            await gateway.serve_asset(session, AssetRequest(relative_path))
        assert raised.value.code == "asset_path_escape"


@pytest.mark.asyncio
async def test_asset_service_reads_open_descriptor_without_path_reopen(
    tmp_path, monkeypatch
):
    root = tmp_path / "demo"
    root.mkdir()
    (root / "index.html").write_text("safe", encoding="utf-8")
    gateway = _gateway_type()(
        catalog=_Catalog(_manifest(root, managed=False)),
        lifecycle=_Lifecycle(),
        catalog_key=CatalogKey("test"),
    )
    session = await gateway.open(_open_request())

    def reject_path_reopen(_self):
        raise AssertionError("validated asset paths must not be reopened")

    monkeypatch.setattr(Path, "read_bytes", reject_path_reopen)
    response = await gateway.serve_asset(session, AssetRequest("index.html"))

    assert response.body == b"safe"


@pytest.mark.asyncio
async def test_zero_idle_timeout_retains_warm_launch_after_last_session_closes(
    tmp_path,
):
    lifecycle = _Lifecycle()
    gateway = _gateway_type()(
        catalog=_Catalog(
            _manifest(tmp_path, managed=True, idle_timeout_seconds=0)
        ),
        lifecycle=lifecycle,
        catalog_key=CatalogKey("test"),
    )

    first = await gateway.open(_open_request())
    second = await gateway.open(_open_request())

    assert first.launch_id == lifecycle.launch_id
    assert second.launch_id == lifecycle.launch_id
    assert len(lifecycle.launch_requests) == 2
    await gateway.close(first.session_id)
    assert lifecycle.stop_calls == []
    await gateway.close(second.session_id)
    await asyncio.sleep(0)
    assert lifecycle.stop_calls == []
    assert lifecycle.release_calls == [first.launch_id, second.launch_id]
    await gateway.close(AppSessionId("unknown"))
    assert lifecycle.stop_calls == []


@pytest.mark.asyncio
async def test_gateway_releases_exact_launch_for_lifecycle_idle_policy(tmp_path):
    lifecycle = _Lifecycle()
    gateway = _gateway_type()(
        catalog=_Catalog(
            _manifest(tmp_path, managed=True, idle_timeout_seconds=0.01)
        ),
        lifecycle=lifecycle,
        catalog_key=CatalogKey("test"),
    )
    session = await gateway.open(_open_request())

    await gateway.close(session.session_id)

    assert lifecycle.release_calls == [session.launch_id]
    assert lifecycle.stop_calls == []


@pytest.mark.asyncio
async def test_reopen_cancels_pending_idle_stop(tmp_path):
    lifecycle = _BlockingReopenLifecycle()
    gateway = _gateway_type()(
        catalog=_Catalog(
            _manifest(tmp_path, managed=True, idle_timeout_seconds=0.02)
        ),
        lifecycle=lifecycle,
        catalog_key=CatalogKey("test"),
    )
    first = await gateway.open(_open_request())
    await gateway.close(first.session_id)

    reopening = asyncio.create_task(gateway.open(_open_request()))
    await lifecycle.reopen_started.wait()
    await asyncio.sleep(0.04)
    lifecycle.release_reopen.set()
    reopened = await reopening

    assert reopened.launch_id == lifecycle.launch_id
    assert lifecycle.stop_calls == []
    assert lifecycle.retain_calls == [first.launch_id, reopened.launch_id]
    assert lifecycle.release_calls == [first.launch_id]


@pytest.mark.asyncio
async def test_runtime_shutdown_stops_zero_idle_warm_launch(tmp_path):
    lifecycle = _Lifecycle()
    gateway = _gateway_type()(
        catalog=_Catalog(
            _manifest(tmp_path, managed=True, idle_timeout_seconds=0)
        ),
        lifecycle=lifecycle,
        catalog_key=CatalogKey("test"),
    )
    session = await gateway.open(_open_request())
    await gateway.close(session.session_id)

    stopped = await gateway.shutdown()

    assert stopped == (lifecycle.launch_id,)
    assert lifecycle.stop_calls == [
        (lifecycle.launch_id, StopReason.SHUTDOWN)
    ]
    assert lifecycle.release_calls == [session.launch_id]


@pytest.mark.asyncio
async def test_shutdown_retries_launch_when_stop_reports_incomplete(tmp_path):
    lifecycle = _Lifecycle(stop_succeeds=False)
    gateway = _gateway_type()(
        catalog=_Catalog(_manifest(tmp_path, managed=True)),
        lifecycle=lifecycle,
        catalog_key=CatalogKey("test"),
    )
    session = await gateway.open(_open_request())
    await gateway.close(session.session_id)

    assert await gateway.shutdown() == ()
    lifecycle.stop_succeeds = True
    assert await gateway.shutdown() == (lifecycle.launch_id,)

    assert lifecycle.stop_calls == [
        (lifecycle.launch_id, StopReason.SHUTDOWN),
        (lifecycle.launch_id, StopReason.SHUTDOWN),
    ]


@pytest.mark.asyncio
async def test_shutdown_includes_open_already_admitted_to_scope_map(tmp_path):
    lifecycle = _BlockingFirstLifecycle()
    gateway = _gateway_type()(
        catalog=_Catalog(_manifest(tmp_path, managed=True)),
        lifecycle=lifecycle,
        catalog_key=CatalogKey("test"),
    )
    opening = asyncio.create_task(gateway.open(_open_request()))
    await lifecycle.launch_started.wait()
    shutting_down = asyncio.create_task(gateway.shutdown())
    await asyncio.sleep(0)

    lifecycle.release_launch.set()
    session = await opening
    assert await shutting_down == (lifecycle.launch_id,)
    await gateway.close(session.session_id)

    with pytest.raises(TargetProviderError) as raised:
        await gateway.open(_open_request())
    assert raised.value.code == "gateway_shutdown"


@pytest.mark.asyncio
async def test_managed_open_rejects_non_loopback_endpoint(tmp_path):
    lifecycle = _Lifecycle(TargetEndpoint("http", "example.com", 80))
    gateway = _gateway_type()(
        catalog=_Catalog(_manifest(tmp_path, managed=True)),
        lifecycle=lifecycle,
        catalog_key=CatalogKey("test"),
    )

    with pytest.raises(TargetProviderError) as raised:
        await gateway.open(_open_request())
    assert raised.value.code == "non_loopback_endpoint"
    assert lifecycle.stop_calls == [(lifecycle.launch_id, StopReason.FAILURE)]


@pytest.mark.asyncio
async def test_managed_open_cleans_up_launch_that_is_not_ready(tmp_path):
    lifecycle = _Lifecycle(state=RuntimeState.FAILED)
    gateway = _gateway_type()(
        catalog=_Catalog(_manifest(tmp_path, managed=True)),
        lifecycle=lifecycle,
        catalog_key=CatalogKey("test"),
    )

    with pytest.raises(TargetProviderError) as raised:
        await gateway.open(_open_request())

    assert raised.value.code == "target_not_ready"
    assert lifecycle.stop_calls == [(lifecycle.launch_id, StopReason.FAILURE)]


class _StreamingResponse(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.started = False
        self.closed = False

    async def __aiter__(self):
        self.started = True
        yield b"event: one\n\n"
        yield b"event: two\n\n"

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_proxy_preserves_stream_status_body_and_query_but_strips_hop_headers(
    tmp_path,
):
    captured: dict[str, object] = {}
    stream = _StreamingResponse()

    async def upstream(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = await request.aread()
        return httpx.Response(
            207,
            headers=[
                ("Content-Type", "text/event-stream"),
                ("Connection", "keep-alive, x-upstream-hop"),
                ("Keep-Alive", "timeout=5"),
                ("X-Upstream-Hop", "remove-me"),
                ("X-Upstream", "keep-me"),
            ],
            stream=stream,
        )

    lifecycle = _Lifecycle(TargetEndpoint("http", "localhost", 8912))
    gateway = _gateway_type()(
        catalog=_Catalog(_manifest(tmp_path, managed=True)),
        lifecycle=lifecycle,
        catalog_key=CatalogKey("test"),
        transport=httpx.MockTransport(upstream),
    )
    session = await gateway.open(_open_request())

    response = await gateway.proxy(
        session,
        ProxyRequest(
            method="POST",
            path="/events",
            query=(("tag", "one"), ("tag", "two"), ("blank", "")),
            headers=(
                ("Content-Type", "application/octet-stream"),
                ("Connection", "keep-alive, x-client-hop"),
                ("Keep-Alive", "timeout=5"),
                ("X-Client-Hop", "remove-me"),
                ("X-Client", "keep-me"),
                ("Host", "untrusted.example"),
            ),
            body=_body(b"hello", b" world"),
        ),
    )

    assert response.status_code == 207
    assert stream.started is False
    assert captured["url"] == (
        "http://127.0.0.1:8912/events?tag=one&tag=two&blank="
    )
    assert captured["body"] == b"hello world"
    upstream_headers = captured["headers"]
    assert upstream_headers["x-client"] == "keep-me"
    assert "keep-alive" not in upstream_headers
    assert "x-client-hop" not in upstream_headers
    assert upstream_headers["host"] == "127.0.0.1:8912"

    response_headers = {key.lower(): value for key, value in response.headers}
    assert response_headers["content-type"] == "text/event-stream"
    assert response_headers["x-upstream"] == "keep-me"
    assert "connection" not in response_headers
    assert "keep-alive" not in response_headers
    assert "x-upstream-hop" not in response_headers
    assert [chunk async for chunk in response.body] == [
        b"event: one\n\n",
        b"event: two\n\n",
    ]
    assert stream.closed is True


@pytest.mark.asyncio
async def test_websocket_upgrade_uses_explicit_route_seam(tmp_path):
    lifecycle = _Lifecycle(TargetEndpoint("http", "localhost", 8912))
    gateway = _gateway_type()(
        catalog=_Catalog(_manifest(tmp_path, managed=True)),
        lifecycle=lifecycle,
        catalog_key=CatalogKey("test"),
        transport=httpx.MockTransport(
            lambda request: pytest.fail("HTTP transport must not fake WebSockets")
        ),
    )
    session = await gateway.open(_open_request())

    endpoint = await gateway.websocket_endpoint(session)
    assert endpoint == TargetEndpoint("http", "127.0.0.1", 8912)

    with pytest.raises(TargetProviderError) as raised:
        await gateway.proxy(
            session,
            ProxyRequest(
                method="GET",
                path="/socket",
                query=(),
                headers=(("Connection", "Upgrade"), ("Upgrade", "websocket")),
                body=_body(),
            ),
        )
    assert raised.value.code == "websocket_route_required"
