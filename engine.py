#!/usr/bin/env python3
"""app-engine — a minimal, standalone host runtime for Atrium-style apps.

This is the *extracted* app engine: everything an Atrium "app" needs at
runtime, with zero dependency on Atrium itself. Point it at a directory of
apps and it discovers, serves, and runs them.

The host<->app contract it implements (identical to Atrium's):

  GET  /api/apps                 -> discovered app list (launcher metadata)
  GET  /state                    -> {csrf_token, session}   (stub, single-user)
  GET  /api/app-state/{app_id}   -> per-app JSON blob (localStorage replacement)
  PUT  /api/app-state/{app_id}   -> persist per-app JSON blob
  POST /api/app-chat             -> SSE proxy to any OpenAI-compatible LLM
  GET  /apps/{file}              -> shared root files (e.g. app-state-bridge.js)
  GET  /apps/{app_id}/{path}     -> app static files, OR reverse-proxy to
                                    the app's `entry_point` backend

Config via env:
  APP_ENGINE_APPS_DIR   directory whose immediate children are apps
                        (each with app.json + index.html, or an entry_point)
  APP_ENGINE_STATE_DIR  where per-app state JSON is stored
                        (default: ~/.config/app-engine/app-state)
  APP_ENGINE_LLM_BASE   OpenAI-compatible base URL for chat
                        (default: http://localhost:11434  — Ollama)
  APP_ENGINE_LLM_MODEL  chat model name (default: gemma3:4b)
  APP_ENGINE_HOST/PORT  bind address (default 127.0.0.1:8770)
"""
from __future__ import annotations

import json
import mimetypes
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

# ── Config ────────────────────────────────────────────────────────────────────

APPS_DIR = Path(os.environ.get("APP_ENGINE_APPS_DIR", "./apps")).expanduser().resolve()
STATE_DIR = Path(
    os.environ.get("APP_ENGINE_STATE_DIR", "~/.config/app-engine/app-state")
).expanduser()
LLM_BASE = os.environ.get("APP_ENGINE_LLM_BASE", "http://localhost:11434").rstrip("/")
LLM_MODEL = os.environ.get("APP_ENGINE_LLM_MODEL", "gemma3:4b")
MAX_STATE_BYTES = 100 * 1024  # 100 KB, matches Atrium
_APP_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

app = FastAPI(title="app-engine")
_proxy: httpx.AsyncClient | None = None


def _client() -> httpx.AsyncClient:
    global _proxy
    if _proxy is None or _proxy.is_closed:
        _proxy = httpx.AsyncClient(timeout=30.0, follow_redirects=True)
    return _proxy


# ── Discovery ─────────────────────────────────────────────────────────────────

@dataclass
class App:
    id: str
    label: str
    icon: str
    sandbox: str
    chat_enabled: bool
    chat_system_prompt: str
    entry_point: str
    url: str
    root: str  # filesystem path (not sent to client)


def discover() -> dict[str, App]:
    """Scan APPS_DIR's immediate children for valid apps (app.json)."""
    out: dict[str, App] = {}
    if not APPS_DIR.is_dir():
        return out
    for d in sorted(APPS_DIR.iterdir()):
        if d.name.startswith(".") or not d.is_dir():
            continue
        manifest = d / "app.json"
        if not manifest.is_file():
            continue
        try:
            m = json.loads(manifest.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        entry_point = m.get("entry_point", "")
        if not (d / "index.html").is_file() and not entry_point:
            continue
        app_id = m.get("id", d.name)
        if not _APP_ID_RE.match(app_id) or not m.get("label") or not m.get("icon"):
            continue
        sandbox = m.get("sandbox", "allow-scripts")
        if sandbox is False:
            sandbox = ""
        out[app_id] = App(
            id=app_id,
            label=m["label"],
            icon=m["icon"],
            sandbox=sandbox,
            chat_enabled=bool(m.get("chat_enabled", False)),
            chat_system_prompt=m.get("chat_system_prompt", ""),
            entry_point=entry_point,
            url=f"/apps/{app_id}/",
            root=str(d.resolve()),
        )
    return out


# ── App-state persistence (localStorage replacement) ──────────────────────────

def _state_file(app_id: str) -> Path:
    if not _APP_ID_RE.match(app_id):
        raise HTTPException(404)
    return STATE_DIR / f"{app_id}.json"


@app.get("/api/app-state/{app_id}")
async def get_app_state(app_id: str) -> JSONResponse:
    f = _state_file(app_id)
    if not f.is_file():
        return JSONResponse({})
    try:
        return JSONResponse(json.loads(f.read_text()))
    except (json.JSONDecodeError, OSError):
        return JSONResponse({})


@app.put("/api/app-state/{app_id}")
async def put_app_state(app_id: str, request: Request) -> JSONResponse:
    raw = await request.body()
    if len(raw) > MAX_STATE_BYTES:
        raise HTTPException(413, "state too large")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(400, "invalid JSON")
    if not isinstance(data, (dict, list)):
        raise HTTPException(400, "state must be object or array")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    _state_file(app_id).write_text(json.dumps(data))
    return JSONResponse({"ok": True})


# ── /state stub (single-user, no auth) ────────────────────────────────────────

@app.get("/state")
async def state() -> JSONResponse:
    return JSONResponse(
        {"csrf_token": "", "session": {"username": "local", "role": "admin"}}
    )


# ── Chat proxy (OpenAI-compatible; Ollama by default) ─────────────────────────

@app.post("/api/app-chat")
async def app_chat(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    app_id = (body.get("app_id") or "").strip()
    messages = body.get("messages")
    apps = discover()
    if app_id not in apps:
        return JSONResponse({"error": "app not found"}, status_code=404)
    if not apps[app_id].chat_enabled:
        return JSONResponse({"error": "chat not enabled"}, status_code=403)
    if not isinstance(messages, list) or not messages:
        return JSONResponse({"error": "messages required"}, status_code=400)

    system = apps[app_id].chat_system_prompt or "You are a helpful tutor."
    payload = {
        "model": LLM_MODEL,
        "messages": [{"role": "system", "content": system}, *messages],
        "stream": True,
    }

    async def gen():
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(120.0)) as c:
                async with c.stream(
                    "POST", f"{LLM_BASE}/v1/chat/completions", json=payload
                ) as resp:
                    if resp.status_code != 200:
                        err = (await resp.aread()).decode()[:200]
                        yield f"data: {json.dumps({'type': 'error', 'error': f'LLM {resp.status_code}: {err}'})}\n\n"
                        return
                    async for line in resp.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        chunk = line[6:].strip()
                        if chunk == "[DONE]":
                            break
                        try:
                            delta = json.loads(chunk)["choices"][0]["delta"]
                            text = delta.get("content", "")
                            if text:
                                yield f"data: {json.dumps({'type': 'token', 'text': text})}\n\n"
                        except (json.JSONDecodeError, KeyError, IndexError):
                            continue
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
        except httpx.ConnectError:
            yield f"data: {json.dumps({'type': 'error', 'error': f'LLM not reachable at {LLM_BASE}'})}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


# ── Static / entry_point serving ──────────────────────────────────────────────

_SEC_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "frame-ancestors *",
}


def _inject_base_href(html: bytes, app_id: str) -> bytes:
    base = f'<base href="/apps/{app_id}/">'.encode()
    m = re.search(rb"(<head\b[^>]*>)", html, re.IGNORECASE)
    if not m:
        return html
    html = html[: m.end()] + base + html[m.end() :]
    attr = re.compile(rb"(\b(?:src|href|action)=[\"'])/(?!/|apps/)([^\"']*)", re.IGNORECASE)
    pref = f"/apps/{app_id}/".encode()
    return attr.sub(lambda x: x.group(1) + pref + x.group(2), html)


@app.get("/apps/{filename}")
async def shared_file(filename: str) -> Response:
    """Serve shared root-level files (e.g. app-state-bridge.js) referenced as
    ``../file.js`` from inside an app iframe."""
    if "." not in filename or "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(404)
    candidate = (APPS_DIR / filename).resolve()
    if candidate.is_file() and str(candidate).startswith(str(APPS_DIR)):
        mime, _ = mimetypes.guess_type(str(candidate))
        return Response(candidate.read_bytes(), media_type=mime or "application/octet-stream",
                        headers=_SEC_HEADERS)
    raise HTTPException(404)


async def _proxy_entry_point(app_obj: App, path: str, request: Request) -> Response:
    target = app_obj.entry_point.rstrip("/") + "/" + path
    headers = {k: v for k, v in request.headers.items()
               if k.lower() not in ("host", "content-length", "transfer-encoding")}
    try:
        resp = await _client().request(
            request.method, target, headers=headers,
            content=await request.body(), params=dict(request.query_params),
        )
    except httpx.HTTPError:
        raise HTTPException(502, "entry_point unreachable")
    ctype = resp.headers.get("content-type", "")
    content = resp.content
    if "text/html" in ctype:
        content = _inject_base_href(content, app_obj.id)
    skip = {"transfer-encoding", "content-encoding", "content-length", "connection"}
    hdrs = {k: v for k, v in resp.headers.items() if k.lower() not in skip}
    return Response(content, status_code=resp.status_code, headers=hdrs, media_type=ctype)


@app.api_route("/apps/{app_id}/", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
@app.api_route("/apps/{app_id}/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def serve_app(app_id: str, request: Request, path: str = "") -> Response:
    apps = discover()
    app_obj = apps.get(app_id)
    if not app_obj:
        raise HTTPException(404)

    # entry_point apps: reverse-proxy everything to their own backend.
    if app_obj.entry_point:
        return await _proxy_entry_point(app_obj, path, request)

    safe = Path(path) if path else Path("index.html")
    if ".." in safe.parts or safe.is_absolute():
        raise HTTPException(404)
    root = Path(app_obj.root)
    full = (root / safe).resolve()
    if not str(full).startswith(str(root)):
        raise HTTPException(404)
    if full.is_dir():
        full = full / "index.html"
    if not full.is_file():
        raise HTTPException(404)
    mime, _ = mimetypes.guess_type(str(full))
    return Response(full.read_bytes(), media_type=mime or "application/octet-stream",
                    headers=_SEC_HEADERS)


# ── Launcher UI ───────────────────────────────────────────────────────────────

@app.get("/api/apps")
async def api_apps() -> JSONResponse:
    apps = [asdict(a) for a in discover().values()]
    for a in apps:
        a.pop("root", None)
    return JSONResponse(apps)


@app.get("/", response_class=HTMLResponse)
async def launcher() -> HTMLResponse:
    return HTMLResponse((Path(__file__).parent / "launcher.html").read_text())


@app.on_event("shutdown")
async def _shutdown():
    if _proxy and not _proxy.is_closed:
        await _proxy.aclose()


if __name__ == "__main__":
    print(f"app-engine → apps from {APPS_DIR}")
    print(f"            state in {STATE_DIR}")
    print(f"            LLM at   {LLM_BASE} ({LLM_MODEL})")
    uvicorn.run(
        app,
        host=os.environ.get("APP_ENGINE_HOST", "127.0.0.1"),
        port=int(os.environ.get("APP_ENGINE_PORT", "8770")),
    )
