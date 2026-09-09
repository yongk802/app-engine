import importlib
import os

from fastapi.testclient import TestClient


def load_engine(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENGINE_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("APP_ENGINE_APPS_DIR", str(tmp_path / "apps"))
    import engine
    return importlib.reload(engine)


def _admin(module):
    return {"x-app-engine-admin": module._admin_capability}


def test_local_ai_status_is_actionable(tmp_path, monkeypatch):
    module = load_engine(tmp_path, monkeypatch)
    with TestClient(module.app) as client:
        data = client.get("/api/local-ai/status").json()
    assert data["state"] in {"ready", "setup_required", "error"}
    assert data["selected_profile"] == "quality"
    assert data["recommendation"]["profile_id"] in {"quality", "compatibility"}
    assert data["privacy"] == "Everything stays on this computer."


def test_profile_endpoint_rejects_unknown_profile(tmp_path, monkeypatch):
    module = load_engine(tmp_path, monkeypatch)
    with TestClient(module.app) as client:
        response = client.put("/api/local-ai/profile", json={"profile_id": "huge"}, headers=_admin(module))
    assert response.status_code == 400


def test_profile_endpoint_persists_selection(tmp_path, monkeypatch):
    module = load_engine(tmp_path, monkeypatch)
    with TestClient(module.app) as client:
        assert client.put("/api/local-ai/profile", json={"profile_id": "compatibility"}, headers=_admin(module)).status_code == 200
        assert client.get("/api/local-ai/status").json()["selected_profile"] == "compatibility"


def test_install_plan_requires_bound_confirmation(tmp_path, monkeypatch):
    module = load_engine(tmp_path, monkeypatch)
    with TestClient(module.app) as client:
        plan = client.get("/api/local-ai/install-plan").json()
        bad = client.post("/api/local-ai/install", json={"plan_id": plan["plan_id"], "token": "bad"}, headers=_admin(module))
        assert bad.status_code == 400
        token = client.post("/api/local-ai/authorize", json={"plan_id": plan["plan_id"]}, headers=_admin(module)).json()["token"]
    assert token


def test_diagnostics_omit_chat_content_and_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("VERY_SECRET_VALUE", "do-not-include")
    module = load_engine(tmp_path, monkeypatch)
    with TestClient(module.app) as client:
        text = client.get("/api/local-ai/diagnostics").text
    assert "do-not-include" not in text
    assert "selected_profile" in text


def test_managed_v2_catalog_is_exposed_to_launcher(tmp_path, monkeypatch):
    import json
    module = load_engine(tmp_path, monkeypatch)
    app_dir = tmp_path / 'apps' / 'game'
    app_dir.mkdir(parents=True)
    (app_dir / 'app.json').write_text(json.dumps({
        'manifest_version': 2, 'id': 'cyberpunk-tcg', 'label': 'Night City Table', 'icon': 'N',
        'default_target': 'web',
        'browser': {'sandbox': 'allow-scripts allow-same-origin', 'permissions': ['microphone']},
        'targets': [{'id': 'web', 'kind': 'web', 'runtime': {'driver': 'process', 'start': {'argv': ['python', 'server.py']}}}],
    }))
    with TestClient(module.app, base_url='http://localhost') as client:
        catalog = client.get('/api/app-engine/catalog').json()
        assert catalog['apps'], catalog['rejections']
        app = next(a for a in client.get('/api/apps').json() if a['id'] == 'cyberpunk-tcg')
        assert client.get('/api/apps/rejected').json() == []
    assert app['engine_managed'] is True
    assert 'microphone' in app['allow']
