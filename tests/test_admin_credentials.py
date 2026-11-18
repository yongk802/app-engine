"""Migration is not rotation; concurrent rotation must revoke issued tokens."""
import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from app_engine.admin_credentials import PREFIX, hash_secret, verify_secret
from app_engine.public_origin import Accounts


def legacy_accounts(root):
    accounts = Accounts.load(root)
    accounts.data['admin']['secret_hash'] = hashlib.sha256(b'test-old-owner-secret').hexdigest()
    accounts.save()
    owner = accounts.create_admin_session()
    _, invite = accounts.create_player('alice', ['notes'])
    _, player = accounts.redeem_invite(invite, 'test-player-password')
    return accounts, owner, player


@pytest.mark.parametrize('configured', [False, True])
def test_legacy_hash_upgrades_without_revoking_existing_sessions(tmp_path, configured):
    accounts, owner, player = legacy_accounts(tmp_path)
    legacy = accounts.data['admin']['secret_hash']
    assert not accounts.verify_admin_secret('wrong')
    assert accounts.data['admin']['secret_hash'] == legacy
    if configured:
        assert accounts.ensure_admin_secret('test-old-owner-secret') is None
    else:
        assert accounts.verify_admin_secret('test-old-owner-secret')
    upgraded = Accounts.load(tmp_path)
    assert upgraded.data['admin']['secret_hash'].startswith(PREFIX + '$600000$')
    assert upgraded.resolve(owner).owner
    assert upgraded.resolve(player).role == 'player'
    assert 'test-old-owner-secret' not in upgraded.path.read_text()
    assert upgraded.sign_in_admin('test-old-owner-secret')


def test_same_secret_gets_independent_random_salts():
    first, second = hash_secret('test-owner-secret'), hash_secret('test-owner-secret')
    assert first != second
    assert verify_secret('test-owner-secret', first)
    assert verify_secret('test-owner-secret', second)
    assert not verify_secret('wrong', first)


def test_real_rotation_revokes_owner_and_player_sessions(tmp_path):
    accounts, owner, player = legacy_accounts(tmp_path)
    stale = Accounts.load(tmp_path)
    accounts.ensure_admin_secret('test-new-owner-secret')
    # A stale worker writing an unrelated approval must not restore credentials.
    stale.approve_plan('test-approved-content')
    assert stale.resolve(owner) is None
    assert stale.resolve(player) is None
    assert stale.sign_in_admin('test-old-owner-secret') is None
    token = stale.sign_in_admin('test-new-owner-secret')
    assert stale.resolve(token).owner


def test_login_and_rotation_are_serialized_across_account_instances(tmp_path, monkeypatch):
    import app_engine.public_origin as module
    accounts, _, _ = legacy_accounts(tmp_path)
    login = Accounts.load(tmp_path)
    entered, release, rotation_started, rotated = [threading.Event() for _ in range(4)]
    original = module.verify_secret

    def paused(candidate, stored):
        entered.set()
        assert release.wait(3), 'rotation/login test coordination failed'
        return original(candidate, stored)

    def rotate():
        rotation_started.set()
        accounts.set_admin_secret('test-new-owner-secret')
        rotated.set()

    monkeypatch.setattr(module, 'verify_secret', paused)
    with ThreadPoolExecutor(max_workers=2) as pool:
        signed = pool.submit(login.sign_in_admin, 'test-old-owner-secret')
        try:
            assert entered.wait(3)
            rotation = pool.submit(rotate)
            assert rotation_started.wait(3)
            assert not rotated.wait(.05), 'rotation passed an active credential transaction'
        finally:
            release.set()
        token = signed.result(timeout=5)
        rotation.result(timeout=5)
    assert token
    assert Accounts.load(tmp_path).resolve(token) is None


@pytest.mark.parametrize('stored', [None, '', 'unknown$600000$salt$digest',
    PREFIX + '$9999999999999999999999$' + 'a' * 32 + '$' + 'b' * 64,
    PREFIX + '$600000$bad$' + 'b' * 64,
    PREFIX + '$600000$' + 'a' * 32 + '$short'])
def test_malformed_hashes_are_rejected_before_expensive_derivation(stored, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('malformed hash started an expensive derivation')
    monkeypatch.setattr(hashlib, 'pbkdf2_hmac', forbidden)
    assert not verify_secret('test-secret', stored)
