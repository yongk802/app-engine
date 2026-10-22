"""The transient unit refuses malformed command plans before exec."""

import json

import pytest

from app_engine.isolated_exec import decode_plan


def _plan(cwd):
    return {"version": 1, "argv": ["/usr/bin/python3", "server.py"],
            "cwd": str(cwd), "env": {"PORT": "18000", "APP_SECRET": "canary"}}


def test_valid_plan_keeps_exact_argv_and_environment(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    plan = decode_plan(json.dumps(_plan(workspace)).encode(), roots=(workspace,))
    assert plan.argv == ("/usr/bin/python3", "server.py")
    assert plan.env["APP_SECRET"] == "canary"


def test_plan_rejects_working_directory_symlink_escape(tmp_path):
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    private.mkdir()
    (workspace / "escape").symlink_to(private, target_is_directory=True)
    with pytest.raises(ValueError, match="working directory"):
        decode_plan(json.dumps(_plan(workspace / "escape")).encode(), roots=(workspace,))


def test_plan_rejects_oversize_and_nul_without_echoing_values(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with pytest.raises(ValueError, match="too large"):
        decode_plan(b"x" * (256 * 1024 + 1), roots=(workspace,))
    invalid = _plan(workspace)
    invalid["env"]["APP_SECRET"] = "canary\x00suffix"
    with pytest.raises(ValueError) as caught:
        decode_plan(json.dumps(invalid).encode(), roots=(workspace,))
    assert "canary" not in str(caught.value)
