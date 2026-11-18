"""Standalone user identities, scoped runtime storage and deny-by-default policy."""
import json
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from test_public_origin import PUBLIC, load_engine, public_client, sign_in

from app_engine.backend_bindings import backend_url
from app_engine.network_guard import restrict_network
from app_engine.process_isolation import IsolationMode, ProcessIsolationPolicy
from app_engine.public_origin import SESSION_COOKIE


def test_default_process_policy_requires_the_isolated_runner(monkeypatch):
    monkeypatch.delenv('APP_ENGINE_PROCESS_ISOLATION')
    assert ProcessIsolationPolicy.from_environment().mode is IsolationMode.ENFORCE


def test_network_policy_has_no_non_linux_fallback(monkeypatch):
    monkeypatch.setattr('app_engine.network_guard.platform.system', lambda: 'Darwin')
    with pytest.raises(ValueError, match='requires Linux'):
        restrict_network((), ())


@pytest.mark.parametrize('url', ['http://127.0.0.1:8770', 'http://localhost:9900',
                                  'http://127.0.0.1:11434', 'http://192.168.1.1:18888',
                                  'http://127.0.0.1:18888?secret', 'http://user@localhost:18888',
                                  'http://example.com:18888', 'http://localhost:18777'])
def test_legacy_binding_rejects_engine_host_and_network_targets(url):
    with pytest.raises(ValueError):
        backend_url(url, (18777,))


def test_backend_binding_pins_localhost_and_preserves_app_base_path():
    assert backend_url('http://localhost:18888/app/') == 'http://127.0.0.1:18888/app/'


def test_signed_in_users_get_separate_per_user_processes_and_storage(tmp_path, monkeypatch):
    module = load_engine(tmp_path, monkeypatch, APP_ENGINE_PUBLIC_ORIGIN=PUBLIC,
                         APP_ENGINE_ADMIN_SECRET='test-owner-secret')
    root = module.APPS_DIR / 'notes'
    (root / 'server.py').write_text('''import json, os
from http.server import BaseHTTPRequestHandler, HTTPServer
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({'data': os.environ['APP_DATA_DIR'], 'build': os.environ['APP_BUILD_DIR']}).encode()
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *args): pass
HTTPServer(('127.0.0.1', int(os.environ['PORT'])), Handler).serve_forever()
''')
    (root / 'app.json').write_text(json.dumps({'id': 'notes', 'label': 'Notes', 'icon': 'N',
        'manifest_version': 2, 'targets': {'web': {'kind': 'web', 'runtime': {
            'driver': 'process', 'scope': 'per_user', 'start': {'argv': [sys.executable, 'server.py']},
            'health': {'path': '/health', 'timeout_seconds': 5}}}}, 'default_target': 'web'}))
    credentials = []
    for name in ('alice', 'bob'):
        player, invite = module._accounts.create_player(name, ['notes'])
        _, token = module._accounts.redeem_invite(invite, 'test-player-password')
        credentials.append((player['id'], {'cookie': f'{SESSION_COOKIE}={token}'}))
    with public_client(module) as client:
        assert sign_in(client, 'test-owner-secret').status_code == 303
        plan = client.post('/api/app-engine/apps/notes/preview', json={}).json()
        assert client.post('/api/app-engine/plans/' + plan['fingerprint'] + '/approve',
                           headers={'x-app-engine-admin': module._admin_capability}).status_code == 204
        opened = []
        values = []
        for user_id, credential in credentials:
            response = client.post('/api/app-engine/apps/notes/open', json={}, headers=credential)
            assert response.status_code == 200, response.text
            opened.append(response.json())
            content = client.get('https://notes.play.example.com/api/app-engine/sessions/' +
                                 opened[-1]['session_id'] + '/proxy/', headers=credential)
            assert content.status_code == 200, content.text
            value = content.json()
            assert Path(value['data']).is_dir() and Path(value['build']).is_dir()
            values.append(value)
            record = module._app_runtime.lifecycle._records[opened[-1]['launch_id']]
            assert record.scope_key.subject_id == user_id
        assert opened[0]['launch_id'] != opened[1]['launch_id']
        assert values[0]['data'] != values[1]['data']
        assert values[0]['build'] != values[1]['build']
        assert client.get('https://notes.play.example.com/api/app-engine/sessions/' +
                          opened[0]['session_id'] + '/proxy/', headers=credentials[1][1]).status_code == 404


def test_unapproved_or_changed_legacy_binding_never_contacts_backend(tmp_path, monkeypatch):
    module = load_engine(tmp_path, monkeypatch)
    root = module.APPS_DIR / 'notes'
    (root / 'app.json').write_text(json.dumps({'id': 'notes', 'label': 'Notes', 'icon': 'N',
                                              'entry_point': 'http://127.0.0.1:18888'}))
    received = []
    def backend(request):
        received.append(request)
        return httpx.Response(200, text='<html>app backend</html>')
    module._proxy = httpx.AsyncClient(transport=httpx.MockTransport(backend))
    with TestClient(module.app) as client:
        content = 'http://notes.localhost/apps/notes/'
        assert client.get(content).status_code == 403
        assert received == []
        plan = client.get('/api/backend-plans/notes').json()
        endpoint = '/api/backend-plans/notes/approve'
        assert client.post(endpoint, json=plan).status_code == 403
        headers = {'x-app-engine-admin': module._admin_capability}
        assert client.post(endpoint, json=plan, headers=headers).status_code == 200
        assert client.get(content).status_code == 200
        assert len(received) == 1
        (root / 'helper.py').write_text('changed')
        assert client.get(content).status_code == 403
        assert client.post(endpoint, json=plan, headers=headers).status_code == 409
        assert len(received) == 1
