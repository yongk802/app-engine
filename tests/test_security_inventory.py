import json
import os
import plistlib

from app_engine.security_inventory import collect, file_evidence, review


def test_missing_evidence_never_becomes_a_pass():
    checks = review({})
    assert checks and all(item['status'] == 'unverified' for item in checks)


def test_audit_mode_never_claims_containment():
    checks = review({'environment': {'APP_ENGINE_PROCESS_ISOLATION': 'audit'}})
    assert checks[0]['status'] == 'trusted_host'
    assert all(item['check'] != 'effective_kernel_containment' for item in checks)


def test_enforced_mode_still_requires_permissions_and_kernel_evidence():
    checks = review({'environment': {'APP_ENGINE_PROCESS_ISOLATION': 'enforce'},
                     'runner_policy': {'exists': True, 'uid': 1000}})
    assert checks[0]['status'] == 'fail'
    assert checks[1]['status'] == 'unverified'


def test_inventory_never_exports_secrets_from_service_configuration(tmp_path):
    path = tmp_path/'service.plist'
    path.write_bytes(plistlib.dumps({'EnvironmentVariables': {
        'LDAPPASS': 'must-not-appear', 'APP_ENGINE_ADMIN_SECRET': 'must-not-appear',
        'APP_ENGINE_PROCESS_ISOLATION': 'audit'}}))
    value = collect(tmp_path, plist=path)
    encoded = json.dumps(value)
    assert 'must-not-appear' not in encoded and 'LDAPPASS' not in encoded
    assert value['environment']['APP_ENGINE_PROCESS_ISOLATION'] == 'audit'


def test_world_readable_state_is_reported(tmp_path):
    if os.name == 'nt':
        return
    tmp_path.chmod(0o755)
    snapshot = {'state_permissions': file_evidence(tmp_path)}
    check = next(item for item in review(snapshot) if item['check'] == 'state_permissions')
    assert check['status'] == 'fail'
