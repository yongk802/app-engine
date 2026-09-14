import importlib
import json

from fastapi.testclient import TestClient

from app_engine import manifest


def load_engine(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENGINE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("APP_ENGINE_APPS_DIR", str(tmp_path / "apps"))
    import engine
    return importlib.reload(engine)


def make_app(apps_dir, name, manifest_dict, *, index=True):
    d = apps_dir / name
    d.mkdir(parents=True)
    (d / "app.json").write_text(json.dumps(manifest_dict))
    if index:
        (d / "index.html").write_text("<h1>ok</h1>")
    return d


# ── manifest validation unit tests ────────────────────────────────────────────

def test_minimal_manifest_is_valid():
    errors, warnings = manifest.validate_manifest(
        {"label": "X", "icon": "x"}, has_index=True, dir_name="x")
    assert errors == []
    # no version → a soft warning, never an error
    assert any("version" in w for w in warnings)


def test_missing_label_and_icon_are_errors():
    errors, _ = manifest.validate_manifest({}, has_index=True, dir_name="x")
    assert any("label" in e for e in errors)
    assert any("icon" in e for e in errors)


def test_no_index_and_no_entry_point_is_error():
    errors, _ = manifest.validate_manifest(
        {"label": "X", "icon": "x"}, has_index=False, dir_name="x")
    assert any("index.html" in e or "entry_point" in e for e in errors)


def test_loopback_entry_point_is_valid():
    errors, _ = manifest.validate_manifest(
        {"label": "X", "icon": "x", "entry_point": "http://127.0.0.1:8550"},
        has_index=False, dir_name="x")
    assert errors == []


def test_bad_id_and_version_and_categories_and_screenshots():
    errors, _ = manifest.validate_manifest({
        "id": "Bad_ID", "label": "X", "icon": "x",
        "version": "not-a-version", "categories": "games",
        "screenshots": ["../secret.png"],
    }, has_index=True, dir_name="x")
    assert any("id" in e for e in errors)
    assert any("version" in e for e in errors)
    assert any("categories" in e for e in errors)
    assert any("screenshots" in e for e in errors)


def test_unknown_field_warns_but_does_not_error():
    errors, warnings = manifest.validate_manifest(
        {"label": "X", "icon": "x", "version": "1.0.0", "wat": 1},
        has_index=True, dir_name="x")
    assert errors == []
    assert any("wat" in w for w in warnings)


def test_atrium_only_fields_do_not_warn():
    _, warnings = manifest.validate_manifest(
        {"label": "X", "icon": "x", "version": "1.0.0",
         "secret": True, "allow": "microphone", "proxy_read_timeout": 30},
        has_index=True, dir_name="x")
    assert not any("secret" in w or "allow" in w or "proxy_read_timeout" in w for w in warnings)


def test_version_ordering_and_compatibility():
    assert manifest.version_ge("1.2.0", "1.1.9")
    assert manifest.version_ge("1.0.0", "1.0.0")
    assert not manifest.version_ge("1.0.0", "1.0.1")
    assert manifest.is_compatible("", "1.0.0")           # unset = always ok
    assert manifest.is_compatible("1.0.0", "1.0.0")
    assert not manifest.is_compatible("2.0.0", "1.0.0")  # app needs newer engine


def test_normalize_defaults():
    n = manifest.normalize({"label": "X", "icon": "x"}, "mydir")
    assert n["id"] == "mydir"
    assert n["version"] == "0.0.0"
    assert n["categories"] == [] and n["screenshots"] == []


# ── engine integration ────────────────────────────────────────────────────────

def test_api_apps_exposes_v2_metadata(tmp_path, monkeypatch):
    apps = tmp_path / "apps"
    make_app(apps, "good", {
        "id": "good", "label": "Good", "icon": "✅", "version": "1.2.0",
        "description": "A test app", "categories": ["tools"], "author": "Me",
    })
    module = load_engine(tmp_path, monkeypatch)
    with TestClient(module.app) as client:
        app = client.get("/api/apps").json()[0]
    assert app["version"] == "1.2.0"
    assert app["description"] == "A test app"
    assert app["categories"] == ["tools"]
    assert app["author"] == "Me"
    assert app["compatible"] is True
    assert "root" not in app  # filesystem path never leaks


def test_engine_version_endpoint(tmp_path, monkeypatch):
    module = load_engine(tmp_path, monkeypatch)
    with TestClient(module.app) as client:
        data = client.get("/api/engine").json()
    assert data["name"] == "app-engine"
    assert data["version"] == manifest.ENGINE_VERSION


def test_malformed_app_is_rejected_not_silent(tmp_path, monkeypatch):
    apps = tmp_path / "apps"
    make_app(apps, "broken", {"icon": "x"})           # missing label → rejected
    make_app(apps, "fine", {"label": "Fine", "icon": "✅", "version": "1.0.0"})
    module = load_engine(tmp_path, monkeypatch)
    with TestClient(module.app) as client:
        listed = client.get("/api/apps").json()
        rejected = client.get("/api/apps/rejected").json()
    assert [a["id"] for a in listed] == ["fine"]
    assert len(rejected) == 1
    assert rejected[0]["dir"] == "broken"
    assert "label" in rejected[0]["reason"]


def test_incompatible_app_is_listed_but_flagged(tmp_path, monkeypatch):
    apps = tmp_path / "apps"
    make_app(apps, "future", {
        "label": "Future", "icon": "🔮", "version": "1.0.0",
        "min_engine_version": "999.0.0",
    })
    module = load_engine(tmp_path, monkeypatch)
    with TestClient(module.app) as client:
        app = client.get("/api/apps").json()[0]
    assert app["compatible"] is False
    assert app["min_engine_version"] == "999.0.0"


def test_multiplayer_field_normalizes_and_rejects_unsafe_modules(tmp_path):
    from app_engine import manifest as mf

    good = {"label": "T", "icon": "x", "multiplayer": {"rules": "multiplayer/rules.mjs", "ai": "ai.mjs"}}
    errors, warnings = mf.validate_manifest(good, has_index=True, dir_name="t")
    assert errors == []
    assert not any("multiplayer" in w for w in warnings)
    assert mf.normalize(good, "t")["multiplayer"] == {"rules": "multiplayer/rules.mjs", "ai": "ai.mjs", "protocol": 1, "servers": []}
    assert mf.normalize({"label": "T", "icon": "x"}, "t")["multiplayer"] is None

    for bad in ({"rules": "../escape.mjs"}, {"rules": "/abs.mjs"}, {"rules": ""}, {"rules": "r.mjs", "ai": "../ai.mjs"},
                {"rules": "r.mjs", "protocol": 2}, {"rules": "r.mjs", "protocol": True}, "rules.mjs"):
        errors, _ = mf.validate_manifest({"label": "T", "icon": "x", "multiplayer": bad}, has_index=True, dir_name="t")
        assert any("multiplayer" in e for e in errors), bad

    _, warnings = mf.validate_manifest({"label": "T", "icon": "x", "multiplayer": {"rules": "r.mjs", "bogus": 1}}, has_index=True, dir_name="t")
    assert any("unknown multiplayer field 'bogus'" in w for w in warnings)

    app = tmp_path / "game"
    app.mkdir()
    (app / "index.html").write_text("<!doctype html>")
    (app / "app.json").write_text('{"manifest_version": 2, "label": "G", "icon": "g", "multiplayer": {"rules": "rules.mjs"}}')
    inspection = mf.parse_manifest(app)
    assert inspection.manifest is not None, inspection.errors
    assert inspection.manifest.multiplayer.rules == "rules.mjs"
    assert inspection.manifest.multiplayer.ai is None
    assert inspection.manifest.multiplayer.protocol == 1
    (app / "app.json").write_text('{"label": "G", "icon": "g", "multiplayer": {"rules": "rules.mjs", "ai": "ai.mjs"}}')
    assert mf.parse_manifest(app).manifest.multiplayer.ai == "ai.mjs"
    (app / "app.json").write_text('{"label": "G", "icon": "g"}')
    assert mf.parse_manifest(app).manifest.multiplayer is None


def test_multiplayer_servers_are_validated_and_normalized():
    from app_engine import manifest as mf

    good = {"label": "T", "icon": "x", "multiplayer": {"rules": "r.mjs", "servers": [{"name": " Yong's table ", "url": "https://play.example.com/"}, {"name": "LAN", "url": "http://games.lan:8770"}]}}
    errors, _ = mf.validate_manifest(good, has_index=True, dir_name="t")
    assert errors == []
    assert mf.normalize(good, "t")["multiplayer"]["servers"] == [{"name": "Yong's table", "url": "https://play.example.com"}, {"name": "LAN", "url": "http://games.lan:8770"}]
    assert mf.normalize({"label": "T", "icon": "x", "multiplayer": {"rules": "r.mjs"}}, "t")["multiplayer"]["servers"] == []
    for bad in ([{"name": "x"}], [{"name": "x", "url": "http://play.example.com"}], [{"name": "", "url": "https://a.b"}], [{"name": "x", "url": "https://a.b/path"}], "https://a.b", [{}] * 21):
        errors, _ = mf.validate_manifest({"label": "T", "icon": "x", "multiplayer": {"rules": "r.mjs", "servers": bad}}, has_index=True, dir_name="t")
        assert any("servers" in e for e in errors), bad
