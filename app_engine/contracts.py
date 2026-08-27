"""Executable public contracts for the reusable app-engine runtime.

Implementation and tests must depend on these types without changing public
signatures independently. Concrete services live in focused sibling modules.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import AsyncIterator, NewType


AppId = NewType("AppId", str)
TargetId = NewType("TargetId", str)
LaunchId = NewType("LaunchId", str)
InstanceId = NewType("InstanceId", str)
CatalogKey = NewType("CatalogKey", str)
PlanFingerprint = NewType("PlanFingerprint", str)
AppSessionId = NewType("AppSessionId", str)


class AppEngineError(Exception):
    """Base error with stable machine and safe human representations."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        remediation: str | None = None,
        context: tuple[tuple[str, str], ...] = (),
    ) -> None:
        self.code = code
        self.message = message
        self.remediation = remediation
        self.context = context
        super().__init__(message)


class ManifestError(AppEngineError):
    """Manifest parsing, validation, or compatibility failed."""


class ConfigurationError(AppEngineError):
    """Required or supplied app configuration is invalid."""


class AuthorizationError(AppEngineError):
    """The host denied a plan or its approval receipt is stale."""


class ToolchainError(AppEngineError):
    """A required executable or toolchain is unavailable."""


class PreparationError(AppEngineError):
    """An install or build step failed."""


class LaunchError(AppEngineError):
    """A prepared target could not start or exited unexpectedly."""


class HealthCheckError(AppEngineError):
    """A target did not become healthy within its contract."""


class TargetProviderError(AppEngineError):
    """A target provider cannot satisfy the declared target."""


class ShutdownError(AppEngineError):
    """Runtime shutdown left resources active after its grace period."""


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


class StopReason(str, Enum):
    USER = "user"
    IDLE = "idle"
    RESTART = "restart"
    FAILURE = "failure"
    SHUTDOWN = "shutdown"


class CacheStatus(str, Enum):
    HIT = "hit"
    MISS = "miss"
    DIRTY = "dirty"


class EnvironmentSource(str, Enum):
    LITERAL = "literal"
    CONFIGURATION = "configuration"
    HOST = "host"


class ConfigurationType(str, Enum):
    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"
    ENUM = "enum"
    PATH = "path"
    SECRET = "secret"


@dataclass(frozen=True)
class EnvironmentBinding:
    name: str
    source: EnvironmentSource
    value: str


@dataclass(frozen=True)
class HealthSpec:
    kind: str = "http"
    path: str = "/health"
    timeout_seconds: float = 30.0
    interval_seconds: float = 0.1


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
class ConfigurationField:
    key: str
    value_type: ConfigurationType
    label: str
    description: str = ""
    required: bool = False
    scope: ProcessScope = ProcessScope.PER_USER
    environment_name: str | None = None
    default_value: str | None = None
    choices: tuple[str, ...] = ()


@dataclass(frozen=True)
class AppMetadata:
    version: str = "0.0.0"
    description: str = ""
    categories: tuple[str, ...] = ()
    author: str = ""
    screenshots: tuple[str, ...] = ()
    min_engine_version: str = ""
    chat_enabled: bool = False
    chat_system_prompt: str = ""
    chat_knowledge: str = ""


@dataclass(frozen=True)
class BrowserPolicy:
    sandbox: str = "allow-scripts"
    permissions: tuple[str, ...] = ()
    allow: str = ""
    secret: bool = False
    multi: bool = False
    proxy_read_timeout: float = 30.0


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


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    message: str
    path: str = ""


@dataclass(frozen=True)
class ManifestInspection:
    root: Path
    manifest: AppManifest | None
    errors: tuple[ValidationIssue, ...] = ()
    warnings: tuple[ValidationIssue, ...] = ()


@dataclass(frozen=True)
class AppSource:
    kind: str
    root: Path
    priority: int


@dataclass(frozen=True)
class CatalogApp:
    manifest: AppManifest
    source: AppSource
    compatible: bool
    warnings: tuple[ValidationIssue, ...] = ()


@dataclass(frozen=True)
class CatalogRejection:
    root: Path
    app_id: str
    reason: str
    detail: str
    source: AppSource


@dataclass(frozen=True)
class CatalogSnapshot:
    key: CatalogKey
    generation: int
    apps: tuple[CatalogApp, ...]
    rejections: tuple[CatalogRejection, ...]
    created_at: datetime


@dataclass(frozen=True)
class CatalogEvent:
    key: CatalogKey
    generation: int
    changed_app_ids: tuple[AppId, ...]


@dataclass(frozen=True)
class HostSubject:
    subject_id: str
    role: str


@dataclass(frozen=True)
class ConfigurationValue:
    key: str
    value: str
    secret: bool = False


@dataclass(frozen=True)
class ResolvedConfiguration:
    app_id: AppId
    values: tuple[ConfigurationValue, ...]
    missing_required: tuple[str, ...] = ()


@dataclass(frozen=True)
class ScopeKey:
    app_id: AppId
    target_id: TargetId
    subject_id: str | None
    instance_id: InstanceId | None


@dataclass(frozen=True)
class LaunchRequest:
    app_id: AppId
    target_id: TargetId | None
    subject: HostSubject
    instance_id: InstanceId | None = None
    force_rebuild: bool = False


@dataclass(frozen=True)
class ResolvedStep:
    phase: str
    command: CommandStep
    executable: Path
    cwd: Path


@dataclass(frozen=True)
class LaunchPlan:
    request: LaunchRequest
    fingerprint: PlanFingerprint
    scope_key: ScopeKey
    steps: tuple[ResolvedStep, ...]
    required_configuration: tuple[ConfigurationField, ...]
    requested_capabilities: tuple[str, ...]
    cache_status: CacheStatus
    approval_required: bool


@dataclass(frozen=True)
class ApprovalReceipt:
    fingerprint: PlanFingerprint
    subject_id: str
    approved_at: datetime
    expires_at: datetime | None = None


@dataclass(frozen=True)
class Authorization:
    allowed: bool
    approval_required: bool
    reason: str = ""


@dataclass(frozen=True)
class LaunchHandle:
    launch_id: LaunchId
    scope_key: ScopeKey
    initial_state: RuntimeState


@dataclass(frozen=True)
class TargetEndpoint:
    scheme: str
    host: str
    port: int
    base_path: str = "/"


@dataclass(frozen=True)
class RuntimeFailure:
    code: str
    message: str
    remediation: str | None = None


@dataclass(frozen=True)
class LaunchStatus:
    launch_id: LaunchId
    state: RuntimeState
    endpoint: TargetEndpoint | None
    started_at: datetime | None
    failure: RuntimeFailure | None = None


@dataclass(frozen=True)
class RuntimeEvent:
    sequence: int
    launch_id: LaunchId
    state: RuntimeState
    occurred_at: datetime
    message: str = ""
    failure: RuntimeFailure | None = None


@dataclass(frozen=True)
class LogRecord:
    sequence: int
    stream: str
    text: str
    occurred_at: datetime


@dataclass(frozen=True)
class LogPage:
    records: tuple[LogRecord, ...]
    next_cursor: int | None


@dataclass(frozen=True)
class StopResult:
    launch_id: LaunchId
    stopped: bool
    exit_code: int | None


@dataclass(frozen=True)
class ToolchainInfo:
    toolchain_id: str
    display_name: str
    executable: Path | None
    version: str
    available: bool


@dataclass(frozen=True)
class ToolchainReport:
    toolchains: tuple[ToolchainInfo, ...]


@dataclass(frozen=True)
class ValidationReport:
    errors: tuple[ValidationIssue, ...] = ()
    warnings: tuple[ValidationIssue, ...] = ()


@dataclass(frozen=True)
class PrepareContext:
    manifest: AppManifest
    target: TargetSpec
    configuration: ResolvedConfiguration
    scope_key: ScopeKey
    data_dir: Path
    build_dir: Path
    force_rebuild: bool


@dataclass(frozen=True)
class PreparedTarget:
    context: PrepareContext
    fingerprint: PlanFingerprint
    environment: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class TargetSession:
    launch_id: LaunchId
    prepared: PreparedTarget
    endpoint: TargetEndpoint | None
    process_id: int | None


@dataclass(frozen=True)
class TargetEvent:
    state: RuntimeState
    message: str = ""
    failure: RuntimeFailure | None = None


@dataclass(frozen=True)
class TemplateFile:
    relative_path: str
    content: bytes


@dataclass(frozen=True)
class ProjectTemplate:
    template_id: str
    target_kind: str
    label: str
    description: str
    required_toolchains: tuple[str, ...]
    files: tuple[TemplateFile, ...]


@dataclass(frozen=True)
class TemplateSummary:
    template_id: str
    target_kind: str
    label: str
    description: str
    available: bool
    missing_toolchains: tuple[str, ...] = ()


@dataclass(frozen=True)
class ToolchainInventory:
    report: ToolchainReport
    checked_at: datetime


@dataclass(frozen=True)
class StudioRoot:
    root: Path
    inspect: bool
    create: bool
    write: bool


@dataclass(frozen=True)
class CreateProjectRequest:
    subject: HostSubject
    root: Path
    directory_name: str
    template_id: str
    app_id: AppId
    label: str
    icon: str


@dataclass(frozen=True)
class PlannedFile:
    path: Path
    content_sha256: str


@dataclass(frozen=True)
class ProjectPlan:
    request: CreateProjectRequest
    fingerprint: PlanFingerprint
    destination: Path
    files: tuple[PlannedFile, ...]
    lifecycle_commands: tuple[CommandStep, ...]


@dataclass(frozen=True)
class CreatedProject:
    root: Path
    manifest: AppManifest


@dataclass(frozen=True)
class ImportProposal:
    root: Path
    fingerprint: PlanFingerprint
    suggested_template_id: str | None
    suggested_manifest: AppManifest | None
    evidence: tuple[str, ...]


@dataclass(frozen=True)
class ManifestChange:
    subject: HostSubject
    app_root: Path
    content: str


@dataclass(frozen=True)
class ManifestChangePlan:
    change: ManifestChange
    fingerprint: PlanFingerprint
    inspection: ManifestInspection


@dataclass(frozen=True)
class ConfigurationForm:
    app_id: AppId
    fields: tuple[ConfigurationField, ...]
    values: tuple[ConfigurationValue, ...]


@dataclass(frozen=True)
class ConfigurationResult:
    app_id: AppId
    saved_keys: tuple[str, ...]


@dataclass(frozen=True)
class OpenAppRequest:
    launch: LaunchRequest
    approval: ApprovalReceipt | None = None


@dataclass(frozen=True)
class ScopedCapability:
    name: str
    token: str
    expires_at: datetime


@dataclass(frozen=True)
class AppSession:
    session_id: AppSessionId
    app_id: AppId
    launch_id: LaunchId | None
    origin: str
    entry_url: str
    capabilities: tuple[ScopedCapability, ...]


@dataclass(frozen=True)
class AssetRequest:
    relative_path: str
    if_none_match: str | None = None


@dataclass(frozen=True)
class AssetResponse:
    status_code: int
    media_type: str
    headers: tuple[tuple[str, str], ...]
    body: bytes


@dataclass(frozen=True)
class ProxyRequest:
    method: str
    path: str
    query: tuple[tuple[str, str], ...]
    headers: tuple[tuple[str, str], ...]
    body: AsyncIterator[bytes]


@dataclass(frozen=True)
class ProxyResponse:
    status_code: int
    headers: tuple[tuple[str, str], ...]
    body: AsyncIterator[bytes]


@dataclass(frozen=True)
class ShutdownReport:
    stopped_launch_ids: tuple[LaunchId, ...]
    incomplete_launch_ids: tuple[LaunchId, ...]


class AppStateStore(ABC):
    @abstractmethod
    async def load(self) -> bytes:
        raise NotImplementedError

    @abstractmethod
    async def save(self, content: bytes) -> None:
        raise NotImplementedError


class HostCapabilityProviders(ABC):
    """Typed optional host services; concrete hosts may expose none."""

    @abstractmethod
    def names(self) -> tuple[str, ...]:
        raise NotImplementedError


class AppCatalog(ABC):
    @abstractmethod
    async def configure(
        self, catalog_key: CatalogKey, sources: tuple[AppSource, ...]
    ) -> CatalogSnapshot:
        raise NotImplementedError

    @abstractmethod
    def snapshot(self, catalog_key: CatalogKey) -> CatalogSnapshot:
        raise NotImplementedError

    @abstractmethod
    async def inspect(self, app_root: Path) -> ManifestInspection:
        raise NotImplementedError

    @abstractmethod
    async def refresh(self, catalog_key: CatalogKey) -> CatalogSnapshot:
        raise NotImplementedError

    @abstractmethod
    async def events(self, catalog_key: CatalogKey) -> AsyncIterator[CatalogEvent]:
        raise NotImplementedError


class LifecycleManager(ABC):
    @abstractmethod
    async def preview(self, request: LaunchRequest) -> LaunchPlan:
        raise NotImplementedError

    @abstractmethod
    async def launch(
        self, request: LaunchRequest, approval: ApprovalReceipt | None
    ) -> LaunchHandle:
        raise NotImplementedError

    @abstractmethod
    async def retain(self, launch_id: LaunchId) -> None:
        """Keep an exact launch active while a consumer session uses it."""
        raise NotImplementedError

    @abstractmethod
    async def release(self, launch_id: LaunchId) -> None:
        """Release one launch lease and begin its idle policy at zero leases."""
        raise NotImplementedError

    @abstractmethod
    async def stop(self, launch_id: LaunchId, reason: StopReason) -> StopResult:
        raise NotImplementedError

    @abstractmethod
    async def status(self, launch_id: LaunchId) -> LaunchStatus:
        raise NotImplementedError

    @abstractmethod
    async def events(
        self, launch_id: LaunchId, after: int | None = None
    ) -> AsyncIterator[RuntimeEvent]:
        raise NotImplementedError

    @abstractmethod
    async def logs(
        self, launch_id: LaunchId, after: int | None, limit: int
    ) -> LogPage:
        raise NotImplementedError


class TargetProvider(ABC):
    kind: str

    @abstractmethod
    async def probe(self) -> ToolchainReport:
        raise NotImplementedError

    @abstractmethod
    def templates(self) -> tuple[ProjectTemplate, ...]:
        raise NotImplementedError

    @abstractmethod
    def validate(self, manifest: AppManifest, target: TargetSpec) -> ValidationReport:
        raise NotImplementedError

    @abstractmethod
    async def prepare(self, context: PrepareContext) -> PreparedTarget:
        raise NotImplementedError

    @abstractmethod
    async def launch(self, target: PreparedTarget) -> TargetSession:
        raise NotImplementedError

    @abstractmethod
    async def observe(self, session: TargetSession) -> AsyncIterator[TargetEvent]:
        raise NotImplementedError

    @abstractmethod
    async def stop(self, session: TargetSession, grace_seconds: float) -> StopResult:
        raise NotImplementedError


class HostAdapter(ABC):
    @abstractmethod
    async def sources(self, subject: HostSubject) -> tuple[AppSource, ...]:
        raise NotImplementedError

    @abstractmethod
    async def configuration(
        self, subject: HostSubject, app_id: AppId
    ) -> ResolvedConfiguration:
        raise NotImplementedError

    @abstractmethod
    async def authorize(self, subject: HostSubject, plan: LaunchPlan) -> Authorization:
        raise NotImplementedError

    @abstractmethod
    async def record_event(self, event: RuntimeEvent) -> None:
        raise NotImplementedError

    @abstractmethod
    async def resolve_app_state(
        self, subject: HostSubject, app_id: AppId
    ) -> AppStateStore:
        raise NotImplementedError

    @abstractmethod
    async def studio_roots(self, subject: HostSubject) -> tuple[StudioRoot, ...]:
        raise NotImplementedError

    @abstractmethod
    def capability_providers(self) -> HostCapabilityProviders:
        raise NotImplementedError


class StudioService(ABC):
    @abstractmethod
    async def toolchains(self) -> ToolchainInventory:
        raise NotImplementedError

    @abstractmethod
    async def templates(self) -> tuple[TemplateSummary, ...]:
        raise NotImplementedError

    @abstractmethod
    async def inspect_import(self, path: Path) -> ImportProposal:
        raise NotImplementedError

    @abstractmethod
    async def preview_create(self, request: CreateProjectRequest) -> ProjectPlan:
        raise NotImplementedError

    @abstractmethod
    async def create(
        self, request: CreateProjectRequest, accepted_plan: PlanFingerprint
    ) -> CreatedProject:
        raise NotImplementedError

    @abstractmethod
    async def preview_manifest(self, change: ManifestChange) -> ManifestChangePlan:
        raise NotImplementedError

    @abstractmethod
    async def apply_manifest(
        self, change: ManifestChange, accepted_plan: PlanFingerprint
    ) -> ManifestInspection:
        raise NotImplementedError

    @abstractmethod
    async def configuration_form(
        self, subject: HostSubject, app_id: AppId
    ) -> ConfigurationForm:
        raise NotImplementedError

    @abstractmethod
    async def save_configuration(
        self, subject: HostSubject, values: tuple[ConfigurationValue, ...]
    ) -> ConfigurationResult:
        raise NotImplementedError


class AppGateway(ABC):
    @abstractmethod
    async def open(self, request: OpenAppRequest) -> AppSession:
        raise NotImplementedError

    @abstractmethod
    async def close(self, session_id: AppSessionId) -> None:
        raise NotImplementedError

    @abstractmethod
    async def serve_asset(
        self, session: AppSession, request: AssetRequest
    ) -> AssetResponse:
        raise NotImplementedError

    @abstractmethod
    async def proxy(
        self, session: AppSession, request: ProxyRequest
    ) -> ProxyResponse:
        raise NotImplementedError


class AppEngineRuntime(ABC):
    catalog: AppCatalog
    lifecycle: LifecycleManager
    studio: StudioService
    gateway: AppGateway

    @abstractmethod
    async def start(self) -> None:
        raise NotImplementedError

    @abstractmethod
    async def close(self, grace_seconds: float) -> ShutdownReport:
        raise NotImplementedError
