"""Refuse requests addressed to a host name the loopback engine does not answer to.

The loopback product has no sign-in: being reachable at 127.0.0.1 is the
credential. A DNS-rebinding page (``evil.example`` re-pointed at 127.0.0.1)
reaches the same socket, but its requests carry ``Host: evil.example`` and the
browser treats the answers as same-origin to the attacker. Checking the Host
header closes that, the way Atrium's session cookie closes it for its own vault.
Public-origin mode is gated by the owner session instead and is left alone.
"""

from __future__ import annotations

import ipaddress

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

_LOCAL_NAMES = {"localhost", "testserver"}


def _hostname(authority: str) -> str:
    authority = authority.strip().lower()
    if authority.startswith("["):
        return authority[1:authority.find("]")] if "]" in authority else ""
    return authority.rsplit(":", 1)[0] if authority.count(":") == 1 else authority


def local_host_allowed(authority: str, extra: frozenset[str] = frozenset()) -> bool:
    """Is *authority* (a Host header value) a name this computer alone resolves?"""
    host = _hostname(authority).rstrip(".")
    if not host:
        return False
    if host in _LOCAL_NAMES or host.endswith(".localhost") or host in extra:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class LocalHostGate:
    """ASGI middleware for loopback mode: 421 for any Host outside the local names.

    *extra* names more hosts to accept (``APP_ENGINE_ALLOWED_HOSTS``), and
    *trusted* lets a request through that a configured local proxy vouched for,
    such as the mTLS MCP gateway, which forwards its own server name.
    """

    def __init__(self, app: ASGIApp, extra: frozenset[str] = frozenset(),
                 trusted=None):
        self.app, self.extra, self.trusted = app, extra, trusted

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in {"http", "websocket"}:
            host = Headers(scope=scope).get("host", "")
            if not local_host_allowed(host, self.extra) and not (self.trusted and self.trusted(scope)):
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 1008})
                else:
                    await JSONResponse({"detail": "unrecognised host name"},
                                       status_code=421)(scope, receive, send)
                return
        await self.app(scope, receive, send)
