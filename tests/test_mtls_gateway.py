"""End-to-end nginx client certificate and app-engine bearer gate."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import sys
import time

import httpx
import pytest


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_mcp_gateway_requires_client_certificate_and_bearer(tmp_path):  # noqa: C901
    if not shutil.which("nginx") or not shutil.which("openssl"):
        pytest.skip("nginx and OpenSSL are required for the live mTLS gateway probe")
    root = Path(__file__).resolve().parents[1]
    bundle = tmp_path / "certs"
    subprocess.run(["bash", str(root / "scripts/setup-mtls.sh"), str(bundle),
                    "mcp.localhost", "store.localhost"], check=True, capture_output=True)
    assert not (bundle / "ca.key").exists()
    assert (tmp_path / "certs.ca-offline" / "ca.key").is_file()
    assert not (bundle / "engine" / "atrium-client.key").exists()
    assert not (bundle / "atrium" / "app-engine-client.key").exists()
    engine_port, proxy_port = _free_port(), _free_port()
    secret = (bundle / "engine" / "app-engine-mcp.env").read_text().strip().split("=", 1)[1]
    state = tmp_path / "state"
    (state / ".assistant").mkdir(parents=True)
    key = "ae_gateway_integration_probe"
    (state / ".assistant" / "settings.json").write_text(json.dumps({
        "chat_url": "", "key_hash": hashlib.sha256(key.encode()).hexdigest()}))
    env = {**os.environ, "APP_ENGINE_STATE_DIR": str(state),
           "APP_ENGINE_APPS_DIR": str(tmp_path / "apps"),
           "APP_ENGINE_PUBLIC_ORIGIN": "https://play.localhost",
           "APP_ENGINE_ADMIN_SECRET": "gateway-integration-admin-secret",
           "APP_ENGINE_MCP_PROXY_SECRET": secret,
           "APP_ENGINE_MCP_URL": f"https://mcp.localhost:{proxy_port}/api/mcp"}
    engine = subprocess.Popen([sys.executable, "-m", "uvicorn", "engine:app", "--host",
                               "127.0.0.1", "--port", str(engine_port), "--no-access-log"],
                              cwd=root, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    snippet = (bundle / "mcp-gateway" / "nginx-app-engine-mcp.conf").read_text()
    assert "listen [::]:443 ssl;" in snippet
    snippet = snippet.replace("listen 443 ssl;", f"listen {proxy_port} ssl;")
    snippet = snippet.replace("listen [::]:443 ssl;", "")
    snippet = snippet.replace("proxy_pass http://127.0.0.1:8770;",
                              f"proxy_pass http://127.0.0.1:{engine_port};")
    (bundle / "mcp-gateway" / "nginx-app-engine-mcp.conf").write_text(snippet)
    (bundle / "logs").mkdir()
    (bundle / "nginx.conf").write_text(
        f"pid {bundle}/logs/nginx.pid;\n"
        f"error_log {bundle}/logs/error.log;\n"
        f"events {{}}\nhttp {{ access_log {bundle}/logs/access.log; "
        f"include {bundle}/mcp-gateway/nginx-app-engine-mcp.conf; }}\n")
    proxy = None
    try:
        for _ in range(100):
            if engine.poll() is not None:
                raise AssertionError(f"app-engine exited: {engine.stderr.read().decode()}")
            try:
                if httpx.get(f"http://127.0.0.1:{engine_port}/api/engine", timeout=0.2).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.05)
        else:
            raise AssertionError("app-engine did not start")
        proxy = subprocess.Popen(["nginx", "-c", str(bundle / "nginx.conf"),
                                  "-p", str(bundle), "-g", "daemon off;"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        url = f"https://mcp.localhost:{proxy_port}/api/mcp"
        ca = str(bundle / "atrium" / "ca.crt")
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        context = ssl.create_default_context(cafile=ca)
        context.load_cert_chain(str(bundle / "atrium" / "atrium-client.crt"), str(bundle / "atrium" / "atrium-client.key"))
        for _ in range(100):
            if proxy.poll() is not None:
                raise AssertionError(f"nginx exited: {proxy.stderr.read().decode()}")
            try:
                with httpx.Client(verify=context, trust_env=False, timeout=0.5) as client:
                    assert client.post(url, json=body).status_code == 401
                    assert client.post(url, json=body,
                                       headers={"authorization": "Bearer wrong"}).status_code == 401
                    accepted = client.post(url, json=body, headers={"authorization": f"Bearer {key}"})
                    assert accepted.status_code == 200, accepted.text
                    assert "tools" in accepted.json()["result"]
                break
            except httpx.TransportError:
                time.sleep(0.05)
        else:
            raise AssertionError("nginx did not start")
        with httpx.Client(verify=ssl.create_default_context(cafile=ca),
                          trust_env=False, timeout=2) as client:
            try:
                unauthenticated = client.post(url, json=body,
                                               headers={"authorization": f"Bearer {key}"})
            except httpx.TransportError:
                pass  # TLS handshake was rejected before HTTP.
            else:
                assert unauthenticated.status_code != 200
        wrong_role = ssl.create_default_context(cafile=ca)
        wrong_role.load_cert_chain(str(bundle / "engine" / "app-engine-client.crt"),
                                   str(bundle / "engine" / "app-engine-client.key"))
        with httpx.Client(verify=wrong_role, trust_env=False, timeout=2) as client:
            assert client.post(url, json=body,
                               headers={"authorization": f"Bearer {key}"}).status_code == 403
        rogue = bundle / "rogue"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-noenc",
                        "-days", "1", "-subj", "/CN=rogue", "-keyout", str(rogue) + ".key",
                        "-out", str(rogue) + ".crt"], check=True, capture_output=True)
        rogue_context = ssl.create_default_context(cafile=ca)
        rogue_context.load_cert_chain(str(rogue) + ".crt", str(rogue) + ".key")
        with httpx.Client(verify=rogue_context, trust_env=False, timeout=2) as client:
            try:
                rejected = client.post(url, json=body,
                                       headers={"authorization": f"Bearer {key}"})
            except httpx.TransportError:
                pass
            else:
                assert rejected.status_code != 200
        with httpx.Client(verify=ssl.create_default_context(cafile=str(rogue) + ".crt"),
                          trust_env=False, timeout=2) as client:
            with pytest.raises(httpx.TransportError):
                client.post(url, json=body)
    finally:
        for process in (proxy, engine):
            if process is not None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
