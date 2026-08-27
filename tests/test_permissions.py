import importlib
import json

from fastapi.testclient import TestClient

from app_engine import manifest


def load_engine(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENGINE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("APP_ENGINE_APPS_DIR", str(tmp_path / "apps"))
    monkeypatch.delenv("APP_ENGINE_APP_STATE_ISOLATION", raising=False)
    import engine
    return importlib.reload(engine)


def make_app(apps_dir, name, m):
    d = apps_dir / name
    d.mkdir(parents=True)
    (d / "app.json").write_text(json.dumps(m))
    (d / "index.html").write_text("<h1>ok</h1>")


def _ref(app_id):
    return {"host": f"{app_id}.localhost", "referrer-policy": "no-referrer"}


def _admin(module):
    return {"x-app-engine-admin": module._admin_capability}


# ── manifest permission parsing ───────────────────────────────────────────────

def test_permissions_parsed_and_allow_derived():
    n = manifest.normalize(
        {"label": "X", "icon": "x", "permissions": ["microphone", "camera"]}, "x")
    assert n["permissions"] == ["microphone", "camera"]
    assert n["allow"] == "microphone; camera"


def test_unknown_permission_warns_not_errors():
    errors, warnings = manifest.validate_manifest(
        {"label": "X", "icon": "x", "version": "1.0.0", "permissions": ["microphone", "telepathy"]},
        has_index=True, dir_name="x")
    assert errors == []
    assert any("telepathy" in w for w in warnings)


def test_permissions_must_be_list():
    errors, _ = manifest.validate_manifest(
        {"label": "X", "icon": "x", "permissions": "microphone"}, has_index=True, dir_name="x")
    assert any("permissions must be a list" in e for e in errors)


def test_legacy_allow_string_merges_into_permissions():
    # Atrium's `allow` manifests remain a valid source under the shared contract.
    n = manifest.normalize({"label": "X", "icon": "x", "allow": "microphone; camera"}, "x")
    assert n["permissions"] == ["microphone", "camera"]


def test_unknown_features_dropped():
    n = manifest.normalize({"label": "X", "icon": "x", "permissions": ["camera", "telepathy"]}, "x")
    assert n["permissions"] == ["camera"]


# ── engine surfaces permissions ───────────────────────────────────────────────

def test_api_apps_exposes_permissions_and_allow(tmp_path, monkeypatch):
    make_app(tmp_path / "apps", "cam", {
        "id": "cam", "label": "Cam", "icon": "📷", "version": "1.0.0",
        "permissions": ["camera", "microphone"],
    })
    module = load_engine(tmp_path, monkeypatch)
    with TestClient(module.app) as client:
        app = client.get("/api/apps").json()[0]
    assert app["permissions"] == ["camera", "microphone"]
    assert app["allow"] == "camera; microphone"


# ── host-capability enforcement: local-AI management is launcher-only ──────────

def test_local_ai_management_denied_to_app_iframes(tmp_path, monkeypatch):
    module = load_engine(tmp_path, monkeypatch)
    with TestClient(module.app) as client:
        # An app origin cannot switch models, even with its Referer suppressed.
        r = client.put("/api/local-ai/profile", json={"profile_id": "compatibility"}, headers=_ref("evil"))
        assert r.status_code == 403
        # …nor start the runtime or remove a model
        assert client.post("/api/local-ai/start", headers=_ref("evil")).status_code == 403
        assert client.delete("/api/local-ai/models/qwen3-8b", headers=_ref("evil")).status_code == 403


def test_local_ai_management_allowed_for_launcher(tmp_path, monkeypatch):
    module = load_engine(tmp_path, monkeypatch)
    with TestClient(module.app) as client:
        assert client.put("/api/local-ai/profile", json={"profile_id": "compatibility"},
                          headers=_admin(module)).status_code == 200


def test_local_ai_status_stays_readable_by_apps(tmp_path, monkeypatch):
    module = load_engine(tmp_path, monkeypatch)
    with TestClient(module.app) as client:
        # read-only status is not gated (chat panels poll it)
        assert client.get("/api/local-ai/status", headers=_ref("notes")).status_code == 200
