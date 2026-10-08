import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from test_assistant import engine_client, key, rpc  # noqa: F401


def test_declared_backend_tool_is_lazy_and_executes_real_http(engine_client):
    module, client, admin = engine_client
    received = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append((self.path, json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
            data = json.dumps({'note': received[-1][1]['text']}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        def log_message(self, *_):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    root = module.APPS_DIR / 'backend'
    root.mkdir()
    (root / 'app.json').write_text(json.dumps({'id': 'backend', 'label': 'Backend', 'icon': 'B', 'entry_point': f'http://127.0.0.1:{server.server_port}'}))
    declaration = {'schema_version': 1, 'tools': [{'name': 'add_note', 'description': 'Add a note', 'inputSchema': {'type': 'object', 'properties': {'text': {'type': 'string'}}, 'required': ['text'], 'additionalProperties': False}, 'path': '/api/notes'}]}
    (root / 'app-tools.json').write_text(json.dumps(declaration))
    try:
        assert client.post('/api/app-engine/catalog/refresh', headers=admin).status_code == 200
        token = key(client, admin)
        tools = rpc(client, token, 'tools/list').json()['result']['tools']
        name = next(t['name'] for t in tools if t['description'] == 'Add a note')
        assert not received
        bad = rpc(client, token, 'tools/call', {'name': name, 'arguments': {'text': 42}}).json()['result']
        assert bad['isError']
        assert not received
        good = rpc(client, token, 'tools/call', {'name': name, 'arguments': {'text': 'from the harness'}}).json()['result']
        assert not good.get('isError'), good
        assert good['structuredContent'] == {'note': 'from the harness'}
        assert received == [('/api/notes', {'text': 'from the harness'})]
        assert not module._app_runtime.gateway._sessions
        declaration['tools'][0]['path'] = '/api/../admin'
        (root / 'app-tools.json').write_text(json.dumps(declaration))
        tools = rpc(client, token, 'tools/list').json()['result']['tools']
        assert name not in [t['name'] for t in tools]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_state_symlinks_and_unknown_apps_are_refused(engine_client, tmp_path):
    module, client, admin = engine_client
    token = key(client, admin)
    external = tmp_path / 'private.json'
    external.write_text('{"secret": 1}')
    (module.STATE_DIR / 'notes.json').symlink_to(external)
    for name, arguments in [('apps_read_state', {'app_id': 'notes'}), ('apps_open', {'app_id': 'missing'})]:
        result = rpc(client, token, 'tools/call', {'name': name, 'arguments': arguments}).json()['result']
        assert result['isError']
        assert 'secret' not in json.dumps(result)


def test_managed_tool_requires_approval_and_idle_releases_process(engine_client):
    import sys
    import time
    module, client, admin = engine_client
    root = module.APPS_DIR / 'managed'
    root.mkdir()
    (root / 'server.py').write_text('''import json, os
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer
Path('started').write_text('yes')
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b'ok')
    def do_POST(self):
        self.rfile.read(int(self.headers['Content-Length']))
        self.send_response(200); self.end_headers(); self.wfile.write(b'{"done": true}')
    def log_message(self, *args): pass
HTTPServer(('127.0.0.1', int(os.environ['PORT'])), Handler).serve_forever()
''')
    runtime = {'driver': 'process', 'start': {'argv': [sys.executable, 'server.py']}, 'health': {'path': '/health', 'timeout_seconds': 3, 'interval_seconds': 0.02}, 'idle_timeout_seconds': 0.05}
    (root / 'app.json').write_text(json.dumps({'manifest_version': 2, 'id': 'managed', 'label': 'Managed', 'icon': 'M', 'default_target': 'web', 'targets': {'web': {'kind': 'web', 'runtime': runtime}}}))
    (root / 'app-tools.json').write_text(json.dumps({'schema_version': 1, 'tools': [{'name': 'act', 'description': 'Act', 'path': '/act', 'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False}}]}))
    client.post('/api/app-engine/catalog/refresh', headers=admin)
    token = key(client, admin)
    tools = rpc(client, token, 'tools/list').json()['result']['tools']
    assert 'app__managed__act' in [t['name'] for t in tools]
    assert not (root / 'started').exists()
    def call():
        return rpc(client, token, 'tools/call', {'name': 'app__managed__act', 'arguments': {}}).json()['result']
    assert call()['isError']
    assert not (root / 'started').exists()
    plan = client.post('/api/app-engine/apps/managed/preview', headers=admin, json={}).json()
    assert plan['approval_required']
    assert client.post('/api/app-engine/plans/' + plan['fingerprint'] + '/approve', headers=admin).status_code == 204
    result = call()
    assert not result['isError'], result
    assert result['structuredContent'] == {'done': True}
    assert not module._app_runtime.gateway._sessions
    deadline = time.monotonic() + 3
    while module._app_runtime.diagnostics()['active_launch_count'] and time.monotonic() < deadline:
        time.sleep(.02)
    assert module._app_runtime.diagnostics()['active_launch_count'] == 0


def test_malformed_json_is_refused(engine_client):
    _, client, admin = engine_client
    token = key(client, admin)
    for content in [b'{', b'\xff', b'{"value":NaN}']:
        response = client.post('/api/mcp', headers={'Authorization': f'Bearer {token}'}, content=content)
        assert response.status_code == 400


import pytest


@pytest.mark.parametrize('mode', ['http_error', 'oversized', 'invalid_json', 'timeout'])
def test_failed_app_calls_release_gateway_lease(engine_client, monkeypatch, mode):
    import time
    import app_engine.app_tools as implementation
    module, client, admin = engine_client
    monkeypatch.setattr(implementation, 'TOOL_TIMEOUT', .03 if mode == 'timeout' else 5, raising=False)
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            if mode == 'timeout':
                time.sleep(.15)
            body = b'x' * (257 * 1024) if mode == 'oversized' else b'bad' if mode == 'invalid_json' else b'{}'
            self.send_response(503 if mode == 'http_error' else 200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass
        def log_message(self, *_):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    root = module.APPS_DIR / 'failing'
    root.mkdir()
    (root / 'app.json').write_text(json.dumps({'id': 'failing', 'label': 'Failing', 'icon': 'F', 'entry_point': f'http://127.0.0.1:{server.server_port}'}))
    (root / 'app-tools.json').write_text(json.dumps({'schema_version': 1, 'tools': [{'name': 'act', 'description': 'Act', 'path': '/act', 'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False}}]}))
    try:
        client.post('/api/app-engine/catalog/refresh', headers=admin)
        result = rpc(client, key(client, admin), 'tools/call', {'name': 'app__failing__act', 'arguments': {}}).json()['result']
        assert result['isError'], result
        assert not module._app_runtime.gateway._sessions
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize('bad_value', ['NaN', '1e999', '"\\ud800"'])
def test_invalid_declaration_does_not_poison_tool_discovery(engine_client, bad_value):
    module, client, admin = engine_client
    root = module.APPS_DIR / 'invalid'
    root.mkdir()
    (root / 'app.json').write_text(json.dumps({'id': 'invalid', 'label': 'Invalid', 'icon': 'I', 'entry_point': 'http://127.0.0.1:9'}))
    declaration = {'schema_version': 1, 'tools': [{'name': 'act', 'description': 'Act', 'path': '/act', 'inputSchema': {'type': 'object', 'properties': {'value': {'type': 'number', 'enum': ['replace-me']}}, 'additionalProperties': False}}]}
    (root / 'app-tools.json').write_text(json.dumps(declaration).replace('"replace-me"', bad_value))
    client.post('/api/app-engine/catalog/refresh', headers=admin)
    response = rpc(client, key(client, admin), 'tools/list')
    assert response.status_code == 200
    names = [t['name'] for t in response.json()['result']['tools']]
    assert 'apps_list' in names
    assert 'app__invalid__act' not in names
