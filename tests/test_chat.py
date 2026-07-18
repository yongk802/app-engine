import json
from pathlib import Path

import httpx
import pytest

from app_engine.chat import ChatRuntime, SHARED_TUTOR_POLICY
from app_engine.models import LocalAIConfig
from app_engine.registry import ModelRegistry


class Store:
    def __init__(self, profile="quality"):
        self.value = LocalAIConfig(selected_profile=profile)
    def load(self):
        return self.value


def runtime(handler, ready=True):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://127.0.0.1:11434")
    return ChatRuntime(ModelRegistry.load(Path("model-registry.json")), Store(), client,
                       readiness=lambda: ready)


@pytest.mark.asyncio
async def test_returns_setup_required_without_calling_ollama():
    called = False
    def handler(request):
        nonlocal called
        called = True
        return httpx.Response(500)
    chat = runtime(handler, ready=False)
    events = [event async for event in chat.stream("Be helpful", [{"role": "user", "content": "Hi"}])]
    assert events == [{"type": "setup_required"}]
    assert called is False
    await chat.close()


@pytest.mark.asyncio
async def test_streams_native_ollama_chunks_and_terminal_complete():
    def handler(request):
        payload = json.loads(request.content)
        assert SHARED_TUTOR_POLICY in payload["messages"][0]["content"]
        assert "Math tutor" in payload["messages"][0]["content"]
        body = b'{"message":{"content":"Hello"},"done":false}\n{"message":{"content":"!"},"done":false}\n{"done":true}\n'
        return httpx.Response(200, content=body)
    chat = runtime(handler)
    events = [event async for event in chat.stream("Math tutor", [{"role": "user", "content": "Hi"}])]
    assert events == [{"type": "token", "text": "Hello"}, {"type": "token", "text": "!"}, {"type": "complete"}]
    await chat.close()


@pytest.mark.asyncio
async def test_malformed_stream_ends_with_actionable_error():
    chat = runtime(lambda r: httpx.Response(200, content=b"not-json\n"))
    events = [event async for event in chat.stream("Tutor", [{"role": "user", "content": "Hi"}])]
    assert events[-1]["type"] == "error"
    assert events[-1]["code"] == "malformed_stream"
    await chat.close()


@pytest.mark.asyncio
async def test_timeout_ends_with_retryable_error():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)
    chat = runtime(handler)
    events = [event async for event in chat.stream("Tutor", [{"role": "user", "content": "Hi"}])]
    assert events[-1] == {"type": "error", "code": "timeout", "message": "The local model took too long to respond.", "retryable": True}
    await chat.close()
