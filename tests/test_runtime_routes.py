from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from app_engine.contracts import HostSubject


def _write_static_app(root: Path) -> None:
    app = root / "demo"
    app.mkdir(parents=True)
    (app / "app.json").write_text(
        json.dumps({"id": "demo", "label": "Demo", "icon": "D"}),
        encoding="utf-8",
    )
    (app / "index.html").write_text("<h1>demo</h1>", encoding="utf-8")


@pytest.mark.asyncio
async def test_streaming_proxy_response_preserves_repeated_headers():
    from app_engine.contracts import ProxyResponse
    from app_engine.routes import _streaming_proxy_response

    async def body():
        yield b"partial"

    response = _streaming_proxy_response(
        ProxyResponse(207, (("x-demo", "one"), ("x-demo", "two")), body())
    )
    assert response.status_code == 207
    assert response.raw_headers == [(b"x-demo", b"one"), (b"x-demo", b"two")]


@pytest.mark.asyncio
async def test_runtime_composes_catalog_gateway_studio_and_diagnostics(tmp_path):
    from app_engine.runtime import DefaultAppEngineRuntime, LocalHostAdapter

    apps = tmp_path / "apps"
    apps.mkdir()
    _write_static_app(apps)
    subject = HostSubject("local", "admin")
    host = LocalHostAdapter(
        apps_roots=(apps,), state_root=tmp_path / "state", subject=subject
    )
    runtime = DefaultAppEngineRuntime(
        host=host, subject=subject, runtime_root=tmp_path / "runtime"
    )

    await runtime.start()
    try:
        assert runtime.catalog.snapshot(runtime.catalog_key).apps[0].manifest.app_id == "demo"
        diagnostics = runtime.diagnostics()
        assert diagnostics["package_version"]
        assert diagnostics["catalog_generation"] == 1
        assert diagnostics["active_launch_count"] == 0
        assert {item.template_id for item in await runtime.studio.templates()} >= {
            "static-web", "python-uv", "node", "go", "rust"
        }
    finally:
        report = await runtime.close(1.0)
    assert report.incomplete_launch_ids == ()


@pytest.mark.asyncio
async def test_local_host_state_and_configuration_are_contained_and_persistent(tmp_path):
    from app_engine.contracts import AppId, ConfigurationValue
    from app_engine.runtime import LocalHostAdapter

    apps = tmp_path / "apps"
    apps.mkdir()
    subject = HostSubject("person", "admin")
    host = LocalHostAdapter(
        apps_roots=(apps,), state_root=tmp_path / "state", subject=subject
    )
    store = await host.resolve_app_state(subject, AppId("demo"))
    await store.save(b'{"ok":true}')
    assert await store.load() == b'{"ok":true}'

    result = await host.save_configuration(
        subject, AppId("demo"), (ConfigurationValue("token", "secret", True),)
    )
    assert result.saved_keys == ("token",)
    resolved = await host.configuration(subject, AppId("demo"))
    assert resolved.values == (ConfigurationValue("token", "secret", True),)


@pytest.mark.asyncio
async def test_reusable_router_exposes_catalog_studio_assets_and_static_session(tmp_path):
    from app_engine.routes import create_app_engine_router
    from app_engine.runtime import DefaultAppEngineRuntime, LocalHostAdapter

    apps = tmp_path / "apps"
    apps.mkdir()
    _write_static_app(apps)
    subject = HostSubject("local", "admin")
    runtime = DefaultAppEngineRuntime(
        host=LocalHostAdapter(
            apps_roots=(apps,), state_root=tmp_path / "state", subject=subject
        ),
        subject=subject,
        runtime_root=tmp_path / "runtime",
    )
    await runtime.start()
    api = FastAPI()

    async def authenticate() -> HostSubject:
        return subject

    api.include_router(create_app_engine_router(runtime, authenticate=authenticate))
    transport = httpx.ASGITransport(app=api)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            catalog = await client.get("/api/app-engine/catalog")
            assert catalog.status_code == 200
            assert catalog.json()["apps"][0]["id"] == "demo"
            found = await client.get("/api/app-engine/catalog", params={"q": "dem"})
            assert [app["id"] for app in found.json()["apps"]] == ["demo"]
            filtered = await client.get("/api/app-engine/catalog", params={"category": "games"})
            assert filtered.json()["apps"] == []
            categories = await client.get("/api/app-engine/categories")
            assert {c["id"]: c["count"] for c in categories.json()}["games"] == 0

            studio = await client.get("/api/app-engine/studio/")
            assert studio.status_code == 200
            assert "App Studio" in studio.text
            assert "app-engine-csrf-url" in studio.text
            assert "o.entry_url||o.url" in studio.text
            assert "approval_required" in studio.text

            opened = await client.post("/api/app-engine/apps/demo/open", json={})
            assert opened.status_code == 200
            session_id = opened.json()["session_id"]
            asset = await client.get(
                f"/api/app-engine/sessions/{session_id}/assets/index.html"
            )
            assert asset.status_code == 200
            assert asset.text == "<h1>demo</h1>"
            closed = await client.delete(f"/api/app-engine/sessions/{session_id}")
            assert closed.status_code == 204
    finally:
        await runtime.close(1.0)


@pytest.mark.asyncio
async def test_router_uses_host_authentication_dependency(tmp_path):
    from app_engine.routes import create_app_engine_router
    from app_engine.runtime import DefaultAppEngineRuntime, LocalHostAdapter

    apps = tmp_path / "apps"
    apps.mkdir()
    subject = HostSubject("local", "admin")
    runtime = DefaultAppEngineRuntime(
        host=LocalHostAdapter(
            apps_roots=(apps,), state_root=tmp_path / "state", subject=subject
        ),
        subject=subject,
        runtime_root=tmp_path / "runtime",
    )
    await runtime.start()
    api = FastAPI()

    async def deny() -> HostSubject:
        raise HTTPException(401, "login required")

    api.include_router(create_app_engine_router(runtime, authenticate=deny))
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api), base_url="http://test"
        ) as client:
            response = await client.get("/api/app-engine/catalog")
        assert response.status_code == 401
    finally:
        await runtime.close(1.0)


@pytest.mark.asyncio
async def test_studio_import_inspects_the_path_as_the_caller():
    from app_engine.routes import create_app_engine_router

    seen = {}

    class Studio:
        async def inspect_import(self, path, subject=None):
            seen["path"], seen["subject"] = path, subject
            return SimpleNamespace(
                root=path, fingerprint="fp-1", suggested_template_id=None,
                suggested_manifest=None, evidence=("pyproject.toml",),
            )

    caller = HostSubject("alice", "admin")
    api = FastAPI()
    api.include_router(create_app_engine_router(
        SimpleNamespace(studio=Studio()), authenticate=lambda: caller,
    ))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api), base_url="http://test"
    ) as client:
        response = await client.post("/api/app-engine/studio/import", json={"path": "/w/app"})

    assert response.status_code == 200, response.text
    assert response.json()["fingerprint"] == "fp-1"
    assert seen == {"path": Path("/w/app"), "subject": caller}
