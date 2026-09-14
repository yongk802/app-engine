"""Public-origin mode: apps at <id>.<public host>, owner sign-in on everything, loopback unchanged."""

import importlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app_engine.public_origin import Accounts, PublicOrigin, SignInLimiter, bind_is_allowed

PUBLIC = "https://play.example.com"


def make_app(apps: Path, app_id: str = "notes") -> None:
    root = apps / app_id
    root.mkdir(parents=True, exist_ok=True)
    (root / "index.html").write_text("<!doctype html><title>notes</title>")
    (root / "app.json").write_text(json.dumps({"label": app_id, "icon": "n"}))


def load_engine(tmp_path, monkeypatch, **env):
    monkeypatch.setenv("APP_ENGINE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("APP_ENGINE_APPS_DIR", str(tmp_path / "apps"))
    for name in ("APP_ENGINE_PUBLIC_ORIGIN", "APP_ENGINE_ADMIN_SECRET", "APP_ENGINE_TRUST_PROXY"):
        monkeypatch.delenv(name, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    make_app(tmp_path / "apps")
    import engine
    return importlib.reload(engine)


LAUNCHER = "https://play.example.com"
APP = "https://notes.play.example.com"


def public_client(module):
    return TestClient(module.app, base_url=LAUNCHER)


def sign_in(client, secret="hunter2-but-longer"):
    return client.post(f"{LAUNCHER}/admin/sign-in", data={"secret": secret}, follow_redirects=False)


def test_public_origin_parsing_and_app_hosts():
    origin = PublicOrigin.parse("https://play.example.com/")
    assert origin.origin == "https://play.example.com" and origin.secure
    assert origin.app_authority("cyberpunk-tcg") == "cyberpunk-tcg.play.example.com"
    assert origin.app_id_for("cyberpunk-tcg.play.example.com") == "cyberpunk-tcg"
    assert origin.app_id_for("play.example.com") is None
    assert origin.app_id_for("a.b.play.example.com") is None, "one label only: no nested hosts"
    assert origin.app_id_for("evil-play.example.com") is None
    assert PublicOrigin.parse("http://games.lan:8080").host == "games.lan:8080"
    assert PublicOrigin.parse("play.example.com").origin == "https://play.example.com", "https is the default scheme"
    assert PublicOrigin.parse("") is None and PublicOrigin.parse(None) is None
    assert PublicOrigin.parse("http://play.localhost:8770").app_authority("notes") == "notes.play.localhost:8770", "rehearsal on one machine"
    for bad in ("https://127.0.0.1", "https://localhost", "https://play.example.com/path", "ftp://x.y", "https://user@play.example.com"):
        with pytest.raises(ValueError):
            PublicOrigin.parse(bad)


def test_loopback_mode_is_unchanged(tmp_path, monkeypatch):
    module = load_engine(tmp_path, monkeypatch)
    assert module._PUBLIC is None and module._accounts is None
    with TestClient(module.app) as client:
        assert client.get("/").status_code == 200, "no sign-in in loopback mode"
        assert client.get("/admin").status_code == 404
        listing = client.get("/api/apps").json()
        assert listing[0]["url"].startswith("http://notes.localhost/apps/notes/#atrium_state_token=")
        assert "frame-ancestors *" in client.get("/apps/notes/", headers={"host": "notes.localhost"}).headers["content-security-policy"]


def test_everything_needs_the_owner_session_in_public_mode(tmp_path, monkeypatch):
    module = load_engine(tmp_path, monkeypatch, APP_ENGINE_PUBLIC_ORIGIN=PUBLIC, APP_ENGINE_ADMIN_SECRET="hunter2-but-longer")
    assert module._minted_admin_secret is None, "a configured secret is adopted, not replaced"
    with public_client(module) as client:
        landing = client.get(f"{LAUNCHER}/", headers={"accept": "text/html"}, follow_redirects=False)
        assert landing.status_code == 303 and landing.headers["location"] == "/admin"
        assert client.get(f"{LAUNCHER}/api/apps").status_code == 401
        assert client.get(f"{APP}/apps/notes/").status_code == 401
        assert client.get(f"{APP}/api/app-state/notes").status_code == 401
        assert client.post(f"{APP}/api/app-multiplayer/notes/command").status_code == 401
        assert client.get(f"{LAUNCHER}/api/app-engine/catalog").status_code == 401
        assert client.get(f"{LAUNCHER}/api/engine").status_code == 200, "identity stays public"
        page = client.get(f"{LAUNCHER}/admin")
        assert page.status_code == 200 and "Sign in" in page.text and "frame-ancestors 'none'" in page.headers["content-security-policy"]

        wrong = sign_in(client, "nope")
        assert wrong.status_code == 403 and "not right" in wrong.text and "set-cookie" not in wrong.headers
        ok = sign_in(client)
        assert ok.status_code == 303 and ok.headers["location"] == "/"
        cookie = ok.headers["set-cookie"].lower()
        assert "httponly" in cookie and "secure" in cookie and "samesite=lax" in cookie and "domain=play.example.com" in cookie
        players = (tmp_path / "state" / "players.json").read_text()
        assert json.loads(players)["admin"]["sessions"] and "hunter2" not in players, "only hashes at rest"

        assert client.get(f"{LAUNCHER}/").status_code == 200
        listing = client.get(f"{LAUNCHER}/api/apps").json()
        assert listing[0]["url"].startswith(f"{APP}/apps/notes/#atrium_state_token="), "app URLs come from the configured origin"
        token = listing[0]["url"].split("atrium_state_token=")[1]
        served = client.get(f"{APP}/apps/notes/")
        assert served.status_code == 200, "the session cookie covers the app subdomain"
        assert served.headers["content-security-policy"] == "frame-ancestors https://play.example.com"
        state_headers = {"x-app-state-token": token}
        assert client.put(f"{APP}/api/app-state/notes", json={"n": 1}, headers=state_headers).status_code == 200
        assert client.get(f"{APP}/api/app-state/notes", headers=state_headers).json() == {"n": 1}
        assert client.get("https://notes.evil.example.com/apps/notes/", headers={"cookie": ok.headers["set-cookie"].split(";")[0]}).status_code == 403
        assert client.get(f"{LAUNCHER}/api/local-ai/status").status_code == 403, "Local AI never leaves the owner's computer"
        assert client.post(f"{LAUNCHER}/api/app-chat", json={"app_id": "notes", "messages": []}).status_code == 403
        assert client.get(f"{LAUNCHER}/admin", follow_redirects=False).headers["location"] == "/", "signed in owners skip the form"

        out = client.post(f"{LAUNCHER}/admin/sign-out", follow_redirects=False)
        assert out.status_code == 303 and out.headers["location"] == "/admin"
        client.cookies.clear()
        assert client.get(f"{LAUNCHER}/api/apps").status_code == 401, "sign-out revoked the session on the server"


def test_first_start_mints_a_secret_once_and_keeps_only_its_hash(tmp_path, monkeypatch):
    module = load_engine(tmp_path, monkeypatch, APP_ENGINE_PUBLIC_ORIGIN=PUBLIC)
    minted = module._minted_admin_secret
    assert minted and len(minted) >= 24
    with public_client(module) as client:
        assert sign_in(client, minted).status_code == 303
    again = importlib.reload(module)
    assert again._minted_admin_secret is None, "a restart reuses the stored hash"
    with public_client(again) as client:
        assert sign_in(client, minted).status_code == 303
    rotated = load_engine(tmp_path, monkeypatch, APP_ENGINE_PUBLIC_ORIGIN=PUBLIC, APP_ENGINE_ADMIN_SECRET="a-new-configured-secret")
    with public_client(rotated) as client:
        assert sign_in(client, minted).status_code == 403, "a configured secret replaces the minted one and revokes sessions"
        assert sign_in(client, "a-new-configured-secret").status_code == 303


def test_rehearsal_origin_under_localhost_keeps_app_hosts_apart(tmp_path, monkeypatch):
    module = load_engine(tmp_path, monkeypatch, APP_ENGINE_PUBLIC_ORIGIN="http://play.localhost:8770", APP_ENGINE_ADMIN_SECRET="hunter2-but-longer")
    with TestClient(module.app, base_url="http://play.localhost:8770") as client:
        assert client.post("http://play.localhost:8770/admin/sign-in", data={"secret": "hunter2-but-longer"}, follow_redirects=False).status_code == 303
        token = client.get("http://play.localhost:8770/api/apps").json()[0]["url"].split("atrium_state_token=")[1]
        headers = {"x-app-state-token": token}
        assert client.get("http://notes.play.localhost:8770/api/app-state/notes", headers=headers).status_code == 200, "public app host wins over the .localhost suffix"
        assert client.get("http://notes.localhost:8770/api/app-state/notes", headers=headers).status_code == 401, "the session cookie belongs to the public domain; loopback app hosts are not signed in"
        assert client.get("http://other.play.localhost:8770/api/app-state/notes", headers=headers).status_code == 403


def test_sign_in_attempts_are_rate_limited(tmp_path, monkeypatch):
    module = load_engine(tmp_path, monkeypatch, APP_ENGINE_PUBLIC_ORIGIN=PUBLIC, APP_ENGINE_ADMIN_SECRET="hunter2-but-longer")
    with public_client(module) as client:
        codes = [sign_in(client, "wrong").status_code for _ in range(6)]
        assert codes[:5] == [403] * 5 and codes[5] == 429
    limiter = SignInLimiter(attempts=2, window=1000)
    assert limiter.allow("a") and limiter.allow("a") and not limiter.allow("a") and limiter.allow("b")


def test_bind_guard_and_bad_origin(tmp_path, monkeypatch):
    accounts = Accounts.load(tmp_path)
    assert bind_is_allowed("127.0.0.1", None, None) is None
    assert "administrator" in bind_is_allowed("0.0.0.0", None, None)
    public = PublicOrigin.parse(PUBLIC)
    assert "admin secret" in bind_is_allowed("0.0.0.0", public, accounts)
    accounts.set_admin_secret("s3cret-long-enough")
    assert bind_is_allowed("0.0.0.0", public, accounts) is None
    monkeypatch.setenv("APP_ENGINE_PUBLIC_ORIGIN", "https://127.0.0.1")
    monkeypatch.setenv("APP_ENGINE_STATE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("APP_ENGINE_APPS_DIR", str(tmp_path / "a"))
    import engine
    with pytest.raises(SystemExit, match="public host name"):
        importlib.reload(engine)
    monkeypatch.delenv("APP_ENGINE_PUBLIC_ORIGIN")
    importlib.reload(engine)
