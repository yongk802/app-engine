"""Validated transient systemd unit arguments for isolated app commands."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

_SAFE_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}\Z")
_SAFE_UNIT = re.compile(r"ae-[a-zA-Z0-9_-]+-(?:install|build|start)-[a-f0-9]{3,32}\Z")


@dataclass(frozen=True)
class UnitRequest:
    app_id: str
    phase: str
    app_root: Path
    data_dir: Path
    build_dir: Path
    payload: str


@dataclass(frozen=True)
class UnitBoundary:
    apps_roots: tuple[Path, ...]
    state_root: Path
    engine_uid: int
    engine_group: str

    def command(self, unit_name: str, request: UnitRequest) -> tuple[str, ...]:
        if not _SAFE_ID.fullmatch(request.app_id):
            raise ValueError("unsafe app ID")
        if request.phase not in {"install", "build", "start"}:
            raise ValueError("unsupported command phase")
        if not _SAFE_UNIT.fullmatch(unit_name) or not unit_name.startswith(
            f"ae-{request.app_id}-{request.phase}-"
        ):
            raise ValueError("invalid unit name")
        if not _SAFE_ID.fullmatch(self.engine_group):
            raise ValueError("invalid engine group")
        self._validate_paths(request)

        return (
            "systemd-run", "--quiet", "--collect", "--wait", "--pipe",
            f"--unit={unit_name}",
            "--property=DynamicUser=yes",
            f"--property=Group={self.engine_group}",
            "--property=UMask=0002",
            "--property=NoNewPrivileges=yes",
            "--property=ProtectHome=yes",
            "--property=ProtectSystem=strict",
            "--property=PrivateDevices=yes",
            "--property=PrivateTmp=yes",
            "--property=ProtectProc=invisible",
            "--property=RestrictSUIDSGID=yes",
            "--property=IPAddressDeny=any",
            "--property=IPAddressAllow=localhost",
            "--property=InaccessiblePaths=/run/app-engine-runner.sock",
            f"--property=BindPaths={request.app_root}:/workspace",
            f"--property=BindPaths={request.data_dir}:/app-data",
            f"--property=BindPaths={request.build_dir}:/app-build",
            "--property=WorkingDirectory=/workspace",
            "/usr/bin/python3", "/usr/local/libexec/app-engine/isolated_exec.py",
        )

    def _validate_paths(self, request: UnitRequest) -> None:
        if not self.apps_roots:
            raise ValueError("no app roots configured")
        apps = tuple(root.resolve(strict=True) for root in self.apps_roots)
        state = self.state_root.resolve(strict=True)
        app_root = request.app_root
        if (not app_root.is_absolute() or app_root.resolve(strict=True) != app_root
                or app_root.resolve(strict=True).parent not in apps
                or app_root.stat().st_uid != self.engine_uid):
            raise ValueError("invalid app root")
        manifest = app_root / "app.json"
        if manifest.is_symlink() or not manifest.is_file() or manifest.stat().st_size > 65536:
            raise ValueError("invalid app root manifest")
        try:
            declared = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid app root manifest") from exc
        if not isinstance(declared, dict) or declared.get("id", app_root.name) != request.app_id:
            raise ValueError("app root manifest does not match app ID")
        scope_digest = request.data_dir.name
        if not re.fullmatch(r"[a-f0-9]{16}", scope_digest):
            raise ValueError("invalid data directory")
        required = (
            (request.data_dir, state / "data" / request.app_id / scope_digest, "data directory"),
            (request.build_dir, state / "build" / request.app_id, "build directory"),
        )
        for supplied, expected, label in required:
            if (not supplied.is_absolute() or supplied.is_symlink()
                    or supplied.resolve(strict=True) != expected):
                raise ValueError(f"invalid {label}")
            if supplied.stat().st_uid != self.engine_uid:
                raise ValueError(f"invalid {label} owner")
        for path in (app_root, request.data_dir, request.build_dir):
            if any(char in str(path) for char in (":", "\n", "\r", "\x00")):
                raise ValueError("unsupported path character")
