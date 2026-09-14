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
import re
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
INVITE_LIFETIME = 7 * 86400
SIGN_IN_ATTEMPTS = 5          # per address per minute
MAX_PLAYERS = int(os.environ.get("APP_ENGINE_MAX_PLAYERS", "50") or 50)
# APP_ENGINE_RATE_LIMIT="20,60": sustained requests per second and burst, per session or address.
_rate = (os.environ.get("APP_ENGINE_RATE_LIMIT", "") or "20,60").split(",")
RATE_PER_SECOND = float(_rate[0] or 20)
RATE_BURST = int(_rate[1] if len(_rate) > 1 and _rate[1] else 60)
_LOOPBACK = {"127.0.0.1", "::1", "localhost", "testclient"}
_APP_ID = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_USERNAME = re.compile(r"^[a-z0-9][a-z0-9_-]{2,23}$")
_NAME_LIMIT = 40
PASSWORD_MIN = 8
_PBKDF2_ROUNDS = 200_000


def hash_password(password: str, salt: bytes | None = None) -> str:
    """PBKDF2-HMAC-SHA256 with a per-user salt: ``pbkdf2$rounds$salt$hash``, all standard library."""
    salt = salt or secrets.token_bytes(16)
    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ROUNDS)
    return f"pbkdf2${_PBKDF2_ROUNDS}${salt.hex()}${derived.hex()}"


def check_password(password: str, stored: str | None) -> bool:
    try:
        scheme, rounds, salt, digest = (stored or "").split("$")
        if scheme != "pbkdf2":
            return False
        derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), int(rounds))
        return hmac.compare_digest(derived.hex(), digest)
    except (ValueError, TypeError):
        return False


@dataclass(frozen=True)
class Session:
    """Who is making a request. ``apps`` is None for the owner (everything) and the
    allow-list for a player."""

    subject_id: str
    role: str
    name: str
    apps: tuple[str, ...] | None = None
    username: str = ""

    @property
    def owner(self) -> bool:
        return self.role == "admin"

    def may_open(self, app_id: str) -> bool:
        return self.apps is None or app_id in self.apps


LOCAL_SESSION = Session("local", "admin", "local")


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _same(value: str, expected_hash: str) -> bool:
    return hmac.compare_digest(_digest(value), expected_hash)


@dataclass(frozen=True)
class PublicOrigin:
    """The launcher's public origin and what follows from it.

    ``layout`` is where apps live: ``"subdomain"`` (``https://<id>.<host>``, the isolated
    default, which needs a wildcard DNS record and certificate) or ``"same"`` (apps under
    ``https://<host>/apps/<id>/`` for a server that only has one host name, such as a
    hosting provider's ``srvNNN.example.cloud``; apps then share one browser origin).
    """

    scheme: str
    host: str          # e.g. play.example.com (no port unless non-default)
    hostname: str      # host without port, lower-case
    layout: str = "subdomain"

    @classmethod
    def parse(cls, value: str | None, layout: str | None = None) -> PublicOrigin | None:
        raw = (value or "").strip()
        if not raw:
            return None
        layout = (layout or "subdomain").strip().lower() or "subdomain"
        if layout not in {"subdomain", "same"}:
            raise ValueError("APP_ENGINE_APP_ORIGINS must be 'subdomain' or 'same'")
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
        return cls(scheme=parts.scheme, host=host, hostname=hostname, layout=layout)

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
        return self.host if self.layout == "same" else f"{app_id}.{self.host}"

    def app_origin(self, app_id: str) -> str:
        return f"{self.scheme}://{self.app_authority(app_id)}"

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
    stamp: float = 0.0

    @staticmethod
    def _empty() -> dict:
        return {"version": 1, "admin": {"secret_hash": None, "created": None, "sessions": []}, "players": [], "approvals": []}

    @classmethod
    def load(cls, state_dir: Path) -> Accounts:
        accounts = cls(path=Path(state_dir) / "players.json", data=cls._empty())
        accounts.refresh(force=True)
        return accounts

    def refresh(self, force: bool = False) -> None:
        """Re-read the file when something else (the CLI) changed it."""
        try:
            stamp = self.path.stat().st_mtime_ns
        except OSError:
            stamp = 0
        if not force and stamp == self.stamp:
            return
        self.stamp = stamp
        data = self._empty()
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(saved, dict) and saved.get("version") == 1 and isinstance(saved.get("admin"), dict):
                data = saved
                data["admin"].setdefault("sessions", [])
                data.setdefault("players", [])
                data.setdefault("approvals", [])
        except (FileNotFoundError, json.JSONDecodeError, UnicodeError, OSError):
            pass
        self.data = data

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".players-{secrets.token_hex(8)}.tmp")
        with open(temporary, "w", encoding="utf-8", opener=lambda p, f: os.open(p, f, 0o600)) as handle:
            json.dump(self.data, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.path)
        try:
            self.stamp = self.path.stat().st_mtime_ns
        except OSError:
            self.stamp = 0

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

    def resolve(self, token: str | None) -> Session | None:
        """The session for a live token (owner or player), refreshing its sliding expiry."""
        if not token or not isinstance(token, str) or len(token) > 256:
            return None
        self.refresh()
        digest = _digest(token)
        now = int(time.time())
        holders = [(self.data["admin"], Session("admin", "admin", "owner"))]
        holders += [(p, self._session_for(p)) for p in self.data["players"] if not p.get("disabled")]
        for record, session in holders:
            for entry in record.get("sessions", []):
                if not hmac.compare_digest(digest, entry.get("hash", "")):
                    continue
                if now - entry.get("last_seen", 0) >= SESSION_LIFETIME:
                    return None
                if now - entry.get("last_seen", 0) > 3600:
                    entry["last_seen"] = now
                    self.save()
                return session
        return None

    def revoke_session(self, token: str | None) -> None:
        if not token:
            return
        digest = _digest(token)
        keep = lambda entries: [s for s in entries if not hmac.compare_digest(digest, s.get("hash", ""))]  # noqa: E731
        self.data["admin"]["sessions"] = keep(self.data["admin"]["sessions"])
        for player in self.data["players"]:
            player["sessions"] = keep(player.get("sessions", []))
        self.save()

    # ── players ────────────────────────────────────────────────────────────

    def _player(self, player_id: str) -> dict | None:
        return next((p for p in self.data["players"] if p["id"] == player_id), None)

    @staticmethod
    def _session_for(player: dict) -> Session:
        return Session(player["id"], "player", player["name"], tuple(player.get("apps", [])), player.get("username", ""))

    def _new_session(self, player: dict) -> str:
        now = int(time.time())
        token = secrets.token_urlsafe(32)
        player["sessions"] = [s for s in player.get("sessions", []) if now - s.get("last_seen", 0) < SESSION_LIFETIME][-9:] + [{"hash": _digest(token), "created": now, "last_seen": now}]
        return token

    @staticmethod
    def _clean_apps(apps) -> list[str]:
        if not isinstance(apps, (list, tuple)):
            raise ValueError("apps must be a list of app ids")
        cleaned = []
        for app_id in apps:
            if not isinstance(app_id, str) or not _APP_ID.match(app_id):
                raise ValueError(f"{app_id!r} is not an app id")
            if app_id not in cleaned:
                cleaned.append(app_id)
        return cleaned

    def create_player(self, username: str, apps, name: str | None = None) -> tuple[dict, str]:
        """A new player with a one-time invitation; returns (public record, invite code).
        The player chooses a password when redeeming the invitation."""
        self.refresh()
        username = (username or "").strip().lower()
        if not _USERNAME.match(username):
            raise ValueError("a username is 3 to 24 characters: letters, digits, _ or -")
        if any(p.get("username") == username for p in self.data["players"]):
            raise ValueError(f"the username {username} is taken")
        name = (name or "").strip() or username
        if len(name) > _NAME_LIMIT:
            raise ValueError("a display name has at most 40 characters")
        if len(self.data["players"]) >= MAX_PLAYERS:
            raise ValueError("the player list is full")
        player = {"id": f"p_{secrets.token_hex(6)}", "username": username, "name": name, "apps": self._clean_apps(apps), "disabled": False,
                  "created": int(time.time()), "password_hash": None, "invite_hash": None, "invite_expires": 0, "sessions": []}
        self.data["players"].append(player)
        code = self._new_invite(player)
        self.save()
        return self.public_player(player), code

    def _new_invite(self, player: dict) -> str:
        code = secrets.token_urlsafe(24)
        player["invite_hash"] = _digest(code)
        player["invite_expires"] = int(time.time()) + INVITE_LIFETIME
        return code

    def regenerate_invite(self, player_id: str) -> str | None:
        self.refresh()
        player = self._player(player_id)
        if player is None:
            return None
        code = self._new_invite(player)
        self.save()
        return code

    def invited_player(self, code: str | None) -> dict | None:
        """The player a live invitation code belongs to (not consumed)."""
        if not code or not isinstance(code, str) or len(code) > 256:
            return None
        self.refresh()
        digest = _digest(code)
        now = int(time.time())
        for player in self.data["players"]:
            stored = player.get("invite_hash")
            if stored and hmac.compare_digest(digest, stored) and not player.get("disabled") and player.get("invite_expires", 0) > now:
                return player
        return None

    def redeem_invite(self, code: str | None, password: str) -> tuple[Session, str] | None:
        """Consume a one-time invitation with the password the player chose; returns the
        player's session and a token for it."""
        player = self.invited_player(code)
        if player is None:
            return None
        if not isinstance(password, str) or len(password) < PASSWORD_MIN or len(password) > 256:
            raise ValueError(f"a password has at least {PASSWORD_MIN} characters")
        player["password_hash"] = hash_password(password)
        player["invite_hash"] = None
        player["invite_expires"] = 0
        token = self._new_session(player)
        self.save()
        return self._session_for(player), token

    def sign_in_player(self, username: str, password: str) -> tuple[Session, str] | None:
        """Username and password to a session token, for a browser here or a game client elsewhere."""
        self.refresh()
        username = (username or "").strip().lower()
        player = next((p for p in self.data["players"] if p.get("username") == username), None)
        if player is None or player.get("disabled") or not check_password(password if isinstance(password, str) else "", player.get("password_hash")):
            return None
        token = self._new_session(player)
        self.save()
        return self._session_for(player), token

    def update_player(self, player_id: str, *, disabled: bool | None = None, apps=None, name: str | None = None) -> dict | None:
        self.refresh()
        player = self._player(player_id)
        if player is None:
            return None
        if disabled is not None:
            player["disabled"] = bool(disabled)
            if disabled:
                player["sessions"] = []
        if apps is not None:
            player["apps"] = self._clean_apps(apps)
        if name is not None:
            name = name.strip()
            if not name or len(name) > _NAME_LIMIT:
                raise ValueError("a player needs a name of up to 40 characters")
            player["name"] = name
        self.save()
        return self.public_player(player)

    def remove_player(self, player_id: str) -> bool:
        self.refresh()
        before = len(self.data["players"])
        self.data["players"] = [p for p in self.data["players"] if p["id"] != player_id]
        if len(self.data["players"]) == before:
            return False
        self.save()
        return True

    @staticmethod
    def public_player(player: dict) -> dict:
        now = int(time.time())
        seen = max((s.get("last_seen", 0) for s in player.get("sessions", [])), default=None)
        return {"id": player["id"], "username": player.get("username", ""), "name": player["name"], "apps": list(player.get("apps", [])),
                "disabled": bool(player.get("disabled")), "created": player.get("created"), "has_password": bool(player.get("password_hash")),
                "invite_pending": bool(player.get("invite_hash")) and player.get("invite_expires", 0) > now,
                "invite_expires": player.get("invite_expires") if player.get("invite_hash") else None,
                "signed_in": bool(player.get("sessions")), "last_seen": seen}

    def players(self) -> list[dict]:
        self.refresh()
        return [self.public_player(p) for p in self.data["players"]]

    # ── standing launch approvals ──────────────────────────────────────────

    def approve_plan(self, fingerprint: str) -> None:
        """The owner approved this exact plan; players (and the owner) may open it until it changes."""
        self.refresh()
        if fingerprint not in self.data["approvals"]:
            self.data["approvals"] = (self.data["approvals"] + [fingerprint])[-200:]
            self.save()

    def plan_approved(self, fingerprint: str) -> bool:
        self.refresh()
        return fingerprint in self.data["approvals"]


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


class RateLimiter:
    """A token bucket per key (a session or an address): a lost game client in a retry loop, or
    a stranger hammering the player API, is slowed down rather than the whole server."""

    def __init__(self, per_second: float = RATE_PER_SECOND, burst: int = RATE_BURST):
        self.per_second, self.burst = per_second, burst
        self._buckets: dict[str, tuple[float, float]] = {}

    def allow(self, key: str) -> float:
        """0.0 when the request may proceed, else the seconds to wait."""
        now = time.monotonic()
        tokens, stamp = self._buckets.get(key, (float(self.burst), now))
        tokens = min(self.burst, tokens + (now - stamp) * self.per_second)
        if tokens < 1.0:
            self._buckets[key] = (tokens, now)
            return (1.0 - tokens) / self.per_second
        self._buckets[key] = (tokens - 1.0, now)
        if len(self._buckets) > 10000:
            oldest = sorted(self._buckets.items(), key=lambda item: item[1][1])[: len(self._buckets) // 2]
            for stale, _ in oldest:
                self._buckets.pop(stale, None)
        return 0.0


class Audit:
    """Append-only JSON lines under the state directory: who signed in, who was invited,
    what was approved. Never secrets, tokens or passwords."""

    def __init__(self, state_dir: Path):
        self.path = Path(state_dir) / "audit.log"

    def record(self, event: str, **fields) -> None:
        line = json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "event": event, **fields}, sort_keys=True)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8", opener=lambda p, f: os.open(p, f, 0o600)) as handle:
                handle.write(line + "\n")
        except OSError:
            pass


class OwnerGate:
    """ASGI middleware: in public-origin mode nothing but the sign-in surface is served
    without a live owner session. HTML navigations are redirected to /admin; everything
    else answers 401 JSON. The subject is stored on ``scope['state']`` for the routes."""

    OPEN_PATHS = {"/admin", "/admin/sign-in", "/sign-in", "/api/engine"}
    # Invitations, and the token-authenticated API that game clients on other machines use.
    OPEN_PREFIXES = ("/join/", "/api/players/")
    # The busy, per-player surfaces; the launcher and static apps are not worth metering.
    LIMITED_PREFIXES = ("/api/players/", "/api/app-multiplayer/", "/api/app-state/", "/api/chat")

    def __init__(self, app: ASGIApp, accounts: Accounts, enabled: bool, limiter: RateLimiter | None = None):
        self.app, self.accounts, self.enabled = app, accounts, enabled
        self.limiter = limiter or RateLimiter()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self.enabled:
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        session = self.accounts.resolve(request.cookies.get(SESSION_COOKIE))
        scope.setdefault("state", {})
        scope["state"]["session"] = session
        path = request.url.path
        if path.startswith(self.LIMITED_PREFIXES) and request.method != "OPTIONS":
            bearer = request.headers.get("authorization", "")
            key = f"s:{_digest(request.cookies.get(SESSION_COOKIE) or bearer[7:])}" if (session or bearer) else f"a:{request.client.host if request.client else ''}"
            wait = self.limiter.allow(key)
            if wait:
                response = JSONResponse({"ok": False, "error": {"code": "RATE_LIMITED", "message": "Slow down: too many requests."}}, status_code=429, headers={"Retry-After": str(max(1, int(wait + 0.999)))})
                await response(scope, receive, send)
                return
        if session is None and path not in self.OPEN_PATHS and not path.startswith(self.OPEN_PREFIXES):
            accepts_html = "text/html" in request.headers.get("accept", "") and request.method == "GET"
            response: Response = RedirectResponse("/admin", status_code=303) if accepts_html else JSONResponse({"detail": "sign in required"}, status_code=401)
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def _page(title: str, body: str) -> str:
    return f"""<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>app-engine · {title}</title>
<style>body{{margin:0;min-height:100vh;display:grid;place-items:center;background:#0d1117;color:#e6edf3;font:15px system-ui,sans-serif}}
form,main{{display:grid;gap:12px;width:min(380px,90vw);padding:28px;background:#161b22;border:1px solid #30363d;border-radius:10px}}
h1{{margin:0;font-size:20px}}p{{margin:0;color:#8b949e;font-size:13px}}input{{padding:10px;border-radius:6px;border:1px solid #30363d;background:#0d1117;color:inherit;font-size:15px}}
button{{padding:10px;border-radius:6px;border:0;background:#2f81f7;color:#fff;font-weight:600;cursor:pointer}}.error{{color:#ff7b72}}code{{color:#e6edf3}}</style>
{body}"""


def join_page(code: str, username: str, error: str = "") -> str:
    err = f'<p class="error">{error}</p>' if error else ""
    return _page("welcome", f"""<form method="post" action="/join/{code}"><h1>Welcome, {username}</h1>
<p>You were invited to this app-engine server. Choose the password you will sign in with (at least {PASSWORD_MIN} characters).</p>{err}
<input type="text" name="username" value="{username}" autocomplete="username" readonly>
<input type="password" name="password" placeholder="Password" autocomplete="new-password" minlength="{PASSWORD_MIN}" required autofocus>
<input type="password" name="confirm" placeholder="Password again" autocomplete="new-password" minlength="{PASSWORD_MIN}" required>
<button type="submit">Set password and enter</button></form>""")


def player_sign_in_page(error: str = "") -> str:
    err = f'<p class="error">{error}</p>' if error else ""
    return _page("sign in", f"""<form method="post" action="/sign-in"><h1>Sign in</h1><p>Your username and password for this server.</p>{err}
<input type="text" name="username" placeholder="Username" autocomplete="username" autofocus required>
<input type="password" name="password" placeholder="Password" autocomplete="current-password" required>
<button type="submit">Sign in</button><p>Server owner? <a href="/admin" style="color:#58a6ff">Owner sign-in</a></p></form>""")


class RemoteApiCors:
    """The token-authenticated ``/api/players/`` API is meant for game clients on other
    origins (a player's own app-engine or Atrium). Bearer tokens are never sent by
    browsers on their own, so the API can be open to any origin without credentials."""

    PREFIX = "/api/players/"
    HEADERS = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, POST, PUT, OPTIONS",
        "Access-Control-Allow-Headers": "Authorization, Content-Type, X-App-Multiplayer-Credential",
        "Access-Control-Max-Age": "600",
        "Vary": "Origin",
    }

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith(self.PREFIX):
            await self.app(scope, receive, send)
            return
        if scope["method"] == "OPTIONS":
            await Response(status_code=204, headers=self.HEADERS)(scope, receive, send)
            return

        async def send_with_cors(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers += [(k.lower().encode(), v.encode()) for k, v in self.HEADERS.items()]
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_cors)


JOIN_ERROR_PAGE = """<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>app-engine · invitation</title>
<style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0d1117;color:#e6edf3;font:15px system-ui,sans-serif}
main{width:min(420px,90vw);padding:28px;background:#161b22;border:1px solid #30363d;border-radius:10px;display:grid;gap:10px}h1{margin:0;font-size:20px}p{margin:0;color:#8b949e}</style>
<main><h1>This invitation cannot be used</h1><p>It may have been used already, expired, or been withdrawn. Ask the person who invited you for a new link.</p></main>"""


def invite_link(public: PublicOrigin, code: str) -> str:
    return f"{public.origin}/join/{code}"


def set_session_cookie(response: Response, token: str, public: PublicOrigin) -> None:
    response.set_cookie(SESSION_COOKIE, token, max_age=SESSION_LIFETIME, httponly=True, secure=public.secure, samesite="lax", domain=public.cookie_domain(), path="/")


def clear_session_cookie(response: Response, public: PublicOrigin) -> None:
    response.delete_cookie(SESSION_COOKIE, domain=public.cookie_domain(), path="/")


def startup_warnings(host: str, public: PublicOrigin | None, trust_proxy: bool) -> list[str]:
    """Deployment mistakes worth a line in the log before the first visitor arrives."""
    notes: list[str] = []
    if public is None:
        return notes
    if public.secure and not trust_proxy:
        notes.append("APP_ENGINE_PUBLIC_ORIGIN is https but APP_ENGINE_TRUST_PROXY is not set: behind a TLS proxy the engine will see http and Secure cookies will not be sent. Set APP_ENGINE_TRUST_PROXY=1.")
    if host.strip().strip("[]").lower() not in _LOOPBACK:
        notes.append(f"APP_ENGINE_HOST={host} listens on a public interface without TLS. Bind 127.0.0.1 and put Caddy or nginx in front.")
    if public.layout == "same":
        notes.append("APP_ENGINE_APP_ORIGINS=same: apps share one browser origin on this server (no per-app isolation). Use a domain with a wildcard record for the subdomain layout.")
    return notes


def bind_is_allowed(host: str, public: PublicOrigin | None, accounts: Accounts | None) -> str | None:
    """Why a bind address is refused, or None when it is fine."""
    if host.strip().strip("[]").lower() in _LOOPBACK:
        return None
    if public is None:
        return f"APP_ENGINE_HOST={host} would make every visitor the administrator. Set APP_ENGINE_PUBLIC_ORIGIN and put a TLS proxy in front, or bind 127.0.0.1."
    if accounts is None or not accounts.has_admin_secret:
        return "Public-origin mode needs an admin secret before binding a public address."
    return None
