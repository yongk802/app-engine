"""Operator policy for managed app command execution."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from enum import Enum


class IsolationMode(str, Enum):
    LEGACY = "legacy"
    AUDIT = "audit"
    ENFORCE = "enforce"


@dataclass(frozen=True)
class ProcessDecision:
    mode: IsolationMode
    allowed: bool
    execution_class: str
    reason: str


@dataclass(frozen=True)
class ProcessIsolationPolicy:
    mode: IsolationMode
    pilot_app_ids: frozenset[str] = frozenset()
    blocked_app_ids: frozenset[str] = frozenset()

    @classmethod
    def from_value(cls, value: str) -> ProcessIsolationPolicy:
        try:
            return cls(IsolationMode(value.strip().lower()))
        except ValueError as exc:
            raise ValueError(
                "APP_ENGINE_PROCESS_ISOLATION must be legacy, audit, or enforce"
            ) from exc

    @classmethod
    def from_environment(cls) -> ProcessIsolationPolicy:
        mode = cls.from_value(os.environ.get("APP_ENGINE_PROCESS_ISOLATION", "legacy")).mode
        return cls(
            mode,
            _app_ids_from_environment("APP_ENGINE_ISOLATED_APP_IDS"),
            _app_ids_from_environment("APP_ENGINE_BLOCKED_APP_IDS"),
        )

    def is_blocked(self, app_id: str) -> bool:
        return app_id in self.blocked_app_ids

    def mode_for(self, app_id: str | None = None) -> IsolationMode:
        return (IsolationMode.ENFORCE if app_id in self.pilot_app_ids
                else self.mode)

    def decide(self, phase: str, app_id: str | None = None) -> ProcessDecision:
        if phase not in {"install", "build", "start"}:
            raise ValueError(f"unsupported managed process phase: {phase}")
        mode = self.mode_for(app_id)
        if app_id is not None and self.is_blocked(app_id):
            return ProcessDecision(
                mode=mode,
                allowed=False,
                execution_class="blocked",
                reason="This app is blocked by the operator.",
            )
        if mode is IsolationMode.ENFORCE:
            return ProcessDecision(
                mode=mode,
                allowed=True,
                execution_class="isolated_unit",
                reason="The authenticated local broker must start a scoped unit.",
            )
        return ProcessDecision(
            mode=mode,
            allowed=True,
            execution_class="host_process",
            reason="Managed command executes on the service host.",
        )


def _app_ids_from_environment(name: str) -> frozenset[str]:
    raw = os.environ.get(name, "")
    ids = tuple(item.strip() for item in raw.split(",")) if raw else ()
    if any(not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", item) for item in ids):
        raise ValueError(f"{name} contains an invalid app ID")
    return frozenset(ids)


__all__ = ["IsolationMode", "ProcessDecision", "ProcessIsolationPolicy"]
