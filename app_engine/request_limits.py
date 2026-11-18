"""Bound ASGI request consumption without reading ahead of authentication."""
from __future__ import annotations

import asyncio
import math
import re

from starlette.datastructures import Headers
from starlette.responses import JSONResponse


class _BudgetExceeded(BaseException):
    """Control signal that broad JSON/parser error handlers must not swallow."""

    def __init__(self, status: int, message: str):
        self.status, self.message = status, message


def body_limit(path: str, proxy_bytes: int) -> int:
    if '/proxy/' in path or path.startswith('/apps/'):
        return proxy_bytes
    if path in {'/admin/sign-in', '/sign-in', '/api/players/sign-in'} or path.startswith('/join/'):
        return 8 * 1024
    if path.startswith(('/api/app-state/', '/api/players/state/')):
        return 100 * 1024
    if path.startswith('/api/app-multiplayer/') or re.fullmatch(r'/api/players/[^/]+/command', path):
        return 64 * 1024
    if path.startswith(('/api/app-engine/store', '/api/backend-plans/', '/api/multiplayer-plans/')):
        return 16 * 1024
    if re.fullmatch(r'/api/app-engine/apps/[^/]+/(open|preview)', path):
        return 8 * 1024
    return 256 * 1024


class RequestLimits:
    """Wrap receive, rather than prebuffering bodies before route authentication."""

    def __init__(self, app, *, timeout_seconds: float = 10,
                 proxy_bytes: int = 8 * 1024 * 1024):
        if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 300:
            raise ValueError('body timeout must be greater than 0 and at most 300 seconds')
        if not 1 <= proxy_bytes <= 128 * 1024 * 1024:
            raise ValueError('proxy body budget must be between 1 byte and 128 MiB')
        self.app, self.timeout, self.proxy_bytes = app, timeout_seconds, proxy_bytes

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            await self.app(scope, receive, send)
            return
        maximum = body_limit(scope['path'], self.proxy_bytes)
        deadline = asyncio.get_running_loop().time() + self.timeout
        consumed = 0
        complete = False
        started = False

        async def limited_receive():
            nonlocal consumed, complete
            # Streaming responses also use receive to wait for disconnects.
            # Once the final body chunk has arrived, the body budget is done;
            # applying its deadline to that wait would truncate valid streams.
            if complete:
                return await receive()
            # Delayed until the first read: unauthorised requests are rejected
            # by the normal gates, even with an oversized Content-Length.
            _check_length(Headers(scope=scope), maximum)
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise _BudgetExceeded(408, 'request body deadline exceeded')
            try:
                message = await asyncio.wait_for(receive(), timeout=remaining)
            except asyncio.TimeoutError:
                raise _BudgetExceeded(408, 'request body deadline exceeded') from None
            if message['type'] == 'http.request':
                consumed += len(message.get('body', b''))
                if consumed > maximum:
                    raise _BudgetExceeded(413, 'request body exceeds its byte budget')
                complete = not message.get('more_body', False)
            elif message['type'] == 'http.disconnect':
                complete = True
            return message

        async def response_send(message):
            nonlocal started
            if message['type'] == 'http.response.start':
                started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, response_send)
        except _BudgetExceeded as exc:
            if started:
                raise  # A streaming response must close, never emit a second status line.
            await JSONResponse({'detail': {'code': 'invalid_request', 'message': exc.message}}, status_code=exc.status,
                               headers={'Connection': 'close'})(scope, receive, send)


def _check_length(headers: Headers, maximum: int) -> None:
    values = headers.getlist('content-length')
    if not values:
        return
    if len(values) != 1 or not values[0].isascii() or not values[0].isdecimal() or len(values[0]) > 20:
        raise _BudgetExceeded(400, 'invalid Content-Length')
    if int(values[0]) > maximum:
        raise _BudgetExceeded(413, 'request body exceeds its byte budget')
