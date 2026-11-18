"""Serialize account transactions across engine threads and the operator CLI."""
from __future__ import annotations

import os
import stat
from contextlib import contextmanager
from functools import wraps


@contextmanager
def account_file_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path.parent / '.players.lock', os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1:
            raise ValueError('invalid account lock file')
        if os.name == 'posix':
            import fcntl
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        elif os.name == 'nt':
            import msvcrt
            if status.st_size == 0:
                os.write(descriptor, b'\0')
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
        else:
            raise ValueError('account locking is unavailable')
        yield
    finally:
        os.close(descriptor)


def account_transaction(method=None, *, refresh=True):
    def decorate(operation):
        @wraps(operation)
        def wrapped(self, *args, **kwargs):
            with self._mutex:
                if self._transaction_depth:
                    return operation(self, *args, **kwargs)
                with account_file_lock(self.path):
                    self._transaction_depth += 1
                    try:
                        if refresh:
                            self.refresh(force=True)
                        return operation(self, *args, **kwargs)
                    finally:
                        self._transaction_depth -= 1
        return wrapped
    return decorate(method) if method is not None else decorate
