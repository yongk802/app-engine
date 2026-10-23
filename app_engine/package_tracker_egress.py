"""Destination policy for Package Tracker's authenticated CONNECT proxy."""

from __future__ import annotations

import ipaddress
import base64
import binascii
import hmac
import os
import re
import select
import socket
import socketserver
import stat
import argparse
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

_DNS_NAME = re.compile(r"(?=.{1,253}\Z)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*\Z")
_CARRIER_HOSTS = frozenset({
    "onlinetools.ups.com", "apis.fedex.com", "apis.usps.com",
    "api.yunexpress.com",
})


@dataclass(frozen=True)
class Destination:
    hostname: str
    ip: str
    port: int


class EgressPolicy:
    """Authorize a hostname once and pin its public address for the dial."""

    def __init__(self, *, resolve: Callable = socket.getaddrinfo):
        self._resolve = resolve

    def authorize(self, profile: str, authority: str) -> Destination:
        if not isinstance(authority, str) or not authority or any(
            char in authority for char in "/?#@\\\r\n\x00"
        ):
            raise ValueError("destination denied")
        try:
            parsed = urlsplit(f"//{authority}")
            hostname = parsed.hostname
            port = parsed.port
        except ValueError as exc:
            raise ValueError("destination denied") from exc
        if (not hostname or port != 443 or parsed.username or parsed.password
                or parsed.path or parsed.query or parsed.fragment):
            raise ValueError("destination denied")
        hostname = hostname.lower()
        if profile not in {"browser", "carrier"}:
            raise ValueError("destination denied")
        if profile == "carrier" and hostname not in _CARRIER_HOSTS:
            raise ValueError("destination denied")
        try:
            literal = ipaddress.ip_address(hostname)
        except ValueError:
            literal = None
        if literal is not None:
            if profile == "carrier" or not literal.is_global:
                raise ValueError("destination denied")
            return Destination(hostname, literal.compressed, 443)
        if not _DNS_NAME.fullmatch(hostname):
            raise ValueError("destination denied")
        try:
            answers = self._resolve(hostname, 443, type=socket.SOCK_STREAM)
            ips = [ipaddress.ip_address(answer[4][0]) for answer in answers]
        except (OSError, ValueError, IndexError, TypeError) as exc:
            raise ValueError("destination denied") from exc
        if not ips or not all(ip.is_global for ip in ips):
            raise ValueError("destination denied")
        return Destination(hostname, ips[0].compressed, 443)


class _ConnectHandler(BaseHTTPRequestHandler):
    rbufsize = 0
    protocol_version = "HTTP/1.1"

    def log_message(self, _format, *_args):
        # Authorities may contain private tracking identifiers; never log them.
        pass

    def _reply(self, code: int, reason: str) -> None:
        self.send_response_only(code, reason)
        if code == 407:
            self.send_header("Proxy-Authenticate", 'Basic realm="package-tracker"')
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    def _profile(self) -> str | None:
        supplied = self.headers.get("Proxy-Authorization", "")
        scheme, _, encoded = supplied.partition(" ")
        if scheme.lower() != "basic" or not encoded:
            return None
        try:
            decoded = base64.b64decode(encoded, validate=True)
            profile, sep, password = decoded.partition(b":")
            name = profile.decode("ascii")
        except (binascii.Error, UnicodeError):
            return None
        expected = self.server.credentials.get(name)
        if not sep or not expected or not hmac.compare_digest(password, expected.encode("utf-8")):
            return None
        return name

    def do_CONNECT(self) -> None:
        profile = self._profile()
        if profile is None:
            self._reply(407, "Proxy Authentication Required")
            return
        try:
            target = self.server.policy.authorize(profile, self.path)
        except ValueError:
            self._reply(403, "Forbidden")
            return
        try:
            upstream = self.server.dial(target)
        except OSError:
            self._reply(502, "Bad Gateway")
            return
        with upstream:
            self.send_response_only(200, "Connection Established")
            self.end_headers()
            self.wfile.flush()
            self.connection.settimeout(30)
            upstream.settimeout(30)
            while True:
                try:
                    ready, _, _ = select.select((self.connection, upstream), (), (), 30)
                    if not ready:
                        break
                    for source in ready:
                        chunk = source.recv(65536)
                        if not chunk:
                            return
                        (upstream if source is self.connection else self.connection).sendall(chunk)
                except OSError:
                    return
        self.close_connection = True

    def do_GET(self) -> None:
        self._reply(405, "Method Not Allowed")

    do_POST = do_PUT = do_DELETE = do_HEAD = do_OPTIONS = do_GET


class _ConnectServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


def _dial(target: Destination) -> socket.socket:
    return socket.create_connection((target.ip, target.port), timeout=5)


def make_proxy_server(address: tuple[str, int], *, credentials: dict[str, str],
                      policy: EgressPolicy | None = None,
                      dial: Callable[[Destination], socket.socket] | None = None) -> _ConnectServer:
    if address[0] != "127.0.0.1" or set(credentials) != {"browser", "carrier"}:
        raise ValueError("invalid proxy listener")
    if any(not isinstance(token, str) or len(token) < 12 for token in credentials.values()):
        raise ValueError("invalid proxy credentials")
    server = _ConnectServer(address, _ConnectHandler)
    server.credentials = credentials
    server.policy = policy or EgressPolicy()
    server.dial = dial or _dial
    return server


def read_token_file(path: Path) -> str:
    """Read one random token from a private, non-symlinked credential file."""
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as stream:
            status = os.fstat(stream.fileno())
            private_owner = (status.st_uid == os.geteuid() and not status.st_mode & 0o077)
            systemd_credential = (status.st_uid == 0 and not status.st_mode & 0o037)
            if (not stat.S_ISREG(status.st_mode)
                    or not (private_owner or systemd_credential)
                    or status.st_size > 128):
                raise ValueError("invalid proxy token file")
            raw = stream.read(129)
        token = raw.removesuffix(b"\n").decode("ascii")
    except (OSError, UnicodeError) as exc:
        raise ValueError("invalid proxy token file") from exc
    if not re.fullmatch(r"[a-f0-9]{64}", token):
        raise ValueError("invalid proxy token file")
    return token


def main() -> int:
    parser = argparse.ArgumentParser(description="Package Tracker loopback CONNECT proxy")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--browser-token-file", type=Path, required=True)
    parser.add_argument("--carrier-token-file", type=Path, required=True)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    try:
        if not 1024 <= args.port <= 65535:
            raise ValueError("invalid proxy port")
        credentials = {
            "browser": read_token_file(args.browser_token_file),
            "carrier": read_token_file(args.carrier_token_file),
        }
        if args.check:
            return 0
        with make_proxy_server(("127.0.0.1", args.port), credentials=credentials) as server:
            server.serve_forever(poll_interval=0.5)
        return 0
    except (OSError, ValueError):
        return 78


if __name__ == "__main__":
    raise SystemExit(main())
