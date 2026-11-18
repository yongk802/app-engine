import importlib
import json

import pytest
from fastapi.testclient import TestClient


def load_engine(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENGINE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("APP_ENGINE_APPS_DIR", str(tmp_path / "apps"))
    import engine
    return importlib.reload(engine)


def _cap(module, app_id):
    return {"host": f"{app_id}.localhost", "x-app-state-token": module._app_capability(app_id)}


def test_same_app_can_read_and_write_its_own_state(tmp_path, monkeypatch):
    module = load_engine(tmp_path, monkeypatch)
    with TestClient(module.app) as client:
        headers = _cap(module, "notes")
        assert client.put("/api/app-state/notes", json={"n": 1}, headers=headers).status_code == 200
        got = client.get("/api/app-state/notes", headers=headers)
        assert got.status_code == 200 and got.json() == {"n": 1}


def test_cross_app_token_cannot_read_or_write_state(tmp_path, monkeypatch):
    module = load_engine(tmp_path, monkeypatch)
    with TestClient(module.app) as client:
        notes = _cap(module, "notes")
        blocks = _cap(module, "blocks")
        client.put("/api/app-state/notes", json={"secret": 1}, headers=notes)
        assert client.get("/api/app-state/notes", headers=blocks).status_code == 403
        assert client.put("/api/app-state/notes", json={"evil": 1}, headers=blocks).status_code == 403
        assert client.get("/api/app-state/notes", headers=notes).json() == {"secret": 1}


def test_missing_or_forged_capability_is_denied_even_without_referer(tmp_path, monkeypatch):
    module = load_engine(tmp_path, monkeypatch)
    with TestClient(module.app) as client:
        assert client.get("/api/app-state/notes", headers={"host": "notes.localhost"}).status_code == 403
        assert client.get("/api/app-state/notes", headers={
            "host": "notes.localhost", "x-app-state-token": "forged", "referrer-policy": "no-referrer"
        }).status_code == 403
        assert client.get("/api/app-state/notes", headers={
            "host": "evil.example", "x-app-state-token": module._app_capability("notes")
        }).status_code == 421


def test_app_listing_uses_isolated_origin_and_fragment_capability(tmp_path, monkeypatch):
    apps = tmp_path / "apps" / "notes"
    apps.mkdir(parents=True)
    (apps / "index.html").write_text("ok")
    (apps / "app.json").write_text('{"id":"notes","label":"Notes","icon":"N"}')
    module = load_engine(tmp_path, monkeypatch)
    with TestClient(module.app) as client:
        listed = client.get("/api/apps").json()[0]
        assert listed["url"].startswith("http://notes.localhost/apps/notes/")
        assert "#atrium_state_token=" in listed["url"]
        assert client.get("/api/apps", headers={"host": "notes.localhost"}).status_code == 403


@pytest.mark.parametrize("app_id", ["players", "stores", "local-ai", "chat"])
def test_app_save_cannot_read_or_replace_engine_control_file(tmp_path, monkeypatch, app_id):
    module = load_engine(tmp_path, monkeypatch)
    root = module.APPS_DIR / app_id
    root.mkdir(parents=True)
    (root / "app.json").write_text(json.dumps({"id": app_id, "label": app_id, "icon": "A"}))
    (root / "index.html").write_text("test app")
    control = module.STATE_DIR / f"{app_id}.json"
    original = b'{"engine_canary":"must stay private"}'
    control.write_bytes(original)
    with TestClient(module.app) as client:
        headers = _cap(module, app_id)
        response = client.get(f"/api/app-state/{app_id}", headers=headers)
        assert response.status_code == 200
        assert response.json() == {}, "engine control data must never become an app save"
        response = client.put(f"/api/app-state/{app_id}", json={"draft": "app data"}, headers=headers)
        assert response.status_code == 200
        assert client.get(f"/api/app-state/{app_id}", headers=headers).json() == {"draft": "app data"}
        assert control.read_bytes() == original


def test_existing_owner_save_migrates_without_reusing_legacy_path(tmp_path, monkeypatch):
    module = load_engine(tmp_path, monkeypatch)
    root = module.APPS_DIR / "notes"
    root.mkdir(parents=True)
    (root / "app.json").write_text('{"id":"notes","label":"Notes","icon":"N"}')
    (root / "index.html").write_text("notes")
    legacy = module.STATE_DIR / "notes.json"
    legacy.write_text('{"draft":"before upgrade"}')
    with TestClient(module.app) as client:
        headers = _cap(module, "notes")
        assert client.get("/api/app-state/notes", headers=headers).json() == {"draft": "before upgrade"}
        assert client.put("/api/app-state/notes", json={"draft": "after upgrade"}, headers=headers).status_code == 200
        assert json.loads(legacy.read_text()) == {"draft": "before upgrade"}, "leave a rollback copy"
        assert json.loads((module.STATE_DIR / "saves" / "owner" / "notes.json").read_text()) == {"draft": "after upgrade"}


def test_linked_save_is_refused_without_exposing_or_modifying_target(tmp_path, monkeypatch):
    module = load_engine(tmp_path, monkeypatch)
    private = module.STATE_DIR / 'players.json'
    original = b'{"private":"account canary"}'
    private.write_bytes(original)
    saves = module.STATE_DIR / 'saves' / 'owner'
    saves.mkdir(parents=True)
    (saves / 'notes.json').symlink_to(private)
    with TestClient(module.app) as client:
        headers = _cap(module, 'notes')
        for response in (client.get('/api/app-state/notes', headers=headers),
                         client.put('/api/app-state/notes', headers=headers, json={})):
            assert response.status_code == 409
            assert 'canary' not in response.text
    assert private.read_bytes() == original
