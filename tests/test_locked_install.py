"""Offline installation rejects altered bytes before accepting a wheel."""
import hashlib
import os
import subprocess
import sys
import zipfile


def test_hash_checked_install_rejects_tampering_and_accepts_reviewed_bytes(tmp_path):
    wheel = tmp_path / 'audit_fixture-1.0-py3-none-any.whl'
    with zipfile.ZipFile(wheel, 'w') as archive:
        archive.writestr('audit_fixture/__init__.py', 'VALUE = "reviewed fixture"\n')
        archive.writestr('audit_fixture-1.0.dist-info/METADATA',
                         'Metadata-Version: 2.1\nName: audit-fixture\nVersion: 1.0\n')
        archive.writestr('audit_fixture-1.0.dist-info/WHEEL',
                         'Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n')
        archive.writestr('audit_fixture-1.0.dist-info/RECORD', '')
    environment = {**os.environ, 'UV_OFFLINE': '1'}
    venv = tmp_path / 'venv'
    created = subprocess.run(['uv', 'venv', '--python', sys.executable, str(venv)],
                             env=environment, capture_output=True, text=True, check=False, timeout=20)
    assert created.returncode == 0, created.stderr
    python = venv / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    requirement = tmp_path / 'requirements.txt'

    def install(digest):
        requirement.write_text(f'audit-fixture @ {wheel.as_uri()} --hash=sha256:{digest}\n')
        return subprocess.run(['uv', 'pip', 'install', '--python', str(python), '--require-hashes',
                               '--no-index', '--no-deps', '-r', str(requirement)],
                              env=environment, capture_output=True, text=True, check=False, timeout=20)

    rejected = install('0' * 64)
    assert rejected.returncode != 0
    assert 'hash' in rejected.stderr.lower()
    accepted = install(hashlib.sha256(wheel.read_bytes()).hexdigest())
    assert accepted.returncode == 0, accepted.stderr
    loaded = subprocess.run([str(python), '-I', '-c', 'import audit_fixture; print(audit_fixture.VALUE)'],
                            env=environment, capture_output=True, text=True, check=False, timeout=10)
    assert loaded.returncode == 0, loaded.stderr
    assert loaded.stdout.strip() == 'reviewed fixture'
