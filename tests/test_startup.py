import runpy

import uvicorn


def test_executable_startup_uses_local_ai_configuration(monkeypatch, tmp_path, capsys):
    called = {}
    monkeypatch.setenv("APP_ENGINE_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(uvicorn, "run", lambda app, host, port: called.update(host=host, port=port))
    runpy.run_path("engine.py", run_name="__main__")
    output = capsys.readouterr().out
    assert "Local AI" in output
    assert "http://127.0.0.1:11434" in output
    assert called == {"host": "127.0.0.1", "port": 8770}
