from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SystemCapabilities:
    os: str
    architecture: str
    ram_bytes: int
    free_disk_bytes: int
    acceleration: str
    ollama_endpoint: str | None
    ollama_executable: str | None
    ollama_service_state: str
    installation_method: str | None


@dataclass(frozen=True)
class ModelProfile:
    profile_id: str
    model_id: str
    ollama_tag: str
    display_name: str
    expected_download_bytes: int
    minimum_ram_bytes: int
    recommended_ram_bytes: int
    minimum_free_disk_bytes: int
    context_limit: int
    architectures: tuple[str, ...]


@dataclass(frozen=True)
class ModelRecommendation:
    profile: ModelProfile
    reason: str
    warnings: tuple[str, ...]
    override_safe: bool


@dataclass(frozen=True)
class LocalAIConfig:
    schema_version: int = 1
    selected_profile: str = "quality"
    ollama_endpoint: str = "http://127.0.0.1:11434"
    managed_model_ids: tuple[str, ...] = ()
    completed_setup_version: int = 0
