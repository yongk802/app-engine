import json
from pathlib import Path

import httpx
import pytest

from app_engine.config import ConfigStore
from app_engine.ollama import ConfirmationError, OllamaManager, UnmanagedModelError
from app_engine.registry import ModelRegistry


def manager(tmp_path, handler, os_name="macos", clock=lambda: 1000.0, launcher=lambda argv: None):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://127.0.0.1:11434")
    return OllamaManager(ModelRegistry.load(Path("model-registry.json")), ConfigStore(tmp_path),
                         client=client, os_name=os_name, clock=clock, launcher=launcher)


@pytest.mark.asyncio
async def test_status_matches_installed_registry_models(tmp_path):
    def handler(request):
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "0.9.0"})
        return httpx.Response(200, json={"models": [{"name": "qwen3:8b", "size": 12}]})
    mgr = manager(tmp_path, handler)
    status = await mgr.status()
    assert status.running is True
    assert status.version == "0.9.0"
    assert status.installed_model_ids == ("qwen3-8b",)
    await mgr.close()


def test_installation_plan_is_fixed_and_requires_confirmation(tmp_path):
    mgr = manager(tmp_path, lambda r: httpx.Response(500))
    plan = mgr.installation_plan()
    assert plan.official_source == "https://ollama.com/download"
    assert plan.argv == ("open", "https://ollama.com/download")
    with pytest.raises(ConfirmationError):
        mgr.install(plan.plan_id, "wrong")


def test_confirmation_is_single_use(tmp_path):
    calls = []
    mgr = manager(tmp_path, lambda r: httpx.Response(500), launcher=lambda argv: calls.append(argv))
    plan = mgr.installation_plan()
    token = mgr.authorize(plan.plan_id)
    mgr.install(plan.plan_id, token)
    assert calls == [plan.argv]
    with pytest.raises(ConfirmationError):
        mgr.install(plan.plan_id, token)


@pytest.mark.asyncio
async def test_pull_streams_progress_and_marks_model_managed(tmp_path):
    body = b'{"status":"pulling","total":100,"completed":25}\n{"status":"success","total":100,"completed":100}\n'
    mgr = manager(tmp_path, lambda r: httpx.Response(200, content=body))
    events = [event async for event in mgr.pull("qwen3-1.7b")]
    assert events[-1].percent == 100
    assert ConfigStore(tmp_path).load().managed_model_ids == ("qwen3-1.7b",)
    await mgr.close()


@pytest.mark.asyncio
async def test_refuses_to_remove_unmanaged_model(tmp_path):
    mgr = manager(tmp_path, lambda r: httpx.Response(200))
    with pytest.raises(UnmanagedModelError):
        await mgr.remove_managed_model("qwen3-8b")
    await mgr.close()


@pytest.mark.asyncio
async def test_verify_requires_meaningful_response(tmp_path):
    def handler(request):
        payload = json.loads(request.content)
        assert payload["model"] == "qwen3:8b"
        return httpx.Response(200, json={"message": {"content": "Local tutoring is ready."}})
    mgr = manager(tmp_path, handler)
    result = await mgr.verify("qwen3-8b")
    assert result.ok is True
    assert "ready" in result.sample.lower()
    await mgr.close()
