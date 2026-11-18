"""A hostile store cannot replace its own checksum and impersonate trusted keys."""
import base64
from dataclasses import asdict, replace
import json

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
import httpx
import pytest

from app_engine.publisher_trust import PublisherPolicy, canonical, release_statement
from app_engine.store_bridge import StoreBridge
from app_engine.store_contracts import Release, StoreError


def public(key):
    return base64.b64encode(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()


def fixture():
    publisher, builder, rotated = [Ed25519PrivateKey.generate() for _ in range(3)]
    identity = {'keys': {'old': public(publisher), 'new': public(rotated)}, 'revoked_keys': [],
                'apps': ['demo'], 'repository': 'https://example.test/repo', 'builders': ['ci']}
    raw = {'mode': 'require_signed', 'publishers': {'owner': identity}, 'builders': {'ci': {
        'keys': {'build': public(builder)}, 'revoked_keys': [], 'apps': ['demo'],
        'repository': identity['repository']}}}
    release = Release('demo', '1.0.0', 'Demo', 'D', '', (), 'Owner', '', 'demo.zip', 'a'*64, 100)
    return raw, release, publisher, builder, rotated


def sign(release, publisher, builder_key, key_id='old', **provenance_changes):
    statement = {'type': 'app-engine-provenance-v1', 'app_id': release.id, 'version': release.version,
                 'sha256': release.sha256, 'size_bytes': release.size_bytes, 'builder': 'ci',
                 'repository': 'https://example.test/repo', 'commit': '1'*40, **provenance_changes}
    provenance = {'statement': statement, 'key_id': 'build',
                  'signature': base64.b64encode(builder_key.sign(canonical(statement))).decode()}
    envelope = {'publisher': 'owner', 'key_id': key_id, 'provenance': provenance,
                'signature': base64.b64encode(publisher.sign(canonical(
                    release_statement(release, 'owner', key_id, provenance)))).decode()}
    return replace(release, attestation=json.dumps(envelope))


def test_signatures_bind_archive_listing_and_all_metadata():
    raw, release, publisher, builder, _ = fixture()
    signed = sign(release, publisher, builder)
    policy = PublisherPolicy(raw)
    policy.verify(signed)
    for changes in ({'sha256': 'b'*64}, {'version': '2'}, {'id': 'other'},
                    {'author': 'Impersonator'}, {'package_url': 'evil.zip'}, {'size_bytes': 200}):
        with pytest.raises(StoreError, match='verification failed'):
            policy.verify(replace(signed, **changes))
    # Even a compromised publisher key cannot forge an independently trusted builder.
    attacker = Ed25519PrivateKey.generate()
    with pytest.raises(StoreError):
        policy.verify(sign(release, publisher, attacker))
    with pytest.raises(StoreError):
        policy.verify(sign(release, attacker, builder))


@pytest.mark.parametrize('change', [{'repository': 'https://evil.test/repo'}, {'builder': 'unknown'},
                                   {'commit': 'not-a-commit'}, {'sha256': 'b'*64}])
def test_fully_signed_but_wrong_build_claims_fail_policy(change):
    raw, release, publisher, builder, _ = fixture()
    with pytest.raises(StoreError):
        PublisherPolicy(raw).verify(sign(release, publisher, builder, **change))


def test_rotation_revocation_and_unsigned_compatibility():
    raw, release, publisher, builder, rotated = fixture()
    old, new = sign(release, publisher, builder), sign(release, rotated, builder, 'new')
    PublisherPolicy(raw).verify(old)
    PublisherPolicy(raw).verify(new)
    raw['publishers']['owner']['revoked_keys'] = ['old']
    policy = PublisherPolicy(raw)
    with pytest.raises(StoreError):
        policy.verify(old)
    policy.verify(new)
    raw['builders']['ci']['revoked_keys'] = ['build']
    with pytest.raises(StoreError):
        PublisherPolicy(raw).verify(new)
    with pytest.raises(StoreError):
        policy.verify(release)
    PublisherPolicy().verify(release)  # Explicit legacy mode; no identity claim.
    with pytest.raises(StoreError):
        PublisherPolicy().verify(old)  # A claimed identity still needs trusted keys.


@pytest.mark.asyncio
async def test_hostile_store_with_replaced_bytes_and_checksum_cannot_install(tmp_path):
    raw, release, publisher, builder, _ = fixture()
    signed = sign(release, publisher, builder)
    listing = asdict(signed)
    listing['attestation'] = json.loads(signed.attestation)
    listing['sha256'] = 'b'*64
    requests = []
    def store(request):
        requests.append(request.url.path)
        return httpx.Response(200, json={'protocol_version': 1, 'name': 'Compromised', 'apps': [listing]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(store)) as client:
        bridge = StoreBridge(tmp_path/'state', tmp_path/'apps', client=client, publisher_policy=PublisherPolicy(raw))
        source = await bridge.add_store('https://store.test')
        with pytest.raises(StoreError) as error:
            await bridge.preview(source.id, release.id, release.version)
        assert error.value.code == 'publisher_verification_failed'
        assert requests == ['/api/v1/catalog']
        assert not (tmp_path/'apps').exists()


def test_policy_cannot_be_overwritten_by_untrusted_file_or_symlink(tmp_path):
    raw, *_ = fixture()
    path = tmp_path/'policy.json'
    path.write_text(json.dumps(raw))
    path.chmod(0o600)
    PublisherPolicy.from_file(path)
    alias = tmp_path/'alias'
    alias.symlink_to(path)
    with pytest.raises(StoreError):
        PublisherPolicy.from_file(alias)
    import os
    if os.name != 'nt':
        path.chmod(0o666)
        with pytest.raises(StoreError):
            PublisherPolicy.from_file(path)


def test_policy_with_app_writable_parent_is_not_an_independent_trust_root(tmp_path):
    import os
    if os.name == 'nt':
        pytest.skip('Unix directory permissions')
    raw, *_ = fixture()
    folder = tmp_path/'app-data'
    folder.mkdir(mode=0o770)
    folder.chmod(0o770)
    path = folder/'policy.json'
    path.write_text(json.dumps(raw))
    path.chmod(0o600)
    with pytest.raises(StoreError, match='ancestors'):
        PublisherPolicy.from_file(path)
