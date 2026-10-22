import asyncio
import ssl

import httpx
import pytest


def test_atrium_status_requires_https_and_configured_client_identity():
    from app_engine.atrium_status import AtriumStatusProbe
    from app_engine.outbound_tls import OutboundTlsPolicy

    with pytest.raises(ValueError, match="HTTPS"):
        AtriumStatusProbe("http://atrium.example/health", OutboundTlsPolicy())
    with pytest.raises(ValueError, match="mTLS"):
        AtriumStatusProbe("https://atrium.example/health", OutboundTlsPolicy())


def test_atrium_status_checks_health_without_following_redirects():
    from app_engine.atrium_status import AtriumStatusProbe
    from app_engine.outbound_tls import OutboundTlsPolicy

    context = ssl.create_default_context()
    policy = OutboundTlsPolicy({"https://atrium.example:443": context})
    captured = []

    def upstream(request):
        captured.append(str(request.url))
        return httpx.Response(200, json={"ok": True, "version": "test-version"})

    probe = AtriumStatusProbe("https://atrium.example/health", policy,
                              transport=httpx.MockTransport(upstream))
    result = asyncio.run(probe.check())
    assert result["ok"] is True and result["version"] == "test-version"
    assert captured == ["https://atrium.example/health"]

    def redirect(request):
        captured.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://evil.example/"})

    probe = AtriumStatusProbe("https://atrium.example/health", policy,
                              transport=httpx.MockTransport(redirect))
    result = asyncio.run(probe.check())
    assert result["ok"] is False
    assert captured[-1] == "https://atrium.example/health"
