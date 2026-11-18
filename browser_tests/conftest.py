"""Real engine/browser fixtures, with disposable data and no production services."""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / 'fixtures'


class FakeAI(BaseHTTPRequestHandler):
    def do_GET(self):
        value = {'models': []} if self.path == '/api/tags' else {'version': 'browser-test'}
        body = json.dumps(value).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class EngineProcess:
    def __init__(self, root: Path, ai_port: int):
        self.root = root
        self.apps = root / 'apps'
        self.state = root / 'state'
        self.apps.mkdir()
        self.state.mkdir()
        self.process = None
        self.log = None
        self.starts = 0
        self.app_ids = set()
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            self.port = sock.getsockname()[1]
        self.url = f'http://localhost:{self.port}'
        (self.state / 'local-ai.json').write_text(json.dumps({
            'schema_version': 1, 'ollama_endpoint': f'http://127.0.0.1:{ai_port}',
        }))
        self.env = {key: os.environ[key] for key in (
            'PATH', 'SYSTEMROOT', 'WINDIR', 'LANG', 'LC_ALL', 'PATHEXT',
        ) if key in os.environ}
        self.env.update({
            'HOME': str(root / 'home'), 'USERPROFILE': str(root / 'home'),
            'TEMP': str(root), 'TMP': str(root), 'TMPDIR': str(root),
            'APP_ENGINE_APPS_DIR': str(self.apps),
            'APP_ENGINE_STATE_DIR': str(self.state),
            'APP_ENGINE_HOST': '127.0.0.1', 'APP_ENGINE_PORT': str(self.port),
            'APP_ENGINE_PROCESS_ISOLATION': 'legacy', 'PYTHONUNBUFFERED': '1',
            'PYTHONPATH': str(ROOT),
        })

    def add_app(self, app_id: str, *, managed: bool = False, multiplayer: bool = False):
        directory = self.apps / app_id
        directory.mkdir()
        self.app_ids.add(app_id)
        html = (FIXTURES / 'notes.html').read_text().replace('__APP_ID__', app_id)
        (directory / 'index.html').write_text(html)
        manifest = {'id': app_id, 'label': 'Browser Notes', 'icon': 'N',
                    'sandbox': 'allow-scripts allow-same-origin allow-forms'}
        if multiplayer:
            shutil.copyfile(ROOT / 'app_engine/multiplayer/test/fake-rules.mjs', directory / 'rules.mjs')
            manifest['multiplayer'] = {'rules': 'rules.mjs'}
        if managed:
            shutil.copyfile(FIXTURES / 'server.py', directory / 'server.py')
            manifest.update({
                'manifest_version': 2, 'default_target': 'backend',
                'targets': {'backend': {'kind': 'web', 'runtime': {
                    'driver': 'process',
                    'start': {'argv': [sys.executable, 'server.py'],
                              'inputs': ['server.py', 'index.html']},
                    'health': {'path': '/health', 'timeout_seconds': 10},
                }}},
            })
        (directory / 'app.json').write_text(json.dumps(manifest))
        return directory

    def start(self):
        assert self.process is None
        self.starts += 1
        log_path = self.root / f'engine-{self.starts}.log'
        self.log = log_path.open('wb')
        self.process = subprocess.Popen(
            [sys.executable, 'engine.py'], cwd=ROOT, env=self.env,
            stdout=self.log, stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + 20
        with httpx.Client(trust_env=False, timeout=.5) as client:
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    break
                try:
                    response = client.get(f'http://127.0.0.1:{self.port}/api/engine')
                    if response.status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
                time.sleep(.05)
        self.stop()
        pytest.fail(f'Fixture engine did not start:\n{log_path.read_text()}')

    def stop(self):
        if self.process is not None:
            process, self.process = self.process, None
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
                pytest.fail('Fixture engine did not shut down gracefully')
            finally:
                self.log.close()


@pytest.fixture
def engine_server(tmp_path):
    ai = ThreadingHTTPServer(('127.0.0.1', 0), FakeAI)
    thread = threading.Thread(target=ai.serve_forever, daemon=True)
    thread.start()
    engine = EngineProcess(tmp_path, ai.server_port)
    try:
        yield engine
    finally:
        engine.stop()
        ai.shutdown()
        ai.server_close()
        thread.join(timeout=5)


@pytest.fixture(scope='session')
def browser():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(args=['--no-proxy-server'])
        yield browser
        browser.close()


@pytest.fixture
def browser_page(browser, tmp_path):
    runs = 0

    @contextmanager
    def open_page(engine):
        nonlocal runs
        runs += 1
        context = browser.new_context(service_workers='block')
        blocked, errors = [], []
        hosts = {'localhost', '127.0.0.1'} | {f'{app_id}.localhost' for app_id in engine.app_ids}
        launcher_host = urlsplit(engine.url).hostname
        hosts.add(launcher_host)
        hosts.update(f'{app_id}.{launcher_host}' for app_id in engine.app_ids)

        def restrict_network(route):
            url = urlsplit(route.request.url)
            if url.scheme == 'http' and url.hostname in hosts and url.port == engine.port:
                route.continue_()
            else:
                blocked.append(route.request.url)
                route.abort()

        context.route('**/*', restrict_network)
        context.tracing.start(screenshots=True, snapshots=True, sources=True)
        page = context.new_page()
        page.on('pageerror', lambda error: errors.append(str(error)))
        try:
            yield page
            assert not blocked, f'Unexpected browser network access: {blocked}'
            assert not errors, f'Browser JavaScript errors: {errors}'
        finally:
            page.screenshot(path=str(tmp_path / f'browser-{runs}.png'), full_page=True)
            context.tracing.stop(path=str(tmp_path / f'browser-{runs}.zip'))
            context.close()

    return open_page


@pytest.fixture
def legacy_backend():
    """A separate, explicitly approved service; it never runs app-owned scripts."""
    @contextmanager
    def serve(directory):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = (directory / 'index.html').read_bytes()
                self.send_response(200)
                self.send_header('Content-Type', 'text/html')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f'http://127.0.0.1:{server.server_port}'
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
    return serve
