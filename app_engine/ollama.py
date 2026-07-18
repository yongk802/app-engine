from __future__ import annotations

import hashlib
import json
import secrets
import subprocess
import time
from dataclasses import dataclass
from typing import AsyncIterator, Callable

import httpx

from .config import ConfigStore
from .registry import InvalidRegistryError, ModelRegistry


class ConfirmationError(ValueError):
    pass


class UnmanagedModelError(ValueError):
    pass


class OllamaOperationError(RuntimeError):
    pass


@dataclass(frozen=True)
class InstallationPlan:
    plan_id: str
    os_name: str
    official_source: str
    argv: tuple[str, ...]
    effect: str
    expires_at: float


@dataclass(frozen=True)
class OllamaStatus:
    running: bool
    version: str | None
    installed_model_ids: tuple[str, ...]
    installed_models: tuple[dict, ...]


@dataclass(frozen=True)
class ModelProgress:
    status: str
    completed: int
    total: int
    percent: int


@dataclass(frozen=True)
class VerificationResult:
    ok: bool
    sample: str


class OllamaManager:
    def __init__(self, registry: ModelRegistry, config: ConfigStore,
                 client: httpx.AsyncClient | None = None, os_name: str = "linux",
                 clock: Callable[[], float] = time.time,
                 launcher: Callable[[tuple[str, ...]], None] | None = None):
        self.registry = registry
        self.config = config
        self.clock = clock
        self.os_name = os_name
        endpoint = config.load().ollama_endpoint
        self.client = client or httpx.AsyncClient(base_url=endpoint, timeout=httpx.Timeout(120.0))
        self._owns_client = client is None
        self.launcher = launcher or (lambda argv: subprocess.Popen(argv, close_fds=True))
        self._plans: dict[str, InstallationPlan] = {}
        self._tokens: dict[str, tuple[str, float]] = {}

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def status(self) -> OllamaStatus:
        try:
            version_resp = await self.client.get("/api/version", timeout=2.0)
            version_resp.raise_for_status()
            tags_resp = await self.client.get("/api/tags", timeout=3.0)
            tags_resp.raise_for_status()
            models = tuple(tags_resp.json().get("models", []))
        except (httpx.HTTPError, ValueError):
            return OllamaStatus(False, None, (), ())
        installed: list[str] = []
        names = {str(m.get("name", "")).split("@", 1)[0] for m in models}
        for profile in self.registry.profiles:
            if profile.ollama_tag in names or any(name.startswith(profile.ollama_tag + ":") for name in names):
                installed.append(profile.model_id)
        return OllamaStatus(True, version_resp.json().get("version"), tuple(installed), models)

    def installation_plan(self) -> InstallationPlan:
        source = "https://ollama.com/download"
        argv_by_os = {
            "macos": ("open", source),
            "windows": ("cmd", "/c", "start", "", source),
            "linux": ("xdg-open", "https://ollama.com/download/linux"),
        }
        if self.os_name not in argv_by_os:
            raise OllamaOperationError(f"unsupported platform: {self.os_name}")
        expires = self.clock() + 600
        raw = f"{self.os_name}|{source}|{expires}".encode()
        plan_id = hashlib.sha256(raw).hexdigest()[:20]
        plan = InstallationPlan(plan_id, self.os_name, source, argv_by_os[self.os_name],
                                "Open the official Ollama installer for this platform.", expires)
        self._plans[plan_id] = plan
        return plan

    def authorize(self, plan_id: str) -> str:
        plan = self._plans.get(plan_id)
        if not plan or plan.expires_at < self.clock():
            raise ConfirmationError("installation plan expired")
        token = secrets.token_urlsafe(24)
        self._tokens[token] = (plan_id, self.clock() + 120)
        return token

    def install(self, plan_id: str, token: str) -> None:
        record = self._tokens.pop(token, None)
        plan = self._plans.get(plan_id)
        if not record or record[0] != plan_id or record[1] < self.clock() or not plan or plan.expires_at < self.clock():
            raise ConfirmationError("valid confirmation required")
        self.launcher(plan.argv)

    async def pull(self, model_id: str) -> AsyncIterator[ModelProgress]:
        profile = self.registry.get_model(model_id)
        try:
            async with self.client.stream("POST", "/api/pull", json={"model": profile.ollama_tag, "stream": True}) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    total = int(event.get("total") or 0)
                    completed = int(event.get("completed") or (total if event.get("status") == "success" else 0))
                    percent = round(completed * 100 / total) if total else (100 if event.get("status") == "success" else 0)
                    yield ModelProgress(str(event.get("status", "downloading")), completed, total, percent)
        except httpx.HTTPError as exc:
            raise OllamaOperationError(f"model download failed: {exc}") from exc
        self.config.mark_managed(model_id)

    async def remove_managed_model(self, model_id: str) -> None:
        if model_id not in self.config.load().managed_model_ids:
            raise UnmanagedModelError("app-engine did not install this model")
        profile = self.registry.get_model(model_id)
        response = await self.client.request("DELETE", "/api/delete", json={"model": profile.ollama_tag})
        response.raise_for_status()
        self.config.unmark_managed(model_id)

    async def verify(self, model_id: str) -> VerificationResult:
        try:
            profile = self.registry.get_model(model_id)
        except InvalidRegistryError:
            raise
        response = await self.client.post("/api/chat", json={
            "model": profile.ollama_tag,
            "messages": [{"role": "user", "content": "Reply with one short sentence saying local tutoring is ready."}],
            "stream": False,
        }, timeout=120.0)
        response.raise_for_status()
        sample = str(response.json().get("message", {}).get("content", "")).strip()
        return VerificationResult(bool(sample), sample)
