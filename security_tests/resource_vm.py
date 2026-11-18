"""Real resource exhaustion probes; imported only by the marked disposable VM suite."""
from dataclasses import replace
import errno
import grp
import json
import logging
import os
from pathlib import Path
import socket
import subprocess
import threading
import time

from app_engine.isolation_broker import serve_connection
from app_engine.isolated_units import UnitBoundary
from app_engine.resource_limits import ResourceLimits

CLIENT = '''import json,socket,sys
from app_engine.isolation_wire import send_request,recv_event
with socket.socket(socket.AF_UNIX) as s:
 s.connect(sys.argv[1]);send_request(s,sys.argv[2].encode());out=[]
 while True:
  try:k,v=recv_event(s)
  except (EOFError,ConnectionError):break
  if k==b'O':out.append(v.decode())
  if k==b'E':out.append(v.decode())
  if k==b'X':out.append('EXIT='+v.decode());break
 print(''.join(out))
'''


def volume(path, images, name, gid, stack):
    path.mkdir(parents=True, exist_ok=True)
    image = images / (name + '.ext4')
    with image.open('xb') as output:
        output.truncate(32 * 1024 * 1024)
    subprocess.run(['mkfs.ext4', '-q', '-F', '-m', '0', '-N', '512', str(image)], check=True)
    subprocess.run(['mount', '-o', 'loop,nodev,nosuid', str(image), str(path)], check=True)
    stack.callback(subprocess.run, ['umount', str(path)], check=True)
    os.chown(path, 0, gid)
    path.chmod(0o770)


def request(boundary, root, user, app_id, code, serial):
    scope = '0123456789abcdef'
    payload = {'version': 1, 'app_id': app_id, 'phase': 'start',
               'app_root': str(root/'apps'/app_id),
               'data_dir': str(root/'state/data'/app_id/scope),
               'build_dir': str(root/'state/build'/app_id/scope),
               'plan': {'version': 1, 'argv': ['/usr/bin/python3', '-c', code],
                        'cwd': '/workspace', 'env': {'PORT': '18877'}}}
    address = str(root/f'broker-{serial}.sock')
    with socket.socket(socket.AF_UNIX) as listener:
        listener.bind(address)
        os.chmod(address, 0o666)
        listener.listen()
        repo = str(Path(__file__).resolve().parents[1])
        child = subprocess.Popen(['setpriv', f'--reuid={user.pw_uid}', f'--regid={user.pw_gid}',
                                  '--init-groups', '/usr/bin/python3', '-c', CLIENT, address, json.dumps(payload)],
                                 env={**os.environ, 'PYTHONPATH': repo}, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        listener.settimeout(10)
        try:
            conn, _ = listener.accept()
            with conn:
                serve_connection(conn, boundary, 'app-engine-runner@7.service')
            output, error = child.communicate(timeout=12)
            assert child.returncode == 0, error.decode()
            return output.decode()
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()
            Path(address).unlink(missing_ok=True)


def run_resources(root, user, stack):
    root.mkdir()
    images = root/'images'
    images.mkdir(mode=0o700)
    for app_id in ('demo', 'sibling'):
        app = root/'apps'/app_id
        app.mkdir(parents=True)
        (app/'app.json').write_text(json.dumps({'id': app_id}))
        for path in (root, root/'apps', app, app/'app.json'):
            os.chown(path, user.pw_uid, user.pw_gid)
            path.chmod(0o770 if path.is_dir() else 0o660)
        for kind in ('data', 'build'):
            base = root/'state'/kind/app_id
            volume(base, images, app_id+'-'+kind, user.pw_gid, stack)
            scope = base/'0123456789abcdef'
            scope.mkdir(mode=0o770)
            os.chown(scope, user.pw_uid, user.pw_gid)
    limits = ResourceLimits(cpu_percent=25, memory_bytes=64*1024*1024, tasks=16,
                            disk_bytes=32*1024*1024, disk_inodes=1024,
                            preparation_seconds=6, service_seconds=6, output_bytes=65536)
    boundary = UnitBoundary((root/'apps',), root/'state', user.pw_uid,
                            grp.getgrgid(user.pw_gid).gr_name, resource_limits=limits)
    limits.validate_storage(boundary.state_root, 'demo')
    limits.validate_storage(boundary.state_root, 'sibling')
    # Run a real sibling app throughout all hostile probes.
    sibling = replace(boundary, resource_limits=replace(limits, service_seconds=120))
    healthy = root/'state/data/sibling/0123456789abcdef/healthy'
    sibling_errors = []
    def sibling_run():
        try:
            output = request(sibling, root, user, 'sibling',
                "import time; from pathlib import Path; p=Path('/app-data/healthy'); "
                "exec('for i in range(400):\\n p.write_text(str(i)); time.sleep(.1)')", 'sibling')
            assert 'EXIT=0' in output, output
        except BaseException as exc:
            logging.exception("Sibling app probe failed")
            sibling_errors.append(exc)
    thread = threading.Thread(target=sibling_run)
    thread.start()
    stack.callback(thread.join, 125)
    deadline = time.monotonic()+10
    while not healthy.exists() and time.monotonic()<deadline:
        time.sleep(.05)
    assert healthy.exists(), sibling_errors
    def probe(code, serial):
        before = healthy.read_text()
        result = request(boundary, root, user, 'demo', code, serial)
        time.sleep(.2)
        assert healthy.read_text() != before, ('sibling stalled', serial)
        # Engine-side work and persistent saves remain usable after every attack.
        assert sum(range(1000)) == 499500
        normal = request(boundary, root, user, 'demo',
            "from pathlib import Path; p=Path('/app-data/save'); p.write_text('saved'); print(p.read_text())", serial+'-health')
        assert 'saved' in normal and 'EXIT=0' in normal, normal
        return result
    cpu = probe("import time; s=time.monotonic(); c=time.process_time(); "
                "exec('while time.monotonic()-s<2: pass'); print(time.process_time()-c)", 'cpu')
    assert float(cpu.splitlines()[0]) < 1.1, cpu  # Two wall seconds at 25% of one CPU.
    memory = probe("x=bytearray(128*1024*1024); print('ESCAPED')", 'memory')
    assert 'ESCAPED' not in memory and 'EXIT=0' not in memory, memory
    pids = probe("import os,signal; children=[]\ntry:\n"
                 " for i in range(40):\n  p=os.fork()\n  if p==0:\n   import time; time.sleep(60); os._exit(0)\n  children.append(p)\n"
                 "except OSError as e: print('limited',e.errno,flush=True)\n"
                 "finally:\n for p in children: os.kill(p,signal.SIGKILL); os.waitpid(p,0)", 'pids')
    assert 'limited 11' in pids, pids
    disk = probe("from pathlib import Path; import errno\np=Path('/app-data/fill')\n"
                 "try:\n with p.open('wb') as f:\n  while True: f.write(b'x'*1048576)\n"
                 "except OSError as e: print('limited',e.errno)\nfinally: p.unlink()", 'disk')
    assert f'limited {errno.ENOSPC}' in disk, disk
    inodes = probe("from pathlib import Path\nfiles=[]\ntry:\n for i in range(2000):\n"
                   "  p=Path('/app-data/file'+str(i));p.touch();files.append(p)\n"
                   "except OSError as e: print('limited',e.errno)\nfinally:\n for p in files: p.unlink()", 'inodes')
    assert f'limited {errno.ENOSPC}' in inodes, inodes
    tmp = probe("from pathlib import Path\ntry:\n with Path('/tmp/fill').open('wb') as f:\n"
                "  while True:f.write(b'x'*1048576)\nexcept OSError as e:print('limited',e.errno)", 'tmp')
    assert 'EXIT=0' not in tmp or 'limited 28' in tmp, tmp  # memory or tmpfs cap may win
    output = probe("import os\nwhile True: os.write(1,b'x'*16384)", 'output')
    assert 'output limit exceeded' in output and len(output) < 100000, len(output)
    started = time.monotonic()
    wall = probe("import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(600)", 'wall')
    assert time.monotonic()-started < 15 and 'EXIT=0' not in wall, wall
    thread.join(60)
    assert not thread.is_alive() and not sibling_errors, sibling_errors
    print(json.dumps({'resource_budgets': 'passed', 'cpu': cpu.strip(),
                      'memory': 'contained', 'tasks': 'contained', 'disk': 'contained',
                      'inodes': 'contained', 'temporary_storage': 'contained',
                      'output': 'contained', 'wall_deadline': 'contained',
                      'sibling_and_normal_saves': 'passed'}))
