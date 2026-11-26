"""App Review: the contract + OWASP admission gate for every app app-engine serves."""
import asyncio
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import zipfile

import httpx
import pytest
from fastapi.testclient import TestClient

from app_engine.app_review import ReviewGate, ReviewStore, main, review_app
from app_engine.catalog import ImmutableAppCatalog
from app_engine.contracts import AppSource, CatalogKey

LISTING = {"version": "1.0.0", "description": "A small app used to exercise App Review.",
           "author": "Fixture", "categories": ["developer"]}


def make_app(root: Path, app_id: str = "clean", files: dict | None = None, **manifest) -> Path:
    app = root / app_id
    app.mkdir(parents=True)
    (app / "app.json").write_text(json.dumps({"id": app_id, "label": app_id.title(), "icon": "🧪",
                                               **LISTING, **manifest}))
    defaults = {"index.html": "<!doctype html><title>Clean</title><script src=\"main.js\"></script>",
                "main.js": "document.body.textContent = 'hello';\n"}
    for name, content in {**defaults, **(files or {})}.items():
        path = app / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content)
    return app


def rules(report, blocking_only=False):
    return {f.rule for f in report.findings if f.blocking or not blocking_only}


def test_clean_app_passes_both_profiles(tmp_path):
    app = make_app(tmp_path)
    for profile in ("local", "store"):
        report = review_app(app, profile=profile)
        assert report.passed, report.summary()
        assert report.score == 100
        assert report.to_dict()["verdict"] == "pass"


def test_secrets_block_and_are_redacted(tmp_path):
    key = "AKIA" + "Q" * 16
    app = make_app(tmp_path, files={"main.js": f"const k = '{key}';\n"})
    report = review_app(app)
    assert not report.passed
    finding = next(f for f in report.findings if f.rule == "SEC-AWS-KEY")
    assert finding.severity == "critical" and finding.path == "main.js" and finding.line == 1
    assert key not in json.dumps(report.to_dict())


def test_private_key_bodies_block_but_header_markers_do_not(tmp_path):
    header = "-----BEGIN " + "OPENSSH PRIVATE KEY-----"
    body = "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAAB"
    shipped = {
        "id_ed25519": f"{header}\n{body}\n-----END OPENSSH PRIVATE KEY-----\n",
        "key.js": f"const k = '{header}\\n{body}';\n",
        "key.py": f'KEY = """{header}\n{body}\n"""\n',
        "split.js": f"const k = '{header}' +\n  '{body}';\n",
    }
    for name, content in shipped.items():
        app = make_app(tmp_path / name.replace(".", "-"), files={name: content})
        assert "SEC-PRIVATE-KEY" in rules(review_app(app), blocking_only=True), name
    parser = make_app(tmp_path / "parser", files={"keyd.swift": (
        f'guard text.contains("{header}"), let decoded = Data(base64Encoded: body) else {{ throw Failure() }}\n'
        f'if line.startsWith("{header}") && ok {{ parse(line) }}\n')})
    assert "SEC-PRIVATE-KEY" not in rules(review_app(parser))


def test_code_sinks_flag_code_but_not_prose_or_strings(tmp_path):
    app = make_app(tmp_path, files={
        "main.js": "const run = s => eval(s);\nconst f = new Function('a', 'return a');\n",
        "lesson.js": "const tip = 'never call eval(input) on user text';\n",
        "guide.html": "<p><code>eval(expr)</code> runs code</p>",
    })
    report = review_app(app)
    flagged = {(f.rule, f.path) for f in report.findings}
    assert ("INJ-EVAL", "main.js") in flagged and ("INJ-NEW-FUNCTION", "main.js") in flagged
    assert not any(path in {"lesson.js", "guide.html"} for rule, path in flagged if rule == "INJ-EVAL")
    assert not report.passed


def test_server_side_tests_never_block_but_served_test_code_does(tmp_path):
    report = review_app(make_app(tmp_path, files={"tests/test_tool.py": "eval(source)\n"}))
    finding = next(f for f in report.findings if f.rule == "INJ-PY-EVAL")
    assert finding.severity == "low" and report.passed
    served = review_app(make_app(tmp_path, "served", files={"tests/runner.js": "eval(source);\n"}))
    assert not served.passed and "INJ-EVAL" in rules(served, blocking_only=True)


def test_no_folder_or_minified_hiding_place(tmp_path):
    payload = "eval(location.hash.slice(1));fetch('/api/app-engine/stores');\n"
    for name in ("lib/app.js", "vendor/x.min.js", "node_modules/pkg/index.js", "dist/bundle.js",
                 "spec/helper.spec.js", "page.xhtml"):
        app = make_app(tmp_path, name.replace("/", "-").replace(".", "-"), files={name: payload})
        blocking = rules(review_app(app), blocking_only=True)
        assert {"INJ-EVAL", "ACL-HOST-ADMIN"} <= blocking, (name, blocking)
    long_line = make_app(tmp_path, "long", files={"main.js": "var a=1;" * 2000 + payload})
    assert "INJ-EVAL" in rules(review_app(long_line), blocking_only=True)


def test_quote_tricks_do_not_hide_code(tmp_path):
    for code in ("x = /'/; eval(y);\n", "const s = `${eval(input)}`;\n"):
        app = make_app(tmp_path, f"q{abs(hash(code)) % 10**6}", files={"main.js": code})
        assert "INJ-EVAL" in rules(review_app(app), blocking_only=True), code


def test_unreviewable_files_block(tmp_path):
    big = make_app(tmp_path, "big", files={"main.js": "// " + "a" * (16 * 1024 * 1024 + 10) + "\n"})
    report = review_app(big)
    assert not report.passed and "SCAN-SKIPPED" in rules(report, blocking_only=True)


def test_hostile_markup_is_reviewed_in_linear_time(tmp_path):
    import time
    app = make_app(tmp_path, files={"index.html": "<title>x</title>" + "<script " * 40000})
    started = time.monotonic()
    review_app(app)
    assert time.monotonic() - started < 5


def test_whole_file_waivers_are_bound_to_content(tmp_path):
    app = make_app(tmp_path / "apps", files={"deploy.pem": b"-one-"})
    store = ReviewStore(tmp_path / "state")
    finding = store.report(app).blocking[0]
    store.set_waiver("clean", finding.fingerprint, "reviewed build")
    assert store.report(app, max_age=0).passed
    (app / "deploy.pem").write_bytes(b"-two-")
    assert not store.report(app, max_age=0).passed


def test_skipped_folders_are_exactly_the_unserved_ones():
    from app_engine.app_review import is_unserved
    assert is_unserved("private/token.txt") and is_unserved("Private/x") and is_unserved(".venv/a.py")
    assert not is_unserved("node_modules/x.js") and not is_unserved("lib/app.js")


def test_remote_code_supply_chain(tmp_path):
    page = ("<title>x</title>"
            "<script src=\"https://cdn.jsdelivr.net/npm/three/build/three.min.js\"></script>"
            "<script src=\"https://cdn.jsdelivr.net/npm/d3@7.8.5/dist/d3.min.js\"></script>"
            "<script src=\"http://example.com/lib.js\"></script>")
    report = review_app(make_app(tmp_path, files={"index.html": page}))
    found = {(f.rule, f.severity) for f in report.findings}
    assert ("SUPPLY-UNPINNED", "high") in found
    assert ("SUPPLY-NO-SRI", "medium") in found
    assert ("SUPPLY-CLEARTEXT", "high") in found


def test_known_vulnerable_library(tmp_path):
    app = make_app(tmp_path, files={"vendor/jquery-3.4.1.min.js": "/*! jQuery v3.4.1 */"})
    finding = next(f for f in review_app(app).findings if f.rule == "VULN-LIBRARY")
    assert finding.severity == "high" and "3.4.1" in finding.title
    fixed = make_app(tmp_path, "fixed", files={"vendor/jquery-3.7.1.min.js": "/*! jQuery v3.7.1 */"})
    assert "VULN-LIBRARY" not in rules(review_app(fixed))


def test_boundary_violations(tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    app = make_app(tmp_path, files={
        "main.js": "fetch('/api/app-state/other-app');\nwindow.parent.document.title = 'x';\n",
        "tool.bin": b"\x7fELF\x02\x01\x01" + b"\0" * 32,
    })
    expected = {"ACL-CROSS-APP-STATE", "ACL-FRAME-ESCAPE"}
    try:
        os.symlink(outside, app / "leak.txt")
        expected.add("FS-SYMLINK-ESCAPE")
    except OSError:   # Windows without the symlink privilege
        pass
    assert expected <= rules(review_app(app), blocking_only=True)
    assert "FS-NATIVE-CODE" in rules(review_app(app, profile="store"), blocking_only=True)


def test_local_build_outputs_are_reported_not_blocking(tmp_path):
    app = make_app(tmp_path, files={"backend/server": b"\xcf\xfa\xed\xfe" + b"\0" * 64})
    local = next(f for f in review_app(app).findings if f.rule == "FS-NATIVE-CODE")
    assert local.severity == "medium" and review_app(app).passed
    assert not review_app(app, profile="store").passed


def test_own_state_and_generated_dirs_are_fine(tmp_path):
    app = make_app(tmp_path, files={
        "main.js": "fetch('/api/app-state/clean');\n",
        ".venv/lib/evil.py": "import os; os.system('x')\n",
        "private/token.txt": "ghp_" + "a" * 40,
    })
    report = review_app(app)
    assert report.passed and report.score == 100, report.summary()


def test_contract_checks(tmp_path):
    app = make_app(tmp_path, icon="icon.png", screenshots=["shots/one.png"],
                   files={"index.html": "<script src=\"missing.js\"></script>",
                          "main.js": "navigator.mediaDevices.getUserMedia({video: true});\n"})
    found = rules(review_app(app))
    assert {"CONTRACT-ICON", "CONTRACT-SCREENSHOT", "CONTRACT-MISSING-ASSET",
            "CONTRACT-TITLE", "CONTRACT-PERMISSION-UNDECLARED"} <= found
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "app.json").write_text("{not json")
    report = review_app(broken)
    assert not report.passed and report.findings[0].rule == "CONTRACT-MANIFEST"
    assert not report.findings[0].waivable


def test_store_profile_requires_listing(tmp_path):
    app = tmp_path / "bare"
    app.mkdir()
    (app / "app.json").write_text(json.dumps({"id": "bare", "label": "Bare", "icon": "B"}))
    (app / "index.html").write_text("<title>Bare</title>")
    assert review_app(app).passed
    store = review_app(app, profile="store")
    assert not store.passed
    assert {f.title for f in store.blocking} >= {"Store listing needs `version`", "Store listing needs `description`"}


def test_waivers_and_cache(tmp_path):
    app = make_app(tmp_path / "apps", files={"main.js": "eval(code);\n"})
    store = ReviewStore(tmp_path / "state")
    gate = ReviewGate(store)
    admitted, report = gate.admit(app)
    assert not admitted
    finding = report.blocking[0]
    store.set_waiver("clean", finding.fingerprint, "sandboxed code runner by design")
    admitted, report = gate.admit(app)
    assert admitted and report.counts()["waived"] == 1
    assert report.findings[0].waiver_reason == "sandboxed code runner by design"
    # The cached report is reused until the files change.
    first = store.report(app, max_age=0).scanned_at
    assert store.report(app, max_age=0).scanned_at == first
    (app / "main.js").write_text("eval(code);\nwindow.top.location.href = 'x';\n")
    os.utime(app / "main.js", ns=(1, 1))
    report = store.report(app, max_age=0)
    assert not report.passed and "ACL-FRAME-ESCAPE" in rules(report, blocking_only=True)
    store.set_waiver("clean", finding.fingerprint, None)
    assert store.waivers("clean") == {}


def test_a_waiver_covers_one_statement_only(tmp_path):
    app = make_app(tmp_path / "apps", files={"main.js": "eval(a);\neval(b);\n"})
    store = ReviewStore(tmp_path / "state")
    first, second = store.report(app).blocking
    assert first.fingerprint != second.fingerprint
    store.set_waiver("clean", first.fingerprint, "reviewed")
    report = store.report(app)
    assert not report.passed and [f.line for f in report.blocking] == [2]


def test_gate_modes(tmp_path):
    app = make_app(tmp_path / "apps", files={"main.js": "eval(code);\n"})
    assert ReviewGate(ReviewStore(tmp_path / "s1"), "warn").admit(app)[0]
    assert ReviewGate(ReviewStore(tmp_path / "s2"), "off").admit(app) == (True, None)
    assert not ReviewGate(ReviewStore(tmp_path / "s3"), "enforce").admit(app)[0]
    with pytest.raises(ValueError):
        ReviewGate(ReviewStore(tmp_path / "s4"), "loose")


def test_catalog_rejects_apps_that_fail_review(tmp_path):
    apps = tmp_path / "apps"
    make_app(apps, "good")
    make_app(apps, "bad", files={"main.js": "eval(code);\n"})
    gate = ReviewGate(ReviewStore(tmp_path / "state"))

    def reviewer(manifest, source):
        admitted, report = gate.admit(manifest.root)
        return None if admitted else report.summary()

    catalog = ImmutableAppCatalog(reviewer=reviewer)
    snapshot = asyncio.run(catalog.configure(CatalogKey("k"), (AppSource("trusted", apps, 1),)))
    assert [str(app.manifest.app_id) for app in snapshot.apps] == ["good"]
    rejection = next(item for item in snapshot.rejections if item.app_id == "bad")
    assert rejection.reason == "review_failed" and "INJ-EVAL" in rejection.detail


def test_cli_exit_codes(tmp_path, capsys):
    good = make_app(tmp_path, "good")
    bad = make_app(tmp_path, "bad", files={"main.js": "eval(code);\n"})
    assert main([str(good)]) == 0
    assert "Verdict: PASS" in capsys.readouterr().out
    assert main([str(bad), "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["verdict"] == "fail"


def _engine(tmp_path, monkeypatch, mode="enforce"):
    monkeypatch.setenv("APP_ENGINE_APPS_DIR", str(tmp_path / "local"))
    monkeypatch.setenv("APP_ENGINE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("APP_ENGINE_APP_REVIEW", mode)
    for name in ("APP_ENGINE_PUBLIC_ORIGIN", "APP_ENGINE_APP_ORIGINS", "APP_ENGINE_TRUST_SAME_ORIGIN_APPS"):
        monkeypatch.delenv(name, raising=False)
    import engine
    return importlib.reload(engine)


def test_engine_blocks_failing_local_apps_and_owner_can_waive(tmp_path, monkeypatch):
    make_app(tmp_path / "local", "good")
    make_app(tmp_path / "local", "risky", files={"main.js": "eval(code);\n"})
    module = _engine(tmp_path, monkeypatch)
    admin = {"x-app-engine-admin": module._admin_capability}
    with TestClient(module.app) as client:
        ids = {app["id"] for app in client.get("/api/apps").json()}
        assert "good" in ids and "risky" not in ids
        rejected = client.get("/api/apps/rejected").json()
        assert any(item["dir"] == "risky" and item["review"] for item in rejected)

        overview = client.get("/api/app-engine/review").json()
        assert overview["mode"] == "enforce"
        assert {a["app_id"]: a["verdict"] for a in overview["apps"]} == {"good": "pass", "risky": "fail"}
        report = client.get("/api/app-engine/review/apps/risky").json()
        finding = next(f for f in report["findings"] if f["blocking"])

        body = {"fingerprint": finding["fingerprint"], "reason": "code runner by design"}
        assert client.post("/api/app-engine/review/apps/risky/waivers", json=body).status_code == 403
        assert client.post("/api/app-engine/review/apps/risky/waivers", headers=admin,
                           json={"fingerprint": finding["fingerprint"], "reason": ""}).status_code == 400
        waived = client.post("/api/app-engine/review/apps/risky/waivers", headers=admin, json=body)
        assert waived.status_code == 200 and waived.json()["verdict"] == "pass"
        assert "risky" in {app["id"] for app in client.get("/api/apps").json()}

        undone = client.delete(f"/api/app-engine/review/apps/risky/waivers/{finding['fingerprint']}", headers=admin)
        assert undone.json()["verdict"] == "fail"
        assert client.post("/api/app-engine/review/apps/risky/rescan", headers=admin).status_code == 200
        assert client.get("/api/app-engine/review/apps/nope").status_code == 404

        page = client.get("/api/app-engine/review/ui")
        assert page.status_code == 200 and module._admin_capability in page.text
        policy = page.headers["content-security-policy"]
        assert "frame-ancestors 'none'" in policy and "unsafe-inline" not in policy
        assert client.get("/api/app-engine/review/rules").json()["rules"]


def test_engine_never_serves_folders_the_review_skips(tmp_path, monkeypatch):
    make_app(tmp_path / "local", "good", files={"private/token.txt": "secret", "Private/x.js": "eval(a)",
                                                 "node_modules/lib/ok.js": "export const ok = 1;\n"})
    module = _engine(tmp_path, monkeypatch)
    host = {"host": "good.localhost"}
    with TestClient(module.app) as client:
        assert client.get("/apps/good/main.js", headers=host).status_code == 200
        assert client.get("/apps/good/node_modules/lib/ok.js", headers=host).status_code == 200
        assert client.get("/apps/good/private/token.txt", headers=host).status_code == 404
        assert client.get("/apps/good/Private/x.js", headers=host).status_code == 404


def test_engine_warn_mode_serves_failing_apps(tmp_path, monkeypatch):
    make_app(tmp_path / "local", "risky", files={"main.js": "eval(code);\n"})
    module = _engine(tmp_path, monkeypatch, mode="warn")
    with TestClient(module.app) as client:
        assert "risky" in {app["id"] for app in client.get("/api/apps").json()}
        app = client.get("/api/app-engine/review").json()["apps"][0]
        assert app["verdict"] == "fail" and app["served"] is True


def _store_transport(files: dict, manifest: dict):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("app.json", json.dumps(manifest))
        for name, content in files.items():
            archive.writestr(name, content)
    body = buffer.getvalue()
    release = dict(id=manifest["id"], version="1.0.0", label=manifest["label"], icon="S",
                   description=manifest["description"], categories=manifest["categories"],
                   author=manifest["author"], min_engine_version="",
                   package_url=f"api/v1/packages/{manifest['id']}/1.0.0",
                   sha256=hashlib.sha256(body).hexdigest(), size_bytes=len(body))

    def handle(request):
        if request.url.path.endswith("/catalog"):
            return httpx.Response(200, json={"protocol_version": 1, "name": "Fixture", "apps": [release]})
        return httpx.Response(200, content=body)
    return httpx.MockTransport(handle)


def test_store_install_is_refused_with_the_report(tmp_path, monkeypatch):
    module = _engine(tmp_path, monkeypatch)
    manifest = {"id": "leaky", "label": "Leaky", "icon": "S", **LISTING}
    transport = _store_transport({"index.html": "<title>Leaky</title>",
                                  "config.js": "const key = 'sk-ant-" + "x" * 40 + "';\n"}, manifest)
    admin = {"x-app-engine-admin": module._admin_capability}
    with TestClient(module.app) as client:
        module._store_bridge._client = httpx.AsyncClient(transport=transport)
        store = client.post("/api/app-engine/stores", headers=admin, json={"url": "http://store.test:8780"})
        preview = client.post("/api/app-engine/store-installs/preview", headers=admin,
                              json={"store_id": store.json()["id"], "app_id": "leaky", "version": "1.0.0"})
        refused = client.post("/api/app-engine/store-installs", headers=admin,
                              json={"fingerprint": preview.json()["fingerprint"]})
        assert refused.status_code == 422
        detail = refused.json()["detail"]
        assert detail["code"] == "review_failed"
        assert detail["review"]["verdict"] == "fail" and detail["review"]["root"] == ""
        assert any(f["rule"] == "SEC-AI-KEY" for f in detail["review"]["findings"])
        assert "sk-ant-xxxx" not in refused.text
        assert not (module.STORE_APPS_DIR / "leaky").exists()
