# App-engine Runtime Kernel PRD

**Date:** 2026-08-27

**Status:** Approved for implementation by the parent design and explicit user instruction

**Parent design:** `atrium/docs/superpowers/specs/2026-08-27-app-engine-atrium-integration-design.md`

## Problem

App-engine and Atrium separately discover, validate, serve, and proxy the same
apps. Neither owns a general process lifecycle, so backend apps must already be
running. App creation also depends on hand-written manifests. This duplicates
code, makes launch slower than necessary, and prevents app-engine from being a
portable app-building product.

## Goals

- Publish app-engine as an importable package while retaining `python engine.py`.
- Provide one typed, versioned manifest contract with v1 compatibility.
- Serve immutable catalog snapshots without filesystem I/O on reads.
- Prepare and supervise HTTP app processes from argv-only lifecycle steps.
- Cache unchanged install/build work and reuse warm scoped processes.
- Provide target-provider and host-adapter boundaries suitable for Atrium and
  future iOS/Android providers.
- Bundle an App Studio that creates, imports, validates, configures, launches,
  diagnoses, and stops apps without Atrium.
- Preserve existing standalone state, local-AI, launcher, static-app, and
  `entry_point` behavior.

## Non-goals

- iOS or Android implementations.
- Shell command strings.
- Toolchain installation.
- NDJSON application transport.
- Atrium authentication or settings logic inside app-engine.

## Success Metrics

- Existing app-engine tests stay green.
- A v2 managed-process fixture reaches `ready`, proxies HTTP, streams logs, and
  stops without orphaning its process group.
- Repeating the launch with unchanged inputs does not rerun preparation or
  start another scoped process.
- `AppCatalog.snapshot()` performs no filesystem calls.
- Standalone API exposes catalog, lifecycle, Studio, and diagnostics.
- Atrium can import the built distribution and pass its contract tests.

## Package Layout

```text
app_engine/
  __init__.py          package version and public exports
  contracts.py         immutable DTOs, enums, errors, Protocol interfaces
  manifest.py          v1/v2 parsing, validation, normalization
  catalog.py           source scanning, immutable snapshots, refresh events
  fingerprint.py       stable plan and input fingerprints
  process.py           argv process-group execution and bounded log capture
  lifecycle.py         preparation cache, scope resolution, state machine
  providers/
    __init__.py        provider registry exports
    web.py             port assignment, HTTP health, process target
  gateway.py           static resolution and streaming HTTP proxy service
  studio.py            probes, templates, preview/apply services
  routes.py            reusable FastAPI router factories
  runtime.py           composition root and local host adapter
studio/
  index.html           bundled Studio/launcher UI
templates/
  static-web/
  generic-http/
  python-uv/
  node/
  go/
  rust/
tests/
  test_contracts.py
  test_manifest_v2.py
  test_catalog_service.py
  test_fingerprint.py
  test_process_runner.py
  test_lifecycle.py
  test_web_provider.py
  test_gateway_service.py
  test_studio_service.py
  test_runtime_routes.py
```

`engine.py` becomes a thin standalone composition root after the reusable
services are proven. Existing focused local-AI modules stay intact.

## Interface Contract

The executable source of truth is `app_engine/contracts.py`. Public collections
are tuples or immutable mappings. Public interfaces do not accept untyped
`dict` or `Any`.

### Identity and enums

```python
AppId = NewType("AppId", str)
TargetId = NewType("TargetId", str)
LaunchId = NewType("LaunchId", str)
InstanceId = NewType("InstanceId", str)
CatalogKey = NewType("CatalogKey", str)
PlanFingerprint = NewType("PlanFingerprint", str)

class ProcessScope(str, Enum):
    SHARED = "shared"
    PER_USER = "per_user"
    PER_INSTANCE = "per_instance"

class RuntimeState(str, Enum):
    NEEDS_CONFIG = "needs_config"
    NEEDS_APPROVAL = "needs_approval"
    PREPARING = "preparing"
    BUILDING = "building"
    STARTING = "starting"
    READY = "ready"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"
```

### Manifest DTOs

```python
@dataclass(frozen=True)
class CommandStep:
    argv: tuple[str, ...]
    step_id: str | None = None
    cwd: str = "."
    environment: tuple[EnvironmentBinding, ...] = ()
    inputs: tuple[str, ...] = ()
    outputs: tuple[str, ...] = ()
    timeout_seconds: float | None = None

@dataclass(frozen=True)
class RuntimeSpec:
    driver: str
    scope: ProcessScope
    install: tuple[CommandStep, ...]
    build: tuple[CommandStep, ...]
    start: CommandStep
    health: HealthSpec
    idle_timeout_seconds: float
    autostart: bool

@dataclass(frozen=True)
class TargetSpec:
    target_id: TargetId
    kind: str
    runtime: RuntimeSpec | None

@dataclass(frozen=True)
class AppManifest:
    manifest_version: int
    app_id: AppId
    label: str
    icon: str
    root: Path
    default_target: TargetId
    targets: tuple[TargetSpec, ...]
    configuration: tuple[ConfigurationField, ...]
    metadata: AppMetadata
    browser: BrowserPolicy
```

### Catalog

```python
class AppCatalog(Protocol):
    async def configure(
        self, catalog_key: CatalogKey, sources: tuple[AppSource, ...]
    ) -> CatalogSnapshot: ...

    def snapshot(self, catalog_key: CatalogKey) -> CatalogSnapshot: ...
    async def inspect(self, app_root: Path) -> ManifestInspection: ...
    async def refresh(self, catalog_key: CatalogKey) -> CatalogSnapshot: ...
    async def events(
        self, catalog_key: CatalogKey
    ) -> AsyncIterator[CatalogEvent]: ...
```

`snapshot()` is thread-safe and reads no files. `configure()` and `refresh()`
scan outside the event loop and atomically replace the entire snapshot. Native
sources win over later scan sources; duplicate IDs are explicit rejections.

### Lifecycle

```python
class LifecycleManager(Protocol):
    async def preview(self, request: LaunchRequest) -> LaunchPlan: ...
    async def launch(
        self, request: LaunchRequest, approval: ApprovalReceipt | None
    ) -> LaunchHandle: ...
    async def stop(self, launch_id: LaunchId, reason: StopReason) -> StopResult: ...
    async def status(self, launch_id: LaunchId) -> LaunchStatus: ...
    async def events(
        self, launch_id: LaunchId, after: int | None = None
    ) -> AsyncIterator[RuntimeEvent]: ...
    async def logs(
        self, launch_id: LaunchId, after: int | None, limit: int
    ) -> LogPage: ...
```

`preview()` is side-effect free. `launch()` is single-flight by resolved scope
key. It refuses missing configuration and stale approval before any command
runs. Install/build failure is terminal for that attempt. Runtime crash restart
is bounded. `stop()` terminates the whole child process group and is idempotent.

### Target provider

```python
class TargetProvider(Protocol):
    kind: str
    async def probe(self) -> ToolchainReport: ...
    def templates(self) -> tuple[ProjectTemplate, ...]: ...
    def validate(
        self, manifest: AppManifest, target: TargetSpec
    ) -> ValidationReport: ...
    async def prepare(self, context: PrepareContext) -> PreparedTarget: ...
    async def launch(self, target: PreparedTarget) -> TargetSession: ...
    async def observe(
        self, session: TargetSession
    ) -> AsyncIterator[TargetEvent]: ...
    async def stop(
        self, session: TargetSession, grace_seconds: float
    ) -> StopResult: ...
```

The web provider injects `PORT`, `APP_ID`, `APP_INSTANCE_ID`, `APP_DATA_DIR`,
and `APP_BASE_URL`; starts argv without a shell; waits on loopback HTTP health;
and returns a loopback endpoint only after health succeeds.

### Host adapter

```python
class HostAdapter(Protocol):
    async def sources(self, subject: HostSubject) -> tuple[AppSource, ...]: ...
    async def configuration(
        self, subject: HostSubject, app_id: AppId
    ) -> ResolvedConfiguration: ...
    async def authorize(
        self, subject: HostSubject, plan: LaunchPlan
    ) -> Authorization: ...
    async def record_event(self, event: RuntimeEvent) -> None: ...
    async def resolve_app_state(
        self, subject: HostSubject, app_id: AppId
    ) -> AppStateStore: ...
    async def studio_roots(
        self, subject: HostSubject
    ) -> tuple[StudioRoot, ...]: ...
    def capability_providers(self) -> HostCapabilityProviders: ...
```

### Studio

```python
class StudioService(Protocol):
    async def toolchains(self) -> ToolchainInventory: ...
    async def templates(self) -> tuple[TemplateSummary, ...]: ...
    async def inspect_import(self, path: Path) -> ImportProposal: ...
    async def preview_create(self, request: CreateProjectRequest) -> ProjectPlan: ...
    async def create(
        self, request: CreateProjectRequest, accepted_plan: PlanFingerprint
    ) -> CreatedProject: ...
    async def preview_manifest(self, change: ManifestChange) -> ManifestChangePlan: ...
    async def apply_manifest(
        self, change: ManifestChange, accepted_plan: PlanFingerprint
    ) -> ManifestInspection: ...
    async def configuration_form(
        self, subject: HostSubject, app_id: AppId
    ) -> ConfigurationForm: ...
    async def save_configuration(
        self, subject: HostSubject, values: tuple[ConfigurationValue, ...]
    ) -> ConfigurationResult: ...
```

Preview methods write nothing. Apply methods require the exact preview
fingerprint, stage output, and never overwrite existing files implicitly.

### Gateway and runtime

```python
class AppGateway(Protocol):
    async def open(self, request: OpenAppRequest) -> AppSession: ...
    async def close(self, session_id: str) -> None: ...
    async def serve_asset(
        self, session: AppSession, request: AssetRequest
    ) -> AssetResponse: ...
    async def proxy(
        self, session: AppSession, request: ProxyRequest
    ) -> ProxyResponse: ...

class AppEngineRuntime:
    catalog: AppCatalog
    lifecycle: LifecycleManager
    studio: StudioService
    gateway: AppGateway
    async def start(self) -> None: ...
    async def close(self, grace_seconds: float) -> ShutdownReport: ...
```

Streaming HTTP DTOs carry async byte iterators. The gateway preserves repeated
query parameters, supports SSE, removes hop-by-hop headers, and never exposes a
non-loopback managed target. The frozen HTTP DTO cannot represent bidirectional
WebSocket frames: ``DefaultAppGateway.websocket_endpoint()`` is the explicit
seam for a host route adapter to bridge authenticated WebSocket frames to the
pinned loopback endpoint. HTTP proxy calls reject upgrades rather than silently
degrading them into ordinary requests.

The concrete gateway also exposes ``shutdown()`` for runtime composition. It
cancels pending idle timers, stops warm zero-idle launches with the shutdown
reason, and closes pooled HTTP resources; the frozen ``AppGateway.close``
method remains scoped to one public app session.

## Errors

```text
AppEngineError(code, message, remediation, context)
├── ManifestError
├── ConfigurationError
├── AuthorizationError
├── ToolchainError
├── PreparationError
├── LaunchError
├── HealthCheckError
├── TargetProviderError
└── ShutdownError
```

The stable codes are those in the parent design. Public messages and context
must be secret-safe. Internal causes remain exception chains and trusted logs.

## Security

- Direct argv execution only.
- Resolved path containment for cwd, inputs, outputs, imports, and templates.
- Isolated app origins and scoped, expiring capabilities.
- Approval bound to normalized commands, executables, provider, driver, and
  capabilities.
- Loopback-only managed web endpoints.
- Complete process-group teardown on cancellation, failure, and shutdown.
- No manifest or app iframe can invoke local-AI management or Studio writes.

## Testing

Tests exercise public interfaces and real temporary files/processes wherever
possible. They do not assert mock implementation details. Each behavior follows
red-green-refactor. Implementers do not modify interface signatures; test
writers do not inspect implementation files.

## Rollout

1. Package and contracts.
2. Manifest v2 and catalog.
3. Fingerprints, process runner, lifecycle, web provider.
4. Gateway and routes.
5. Studio services and bundled UI.
6. Standalone composition cutover with compatibility tests.
7. Tag/pin a release for Atrium integration.

Rollback keeps the current `engine.py` behavior available until the new
composition root passes the existing contract and release-smoke suites.
