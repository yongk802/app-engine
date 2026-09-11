import importlib
import json

from fastapi.testclient import TestClient

from app_engine import manifest
from app_engine.search import CATEGORY_IDS, category_counts, parse_query, search_apps


APPS = [
    {"id": "cookbook", "label": "Cookbook", "description": "Vegetable-forward Instant Pot recipes with email export.", "categories": ["home"]},
    {"id": "llm-stack", "label": "LLM Stack", "description": "Control panel for local inference services.", "categories": ["ai", "developer"]},
    {"id": "nano-llm", "label": "Build a Tiny LLM", "description": "Train a tiny GPT language model from scratch.", "categories": ["learning", "ai"]},
    {"id": "pool", "label": "Pool", "description": "8-ball pool with a friend over the sync server.", "categories": ["games"]},
]


def ids(apps):
    return [app["id"] for app in apps]


def make_app(apps_dir, name, manifest_dict):
    d = apps_dir / name
    d.mkdir(parents=True)
    (d / "app.json").write_text(json.dumps(manifest_dict))
    (d / "index.html").write_text("<h1>ok</h1>")
    return d


def test_empty_query_keeps_host_order():
    assert ids(search_apps(APPS)) == ids(APPS)


def test_description_terms_match_and_every_term_is_required():
    assert ids(search_apps(APPS, "instant pot")) == ["cookbook"]
    assert ids(search_apps(APPS, "8-ball")) == ["pool"]
    assert search_apps(APPS, "instant gpt") == []


def test_label_matches_outrank_weaker_matches():
    assert ids(search_apps(APPS, "llm")) == ["llm-stack", "nano-llm"]


def test_short_terms_do_not_match_inside_words():
    # "ai" is a category on two apps; "email" in cookbook's description must not count.
    assert ids(search_apps(APPS, "ai")) == ["llm-stack", "nano-llm"]


def test_category_terms_match_ids_and_labels():
    assert ids(search_apps(APPS, "games")) == ["pool"]
    assert ids(search_apps(APPS, "models")) == ["llm-stack", "nano-llm"]


def test_category_filters_combine_with_text():
    assert ids(search_apps(APPS, categories=["ai"])) == ["llm-stack", "nano-llm"]
    assert ids(search_apps(APPS, categories=["AI & Models"])) == ["llm-stack", "nano-llm"]
    assert ids(search_apps(APPS, "category:ai local")) == ["llm-stack"]
    assert ids(search_apps(APPS, categories=["games", "home"])) == ["cookbook", "pool"]
    assert search_apps(APPS, "tiny", categories=["Games"]) == []


def test_parse_query_extracts_category_tokens():
    assert parse_query("Cat:Games  Pool") == (("pool",), ("games",))


def test_category_counts_cover_vocabulary_and_extra_tags():
    counts = {c["id"]: c for c in category_counts([*APPS, {"id": "x", "categories": ["audio-tools"]}])}
    assert CATEGORY_IDS <= set(counts)
    assert counts["ai"]["count"] == 2 and counts["ai"]["label"] == "AI & Models"
    assert counts["security"]["count"] == 0
    assert counts["audio-tools"] == {"id": "audio-tools", "label": "Audio Tools", "description": "", "count": 1}


def test_schema_enum_matches_vocabulary():
    schema = json.loads(open("app.schema.json", encoding="utf-8").read())
    assert set(schema["properties"]["categories"]["items"]["enum"]) == CATEGORY_IDS


def test_listing_problems_are_warnings_not_errors():
    errors, warnings = manifest.validate_manifest(
        {"label": "X", "icon": "x", "version": "1.0.0", "categories": ["tools"]},
        has_index=True, dir_name="x")
    assert errors == []
    assert any(w.startswith("no description") for w in warnings)
    assert any(w.startswith("unknown category 'tools'") for w in warnings)
    _, missing = manifest.validate_manifest(
        {"label": "X", "icon": "x", "version": "1.0.0"}, has_index=True, dir_name="x")
    assert any(w.startswith("no categories") for w in missing)
    _, clean = manifest.validate_manifest(
        {"label": "X", "icon": "x", "version": "1.0.0", "description": "Does one thing.", "categories": ["games"]},
        has_index=True, dir_name="x")
    assert clean == []


def test_v2_nested_listing_metadata_satisfies_the_check(tmp_path):
    root = make_app(tmp_path, "nested", {
        "manifest_version": 2, "label": "Nested", "icon": "n",
        "metadata": {"version": "1.0.0", "description": "Nested listing.", "categories": ["learning"]},
    })
    inspection = manifest.parse_manifest(root)
    assert inspection.manifest is not None
    assert not [w for w in inspection.warnings if "description" in w.message or "categor" in w.message]


def test_strict_cli_fails_apps_missing_listing_metadata(tmp_path, capsys):
    make_app(tmp_path, "bare", {"label": "Bare", "icon": "b", "version": "1.0.0"})
    make_app(tmp_path, "good", {"label": "Good", "icon": "g", "version": "1.0.0",
                                "description": "A good app.", "categories": ["games"]})
    assert manifest._cli([str(tmp_path / "bare")]) == 0
    assert manifest._cli(["--strict", str(tmp_path / "good")]) == 0
    capsys.readouterr()
    assert manifest._cli(["--strict", str(tmp_path / "bare")]) == 1
    out = capsys.readouterr().out
    assert "error:   no description" in out
    assert "warning: no description" not in out


def load_engine(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENGINE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("APP_ENGINE_APPS_DIR", str(tmp_path / "apps"))
    import engine
    return importlib.reload(engine)


def test_launcher_api_searches_and_counts_categories(tmp_path, monkeypatch):
    for app in APPS:
        make_app(tmp_path / "apps", app["id"], {**app, "icon": "x", "version": "1.0.0"})
    module = load_engine(tmp_path, monkeypatch)
    with TestClient(module.app) as client:
        assert ids(client.get("/api/apps").json()) == ids(APPS)
        ranked = client.get("/api/apps", params={"q": "llm"}).json()
        assert ids(ranked) == ["llm-stack", "nano-llm"]
        assert all(app["url"] and "root" not in app for app in ranked)
        assert ids(client.get("/api/apps", params={"category": "ai", "q": "tiny"}).json()) == ["nano-llm"]
        counts = {c["id"]: c["count"] for c in client.get("/api/apps/categories").json()}
    assert counts["ai"] == 2 and counts["games"] == 1 and counts["security"] == 0
