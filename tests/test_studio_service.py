"""Public contract tests for app-engine's portable App Studio service."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app_engine.catalog import ImmutableAppCatalog
from app_engine.contracts import (
    AppEngineError,
    AppId,
    AppSource,
    Authorization,
    CatalogKey,
    ConfigurationError,
    ConfigurationResult,
    ConfigurationValue,
    CreateProjectRequest,
    HostAdapter,
    HostCapabilityProviders,
    HostSubject,
    ManifestChange,
    PlanFingerprint,
    PreparedTarget,
    ProjectTemplate,
    ResolvedConfiguration,
    RuntimeEvent,
    StopResult,
    StudioRoot,
    TargetEvent,
    TargetProvider,
    TemplateFile,
    ToolchainInfo,
    ToolchainReport,
    ValidationReport,
)
from app_engine.providers import ProviderRegistry


CATALOG_KEY = CatalogKey("studio-tests")
SUBJECT = HostSubject(subject_id="person-1", role="admin")


def _studio_type():
    try:
        from app_engine.studio import PortableStudioService
    except ImportError as exc:
        pytest.fail(
            "app_engine.studio.PortableStudioService is not implemented: "
            f"{exc}"
        )
    return PortableStudioService


class _NoCapabilities(HostCapabilityProviders):
    def names(self) -> tuple[str, ...]:
        return ()


class _RecordingHost(HostAdapter):
    def __init__(self, roots: tuple[StudioRoot, ...]) -> None:
        self.roots = roots
        self.configuration_by_app: dict[str, ResolvedConfiguration] = {}
        self.saved: list[tuple[HostSubject, tuple[ConfigurationValue, ...]]] = []
        self.save_app_id = AppId("configured-app")

    async def sources(self, subject: HostSubject) -> tuple[AppSource, ...]:
        return ()

    async def configuration(
        self, subject: HostSubject, app_id: AppId
    ) -> ResolvedConfiguration:
        return self.configuration_by_app.get(
            str(app_id), ResolvedConfiguration(app_id=app_id, values=())
        )

    async def save_configuration(
        self,
        subject: HostSubject,
        values: tuple[ConfigurationValue, ...],
    ) -> ConfigurationResult:
        self.saved.append((subject, values))
        return ConfigurationResult(
            app_id=self.save_app_id,
            saved_keys=tuple(value.key for value in values),
        )

    async def authorize(self, subject, plan) -> Authorization:
        return Authorization(allowed=True, approval_required=False)

    async def record_event(self, event: RuntimeEvent) -> None:
        return None

    async def resolve_app_state(self, subject, app_id):
        raise AssertionError("Studio tests do not use app state")

    async def studio_roots(
        self, subject: HostSubject
    ) -> tuple[StudioRoot, ...]:
        return self.roots

    def capability_providers(self) -> HostCapabilityProviders:
        return _NoCapabilities()


class _FutureMobileProvider(TargetProvider):
    kind = "ios"

    async def probe(self) -> ToolchainReport:
        return ToolchainReport(
            (
                ToolchainInfo(
                    toolchain_id="xcode",
                    display_name="Xcode",
                    executable=None,
                    version="",
                    available=False,
                ),
            )
        )

    def templates(self) -> tuple[ProjectTemplate, ...]:
        return (
            ProjectTemplate(
                template_id="ios-swift",
                target_kind="ios",
                label="iOS Swift",
                description="A future mobile provider template",
                required_toolchains=("xcode",),
                files=(TemplateFile("Sources/App.swift", b"print(\"hello\")\n"),),
            ),
        )

    def validate(self, manifest, target) -> ValidationReport:
        return ValidationReport()

    async def prepare(self, context) -> PreparedTarget:
        raise AssertionError("Studio preview must not prepare a target")

    async def launch(self, target):
        raise AssertionError("Studio preview must not launch a target")

    async def observe(self, session):
        if False:
            yield TargetEvent(state=None)  # pragma: no cover

    async def stop(self, session, grace_seconds: float) -> StopResult:
        raise AssertionError("Studio preview must not stop a target")


async def _service(
    tmp_path: Path,
    *,
    providers: ProviderRegistry | None = None,
) -> tuple[object, _RecordingHost, Path, ImmutableAppCatalog]:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    source_root = tmp_path / "catalog"
    source_root.mkdir(parents=True)
    host = _RecordingHost(
        (
            StudioRoot(
                root=workspace.resolve(), inspect=True, create=True, write=True
            ),
        )
    )
    catalog = ImmutableAppCatalog()
    await catalog.configure(
        CATALOG_KEY,
        (AppSource(kind="test", root=source_root, priority=1),),
    )
    service = _studio_type()(
        host=host,
        catalog=catalog,
        catalog_key=CATALOG_KEY,
        providers=providers or ProviderRegistry(),
    )
    return service, host, workspace, catalog


def _create_request(
    workspace: Path,
    *,
    directory_name: str = "hello-app",
    template_id: str = "static-web",
    label: str = "Hello App",
) -> CreateProjectRequest:
    return CreateProjectRequest(
        subject=SUBJECT,
        root=workspace,
        directory_name=directory_name,
        template_id=template_id,
        app_id=AppId("hello-app"),
        label=label,
        icon="H",
    )


def _v2_manifest(
    *, app_id: str = "configured-app", configuration: dict | None = None
) -> dict:
    payload: dict[str, object] = {
        "manifest_version": 2,
        "id": app_id,
        "label": "Configured App",
        "icon": "C",
        "default_target": "web",
        "targets": {"web": {"kind": "web"}},
    }
    if configuration is not None:
        payload["configuration"] = configuration
    return payload


@pytest.mark.asyncio
async def test_bundled_templates_cover_initial_language_neutral_workflows(tmp_path):
    service, _, _, _ = await _service(tmp_path)

    summaries = await service.templates()

    assert {
        "static-web",
        "generic-http",
        "python-uv",
        "node",
        "go",
        "rust",
    } <= {summary.template_id for summary in summaries}
    assert all(summary.target_kind == "web" for summary in summaries)


@pytest.mark.asyncio
async def test_provider_templates_aggregate_and_missing_tools_are_diagnostic_only(
    tmp_path,
):
    registry = ProviderRegistry((_FutureMobileProvider(),))
    service, _, workspace, _ = await _service(tmp_path, providers=registry)

    inventory = await service.toolchains()
    summaries = await service.templates()
    mobile = next(item for item in summaries if item.template_id == "ios-swift")
    plan = await service.preview_create(
        _create_request(workspace, template_id="ios-swift")
    )

    xcode = next(
        item for item in inventory.report.toolchains if item.toolchain_id == "xcode"
    )
    assert xcode.available is False
    assert mobile.available is False
    assert mobile.missing_toolchains == ("xcode",)
    assert plan.destination == (workspace / "hello-app").resolve()
    assert not plan.destination.exists()


@pytest.mark.asyncio
async def test_preview_create_is_write_free_and_fingerprint_is_deterministic(tmp_path):
    service, _, workspace, _ = await _service(tmp_path)
    request = _create_request(workspace)
    before = tuple(workspace.rglob("*"))

    first = await service.preview_create(request)
    second = await service.preview_create(request)
    changed = await service.preview_create(
        _create_request(workspace, label="A Different Label")
    )

    assert first == second
    assert first.fingerprint != changed.fingerprint
    assert first.destination == (workspace / "hello-app").resolve()
    assert first.files
    assert all(first.destination in file.path.parents for file in first.files)
    assert tuple(workspace.rglob("*")) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("unsafe_root", ["outside", "traversal"])
async def test_preview_create_must_stay_inside_a_create_enabled_studio_root(
    tmp_path, unsafe_root
):
    service, _, workspace, _ = await _service(tmp_path)
    if unsafe_root == "outside":
        outside = tmp_path / "outside"
        outside.mkdir()
        request = _create_request(outside)
    else:
        request = _create_request(workspace, directory_name="../outside")

    with pytest.raises(AppEngineError):
        await service.preview_create(request)

    assert not (tmp_path / "outside" / "app.json").exists()


@pytest.mark.asyncio
async def test_create_requires_exact_fingerprint_and_leaves_no_partial_project(
    tmp_path,
):
    service, _, workspace, _ = await _service(tmp_path)
    request = _create_request(workspace)
    plan = await service.preview_create(request)

    with pytest.raises(AppEngineError):
        await service.create(request, PlanFingerprint("stale"))

    assert not plan.destination.exists()


@pytest.mark.asyncio
async def test_create_stages_complete_project_and_never_overwrites(tmp_path):
    service, _, workspace, _ = await _service(tmp_path)
    request = _create_request(workspace)
    plan = await service.preview_create(request)

    created = await service.create(request, plan.fingerprint)

    assert created.root == plan.destination
    assert created.manifest.app_id == "hello-app"
    assert (created.root / "app.json").is_file()
    assert (created.root / "index.html").is_file()
    assert sorted(workspace.iterdir()) == [created.root]

    sentinel = "user-owned content"
    (created.root / "index.html").write_text(sentinel, encoding="utf-8")
    with pytest.raises(AppEngineError):
        await service.create(request, plan.fingerprint)
    assert (created.root / "index.html").read_text(encoding="utf-8") == sentinel


@pytest.mark.asyncio
async def test_import_recognizes_an_existing_typed_manifest(tmp_path):
    service, _, workspace, _ = await _service(tmp_path)
    project = workspace / "existing"
    project.mkdir()
    (project / "app.json").write_text(
        json.dumps(_v2_manifest(app_id="existing")), encoding="utf-8"
    )

    proposal = await service.inspect_import(project)

    assert proposal.root == project.resolve()
    assert proposal.suggested_manifest is not None
    assert proposal.suggested_manifest.app_id == "existing"
    assert any("app.json" in item.lower() for item in proposal.evidence)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("marker", "content", "template_id"),
    [
        ("pyproject.toml", "[project]\nname='demo'\n", "python-uv"),
        ("package.json", "{}\n", "node"),
        ("go.mod", "module example.test/demo\n", "go"),
        ("Cargo.toml", "[package]\nname='demo'\n", "rust"),
    ],
)
async def test_import_recognizes_common_language_projects(
    tmp_path, marker, content, template_id
):
    service, _, workspace, _ = await _service(tmp_path)
    project = workspace / "existing"
    project.mkdir()
    (project / marker).write_text(content, encoding="utf-8")

    proposal = await service.inspect_import(project)

    assert proposal.suggested_template_id == template_id
    assert proposal.suggested_manifest is None
    assert any(marker.lower() in item.lower() for item in proposal.evidence)


@pytest.mark.asyncio
async def test_manifest_preview_is_write_free_and_apply_requires_exact_plan(tmp_path):
    service, _, workspace, _ = await _service(tmp_path)
    project = workspace / "manifest-app"
    project.mkdir()
    manifest_path = project / "app.json"
    original = "{\"existing\": true}\n"
    manifest_path.write_text(original, encoding="utf-8")
    content = json.dumps(_v2_manifest(app_id="manifest-app"), indent=2)
    change = ManifestChange(subject=SUBJECT, app_root=project, content=content)

    plan = await service.preview_manifest(change)

    assert plan.inspection.errors == ()
    assert plan.inspection.manifest is not None
    assert manifest_path.read_text(encoding="utf-8") == original

    with pytest.raises(AppEngineError):
        await service.apply_manifest(change, PlanFingerprint("stale"))
    assert manifest_path.read_text(encoding="utf-8") == original

    applied = await service.apply_manifest(change, plan.fingerprint)
    assert applied.errors == ()
    assert applied.manifest is not None
    assert json.loads(manifest_path.read_text(encoding="utf-8"))["id"] == "manifest-app"


@pytest.mark.asyncio
async def test_manifest_preview_rejects_embedded_configuration_values(tmp_path):
    service, _, workspace, _ = await _service(tmp_path)
    project = workspace / "manifest-app"
    project.mkdir()
    payload = _v2_manifest(
        app_id="manifest-app",
        configuration={
            "TOKEN": {
                "type": "secret",
                "label": "Token",
                "value": "must-not-be-embedded",
            }
        },
    )
    change = ManifestChange(
        subject=SUBJECT, app_root=project, content=json.dumps(payload)
    )

    plan = await service.preview_manifest(change)

    assert plan.inspection.manifest is None
    assert any(
        issue.code == "configuration_value_forbidden"
        for issue in plan.inspection.errors
    )
    assert not (project / "app.json").exists()


@pytest.mark.asyncio
async def test_configuration_form_and_save_delegate_declared_keys_only(tmp_path):
    service, host, _, catalog = await _service(tmp_path)
    source_root = tmp_path / "catalog"
    app_root = source_root / "configured"
    app_root.mkdir()
    (app_root / "app.json").write_text(
        json.dumps(
            _v2_manifest(
                configuration={
                    "TOKEN": {
                        "type": "secret",
                        "label": "API token",
                        "required": True,
                        "env": "TOKEN",
                    }
                }
            )
        ),
        encoding="utf-8",
    )
    await catalog.refresh(CATALOG_KEY)
    current = ConfigurationValue(key="TOKEN", value="already-set", secret=True)
    host.configuration_by_app["configured-app"] = ResolvedConfiguration(
        app_id=AppId("configured-app"), values=(current,)
    )
    host.save_app_id = AppId("configured-app")

    form = await service.configuration_form(SUBJECT, AppId("configured-app"))

    assert [field.key for field in form.fields] == ["TOKEN"]
    assert form.values == (current,)

    with pytest.raises(ConfigurationError):
        await service.save_configuration(
            SUBJECT, (ConfigurationValue(key="UNDECLARED", value="x"),)
        )
    assert host.saved == []

    supplied = (ConfigurationValue(key="TOKEN", value="new", secret=True),)
    result = await service.save_configuration(SUBJECT, supplied)
    assert result == ConfigurationResult(
        app_id=AppId("configured-app"), saved_keys=("TOKEN",)
    )
    assert host.saved == [(SUBJECT, supplied)]
