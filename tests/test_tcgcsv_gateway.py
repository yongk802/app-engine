"""The local price gateway grants only the Cyberpunk TCG public feed paths."""

import socket
from http.client import HTTPConnection
from threading import Thread

from app_engine import tcgcsv_gateway


def test_gateway_path_allowlist():
    assert tcgcsv_gateway.allowed_path("/last-updated.txt")
    assert tcgcsv_gateway.allowed_path("/tcgplayer/categories")
    assert tcgcsv_gateway.allowed_path("/tcgplayer/92/groups")
    assert tcgcsv_gateway.allowed_path("/tcgplayer/92/123/products")
    for path in (
        "/", "/tcgplayer/91/groups", "/tcgplayer/92/0/prices",
        "/tcgplayer/92/1/../../admin", "/tcgplayer/92/1/products?url=x",
        "//localhost/private", "/tcgplayer/92/1/products/", "/health?x=1",
    ):
        assert not tcgcsv_gateway.allowed_path(path), path


def test_gateway_http_rejects_unlisted_paths_and_methods(monkeypatch):
    requests = []

    def fetch(path):
        requests.append(path)
        return b'{"success":true,"results":[]}'

    monkeypatch.setattr(tcgcsv_gateway, "fetch", fetch)
    server = tcgcsv_gateway.make_server(0)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        def request(method, path):
            connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
            connection.request(method, path)
            response = connection.getresponse()
            result = response.status, response.read()
            connection.close()
            return result

        assert request("GET", "/health")[0] == 200
        assert request("POST", "/tcgplayer/categories")[0] == 405
        assert request("GET", "/tcgplayer/92/1/products?other=1")[0] == 404
        assert request("GET", "/tcgplayer/categories") == (200, b'{"success":true,"results":[]}')
        assert requests == ["/tcgplayer/categories"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_idle_local_connection_does_not_block_health():
    server = tcgcsv_gateway.make_server(0)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    idle = socket.create_connection(("127.0.0.1", server.server_port), timeout=2)
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        connection.request("GET", "/health")
        response = connection.getresponse()
        assert response.status == 200
        assert response.read() == b"ok"
        connection.close()
    finally:
        idle.close()
        server.shutdown()
        server.server_close()
        thread.join()
