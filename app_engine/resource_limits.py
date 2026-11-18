"""Root-selected budgets shared by every phase and user scope of one app."""
from __future__ import annotations

from dataclasses import dataclass, fields
import hashlib
import os
from pathlib import Path
import subprocess


@dataclass(frozen=True)
class ResourceLimits:
    cpu_percent: int = 100
    memory_bytes: int = 1024 * 1024 * 1024
    tasks: int = 128
    disk_bytes: int = 1024 * 1024 * 1024  # each of data and build volumes
    disk_inodes: int = 65536
    preparation_seconds: int = 900
    service_seconds: int = 86400
    output_bytes: int = 16 * 1024 * 1024

    @classmethod
    def parse(cls, value: object) -> ResourceLimits:
        names = {field.name for field in fields(cls)}
        if (not isinstance(value, dict) or set(value) - names
                or any(type(v) is not int or not 1 <= v <= 2**40 for v in value.values())):
            raise ValueError('invalid resource limits')
        limits = cls(**value)
        if limits.cpu_percent > 800 or limits.tasks > 4096 or limits.service_seconds > 604800:
            raise ValueError('resource limits exceed safety ceiling')
        return limits

    def slice_name(self, app_id: str) -> str:
        return 'ae-' + hashlib.sha256(app_id.encode()).hexdigest()[:32] + '.slice'

    def configure_slice(self, app_id: str) -> None:
        # Aggregate across concurrent users, phases, and units; unit-only limits
        # would allow N launches to multiply the memory and process budgets.
        subprocess.run(['systemctl', 'set-property', '--runtime', self.slice_name(app_id),
                        f'CPUQuota={self.cpu_percent}%', f'MemoryMax={self.memory_bytes}',
                        'MemorySwapMax=0', f'TasksMax={self.tasks}'],
                       capture_output=True, check=True, timeout=10)

    def validate_storage(self, state: Path, app_id: str, *, multiplayer: Path | None = None) -> None:
        """Refuse ordinary host directories: persistent data needs finite volumes.

        Root provisions one dedicated filesystem for each app's data/build (and
        multiplayer state). Mounts cover all user scopes of the app. No app can
        obtain a larger budget by creating more files or launching more users.
        """
        paths = [state / 'data' / app_id, state / 'build' / app_id]
        if multiplayer is not None:
            paths.append(multiplayer / app_id)
        devices = {state.stat().st_dev}
        if multiplayer is not None:
            devices.add(multiplayer.stat().st_dev)
        for path in paths:
            status = path.stat()
            if (path.is_symlink() or path.resolve(strict=True) != path
                    or not os.path.ismount(path) or status.st_uid != 0
                    or status.st_dev in devices):
                raise ValueError('app storage requires dedicated root-owned bounded mounts')
            devices.add(status.st_dev)
            size = os.statvfs(path)
            if (not 0 < size.f_blocks * size.f_frsize <= self.disk_bytes
                    or not 0 < size.f_files <= self.disk_inodes):
                raise ValueError('app storage volume exceeds resource budget')

    def unit_properties(self, app_id: str, phase: str) -> tuple[str, ...]:
        seconds = self.preparation_seconds if phase in {'install', 'build'} else self.service_seconds
        return (f'--slice={self.slice_name(app_id)}',
                f'--property=RuntimeMaxSec={seconds}',
                '--property=TimeoutStopSec=5', '--property=KillMode=control-group',
                '--property=SendSIGKILL=yes', '--property=OOMPolicy=kill',
                '--property=LimitCORE=0', '--property=LimitNOFILE=1024',
                # PrivateTmp must not provide an unbounded host /tmp escape.
                '--property=TemporaryFileSystem=/tmp:rw,size=64M,nr_inodes=4096 '
                '/var/tmp:rw,size=64M,nr_inodes=4096')
