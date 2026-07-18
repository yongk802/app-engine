import importlib
import os

from fastapi.testclient import TestClient


def load_engine(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENGINE_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("APP_ENGINE_APPS_DIR", str(tmp_path / "apps"))
    import engine
    return importlib.reload(engine)


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
        response = client.put("/api/local-ai/profile", json={"profile_id": "huge"})
    assert response.status_code == 400


def test_profile_endpoint_persists_selection(tmp_path, monkeypatch):
    module = load_engine(tmp_path, monkeypatch)
    with TestClient(module.app) as client:
        assert client.put("/api/local-ai/profile", json={"profile_id": "compatibility"}).status_code == 200
        assert client.get("/api/local-ai/status").json()["selected_profile"] == "compatibility"


def test_install_plan_requires_bound_confirmation(tmp_path, monkeypatch):
    module = load_engine(tmp_path, monkeypatch)
    with TestClient(module.app) as client:
        plan = client.get("/api/local-ai/install-plan").json()
        bad = client.post("/api/local-ai/install", json={"plan_id": plan["plan_id"], "token": "bad"})
        assert bad.status_code == 400
        token = client.post("/api/local-ai/authorize", json={"plan_id": plan["plan_id"]}).json()["token"]
    assert token


def test_diagnostics_omit_chat_content_and_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("VERY_SECRET_VALUE", "do-not-include")
    module = load_engine(tmp_path, monkeypatch)
    with TestClient(module.app) as client:
        text = client.get("/api/local-ai/diagnostics").text
    assert "do-not-include" not in text
    assert "selected_profile" in text
