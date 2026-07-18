from __future__ import annotations

import json
from pathlib import Path

from .models import ModelProfile, ModelRecommendation, SystemCapabilities


class InvalidRegistryError(ValueError):
    pass


class ModelRegistry:
    def __init__(self, version: int, profiles: tuple[ModelProfile, ...]):
        self.version = version
        self.profiles = profiles
        self._by_id = {p.profile_id: p for p in profiles}

    @classmethod
    def load(cls, path: Path) -> "ModelRegistry":
        try:
            raw = json.loads(path.read_text())
            profiles = tuple(ModelProfile(
                profile_id=item["profile_id"], model_id=item["model_id"],
                ollama_tag=item["ollama_tag"], display_name=item["display_name"],
                expected_download_bytes=int(item["expected_download_bytes"]),
                minimum_ram_bytes=int(item["minimum_ram_bytes"]),
                recommended_ram_bytes=int(item["recommended_ram_bytes"]),
                minimum_free_disk_bytes=int(item["minimum_free_disk_bytes"]),
                context_limit=int(item["context_limit"]),
                architectures=tuple(item["architectures"]),
            ) for item in raw["profiles"])
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise InvalidRegistryError(str(exc)) from exc
        ids = [p.profile_id for p in profiles]
        model_ids = [p.model_id for p in profiles]
        if set(ids) != {"quality", "compatibility"} or len(ids) != len(set(ids)):
            raise InvalidRegistryError("registry must contain unique quality and compatibility profiles")
        if len(model_ids) != len(set(model_ids)):
            raise InvalidRegistryError("model IDs must be unique")
        return cls(int(raw.get("version", 1)), profiles)

    def get_profile(self, profile_id: str) -> ModelProfile:
        try:
            return self._by_id[profile_id]
        except KeyError as exc:
            raise InvalidRegistryError(f"unknown profile: {profile_id}") from exc

    def get_model(self, model_id: str) -> ModelProfile:
        for profile in self.profiles:
            if profile.model_id == model_id:
                return profile
        raise InvalidRegistryError(f"unknown model: {model_id}")

    def recommend(self, capabilities: SystemCapabilities) -> ModelRecommendation:
        quality = self.get_profile("quality")
        compatibility = self.get_profile("compatibility")
        warnings: list[str] = []
        if capabilities.ram_bytes >= quality.recommended_ram_bytes:
            selected = quality
            reason = "Recommended for this computer's memory."
        else:
            selected = compatibility
            reason = "The smaller model is recommended for available memory."
        if capabilities.free_disk_bytes < selected.minimum_free_disk_bytes:
            selected = compatibility
            warnings.append("Available disk space may be too low for this model download.")
        return ModelRecommendation(selected, reason, tuple(warnings), not warnings)
