"""Checksums admit reviewed unsigned bytes, never automatic code execution."""
import hashlib
import io
import json
import sys
import zipfile

import httpx
from fastapi.testclient import TestClient
from test_store_routes import load


def test_unsigned_store_install_requires_owner_and_does_not_approve_or_execute_code(tmp_path, monkeypatch):
    marker = tmp_path / 'must-not-run'
    code = f'from pathlib import Path; Path({str(marker)!r}).write_text("executed")'
    manifest = {'id': 'reviewed', 'version': '1.0.0', 'label': 'Reviewed', 'icon': 'R',
                'manifest_version': 2, 'default_target': 'web', 'targets': {'web': {'kind': 'web',
                'runtime': {'driver': 'process', 'install': [{'argv': [sys.executable, '-c', code]}],
                            'start': {'argv': [sys.executable, '-c', code]}}}}}
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr('app.json', json.dumps(manifest))
        archive.writestr('index.html', '<title>Reviewed package</title>')
    package = buffer.getvalue()
    release = {'id': 'reviewed', 'version': '1.0.0', 'label': 'Reviewed', 'icon': 'R',
               'description': '', 'categories': [], 'author': '', 'min_engine_version': '',
               'package_url': 'api/v1/packages/reviewed/1.0.0',
               'sha256': hashlib.sha256(package).hexdigest(), 'size_bytes': len(package)}

    def store(request):
        if request.url.path.endswith('/catalog'):
            return httpx.Response(200, json={'protocol_version': 1, 'name': 'Explicitly trusted test store',
                                           'apps': [release]})
        return httpx.Response(200, content=package)

    module = load(tmp_path, monkeypatch)
    with TestClient(module.app) as client:
        module._store_bridge._client = httpx.AsyncClient(transport=httpx.MockTransport(store))
        admin = {'x-app-engine-admin': module._admin_capability}
        source = client.post('/api/app-engine/stores', json={'url': 'https://unsigned.test'}, headers=admin)
        assert source.status_code == 201
        preview = client.post('/api/app-engine/store-installs/preview', headers=admin,
            json={'store_id': source.json()['id'], 'app_id': 'reviewed', 'version': '1.0.0'})
        assert preview.status_code == 200, preview.text
        request = {'fingerprint': preview.json()['fingerprint']}
        assert client.post('/api/app-engine/store-installs', json=request).status_code == 403
        installed = client.post('/api/app-engine/store-installs', json=request, headers=admin)
        assert installed.status_code == 201, installed.text
        assert not marker.exists(), 'Installing bytes must not run preparation scripts'
        opened = client.post('/api/app-engine/apps/reviewed/open', json={}, headers=admin)
        assert opened.status_code == 409, opened.text
        assert opened.json()['detail']['code'] == 'approval_required'
        assert not marker.exists(), 'Store trust must not grant launch approval'
