"""Public manifest parser contract, including v1 compatibility."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app_engine.contracts import (
    AppManifest,
    BrowserPolicy,
    ConfigurationType,
    ManifestInspection,
    ProcessScope,
)
from app_engine import manifest


def _write_app(tmp_path: Path, payload: dict, *, index: bool = True) -> Path:
    root = tmp_path / "demo"
    root.mkdir(parents=True)
    (root / "app.json").write_text(json.dumps(payload), encoding="utf-8")
    if index:
        (root / "index.html").write_text("<!doctype html>", encoding="utf-8")
    return root


def _step(argv: list[str], **extra: object) -> dict:
    return {"argv": argv, **extra}


def _runtime(**overrides: object) -> dict:
    runtime = {
        "driver": "process",
        "scope": "per_user",
        "install": [_step(["python", "-m", "pip", "install", "-r", "requirements.txt"])],
        "build": [_step(["python", "-m", "compileall", "."])],
        "start": _step(["python", "server.py"]),
        "health": {
            "kind": "http",
            "path": "/health",
            "timeout_seconds": 5,
            "interval_seconds": 0.2,
        },
        "idle_timeout_seconds": 120,
        "autostart": True,
    }
    runtime.update(overrides)
    return runtime


def _target(target_id: str = "web", **overrides: object) -> dict:
    target = {"kind": "web", "runtime": _runtime()}
    target.update(overrides)
    return target


def _v2(**overrides: object) -> dict:
    payload = {
        "manifest_version": 2,
        "id": "demo",
        "label": "Demo",
        "icon": "D",
        "default_target": "web",
        "targets": {"web": _target()},
        "configuration": {
            "API_URL": {
                "type": "string",
                "label": "API URL",
                "description": "Service endpoint",
                "required": True,
                "scope": "user",
                "env": "API_URL",
            }
        },
        "metadata": {
            "version": "1.2.3",
            "description": "A demo",
            "categories": ["tools"],
            "author": "Atrium",
        },
        "browser": {
            "sandbox": "allow-scripts",
            "permissions": ["clipboard-read"],
            "allow": "clipboard-read",
            "secret": False,
            "multi": True,
            "proxy_read_timeout": 15,
        },
    }
    payload.update(overrides)
    if "targets" in overrides and overrides["targets"] is None:
        payload.pop("targets", None)
    if "default_target" in overrides and overrides["default_target"] is None:
        payload.pop("default_target", None)
    return payload


def _inspect(tmp_path: Path, payload: dict, *, index: bool = True) -> ManifestInspection:
    result = manifest.parse_manifest(_write_app(tmp_path, payload, index=index))
    assert isinstance(result, ManifestInspection)
    return result


def _error_text(result: ManifestInspection) -> str:
    return "\n".join(
        f"{issue.path}: {issue.code}: {issue.message}" for issue in result.errors
    ).lower()


def test_v1_validation_and_normalization_remain_compatible():
    errors, warnings = manifest.validate_manifest(
        {"label": "X", "icon": "x"}, has_index=True, dir_name="x"
    )
    assert errors == []
    assert any("version" in warning for warning in warnings)

    normalized = manifest.normalize({"label": "X", "icon": "x"}, "mydir")
    assert normalized["id"] == "mydir"
    assert normalized["version"] == "0.0.0"
    assert normalized["categories"] == []
    assert normalized["screenshots"] == []


def test_parse_manifest_builds_typed_v2_manifest(tmp_path):
    result = _inspect(tmp_path, _v2())

    assert result.errors == ()
    assert isinstance(result.manifest, AppManifest)
    assert result.manifest.root == tmp_path / "demo"
    assert result.manifest.manifest_version == 2
    assert result.manifest.app_id == "demo"
    assert result.manifest.default_target == "web"
    assert [target.target_id for target in result.manifest.targets] == ["web"]
    target = result.manifest.targets[0]
    assert target.kind == "web"
    assert target.runtime is not None
    assert target.runtime.scope is ProcessScope.PER_USER
    assert target.runtime.start.argv == ("python", "server.py")
    assert target.runtime.install[0].argv[:2] == ("python", "-m")
    assert target.runtime.health.path == "/health"
    assert result.manifest.configuration[0].value_type is ConfigurationType.STRING
    assert result.manifest.configuration[0].environment_name == "API_URL"
    assert result.manifest.metadata.description == "A demo"
    assert result.manifest.browser == BrowserPolicy(
        sandbox="allow-scripts",
        permissions=("clipboard-read",),
        allow="clipboard-read",
        secret=False,
        multi=True,
        proxy_read_timeout=15,
    )


def test_v2_supports_each_declared_configuration_type(tmp_path):
    configuration = {
        "TEXT": {"type": "string", "label": "Text"},
        "COUNT": {"type": "integer", "label": "Count"},
        "RATIO": {"type": "number", "label": "Ratio"},
        "ENABLED": {"type": "boolean", "label": "Enabled"},
        "MODE": {
            "type": "enum",
            "label": "Mode",
            "choices": ["fast", "safe"],
        },
        "DIRECTORY": {"type": "path", "label": "Directory"},
        "TOKEN": {"type": "secret", "label": "Token"},
    }
    result = _inspect(tmp_path, _v2(configuration=configuration))

    assert result.errors == ()
    assert tuple(field.value_type for field in result.manifest.configuration) == tuple(
        ConfigurationType
    )
    assert result.manifest.configuration[4].choices == ("fast", "safe")


def test_v2_supports_named_targets_default_and_all_process_scopes(tmp_path):
    payload = _v2(
        default_target="api",
        targets={
            "web": _target("web", runtime=None),
            "api": _target("api", runtime=_runtime(scope="shared")),
            "worker": _target("worker", runtime=_runtime(scope="per_instance")),
        },
    )
    result = _inspect(tmp_path, payload)

    assert result.errors == ()
    assert [target.target_id for target in result.manifest.targets] == [
        "web",
        "api",
        "worker",
    ]
    assert result.manifest.default_target == "api"
    assert result.manifest.targets[1].runtime.scope is ProcessScope.SHARED
    assert result.manifest.targets[2].runtime.scope is ProcessScope.PER_INSTANCE


def test_static_v2_without_targets_gets_implicit_web_target(tmp_path):
    payload = _v2(targets=None, default_target=None)
    result = _inspect(tmp_path, payload)

    assert result.errors == ()
    assert len(result.manifest.targets) == 1
    assert result.manifest.targets[0].target_id == "web"
    assert result.manifest.targets[0].kind == "web"
    assert result.manifest.targets[0].runtime is None


def test_default_target_is_optional_for_one_target_but_required_for_many(tmp_path):
    one = _inspect(tmp_path / "one", _v2(default_target=None))
    assert one.errors == ()
    assert one.manifest.default_target == "web"

    many_payload = _v2(
        default_target=None,
        targets={"web": _target("web"), "api": _target("api")},
    )
    many = _inspect(tmp_path / "many", many_payload)
    assert many.manifest is None
    assert "default_target" in _error_text(many)


def test_v1_entry_point_becomes_static_web_target_without_managed_runtime(tmp_path):
    result = _inspect(
        tmp_path,
        {
            "id": "legacy",
            "label": "Legacy",
            "icon": "L",
            "entry_point": "http://127.0.0.1:8550",
        },
        index=False,
    )

    assert result.errors == ()
    assert result.manifest.manifest_version == 1
    assert len(result.manifest.targets) == 1
    assert result.manifest.targets[0].target_id == "web"
    assert result.manifest.targets[0].kind == "web"
    assert result.manifest.targets[0].runtime is None
    assert result.manifest.targets[0].entry_point == "http://127.0.0.1:8550"


def test_unknown_safe_v2_fields_are_warnings(tmp_path):
    result = _inspect(tmp_path, _v2(future_field={"enabled": True}))

    assert result.errors == ()
    assert any("future_field" in issue.message or "future_field" in issue.path for issue in result.warnings)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("install", [{"command": "python server.py"}]),
        ("build", [_step([])]),
        ("start", {"command": "python server.py"}),
    ],
)
def test_runtime_steps_require_nonempty_argv_and_reject_shell_strings(
    tmp_path, field, value
):
    runtime = _runtime()
    runtime[field] = value
    result = _inspect(tmp_path, _v2(targets={"web": _target(runtime=runtime)}))

    assert result.manifest is None
    assert "argv" in _error_text(result)


@pytest.mark.parametrize("field", ["cwd", "inputs", "outputs"])
def test_runtime_paths_must_stay_inside_app_root(tmp_path, field):
    runtime = _runtime()
    if field == "cwd":
        runtime["start"] = _step(["python", "server.py"], cwd="../outside")
    else:
        runtime["build"] = [_step(["python", "-m", "compileall"], **{field: ["../outside"]})]
    result = _inspect(tmp_path, _v2(targets={"web": _target(runtime=runtime)}))

    assert result.manifest is None
    assert field in _error_text(result)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("driver", "unknown-driver"),
        ("scope", "global"),
    ],
)
def test_unknown_driver_and_scope_are_rejected(tmp_path, field, value):
    target = _target()
    target["runtime"] = _runtime(**{field: value})
    result = _inspect(tmp_path, _v2(targets={"web": target}))

    assert result.manifest is None
    assert field in _error_text(result)


def test_reserved_shell_target_kind_is_rejected(tmp_path):
    result = _inspect(tmp_path, _v2(targets={"web": _target(kind="shell")}))

    assert result.manifest is None
    assert "kind" in _error_text(result)


@pytest.mark.parametrize("kind", ["ios", "android", "future-provider"])
def test_safe_future_provider_kinds_are_preserved(tmp_path, kind):
    result = _inspect(tmp_path, _v2(targets={"web": _target(kind=kind)}))

    assert result.errors == ()
    assert result.manifest.targets[0].kind == kind


@pytest.mark.parametrize(
    "health",
    [
        {"kind": "tcp", "path": "/health"},
        {"kind": "http", "path": "health"},
        {"kind": "http", "path": "/health", "timeout_seconds": 0},
        {"kind": "http", "path": "/health", "interval_seconds": -1},
    ],
)
def test_health_values_are_validated(tmp_path, health):
    result = _inspect(
        tmp_path, _v2(targets={"web": _target(runtime=_runtime(health=health))})
    )

    assert result.manifest is None
    assert "health" in _error_text(result)


@pytest.mark.parametrize(
    ("payload_change", "expected"),
    [
        ({"default_target": "missing"}, "default_target"),
        ({"targets": {"": _target()}}, "target"),
        (
            {
                "targets": {"web": _target(), "bad/id": _target()},
                "default_target": "web",
            },
            "target",
        ),
    ],
)
def test_invalid_default_target_and_target_names_are_rejected(tmp_path, payload_change, expected):
    payload = _v2(**payload_change)
    result = _inspect(tmp_path, payload)

    assert result.manifest is None
    assert expected in _error_text(result)


@pytest.mark.parametrize(
    "configuration",
    [
        {"COUNT": {"type": "integer", "label": "Count", "env": "bad-name"}},
        {"COUNT": {"type": "integer", "label": "Count", "env": 42}},
        {"COUNT": {"type": "not-a-type", "label": "Count"}},
        {"MODE": {"type": "enum", "label": "Mode", "choices": "fast"}},
    ],
)
def test_configuration_types_and_environment_declarations_are_validated(tmp_path, configuration):
    result = _inspect(tmp_path, _v2(configuration=configuration))

    assert result.manifest is None
    assert "configuration" in _error_text(result)


def test_secret_configuration_schema_is_allowed_but_values_are_not(tmp_path):
    schema = {
        "TOKEN": {
            "type": "secret",
            "label": "Token",
            "required": True,
            "scope": "user",
            "env": "TOKEN",
        }
    }
    declaration = _inspect(tmp_path / "declaration", _v2(configuration=schema))
    assert declaration.errors == ()
    assert declaration.manifest.configuration[0].value_type is ConfigurationType.SECRET

    with_value = _inspect(
        tmp_path / "value",
        _v2(
            configuration={"TOKEN": {**schema["TOKEN"], "value": "do-not-accept"}},
        ),
    )
    assert with_value.manifest is None
    assert "secret" in _error_text(with_value) or "value" in _error_text(with_value)


def test_non_secret_configuration_defaults_are_typed_and_normalized(tmp_path):
    result = _inspect(
        tmp_path,
        _v2(
            configuration={
                "TEXT": {"type": "string", "label": "Text", "default": "hello"},
                "COUNT": {"type": "integer", "label": "Count", "default": 3},
                "RATIO": {"type": "number", "label": "Ratio", "default_value": 2.5},
                "ENABLED": {"type": "boolean", "label": "Enabled", "default": True},
                "MODE": {
                    "type": "enum", "label": "Mode", "choices": ["fast", "safe"],
                    "default": "safe",
                },
                "DIRECTORY": {"type": "path", "label": "Directory", "default": "data"},
            }
        ),
    )

    assert result.errors == ()
    assert [field.default_value for field in result.manifest.configuration] == [
        "hello", "3", "2.5", "true", "safe", "data",
    ]


@pytest.mark.parametrize(
    "field",
    [
        {"type": "integer", "label": "Count", "default": "3"},
        {"type": "enum", "label": "Mode", "choices": ["fast"], "default": "safe"},
        {"type": "secret", "label": "Token", "default": "nope"},
        {"type": "string", "label": "Text", "default": "a", "default_value": "a"},
    ],
)
def test_invalid_configuration_defaults_are_rejected(tmp_path, field):
    result = _inspect(tmp_path, _v2(configuration={"VALUE": field}))

    assert result.manifest is None
    assert "default" in _error_text(result)
