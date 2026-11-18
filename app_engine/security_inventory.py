"""Read-only, secret-free deployment evidence. Unknown evidence never passes."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import plistlib
import stat
import subprocess
import urllib.request

ENV_KEYS = frozenset({'APP_ENGINE_PROCESS_ISOLATION', 'APP_ENGINE_ISOLATED_APP_IDS',
                     'APP_ENGINE_BLOCKED_APP_IDS', 'APP_ENGINE_HOST', 'APP_ENGINE_PORT',
                     'APP_ENGINE_PUBLIC_ORIGIN', 'APP_ENGINE_STATE_DIR',
                     'APP_ENGINE_APPS_DIR', 'APP_ENGINE_PUBLISHER_POLICY'})


def file_evidence(path: Path) -> dict:
    """Record permissions/digest, never file contents or credential values."""
    try:
        info = path.lstat()
        return {'exists': True, 'symlink': stat.S_ISLNK(info.st_mode),
                'uid': info.st_uid, 'gid': info.st_gid,
                'mode': oct(stat.S_IMODE(info.st_mode)),
                'world_accessible': bool(info.st_mode & 0o007),
                'group_writable': bool(info.st_mode & 0o020)}
    except OSError:
        return {'exists': False, 'review': 'unverified'}


def review(snapshot: dict) -> list[dict]:
    results = []
    def check(name, status, detail):
        results.append({'check': name, 'status': status, 'detail': detail})
    env = snapshot.get('environment', {})
    mode = env.get('APP_ENGINE_PROCESS_ISOLATION')
    if mode == 'audit':
        check('process_containment', 'trusted_host', 'Audit mode grants app code host privileges.')
    elif mode == 'enforce':
        runner = snapshot.get('runner_policy', {})
        safe = (runner.get('exists') and runner.get('uid') == 0 and not runner.get('symlink')
                and not runner.get('group_writable') and not runner.get('world_accessible'))
        check('runner_policy_permissions', 'pass' if safe else 'fail', 'Enforced mode requires a private root-owned runner policy.')
        check('effective_kernel_containment', 'unverified', 'Verify installed code, real unit policy, storage mounts and kernel probes on this host.')
    else:
        check('process_containment', 'unverified', 'Effective isolation mode not established; source defaults are insufficient evidence.')
    state = snapshot.get('state_permissions', {})
    if not state.get('exists'):
        check('state_permissions', 'unverified', 'State path not available.')
    else:
        check('state_permissions', 'fail' if state.get('world_accessible') or state.get('symlink') else 'pass',
              'Group access may be required by the isolated runner; unrelated users must have no access.')
    for name in ('tls_and_proxy', 'wildcard_ownership', 'keys_and_backups', 'dependency_advisories'):
        evidence = snapshot.get(name, {})
        check(name, evidence.get('status', 'unverified'), evidence.get('detail', 'No current evidence supplied.'))
    return results


def command(*argv: str) -> str:
    result = subprocess.run(argv, capture_output=True, text=True, timeout=10, check=True)
    return result.stdout.strip()


def collect(repo: Path, state: Path | None = None, plist: Path | None = None,
            service: str | None = None, endpoint: str | None = None) -> dict:
    snapshot = {'observed_at': datetime.now(timezone.utc).isoformat(),
                'repository': str(repo), 'environment': {}}
    try:
        snapshot['commit'] = command('git', '-C', str(repo), 'rev-parse', 'HEAD')
        snapshot['tracked_clean'] = not bool(command('git', '-C', str(repo), 'status', '--porcelain', '--untracked-files=no'))
    except (OSError, subprocess.SubprocessError):
        snapshot['commit'] = 'unverified'
    if plist is not None:
        raw = plistlib.loads(plist.read_bytes())
        snapshot['environment'] = {k: v for k, v in raw.get('EnvironmentVariables', {}).items() if k in ENV_KEYS}
        snapshot['environment_source'] = 'launchd plist (runtime activation must be reconciled separately)'
        snapshot['service_file'] = file_evidence(plist)
    if service is not None:
        # Only use a literal, validated service name; no shell and no service changes.
        import re
        if not re.fullmatch(r'[a-zA-Z0-9_.@-]{1,128}', service):
            raise ValueError('invalid service name')
        pid = int(command('systemctl', 'show', '--value', '--property=MainPID', service))
        if pid > 0:
            snapshot['pid'] = pid
            raw = Path(f'/proc/{pid}/environ').read_bytes()
            # Do not serialize the unfiltered environment (including secrets).
            for pair in raw.split(b'\0'):
                name, separator, value = pair.partition(b'=')
                if separator and name.decode(errors='replace') in ENV_KEYS:
                    snapshot['environment'][name.decode()] = value.decode(errors='replace')
            del raw
            snapshot['environment_source'] = 'running service process'
            snapshot['service_boundary'] = {key: command('systemctl', 'show', '--value', f'--property={key}', service)
                for key in ('User', 'Group', 'NoNewPrivileges', 'ProtectSystem', 'ProtectHome', 'PrivateTmp', 'CapabilityBoundingSet')}
    state = state or (Path(snapshot['environment']['APP_ENGINE_STATE_DIR'])
                       if snapshot['environment'].get('APP_ENGINE_STATE_DIR') else None)
    if state is not None:
        snapshot['state_permissions'] = file_evidence(state)
    snapshot['runner_policy'] = file_evidence(Path('/etc/app-engine-runner.json'))
    if endpoint:
        # Operator provides a metadata endpoint. TLS uses normal certificate validation.
        try:
            with urllib.request.urlopen(endpoint.rstrip('/') + '/api/engine', timeout=5) as response:
                raw = json.loads(response.read(4096))
            snapshot['reported_engine'] = {key: raw.get(key) for key in ('name', 'version')}
        except (OSError, ValueError):
            snapshot['reported_engine'] = {'status': 'unverified'}
    snapshot['checks'] = review(snapshot)
    return snapshot


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, required=True)
    parser.add_argument('--state-dir', type=Path)
    parser.add_argument('--plist', type=Path)
    parser.add_argument('--service')
    parser.add_argument('--endpoint')
    args = parser.parse_args()
    print(json.dumps(collect(args.repo, args.state_dir, args.plist, args.service, args.endpoint), indent=2))


if __name__ == '__main__':
    main()
