"""Independent operator trust roots for publisher and builder attestations.

No key, repository or builder is learned from the store. These signatures
authenticate claims by selected identities; they do not prove code is benign
or that a build is reproducible, and never replace execution approval.
"""
from __future__ import annotations

import base64
from dataclasses import asdict
import json
import os
from pathlib import Path
import re
import stat

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .store_contracts import Release, StoreError


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=True, allow_nan=False).encode('ascii')


def release_statement(release: Release, publisher: str, key_id: str, provenance: dict) -> dict:
    metadata = asdict(release)
    metadata.pop('attestation')
    return {'type': 'app-engine-release-v1', 'release': metadata,
            'publisher': publisher, 'key_id': key_id, 'provenance': provenance}


class PublisherPolicy:
    def __init__(self, raw: dict | None = None):
        raw = raw if raw is not None else {'mode': 'allow_unsigned', 'publishers': {}, 'builders': {}}
        try:
            if set(raw) != {'mode', 'publishers', 'builders'} or raw['mode'] not in {'require_signed', 'allow_unsigned'}:
                raise ValueError()
            for kind in ('publishers', 'builders'):
                group = raw[kind]
                if not isinstance(group, dict) or len(group) > 128:
                    raise ValueError()
                for name, identity in group.items():
                    required = {'keys', 'revoked_keys', 'apps', 'repository'}
                    if kind == 'publishers':
                        required.add('builders')
                    if (not isinstance(name, str) or not name or len(name) > 200
                            or not isinstance(identity, dict) or set(identity) != required
                            or not isinstance(identity['keys'], dict) or not 1 <= len(identity['keys']) <= 16
                            or not isinstance(identity['repository'], str) or not identity['repository'].startswith('https://')):
                        raise ValueError()
                    for key_id, key in identity['keys'].items():
                        if not isinstance(key_id, str) or not key_id or len(key_id) > 100:
                            raise ValueError()
                        Ed25519PublicKey.from_public_bytes(base64.b64decode(key, validate=True))
                    for field in ('apps', 'revoked_keys', *(('builders',) if kind == 'publishers' else ())):
                        if (not isinstance(identity[field], list) or len(identity[field]) > 128
                                or any(not isinstance(item, str) or not item or len(item) > 200 for item in identity[field])):
                            raise ValueError()
            # Snapshot caller-owned data, so changing a parsed store cannot mutate policy.
            self.raw = json.loads(json.dumps(raw))
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise StoreError('invalid_publisher_policy', 'Publisher trust policy is invalid.') from exc

    @classmethod
    def from_file(cls, path: Path) -> PublisherPolicy:
        status = path.lstat()
        if (not stat.S_ISREG(status.st_mode) or status.st_size > 65536
                or (os.name != 'nt' and (status.st_uid not in {0, os.getuid()} or status.st_mode & 0o022))):
            raise StoreError('invalid_publisher_policy', 'Publisher policy must be an operator-controlled regular file.')
        try:
            return cls(json.loads(path.read_bytes()))
        except (ValueError, UnicodeError) as exc:
            raise StoreError('invalid_publisher_policy', 'Publisher trust policy is invalid.') from exc

    def _verify(self, kind: str, name: str, key_id: str, signature: str, statement: dict,
                app_id: str, repository: str) -> None:
        identity = self.raw[kind][name]
        if (key_id in identity['revoked_keys'] or app_id not in identity['apps']
                or repository != identity['repository']):
            raise ValueError('untrusted identity or scope')
        public = Ed25519PublicKey.from_public_bytes(base64.b64decode(identity['keys'][key_id], validate=True))
        public.verify(base64.b64decode(signature, validate=True), canonical(statement))

    def verify(self, release: Release) -> None:
        if not release.attestation:
            if self.raw['mode'] == 'require_signed':
                raise StoreError('publisher_verification_failed', 'A trusted publisher and builder signature are required.')
            return
        try:
            envelope = json.loads(release.attestation)
            if set(envelope) != {'publisher', 'key_id', 'signature', 'provenance'}:
                raise ValueError()
            provenance = envelope['provenance']
            if not isinstance(provenance, dict) or set(provenance) != {'statement', 'key_id', 'signature'}:
                raise ValueError()
            statement = provenance['statement']
            if (not isinstance(statement, dict)
                    or set(statement) != {'type', 'app_id', 'version', 'sha256', 'size_bytes', 'builder', 'repository', 'commit'}
                    or statement['type'] != 'app-engine-provenance-v1'
                    or statement['app_id'] != release.id or statement['version'] != release.version
                    or statement['sha256'] != release.sha256 or type(statement['size_bytes']) is not int
                    or statement['size_bytes'] != release.size_bytes
                    or not isinstance(statement['commit'], str) or not re.fullmatch('[0-9a-f]{40}|[0-9a-f]{64}', statement['commit'])):
                raise ValueError()
            publisher, key_id = envelope['publisher'], envelope['key_id']
            builder, repository = statement['builder'], statement['repository']
            if builder not in self.raw['publishers'][publisher]['builders']:
                raise ValueError()
            self._verify('builders', builder, provenance['key_id'], provenance['signature'], statement,
                         release.id, repository)
            self._verify('publishers', publisher, key_id, envelope['signature'],
                         release_statement(release, publisher, key_id, provenance), release.id, repository)
        except (ValueError, TypeError, KeyError, AttributeError, InvalidSignature) as exc:
            raise StoreError('publisher_verification_failed', 'Publisher or build provenance verification failed.') from exc
