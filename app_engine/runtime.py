"""Standalone composition root and local host adapter for app-engine."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .catalog import ImmutableAppCatalog
from .contracts import (
    AppEngineRuntime,
    AppId,
    AppSource,
    AppStateStore,
    Authorization,
    CatalogKey,
    ConfigurationResult,
    ConfigurationValue,
    HostAdapter,
    HostCapabilityProviders,
    HostSubject,
    LaunchRequest,
    ResolvedConfiguration,
    RuntimeEvent,
    ShutdownReport,
    StudioRoot,
)
from .gateway import DefaultAppGateway
from .lifecycle import ProcessLifecycleManager
from .manifest import APP_ID_RE
from .providers import ProviderRegistry
from .studio import PortableStudioService


class _NoCapabilities(HostCapabilityProviders):
    def names(self) -> tuple[str, ...]:
        return ()


class _FileStateStore(AppStateStore):
    def __init__(self, path: Path, *, maximum_bytes: int = 1024 * 1024) -> None:
        self._path = path
        self._maximum_bytes = maximum_bytes

    async def load(self) -> bytes:
        try:
            return await asyncio.to_thread(self._path.read_bytes)
        except FileNotFoundError:
            return b"{}"

    async def save(self, content: bytes) -> None:
        if len(content) > self._maximum_bytes:
            raise ValueError("app state exceeds the configured size limit")
        await asyncio.to_thread(self._save, content)

    def _save(self, content: bytes) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_name(f".{self._path.name}.{os.getpid()}.tmp")
        try:
            temporary.write_bytes(content)
            os.chmod(temporary, 0o600)
            os.replace(temporary, self._path)
        finally:
            temporary.unlink(missing_ok=True)


class LocalHostAdapter(HostAdapter):
    """Single-user file-backed host used by the distributable standalone app."""

    def __init__(
        self,
        *,
        apps_roots: tuple[Path, ...],
        state_root: Path,
        subject: HostSubject | None = None,
        trusted_roots: tuple[Path, ...] = (),
        studio_roots: tuple[StudioRoot, ...] | None = None,
    ) -> None:
        self.subject = subject or HostSubject("local", "admin")
        self._apps_roots = tuple(Path(root).expanduser().resolve(strict=False) for root in apps_roots)
        self._state_root = Path(state_root).expanduser().resolve(strict=False)
        self._trusted_roots = tuple(Path(root).expanduser().resolve(strict=False) for root in trusted_roots)
        default_studio = tuple(
            StudioRoot(root=root, inspect=True, create=True, write=True)
            for root in self._apps_roots
        )
        self._studio_roots = studio_roots if studio_roots is not None else default_studio
        self.recorded_events: list[RuntimeEvent] = []

    async def sources(self, subject: HostSubject) -> tuple[AppSource, ...]:
        self._require_subject(subject)
        count = len(self._apps_roots)
        return tuple(
            AppSource(
                kind="trusted" if self._contained(root, self._trusted_roots) else "external",
                root=root,
                priority=count - index,
            )
            for index, root in enumerate(self._apps_roots)
        )

    async def configuration(
        self, subject: HostSubject, app_id: AppId
    ) -> ResolvedConfiguration:
        self._require_subject(subject)
        path = self._configuration_path(subject, app_id)
        try:
            raw = await asyncio.to_thread(path.read_text, "utf-8")
            payload = json.loads(raw)
        except FileNotFoundError:
            return ResolvedConfiguration(app_id=app_id, values=())
        except (OSError, json.JSONDecodeError, TypeError):
            return ResolvedConfiguration(app_id=app_id, values=())
        values = tuple(
            ConfigurationValue(
                key=str(item["key"]),
                value=str(item["value"]),
                secret=bool(item.get("secret", False)),
            )
            for item in payload.get("values", ())
            if isinstance(item, dict) and "key" in item and "value" in item
        )
        return ResolvedConfiguration(app_id=app_id, values=values)

    async def save_configuration(
        self,
        subject: HostSubject,
        app_id: AppId,
        values: tuple[ConfigurationValue, ...],
    ) -> ConfigurationResult:
        self._require_subject(subject)
        path = self._configuration_path(subject, app_id)
        payload = {
            "values": [
                {"key": value.key, "value": value.value, "secret": value.secret}
                for value in values
            ]
        }
        await asyncio.to_thread(
            _FileStateStore(path)._save,
            (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8"),
        )
        return ConfigurationResult(
            app_id=app_id, saved_keys=tuple(value.key for value in values)
        )

    async def authorize(self, subject: HostSubject, plan) -> Authorization:
        self._require_subject(subject)
        if not plan.steps:
            return Authorization(allowed=True, approval_required=False)
        trusted = all(
            self._contained(step.cwd, self._trusted_roots) for step in plan.steps
        )
        return Authorization(
            allowed=True,
            approval_required=not trusted,
            reason="External process plans require approval." if not trusted else "",
        )

    async def record_event(self, event: RuntimeEvent) -> None:
        self.recorded_events.append(event)

    async def resolve_app_state(
        self, subject: HostSubject, app_id: AppId
    ) -> AppStateStore:
        self._require_subject(subject)
        self._validate_app_id(app_id)
        return _FileStateStore(
            self._state_root / "app-state" / self._subject_key(subject) / f"{app_id}.json"
        )

    async def studio_roots(self, subject: HostSubject) -> tuple[StudioRoot, ...]:
        self._require_subject(subject)
        return self._studio_roots

    def capability_providers(self) -> HostCapabilityProviders:
        return _NoCapabilities()

    def _configuration_path(self, subject: HostSubject, app_id: AppId) -> Path:
        self._validate_app_id(app_id)
        return self._state_root / "configuration" / self._subject_key(subject) / f"{app_id}.json"

    @staticmethod
    def _subject_key(subject: HostSubject) -> str:
        return hashlib.sha256(subject.subject_id.encode("utf-8")).hexdigest()

    def _require_subject(self, subject: HostSubject) -> None:
        if subject != self.subject:
            raise PermissionError("standalone app-engine subject mismatch")

    @staticmethod
    def _validate_app_id(app_id: AppId) -> None:
        if APP_ID_RE.fullmatch(str(app_id)) is None:
            raise ValueError("invalid app id")

    @staticmethod
    def _contained(path: Path, roots: tuple[Path, ...]) -> bool:
        resolved = Path(path).resolve(strict=False)
        for root in roots:
            try:
                resolved.relative_to(root)
                return True
            except ValueError:
                continue
        return False


class DefaultAppEngineRuntime(AppEngineRuntime):
    """One in-process kernel shared by standalone hosts and Atrium."""

    def __init__(
        self,
        *,
        host: HostAdapter,
        subject: HostSubject,
        runtime_root: Path,
        catalog_key: CatalogKey = CatalogKey("default"),
        providers: ProviderRegistry | None = None,
    ) -> None:
        self.host = host
        self.subject = subject
        self.catalog_key = catalog_key
        self.providers = providers or ProviderRegistry()
        self.catalog = ImmutableAppCatalog()
        self.lifecycle = ProcessLifecycleManager(
            catalog=self.catalog,
            catalog_key=catalog_key,
            host=host,
            runtime_root=runtime_root,
        )
        self.gateway = DefaultAppGateway(
            catalog=self.catalog,
            lifecycle=self.lifecycle,
            catalog_key=catalog_key,
        )
        self.studio = PortableStudioService(
            host=host,
            catalog=self.catalog,
            catalog_key=catalog_key,
            providers=self.providers,
        )
        self._started = False
        self._closed = False

    async def start(self) -> None:
        if self._started:
            return
        if self._closed:
            raise RuntimeError("app-engine runtime is closed")
        await self.catalog.configure(
            self.catalog_key, await self.host.sources(self.subject)
        )
        self._started = True
        for app in self.catalog.snapshot(self.catalog_key).apps:
            for target in app.manifest.targets:
                if target.runtime is None or not target.runtime.autostart:
                    continue
                request = LaunchRequest(
                    app_id=app.manifest.app_id,
                    target_id=target.target_id,
                    subject=self.subject,
                )
                plan = await self.lifecycle.preview(request)
                if not plan.approval_required:
                    await self.lifecycle.launch(request, approval=None)

    async def close(self, grace_seconds: float) -> ShutdownReport:
        if self._closed:
            return ShutdownReport((), ())
        stopped = await self.gateway.shutdown()
        active = self.lifecycle.active_launch_ids()
        incomplete = tuple(item for item in active if item not in stopped)
        self._closed = True
        return ShutdownReport(stopped_launch_ids=stopped, incomplete_launch_ids=incomplete)

    def diagnostics(self) -> dict[str, object]:
        generation = 0
        if self._started:
            generation = self.catalog.snapshot(self.catalog_key).generation
        return {
            "package_version": __version__,
            "module_path": str(Path(__file__).resolve()),
            "catalog_generation": generation,
            "active_launch_count": len(self.lifecycle.active_launch_ids()),
            "provider_kinds": self.providers.kinds(),
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }


__all__ = ["DefaultAppEngineRuntime", "LocalHostAdapter"]
