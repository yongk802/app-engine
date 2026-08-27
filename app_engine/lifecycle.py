"""Safe managed-process lifecycle for app-engine web targets.

The supervisor deliberately owns only the portable process boundary.  Host
policy, identity, configuration storage, and approvals remain behind the
``HostAdapter`` contract.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import signal
import socket
import urllib.error
import urllib.request
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import AsyncIterator, Any

from .contracts import (
    AppCatalog,
    AppManifest,
    ApprovalReceipt,
    AuthorizationError,
    CacheStatus,
    CatalogKey,
    CommandStep,
    ConfigurationError,
    ConfigurationType,
    EnvironmentSource,
    HealthCheckError,
    HostAdapter,
    LaunchError,
    LaunchHandle,
    LaunchId,
    LaunchPlan,
    LaunchRequest,
    LaunchStatus,
    LifecycleManager,
    LogPage,
    LogRecord,
    PlanFingerprint,
    PreparationError,
    ProcessScope,
    ResolvedConfiguration,
    ResolvedStep,
    RuntimeEvent,
    RuntimeFailure,
    RuntimeState,
    ScopeKey,
    StopReason,
    StopResult,
    TargetEndpoint,
    TargetId,
    TargetSpec,
    ToolchainError,
)


_SAFE_PARENT_ENVIRONMENT = (
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "TMPDIR",
    "USERPROFILE",
    "WINDIR",
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _json_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


@dataclass
class _LaunchRecord:
    launch_id: LaunchId
    scope_key: ScopeKey
    fingerprint: PlanFingerprint
    plan: LaunchPlan
    manifest: AppManifest
    target: TargetSpec
    configuration: ResolvedConfiguration
    process: asyncio.subprocess.Process | None = None
    process_group_id: int | None = None
    state: RuntimeState = RuntimeState.STARTING
    endpoint: TargetEndpoint | None = None
    started_at: datetime | None = None
    failure: RuntimeFailure | None = None
    logs: deque[LogRecord] = field(default_factory=deque)
    events: list[RuntimeEvent] = field(default_factory=list)
    log_sequence: int = 0
    log_bytes: int = 0
    event_sequence: int = 0
    restart_count: int = 0
    leases: int = 0
    idle_armed: bool = False
    stream_tasks: list[asyncio.Task[None]] = field(default_factory=list)
    watcher: asyncio.Task[None] | None = None
    idle_task: asyncio.Task[None] | None = None
    changed: asyncio.Condition = field(default_factory=asyncio.Condition)
    stop_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class ProcessLifecycleManager(LifecycleManager):
    """Prepare, launch, reuse, observe, and stop loopback app processes.

    Catalog-wide ``autostart`` is intentionally a composition concern: this
    request-scoped manager has no host subject with which to authorize or
    configure an app until the embedding runtime supplies one.
    """

    def __init__(
        self,
        *,
        catalog: AppCatalog,
        catalog_key: CatalogKey,
        host: HostAdapter,
        runtime_root: Path,
        max_log_records: int = 1000,
        max_log_bytes: int = 1024 * 1024,
        stop_grace_seconds: float = 5.0,
        max_crash_restarts: int = 1,
    ) -> None:
        if max_log_records < 1:
            raise ValueError("max_log_records must be positive")
        if stop_grace_seconds <= 0:
            raise ValueError("stop_grace_seconds must be positive")
        if max_log_bytes < 1:
            raise ValueError("max_log_bytes must be positive")
        if max_crash_restarts < 0:
            raise ValueError("max_crash_restarts cannot be negative")
        self._catalog = catalog
        self._catalog_key = catalog_key
        self._host = host
        self._runtime_root = runtime_root.resolve(strict=False)
        self._max_log_records = max_log_records
        self._max_log_bytes = max_log_bytes
        self._max_log_record_bytes = min(64 * 1024, max_log_bytes)
        self._stop_grace_seconds = stop_grace_seconds
        self._max_crash_restarts = max_crash_restarts
        self._records: dict[LaunchId, _LaunchRecord] = {}
        self._active: dict[ScopeKey, LaunchId] = {}
        self._scope_locks: dict[ScopeKey, asyncio.Lock] = {}
        self._prepare_locks: dict[PlanFingerprint, asyncio.Lock] = {}
        self._prepared: set[PlanFingerprint] = set()
        self._latest_fingerprint: dict[tuple[str, str], PlanFingerprint] = {}

    async def preview(self, request: LaunchRequest) -> LaunchPlan:
        manifest, target = self._resolve_target(request)
        runtime = target.runtime
        if runtime is None or runtime.driver != "process":
            raise LaunchError(
                "unsupported_runtime",
                "The selected target does not declare a managed process runtime.",
                remediation="Choose a target with a process runtime provider.",
            )

        scope_key = self._scope_key(request, target.target_id, runtime.scope)
        configuration = await self._host.configuration(
            request.subject, manifest.app_id
        )
        if configuration.missing_required:
            raise ConfigurationError(
                "missing_configuration",
                "Required app configuration is missing.",
                remediation="Configure the required fields before launching the app.",
                context=(("app_id", str(manifest.app_id)),),
            )

        steps: list[ResolvedStep] = []
        for phase, commands in (
            ("install", runtime.install),
            ("build", runtime.build),
            ("start", (runtime.start,)),
        ):
            for command in commands:
                cwd = self._resolve_cwd(manifest.root, command)
                executable = self._resolve_executable(manifest.root, cwd, command)
                self._validate_declared_paths(manifest.root, command)
                steps.append(
                    ResolvedStep(
                        phase=phase,
                        command=command,
                        executable=executable,
                        cwd=cwd,
                    )
                )

        fingerprint = self._fingerprint(
            manifest, target, scope_key, configuration, tuple(steps)
        )
        cache_key = (str(manifest.app_id), str(target.target_id))
        if request.force_rebuild:
            cache_status = CacheStatus.DIRTY
        elif fingerprint in self._prepared and self._outputs_exist(
            manifest.root, tuple(steps)
        ):
            cache_status = CacheStatus.HIT
        elif cache_key in self._latest_fingerprint:
            cache_status = CacheStatus.DIRTY
        else:
            cache_status = CacheStatus.MISS

        provisional = LaunchPlan(
            request=request,
            fingerprint=fingerprint,
            scope_key=scope_key,
            steps=tuple(steps),
            required_configuration=manifest.configuration,
            requested_capabilities=manifest.browser.permissions,
            cache_status=cache_status,
            approval_required=False,
        )
        authorization = await self._host.authorize(request.subject, provisional)
        if not authorization.allowed:
            raise AuthorizationError(
                "launch_denied",
                authorization.reason or "The host denied this launch plan.",
                remediation="Review host policy or choose a permitted app.",
            )
        return LaunchPlan(
            request=request,
            fingerprint=fingerprint,
            scope_key=scope_key,
            steps=tuple(steps),
            required_configuration=manifest.configuration,
            requested_capabilities=manifest.browser.permissions,
            cache_status=cache_status,
            approval_required=authorization.approval_required,
        )

    async def launch(
        self, request: LaunchRequest, approval: ApprovalReceipt | None
    ) -> LaunchHandle:
        plan = await self.preview(request)
        self._validate_approval(plan, request, approval)
        lock = self._scope_locks.setdefault(plan.scope_key, asyncio.Lock())
        async with lock:
            # Re-preview under the single-flight lock so filesystem content and
            # approval binding cannot change between authorization and spawn.
            plan = await self.preview(request)
            self._validate_approval(plan, request, approval)
            existing_id = self._active.get(plan.scope_key)
            if existing_id is not None:
                existing = self._records.get(existing_id)
                if existing is not None:
                    async with existing.stop_lock:
                        if (
                            existing.state is RuntimeState.READY
                            and existing.process is not None
                            and existing.process.returncode is None
                            and existing.fingerprint == plan.fingerprint
                            and not request.force_rebuild
                        ):
                            self._disarm_idle(existing)
                            return LaunchHandle(
                                launch_id=existing.launch_id,
                                scope_key=existing.scope_key,
                                initial_state=existing.state,
                            )
                if existing is not None:
                    await self.stop(existing.launch_id, StopReason.RESTART)

            manifest, target = self._resolve_target(request)
            configuration = await self._host.configuration(
                request.subject, manifest.app_id
            )
            await self._prepare(plan, manifest, configuration)

            launch_id = LaunchId(uuid.uuid4().hex)
            record = _LaunchRecord(
                launch_id=launch_id,
                scope_key=plan.scope_key,
                fingerprint=plan.fingerprint,
                plan=plan,
                manifest=manifest,
                target=target,
                configuration=configuration,
            )
            self._records[launch_id] = record
            self._active[plan.scope_key] = launch_id
            await self._transition(record, RuntimeState.STARTING, "Starting app")
            try:
                await self._spawn(record, plan, manifest, target, configuration)
                await self._wait_for_health(record, target)
            except asyncio.CancelledError:
                await self._terminate_process(record)
                await self._drain_streams(record)
                await self._fail(
                    record,
                    RuntimeFailure(
                        "launch_cancelled",
                        "The app launch was cancelled.",
                        "Launch the app again when ready.",
                    ),
                )
                self._remove_active(record)
                raise
            except (HealthCheckError, LaunchError):
                await self._terminate_process(record)
                failure = RuntimeFailure(
                    "health_check_failed",
                    "The app did not satisfy its startup health contract.",
                    "Check the app logs and health endpoint, then retry.",
                )
                await self._fail(record, failure)
                self._remove_active(record)
                raise

            await self._transition(record, RuntimeState.READY, "App is ready")
            record.watcher = asyncio.create_task(self._watch(record))
            return LaunchHandle(
                launch_id=record.launch_id,
                scope_key=record.scope_key,
                initial_state=RuntimeState.READY,
            )

    async def stop(self, launch_id: LaunchId, reason: StopReason) -> StopResult:
        record = self._record(launch_id)
        async with record.stop_lock:
            return await self._stop_locked(record, reason)

    async def retain(self, launch_id: LaunchId) -> None:
        """Acquire a lease that suppresses idling for one exact launch."""
        record = self._record(launch_id)
        async with record.stop_lock:
            if record.state is not RuntimeState.READY:
                raise LaunchError(
                    "launch_not_ready",
                    "Only a ready launch can be retained.",
                    remediation="Launch the app again and retain its new identifier.",
                    context=(("launch_id", str(launch_id)),),
                )
            record.leases += 1
            record.idle_armed = False
            idle_task = record.idle_task
            record.idle_task = None
            if idle_task is not None and idle_task is not asyncio.current_task():
                idle_task.cancel()
                await asyncio.gather(idle_task, return_exceptions=True)

    async def release(self, launch_id: LaunchId) -> None:
        """Release one lease and apply idle policy when none remain."""
        record = self._record(launch_id)
        async with record.stop_lock:
            if record.leases <= 0:
                raise LaunchError(
                    "launch_not_retained",
                    "The launch has no session lease to release.",
                    context=(("launch_id", str(launch_id)),),
                )
            record.leases -= 1
            if record.leases == 0 and record.state is RuntimeState.READY:
                record.idle_armed = True
                self._schedule_idle(record)

    async def _stop_locked(
        self, record: _LaunchRecord, reason: StopReason
    ) -> StopResult:
        process = record.process
        if record.state is RuntimeState.STOPPED:
            return StopResult(
                launch_id=record.launch_id,
                stopped=True,
                exit_code=process.returncode if process else None,
            )
        idle_task = record.idle_task
        if idle_task is not None and idle_task is not asyncio.current_task():
            idle_task.cancel()
        if record.state is not RuntimeState.FAILED:
            await self._transition(record, RuntimeState.STOPPING, reason.value)
        await self._terminate_process(record)
        await self._drain_streams(record)
        stopped = process is None or process.returncode is not None
        if not stopped:
            if record.state is not RuntimeState.FAILED:
                await self._transition(
                    record,
                    RuntimeState.READY,
                    "Process cleanup incomplete; retaining launch for retry",
                )
            return StopResult(record.launch_id, False, None)
        if record.state is not RuntimeState.FAILED:
            await self._transition(record, RuntimeState.STOPPED, reason.value)
        record.idle_armed = False
        self._remove_active(record)
        return StopResult(
            launch_id=record.launch_id,
            stopped=True,
            exit_code=process.returncode if process else None,
        )

    async def status(self, launch_id: LaunchId) -> LaunchStatus:
        record = self._record(launch_id)
        return LaunchStatus(
            launch_id=record.launch_id,
            state=record.state,
            endpoint=record.endpoint,
            started_at=record.started_at,
            failure=record.failure,
        )

    async def events(
        self, launch_id: LaunchId, after: int | None = None
    ) -> AsyncIterator[RuntimeEvent]:
        record = self._record(launch_id)
        cursor = after or 0
        while True:
            available = tuple(
                event for event in record.events if event.sequence > cursor
            )
            for event in available:
                cursor = event.sequence
                yield event
            if record.state in {RuntimeState.STOPPED, RuntimeState.FAILED}:
                return
            async with record.changed:
                await record.changed.wait_for(
                    lambda: any(e.sequence > cursor for e in record.events)
                    or record.state in {RuntimeState.STOPPED, RuntimeState.FAILED}
                )

    async def logs(
        self, launch_id: LaunchId, after: int | None, limit: int
    ) -> LogPage:
        record = self._record(launch_id)
        if limit < 1:
            return LogPage(records=(), next_cursor=after)
        cursor = after or 0
        selected = tuple(
            item for item in record.logs if item.sequence > cursor
        )[:limit]
        next_cursor = selected[-1].sequence if selected else after
        return LogPage(records=selected, next_cursor=next_cursor)

    def _resolve_target(
        self, request: LaunchRequest
    ) -> tuple[AppManifest, TargetSpec]:
        snapshot = self._catalog.snapshot(self._catalog_key)
        app = next(
            (item for item in snapshot.apps if item.manifest.app_id == request.app_id),
            None,
        )
        if app is None:
            raise LaunchError(
                "app_not_found",
                f"App {request.app_id!s} is not in the current catalog.",
                remediation="Refresh the app catalog and try again.",
            )
        target_id = request.target_id or app.manifest.default_target
        target = next(
            (item for item in app.manifest.targets if item.target_id == target_id),
            None,
        )
        if target is None:
            raise LaunchError(
                "target_not_found",
                f"Target {target_id!s} is not declared by the app.",
                remediation="Choose one of the app's declared targets.",
            )
        if target.kind != "web":
            raise LaunchError(
                "unsupported_target",
                f"Target kind {target.kind!r} has no registered lifecycle provider.",
                remediation="Install a provider for this target kind.",
            )
        return app.manifest, target

    @staticmethod
    def _scope_key(
        request: LaunchRequest, target_id: TargetId, scope: ProcessScope
    ) -> ScopeKey:
        if scope is ProcessScope.SHARED:
            return ScopeKey(request.app_id, target_id, None, None)
        if scope is ProcessScope.PER_USER:
            return ScopeKey(
                request.app_id,
                target_id,
                request.subject.subject_id,
                None,
            )
        if request.instance_id is None:
            raise LaunchError(
                "instance_required",
                "This app requires an instance identifier.",
                remediation="Supply an instance identifier when launching the app.",
            )
        return ScopeKey(
            request.app_id,
            target_id,
            request.subject.subject_id,
            request.instance_id,
        )

    @staticmethod
    def _resolve_cwd(root: Path, command: CommandStep) -> Path:
        resolved_root = root.resolve(strict=True)
        cwd = (resolved_root / command.cwd).resolve(strict=False)
        if not _inside(cwd, resolved_root):
            raise LaunchError(
                "cwd_outside_app",
                "A lifecycle command cwd escapes the app root.",
                remediation="Use a cwd contained within the app directory.",
            )
        if not cwd.is_dir():
            raise LaunchError(
                "cwd_missing",
                "A lifecycle command cwd does not exist.",
                remediation="Create the declared cwd inside the app directory.",
            )
        return cwd

    @staticmethod
    def _resolve_executable(root: Path, cwd: Path, command: CommandStep) -> Path:
        if not command.argv or not command.argv[0]:
            raise ToolchainError(
                "missing_executable",
                "A lifecycle command has no executable.",
                remediation="Declare an argv array whose first item is an executable.",
            )
        declared = command.argv[0]
        candidate = Path(declared)
        if candidate.is_absolute():
            executable = candidate.resolve(strict=False)
        elif "/" in declared or (os.altsep and os.altsep in declared):
            executable = (cwd / candidate).resolve(strict=False)
            if not _inside(executable, root.resolve(strict=True)):
                raise LaunchError(
                    "executable_outside_app",
                    "A relative lifecycle executable escapes the app root.",
                    remediation="Use a contained relative executable or an absolute toolchain path.",
                )
        else:
            found = shutil.which(declared)
            if found is None:
                raise ToolchainError(
                    "executable_not_found",
                    f"Lifecycle executable {declared!r} is unavailable.",
                    remediation="Install the executable and ensure it is available on PATH.",
                )
            executable = Path(found).resolve(strict=False)
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise ToolchainError(
                "executable_unavailable",
                f"Lifecycle executable {declared!r} is not executable.",
                remediation="Install it or grant executable permission, then retry.",
            )
        return executable

    @staticmethod
    def _validate_declared_paths(root: Path, command: CommandStep) -> None:
        resolved_root = root.resolve(strict=True)
        for declared in (*command.inputs, *command.outputs):
            # Validate the non-glob prefix as well as every current match.
            prefix = declared.split("*")[0].split("?")[0].split("[")[0]
            probe = (resolved_root / (prefix or ".")).resolve(strict=False)
            if not _inside(probe, resolved_root):
                raise LaunchError(
                    "declared_path_outside_app",
                    "A lifecycle input or output escapes the app root.",
                    remediation="Use input and output paths contained in the app directory.",
                )
            for match in resolved_root.glob(declared):
                candidates = (match, *match.rglob("*")) if match.is_dir() else (match,)
                if any(
                    not _inside(candidate.resolve(strict=False), resolved_root)
                    for candidate in candidates
                ):
                    raise LaunchError(
                        "declared_path_outside_app",
                        "A lifecycle input or output escapes the app root.",
                        remediation="Remove symlinks or paths that leave the app directory.",
                    )

    @staticmethod
    def _fingerprint(
        manifest: AppManifest,
        target: TargetSpec,
        scope_key: ScopeKey,
        configuration: ResolvedConfiguration,
        steps: tuple[ResolvedStep, ...],
    ) -> PlanFingerprint:
        digest = hashlib.sha256()
        structural = {
            "manifest": asdict(manifest),
            "target": asdict(target),
            "scope": asdict(scope_key),
            "configuration": ProcessLifecycleManager._configuration_fingerprint(
                manifest, configuration, steps
            ),
            "steps": [asdict(step) for step in steps],
        }
        digest.update(
            json.dumps(
                _json_value(structural), sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        )
        root = manifest.root.resolve(strict=True)
        for step in steps:
            for declared in step.command.inputs:
                matches = sorted(root.glob(declared), key=lambda item: str(item))
                digest.update(b"input\0" + declared.encode("utf-8") + b"\0")
                if not matches:
                    digest.update(b"missing\0")
                for match in matches:
                    resolved = match.resolve(strict=False)
                    relative = match.relative_to(root).as_posix()
                    digest.update(relative.encode("utf-8") + b"\0")
                    if resolved.is_file():
                        digest.update(resolved.read_bytes())
                    elif resolved.is_dir():
                        for child in sorted(resolved.rglob("*"), key=lambda item: str(item)):
                            child_resolved = child.resolve(strict=False)
                            if not _inside(child_resolved, root):
                                raise LaunchError(
                                    "declared_path_outside_app",
                                    "A nested lifecycle input escapes the app root.",
                                    remediation="Remove symlinks that leave the app directory.",
                                )
                            if not child_resolved.is_file():
                                continue
                            digest.update(
                                child.relative_to(root).as_posix().encode("utf-8")
                                + b"\0"
                            )
                            digest.update(child_resolved.read_bytes())
        return PlanFingerprint(digest.hexdigest())

    @staticmethod
    def _configuration_fingerprint(
        manifest: AppManifest,
        configuration: ResolvedConfiguration,
        steps: tuple[ResolvedStep, ...],
    ) -> list[dict[str, object]]:
        relevant = {field.key for field in manifest.configuration}
        relevant.update(
            binding.value
            for step in steps
            for binding in step.command.environment
            if binding.source is EnvironmentSource.CONFIGURATION
        )
        secret_keys = {
            field.key
            for field in manifest.configuration
            if field.value_type is ConfigurationType.SECRET
        }
        result: list[dict[str, object]] = []
        for value in sorted(configuration.values, key=lambda item: item.key):
            if value.key not in relevant:
                continue
            secret = value.secret or value.key in secret_keys
            result.append(
                {
                    "key": value.key,
                    # Secret material never enters an approval fingerprint.
                    # The marker still binds the plan to its presence.
                    "value": "<configured-secret>" if secret else value.value,
                    "secret": secret,
                }
            )
        return result

    @staticmethod
    def _outputs_exist(root: Path, steps: tuple[ResolvedStep, ...]) -> bool:
        outputs = [output for step in steps for output in step.command.outputs]
        return all(any(root.glob(declared)) for declared in outputs)

    @staticmethod
    def _validate_approval(
        plan: LaunchPlan,
        request: LaunchRequest,
        approval: ApprovalReceipt | None,
    ) -> None:
        if not plan.approval_required:
            return
        if approval is None:
            raise AuthorizationError(
                "approval_required",
                "This launch plan requires approval.",
                remediation="Review and approve the exact launch plan before retrying.",
            )
        expires_at = approval.expires_at
        if (
            approval.fingerprint != plan.fingerprint
            or approval.subject_id != request.subject.subject_id
            or (expires_at is not None and expires_at <= _now())
        ):
            raise AuthorizationError(
                "stale_approval",
                "The approval does not match this launch plan and subject.",
                remediation="Review and approve the current launch plan.",
            )

    async def _prepare(
        self,
        plan: LaunchPlan,
        manifest: AppManifest,
        configuration: ResolvedConfiguration,
    ) -> None:
        lock = self._prepare_locks.setdefault(plan.fingerprint, asyncio.Lock())
        async with lock:
            if (
                not plan.request.force_rebuild
                and plan.fingerprint in self._prepared
                and self._outputs_exist(manifest.root, plan.steps)
            ):
                return
            await self._run_preparation_steps(plan, manifest, configuration)

    async def _run_preparation_steps(
        self,
        plan: LaunchPlan,
        manifest: AppManifest,
        configuration: ResolvedConfiguration,
    ) -> None:
        for resolved in plan.steps:
            if resolved.phase == "start":
                continue
            environment = self._environment(
                resolved.command, configuration, reserved={}
            )
            try:
                process = await asyncio.create_subprocess_exec(
                    str(resolved.executable),
                    *resolved.command.argv[1:],
                    cwd=str(resolved.cwd),
                    env=environment,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,
                )
                try:
                    _stdout, _stderr = await asyncio.wait_for(
                        process.communicate(), timeout=resolved.command.timeout_seconds
                    )
                except asyncio.TimeoutError as exc:
                    self._signal_group(process, signal.SIGKILL)
                    await process.wait()
                    raise PreparationError(
                        "preparation_timeout",
                        f"The {resolved.phase} step timed out.",
                        remediation="Increase its timeout or make the step complete sooner.",
                    ) from exc
                except asyncio.CancelledError:
                    self._signal_group(process, signal.SIGKILL)
                    await process.wait()
                    raise
            except OSError as exc:
                raise PreparationError(
                    "preparation_spawn_failed",
                    f"The {resolved.phase} step could not start.",
                    remediation="Check the executable, argv, and working directory.",
                ) from exc
            if process.returncode != 0:
                raise PreparationError(
                    "preparation_failed",
                    f"The {resolved.phase} step exited with code {process.returncode}.",
                    remediation="Run the step locally and correct the reported error.",
                    context=(("phase", resolved.phase),),
                )
            for declared in resolved.command.outputs:
                if not any(manifest.root.glob(declared)):
                    raise PreparationError(
                        "preparation_output_missing",
                        f"The {resolved.phase} step did not create a declared output.",
                        remediation="Correct the output declaration or build command.",
                    )
        self._prepared.add(plan.fingerprint)
        self._latest_fingerprint[
            (str(plan.scope_key.app_id), str(plan.scope_key.target_id))
        ] = plan.fingerprint

    async def _spawn(
        self,
        record: _LaunchRecord,
        plan: LaunchPlan,
        manifest: AppManifest,
        target: TargetSpec,
        configuration: ResolvedConfiguration,
    ) -> None:
        runtime = target.runtime
        assert runtime is not None
        start = next(step for step in plan.steps if step.phase == "start")
        port = self._free_port()
        scope_digest = hashlib.sha256(repr(plan.scope_key).encode()).hexdigest()[:16]
        data_dir = self._runtime_root / "data" / str(manifest.app_id) / scope_digest
        build_dir = self._runtime_root / "build" / str(manifest.app_id)
        data_dir.mkdir(parents=True, exist_ok=True)
        build_dir.mkdir(parents=True, exist_ok=True)
        base_url = f"http://127.0.0.1:{port}/"
        reserved = {
            "PORT": str(port),
            "APP_ID": str(manifest.app_id),
            "APP_INSTANCE_ID": str(plan.scope_key.instance_id or ""),
            "APP_DATA_DIR": str(data_dir),
            "APP_BUILD_DIR": str(build_dir),
            "APP_BASE_URL": base_url,
        }
        environment = self._environment(start.command, configuration, reserved)
        try:
            process = await asyncio.create_subprocess_exec(
                str(start.executable),
                *start.command.argv[1:],
                cwd=str(start.cwd),
                env=environment,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            raise LaunchError(
                "process_spawn_failed",
                "The app process could not be started.",
                remediation="Check its executable, argv, and working directory.",
            ) from exc
        record.process = process
        record.process_group_id = process.pid
        record.started_at = _now()
        record.endpoint = TargetEndpoint("http", "127.0.0.1", port, "/")
        assert process.stdout is not None and process.stderr is not None
        record.stream_tasks = [
            asyncio.create_task(self._capture(record, "stdout", process.stdout)),
            asyncio.create_task(self._capture(record, "stderr", process.stderr)),
        ]

    @staticmethod
    def _environment(
        command: CommandStep,
        configuration: ResolvedConfiguration,
        reserved: dict[str, str],
    ) -> dict[str, str]:
        environment = {
            key: os.environ[key]
            for key in _SAFE_PARENT_ENVIRONMENT
            if key in os.environ
        }
        configured = {item.key: item.value for item in configuration.values}
        for binding in command.environment:
            if binding.source is EnvironmentSource.LITERAL:
                value = binding.value
            else:
                try:
                    value = configured[binding.value]
                except KeyError as exc:
                    raise ConfigurationError(
                        "environment_value_missing",
                        f"Configuration for environment variable {binding.name!r} is missing.",
                        remediation="Configure the declared value before launching the app.",
                    ) from exc
            environment[binding.name] = value
        # Engine-owned variables cannot be overridden by the manifest.
        environment.update(reserved)
        return environment

    async def _wait_for_health(
        self, record: _LaunchRecord, target: TargetSpec
    ) -> None:
        runtime = target.runtime
        assert runtime is not None and record.endpoint is not None
        health = runtime.health
        if health.kind != "http":
            raise HealthCheckError(
                "unsupported_health_check",
                f"Health check kind {health.kind!r} is unsupported.",
                remediation="Use an HTTP health check for a web target.",
            )
        deadline = asyncio.get_running_loop().time() + health.timeout_seconds
        path = health.path if health.path.startswith("/") else f"/{health.path}"
        url = f"http://127.0.0.1:{record.endpoint.port}{path}"
        while asyncio.get_running_loop().time() < deadline:
            process = record.process
            if process is not None and process.returncode is not None:
                await self._drain_streams(record)
                raise LaunchError(
                    "process_exited_during_startup",
                    f"The app process exited with code {process.returncode} before health succeeded.",
                    remediation="Check the app logs and start command.",
                )
            try:
                status = await asyncio.to_thread(self._health_status, url)
                if 200 <= status < 300:
                    return
            except (OSError, urllib.error.URLError):
                pass
            await asyncio.sleep(max(0.001, health.interval_seconds))
        raise HealthCheckError(
            "health_timeout",
            "The app health check did not succeed before its timeout.",
            remediation="Check the health path and app logs, or increase the timeout.",
        )

    @staticmethod
    def _health_status(url: str) -> int:
        request = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(request, timeout=0.2) as response:
            return response.status

    async def _capture(
        self,
        record: _LaunchRecord,
        stream_name: str,
        reader: asyncio.StreamReader,
    ) -> None:
        pending = bytearray()
        while True:
            chunk = await reader.read(16 * 1024)
            if not chunk:
                if pending:
                    self._append_log(record, stream_name, bytes(pending))
                return
            pending.extend(chunk)
            while True:
                newline = pending.find(b"\n")
                if newline >= 0:
                    payload = bytes(pending[: newline + 1])
                    del pending[: newline + 1]
                    self._append_log(record, stream_name, payload)
                elif len(pending) >= self._max_log_record_bytes:
                    payload = bytes(pending[: self._max_log_record_bytes])
                    del pending[: self._max_log_record_bytes]
                    self._append_log(record, stream_name, payload)
                else:
                    break

    def _append_log(
        self, record: _LaunchRecord, stream_name: str, payload: bytes
    ) -> None:
        payload = payload[-self._max_log_record_bytes :]
        text = payload.decode("utf-8", "replace")
        size = len(text.encode("utf-8"))
        while record.logs and (
            len(record.logs) >= self._max_log_records
            or record.log_bytes + size > self._max_log_bytes
        ):
            removed = record.logs.popleft()
            record.log_bytes -= len(removed.text.encode("utf-8"))
        record.log_sequence += 1
        record.logs.append(
            LogRecord(
                sequence=record.log_sequence,
                stream=stream_name,
                text=text,
                occurred_at=_now(),
            )
        )
        record.log_bytes += size

    async def _watch(self, record: _LaunchRecord) -> None:
        process = record.process
        if process is None:
            return
        return_code = await process.wait()
        async with record.stop_lock:
            await self._terminate_process(record)
            await self._drain_streams(record)
            if record.state in {RuntimeState.STOPPING, RuntimeState.STOPPED}:
                return
            idle_task = record.idle_task
            if idle_task is not None and idle_task is not asyncio.current_task():
                idle_task.cancel()
            if (
                record.restart_count < self._max_crash_restarts
                and self._active.get(record.scope_key) == record.launch_id
            ):
                record.restart_count += 1
                record.failure = None
                await self._transition(
                    record,
                    RuntimeState.STARTING,
                    f"Restarting app after unexpected exit {return_code}",
                )
                try:
                    await self._spawn(
                        record,
                        record.plan,
                        record.manifest,
                        record.target,
                        record.configuration,
                    )
                    await self._wait_for_health(record, record.target)
                except (HealthCheckError, LaunchError) as exc:
                    await self._terminate_process(record)
                    await self._drain_streams(record)
                    await self._fail(
                        record,
                        RuntimeFailure(
                            exc.code,
                            "The app restart did not satisfy its health contract.",
                            exc.remediation,
                        ),
                    )
                    self._remove_active(record)
                    return
                await self._transition(record, RuntimeState.READY, "App is ready")
                record.watcher = asyncio.create_task(self._watch(record))
                self._schedule_idle(record)
                return
            failure = RuntimeFailure(
                "process_exited",
                f"The app process exited unexpectedly with code {return_code}.",
                "Check the app logs and restart it.",
            )
            await self._fail(record, failure)
            self._remove_active(record)

    def _schedule_idle(self, record: _LaunchRecord) -> None:
        runtime = record.target.runtime
        previous = record.idle_task
        if previous is not None and previous is not asyncio.current_task():
            previous.cancel()
        record.idle_task = None
        if (
            runtime is None
            or runtime.idle_timeout_seconds <= 0
            or record.leases > 0
            or not record.idle_armed
            or record.state is not RuntimeState.READY
        ):
            return
        record.idle_task = asyncio.create_task(
            self._idle_stop(record, runtime.idle_timeout_seconds)
        )

    async def _idle_stop(self, record: _LaunchRecord, delay: float) -> None:
        current = asyncio.current_task()
        retry = False
        try:
            await asyncio.sleep(delay)
            async with record.stop_lock:
                if (
                    record.leases != 0
                    or record.state is not RuntimeState.READY
                    or self._active.get(record.scope_key) != record.launch_id
                ):
                    return
                result = await self._stop_locked(record, StopReason.IDLE)
                retry = not result.stopped
        except asyncio.CancelledError:
            return
        finally:
            if record.idle_task is current:
                record.idle_task = None
        if retry:
            self._schedule_idle(record)

    @staticmethod
    def _disarm_idle(record: _LaunchRecord) -> None:
        record.idle_armed = False
        idle_task = record.idle_task
        record.idle_task = None
        if idle_task is not None and idle_task is not asyncio.current_task():
            idle_task.cancel()

    async def _transition(
        self, record: _LaunchRecord, state: RuntimeState, message: str
    ) -> None:
        record.state = state
        record.event_sequence += 1
        event = RuntimeEvent(
            sequence=record.event_sequence,
            launch_id=record.launch_id,
            state=state,
            occurred_at=_now(),
            message=message,
            failure=record.failure,
        )
        record.events.append(event)
        try:
            await self._host.record_event(event)
        except Exception:
            # Observability is best-effort.  A host event sink outage must not
            # strand or duplicate supervised operating-system processes.
            pass
        finally:
            async with record.changed:
                record.changed.notify_all()

    async def _fail(
        self, record: _LaunchRecord, failure: RuntimeFailure
    ) -> None:
        record.failure = failure
        await self._transition(record, RuntimeState.FAILED, failure.message)

    async def _terminate_process(self, record: _LaunchRecord) -> None:
        process = record.process
        group_id = record.process_group_id
        if process is None or group_id is None:
            return
        self._signal_group_id(group_id, signal.SIGTERM)
        deadline = asyncio.get_running_loop().time() + self._stop_grace_seconds
        while self._group_alive(group_id):
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                self._signal_group_id(group_id, signal.SIGKILL)
                break
            await asyncio.sleep(min(0.02, remaining))
        if process.returncode is None:
            try:
                await asyncio.wait_for(process.wait(), self._stop_grace_seconds)
            except asyncio.TimeoutError:
                self._signal_group_id(group_id, signal.SIGKILL)

    @staticmethod
    def _signal_group(process: asyncio.subprocess.Process, sig: signal.Signals) -> None:
        ProcessLifecycleManager._signal_group_id(process.pid, sig)

    @staticmethod
    def _signal_group_id(group_id: int, sig: signal.Signals) -> None:
        try:
            os.killpg(group_id, sig)
        except ProcessLookupError:
            return

    @staticmethod
    def _group_alive(group_id: int) -> bool:
        try:
            os.killpg(group_id, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    async def _drain_streams(self, record: _LaunchRecord) -> None:
        current = asyncio.current_task()
        tasks = [task for task in record.stream_tasks if task is not current]
        if tasks:
            gathering = asyncio.gather(*tasks, return_exceptions=True)
            try:
                await asyncio.wait_for(
                    gathering, timeout=max(0.1, self._stop_grace_seconds)
                )
            except asyncio.TimeoutError:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    def _remove_active(self, record: _LaunchRecord) -> None:
        if self._active.get(record.scope_key) == record.launch_id:
            self._active.pop(record.scope_key, None)

    @staticmethod
    def _free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    def _record(self, launch_id: LaunchId) -> _LaunchRecord:
        try:
            return self._records[launch_id]
        except KeyError:
            raise LaunchError(
                "launch_not_found",
                f"Launch {launch_id!s} is unknown.",
                remediation="Use a launch identifier returned by this runtime.",
            ) from None


__all__ = ["ProcessLifecycleManager"]
