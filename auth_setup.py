#!/usr/bin/env python3
"""One-time interactive Google auth for the Keep bridge.

Run this once. It turns a fresh, single-use oauth2_4/ code from the browser
into a durable master token, verifies the token by actually signing in to
Keep, and saves it to .env. After it succeeds, sync.py runs unattended and
this script is not needed again unless the token is revoked.

The code is read with input() and never taken from the command line, so it
does not land in shell history or the process list.
"""

from __future__ import annotations

import os
import secrets
import sys
import tempfile
from pathlib import Path

ENV_PATH = Path(__file__).resolve().parent / ".env"


def _read_env() -> dict[str, str]:
    values: dict[str, str] = {}
    if not ENV_PATH.exists():
        return values
    for raw in ENV_PATH.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def env(name: str, default: str = "") -> str:
    return os.environ.get(name) or _read_env().get(name, default)


def set_env(name: str, value: str) -> None:
    """Rewrite one key in place, preserving the rest of the file.

    Temp-file-and-rename, so an interrupted write cannot leave a truncated
    .env holding half a credential.
    """
    lines = ENV_PATH.read_text().splitlines() if ENV_PATH.exists() else []
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

    handle, tmp_name = tempfile.mkstemp(dir=ENV_PATH.parent, prefix=".env.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(handle, "w") as fh:
            fh.write("\n".join(lines) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, ENV_PATH)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise

BROWSER_STEPS = """
Get a fresh code first -- it is single-use and expires within minutes:

  1. Open a NEW INCOGNITO window (an existing session reuses a spent code).
  2. Go to  https://accounts.google.com/EmbeddedSetup
  3. Sign in fully as {email}, then accept the prompt.
     The page may then appear to load forever. That is expected.
  4. Open devtools -> Application -> Cookies -> accounts.google.com
  5. Copy the VALUE of the cookie named  oauth_token
     It starts with  oauth2_4/  or  oauth2_1/
  6. Come straight back here and paste it. Do not wait.
"""


def device_id() -> str:
    """A stable 16-hex device id, generated once and kept in .env.

    Google ties the master token to this id, so it has to be the same value
    on every later sign-in. Generating a new one silently would invalidate a
    working token.
    """
    existing = env("KEEP_DEVICE_ID").strip()
    if existing:
        return existing
    generated = secrets.token_hex(8)
    set_env("KEEP_DEVICE_ID", generated)
    print(f"  generated a device id and saved it to .env: {generated}")
    return generated


def prompt_for_code() -> str:
    print(BROWSER_STEPS.format(email=env("EMAIL") or "your account"))
    try:
        code = input("Paste the oauth_token cookie value: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\nStopped before a code was entered.")
        raise SystemExit(1)

    if not code:
        print("No code entered.")
        raise SystemExit(1)
    if code.startswith("aas_et/"):
        print(
            "\nThat is already a master token, not a browser code.\n"
            "If it works, put it straight into .env as GOOGLE_KEEP_TOKEN.\n"
            "If it does not, get a fresh oauth_token cookie instead."
        )
        raise SystemExit(1)
    if not code.startswith(("oauth2_4/", "oauth2_1/")):
        print(
            f"\nThat does not look like an oauth_token cookie "
            f"(it starts with {code[:10]!r}).\n"
            "Copy the cookie VALUE, not the URL and not the page contents."
        )
        raise SystemExit(1)
    return code


def exchange(email: str, code: str, android_id: str) -> str:
    import gpsoauth

    print("\nExchanging the code for a master token...")
    try:
        response = gpsoauth.exchange_token(email, code, android_id)
    except Exception as exc:
        print(f"  the exchange request did not complete: {exc}")
        raise SystemExit(explain_failure())

    token = response.get("Token")
    if token:
        return token

    # Surface Google's own reason. Never echo the response wholesale: it can
    # carry other account tokens.
    reason = response.get("Error", "Unknown")
    detail = response.get("ErrorDetail", "")
    print(f"  Google declined the exchange: {reason}" + (f" -- {detail}" if detail else ""))
    if reason in ("BadAuthentication", "Unknown"):
        print(
            "\n  'Unknown' here almost always means the code was already spent\n"
            "  or had expired. A code works once, for a couple of minutes."
        )
    raise SystemExit(explain_failure())


def verify(email: str, token: str, android_id: str) -> int:
    """Prove the token works by signing in to Keep for real."""
    import gkeepapi

    print("Verifying the token by signing in to Keep...")
    keep = gkeepapi.Keep()
    try:
        keep.authenticate(email, token, device_id=android_id, sync=True)
    except Exception as exc:
        print(f"  the token was rejected by Keep: {exc}")
        raise SystemExit(explain_failure())
    return sum(1 for _ in keep.all())


def explain_failure() -> int:
    print(
        "\nWhat to redo, in order:\n"
        "  1. Close the incognito window completely.\n"
        "  2. Open a fresh incognito window.\n"
        "  3. Sign in again at https://accounts.google.com/EmbeddedSetup\n"
        "  4. Copy the NEW oauth_token cookie value.\n"
        "  5. Run this script again immediately -- within about two minutes.\n"
        "\nThe code is single-use. Re-pasting the previous one always declines."
    )
    return 1


def main() -> int:
    email = env("EMAIL").strip()
    if not email:
        print("EMAIL is not set in .env. Add it and run again.")
        return 1

    print(f"Setting up Keep access for {email}")
    android_id = device_id()
    code = prompt_for_code()
    token = exchange(email, code, android_id)
    note_count = verify(email, token, android_id)

    set_env("GOOGLE_KEEP_TOKEN", token)
    print(
        f"\nDone. Signed in and saw {note_count} notes.\n"
        f"  The master token is saved in .env as GOOGLE_KEEP_TOKEN (file mode 600).\n"
        f"  The device id is saved as KEEP_DEVICE_ID -- both are needed together.\n"
        f"  The token is long-lived; you should not need to run this again unless\n"
        f"  it is revoked by a password change or a device sign-out.\n"
        f"\nNext:  python3 sync.py"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
