"""Resource policy rejects unbounded storage before starting any app code."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app_engine.resource_limits import ResourceLimits


def test_limits_are_shared_across_phases_and_scopes_and_have_stop_deadlines():
    limits = ResourceLimits()
    start = limits.unit_properties('demo', 'start')
    build = limits.unit_properties('demo', 'build')
    assert start[0] == build[0]
    assert start[0] != limits.unit_properties('sibling', 'start')[0]
    assert '--property=RuntimeMaxSec=86400' in start
    assert '--property=RuntimeMaxSec=900' in build
    assert '--property=KillMode=control-group' in start
    assert '--property=LimitCORE=0' in start
    assert any('nr_inodes=4096' in prop and '/var/tmp' in prop for prop in start)


def test_root_slice_configuration_is_finite_and_failure_propagates(monkeypatch):
    calls = Mock()
    monkeypatch.setattr('app_engine.resource_limits.subprocess.run', calls)
    ResourceLimits().configure_slice('demo')
    args = calls.call_args.args[0]
    assert {'CPUQuota=100%', 'MemoryMax=1073741824', 'MemorySwapMax=0', 'TasksMax=128'} <= set(args)
    assert calls.call_args.kwargs['check'] is True


@pytest.mark.parametrize('raw', [{'tasks': 0}, {'memory_bytes': 'infinity'}, {'cpu_percent': True},
                                  {'service_seconds': 604801}, {'unknown': 1}])
def test_unbounded_or_malformed_policy_rejected(raw):
    with pytest.raises(ValueError):
        ResourceLimits.parse(raw)


def test_ordinary_host_directories_cannot_claim_disk_containment(tmp_path):
    state = tmp_path/'state'
    for kind in ('data', 'build'):
        (state/kind/'demo').mkdir(parents=True)
    with pytest.raises(ValueError, match='bounded mounts'):
        ResourceLimits().validate_storage(state, 'demo')


def test_overlarge_shared_or_unowned_mount_rejected(tmp_path, monkeypatch):
    state = tmp_path/'state'
    for kind in ('data', 'build'):
        (state/kind/'demo').mkdir(parents=True)
    real_stat = Path.stat
    def fake_stat(path, *args, **kwargs):
        actual = real_stat(path, *args, **kwargs)
        if kwargs.get('follow_symlinks', True) and path in (state/'data/demo', state/'build/demo'):
            return SimpleNamespace(st_uid=0, st_dev=123 if 'data' in path.parts else 124)
        return actual
    monkeypatch.setattr(Path, 'stat', fake_stat)
    monkeypatch.setattr('app_engine.resource_limits.os.path.ismount', lambda path: True)
    monkeypatch.setattr('app_engine.resource_limits.os.statvfs', lambda path: SimpleNamespace(
        f_blocks=2048, f_frsize=1024*1024, f_files=100))
    with pytest.raises(ValueError, match='exceeds resource budget'):
        ResourceLimits().validate_storage(state, 'demo')
