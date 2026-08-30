"""A single-run lock.

Two syncs running at once (a cron job overlapping a manual run) would both
read the same ledger, both merge against a stale base and both write -- which
duplicates notes and corrupts the merge base. One run at a time.
"""

from __future__ import annotations

import errno
import fcntl
import os
from contextlib import contextmanager
from pathlib import Path


class LockBusy(RuntimeError):
    pass


@contextmanager
def run_lock(path: Path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise LockBusy(f"another keep-bridge run holds {path}") from exc
            raise
        os.truncate(handle, 0)
        os.write(handle, f"{os.getpid()}\n".encode())
        yield
    finally:
        try:
            fcntl.flock(handle, fcntl.LOCK_UN)
        finally:
            os.close(handle)
