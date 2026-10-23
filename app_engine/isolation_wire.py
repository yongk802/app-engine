"""Bounded binary frames for the local command broker."""

from __future__ import annotations

import socket
import struct

MAX_REQUEST_BYTES = 256 * 1024
MAX_EVENT_BYTES = 64 * 1024


def _read_exact(sock: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise ValueError("truncated broker frame")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def recv_request(sock: socket.socket) -> bytes:
    size = struct.unpack("!I", _read_exact(sock, 4))[0]
    if size == 0 or size > MAX_REQUEST_BYTES:
        raise ValueError("broker request too large or empty")
    return _read_exact(sock, size)


def send_event(sock: socket.socket, kind: bytes, payload: bytes) -> None:
    if kind not in {b"S", b"O", b"E", b"X"}:
        raise ValueError("invalid broker event")
    if len(payload) > MAX_EVENT_BYTES:
        raise ValueError("broker event too large")
    sock.sendall(kind + struct.pack("!I", len(payload)) + payload)
