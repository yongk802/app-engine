import asyncio
import gzip
import hashlib
import io
import json
import threading
import zipfile
from pathlib import Path

import httpx
import pytest


def package(app_id='hello', version='1.0.0', extra=None):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as archive:
        archive.writestr('app.json', json.dumps({'id': app_id, 'version': version, 'label': 'Hello', 'icon': 'H'}))
        archive.writestr('index.html', '<!doctype html><title>Hello from store</title>')
        for name, value in (extra or {}).items():
            archive.writestr(name, value)
    return stream.getvalue()


def release(body, **overrides):
    return dict(id='hello', version='1.0.0', label='Hello', icon='H', description='', categories=[], author='', min_engine_version='', package_url='api/v1/packages/hello/1.0.0', sha256=hashlib.sha256(body).hexdigest(), size_bytes=len(body), **overrides)


def bridge(tmp_path, catalogs=None, body=None):
    from app_engine.store_bridge import StoreBridge
    body = body or package()
    catalogs = catalogs or {}

    def handle(request):
        host = request.url.host
        if host == 'down.test':
            raise httpx.ConnectError('offline', request=request)
        if request.url.path.endswith('/catalog'):
            payload = catalogs.get(host, {'protocol_version': 1, 'name': host, 'apps': [release(body)]})
            return httpx.Response(200, json=payload)
        return httpx.Response(200, content=body)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    return StoreBridge(tmp_path / 'state', tmp_path / 'installed', local_roots=(tmp_path / 'local',), client=client), client


def test_multiple_stores_survive_reload_with_canonical_identity(tmp_path):
    async def run():
        from app_engine.store_bridge import StoreBridge
        service, client = bridge(tmp_path)
        first = await service.add_store('HTTP://ONE.TEST:80/catalogs/', 'One')
        again = await service.add_store('http://one.test/catalogs', 'One')
        assert first.id == again.id
        await service.add_store('https://two.test', 'Two')
        restored = StoreBridge(tmp_path / 'state', tmp_path / 'installed')
        assert [store.url for store in restored.stores()] == ['http://one.test/catalogs', 'https://two.test']
        await restored.remove_store(first.id)
        assert len(restored.stores()) == 1
        await restored.close()
        await client.aclose()
    asyncio.run(run())


def test_catalog_retains_colliding_ids_and_isolates_outages(tmp_path):
    async def run():
        service, client = bridge(tmp_path)
        for host in ('one.test', 'down.test', 'two.test'):
            await service.add_store('http://' + host)
        catalog = await service.catalog()
        assert [app.release.id for app in catalog.apps] == ['hello', 'hello']
        assert len({app.store_id for app in catalog.apps}) == 2
        assert [status.error_code for status in catalog.stores] == ['', 'store_unavailable', '']
        await client.aclose()
    asyncio.run(run())


def test_preview_install_feeds_existing_runtime_catalog(tmp_path):
    async def run():
        from app_engine.store_bridge import StoreError
        from app_engine.catalog import ImmutableAppCatalog
        from app_engine.contracts import AppSource, CatalogKey
        service, client = bridge(tmp_path)
        store = await service.add_store('http://one.test')
        preview = await service.preview(store.id, 'hello', '1.0.0')
        assert not (tmp_path / 'installed' / 'hello').exists()
        installed = await service.install(preview.fingerprint)
        assert installed.app_id == 'hello'
        catalog = ImmutableAppCatalog()
        snapshot = await catalog.configure(CatalogKey('test'), (AppSource('external', tmp_path / 'installed', 0),))
        assert [str(app.manifest.app_id) for app in snapshot.apps] == ['hello']
        with pytest.raises(StoreError) as exc:
            await service.install(preview.fingerprint)
        assert exc.value.code == 'invalid_preview'
        await client.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('url', ['ftp://store.test', 'http://user:pass@store.test', 'https://store.test?a=b', 'https://store.test/#x', 'http://store.test/../escape', 'http://store.test/%2e%2e'])
def test_rejects_ambiguous_store_urls(tmp_path, url):
    async def run():
        from app_engine.store_bridge import StoreError
        service, client = bridge(tmp_path)
        with pytest.raises(StoreError) as exc:
            await service.add_store(url)
        assert exc.value.code == 'invalid_store_url'
        await client.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('entry', ['../escape.txt', '/absolute.txt', 'C:/windows.txt', 'a\\b.txt', './index.html'])
def test_unsafe_archives_never_publish(tmp_path, entry):
    async def run():
        from app_engine.store_bridge import StoreError
        service, client = bridge(tmp_path, body=package(extra={entry: 'bad'}))
        store = await service.add_store('http://one.test')
        plan = await service.preview(store.id, 'hello', '1.0.0')
        with pytest.raises(StoreError) as exc:
            await service.install(plan.fingerprint)
        assert exc.value.code == 'invalid_package'
        assert not (tmp_path / 'installed' / 'hello').exists()
        await client.aclose()
    asyncio.run(run())


def test_changed_release_invalidates_preview(tmp_path):
    async def run():
        from app_engine.store_bridge import StoreError
        body = package()
        payload = {'protocol_version': 1, 'name': 'One', 'apps': [release(body)]}
        service, client = bridge(tmp_path, {'one.test': payload}, body)
        store = await service.add_store('http://one.test')
        plan = await service.preview(store.id, 'hello', '1.0.0')
        payload['apps'][0]['sha256'] = '0' * 64
        with pytest.raises(StoreError) as exc:
            await service.install(plan.fingerprint)
        assert exc.value.code == 'release_changed'
        await client.aclose()
    asyncio.run(run())


def test_existing_local_app_is_never_overwritten(tmp_path):
    async def run():
        from app_engine.store_bridge import StoreError
        root = tmp_path / 'local' / 'different-directory'
        root.mkdir(parents=True)
        (root / 'app.json').write_text(json.dumps({'id': 'hello', 'label': 'Local', 'icon': 'L'}))
        (root / 'index.html').write_text('local')
        service, client = bridge(tmp_path)
        store = await service.add_store('http://one.test')
        with pytest.raises(StoreError) as exc:
            await service.preview(store.id, 'hello', '1.0.0')
        assert exc.value.code == 'install_conflict'
        assert (root / 'index.html').read_text() == 'local'
        await client.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('kind', ['digest', 'version', 'symlink', 'incompatible', 'foreign-url', 'catalog-shape'])
def test_invalid_release_rejected(tmp_path, kind):
    async def run():
        from app_engine.store_bridge import StoreError
        body = package(version='2.0.0' if kind == 'version' else '1.0.0')
        if kind == 'symlink':
            buffer = io.BytesIO(body)
            with zipfile.ZipFile(buffer, 'a') as archive:
                info = zipfile.ZipInfo('link')
                info.create_system = 3
                info.external_attr = (0o120777 << 16)
                archive.writestr(info, '/etc/passwd')
            body = buffer.getvalue()
        item = release(body)
        if kind == 'digest': item['sha256'] = '0' * 64
        if kind == 'incompatible': item['min_engine_version'] = '999.0.0'
        if kind == 'foreign-url': item['package_url'] = 'https://evil.test/package.zip'
        payload = {'protocol_version': 1, 'name': 'One', 'apps': [item]}
        if kind == 'catalog-shape': payload['apps'] = ['bad']
        service, client = bridge(tmp_path, {'one.test': payload}, body)
        store = await service.add_store('http://one.test')
        with pytest.raises(StoreError):
            plan = await service.preview(store.id, 'hello', '1.0.0')
            await service.install(plan.fingerprint)
        assert not (tmp_path / 'installed' / 'hello').exists()
        await client.aclose()
    asyncio.run(run())


def test_recursive_catalog_does_not_hide_healthy_store(tmp_path):
    async def run():
        from app_engine.store_bridge import StoreBridge
        body = package()
        def handle(request):
            if request.url.host == 'bad.test':
                return httpx.Response(200, content='[' * 10000 + '0' + ']' * 10000)
            return httpx.Response(200, json={'protocol_version': 1, 'name': 'Good', 'apps': [release(body)]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            service = StoreBridge(tmp_path / 'state', tmp_path / 'installed', client=client)
            await service.add_store('http://bad.test')
            await service.add_store('http://good.test')
            result = await service.catalog()
            assert len(result.apps) == 1
            assert result.stores[0].error_code == 'invalid_catalog'
    asyncio.run(run())


def test_idna_equivalent_hosts_share_connection(tmp_path):
    async def run():
        service, client = bridge(tmp_path)
        first = await service.add_store('http://例子.test')
        second = await service.add_store('http://xn--fsqu00a.test')
        assert first.id == second.id
        assert len(service.stores()) == 1
        await client.aclose()
    asyncio.run(run())


def test_recursive_package_manifest_rejected_with_safe_error(tmp_path):
    async def run():
        from app_engine.store_bridge import StoreError
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as archive:
            archive.writestr('app.json', '[' * 10000 + '0' + ']' * 10000)
            archive.writestr('index.html', 'bad')
        service, client = bridge(tmp_path, body=buffer.getvalue())
        store = await service.add_store('http://one.test')
        plan = await service.preview(store.id, 'hello', '1.0.0')
        with pytest.raises(StoreError) as exc:
            await service.install(plan.fingerprint)
        assert exc.value.code == 'invalid_package'
        assert not (tmp_path / 'installed' / 'hello').exists()
        await client.aclose()
    asyncio.run(run())


def test_encoded_http_response_rejected_before_decompression(tmp_path):
    async def run():
        from app_engine.store_bridge import StoreBridge
        compressed = gzip.compress(b' ' * (16 * 1024 * 1024))
        def handle(request):
            return httpx.Response(200, headers={'content-encoding': 'gzip'}, stream=httpx.ByteStream(compressed))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            service = StoreBridge(tmp_path / 'state', tmp_path / 'installed', client=client)
            await service.add_store('http://compressed.test')
            result = await service.catalog()
            assert result.stores[0].error_code == 'unsupported_encoding'
    asyncio.run(run())


def test_canceled_connection_write_cannot_overwrite_next_success(tmp_path, monkeypatch):
    async def run():
        service, client = bridge(tmp_path)
        started, proceed = threading.Event(), threading.Event()
        original = service._save
        def delayed(stores):
            if stores[-1].url == 'http://one.test':
                started.set()
                proceed.wait(5)
            original(stores)
        monkeypatch.setattr(service, '_save', delayed)
        first = asyncio.create_task(service.add_store('http://one.test'))
        assert await asyncio.to_thread(started.wait, 2)
        first.cancel()
        second = asyncio.create_task(service.add_store('http://two.test'))
        await asyncio.sleep(0.03)
        proceed.set()
        await asyncio.gather(first, second, return_exceptions=True)
        # Let an incorrectly detached persistence worker finish before checking.
        await asyncio.to_thread(lambda: original.__self__.path.exists())
        await asyncio.sleep(0.03)
        assert [store.url for store in service.stores()] == ['http://one.test', 'http://two.test']
        assert len(json.loads(service.path.read_text())['stores']) == 2
        await client.aclose()
    asyncio.run(run())


def test_canceled_publication_finishes_before_install_lock_released(tmp_path, monkeypatch):
    async def run():
        service, client = bridge(tmp_path)
        store = await service.add_store('http://one.test')
        plan = await service.preview(store.id, 'hello', '1.0.0')
        started, proceed = threading.Event(), threading.Event()
        original = service._publish
        def delayed(body, release):
            started.set()
            proceed.wait(5)
            original(body, release)
        monkeypatch.setattr(service, '_publish', delayed)
        task = asyncio.create_task(service.install(plan.fingerprint))
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        await asyncio.sleep(0.03)
        assert service._lock.locked()
        proceed.set()
        await asyncio.gather(task, return_exceptions=True)
        assert (tmp_path / 'installed' / 'hello' / 'index.html').is_file()
        assert not service._lock.locked()
        await client.aclose()
    asyncio.run(run())


def test_v2_nested_metadata_version_installs(tmp_path):
    async def run():
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as archive:
            archive.writestr('app.json', json.dumps({
                'manifest_version': 2, 'id': 'hello', 'label': 'Hello', 'icon': 'H',
                'default_target': 'web', 'targets': {'web': {'kind': 'web'}},
                'metadata': {'version': '1.0.0', 'description': 'Nested metadata'},
            }))
            archive.writestr('index.html', '<title>V2 hello</title>')
        service, client = bridge(tmp_path, body=buffer.getvalue())
        store = await service.add_store('http://one.test')
        plan = await service.preview(store.id, 'hello', '1.0.0')
        installed = await service.install(plan.fingerprint)
        assert installed.version == '1.0.0'
        assert (tmp_path / 'installed' / 'hello' / 'index.html').read_text() == '<title>V2 hello</title>'
        await client.aclose()
    asyncio.run(run())
