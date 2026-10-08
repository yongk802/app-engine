"""Lazy MCP-facing app operations. Reading the catalog never opens an app."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .app_tool_schema import check_schema, validate
from .contracts import AppId, ApprovalReceipt, LaunchRequest, OpenAppRequest, ProxyRequest

MAX_STATE = 100 * 1024
MAX_OUTPUT = 256 * 1024
TOOL_TIMEOUT = 60.0
_ID = re.compile(r'^[a-z0-9][a-z0-9-]*$')
_NAME = re.compile(r'^[a-zA-Z][a-zA-Z0-9_]{0,47}$')
_PATH = re.compile(r'^/(?:[a-zA-Z0-9_-]+/)*[a-zA-Z0-9_-]+$')


def object_schema(properties, required):
    return {'type': 'object', 'properties': properties, 'required': required, 'additionalProperties': False}


_APP = {'app_id': {'type': 'string', 'maxLength': 128}}
BUILTINS = [
    {'name': 'apps_list', 'description': 'List installed apps and their declared tool availability. Does not start apps.', 'inputSchema': object_schema({}, []), 'annotations': {'readOnlyHint': True}},
    {'name': 'apps_open', 'description': 'Queue an app to open in connected app-engine chat panels. Returns queued, not opened. The owner must approve a new process plan in the launcher.', 'inputSchema': object_schema(_APP, ['app_id'])},
    {'name': 'apps_read_state', 'description': 'Read an installed app\'s persisted shared JSON state and revision. Apps with their own backend may keep their data elsewhere.', 'inputSchema': object_schema(_APP, ['app_id']), 'annotations': {'readOnlyHint': True}},
    {'name': 'apps_write_state', 'description': 'Replace an app\'s shared JSON state, using the revision from apps_read_state. State must be an object or array, at most 100 KB. Reload the app to see changes; use app-specific tools when provided.', 'inputSchema': object_schema({**_APP, 'revision': {'type': 'string', 'maxLength': 64}, 'state': {}}, ['app_id', 'revision', 'state']), 'annotations': {'destructiveHint': True}},
]


class AppTools:
    def __init__(self, runtime, state_dir: Path, approved: Callable[[str], bool]):
        self.runtime = runtime
        self.state_dir = Path(state_dir)
        self.approved = approved
        self.events = deque(maxlen=64)
        self.cursor = 0
        self.panel_seen = None

    def installed(self):
        return self.runtime.catalog.snapshot(self.runtime.catalog_key).apps

    def _app(self, app_id):
        if not isinstance(app_id, str) or not _ID.fullmatch(app_id):
            raise ValueError('Invalid app ID')
        item = next((a for a in self.installed() if a.manifest.app_id == app_id), None)
        if item is None or not item.compatible:
            raise ValueError('App is not installed or is incompatible')
        return item.manifest

    def _declarations(self):
        declarations = {}
        for item in self.installed():
            if not item.compatible:
                continue
            manifest = item.manifest
            path = manifest.root / 'app-tools.json'
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                with path.open('rb') as stream:
                    raw = stream.read(MAX_OUTPUT + 1)
                if len(raw) > MAX_OUTPUT:
                    continue
                data = json.loads(raw)
                json.dumps(data, allow_nan=False, ensure_ascii=False).encode('utf-8')
                if not isinstance(data, dict) or data.get('schema_version') != 1 or not isinstance(data.get('tools'), list) or len(data['tools']) > 32:
                    continue
                target = next(t for t in manifest.targets if t.target_id == manifest.default_target)
                if target.runtime is None and target.entry_point is None:
                    continue
                app_tools = {}
                for tool in data['tools']:
                    if not isinstance(tool, dict) or not _NAME.fullmatch(tool.get('name', '')) or not _PATH.fullmatch(tool.get('path', '')):
                        raise ValueError('Invalid tool declaration')
                    if not isinstance(tool.get('description'), str) or not 1 <= len(tool['description']) <= 2048:
                        raise ValueError('Invalid tool description')
                    schema = tool.get('inputSchema')
                    check_schema(schema)
                    if schema['type'] != 'object':
                        raise ValueError('Tool input must be an object')
                    name = f'app__{manifest.app_id}__{tool["name"]}'
                    if name in app_tools or len(name) > 128:
                        raise ValueError('Duplicate or overlong tool name')
                    app_tools[name] = (manifest, tool)
                declarations.update(app_tools)
            except (OSError, ValueError, TypeError, StopIteration, RecursionError, OverflowError):
                continue
        return declarations

    async def list_tools(self):
        declarations = await asyncio.to_thread(self._declarations)
        return [*BUILTINS, *[{'name': name, 'description': tool['description'], 'inputSchema': tool['inputSchema']} for name, (_, tool) in declarations.items()]]

    def events_after(self, cursor):
        self.panel_seen = time.monotonic()
        return {'events': [{k: v for k, v in event.items() if k != 'time'} for event in self.events if event['id'] > cursor and time.monotonic() - event['time'] < 60], 'cursor': self.cursor}

    def _state_path(self, app_id):
        self._app(app_id)
        path = self.state_dir / f'{app_id}.json'
        if path.is_symlink():
            raise ValueError('App state is not a regular file')
        return path

    def _read_state(self, app_id):
        path = self._state_path(app_id)
        if not path.exists():
            return {'state': {}, 'revision': hashlib.sha256(b'absent').hexdigest()}
        with path.open('rb') as stream:
            raw = stream.read(MAX_STATE + 1)
        if len(raw) > MAX_STATE:
            raise ValueError('App state exceeds 100 KB')
        return {'state': json.loads(raw), 'revision': hashlib.sha256(raw).hexdigest()}

    async def call(self, name: str, arguments: dict):
        builtin = next((t for t in BUILTINS if t['name'] == name), None)
        if builtin:
            schema = builtin['inputSchema']
            if name == 'apps_write_state':
                # The state payload is deliberately arbitrary JSON, independently bounded.
                validate({k: v for k, v in arguments.items() if k != 'state'}, object_schema({k: v for k, v in schema['properties'].items() if k != 'state'}, ['app_id', 'revision']))
                if not isinstance(arguments.get('state'), (dict, list)):
                    raise ValueError('State must be an object or array')
            else:
                validate(arguments, schema)
            if name == 'apps_list':
                declarations = await asyncio.to_thread(self._declarations)
                return {'apps': [{'id': str(a.manifest.app_id), 'label': a.manifest.label, 'compatible': a.compatible, 'tools': [n for n, (m, _) in declarations.items() if m.app_id == a.manifest.app_id]} for a in self.installed()]}
            app_id = arguments['app_id']
            self._app(app_id)
            if name == 'apps_open':
                if self.panel_seen is None or time.monotonic() - self.panel_seen > 10:
                    raise ValueError('Open the app-engine chat panel before requesting an app to open')
                self.cursor += 1
                self.events.append({'id': self.cursor, 'type': 'open', 'app_id': app_id, 'time': time.monotonic()})
                return {'status': 'queued', 'event_id': self.cursor, 'message': 'Queued for connected chat panels. Delivery is best effort and expires after 60 seconds.'}
            current = self._read_state(app_id)
            if name == 'apps_read_state':
                return current
            if current['revision'] != arguments['revision']:
                raise ValueError('App state changed; read it again before writing')
            raw = json.dumps(arguments['state'], ensure_ascii=False, allow_nan=False).encode('utf-8')
            if len(raw) > MAX_STATE:
                raise ValueError('App state exceeds 100 KB')
            path = self._state_path(app_id)
            self.state_dir.mkdir(parents=True, exist_ok=True)
            # No await between revision check and atomic replacement: serial with host writes.
            import tempfile
            import os
            fd, temporary = tempfile.mkstemp(prefix='.assistant-state-', dir=self.state_dir)
            try:
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(raw)
                Path(temporary).replace(path)
            finally:
                Path(temporary).unlink(missing_ok=True)
            return {'revision': hashlib.sha256(raw).hexdigest(), 'saved': True}
        declared = (await asyncio.to_thread(self._declarations)).get(name)
        if declared is None:
            raise ValueError('Unknown or unavailable app tool')
        manifest, tool = declared
        validate(arguments, tool['inputSchema'])
        return await asyncio.wait_for(self._invoke(manifest, tool, arguments), timeout=TOOL_TIMEOUT)

    async def _invoke(self, manifest, tool, arguments):
        launch = LaunchRequest(AppId(manifest.app_id), None, self.runtime.subject)
        target = next(t for t in manifest.targets if t.target_id == manifest.default_target)
        approval = None
        if target.runtime is not None:
            plan = await self.runtime.lifecycle.preview(launch)
            if plan.approval_required:
                if not self.approved(str(plan.fingerprint)):
                    raise ValueError('Approval required: open this app in the launcher and approve its process plan first')
                approval = ApprovalReceipt(plan.fingerprint, launch.subject.subject_id, datetime.now(timezone.utc))
        session = await self.runtime.gateway.open(OpenAppRequest(launch, approval))
        try:
            encoded = json.dumps(arguments, allow_nan=False).encode()
            async def body():
                yield encoded
            response = await self.runtime.gateway.proxy(session, ProxyRequest('POST', tool['path'], (), (('content-type', 'application/json'), ('content-length', str(len(encoded))), ('accept-encoding', 'identity')), body()))
            data = bytearray()
            try:
                async for chunk in response.body:
                    data.extend(chunk)
                    if len(data) > MAX_OUTPUT:
                        raise ValueError('App tool response exceeds 256 KB')
            finally:
                if hasattr(response.body, 'aclose'):
                    await response.body.aclose()
            if not 200 <= response.status_code < 300:
                raise ValueError(f'App tool returned HTTP {response.status_code}')
            result = json.loads(data)
            return result if isinstance(result, dict) else {'result': result}
        finally:
            await self.runtime.gateway.close(session.session_id)
