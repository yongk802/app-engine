"""Version-one store protocol and typed engine-side results."""
from __future__ import annotations

from dataclasses import dataclass
import re
from urllib.parse import unquote, urlsplit, urlunsplit

MAX_PACKAGE_BYTES = 64 * 1024 * 1024
MAX_CATALOG_BYTES = 2 * 1024 * 1024
SEGMENT = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9._+-]{0,63}$')
APP_ID = re.compile(r'^[a-z0-9][a-z0-9-]{0,63}$')


class StoreError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def safe_path(path: str) -> bool:
    decoded = unquote(path)
    return (decoded == path and not path.startswith('/') and
            all(part not in {'', '.', '..'} for part in path.split('/')) and
            not any(char in path for char in '\\:\x00?#'))


def canonical_url(value: str) -> str:
    try:
        parts = urlsplit(value.strip())
        port = parts.port
        path = parts.path.rstrip('/')
        if (parts.scheme not in {'http', 'https'} or not parts.hostname or
                parts.username is not None or parts.password is not None or
                parts.query or parts.fragment or
                any(char.isspace() or ord(char) < 32 for char in value) or
                (path and not safe_path(path.lstrip('/')))):
            raise ValueError()
        host = parts.hostname.lower()
        if ':' in host:
            host = '[' + host + ']'
        if port is not None and port != (80 if parts.scheme == 'http' else 443):
            host += ':' + str(port)
        return urlunsplit((parts.scheme, host, path, '', ''))
    except (ValueError, AttributeError):
        raise StoreError('invalid_store_url', 'Provide an HTTP(S) store URL without credentials, query or fragment.') from None


@dataclass(frozen=True)
class StoreConnection:
    id: str
    url: str
    name: str


@dataclass(frozen=True)
class Release:
    id: str
    version: str
    label: str
    icon: str
    description: str
    categories: tuple[str, ...]
    author: str
    min_engine_version: str
    package_url: str
    sha256: str
    size_bytes: int

    @classmethod
    def parse(cls, raw: object) -> Release:
        if not isinstance(raw, dict):
            raise StoreError('invalid_catalog', 'A release must be an object.')
        fields = ('id', 'version', 'label', 'icon', 'description', 'author',
                  'min_engine_version', 'package_url', 'sha256')
        if any(not isinstance(raw.get(key), str) or len(raw[key]) > 8192 for key in fields):
            raise StoreError('invalid_catalog', 'Release metadata is invalid.')
        size = raw.get('size_bytes')
        categories = raw.get('categories')
        if (not APP_ID.fullmatch(raw['id']) or not SEGMENT.fullmatch(raw['version']) or
                not raw['label'] or not raw['icon'] or not safe_path(raw['package_url']) or
                not re.fullmatch('[0-9a-f]{64}', raw['sha256']) or
                type(size) is not int or not 0 < size <= MAX_PACKAGE_BYTES or
                not isinstance(categories, list) or len(categories) > 100 or
                any(not isinstance(item, str) or len(item) > 100 for item in categories)):
            raise StoreError('invalid_catalog', 'Release metadata or package location is invalid.')
        return cls(**{key: raw[key] for key in fields}, size_bytes=size, categories=tuple(categories))


@dataclass(frozen=True)
class StoreListing:
    store_id: str
    store_url: str
    release: Release


@dataclass(frozen=True)
class StoreStatus:
    store_id: str
    store_url: str
    name: str
    error_code: str = ''
    error: str = ''


@dataclass(frozen=True)
class StoreCatalog:
    apps: tuple[StoreListing, ...]
    stores: tuple[StoreStatus, ...]


@dataclass(frozen=True)
class InstallPreview:
    fingerprint: str
    store_id: str
    store_url: str
    release: Release
    destination: str


@dataclass(frozen=True)
class InstalledRelease:
    app_id: str
    version: str
    store_id: str
    root: str
