"""The Mac mTLS gateway exposes only Package Tracker's read-only mail API."""

import importlib.util
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/install-package-tracker-mail-gateway.py"
spec = importlib.util.spec_from_file_location("package_tracker_mail_gateway", SCRIPT)
gateway = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gateway)


EXISTING = """http {
server {
    listen 127.0.0.1:9443 ssl;
    ssl_verify_client on;
    location = /health {
        if ($ssl_client_s_dn != "CN=app-engine-client") { return 403; }
        limit_except GET { deny all; }
        proxy_pass https://127.0.0.1:9900;
        proxy_ssl_verify off;
    }
    location / { return 404; }
}
}
"""


def test_renderer_adds_only_three_get_mail_routes_with_separate_client_identity():
    rendered = gateway.render(EXISTING, "/secure/atrium-local-ca.pem")
    assert rendered.count('"CN=package-tracker-client"') == 3
    assert "location = /api/email/accounts {" in rendered
    assert "location ~ ^/api/email/accounts/[^/]+/inbox$ {" in rendered
    assert "location ~ ^/api/email/accounts/[^/]+/message/[^/]+$ {" in rendered
    assert rendered.count("limit_except GET { deny all; }") == 4
    assert "proxy_ssl_verify off;" not in rendered
    assert rendered.count("proxy_ssl_trusted_certificate /secure/atrium-local-ca.pem;") == 4
    assert '"CN=app-engine-client"' in rendered
    assert rendered.count("location / { return 404; }") == 1
    assert gateway.render(rendered, "/secure/atrium-local-ca.pem") == rendered


def test_renderer_rejects_an_unexpected_gateway_shape():
    with pytest.raises(ValueError, match="gateway"):
        gateway.render(EXISTING.replace("ssl_verify_client on", "ssl_verify_client off"),
                       "/secure/atrium-local-ca.pem")


def test_nginx_reload_uses_signal_and_reload_argument(monkeypatch):
    calls = []
    monkeypatch.setattr(gateway.subprocess, "run", lambda argv, **_kwargs: calls.append(argv))
    gateway._nginx("nginx", Path("/gateway"), Path("/gateway/nginx.conf"), "reload")
    assert calls == [["nginx", "-p", "/gateway", "-c", "/gateway/nginx.conf", "-s", "reload"]]
