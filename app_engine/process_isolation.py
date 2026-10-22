"""Operator policy for managed app command execution.

There is currently no verified isolated runner. Enforce mode therefore denies
every managed command rather than falling back to a host subprocess.
"""

from __future__ import annotations

import os
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
        return cls.from_value(
            os.environ.get("APP_ENGINE_PROCESS_ISOLATION", "legacy")
        )

    def decide(self, phase: str) -> ProcessDecision:
        if phase not in {"install", "build", "start"}:
            raise ValueError(f"unsupported managed process phase: {phase}")
        if self.mode is IsolationMode.ENFORCE:
            return ProcessDecision(
                mode=self.mode,
                allowed=False,
                execution_class="denied",
                reason="No verified isolated runner is configured; host execution is forbidden.",
            )
        return ProcessDecision(
            mode=self.mode,
            allowed=True,
            execution_class="host_process",
            reason="Managed command executes on the service host.",
        )


__all__ = ["IsolationMode", "ProcessDecision", "ProcessIsolationPolicy"]
