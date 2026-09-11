"""Reusable FastAPI routes for an embedded app-engine runtime."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from importlib.resources import files
from pathlib import Path
from typing import Awaitable, Callable

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, StreamingResponse

from .contracts import (
    AppEngineError,
    AppId,
    AppSession,
    AppSessionId,
    ApprovalReceipt,
    AssetRequest,
    ConfigurationValue,
    CreateProjectRequest,
    HostSubject,
    InstanceId,
    LaunchId,
    LaunchRequest,
    OpenAppRequest,
    PlanFingerprint,
    ManifestChange,
    ProxyRequest,
    StopReason,
    TargetId,
)
from .runtime import DefaultAppEngineRuntime
from .search import category_counts, search_apps


Authenticate = Callable[[], HostSubject | Awaitable[HostSubject]]


def _streaming_proxy_response(result) -> StreamingResponse:
    """Build a streaming response without collapsing repeated HTTP headers."""
    response = StreamingResponse(result.body, status_code=result.status_code)
    response.raw_headers = [
        (name.lower().encode("latin-1"), value.encode("latin-1"))
        for name, value in result.headers
    ]
    return response


def _catalog_app(item) -> dict[str, object]:
    manifest = item.manifest
    return {
        "id": str(manifest.app_id),
        "label": manifest.label,
        "icon": manifest.icon,
        "version": manifest.metadata.version,
        "description": manifest.metadata.description,
        "categories": list(manifest.metadata.categories),
        "author": manifest.metadata.author,
        "default_target": str(manifest.default_target),
        "targets": [
            {
                "id": str(target.target_id),
                "kind": target.kind,
                "managed": target.runtime is not None,
            }
            for target in manifest.targets
        ],
        "browser": {
            "sandbox": manifest.browser.sandbox,
            "permissions": list(manifest.browser.permissions),
            "allow": manifest.browser.allow,
        },
        "source": item.source.kind,
        "compatible": item.compatible,
        "warnings": [asdict(warning) for warning in item.warnings],
    }


def create_app_engine_router(
    runtime: DefaultAppEngineRuntime,
    *,
    authenticate: Authenticate,
    prefix: str = "/api/app-engine",
) -> APIRouter:
    """Build a host-authenticated router without starting another server."""

    router = APIRouter(prefix=prefix, tags=["app-engine"])
    sessions: dict[AppSessionId, tuple[str, AppSession]] = {}
    launch_owners: dict[LaunchId, str] = {}
    previewed: dict[PlanFingerprint, tuple[str, LaunchRequest]] = {}
    approved: set[tuple[str, PlanFingerprint]] = set()

    def _launch_request(app_id: str, subject: HostSubject, payload: dict) -> LaunchRequest:
        target = payload.get("target_id")
        instance = payload.get("instance_id")
        return LaunchRequest(
            app_id=AppId(app_id),
            target_id=TargetId(str(target)) if target else None,
            subject=subject,
            instance_id=InstanceId(str(instance)) if instance else None,
            force_rebuild=bool(payload.get("force_rebuild", False)),
        )

    def _managed_target(launch: LaunchRequest) -> bool:
        for item in runtime.catalog.snapshot(runtime.catalog_key).apps:
            if item.manifest.app_id != launch.app_id:
                continue
            target_id = launch.target_id or item.manifest.default_target
            return any(
                target.target_id == target_id and target.runtime is not None
                for target in item.manifest.targets
            )
        return False

    def _require_launch_owner(launch_id: LaunchId, subject: HostSubject) -> None:
        if launch_owners.get(launch_id) != subject.subject_id:
            raise HTTPException(404, "launch not found")

    @router.get("/catalog")
    async def catalog(
        q: str = "",
        category: list[str] = Query(default=[]),
        subject: HostSubject = Depends(authenticate),
    ):
        del subject
        snapshot = runtime.catalog.snapshot(runtime.catalog_key)
        apps = [_catalog_app(item) for item in snapshot.apps]
        return {
            "generation": snapshot.generation,
            "apps": search_apps(apps, q, category),
            "rejections": [asdict(item) for item in snapshot.rejections],
        }

    @router.get("/categories")
    async def categories(subject: HostSubject = Depends(authenticate)):
        del subject
        snapshot = runtime.catalog.snapshot(runtime.catalog_key)
        return category_counts(_catalog_app(item) for item in snapshot.apps)

    @router.post("/catalog/refresh")
    async def refresh(subject: HostSubject = Depends(authenticate)):
        snapshot = await runtime.catalog.configure(
            runtime.catalog_key, await runtime.host.sources(subject)
        )
        return {"generation": snapshot.generation}

    @router.get("/diagnostics")
    async def diagnostics(subject: HostSubject = Depends(authenticate)):
        del subject
        return runtime.diagnostics()

    @router.post("/apps/{app_id}/preview")
    async def preview(app_id: str, request: Request, subject: HostSubject = Depends(authenticate)):
        payload = await request.json() if await request.body() else {}
        launch = _launch_request(app_id, subject, payload)
        try:
            plan = await runtime.lifecycle.preview(launch)
        except AppEngineError as exc:
            raise HTTPException(400, detail={"code": exc.code, "message": str(exc)}) from exc
        previewed[plan.fingerprint] = (subject.subject_id, launch)
        return {
            "fingerprint": str(plan.fingerprint),
            "approval_required": plan.approval_required,
            "cache_status": plan.cache_status.value,
            "scope": asdict(plan.scope_key),
            "steps": [
                {"phase": step.phase, "argv": list(step.command.argv), "cwd": str(step.cwd)}
                for step in plan.steps
            ],
        }

    @router.post("/plans/{fingerprint}/approve", status_code=204)
    async def approve(fingerprint: str, subject: HostSubject = Depends(authenticate)):
        typed = PlanFingerprint(fingerprint)
        owner = previewed.get(typed)
        if owner is None or owner[0] != subject.subject_id:
            raise HTTPException(404, "previewed plan not found")
        approved.add((subject.subject_id, typed))
        return Response(status_code=204)

    @router.post("/apps/{app_id}/open")
    async def open_app(app_id: str, request: Request, subject: HostSubject = Depends(authenticate)):
        body = await request.body()
        payload = await request.json() if body else {}
        launch = _launch_request(app_id, subject, payload)
        try:
            plan = await runtime.lifecycle.preview(launch) if _managed_target(launch) else None
            receipt = None
            if plan is not None and plan.approval_required:
                key = (subject.subject_id, plan.fingerprint)
                if key not in approved:
                    previewed[plan.fingerprint] = (subject.subject_id, launch)
                    raise HTTPException(
                        409,
                        detail={
                            "code": "approval_required",
                            "fingerprint": str(plan.fingerprint),
                        },
                    )
                approved.discard(key)
                receipt = ApprovalReceipt(
                    fingerprint=plan.fingerprint,
                    subject_id=subject.subject_id,
                    approved_at=datetime.now(timezone.utc),
                )
            session = await runtime.gateway.open(OpenAppRequest(launch, receipt))
        except HTTPException:
            raise
        except AppEngineError as exc:
            raise HTTPException(400, detail={"code": exc.code, "message": str(exc)}) from exc
        sessions[session.session_id] = (subject.subject_id, session)
        if session.launch_id is not None:
            launch_owners[session.launch_id] = subject.subject_id
        return {
            "session_id": str(session.session_id),
            "app_id": str(session.app_id),
            "launch_id": str(session.launch_id) if session.launch_id else None,
            "entry_url": session.entry_url,
        }

    @router.delete("/sessions/{session_id}", status_code=204)
    async def close_session(session_id: str, subject: HostSubject = Depends(authenticate)):
        typed = AppSessionId(session_id)
        owned = sessions.get(typed)
        if owned is not None and owned[0] != subject.subject_id:
            raise HTTPException(404, "app session not found")
        session = sessions.pop(typed, ("", None))[1]
        if session is not None:
            await runtime.gateway.close(typed)
        return Response(status_code=204)

    @router.get("/sessions/{session_id}/assets/{asset_path:path}")
    async def asset(session_id: str, asset_path: str, request: Request, subject: HostSubject = Depends(authenticate)):
        owned = sessions.get(AppSessionId(session_id))
        if owned is None or owned[0] != subject.subject_id:
            raise HTTPException(404, "app session not found")
        session = owned[1]
        try:
            result = await runtime.gateway.serve_asset(
                session,
                AssetRequest(asset_path, request.headers.get("if-none-match")),
            )
        except AppEngineError as exc:
            raise HTTPException(404, detail={"code": exc.code}) from exc
        return Response(
            content=result.body,
            status_code=result.status_code,
            media_type=result.media_type,
            headers=dict(result.headers),
        )

    @router.api_route(
        "/sessions/{session_id}/proxy/{proxy_path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    )
    async def proxy(session_id: str, proxy_path: str, request: Request, subject: HostSubject = Depends(authenticate)):
        owned = sessions.get(AppSessionId(session_id))
        if owned is None or owned[0] != subject.subject_id:
            raise HTTPException(404, "app session not found")
        session = owned[1]

        async def request_body():
            async for chunk in request.stream():
                yield chunk

        result = await runtime.gateway.proxy(
            session,
            ProxyRequest(
                method=request.method,
                path=proxy_path,
                query=tuple(request.query_params.multi_items()),
                headers=tuple(request.headers.items()),
                body=request_body(),
            ),
        )
        return _streaming_proxy_response(result)

    @router.get("/launches/{launch_id}")
    async def launch_status(launch_id: str, subject: HostSubject = Depends(authenticate)):
        typed = LaunchId(launch_id)
        _require_launch_owner(typed, subject)
        return asdict(await runtime.lifecycle.status(typed))

    @router.get("/launches/{launch_id}/logs")
    async def launch_logs(
        launch_id: str,
        after: int | None = None,
        limit: int = 200,
        subject: HostSubject = Depends(authenticate),
    ):
        typed = LaunchId(launch_id)
        _require_launch_owner(typed, subject)
        page = await runtime.lifecycle.logs(typed, after, min(limit, 1000))
        return asdict(page)

    @router.post("/launches/{launch_id}/stop")
    async def stop_launch(launch_id: str, subject: HostSubject = Depends(authenticate)):
        typed = LaunchId(launch_id)
        _require_launch_owner(typed, subject)
        result = await runtime.lifecycle.stop(typed, StopReason.USER)
        if result.stopped:
            launch_owners.pop(typed, None)
        return asdict(result)

    @router.get("/studio/templates")
    async def studio_templates(subject: HostSubject = Depends(authenticate)):
        del subject
        return [asdict(item) for item in await runtime.studio.templates()]

    @router.get("/studio/toolchains")
    async def studio_toolchains(subject: HostSubject = Depends(authenticate)):
        del subject
        return asdict(await runtime.studio.toolchains())

    @router.get("/studio/roots")
    async def studio_roots(subject: HostSubject = Depends(authenticate)):
        return [
            {
                "root": str(item.root),
                "inspect": item.inspect,
                "create": item.create,
                "write": item.write,
            }
            for item in await runtime.host.studio_roots(subject)
        ]

    @router.post("/studio/create/preview")
    async def studio_create_preview(request: Request, subject: HostSubject = Depends(authenticate)):
        payload = await request.json()
        project = CreateProjectRequest(
            subject=subject,
            root=Path(str(payload["root"])),
            directory_name=str(payload["directory_name"]),
            template_id=str(payload["template_id"]),
            app_id=AppId(str(payload["app_id"])),
            label=str(payload["label"]),
            icon=str(payload["icon"]),
        )
        try:
            plan = await runtime.studio.preview_create(project)
        except (AppEngineError, KeyError) as exc:
            if isinstance(exc, AppEngineError):
                detail = {"code": exc.code, "message": str(exc)}
            else:
                detail = {"code": "invalid_request", "message": f"missing {exc.args[0]}"}
            raise HTTPException(400, detail=detail) from exc
        return {
            "fingerprint": str(plan.fingerprint),
            "destination": str(plan.destination),
            "files": [
                {"path": str(item.path), "content_sha256": item.content_sha256}
                for item in plan.files
            ],
            "lifecycle_commands": [list(item.argv) for item in plan.lifecycle_commands],
        }

    @router.post("/studio/create")
    async def studio_create(request: Request, subject: HostSubject = Depends(authenticate)):
        payload = await request.json()
        project = CreateProjectRequest(
            subject=subject,
            root=Path(str(payload["root"])),
            directory_name=str(payload["directory_name"]),
            template_id=str(payload["template_id"]),
            app_id=AppId(str(payload["app_id"])),
            label=str(payload["label"]),
            icon=str(payload["icon"]),
        )
        try:
            created = await runtime.studio.create(
                project, PlanFingerprint(str(payload["accepted_plan"]))
            )
            await runtime.catalog.refresh(runtime.catalog_key)
        except (AppEngineError, KeyError) as exc:
            detail = {"code": getattr(exc, "code", "invalid_request"), "message": str(exc)}
            raise HTTPException(400, detail=detail) from exc
        return {"root": str(created.root), "app_id": str(created.manifest.app_id)}

    @router.post("/studio/import")
    async def studio_import(request: Request, subject: HostSubject = Depends(authenticate)):
        del subject
        payload = await request.json()
        try:
            proposal = await runtime.studio.inspect_import(
                Path(str(payload["path"])), subject=subject
            )
        except (AppEngineError, KeyError) as exc:
            raise HTTPException(400, detail={"code": getattr(exc, "code", "invalid_request"), "message": str(exc)}) from exc
        return {
            "root": str(proposal.root),
            "fingerprint": str(proposal.fingerprint),
            "suggested_template_id": proposal.suggested_template_id,
            "suggested_app_id": (
                str(proposal.suggested_manifest.app_id)
                if proposal.suggested_manifest else None
            ),
            "evidence": list(proposal.evidence),
        }

    @router.post("/studio/manifest/preview")
    async def studio_manifest_preview(request: Request, subject: HostSubject = Depends(authenticate)):
        payload = await request.json()
        change = ManifestChange(
            subject=subject,
            app_root=Path(str(payload["app_root"])),
            content=str(payload["content"]),
        )
        plan = await runtime.studio.preview_manifest(change)
        return {
            "fingerprint": str(plan.fingerprint),
            "valid": plan.inspection.manifest is not None,
            "errors": [asdict(item) for item in plan.inspection.errors],
            "warnings": [asdict(item) for item in plan.inspection.warnings],
        }

    @router.post("/studio/manifest/apply")
    async def studio_manifest_apply(request: Request, subject: HostSubject = Depends(authenticate)):
        payload = await request.json()
        change = ManifestChange(
            subject=subject,
            app_root=Path(str(payload["app_root"])),
            content=str(payload["content"]),
        )
        try:
            inspection = await runtime.studio.apply_manifest(
                change, PlanFingerprint(str(payload["accepted_plan"]))
            )
            await runtime.catalog.refresh(runtime.catalog_key)
        except AppEngineError as exc:
            raise HTTPException(400, detail={"code": exc.code, "message": str(exc)}) from exc
        return {
            "valid": inspection.manifest is not None,
            "errors": [asdict(item) for item in inspection.errors],
            "warnings": [asdict(item) for item in inspection.warnings],
        }

    @router.get("/apps/{app_id}/configuration")
    async def configuration_form(app_id: str, subject: HostSubject = Depends(authenticate)):
        form = await runtime.studio.configuration_form(subject, AppId(app_id))
        return {
            "app_id": str(form.app_id),
            "fields": [
                {
                    **asdict(field),
                    "value_type": field.value_type.value,
                    "scope": field.scope.value,
                }
                for field in form.fields
            ],
            "values": [
                {"key": item.key, "value": "" if item.secret else item.value, "configured": True, "secret": item.secret}
                for item in form.values
            ],
        }

    @router.put("/apps/{app_id}/configuration")
    async def save_configuration(app_id: str, request: Request, subject: HostSubject = Depends(authenticate)):
        await runtime.studio.configuration_form(subject, AppId(app_id))
        payload = await request.json()
        values = tuple(
            ConfigurationValue(
                key=str(item["key"]),
                value=str(item.get("value", "")),
                secret=bool(item.get("secret", False)),
            )
            for item in payload.get("values", ())
        )
        result = await runtime.studio.save_configuration(subject, values)
        return {"app_id": str(result.app_id), "saved_keys": list(result.saved_keys)}

    @router.get("/studio/", response_class=HTMLResponse)
    async def studio_ui(subject: HostSubject = Depends(authenticate)):
        del subject
        html = files("app_engine").joinpath("studio/index.html").read_text("utf-8")
        return HTMLResponse(html, headers={"Cache-Control": "no-cache"})

    return router


__all__ = ["create_app_engine_router"]
