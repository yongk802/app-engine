"""Small, certificate-authenticated outbound health probe for Atrium."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from urllib.parse import urlsplit

import httpx

from .outbound_tls import OutboundTlsPolicy


class AtriumStatusProbe:
    def __init__(self, url: str, tls_policy: OutboundTlsPolicy, *,
                 transport: httpx.AsyncBaseTransport | None = None,
                 interval_seconds: float = 60.0):
        try:
            parsed = urlsplit(url)
            valid = (parsed.scheme == "https" and bool(parsed.hostname) and
                     parsed.path == "/health" and not parsed.query and
                     not parsed.fragment and not parsed.username and
                     not parsed.password and parsed.port != 0)
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("Atrium status URL must be an HTTPS /health endpoint")
        context = tls_policy.for_origin(url)
        if context is None:
            raise ValueError("Atrium status requires a configured mTLS identity for its exact origin")
        self.url = url
        self.context = context
        self.transport = transport
        self.interval_seconds = interval_seconds
        self.last: dict[str, object] = {"configured": True, "ok": False, "checked_at": None}
        self._task: asyncio.Task[None] | None = None

    async def check(self) -> dict[str, object]:
        result: dict[str, object] = {"configured": True, "ok": False,
                                     "checked_at": datetime.now(timezone.utc).isoformat()}
        try:
            async with httpx.AsyncClient(verify=self.context, transport=self.transport,
                                         trust_env=False, follow_redirects=False,
                                         timeout=5.0) as client:
                response = await client.get(self.url)
                response.raise_for_status()
                data = response.json()
            if not isinstance(data, dict) or data.get("ok") is not True:
                raise ValueError("Atrium health response is invalid")
            result["ok"] = True
            result["version"] = str(data.get("version", ""))[:100]
        except (httpx.HTTPError, ValueError):
            result["error"] = "atrium_unavailable"
        self.last = result
        return result

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        while True:
            await self.check()
            await asyncio.sleep(self.interval_seconds)

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
