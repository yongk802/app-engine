"""The server chat: one channel, cookie or bearer access, bounded and metered."""

import json

import pytest
from fastapi.testclient import TestClient

from app_engine.chat_room import ChatRoom
from tests.test_public_origin import LAUNCHER, PUBLIC, load_engine, make_app, owner_client, public_client


def test_room_bounds_text_meters_senders_and_persists(tmp_path):
    room = ChatRoom(tmp_path, per_minute=3)
    first = room.post("p1", "Rook", "player", "  hello \x07world ")
    assert first["text"] == "hello world" and first["id"] == 1
    with pytest.raises(ValueError):
        room.post("p1", "Rook", "player", "   ")
    with pytest.raises(ValueError):
        room.post("p1", "Rook", "player", "x" * 501)
    with pytest.raises(PermissionError):
        room.post("p1", "Rook", "player", "too fast")      # one per second
    import time
    now = time.time()
    room._sent["p1"] = [now - 30, now - 20, now - 10]      # three already in the last minute
    with pytest.raises(PermissionError):
        room.post("p1", "Rook", "player", "over the minute cap")
    room.touch("p2", "Volt", "player")
    assert [p["name"] for p in room.online()] == ["Rook", "Volt"]
    assert ChatRoom(tmp_path).since(0)[0]["text"] == "hello world", "messages survive a restart"
    assert ChatRoom(tmp_path).since(1) == []


def test_owner_and_players_share_the_channel_here_and_from_afar(tmp_path, monkeypatch):
    make_app(tmp_path / "apps", "cards")
    module = load_engine(tmp_path, monkeypatch, APP_ENGINE_PUBLIC_ORIGIN=PUBLIC, APP_ENGINE_ADMIN_SECRET="hunter2-but-longer", APP_ENGINE_SERVER_NAME="Yong's table")
    owner, admin = owner_client(module)
    try:
        assert owner.get(f"{LAUNCHER}/api/apps").json()[0]["servers"] == [], "the launcher learns each app's server list"
        posted = owner.post(f"{LAUNCHER}/api/chat", json={"text": "welcome, everyone"})
        assert posted.status_code == 200 and posted.json()["message"]["from"] == {"id": "admin", "name": "owner", "role": "admin"}
        assert owner.post(f"{LAUNCHER}/api/chat", json={"text": ""}).status_code == 400
        assert owner.post(f"{LAUNCHER}/api/chat", json={"text": "again at once"}).status_code == 429
        invite = owner.post(f"{LAUNCHER}/admin/players", json={"username": "rook", "apps": ["cards"]}, headers=admin).json()["invite"]
        with public_client(module) as guest:
            guest.post(invite, data={"password": "correct horse battery", "confirm": "correct horse battery"}, follow_redirects=False)
            feed = guest.get(f"{LAUNCHER}/api/chat?after=0").json()
            assert feed["server"] == "Yong's table" and feed["you"]["name"] == "rook"
            assert [m["text"] for m in feed["messages"]] == ["welcome, everyone"]
            assert sorted(p["name"] for p in feed["online"]) == ["owner", "rook"]
        with TestClient(module.app, base_url="https://someones-laptop.example") as remote:
            token = remote.post(f"{LAUNCHER}/api/players/sign-in", json={"username": "rook", "password": "correct horse battery"}).json()["token"]
            bearer = {"authorization": f"Bearer {token}"}
            assert remote.get(f"{LAUNCHER}/api/players/chat").status_code == 401
            sent = remote.post(f"{LAUNCHER}/api/players/chat", json={"text": "hi from my own app-engine"}, headers=bearer)
            assert sent.status_code == 200 and sent.headers["access-control-allow-origin"] == "*"
            later = remote.get(f"{LAUNCHER}/api/players/chat?after=1", headers=bearer).json()
            assert [m["text"] for m in later["messages"]] == ["hi from my own app-engine"] and later["messages"][0]["from"]["role"] == "player"
        assert [m["text"] for m in owner.get(f"{LAUNCHER}/api/chat?after=0").json()["messages"]] == ["welcome, everyone", "hi from my own app-engine"]
        assert (tmp_path / "state" / "chat.json").is_file()
    finally:
        owner.close()


def test_chat_is_absent_on_a_loopback_engine(tmp_path, monkeypatch):
    module = load_engine(tmp_path, monkeypatch)
    with TestClient(module.app) as client:
        assert client.get("/api/chat").status_code == 404
