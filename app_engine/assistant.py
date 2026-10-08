"""Owner-configured external chat and authenticated, stateless HTTP MCP adapter."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import secrets
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse

from . import __version__
from .app_tools import AppTools
from .contracts import AppEngineError

log = logging.getLogger(__name__)
VERSIONS = ('2025-11-25', '2025-06-18', '2025-03-26')
MAX_REQUEST = 160 * 1024
NO_STORE = {'Cache-Control': 'no-store'}


def chat_url(value: object) -> str:
    if not isinstance(value, str) or len(value) > 4096 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError('Enter a valid chat page URL')
    value = value.strip()
    if not value:
        return ''
    parsed = urlsplit(value)
    if parsed.username or parsed.password or not parsed.hostname or '\\' in value:
        raise ValueError('Chat URL must have a hostname and no embedded credentials')
    if parsed.scheme != 'https' and not (parsed.scheme == 'http' and parsed.hostname in {'localhost', '127.0.0.1', '::1'}):
        raise ValueError('Use HTTPS, or HTTP for a chat on this computer')
    _ = parsed.port
    return value


class AssistantSettings:
    def __init__(self, state_dir: Path):
        self.path = Path(state_dir) / '.assistant' / 'settings.json'
        self.url = ''
        self.key_hash = ''
        self.last_connected_at = None
        try:
            if self.path.is_symlink():
                raise ValueError('Settings symlink refused')
            with self.path.open('rb') as stream:
                data = json.loads(stream.read(8193))
            self.url = chat_url(data.get('chat_url', ''))
            digest = data.get('key_hash', '')
            if isinstance(digest, str) and len(digest) == 64:
                self.key_hash = digest
        except (OSError, ValueError, TypeError, AttributeError):
            pass  # Corrupt/missing settings fail closed; no key is accepted.

    def save(self, *, url=None, key_hash=None):
        updated_url = self.url if url is None else url
        updated_hash = self.key_hash if key_hash is None else key_hash
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix='.assistant-', dir=self.path.parent)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump({'chat_url': updated_url, 'key_hash': updated_hash}, stream)
            Path(temporary).replace(self.path)
        finally:
            Path(temporary).unlink(missing_ok=True)
        self.url, self.key_hash = updated_url, updated_hash
        if key_hash is not None:
            self.last_connected_at = None

    def accepts(self, request: Request) -> bool:
        authorization = request.headers.get('authorization', '')
        if not authorization.startswith('Bearer ') or not self.key_hash:
            return False
        digest = hashlib.sha256(authorization[7:].encode()).hexdigest()
        return secrets.compare_digest(digest, self.key_hash)


async def payload(request: Request):
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > MAX_REQUEST:
            raise HTTPException(413, 'Request exceeds 160 KB')
    try:
        data = json.loads(raw)
        json.dumps(data, allow_nan=False, ensure_ascii=False).encode('utf-8')
        return data
    except (ValueError, RecursionError):
        raise HTTPException(400, 'Invalid JSON') from None


def create_assistant_router(runtime, state_dir: Path, *, require_owner_action, is_launcher_host, available: bool, approved):
    router = APIRouter()
    settings = AssistantSettings(state_dir)
    app_tools = AppTools(runtime, state_dir, approved)
    slots = asyncio.Semaphore(4)

    def owner(request):
        require_owner_action(request)
        if not available:
            raise HTTPException(403, 'External assistant is currently available only in local-owner mode')

    def authenticate(request):
        if not available:
            raise HTTPException(404)
        if not is_launcher_host(request):
            raise HTTPException(403, 'Launcher host required')
        origin = request.headers.get('origin')
        expected = f'{request.url.scheme}://{request.url.netloc}'
        if origin is not None and origin != expected:
            raise HTTPException(403, 'Origin denied')
        if not settings.accepts(request):
            raise HTTPException(401, 'MCP key required', headers={'WWW-Authenticate': 'Bearer'})

    def status(request):
        return {'available': available, 'chat_url': settings.url, 'mcp_url': str(request.base_url).rstrip('/') + '/api/mcp', 'enabled': bool(settings.key_hash) and available, 'last_connected_at': settings.last_connected_at, 'event_cursor': app_tools.cursor}

    @router.get('/assistant-assets/{filename}')
    async def asset(filename: str):
        if filename not in {'panel.js', 'panel.css'}:
            raise HTTPException(404)
        return FileResponse(Path(__file__).parent / 'assistant_ui' / filename, headers={'Cache-Control': 'no-cache'})

    @router.get('/api/assistant/settings')
    async def read_settings(request: Request):
        require_owner_action(request)
        return JSONResponse(status(request), headers=NO_STORE)

    @router.put('/api/assistant/settings')
    async def update_settings(request: Request):
        owner(request)
        data = await payload(request)
        try:
            if not isinstance(data, dict) or set(data) != {'chat_url'}:
                raise ValueError('Send a chat_url field')
            settings.save(url=chat_url(data['chat_url']))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        return JSONResponse(status(request), headers=NO_STORE)

    @router.post('/api/assistant/key')
    async def rotate_key(request: Request):
        owner(request)
        key = 'ae_' + secrets.token_urlsafe(32)
        settings.save(key_hash=hashlib.sha256(key.encode()).hexdigest())
        return JSONResponse({'key': key}, headers=NO_STORE)

    @router.delete('/api/assistant/key', status_code=204)
    async def revoke_key(request: Request):
        owner(request)
        settings.save(key_hash='')
        return Response(status_code=204, headers=NO_STORE)

    @router.get('/api/assistant/events')
    async def events(request: Request, after: int = 0):
        owner(request)
        return JSONResponse(app_tools.events_after(after), headers=NO_STORE)

    @router.api_route('/api/mcp', methods=['GET', 'DELETE'])
    async def unsupported(request: Request):
        authenticate(request)
        return Response(status_code=405, headers={'Allow': 'POST', **NO_STORE})

    @router.post('/api/mcp')
    async def mcp(request: Request):
        authenticate(request)
        protocol = request.headers.get('mcp-protocol-version', '2025-03-26')
        if protocol not in VERSIONS:
            raise HTTPException(400, 'Unsupported MCP protocol version')
        data = await payload(request)
        if not isinstance(data, dict) or data.get('jsonrpc') != '2.0' or not isinstance(data.get('method'), str) or not isinstance(data.get('params', {}), dict):
            return JSONResponse({'jsonrpc': '2.0', 'id': None, 'error': {'code': -32600, 'message': 'Invalid JSON-RPC request'}}, status_code=400, headers=NO_STORE)
        method, params = data['method'], data.get('params', {})
        request_id = data.get('id')
        if 'id' not in data:
            if not method.startswith('notifications/'):
                raise HTTPException(400, 'Requests require an id')
            return Response(status_code=202, headers=NO_STORE)
        if type(request_id) not in (str, int):
            raise HTTPException(400, 'Invalid request id')
        if slots.locked():
            raise HTTPException(429, 'Too many concurrent MCP requests')
        async with slots:
            settings.last_connected_at = datetime.now(timezone.utc).isoformat()
            reply = {'jsonrpc': '2.0', 'id': request_id}
            if method == 'initialize':
                requested = params.get('protocolVersion')
                reply['result'] = {'protocolVersion': requested if requested in VERSIONS else VERSIONS[0], 'capabilities': {'tools': {}}, 'serverInfo': {'name': 'app-engine', 'version': __version__}, 'instructions': 'Use apps_list to discover installed apps. App-specific actions are declared by each app. apps_open queues a request to connected launcher panels; it does not confirm completion. Process launch approvals remain in the launcher.'}
            elif method == 'ping':
                reply['result'] = {}
            elif method == 'tools/list':
                tools = await app_tools.list_tools()
                cursor = params.get('cursor', '0')
                if not isinstance(cursor, str) or not cursor.isascii() or not cursor.isdigit() or len(cursor) > 8:
                    reply['error'] = {'code': -32602, 'message': 'Invalid cursor'}
                else:
                    start = int(cursor)
                    reply['result'] = {'tools': tools[start:start + 100]}
                    if start + 100 < len(tools):
                        reply['result']['nextCursor'] = str(start + 100)
            elif method == 'tools/call':
                name, arguments = params.get('name'), params.get('arguments', {})
                if not isinstance(name, str) or not isinstance(arguments, dict):
                    reply['error'] = {'code': -32602, 'message': 'Tool name and object arguments required'}
                else:
                    try:
                        result = await app_tools.call(name, arguments)
                        content = json.dumps(result, ensure_ascii=False, allow_nan=False)
                        if len(content.encode()) > 512 * 1024:
                            raise ValueError('Result is too large; narrow the request')
                        reply['result'] = {'content': [{'type': 'text', 'text': content}], 'structuredContent': result, 'isError': False}
                    except (ValueError, AppEngineError) as exc:
                        reply['result'] = {'isError': True, 'content': [{'type': 'text', 'text': str(exc) if isinstance(exc, ValueError) else 'The app runtime could not complete this tool. Open the app in the launcher to inspect its status.'}]}
                    except asyncio.TimeoutError:
                        reply['result'] = {'isError': True, 'content': [{'type': 'text', 'text': 'App tool timed out. Check the app before retrying a write; it may have completed.'}]}
                    except Exception:
                        log.exception('App MCP tool failed')
                        reply['result'] = {'isError': True, 'content': [{'type': 'text', 'text': 'App tool failed; inspect engine logs. A write may have completed; check before retrying.'}]}
            else:
                reply['error'] = {'code': -32601, 'message': 'Method not found'}
            return JSONResponse(reply, headers=NO_STORE)
    return router
