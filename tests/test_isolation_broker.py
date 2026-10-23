"""The root broker accepts only a scoped app request."""

import json
import os
import socket
import struct
import subprocess
import sys
import threading
from dataclasses import replace
from unittest.mock import Mock

import pytest

pytestmark = pytest.mark.skipif(os.name == "nt", reason="Linux systemd broker")

from app_engine.isolation_broker import (
    decode_request,
    parse_tracker_capability,
    require_peer_uid,
    serve_connection,
)
from app_engine.isolation_wire import recv_event, send_request


def request(tmp_path):
    return {
        "version": 1,
        "app_id": "sample",
        "phase": "start",
        "app_root": str(tmp_path / "apps" / "sample"),
        "data_dir": str(tmp_path / "state" / "data" / "sample" / ("a" * 16)),
        "build_dir": str(tmp_path / "state" / "build" / "sample"),
        "plan": {"version": 1, "argv": ["/usr/bin/python3", "main.py"],
                 "cwd": "/workspace", "env": {"PORT": "12345"}},
    }


def test_decoder_rejects_extra_root_broker_options(tmp_path):
    payload = request(tmp_path)
    payload["unit_name"] = "app-engine.service"
    with pytest.raises(ValueError, match="shape"):
        decode_request(json.dumps(payload).encode())


def test_decoder_keeps_command_plan_in_payload(tmp_path):
    payload = request(tmp_path)
    result = decode_request(json.dumps(payload).encode())
    assert result.unit.app_id == "sample"
    assert result.unit.phase == "start"
    assert result.unit.payload == json.dumps(payload["plan"], separators=(",", ":"))


@pytest.mark.parametrize("field,value", [
    ("app_root", "relative/path"),
    ("data_dir", "/tmp/path:bad"),
    ("phase", "arbitrary"),
    ("app_id", "../secret"),
])
def test_decoder_rejects_bad_scope(tmp_path, field, value):
    payload = request(tmp_path)
    payload[field] = value
    with pytest.raises(ValueError):
        decode_request(json.dumps(payload).encode())


def test_broker_checks_kernel_peer_uid(monkeypatch):
    monkeypatch.setattr(socket, "SO_PEERCRED", 17, raising=False)
    sock = Mock(spec=socket.socket)
    sock.getsockopt.return_value = struct.pack("3i", 123, 999, 986)
    require_peer_uid(sock, 999)
    with pytest.raises(PermissionError, match="peer"):
        require_peer_uid(sock, 1000)


def test_broker_rejects_missing_peer_credential(monkeypatch):
    monkeypatch.setattr(socket, "SO_PEERCRED", 17, raising=False)
    sock = Mock(spec=socket.socket)
    sock.getsockopt.side_effect = OSError("unsupported")
    with pytest.raises(PermissionError, match="peer"):
        require_peer_uid(sock, 999)


def test_broker_streams_output_and_exact_exit(tmp_path, monkeypatch):
    monkeypatch.setattr("app_engine.isolation_broker.require_peer_uid", lambda *_: None)

    class TestBoundary:
        engine_uid = 999

        def command(self, _unit, _request, _broker_unit):
            return (sys.executable, "-c",
                    "import sys; sys.stdin.read(); sys.stdout.buffer.write(b'hi\\xff')")

    client, broker = socket.socketpair()
    errors = []

    def run():
        try:
            serve_connection(broker, TestBoundary(), "app-engine-runner@7.service")
        except Exception as exc:  # noqa: BLE001 - surface worker failure in test thread
            errors.append(exc)
        finally:
            broker.close()

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    try:
        payload = request(tmp_path)
        payload["plan"]["env"].update({f"BLOB_{index}": "x" * 15000
                                         for index in range(8)})
        send_request(client, json.dumps(payload).encode())
        events = []
        while True:
            event = recv_event(client)
            events.append(event)
            if event[0] == b"X":
                break
        assert events[0] == (b"S", b"")
        assert (b"O", b"hi\xff") in events
        assert events[-1] == (b"X", b"0")
        worker.join(timeout=2)
        assert not worker.is_alive()
        assert errors == []
    finally:
        client.close()


def test_broker_uses_boundary_prepared_payload(tmp_path, monkeypatch):
    monkeypatch.setattr("app_engine.isolation_broker.require_peer_uid", lambda *_: None)

    class TestBoundary:
        engine_uid = 999

        def prepare_request(self, request):
            return replace(request, payload='{"prepared":true}')

        def command(self, _unit, _request, _broker_unit):
            return (sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read())")

    client, broker = socket.socketpair()
    worker = threading.Thread(
        target=lambda: serve_connection(broker, TestBoundary(),
                                        "app-engine-runner@7.service"), daemon=True)
    worker.start()
    try:
        send_request(client, json.dumps(request(tmp_path)).encode())
        events = []
        while True:
            event = recv_event(client)
            events.append(event)
            if event[0] == b"X":
                break
        assert (b"O", b'{"prepared":true}') in events
        assert events[-1] == (b"X", b"0")
    finally:
        client.close()
        broker.close()
        worker.join(timeout=2)


def test_tracker_capability_policy_requires_private_files(tmp_path):
    mtls = tmp_path / "mtls"
    mtls.mkdir(mode=0o710)
    mtls.chmod(0o710)
    for name in ("ca.crt", "package-tracker-client.crt", "package-tracker-client.key"):
        file = mtls / name
        file.write_text("certificate fixture")
        file.chmod(0o640)
    carrier = tmp_path / "carrier-token"
    worker = tmp_path / "worker-token"
    for path, token in ((carrier, "a" * 64), (worker, "b" * 64)):
        path.write_text(token)
        path.chmod(0o600)
    policy = {"mtls_dir": str(mtls), "carrier_token_file": str(carrier),
              "worker_token_file": str(worker), "carrier_proxy_port": 8788,
              "browser_worker_port": 8799}
    cap = parse_tracker_capability(policy, owner_uid=os.getuid(), engine_gid=os.getgid())
    assert cap.mtls_dir == mtls

    (mtls / "package-tracker-client.key").chmod(0o644)
    with pytest.raises(ValueError, match="certificate"):
        parse_tracker_capability(policy, owner_uid=os.getuid(), engine_gid=os.getgid())
    (mtls / "package-tracker-client.key").chmod(0o640)
    with pytest.raises(ValueError, match="port"):
        parse_tracker_capability(dict(policy, carrier_proxy_port=80),
                                 owner_uid=os.getuid(), engine_gid=os.getgid())


def test_client_disconnect_stops_exact_unit(tmp_path, monkeypatch):
    monkeypatch.setattr("app_engine.isolation_broker.require_peer_uid", lambda *_: None)
    launched = []
    stopped = []
    real_popen = subprocess.Popen

    def capture_popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        launched.append(process)
        return process

    def stop_unit(name):
        stopped.append(name)
        launched[0].terminate()

    monkeypatch.setattr("app_engine.isolation_broker.subprocess.Popen", capture_popen)
    monkeypatch.setattr("app_engine.isolation_broker._stop_unit", stop_unit)

    class TestBoundary:
        engine_uid = 999

        def command(self, _unit, _request, _broker_unit):
            return (sys.executable, "-c", "import sys,time; sys.stdin.read(); time.sleep(60)")

    client, broker = socket.socketpair()
    worker = threading.Thread(
        target=lambda: serve_connection(broker, TestBoundary(),
                                        "app-engine-runner@7.service"), daemon=True)
    worker.start()
    try:
        send_request(client, json.dumps(request(tmp_path)).encode())
        assert recv_event(client) == (b"S", b"")
        client.close()
        worker.join(timeout=2)
        assert not worker.is_alive()
        assert len(stopped) == 1
        assert stopped[0].startswith("ae-sample-start-")
        assert launched[0].poll() is not None
    finally:
        client.close()
        broker.close()
        for process in launched:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=2)
