import importlib

from fastapi.testclient import TestClient

from app_engine.config import ConfigStore, InvalidEndpointError
from app_engine.ollama import ConfirmationError


def load_engine(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENGINE_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("APP_ENGINE_APPS_DIR", str(tmp_path / "apps"))
    import engine
    return importlib.reload(engine)


def _admin(module):
    return {"x-app-engine-admin": module._admin_capability}


def test_unknown_model_cannot_be_pulled(tmp_path, monkeypatch):
    module = load_engine(tmp_path, monkeypatch)
    with TestClient(module.app) as client:
        assert client.post("/api/local-ai/pull/arbitrary-shell-text", headers=_admin(module)).status_code == 404


def test_unknown_or_unmanaged_model_cannot_be_deleted(tmp_path, monkeypatch):
    module = load_engine(tmp_path, monkeypatch)
    with TestClient(module.app) as client:
        assert client.delete("/api/local-ai/models/qwen3-8b", headers=_admin(module)).status_code == 400
        assert client.delete("/api/local-ai/models/arbitrary", headers=_admin(module)).status_code == 400


def test_app_static_route_rejects_symlink_escape(tmp_path, monkeypatch):
    root = tmp_path / "apps" / "demo"; root.mkdir(parents=True)
    (root / "app.json").write_text('{"id":"demo","label":"Demo","icon":"D"}')
    (root / "index.html").write_text("safe")
    secret = tmp_path / "outside.txt"; secret.write_text("secret")
    (root / "escape.txt").symlink_to(secret)
    module = load_engine(tmp_path, monkeypatch)
    with TestClient(module.app) as client:
        assert client.get("/apps/demo/escape.txt", headers={"host": "demo.localhost"}).status_code == 404


def test_entry_point_must_be_loopback():
    from app_engine.manifest import validate_manifest
    for url in ("http://169.254.169.254/latest/meta-data", "https://example.com", "file:///etc/passwd"):
        errors, _ = validate_manifest({"id":"x","label":"X","icon":"X","entry_point":url},
                                      has_index=False, dir_name="x")
        assert any("loopback" in error for error in errors)


def test_manifest_html_is_data_not_launcher_markup(tmp_path, monkeypatch):
    apps = tmp_path / "apps"; bad = apps / "bad"; bad.mkdir(parents=True)
    (bad / "app.json").write_text('{"id":"bad","label":"<img src=x onerror=alert(1)>","icon":"x","sandbox":"allow-scripts"}')
    (bad / "index.html").write_text("safe")
    module = load_engine(tmp_path, monkeypatch)
    with TestClient(module.app) as client:
        assert client.get("/api/apps").json()[0]["label"].startswith("<img")
        launcher = client.get("/").text
    assert "btn.innerHTML" not in launcher
    # label is inserted via element('span','label', app.label) → node.textContent=text
    assert "element('span','label', app.label)" in launcher
    assert "node.textContent=text" in launcher


def test_dns_rebinding_host_cannot_reach_launcher_or_admin(tmp_path, monkeypatch):
    module = load_engine(tmp_path, monkeypatch)
    hostile = {"host": "attacker.example", **_admin(module)}
    with TestClient(module.app) as client:
        assert client.get("/", headers={"host": "attacker.example"}).status_code == 403
        assert client.get("/api/apps", headers={"host": "attacker.example"}).status_code == 403
        assert client.put("/api/local-ai/profile", json={"profile_id": "compatibility"},
                          headers=hostile).status_code == 403


def test_local_ai_config_rejects_lan_and_public_endpoints(tmp_path):
    store = ConfigStore(tmp_path)
    for endpoint in ("http://192.168.1.5:11434", "https://ollama.example.com"):
        try:
            store.set_endpoint(endpoint)
        except InvalidEndpointError:
            continue
        raise AssertionError(f"accepted non-loopback endpoint {endpoint}")
