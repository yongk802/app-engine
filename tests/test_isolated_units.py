"""Security properties of the root broker's transient command units."""

import json
import os
from pathlib import Path

import pytest

from app_engine.isolated_units import UnitBoundary, UnitRequest


def _boundary(tmp_path: Path) -> tuple[UnitBoundary, Path, Path, Path]:
    apps = tmp_path / "apps"
    state = tmp_path / "state"
    apps.mkdir()
    state.mkdir()
    app = apps / "demo"
    data = state / "data" / "demo" / "0123456789abcdef"
    build = state / "build" / "demo"
    for path in (app, data, build):
        path.mkdir(parents=True)
    (app / "app.json").write_text(json.dumps({"id": "demo"}))
    return UnitBoundary((apps,), state, os.getuid(), "appengine"), app, data, build


def _command(boundary: UnitBoundary, name: str, request: UnitRequest) -> tuple[str, ...]:
    return boundary.command(name, request, "app-engine-runner@7.service")


def test_unit_confines_identity_files_and_external_network(tmp_path):
    boundary, app, data, build = _boundary(tmp_path)
    request = UnitRequest("demo", "start", app, data, build, "secret-canary")

    argv = _command(boundary, "ae-demo-start-123", request)

    assert "--property=DynamicUser=yes" in argv
    assert "--property=Group=appengine" in argv
    assert "--property=UMask=0002" in argv
    assert not any("SupplementaryGroups=" in part for part in argv)
    assert "--property=ProtectHome=yes" in argv
    assert "--property=ProtectSystem=strict" in argv
    assert "--property=IPAddressDeny=any" in argv
    assert "--property=IPAddressAllow=localhost" in argv
    assert f"--property=BindPaths={app}:/workspace" in argv
    assert f"--property=BindPaths={data}:/app-data" in argv
    assert f"--property=BindPaths={build}:/app-build" in argv
    assert "secret-canary" not in " ".join(argv)
    assert "--property=InaccessiblePaths=/run/app-engine-runner.sock" in argv
    assert "--property=BindsTo=app-engine-runner@7.service" in argv


def test_unit_rejects_app_symlink_outside_allowlist(tmp_path):
    boundary, _app, data, build = _boundary(tmp_path)
    outside = tmp_path / "private"
    outside.mkdir()
    (boundary.apps_roots[0] / "escape").symlink_to(outside)

    with pytest.raises(ValueError, match="app root"):
        _command(boundary, "ae-escape-start-123", UnitRequest(
            "escape", "start", boundary.apps_roots[0] / "escape", data, build, ""
        ))


def test_unit_accepts_store_root_with_custom_folder_and_matching_manifest(tmp_path):
    boundary, _app, data, build = _boundary(tmp_path)
    store = tmp_path / "store-apps"
    store.mkdir()
    custom = store / "release-42"
    custom.mkdir()
    (custom / "app.json").write_text(json.dumps({"id": "demo"}))
    boundary = UnitBoundary((*boundary.apps_roots, store), boundary.state_root,
                            boundary.engine_uid, boundary.engine_group)

    argv = _command(boundary, "ae-demo-start-123", UnitRequest(
        "demo", "start", custom, data, build, ""
    ))
    assert f"--property=BindPaths={custom}:/workspace" in argv

    (custom / "app.json").write_text(json.dumps({"id": "other"}))
    with pytest.raises(ValueError, match="app root"):
        _command(boundary, "ae-demo-start-123", UnitRequest(
            "demo", "start", custom, data, build, ""
        ))


def test_unit_rejects_a_symlinked_parent_even_when_it_targets_allowed_root(tmp_path):
    boundary, _app, data, build = _boundary(tmp_path)
    alias = tmp_path / "alias"
    alias.symlink_to(boundary.apps_roots[0], target_is_directory=True)
    with pytest.raises(ValueError, match="app root"):
        _command(boundary, "ae-demo-start-123", UnitRequest(
            "demo", "start", alias / "demo", data, build, ""
        ))


def test_unit_accepts_scoped_data_path_but_rejects_other_app_data(tmp_path):
    boundary, app, data, build = _boundary(tmp_path)
    assert _command(boundary, "ae-demo-start-123", UnitRequest(
        "demo", "start", app, data, build, ""
    ))
    other = boundary.state_root / "data" / "other" / "0123456789abcdef"
    other.mkdir(parents=True)
    with pytest.raises(ValueError, match="data directory"):
        _command(boundary, "ae-demo-start-123", UnitRequest(
            "demo", "start", app, other, build, ""
        ))


def test_unit_rejects_mismatched_or_unsafe_unit_name(tmp_path):
    boundary, app, data, build = _boundary(tmp_path)
    request = UnitRequest("demo", "start", app, data, build, "")
    with pytest.raises(ValueError, match="unit name"):
        _command(boundary, "ae-demo-start-123;touch /tmp/x", request)


def test_unit_rejects_untrusted_broker_service_name(tmp_path):
    boundary, app, data, build = _boundary(tmp_path)
    request = UnitRequest("demo", "start", app, data, build, "")
    with pytest.raises(ValueError, match="broker unit"):
        boundary.command("ae-demo-start-123", request,
                         "app-engine-runner@7.service;evil.service")
