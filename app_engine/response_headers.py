"""Response headers shared by the engine's own pages and its JSON API.

``JsonNosniff`` marks every JSON response ``X-Content-Type-Options: nosniff``:
the launcher page already sent it, the JSON API did not, so an old or
permissive browser might sniff an answer that echoes user text as HTML or
script. ``nonce_page`` gives a self-contained management page (store, studio)
a script policy that runs only its own inline block.
"""

from __future__ import annotations

import secrets

from starlette.types import ASGIApp, Message, Receive, Scope, Send

_NOSNIFF = (b"x-content-type-options", b"nosniff")

_PAGE_CSP = ("default-src 'self'; script-src 'self' 'nonce-{nonce}'; script-src-attr 'none'; "
             "style-src 'self' 'unsafe-inline'; img-src 'self' data:; object-src 'none'; "
             "base-uri 'none'; form-action 'self'; frame-ancestors 'none'")


def nonce_page(html: str) -> tuple[str, dict[str, str]]:
    """Stamp each ``<script>`` with a fresh nonce; return the page and its headers.

    Everything else (an injected inline script, an ``onclick=`` attribute, a
    foreign script, a frame around the page) is refused by the browser.
    """
    nonce = secrets.token_urlsafe(18)
    html = html.replace("<script>", f'<script nonce="{nonce}">')
    return html, {"Content-Security-Policy": _PAGE_CSP.format(nonce=nonce),
                  "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"}


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
