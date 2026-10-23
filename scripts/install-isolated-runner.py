#!/usr/bin/env python3
"""Install the isolated command broker from a root-owned release checkout.

The socket can be installed while app-engine remains in audit mode. Only an
explicit, separately verified engine configuration selects enforce mode.
"""

from __future__ import annotations

import argparse
import grp
import json
import os
import pwd
import re
import stat
import subprocess
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LIB = Path("/usr/local/libexec/app-engine")
CONFIG = Path("/etc/app-engine-runner.json")
SYSTEMD = Path("/etc/systemd/system")
MODULES = ("isolated_units.py", "isolation_wire.py", "isolation_broker.py")
_SAFE_ACCOUNT = re.compile(r"[a-zA-Z_][a-zA-Z0-9_-]{0,31}\Z")


def _trusted_source(path: Path) -> None:
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError(f"source path is a symlink: {part}")
        metadata = part.stat()
        if metadata.st_uid != 0 or metadata.st_mode & 0o022:
            raise ValueError(f"source path is not root controlled: {part}")
    if not stat.S_ISREG(path.stat().st_mode):
        raise ValueError(f"source is not a regular file: {path}")


def _install(path: Path, content: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    for parent in (path.parent, *path.parent.parents):
        metadata = parent.stat()
        if (parent.is_symlink() or metadata.st_uid != 0
                or metadata.st_mode & 0o022):
            raise ValueError(f"destination directory is not root controlled: {parent}")
    fd, temporary = tempfile.mkstemp(prefix=".app-engine-runner-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fchmod(stream.fileno(), mode)
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apps-root", type=Path, action="append", required=True)
    parser.add_argument("--state-root", type=Path, required=True,
                        help="Runtime state root containing data/ and build/")
    parser.add_argument("--engine-user", default="appengine")
    parser.add_argument("--engine-group", default="appengine")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--check", action="store_true")
    action.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if (not _SAFE_ACCOUNT.fullmatch(args.engine_user)
            or not _SAFE_ACCOUNT.fullmatch(args.engine_group)):
        parser.error("invalid engine account")
    try:
        pwd.getpwnam(args.engine_user)
        grp.getgrnam(args.engine_group)
    except KeyError:
        parser.error("engine user or group does not exist")
    roots = [str(path.resolve(strict=True)) for path in args.apps_root]
    state = str(args.state_root.resolve(strict=True))
    if any(not Path(path).is_dir() for path in (*roots, state)):
        parser.error("app and state roots must be directories")
    policy = json.dumps({"apps_roots": roots, "state_root": state,
                         "engine_user": args.engine_user,
                         "engine_group": args.engine_group},
                        sort_keys=True, indent=2).encode() + b"\n"
    files = {LIB / "app_engine" / name: REPO / "app_engine" / name
             for name in MODULES}
    files[LIB / "isolated_exec.py"] = REPO / "app_engine" / "isolated_exec.py"
    files[LIB / "tcgcsv_gateway.py"] = REPO / "app_engine" / "tcgcsv_gateway.py"
    socket_source = REPO / "scripts" / "systemd" / "app-engine-runner.socket"
    service_source = REPO / "scripts" / "systemd" / "app-engine-runner@.service"
    files[SYSTEMD / socket_source.name] = socket_source
    files[SYSTEMD / service_source.name] = service_source
    gateway_source = REPO / "scripts" / "systemd" / "app-engine-tcgcsv-gateway.service"
    files[SYSTEMD / gateway_source.name] = gateway_source
    for source in files.values():
        if not source.is_file():
            parser.error(f"missing runner source file: {source}")
    if args.check:
        print("Runner installation is ready for a root-owned release checkout.")
        return 0
    if os.geteuid() != 0:
        parser.error("--apply must run as root")
    for source in files.values():
        _trusted_source(source)
    for destination, source in files.items():
        content = source.read_bytes()
        if source == socket_source:
            content = content.replace(b"SocketUser=appengine",
                                      f"SocketUser={args.engine_user}".encode())
        _install(destination, content, 0o644)
    _install(LIB / "app_engine" / "__init__.py", b"", 0o644)
    _install(CONFIG, policy, 0o600)
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "enable", "--now", "app-engine-runner.socket"], check=True)
    subprocess.run(["systemctl", "enable", "--now", "app-engine-tcgcsv-gateway.service"], check=True)
    print("Isolated runner socket and price gateway installed; app-engine mode is unchanged.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
