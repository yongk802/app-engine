"""Negative cases must never import or spawn app code."""
import asyncio
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from test_lifecycle import _Host, _manager, _manifest, _request
from test_multiplayer_host import load_engine, make_game

from app_engine.code_approval import tree_digest
from app_engine.contracts import ApprovalReceipt, AuthorizationError
from app_engine.multiplayer_host import MultiplayerHost, MultiplayerUnavailable
from app_engine.process_isolation import IsolationMode, ProcessIsolationPolicy


def test_approval_accepts_consistent_handle_timestamp_precision(tmp_path, monkeypatch):
    import app_engine.code_approval as approval
    root = tmp_path / 'app'
    root.mkdir()
    source = root / 'server.py'
    source.write_text('reviewed code')
    os.utime(source, ns=(1700000000123456700, 1700000000123456700))
    expected = tree_digest(root)
    fstat = os.fstat

    def handle_stat(fd):
        value = fstat(fd)
        fields = {name: getattr(value, name) for name in dir(value) if name.startswith('st_')}
        for name in ('st_mtime_ns', 'st_ctime_ns'):
            fields[name] = fields[name] // 1_000_000_000 * 1_000_000_000
        return SimpleNamespace(**fields)

    monkeypatch.setattr(approval, 'os', SimpleNamespace(walk=os.walk, fstat=handle_stat))
    assert tree_digest(root) == expected


def test_approval_rechecks_path_identity_after_hashing(tmp_path, monkeypatch):
    import app_engine.code_approval as approval
    root = tmp_path / 'app'
    root.mkdir()
    source = root / 'server.py'
    source.write_text('reviewed')
    replacement = tmp_path / 'replacement'
    replacement.write_text('tampered')
    fstat = os.fstat
    original = None

    def handle_stat(fd):
        nonlocal original
        if original is None:
            original = fstat(fd)
        else:
            # Even unchanged handle metadata must not hide a pathname swap.
            os.replace(replacement, source)
        return original

    monkeypatch.setattr(approval, 'os', SimpleNamespace(walk=os.walk, fstat=handle_stat))
    with pytest.raises((ValueError, PermissionError)) as failure:
        tree_digest(root)
    if isinstance(failure.value, PermissionError):
        # Windows denies replacing this open file in the first place.
        assert os.name == 'nt'
        assert source.read_text() == 'reviewed'
        assert replacement.read_text() == 'tampered'
    else:
        assert 'changed during approval' in str(failure.value)
        assert source.read_text() == 'tampered'


@pytest.mark.asyncio
@pytest.mark.parametrize('changed', ['server.py', 'helper.py', '.hidden-data'])
async def test_omitted_inputs_cannot_keep_an_old_managed_approval(tmp_path, monkeypatch, changed):
    root = tmp_path / 'app'
    manager = _manager(tmp_path, _manifest(root), host=_Host(approval_required=True))
    plan = await manager.preview(_request())
    receipt = ApprovalReceipt(plan.fingerprint, 'alice', datetime.now(timezone.utc))
    (root / changed).write_text('changed after approval')

    async def forbidden(*args, **kwargs):
        pytest.fail('Unapproved content spawned a process')

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', forbidden)
    with pytest.raises(AuthorizationError):
        await manager.launch(_request(), receipt)


@pytest.mark.parametrize('mode', list(IsolationMode))
@pytest.mark.parametrize('external', ['', 'http://127.0.0.1:1'])
def test_blocked_multiplayer_never_spawns_or_uses_override(tmp_path, mode, external, monkeypatch):
    root = make_game(tmp_path / 'apps', 'cards')
    host = MultiplayerHost(tmp_path / 'state', external=external,
                          approval_check=lambda fingerprint: True,
                          isolation_policy=ProcessIsolationPolicy(mode, blocked_app_ids=frozenset({'cards'})))

    async def forbidden(*args, **kwargs):
        pytest.fail('Blocked multiplayer code spawned')

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', forbidden)
    with pytest.raises(MultiplayerUnavailable, match='blocked'):
        asyncio.run(host.ensure('cards', root, {'rules': 'rules.mjs'}))
    assert not (tmp_path / 'state').exists()


def test_multiplayer_approval_is_owner_only_content_bound_and_precedes_spawn(tmp_path, monkeypatch):
    root = make_game(tmp_path / 'apps', 'cards')
    module = load_engine(tmp_path, monkeypatch, approve=False)
    with TestClient(module.app) as client:
        plan = client.get('/api/multiplayer-plans/cards').json()
        assert not plan['approved']
        with pytest.raises(MultiplayerUnavailable, match='approve'):
            asyncio.run(module._multiplayer.ensure('cards', root, {'rules': 'rules.mjs'}))
        assert module._multiplayer._processes == {}
        endpoint = '/api/multiplayer-plans/cards/approve'
        assert client.post(endpoint, json=plan).status_code == 403
        headers = {'x-app-engine-admin': module._admin_capability}
        assert client.post(endpoint, json=plan, headers={**headers, 'host': 'cards.localhost'}).status_code == 403
        assert client.post(endpoint, json=plan, headers=headers).status_code == 200
        assert client.get('/api/multiplayer-plans/cards').json()['approved']
        (root / 'undeclared.mjs').write_text('export const value = 2;')
        assert client.post(endpoint, json=plan, headers=headers).status_code == 409
        assert not client.get('/api/multiplayer-plans/cards').json()['approved']
        with pytest.raises(MultiplayerUnavailable, match='approve'):
            asyncio.run(module._multiplayer.ensure('cards', root, {'rules': 'rules.mjs'}))


@pytest.mark.skipif(os.name == 'nt', reason='Unix runner installation')
def test_fresh_runner_installation_imports_all_declared_modules_and_runs_fixture(tmp_path):
    repo = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location('runner_installer', repo / 'scripts/install-isolated-runner.py')
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    package = tmp_path / 'app_engine'
    package.mkdir()
    (package / '__init__.py').touch()
    for name in installer.MODULES:
        shutil.copyfile(repo / 'app_engine' / name, package / name)
    shutil.copyfile(repo / 'app_engine/isolated_exec.py', tmp_path / 'isolated_exec.py')
    fixture = tmp_path / 'fixture'
    fixture.mkdir()
    marker = fixture / 'ran'
    # Use the installed decoder and executor with only test-owned allowed roots.
    code = '''import sys,json,os
sys.path.insert(0,sys.argv[1])
from app_engine import isolation_broker, isolated_units, isolation_wire
from isolated_exec import decode_plan
from pathlib import Path
plan=decode_plan(sys.stdin.buffer.read(), roots=(Path(sys.argv[2]),))
os.chdir(plan.cwd)
os.execve(plan.argv[0],plan.argv,plan.env)
'''
    plan = {'version': 1, 'argv': [sys.executable, '-c', 'from pathlib import Path; Path("ran").write_text("ok")'],
            'cwd': str(fixture), 'env': {}}
    result = subprocess.run([sys.executable, '-I', '-S', '-c', code, str(tmp_path), str(fixture)],
                            input=json.dumps(plan), capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert marker.read_text() == 'ok'


@pytest.mark.skipif(os.name == 'nt', reason='Unix links')
def test_approval_rejects_symlink_and_hardlink_inputs(tmp_path):
    root = tmp_path / 'app'
    root.mkdir()
    outside = tmp_path / 'outside'
    outside.write_text('private')
    link = root / 'module'
    link.symlink_to(outside)
    with pytest.raises(ValueError, match='regular'):
        tree_digest(root)
    link.unlink()
    os.link(outside, link)
    with pytest.raises(ValueError, match='regular'):
        tree_digest(root)
