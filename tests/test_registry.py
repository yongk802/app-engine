import json
from pathlib import Path

import pytest

from app_engine.models import SystemCapabilities
from app_engine.registry import InvalidRegistryError, ModelRegistry


def caps(ram_gb=16, disk_gb=20):
    return SystemCapabilities(
        os="macos", architecture="arm64", ram_bytes=ram_gb * 1024**3,
        free_disk_bytes=disk_gb * 1024**3, acceleration="metal",
        ollama_endpoint=None, ollama_executable=None,
        ollama_service_state="missing", installation_method="official",
    )


def test_bundled_registry_has_exactly_two_profiles():
    registry = ModelRegistry.load(Path("model-registry.json"))
    assert {m.profile_id for m in registry.profiles} == {"quality", "compatibility"}


def test_recommends_quality_for_sixteen_gb():
    result = ModelRegistry.load(Path("model-registry.json")).recommend(caps())
    assert result.profile.profile_id == "quality"
    assert result.warnings == ()


def test_recommends_compatibility_for_eight_gb():
    result = ModelRegistry.load(Path("model-registry.json")).recommend(caps(8))
    assert result.profile.profile_id == "compatibility"
    assert "memory" in result.reason.lower()


def test_warns_when_disk_is_too_small():
    result = ModelRegistry.load(Path("model-registry.json")).recommend(caps(16, 1))
    assert result.profile.profile_id == "compatibility"
    assert any("disk" in warning.lower() for warning in result.warnings)


def test_rejects_duplicate_profiles(tmp_path):
    path = tmp_path / "models.json"
    model = {"profile_id": "quality", "model_id": "a", "ollama_tag": "a:1b",
             "display_name": "A", "expected_download_bytes": 1,
             "minimum_ram_bytes": 1, "recommended_ram_bytes": 1,
             "minimum_free_disk_bytes": 1, "context_limit": 2048,
             "architectures": ["arm64", "x86_64"]}
    path.write_text(json.dumps({"version": 1, "profiles": [model, model]}))
    with pytest.raises(InvalidRegistryError):
        ModelRegistry.load(path)
