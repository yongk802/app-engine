import importlib
import json

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def engine_client(tmp_path, monkeypatch):
    monkeypatch.delenv('APP_ENGINE_PUBLIC_ORIGIN', raising=False)
    monkeypatch.setenv('APP_ENGINE_STATE_DIR', str(tmp_path / 'state'))
    monkeypatch.setenv('APP_ENGINE_APPS_DIR', str(tmp_path / 'apps'))
    root = tmp_path / 'apps' / 'notes'
    root.mkdir(parents=True)
    (root / 'app.json').write_text(json.dumps({'id': 'notes', 'label': 'Notes', 'icon': 'N'}))
    (root / 'index.html').write_text('<h1>Notes</h1>')
    import engine
    module = importlib.reload(engine)
    with TestClient(module.app, base_url='http://localhost') as client:
        yield module, client, {'x-app-engine-admin': module._admin_capability}


def key(client, admin):
    response = client.post('/api/assistant/key', headers=admin)
    assert response.status_code == 200, response.text
    return response.json()['key']


def rpc(client, token, method, params=None, **kwargs):
    return client.post('/api/mcp', headers={'Authorization': f'Bearer {token}', 'Accept': 'application/json, text/event-stream', **kwargs}, json={'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params or {}})


def test_settings_gated_persisted_and_key_not_stored_in_clear(engine_client):
    module, client, admin = engine_client
    assert client.get('/api/assistant/settings').status_code == 403
    assert client.get('/api/assistant/settings', headers={**admin, 'host': 'notes.localhost'}).status_code == 403
    assert client.put('/api/assistant/settings', headers=admin, json={'chat_url': 'https://chat.example/conversation'}).status_code == 200
    data = client.get('/api/assistant/settings', headers=admin).json()
    assert data['chat_url'] == 'https://chat.example/conversation'
    assert data['mcp_url'] == 'http://localhost/api/mcp'
    token = key(client, admin)
    saved = (module.STATE_DIR / '.assistant' / 'settings.json').read_text()
    assert token not in saved
    assert 'https://chat.example/conversation' in saved
    assert client.get('/api/assistant/settings', headers=admin).json()['enabled']


@pytest.mark.parametrize('url', ['javascript:alert(1)', 'http://remote.example/chat', 'https://user:pass@chat.example', 'file:///tmp/chat', 'https://chat.example/\nfoo'])
def test_invalid_chat_urls(engine_client, url):
    _, client, admin = engine_client
    assert client.put('/api/assistant/settings', headers=admin, json={'chat_url': url}).status_code == 400


def test_mcp_auth_origin_protocol_and_revocation(engine_client):
    _, client, admin = engine_client
    assert rpc(client, 'wrong', 'initialize').status_code == 401
    token = key(client, admin)
    result = rpc(client, token, 'initialize', {'protocolVersion': '2025-11-25', 'capabilities': {}, 'clientInfo': {'name': 'test', 'version': '1'}})
    assert result.status_code == 200, result.text
    assert result.json()['result']['protocolVersion'] == '2025-11-25'
    assert rpc(client, token, 'tools/list', Origin='https://evil.example').status_code == 403
    assert rpc(client, token, 'tools/list', Host='evil.example').status_code == 403
    assert rpc(client, token, 'tools/list', **{'MCP-Protocol-Version': 'future'}).status_code == 400
    rotated = key(client, admin)
    assert rpc(client, token, 'tools/list').status_code == 401
    assert rpc(client, rotated, 'tools/list').status_code == 200
    assert client.delete('/api/assistant/key', headers=admin).status_code == 204
    assert rpc(client, rotated, 'tools/list').status_code == 401


def test_mcp_state_roundtrip_open_and_lazy_catalog(engine_client):
    module, client, admin = engine_client
    token = key(client, admin)
    def call(name, arguments):
        response = rpc(client, token, 'tools/call', {'name': name, 'arguments': arguments})
        assert response.status_code == 200, response.text
        return response.json()['result']
    tools = rpc(client, token, 'tools/list').json()['result']['tools']
    assert 'apps_read_state' in [t['name'] for t in tools]
    listing = call('apps_list', {})['structuredContent']
    assert listing['apps'][0]['id'] == 'notes'
    assert module._app_runtime.diagnostics()['active_launch_count'] == 0
    state = call('apps_read_state', {'app_id': 'notes'})['structuredContent']
    assert state['state'] == {}
    changed = call('apps_write_state', {'app_id': 'notes', 'revision': state['revision'], 'state': {'note': 'hello'}})
    assert not changed.get('isError')
    assert json.loads((module.STATE_DIR / 'notes.json').read_text()) == {'note': 'hello'}
    stale = call('apps_write_state', {'app_id': 'notes', 'revision': state['revision'], 'state': {}})
    assert stale['isError']
    assert call('apps_read_state', {'app_id': '../secret'})['isError']
    client.get('/api/assistant/events?after=0', headers=admin)
    opened = call('apps_open', {'app_id': 'notes'})['structuredContent']
    assert opened['status'] == 'queued'
    events = client.get('/api/assistant/events?after=0', headers=admin).json()
    assert events['events'][0]['app_id'] == 'notes'
    assert events['events'][0]['type'] == 'open'


def test_malformed_requests_fail_without_server_errors(engine_client):
    _, client, admin = engine_client
    token = key(client, admin)
    headers = {'Authorization': f'Bearer {token}'}
    for body in [[], 1, None, {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': []}]:
        response = client.post('/api/mcp', headers=headers, json=body)
        assert response.status_code == 400, response.text
    response = client.post('/api/mcp', headers=headers, json={'jsonrpc': '2.0', 'method': 'notifications/initialized'})
    assert response.status_code == 202
    assert rpc(client, token, 'made/up').json()['error']['code'] == -32601
    assert client.get('/api/mcp', headers=headers).status_code == 405
    assert client.get('/api/assistant/settings', headers=admin).json()['last_connected_at'] is not None


def test_settings_do_not_collide_with_an_app_named_assistant(engine_client):
    module, client, admin = engine_client
    module.STATE_DIR.mkdir(parents=True, exist_ok=True)
    app_state = module.STATE_DIR / 'assistant.json'
    app_state.write_text('{"draft":"keep me"}')
    token = key(client, admin)
    assert json.loads(app_state.read_text()) == {'draft': 'keep me'}
    from app_engine.assistant import AssistantSettings
    restored = AssistantSettings(module.STATE_DIR)
    assert restored.key_hash
    assert token not in restored.path.read_text()


def test_open_requires_a_connected_panel_and_large_input_is_refused(engine_client):
    _, client, admin = engine_client
    token = key(client, admin)
    result = rpc(client, token, 'tools/call', {'name': 'apps_open', 'arguments': {'app_id': 'notes'}}).json()['result']
    assert result['isError']
    assert 'panel' in result['content'][0]['text']
    response = client.post('/api/mcp', headers={'Authorization': f'Bearer {token}'}, content='x' * (161 * 1024))
    assert response.status_code == 413


@pytest.mark.parametrize('cursor', ['²', '-1', 2, {}, '123456789'])
def test_invalid_tool_cursors_are_protocol_errors(engine_client, cursor):
    _, client, admin = engine_client
    response = rpc(client, key(client, admin), 'tools/list', {'cursor': cursor})
    assert response.status_code == 200
    assert response.json()['error']['code'] == -32602
