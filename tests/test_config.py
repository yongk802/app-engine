import json

import pytest

from app_engine.config import ConfigStore, InvalidEndpointError


def test_defaults_to_quality_and_loopback(tmp_path):
    config = ConfigStore(tmp_path).load()
    assert config.selected_profile == "quality"
    assert config.ollama_endpoint == "http://127.0.0.1:11434"


def test_round_trips_profile_and_managed_models(tmp_path):
    store = ConfigStore(tmp_path)
    store.set_profile("compatibility")
    store.mark_managed("small-tutor")
    loaded = store.load()
    assert loaded.selected_profile == "compatibility"
    assert loaded.managed_model_ids == ("small-tutor",)


def test_rejects_non_loopback_endpoint(tmp_path):
    with pytest.raises(InvalidEndpointError):
        ConfigStore(tmp_path).set_endpoint("http://example.com:11434")


def test_quarantines_corrupt_config(tmp_path):
    (tmp_path / "local-ai.json").write_text("not json")
    loaded = ConfigStore(tmp_path).load()
    assert loaded.selected_profile == "quality"
    assert list(tmp_path.glob("local-ai.corrupt-*.json"))


def test_config_write_is_valid_json(tmp_path):
    ConfigStore(tmp_path).set_profile("compatibility")
    assert json.loads((tmp_path / "local-ai.json").read_text())["schema_version"] == 1
