"""Legacy proxies require an owner-granted, content-bound loopback binding."""
from __future__ import annotations

import hashlib
import ipaddress
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .code_approval import tree_digest


def backend_url(value: str, engine_ports: tuple[int, ...] = ()) -> str:
    parsed = urlsplit(value)
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == 'https' else 80)
    if host == 'localhost':
        host = '127.0.0.1'
    if (parsed.scheme not in {'http', 'https'} or not host
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or port < 1024
            or port in {8770, 9900, 11434, *engine_ports}
            or not ipaddress.ip_address(host).is_loopback):
        raise ValueError('backend binding is not an app-owned loopback service')
    authority = f'[{host}]:{port}' if ':' in host else f'{host}:{port}'
    return urlunsplit((parsed.scheme, authority, parsed.path, '', ''))


def binding_fingerprint(app_id: str, root: Path, value: str, engine_ports=()) -> tuple[str, str]:
    pinned = backend_url(value, engine_ports)
    fingerprint = hashlib.sha256(
        ('backend:' + app_id + ':' + pinned + ':' + tree_digest(root)).encode()).hexdigest()
    return fingerprint, pinned
