"""Public-origin mode: apps at <id>.<public host>, owner sign-in on everything, loopback unchanged."""

import importlib
import json
import shutil
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app_engine.public_origin import SESSION_COOKIE, Accounts, PublicOrigin, SignInLimiter, bind_is_allowed

FAKE_RULES = Path(__file__).resolve().parents[1] / "app_engine" / "multiplayer" / "test" / "fake-rules.mjs"

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


def owner_client(module):
    client = public_client(module)
    client.__enter__()          # run the lifespan: the runtime and its catalog start
    client.close = lambda: TestClient.__exit__(client, None, None, None)
    assert sign_in(client).status_code == 303
    html = client.get(f"{LAUNCHER}/").text
    capability = html.split("'X-App-Engine-Admin':'")[1].split("'")[0]
    return client, {"X-App-Engine-Admin": capability}


def test_players_are_invited_choose_a_password_and_see_only_their_apps(tmp_path, monkeypatch):
    make_app(tmp_path / "apps", "cards")
    module = load_engine(tmp_path, monkeypatch, APP_ENGINE_PUBLIC_ORIGIN=PUBLIC, APP_ENGINE_ADMIN_SECRET="hunter2-but-longer")
    owner, admin = owner_client(module)
    assert "__APP_ENGINE_ROLE__" not in owner.get(f"{LAUNCHER}/").text and "'owner'" in owner.get(f"{LAUNCHER}/").text
    assert owner.post(f"{LAUNCHER}/admin/players", json={"username": "rook", "apps": ["cards"]}).status_code == 403, "mutations need the launcher capability too"
    assert owner.post(f"{LAUNCHER}/admin/players", json={"username": "Rook!", "apps": ["cards"]}, headers=admin).status_code == 400
    assert owner.post(f"{LAUNCHER}/admin/players", json={"username": "rook", "apps": ["nope"]}, headers=admin).status_code == 400
    created = owner.post(f"{LAUNCHER}/admin/players", json={"username": "Rook", "name": "Rook the Bot", "apps": ["cards"]}, headers=admin)
    assert created.status_code == 201, created.text
    player, invite = created.json()["player"], created.json()["invite"]
    assert player["username"] == "rook" and player["invite_pending"] and not player["has_password"]
    assert invite.startswith(f"{LAUNCHER}/join/")
    assert owner.post(f"{LAUNCHER}/admin/players", json={"username": "rook", "apps": []}, headers=admin).status_code == 400, "usernames are unique"
    listed = owner.get(f"{LAUNCHER}/admin/players").json()
    assert [p["username"] for p in listed["players"]] == ["rook"] and listed["apps"] == ["cards", "notes"]

    with public_client(module) as guest:
        form = guest.get(invite)
        assert form.status_code == 200 and 'value="rook"' in form.text
        assert guest.post(invite, data={"password": "short", "confirm": "short"}).status_code == 400
        assert guest.post(invite, data={"password": "long enough", "confirm": "different"}).status_code == 400
        joined = guest.post(invite, data={"password": "correct horse battery", "confirm": "correct horse battery"}, follow_redirects=False)
        assert joined.status_code == 303 and "set-cookie" in joined.headers
        assert guest.get(invite).status_code == 404, "an invitation is one-time"
        players_file = (tmp_path / "state" / "players.json").read_text()
        assert "correct horse" not in players_file and "pbkdf2$" in players_file

        home = guest.get(f"{LAUNCHER}/")
        assert home.status_code == 200 and "'player'" in home.text and "'X-App-Engine-Admin':''" in home.text, "a player's launcher carries no owner capability"
        assert guest.get(f"{LAUNCHER}/state").json()["session"] == {"username": "Rook the Bot", "role": "player"}
        listing = guest.get(f"{LAUNCHER}/api/apps").json()
        assert [a["id"] for a in listing] == ["cards"], "only the allowed app is listed"
        assert guest.get(f"{LAUNCHER}/api/apps/categories").status_code == 200
        token = listing[0]["url"].split("atrium_state_token=")[1]
        assert guest.get("https://cards.play.example.com/apps/cards/").status_code == 200
        assert guest.get(f"{APP}/apps/notes/").status_code == 404, "an app not granted does not exist for the player"
        assert guest.get(f"{LAUNCHER}/api/apps/rejected").status_code == 404
        assert guest.get(f"{LAUNCHER}/admin/players").status_code == 404
        assert guest.get(f"{LAUNCHER}/api/app-engine/catalog").status_code == 404
        assert guest.get(f"{LAUNCHER}/api/app-engine/studio/").status_code == 404
        opened = guest.post(f"{LAUNCHER}/api/app-engine/apps/cards/open", json={})
        assert opened.status_code == 200, opened.text
        assert guest.post(f"{LAUNCHER}/api/app-engine/apps/notes/open", json={}).status_code == 404
        state_headers = {"x-app-state-token": token}
        assert guest.put("https://cards.play.example.com/api/app-state/cards", json={"deck": "mine"}, headers=state_headers).status_code == 200
        assert (tmp_path / "state" / "players" / player["id"] / "cards.json").is_file(), "saves are per player"
        assert not (tmp_path / "state" / "cards.json").exists()
        assert guest.get("https://cards.play.example.com/api/app-state/cards", headers={"x-app-state-token": "wrong"}).status_code == 403

    # The owner's capability for the same app is a different one, and their save is separate.
    owner_token = owner.get(f"{LAUNCHER}/api/apps").json()[0]["url"].split("atrium_state_token=")[1]
    assert owner_token != token
    assert owner.get("https://cards.play.example.com/api/app-state/cards", headers={"x-app-state-token": owner_token}).json() == {}

    # Password sign-in for a browser at the server; disabling ends every session.
    with public_client(module) as again:
        assert again.post(f"{LAUNCHER}/sign-in", data={"username": "rook", "password": "wrong password"}, follow_redirects=False).status_code == 403
        assert again.post(f"{LAUNCHER}/sign-in", data={"username": "ROOK", "password": "correct horse battery"}, follow_redirects=False).status_code == 303
        assert again.get(f"{LAUNCHER}/api/apps").status_code == 200
        assert owner.post(f"{LAUNCHER}/admin/players/{player['id']}", json={"disabled": True}, headers=admin).json()["player"]["disabled"] is True
        assert again.get(f"{LAUNCHER}/api/apps").status_code == 401
        assert again.post(f"{LAUNCHER}/sign-in", data={"username": "rook", "password": "correct horse battery"}, follow_redirects=False).status_code == 403
        assert owner.post(f"{LAUNCHER}/admin/players/{player['id']}", json={"disabled": False, "apps": ["cards", "notes"]}, headers=admin).json()["player"]["apps"] == ["cards", "notes"]
        relink = owner.post(f"{LAUNCHER}/admin/players/{player['id']}/invite", headers=admin).json()["invite"]
        assert again.get(relink).status_code == 200, "a fresh invitation lets the player set a new password"
        assert owner.delete(f"{LAUNCHER}/admin/players/{player['id']}", headers=admin).status_code == 204
        assert again.get(relink).status_code == 404
    owner.close()


def test_remote_game_clients_sign_in_with_a_password_and_use_bearer_tokens(tmp_path, monkeypatch):
    make_app(tmp_path / "apps", "cards")
    module = load_engine(tmp_path, monkeypatch, APP_ENGINE_PUBLIC_ORIGIN=PUBLIC, APP_ENGINE_ADMIN_SECRET="hunter2-but-longer")
    owner, admin = owner_client(module)
    invite = owner.post(f"{LAUNCHER}/admin/players", json={"username": "rook", "apps": ["cards"]}, headers=admin).json()["invite"]
    with public_client(module) as guest:
        guest.post(invite, data={"password": "correct horse battery", "confirm": "correct horse battery"}, follow_redirects=False)
    owner.close()

    with TestClient(module.app, base_url="https://someones-laptop.example") as remote:
        # A game on another origin: no cookies, preflight first, then a bearer token.
        preflight = remote.options(f"{LAUNCHER}/api/players/sign-in", headers={"origin": "http://cyberpunk-tcg.localhost:8770", "access-control-request-method": "POST"})
        assert preflight.status_code == 204 and preflight.headers["access-control-allow-origin"] == "*"
        assert "authorization" in preflight.headers["access-control-allow-headers"].lower()
        assert "PUT" in preflight.headers["access-control-allow-methods"], "a player's game can write their save"
        assert remote.post(f"{LAUNCHER}/api/players/sign-in", json={"username": "rook", "password": "nope"}).status_code == 403
        assert remote.post(f"{LAUNCHER}/api/players/sign-in", content="not json", headers={"content-type": "application/json"}).status_code == 400
        signed = remote.post(f"{LAUNCHER}/api/players/sign-in", json={"username": "rook", "password": "correct horse battery"}, headers={"origin": "http://cyberpunk-tcg.localhost:8770"})
        assert signed.status_code == 200 and signed.headers["access-control-allow-origin"] == "*"
        body = signed.json()
        assert body["ok"] and body["player"]["username"] == "rook" and body["player"]["apps"] == ["cards"] and body["server"]["origin"] == PUBLIC
        bearer = {"authorization": f"Bearer {body['token']}"}
        assert remote.get(f"{LAUNCHER}/api/players/me").status_code == 401
        assert remote.get(f"{LAUNCHER}/api/players/me", headers=bearer).json()["player"]["name"] == "rook"
        denied = remote.post(f"{LAUNCHER}/api/players/notes/command", json={"op": "create"}, headers=bearer)
        assert denied.status_code == 404 and denied.json()["error"]["code"] == "MULTIPLAYER_UNAVAILABLE"
        unhosted = remote.post(f"{LAUNCHER}/api/players/cards/command", json={"op": "create"}, headers=bearer)
        assert unhosted.status_code == 503 and unhosted.json()["error"]["code"] == "MULTIPLAYER_UNAVAILABLE", "cards declares no rules module"
        assert remote.post(f"{LAUNCHER}/api/players/cards/command", content="[]", headers={**bearer, "content-type": "application/json"}).status_code == 400
        assert remote.post(f"{LAUNCHER}/api/players/cards/command", json={"op": "create"}).status_code == 401
        assert remote.post(f"{LAUNCHER}/api/players/sign-out", headers=bearer).json()["ok"] is True
        assert remote.get(f"{LAUNCHER}/api/players/me", headers=bearer).status_code == 401, "signed out tokens die"
        assert remote.get(f"{LAUNCHER}/api/apps").status_code == 401, "the cookie surface is untouched by the remote API"


def test_players_cli_changes_are_seen_by_a_running_engine(tmp_path, monkeypatch, capsys):
    from app_engine.__main__ import players_main

    make_app(tmp_path / "apps", "cards")
    module = load_engine(tmp_path, monkeypatch, APP_ENGINE_PUBLIC_ORIGIN=PUBLIC, APP_ENGINE_ADMIN_SECRET="hunter2-but-longer")
    state = str(tmp_path / "state")
    assert players_main(["--state-dir", state, "--origin", PUBLIC, "invite", "--username", "Rook", "--apps", "cards"]) == 0
    out = capsys.readouterr().out
    assert "invited rook for cards" in out
    link = out.split("send this link once (valid 7 days): ")[1].strip()
    assert link.startswith(f"{LAUNCHER}/join/")
    with public_client(module) as guest:
        assert guest.get(link).status_code == 200, "the engine re-reads players.json when the CLI changed it"
        assert guest.post(link, data={"password": "correct horse battery", "confirm": "correct horse battery"}, follow_redirects=False).status_code == 303
        assert [a["id"] for a in guest.get(f"{LAUNCHER}/api/apps").json()] == ["cards"]
        assert players_main(["--state-dir", state, "list"]) == 0
        listing = capsys.readouterr().out
        assert "rook" in listing and "active" in listing and "cards" in listing
        assert players_main(["--state-dir", state, "disable", "rook"]) == 0
        assert guest.get(f"{LAUNCHER}/api/apps").status_code == 401
        assert players_main(["--state-dir", state, "enable", "rook"]) == 0
        assert players_main(["--state-dir", state, "--origin", PUBLIC, "reinvite", "rook"]) == 0
        assert "/join/" in capsys.readouterr().out
        assert players_main(["--state-dir", state, "remove", "rook"]) == 0
    with pytest.raises(SystemExit):
        players_main(["--state-dir", state, "remove", "nobody"])
    with pytest.raises(SystemExit):
        players_main(["--state-dir", state, "invite", "--username", "x"])


def test_phase_four_limits_audit_and_same_origin_layout(tmp_path, monkeypatch):
    from app_engine.public_origin import RateLimiter, startup_warnings

    limiter = RateLimiter(per_second=1000, burst=3)
    assert [limiter.allow("k") for _ in range(4)][:3] == [0.0, 0.0, 0.0] and limiter.allow("k") > 0
    assert limiter.allow("other") == 0.0, "buckets are per key"

    make_app(tmp_path / "apps", "cards")
    monkeypatch.setenv("APP_ENGINE_RATE_LIMIT", "0.001,5")
    import app_engine.public_origin as public_origin
    importlib.reload(public_origin)
    module = load_engine(tmp_path, monkeypatch, APP_ENGINE_PUBLIC_ORIGIN="https://srv1242099.hstgr.cloud", APP_ENGINE_APP_ORIGINS="same", APP_ENGINE_ADMIN_SECRET="hunter2-but-longer")
    assert module._PUBLIC.layout == "same"
    assert any("share one browser origin" in w for w in startup_warnings("127.0.0.1", module._PUBLIC, True))
    assert any("TRUST_PROXY" in w for w in startup_warnings("127.0.0.1", module._PUBLIC, False))
    assert any("public interface" in w for w in startup_warnings("0.0.0.0", module._PUBLIC, True))
    base = "https://srv1242099.hstgr.cloud"
    with TestClient(module.app, base_url=base) as client:
        assert client.post(f"{base}/admin/sign-in", data={"secret": "hunter2-but-longer"}, follow_redirects=False).status_code == 303
        listing = client.get(f"{base}/api/apps").json()
        assert listing[0]["url"].startswith(f"{base}/apps/cards/#atrium_state_token="), "apps live under the one host name"
        token = listing[0]["url"].split("atrium_state_token=")[1]
        assert client.get(f"{base}/apps/cards/").status_code == 200
        assert client.put(f"{base}/api/app-state/cards", json={"n": 1}, headers={"x-app-state-token": token}).status_code == 200
        assert client.get("https://cards.srv1242099.hstgr.cloud/apps/cards/", headers={"cookie": ""}).status_code in (401, 403), "subdomains are not the layout here"
        # Rate limiting: the busy surfaces answer 429 with Retry-After once the burst is spent.
        codes = [client.get(f"{base}/api/app-state/cards", headers={"x-app-state-token": token}).status_code for _ in range(7)]
        assert codes == [200] * 4 + [429] * 3, "the PUT above spent one of the five burst tokens; refill is negligible here"
        limited = client.get(f"{base}/api/app-state/cards", headers={"x-app-state-token": token})
        assert limited.status_code == 429 and limited.headers["retry-after"] and limited.json()["error"]["code"] == "RATE_LIMITED"
        assert client.get(f"{base}/").status_code == 200, "the launcher is not metered"

    monkeypatch.delenv("APP_ENGINE_RATE_LIMIT")
    importlib.reload(public_origin)
    audit = (tmp_path / "state" / "audit.log").read_text().splitlines()
    events = [json.loads(line)["event"] for line in audit]
    assert "engine.start" in events and "owner.sign_in" in events
    assert "hunter2" not in "".join(audit), "the audit log never carries secrets"


def test_player_cap_is_configurable(tmp_path, monkeypatch):
    from app_engine import public_origin
    monkeypatch.setattr(public_origin, "MAX_PLAYERS", 1)
    accounts = public_origin.Accounts.load(tmp_path)
    accounts.create_player("one", [])
    with pytest.raises(ValueError, match="full"):
        accounts.create_player("two", [])


def make_managed_game(apps: Path, app_id: str = "game") -> Path:
    """An app with a managed backend: a tiny HTTP server the runtime starts and health-checks."""
    root = apps / app_id
    root.mkdir(parents=True)
    (root / "index.html").write_text("<!doctype html><title>game</title>")
    (root / "server.py").write_text(
        "import os\nfrom http.server import BaseHTTPRequestHandler, HTTPServer\n"
        "class H(BaseHTTPRequestHandler):\n"
        "    def do_GET(self):\n        self.send_response(200); self.end_headers(); self.wfile.write(b'ok')\n"
        "    def log_message(self, *a): pass\n"
        "HTTPServer(('127.0.0.1', int(os.environ['PORT'])), H).serve_forever()\n")
    (root / "app.json").write_text(json.dumps({
        "manifest_version": 2, "label": "Game", "icon": "g", "default_target": "web",
        "targets": {"web": {"kind": "web", "runtime": {"driver": "process", "scope": "shared",
                    "start": {"argv": [sys.executable, "server.py"]}, "health": {"kind": "http", "path": "/"}}}},
    }))
    return root


def test_owner_and_player_open_a_managed_app_in_public_mode(tmp_path, monkeypatch):
    """Night City runs a managed backend: opening it must work for the signed-in owner and,
    once the owner approved its plan, for a player. (The runtime is single-subject.)"""
    make_managed_game(tmp_path / "apps")
    module = load_engine(tmp_path, monkeypatch, APP_ENGINE_PUBLIC_ORIGIN=PUBLIC, APP_ENGINE_ADMIN_SECRET="hunter2-but-longer")
    owner, admin = owner_client(module)
    try:
        first = owner.post(f"{LAUNCHER}/api/app-engine/apps/game/open", json={})
        assert first.status_code == 409 and first.json()["detail"]["code"] == "approval_required", first.text
        plan = owner.post(f"{LAUNCHER}/api/app-engine/apps/game/preview", json={}, headers=admin).json()
        assert owner.post(f"{LAUNCHER}/api/app-engine/plans/{plan['fingerprint']}/approve", headers=admin).status_code == 204
        opened = owner.post(f"{LAUNCHER}/api/app-engine/apps/game/open", json={})
        assert opened.status_code == 200, opened.text
        assert owner.post(f"{LAUNCHER}/api/app-engine/apps/game/open", json={}).status_code == 200, "the approval stands for the next open"
        invite = owner.post(f"{LAUNCHER}/admin/players", json={"username": "rook", "apps": ["game"]}, headers=admin).json()["invite"]
        with public_client(module) as guest:
            guest.post(invite, data={"password": "correct horse battery", "confirm": "correct horse battery"}, follow_redirects=False)
            player_open = guest.post(f"{LAUNCHER}/api/app-engine/apps/game/open", json={})
            assert player_open.status_code == 200, player_open.text
            body = player_open.json()
            assert body["session_id"] and body["launch_id"], "the player got a session on the shared managed runtime"
            # (Streaming the proxied backend through the sync TestClient is not supported; the
            # session proxy itself is covered by tests/test_runtime_routes.py.)
            assert guest.delete(f"{LAUNCHER}/api/app-engine/sessions/{body['session_id']}").status_code == 204
            assert guest.post(f"{LAUNCHER}/api/app-engine/apps/notes/open", json={}).status_code == 404
    finally:
        owner.close()


def test_a_public_server_lists_and_serves_only_the_apps_it_hosts(tmp_path, monkeypatch):
    make_app(tmp_path / "apps", "cards")
    make_app(tmp_path / "apps", "dice")
    module = load_engine(tmp_path, monkeypatch, APP_ENGINE_PUBLIC_ORIGIN=PUBLIC, APP_ENGINE_ADMIN_SECRET="hunter2-but-longer", APP_ENGINE_PUBLIC_APPS="cards, notes")
    owner, admin = owner_client(module)
    try:
        assert sorted(a["id"] for a in owner.get(f"{LAUNCHER}/api/apps").json()) == ["cards", "notes"]
        assert owner.get("https://dice.play.example.com/apps/dice/").status_code == 404, "an unhosted app does not exist on this server"
        assert owner.post(f"{LAUNCHER}/api/app-engine/apps/dice/open", json={}).status_code == 404
        assert owner.post(f"{LAUNCHER}/api/app-engine/apps/cards/open", json={}).status_code == 200
        assert owner.get(f"{LAUNCHER}/admin/players").json()["apps"] == ["cards", "notes"]
        assert owner.post(f"{LAUNCHER}/admin/players", json={"username": "rook", "apps": ["dice"]}, headers=admin).status_code == 400
    finally:
        owner.close()


def test_a_player_reads_and_writes_their_own_save_from_their_own_game(tmp_path, monkeypatch):
    make_app(tmp_path / "apps", "cards")
    module = load_engine(tmp_path, monkeypatch, APP_ENGINE_PUBLIC_ORIGIN=PUBLIC, APP_ENGINE_ADMIN_SECRET="hunter2-but-longer")
    owner, admin = owner_client(module)
    invite = owner.post(f"{LAUNCHER}/admin/players", json={"username": "rook", "apps": ["cards"]}, headers=admin).json()["invite"]
    with public_client(module) as guest:
        guest.post(invite, data={"password": "correct horse battery", "confirm": "correct horse battery"}, follow_redirects=False)
        token = guest.get(f"{LAUNCHER}/api/apps").json()[0]["url"].split("atrium_state_token=")[1]
        assert guest.put("https://cards.play.example.com/api/app-state/cards", json={"version": 1, "decks": [{"name": "browser crew"}]}, headers={"x-app-state-token": token}).status_code == 200
    owner.close()
    with TestClient(module.app, base_url="https://someones-laptop.example") as remote:
        bearer = {"authorization": "Bearer " + remote.post(f"{LAUNCHER}/api/players/sign-in", json={"username": "rook", "password": "correct horse battery"}).json()["token"]}
        assert remote.get(f"{LAUNCHER}/api/players/state/cards").status_code == 401
        assert remote.get(f"{LAUNCHER}/api/players/state/cards", headers=bearer).json() == {"version": 1, "decks": [{"name": "browser crew"}]}, "the same save the browser game uses"
        assert remote.get(f"{LAUNCHER}/api/players/state/notes", headers=bearer).status_code == 404, "only allowed apps"
        assert remote.put(f"{LAUNCHER}/api/players/state/cards", json={"version": 1, "decks": [{"name": "browser crew"}, {"name": "offline crew"}]}, headers=bearer).status_code == 200
        assert remote.put(f"{LAUNCHER}/api/players/state/cards", content="x" * 200_000, headers={**bearer, "content-type": "application/json"}).status_code == 413
    assert len(json.loads((tmp_path / "state" / "players").glob("*/cards.json").__next__().read_text())["decks"]) == 2


def invite_and_join(module, username="rook", apps=("cards",), password="correct horse battery"):
    """The owner invites a player who then sets a password; returns the owner client and headers."""
    owner, admin = owner_client(module)
    invite = owner.post(f"{LAUNCHER}/admin/players", json={"username": username, "apps": list(apps)}, headers=admin).json()["invite"]
    with public_client(module) as guest:
        assert guest.post(invite, data={"password": password, "confirm": password}, follow_redirects=False).status_code == 303
    return owner, admin


def test_owner_sign_in_keeps_what_the_cli_changed_meanwhile(tmp_path, monkeypatch, capsys):
    """A cookie-less sign-in used to save the engine's stale copy of players.json over a
    change the CLI made since, quietly re-enabling a player the operator had disabled."""
    from app_engine.__main__ import players_main

    make_app(tmp_path / "apps", "cards")
    module = load_engine(tmp_path, monkeypatch, APP_ENGINE_PUBLIC_ORIGIN=PUBLIC, APP_ENGINE_ADMIN_SECRET="hunter2-but-longer")
    state = str(tmp_path / "state")
    assert players_main(["--state-dir", state, "--origin", PUBLIC, "invite", "--username", "rook", "--apps", "cards"]) == 0
    link = capsys.readouterr().out.split("send this link once (valid 7 days): ")[1].strip()
    with public_client(module) as player, public_client(module) as owner:
        assert player.post(link, data={"password": "correct horse battery", "confirm": "correct horse battery"}, follow_redirects=False).status_code == 303
        assert player.get(f"{LAUNCHER}/api/apps").status_code == 200
        assert players_main(["--state-dir", state, "disable", "rook"]) == 0
        # No request carrying a token reaches the engine between the CLI write and this sign-in.
        assert sign_in(owner).status_code == 303
        saved = json.loads((tmp_path / "state" / "players.json").read_text())
        assert saved["players"][0]["disabled"] is True, "the sign-in saved a stale copy over the CLI's change"
        assert player.get(f"{LAUNCHER}/api/apps").status_code == 401, "the disabled player's session stays dead"
        assert owner.get(f"{LAUNCHER}/api/apps").status_code == 200


@pytest.mark.skipif(shutil.which("node") is None, reason="multiplayer needs Node.js")
def test_a_remote_game_client_reads_health_with_its_bearer_token(tmp_path, monkeypatch):
    """The documented remote path is bearer-only; the health route used to demand the cookie
    session after the bearer had already been accepted, so it answered 401 to every client."""
    monkeypatch.delenv("APP_ENGINE_MULTIPLAYER_SERVER", raising=False)
    root = tmp_path / "apps" / "cards"
    root.mkdir(parents=True)
    (root / "index.html").write_text("<!doctype html><title>cards</title>")
    (root / "rules.mjs").write_text(f"export {{default}} from '{FAKE_RULES.as_posix()}';\n")
    (root / "app.json").write_text(json.dumps({"label": "cards", "icon": "c", "multiplayer": {"rules": "rules.mjs"}}))
    module = load_engine(tmp_path, monkeypatch, APP_ENGINE_PUBLIC_ORIGIN=PUBLIC, APP_ENGINE_ADMIN_SECRET="hunter2-but-longer")
    invite_and_join(module)[0].close()
    with TestClient(module.app, base_url="https://someones-laptop.example") as remote:
        token = remote.post(f"{LAUNCHER}/api/players/sign-in", json={"username": "rook", "password": "correct horse battery"}).json()["token"]
        assert remote.get(f"{LAUNCHER}/api/players/cards/health").status_code == 401, "no bearer, no answer"
        health = remote.get(f"{LAUNCHER}/api/players/cards/health", headers={"authorization": f"Bearer {token}"})
        assert health.status_code == 200, health.text
        assert health.json()["rulesVersion"] == "fake-rules-1"
        assert health.json()["host"] == {"server": "play.example.com", "public": True, "you": {"name": "rook", "role": "player"}}


def test_rate_limits_key_on_the_person_the_request_resolved_to(tmp_path, monkeypatch):
    """A made-up cookie or bearer used to get its own bucket, so a stranger varying the header
    was never limited and a player could escape their own bucket with a junk cookie."""
    make_app(tmp_path / "apps", "cards")
    monkeypatch.setenv("APP_ENGINE_RATE_LIMIT", "0.001,6")
    import app_engine.public_origin as public_origin
    importlib.reload(public_origin)
    try:
        module = load_engine(tmp_path, monkeypatch, APP_ENGINE_PUBLIC_ORIGIN=PUBLIC, APP_ENGINE_ADMIN_SECRET="hunter2-but-longer")
        invite_and_join(module)[0].close()
        with TestClient(module.app, base_url="https://someones-laptop.example") as remote:
            token = remote.post(f"{LAUNCHER}/api/players/sign-in", json={"username": "rook", "password": "correct horse battery"}).json()["token"]
            # A signed-in player is one bucket however many junk cookies ride along.
            codes = [remote.get(f"{LAUNCHER}/api/players/me", headers={"authorization": f"Bearer {token}", "cookie": f"{SESSION_COOKIE}=junk{i}"}).status_code for i in range(7)]
            assert codes == [200] * 6 + [429], codes
            # A stranger with a different made-up bearer each time shares the address bucket (the sign-in above spent one).
            codes = [remote.get(f"{LAUNCHER}/api/players/me", headers={"authorization": f"Bearer nobody-{i}"}).status_code for i in range(6)]
            assert codes == [401] * 5 + [429], codes
    finally:
        monkeypatch.delenv("APP_ENGINE_RATE_LIMIT")
        importlib.reload(public_origin)


def test_only_the_owner_may_force_a_rebuild_of_the_shared_backend(tmp_path, monkeypatch):
    """Players share one managed backend; ``force_rebuild`` restarts it for everyone, so a
    player's request drops the flag while the owner's keeps it."""
    make_managed_game(tmp_path / "apps")
    module = load_engine(tmp_path, monkeypatch, APP_ENGINE_PUBLIC_ORIGIN=PUBLIC, APP_ENGINE_ADMIN_SECRET="hunter2-but-longer")
    seen: list[bool] = []
    lifecycle = module._app_runtime.lifecycle
    original = lifecycle.preview

    async def spy(launch):
        seen.append(launch.force_rebuild)
        return await original(launch)

    monkeypatch.setattr(lifecycle, "preview", spy)
    owner, admin = owner_client(module)
    try:
        plan = owner.post(f"{LAUNCHER}/api/app-engine/apps/game/preview", json={}, headers=admin).json()
        assert owner.post(f"{LAUNCHER}/api/app-engine/plans/{plan['fingerprint']}/approve", headers=admin).status_code == 204
        assert owner.post(f"{LAUNCHER}/api/app-engine/apps/game/open", json={}).status_code == 200
        seen.clear()
        assert owner.post(f"{LAUNCHER}/api/app-engine/apps/game/open", json={"force_rebuild": True}).status_code == 200
        assert seen and all(seen), "the owner may rebuild (the open previews the plan more than once)"
        invite = owner.post(f"{LAUNCHER}/admin/players", json={"username": "rook", "apps": ["game"]}, headers=admin).json()["invite"]
        with public_client(module) as guest:
            guest.post(invite, data={"password": "correct horse battery", "confirm": "correct horse battery"}, follow_redirects=False)
            seen.clear()
            opened = guest.post(f"{LAUNCHER}/api/app-engine/apps/game/open", json={"force_rebuild": True})
            assert opened.status_code == 200, opened.text
            assert seen and not any(seen), "a player's open never restarts the backend everyone shares"
            assert guest.post(f"{LAUNCHER}/api/app-engine/apps/game/open", content="[]", headers={"content-type": "application/json"}).status_code == 400
    finally:
        owner.close()
