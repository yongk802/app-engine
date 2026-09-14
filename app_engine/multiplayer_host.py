"""Host side of multiplayer: one room-service process per app, and the relay to it.

An app that declares ``"multiplayer": {"rules": ...}`` in its manifest plays through
the host. The first command for that app starts ``multiplayer/server.mjs`` (Node)
with the app's rules module, a private storage directory under the state root, and
an ephemeral loopback port; the process is health-checked, its output kept for
diagnostics, and it is restarted if it dies. Commands are relayed as-is, so every
client keeps the ``/v1/command`` wire contract, and the browser never learns the
service's port.

``APP_ENGINE_MULTIPLAYER_SERVER`` points an installation at an already-running
service instead (one URL for every app, or a JSON object keyed by app id), which
is how two machines share a table server over a LAN or VPN.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import httpx

MAX_BODY = 64 * 1024
MAX_REPLY = 2 * 1024 * 1024
HEALTH_TIMEOUT = 15.0
COMMAND_TIMEOUT = 15.0
_LISTENING = re.compile(r"listening on ([^:\s]+):(\d+)")
_APP_ID = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def package_dir() -> Path:
    """Where the Node room service lives inside the installed package."""
    return Path(__file__).resolve().parent / "multiplayer"


def failure(code: str, message: str) -> dict:
    return {"ok": False, "error": {"code": code, "message": message}}


class MultiplayerUnavailable(Exception):
    """The room service for this app cannot be reached; the app stays playable solo."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


@dataclass
class RoomProcess:
    app_id: str
    rules: Path
    ai: Path | None
    data_dir: Path
    process: asyncio.subprocess.Process | None = None
    base_url: str = ""
    logs: deque = field(default_factory=lambda: deque(maxlen=200))
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def alive(self) -> bool:
        return self.process is not None and self.process.returncode is None and bool(self.base_url)


class MultiplayerHost:
    def __init__(
        self,
        state_root: Path,
        *,
        node: str | None = None,
        external: str | None = None,
        package: Path | None = None,
    ):
        self.state_root = Path(state_root)
        self.node = node
        self.package = package or package_dir()
        self.external = self._parse_external(external if external is not None else os.environ.get("APP_ENGINE_MULTIPLAYER_SERVER", ""))
        self._processes: dict[str, RoomProcess] = {}
        self._client: httpx.AsyncClient | None = None

    # ── configuration ────────────────────────────────────────────────────────

    @staticmethod
    def _parse_external(raw: str) -> dict[str, str] | str | None:
        value = (raw or "").strip()
        if not value:
            return None
        if value.startswith("{"):
            try:
                mapping = json.loads(value)
            except json.JSONDecodeError:
                return None
            return {str(k): str(v).rstrip("/") for k, v in mapping.items()} if isinstance(mapping, dict) else None
        return value.rstrip("/")

    def external_url(self, app_id: str) -> str | None:
        if isinstance(self.external, dict):
            return self.external.get(app_id)
        return self.external

    def data_dir(self, app_id: str) -> Path:
        """The single seam for multiplayer storage. Rooms, tournaments and the friend
        roster are per app today; a host-wide roster later is a change here alone."""
        return self.state_root / "multiplayer" / app_id

    def client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            # A fixed loopback destination: no redirects, no proxy environment, bounded time.
            self._client = httpx.AsyncClient(follow_redirects=False, trust_env=False, timeout=COMMAND_TIMEOUT)
        return self._client

    # ── process lifecycle ────────────────────────────────────────────────────

    async def ensure(self, app_id: str, app_root: Path, spec: dict) -> str:
        """Return the base URL of a healthy room service for the app, starting it if needed."""
        if not _APP_ID.match(app_id):
            raise MultiplayerUnavailable("Unknown app.")
        external = self.external_url(app_id)
        if external:
            return external
        rules = (Path(app_root) / spec["rules"]).resolve()
        ai = (Path(app_root) / spec["ai"]).resolve() if spec.get("ai") else None
        record = self._processes.get(app_id)
        if record is None:
            record = RoomProcess(app_id=app_id, rules=rules, ai=ai, data_dir=self.data_dir(app_id))
            self._processes[app_id] = record
        async with record.lock:
            if record.alive:
                return record.base_url
            await self._start(record)
            return record.base_url

    async def _start(self, record: RoomProcess) -> None:
        node = self.node or shutil.which("node")
        if not node:
            raise MultiplayerUnavailable("Multiplayer needs Node.js 18 or newer on this computer.")
        if not record.rules.is_file():
            raise MultiplayerUnavailable(f"The rules module {record.rules.name} is missing from the app.")
        if record.ai is not None and not record.ai.is_file():
            raise MultiplayerUnavailable(f"The AI module {record.ai.name} is missing from the app.")
        record.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        argv = [node, str(self.package / "server.mjs"), "--rules", str(record.rules), "--data-dir", str(record.data_dir), "--host", "127.0.0.1", "--port", "0"]
        if record.ai is not None:
            argv += ["--ai", str(record.ai)]
        record.base_url = ""
        record.logs.append(f"starting: {' '.join(argv)}")
        try:
            record.process = await asyncio.create_subprocess_exec(
                *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                cwd=str(record.rules.parent), env=self._environment(),
            )
        except OSError as exc:
            record.logs.append(f"spawn failed: {exc}")
            raise MultiplayerUnavailable("The room service could not be started.") from exc
        base = await self._await_listening(record)
        record.base_url = base
        asyncio.create_task(self._pump(record))
        await self._await_health(record)
        # Local tooling (a terminal-driven second seat, a smoke test) finds the service here;
        # the file is private to the user and removed when the service stops.
        address = record.data_dir / "address.json"
        address.write_text(json.dumps({"url": base, "pid": record.process.pid}))
        address.chmod(0o600)

    @staticmethod
    def _environment() -> dict[str, str]:
        keep = {"PATH", "HOME", "USERPROFILE", "SYSTEMROOT", "TMPDIR", "TEMP", "TMP", "LANG", "LC_ALL"}
        return {k: v for k, v in os.environ.items() if k in keep}

    async def _await_listening(self, record: RoomProcess) -> str:
        process = record.process
        assert process is not None and process.stdout is not None
        async def read_until_listening() -> str | None:
            while True:
                line = await process.stdout.readline()
                if not line:
                    return None
                text = line.decode("utf-8", "replace").rstrip()
                record.logs.append(text)
                match = _LISTENING.search(text)
                if match:
                    return f"http://{match.group(1)}:{match.group(2)}"

        try:
            base = await asyncio.wait_for(read_until_listening(), HEALTH_TIMEOUT)
            if base:
                return base
        except (asyncio.TimeoutError, TimeoutError):
            pass
        await self._terminate(record)
        raise MultiplayerUnavailable("The room service did not start. " + (record.logs[-1] if record.logs else ""))

    async def _await_health(self, record: RoomProcess) -> None:
        deadline = asyncio.get_running_loop().time() + HEALTH_TIMEOUT
        while asyncio.get_running_loop().time() < deadline:
            try:
                response = await self.client().get(f"{record.base_url}/health", timeout=2.0)
                if response.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.2)
        await self._terminate(record)
        raise MultiplayerUnavailable("The room service started but never became healthy.")

    async def _pump(self, record: RoomProcess) -> None:
        process = record.process
        if process is None or process.stdout is None:
            return
        while True:
            line = await process.stdout.readline()
            if not line:
                break
            record.logs.append(line.decode("utf-8", "replace").rstrip())
        code = await process.wait()
        record.logs.append(f"exited with {code}")
        if record.process is process:
            record.base_url = ""

    async def _terminate(self, record: RoomProcess) -> None:
        process = record.process
        record.base_url = ""
        (record.data_dir / "address.json").unlink(missing_ok=True)
        if process is None or process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), 5.0)
        except (asyncio.TimeoutError, TimeoutError):
            process.kill()
            await process.wait()

    async def close(self) -> None:
        for record in self._processes.values():
            async with record.lock:
                await self._terminate(record)
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    def logs(self, app_id: str) -> list[str]:
        record = self._processes.get(app_id)
        return list(record.logs) if record else []

    # ── relay ────────────────────────────────────────────────────────────────

    async def relay(self, base_url: str, body: bytes, credential: str | None) -> tuple[int, dict]:
        """POST one command to the service. Only the credential header crosses; never cookies."""
        headers = {"Content-Type": "application/json"}
        if credential:
            if len(credential) > 256 or not re.fullmatch(r"[A-Za-z0-9_-]+", credential):
                return 401, failure("AUTH_REQUIRED", "Invalid credential.")
            headers["Authorization"] = f"Bearer {credential}"
        try:
            response = await self.client().post(f"{base_url}/v1/command", content=body, headers=headers)
            if len(response.content) > MAX_REPLY:
                raise ValueError("Oversized room response")
            value = response.json()
            if not isinstance(value, dict) or not isinstance(value.get("ok"), bool):
                raise ValueError("Unexpected room service")
            return response.status_code, value
        except (httpx.HTTPError, ValueError):
            return 503, failure("ROOM_SERVER_UNAVAILABLE", "The table server is unavailable. Reconnect when it is running; your saved match is kept there.")

    async def health(self, base_url: str) -> tuple[int, dict]:
        try:
            response = await self.client().get(f"{base_url}/health", timeout=5.0)
            return response.status_code, response.json()
        except (httpx.HTTPError, ValueError):
            return 503, failure("ROOM_SERVER_UNAVAILABLE", "The table server is unavailable.")
