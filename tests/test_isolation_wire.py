"""Broker framing is bounded and does not interpret command output."""

import socket
import struct

import pytest

from app_engine.isolation_wire import recv_request, send_event


def test_request_frame_is_bounded_before_receiving_body():
    client, broker = socket.socketpair()
    try:
        client.sendall(struct.pack("!I", 256 * 1024 + 1))
        with pytest.raises(ValueError, match="too large"):
            recv_request(broker)
    finally:
        client.close()
        broker.close()


def test_truncated_request_fails_closed():
    client, broker = socket.socketpair()
    try:
        client.sendall(struct.pack("!I", 8) + b"short")
        client.shutdown(socket.SHUT_WR)
        with pytest.raises(ValueError, match="truncated"):
            recv_request(broker)
    finally:
        client.close()
        broker.close()


def test_output_frame_preserves_binary_bytes():
    client, broker = socket.socketpair()
    try:
        send_event(broker, b"O", b"\x00\xffsecret\n")
        assert client.recv(5) == b"O" + struct.pack("!I", 9)
        assert client.recv(9) == b"\x00\xffsecret\n"
    finally:
        client.close()
        broker.close()
