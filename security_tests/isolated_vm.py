"""Destructive only inside an explicitly marked disposable Linux VM.

Run as root with --engine-user naming an unprivileged, test-owned account.
No packages, application credentials or host mounts are needed.
"""
from __future__ import annotations

import argparse
import json
import os
import pwd
import shutil
import socket
import socketserver
import subprocess
import tempfile
import threading
import time
from contextlib import ExitStack
from pathlib import Path

from app_engine.isolated_units import UnitBoundary, UnitRequest

APP = r'''
import json, os, socket
from pathlib import Path
out = {}
for name, path in json.loads(os.environ['PRIVATE_PATHS']).items():
    try:
        Path(path).read_bytes()
        out[name] = 'EXPOSED'
    except OSError:
        out[name] = 'denied'
for name, port in json.loads(os.environ['TCP_PORTS']).items():
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=1) as sock:
            out[name] = sock.recv(32).decode()
    except OSError:
        out[name] = 'denied'
for name, address in [('unix_file', os.environ['UNIX_FILE']), ('unix_abstract', '\0ae-vm-private')]:
    try:
        with socket.socket(socket.AF_UNIX) as sock:
            sock.connect(address)
        out[name] = 'EXPOSED'
    except OSError:
        out[name] = 'denied'
for name, protocol in [('sctp', 132), ('multipath_tcp', 262)]:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM, protocol):
            out[name] = 'EXPOSED'
    except OSError as error:
        out[name] = 'denied' if error.errno == 1 else 'UNVERIFIED'
left, right = socket.socketpair()
try:
    right.close()
    left.connect('\0ae-vm-private')
    out['socketpair_reconnect'] = 'EXPOSED'
except OSError:
    out['socketpair_reconnect'] = 'denied'
finally:
    left.close()
try:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.sendto(b'canary', ('127.0.0.1', int(os.environ['UDP_PORT'])))
    out['udp'] = 'EXPOSED'
except OSError:
    out['udp'] = 'denied'
try:
    with socket.create_connection((os.environ['EXTERNAL_HOST'], json.loads(os.environ['TCP_PORTS'])['allowed']), timeout=1):
        out['external_network'] = 'EXPOSED'
except OSError as error:
    out['external_network'] = 'denied'
out['capability_host'] = socket.gethostbyname('atrium-status.localhost')
out['own_data'] = Path('/app-data/private').read_text()
Path('/app-build/result').write_text('own build works')
with socket.socket() as listener:
    listener.bind(('127.0.0.1', int(os.environ['PORT'])))
    listener.listen()
    listener.settimeout(10)
    sock, _ = listener.accept()
    with sock:
        sock.sendall(b'app health ok')
Path('/app-data/result.json').write_text(json.dumps(out))
print(json.dumps(out), flush=True)
'''


class Canary(socketserver.BaseRequestHandler):
    def handle(self):
        self.server.hits += 1
        self.request.sendall(b'allowed service')


def server(stack):
    service = socketserver.ThreadingTCPServer(('0.0.0.0', 0), Canary)
    service.hits = 0
    thread = threading.Thread(target=service.serve_forever, daemon=True)
    thread.start()
    stack.callback(thread.join, 3)
    stack.callback(service.server_close)
    stack.callback(service.shutdown)
    return service


def prepare(root, user):
    app = root / 'apps/demo'
    data = root / 'state/data/demo/0123456789abcdef'
    build = root / 'state/build/demo/0123456789abcdef'
    other = root / 'state/data/demo/fedcba9876543210/private'
    sibling = root / 'apps/sibling/private'
    for directory in (app, data, build, other.parent, sibling.parent):
        directory.mkdir(parents=True)
    (app / 'app.json').write_text('{"id":"demo"}')
    (app / 'probe.py').write_text(APP)
    for private in (data / 'private', other, sibling):
        private.write_text('test canary')
    for path in (root, *root.rglob('*')):
        os.chown(path, user.pw_uid, user.pw_gid)
        path.chmod(0o770 if path.is_dir() else 0o660)
    return app, data, build, other, sibling


def install_executor():
    repo = Path(__file__).resolve().parents[1]
    lib = Path('/usr/local/libexec/app-engine')
    package = lib / 'app_engine'
    package.mkdir(parents=True, exist_ok=True)
    (package / '__init__.py').touch()
    shutil.copyfile(repo / 'app_engine/isolated_exec.py', lib / 'isolated_exec.py')
    shutil.copyfile(repo / 'app_engine/network_guard.py', package / 'network_guard.py')
    root = Path('/var/lib/app-engine-runner/root')
    (root / 'etc').mkdir(parents=True, exist_ok=True)
    (root / 'etc/hosts').write_text('127.0.0.1 localhost atrium-status.localhost\n::1 localhost\n')
    (root / 'etc/nsswitch.conf').write_text('hosts: files\n')


def run(root, user, stack):
    import grp
    app, data, build, other, sibling = prepare(root, user)
    permitted, engine, neighbor = [server(stack) for _ in range(3)]
    udp = stack.enter_context(socket.socket(socket.AF_INET, socket.SOCK_DGRAM))
    udp.bind(('127.0.0.1', 0))
    unix = stack.enter_context(socket.socket(socket.AF_UNIX))
    unix_path = str(root / 'privileged.sock')
    unix.bind(unix_path)
    unix.listen()
    abstract = stack.enter_context(socket.socket(socket.AF_UNIX))
    abstract.bind('\0ae-vm-private')
    abstract.listen()
    with socket.socket() as reserve:
        reserve.bind(('127.0.0.1', 0))
        port = reserve.getsockname()[1]
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route_probe:
        route_probe.connect(('192.0.2.1', 9))  # Route lookup, no packet is sent.
        interface = route_probe.getsockname()[0]
    with socket.create_connection((interface, permitted.server_address[1]), timeout=1) as probe:
        assert probe.recv(32) == b'allowed service'
    environment = {'EXTERNAL_HOST': interface, 'PORT': str(port), 'PRIVATE_PATHS': json.dumps({
        'other_user': str(other), 'sibling_file': str(sibling),
        'host_identity': '/etc/passwd', 'original_app_path': str(app / 'probe.py')}),
        'TCP_PORTS': json.dumps({'allowed': permitted.server_address[1],
                                 'engine_http': engine.server_address[1],
                                 'sibling_http': neighbor.server_address[1]}),
        'UNIX_FILE': unix_path, 'UDP_PORT': str(udp.getsockname()[1])}
    boundary = UnitBoundary((root / 'apps',), root / 'state', user.pw_uid,
                            grp.getgrgid(user.pw_gid).gr_name,
                            service_ports=(('demo', (permitted.server_address[1],)),))
    # A client cannot insert its own connect grants into the broker's plan.
    payload = json.dumps({'version': 1, 'argv': ['/usr/bin/python3', '/workspace/probe.py'],
                          'cwd': '/workspace', 'env': environment,
                          'network': {'bind': [port], 'connect': [engine.server_address[1]]}})
    request = boundary.prepare_request(UnitRequest('demo', 'start', app, data, build, payload))
    command = boundary.command('ae-demo-start-abc123', request, 'app-engine-runner@7.service')
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    stack.callback(lambda: process.kill() if process.poll() is None else None)
    process.stdin.write(request.payload.encode())
    process.stdin.close()
    deadline = time.monotonic() + 20
    health = None
    while process.poll() is None and time.monotonic() < deadline:
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=.2) as client:
                health = client.recv(32)
                break
        except OSError:
            time.sleep(.05)
    code = process.wait(timeout=20)
    output = process.stdout.read().decode() + process.stderr.read().decode()
    assert code == 0, (code, output)
    assert health == b'app health ok', output
    result = json.loads((data / 'result.json').read_text())
    for name in ('other_user', 'sibling_file', 'host_identity', 'original_app_path',
                 'engine_http', 'sibling_http', 'unix_file', 'unix_abstract', 'udp', 'external_network',
                 'sctp', 'multipath_tcp', 'socketpair_reconnect'):
        assert result[name] == 'denied', result
    assert result['allowed'] == 'allowed service', result
    assert result['capability_host'] == '127.0.0.1'
    assert result['own_data'] == 'test canary'
    assert (build / 'result').read_text() == 'own build works'
    assert permitted.hits == 2 and engine.hits == neighbor.hits == 0
    print(json.dumps({'passed': True, 'checks': result, 'health': health.decode()}, sort_keys=True))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--engine-user', required=True)
    args = parser.parse_args()
    if os.geteuid() != 0 or not Path('/run/app-engine-audit-disposable').is_file():
        parser.error('Run only as root in an explicitly marked disposable VM')
    user = pwd.getpwnam(args.engine_user)
    if user.pw_uid == 0:
        parser.error('test engine user must be unprivileged')
    install_executor()
    subprocess.run(['systemd-run', '--quiet', '--unit=app-engine-runner@7', '/usr/bin/sleep', '180'], check=True)
    try:
        with tempfile.TemporaryDirectory(prefix='ae-boundary-', dir='/var/tmp') as temporary, ExitStack() as stack:
            run(Path(temporary), user, stack)
            from resource_vm import run_resources
            run_resources(Path(temporary) / "budgets", user, stack)
    finally:
        subprocess.run(['systemctl', 'stop', 'app-engine-runner@7.service'], check=False)


if __name__ == '__main__':
    main()
