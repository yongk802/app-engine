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
from urllib.parse import parse_qs, urlparse

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse

from app_engine.chat import ChatRuntime
from app_engine.media_types import asset_media_type
from app_engine.config import ConfigStore
from app_engine.grounding import InvalidKnowledgePackError, KnowledgeBase, load_declared_knowledge
from app_engine import manifest as _manifest
from app_engine.manifest import ENGINE_VERSION
from app_engine.multiplayer_host import MAX_BODY as MULTIPLAYER_MAX_BODY, MultiplayerHost, MultiplayerUnavailable, failure as multiplayer_failure
from app_engine.public_origin import (
    JOIN_ERROR_PAGE,
    LOCAL_SESSION,
    Audit,
    SESSION_COOKIE,
    Accounts,
    OwnerGate,
    PublicOrigin,
    RemoteApiCors,
    Session,
    SignInLimiter,
    bind_is_allowed,
    clear_session_cookie,
    invite_link,
    join_page,
    player_sign_in_page,
    set_session_cookie,
    sign_in_page,
    startup_warnings,
)
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
_app_capabilities: dict[tuple[str, str], str] = {}   # (subject, app) -> capability
_admin_capability = secrets.token_urlsafe(32)

_proxy: httpx.AsyncClient | None = None

# Public-origin mode: apps at <id>.<public host>, owner sign-in required on every
# request, TLS terminated by a loopback proxy. Absent, this is the loopback product.
try:
    _PUBLIC = PublicOrigin.parse(os.environ.get("APP_ENGINE_PUBLIC_ORIGIN"), os.environ.get("APP_ENGINE_APP_ORIGINS"))
except ValueError as _exc:
    raise SystemExit(f"app-engine: {_exc}") from _exc
_TRUST_PROXY = os.environ.get("APP_ENGINE_TRUST_PROXY", "") in {"1", "true", "yes"}
_accounts = Accounts.load(STATE_DIR) if _PUBLIC else None
_audit = Audit(STATE_DIR) if _PUBLIC else None
_minted_admin_secret = _accounts.ensure_admin_secret(os.environ.get("APP_ENGINE_ADMIN_SECRET")) if _accounts else None
_sign_in_limiter = SignInLimiter()

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
    if _PUBLIC:
        where = f"{_PUBLIC.origin}/apps/<id>/" if _PUBLIC.layout == "same" else f"<id>.{_PUBLIC.host}"
        # flush=True: under systemd stdout is a pipe, and the one-time secret must reach the journal now.
        print(f"app-engine -> public origin {_PUBLIC.origin}; apps at {where}; owner sign-in at {_PUBLIC.origin}/admin", flush=True)
        if _minted_admin_secret:
            print(f"            admin secret (shown once, kept only as a hash): {_minted_admin_secret}", flush=True)
        for note in startup_warnings(os.environ.get("APP_ENGINE_HOST", "127.0.0.1"), _PUBLIC, _TRUST_PROXY):
            print(f"            warning: {note}", flush=True)
        _audit.record("engine.start", origin=_PUBLIC.origin, layout=_PUBLIC.layout)
    await _app_runtime.start()
    yield
    await _app_runtime.close(5.0)
    if _proxy and not _proxy.is_closed:
        await _proxy.aclose()
    await _ollama_manager.close()
    await _multiplayer.close()


app = FastAPI(title="app-engine", lifespan=lifespan)
if _PUBLIC:
    app.add_middleware(OwnerGate, accounts=_accounts, enabled=True)
    app.add_middleware(RemoteApiCors)


def _session(request: Request) -> Session:
    """Who is asking: the gate's session in public-origin mode, the local owner otherwise."""
    if not _PUBLIC:
        return LOCAL_SESSION
    session = request.scope.get("state", {}).get("session")
    if session is None:
        raise HTTPException(401, "sign in required")
    return session


# Players may open apps they were granted and use their sessions; every other host route is the owner's.
_PLAYER_ROUTE = re.compile(r"^/api/app-engine/(?:apps/(?P<app>[a-z0-9][a-z0-9-]*)/open|sessions/[^/]+(?:/.*)?)$")


async def _runtime_authenticate(request: Request) -> HostSubject:
    """Gate the host router by session, then hand the runtime its one subject.

    The runtime's host adapter is single-subject (configuration, state and
    approvals belong to this installation, not to a person), so the owner and
    every player act as that subject once the session and allow-list checks pass.
    """
    session = _session(request)
    if session.role == "player":
        match = _PLAYER_ROUTE.match(request.url.path)
        if not match or (match.group("app") and not session.may_open(match.group("app"))):
            raise HTTPException(404)
    return _runtime_subject


app.include_router(
    create_app_engine_router(
        _app_runtime,
        authenticate=_runtime_authenticate,
        standing_approval=(lambda fingerprint: _accounts.plan_approved(fingerprint)) if _accounts else None,
        record_approval=(lambda fingerprint: (_accounts.approve_plan(fingerprint), _audit.record("plan.approved", fingerprint=fingerprint))) if _accounts else None,
    )
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
    multiplayer: dict | None = None  # {rules, ai, protocol} when the app plays through the host


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
            multiplayer=n["multiplayer"],
        )
    return out, rejected


def discover() -> dict[str, App]:
    """Scan APPS_DIR's immediate children for valid apps (app.json)."""
    return inspect_apps()[0]


# ── App-state persistence (localStorage replacement) ──────────────────────────

def _state_file(session: Session, app_id: str) -> Path:
    """The owner's saves stay where they always were; each player has their own folder."""
    if not _APP_ID_RE.match(app_id):
        raise HTTPException(404)
    if session.role == "player":
        return STATE_DIR / "players" / session.subject_id / f"{app_id}.json"
    return STATE_DIR / f"{app_id}.json"


def _hostname(request: Request) -> str:
    return (request.url.hostname or "").lower().rstrip(".")


def _app_host_id(request: Request) -> str | None:
    host = _hostname(request)
    # The public authority first: a rehearsal origin such as play.localhost also ends in .localhost.
    candidate = _PUBLIC.app_id_for(host) if _PUBLIC and _PUBLIC.layout == "subdomain" else None
    if candidate is None and host.endswith(".localhost"):
        candidate = host[:-len(".localhost")]
    if candidate is None:
        return None
    return candidate if _APP_ID_RE.fullmatch(candidate) else None


def _served_on_app_origin(request: Request, app_id: str) -> bool:
    """Is this request coming from where the app is allowed to live?"""
    if _PUBLIC and _PUBLIC.layout == "same" and _hostname(request) == _PUBLIC.hostname:
        return True
    return _app_host_id(request) == app_id


def _is_launcher_host(request: Request) -> bool:
    host = _hostname(request)
    return host in {"127.0.0.1", "localhost", "::1", "testserver"} or bool(_PUBLIC and host == _PUBLIC.hostname)


def _app_origin(request: Request, app_id: str) -> str:
    """Where the launcher must open the app: its public authority, or ``<id>.localhost``."""
    if _PUBLIC:
        return _PUBLIC.app_origin(app_id)
    port = request.url.port
    return f"{request.url.scheme}://{app_id}.localhost" + (f":{port}" if port else "")


def _app_capability(session: Session | str, app_id: str | None = None) -> str:
    """The unguessable capability for (subject, app). ``_app_capability("notes")`` is the local owner's."""
    if isinstance(session, str):
        session, app_id = LOCAL_SESSION, session
    return _app_capabilities.setdefault((session.subject_id, app_id), secrets.token_urlsafe(32))


def _require_app_access(session: Session, app_id: str) -> None:
    """An app a player was not invited to does not exist for them."""
    if not session.may_open(app_id):
        raise HTTPException(404)


def _enforce_app_state_access(request: Request, app_id: str) -> Session:
    session = _session(request)
    _require_app_access(session, app_id)
    supplied = request.headers.get("x-app-state-token", "")
    expected = _app_capability(session, app_id)
    if not _served_on_app_origin(request, app_id) or not secrets.compare_digest(supplied, expected):
        raise HTTPException(403, "app-state capability denied")
    return session


def _deny_app_iframe(request: Request) -> None:
    """Require the launcher-only capability for local-AI mutations."""
    _deny_public_local_ai()
    supplied = request.headers.get("x-app-engine-admin", "")
    if not _is_launcher_host(request) or not secrets.compare_digest(supplied, _admin_capability):
        raise HTTPException(403, "launcher capability required")


def _deny_public_local_ai() -> None:
    """Local AI never leaves the owner's own computer: a public engine has none."""
    if _PUBLIC:
        raise HTTPException(403, "Local AI is not available in public-origin mode")


@app.get("/api/app-state/{app_id}")
async def get_app_state(app_id: str, request: Request) -> JSONResponse:
    session = _enforce_app_state_access(request, app_id)
    f = _state_file(session, app_id)
    if not f.is_file():
        return JSONResponse({})
    try:
        return JSONResponse(json.loads(f.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, UnicodeError, OSError):
        return JSONResponse({})


@app.put("/api/app-state/{app_id}")
async def put_app_state(app_id: str, request: Request) -> JSONResponse:
    session = _enforce_app_state_access(request, app_id)
    raw = await request.body()
    if len(raw) > MAX_STATE_BYTES:
        raise HTTPException(413, "state too large")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(400, "invalid JSON")
    if not isinstance(data, (dict, list)):
        raise HTTPException(400, "state must be object or array")
    target = _state_file(session, app_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data), encoding="utf-8")
    return JSONResponse({"ok": True})


# ── Multiplayer (host-run room service per app) ───────────────────────────────

_multiplayer = MultiplayerHost(STATE_DIR)


def _multiplayer_reply(status: int, value: dict) -> JSONResponse:
    return JSONResponse(value, status_code=status, headers={"Cache-Control": "no-store"})


def _multiplayer_app(app_id: str) -> App | None:
    app_obj = discover().get(app_id)
    return app_obj if app_obj and app_obj.multiplayer else None


async def _multiplayer_base(request: Request, app_id: str) -> str | JSONResponse:
    """The app's room service URL, or the JSON error the client should show."""
    _enforce_app_state_access(request, app_id)
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(403, "cross-site multiplayer requests are refused")
    app_obj = _multiplayer_app(app_id)
    if app_obj is None:
        return _multiplayer_reply(503, multiplayer_failure("MULTIPLAYER_UNAVAILABLE", "This app does not play through the host."))
    try:
        return await _multiplayer.ensure(app_id, Path(app_obj.root), app_obj.multiplayer)
    except MultiplayerUnavailable as exc:
        return _multiplayer_reply(503, multiplayer_failure("MULTIPLAYER_UNAVAILABLE", exc.message))


@app.post("/api/app-multiplayer/{app_id}/command")
async def app_multiplayer_command(app_id: str, request: Request) -> JSONResponse:
    """Relay one room command from the app to its room service.

    Same origin and the app-state capability gate it like ``/api/app-state``; the
    seat, tournament or friend credential rides in ``X-App-Multiplayer-Credential``
    and becomes the service's bearer token. Bodies are bounded and must be JSON
    commands (``{"op": ..., ...}``); the service validates everything else.
    """
    if not re.match(r"^application/json(?:\s*;|$)", request.headers.get("content-type", ""), re.I):
        return _multiplayer_reply(415, multiplayer_failure("INVALID_REQUEST", "Commands require application/json."))
    raw = await request.body()
    if len(raw) > MULTIPLAYER_MAX_BODY:
        return _multiplayer_reply(413, multiplayer_failure("INVALID_REQUEST", "Request is too large."))
    try:
        body = json.loads(raw)
    except (json.JSONDecodeError, UnicodeError):
        body = None
    if not isinstance(body, dict) or not isinstance(body.get("op"), str):
        return _multiplayer_reply(400, multiplayer_failure("INVALID_REQUEST", "A command operation is required."))
    base = await _multiplayer_base(request, app_id)
    if isinstance(base, JSONResponse):
        return base
    session = _session(request)
    status, result = await _multiplayer.relay(base, raw, request.headers.get("x-app-multiplayer-credential"), player=session.subject_id if session.role == "player" else None)
    return _multiplayer_reply(status, result)


@app.get("/api/app-multiplayer/{app_id}/health")
async def app_multiplayer_health(app_id: str, request: Request) -> JSONResponse:
    base = await _multiplayer_base(request, app_id)
    if isinstance(base, JSONResponse):
        return base
    status, result = await _multiplayer.health(base)
    return _multiplayer_reply(status, result)


# ── /state stub (single-user, no auth) ────────────────────────────────────────

@app.get("/state")
async def state(request: Request) -> JSONResponse:
    session = _session(request)
    return JSONResponse(
        {"csrf_token": "", "session": {"username": session.name, "role": session.role}}
    )


# ── Chat proxy (OpenAI-compatible; Ollama by default) ─────────────────────────

@app.post("/api/app-chat")
async def app_chat(request: Request):
    _deny_public_local_ai()
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
    _deny_public_local_ai()
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
    _deny_public_local_ai()
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
    # Apps are framed by the launcher; in public mode only by the public launcher.
    "Content-Security-Policy": f"frame-ancestors {_PUBLIC.origin}" if _PUBLIC else "frame-ancestors *",
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
    _require_app_access(_session(request), app_id)
    apps = discover()
    app_obj = apps.get(app_id)
    if not app_obj:
        raise HTTPException(404)
    if not _served_on_app_origin(request, app_id):
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
    session = _session(request)
    apps = [a for a in search_apps(_app_listing(), q, category) if session.may_open(a["id"])]
    for a in apps:
        a["url"] = f"{_app_origin(request, a['id'])}/apps/{a['id']}/#atrium_state_token={_app_capability(session, a['id'])}"
        if _PUBLIC:
            a["chat_enabled"] = False   # Local AI is not served publicly
    return JSONResponse(apps)


@app.get("/api/apps/categories")
async def api_app_categories(request: Request) -> JSONResponse:
    """Category vocabulary with per-category app counts, for the filter control."""
    if not _is_launcher_host(request):
        raise HTTPException(403, "launcher origin required")
    session = _session(request)
    return JSONResponse(category_counts([a for a in _app_listing() if session.may_open(a["id"])]))


@app.get("/api/engine")
async def api_engine() -> JSONResponse:
    """Engine identity + version, so apps and the launcher can gate on it."""
    return JSONResponse({"name": "app-engine", "version": ENGINE_VERSION})


@app.get("/api/apps/rejected")
async def api_apps_rejected(request: Request) -> JSONResponse:
    """Folders that look like an app but failed manifest validation.

    Surfaces malformed manifests for developers instead of silently dropping
    them. Empty in normal operation.
    """
    _require_owner(request)
    accepted = {item.manifest.root.resolve() for item in
                _app_runtime.catalog.snapshot(_app_runtime.catalog_key).apps}
    return JSONResponse([item for item in inspect_apps()[1]
                         if (APPS_DIR / item["dir"]).resolve() not in accepted])


@app.get("/", response_class=HTMLResponse)
async def launcher(request: Request) -> HTMLResponse:
    if not _is_launcher_host(request):
        raise HTTPException(403, "launcher origin required")
    session = _session(request)
    html = (Path(__file__).parent / "launcher.html").read_text(encoding="utf-8")
    # A player's page never carries the owner's capability: it cannot approve, install or configure.
    html = html.replace("__APP_ENGINE_ADMIN_CAPABILITY__", _admin_capability if session.owner else "")
    html = html.replace("__APP_ENGINE_ROLE__", "player" if session.role == "player" else ("owner" if _PUBLIC else "local"))
    html = html.replace("__APP_ENGINE_USER__", session.name.replace("<", "").replace(">", "").replace("'", ""))
    return HTMLResponse(html, headers={"Content-Security-Policy": "frame-ancestors 'none'", "Cache-Control": "no-store"})


# ── Owner sign-in and players (public-origin mode only) ───────────────────────

def _require_public() -> None:
    if not _PUBLIC:
        raise HTTPException(404)


def _require_owner(request: Request) -> Session:
    session = _session(request)
    if not session.owner:
        raise HTTPException(404)
    return session


def _require_owner_action(request: Request) -> Session:
    """A mutation from the owner's launcher: session cookie plus the launcher capability."""
    session = _require_owner(request)
    supplied = request.headers.get("x-app-engine-admin", "")
    if not _is_launcher_host(request) or not secrets.compare_digest(supplied, _admin_capability):
        raise HTTPException(403, "launcher capability required")
    return session


_NO_STORE = {"Cache-Control": "no-store", "Content-Security-Policy": "frame-ancestors 'none'"}


@app.get("/join/{code}")
async def join(code: str, request: Request) -> Response:
    """A one-time invitation link: the player chooses a password, then is signed in here."""
    _require_public()
    player = _accounts.invited_player(code)
    if player is None:
        return HTMLResponse(JOIN_ERROR_PAGE, status_code=404, headers=_NO_STORE)
    return HTMLResponse(join_page(code, player["username"]), headers=_NO_STORE)


@app.post("/join/{code}")
async def join_submit(code: str, request: Request) -> Response:
    _require_public()
    player = _accounts.invited_player(code)
    if player is None:
        return HTMLResponse(JOIN_ERROR_PAGE, status_code=404, headers=_NO_STORE)
    form = parse_qs((await request.body()).decode("utf-8", "replace"), keep_blank_values=True)
    password, confirm = (form.get("password") or [""])[0], (form.get("confirm") or [""])[0]
    if password != confirm:
        return HTMLResponse(join_page(code, player["username"], "The two passwords differ."), status_code=400, headers=_NO_STORE)
    try:
        redeemed = _accounts.redeem_invite(code, password)
    except ValueError as exc:
        return HTMLResponse(join_page(code, player["username"], str(exc)), status_code=400, headers=_NO_STORE)
    if redeemed is None:
        return HTMLResponse(JOIN_ERROR_PAGE, status_code=404, headers=_NO_STORE)
    session, token = redeemed
    _audit.record("player.joined", player=session.subject_id, username=session.username, address=request.client.host if request.client else "")
    response = RedirectResponse("/", status_code=303)
    set_session_cookie(response, token, _PUBLIC)
    return response


@app.get("/sign-in", response_class=HTMLResponse)
async def player_sign_in(request: Request) -> Response:
    _require_public()
    if request.scope.get("state", {}).get("session"):
        return RedirectResponse("/", status_code=303)
    return HTMLResponse(player_sign_in_page(), headers=_NO_STORE)


@app.post("/sign-in")
async def player_sign_in_submit(request: Request) -> Response:
    _require_public()
    address = request.client.host if request.client else ""
    if not _sign_in_limiter.allow(address):
        return HTMLResponse(player_sign_in_page("Too many attempts. Wait a minute."), status_code=429, headers=_NO_STORE)
    form = parse_qs((await request.body()).decode("utf-8", "replace"), keep_blank_values=True)
    username = (form.get("username") or [""])[0]
    signed = _accounts.sign_in_player(username, (form.get("password") or [""])[0])
    if signed is None:
        _audit.record("player.sign_in.failed", username=username[:40], address=address, via="browser")
        return HTMLResponse(player_sign_in_page("That username or password is not right."), status_code=403, headers=_NO_STORE)
    _audit.record("player.sign_in", player=signed[0].subject_id, username=signed[0].username, address=address, via="browser")
    response = RedirectResponse("/", status_code=303)
    set_session_cookie(response, signed[1], _PUBLIC)
    return response


@app.post("/sign-out")
async def player_sign_out(request: Request) -> Response:
    _require_public()
    _accounts.revoke_session(request.cookies.get(SESSION_COOKIE))
    response = RedirectResponse("/sign-in", status_code=303)
    clear_session_cookie(response, _PUBLIC)
    return response


# ── Remote player API: game clients on other machines, bearer tokens, CORS-open ──

def _bearer_session(request: Request) -> Session:
    """The player behind ``Authorization: Bearer``; never a cookie, so a foreign page cannot ride along."""
    _require_public()
    header = request.headers.get("authorization", "")
    token = header[7:].strip() if header.lower().startswith("bearer ") else ""
    session = _accounts.resolve(token) if token else None
    if session is None:
        raise HTTPException(401, "sign in with a username and password")
    return session


def _player_json(session: Session) -> dict:
    return {"id": session.subject_id, "username": session.username, "name": session.name, "apps": list(session.apps or [])}


@app.post("/api/players/sign-in")
async def api_player_sign_in(request: Request) -> JSONResponse:
    """Username + password to a bearer token. The standard way a game connects to a server."""
    _require_public()
    address = request.client.host if request.client else ""
    if not _sign_in_limiter.allow(address):
        return JSONResponse({"ok": False, "error": {"code": "RATE_LIMITED", "message": "Too many attempts. Wait a minute."}}, status_code=429)
    try:
        body = await request.json()
    except Exception:
        body = None
    if not isinstance(body, dict):
        return JSONResponse({"ok": False, "error": {"code": "INVALID_REQUEST", "message": "Send a JSON object with username and password."}}, status_code=400)
    signed = _accounts.sign_in_player(str(body.get("username", "")), body.get("password", ""))
    if signed is None:
        _audit.record("player.sign_in.failed", username=str(body.get("username", ""))[:40], address=address, via="api")
        return JSONResponse({"ok": False, "error": {"code": "AUTH_REQUIRED", "message": "That username or password is not right."}}, status_code=403)
    session, token = signed
    _audit.record("player.sign_in", player=session.subject_id, username=session.username, address=address, via="api")
    return JSONResponse({"ok": True, "token": token, "player": _player_json(session), "server": {"name": "app-engine", "origin": _PUBLIC.origin, "version": ENGINE_VERSION}})


@app.get("/api/players/me")
async def api_player_me(request: Request) -> JSONResponse:
    session = _bearer_session(request)
    return JSONResponse({"ok": True, "player": _player_json(session)})


@app.post("/api/players/sign-out")
async def api_player_sign_out(request: Request) -> JSONResponse:
    _bearer_session(request)
    _accounts.revoke_session(request.headers.get("authorization", "")[7:].strip())
    return JSONResponse({"ok": True})


async def _remote_multiplayer_base(request: Request, app_id: str) -> str | JSONResponse:
    session = _bearer_session(request)
    if not session.may_open(app_id):
        return _multiplayer_reply(404, multiplayer_failure("MULTIPLAYER_UNAVAILABLE", "You are not signed up for this game on this server."))
    app_obj = _multiplayer_app(app_id)
    if app_obj is None:
        return _multiplayer_reply(503, multiplayer_failure("MULTIPLAYER_UNAVAILABLE", "This server does not host that game."))
    try:
        return await _multiplayer.ensure(app_id, Path(app_obj.root), app_obj.multiplayer)
    except MultiplayerUnavailable as exc:
        return _multiplayer_reply(503, multiplayer_failure("MULTIPLAYER_UNAVAILABLE", exc.message))


@app.post("/api/players/{app_id}/command")
async def api_player_command(app_id: str, request: Request) -> JSONResponse:
    """A room command from a signed-in player's own game client, anywhere on the internet."""
    if not _APP_ID_RE.match(app_id):
        raise HTTPException(404)
    raw = await request.body()
    if len(raw) > MULTIPLAYER_MAX_BODY:
        return _multiplayer_reply(413, multiplayer_failure("INVALID_REQUEST", "Request is too large."))
    try:
        body = json.loads(raw)
    except (json.JSONDecodeError, UnicodeError):
        body = None
    if not isinstance(body, dict) or not isinstance(body.get("op"), str):
        return _multiplayer_reply(400, multiplayer_failure("INVALID_REQUEST", "A command operation is required."))
    base = await _remote_multiplayer_base(request, app_id)
    if isinstance(base, JSONResponse):
        return base
    session = _bearer_session(request)
    status, result = await _multiplayer.relay(base, raw, request.headers.get("x-app-multiplayer-credential"), player=session.subject_id)
    return _multiplayer_reply(status, result)


@app.get("/api/players/{app_id}/health")
async def api_player_health(app_id: str, request: Request) -> JSONResponse:
    if not _APP_ID_RE.match(app_id):
        raise HTTPException(404)
    base = await _remote_multiplayer_base(request, app_id)
    if isinstance(base, JSONResponse):
        return base
    status, result = await _multiplayer.health(base)
    return _multiplayer_reply(status, result)


@app.get("/admin/players")
async def admin_players(request: Request) -> JSONResponse:
    _require_public()
    _require_owner(request)
    return JSONResponse({"players": _accounts.players(), "apps": sorted(discover())})


@app.post("/admin/players")
async def admin_players_create(request: Request) -> JSONResponse:
    _require_public()
    _require_owner_action(request)
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("expected an object")
        unknown = [a for a in body.get("apps", []) if isinstance(a, str) and a not in discover()]
        if unknown:
            raise ValueError(f"unknown app: {', '.join(unknown)}")
        player, code = _accounts.create_player(str(body.get("username", "")), body.get("apps", []), body.get("name"))
    except (ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(400, str(exc)) from exc
    _audit.record("player.invited", player=player["id"], username=player["username"], apps=player["apps"])
    return JSONResponse({"player": player, "invite": invite_link(_PUBLIC, code)}, status_code=201)


@app.post("/admin/players/{player_id}/invite")
async def admin_players_invite(player_id: str, request: Request) -> JSONResponse:
    _require_public()
    _require_owner_action(request)
    code = _accounts.regenerate_invite(player_id)
    if code is None:
        raise HTTPException(404, "player not found")
    _audit.record("player.reinvited", player=player_id)
    return JSONResponse({"invite": invite_link(_PUBLIC, code)})


@app.post("/admin/players/{player_id}")
async def admin_players_update(player_id: str, request: Request) -> JSONResponse:
    _require_public()
    _require_owner_action(request)
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("expected an object")
        apps = body.get("apps")
        if apps is not None:
            unknown = [a for a in apps if isinstance(a, str) and a not in discover()]
            if unknown:
                raise ValueError(f"unknown app: {', '.join(unknown)}")
        player = _accounts.update_player(player_id, disabled=body.get("disabled"), apps=apps, name=body.get("name"))
    except (ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(400, str(exc)) from exc
    if player is None:
        raise HTTPException(404, "player not found")
    _audit.record("player.updated", player=player_id, disabled=player["disabled"], apps=player["apps"])
    return JSONResponse({"player": player})


@app.delete("/admin/players/{player_id}", status_code=204)
async def admin_players_delete(player_id: str, request: Request) -> Response:
    _require_public()
    _require_owner_action(request)
    if not _accounts.remove_player(player_id):
        raise HTTPException(404, "player not found")
    _audit.record("player.removed", player=player_id)
    return Response(status_code=204)


@app.get("/admin", response_class=HTMLResponse)
async def admin_sign_in(request: Request) -> Response:
    _require_public()
    if request.scope.get("state", {}).get("session"):
        return RedirectResponse("/", status_code=303)
    return HTMLResponse(sign_in_page(), headers={"Content-Security-Policy": "frame-ancestors 'none'", "Cache-Control": "no-store"})


@app.post("/admin/sign-in")
async def admin_sign_in_submit(request: Request) -> Response:
    _require_public()
    address = request.client.host if request.client else ""
    if not _sign_in_limiter.allow(address):
        return HTMLResponse(sign_in_page("Too many attempts. Wait a minute."), status_code=429)
    form = parse_qs((await request.body()).decode("utf-8", "replace"), keep_blank_values=True)
    secret = (form.get("secret") or [""])[0]
    if not _accounts.verify_admin_secret(secret):
        _audit.record("owner.sign_in.failed", address=address)
        return HTMLResponse(sign_in_page("That secret is not right."), status_code=403)
    _audit.record("owner.sign_in", address=address)
    response = RedirectResponse("/", status_code=303)
    set_session_cookie(response, _accounts.create_admin_session(), _PUBLIC)
    return response


@app.post("/admin/sign-out")
async def admin_sign_out(request: Request) -> Response:
    _require_public()
    _accounts.revoke_session(request.cookies.get(SESSION_COOKIE))
    response = RedirectResponse("/admin", status_code=303)
    clear_session_cookie(response, _PUBLIC)
    return response


if __name__ == "__main__":
    bind_host = os.environ.get("APP_ENGINE_HOST", "127.0.0.1")
    refusal = bind_is_allowed(bind_host, _PUBLIC, _accounts)
    if refusal:
        raise SystemExit(f"app-engine: {refusal}")
    print(f"app-engine -> apps from {APPS_DIR}")
    print(f"            state in {STATE_DIR}")
    print(f"            Local AI at {_config.load().ollama_endpoint} ({_config.load().selected_profile})")
    uvicorn.run(
        app,
        host=bind_host,
        port=int(os.environ.get("APP_ENGINE_PORT", "8770")),
        # A loopback reverse proxy may report the real scheme and client; nobody else.
        proxy_headers=_TRUST_PROXY,
        forwarded_allow_ips="127.0.0.1,::1" if _TRUST_PROXY else None,
    )
