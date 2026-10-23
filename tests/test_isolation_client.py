"""The engine client only accepts a bounded authenticated broker transcript."""

import asyncio
import socket
import struct
from unittest.mock import Mock

import pytest

from app_engine.isolation_client import (
    IsolatedProcess,
    _require_root_peer,
    command_request,
)
from app_engine.isolation_wire import recv_request, send_event


def test_client_streams_output_and_exit(monkeypatch):
    async def run():
        client, broker = socket.socketpair()
        monkeypatch.setattr("app_engine.isolation_client._require_root_peer", lambda *_: None)

        async def connect(_path):
            return await asyncio.open_connection(sock=client)

        monkeypatch.setattr("app_engine.isolation_client.asyncio.open_unix_connection",
                            connect)

        def serve():
            assert recv_request(broker)
            send_event(broker, b"S", b"")
            send_event(broker, b"O", b"hello\xff")
            send_event(broker, b"E", b"warning")
            send_event(broker, b"X", b"3")
            broker.close()

        server = asyncio.create_task(asyncio.to_thread(serve))
        process = await IsolatedProcess.start({"version": 1})
        stdout, stderr = await process.communicate()
        await server
        assert stdout == b"hello\xff"
        assert stderr == b"warning"
        assert process.returncode == 3

    asyncio.run(run())


def test_client_rejects_oversized_event(monkeypatch):
    async def run():
        client, broker = socket.socketpair()
        monkeypatch.setattr("app_engine.isolation_client._require_root_peer", lambda *_: None)

        async def connect(_path):
            return await asyncio.open_connection(sock=client)

        monkeypatch.setattr("app_engine.isolation_client.asyncio.open_unix_connection",
                            connect)

        def serve():
            recv_request(broker)
            broker.sendall(b"O" + struct.pack("!I", 64 * 1024 + 1))
            broker.close()

        server = asyncio.create_task(asyncio.to_thread(serve))
        with pytest.raises(RuntimeError, match="broker"):
            await IsolatedProcess.start({"version": 1})
        await server

    asyncio.run(run())


def test_client_rejects_nonroot_broker_peer(monkeypatch):
    monkeypatch.setattr(socket, "SO_PEERCRED", 17, raising=False)
    peer = Mock()
    peer.getsockopt.return_value = struct.pack("3i", 100, 999, 986)
    writer = Mock()
    writer.get_extra_info.return_value = peer
    with pytest.raises(RuntimeError, match="not root"):
        _require_root_peer(writer)


def test_command_request_maps_only_scoped_paths(tmp_path):
    app = tmp_path / "apps" / "demo"
    data = tmp_path / "state" / "data" / "demo" / ("a" * 16)
    build = tmp_path / "state" / "build" / "demo"
    request = command_request(
        app_id="demo", phase="start", app_root=app, data_dir=data,
        build_dir=build, executable=app / "bin" / "serve",
        argv=("serve", str(app / "cfg.json"), "--port", "1234"),
        cwd=app / "web", env={"APP_DATA_DIR": str(data),
                                "APP_BUILD_DIR": str(build),
                                "HOME": "/home/appengine", "PATH": "/usr/bin"})
    assert request["plan"]["argv"] == ["/workspace/bin/serve",
                                        "/workspace/cfg.json", "--port", "1234"]
    assert request["plan"]["cwd"] == "/workspace/web"
    assert request["plan"]["env"]["APP_DATA_DIR"] == "/app-data"
    assert request["plan"]["env"]["HOME"] == "/app-data"
    assert request["plan"]["env"]["PATH"] == "/usr/bin"
