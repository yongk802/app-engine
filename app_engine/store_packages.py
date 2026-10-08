"""Bounded ZIP extraction followed by the engine's manifest validation."""
from __future__ import annotations

import io
import json
from pathlib import Path
import stat
import zipfile

from .manifest import is_compatible, parse_manifest
from .store_contracts import Release, StoreError, safe_path

MAX_EXPANDED_BYTES = 256 * 1024 * 1024
MAX_FILES = 10000


def extract_package(body: bytes, destination: Path, release: Release) -> None:
    """Extract a validated package only into a fresh private staging directory."""
    try:
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            members = archive.infolist()
            if len(members) > MAX_FILES or sum(item.file_size for item in members) > MAX_EXPANDED_BYTES:
                raise ValueError('Archive exceeds installation limits.')
            seen: set[str] = set()
            for item in members:
                name = item.filename.rstrip('/') if item.is_dir() else item.filename
                mode = item.external_attr >> 16
                if (not safe_path(name) or name.casefold() in seen or
                        item.flag_bits & 1 or stat.S_ISLNK(mode) or
                        (stat.S_IFMT(mode) not in {0, stat.S_IFREG, stat.S_IFDIR}) or
                        any(part.casefold() in {'.git', '.venv'} for part in name.split('/'))):
                    raise ValueError('Archive contains an unsafe or duplicate entry.')
                seen.add(name.casefold())
                target = destination.joinpath(*name.split('/'))
                target.relative_to(destination)
                if item.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(item) as source, target.open('xb') as output:
                    remaining = item.file_size
                    while chunk := source.read(min(65536, remaining + 1)):
                        remaining -= len(chunk)
                        if remaining < 0:
                            raise ValueError('Archive entry exceeds its declared size.')
                        output.write(chunk)
                if remaining != 0:
                    raise ValueError('Archive entry is truncated.')
                target.chmod(0o755 if mode & 0o111 else 0o644)
        raw = json.loads((destination / 'app.json').read_text('utf-8'))
        if not isinstance(raw, dict) or raw.get('id') != release.id or raw.get('version') != release.version:
            raise ValueError('Package manifest does not match its listing.')
        inspection = parse_manifest(destination)
        if inspection.manifest is None:
            raise ValueError('Package manifest is invalid.')
        if not is_compatible(inspection.manifest.metadata.min_engine_version):
            raise StoreError('incompatible_app', 'The app requires a newer engine.')
    except StoreError:
        raise
    except (ValueError, TypeError, OSError, RuntimeError, zipfile.BadZipFile, UnicodeError, NotImplementedError):
        raise StoreError('invalid_package', 'The package is unsafe, incomplete or has an invalid manifest.') from None
