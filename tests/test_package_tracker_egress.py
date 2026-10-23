"""Package Tracker's outbound proxy must reject private destinations."""

import os
import socket
import threading
from contextlib import contextmanager

import pytest

from app_engine.package_tracker_egress import EgressPolicy, make_proxy_server, read_token_file


def answers(*ips):
    def resolve(_host, port, *, type):
        assert port == 443 and type == socket.SOCK_STREAM
        return [(socket.AF_INET6 if ":" in ip else socket.AF_INET,
                 socket.SOCK_STREAM, 6, "", (ip, port)) for ip in ips]
    return resolve


def test_browser_policy_pins_a_public_dns_answer():
    policy = EgressPolicy(resolve=answers("8.8.8.8"))
    target = policy.authorize("browser", "tracking.example:443")
    assert (target.hostname, target.ip, target.port) == (
        "tracking.example", "8.8.8.8", 443)


@pytest.mark.parametrize("ips", [
    ("127.0.0.1",), ("169.254.169.254",), ("10.0.0.1",),
    ("8.8.8.8", "192.168.1.10"),
])
def test_browser_policy_rejects_private_and_mixed_dns_answers(ips):
    policy = EgressPolicy(resolve=answers(*ips))
    with pytest.raises(ValueError, match="destination"):
        policy.authorize("browser", "tracking.example:443")


@pytest.mark.parametrize("profile,authority", [
    ("carrier", "tracking.example:443"),
    ("browser", "tracking.example:80"),
    ("browser", "user@tracking.example:443"),
    ("browser", "tracking.example:443/path"),
    ("browser", "[::1]:443"),
])
def test_proxy_policy_rejects_unscoped_authorities(profile, authority):
    policy = EgressPolicy(resolve=answers("8.8.8.8"))
    with pytest.raises(ValueError, match="destination"):
        policy.authorize(profile, authority)


def test_carrier_policy_accepts_only_the_four_api_hosts():
    policy = EgressPolicy(resolve=answers("8.8.8.8"))
    for host in ("onlinetools.ups.com", "apis.fedex.com", "apis.usps.com", "api.yunexpress.com"):
        assert policy.authorize("carrier", f"{host}:443").hostname == host


@contextmanager
def proxy_server(*, dial=None):
    server = make_proxy_server(
        ("127.0.0.1", 0), credentials={"browser": "browser-secret", "carrier": "carrier-secret"},
        policy=EgressPolicy(resolve=answers("8.8.8.8")), dial=dial)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def response(address, request):
    with socket.create_connection(address, timeout=2) as conn:
        conn.sendall(request)
        return conn.recv(4096)


def test_connect_proxy_requires_its_profile_secret():
    with proxy_server() as address:
        denied = response(address, b"CONNECT tracking.example:443 HTTP/1.1\r\nHost: tracking.example:443\r\n\r\n")
        assert b" 407 " in denied.split(b"\r\n", 1)[0]


def test_connect_proxy_does_not_forward_plain_http():
    with proxy_server() as address:
        denied = response(address, b"GET https://tracking.example/ HTTP/1.1\r\nHost: tracking.example\r\n\r\n")
        assert b" 405 " in denied.split(b"\r\n", 1)[0]


def test_connect_proxy_rejects_private_dns_even_when_authenticated():
    import base64
    header = base64.b64encode(b"browser:browser-secret")
    with proxy_server() as address:
        # A literal private address never reaches the dialer.
        denied = response(address, b"CONNECT 127.0.0.1:443 HTTP/1.1\r\nProxy-Authorization: Basic "
                          + header + b"\r\n\r\n")
        assert b" 403 " in denied.split(b"\r\n", 1)[0]


def test_connect_proxy_pins_validated_ip_and_tunnels_bytes():
    import base64
    other_end = []
    dialed = []

    def dial(target):
        dialed.append(target)
        near, far = socket.socketpair()
        other_end.append(far)
        return near

    header = base64.b64encode(b"browser:browser-secret")
    with proxy_server(dial=dial) as address:
        with socket.create_connection(address, timeout=2) as client:
            client.sendall(b"CONNECT tracking.example:443 HTTP/1.1\r\nProxy-Authorization: Basic "
                           + header + b"\r\n\r\n")
            assert b" 200 " in client.recv(4096).split(b"\r\n", 1)[0]
            client.sendall(b"ping")
            assert other_end[0].recv(4) == b"ping"
            other_end[0].sendall(b"pong")
            assert client.recv(4) == b"pong"
    for peer in other_end:
        peer.close()
    assert [(d.hostname, d.ip, d.port) for d in dialed] == [("tracking.example", "8.8.8.8", 443)]


@pytest.mark.skipif(os.name != "posix", reason="Unix file ownership and modes")
def test_token_reader_rejects_world_readable_or_symlinked_secret(tmp_path):
    secret = tmp_path / "token"
    secret.write_text("a" * 64 + "\n")
    secret.chmod(0o644)
    with pytest.raises(ValueError, match="token"):
        read_token_file(secret)
    secret.chmod(0o600)
    assert read_token_file(secret) == "a" * 64
    alias = tmp_path / "alias"
    alias.symlink_to(secret)
    with pytest.raises(ValueError, match="token"):
        read_token_file(alias)
