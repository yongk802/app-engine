"""Private browser/MCP saves, separate from the engine's control files.

Legacy files are copied only during startup migration, never as a fallback
while serving a request. Control filenames are not eligible for migration.
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import stat
from contextlib import contextmanager
from pathlib import Path

MAX_SAVE_BYTES = 100 * 1024
_APP_ID = re.compile(r"[a-z0-9][a-z0-9-]*\Z")
_PLAYER_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_CONTROL_IDS = frozenset({"players", "stores", "local-ai", "chat"})
_log = logging.getLogger(__name__)


def _check_kind(metadata: os.stat_result, *, directory: bool) -> None:
    reparse = getattr(metadata, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if (reparse or stat.S_ISLNK(metadata.st_mode)
            or not (stat.S_ISDIR(metadata.st_mode) if directory else stat.S_ISREG(metadata.st_mode))
            or (not directory and metadata.st_nlink != 1)):
        raise ValueError("App save storage must use ordinary directories and private regular files")


def _check_windows_handle(descriptor: int, expected: Path) -> None:
    """Verify the opened handle, including a reparse-point swap during open."""
    if os.name != "nt":
        return
    import ctypes
    import msvcrt

    buffer = ctypes.create_unicode_buffer(32768)
    length = ctypes.windll.kernel32.GetFinalPathNameByHandleW(
        ctypes.c_void_p(msvcrt.get_osfhandle(descriptor)), buffer, len(buffer), 0)
    if not length or length >= len(buffer):
        raise ValueError("App save path could not be verified")
    actual = buffer.value
    if actual.startswith("\\\\?\\UNC\\"):
        actual = "\\\\" + actual[8:]
    else:
        actual = actual.removeprefix("\\\\?\\")
    if os.path.normcase(actual) != os.path.normcase(str(expected)):
        raise ValueError("App save path changed during access")


class AppSaveStore:
    def __init__(self, state_dir: Path):
        # The operator-selected root may itself be an alias. Descendants may not.
        self.root = Path(state_dir).expanduser().resolve()

    @staticmethod
    def _parts(app_id: str, player_id: str | None) -> tuple[tuple[str, ...], str]:
        if not isinstance(app_id, str) or not _APP_ID.fullmatch(app_id):
            raise ValueError("Invalid app ID")
        if player_id is None:
            return ("saves", "owner"), f"{app_id}.json"
        if not isinstance(player_id, str) or not _PLAYER_ID.fullmatch(player_id):
            raise ValueError("Invalid player ID")
        return ("saves", "players", player_id), f"{app_id}.json"

    @contextmanager
    def _directory(self, parts: tuple[str, ...], *, create: bool = False):
        """Anchor POSIX operations to no-follow directory descriptors."""
        if create:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        _check_kind(self.root.lstat(), directory=True)
        path = self.root
        descriptors = []
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            if os.name != "nt":
                descriptors.append(os.open(path, flags))
            for part in parts:
                path = path / part
                parent = descriptors[-1] if descriptors else None
                if create:
                    try:
                        if parent is None:
                            path.mkdir(mode=0o700)
                        else:
                            os.mkdir(part, mode=0o700, dir_fd=parent)
                    except FileExistsError:
                        pass
                if parent is None:
                    _check_kind(path.lstat(), directory=True)
                else:
                    descriptors.append(os.open(part, flags, dir_fd=parent))
            yield path, descriptors[-1] if descriptors else None
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    @staticmethod
    def _metadata(path: Path, directory_fd: int | None, name: str):
        return (path.joinpath(name).lstat() if directory_fd is None else
                os.stat(name, dir_fd=directory_fd, follow_symlinks=False))

    def _read(self, parts: tuple[str, ...], name: str) -> bytes | None:
        try:
            with self._directory(parts) as (path, directory_fd):
                _check_kind(self._metadata(path, directory_fd, name), directory=False)
                flags = (os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                         | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0))
                descriptor = (os.open(path / name, flags) if directory_fd is None else
                              os.open(name, flags, dir_fd=directory_fd))
                with os.fdopen(descriptor, "rb") as stream:
                    _check_kind(os.fstat(stream.fileno()), directory=False)
                    _check_windows_handle(stream.fileno(), path / name)
                    raw = stream.read(MAX_SAVE_BYTES + 1)
                if len(raw) > MAX_SAVE_BYTES:
                    raise ValueError("App state exceeds 100 KB")
                return raw
        except FileNotFoundError:
            return None

    def read(self, app_id: str, player_id: str | None = None) -> bytes | None:
        return self._read(*self._parts(app_id, player_id))

    def _publish(self, parts: tuple[str, ...], name: str, raw: bytes, *, replace: bool) -> bool:
        if len(raw) > MAX_SAVE_BYTES:
            raise ValueError("App state exceeds 100 KB")
        with self._directory(parts, create=True) as (path, directory_fd):
            try:
                _check_kind(self._metadata(path, directory_fd, name), directory=False)
            except FileNotFoundError:
                pass
            temporary = f".save-{secrets.token_hex(16)}.tmp"
            scope = {} if directory_fd is None else {"dir_fd": directory_fd}
            temp_name = path / temporary if directory_fd is None else temporary
            target_name = path / name if directory_fd is None else name
            descriptor = os.open(temp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                                 | getattr(os, "O_BINARY", 0), 0o600, **scope)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    _check_windows_handle(stream.fileno(), path / temporary)
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                operation_scope = ({} if directory_fd is None else
                                   {"src_dir_fd": directory_fd, "dst_dir_fd": directory_fd})
                if replace:
                    os.replace(temp_name, target_name, **operation_scope)
                else:
                    # Publish the completed copy without overwriting a newer save,
                    # including when two engine workers start at the same time.
                    try:
                        os.link(temp_name, target_name, **operation_scope)
                    except FileExistsError:
                        return False
            finally:
                try:
                    os.unlink(temp_name, **scope)
                except FileNotFoundError:
                    pass
            return True

    def write(self, app_id: str, raw: bytes, player_id: str | None = None) -> None:
        self._publish(*self._parts(app_id, player_id), raw, replace=True)

    def migrate_legacy(self, app_ids, *, excluded_paths=()) -> None:
        """Copy installed apps' valid saves, preserving originals for recovery.

        The four legacy control names are ambiguous and never imported as owner
        saves. Configured control files (such as an outbound mTLS policy) are
        excluded too. Missing/invalid/linked legacy files stay untouched.
        """
        ids = sorted(app_id for app_id in app_ids if _APP_ID.fullmatch(app_id))
        excluded = {Path(path).resolve() for path in excluded_paths}

        def copy(parts, app_id, player_id=None):
            source = self.root.joinpath(*parts, f"{app_id}.json")
            if source in excluded:
                return
            try:
                raw = self._read(parts, f"{app_id}.json")
                if raw is None:
                    return
                if not isinstance(json.loads(raw), (dict, list)):
                    raise TypeError("not an object or array")
                self._publish(*self._parts(app_id, player_id), raw, replace=False)
            except (OSError, TypeError, ValueError, RecursionError):
                _log.warning("Legacy app save was not migrated: %s", source)

        for app_id in ids:
            if app_id not in _CONTROL_IDS:
                copy((), app_id)
        try:
            with self._directory(("players",)) as (path, directory_fd):
                players = os.listdir(path if directory_fd is None else directory_fd)
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            _log.warning("Legacy player saves were not migrated: unsafe player directory")
            return
        for player in sorted(players):
            if _PLAYER_ID.fullmatch(player):
                for app_id in ids:
                    copy(("players", player), app_id, player)
