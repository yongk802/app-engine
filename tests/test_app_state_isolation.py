import importlib

from fastapi.testclient import TestClient


def load_engine(tmp_path, monkeypatch, isolation=None):
    monkeypatch.setenv("APP_ENGINE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("APP_ENGINE_APPS_DIR", str(tmp_path / "apps"))
    if isolation is not None:
        monkeypatch.setenv("APP_ENGINE_APP_STATE_ISOLATION", isolation)
    else:
        monkeypatch.delenv("APP_ENGINE_APP_STATE_ISOLATION", raising=False)
    import engine
    return importlib.reload(engine)


def _ref(app_id):
    # Emulates a browser fetch from inside an app iframe; host matches TestClient's.
    return {"referer": f"http://testserver/apps/{app_id}/index.html"}


# ── default mode: "referer" ───────────────────────────────────────────────────

def test_same_app_can_read_and_write_its_own_state(tmp_path, monkeypatch):
    module = load_engine(tmp_path, monkeypatch)
    with TestClient(module.app) as client:
        assert client.put("/api/app-state/notes", json={"n": 1}, headers=_ref("notes")).status_code == 200
        got = client.get("/api/app-state/notes", headers=_ref("notes"))
        assert got.status_code == 200 and got.json() == {"n": 1}


def test_cross_app_read_and_write_are_blocked(tmp_path, monkeypatch):
    module = load_engine(tmp_path, monkeypatch)
    with TestClient(module.app) as client:
        # notes writes its own state
        client.put("/api/app-state/notes", json={"secret": 1}, headers=_ref("notes"))
        # blocks (a different app) may not read or clobber it
        assert client.get("/api/app-state/notes", headers=_ref("blocks")).status_code == 403
        assert client.put("/api/app-state/notes", json={"evil": 1}, headers=_ref("blocks")).status_code == 403
        # and notes' data is intact
        assert client.get("/api/app-state/notes", headers=_ref("notes")).json() == {"secret": 1}


def test_non_app_caller_is_allowed_in_referer_mode(tmp_path, monkeypatch):
    """curl / tests / the top-level launcher (no app Referer) keep working."""
    module = load_engine(tmp_path, monkeypatch)
    with TestClient(module.app) as client:
        assert client.put("/api/app-state/notes", json={"n": 2}).status_code == 200   # no referer
        assert client.get("/api/app-state/notes").status_code == 200
        # a foreign-origin referer is ignored, not trusted
        foreign = {"referer": "http://evil.example.com/apps/blocks/"}
        assert client.get("/api/app-state/notes", headers=foreign).status_code == 200


# ── strict mode ───────────────────────────────────────────────────────────────

def test_strict_requires_matching_app_referer(tmp_path, monkeypatch):
    module = load_engine(tmp_path, monkeypatch, isolation="strict")
    with TestClient(module.app) as client:
        assert client.put("/api/app-state/notes", json={"n": 1}, headers=_ref("notes")).status_code == 200
        assert client.get("/api/app-state/notes", headers=_ref("notes")).status_code == 200
        # no referer is denied under strict
        assert client.get("/api/app-state/notes").status_code == 403
        # cross-app still denied
        assert client.get("/api/app-state/notes", headers=_ref("blocks")).status_code == 403


# ── off mode (legacy) ─────────────────────────────────────────────────────────

def test_off_mode_allows_cross_app(tmp_path, monkeypatch):
    module = load_engine(tmp_path, monkeypatch, isolation="off")
    with TestClient(module.app) as client:
        client.put("/api/app-state/notes", json={"n": 1}, headers=_ref("notes"))
        assert client.get("/api/app-state/notes", headers=_ref("blocks")).status_code == 200
