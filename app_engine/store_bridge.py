"""Persistent one-to-many store connections and explicit package installation."""
from __future__ import annotations

import asyncio
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import secrets
import tempfile

import httpx

from .manifest import is_compatible, parse_manifest
from .store_contracts import (
    MAX_CATALOG_BYTES, MAX_PACKAGE_BYTES, InstallPreview, InstalledRelease,
    Release, StoreCatalog, StoreConnection, StoreError, StoreListing, StoreStatus,
    canonical_url,
)
from .store_packages import extract_package


class StoreBridge:
    def __init__(self, state_dir: Path, apps_dir: Path, *,
                 local_roots: tuple[Path, ...] = (), client: httpx.AsyncClient | None = None):
        self.path = Path(state_dir) / 'stores.json'
        self.apps_dir = Path(apps_dir).resolve()
        self.local_roots = tuple(Path(root).resolve() for root in local_roots)
        self._client = client
        self._owns_client = client is None
        self._stores: tuple[StoreConnection, ...] = self._load()
        self._previews: dict[str, InstallPreview] = {}
        self._lock = asyncio.Lock()

    def _load(self) -> tuple[StoreConnection, ...]:
        try:
            raw = json.loads(self.path.read_text('utf-8'))
            if raw.get('protocol_version') != 1 or not isinstance(raw.get('stores'), list):
                raise ValueError()
            stores = []
            for item in raw['stores']:
                url = canonical_url(item['url'])
                connection = StoreConnection(self._store_id(url), url, str(item['name'])[:200])
                if connection.id in {store.id for store in stores}:
                    raise ValueError()
                stores.append(connection)
            return tuple(stores)
        except FileNotFoundError:
            return ()
        except (ValueError, TypeError, KeyError, AttributeError, OSError):
            raise StoreError('invalid_configuration', 'Store configuration is invalid; restore stores.json before continuing.') from None

    @staticmethod
    def _store_id(url: str) -> str:
        return hashlib.sha256(url.encode()).hexdigest()[:20]

    def stores(self) -> tuple[StoreConnection, ...]:
        return self._stores

    def _save(self, stores: tuple[StoreConnection, ...]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.path.parent, delete=False) as output:
            temp = Path(output.name)
            json.dump({'protocol_version': 1, 'stores': [asdict(store) for store in stores]}, output, indent=2)
        try:
            temp.replace(self.path)
        finally:
            temp.unlink(missing_ok=True)
        self._stores = stores

    async def add_store(self, url: str, name: str = '') -> StoreConnection:
        url = canonical_url(url)
        if not isinstance(name, str) or len(name) > 200:
            raise StoreError('invalid_store_name', 'Store names must be at most 200 characters.')
        connection = StoreConnection(self._store_id(url), url, name.strip() or url)
        async with self._lock:
            stores = tuple(connection if store.id == connection.id else store for store in self._stores)
            if not any(store.id == connection.id for store in self._stores):
                if len(stores) >= 100:
                    raise StoreError('store_limit', 'At most 100 stores can be connected.')
                stores += (connection,)
            await asyncio.to_thread(self._save, stores)
        return connection

    async def remove_store(self, store_id: str) -> None:
        async with self._lock:
            self._store(store_id)
            await asyncio.to_thread(self._save, tuple(store for store in self._stores if store.id != store_id))
            self._previews = {key: plan for key, plan in self._previews.items() if plan.store_id != store_id}

    def _store(self, store_id: str) -> StoreConnection:
        for store in self._stores:
            if store.id == store_id:
                return store
        raise StoreError('store_not_found', 'The store connection does not exist.')

    async def _download(self, url: str, limit: int) -> bytes:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=15.0, follow_redirects=False, trust_env=False)
        try:
            async with self._client.stream('GET', url, follow_redirects=False, timeout=15.0) as response:
                response.raise_for_status()
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > limit:
                        raise StoreError('invalid_package', 'Store response exceeds its size limit.')
                return bytes(data)
        except httpx.HTTPError:
            raise StoreError('store_unavailable', 'The store could not be reached or returned an HTTP error.') from None

    async def _releases(self, store: StoreConnection) -> tuple[str, tuple[Release, ...]]:
        try:
            body = await self._download(store.url + '/api/v1/catalog', MAX_CATALOG_BYTES)
            raw = json.loads(body)
            if (not isinstance(raw, dict) or type(raw.get('protocol_version')) is not int or
                    raw['protocol_version'] != 1 or not isinstance(raw.get('name'), str) or
                    not isinstance(raw.get('apps'), list) or len(raw['apps']) > 10000):
                raise ValueError()
            releases = tuple(Release.parse(item) for item in raw['apps'])
            if len({(item.id, item.version) for item in releases}) != len(releases):
                raise ValueError()
            return raw['name'][:200], releases
        except (ValueError, UnicodeError) as exc:
            if isinstance(exc, StoreError):
                raise
            raise StoreError('invalid_catalog', 'The store returned an invalid version-one catalog.') from None

    async def catalog(self) -> StoreCatalog:
        async def fetch(store: StoreConnection):
            try:
                name, releases = await self._releases(store)
                return tuple(StoreListing(store.id, store.url, item) for item in releases), StoreStatus(store.id, store.url, store.name or name)
            except StoreError as exc:
                return (), StoreStatus(store.id, store.url, store.name, exc.code, str(exc))
        results = await asyncio.gather(*(fetch(store) for store in self._stores))
        return StoreCatalog(tuple(item for apps, _ in results for item in apps), tuple(status for _, status in results))

    async def _release(self, store: StoreConnection, app_id: str, version: str) -> Release:
        _, releases = await self._releases(store)
        for release in releases:
            if release.id == app_id and release.version == version:
                return release
        raise StoreError('release_not_found', 'The requested release was not found in this store.')

    def _check_conflict(self, app_id: str) -> None:
        if (self.apps_dir / app_id).exists():
            raise StoreError('install_conflict', 'This app ID is already installed. Existing apps are preserved.')
        for root in self.local_roots:
            if not root.is_dir():
                continue
            for child in root.iterdir():
                if not child.is_dir() or not (child / 'app.json').is_file():
                    continue
                inspection = parse_manifest(child)
                if inspection.manifest is not None and inspection.manifest.app_id == app_id:
                    raise StoreError('install_conflict', 'This app ID already exists in a local app source.')

    async def preview(self, store_id: str, app_id: str, version: str) -> InstallPreview:
        store = self._store(store_id)
        release = await self._release(store, app_id, version)
        if not is_compatible(release.min_engine_version):
            raise StoreError('incompatible_app', 'The app requires a newer engine.')
        await asyncio.to_thread(self._check_conflict, release.id)
        fingerprint = secrets.token_urlsafe(32)
        plan = InstallPreview(fingerprint, store.id, store.url, release, str(self.apps_dir / release.id))
        async with self._lock:
            self._store(store_id)
            if len(self._previews) >= 100:
                self._previews.pop(next(iter(self._previews)))
            self._previews[fingerprint] = plan
        return plan

    async def install(self, fingerprint: str) -> InstalledRelease:
        async with self._lock:
            plan = self._previews.pop(fingerprint, None)
            if plan is None:
                raise StoreError('invalid_preview', 'Preview this release again before installing.')
            store = self._store(plan.store_id)
            release = await self._release(store, plan.release.id, plan.release.version)
            if release != plan.release:
                raise StoreError('release_changed', 'The release changed after preview; review a new preview.')
            await asyncio.to_thread(self._check_conflict, release.id)
            body = await self._download(store.url + '/' + release.package_url, min(release.size_bytes, MAX_PACKAGE_BYTES))
            if len(body) != release.size_bytes or hashlib.sha256(body).hexdigest() != release.sha256:
                raise StoreError('invalid_package', 'The downloaded package does not match its declared size and SHA-256.')
            await asyncio.to_thread(self._publish, body, release)
            return InstalledRelease(release.id, release.version, store.id, plan.destination)

    def _publish(self, body: bytes, release: Release) -> None:
        self.apps_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='.install-', dir=self.apps_dir) as stage:
            staged = Path(stage) / 'app'
            staged.mkdir()
            extract_package(body, staged, release)
            self._check_conflict(release.id)
            # rename never replaces an installed non-empty app directory.
            staged.rename(self.apps_dir / release.id)

    async def close(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
