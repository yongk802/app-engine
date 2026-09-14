import runpy

import pytest
import uvicorn


def _capture(monkeypatch):
    called = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, host, port, **options: called.update(host=host, port=port, **options))
    return called


def test_executable_startup_uses_local_ai_configuration(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("APP_ENGINE_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("APP_ENGINE_PUBLIC_ORIGIN", raising=False)
    monkeypatch.delenv("APP_ENGINE_TRUST_PROXY", raising=False)
    called = _capture(monkeypatch)
    runpy.run_path("engine.py", run_name="__main__")
    output = capsys.readouterr().out
    assert "Local AI" in output
    assert "http://127.0.0.1:11434" in output
    assert called == {"host": "127.0.0.1", "port": 8770, "proxy_headers": False, "forwarded_allow_ips": None}


def test_public_bind_is_refused_without_public_origin_mode(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_ENGINE_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("APP_ENGINE_HOST", "0.0.0.0")
    monkeypatch.delenv("APP_ENGINE_PUBLIC_ORIGIN", raising=False)
    called = _capture(monkeypatch)
    with pytest.raises(SystemExit, match="administrator"):
        runpy.run_path("engine.py", run_name="__main__")
    assert called == {}, "uvicorn never started"


def test_public_origin_mode_binds_behind_a_trusted_proxy(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("APP_ENGINE_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("APP_ENGINE_HOST", "0.0.0.0")
    monkeypatch.setenv("APP_ENGINE_PUBLIC_ORIGIN", "https://play.example.com")
    monkeypatch.setenv("APP_ENGINE_TRUST_PROXY", "1")
    monkeypatch.delenv("APP_ENGINE_ADMIN_SECRET", raising=False)
    called = _capture(monkeypatch)
    runpy.run_path("engine.py", run_name="__main__")
    assert called["host"] == "0.0.0.0" and called["proxy_headers"] is True and called["forwarded_allow_ips"] == "127.0.0.1,::1"
    assert (tmp_path / "players.json").is_file(), "the minted admin secret is stored as a hash before the engine listens"
