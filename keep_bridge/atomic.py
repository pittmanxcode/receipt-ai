"""Crash-safe file writes.

The ledger is the merge base for every future run: a half-written one costs
the record of what the two sides agreed on. Write to a sibling temp file and
rename, which is atomic on POSIX, so a reader sees either the old file or the
new one and never a truncated one.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def write_text(path: Path, data: str, mode: int = 0o644) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(handle, "w") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
