"""Target-provider registry tests for current and future platform kinds."""

from __future__ import annotations

import json

import pytest

from app_engine.contracts import (
    PlanFingerprint,
    PreparedTarget,
    ProjectTemplate,
    StopResult,
    TargetEvent,
    TargetProvider,
    ToolchainReport,
    ValidationReport,
)
from app_engine.manifest import parse_manifest


def _registry_type():
    try:
        from app_engine.providers import ProviderRegistry
    except ImportError as exc:
        pytest.fail(
            "app_engine.providers.ProviderRegistry is not implemented: " f"{exc}"
        )
    return ProviderRegistry


class _FutureProvider(TargetProvider):
    kind = "ios"

    async def probe(self) -> ToolchainReport:
        return ToolchainReport(())

    def templates(self) -> tuple[ProjectTemplate, ...]:
        return ()

    def validate(self, manifest, target) -> ValidationReport:
        return ValidationReport()

    async def prepare(self, context) -> PreparedTarget:
        return PreparedTarget(context, PlanFingerprint("future"), ())

    async def launch(self, target):
        raise NotImplementedError

    async def observe(self, session):
        if False:
            yield TargetEvent(state=None)

    async def stop(self, session, grace_seconds: float) -> StopResult:
        raise NotImplementedError


def test_registry_accepts_future_provider_kind_without_parser_change(tmp_path):
    root = tmp_path / "mobile"
    root.mkdir()
    (root / "app.json").write_text(
        json.dumps(
            {
                "manifest_version": 2,
                "id": "mobile",
                "label": "Mobile",
                "icon": "📱",
                "default_target": "phone",
                "targets": {
                    "phone": {"kind": "ios"},
                },
            }
        ),
        encoding="utf-8",
    )

    inspection = parse_manifest(root)
    assert inspection.errors == ()
    target = inspection.manifest.targets[0]
    assert target.kind == "ios"

    provider = _FutureProvider()
    registry = _registry_type()()
    registry.register(provider)

    assert registry.get(target.kind) is provider

