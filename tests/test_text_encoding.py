import json
from pathlib import Path

from fastapi.testclient import TestClient

from test_security import load_engine


def test_launcher_and_unicode_manifest_ignore_windows_locale(tmp_path, monkeypatch):
    root = tmp_path / 'apps' / 'demo'
    root.mkdir(parents=True)
    (root / 'app.json').write_text(json.dumps({'id': 'demo', 'label': 'Night City — 東京', 'icon': '🎴'}, ensure_ascii=False), encoding='utf-8')
    (root / 'index.html').write_text('game', encoding='utf-8')
    module = load_engine(tmp_path, monkeypatch)
    read = Path.read_text

    def windows_read(path, encoding=None, errors=None):
        return read(path, encoding=encoding or 'cp1252', errors=errors)

    monkeypatch.setattr(Path, 'read_text', windows_read)
    with TestClient(module.app) as client:
        assert client.get('/').status_code == 200
        app = client.get('/api/apps').json()[0]
        assert app['label'] == 'Night City — 東京'
        assert app['icon'] == '🎴'
