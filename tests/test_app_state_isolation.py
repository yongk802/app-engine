import importlib

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
        }).status_code == 403


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
