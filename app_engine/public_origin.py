"""Public-origin mode: one app-engine behind a TLS proxy, reachable by its owner from anywhere.

Off unless ``APP_ENGINE_PUBLIC_ORIGIN`` names the launcher's public origin (for
example ``https://play.example.com``). Then:

* apps live at ``https://<id>.<public host>`` next to the familiar ``<id>.localhost``;
* every request must carry the owner's session cookie, obtained by signing in at
  ``/admin`` with the admin secret (``APP_ENGINE_ADMIN_SECRET``, or one generated and
  printed once on first start and kept only as a hash);
* proxy headers are honoured only when ``APP_ENGINE_TRUST_PROXY=1`` and the peer is
  loopback, so a reverse proxy on the same machine can terminate TLS.

Without the variable the engine is the single-user loopback product it always was.
Binding a non-loopback address without this mode is refused at startup, because
every host route would otherwise treat any visitor as the administrator.

See docs/specs/2026-09-13-public-origin-mode-design.md.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

SESSION_COOKIE = "app_engine_session"
SESSION_LIFETIME = 90 * 86400
SIGN_IN_ATTEMPTS = 5          # per address per minute
_LOOPBACK = {"127.0.0.1", "::1", "localhost", "testclient"}


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _same(value: str, expected_hash: str) -> bool:
    return hmac.compare_digest(_digest(value), expected_hash)


@dataclass(frozen=True)
class PublicOrigin:
    """The launcher's public origin and what follows from it."""

    scheme: str
    host: str          # e.g. play.example.com (no port unless non-default)
    hostname: str      # host without port, lower-case

    @classmethod
    def parse(cls, value: str | None) -> PublicOrigin | None:
        raw = (value or "").strip()
        if not raw:
            return None
        parts = urlsplit(raw if "://" in raw else f"https://{raw}")
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.path not in {"", "/"} or parts.query or parts.fragment or parts.username:
            raise ValueError("APP_ENGINE_PUBLIC_ORIGIN must be an origin such as https://play.example.com")
        hostname = parts.hostname.lower().rstrip(".")
        # A name under .localhost is allowed for rehearsing the mode on one machine
        # (browsers resolve it to loopback); bare loopback addresses are not a public origin.
        if hostname in _LOOPBACK:
            raise ValueError("APP_ENGINE_PUBLIC_ORIGIN must be a public host name, not a loopback address")
        port = parts.port
        default = 443 if parts.scheme == "https" else 80
        host = hostname if port in (None, default) else f"{hostname}:{port}"
        return cls(scheme=parts.scheme, host=host, hostname=hostname)

    @property
    def origin(self) -> str:
        return f"{self.scheme}://{self.host}"

    @property
    def secure(self) -> bool:
        return self.scheme == "https"

    def app_id_for(self, hostname: str) -> str | None:
        """``<id>.<public host>`` -> ``<id>``; anything else -> None."""
        suffix = "." + self.hostname
        if not hostname.endswith(suffix):
            return None
        candidate = hostname[: -len(suffix)]
        return candidate if candidate and "." not in candidate else None

    def app_authority(self, app_id: str) -> str:
        return f"{app_id}.{self.host}"

    def cookie_domain(self) -> str:
        # A leading dot is implied: the launcher and every app subdomain share the session.
        return self.hostname


def trusted_proxy(request: Request, enabled: bool) -> bool:
    """Only a loopback peer may speak for the real client."""
    client = request.client.host if request.client else ""
    return enabled and client in _LOOPBACK


@dataclass
class Accounts:
    """The owner's secret and sessions, hashed at rest in players.json beside the app state.

    Players (phase 3) will live in the same file; the schema already reserves the key.
    """

    path: Path
    data: dict

    @classmethod
    def load(cls, state_dir: Path) -> Accounts:
        path = Path(state_dir) / "players.json"
        data: dict = {"version": 1, "admin": {"secret_hash": None, "created": None, "sessions": []}, "players": []}
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(saved, dict) and saved.get("version") == 1 and isinstance(saved.get("admin"), dict):
                data = saved
                data["admin"].setdefault("sessions", [])
                data.setdefault("players", [])
        except (FileNotFoundError, json.JSONDecodeError, UnicodeError, OSError):
            pass
        return cls(path=path, data=data)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".players-{secrets.token_hex(8)}.tmp")
        with open(temporary, "w", encoding="utf-8", opener=lambda p, f: os.open(p, f, 0o600)) as handle:
            json.dump(self.data, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.path)

    # ── admin secret ───────────────────────────────────────────────────────

    @property
    def has_admin_secret(self) -> bool:
        return bool(self.data["admin"].get("secret_hash"))

    def set_admin_secret(self, secret: str) -> None:
        """Store only the hash; every owner session is revoked with a new secret."""
        self.data["admin"]["secret_hash"] = _digest(secret)
        self.data["admin"]["created"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.data["admin"]["sessions"] = []
        self.save()

    def ensure_admin_secret(self, configured: str | None) -> str | None:
        """Adopt the configured secret, or mint one if none exists. Returns a freshly minted
        secret exactly once so the operator can be told; otherwise None."""
        if configured:
            if not self.has_admin_secret or not _same(configured, self.data["admin"]["secret_hash"]):
                self.set_admin_secret(configured)
            return None
        if self.has_admin_secret:
            return None
        minted = secrets.token_urlsafe(24)
        self.set_admin_secret(minted)
        return minted

    def verify_admin_secret(self, candidate: str) -> bool:
        stored = self.data["admin"].get("secret_hash")
        return bool(stored) and isinstance(candidate, str) and len(candidate) <= 256 and _same(candidate, stored)

    # ── sessions ───────────────────────────────────────────────────────────

    def create_admin_session(self) -> str:
        token = secrets.token_urlsafe(32)
        now = int(time.time())
        sessions = [s for s in self.data["admin"]["sessions"] if now - s.get("last_seen", 0) < SESSION_LIFETIME]
        sessions.append({"hash": _digest(token), "created": now, "last_seen": now})
        self.data["admin"]["sessions"] = sessions[-20:]
        self.save()
        return token

    def resolve(self, token: str | None) -> str | None:
        """The subject id for a live session token, refreshing its sliding expiry."""
        if not token or not isinstance(token, str) or len(token) > 256:
            return None
        now = int(time.time())
        for session in self.data["admin"]["sessions"]:
            if hmac.compare_digest(_digest(token), session.get("hash", "")):
                if now - session.get("last_seen", 0) >= SESSION_LIFETIME:
                    return None
                if now - session.get("last_seen", 0) > 3600:
                    session["last_seen"] = now
                    self.save()
                return "admin"
        return None

    def revoke_session(self, token: str | None) -> None:
        if not token:
            return
        digest = _digest(token)
        self.data["admin"]["sessions"] = [s for s in self.data["admin"]["sessions"] if not hmac.compare_digest(digest, s.get("hash", ""))]
        self.save()


class SignInLimiter:
    """A small fixed window per client address; a wrong secret is cheap to try otherwise."""

    def __init__(self, attempts: int = SIGN_IN_ATTEMPTS, window: float = 60.0):
        self.attempts, self.window = attempts, window
        self._seen: dict[str, list[float]] = {}

    def allow(self, address: str) -> bool:
        now = time.monotonic()
        recent = [t for t in self._seen.get(address, []) if now - t < self.window]
        self._seen[address] = recent
        if len(recent) >= self.attempts:
            return False
        recent.append(now)
        return True


SIGN_IN_PAGE = """<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>app-engine · sign in</title>
<style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0d1117;color:#e6edf3;font:15px system-ui,sans-serif}
form{display:grid;gap:12px;width:min(360px,90vw);padding:28px;background:#161b22;border:1px solid #30363d;border-radius:10px}
h1{margin:0;font-size:20px}p{margin:0;color:#8b949e;font-size:13px}input{padding:10px;border-radius:6px;border:1px solid #30363d;background:#0d1117;color:inherit;font-size:15px}
button{padding:10px;border-radius:6px;border:0;background:#2f81f7;color:#fff;font-weight:600;cursor:pointer}.error{color:#ff7b72}</style>
<form method="post" action="/admin/sign-in"><h1>app-engine</h1><p>This engine is public. Sign in with the admin secret to open the launcher.</p>
__ERROR__<input type="password" name="secret" placeholder="Admin secret" autocomplete="current-password" autofocus required>
<button type="submit">Sign in</button></form>"""


def sign_in_page(error: str = "") -> str:
    return SIGN_IN_PAGE.replace("__ERROR__", f'<p class="error">{error}</p>' if error else "")


class OwnerGate:
    """ASGI middleware: in public-origin mode nothing but the sign-in surface is served
    without a live owner session. HTML navigations are redirected to /admin; everything
    else answers 401 JSON. The subject is stored on ``scope['state']`` for the routes."""

    OPEN_PATHS = {"/admin", "/admin/sign-in", "/api/engine"}

    def __init__(self, app: ASGIApp, accounts: Accounts, enabled: bool):
        self.app, self.accounts, self.enabled = app, accounts, enabled

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self.enabled:
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        subject = self.accounts.resolve(request.cookies.get(SESSION_COOKIE))
        scope.setdefault("state", {})
        scope["state"]["owner"] = subject
        if subject is None and request.url.path not in self.OPEN_PATHS:
            accepts_html = "text/html" in request.headers.get("accept", "") and request.method == "GET"
            response: Response = RedirectResponse("/admin", status_code=303) if accepts_html else JSONResponse({"detail": "sign in required"}, status_code=401)
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def set_session_cookie(response: Response, token: str, public: PublicOrigin) -> None:
    response.set_cookie(SESSION_COOKIE, token, max_age=SESSION_LIFETIME, httponly=True, secure=public.secure, samesite="lax", domain=public.cookie_domain(), path="/")


def clear_session_cookie(response: Response, public: PublicOrigin) -> None:
    response.delete_cookie(SESSION_COOKIE, domain=public.cookie_domain(), path="/")


def bind_is_allowed(host: str, public: PublicOrigin | None, accounts: Accounts | None) -> str | None:
    """Why a bind address is refused, or None when it is fine."""
    if host.strip().strip("[]").lower() in _LOOPBACK:
        return None
    if public is None:
        return f"APP_ENGINE_HOST={host} would make every visitor the administrator. Set APP_ENGINE_PUBLIC_ORIGIN and put a TLS proxy in front, or bind 127.0.0.1."
    if accounts is None or not accounts.has_admin_secret:
        return "Public-origin mode needs an admin secret before binding a public address."
    return None
