"""Root-side parsing for requests to the isolated command runner.

Only the fixed isolated executor is started on the host. App-supplied command
arguments travel on its standard input after systemd applies the unit policy.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import selectors
import socket
import stat
import struct
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .isolated_units import UnitBoundary, UnitRequest
from .isolation_wire import MAX_REQUEST_BYTES, recv_request, send_event

_REQUEST_FIELDS = {"version", "app_id", "phase", "app_root", "data_dir",
                   "build_dir", "plan"}
_SAFE_APP_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}\Z")
_CONFIG_PATH = Path("/etc/app-engine-runner.json")
_MAX_COMMAND_OUTPUT_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class BrokerRequest:
    unit: UnitRequest


def require_peer_uid(sock: socket.socket, expected_uid: int) -> None:
    """Authenticate a Unix socket peer using Linux kernel credentials."""
    option = getattr(socket, "SO_PEERCRED", None)
    if option is None:
        raise PermissionError("kernel peer credentials unavailable")
    try:
        raw = sock.getsockopt(socket.SOL_SOCKET, option, struct.calcsize("3i"))
        _pid, uid, _gid = struct.unpack("3i", raw)
    except (OSError, struct.error) as exc:
        raise PermissionError("kernel peer credentials unavailable") from exc
    if uid != expected_uid:
        raise PermissionError("unexpected broker peer UID")


def decode_request(raw: bytes) -> BrokerRequest:
    if not raw or len(raw) > MAX_REQUEST_BYTES:
        raise ValueError("invalid broker request size")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid broker request JSON") from exc
    if (not isinstance(value, dict) or set(value) != _REQUEST_FIELDS
            or type(value["version"]) is not int or value["version"] != 1
            or not isinstance(value["app_id"], str)
            or not isinstance(value["phase"], str)
            or not isinstance(value["plan"], dict)):
        raise ValueError("invalid broker request shape")
    paths = []
    for key in ("app_root", "data_dir", "build_dir"):
        supplied = value[key]
        if (not isinstance(supplied, str) or not supplied.startswith("/")
                or len(supplied) > 4096 or any(char in supplied for char in (":", "\n", "\r", "\x00"))):
            raise ValueError("invalid broker scope path")
        paths.append(Path(supplied))
    if value["phase"] not in {"install", "build", "start"}:
        raise ValueError("invalid broker phase")
    if not _SAFE_APP_ID.fullmatch(value["app_id"]):
        raise ValueError("invalid broker app ID")
    try:
        plan = json.dumps(value["plan"], separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid broker command plan") from exc
    if len(plan.encode("utf-8")) > MAX_REQUEST_BYTES:
        raise ValueError("broker command plan too large")
    return BrokerRequest(UnitRequest(
        app_id=value["app_id"], phase=value["phase"], app_root=paths[0],
        data_dir=paths[1], build_dir=paths[2], payload=plan,
    ))


def _stop_unit(unit_name: str) -> None:
    subprocess.run(["systemctl", "stop", f"{unit_name}.service"],
                   check=False, capture_output=True, timeout=10)


def serve_connection(sock: socket.socket, boundary: UnitBoundary,
                     broker_unit_name: str) -> None:
    """Run one command; a lost client cannot leave its unit behind."""
    sock.settimeout(10)
    require_peer_uid(sock, boundary.engine_uid)
    request = decode_request(recv_request(sock)).unit
    unit_name = f"ae-{request.app_id}-{request.phase}-{secrets.token_hex(8)}"
    command = boundary.command(unit_name, request, broker_unit_name)
    process = subprocess.Popen(command, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               close_fds=True)
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None
    try:
        payload = request.payload.encode("utf-8")
        offset = 0
        os.set_blocking(process.stdin.fileno(), False)
        send_event(sock, b"S", b"")
        with selectors.DefaultSelector() as selected:
            output_bytes = 0
            selected.register(sock, selectors.EVENT_READ, "control")
            selected.register(process.stdin, selectors.EVENT_WRITE, "input")
            selected.register(process.stdout, selectors.EVENT_READ, b"O")
            selected.register(process.stderr, selectors.EVENT_READ, b"E")
            while True:
                if process.poll() is not None and len(selected.get_map()) == 1:
                    break
                for key, _ in selected.select(timeout=0.5):
                    if key.data == "control":
                        # The only client command is stop. EOF also stops.
                        if sock.recv(1) != b"T":
                            return
                        return
                    if key.data == "input":
                        try:
                            offset += os.write(key.fileobj.fileno(), payload[offset:offset + 16 * 1024])
                        except BrokenPipeError:
                            offset = len(payload)
                        if offset >= len(payload):
                            selected.unregister(key.fileobj)
                            key.fileobj.close()
                        continue
                    chunk = os.read(key.fileobj.fileno(), 16 * 1024)
                    if chunk:
                        output_bytes += len(chunk)
                        if output_bytes > _MAX_COMMAND_OUTPUT_BYTES:
                            send_event(sock, b"E", b"Isolated command output limit exceeded.\n")
                            return
                        send_event(sock, key.data, chunk)
                    else:
                        selected.unregister(key.fileobj)
        send_event(sock, b"X", str(process.returncode).encode("ascii"))
    finally:
        if process.poll() is None:
            _stop_unit(unit_name)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def load_boundary(path: Path = _CONFIG_PATH) -> UnitBoundary:
    """Read only a root-owned, non-writable runner policy file."""
    if os.name != "posix":
        raise ValueError("isolated broker requires Unix peer credentials")
    import pwd

    if path.is_symlink():
        raise ValueError("runner policy is a symlink")
    status = path.stat()
    if (not stat.S_ISREG(status.st_mode) or status.st_uid != 0
            or status.st_mode & 0o022):
        raise ValueError("runner policy must be a root-owned regular file")
    raw = path.read_bytes()
    if not raw or len(raw) > 64 * 1024:
        raise ValueError("invalid runner policy size")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("invalid runner policy JSON") from exc
    if (not isinstance(value, dict)
            or set(value) != {"apps_roots", "state_root", "engine_user", "engine_group"}
            or not isinstance(value["apps_roots"], list)
            or not 1 <= len(value["apps_roots"]) <= 8
            or any(not isinstance(item, str) or not item.startswith("/")
                   for item in value["apps_roots"])
            or not isinstance(value["state_root"], str)
            or not value["state_root"].startswith("/")
            or not isinstance(value["engine_user"], str)
            or not isinstance(value["engine_group"], str)):
        raise ValueError("invalid runner policy shape")
    user = pwd.getpwnam(value["engine_user"])
    return UnitBoundary(tuple(Path(item) for item in value["apps_roots"]),
                        Path(value["state_root"]), user.pw_uid,
                        value["engine_group"])


def main() -> int:
    if os.geteuid() != 0 or len(sys.argv) != 2:
        return 78
    try:
        boundary = load_boundary()
    except (OSError, KeyError, ValueError):
        return 78
    with socket.socket(fileno=0) as conn:
        try:
            serve_connection(conn, boundary, sys.argv[1])
        except (PermissionError, ValueError):
            try:
                send_event(conn, b"X", b"64")
            except OSError:
                pass
            return 64
        except (OSError, subprocess.SubprocessError):
            try:
                send_event(conn, b"X", b"70")
            except OSError:
                pass
            return 70
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
