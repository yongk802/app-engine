from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable

import httpx

from .config import ConfigStore
from .grounding import (
    ANSWER_SCHEMA, KnowledgeBase, build_grounding_context, detect_misconceptions,
    extract_visible_answer,
)
from .registry import ModelRegistry


SHARED_TUTOR_POLICY = (
    "Explain clearly with concrete examples. Give hints before full solutions when appropriate. "
    "Be honest about uncertainty and do not claim internet, file, or tool access you do not have. "
    "Keep the first answer concise and allow follow-up depth. Answer directly without exposing private reasoning or planning."
)
class ChatRuntime:
    def __init__(self, registry: ModelRegistry, config: ConfigStore,
                 client: httpx.AsyncClient, readiness: Callable[[], bool],
                 knowledge: KnowledgeBase):
        self.registry = registry
        self.config = config
        self.client = client
        self.readiness = readiness
        self.knowledge = knowledge

    async def close(self) -> None:
        await self.client.aclose()

    async def _generate(self, payload: dict) -> tuple[list[str], dict | None]:
        chunks: list[str] = []
        saw_valid = False
        saw_terminal = False
        try:
            async with self.client.stream("POST", "/api/chat", json=payload, timeout=httpx.Timeout(180.0)) as response:
                if response.status_code == 404:
                    return [], {"type": "error", "code": "model_missing", "message": "The selected local model is not installed.", "retryable": True}
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
                        chunks.append(text)
                    if event.get("done"):
                        saw_terminal = True
                        break
        except httpx.TimeoutException:
            return [], {"type": "error", "code": "timeout", "message": "The local model took too long to respond.", "retryable": True}
        except httpx.ConnectError:
            return [], {"type": "error", "code": "ollama_unavailable", "message": "Local AI is not running.", "retryable": True}
        except httpx.HTTPError as exc:
            return [], {"type": "error", "code": "ollama_error", "message": f"Local AI request failed: {exc}", "retryable": True}
        if not saw_valid or not saw_terminal:
            return [], {"type": "error", "code": "malformed_stream", "message": "Local AI returned an incomplete response.", "retryable": True}
        return chunks, None

    async def stream(self, app_id: str, system_prompt: str, messages: list[dict]) -> AsyncIterator[dict]:
        if not self.readiness():
            yield {"type": "setup_required"}
            return
        config = self.config.load()
        profile = self.registry.get_profile(config.selected_profile)
        question = str(messages[-1].get("content", "")) if messages else ""
        references = self.knowledge.retrieve(app_id, question)
        grounding = build_grounding_context(references)
        assembled_prompt = f"{system_prompt}\n\n{SHARED_TUTOR_POLICY}"
        if grounding:
            assembled_prompt += f"\n\n{grounding}"
        payload = {
            "model": profile.ollama_tag,
            "messages": [{"role": "system", "content": assembled_prompt}, *messages],
            "stream": True,
            "think": False,
            "format": ANSWER_SCHEMA,
            "options": {"num_ctx": profile.context_limit, "num_predict": 384},
        }
        chunks, error = await self._generate(payload)
        if error:
            yield error
            return
        chunks = extract_visible_answer(chunks)
        original_chunks = chunks
        misconceptions = detect_misconceptions("".join(chunks), references)
        if misconceptions:
            corrections = "\n".join(f"- {item.correction}" for item in misconceptions)
            retry_payload = dict(payload)
            retry_payload["messages"] = [*payload["messages"],
                {"role": "assistant", "content": "".join(chunks)},
                {"role": "user", "content": f"Correct the answer using these reviewed facts. Return only the corrected answer:\n{corrections}"},
            ]
            retry_chunks, retry_error = await self._generate(retry_payload)
            if not retry_error and retry_chunks:
                chunks = extract_visible_answer(retry_chunks)
            else:
                chunks = original_chunks
        for chunk in chunks:
            yield {"type": "token", "text": chunk}
        yield {"type": "complete"}
