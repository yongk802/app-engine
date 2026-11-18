"""Launcher routes answer on the launcher's host name only; an app's origin reaches what an app needs."""

import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from app_engine import host_gate
from app_engine.origin_gate import app_origin_may_call
from app_engine.public_origin import Accounts, startup_warnings, PublicOrigin
from test_public_origin import APP, LAUNCHER, PUBLIC, load_engine, public_client, sign_in

SECRET = "hunter2-but-longer"


@pytest.mark.parametrize("path", [
    "/apps/notes/", "/apps/notes/app.js", "/apps/app-state-bridge.js", "/api/app-state/notes",
    "/api/app-multiplayer/notes/command", "/api/app-engine/sessions/abc/assets/index.html",
    "/api/app-engine/sessions/abc/proxy/api/x", "/api/players/sign-in", "/api/engine", "/state",
    "/api/app-chat", "/api/local-ai/status", "/join/code", "/admin", "/admin/sign-in", "/sign-in",
])
def test_an_app_origin_reaches_what_an_app_needs(path):
    assert app_origin_may_call(path)


@pytest.mark.parametrize("path", [
    "/", "/admin/players", "/admin/players/p_1", "/admin/sign-out", "/sign-out", "/api/chat", "/api/apps",
    "/api/apps/rejected", "/api/local-ai/diagnostics", "/api/local-ai/install-plan", "/api/local-ai/start",
    "/api/speech", "/api/assistant/settings", "/api/mcp", "/api/app-engine/catalog",
    "/api/app-engine/sessions/abc", "/api/app-engine/stores", "/api/app-engine/studio/create",
    "/api/app-engine/plans/f/approve", "/state/", "/api/enginex", "/assistant-assets/panel.js",
])
def test_an_app_origin_reaches_no_launcher_route(path):
    assert not app_origin_may_call(path)


def test_a_hosted_app_cannot_use_the_owners_session_on_its_own_origin(tmp_path, monkeypatch):
    """The session cookie covers every <id>.<host>, so the app's script could call these same-origin."""
    module = load_engine(tmp_path, monkeypatch, APP_ENGINE_PUBLIC_ORIGIN=PUBLIC, APP_ENGINE_ADMIN_SECRET=SECRET)
    with public_client(module) as client:
        assert sign_in(client).status_code == 303
        for path in ("/admin/players", "/api/apps/rejected", "/api/chat"):
            assert client.get(f"{LAUNCHER}{path}").status_code == 200, path
            assert client.get(f"{APP}{path}").status_code == 403, path
        posted = client.post(f"{APP}/api/chat", json={"text": "from the app"}, headers={"sec-fetch-site": "same-origin"})
        assert posted.status_code == 403
        assert client.post(f"{APP}/sign-out", follow_redirects=False).status_code == 403
        assert client.get(f"{LAUNCHER}/api/chat").json()["messages"] == []
        # What the app does need still answers there.
        assert client.get(f"{APP}/state").status_code == 200
        assert client.get(f"{APP}/apps/notes/").status_code == 200
        assert client.get(f"{APP}/api/engine").status_code == 200


def test_sign_in_is_still_asked_for_first_on_an_app_origin(tmp_path, monkeypatch):
    module = load_engine(tmp_path, monkeypatch, APP_ENGINE_PUBLIC_ORIGIN=PUBLIC, APP_ENGINE_ADMIN_SECRET=SECRET)
    with public_client(module) as client:
        assert client.get(f"{APP}/admin/players").status_code == 401


def test_a_local_app_origin_reads_no_launcher_report(tmp_path, monkeypatch):
    module = load_engine(tmp_path, monkeypatch)
    with TestClient(module.app) as client:
        app_host = {"host": "notes.localhost:8770"}
        for path in ("/api/local-ai/diagnostics", "/api/local-ai/install-plan", "/api/speech/status"):
            assert client.get(path, headers=app_host).status_code == 403, path
            assert client.get(path, headers={"host": "127.0.0.1:8770"}).status_code != 403, path
        assert client.get("/state", headers=app_host).status_code == 200
        assert client.get("/api/local-ai/status", headers=app_host).status_code == 200


def test_testserver_is_a_local_name_only_under_test(tmp_path, monkeypatch):
    """A bare name can be made to resolve to an attacker and then rebound to 127.0.0.1."""
    assert host_gate.local_host_allowed("testserver:8770")
    module = load_engine(tmp_path, monkeypatch)
    monkeypatch.delenv("APP_ENGINE_TESTING")
    assert not host_gate.local_host_allowed("testserver:8770")
    assert not host_gate.local_host_allowed("testserver")
    with TestClient(module.app) as client:
        assert client.get("/").status_code == 421
        page = client.get("/", headers={"host": "127.0.0.1:8770"})
        assert page.status_code == 200 and module._admin_capability in page.text


def test_the_admin_secret_is_stored_salted_and_stretched(tmp_path):
    accounts = Accounts.load(tmp_path)
    accounts.set_admin_secret(SECRET)
    stored = json.loads((tmp_path / "players.json").read_text())["admin"]["secret_hash"]
    assert stored.startswith("pbkdf2$") and hashlib.sha256(SECRET.encode()).hexdigest() not in stored
    assert accounts.verify_admin_secret(SECRET)
    assert not accounts.verify_admin_secret(SECRET + "x") and not accounts.verify_admin_secret("")
    other = Accounts.load(tmp_path / "other")
    other.set_admin_secret(SECRET)
    assert other.data["admin"]["secret_hash"] != stored   # a salt per record


def _legacy(tmp_path) -> Accounts:
    """A state file written before the change: a bare SHA-256 of the secret, and a live session."""
    accounts = Accounts.load(tmp_path)
    accounts.data["admin"]["secret_hash"] = hashlib.sha256(SECRET.encode()).hexdigest()
    accounts.save()
    accounts.create_admin_session()
    return Accounts.load(tmp_path)


def test_an_old_secret_record_still_signs_in_and_is_upgraded(tmp_path):
    accounts = _legacy(tmp_path)
    assert not accounts.verify_admin_secret("wrong-secret-entirely")
    assert not accounts.data["admin"]["secret_hash"].startswith("pbkdf2$")
    assert accounts.verify_admin_secret(SECRET)
    reread = Accounts.load(tmp_path)
    assert reread.data["admin"]["secret_hash"].startswith("pbkdf2$")
    assert len(reread.data["admin"]["sessions"]) == 1   # same secret: nobody is signed out
    assert reread.verify_admin_secret(SECRET)


def test_a_configured_secret_upgrades_its_old_record_at_startup(tmp_path):
    accounts = _legacy(tmp_path)
    assert accounts.ensure_admin_secret(SECRET) is None
    assert accounts.data["admin"]["secret_hash"].startswith("pbkdf2$")
    assert len(accounts.data["admin"]["sessions"]) == 1
    first = accounts.data["admin"]["secret_hash"]
    assert accounts.ensure_admin_secret(SECRET) is None and accounts.data["admin"]["secret_hash"] == first
    assert accounts.ensure_admin_secret("a-different-long-secret") is None
    assert accounts.data["admin"]["sessions"] == [] and accounts.verify_admin_secret("a-different-long-secret")


def test_a_short_configured_secret_is_warned_about():
    public = PublicOrigin.parse(PUBLIC)
    assert any("APP_ENGINE_ADMIN_SECRET" in note for note in startup_warnings("127.0.0.1", public, True, admin_secret="short"))
    assert not any("APP_ENGINE_ADMIN_SECRET" in note for note in startup_warnings("127.0.0.1", public, True, admin_secret=SECRET))
    assert not any("APP_ENGINE_ADMIN_SECRET" in note for note in startup_warnings("127.0.0.1", public, True))
