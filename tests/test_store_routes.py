import hashlib
import importlib
import io
import json
import zipfile

import httpx
import pytest
from fastapi.testclient import TestClient


def load(tmp_path, monkeypatch, public=False):
    monkeypatch.setenv('APP_ENGINE_APPS_DIR', str(tmp_path / 'local'))
    monkeypatch.setenv('APP_ENGINE_STATE_DIR', str(tmp_path / 'state'))
    monkeypatch.delenv('APP_ENGINE_PUBLIC_ORIGIN', raising=False)
    if public:
        monkeypatch.setenv('APP_ENGINE_PUBLIC_ORIGIN', 'https://play.example.com')
        monkeypatch.setenv('APP_ENGINE_ADMIN_SECRET', 'a-long-test-owner-secret')
    import engine
    return importlib.reload(engine)


def transport():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr('app.json', json.dumps({'id': 'store-hello', 'version': '1.0.0', 'label': 'Store Hello', 'icon': 'H'}))
        archive.writestr('index.html', '<title>Installed from store</title>')
    body = buffer.getvalue()
    release = dict(id='store-hello', version='1.0.0', label='Store Hello', icon='H', description='', categories=[], author='', min_engine_version='', package_url='api/v1/packages/store-hello/1.0.0', sha256=hashlib.sha256(body).hexdigest(), size_bytes=len(body))
    def handle(request):
        if request.url.path.endswith('/catalog'):
            return httpx.Response(200, json={'protocol_version': 1, 'name': 'Fixture', 'apps': [release]})
        return httpx.Response(200, content=body)
    return httpx.MockTransport(handle)


def test_store_routes_install_and_launch_through_existing_engine(tmp_path, monkeypatch):
    module = load(tmp_path, monkeypatch)
    with TestClient(module.app) as client:
        module._store_bridge._client = httpx.AsyncClient(transport=transport())
        headers = {'x-app-engine-admin': module._admin_capability}
        page = client.get('/api/app-engine/stores/ui')
        assert page.status_code == 200
        assert module._admin_capability in page.text
        assert 'Review installation' in page.text
        assert 'textContent' in page.text
        assert page.headers['cache-control'] == 'no-store'
        assert page.headers['content-security-policy'] == "frame-ancestors 'none'"
        assert 'id="app-stores"' in client.get('/').text
        store = client.post('/api/app-engine/stores', json={'url': 'http://one.test'}, headers=headers)
        assert store.status_code == 201
        source = store.json()['id']
        catalog = client.get('/api/app-engine/store-catalog').json()
        assert catalog['apps'][0]['store_id'] == source
        preview = client.post('/api/app-engine/store-installs/preview', json={'store_id': source, 'app_id': 'store-hello', 'version': '1.0.0'}, headers=headers)
        assert preview.status_code == 200
        result = client.post('/api/app-engine/store-installs', json={'fingerprint': preview.json()['fingerprint']}, headers=headers)
        assert result.status_code == 201
        assert any(item['id'] == 'store-hello' for item in client.get('/api/apps').json())
        assert any(item['id'] == 'store-hello' for item in client.get('/api/app-engine/catalog').json()['apps'])
        served = client.get('/apps/store-hello/', headers={'host': 'store-hello.localhost'})
        assert served.status_code == 200 and 'Installed from store' in served.text
        opened = client.post('/api/app-engine/apps/store-hello/open', json={})
        assert opened.status_code == 200
        assert client.delete('/api/app-engine/stores/' + source, headers=headers).status_code == 204


def test_store_owner_capability_and_launcher_origin_required(tmp_path, monkeypatch):
    module = load(tmp_path, monkeypatch)
    with TestClient(module.app) as client:
        assert client.post('/api/app-engine/stores', json={'url': 'http://one.test'}).status_code == 403
        headers = {'x-app-engine-admin': module._admin_capability, 'host': 'hello.localhost'}
        assert client.post('/api/app-engine/stores', json={'url': 'http://one.test'}, headers=headers).status_code == 403
        assert client.get('/api/app-engine/stores/ui', headers={'host': 'hello.localhost'}).status_code == 403
        assert client.get('/api/app-engine/stores', headers={'host': 'hello.localhost'}).status_code == 403
        assert client.get('/api/app-engine/store-catalog', headers={'host': 'hello.localhost'}).status_code == 403
        assert module._admin_capability not in client.get('/api/app-engine/stores/ui', headers={'host': 'hello.localhost'}).text
        assert client.post('/api/app-engine/store-installs', json={'fingerprint': 'unknown'}, headers={'x-app-engine-admin': module._admin_capability}).json()['detail']['code'] == 'invalid_preview'


def test_public_players_cannot_manage_stores(tmp_path, monkeypatch):
    module = load(tmp_path, monkeypatch, public=True)
    with TestClient(module.app, base_url='https://play.example.com') as client:
        assert client.get('/api/app-engine/stores').status_code == 401
        client.post('/admin/sign-in', data={'secret': 'a-long-test-owner-secret'})
        assert client.get('/api/app-engine/stores').status_code == 200
        # Existing owner helpers enforce the same player boundary for store routes.
        from app_engine.public_origin import Session
        player = Session('player-test', 'player', 'player', frozenset())
        monkeypatch.setattr(module, '_session', lambda request: player)
        assert client.get('/api/app-engine/stores').status_code == 404
        assert client.get('/api/app-engine/stores/ui').status_code == 404
        assert client.post('/api/app-engine/stores', json={'url': 'http://one.test'}, headers={'x-app-engine-admin': module._admin_capability}).status_code == 404


def test_store_catalog_keeps_sources_and_searches_releases(tmp_path, monkeypatch):
    module = load(tmp_path, monkeypatch)
    with TestClient(module.app) as client:
        module._store_bridge._client = httpx.AsyncClient(transport=transport())
        headers = {'x-app-engine-admin': module._admin_capability}
        for url in ('http://one.test', 'http://two.test'):
            assert client.post('/api/app-engine/stores', json={'url': url}, headers=headers).status_code == 201
        result = client.get('/api/app-engine/store-catalog?q=hello').json()
        assert len(result['apps']) == 2
        assert len({item['store_id'] for item in result['apps']}) == 2
        assert len(result['stores']) == 2
        assert client.get('/api/app-engine/store-catalog?q=missing').json()['apps'] == []
        assert client.post('/api/app-engine/stores', json={'url': 123}, headers=headers).status_code == 400


def test_local_apps_take_priority_over_installed_apps(tmp_path, monkeypatch):
    module = load(tmp_path, monkeypatch)
    for root, label in ((module.APPS_DIR, 'Local app'), (module.STORE_APPS_DIR, 'Installed app')):
        folder = root / 'same-app'
        folder.mkdir(parents=True)
        (folder / 'app.json').write_text(json.dumps({'id': 'same-app', 'label': label, 'icon': 'A'}))
        (folder / 'index.html').write_text(label)
    with TestClient(module.app) as client:
        assert client.get('/api/apps').json()[0]['label'] == 'Local app'
        assert client.get('/api/app-engine/catalog').json()['apps'][0]['label'] == 'Local app'
        assert 'Local app' in client.get('/apps/same-app/', headers={'host': 'same-app.localhost'}).text


@pytest.mark.parametrize('body,status', [
    ('{"url":"http://one.test","padding":"' + 'x' * 17000 + '"}', 413),
    ('not JSON', 400),
    ('[' * 2000 + '0' + ']' * 2000, 400),
    (json.dumps({'url': 'http://one.test/' + 'x' * 2048}), 400),
    (json.dumps({'url': 'http://one.test', 'name': 'x' * 201}), 400),
], ids=['oversized-body', 'malformed-json', 'deep-json', 'long-url', 'long-name'])
def test_store_requests_reject_oversized_and_invalid_bodies(tmp_path, monkeypatch, body, status):
    module = load(tmp_path, monkeypatch)
    with TestClient(module.app) as client:
        response = client.post('/api/app-engine/stores', content=body,
                               headers={'x-app-engine-admin': module._admin_capability})
        assert response.status_code == status
        assert response.json()['detail']['code'] == 'invalid_request'
        assert client.get('/api/app-engine/stores').json() == []


def test_store_route_errors_have_useful_http_status(tmp_path, monkeypatch):
    module = load(tmp_path, monkeypatch)
    with TestClient(module.app) as client:
        headers = {'x-app-engine-admin': module._admin_capability}
        assert client.delete('/api/app-engine/stores/missing', headers=headers).status_code == 404
        response = client.post('/api/app-engine/store-installs/preview',
                               json={'store_id': 'missing', 'app_id': 'hello', 'version': '1.0.0'}, headers=headers)
        assert response.status_code == 404
