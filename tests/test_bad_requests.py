"""Requests the 2026-11-12 ZAP scan turned into 500s: each is a refusal, not a crash."""


import httpx
import pytest
from fastapi.testclient import TestClient

from test_public_origin import load_engine
import test_security_headers
from test_security_headers import _add_backend_app, _open_proxy

backend = test_security_headers.backend  # the app-backend fixture, shared with the header tests


def test_malformed_json_to_open_and_preview_is_a_400(tmp_path, monkeypatch, backend):
    module = load_engine(tmp_path, monkeypatch)
    _add_backend_app(module, backend)
    with TestClient(module.app, raise_server_exceptions=False) as client:
        _open_proxy(client, module)
        admin = {"x-app-engine-admin": module._admin_capability, "content-type": "application/json"}
        for action in ("open", "preview"):
            for body in (b"{", b"not json", b"[1]", b'"text"'):
                answer = client.post(f"/api/app-engine/apps/backend/{action}", content=body, headers=admin)
                assert answer.status_code == 400, (action, body, answer.status_code, answer.text)


def test_proxy_path_no_url_can_carry_is_a_400(tmp_path, monkeypatch, backend):
    module = load_engine(tmp_path, monkeypatch)
    _add_backend_app(module, backend)
    with TestClient(module.app, raise_server_exceptions=False) as client:
        proxy = _open_proxy(client, module)
        for path in ("/api/runs/%27%22%00%3Cscript%3E", "/%00", "/a%0d%0ab"):
            answer = client.get(f"{proxy}{path}", headers={"host": "backend.localhost"})
            assert answer.status_code in (400, 200, 404), (path, answer.status_code, answer.text)
        nul = client.get(f"{proxy}/%00", headers={"host": "backend.localhost"})
        assert nul.status_code == 400 and nul.json()["detail"]["code"] == "invalid_request_target"


def test_proxy_to_a_backend_that_is_gone_is_a_502(tmp_path, monkeypatch, backend):
    module = load_engine(tmp_path, monkeypatch)
    _add_backend_app(module, backend)
    with TestClient(module.app, raise_server_exceptions=False) as client:
        proxy = _open_proxy(client, module)
        backend.shutdown()
        backend.server_close()
        answer = client.get(f"{proxy}/", headers={"host": "backend.localhost"})
        assert answer.status_code == 502, answer.text
        assert answer.json()["detail"]["code"] == "upstream_unavailable"


@pytest.mark.asyncio
async def test_backend_dropping_mid_body_ends_the_response_without_raising():
    """The gateway's body stream stops quietly when the backend hangs up."""
    from test_gateway_service import (CatalogKey, ProxyRequest, TargetEndpoint, _body, _Catalog,
                                      _gateway_type, _Lifecycle, _manifest, _open_request)
    import tempfile
    from pathlib import Path

    class Dropping(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"first"
            raise httpx.ReadError("connection dropped")

        async def aclose(self):
            pass

    async def upstream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=Dropping())

    with tempfile.TemporaryDirectory() as tmp:
        gateway = _gateway_type()(
            catalog=_Catalog(_manifest(Path(tmp), managed=True)),
            lifecycle=_Lifecycle(TargetEndpoint("http", "localhost", 8912)),
            catalog_key=CatalogKey("test"),
            transport=httpx.MockTransport(upstream),
        )
        session = await gateway.open(_open_request())
        result = await gateway.proxy(session, ProxyRequest(method="GET", path="/", query=(), headers=(), body=_body()))
        assert [chunk async for chunk in result.body] == [b"first"]
