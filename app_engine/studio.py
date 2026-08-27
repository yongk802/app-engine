"""Portable, host-neutral services for creating and configuring applications.

Studio deliberately plans before it writes.  Every mutating operation must be
replayed against an exact content fingerprint, and project creation is staged
beside its destination before the completed directory is published.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import shutil
import sys
import tempfile
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from .contracts import (
    AppCatalog,
    AppEngineError,
    AppId,
    AppManifest,
    CatalogKey,
    CommandStep,
    ConfigurationError,
    ConfigurationForm,
    ConfigurationResult,
    ConfigurationType,
    ConfigurationValue,
    CreateProjectRequest,
    CreatedProject,
    HostAdapter,
    HostSubject,
    ImportProposal,
    ManifestChange,
    ManifestChangePlan,
    ManifestInspection,
    PlanFingerprint,
    PlannedFile,
    ProcessScope,
    ProjectPlan,
    ProjectTemplate,
    StudioRoot,
    StudioService,
    TemplateFile,
    TemplateSummary,
    ToolchainInfo,
    ToolchainInventory,
    ToolchainReport,
)
from .manifest import APP_ID_RE, parse_manifest
from .providers import ProviderRegistry


_TEMPLATE_ROOT = Path(__file__).with_name("templates")
_MARKERS = (
    ("pyproject.toml", "python-uv"),
    ("package.json", "node"),
    ("go.mod", "go"),
    ("Cargo.toml", "rust"),
)
_TOOLCHAINS = (
    ("python", "Python", sys.executable),
    ("uv", "uv", "uv"),
    ("node", "Node.js", "node"),
    ("go", "Go", "go"),
    ("cargo", "Rust (Cargo)", "cargo"),
)
_TOOLCHAIN_CACHE_SECONDS = 5.0


def _fingerprint(value: object) -> PlanFingerprint:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return PlanFingerprint(hashlib.sha256(encoded).hexdigest())


def _content_hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _safe_template_path(relative_path: str) -> Path:
    candidate = Path(relative_path)
    if (
        not relative_path
        or candidate.is_absolute()
        or ".." in candidate.parts
        or candidate in {Path("."), Path("")}
    ):
        raise AppEngineError(
            "unsafe_template_path",
            "A template file must use a contained relative path.",
            context=(("path", relative_path),),
        )
    return candidate


def _command(argv: tuple[str, ...]) -> dict[str, object]:
    return {"argv": list(argv)}


_RUNTIMES: dict[str, dict[str, object] | None] = {
    "static-web": None,
    "generic-http": {
        "driver": "process",
        "scope": "shared",
        "start": _command(("./server",)),
        "health": {"kind": "http", "path": "/health"},
        "idle_timeout_seconds": 300,
    },
    "python-uv": {
        "driver": "process",
        "scope": "shared",
        "install": [_command(("uv", "sync"))],
        "start": _command(("uv", "run", "python", "main.py")),
        "health": {"kind": "http", "path": "/health"},
        "idle_timeout_seconds": 300,
    },
    "node": {
        "driver": "process",
        "scope": "shared",
        "start": _command(("node", "server.mjs")),
        "health": {"kind": "http", "path": "/health"},
        "idle_timeout_seconds": 300,
    },
    "go": {
        "driver": "process",
        "scope": "shared",
        "build": [_command(("go", "build", "-o", "app-server", "."))],
        "start": _command(("./app-server",)),
        "health": {"kind": "http", "path": "/health"},
        "idle_timeout_seconds": 300,
    },
    "rust": {
        "driver": "process",
        "scope": "shared",
        "build": [_command(("cargo", "build", "--release"))],
        "start": _command(("target/release/app-server",)),
        "health": {"kind": "http", "path": "/health"},
        "idle_timeout_seconds": 300,
    },
}


def _lifecycle_commands(template_id: str) -> tuple[CommandStep, ...]:
    runtime = _RUNTIMES.get(template_id)
    if runtime is None:
        return ()
    raw_commands = [
        *(runtime.get("install", [])),
        *(runtime.get("build", [])),
        runtime["start"],
    ]
    return tuple(
        CommandStep(argv=tuple(command["argv"]))
        for command in raw_commands
        if isinstance(command, dict)
    )


def _manifest_content(
    request: CreateProjectRequest, template: ProjectTemplate
) -> bytes:
    target: dict[str, object] = {"kind": template.target_kind}
    runtime = _RUNTIMES.get(template.template_id)
    if runtime is not None:
        target["runtime"] = runtime
    payload = {
        "manifest_version": 2,
        "id": str(request.app_id),
        "label": request.label,
        "icon": request.icon,
        "default_target": template.target_kind,
        "targets": {template.target_kind: target},
        "metadata": {
            "version": "0.1.0",
            "description": template.description,
        },
    }
    return (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _render_template_file(
    file: TemplateFile, request: CreateProjectRequest
) -> tuple[Path, bytes]:
    relative = _safe_template_path(file.relative_path)
    content = file.content
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return relative, content
    replacements = {
        "{{APP_ID}}": str(request.app_id),
        "{{APP_LABEL}}": request.label,
        "{{APP_ICON}}": request.icon,
        "{{DIRECTORY_NAME}}": request.directory_name,
    }
    for marker, value in replacements.items():
        text = text.replace(marker, value)
    return relative, text.encode("utf-8")


def _load_bundled_templates() -> tuple[ProjectTemplate, ...]:
    try:
        registry = json.loads((_TEMPLATE_ROOT / "registry.json").read_text("utf-8"))
        entries = registry["templates"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise AppEngineError(
            "template_registry_unavailable",
            "The bundled template registry could not be loaded.",
        ) from exc
    if not isinstance(entries, list):
        raise AppEngineError(
            "invalid_template_registry",
            "The bundled template registry must contain a templates array.",
        )
    templates: list[ProjectTemplate] = []
    for raw in entries:
        if not isinstance(raw, dict):
            raise AppEngineError(
                "invalid_template_registry", "A template entry must be an object."
            )
        template_id = raw.get("template_id")
        if not isinstance(template_id, str) or not template_id:
            raise AppEngineError(
                "invalid_template_registry", "A template id must be non-empty."
            )
        directory = (_TEMPLATE_ROOT / template_id).resolve(strict=False)
        if not _is_within(directory, _TEMPLATE_ROOT.resolve(strict=False)):
            raise AppEngineError(
                "unsafe_template_path", "A bundled template escaped its registry."
            )
        files = tuple(
            TemplateFile(
                path.relative_to(directory).as_posix(), path.read_bytes()
            )
            for path in sorted(directory.rglob("*"))
            if path.is_file()
        )
        templates.append(
            ProjectTemplate(
                template_id=template_id,
                target_kind=str(raw.get("target_kind", "web")),
                label=str(raw.get("label", template_id)),
                description=str(raw.get("description", "")),
                required_toolchains=tuple(raw.get("required_toolchains", ())),
                files=files,
            )
        )
    return tuple(templates)


def _probe_executable(
    toolchain_id: str, display_name: str, command: str
) -> ToolchainInfo:
    executable_value = command if os.path.isabs(command) else shutil.which(command)
    executable = Path(executable_value).resolve() if executable_value else None
    version = ""
    if executable is not None:
        try:
            import subprocess

            result = subprocess.run(
                [str(executable), "--version"],
                check=False,
                capture_output=True,
                text=True,
                timeout=2,
            )
            version = (result.stdout or result.stderr).strip().splitlines()[0]
        except (OSError, subprocess.SubprocessError, IndexError):
            version = ""
    return ToolchainInfo(
        toolchain_id=toolchain_id,
        display_name=display_name,
        executable=executable,
        version=version,
        available=executable is not None,
    )


def _inspection_with_root(
    inspection: ManifestInspection, root: Path
) -> ManifestInspection:
    manifest = inspection.manifest
    if manifest is not None:
        manifest = replace(manifest, root=root)
    return replace(inspection, root=root, manifest=manifest)


class PortableStudioService(StudioService):
    """A reusable App Studio with no dependency on a particular host UI."""

    def __init__(
        self,
        *,
        host: HostAdapter,
        catalog: AppCatalog,
        catalog_key: CatalogKey,
        providers: ProviderRegistry,
    ) -> None:
        self._host = host
        self._catalog = catalog
        self._catalog_key = catalog_key
        self._providers = providers
        self._bundled_templates = _load_bundled_templates()
        self._toolchain_inventory: ToolchainInventory | None = None
        self._toolchain_checked_monotonic = 0.0
        self._toolchain_lock = asyncio.Lock()
        self._configuration_context: dict[
            HostSubject,
            tuple[
                AppManifest,
                frozenset[str],
                tuple[ConfigurationValue, ...],
            ],
        ] = {}

    def _all_templates(self) -> tuple[ProjectTemplate, ...]:
        templates = list(self._bundled_templates)
        seen = {template.template_id for template in templates}
        for kind in self._providers.kinds():
            for template in self._providers.get(kind).templates():
                if template.target_kind != kind:
                    raise AppEngineError(
                        "template_provider_mismatch",
                        f"Template {template.template_id!r} declares target kind "
                        f"{template.target_kind!r}, but provider {kind!r} supplied it.",
                    )
                if template.template_id in seen:
                    raise AppEngineError(
                        "duplicate_template_id",
                        f"More than one template uses id {template.template_id!r}.",
                        context=(("template_id", template.template_id),),
                    )
                seen.add(template.template_id)
                templates.append(template)
        return tuple(sorted(templates, key=lambda item: item.template_id))

    def _template(self, template_id: str) -> ProjectTemplate:
        for template in self._all_templates():
            if template.template_id == template_id:
                return template
        raise AppEngineError(
            "template_not_found",
            f"No project template named {template_id!r} is available.",
            context=(("template_id", template_id),),
        )

    async def _allowed_path(
        self,
        subject: HostSubject,
        path: Path,
        capability: str,
    ) -> tuple[Path, StudioRoot]:
        resolved = await asyncio.to_thread(Path(path).resolve, strict=False)
        roots = await self._host.studio_roots(subject)
        for studio_root in roots:
            root = await asyncio.to_thread(studio_root.root.resolve, strict=False)
            enabled = bool(getattr(studio_root, capability))
            if enabled and _is_within(resolved, root):
                return resolved, studio_root
        raise AppEngineError(
            "studio_path_not_allowed",
            "The selected path is outside the host's allowed Studio roots.",
            remediation="Choose a directory exposed by the host for this operation.",
            context=(("path", str(resolved)), ("capability", capability)),
        )

    async def toolchains(self) -> ToolchainInventory:
        now = time.monotonic()
        if (
            self._toolchain_inventory is not None
            and now - self._toolchain_checked_monotonic < _TOOLCHAIN_CACHE_SECONDS
        ):
            return self._toolchain_inventory
        async with self._toolchain_lock:
            now = time.monotonic()
            if (
                self._toolchain_inventory is not None
                and now - self._toolchain_checked_monotonic
                < _TOOLCHAIN_CACHE_SECONDS
            ):
                return self._toolchain_inventory
            builtins = await asyncio.gather(
                *(
                    asyncio.to_thread(_probe_executable, tool_id, label, command)
                    for tool_id, label, command in _TOOLCHAINS
                )
            )
            provider_reports = await asyncio.gather(
                *(
                    self._providers.get(kind).probe()
                    for kind in self._providers.kinds()
                ),
                return_exceptions=True,
            )
            by_id = {item.toolchain_id: item for item in builtins}
            for report in provider_reports:
                if isinstance(report, ToolchainReport):
                    for item in report.toolchains:
                        by_id[item.toolchain_id] = item
            inventory = ToolchainInventory(
                report=ToolchainReport(
                    tuple(by_id[key] for key in sorted(by_id))
                ),
                checked_at=datetime.now(timezone.utc),
            )
            self._toolchain_inventory = inventory
            self._toolchain_checked_monotonic = time.monotonic()
            return inventory

    async def templates(self) -> tuple[TemplateSummary, ...]:
        inventory = await self.toolchains()
        available = {
            item.toolchain_id
            for item in inventory.report.toolchains
            if item.available
        }
        return tuple(
            TemplateSummary(
                template_id=template.template_id,
                target_kind=template.target_kind,
                label=template.label,
                description=template.description,
                available=not (
                    missing := tuple(
                        tool
                        for tool in template.required_toolchains
                        if tool not in available
                    )
                ),
                missing_toolchains=missing,
            )
            for template in self._all_templates()
        )

    async def inspect_import(
        self, path: Path, subject: HostSubject | None = None
    ) -> ImportProposal:
        root, _ = await self._allowed_path(
            # Import has no subject in the frozen contract, so the catalog's
            # configured host identity is represented by the sole inspect root.
            subject or await self._inspection_subject(), path, "inspect"
        )
        if not await asyncio.to_thread(root.is_dir):
            raise AppEngineError(
                "import_path_not_directory",
                "Only an existing project directory can be inspected.",
                context=(("path", str(root)),),
            )
        evidence: list[str] = []
        suggested_template: str | None = None
        suggested_manifest: AppManifest | None = None
        digest_inputs: list[tuple[str, str]] = []
        manifest_path = root / "app.json"
        if await asyncio.to_thread(manifest_path.is_file):
            if not _is_within(
                await asyncio.to_thread(manifest_path.resolve, strict=False), root
            ):
                raise AppEngineError(
                    "unsafe_import_manifest",
                    "The project's app.json resolves outside the project.",
                )
            inspection = await self._catalog.inspect(root)
            evidence.append("Found app.json and inspected the typed manifest.")
            suggested_manifest = inspection.manifest
            content = await asyncio.to_thread(manifest_path.read_bytes)
            digest_inputs.append(("app.json", _content_hash(content)))
        else:
            for marker, template_id in _MARKERS:
                marker_path = root / marker
                if await asyncio.to_thread(marker_path.is_file):
                    suggested_template = template_id
                    evidence.append(
                        f"Found {marker}; suggested the {template_id} template."
                    )
                    digest_inputs.append((marker, "present"))
                    break
        if not evidence:
            evidence.append(
                "No typed manifest or recognized language marker was found."
            )
        return ImportProposal(
            root=root,
            fingerprint=_fingerprint(
                {
                    "operation": "inspect_import",
                    "root": str(root),
                    "evidence": evidence,
                    "inputs": digest_inputs,
                }
            ),
            suggested_template_id=suggested_template,
            suggested_manifest=suggested_manifest,
            evidence=tuple(evidence),
        )

    async def _inspection_subject(self) -> HostSubject:
        """Resolve the only host subject available to the legacy import API.

        ``inspect_import`` predates the subject-bearing Studio requests.  A
        portable host generally exposes the same roots to every local admin;
        remember the subject from prior requests, or use an explicit local
        administrative subject for this read-only inspection.
        """
        if self._configuration_context:
            return next(reversed(self._configuration_context))
        return HostSubject(subject_id="local", role="admin")

    async def _project_material(
        self, request: CreateProjectRequest
    ) -> tuple[ProjectTemplate, Path, tuple[tuple[Path, bytes], ...]]:
        base, _ = await self._allowed_path(request.subject, request.root, "create")
        directory = Path(request.directory_name)
        if (
            not request.directory_name
            or directory.name != request.directory_name
            or "/" in request.directory_name
            or "\\" in request.directory_name
            or request.directory_name in {".", ".."}
        ):
            raise AppEngineError(
                "invalid_project_directory",
                "The project directory must be one contained directory name.",
            )
        if APP_ID_RE.fullmatch(str(request.app_id)) is None:
            raise AppEngineError(
                "invalid_app_id",
                "The app id must be a lowercase, hyphenated identifier.",
                context=(("app_id", str(request.app_id)),),
            )
        if not request.label or not request.icon:
            raise AppEngineError(
                "invalid_app_listing",
                "An app label and icon are required.",
            )
        destination = await asyncio.to_thread(
            (base / directory).resolve, strict=False
        )
        if not _is_within(destination, base) or destination == base:
            raise AppEngineError(
                "project_path_escape",
                "The project destination must stay inside its Studio root.",
            )
        template = self._template(request.template_id)
        material: dict[Path, bytes] = {
            Path("app.json"): _manifest_content(request, template)
        }
        for template_file in template.files:
            relative, content = _render_template_file(template_file, request)
            if relative in material:
                raise AppEngineError(
                    "duplicate_template_file",
                    f"The template writes {relative.as_posix()!r} more than once.",
                )
            material[relative] = content
        return template, destination, tuple(sorted(material.items()))

    async def preview_create(self, request: CreateProjectRequest) -> ProjectPlan:
        template, destination, material = await self._project_material(request)
        planned = tuple(
            PlannedFile(
                path=destination / relative,
                content_sha256=_content_hash(content),
            )
            for relative, content in material
        )
        fingerprint = _fingerprint(
            {
                "operation": "create",
                "subject": {
                    "id": request.subject.subject_id,
                    "role": request.subject.role,
                },
                "root": str(destination.parent),
                "destination": str(destination),
                "directory_name": request.directory_name,
                "template_id": request.template_id,
                "target_kind": template.target_kind,
                "app_id": str(request.app_id),
                "label": request.label,
                "icon": request.icon,
                "files": [
                    (file.path.relative_to(destination).as_posix(), file.content_sha256)
                    for file in planned
                ],
            }
        )
        return ProjectPlan(
            request=request,
            fingerprint=fingerprint,
            destination=destination,
            files=planned,
            lifecycle_commands=_lifecycle_commands(template.template_id),
        )

    async def create(
        self, request: CreateProjectRequest, accepted_plan: PlanFingerprint
    ) -> CreatedProject:
        plan = await self.preview_create(request)
        if plan.fingerprint != accepted_plan:
            raise AppEngineError(
                "stale_project_plan",
                "The accepted project plan no longer matches this request.",
                remediation="Preview the project again and accept the new plan.",
            )
        template, destination, material = await self._project_material(request)
        del template
        if await asyncio.to_thread(destination.exists):
            raise AppEngineError(
                "project_already_exists",
                "Studio never overwrites an existing project directory.",
                context=(("path", str(destination)),),
            )
        return await asyncio.to_thread(
            self._create_staged, destination, material
        )

    def _create_staged(
        self, destination: Path, material: tuple[tuple[Path, bytes], ...]
    ) -> CreatedProject:
        destination.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(
            tempfile.mkdtemp(
                prefix=f".{destination.name}.app-engine-", dir=destination.parent
            )
        )
        published = False
        try:
            for relative, content in material:
                path = staging / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            inspection = parse_manifest(staging)
            if inspection.manifest is None:
                detail = "; ".join(issue.message for issue in inspection.errors)
                raise AppEngineError(
                    "generated_manifest_invalid",
                    "The selected template produced an invalid app manifest.",
                    context=(("detail", detail),),
                )
            if destination.exists():
                raise AppEngineError(
                    "project_already_exists",
                    "Studio never overwrites an existing project directory.",
                    context=(("path", str(destination)),),
                )
            os.rename(staging, destination)
            published = True
            final = parse_manifest(destination)
            if final.manifest is None:
                raise AppEngineError(
                    "generated_manifest_invalid",
                    "The completed project manifest could not be inspected.",
                )
            return CreatedProject(root=destination, manifest=final.manifest)
        finally:
            if not published:
                shutil.rmtree(staging, ignore_errors=True)

    async def preview_manifest(
        self, change: ManifestChange
    ) -> ManifestChangePlan:
        app_root, _ = await self._allowed_path(
            change.subject, change.app_root, "write"
        )
        inspection = await asyncio.to_thread(
            self._inspect_manifest_content, app_root, change.content
        )
        current_path = app_root / "app.json"
        if await asyncio.to_thread(current_path.is_file):
            current = _content_hash(await asyncio.to_thread(current_path.read_bytes))
        else:
            current = "missing"
        fingerprint = _fingerprint(
            {
                "operation": "manifest",
                "subject": {
                    "id": change.subject.subject_id,
                    "role": change.subject.role,
                },
                "root": str(app_root),
                "content_sha256": _content_hash(change.content.encode("utf-8")),
                "current_sha256": current,
            }
        )
        normalized_change = replace(change, app_root=app_root)
        return ManifestChangePlan(
            change=normalized_change,
            fingerprint=fingerprint,
            inspection=inspection,
        )

    @staticmethod
    def _inspect_manifest_content(root: Path, content: str) -> ManifestInspection:
        with tempfile.TemporaryDirectory(prefix="app-engine-manifest-") as raw_temp:
            temporary = Path(raw_temp)
            (temporary / "app.json").write_text(content, encoding="utf-8")
            if (root / "index.html").is_file():
                (temporary / "index.html").touch()
            return _inspection_with_root(parse_manifest(temporary), root)

    async def apply_manifest(
        self, change: ManifestChange, accepted_plan: PlanFingerprint
    ) -> ManifestInspection:
        plan = await self.preview_manifest(change)
        if plan.fingerprint != accepted_plan:
            raise AppEngineError(
                "stale_manifest_plan",
                "The accepted manifest plan no longer matches the project.",
                remediation="Preview the manifest again and accept the new plan.",
            )
        if plan.inspection.manifest is None:
            raise AppEngineError(
                "invalid_manifest_plan",
                "An invalid manifest plan cannot be applied.",
            )
        await asyncio.to_thread(
            self._atomic_write_manifest,
            plan.change.app_root / "app.json",
            plan.change.content,
        )
        return await self._catalog.inspect(plan.change.app_root)

    @staticmethod
    def _atomic_write_manifest(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".app.json.", dir=path.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _catalog_manifest(self, app_id: AppId) -> AppManifest:
        snapshot = self._catalog.snapshot(self._catalog_key)
        for app in snapshot.apps:
            if app.manifest.app_id == app_id:
                return app.manifest
        raise ConfigurationError(
            "app_not_found",
            f"App {str(app_id)!r} is not present in the current catalog.",
            context=(("app_id", str(app_id)),),
        )

    async def configuration_form(
        self, subject: HostSubject, app_id: AppId
    ) -> ConfigurationForm:
        manifest = self._catalog_manifest(app_id)
        declared = frozenset(field.key for field in manifest.configuration)
        resolved = await self._host.configuration(subject, app_id)
        values = tuple(value for value in resolved.values if value.key in declared)
        self._configuration_context[subject] = (manifest, declared, values)
        return ConfigurationForm(
            app_id=app_id, fields=manifest.configuration, values=values
        )

    async def save_configuration(
        self, subject: HostSubject, values: tuple[ConfigurationValue, ...]
    ) -> ConfigurationResult:
        context = self._configuration_context.get(subject)
        if context is None:
            raise ConfigurationError(
                "configuration_context_required",
                "Open an app's configuration form before saving values.",
            )
        manifest, declared, existing = context
        keys = tuple(value.key for value in values)
        if len(set(keys)) != len(keys):
            raise ConfigurationError(
                "duplicate_configuration_value",
                "Each configuration key may be supplied only once.",
            )
        undeclared = tuple(key for key in keys if key not in declared)
        if undeclared:
            raise ConfigurationError(
                "undeclared_configuration_key",
                "Only configuration keys declared by the app may be saved.",
                context=tuple(("key", key) for key in undeclared),
            )
        by_key = {field.key: field for field in manifest.configuration}
        supplied = {value.key: value for value in values}
        effective = {value.key: value for value in existing}
        effective.update(supplied)
        for key, value in supplied.items():
            field = by_key[key]
            self._validate_configuration_value(field.value_type, field.choices, value)
            if field.value_type is ConfigurationType.SECRET and not value.secret:
                raise ConfigurationError(
                    "secret_flag_required",
                    f"Configuration value {key!r} must be stored as a secret.",
                    context=(("key", key),),
                )
        missing = tuple(
            field.key
            for field in manifest.configuration
            if field.required
            and not field.default_value
            and (
                field.key not in effective
                or effective[field.key].value == ""
            )
        )
        if missing:
            raise ConfigurationError(
                "required_configuration_missing",
                "Required configuration values are missing.",
                context=tuple(("key", key) for key in missing),
            )
        result = await self._host.save_configuration(
            subject, manifest.app_id, values
        )
        self._configuration_context[subject] = (
            manifest,
            declared,
            tuple(effective[key] for key in sorted(effective)),
        )
        return result

    @staticmethod
    def _validate_configuration_value(
        value_type: ConfigurationType,
        choices: tuple[str, ...],
        value: ConfigurationValue,
    ) -> None:
        valid = True
        if value_type is ConfigurationType.INTEGER:
            try:
                int(value.value)
            except ValueError:
                valid = False
        elif value_type is ConfigurationType.NUMBER:
            try:
                valid = math.isfinite(float(value.value))
            except ValueError:
                valid = False
        elif value_type is ConfigurationType.BOOLEAN:
            valid = value.value.lower() in {"true", "false"}
        elif value_type is ConfigurationType.ENUM:
            valid = value.value in choices
        if not valid:
            raise ConfigurationError(
                "invalid_configuration_value",
                f"Configuration value {value.key!r} does not match its declared type.",
                context=(("key", value.key),),
            )


__all__ = ["PortableStudioService"]
