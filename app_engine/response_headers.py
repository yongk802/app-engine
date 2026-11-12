"""Response headers shared by the engine's own pages, its API and its proxies.

``Nosniff`` marks every response ``X-Content-Type-Options: nosniff``: the
launcher page already sent it, the JSON API and the bundled assets did not, so
an old or permissive browser might sniff an answer that echoes user text as
HTML or script. ``nonce_page`` gives a self-contained management page (store,
studio) a script policy that runs only its own inline block.
``proxied_defaults`` fills in what an app backend's response left out.
"""

from __future__ import annotations

import secrets

from starlette.types import ASGIApp, Message, Receive, Scope, Send

_NOSNIFF = (b"x-content-type-options", b"nosniff")

_PAGE_CSP = ("default-src 'self'; script-src 'self' 'nonce-{nonce}'; script-src-attr 'none'; "
             "style-src 'self' 'nonce-{nonce}'; style-src-attr 'none'; img-src 'self' data:; object-src 'none'; "
             "base-uri 'none'; form-action 'self'; frame-ancestors 'none'")


def nonce_page(html: str) -> tuple[str, dict[str, str]]:
    """Stamp each ``<script>`` and ``<style>`` with a fresh nonce; return the page and its headers.

    Everything else (an injected inline script or stylesheet, an ``onclick=`` or
    ``style=`` attribute, a foreign script, a frame around the page) is refused
    by the browser. The page's own scripts still style elements through the DOM.
    """
    nonce = secrets.token_urlsafe(18)
    html = html.replace("<script>", f'<script nonce="{nonce}">').replace("<style>", f'<style nonce="{nonce}">')
    return html, {"Content-Security-Policy": _PAGE_CSP.format(nonce=nonce),
                  "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"}


def proxied_defaults(headers: list[tuple[bytes, bytes]],
                     html_csp: str | None = None) -> list[tuple[bytes, bytes]]:
    """Add the headers an app backend's response lacks; never touch one it sent.

    Every response gets nosniff. An HTML page with no ``Content-Security-Policy``
    gets ``html_csp`` (the host's framing policy); a backend that sends its own
    policy keeps it exactly as sent.
    """
    names = {name.lower(): value for name, value in headers}
    added = [] if b"x-content-type-options" in names else [_NOSNIFF]
    if (html_csp and b"content-security-policy" not in names
            and names.get(b"content-type", b"").lower().startswith(b"text/html")):
        added.append((b"content-security-policy", html_csp.encode("latin-1")))
    return [*headers, *added]


class Nosniff:
    """ASGI middleware: add nosniff to every response that lacks it.

    It only appends a header to the response start, so streamed bodies (SSE,
    proxied apps) pass through unbuffered.
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_nosniff(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                if not any(name.lower() == b"x-content-type-options" for name, _ in headers):
                    message = {**message, "headers": [*headers, _NOSNIFF]}
            await send(message)

        await self.app(scope, receive, send_with_nosniff)
