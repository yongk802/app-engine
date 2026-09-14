"""The host's multiplayer route: a room service per app, reached only from the app's origin."""

import importlib
import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app_engine.multiplayer_host import MultiplayerHost

FAKE_RULES = Path(__file__).resolve().parents[1] / "app_engine" / "multiplayer" / "test" / "fake-rules.mjs"
COMPAT = {"protocol": 1, "rulesVersion": "fake-rules-1", "catalogDigest": "fakecafe"}
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="multiplayer needs Node.js")


def make_game(apps: Path, app_id: str, *, multiplayer=True, rules_name="rules.mjs") -> Path:
    root = apps / app_id
    root.mkdir(parents=True)
    (root / "index.html").write_text("<!doctype html><title>game</title>")
    if multiplayer:
        # The fake rules module is app-engine's own; a real game ships its engine here.
        (root / rules_name).write_text(f"export {{default}} from '{FAKE_RULES.as_posix()}';\n")
    manifest = {"label": app_id, "icon": "g"}
    if multiplayer:
        manifest["multiplayer"] = {"rules": rules_name}
    (root / "app.json").write_text(json.dumps(manifest))
    return root


def load_engine(tmp_path, monkeypatch, **env):
    monkeypatch.setenv("APP_ENGINE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("APP_ENGINE_APPS_DIR", str(tmp_path / "apps"))
    monkeypatch.delenv("APP_ENGINE_MULTIPLAYER_SERVER", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    import engine
    return importlib.reload(engine)


def headers(module, app_id, credential=None):
    out = {"host": f"{app_id}.localhost", "x-app-state-token": module._app_capability(app_id), "content-type": "application/json"}
    if credential:
        out["x-app-multiplayer-credential"] = credential
    return out


def command(client, module, app_id, body, credential=None, **extra):
    return client.post(f"/api/app-multiplayer/{app_id}/command", content=json.dumps(body), headers={**headers(module, app_id, credential), **extra})


@needs_node
def test_a_game_plays_through_the_host_and_storage_is_per_app(tmp_path, monkeypatch):
    make_game(tmp_path / "apps", "cards")
    make_game(tmp_path / "apps", "dice")
    module = load_engine(tmp_path, monkeypatch)
    with TestClient(module.app) as client:
        health = client.get("/api/app-multiplayer/cards/health", headers=headers(module, "cards"))
        assert health.status_code == 200 and health.json()["rulesVersion"] == "fake-rules-1"

        created = command(client, module, "cards", {"op": "create", "name": "Alex", **COMPAT})
        assert created.status_code == 200, created.text
        owner = created.json()["result"]
        assert owner["seat"] == 0 and "token" in owner

        joined = command(client, module, "cards", {"op": "join", "roomId": owner["roomId"], "invite": owner["invite"], "name": "Blair", "requestId": "j1", **COMPAT})
        guest = joined.json()["result"]
        pending = command(client, module, "cards", {"op": "snapshot", "roomId": owner["roomId"]}, owner["token"]).json()["result"]
        assert pending["pendingJoin"]["name"] == "Blair"
        command(client, module, "cards", {"op": "accept", "roomId": owner["roomId"], "joinId": pending["pendingJoin"]["id"], "requestId": "a1"}, owner["token"])
        deck = {"name": "d", "legends": ["l"], "cards": ["a", "b", "c"]}
        for i, token in enumerate([owner["token"], guest["token"]]):
            command(client, module, "cards", {"op": "deck", "roomId": owner["roomId"], "requestId": f"d{i}", "deck": deck}, token)
            command(client, module, "cards", {"op": "ready", "roomId": owner["roomId"], "requestId": f"r{i}", "ready": True}, token)
        snap = command(client, module, "cards", {"op": "snapshot", "roomId": owner["roomId"]}, owner["token"]).json()["result"]
        assert snap["status"] == "playing"
        assert snap["view"]["players"][1]["hand"] == [], "the other seat's hand stays private through the relay"

        unauthenticated = command(client, module, "cards", {"op": "snapshot", "roomId": owner["roomId"]})
        assert unauthenticated.status_code == 401 and unauthenticated.json()["error"]["code"] == "AUTH_REQUIRED"
        bad_credential = command(client, module, "cards", {"op": "snapshot", "roomId": owner["roomId"]}, "not a token!")
        assert bad_credential.status_code == 401

        # The dice game has its own service and its own storage; the cards room is not there.
        elsewhere = command(client, module, "dice", {"op": "snapshot", "roomId": owner["roomId"]}, owner["token"])
        assert elsewhere.json()["error"]["code"] == "ROOM_NOT_FOUND"
        state = tmp_path / "state" / "multiplayer"
        assert (state / "cards" / "rooms.json").is_file()
        assert not (state / "dice" / "rooms.json").exists()
        assert module._multiplayer.data_dir("cards") != module._multiplayer.data_dir("dice")
        assert module._multiplayer.logs("cards") and "listening" in " ".join(module._multiplayer.logs("cards"))

        # The friend roster is per app: an identity registered for cards does not exist for dice.
        friend = command(client, module, "cards", {"op": "friendRegister", "name": "Alex", **COMPAT}).json()["result"]
        assert command(client, module, "cards", {"op": "friendSnapshot"}, friend["token"]).status_code == 200
        assert command(client, module, "dice", {"op": "friendSnapshot"}, friend["token"]).json()["error"]["code"] == "AUTH_REQUIRED"


@needs_node
def test_the_route_is_gated_like_app_state_and_bounds_its_input(tmp_path, monkeypatch):
    make_game(tmp_path / "apps", "cards")
    module = load_engine(tmp_path, monkeypatch)
    with TestClient(module.app) as client:
        good = headers(module, "cards")
        body = json.dumps({"op": "create", "name": "Alex", **COMPAT})
        assert client.post("/api/app-multiplayer/cards/command", content=body, headers={**good, "x-app-state-token": "wrong"}).status_code == 403
        assert client.post("/api/app-multiplayer/cards/command", content=body, headers={**good, "host": "dice.localhost"}).status_code == 403
        assert client.post("/api/app-multiplayer/cards/command", content=body, headers={**good, "host": "127.0.0.1"}).status_code == 403
        assert client.post("/api/app-multiplayer/cards/command", content=body, headers={**good, "sec-fetch-site": "cross-site"}).status_code == 403
        assert client.post("/api/app-multiplayer/cards/command", content=body, headers={**good, "content-type": "text/plain"}).status_code == 415
        assert client.post("/api/app-multiplayer/cards/command", content="[]", headers=good).status_code == 400
        assert client.post("/api/app-multiplayer/cards/command", content="{not json", headers=good).status_code == 400
        oversized = json.dumps({"op": "message", "text": "x" * 70000})
        assert client.post("/api/app-multiplayer/cards/command", content=oversized, headers=good).status_code == 413
        unknown = client.post("/api/app-multiplayer/cards/command", content=json.dumps({"op": "friendDeleteEverything"}), headers=good)
        assert unknown.status_code == 400 and unknown.json()["error"]["code"] == "INVALID_REQUEST"
        assert client.get("/api/app-multiplayer/cards/health", headers={**good, "x-app-state-token": ""}).status_code == 403


def test_apps_without_the_field_or_module_stay_solo(tmp_path, monkeypatch):
    make_game(tmp_path / "apps", "solo", multiplayer=False)
    make_game(tmp_path / "apps", "broken")
    (tmp_path / "apps" / "broken" / "rules.mjs").unlink()
    module = load_engine(tmp_path, monkeypatch)
    with TestClient(module.app) as client:
        for app_id in ("solo", "broken"):
            reply = command(client, module, app_id, {"op": "create", "name": "Alex", **COMPAT})
            assert reply.status_code == 503, reply.text
            assert reply.json()["error"]["code"] == "MULTIPLAYER_UNAVAILABLE"
        assert client.get("/api/app-state/solo", headers=headers(module, "solo")).status_code == 200


def test_missing_node_is_reported_not_crashed(tmp_path, monkeypatch):
    make_game(tmp_path / "apps", "cards")
    module = load_engine(tmp_path, monkeypatch)
    module._multiplayer.node = str(tmp_path / "no-such-node")
    with TestClient(module.app) as client:
        reply = command(client, module, "cards", {"op": "create", "name": "Alex", **COMPAT})
        assert reply.status_code == 503 and reply.json()["error"]["code"] == "MULTIPLAYER_UNAVAILABLE"


def test_external_server_override_relays_instead_of_spawning(tmp_path, monkeypatch):
    make_game(tmp_path / "apps", "cards")
    module = load_engine(tmp_path, monkeypatch, APP_ENGINE_MULTIPLAYER_SERVER="http://127.0.0.1:1/")
    assert module._multiplayer.external_url("cards") == "http://127.0.0.1:1"
    with TestClient(module.app) as client:
        reply = command(client, module, "cards", {"op": "create", "name": "Alex", **COMPAT})
        assert reply.status_code == 503 and reply.json()["error"]["code"] == "ROOM_SERVER_UNAVAILABLE"
        assert module._multiplayer.logs("cards") == [], "nothing was spawned"
    mapped = MultiplayerHost(tmp_path, external='{"cards": "http://10.0.0.5:18791/", "other": "http://10.0.0.6:18791"}')
    assert mapped.external_url("cards") == "http://10.0.0.5:18791"
    assert mapped.external_url("dice") is None
    assert MultiplayerHost(tmp_path, external="{bad json").external is None
