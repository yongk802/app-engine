"""Loopback mode answers only to local host names, and its unauthenticated POSTs need a JSON fetch."""

import pytest
from fastapi.testclient import TestClient

from app_engine.host_gate import local_host_allowed
from test_public_origin import PUBLIC, load_engine, public_client


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.1:8770", "localhost:8770", "LOCALHOST",
                                  "notes.localhost:8770", "[::1]:8770", "::1", "localhost."])
def test_local_names_are_allowed(host):
    assert local_host_allowed(host)


@pytest.mark.parametrize("host", ["", "evil.example", "evil.example:8770", "127.0.0.1.evil.example",
                                  "localhost.evil.example", "192.168.1.5:8770", "[2001:db8::1]:8770"])
def test_other_names_are_refused(host):
    assert not local_host_allowed(host)


def test_a_rebound_host_name_reaches_nothing(tmp_path, monkeypatch):
    """DNS rebinding: evil.example re-pointed at 127.0.0.1 must not read the owner's API."""
    module = load_engine(tmp_path, monkeypatch)
    with TestClient(module.app) as client:
        rebound = {"host": "evil.example:8770"}
        for path in ("/", "/api/apps", "/api/local-ai/status", "/api/local-ai/diagnostics", "/state", "/api/engine"):
            assert client.get(path, headers=rebound).status_code == 421, path
        assert client.post("/api/app-chat", headers=rebound, json={"app_id": "notes", "messages": [1]}).status_code == 421
        assert client.get("/api/apps", headers={"host": "127.0.0.1:8770"}).status_code == 200


def test_allowed_hosts_extend_the_local_names(tmp_path, monkeypatch):
    module = load_engine(tmp_path, monkeypatch, APP_ENGINE_ALLOWED_HOSTS="engine.lan, Other.Lan")
    with TestClient(module.app) as client:
        assert client.get("/api/engine", headers={"host": "engine.lan:8770"}).status_code == 200
        assert client.get("/api/engine", headers={"host": "other.lan"}).status_code == 200
        assert client.get("/api/engine", headers={"host": "evil.lan"}).status_code == 421


def test_public_mode_is_gated_by_sign_in_instead(tmp_path, monkeypatch):
    module = load_engine(tmp_path, monkeypatch, APP_ENGINE_PUBLIC_ORIGIN=PUBLIC, APP_ENGINE_ADMIN_SECRET="hunter2-but-longer")
    with public_client(module) as client:
        assert client.get("/api/engine").status_code == 200


def test_app_chat_refuses_a_form_post_from_another_page(tmp_path, monkeypatch):
    """A text/plain POST needs no CORS preflight, so any web page could send one."""
    module = load_engine(tmp_path, monkeypatch)
    with TestClient(module.app) as client:
        body = '{"app_id": "notes", "messages": [{"role": "user", "content": "hi"}]}'
        assert client.post("/api/app-chat", content=body, headers={"content-type": "text/plain"}).status_code == 415
        cross = client.post("/api/app-chat", content=body, headers={"content-type": "application/json", "sec-fetch-site": "cross-site"})
        assert cross.status_code == 403
        assert client.post("/api/app-chat", json=["not", "an", "object"]).status_code == 400


def test_local_ai_install_plan_is_not_served_publicly(tmp_path, monkeypatch):
    module = load_engine(tmp_path, monkeypatch, APP_ENGINE_PUBLIC_ORIGIN=PUBLIC, APP_ENGINE_ADMIN_SECRET="hunter2-but-longer")
    with public_client(module) as client:
        signed = client.post("/admin/sign-in", data={"secret": "hunter2-but-longer"}, follow_redirects=False)
        assert signed.status_code == 303
        assert client.get("/api/local-ai/install-plan").status_code == 403


def test_server_chat_post_requires_a_json_fetch(tmp_path, monkeypatch):
    """Every app subdomain shares the session cookie, so a hosted app could otherwise post as the player."""
    module = load_engine(tmp_path, monkeypatch, APP_ENGINE_PUBLIC_ORIGIN=PUBLIC, APP_ENGINE_ADMIN_SECRET="hunter2-but-longer")
    with public_client(module) as client:
        client.post("/admin/sign-in", data={"secret": "hunter2-but-longer"}, follow_redirects=False)
        assert client.post("/api/chat", content='{"text": "hi"}', headers={"content-type": "text/plain"}).status_code == 415
        assert client.post("/api/chat", json={"text": "hi"}).status_code == 200


@pytest.mark.skipif(__import__("os").name == "nt", reason="POSIX modes")
def test_state_directory_is_closed_to_other_users(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    state.chmod(0o775)
    load_engine(tmp_path, monkeypatch)
    assert state.stat().st_mode & 0o777 == 0o770, "other users lose access, the group keeps it"


def test_launcher_runs_only_its_own_scripts(tmp_path, monkeypatch):
    module = load_engine(tmp_path, monkeypatch)
    with TestClient(module.app) as client:
        first, second = client.get("/"), client.get("/")
        policy = first.headers["content-security-policy"]
        nonce = policy.split("'nonce-", 1)[1].split("'", 1)[0]
        assert "script-src-attr 'none'" in policy and "object-src 'none'" in policy and "frame-ancestors 'none'" in policy
        assert f'<script nonce="{nonce}">' in first.text and "<script>" not in first.text
        assert nonce not in second.headers["content-security-policy"], "a fresh nonce per response"
        assert first.headers["referrer-policy"] == "no-referrer"


def test_api_docs_are_not_published(tmp_path, monkeypatch):
    module = load_engine(tmp_path, monkeypatch)
    with TestClient(module.app) as client:
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert client.get(path).status_code == 404, path
