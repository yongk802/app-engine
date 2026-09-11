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
  APP_ENGINE_HOST/PORT  bind address (default 127.0.0.1:8770)
"""
from __future__ import annotations

import json
import os
import platform
import re
import secrets
import shutil
import subprocess
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse

from app_engine.chat import ChatRuntime
from app_engine.media_types import asset_media_type
from app_engine.config import ConfigStore
from app_engine.grounding import InvalidKnowledgePackError, KnowledgeBase, load_declared_knowledge
from app_engine import manifest as _manifest
from app_engine.manifest import ENGINE_VERSION
from app_engine.ollama import ConfirmationError, OllamaManager, OllamaOperationError, UnmanagedModelError
from app_engine.registry import InvalidRegistryError, ModelRegistry
from app_engine.routes import create_app_engine_router
from app_engine.runtime import DefaultAppEngineRuntime, LocalHostAdapter
from app_engine.search import category_counts, search_apps
from app_engine.system_probe import SystemProbe
from app_engine.contracts import HostSubject

# ── Config ────────────────────────────────────────────────────────────────────

APPS_DIR = Path(os.environ.get("APP_ENGINE_APPS_DIR", "./apps")).expanduser().resolve()
STATE_DIR = Path(
    os.environ.get("APP_ENGINE_STATE_DIR", "~/.config/app-engine/app-state")
).expanduser()
MAX_STATE_BYTES = 100 * 1024  # 100 KB, matches Atrium
_APP_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

# Browser apps run on per-app ``<id>.localhost`` origins. Unforgeable random
# capabilities provide defense in depth and also authorize app-state calls from
# non-browser clients. Tokens live only for this engine process and never enter
# query strings or server logs.
_app_capabilities: dict[str, str] = {}
_admin_capability = secrets.token_urlsafe(32)

_proxy: httpx.AsyncClient | None = None

_runtime_subject = HostSubject("local", "admin")
_runtime_host = LocalHostAdapter(
    apps_roots=(APPS_DIR,),
    state_root=STATE_DIR,
    subject=_runtime_subject,
)
_app_runtime = DefaultAppEngineRuntime(
    host=_runtime_host,
    subject=_runtime_subject,
    runtime_root=STATE_DIR / "runtime",
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    await _app_runtime.start()
    yield
    await _app_runtime.close(5.0)
    if _proxy and not _proxy.is_closed:
        await _proxy.aclose()
    await _ollama_manager.close()


app = FastAPI(title="app-engine", lifespan=lifespan)


async def _runtime_authenticate() -> HostSubject:
    """Standalone app-engine is a single local administrative session."""
    return _runtime_subject


app.include_router(
    create_app_engine_router(_app_runtime, authenticate=_runtime_authenticate)
)
_registry = ModelRegistry.load(Path(__file__).parent / "model-registry.json")
_knowledge = KnowledgeBase.load(Path(__file__).parent / "knowledge" / "tutors.json")
_config = ConfigStore(STATE_DIR)
_probe = SystemProbe()
_os_name = {"Darwin": "macos", "Windows": "windows", "Linux": "linux"}.get(platform.system(), platform.system().lower())
_ollama_manager = OllamaManager(_registry, _config, os_name=_os_name)


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
    chat_knowledge: str
    entry_point: str
    url: str
    # listing metadata (shared manifest v2)
    version: str
    description: str
    categories: list
    author: str
    screenshots: list
    min_engine_version: str
    permissions: list        # requested browser capabilities (mic, camera, …)
    allow: str               # iframe Permissions-Policy string derived from them
    compatible: bool
    warnings: list
    root: str  # filesystem path (not sent to client)


def inspect_apps() -> tuple[dict[str, App], list[dict]]:
    """Scan APPS_DIR's immediate children.

    Returns (apps_by_id, rejected). `rejected` lists folders that look like an
    app but failed validation — surfaced so a malformed manifest is visible
    instead of silently vanishing.
    """
    out: dict[str, App] = {}
    rejected: list[dict] = []
    if not APPS_DIR.is_dir():
        return out, rejected
    for d in sorted(APPS_DIR.iterdir()):
        if d.name.startswith(".") or not d.is_dir():
            continue
        manifest = d / "app.json"
        if not manifest.is_file():
            continue
        try:
            m = json.loads(manifest.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeError, OSError) as exc:
            rejected.append({"dir": d.name, "reason": f"invalid app.json: {exc}"})
            continue
        has_index = (d / "index.html").is_file()
        errors, warnings = _manifest.validate_manifest(m, has_index=has_index, dir_name=d.name)
        if errors:
            rejected.append({"dir": d.name, "reason": "; ".join(errors)})
            continue
        n = _manifest.normalize(m, d.name)
        app_id = n["id"]
        out[app_id] = App(
            id=app_id,
            label=n["label"],
            icon=n["icon"],
            sandbox=n["sandbox"],
            chat_enabled=n["chat_enabled"],
            chat_system_prompt=n["chat_system_prompt"],
            chat_knowledge=n["chat_knowledge"],
            entry_point=n["entry_point"],
            url=f"/apps/{app_id}/",
            version=n["version"],
            description=n["description"],
            categories=n["categories"],
            author=n["author"],
            screenshots=n["screenshots"],
            min_engine_version=n["min_engine_version"],
            permissions=n["permissions"],
            allow=n["allow"],
            compatible=_manifest.is_compatible(n["min_engine_version"], ENGINE_VERSION),
            warnings=warnings,
            root=str(d.resolve()),
        )
    return out, rejected


def discover() -> dict[str, App]:
    """Scan APPS_DIR's immediate children for valid apps (app.json)."""
    return inspect_apps()[0]


# ── App-state persistence (localStorage replacement) ──────────────────────────

def _state_file(app_id: str) -> Path:
    if not _APP_ID_RE.match(app_id):
        raise HTTPException(404)
    return STATE_DIR / f"{app_id}.json"


def _hostname(request: Request) -> str:
    return (request.url.hostname or "").lower().rstrip(".")


def _app_host_id(request: Request) -> str | None:
    host = _hostname(request)
    if not host.endswith(".localhost"):
        return None
    candidate = host[:-len(".localhost")]
    return candidate if _APP_ID_RE.fullmatch(candidate) else None


def _is_launcher_host(request: Request) -> bool:
    return _hostname(request) in {"127.0.0.1", "localhost", "::1", "testserver"}


def _app_capability(app_id: str) -> str:
    return _app_capabilities.setdefault(app_id, secrets.token_urlsafe(32))


def _enforce_app_state_access(request: Request, app_id: str) -> None:
    host_app = _app_host_id(request)
    supplied = request.headers.get("x-app-state-token", "")
    expected = _app_capability(app_id)
    if host_app != app_id or not secrets.compare_digest(supplied, expected):
        raise HTTPException(403, "app-state capability denied")


def _deny_app_iframe(request: Request) -> None:
    """Require the launcher-only capability for local-AI mutations."""
    supplied = request.headers.get("x-app-engine-admin", "")
    if not _is_launcher_host(request) or not secrets.compare_digest(supplied, _admin_capability):
        raise HTTPException(403, "launcher capability required")


@app.get("/api/app-state/{app_id}")
async def get_app_state(app_id: str, request: Request) -> JSONResponse:
    _enforce_app_state_access(request, app_id)
    f = _state_file(app_id)
    if not f.is_file():
        return JSONResponse({})
    try:
        return JSONResponse(json.loads(f.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, UnicodeError, OSError):
        return JSONResponse({})


@app.put("/api/app-state/{app_id}")
async def put_app_state(app_id: str, request: Request) -> JSONResponse:
    _enforce_app_state_access(request, app_id)
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
    _state_file(app_id).write_text(json.dumps(data), encoding="utf-8")
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

    async def gen():
        status = await _ollama_manager.status()
        config = _config.load()
        profile = _registry.get_profile(config.selected_profile)
        ready = status.running and profile.model_id in status.installed_model_ids
        client = httpx.AsyncClient(base_url=config.ollama_endpoint, timeout=httpx.Timeout(180.0))
        knowledge = _knowledge
        if apps[app_id].chat_knowledge:
            try:
                knowledge = load_declared_knowledge(Path(apps[app_id].root), apps[app_id].chat_knowledge)
            except InvalidKnowledgePackError:
                pass
        runtime = ChatRuntime(_registry, _config, client, readiness=lambda: ready, knowledge=knowledge)
        try:
            async for event in runtime.stream(app_id, apps[app_id].chat_system_prompt or "You are a helpful tutor.", messages):
                # Preserve the original launcher contract while adding typed error codes.
                if event["type"] == "complete":
                    event = {"type": "done"}
                elif event["type"] == "error":
                    event["error"] = event.get("message", "Local AI failed")
                yield f"data: {json.dumps(event)}\n\n"
        finally:
            await runtime.close()

    return StreamingResponse(gen(), media_type="text/event-stream")


# ── Local AI setup and model management ──────────────────────────────────────

@app.get("/api/local-ai/status")
async def local_ai_status() -> JSONResponse:
    config = _config.load()
    capabilities = _probe.inspect(STATE_DIR, config.ollama_endpoint)
    recommendation = _registry.recommend(capabilities)
    status = await _ollama_manager.status()
    selected = _registry.get_profile(config.selected_profile)
    ready = status.running and selected.model_id in status.installed_model_ids
    return JSONResponse({
        "state": "ready" if ready else "setup_required",
        "selected_profile": config.selected_profile,
        "selected_model_id": selected.model_id,
        "recommendation": {
            "profile_id": recommendation.profile.profile_id,
            "model_id": recommendation.profile.model_id,
            "display_name": recommendation.profile.display_name,
            "reason": recommendation.reason,
            "warnings": recommendation.warnings,
            "download_bytes": recommendation.profile.expected_download_bytes,
        },
        "system": asdict(capabilities),
        "ollama": asdict(status),
        "managed_model_ids": config.managed_model_ids,
        "profiles": [asdict(profile) for profile in _registry.profiles],
        "privacy": "Everything stays on this computer.",
    })


@app.put("/api/local-ai/profile")
async def set_local_ai_profile(request: Request) -> JSONResponse:
    _deny_app_iframe(request)
    body = await request.json()
    try:
        profile = _registry.get_profile(str(body.get("profile_id", "")))
    except InvalidRegistryError as exc:
        raise HTTPException(400, str(exc))
    _config.set_profile(profile.profile_id)
    return JSONResponse({"ok": True, "profile_id": profile.profile_id, "model_id": profile.model_id})


@app.get("/api/local-ai/install-plan")
async def local_ai_install_plan() -> JSONResponse:
    try:
        return JSONResponse(asdict(_ollama_manager.installation_plan()))
    except OllamaOperationError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/local-ai/authorize")
async def authorize_local_ai_install(request: Request) -> JSONResponse:
    _deny_app_iframe(request)
    body = await request.json()
    try:
        token = _ollama_manager.authorize(str(body.get("plan_id", "")))
    except ConfirmationError as exc:
        raise HTTPException(400, str(exc))
    return JSONResponse({"token": token})


@app.post("/api/local-ai/install")
async def install_local_ai(request: Request) -> JSONResponse:
    _deny_app_iframe(request)
    body = await request.json()
    try:
        _ollama_manager.install(str(body.get("plan_id", "")), str(body.get("token", "")))
    except (ConfirmationError, OllamaOperationError) as exc:
        raise HTTPException(400, str(exc))
    return JSONResponse({"ok": True, "message": "Complete the official Ollama installer, then return here."})


@app.post("/api/local-ai/start")
async def start_local_ai(request: Request) -> JSONResponse:
    _deny_app_iframe(request)
    executable = shutil.which("ollama")
    if not executable:
        raise HTTPException(400, "Ollama is not installed")
    subprocess.Popen((executable, "serve"), close_fds=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return JSONResponse({"ok": True})


@app.post("/api/local-ai/pull/{model_id}")
async def pull_local_ai_model(model_id: str, request: Request) -> StreamingResponse:
    _deny_app_iframe(request)
    try:
        _registry.get_model(model_id)
    except InvalidRegistryError as exc:
        raise HTTPException(404, str(exc))

    async def events():
        try:
            async for progress in _ollama_manager.pull(model_id):
                yield f"data: {json.dumps(asdict(progress))}\n\n"
            yield f"data: {json.dumps({'status': 'complete', 'percent': 100})}\n\n"
        except (OllamaOperationError, httpx.HTTPError) as exc:
            yield f"data: {json.dumps({'status': 'error', 'message': str(exc)})}\n\n"
    return StreamingResponse(events(), media_type="text/event-stream")


@app.delete("/api/local-ai/models/{model_id}")
async def remove_local_ai_model(model_id: str, request: Request) -> JSONResponse:
    _deny_app_iframe(request)
    try:
        await _ollama_manager.remove_managed_model(model_id)
    except (UnmanagedModelError, InvalidRegistryError) as exc:
        raise HTTPException(400, str(exc))
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Ollama could not remove the model: {exc}")
    return JSONResponse({"ok": True})


@app.post("/api/local-ai/verify/{model_id}")
async def verify_local_ai_model(model_id: str, request: Request) -> JSONResponse:
    _deny_app_iframe(request)
    try:
        return JSONResponse(asdict(await _ollama_manager.verify(model_id)))
    except InvalidRegistryError as exc:
        raise HTTPException(404, str(exc))
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Local model verification failed: {exc}")


@app.get("/api/local-ai/diagnostics")
async def local_ai_diagnostics() -> JSONResponse:
    config = _config.load()
    status = await _ollama_manager.status()
    return JSONResponse({
        "app_engine": "local-ai-v1",
        "platform": _os_name,
        "architecture": platform.machine(),
        "selected_profile": config.selected_profile,
        "endpoint": config.ollama_endpoint,
        "ollama_running": status.running,
        "ollama_version": status.version,
        "managed_model_ids": config.managed_model_ids,
    })


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


@app.api_route("/apps/{filename}", methods=["GET", "HEAD"])
async def shared_file(filename: str, request: Request) -> Response:
    """Serve shared root-level files (e.g. app-state-bridge.js) referenced as
    ``../file.js`` from inside an app iframe."""
    if "." not in filename or "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(404)
    candidate = (APPS_DIR / filename).resolve()
    try:
        candidate.relative_to(APPS_DIR)
    except ValueError:
        raise HTTPException(404)
    if candidate.is_file():
        return FileResponse(candidate, media_type=asset_media_type(candidate),
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


@app.api_route("/apps/{app_id}/", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"])
@app.api_route("/apps/{app_id}/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"])
async def serve_app(app_id: str, request: Request, path: str = "") -> Response:
    apps = discover()
    app_obj = apps.get(app_id)
    if not app_obj:
        raise HTTPException(404)
    if _app_host_id(request) != app_id:
        raise HTTPException(403, "app must be opened on its isolated origin")

    # entry_point apps: reverse-proxy everything to their own backend.
    if app_obj.entry_point:
        return await _proxy_entry_point(app_obj, path, request)

    safe = Path(path) if path else Path("index.html")
    if ".." in safe.parts or safe.is_absolute():
        raise HTTPException(404)
    root = Path(app_obj.root).resolve()
    full = (root / safe).resolve()
    try:
        full.relative_to(root)
    except ValueError:
        raise HTTPException(404)
    if full.is_dir():
        full = full / "index.html"
    if not full.is_file():
        raise HTTPException(404)
    return FileResponse(full, media_type=asset_media_type(full),
                    headers=_SEC_HEADERS)


# ── Launcher UI ───────────────────────────────────────────────────────────────

def _app_listing() -> list[dict]:
    """Discovered apps merged with managed catalog targets, without URLs."""
    listing = {a.id: asdict(a) for a in discover().values()}
    for item in _app_runtime.catalog.snapshot(_app_runtime.catalog_key).apps:
        manifest = item.manifest
        target = next(t for t in manifest.targets if t.target_id == manifest.default_target)
        if target.runtime is None:
            continue
        listing[str(manifest.app_id)] = {
            **listing.get(str(manifest.app_id), {}),
            "id": str(manifest.app_id), "label": manifest.label, "icon": manifest.icon,
            "version": manifest.metadata.version, "description": manifest.metadata.description,
            "categories": list(manifest.metadata.categories), "author": manifest.metadata.author,
            "sandbox": manifest.browser.sandbox, "allow": manifest.browser.allow,
            "permissions": list(manifest.browser.permissions), "compatible": item.compatible,
            "engine_managed": True,
        }
    apps = list(listing.values())
    for a in apps:
        a.pop("root", None)
    return apps


@app.get("/api/apps")
async def api_apps(
    request: Request, q: str = "", category: list[str] = Query(default=[]),
) -> JSONResponse:
    """Launcher app list. ``q`` and ``category`` filter it and rank by relevance."""
    if not _is_launcher_host(request):
        raise HTTPException(403, "launcher origin required")
    apps = search_apps(_app_listing(), q, category)
    for a in apps:
        port = request.url.port
        authority = f"{a['id']}.localhost" + (f":{port}" if port else "")
        a["url"] = f"{request.url.scheme}://{authority}/apps/{a['id']}/#atrium_state_token={_app_capability(a['id'])}"
    return JSONResponse(apps)


@app.get("/api/apps/categories")
async def api_app_categories(request: Request) -> JSONResponse:
    """Category vocabulary with per-category app counts, for the filter control."""
    if not _is_launcher_host(request):
        raise HTTPException(403, "launcher origin required")
    return JSONResponse(category_counts(_app_listing()))


@app.get("/api/engine")
async def api_engine() -> JSONResponse:
    """Engine identity + version, so apps and the launcher can gate on it."""
    return JSONResponse({"name": "app-engine", "version": ENGINE_VERSION})


@app.get("/api/apps/rejected")
async def api_apps_rejected() -> JSONResponse:
    """Folders that look like an app but failed manifest validation.

    Surfaces malformed manifests for developers instead of silently dropping
    them. Empty in normal operation.
    """
    accepted = {item.manifest.root.resolve() for item in
                _app_runtime.catalog.snapshot(_app_runtime.catalog_key).apps}
    return JSONResponse([item for item in inspect_apps()[1]
                         if (APPS_DIR / item["dir"]).resolve() not in accepted])


@app.get("/", response_class=HTMLResponse)
async def launcher(request: Request) -> HTMLResponse:
    if not _is_launcher_host(request):
        raise HTTPException(403, "launcher origin required")
    html = (Path(__file__).parent / "launcher.html").read_text(encoding="utf-8")
    html = html.replace("__APP_ENGINE_ADMIN_CAPABILITY__", _admin_capability)
    return HTMLResponse(html, headers={"Content-Security-Policy": "frame-ancestors 'none'"})


if __name__ == "__main__":
    print(f"app-engine -> apps from {APPS_DIR}")
    print(f"            state in {STATE_DIR}")
    print(f"            Local AI at {_config.load().ollama_endpoint} ({_config.load().selected_profile})")
    uvicorn.run(
        app,
        host=os.environ.get("APP_ENGINE_HOST", "127.0.0.1"),
        port=int(os.environ.get("APP_ENGINE_PORT", "8770")),
    )
