"""Bound approvals to the whole app, including undeclared imports and data."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

MAX_FILES = 20000
MAX_BYTES = 512 * 1024 * 1024


def tree_digest(root: Path) -> str:
    """Reject links, special files, oversized trees and files changed while hashing.

    No manifest-controlled exclusions: an output, dotfile or data file can also
    be executable input. Apps should write mutable data outside their package.
    """
    root = root.resolve(strict=True)
    digest = hashlib.sha256()
    count = total = 0
    def unreadable(error):
        raise error

    for directory, dirs, files in os.walk(root, followlinks=False, onerror=unreadable):
        for name in sorted(dirs + files):
            path = Path(directory) / name
            before = path.lstat()
            count += 1
            if count > MAX_FILES:
                raise ValueError("app exceeds the code approval file budget")
            relative = path.relative_to(root).as_posix().encode()
            digest.update(len(relative).to_bytes(8, "big") + relative)
            if stat.S_ISDIR(before.st_mode):
                digest.update(b"directory\0")
                continue
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise ValueError("approval requires regular, unlinked app files")
            total += before.st_size
            if count > MAX_FILES or total > MAX_BYTES:
                raise ValueError("app exceeds the code approval size budget")
            digest.update(before.st_size.to_bytes(8, "big"))
            digest.update((before.st_mode & 0o777).to_bytes(2, "big"))
            with path.open("rb") as stream:
                opened = os.fstat(stream.fileno())
                if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                    raise ValueError("app changed during approval")
                read = 0
                for chunk in iter(lambda: stream.read(128 * 1024), b""):
                    read += len(chunk)
                    if read > before.st_size:
                        raise ValueError("app changed during approval")
                    digest.update(chunk)
                after = os.fstat(stream.fileno())
            # Windows Python versions can expose different timestamp representations
            # through path stat and handle stat. Compare like with like, while
            # also checking that the pathname still names the file we hashed.
            if (_file_snapshot(opened) != _file_snapshot(after)
                    or _file_snapshot(before) != _file_snapshot(path.lstat())):
                raise ValueError("app changed during approval")
        dirs.sort()
    return digest.hexdigest()


def _file_snapshot(value):
    return (value.st_dev, value.st_ino, value.st_mode, value.st_nlink,
            value.st_size, value.st_mtime_ns, value.st_ctime_ns)
