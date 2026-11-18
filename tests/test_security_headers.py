"""Headers the 2026-11-12 ZAP scan found missing: proxied apps, app pages, bundled assets."""

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

from app_engine.response_headers import Nosniff, proxied_defaults
from test_public_origin import PUBLIC, load_engine, public_client, sign_in

LOOPBACK_FRAMING = ("frame-ancestors 'self' http://127.0.0.1:* http://localhost:* "
                    "https://127.0.0.1:* https://localhost:*")
APP_PAGE_POLICY = "object-src 'none'; base-uri 'self'; form-action 'self'; "

# What an app with its own strict policy sends; the engine must not alter a byte of it.
OWN_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; script-src 'sha256-AbCd+/=' ; frame-ancestors 'self' http://127.0.0.1:8770",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Resource-Policy": "same-origin",
}


@pytest.fixture
def backend():
    """An app backend: `/` sends no security headers, `/own` sends its own, `/events` streams."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split("?")[0]
            body, kind, extra = b"<!doctype html><html><head></head><body>app</body></html>", "text/html; charset=utf-8", {}
            if path == "/own":
                extra = OWN_HEADERS
            elif path == "/data.json":
                body, kind = b'{"ok": true}', "application/json"
            elif path == "/events":
                body, kind = b"data: one\n\ndata: two\n\n", "text/event-stream"
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            for name, value in extra.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


def _add_backend_app(module, backend):
    root = module.APPS_DIR / "backend"
    root.mkdir()
    (root / "app.json").write_text(json.dumps({
        "id": "backend", "label": "Backend", "icon": "B",
        "entry_point": f"http://127.0.0.1:{backend.server_port}"}))
    from app_engine.backend_bindings import binding_fingerprint
    module._record_code_approval(binding_fingerprint('backend', root, f'http://127.0.0.1:{backend.server_port}')[0])


def _open_proxy(client, module, base=""):
    admin = {"x-app-engine-admin": module._admin_capability}
    assert client.post(f"{base}/api/app-engine/catalog/refresh", headers=admin).status_code == 200
    opened = client.post(f"{base}/api/app-engine/apps/backend/open", json={}, headers=admin)
    assert opened.status_code == 200, opened.text
    return f"/api/app-engine/sessions/{opened.json()['session_id']}/proxy"


# ── 1. The session proxy fills in what the backend left out ──────────────────

def test_proxied_page_without_headers_gets_nosniff_and_the_framing_policy(tmp_path, monkeypatch, backend):
    module = load_engine(tmp_path, monkeypatch, APP_ENGINE_FRAME_ANCESTORS="https://desk.example")
    _add_backend_app(module, backend)
    with TestClient(module.app) as client:
        proxy = _open_proxy(client, module)
        app_host = {"host": "backend.localhost"}
        page = client.get(f"{proxy}/", headers=app_host)
        assert page.status_code == 200 and "app" in page.text
        assert page.headers.get_list("x-content-type-options") == ["nosniff"]
        assert page.headers.get_list("content-security-policy") == [f"{LOOPBACK_FRAMING} https://desk.example"]
        assert "script-src" not in page.headers["content-security-policy"], "a backend's inline scripts must keep working"

        data = client.get(f"{proxy}/data.json", headers=app_host)
        assert data.json() == {"ok": True}
        assert data.headers.get_list("x-content-type-options") == ["nosniff"]
        assert "content-security-policy" not in data.headers, "the framing policy is for pages"

        events = client.get(f"{proxy}/events", headers=app_host)
        assert events.text == "data: one\n\ndata: two\n\n"
        assert events.headers["content-type"] == "text/event-stream"
        assert events.headers.get_list("x-content-type-options") == ["nosniff"]


def test_proxied_page_keeps_the_headers_its_backend_sent(tmp_path, monkeypatch, backend):
    module = load_engine(tmp_path, monkeypatch)
    _add_backend_app(module, backend)
    with TestClient(module.app) as client:
        proxy = _open_proxy(client, module)
        page = client.get(f"{proxy}/own", headers={"host": "backend.localhost"})
        assert page.status_code == 200
        for name, value in OWN_HEADERS.items():
            assert page.headers.get_list(name) == [value], name


def test_proxied_page_is_framed_only_by_the_public_launcher(tmp_path, monkeypatch, backend):
    module = load_engine(tmp_path, monkeypatch, APP_ENGINE_PUBLIC_ORIGIN=PUBLIC, APP_ENGINE_ADMIN_SECRET="hunter2-but-longer")
    _add_backend_app(module, backend)
    with public_client(module) as client:
        assert sign_in(client).status_code == 303
        proxy = _open_proxy(client, module, "https://play.example.com")
        page = client.get(f"https://backend.play.example.com{proxy}/")
        assert page.status_code == 200
        assert page.headers.get_list("content-security-policy") == ["frame-ancestors https://play.example.com"]
        assert page.headers.get_list("x-content-type-options") == ["nosniff"]


def test_proxied_defaults_never_replace_or_double_a_header():
    html = (b"content-type", b"Text/HTML; charset=utf-8")
    assert proxied_defaults([html], "frame-ancestors 'self'") == [
        html, (b"x-content-type-options", b"nosniff"), (b"content-security-policy", b"frame-ancestors 'self'")]
    own = [html, (b"Content-Security-Policy", b"default-src 'none'"), (b"X-Content-Type-Options", b"nosniff")]
    assert proxied_defaults(own, "frame-ancestors 'self'") == own
    assert proxied_defaults([html]) == [html, (b"x-content-type-options", b"nosniff")], "no policy configured, none added"
    assert proxied_defaults([], "frame-ancestors 'self'") == [(b"x-content-type-options", b"nosniff")]


@pytest.mark.asyncio
async def test_nosniff_middleware_does_not_hold_back_a_stream():
    """An SSE event must reach the browser while the response is still open."""
    release = asyncio.Event()
    sent = []

    async def sse(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/event-stream")]})
        await send({"type": "http.response.body", "body": b"data: one\n\n", "more_body": True})
        await release.wait()
        await send({"type": "http.response.body", "body": b"data: two\n\n"})

    async def send(message):
        sent.append(message)

    task = asyncio.create_task(Nosniff(sse)({"type": "http"}, None, send))
    for _ in range(5):
        await asyncio.sleep(0)
    assert [m["type"] for m in sent] == ["http.response.start", "http.response.body"], "first event delivered before the stream ends"
    assert sent[0]["headers"] == [(b"content-type", b"text/event-stream"), (b"x-content-type-options", b"nosniff")]
    assert sent[1]["body"] == b"data: one\n\n"
    release.set()
    await task
    assert sent[2]["body"] == b"data: two\n\n"


# ── 2. App pages: more than a framing policy ─────────────────────────────────

def test_app_pages_refuse_plugins_and_foreign_base_and_form_targets(tmp_path, monkeypatch, backend):
    module = load_engine(tmp_path, monkeypatch)
    _add_backend_app(module, backend)
    (module.APPS_DIR / "notes" / "app.js").write_text("console.log('notes')")
    with TestClient(module.app) as client:
        static = client.get("/apps/notes/", headers={"host": "notes.localhost"})
        assert static.headers.get_list("content-security-policy") == [APP_PAGE_POLICY + LOOPBACK_FRAMING]
        assert "script-src" not in static.headers["content-security-policy"], "apps ship inline scripts"
        script = client.get("/apps/notes/app.js", headers={"host": "notes.localhost"})
        assert script.headers.get_list("x-content-type-options") == ["nosniff"]

        app_host = {"host": "backend.localhost"}
        wrapped = client.get("/apps/backend/", headers=app_host)
        assert '<base href="/apps/backend/">' in wrapped.text, "the injected base is same-origin, so base-uri 'self' allows it"
        assert wrapped.headers.get_list("content-security-policy") == [APP_PAGE_POLICY + LOOPBACK_FRAMING]
        assert wrapped.headers.get_list("x-content-type-options") == ["nosniff"]
        own = client.get("/apps/backend/own", headers=app_host)
        for name, value in OWN_HEADERS.items():
            assert own.headers.get_list(name) == [value], name
        assert "content-security-policy" not in client.get("/apps/backend/data.json", headers=app_host).headers


# ── 3. Everything the engine serves is nosniff, once ─────────────────────────

def test_every_engine_response_is_nosniff_exactly_once(tmp_path, monkeypatch):
    module = load_engine(tmp_path, monkeypatch)
    with TestClient(module.app) as client:
        for path, kind in (("/assistant-assets/panel.css", "text/css"),
                           ("/assistant-assets/panel.js", "javascript"),
                           ("/assistant-assets/missing.js", "application/json"),
                           ("/", "text/html"), ("/api/engine", "application/json"),
                           ("/api/app-engine/studio/", "text/html")):
            response = client.get(path)
            assert kind in response.headers["content-type"], path
            assert response.headers.get_list("x-content-type-options") == ["nosniff"], path
        refused = client.get("/api/engine", headers={"host": "evil.example"})
        assert refused.status_code == 421
        assert refused.headers.get_list("x-content-type-options") == ["nosniff"], "refusals too"
