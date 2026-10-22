"""Live engine-client to Atrium-status gateway mTLS probe."""

import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import socket
import subprocess
import ssl

import httpx

import pytest


def _port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_engine_status_probe_uses_client_certificate(tmp_path):
    if not shutil.which("nginx") or not shutil.which("openssl"):
        pytest.skip("nginx and OpenSSL are required for live mTLS")
    from app_engine.atrium_status import AtriumStatusProbe
    from app_engine.outbound_tls import OutboundTlsPolicy

    root = Path(__file__).resolve().parents[1]
    bundle = tmp_path / "bundle"
    subprocess.run(["bash", str(root / "scripts/setup-mtls.sh"), str(bundle),
                    "mcp.localhost", "store.localhost", "atrium.localhost"],
                   check=True, capture_output=True)
    status_port, tls_port = _port(), _port()

    class Health(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/health":
                self.send_error(404)
                return
            body = b'{"ok":true,"version":"live-test"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    backend = ThreadingHTTPServer(("127.0.0.1", status_port), Health)
    import threading
    thread = threading.Thread(target=backend.serve_forever, daemon=True)
    thread.start()
    snippet = (bundle / "atrium-status-gateway" / "nginx-atrium-status.conf").read_text()
    snippet = snippet.replace("listen 443 ssl;", f"listen {tls_port} ssl;")
    snippet = snippet.replace("proxy_pass http://127.0.0.1:8000;",
                              f"proxy_pass http://127.0.0.1:{status_port};")
    (bundle / "atrium-status-gateway" / "nginx-atrium-status.conf").write_text(snippet)
    (bundle / "logs").mkdir()
    (bundle / "nginx.conf").write_text(
        f"error_log {bundle}/logs/error.log;\n"
        f"events {{}}\nhttp {{ access_log {bundle}/logs/access.log; "
        f"include {bundle}/atrium-status-gateway/nginx-atrium-status.conf; }}\n")
    config = bundle / "engine" / "outbound-mtls.json"
    document = json.loads(config.read_text())
    identity = document.pop("https://atrium.localhost")
    url = f"https://atrium.localhost:{tls_port}/health"
    document[f"https://atrium.localhost:{tls_port}"] = identity
    config.write_text(json.dumps(document))
    proxy = subprocess.Popen(["nginx", "-c", str(bundle / "nginx.conf"),
                              "-p", str(bundle), "-g", "daemon off;"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        policy = OutboundTlsPolicy.from_file(config)
        probe = AtriumStatusProbe(url, policy)

        async def check_until_ready():
            for _ in range(100):
                if proxy.poll() is not None:
                    raise AssertionError(f"nginx exited: {proxy.stderr.read().decode()}")
                result = await probe.check()
                if result["ok"]:
                    return result
                await asyncio.sleep(0.05)
            raise AssertionError("Atrium status mTLS probe did not connect")

        result = asyncio.run(check_until_ready())
        assert result["version"] == "live-test"
        wrong_role = ssl.create_default_context(cafile=str(bundle / "atrium" / "ca.crt"))
        wrong_role.load_cert_chain(str(bundle / "atrium" / "atrium-client.crt"),
                                   str(bundle / "atrium" / "atrium-client.key"))
        with httpx.Client(verify=wrong_role, trust_env=False, timeout=2) as client:
            assert client.get(url).status_code == 403
    finally:
        proxy.terminate()
        try:
            proxy.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proxy.kill()
            proxy.wait(timeout=5)
        backend.shutdown()
        thread.join(timeout=5)
