"""Deny HTTP on a non-loopback ASGI listener, including direct uvicorn imports."""

from __future__ import annotations

import ipaddress

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send


def _loopback_listener(server: object) -> bool:
    if not isinstance(server, (tuple, list)) or not server or not isinstance(server[0], str):
        return False
    host = server[0].strip("[]").lower()
    if host in {"localhost", "testserver"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class LoopbackListenerGate:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        test_transport = (scope.get("client", (None,))[0] == "testclient" and
                          "http.response.debug" in scope.get("extensions", {}))
        if scope["type"] in {"http", "websocket"} and not (
            _loopback_listener(scope.get("server")) or test_transport
        ):
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            else:
                await JSONResponse({"detail": "app-engine requires a loopback listener"},
                                   status_code=503)(scope, receive, send)
            return
        await self.app(scope, receive, send)
