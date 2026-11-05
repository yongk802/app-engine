import importlib
import asyncio
import ipaddress
import os
import socket
import subprocess
import sys
import time

import httpx
import pytest

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


def test_direct_asgi_public_bind_cannot_expose_local_owner_api(tmp_path, monkeypatch):
    module = load_engine(tmp_path, monkeypatch)
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
             "method": "GET", "scheme": "http", "path": "/api/apps",
             "raw_path": b"/api/apps", "query_string": b"",
             "root_path": "", "headers": [(b"host", b"127.0.0.1")],
             "client": ("203.0.113.10", 9000), "server": ("0.0.0.0", 8770)}
    messages = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    asyncio.run(module.app(scope, receive, send))
    assert next(message["status"] for message in messages
                if message["type"] == "http.response.start") == 503


@pytest.mark.skipif(os.name == "nt", reason="Windows runner firewall blocks non-loopback self-connections; direct ASGI boundary is tested above")
def test_direct_uvicorn_public_bind_denies_remote_owner_api(tmp_path):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route:
        try:
            route.connect(("1.1.1.1", 443))
            interface = route.getsockname()[0]
        except OSError:
            pytest.skip("No non-loopback interface to test")
    if ipaddress.ip_address(interface).is_loopback:
        pytest.skip("No non-loopback interface to test")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = {**os.environ, "APP_ENGINE_STATE_DIR": str(tmp_path / "state"),
           "APP_ENGINE_APPS_DIR": str(tmp_path / "apps")}
    env.pop("APP_ENGINE_PUBLIC_ORIGIN", None)
    process = subprocess.Popen([sys.executable, "-m", "uvicorn", "engine:app",
                                "--host", "0.0.0.0", "--port", str(port),
                                "--no-access-log"], cwd=os.path.dirname(__file__) + "/..",
                               env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        for _ in range(100):
            if process.poll() is not None:
                raise AssertionError(f"uvicorn exited: {process.stderr.read().decode()}")
            try:
                response = httpx.get(f"http://{interface}:{port}/api/apps", timeout=0.2,
                                     trust_env=False)
                assert response.status_code == 503
                break
            except httpx.ConnectError:
                time.sleep(0.05)
        else:
            raise AssertionError("uvicorn did not start")
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


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
        # The host gate refuses an unknown name before any route sees it.
        assert client.get("/", headers={"host": "attacker.example"}).status_code == 421
        assert client.get("/api/apps", headers={"host": "attacker.example"}).status_code == 421
        assert client.put("/api/local-ai/profile", json={"profile_id": "compatibility"},
                          headers=hostile).status_code == 421


def test_local_ai_config_rejects_lan_and_public_endpoints(tmp_path):
    store = ConfigStore(tmp_path)
    for endpoint in ("http://192.168.1.5:11434", "https://ollama.example.com"):
        try:
            store.set_endpoint(endpoint)
        except InvalidEndpointError:
            continue
        raise AssertionError(f"accepted non-loopback endpoint {endpoint}")


def test_json_api_answers_are_never_sniffed(tmp_path, monkeypatch):
    module = load_engine(tmp_path, monkeypatch)
    with TestClient(module.app) as client:
        for response in (client.get("/api/apps"), client.get("/api/engine"),
                         client.get("/api/apps/nope-not-here/health")):
            assert response.headers["content-type"].startswith("application/json")
            assert response.headers["x-content-type-options"] == "nosniff"
        page = client.get("/")
        assert page.headers["x-content-type-options"] == "nosniff"
        assert page.headers.get_list("x-content-type-options") == ["nosniff"], "never doubled"


@pytest.mark.parametrize("method,path", [
    ("POST", "/api/app-engine/studio/create/preview"),
    ("POST", "/api/app-engine/studio/create"),
    ("POST", "/api/app-engine/studio/import"),
    ("POST", "/api/app-engine/studio/manifest/preview"),
    ("POST", "/api/app-engine/studio/manifest/apply"),
])
def test_studio_answers_malformed_requests_with_400_not_a_crash(tmp_path, monkeypatch, method, path):
    module = load_engine(tmp_path, monkeypatch)
    with TestClient(module.app, raise_server_exceptions=False) as client:
        for body in ({}, {"root": "x"}, ["a", "list"], "not json"):
            kwargs = {"content": body, "headers": {"content-type": "application/json"}} if isinstance(body, str) else {"json": body}
            assert client.request(method, path, **kwargs).status_code == 400, (path, body)


def test_configuration_of_an_unknown_app_is_404_not_a_crash(tmp_path, monkeypatch):
    module = load_engine(tmp_path, monkeypatch)
    with TestClient(module.app, raise_server_exceptions=False) as client:
        assert client.get("/api/app-engine/apps/nope/configuration").status_code == 404
        assert client.put("/api/app-engine/apps/nope/configuration", json={"values": []}).status_code == 404
