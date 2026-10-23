#!/usr/bin/env python3
"""Stage a verified Package Tracker backup in its private shared scope."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app_engine.contracts import AppId, ScopeKey, TargetId  # noqa: E402

FILES = frozenset({"packages.json", "settings.json", "merchants.json", "credentials.json"})
MAX_FILE = 2 * 1024 * 1024


def scoped_dir(state_root: Path) -> Path:
    scope = ScopeKey(AppId("package-tracker"), TargetId("web"), None, None)
    digest = hashlib.sha256(repr(scope).encode()).hexdigest()[:16]
    return state_root / "data" / "package-tracker" / digest


def read_archive(path: Path) -> tuple[dict[str, bytes], int, int]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("backup must be a regular file")
    files = {}
    with tarfile.open(path, "r:gz") as archive:
        members = archive.getmembers()
        if len(members) != len(FILES) or {m.name for m in members} != FILES:
            raise ValueError("unexpected backup members")
        for member in members:
            if not member.isfile() or member.size > MAX_FILE:
                raise ValueError("unsafe backup member")
            stream = archive.extractfile(member)
            if stream is None:
                raise ValueError("unreadable backup member")
            data = stream.read(MAX_FILE + 1)
            if len(data) != member.size:
                raise ValueError("invalid backup member size")
            files[member.name] = data
    values = {name: json.loads(data) for name, data in files.items()}
    packages, settings = values["packages.json"], values["settings.json"]
    if (not isinstance(packages, dict) or not isinstance(settings, dict)
            or not isinstance(settings.get("seen_ids"), list)
            or not isinstance(values["merchants.json"], dict)
            or not isinstance(values["credentials.json"], dict)):
        raise ValueError("invalid Package Tracker state")
    return files, len(packages), len(settings["seen_ids"])


def install(files: dict[str, bytes], destination: Path) -> None:
    import pwd

    if os.geteuid() != 0:
        raise PermissionError("state migration requires root")
    if destination.exists() or destination.is_symlink():
        raise FileExistsError("scoped Package Tracker state already exists")
    owner = pwd.getpwnam("appengine")
    parent = destination.parent
    for path in (parent.parent.parent, parent.parent):
        if path.is_symlink() or not path.is_dir():
            raise ValueError("unexpected runtime state root")
    parent.mkdir(mode=0o770, exist_ok=True)
    if parent.is_symlink() or not parent.is_dir():
        raise ValueError("unsafe Package Tracker state parent")
    os.chown(parent, owner.pw_uid, owner.pw_gid)
    parent.chmod(0o770)
    temporary = Path(tempfile.mkdtemp(prefix=".package-tracker-state-", dir=parent))
    try:
        os.chown(temporary, owner.pw_uid, owner.pw_gid)
        temporary.chmod(0o770)
        for name, data in files.items():
            target = temporary / name
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
                os.fchown(stream.fileno(), owner.pw_uid, owner.pw_gid)
                os.fchmod(stream.fileno(), 0o660)
        os.rename(temporary, destination)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--state-root", type=Path, required=True)
    args = parser.parse_args()
    if (args.state_root.is_symlink() or not args.state_root.is_dir()
            or args.state_root.resolve(strict=True) != args.state_root):
        parser.error("absolute, non-symlinked runtime state root required")
    try:
        files, packages, seen = read_archive(args.archive)
        destination = scoped_dir(args.state_root)
        if args.apply:
            install(files, destination)
    except (OSError, ValueError, tarfile.TarError) as exc:
        parser.error(str(exc))
    print(f"Package Tracker state: {packages} packages, {seen} seen IDs; scope {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
