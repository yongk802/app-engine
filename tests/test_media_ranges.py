from fastapi.testclient import TestClient
import mimetypes

from test_security import load_engine


def test_script_mime_types_do_not_depend_on_windows_file_associations(tmp_path, monkeypatch):
    root = tmp_path / "apps" / "demo"
    root.mkdir(parents=True)
    (root / "app.json").write_text('{"id":"demo","label":"Demo","icon":"D"}')
    (root / "index.html").write_text("safe")
    (root / "app.mjs").write_text("export const ready = true;")
    (root.parent / "bridge.js").write_text("console.log('bridge');")
    module = load_engine(tmp_path, monkeypatch)
    monkeypatch.setattr(mimetypes, 'guess_type', lambda value: ('text/plain', None))
    with TestClient(module.app) as client:
        for path in ('/apps/demo/app.mjs', '/apps/bridge.js'):
            response = client.get(path, headers={'host': 'demo.localhost'})
            assert response.status_code == 200
            assert response.headers['content-type'].split(';')[0] == 'text/javascript'


def test_media_supports_seeking_and_head(tmp_path, monkeypatch):
    root = tmp_path / "apps" / "demo"
    root.mkdir(parents=True)
    (root / "app.json").write_text('{"id":"demo","label":"Demo","icon":"D"}')
    (root / "index.html").write_text("safe")
    data = bytes(range(256)) * 32
    (root / "song.mp3").write_bytes(data)
    (root.parent / "shared.mp4").write_bytes(data)
    module = load_engine(tmp_path, monkeypatch)
    with TestClient(module.app) as client:
        for path in ("/apps/demo/song.mp3", "/apps/shared.mp4"):
            headers = {"host": "demo.localhost"}
            response = client.get(path, headers={**headers, "Range": "bytes=100-199"})
            assert response.status_code == 206
            assert response.content == data[100:200]
            assert response.headers["content-range"] == f"bytes 100-199/{len(data)}"
            assert response.headers["accept-ranges"] == "bytes"
            response = client.get(path, headers={**headers, "Range": "bytes=-64"})
            assert response.status_code == 206
            assert response.content == data[-64:]
            response = client.get(path, headers={**headers, "Range": "bytes=99999-"})
            assert response.status_code == 416
            response = client.head(path, headers=headers)
            assert response.status_code == 200
            assert response.content == b""
            assert int(response.headers["content-length"]) == len(data)
