import importlib

from fastapi.testclient import TestClient

from app_engine.config import ConfigStore, InvalidEndpointError
from app_engine.ollama import ConfirmationError


def load_engine(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENGINE_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("APP_ENGINE_APPS_DIR", str(tmp_path / "apps"))
    import engine
    return importlib.reload(engine)


def test_unknown_model_cannot_be_pulled(tmp_path, monkeypatch):
    module = load_engine(tmp_path, monkeypatch)
    with TestClient(module.app) as client:
        assert client.post("/api/local-ai/pull/arbitrary-shell-text").status_code == 404


def test_unknown_or_unmanaged_model_cannot_be_deleted(tmp_path, monkeypatch):
    module = load_engine(tmp_path, monkeypatch)
    with TestClient(module.app) as client:
        assert client.delete("/api/local-ai/models/qwen3-8b").status_code == 400
        assert client.delete("/api/local-ai/models/arbitrary").status_code == 400


def test_manifest_html_is_data_not_launcher_markup(tmp_path, monkeypatch):
    apps = tmp_path / "apps"; bad = apps / "bad"; bad.mkdir(parents=True)
    (bad / "app.json").write_text('{"id":"bad","label":"<img src=x onerror=alert(1)>","icon":"x","sandbox":"allow-scripts"}')
    (bad / "index.html").write_text("safe")
    module = load_engine(tmp_path, monkeypatch)
    with TestClient(module.app) as client:
        assert client.get("/api/apps").json()[0]["label"].startswith("<img")
        launcher = client.get("/").text
    assert "btn.innerHTML" not in launcher
    assert "label.textContent = app.label" in launcher


def test_local_ai_config_rejects_lan_and_public_endpoints(tmp_path):
    store = ConfigStore(tmp_path)
    for endpoint in ("http://192.168.1.5:11434", "https://ollama.example.com"):
        try:
            store.set_endpoint(endpoint)
        except InvalidEndpointError:
            continue
        raise AssertionError(f"accepted non-loopback endpoint {endpoint}")
