"""Read-aloud for tutor replies: an optional Qwen3-TTS voice service.

The launcher's ▶ button speaks a finished reply. With ``APP_ENGINE_TTS_URL``
set (a Qwen3-TTS service such as ``http://192.168.1.173:8895``), each sentence
group is rendered there as WAV; without it the launcher uses the browser's own
``speechSynthesis`` voice, so nothing leaves the computer unless the owner
points it somewhere.

The service contract is ``POST /synthesize {text, voice, language}`` → WAV and
``GET /voices`` → ``{"voices": [...]}``. It caps a request at 500 characters,
which is why the launcher sends a reply one chunk at a time.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

import httpx

MAX_CHUNK_CHARS = 500


@dataclass(frozen=True)
class SpeechSettings:
    url: str
    voice: str
    language: str

    @classmethod
    def from_env(cls, env: dict | None = None) -> "SpeechSettings | None":
        env = os.environ if env is None else env
        url = (env.get("APP_ENGINE_TTS_URL") or "").strip().rstrip("/")
        if not url:
            return None
        return cls(url=url, voice=(env.get("APP_ENGINE_TTS_VOICE") or "").strip().lower(),
                   language=(env.get("APP_ENGINE_TTS_LANGUAGE") or "English").strip())


class SpeechUnavailable(Exception):
    """The voice service could not render this text."""


class SpeechService:
    def __init__(self, settings: SpeechSettings, client: httpx.AsyncClient | None = None):
        self.settings = settings
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=5.0))

    async def close(self) -> None:
        await self.client.aclose()

    async def voices(self) -> list[str]:
        try:
            response = await self.client.get(f"{self.settings.url}/voices", timeout=5.0)
            response.raise_for_status()
            return [str(v) for v in response.json().get("voices") or []]
        except (httpx.HTTPError, ValueError, AttributeError):
            return []

    async def synthesize(self, text: str, voice: str | None = None) -> bytes:
        text = " ".join((text or "").split())[:MAX_CHUNK_CHARS]
        if not text:
            raise ValueError("text required")
        payload = {"text": text, "language": self.settings.language,
                   "voice": (voice or self.settings.voice or None)}
        try:
            response = await self.client.post(f"{self.settings.url}/synthesize", json=payload)
            response.raise_for_status()
        except httpx.HTTPError as error:
            raise SpeechUnavailable(str(error) or type(error).__name__) from error
        if not response.content:
            raise SpeechUnavailable("the voice service returned no audio")
        return response.content
