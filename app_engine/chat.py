from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable

import httpx

from .config import ConfigStore
from .registry import ModelRegistry


SHARED_TUTOR_POLICY = (
    "Explain clearly with concrete examples. Give hints before full solutions when appropriate. "
    "Be honest about uncertainty and do not claim internet, file, or tool access you do not have. "
    "Keep the first answer concise and allow follow-up depth."
)


class ChatRuntime:
    def __init__(self, registry: ModelRegistry, config: ConfigStore,
                 client: httpx.AsyncClient, readiness: Callable[[], bool]):
        self.registry = registry
        self.config = config
        self.client = client
        self.readiness = readiness

    async def close(self) -> None:
        await self.client.aclose()

    async def stream(self, system_prompt: str, messages: list[dict]) -> AsyncIterator[dict]:
        if not self.readiness():
            yield {"type": "setup_required"}
            return
        config = self.config.load()
        profile = self.registry.get_profile(config.selected_profile)
        payload = {
            "model": profile.ollama_tag,
            "messages": [{"role": "system", "content": f"{system_prompt}\n\n{SHARED_TUTOR_POLICY}"}, *messages],
            "stream": True,
            "options": {"num_ctx": profile.context_limit},
        }
        saw_valid = False
        saw_terminal = False
        try:
            async with self.client.stream("POST", "/api/chat", json=payload, timeout=httpx.Timeout(180.0)) as response:
                if response.status_code == 404:
                    yield {"type": "error", "code": "model_missing", "message": "The selected local model is not installed.", "retryable": True}
                    return
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    saw_valid = True
                    text = str(event.get("message", {}).get("content", ""))
                    if text:
                        yield {"type": "token", "text": text}
                    if event.get("done"):
                        saw_terminal = True
                        yield {"type": "complete"}
                        return
        except httpx.TimeoutException:
            yield {"type": "error", "code": "timeout", "message": "The local model took too long to respond.", "retryable": True}
            return
        except httpx.ConnectError:
            yield {"type": "error", "code": "ollama_unavailable", "message": "Local AI is not running.", "retryable": True}
            return
        except httpx.HTTPError as exc:
            yield {"type": "error", "code": "ollama_error", "message": f"Local AI request failed: {exc}", "retryable": True}
            return
        if not saw_valid or not saw_terminal:
            yield {"type": "error", "code": "malformed_stream", "message": "Local AI returned an incomplete response.", "retryable": True}
