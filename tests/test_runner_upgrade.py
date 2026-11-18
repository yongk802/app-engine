"""Runner upgrades preserve operator-owned grants; scope changes need review."""
import importlib.util
import json
import os
from pathlib import Path

import pytest

if os.name == 'nt':
    pytest.skip('The root/systemd installer requires Unix account APIs', allow_module_level=True)

spec = importlib.util.spec_from_file_location('install_runner', Path(__file__).parents[1]/'scripts/install-isolated-runner.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


def test_upgrade_preserves_capabilities_and_resource_limits(tmp_path):
    path = tmp_path/'policy.json'
    desired = {'apps_roots': ['/apps'], 'state_root': '/state'}
    existing = {**desired, 'service_ports': {'demo': [18792]},
                'package_tracker': {'test-owned': 'capability'},
                'resource_limits': {'memory_bytes': 134217728}}
    path.write_text(json.dumps(existing))
    path.chmod(0o600)
    merged = installer._preserve_policy(json.dumps(desired).encode(), path, owner_uid=os.getuid() if os.name!='nt' else 0)
    assert json.loads(merged) == existing
    assert json.loads(path.read_text()) == existing


@pytest.mark.parametrize('existing', [{'apps_roots': ['/other'], 'state_root': '/state'},
                                      {'apps_roots': ['/apps'], 'state_root': '/state', 'unknown': True}, None])
def test_upgrade_refuses_to_retarget_or_discard_unknown_policy(tmp_path, existing):
    path = tmp_path/'policy.json'
    path.write_text(json.dumps(existing))
    path.chmod(0o600)
    with pytest.raises(ValueError):
        installer._preserve_policy(b'{"apps_roots":["/apps"],"state_root":"/state"}', path,
                                   owner_uid=os.getuid() if os.name!='nt' else 0)
    assert json.loads(path.read_text()) == existing
