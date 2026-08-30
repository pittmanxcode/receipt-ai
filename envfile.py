"""Minimal .env read/write, so the bridge needs no extra dependency.

Values are never printed or logged by anything in this project.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

ENV_PATH = Path(__file__).resolve().parent / ".env"


def load(path: Path = ENV_PATH) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def get(name: str, default: str = "") -> str:
    """Environment first, then .env -- launchd passes little of the shell in."""
    return os.environ.get(name) or load().get(name, default)


def set_value(name: str, value: str, path: Path = ENV_PATH) -> None:
    """Rewrite one key in place, preserving the rest of the file.

    Written via temp-file-and-rename so an interrupted write cannot leave a
    truncated .env holding half a credential.
    """
    lines = path.read_text().splitlines() if path.exists() else []
    replaced = False
    for index, raw in enumerate(lines):
        stripped = raw.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            if stripped.partition("=")[0].strip() == name:
                lines[index] = f"{name}={value}"
                replaced = True
                break
    if not replaced:
        lines.append(f"{name}={value}")

    handle, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".env.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(handle, "w") as fh:
            fh.write("\n".join(lines) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
