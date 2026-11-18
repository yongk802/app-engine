"""Keep the launcher's routes on the launcher's host name.

Apps run on their own origins (``<id>.localhost``, or ``<id>.<public host>``),
but every origin reaches the same engine, and in public subdomain mode the
session cookie covers them all. Without this gate a hosted app's script could
call a launcher route on its *own* origin (same-origin, so no CORS preflight)
and have it answered with the visitor's session: read the player list or the
server chat, post chat as them, or read the local-AI hardware report.

A request whose Host is not the launcher's may reach only what an app page
needs: its files, its saves, its room service, its session content, and the
host contract's ``/state``, ``/api/app-chat`` and the read-only
``/api/local-ai/status`` its chat panel polls. The bearer-token player API
and the sign-in pages stay reachable on any name: neither answers to a cookie
an app could ride on.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

APP_ORIGIN_PATHS = re.compile(
    r"^/(?:"
    r"apps/"                                              # app files and entry_point backends
    r"|api/app-state/"                                    # per-app saves (capability-gated)
    r"|api/app-multiplayer/"                              # the app's room service
    r"|api/app-engine/sessions/[^/]+/(?:assets|proxy)/"   # managed session content
    r"|api/players/"                                      # bearer-only remote API
    r"|join/"                                             # invitation links
    r"|(?:api/engine|api/app-chat|api/local-ai/status|state|admin|admin/sign-in|sign-in)\Z"
    r")"
)


def app_origin_may_call(path: str) -> bool:
    """May a request addressed to a host other than the launcher's reach *path*?"""
    return APP_ORIGIN_PATHS.match(path) is not None


class AppOriginGate:
    """ASGI middleware: 403 for a launcher route asked for on another host name.

    *is_launcher* says whether the request is addressed to the launcher's own
    host. *trusted* lets a request through that a configured local proxy
    vouched for (the mTLS MCP gateway forwards its own server name).
    """

    def __init__(self, app: ASGIApp, is_launcher: Callable[[Request], bool],
                 trusted: Callable[[Request], bool] | None = None):
        self.app, self.is_launcher, self.trusted = app, is_launcher, trusted

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and not app_origin_may_call(scope["path"]):
            request = Request(scope)
            if not self.is_launcher(request) and not (self.trusted and self.trusted(request)):
                await JSONResponse({"detail": "launcher origin required"},
                                   status_code=403)(scope, receive, send)
                return
        await self.app(scope, receive, send)
