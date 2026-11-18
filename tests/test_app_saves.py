"""Storage-boundary tests use only temporary directories and fake engine files."""
import json
import os
import stat

import pytest

from app_engine.app_saves import MAX_SAVE_BYTES, AppSaveStore


def test_migration_preserves_owner_and_player_saves_without_overwriting_newer_data(tmp_path):
    legacy = tmp_path / 'notes.json'
    legacy.write_bytes(b'{"draft":"original owner save"}')
    players = tmp_path / 'players'
    for player in ('p_alice', 'p_bob'):
        directory = players / player
        directory.mkdir(parents=True)
        (directory / 'notes.json').write_text(json.dumps({'draft': player}))
        # These names are ordinary saves inside a legacy player directory.
        (directory / 'players.json').write_text(json.dumps({'roster': player}))
    store = AppSaveStore(tmp_path)
    store.migrate_legacy({'notes', 'players'})
    assert json.loads(store.read('notes')) == {'draft': 'original owner save'}
    for player in ('p_alice', 'p_bob'):
        assert json.loads(store.read('notes', player)) == {'draft': player}
        assert json.loads(store.read('players', player)) == {'roster': player}
    store.write('notes', b'{"draft":"new owner save"}')
    store.write('notes', b'{"draft":"new alice save"}', 'p_alice')
    store.migrate_legacy({'notes', 'players'})
    assert json.loads(store.read('notes')) == {'draft': 'new owner save'}
    assert json.loads(store.read('notes', 'p_alice')) == {'draft': 'new alice save'}
    assert json.loads(store.read('notes', 'p_bob')) == {'draft': 'p_bob'}
    assert legacy.read_bytes() == b'{"draft":"original owner save"}'
    assert json.loads((players / 'p_alice' / 'notes.json').read_text()) == {'draft': 'p_alice'}


def test_migration_never_imports_controls_unknown_apps_or_configured_private_files(tmp_path):
    control_ids = {'players', 'stores', 'chat', 'local-ai'}
    originals = {}
    for app_id in control_ids | {'notes', 'tls-policy', 'not-installed'}:
        path = tmp_path / f'{app_id}.json'
        originals[path] = json.dumps({'canary': app_id}).encode()
        path.write_bytes(originals[path])
    store = AppSaveStore(tmp_path)
    store.migrate_legacy(control_ids | {'notes', 'tls-policy'}, excluded_paths=(tmp_path / 'tls-policy.json',))
    assert json.loads(store.read('notes')) == {'canary': 'notes'}
    for app_id in control_ids | {'tls-policy', 'not-installed'}:
        assert store.read(app_id) is None
        store.write(app_id, b'{"draft":"separate app data"}')
    assert {path: path.read_bytes() for path in originals} == originals


@pytest.mark.parametrize('raw', [b'invalid JSON', b'42', b'null', b'[]' + b' ' * MAX_SAVE_BYTES])
def test_invalid_or_oversized_legacy_save_is_preserved_but_not_imported(tmp_path, raw):
    legacy = tmp_path / 'notes.json'
    legacy.write_bytes(raw)
    store = AppSaveStore(tmp_path)
    store.migrate_legacy({'notes'})
    assert store.read('notes') is None
    assert legacy.read_bytes() == raw


@pytest.mark.parametrize('level', ['saves', 'owner', 'file'])
def test_current_storage_rejects_symlink_escape_on_read_and_write(tmp_path, level):
    root = tmp_path / 'state'
    root.mkdir()
    private = tmp_path / 'private'
    private.mkdir()
    secret = private / 'notes.json'
    secret.write_bytes(b'{"private":"canary"}')
    if level == 'saves':
        (private / 'owner').mkdir()
        (private / 'owner' / 'notes.json').write_bytes(secret.read_bytes())
        (root / 'saves').symlink_to(private, target_is_directory=True)
    elif level == 'owner':
        (root / 'saves').mkdir()
        (root / 'saves' / 'owner').symlink_to(private, target_is_directory=True)
    else:
        (root / 'saves' / 'owner').mkdir(parents=True)
        (root / 'saves' / 'owner' / 'notes.json').symlink_to(secret)
    before = {p: p.read_bytes() for p in private.rglob('*') if p.is_file()}
    store = AppSaveStore(root)
    for operation in (lambda: store.read('notes'), lambda: store.write('notes', b'{}')):
        with pytest.raises((OSError, ValueError)):
            operation()
    assert {p: p.read_bytes() for p in before} == before


def test_player_directory_cannot_link_to_owner_saves(tmp_path):
    store = AppSaveStore(tmp_path)
    store.write('notes', b'{"draft":"owner private"}')
    players = tmp_path / 'saves' / 'players'
    players.mkdir()
    (players / 'p_alice').symlink_to(tmp_path / 'saves' / 'owner', target_is_directory=True)
    with pytest.raises((OSError, ValueError)):
        store.read('notes', 'p_alice')
    with pytest.raises((OSError, ValueError)):
        store.write('notes', b'{}', 'p_alice')
    assert json.loads(store.read('notes')) == {'draft': 'owner private'}


def test_migration_skips_linked_owner_files_and_player_directories(tmp_path):
    root = tmp_path / 'state'
    root.mkdir()
    private = tmp_path / 'private'
    private.mkdir()
    secret = private / 'notes.json'
    secret.write_bytes(b'{"canary":"not app data"}')
    (root / 'notes.json').symlink_to(secret)
    (root / 'players').mkdir()
    (root / 'players' / 'p_alice').symlink_to(private, target_is_directory=True)
    store = AppSaveStore(root)
    store.migrate_legacy({'notes'})
    assert store.read('notes') is None
    assert store.read('notes', 'p_alice') is None
    assert secret.read_bytes() == b'{"canary":"not app data"}'


def test_hardlinked_save_cannot_alias_a_control_file(tmp_path):
    store = AppSaveStore(tmp_path)
    store.write('notes', b'{}')
    save = tmp_path / 'saves' / 'owner' / 'notes.json'
    save.unlink()
    control = tmp_path / 'players.json'
    control.write_bytes(b'{"canary":"accounts"}')
    os.link(control, save)
    with pytest.raises(ValueError):
        store.read('notes')
    with pytest.raises(ValueError):
        store.write('notes', b'{}')
    assert control.read_bytes() == b'{"canary":"accounts"}'


@pytest.mark.skipif(os.name == 'nt', reason='POSIX file modes and FIFOs')
def test_private_file_modes_and_special_file_rejection(tmp_path):
    store = AppSaveStore(tmp_path)
    store.write('notes', b'{"draft":1}')
    owner = tmp_path / 'saves' / 'owner'
    assert stat.S_IMODE(owner.stat().st_mode) == 0o700
    assert stat.S_IMODE((owner / 'notes.json').stat().st_mode) == 0o600
    os.mkfifo(owner / 'pipe.json')
    with pytest.raises(ValueError):
        store.read('pipe')
    with pytest.raises(ValueError):
        store.write('pipe', b'{}')


def test_save_size_limit_does_not_replace_existing_data(tmp_path):
    store = AppSaveStore(tmp_path)
    store.write('notes', b'[]')
    with pytest.raises(ValueError, match='100 KB'):
        store.write('notes', b'x' * (MAX_SAVE_BYTES + 1))
    assert store.read('notes') == b'[]'


@pytest.mark.parametrize('app_id,player_id', [('../players', None), ('notes\n', None), ('notes', '../owner')])
def test_identifiers_cannot_escape_save_scope(tmp_path, app_id, player_id):
    store = AppSaveStore(tmp_path)
    with pytest.raises(ValueError):
        store.write(app_id, b'{}', player_id)
    assert list(tmp_path.iterdir()) == []


def test_failed_atomic_replace_preserves_previous_save_and_cleans_temporary(tmp_path, monkeypatch):
    store = AppSaveStore(tmp_path)
    store.write('notes', b'{"draft":"kept"}')

    def fail(*args, **kwargs):
        raise OSError('test disk failure')

    monkeypatch.setattr(os, 'replace', fail)
    with pytest.raises(OSError):
        store.write('notes', b'{"draft":"new"}')
    assert store.read('notes') == b'{"draft":"kept"}'
    assert not list((tmp_path / 'saves' / 'owner').glob('.save-*'))


@pytest.mark.skipif(os.name == 'nt', reason='POSIX directory descriptors')
@pytest.mark.parametrize('operation', ['read', 'write'])
def test_directory_swap_during_access_cannot_redirect_to_private_files(tmp_path, monkeypatch, operation):
    root = tmp_path / 'state'
    store = AppSaveStore(root)
    store.write('notes', b'{"draft":"app save"}')
    owner = root / 'saves' / 'owner'
    private = tmp_path / 'private'
    private.mkdir()
    secret = private / 'notes.json'
    secret.write_bytes(b'{"private":"canary"}')
    original_open = os.open
    swapped = False

    def swap_then_open(name, flags, *args, **kwargs):
        nonlocal swapped
        if not swapped and kwargs.get('dir_fd') is not None and (
            name == 'notes.json' or str(name).startswith('.save-')
        ):
            owner.rename(root / 'saves' / 'original-owner')
            owner.symlink_to(private, target_is_directory=True)
            swapped = True
        return original_open(name, flags, *args, **kwargs)

    monkeypatch.setattr(os, 'open', swap_then_open)
    if operation == 'read':
        assert store.read('notes') == b'{"draft":"app save"}'
    else:
        store.write('notes', b'{"draft":"updated"}')
        assert (root / 'saves' / 'original-owner' / 'notes.json').read_bytes() == b'{"draft":"updated"}'
    assert swapped
    assert secret.read_bytes() == b'{"private":"canary"}'
