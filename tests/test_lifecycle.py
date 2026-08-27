"""Behavioral contract for the managed-process lifecycle supervisor.

These tests intentionally use real, short-lived Python child processes.  The
only fakes are the host boundary and the immutable catalog boundary; process
execution, HTTP readiness, log capture, cache invalidation, and shutdown are
observed end to end.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import urllib.request
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app_engine.contracts import (
    AppCatalog,
    AppId,
    AppManifest,
    AppMetadata,
    AppSource,
    AppStateStore,
    ApprovalReceipt,
    Authorization,
    AuthorizationError,
    BrowserPolicy,
    CacheStatus,
    CatalogApp,
    CatalogEvent,
    CatalogKey,
    CatalogSnapshot,
    CommandStep,
    ConfigurationField,
    ConfigurationType,
    ConfigurationValue,
    EnvironmentBinding,
    EnvironmentSource,
    HealthCheckError,
    HealthSpec,
    HostAdapter,
    HostCapabilityProviders,
    HostSubject,
    InstanceId,
    LaunchError,
    LaunchHandle,
    LaunchId,
    LaunchRequest,
    ManifestInspection,
    PlanFingerprint,
    ProcessScope,
    ResolvedConfiguration,
    RuntimeSpec,
    RuntimeState,
    StopReason,
    StudioRoot,
    TargetId,
    TargetSpec,
    ToolchainError,
)


class _MemoryState(AppStateStore):
    def __init__(self) -> None:
        self.content = b""

    async def load(self) -> bytes:
        return self.content

    async def save(self, content: bytes) -> None:
        self.content = content


class _NoCapabilities(HostCapabilityProviders):
    def names(self) -> tuple[str, ...]:
        return ()


class _Host(HostAdapter):
    def __init__(self, *, approval_required: bool = False) -> None:
        self.approval_required = approval_required
        self.recorded_events = []

    async def sources(self, subject: HostSubject) -> tuple[AppSource, ...]:
        return ()

    async def configuration(
        self, subject: HostSubject, app_id: AppId
    ) -> ResolvedConfiguration:
        return ResolvedConfiguration(app_id=app_id, values=())

    async def authorize(self, subject: HostSubject, plan) -> Authorization:
        return Authorization(
            allowed=True,
            approval_required=self.approval_required,
            reason="External app commands require approval"
            if self.approval_required
            else "",
        )

    async def record_event(self, event) -> None:
        self.recorded_events.append(event)

    async def resolve_app_state(
        self, subject: HostSubject, app_id: AppId
    ) -> AppStateStore:
        return _MemoryState()

    async def studio_roots(
        self, subject: HostSubject
    ) -> tuple[StudioRoot, ...]:
        return ()

    def capability_providers(self) -> HostCapabilityProviders:
        return _NoCapabilities()


class _Catalog(AppCatalog):
    def __init__(self, manifest: AppManifest) -> None:
        self.key = CatalogKey("test")
        self.current = CatalogSnapshot(
            key=self.key,
            generation=1,
            apps=(
                CatalogApp(
                    manifest=manifest,
                    source=AppSource(
                        kind="test", root=manifest.root.parent, priority=0
                    ),
                    compatible=True,
                ),
            ),
            rejections=(),
            created_at=datetime.now(timezone.utc),
        )

    async def configure(
        self, catalog_key: CatalogKey, sources: tuple[AppSource, ...]
    ) -> CatalogSnapshot:
        return self.current

    def snapshot(self, catalog_key: CatalogKey) -> CatalogSnapshot:
        assert catalog_key == self.key
        return self.current

    async def inspect(self, app_root: Path) -> ManifestInspection:
        raise AssertionError("lifecycle must read the catalog snapshot")

    async def refresh(self, catalog_key: CatalogKey) -> CatalogSnapshot:
        return self.current

    async def events(self, catalog_key: CatalogKey):
        if False:
            yield CatalogEvent(catalog_key, 1, ())


def _manager_type():
    try:
        from app_engine.lifecycle import ProcessLifecycleManager
    except ImportError as exc:
        pytest.fail(
            "app_engine.lifecycle.ProcessLifecycleManager is not implemented: "
            f"{exc}"
        )
    return ProcessLifecycleManager


def _write_server(root: Path) -> None:
    (root / "server.py").write_text(
        """
import json
import os
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

delay = float(os.environ.get("READY_DELAY", "0"))
if delay:
    time.sleep(delay)

record = os.environ.get("ARGV_RECORD")
if record:
    Path(record).write_text(json.dumps(sys.argv[1:]), encoding="utf-8")
port_record = os.environ.get("PORT_RECORD")
if port_record:
    Path(port_record).write_text(os.environ["PORT"], encoding="utf-8")
env_record = os.environ.get("ENV_RECORD")
if env_record:
    Path(env_record).write_text(json.dumps(sorted(os.environ)), encoding="utf-8")
child_record = os.environ.get("CHILD_PID_RECORD")
if child_record:
    child = subprocess.Popen([
        sys.executable,
        "-c",
        "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)",
    ])
    Path(child_record).write_text(str(child.pid), encoding="utf-8")
crash_record = os.environ.get("CRASH_RECORD")
if crash_record and not Path(crash_record).exists():
    Path(crash_record).write_text("first", encoding="utf-8")
    threading.Timer(0.15, lambda: os._exit(7)).start()

for number in range(int(os.environ.get("LOG_LINES", "0"))):
    print(f"stdout-{number}", flush=True)
if os.environ.get("LOG_BYTES"):
    print("x" * int(os.environ["LOG_BYTES"]), flush=True)
if int(os.environ.get("LOG_LINES", "0")):
    time.sleep(0.05)
print("stderr-final", file=sys.stderr, flush=True)

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def log_message(self, format, *args):
        return

server = ThreadingHTTPServer(("127.0.0.1", int(os.environ["PORT"])), Handler)

def stop(signum, frame):
    marker = os.environ.get("STOP_RECORD")
    if marker:
        Path(marker).write_text("stopped", encoding="utf-8")
    threading.Thread(target=server.shutdown, daemon=True).start()

signal.signal(signal.SIGTERM, stop)
server.serve_forever()
server.server_close()
""".lstrip(),
        encoding="utf-8",
    )


def _write_counter(root: Path) -> None:
    (root / "counter.py").write_text(
        """
import sys
from pathlib import Path

counter = Path(sys.argv[1])
value = int(counter.read_text(encoding="utf-8")) if counter.exists() else 0
counter.write_text(str(value + 1), encoding="utf-8")
Path(sys.argv[2]).write_text("prepared", encoding="utf-8")
""".lstrip(),
        encoding="utf-8",
    )


def _manifest(
    root: Path,
    *,
    scope: ProcessScope = ProcessScope.PER_USER,
    start: CommandStep | None = None,
    install: tuple[CommandStep, ...] = (),
    build: tuple[CommandStep, ...] = (),
    health_timeout: float = 2.0,
) -> AppManifest:
    root.mkdir(parents=True, exist_ok=True)
    _write_server(root)
    runtime = RuntimeSpec(
        driver="process",
        scope=scope,
        install=install,
        build=build,
        start=start
        or CommandStep(argv=(sys.executable, "server.py"), cwd="."),
        health=HealthSpec(
            kind="http",
            path="/health",
            timeout_seconds=health_timeout,
            interval_seconds=0.02,
        ),
        idle_timeout_seconds=60,
        autostart=False,
    )
    return AppManifest(
        manifest_version=2,
        app_id=AppId("demo"),
        label="Demo",
        icon="D",
        root=root,
        default_target=TargetId("web"),
        targets=(
            TargetSpec(target_id=TargetId("web"), kind="web", runtime=runtime),
        ),
        configuration=(),
        metadata=AppMetadata(),
        browser=BrowserPolicy(),
    )


def _manager(
    tmp_path: Path,
    manifest: AppManifest,
    *,
    host: _Host | None = None,
    max_log_records: int = 100,
):
    catalog = _Catalog(manifest)
    manager = _manager_type()(
        catalog=catalog,
        catalog_key=catalog.key,
        host=host or _Host(),
        runtime_root=tmp_path / "runtime",
        max_log_records=max_log_records,
        stop_grace_seconds=0.75,
    )
    return manager


def _request(
    *,
    user: str = "alice",
    instance: str | None = "window-1",
    force_rebuild: bool = False,
) -> LaunchRequest:
    return LaunchRequest(
        app_id=AppId("demo"),
        target_id=TargetId("web"),
        subject=HostSubject(subject_id=user, role="user"),
        instance_id=InstanceId(instance) if instance else None,
        force_rebuild=force_rebuild,
    )


async def _stop_distinct(manager, handles: tuple[LaunchHandle, ...]) -> None:
    seen: set[LaunchId] = set()
    for handle in handles:
        if handle.launch_id not in seen:
            seen.add(handle.launch_id)
            await manager.stop(handle.launch_id, StopReason.SHUTDOWN)


@pytest.mark.asyncio
async def test_launch_executes_argv_directly_without_shell_interpretation(tmp_path):
    root = tmp_path / "app"
    argv_record = tmp_path / "argv.json"
    injected = tmp_path / "shell-injected"
    start = CommandStep(
        argv=(
            sys.executable,
            "server.py",
            "$PORT",
            ";",
            "touch",
            str(injected),
        ),
        environment=(
            EnvironmentBinding(
                "ARGV_RECORD", EnvironmentSource.LITERAL, str(argv_record)
            ),
        ),
    )
    manager = _manager(tmp_path, _manifest(root, start=start))

    handle = await manager.launch(_request(), approval=None)
    try:
        assert handle.initial_state is RuntimeState.READY
        assert json.loads(argv_record.read_text(encoding="utf-8")) == [
            "$PORT",
            ";",
            "touch",
            str(injected),
        ]
    finally:
        await manager.stop(handle.launch_id, StopReason.USER)

    assert not injected.exists()


@pytest.mark.asyncio
async def test_preview_rejects_cwd_that_escapes_the_app_root(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    start = CommandStep(argv=(sys.executable, "server.py"), cwd="../outside")
    manager = _manager(tmp_path, _manifest(tmp_path / "app", start=start))

    with pytest.raises(LaunchError) as raised:
        await manager.preview(_request())

    assert raised.value.code
    assert raised.value.remediation
    assert "cwd" in raised.value.message.lower()


@pytest.mark.asyncio
async def test_preview_rejects_relative_executable_that_escapes_app_root(tmp_path):
    outside_tool = tmp_path / "outside-tool"
    outside_tool.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    outside_tool.chmod(0o755)
    start = CommandStep(argv=("../outside-tool",), cwd=".")
    manager = _manager(tmp_path, _manifest(tmp_path / "app", start=start))

    with pytest.raises(LaunchError) as raised:
        await manager.preview(_request())

    assert raised.value.code
    assert raised.value.remediation
    assert "executable" in raised.value.message.lower()


@pytest.mark.asyncio
async def test_web_launch_assigns_a_free_port_and_reports_ready_endpoint(tmp_path):
    port_record = tmp_path / "port"
    start = CommandStep(
        argv=(sys.executable, "server.py"),
        environment=(
            EnvironmentBinding(
                "PORT_RECORD", EnvironmentSource.LITERAL, str(port_record)
            ),
        ),
    )
    manager = _manager(tmp_path, _manifest(tmp_path / "app", start=start))

    handle = await manager.launch(_request(), approval=None)
    try:
        status = await manager.status(handle.launch_id)
        assert handle.initial_state is RuntimeState.READY
        assert status.state is RuntimeState.READY
        assert status.endpoint is not None
        assert status.endpoint.host in {"127.0.0.1", "localhost", "::1"}
        assert status.endpoint.port > 0
        assert int(port_record.read_text(encoding="utf-8")) == status.endpoint.port
        response = await asyncio.to_thread(
            urllib.request.urlopen,
            f"http://127.0.0.1:{status.endpoint.port}/health",
            None,
            1,
        )
        assert response.read() == b"ok"
    finally:
        await manager.stop(handle.launch_id, StopReason.USER)


@pytest.mark.asyncio
async def test_install_and_build_cache_is_invalidated_by_input_content(tmp_path):
    root = tmp_path / "app"
    root.mkdir()
    _write_counter(root)
    (root / "source.txt").write_text("one", encoding="utf-8")
    install = CommandStep(
        argv=(sys.executable, "counter.py", "install.count", "install.out"),
        step_id="install",
        inputs=("source.txt",),
        outputs=("install.out",),
    )
    build = CommandStep(
        argv=(sys.executable, "counter.py", "build.count", "build.out"),
        step_id="build",
        inputs=("source.txt",),
        outputs=("build.out",),
    )
    manager = _manager(
        tmp_path,
        _manifest(root, install=(install,), build=(build,)),
    )

    first_plan = await manager.preview(_request())
    assert first_plan.cache_status is CacheStatus.MISS
    first = await manager.launch(_request(), approval=None)
    await manager.stop(first.launch_id, StopReason.USER)

    cached_plan = await manager.preview(_request())
    assert cached_plan.cache_status is CacheStatus.HIT
    second = await manager.launch(_request(), approval=None)
    await manager.stop(second.launch_id, StopReason.USER)
    assert (root / "install.count").read_text(encoding="utf-8") == "1"
    assert (root / "build.count").read_text(encoding="utf-8") == "1"

    (root / "source.txt").write_text("two", encoding="utf-8")
    changed_plan = await manager.preview(_request())
    assert changed_plan.fingerprint != cached_plan.fingerprint
    assert changed_plan.cache_status in {CacheStatus.DIRTY, CacheStatus.MISS}
    third = await manager.launch(_request(), approval=None)
    await manager.stop(third.launch_id, StopReason.USER)
    assert (root / "install.count").read_text(encoding="utf-8") == "2"
    assert (root / "build.count").read_text(encoding="utf-8") == "2"


@pytest.mark.asyncio
async def test_shared_scope_reuses_one_warm_process_across_users(tmp_path):
    manager = _manager(
        tmp_path,
        _manifest(tmp_path / "app", scope=ProcessScope.SHARED),
    )
    first = await manager.launch(_request(user="alice"), approval=None)
    second = await manager.launch(_request(user="bob"), approval=None)
    try:
        assert first.launch_id == second.launch_id
    finally:
        await _stop_distinct(manager, (first, second))


@pytest.mark.asyncio
async def test_per_user_scope_reuses_by_user_but_isolates_users(tmp_path):
    manager = _manager(
        tmp_path,
        _manifest(tmp_path / "app", scope=ProcessScope.PER_USER),
    )
    first = await manager.launch(
        _request(user="alice", instance="window-1"), approval=None
    )
    same_user = await manager.launch(
        _request(user="alice", instance="window-2"), approval=None
    )
    other_user = await manager.launch(_request(user="bob"), approval=None)
    try:
        assert first.launch_id == same_user.launch_id
        assert other_user.launch_id != first.launch_id
    finally:
        await _stop_distinct(manager, (first, same_user, other_user))


@pytest.mark.asyncio
async def test_per_instance_scope_reuses_only_the_same_instance(tmp_path):
    manager = _manager(
        tmp_path,
        _manifest(tmp_path / "app", scope=ProcessScope.PER_INSTANCE),
    )
    first = await manager.launch(_request(instance="window-1"), approval=None)
    same_instance = await manager.launch(
        _request(instance="window-1"), approval=None
    )
    other_instance = await manager.launch(
        _request(instance="window-2"), approval=None
    )
    try:
        assert first.launch_id == same_instance.launch_id
        assert other_instance.launch_id != first.launch_id
    finally:
        await _stop_distinct(manager, (first, same_instance, other_instance))


@pytest.mark.asyncio
async def test_approval_is_bound_to_exact_plan_fingerprint_and_subject(tmp_path):
    port_record = tmp_path / "started"
    start = CommandStep(
        argv=(sys.executable, "server.py"),
        environment=(
            EnvironmentBinding(
                "PORT_RECORD", EnvironmentSource.LITERAL, str(port_record)
            ),
        ),
    )
    host = _Host(approval_required=True)
    manager = _manager(
        tmp_path,
        _manifest(tmp_path / "app", start=start),
        host=host,
    )
    request = _request(user="alice")
    plan = await manager.preview(request)
    assert plan.approval_required is True

    stale = ApprovalReceipt(
        fingerprint=PlanFingerprint("stale"),
        subject_id="alice",
        approved_at=datetime.now(timezone.utc),
    )
    with pytest.raises(AuthorizationError):
        await manager.launch(request, stale)
    wrong_subject = ApprovalReceipt(
        fingerprint=plan.fingerprint,
        subject_id="bob",
        approved_at=datetime.now(timezone.utc),
    )
    with pytest.raises(AuthorizationError):
        await manager.launch(request, wrong_subject)
    assert not port_record.exists()

    accepted = ApprovalReceipt(
        fingerprint=plan.fingerprint,
        subject_id="alice",
        approved_at=datetime.now(timezone.utc),
    )
    handle = await manager.launch(request, accepted)
    try:
        assert handle.initial_state is RuntimeState.READY
        assert port_record.exists()
    finally:
        await manager.stop(handle.launch_id, StopReason.USER)


@pytest.mark.asyncio
async def test_logs_are_bounded_and_preserve_stream_identity(tmp_path):
    start = CommandStep(
        argv=(sys.executable, "server.py"),
        environment=(
            EnvironmentBinding("LOG_LINES", EnvironmentSource.LITERAL, "10"),
        ),
    )
    manager = _manager(
        tmp_path,
        _manifest(tmp_path / "app", start=start),
        max_log_records=5,
    )

    handle = await manager.launch(_request(), approval=None)
    try:
        page = await manager.logs(handle.launch_id, after=None, limit=100)
        assert len(page.records) <= 5
        assert any(record.text.strip() == "stdout-9" for record in page.records)
        assert any(
            record.stream == "stderr" and record.text.strip() == "stderr-final"
            for record in page.records
        )
        assert all(record.text.strip() != "stdout-0" for record in page.records)
        assert tuple(record.sequence for record in page.records) == tuple(
            sorted(record.sequence for record in page.records)
        )
    finally:
        await manager.stop(handle.launch_id, StopReason.USER)


@pytest.mark.asyncio
async def test_stop_is_graceful_and_idempotent(tmp_path):
    stop_record = tmp_path / "stop-record"
    start = CommandStep(
        argv=(sys.executable, "server.py"),
        environment=(
            EnvironmentBinding(
                "STOP_RECORD", EnvironmentSource.LITERAL, str(stop_record)
            ),
        ),
    )
    manager = _manager(tmp_path, _manifest(tmp_path / "app", start=start))
    handle = await manager.launch(_request(), approval=None)

    result = await manager.stop(handle.launch_id, StopReason.USER)
    repeated = await manager.stop(handle.launch_id, StopReason.USER)

    assert result.stopped is True
    assert result.exit_code == 0
    assert repeated.launch_id == handle.launch_id
    assert stop_record.read_text(encoding="utf-8") == "stopped"
    assert (await manager.status(handle.launch_id)).state is RuntimeState.STOPPED


@pytest.mark.asyncio
async def test_health_timeout_has_stable_actionable_failure(tmp_path):
    start = CommandStep(
        argv=(sys.executable, "server.py"),
        environment=(
            EnvironmentBinding("READY_DELAY", EnvironmentSource.LITERAL, "2"),
        ),
    )
    manager = _manager(
        tmp_path,
        _manifest(tmp_path / "app", start=start, health_timeout=0.15),
    )

    with pytest.raises(HealthCheckError) as raised:
        await manager.launch(_request(), approval=None)

    assert raised.value.code
    assert raised.value.remediation
    assert "health" in raised.value.message.lower()


@pytest.mark.asyncio
async def test_missing_executable_has_toolchain_remediation(tmp_path):
    start = CommandStep(argv=("definitely-not-an-app-engine-toolchain",), cwd=".")
    manager = _manager(tmp_path, _manifest(tmp_path / "app", start=start))

    with pytest.raises(ToolchainError) as raised:
        await manager.preview(_request())

    assert raised.value.code
    assert raised.value.remediation
    assert "executable" in raised.value.message.lower()


def _replace_runtime(manifest: AppManifest, **changes) -> AppManifest:
    target = manifest.targets[0]
    assert target.runtime is not None
    return replace(
        manifest,
        targets=(replace(target, runtime=replace(target.runtime, **changes)),),
    )


class _ConfigHost(_Host):
    def __init__(self, values: tuple[ConfigurationValue, ...]) -> None:
        super().__init__()
        self.values = values

    async def configuration(
        self, subject: HostSubject, app_id: AppId
    ) -> ResolvedConfiguration:
        return ResolvedConfiguration(app_id=app_id, values=self.values)


class _BrokenEventHost(_Host):
    async def record_event(self, event) -> None:
        raise RuntimeError("event sink unavailable")


@pytest.mark.asyncio
async def test_child_environment_does_not_inherit_undeclared_host_secrets(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("APP_ENGINE_PARENT_SECRET", "must-not-leak")
    record = tmp_path / "environment.json"
    start = CommandStep(
        argv=(sys.executable, "server.py"),
        environment=(
            EnvironmentBinding(
                "ENV_RECORD", EnvironmentSource.LITERAL, str(record)
            ),
        ),
    )
    manager = _manager(tmp_path, _manifest(tmp_path / "app", start=start))

    handle = await manager.launch(_request(), approval=None)
    try:
        environment_keys = json.loads(record.read_text(encoding="utf-8"))
        assert "APP_ENGINE_PARENT_SECRET" not in environment_keys
        assert "APP_ID" in environment_keys
    finally:
        await manager.stop(handle.launch_id, StopReason.USER)


@pytest.mark.asyncio
async def test_fingerprint_uses_declared_nonsecret_configuration_only(tmp_path):
    fields = (
        ConfigurationField(
            "endpoint", ConfigurationType.STRING, "Endpoint", required=True
        ),
        ConfigurationField(
            "token", ConfigurationType.SECRET, "Token", required=True
        ),
    )
    manifest = replace(_manifest(tmp_path / "app"), configuration=fields)
    host = _ConfigHost(
        (
            ConfigurationValue("endpoint", "https://one.example"),
            ConfigurationValue("token", "first-secret", secret=True),
            ConfigurationValue("unrelated", "first-extra"),
        )
    )
    manager = _manager(tmp_path, manifest, host=host)

    first = await manager.preview(_request())
    host.values = (
        ConfigurationValue("endpoint", "https://one.example"),
        ConfigurationValue("token", "different-secret", secret=True),
        ConfigurationValue("unrelated", "different-extra"),
    )
    opaque = await manager.preview(_request())
    assert opaque.fingerprint == first.fingerprint

    host.values = (
        ConfigurationValue("endpoint", "https://two.example"),
        ConfigurationValue("token", "different-secret", secret=True),
    )
    changed = await manager.preview(_request())
    assert changed.fingerprint != first.fingerprint


@pytest.mark.asyncio
async def test_warm_reuse_requires_exact_plan_and_force_rebuild_never_reuses(
    tmp_path,
):
    root = tmp_path / "app"
    manifest = _manifest(root)
    (root / "source.txt").write_text("one", encoding="utf-8")
    target = manifest.targets[0]
    assert target.runtime is not None
    start = replace(target.runtime.start, inputs=("source.txt",))
    manifest = _replace_runtime(manifest, start=start)
    manager = _manager(tmp_path, manifest)

    first = await manager.launch(_request(), approval=None)
    (root / "source.txt").write_text("two", encoding="utf-8")
    second = await manager.launch(_request(), approval=None)
    forced = await manager.launch(
        _request(force_rebuild=True), approval=None
    )
    try:
        assert second.launch_id != first.launch_id
        assert forced.launch_id != second.launch_id
        assert (await manager.status(first.launch_id)).state is RuntimeState.STOPPED
        assert (await manager.status(second.launch_id)).state is RuntimeState.STOPPED
    finally:
        await manager.stop(forced.launch_id, StopReason.USER)


@pytest.mark.asyncio
async def test_concurrent_forced_launches_leave_newest_launch_active(tmp_path):
    manager = _manager(tmp_path, _manifest(tmp_path / "app"))
    first, second = await asyncio.gather(
        manager.launch(_request(force_rebuild=True), approval=None),
        manager.launch(_request(force_rebuild=True), approval=None),
    )
    latest = await manager.launch(_request(), approval=None)
    try:
        assert first.launch_id != second.launch_id
        assert latest.launch_id == second.launch_id
        assert (await manager.status(first.launch_id)).state is RuntimeState.STOPPED
    finally:
        await manager.stop(latest.launch_id, StopReason.USER)


@pytest.mark.asyncio
async def test_event_sink_failure_does_not_break_supervision(tmp_path):
    manager = _manager(
        tmp_path,
        _manifest(tmp_path / "app"),
        host=_BrokenEventHost(),
    )
    handle = await manager.launch(_request(), approval=None)
    result = await manager.stop(handle.launch_id, StopReason.USER)
    assert result.stopped is True


@pytest.mark.asyncio
async def test_nested_input_symlink_cannot_escape_app_root(tmp_path):
    root = tmp_path / "app"
    manifest = _manifest(root)
    nested = root / "inputs" / "nested"
    nested.mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    (nested / "escape.txt").symlink_to(outside)
    target = manifest.targets[0]
    assert target.runtime is not None
    start = replace(target.runtime.start, inputs=("inputs",))
    manager = _manager(tmp_path, _replace_runtime(manifest, start=start))

    with pytest.raises(LaunchError) as raised:
        await manager.preview(_request())
    assert "input" in raised.value.message.lower()


@pytest.mark.asyncio
async def test_log_capture_is_byte_bounded_for_oversized_lines(tmp_path):
    start = CommandStep(
        argv=(sys.executable, "server.py"),
        environment=(
            EnvironmentBinding("LOG_BYTES", EnvironmentSource.LITERAL, "200000"),
        ),
    )
    manifest = _manifest(tmp_path / "app", start=start)
    catalog = _Catalog(manifest)
    manager = _manager_type()(
        catalog=catalog,
        catalog_key=catalog.key,
        host=_Host(),
        runtime_root=tmp_path / "runtime",
        max_log_records=100,
        max_log_bytes=4096,
        stop_grace_seconds=0.75,
    )

    handle = await manager.launch(_request(), approval=None)
    try:
        page = await manager.logs(handle.launch_id, after=None, limit=100)
        assert sum(len(item.text.encode("utf-8")) for item in page.records) <= 4096
    finally:
        await manager.stop(handle.launch_id, StopReason.USER)


@pytest.mark.asyncio
async def test_stop_kills_descendants_after_process_leader_exits(tmp_path):
    child_record = tmp_path / "child.pid"
    start = CommandStep(
        argv=(sys.executable, "server.py"),
        environment=(
            EnvironmentBinding(
                "CHILD_PID_RECORD", EnvironmentSource.LITERAL, str(child_record)
            ),
        ),
    )
    manager = _manager(tmp_path, _manifest(tmp_path / "app", start=start))
    handle = await manager.launch(_request(), approval=None)
    child_pid = int(child_record.read_text(encoding="utf-8"))

    await asyncio.wait_for(
        manager.stop(handle.launch_id, StopReason.USER), timeout=2.0
    )
    for _ in range(50):
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.02)
    else:
        pytest.fail("descendant survived process-group shutdown")


@pytest.mark.asyncio
async def test_ready_process_crash_restarts_once_with_same_launch_id(tmp_path):
    crash_record = tmp_path / "crashed"
    start = CommandStep(
        argv=(sys.executable, "server.py"),
        environment=(
            EnvironmentBinding(
                "CRASH_RECORD", EnvironmentSource.LITERAL, str(crash_record)
            ),
        ),
    )
    host = _Host()
    manager = _manager(
        tmp_path, _manifest(tmp_path / "app", start=start), host=host
    )
    handle = await manager.launch(_request(), approval=None)
    try:
        for _ in range(100):
            events = [
                event
                for event in host.recorded_events
                if event.launch_id == handle.launch_id
            ]
            if sum(event.state is RuntimeState.READY for event in events) >= 2:
                break
            await asyncio.sleep(0.02)
        else:
            pytest.fail("process did not perform its bounded crash restart")
        status = await manager.status(handle.launch_id)
        assert status.state is RuntimeState.READY
    finally:
        await manager.stop(handle.launch_id, StopReason.USER)


@pytest.mark.asyncio
async def test_idle_timeout_begins_only_after_launch_lease_release(tmp_path):
    manifest = _replace_runtime(
        _manifest(tmp_path / "app"), idle_timeout_seconds=0.1
    )
    manager = _manager(tmp_path, manifest)
    handle = await manager.launch(_request(), approval=None)

    await asyncio.sleep(0.15)
    assert (await manager.status(handle.launch_id)).state is RuntimeState.READY
    await manager.retain(handle.launch_id)
    await manager.release(handle.launch_id)

    for _ in range(100):
        if (await manager.status(handle.launch_id)).state is RuntimeState.STOPPED:
            break
        await asyncio.sleep(0.02)
    else:
        pytest.fail("idle process was not stopped")


@pytest.mark.asyncio
async def test_retained_launch_does_not_idle_until_last_release(tmp_path):
    manifest = _replace_runtime(
        _manifest(tmp_path / "app"), idle_timeout_seconds=0.05
    )
    manager = _manager(tmp_path, manifest)
    handle = await manager.launch(_request(), approval=None)
    await manager.retain(handle.launch_id)
    await manager.retain(handle.launch_id)

    await asyncio.sleep(0.08)
    assert (await manager.status(handle.launch_id)).state is RuntimeState.READY
    await manager.release(handle.launch_id)
    await asyncio.sleep(0.08)
    assert (await manager.status(handle.launch_id)).state is RuntimeState.READY

    await manager.release(handle.launch_id)
    for _ in range(100):
        if (await manager.status(handle.launch_id)).state is RuntimeState.STOPPED:
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("released process was not stopped after its idle timeout")


@pytest.mark.asyncio
async def test_idle_stop_retries_when_first_cleanup_is_incomplete(
    tmp_path, monkeypatch
):
    manifest = _replace_runtime(
        _manifest(tmp_path / "app"), idle_timeout_seconds=0.03
    )
    manager = _manager(tmp_path, manifest)
    original_terminate = manager._terminate_process
    attempts = 0

    async def flaky_terminate(record):
        nonlocal attempts
        attempts += 1
        if attempts > 1:
            await original_terminate(record)

    monkeypatch.setattr(manager, "_terminate_process", flaky_terminate)
    handle = await manager.launch(_request(), approval=None)
    await manager.retain(handle.launch_id)
    await manager.release(handle.launch_id)

    for _ in range(100):
        if (await manager.status(handle.launch_id)).state is RuntimeState.STOPPED:
            break
        await asyncio.sleep(0.01)
    else:
        await original_terminate(manager._records[handle.launch_id])
        pytest.fail("incomplete idle cleanup was not retried")

    assert attempts >= 2


@pytest.mark.asyncio
async def test_autostart_waits_for_runtime_composition_to_supply_subject(tmp_path):
    port_record = tmp_path / "should-not-start"
    start = CommandStep(
        argv=(sys.executable, "server.py"),
        environment=(
            EnvironmentBinding(
                "PORT_RECORD", EnvironmentSource.LITERAL, str(port_record)
            ),
        ),
    )
    manifest = _replace_runtime(
        _manifest(tmp_path / "app", start=start), autostart=True
    )
    _manager(tmp_path, manifest)

    await asyncio.sleep(0.05)
    assert not port_record.exists()
