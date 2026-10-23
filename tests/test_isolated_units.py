"""Security properties of the root broker's transient command units."""

import json
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name == "nt", reason="Linux systemd units")

from app_engine.isolated_units import PackageTrackerCapability, UnitBoundary, UnitRequest


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
    assert f"--property=BindReadOnlyPaths={app}:/workspace" in argv
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
    assert f"--property=BindReadOnlyPaths={custom}:/workspace" in argv

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


def test_multiplayer_unit_uses_existing_room_state_and_private_room_code(tmp_path):
    boundary, app, _data, build = _boundary(tmp_path)
    room_root = tmp_path / "multiplayer"
    rooms = room_root / "demo"
    rooms.mkdir(parents=True)
    boundary = UnitBoundary(boundary.apps_roots, boundary.state_root,
                            boundary.engine_uid, boundary.engine_group, room_root)
    request = UnitRequest("demo", "multiplayer", app, rooms, build, "")

    argv = _command(boundary, "ae-demo-multiplayer-123", request)

    assert f"--property=BindPaths={rooms}:/app-data" in argv
    assert not any("/app-build" in part for part in argv)
    assert "--property=BindReadOnlyPaths=/usr/local/libexec/app-engine/multiplayer:/room-server" in argv
    assert "--property=IPAddressDeny=any" in argv
    assert "--property=DynamicUser=yes" in argv

    other = room_root / "other"
    other.mkdir()
    with pytest.raises(ValueError, match="data directory"):
        _command(boundary, "ae-demo-multiplayer-123", UnitRequest(
            "demo", "multiplayer", app, other, build, ""
        ))


def test_package_tracker_capabilities_apply_only_to_its_start_phase(tmp_path):
    boundary, _app, _data, _build = _boundary(tmp_path)
    apps, state = boundary.apps_roots[0], boundary.state_root
    tracker = apps / "package-tracker"
    data = state / "data" / "package-tracker" / ("a" * 16)
    build = state / "build" / "package-tracker"
    for path in (tracker, data, build):
        path.mkdir(parents=True)
    (tracker / "app.json").write_text(json.dumps({"id": "package-tracker"}))
    carrier = tmp_path / "carrier-token"
    worker = tmp_path / "worker-token"
    for path, token in ((carrier, "a" * 64), (worker, "b" * 64)):
        path.write_text(token + "\n")
        path.chmod(0o600)
    mtls = tmp_path / "mtls"
    mtls.mkdir()
    cap = PackageTrackerCapability(mtls, carrier, worker, 8788, 8799)
    boundary = UnitBoundary(boundary.apps_roots, state, boundary.engine_uid,
                            boundary.engine_group, package_tracker=cap)
    request = UnitRequest("package-tracker", "start", tracker, data, build,
                          json.dumps({"version": 1, "argv": ["/usr/bin/python3"],
                                      "cwd": "/workspace", "env": {}}))
    prepared = boundary.prepare_request(request)
    env = json.loads(prepared.payload)["env"]
    assert env["PACKAGE_TRACKER_CARRIER_PROXY_TOKEN"] == "a" * 64
    assert env["PACKAGE_TRACKER_BROWSER_WORKER_TOKEN"] == "b" * 64
    assert env["ATRIUM_BASE_URL"] == "https://atrium-status.localhost:9444"
    assert env["PACKAGE_TRACKER_SHARED_STATE"] == "1"
    argv = _command(boundary, "ae-package-tracker-start-123", prepared)
    assert f"--property=BindReadOnlyPaths={mtls}:/run/package-tracker/mtls" in argv
    assert "a" * 64 not in " ".join(argv)
    assert "b" * 64 not in " ".join(argv)

    install = UnitRequest("package-tracker", "install", tracker, data, build, request.payload)
    assert boundary.prepare_request(install) == install
    assert not any("/run/package-tracker/mtls" in item for item in _command(
        boundary, "ae-package-tracker-install-123", install))
    demo = UnitRequest("demo", "start", _app, _data, _build, request.payload)
    assert boundary.prepare_request(demo) == demo
    assert not any("/run/package-tracker/mtls" in item for item in _command(
        boundary, "ae-demo-start-123", demo))
