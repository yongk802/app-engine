"""Loopback-only, fixed-origin price-feed gateway for isolated Night City Table.

This service has outbound HTTPS access. Managed app units can reach only its
small public TCGCSV path allowlist, while their own cgroups deny external IPs.
"""

from __future__ import annotations

import re
import ssl
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.error import HTTPError
from urllib.request import (
    HTTPRedirectHandler,
    HTTPSHandler,
    ProxyHandler,
    Request,
    build_opener,
)

ORIGIN = "https://tcgcsv.com"
MAX_RESPONSE_BYTES = 20_000_000
_GROUP_PATH = re.compile(r"/tcgplayer/92/[1-9][0-9]{0,9}/(?:products|prices)\Z")
_SIMPLE_PATHS = frozenset({
    "/last-updated.txt", "/tcgplayer/categories", "/tcgplayer/92/groups",
})


def allowed_path(path: str) -> bool:
    return isinstance(path, str) and (
        path in _SIMPLE_PATHS or _GROUP_PATH.fullmatch(path) is not None
    )


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None


def fetch(path: str) -> bytes:
    if not allowed_path(path):
        raise ValueError("unlisted price-feed path")
    opener = build_opener(ProxyHandler({}), HTTPSHandler(context=ssl.create_default_context()),
                          _NoRedirect())
    request = Request(ORIGIN + path, headers={"User-Agent": "NightCityTable/1.0 (pricing gateway)"})
    with opener.open(request, timeout=30) as response:
        if response.status != 200 or response.geturl() != ORIGIN + path:
            raise ValueError("unexpected price-feed response")
        payload = response.read(MAX_RESPONSE_BYTES + 1)
    if len(payload) > MAX_RESPONSE_BYTES:
        raise ValueError("price-feed response too large")
    return payload


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        if self.path == "/health":
            self._respond(200, b"ok")
            return
        if not allowed_path(self.path):
            self._respond(404, b"not found")
            return
        try:
            payload = fetch(self.path)
        except (OSError, ValueError, HTTPError):
            self._respond(502, b"price feed unavailable")
            return
        self._respond(200, payload)

    def do_POST(self) -> None:
        self._respond(405, b"method not allowed")

    def do_HEAD(self) -> None:
        self._respond(405, b"method not allowed")

    def _respond(self, status: int, payload: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)
        self.close_connection = True

    def log_message(self, format: str, *args: object) -> None:
        pass


def make_server(port: int = 18792) -> HTTPServer:
    return HTTPServer(("127.0.0.1", port), Handler)


def main() -> None:
    make_server().serve_forever()


if __name__ == "__main__":
    main()
