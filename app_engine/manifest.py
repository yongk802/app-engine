"""app.json manifest contract — schema, validation, and version compatibility.

This is the single source of truth for the **shared** app manifest that both
app-engine and Atrium honor. Keeping it here (importable, dependency-free) lets
the engine surface useful errors for malformed apps instead of silently
dropping them, and lets a `python -m app_engine.manifest <dir>` CLI validate an
app before it ships.

Backward compatible: every field except id/label/icon (and an entry point) is
optional, and unknown fields are tolerated (with a soft warning) so older and
Atrium-specific manifests keep working.
"""
from __future__ import annotations

import json
import math
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

from . import __version__
from .contracts import (
    AppId,
    AppManifest,
    AppMetadata,
    BrowserPolicy,
    CommandStep,
    ConfigurationField,
    ConfigurationType,
    EnvironmentBinding,
    EnvironmentSource,
    HealthSpec,
    ManifestInspection,
    ProcessScope,
    RuntimeSpec,
    TargetId,
    TargetSpec,
    ValidationIssue,
)
from .search import CATEGORY_IDS

# Bump when the host<->app contract changes in a way apps can depend on.
# Keep manifest compatibility checks and the distribution identity in lockstep.
# ``app_engine.__version__`` is defined without importing any runtime services.
ENGINE_VERSION = __version__

APP_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_SEMVER_RE = re.compile(r"^\d+(\.\d+){0,2}([-+][0-9A-Za-z.-]+)?$")

# Capabilities an app may request via ``permissions``. These are browser
# Permissions-Policy features, each mapping 1:1 to a token in the iframe's
# ``allow`` attribute; the browser then handles runtime consent (the mic/camera
# prompt) at point of use. Kept to a conservative allowlist so a manifest can't
# inject arbitrary policy tokens. Powerful features only work when the app's
# sandbox includes allow-same-origin (an opaque origin can't be granted them).
PERMISSION_FEATURES = frozenset({
    "microphone", "camera", "display-capture", "geolocation",
    "midi", "clipboard-read", "clipboard-write", "fullscreen", "autoplay",
})

# Fields the shared manifest contract recognizes. Atrium-only fields are listed
# so the shared validator doesn't warn about them (keeps apps portable).
KNOWN_FIELDS = frozenset({
    # identity & listing metadata
    "id", "label", "icon", "version", "description", "categories", "author", "screenshots",
    # runtime
    "sandbox", "entry_point", "min_engine_version", "permissions",
    # local-AI tutor
    "chat_enabled", "chat_system_prompt", "chat_knowledge",
    # Atrium-only (recognized here so the shared contract stays warning-free)
    "secret", "allow", "proxy_read_timeout", "launch_mode", "multi",
})


def parse_version(v: str) -> tuple[int, int, int]:
    """Parse a semver-ish string into a comparable (major, minor, patch) tuple.

    Pre-release/build metadata is ignored for ordering. Returns (0,0,0) on junk.
    """
    if not isinstance(v, str):
        return (0, 0, 0)
    core = re.split(r"[-+]", v.strip(), 1)[0]
    parts = core.split(".")
    nums: list[int] = []
    for p in parts[:3]:
        try:
            nums.append(int(p))
        except ValueError:
            nums.append(0)
    while len(nums) < 3:
        nums.append(0)
    return (nums[0], nums[1], nums[2])


def version_ge(a: str, b: str) -> bool:
    """True if version a >= version b (by major.minor.patch)."""
    return parse_version(a) >= parse_version(b)


def is_compatible(min_engine_version: str, engine_version: str = ENGINE_VERSION) -> bool:
    """True if an app declaring `min_engine_version` can run on this engine."""
    if not min_engine_version:
        return True
    return version_ge(engine_version, min_engine_version)


def _is_relative_asset(p: str) -> bool:
    """A screenshot/asset path must stay inside the app dir."""
    if not isinstance(p, str) or not p:
        return False
    if p.startswith("/") or p.startswith("\\") or ".." in Path(p).parts:
        return False
    return True


def listing_problems(description: object, categories: object) -> list[str]:
    """Search-readiness problems in an app's listing metadata.

    Every app should carry a description and at least one category from the
    shared vocabulary (``app_engine.search.CATEGORIES``) so launchers can find
    it by search and category. Discovery reports these as warnings, so older
    apps keep loading; ``python -m app_engine.manifest --strict`` fails on them.
    """
    known = ", ".join(sorted(CATEGORY_IDS))
    problems: list[str] = []
    if not isinstance(description, str) or not description.strip():
        problems.append("no description — add a 2-3 sentence summary so search can find the app")
    if categories is None or isinstance(categories, list):
        declared = [c for c in categories or () if isinstance(c, str) and c.strip()]
        if not declared:
            problems.append(f"no categories — add at least one of: {known}")
        for category in declared:
            if category not in CATEGORY_IDS:
                problems.append(f"unknown category {category!r} — use one of: {known}")
    return problems


def validate_manifest(m: dict, *, has_index: bool, dir_name: str = "") -> tuple[list[str], list[str]]:
    """Validate a parsed app.json.

    Returns (errors, warnings). A non-empty `errors` list means the app is not
    loadable and should be surfaced as rejected. `warnings` are non-fatal
    (missing version, unknown keys, …) and can be attached to a loaded app.
    """
    errors: list[str] = []
    warnings: list[str] = []

    if not isinstance(m, dict):
        return (["app.json must be a JSON object"], warnings)

    app_id = m.get("id", dir_name)
    if not isinstance(app_id, str) or not APP_ID_RE.match(app_id):
        errors.append(f"id {app_id!r} must match [a-z0-9][a-z0-9-]* (or omit it to use the folder name)")

    if not m.get("label") or not isinstance(m.get("label"), str):
        errors.append("label is required and must be a non-empty string")
    if not m.get("icon") or not isinstance(m.get("icon"), str):
        errors.append("icon is required and must be a non-empty string")

    entry_point = m.get("entry_point", "")
    if not isinstance(entry_point, str):
        errors.append("entry_point must be a string (a backend URL)")
        entry_point = ""
    if entry_point:
        try:
            parsed = urlparse(entry_point)
        except ValueError:
            parsed = None
        if (parsed is None or parsed.scheme != "http" or
                parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or
                parsed.username is not None or parsed.password is not None):
            errors.append("entry_point must be an http:// loopback URL (localhost, 127.0.0.1, or ::1)")
    if not has_index and not entry_point:
        errors.append("app has neither an index.html nor an entry_point")

    version = m.get("version", "")
    if version and not _SEMVER_RE.match(str(version)):
        errors.append(f"version {version!r} is not a valid semver (e.g. \"1.0.0\")")
    elif not version:
        warnings.append("no version field — defaulting to 0.0.0; add one for update ordering")

    mev = m.get("min_engine_version", "")
    if mev and not _SEMVER_RE.match(str(mev)):
        errors.append(f"min_engine_version {mev!r} is not a valid semver")

    cats = m.get("categories")
    if cats is not None and not (isinstance(cats, list) and all(isinstance(c, str) for c in cats)):
        errors.append("categories must be a list of strings")

    shots = m.get("screenshots")
    if shots is not None:
        if not isinstance(shots, list) or not all(isinstance(s, str) for s in shots):
            errors.append("screenshots must be a list of relative path strings")
        elif not all(_is_relative_asset(s) for s in shots):
            errors.append("screenshots must be relative paths inside the app (no '/' prefix or '..')")

    for key in ("description", "author"):
        if key in m and not isinstance(m[key], str):
            errors.append(f"{key} must be a string")
    warnings.extend(listing_problems(m.get("description"), cats))

    perms = m.get("permissions")
    if perms is not None:
        if not (isinstance(perms, list) and all(isinstance(p, str) for p in perms)):
            errors.append("permissions must be a list of capability strings")
        else:
            for p in perms:
                if p not in PERMISSION_FEATURES:
                    warnings.append(f"unknown permission {p!r} (ignored; not a recognized capability)")

    sandbox = m.get("sandbox", "allow-scripts")
    if not (sandbox is False or isinstance(sandbox, str)):
        errors.append("sandbox must be a string or false")

    for key in m:
        if key not in KNOWN_FIELDS:
            warnings.append(f"unknown manifest field {key!r} (ignored — check for a typo)")

    return (errors, warnings)


def _features_from_allow(raw: object) -> list[str]:
    """Extract recognized features from a legacy Permissions-Policy ``allow``
    string (e.g. "microphone; camera"). Kept so Atrium's `allow` manifests
    remain a valid source under the shared contract."""
    out: list[str] = []
    if isinstance(raw, str):
        for part in re.split(r"[;,]", raw):
            token = part.strip()
            name = token.split()[0].lower() if token else ""
            if name in PERMISSION_FEATURES and name not in out:
                out.append(name)
    return out


def permission_list(m: dict) -> list[str]:
    """The app's granted browser capabilities: recognized entries from
    ``permissions`` (canonical) unioned with any from a legacy ``allow`` string,
    deduped and order-stable. Unknown tokens are dropped."""
    out: list[str] = []
    perms = m.get("permissions")
    if isinstance(perms, list):
        for p in perms:
            if isinstance(p, str) and p in PERMISSION_FEATURES and p not in out:
                out.append(p)
    for f in _features_from_allow(m.get("allow", "")):
        if f not in out:
            out.append(f)
    return out


def permissions_to_allow(perms: list[str]) -> str:
    """Render a permission list into an iframe ``allow`` (Permissions-Policy)
    attribute value. The container grants each feature to the iframe's own
    origin, so no per-feature origin list is needed."""
    return "; ".join(perms)


def normalize(m: dict, dir_name: str) -> dict:
    """Return the normalized listing/runtime fields for a validated manifest."""
    permissions = permission_list(m)
    sandbox = m.get("sandbox", "allow-scripts")
    if sandbox is False:
        sandbox = ""
    return {
        "id": m.get("id", dir_name),
        "label": m["label"],
        "icon": m["icon"],
        "version": str(m.get("version", "") or "0.0.0"),
        "description": m.get("description", ""),
        "categories": list(m.get("categories", []) or []),
        "author": m.get("author", ""),
        "screenshots": [s for s in (m.get("screenshots", []) or []) if _is_relative_asset(s)],
        "min_engine_version": str(m.get("min_engine_version", "") or ""),
        "permissions": permissions,
        "allow": permissions_to_allow(permissions),
        "sandbox": sandbox,
        "chat_enabled": bool(m.get("chat_enabled", False)),
        "chat_system_prompt": m.get("chat_system_prompt", ""),
        "chat_knowledge": m.get("chat_knowledge", ""),
        "entry_point": m.get("entry_point", "") or "",
    }


_TARGET_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_ENVIRONMENT_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_V2_FIELDS = frozenset({
    "manifest_version", "default_target", "targets", "configuration", "metadata", "browser",
})
_V2_TOP_LEVEL_FIELDS = KNOWN_FIELDS | _V2_FIELDS
_TARGET_FIELDS = frozenset({"id", "kind", "runtime"})
_RUNTIME_FIELDS = frozenset({
    "driver", "scope", "install", "build", "start", "health",
    "idle_timeout_seconds", "autostart",
})
_STEP_FIELDS = frozenset({
    "id", "step_id", "argv", "cwd", "environment", "inputs", "outputs",
    "timeout_seconds",
})
_HEALTH_FIELDS = frozenset({"kind", "path", "timeout_seconds", "interval_seconds"})
_CONFIGURATION_FIELDS = frozenset({
    "key", "type", "value_type", "label", "description", "required", "scope",
    "environment", "environment_name", "env", "choices", "default", "default_value",
})
_ENVIRONMENT_FIELDS = frozenset({"name", "source", "value"})
_METADATA_FIELDS = frozenset({
    "version", "description", "categories", "author", "screenshots", "min_engine_version",
    "chat_enabled", "chat_system_prompt", "chat_knowledge",
})
_BROWSER_FIELDS = frozenset({
    "sandbox", "permissions", "allow", "secret", "multi", "proxy_read_timeout",
})


def _issue(code: str, message: str, path: str = "") -> ValidationIssue:
    return ValidationIssue(code=code, message=message, path=path)


def _unknown_fields(
    value: dict, known: frozenset[str], path: str,
    warnings: list[ValidationIssue],
) -> None:
    for key in value:
        if key not in known:
            field_path = f"{path}.{key}" if path else str(key)
            warnings.append(_issue(
                "unknown_field", "unknown manifest field is ignored", field_path,
            ))


def _contained_relative_path(value: object, root: Path) -> str | None:
    """Return a safe app-root-relative path, or ``None`` for unsafe input.

    ``resolve(strict=False)`` makes existing symlink escapes visible while still
    allowing an install/build step to name a path that does not exist yet.
    """
    if not isinstance(value, str) or not value:
        return None
    candidate = Path(value)
    if candidate.is_absolute() or value.startswith("\\") or ".." in candidate.parts:
        return None
    try:
        resolved = (root / candidate).resolve(strict=False)
        resolved.relative_to(root)
    except (OSError, ValueError):
        return None
    return value


def _safe_path_list(
    value: object, root: Path, path: str, errors: list[ValidationIssue],
) -> tuple[str, ...] | None:
    if not isinstance(value, list):
        errors.append(_issue("invalid_path_list", "must be an array of contained relative paths", path))
        return None
    paths: list[str] = []
    for index, item in enumerate(value):
        safe = _contained_relative_path(item, root)
        if safe is None:
            errors.append(_issue(
                "unsafe_path", "must be a relative path contained by the app root",
                f"{path}[{index}]",
            ))
        else:
            paths.append(safe)
    return tuple(paths)


def _parse_environment(
    value: object, path: str, errors: list[ValidationIssue], warnings: list[ValidationIssue],
) -> tuple[EnvironmentBinding, ...] | None:
    if value is None:
        return ()
    items: list[object]
    if isinstance(value, list):
        items = value
    elif isinstance(value, dict):
        # A mapping is a concise declaration form. It is intentionally limited
        # to source/value declarations; bare values could embed a secret.
        items = [dict(item, name=name) if isinstance(item, dict) else item
                 for name, item in value.items()]
    else:
        errors.append(_issue("invalid_environment", "must be an array or object of environment bindings", path))
        return None

    bindings: list[EnvironmentBinding] = []
    names: set[str] = set()
    for index, item in enumerate(items):
        item_path = f"{path}[{index}]"
        if not isinstance(item, dict):
            errors.append(_issue("invalid_environment", "environment binding must be an object", item_path))
            continue
        _unknown_fields(item, _ENVIRONMENT_FIELDS, item_path, warnings)
        name = item.get("name")
        source = item.get("source")
        binding_value = item.get("value")
        if not isinstance(name, str) or not _ENVIRONMENT_NAME_RE.match(name):
            errors.append(_issue("invalid_environment_name", "environment name must be an uppercase identifier", f"{item_path}.name"))
            continue
        if name in names:
            errors.append(_issue("duplicate_environment", "environment names must be unique", f"{item_path}.name"))
            continue
        if not isinstance(source, str) or source not in {member.value for member in EnvironmentSource}:
            errors.append(_issue("invalid_environment_source", "environment source must be literal, configuration, or host", f"{item_path}.source"))
            continue
        if not isinstance(binding_value, str) or not binding_value:
            # Do not echo an invalid value: it may be a supplied secret.
            errors.append(_issue("invalid_environment_value", "environment binding value must be a non-empty string", f"{item_path}.value"))
            continue
        names.add(name)
        bindings.append(EnvironmentBinding(name, EnvironmentSource(source), binding_value))
    return tuple(bindings)


def _parse_step(
    value: object, root: Path, path: str,
    errors: list[ValidationIssue], warnings: list[ValidationIssue],
) -> CommandStep | None:
    if not isinstance(value, dict):
        errors.append(_issue("invalid_command", "command step must be an object with an argv array", path))
        return None
    _unknown_fields(value, _STEP_FIELDS, path, warnings)
    argv = value.get("argv")
    if not isinstance(argv, list) or not argv or not all(isinstance(arg, str) and arg for arg in argv):
        errors.append(_issue("invalid_argv", "argv must be a non-empty array of strings", f"{path}.argv"))
        return None
    # A shell-style command field is deliberately not a supported alias.
    cwd = _contained_relative_path(value.get("cwd", "."), root)
    if cwd is None:
        errors.append(_issue("unsafe_path", "cwd must be a relative path contained by the app root", f"{path}.cwd"))
        return None
    inputs = _safe_path_list(value.get("inputs", []), root, f"{path}.inputs", errors)
    outputs = _safe_path_list(value.get("outputs", []), root, f"{path}.outputs", errors)
    environment = _parse_environment(value.get("environment"), f"{path}.environment", errors, warnings)
    timeout = value.get("timeout_seconds")
    if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0):
        errors.append(_issue("invalid_timeout", "timeout_seconds must be a positive number", f"{path}.timeout_seconds"))
        timeout = None
    step_id = value.get("step_id", value.get("id"))
    if step_id is not None and (not isinstance(step_id, str) or not step_id):
        errors.append(_issue("invalid_step_id", "step id must be a non-empty string", f"{path}.step_id"))
        step_id = None
    if inputs is None or outputs is None or environment is None:
        return None
    return CommandStep(
        argv=tuple(argv), step_id=step_id, cwd=cwd, environment=environment,
        inputs=inputs, outputs=outputs,
        timeout_seconds=float(timeout) if timeout is not None else None,
    )


def _parse_health(
    value: object, path: str, errors: list[ValidationIssue], warnings: list[ValidationIssue],
) -> HealthSpec | None:
    if value is None:
        return HealthSpec()
    if not isinstance(value, dict):
        errors.append(_issue("invalid_health", "health must be an HTTP health object", path))
        return None
    _unknown_fields(value, _HEALTH_FIELDS, path, warnings)
    kind = value.get("kind", "http")
    health_path = value.get("path", "/health")
    timeout = value.get("timeout_seconds", 30.0)
    interval = value.get("interval_seconds", 0.1)
    if kind != "http":
        errors.append(_issue("invalid_health", "health.kind must be http", f"{path}.kind"))
    if not isinstance(health_path, str) or not health_path.startswith("/") or "://" in health_path:
        errors.append(_issue("invalid_health", "health.path must be an absolute HTTP path", f"{path}.path"))
    for name, number in (("timeout_seconds", timeout), ("interval_seconds", interval)):
        if isinstance(number, bool) or not isinstance(number, (int, float)) or number <= 0:
            errors.append(_issue("invalid_health", f"health.{name} must be a positive number", f"{path}.{name}"))
    if errors and any(issue.path.startswith(path) for issue in errors):
        return None
    return HealthSpec(path=health_path, timeout_seconds=float(timeout), interval_seconds=float(interval))


def _parse_runtime(
    value: object, root: Path, path: str,
    errors: list[ValidationIssue], warnings: list[ValidationIssue],
) -> RuntimeSpec | None:
    if not isinstance(value, dict):
        errors.append(_issue("invalid_runtime", "runtime must be an object", path))
        return None
    _unknown_fields(value, _RUNTIME_FIELDS, path, warnings)
    driver = value.get("driver")
    if driver != "process":
        errors.append(_issue("invalid_driver", "runtime.driver must be process", f"{path}.driver"))
    scope_value = value.get("scope", ProcessScope.SHARED.value)
    try:
        scope = ProcessScope(scope_value)
    except (TypeError, ValueError):
        errors.append(_issue("invalid_scope", "runtime.scope must be shared, per_user, or per_instance", f"{path}.scope"))
        scope = ProcessScope.SHARED
    start = _parse_step(value.get("start"), root, f"{path}.start", errors, warnings)
    install_value = value.get("install", [])
    build_value = value.get("build", [])
    install: list[CommandStep] = []
    build: list[CommandStep] = []
    for phase, raw_steps, destination in (("install", install_value, install), ("build", build_value, build)):
        if not isinstance(raw_steps, list):
            errors.append(_issue("invalid_command_list", f"runtime.{phase} must be an array of command steps", f"{path}.{phase}"))
            continue
        for index, raw_step in enumerate(raw_steps):
            step = _parse_step(raw_step, root, f"{path}.{phase}[{index}]", errors, warnings)
            if step is not None:
                destination.append(step)
    health = _parse_health(value.get("health"), f"{path}.health", errors, warnings)
    idle_timeout = value.get("idle_timeout_seconds", 0.0)
    autostart = value.get("autostart", False)
    if isinstance(idle_timeout, bool) or not isinstance(idle_timeout, (int, float)) or idle_timeout < 0:
        errors.append(_issue("invalid_idle_timeout", "idle_timeout_seconds must be a non-negative number", f"{path}.idle_timeout_seconds"))
    if not isinstance(autostart, bool):
        errors.append(_issue("invalid_autostart", "autostart must be a boolean", f"{path}.autostart"))
    if driver != "process" or start is None or health is None:
        return None
    return RuntimeSpec(
        driver="process", scope=scope, install=tuple(install), build=tuple(build),
        start=start, health=health,
        idle_timeout_seconds=float(idle_timeout) if isinstance(idle_timeout, (int, float)) and not isinstance(idle_timeout, bool) and idle_timeout >= 0 else 0.0,
        autostart=autostart if isinstance(autostart, bool) else False,
    )


def _normalize_configuration_default(
    value: object, value_type: ConfigurationType, choices: tuple[str, ...],
    path: str, errors: list[ValidationIssue],
) -> str | None:
    """Validate a declaration default and normalize it for the typed DTO."""
    valid = False
    normalized = ""
    if value_type in {ConfigurationType.STRING, ConfigurationType.PATH}:
        valid = isinstance(value, str)
        normalized = value if valid else ""
    elif value_type is ConfigurationType.INTEGER:
        valid = isinstance(value, int) and not isinstance(value, bool)
        normalized = str(value) if valid else ""
    elif value_type is ConfigurationType.NUMBER:
        valid = (isinstance(value, (int, float)) and not isinstance(value, bool)
                 and math.isfinite(value))
        normalized = str(value) if valid else ""
    elif value_type is ConfigurationType.BOOLEAN:
        valid = isinstance(value, bool)
        normalized = "true" if value is True else "false" if value is False else ""
    elif value_type is ConfigurationType.ENUM:
        valid = isinstance(value, str) and value in choices
        normalized = value if valid else ""
    if not valid:
        errors.append(_issue(
            "invalid_configuration_default",
            "configuration default does not match its declared type",
            path,
        ))
        return None
    return normalized


def _parse_configuration(
    value: object, errors: list[ValidationIssue], warnings: list[ValidationIssue],
) -> tuple[ConfigurationField, ...]:
    if value is None:
        return ()
    entries: list[tuple[str | None, object, str]]
    if isinstance(value, list):
        entries = [(None, item, f"configuration[{index}]") for index, item in enumerate(value)]
    elif isinstance(value, dict):
        entries = [(str(key), item, f"configuration.{key}") for key, item in value.items()]
    else:
        errors.append(_issue("invalid_configuration", "configuration must be an array or object of field declarations", "configuration"))
        return ()
    fields: list[ConfigurationField] = []
    keys: set[str] = set()
    for map_key, raw, path in entries:
        if not isinstance(raw, dict):
            errors.append(_issue("invalid_configuration", "configuration field must be an object", path))
            continue
        _unknown_fields(raw, _CONFIGURATION_FIELDS, path, warnings)
        # Configuration is a declaration only. Never let a supplied value or
        # secret enter the inspection response.
        for forbidden in ("value", "secret"):
            if forbidden in raw:
                errors.append(_issue("configuration_value_forbidden", "configuration declarations must not include values or secrets", path))
                break
        key = raw.get("key", map_key)
        raw_type = raw.get("type", raw.get("value_type", "string"))
        label = raw.get("label", key)
        description = raw.get("description", "")
        required = raw.get("required", False)
        scope_value = raw.get("scope", ProcessScope.PER_USER.value)
        environment_name = raw.get("environment", raw.get("environment_name", raw.get("env")))
        choices = raw.get("choices", [])
        default_marker = object()
        default = raw.get("default", default_marker)
        default_value = raw.get("default_value", default_marker)
        if not isinstance(key, str) or not key or not re.match(r"^[A-Za-z][A-Za-z0-9_-]*$", key):
            errors.append(_issue("invalid_configuration_key", "configuration key must be an identifier", f"{path}.key"))
            continue
        if key in keys:
            errors.append(_issue("duplicate_configuration", "configuration keys must be unique", f"{path}.key"))
            continue
        try:
            value_type = ConfigurationType(raw_type)
        except (TypeError, ValueError):
            errors.append(_issue("invalid_configuration_type", "configuration type is not supported", f"{path}.type"))
            continue
        if not isinstance(label, str) or not label:
            errors.append(_issue("invalid_configuration_label", "configuration label must be a non-empty string", f"{path}.label"))
            continue
        if not isinstance(description, str) or not isinstance(required, bool):
            errors.append(_issue("invalid_configuration", "configuration description must be a string and required must be a boolean", path))
            continue
        scope_aliases = {
            "user": ProcessScope.PER_USER.value,
            "per-user": ProcessScope.PER_USER.value,
            "instance": ProcessScope.PER_INSTANCE.value,
            "per-instance": ProcessScope.PER_INSTANCE.value,
        }
        try:
            normalized_scope = scope_aliases.get(scope_value, scope_value) if isinstance(scope_value, str) else scope_value
            field_scope = ProcessScope(normalized_scope)
        except (TypeError, ValueError):
            errors.append(_issue("invalid_scope", "configuration scope must be shared, per_user, or per_instance", f"{path}.scope"))
            continue
        if environment_name is not None and (not isinstance(environment_name, str) or not _ENVIRONMENT_NAME_RE.match(environment_name)):
            errors.append(_issue("invalid_environment_name", "configuration environment must be an uppercase identifier", f"{path}.environment"))
            continue
        if not isinstance(choices, list) or not all(isinstance(choice, str) and choice for choice in choices):
            errors.append(_issue("invalid_configuration_choices", "configuration choices must be an array of strings", f"{path}.choices"))
            continue
        if value_type is ConfigurationType.ENUM and not choices:
            errors.append(_issue("invalid_configuration_choices", "enum configuration requires choices", f"{path}.choices"))
            continue
        if default is not default_marker and default_value is not default_marker:
            errors.append(_issue("ambiguous_configuration_default", "use either default or default_value, not both", path))
            continue
        supplied_default = default if default is not default_marker else default_value
        normalized_default: str | None = None
        if supplied_default is not default_marker:
            if value_type is ConfigurationType.SECRET:
                errors.append(_issue("configuration_value_forbidden", "secret configuration declarations must not include a default", path))
                continue
            normalized_default = _normalize_configuration_default(
                supplied_default, value_type, tuple(choices), path, errors,
            )
            if normalized_default is None:
                continue
        keys.add(key)
        fields.append(ConfigurationField(
            key=key, value_type=value_type, label=label, description=description,
            required=required, scope=field_scope, environment_name=environment_name,
            default_value=normalized_default, choices=tuple(choices),
        ))
    return tuple(fields)


def _v1_manifest(
    raw: dict, root: Path, normalized: dict,
) -> AppManifest:
    """Lift the legacy static/proxied surface into its sole typed web target."""
    metadata = AppMetadata(
        version=normalized["version"], description=normalized["description"],
        categories=tuple(normalized["categories"]), author=normalized["author"],
        screenshots=tuple(normalized["screenshots"]),
        min_engine_version=normalized["min_engine_version"],
        chat_enabled=normalized["chat_enabled"],
        chat_system_prompt=normalized["chat_system_prompt"],
        chat_knowledge=normalized["chat_knowledge"],
    )
    return AppManifest(
        manifest_version=1, app_id=AppId(normalized["id"]), label=normalized["label"],
        icon=normalized["icon"], root=root, default_target=TargetId("web"),
        targets=(
            TargetSpec(
                TargetId("web"),
                "web",
                None,
                normalized["entry_point"] or None,
            ),
        ),
        configuration=(),
        metadata=metadata,
        browser=BrowserPolicy(
            sandbox=normalized["sandbox"], permissions=tuple(normalized["permissions"]),
            allow=normalized["allow"], secret=bool(raw.get("secret", False)),
            multi=bool(raw.get("multi", False)),
            proxy_read_timeout=float(raw.get("proxy_read_timeout", 30.0)),
        ),
    )


def _v2_listing_values(
    raw: dict, errors: list[ValidationIssue], warnings: list[ValidationIssue],
) -> dict:
    """Merge v2 nested metadata/browser declarations with legacy top-level keys.

    Top-level keys keep their v1 meaning and take precedence when both forms
    occur.  This permits a gradual migration without duplicating listing data.
    """
    effective = dict(raw)
    for section, fields in (("metadata", _METADATA_FIELDS), ("browser", _BROWSER_FIELDS)):
        nested = raw.get(section)
        if nested is None:
            continue
        if not isinstance(nested, dict):
            errors.append(_issue("invalid_manifest_section", f"{section} must be an object", section))
            continue
        _unknown_fields(nested, fields, section, warnings)
        for field in fields:
            if field in nested and field not in effective:
                effective[field] = nested[field]
    return effective


def _target_entries(
    value: object, errors: list[ValidationIssue],
) -> list[tuple[str, object, str]]:
    """Accept canonical named-target arrays and the mapping shorthand."""
    if value is None:
        return []
    if isinstance(value, dict):
        return [(name, target, f"targets.{name}") for name, target in value.items()]
    if isinstance(value, list):
        entries: list[tuple[str, object, str]] = []
        for index, target in enumerate(value):
            path = f"targets[{index}]"
            if not isinstance(target, dict):
                errors.append(_issue("invalid_target", "target must be an object", path))
                continue
            target_id = target.get("id")
            if not isinstance(target_id, str):
                errors.append(_issue("invalid_target_id", "target id must be a string", f"{path}.id"))
                continue
            entries.append((target_id, target, path))
        return entries
    errors.append(_issue("invalid_targets", "v2 targets must be an array of named targets or an object keyed by target id", "targets"))
    return []


def parse_manifest(app_root: Path) -> ManifestInspection:
    """Read and normalize a v1 or v2 app manifest without raising on user data.

    The returned ``ManifestInspection`` is deliberately the boundary for
    untrusted app files: callers can display its typed issues directly and
    never need to recover from a JSON, path, or type exception.
    """
    root = Path(app_root)
    errors: list[ValidationIssue] = []
    warnings: list[ValidationIssue] = []
    try:
        root = root.resolve(strict=False)
        content = (root / "app.json").read_text(encoding="utf-8")
        raw = json.loads(content)
    except FileNotFoundError:
        return ManifestInspection(root=root, manifest=None, errors=(_issue("manifest_not_found", "app.json was not found", "app.json"),))
    except json.JSONDecodeError:
        return ManifestInspection(root=root, manifest=None, errors=(_issue("invalid_json", "app.json is not valid JSON", "app.json"),))
    except (OSError, TypeError, ValueError):
        return ManifestInspection(root=root, manifest=None, errors=(_issue("manifest_unreadable", "app.json could not be read", "app.json"),))

    if not isinstance(raw, dict):
        return ManifestInspection(root=root, manifest=None, errors=(_issue("invalid_manifest", "app.json must be a JSON object", "app.json"),))

    version = raw.get("manifest_version", 1)
    if isinstance(version, bool) or not isinstance(version, int) or version not in (1, 2):
        return ManifestInspection(root=root, manifest=None, errors=(_issue("unsupported_manifest_version", "manifest_version must be 1 or 2", "manifest_version"),))
    has_index = (root / "index.html").is_file()

    if version == 1:
        legacy_errors, legacy_warnings = validate_manifest(raw, has_index=has_index, dir_name=root.name)
        errors.extend(_issue("invalid_manifest", message) for message in legacy_errors)
        warnings.extend(_issue("manifest_warning", message) for message in legacy_warnings)
        if errors:
            return ManifestInspection(root=root, manifest=None, errors=tuple(errors), warnings=tuple(warnings))
        proxy_timeout = raw.get("proxy_read_timeout", 30.0)
        if (isinstance(proxy_timeout, bool) or not isinstance(proxy_timeout, (int, float))
                or proxy_timeout <= 0):
            errors.append(_issue("invalid_browser_policy", "proxy_read_timeout must be a positive number", "proxy_read_timeout"))
            return ManifestInspection(root=root, manifest=None, errors=tuple(errors), warnings=tuple(warnings))
        return ManifestInspection(root=root, manifest=_v1_manifest(raw, root, normalize(raw, root.name)), warnings=tuple(warnings))

    _unknown_fields(raw, _V2_TOP_LEVEL_FIELDS, "", warnings)
    effective_raw = _v2_listing_values(raw, errors, warnings)
    target_entries = _target_entries(raw.get("targets"), errors)
    # v2 can be an API-only process application; count an explicit target as a
    # valid launch surface when preserving the v1 base validation rules.
    legacy_raw = {key: value for key, value in effective_raw.items() if key in KNOWN_FIELDS}
    legacy_errors, legacy_warnings = validate_manifest(
        legacy_raw, has_index=has_index or bool(target_entries), dir_name=root.name,
    )
    errors.extend(_issue("invalid_manifest", message) for message in legacy_errors)
    warnings.extend(_issue("manifest_warning", message) for message in legacy_warnings)
    for name in ("chat_enabled",):
        if name in effective_raw and not isinstance(effective_raw[name], bool):
            errors.append(_issue("invalid_metadata", f"{name} must be a boolean", name))
    for name in ("chat_system_prompt", "chat_knowledge"):
        if name in effective_raw and not isinstance(effective_raw[name], str):
            errors.append(_issue("invalid_metadata", f"{name} must be a string", name))

    targets: list[TargetSpec] = []
    explicit_ids: set[str] = set()
    for target_name, target_value, target_path in target_entries:
        if not isinstance(target_name, str) or not _TARGET_ID_RE.match(target_name):
            errors.append(_issue("invalid_target_id", "target id must be a lowercase identifier", target_path))
            continue
        if not isinstance(target_value, dict):
            errors.append(_issue("invalid_target", "target must be an object", target_path))
            continue
        _unknown_fields(target_value, _TARGET_FIELDS, target_path, warnings)
        kind = target_value.get("kind", "web")
        if (not isinstance(kind, str) or not _TARGET_ID_RE.match(kind)
                or kind == "shell"):
            errors.append(_issue(
                "invalid_target_kind",
                "target kind must be a safe provider identifier and cannot be shell",
                f"{target_path}.kind",
            ))
            continue
        runtime_value = target_value.get("runtime")
        runtime = None
        if runtime_value is not None:
            runtime = _parse_runtime(runtime_value, root, f"{target_path}.runtime", errors, warnings)
        targets.append(TargetSpec(TargetId(target_name), kind, runtime))
        explicit_ids.add(target_name)

    # index.html is always a routable static web target. It is intentionally
    # implicit so v2 process declarations do not have to duplicate it.
    if has_index and "web" not in explicit_ids:
        targets.append(TargetSpec(TargetId("web"), "web", None))
    if not targets:
        errors.append(_issue("missing_target", "app must declare a target or include index.html", "targets"))

    default_target = raw.get("default_target")
    if default_target is None:
        if len(explicit_ids) > 1:
            errors.append(_issue("missing_default_target", "default_target is required when more than one explicit target is declared", "default_target"))
            selected_target = "web"
        elif len(explicit_ids) == 1:
            selected_target = next(iter(explicit_ids))
        else:
            selected_target = "web"
    elif not isinstance(default_target, str):
        errors.append(_issue("invalid_default_target", "default_target must be a target id", "default_target"))
        selected_target = "web"
    else:
        selected_target = default_target
    if selected_target not in {str(target.target_id) for target in targets}:
        errors.append(_issue("unknown_default_target", "default_target must name a declared or implicit target", "default_target"))

    configuration = _parse_configuration(raw.get("configuration"), errors, warnings)
    normalized = normalize(effective_raw, root.name)
    metadata = AppMetadata(
        version=normalized["version"], description=normalized["description"],
        categories=tuple(normalized["categories"]), author=normalized["author"],
        screenshots=tuple(normalized["screenshots"]),
        min_engine_version=normalized["min_engine_version"],
        chat_enabled=normalized["chat_enabled"],
        chat_system_prompt=normalized["chat_system_prompt"],
        chat_knowledge=normalized["chat_knowledge"],
    )
    sandbox = normalized["sandbox"]
    proxy_timeout = effective_raw.get("proxy_read_timeout", 30.0)
    if not isinstance(proxy_timeout, (int, float)) or isinstance(proxy_timeout, bool) or proxy_timeout <= 0:
        errors.append(_issue("invalid_browser_policy", "proxy_read_timeout must be a positive number", "proxy_read_timeout"))
        proxy_timeout = 30.0
    for name in ("secret", "multi"):
        if name in effective_raw and not isinstance(effective_raw[name], bool):
            errors.append(_issue("invalid_browser_policy", f"{name} must be a boolean", name))
    manifest = AppManifest(
        manifest_version=2, app_id=AppId(normalized["id"]), label=normalized["label"],
        icon=normalized["icon"], root=root, default_target=TargetId(selected_target),
        targets=tuple(targets), configuration=configuration, metadata=metadata,
        browser=BrowserPolicy(
            sandbox=sandbox, permissions=tuple(normalized["permissions"]), allow=normalized["allow"],
            secret=effective_raw.get("secret", False) if isinstance(effective_raw.get("secret", False), bool) else False,
            multi=effective_raw.get("multi", False) if isinstance(effective_raw.get("multi", False), bool) else False,
            proxy_read_timeout=float(proxy_timeout),
        ),
    )
    return ManifestInspection(
        root=root, manifest=None if errors else manifest,
        errors=tuple(errors), warnings=tuple(warnings),
    )


def _cli(argv: list[str]) -> int:
    strict = "--strict" in argv
    paths = [arg for arg in argv if arg != "--strict"]
    if not paths:
        print(
            "usage: python -m app_engine.manifest [--strict] <app-dir> [<app-dir> ...]\n"
            "  --strict  also fail apps missing a description or a known category",
            file=sys.stderr,
        )
        return 2
    rc = 0
    for arg in paths:
        d = Path(arg)
        inspection = parse_manifest(d)
        label = str(inspection.manifest.app_id) if inspection.manifest else d.name
        metadata = inspection.manifest.metadata if inspection.manifest else None
        listing = (
            listing_problems(metadata.description, list(metadata.categories))
            if strict and metadata is not None else []
        )
        if inspection.errors or listing:
            rc = 1
            print(f"✗ {label}: {len(inspection.errors) + len(listing)} error(s)")
            for issue in inspection.errors:
                suffix = f" ({issue.path})" if issue.path else ""
                print(f"    error:   {issue.message}{suffix}")
            for message in listing:
                print(f"    error:   {message}")
        else:
            print(f"✓ {label}: valid" + (f" (engine {ENGINE_VERSION})" if not inspection.warnings else ""))
        for issue in inspection.warnings:
            if issue.message in listing:
                continue
            suffix = f" ({issue.path})" if issue.path else ""
            print(f"    warning: {issue.message}{suffix}")
    return rc


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv[1:]))
