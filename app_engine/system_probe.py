from __future__ import annotations

import os
import platform
import shutil
from pathlib import Path
from urllib.request import urlopen

from .models import SystemCapabilities


def _ram_bytes() -> int:
    if hasattr(os, "sysconf"):
        try:
            return int(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES"))
        except (ValueError, OSError):
            pass
    try:
        import ctypes
        class MemoryStatus(ctypes.Structure):
            _fields_ = [("length", ctypes.c_ulong), ("memory_load", ctypes.c_ulong),
                        ("total_phys", ctypes.c_ulonglong), ("avail_phys", ctypes.c_ulonglong),
                        ("total_page", ctypes.c_ulonglong), ("avail_page", ctypes.c_ulonglong),
                        ("total_virtual", ctypes.c_ulonglong), ("avail_virtual", ctypes.c_ulonglong),
                        ("avail_extended", ctypes.c_ulonglong)]
        status = MemoryStatus()
        status.length = ctypes.sizeof(status)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
        return int(status.total_phys)
    except Exception:
        return 0


def _ollama_check(endpoint: str) -> bool:
    try:
        with urlopen(endpoint.rstrip("/") + "/api/version", timeout=0.7) as response:
            return response.status == 200
    except Exception:
        return False


class SystemProbe:
    def __init__(self, os_name=platform.system, architecture=platform.machine,
                 ram_bytes=_ram_bytes, free_disk_bytes=lambda p: shutil.disk_usage(p).free,
                 which=shutil.which, ollama_check=_ollama_check):
        self.os_name = os_name
        self.architecture = architecture
        self.ram_bytes = ram_bytes
        self.free_disk_bytes = free_disk_bytes
        self.which = which
        self.ollama_check = ollama_check

    def inspect(self, state_dir: Path, endpoint: str) -> SystemCapabilities:
        os_key = {"Darwin": "macos", "Windows": "windows", "Linux": "linux"}.get(self.os_name(), self.os_name().lower())
        arch = {"AMD64": "x86_64", "aarch64": "arm64"}.get(self.architecture(), self.architecture())
        executable = self.which("ollama")
        running = self.ollama_check(endpoint)
        state = "running" if running else ("stopped" if executable else "missing")
        acceleration = "metal" if os_key == "macos" and arch == "arm64" else ("auto" if os_key in {"windows", "linux"} else "cpu")
        probe_path = Path(state_dir)
        while not probe_path.exists() and probe_path != probe_path.parent:
            probe_path = probe_path.parent
        return SystemCapabilities(
            os=os_key, architecture=arch, ram_bytes=max(0, int(self.ram_bytes())),
            free_disk_bytes=max(0, int(self.free_disk_bytes(probe_path))),
            acceleration=acceleration, ollama_endpoint=endpoint if running else None,
            ollama_executable=executable, ollama_service_state=state,
            installation_method="official" if state == "missing" else None,
        )
