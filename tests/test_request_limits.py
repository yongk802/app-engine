"""ASGI streams prove denial before consumption and bounded chunked input."""
import asyncio
import time
from contextlib import suppress

import httpx
import pytest
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from test_public_origin import PUBLIC, load_engine

from app_engine.public_origin import SESSION_COOKIE
from app_engine.request_limits import RequestLimits


async def streamed_call(app, path, *, headers=None, chunks=None, method='POST', base=PUBLIC):
    consumed = []

    async def body():
        for chunk in chunks or [b'x' * 16384] * 100:
            consumed.append(len(chunk))
            yield chunk

    async def loopback_application(scope, receive, send):
        # Real reverse-proxy connections retain a public Host while the ASGI
        # listener is the engine's loopback socket.
        await app({**scope, 'server': ('127.0.0.1', 18500)}, receive, send)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(loopback_application), base_url=base) as client:
        response = await client.request(method, path, headers=headers, content=body())
    return response, consumed


@pytest.mark.asyncio
@pytest.mark.parametrize('path', ['/api/app-engine/apps/notes/open', '/api/players/notes/command',
                                  '/api/players/state/notes', '/api/mcp'])
async def test_unauthenticated_protected_requests_never_consume_the_body(tmp_path, monkeypatch, path):
    module = load_engine(tmp_path, monkeypatch, APP_ENGINE_PUBLIC_ORIGIN=PUBLIC,
                         APP_ENGINE_ADMIN_SECRET='test-owner-secret')
    method = 'PUT' if '/state/' in path else 'POST'
    response, consumed = await streamed_call(module.app, path, method=method,
        headers={'content-type': 'application/json', 'content-length': str(2**30)})
    assert response.status_code == 401, response.text
    assert consumed == []


@pytest.mark.asyncio
async def test_invalid_app_capability_is_rejected_before_reading(tmp_path, monkeypatch):
    module = load_engine(tmp_path, monkeypatch)
    response, consumed = await streamed_call(module.app, '/api/app-multiplayer/notes/command',
        base='http://notes.localhost', headers={'x-app-state-token': 'wrong', 'content-type': 'application/json'})
    assert response.status_code == 403
    assert consumed == []
    assert module._multiplayer._processes == {}


@pytest.mark.asyncio
async def test_valid_player_without_app_grant_is_rejected_before_reading(tmp_path, monkeypatch):
    module = load_engine(tmp_path, monkeypatch, APP_ENGINE_PUBLIC_ORIGIN=PUBLIC,
                         APP_ENGINE_ADMIN_SECRET='test-owner-secret')
    _, invite = module._accounts.create_player('alice', [])
    _, token = module._accounts.redeem_invite(invite, 'test-player-password')
    response, consumed = await streamed_call(module.app, '/api/players/notes/command',
        headers={'authorization': 'Bearer ' + token})
    assert response.status_code == 404
    assert consumed == []


@pytest.mark.asyncio
async def test_wrong_owner_capability_is_rejected_before_reading(tmp_path, monkeypatch):
    module = load_engine(tmp_path, monkeypatch, APP_ENGINE_PUBLIC_ORIGIN=PUBLIC,
                         APP_ENGINE_ADMIN_SECRET='test-owner-secret')
    token = module._accounts.create_admin_session()
    response, consumed = await streamed_call(module.app, '/api/backend-plans/notes/approve',
        headers={'cookie': f'{SESSION_COOKIE}={token}', 'x-app-engine-admin': 'wrong'})
    assert response.status_code == 403
    assert consumed == []


@pytest.mark.asyncio
@pytest.mark.parametrize('path,limit', [('/admin/sign-in', 8192), ('/sign-in', 8192),
                                       ('/api/players/sign-in', 8192),
                                       ('/api/app-engine/apps/notes/open', 8192)])
async def test_oversized_chunked_inputs_stop_before_parsing_or_backend_work(tmp_path, monkeypatch, path, limit):
    module = load_engine(tmp_path, monkeypatch, APP_ENGINE_PUBLIC_ORIGIN=PUBLIC,
                         APP_ENGINE_ADMIN_SECRET='test-owner-secret')
    calls = []
    monkeypatch.setattr(module._accounts, 'verify_admin_secret', lambda secret: calls.append(secret))
    token = module._accounts.create_admin_session()
    response, consumed = await streamed_call(module.app, path, headers={
        'cookie': f'{SESSION_COOKIE}={token}', 'content-type': 'application/json'},
        chunks=[b'x' * (limit // 2)] * 10)
    assert response.status_code == 413, response.text
    assert sum(consumed) == limit + limit // 2
    assert response.headers['connection'] == 'close'
    assert calls == []
    assert module._app_runtime.lifecycle.active_launch_ids() == ()


@pytest.mark.asyncio
async def test_oversized_content_length_is_refused_without_receiving(tmp_path, monkeypatch):
    module = load_engine(tmp_path, monkeypatch, APP_ENGINE_PUBLIC_ORIGIN=PUBLIC)
    response, consumed = await streamed_call(module.app, '/admin/sign-in',
        headers={'content-length': str(2**30)})
    assert response.status_code == 413
    assert consumed == []


@pytest.mark.asyncio
async def test_small_chunked_app_save_still_works(tmp_path, monkeypatch):
    module = load_engine(tmp_path, monkeypatch)
    response, consumed = await streamed_call(module.app, '/api/app-state/notes', method='PUT',
        base='http://notes.localhost', headers={'x-app-state-token': module._app_capability('notes')},
        chunks=[b'{"note":', b'"saved"}'])
    assert response.status_code == 200, response.text
    assert sum(consumed) == 16
    assert (module.STATE_DIR / 'saves/owner/notes.json').read_text() == '{"note":"saved"}'


@pytest.mark.asyncio
async def test_slow_body_deadline_is_total_and_precedes_backend_work():
    reached = []
    async def app(scope, receive, send):
        # Existing routes often catch Exception to return invalid-JSON errors.
        # The budget control signal must escape those handlers.
        with suppress(Exception):
            await Request(scope, receive).body()
        reached.append('backend')
        await JSONResponse({})(scope, receive, send)

    async def slow():
        await asyncio.Event().wait()
        return {'type': 'http.request', 'body': b'{}', 'more_body': False}

    messages = []
    async def send(message):
        messages.append(message)
    scope = {'type': 'http', 'path': '/sign-in', 'headers': []}
    started = time.monotonic()
    await RequestLimits(app, timeout_seconds=.02)(scope, slow, send)
    assert time.monotonic() - started < .5
    assert messages[0]['status'] == 408
    assert reached == []


@pytest.mark.parametrize('timeout', [0, -1, float('inf'), float('nan'), 301])
def test_unbounded_or_invalid_timeout_configuration_is_rejected(timeout):
    with pytest.raises(ValueError):
        RequestLimits(None, timeout_seconds=timeout)


@pytest.mark.asyncio
async def test_trickling_chunks_do_not_reset_the_total_body_deadline():
    reads = []
    async def app(scope, receive, send):
        await Request(scope, receive).body()
        pytest.fail('An endless trickle reached application work')
    async def trickle():
        await asyncio.sleep(.02)
        reads.append(1)
        return {'type': 'http.request', 'body': b'x', 'more_body': True}
    messages = []
    async def send(message):
        messages.append(message)
    await RequestLimits(app, timeout_seconds=.055)(
        {'type': 'http', 'path': '/sign-in', 'headers': []}, trickle, send)
    assert messages[0]['status'] == 408
    assert 1 <= len(reads) <= 2


@pytest.mark.asyncio
@pytest.mark.parametrize('body', [b'', b'{"query":"postgres"}'])
async def test_completed_request_body_does_not_deadline_the_response_stream(body):
    finished = asyncio.Event()
    reads = 0
    messages = []

    async def receive():
        nonlocal reads
        reads += 1
        if reads == 1:
            return {'type': 'http.request', 'body': body, 'more_body': False}
        await finished.wait()
        return {'type': 'http.disconnect'}

    async def send(message):
        messages.append(message)
        if message['type'] == 'http.response.body' and not message.get('more_body'):
            finished.set()

    async def app(scope, receive, send):
        assert await Request(scope, receive).body() == body

        async def events():
            # An actual Starlette disconnect listener remains open longer
            # than the input budget while the response produces its event.
            await asyncio.sleep(.06)
            yield b'data: search finished\n\n'

        await StreamingResponse(events(), media_type='text/event-stream')(scope, receive, send)

    await asyncio.wait_for(RequestLimits(app, timeout_seconds=.02)(
        {'type': 'http', 'path': '/api/search/events', 'method': 'GET',
         'headers': [], 'asgi': {'spec_version': '2.0'}}, receive, send), timeout=1)
    assert messages[0]['status'] == 200
    assert b''.join(m.get('body', b'') for m in messages) == b'data: search finished\n\n'
    assert not messages[-1].get('more_body')
