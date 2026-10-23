"""The root broker accepts only a scoped app request."""

import json

import pytest

from app_engine.isolation_broker import decode_request


def request(tmp_path):
    return {
        "version": 1,
        "app_id": "sample",
        "phase": "start",
        "app_root": str(tmp_path / "apps" / "sample"),
        "data_dir": str(tmp_path / "state" / "data" / "sample" / ("a" * 16)),
        "build_dir": str(tmp_path / "state" / "build" / "sample"),
        "plan": {"version": 1, "argv": ["/usr/bin/python3", "main.py"],
                 "cwd": "/workspace", "env": {"PORT": "12345"}},
    }


def test_decoder_rejects_extra_root_broker_options(tmp_path):
    payload = request(tmp_path)
    payload["unit_name"] = "app-engine.service"
    with pytest.raises(ValueError, match="shape"):
        decode_request(json.dumps(payload).encode())


def test_decoder_keeps_command_plan_in_payload(tmp_path):
    payload = request(tmp_path)
    result = decode_request(json.dumps(payload).encode())
    assert result.unit.app_id == "sample"
    assert result.unit.phase == "start"
    assert result.unit.payload == json.dumps(payload["plan"], separators=(",", ":"))


@pytest.mark.parametrize("field,value", [
    ("app_root", "relative/path"),
    ("data_dir", "/tmp/path:bad"),
    ("phase", "arbitrary"),
    ("app_id", "../secret"),
])
def test_decoder_rejects_bad_scope(tmp_path, field, value):
    payload = request(tmp_path)
    payload[field] = value
    with pytest.raises(ValueError):
        decode_request(json.dumps(payload).encode())
