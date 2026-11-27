"""Owner-only routes behind the Security app: reports, rescans and waivers."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from importlib.resources import files
import json
from pathlib import Path
import re

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .app_review import BLOCKING, OWASP, RULES, ReviewGate, ReviewReport, app_id_of
from .response_headers import nonce_page

_APP_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_FINGERPRINT = re.compile(r"^[0-9a-f]{20}$")
_HEADERS = {"Cache-Control": "no-store"}


class _ReviewApi:
    """``app_roots`` yields (app folder, review profile) in serving priority order."""

    def __init__(self, gate: ReviewGate, app_roots: Callable[[], Iterable[tuple[Path, str]]],
                 on_change: Callable[[], Awaitable[None]], require_owner, require_owner_action,
                 is_launcher_host, admin_capability: str) -> None:
        self.gate = gate
        self.app_roots = app_roots
        self.on_change = on_change
        self.require_owner = require_owner
        self.require_owner_action = require_owner_action
        self.is_launcher_host = is_launcher_host
        self.admin_capability = admin_capability

    # ── helpers ──
    def require_read(self, request: Request) -> None:
        self.require_owner(request)
        if not self.is_launcher_host(request):
            raise HTTPException(403, "launcher origin required")

    def snapshot(self) -> tuple[dict[str, tuple[ReviewReport, bool]], int]:
        """Every known report without scanning, plus how many scans are pending.

        Apps whose report is stale or missing are rescanned in the background,
        so the first visit after a restart or an engine update answers at once.
        """
        store = self.gate.store
        out: dict[str, tuple[ReviewReport, bool]] = {}
        claimed: set[str] = set()
        stale: list[tuple[Path, str]] = []
        for root, profile in self.app_roots():
            report, current = store.peek(root, profile=profile)
            app_id = report.app_id if report is not None else app_id_of(root)
            if app_id in claimed:
                continue
            claimed.add(app_id)
            if not current:
                stale.append((root, profile))
            if report is not None:
                out[app_id] = (report, current)
        if stale:
            store.refresh_later(stale)
        return out, store.pending()

    def one(self, app_id: str, force: bool = False) -> ReviewReport:
        """Scan only the requested app, the first root (by priority) that serves it."""
        if not _APP_ID.match(app_id):
            raise HTTPException(404)
        for root, profile in self.app_roots():
            report, _ = self.gate.store.peek(root, profile=profile)
            if (report.app_id if report is not None else app_id_of(root)) != app_id:
                continue
            report = self.gate.store.report(root, profile=profile, force=force)
            if report.app_id == app_id:
                return report
        raise HTTPException(404, "no such app")

    def served(self, report: ReviewReport) -> bool:
        return report.passed or self.gate.mode != "enforce"

    def answer(self, report: ReviewReport) -> JSONResponse:
        return JSONResponse(dict(report.to_dict(), served=self.served(report), mode=self.gate.mode),
                            headers=_HEADERS)

    # ── routes ──
    async def overview(self, request: Request):
        self.require_read(request)
        apps = []
        reports, pending = await asyncio.to_thread(self.snapshot)
        for report, current in reports.values():
            value = report.to_dict()
            value.pop("findings")
            value["served"] = self.served(report)
            value["stale"] = not current
            apps.append(value)
        apps.sort(key=lambda item: (item["verdict"] == "pass", item["score"], item["app_id"]))
        return JSONResponse({"mode": self.gate.mode, "apps": apps, "scanning": pending}, headers=_HEADERS)

    async def rules(self, request: Request):
        self.require_read(request)
        return JSONResponse({
            "blocking": sorted(BLOCKING),
            "categories": OWASP,
            "rules": [{"rule": r.rule, "severity": r.severity, "category": OWASP[r.owasp],
                       "title": r.title, "remediation": r.remediation} for r in RULES],
        }, headers=_HEADERS)

    async def detail(self, app_id: str, request: Request):
        self.require_read(request)
        return self.answer(await asyncio.to_thread(self.one, app_id))

    async def rescan(self, app_id: str, request: Request):
        self.require_owner_action(request)
        report = await asyncio.to_thread(self.one, app_id, True)
        await self.on_change()
        return self.answer(report)

    async def waive(self, app_id: str, request: Request):
        self.require_owner_action(request)
        fingerprint, reason = _waiver_request(await request.body())
        report = await asyncio.to_thread(self.one, app_id)
        finding = next((item for item in report.findings if item.fingerprint == fingerprint), None)
        if finding is None:
            raise HTTPException(404, "no such finding in the current report")
        if not finding.waivable:
            raise HTTPException(409, "this finding cannot be waived; fix the app instead")
        await asyncio.to_thread(self.gate.store.set_waiver, app_id, fingerprint, reason)
        await self.on_change()
        return self.answer(await asyncio.to_thread(self.one, app_id))

    async def unwaive(self, app_id: str, fingerprint: str, request: Request):
        self.require_owner_action(request)
        if not _APP_ID.match(app_id) or not _FINGERPRINT.match(fingerprint):
            raise HTTPException(404)
        await asyncio.to_thread(self.gate.store.set_waiver, app_id, fingerprint, None)
        await self.on_change()
        return self.answer(await asyncio.to_thread(self.one, app_id))

    async def ui(self, request: Request):
        self.require_read(request)
        html = files("app_engine").joinpath("review_ui/index.html").read_text("utf-8")
        html = html.replace("__APP_ENGINE_ADMIN_CAPABILITY__", self.admin_capability)
        html, page_headers = nonce_page(html)
        return HTMLResponse(html, headers={**_HEADERS, **page_headers})


def _waiver_request(body: bytes) -> tuple[str, str]:
    if len(body) > 4096:
        raise HTTPException(413, "request too large")
    try:
        value = json.loads(body)
    except ValueError:
        raise HTTPException(400, "provide a JSON object") from None
    if not isinstance(value, dict):
        raise HTTPException(400, "provide a JSON object")
    fingerprint, reason = value.get("fingerprint"), value.get("reason")
    if not isinstance(fingerprint, str) or not _FINGERPRINT.match(fingerprint):
        raise HTTPException(400, "fingerprint must name a finding")
    if not isinstance(reason, str) or not 3 <= len(reason.strip()) <= 500:
        raise HTTPException(400, "give a reason (3-500 characters) for the waiver")
    return fingerprint, reason.strip()


def create_review_router(gate: ReviewGate, *, app_roots: Callable[[], Iterable[tuple[Path, str]]],
                         on_change: Callable[[], Awaitable[None]],
                         require_owner, require_owner_action, is_launcher_host,
                         admin_capability: str) -> APIRouter:
    api = _ReviewApi(gate, app_roots, on_change, require_owner, require_owner_action,
                     is_launcher_host, admin_capability)
    router = APIRouter(prefix="/api/app-engine/review", tags=["review"])
    router.add_api_route("", api.overview, methods=["GET"])
    router.add_api_route("/rules", api.rules, methods=["GET"])
    router.add_api_route("/apps/{app_id}", api.detail, methods=["GET"])
    router.add_api_route("/apps/{app_id}/rescan", api.rescan, methods=["POST"])
    router.add_api_route("/apps/{app_id}/waivers", api.waive, methods=["POST"])
    router.add_api_route("/apps/{app_id}/waivers/{fingerprint}", api.unwaive, methods=["DELETE"])
    router.add_api_route("/ui", api.ui, methods=["GET"], response_class=HTMLResponse)
    return router
