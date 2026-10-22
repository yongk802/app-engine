"""Configured service identities are offered only to their exact HTTPS origin."""

import json
import asyncio

import httpx

import pytest


def test_outbound_tls_policy_requires_complete_https_profile(tmp_path):
    from app_engine.outbound_tls import OutboundTlsPolicy

    config = tmp_path / "mtls.json"
    config.write_text(json.dumps({"http://store.example:8780": {
        "ca_file": "/ca.pem", "cert_file": "/client.pem", "key_file": "/client.key"}}))
    with pytest.raises(ValueError, match="HTTPS"):
        OutboundTlsPolicy.from_file(config)
    config.write_text(json.dumps({"https://store.example:0": {
        "ca_file": "/ca.pem", "cert_file": "/client.pem", "key_file": "/client.key"}}))
    with pytest.raises(ValueError, match="HTTPS"):
        OutboundTlsPolicy.from_file(config)
    config.write_text(json.dumps({"https://store.example": {"ca_file": "/ca.pem"}}))
    with pytest.raises(ValueError, match="ca_file.*cert_file.*key_file"):
        OutboundTlsPolicy.from_file(config)


def test_outbound_tls_policy_matches_exact_origin(tmp_path, monkeypatch):
    from app_engine.outbound_tls import OutboundTlsPolicy

    for name in ("ca.pem", "client.pem", "client.key"):
        (tmp_path / name).write_text("certificate fixture")
    class Context:
        def load_cert_chain(self, certfile, keyfile):
            assert certfile == str(tmp_path / "client.pem")
            assert keyfile == str(tmp_path / "client.key")

    context = Context()
    monkeypatch.setattr("app_engine.outbound_tls.ssl.create_default_context",
                        lambda *, cafile: context)
    config = tmp_path / "mtls.json"
    config.write_text(json.dumps({"https://store.example:8780": {
        "ca_file": str(tmp_path / "ca.pem"), "cert_file": str(tmp_path / "client.pem"),
        "key_file": str(tmp_path / "client.key")}}))
    policy = OutboundTlsPolicy.from_file(config)
    assert policy.for_origin("https://store.example:8780/api/v1/catalog") is context
    assert policy.for_origin("https://STORE.example:8780/other") is context
    assert policy.for_origin("https://store.example/api/v1/catalog") is None
    assert policy.for_origin("https://evil.example/") is None
    assert policy.for_origin("http://store.example:8780/") is None


def test_store_bridge_uses_certificate_only_for_configured_origin(tmp_path, monkeypatch):
    from app_engine.outbound_tls import OutboundTlsPolicy
    from app_engine.store_bridge import StoreBridge

    certificate_context = object()
    policy = OutboundTlsPolicy({"https://store.example:443": certificate_context})
    constructor_calls = []

    class FakeClient:
        def __init__(self, **kwargs):
            constructor_calls.append(kwargs)

        def stream(self, method, url, **kwargs):
            class Response:
                headers = {}

                async def __aenter__(self):
                    return self

                async def __aexit__(self, *args):
                    return None

                def raise_for_status(self):
                    return None

                async def aiter_bytes(self, **kwargs):
                    yield b"ok"

            return Response()

        async def aclose(self):
            return None

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    async def run():
        bridge = StoreBridge(tmp_path / "state", tmp_path / "apps", tls_policy=policy)
        assert await bridge._download("https://store.example/api/v1/catalog", 100) == b"ok"
        assert await bridge._download("https://other.example/api/v1/catalog", 100) == b"ok"
        await bridge.close()

    asyncio.run(run())
    assert constructor_calls[0]["verify"] is certificate_context
    assert constructor_calls[1]["verify"] is True
    assert all(call["trust_env"] is False for call in constructor_calls)


def test_public_store_fetch_rejects_plaintext_and_private_unconfigured_hosts(tmp_path):
    from app_engine.store_bridge import StoreBridge
    from app_engine.store_contracts import StoreError

    async def run():
        bridge = StoreBridge(tmp_path / "state", tmp_path / "apps", public_mode=True)
        for url in ("http://store.example/api/v1/catalog",
                    "https://store.example/api/v1/catalog",
                    "https://127.0.0.1/api/v1/catalog",
                    "https://169.254.169.254/latest/meta-data"):
            with pytest.raises(StoreError, match="configured HTTPS mTLS"):
                await bridge._download(url, 100)
        await bridge.close()

    asyncio.run(run())
