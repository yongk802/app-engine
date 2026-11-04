"""Mark every JSON response ``X-Content-Type-Options: nosniff``.

The launcher page already sends it; the JSON API did not. Without it an old
or permissive browser may sniff an API answer that echoes user text as HTML
or script when a page loads it in a frame or a ``<script>`` tag.
"""

from __future__ import annotations

from starlette.types import ASGIApp, Message, Receive, Scope, Send

_NOSNIFF = (b"x-content-type-options", b"nosniff")


class JsonNosniff:
    """ASGI middleware: add nosniff to ``application/json`` responses that lack it."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_nosniff(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                names = {name.lower(): value for name, value in headers}
                if (names.get(b"content-type", b"").lower().startswith(b"application/json")
                        and b"x-content-type-options" not in names):
                    message = {**message, "headers": [*headers, _NOSNIFF]}
            await send(message)

        await self.app(scope, receive, send_with_nosniff)
