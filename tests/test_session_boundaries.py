"""Session content belongs to one app origin and one authenticated person."""
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from test_public_origin import PUBLIC, load_engine, public_client, sign_in

from app_engine.public_origin import SESSION_COOKIE, http_origin


@pytest.mark.parametrize('kind', ['assets', 'proxy'])
def test_session_content_is_pinned_to_the_app_origin(tmp_path, monkeypatch, kind):
    module = load_engine(tmp_path, monkeypatch)
    received = []
    if kind == 'proxy':
        manifest = module.APPS_DIR / 'notes' / 'app.json'
        manifest.write_text(json.dumps({'id': 'notes', 'label': 'Notes', 'icon': 'N',
                                        'entry_point': 'http://127.0.0.1:18888'}))

        def backend(request):
            received.append(request.url)
            class Body(httpx.AsyncByteStream):
                async def __aiter__(self):
                    yield b'<html>app content</html>'
            return httpx.Response(200, stream=Body(), headers={'content-type': 'text/html'})

        from app_engine.backend_bindings import binding_fingerprint
        module._record_code_approval(binding_fingerprint('notes', manifest.parent, 'http://127.0.0.1:18888')[0])
        module._app_runtime.gateway._client = httpx.AsyncClient(transport=httpx.MockTransport(backend))
    with TestClient(module.app, base_url='http://localhost:8770') as client:
        opened = client.post('/api/app-engine/apps/notes/open', json={})
        assert opened.status_code == 200
        session_id = opened.json()['session_id']
        path = f'/api/app-engine/sessions/{session_id}/{kind}/index.html'
        assert client.get('http://notes.localhost:8770' + path).status_code == 200
        for origin in ('http://localhost:8770', 'http://127.0.0.1:8770',
                       'http://other.localhost:8770', 'http://notes.localhost:8771',
                       'https://notes.localhost:8770'):
            response = client.get(origin + path, headers={'origin': 'http://notes.localhost:8770'})
            assert response.status_code == 403, origin
            assert 'app content' not in response.text
        for source in ('http://localhost:8770', 'http://other.localhost:8770', 'null'):
            assert client.get('http://notes.localhost:8770' + path, headers={'origin': source}).status_code == 403
        if kind == 'proxy':
            assert len(received) == 1, 'wrong origins must never reach the backend'


def test_public_sessions_are_private_to_the_actual_user_and_recheck_app_grants(tmp_path, monkeypatch):
    module = load_engine(tmp_path, monkeypatch, APP_ENGINE_PUBLIC_ORIGIN=PUBLIC,
                         APP_ENGINE_ADMIN_SECRET='test-owner-secret')
    credentials = {}
    for username in ('alice', 'bob'):
        player, invite = module._accounts.create_player(username, ['notes'])
        _, token = module._accounts.redeem_invite(invite, 'test-player-password')
        credentials[username] = (player['id'], {'cookie': f'{SESSION_COOKIE}={token}'})
    alice_id, alice = credentials['alice']
    _, bob = credentials['bob']
    with public_client(module) as client:
        assert sign_in(client, 'test-owner-secret').status_code == 303
        opened = client.post('/api/app-engine/apps/notes/open', json={}, headers=alice).json()
        session_id = opened['session_id']
        asset = f'https://notes.play.example.com/api/app-engine/sessions/{session_id}/assets/index.html'
        close = f'/api/app-engine/sessions/{session_id}'
        assert client.get(asset, headers=alice).status_code == 200
        assert client.get(PUBLIC + asset.split('.com', 1)[1], headers=alice).status_code == 403
        assert client.get(asset, headers=bob).status_code == 404
        assert client.get(asset).status_code == 404, 'owner cannot reuse a player content session'
        assert client.delete(close, headers=bob).status_code == 404
        assert client.get(asset, headers=alice).status_code == 200
        module._accounts.update_player(alice_id, apps=[])
        assert client.get(asset, headers=alice).status_code == 404
        assert client.delete(close, headers=alice).status_code == 204, 'own revoked sessions can still be cleaned up'


def test_app_hosts_cannot_use_launcher_chat_or_owner_reads(tmp_path, monkeypatch):
    module = load_engine(tmp_path, monkeypatch, APP_ENGINE_PUBLIC_ORIGIN=PUBLIC,
                         APP_ENGINE_ADMIN_SECRET='test-owner-secret')
    with public_client(module) as client:
        assert sign_in(client, 'test-owner-secret').status_code == 303
        assert client.post('/api/chat', json={'text': 'launcher message'}).status_code == 200
        app = 'https://notes.play.example.com'
        assert client.get(app + '/api/chat').status_code == 403
        assert client.post(app + '/api/chat', json={'text': 'app impersonation'}).status_code == 403
        assert client.get(app + '/admin/players').status_code == 403
        assert client.get(app + '/api/apps/rejected').status_code == 403
        messages = client.get('/api/chat').json()['messages']
        assert [message['text'] for message in messages] == ['launcher message']


def test_same_origin_apps_require_explicit_operator_trust(tmp_path, monkeypatch):
    with pytest.raises(SystemExit, match='APP_ENGINE_TRUST_SAME_ORIGIN_APPS'):
        load_engine(tmp_path, monkeypatch, APP_ENGINE_PUBLIC_ORIGIN=PUBLIC,
                    APP_ENGINE_APP_ORIGINS='same')


def test_launcher_actions_reject_an_app_origin_even_on_the_launcher_host(tmp_path, monkeypatch):
    module = load_engine(tmp_path, monkeypatch, APP_ENGINE_PUBLIC_ORIGIN=PUBLIC,
                         APP_ENGINE_ADMIN_SECRET='test-owner-secret')
    with public_client(module) as client:
        assert sign_in(client, 'test-owner-secret').status_code == 303
        headers = {'origin': 'https://notes.play.example.com'}
        assert client.post('/api/chat', json={'text': 'cross-origin app'}, headers=headers).status_code == 403
        assert client.post('/admin/sign-out', headers=headers).status_code == 403
        assert client.get('/').status_code == 200


@pytest.mark.parametrize('value', ['null', 'https://user:pass@example.com', 'https://example.com/path',
                                  'https://example.com?query', 'https://example.com#fragment',
                                  'https://example.com:0', 'https://example.com:bad'])
def test_non_origins_are_rejected(value):
    assert http_origin(value) is None


def test_origin_comparison_normalizes_only_default_ports_and_hostname_case():
    assert http_origin('https://EXAMPLE.com:443') == http_origin('https://example.com')
    assert http_origin('http://localhost:80') == http_origin('http://localhost')
    assert http_origin('https://example.com:444') != http_origin('https://example.com')
    assert http_origin('https://example.com.') != http_origin('https://example.com')
