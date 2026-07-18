from __future__ import annotations

import json
import os
import time
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlparse

from .models import LocalAIConfig


class InvalidEndpointError(ValueError):
    pass


def validate_loopback_endpoint(endpoint: str) -> str:
    parsed = urlparse(endpoint)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise InvalidEndpointError("Ollama endpoint must be on this computer")
    return endpoint.rstrip("/")


class ConfigStore:
    def __init__(self, state_dir: Path):
        self.state_dir = Path(state_dir)
        self.path = self.state_dir / "local-ai.json"

    def load(self) -> LocalAIConfig:
        if not self.path.exists():
            return LocalAIConfig()
        try:
            data = json.loads(self.path.read_text())
            return LocalAIConfig(
                schema_version=int(data.get("schema_version", 1)),
                selected_profile=data.get("selected_profile", "quality"),
                ollama_endpoint=validate_loopback_endpoint(data.get("ollama_endpoint", "http://127.0.0.1:11434")),
                managed_model_ids=tuple(data.get("managed_model_ids", [])),
                completed_setup_version=int(data.get("completed_setup_version", 0)),
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError, InvalidEndpointError):
            quarantine = self.state_dir / f"local-ai.corrupt-{int(time.time())}.json"
            try:
                self.path.replace(quarantine)
            except OSError:
                pass
            return LocalAIConfig()

    def _write(self, config: LocalAIConfig) -> LocalAIConfig:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(f".tmp-{os.getpid()}")
        data = asdict(config)
        data["managed_model_ids"] = list(config.managed_model_ids)
        temp.write_text(json.dumps(data, indent=2))
        temp.replace(self.path)
        return config

    def set_profile(self, profile_id: str) -> LocalAIConfig:
        current = self.load()
        return self._write(LocalAIConfig(**{**asdict(current), "selected_profile": profile_id}))

    def set_endpoint(self, endpoint: str) -> LocalAIConfig:
        current = self.load()
        return self._write(LocalAIConfig(**{**asdict(current), "ollama_endpoint": validate_loopback_endpoint(endpoint)}))

    def mark_managed(self, model_id: str) -> LocalAIConfig:
        current = self.load()
        managed = tuple(sorted(set(current.managed_model_ids) | {model_id}))
        return self._write(LocalAIConfig(**{**asdict(current), "managed_model_ids": managed}))

    def unmark_managed(self, model_id: str) -> LocalAIConfig:
        current = self.load()
        managed = tuple(x for x in current.managed_model_ids if x != model_id)
        return self._write(LocalAIConfig(**{**asdict(current), "managed_model_ids": managed}))
