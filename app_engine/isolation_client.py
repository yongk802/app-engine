"""Async app-engine client for the privileged local command broker."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import struct
from pathlib import Path, PurePosixPath
from typing import Any

from .isolation_wire import MAX_EVENT_BYTES, MAX_REQUEST_BYTES

BROKER_SOCKET = "/run/app-engine-runner.sock"


def _unit_path(path: Path, app_root: Path, data_dir: Path,
               build_dir: Path) -> PurePosixPath | None:
    for host_root, unit_root in ((app_root, PurePosixPath("/workspace")),
                                 (data_dir, PurePosixPath("/app-data")),
                                 (build_dir, PurePosixPath("/app-build"))):
        try:
            suffix = path.relative_to(host_root)
        except ValueError:
            continue
        return unit_root / suffix
    return None


def command_request(*, app_id: str, phase: str, app_root: Path,
                    data_dir: Path, build_dir: Path, executable: Path,
                    argv: tuple[str, ...], cwd: Path,
                    env: dict[str, str]) -> dict[str, Any]:
    """Translate lifecycle host paths into the unit's three scoped mounts."""
    unit_cwd = _unit_path(cwd, app_root, data_dir, build_dir)
    if unit_cwd is None:
        raise ValueError("command cwd is outside isolated roots")
    unit_executable = _unit_path(executable, app_root, data_dir, build_dir)
    arguments = [str(unit_executable or executable)]
    for argument in argv[1:]:
        candidate = Path(argument)
        mapped = (_unit_path(candidate, app_root, data_dir, build_dir)
                  if candidate.is_absolute() else None)
        arguments.append(str(mapped) if mapped else argument)
    environment = dict(env)
    environment["HOME"] = "/app-data"
    environment["XDG_CACHE_HOME"] = "/app-data/.cache"
    environment["UV_CACHE_DIR"] = "/app-data/.uv-cache"
    if "APP_DATA_DIR" in environment:
        environment["APP_DATA_DIR"] = "/app-data"
    if "APP_BUILD_DIR" in environment:
        environment["APP_BUILD_DIR"] = "/app-build"
    return {
        "version": 1, "app_id": app_id, "phase": phase,
        "app_root": str(app_root), "data_dir": str(data_dir),
        "build_dir": str(build_dir),
        "plan": {"version": 1, "argv": arguments, "cwd": str(unit_cwd),
                 "env": environment},
    }


def _require_root_peer(writer: asyncio.StreamWriter) -> None:
    option = getattr(socket, "SO_PEERCRED", None)
    peer = writer.get_extra_info("socket")
    if option is None or peer is None:
        raise RuntimeError("broker kernel credentials unavailable")
    try:
        raw = peer.getsockopt(socket.SOL_SOCKET, option, struct.calcsize("3i"))
        _pid, uid, _gid = struct.unpack("3i", raw)
    except (OSError, struct.error) as exc:
        raise RuntimeError("broker kernel credentials unavailable") from exc
    if uid != 0:
        raise RuntimeError("broker is not root owned")


class IsolatedProcess:
    """Subset of asyncio Process used by the managed-app lifecycle."""

    def __init__(self, reader: asyncio.StreamReader,
                 writer: asyncio.StreamWriter) -> None:
        loop = asyncio.get_running_loop()
        self._reader = reader
        self._writer = writer
        self._started: asyncio.Future[None] = loop.create_future()
        self._finished: asyncio.Future[int] = loop.create_future()
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.returncode: int | None = None
        self._pump_task = asyncio.create_task(self._pump())

    @classmethod
    async def start(cls, request: dict[str, Any]) -> IsolatedProcess:
        payload = json.dumps(request, separators=(",", ":"),
                             ensure_ascii=False, allow_nan=False).encode("utf-8")
        if not payload or len(payload) > MAX_REQUEST_BYTES:
            raise ValueError("isolated command request too large")
        if os.name != "posix" or not hasattr(asyncio, "open_unix_connection"):
            raise RuntimeError("isolated broker requires Unix sockets")
        try:
            reader, writer = await asyncio.open_unix_connection(BROKER_SOCKET)
        except NotImplementedError as exc:
            raise RuntimeError("isolated broker requires Unix sockets") from exc
        try:
            _require_root_peer(writer)
            writer.write(struct.pack("!I", len(payload)) + payload)
            await writer.drain()
            process = cls(reader, writer)
            await asyncio.wait_for(process._started, timeout=10)
            return process
        except BaseException:
            writer.close()
            await writer.wait_closed()
            raise

    async def _pump(self) -> None:
        try:
            while True:
                header = await self._reader.readexactly(5)
                kind, size = header[:1], struct.unpack("!I", header[1:])[0]
                if size > MAX_EVENT_BYTES or kind not in {b"S", b"O", b"E", b"X"}:
                    raise ValueError("invalid broker event")
                payload = await self._reader.readexactly(size)
                if kind == b"S":
                    if self._started.done() or payload:
                        raise ValueError("invalid broker start event")
                    self._started.set_result(None)
                elif kind in {b"O", b"E"}:
                    if not self._started.done():
                        raise ValueError("broker output before start")
                    (self.stdout if kind == b"O" else self.stderr).feed_data(payload)
                else:
                    if not self._started.done() or not payload or len(payload) > 5:
                        raise ValueError("invalid broker exit event")
                    code = int(payload.decode("ascii"))
                    if not -255 <= code <= 255:
                        raise ValueError("invalid broker exit code")
                    self.returncode = code
                    break
        except (asyncio.IncompleteReadError, UnicodeError, ValueError):
            if self.returncode is None:
                self.returncode = 70
        finally:
            if not self._started.done():
                self._started.set_exception(RuntimeError("isolated broker did not start command"))
            self.stdout.feed_eof()
            self.stderr.feed_eof()
            if not self._finished.done():
                self._finished.set_result(self.returncode if self.returncode is not None else 70)
            self._writer.close()

    async def wait(self) -> int:
        # Preparation timeouts cancel communicate(); that must not cancel the
        # process-completion signal needed by stop() and broker cleanup.
        return await asyncio.shield(self._finished)

    async def communicate(self) -> tuple[bytes, bytes]:
        stdout, stderr, _code = await asyncio.gather(
            self.stdout.read(), self.stderr.read(), self.wait())
        return stdout, stderr

    async def stop(self) -> None:
        if self.returncode is not None:
            return
        self._writer.write(b"T")
        try:
            await self._writer.drain()
            await asyncio.wait_for(self.wait(), timeout=15)
        finally:
            self._writer.close()
            await self._writer.wait_closed()
