#!/usr/bin/env python3
"""Keep -> Notion capture bridge.

Reads Google Keep across one or more accounts and creates one row per note in
the Notes Inbox database. Runs unattended every 15 minutes under launchd.

Read-only against Keep: this never sets an attribute on a note and never
pushes a change back. Keep is where capture happens; Notion is where the OS
picks the note up.

By default only notes touched in the last 24 hours are considered -- a rolling
window rather than "since midnight", so a note written at 23:58 is still
captured by the run after midnight. Old notes are left in Keep; there is no
backlog import unless you ask for one with --since or --all.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENV_PATH = HERE / ".env"
STATE_PATH = HERE / ".sync_state.json"
LOG_PATH = HERE / "sync.log"
LOCK_PATH = HERE / ".sync.lock"

# The one database this bridge may write to. Nothing else is ever touched.
DATA_SOURCE_ID = "0cb32494-0d57-4039-bb1d-1a6c5ed66fc1"
DATABASE_ID = "1eb54765d75c46cd8075a0c03f85b9a3"

DEFAULT_WINDOW_HOURS = 24
TITLE_LIMIT = 200        # keep inbox rows scannable
BLOCK_TEXT_LIMIT = 1900  # Notion caps a rich_text run at 2000
BLOCKS_PER_REQUEST = 100
STOP_AFTER_FAILURES = 3  # a systematic fault should report once, not 342 times


def env(name: str, default: str = "") -> str:
    """Environment first, then .env -- launchd passes little of the shell in.

    Read-only: this script never writes a credential back.
    """
    if os.environ.get(name):
        return os.environ[name]
    if not ENV_PATH.exists():
        return default
    for raw in ENV_PATH.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() != name:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        return value
    return default


def slug(email: str) -> str:
    """A .env-safe suffix for an account, independent of ordering."""
    return re.sub(r"[^A-Z0-9]", "_", email.strip().upper())


def accounts() -> list[str]:
    """Every Keep account to sync, in KEEP_ACCOUNTS order.

    Falls back to the single-account EMAIL form so an existing .env keeps
    working without being rewritten.
    """
    listed = [a.strip() for a in env("KEEP_ACCOUNTS").split(",") if a.strip()]
    if listed:
        return listed
    single = env("EMAIL").strip()
    return [single] if single else []


def credentials(email: str) -> tuple[str, str | None]:
    """(master token, device id) for one account.

    The legacy single-account keys are honoured only while KEEP_ACCOUNTS is
    unset. Once accounts are listed explicitly, every one must carry its own
    token: a shared fallback could hand one account another's credentials and
    file its notes under the wrong address.
    """
    token = env(f"KEEP_TOKEN_{slug(email)}").strip()
    device = env(f"KEEP_DEVICE_ID_{slug(email)}").strip()
    if not token and not env("KEEP_ACCOUNTS").strip() and email == env("EMAIL").strip():
        token = env("GOOGLE_KEEP_TOKEN").strip()
        device = env("KEEP_DEVICE_ID").strip()
    return token, (device or None)


def log(message: str) -> None:
    stamp = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    line = f"{stamp}  {message}"
    print(line)
    with LOG_PATH.open("a") as fh:
        fh.write(line + "\n")


# -- state ----------------------------------------------------------------


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"version": 2, "notes": {}}
    try:
        data = json.loads(STATE_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        # A damaged state file must not cause a re-import of everything.
        log("state file unreadable; stopping so nothing is duplicated")
        raise SystemExit(1)
    data.setdefault("notes", {})
    if data.get("version") == 1:
        # v1 keyed by bare Keep note id; v2 prefixes the account so two
        # accounts can never collide on an id.
        primary = (accounts() or [""])[0]
        data["notes"] = {f"{primary}::{k}": v for k, v in data["notes"].items()}
        data["version"] = 2
    return data


def save_state(state: dict) -> None:
    handle, tmp_name = tempfile.mkstemp(dir=HERE, prefix=".sync_state.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(handle, "w") as fh:
            json.dump(state, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, STATE_PATH)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


# -- Keep -----------------------------------------------------------------


def open_keep(email: str):
    import gkeepapi

    token, device = credentials(email)
    if not token:
        log(f"{email}: no master token in .env; run  python auth_setup.py  for this account")
        return None

    keep = gkeepapi.Keep()
    try:
        keep.authenticate(email, token, device_id=device, sync=True)
    except Exception as exc:
        log(f"{email}: Keep sign-in was declined ({type(exc).__name__}); re-run auth_setup.py")
        return None
    return keep


def note_updated(node):
    stamp = getattr(node.timestamps, "updated", None)
    if stamp is None:
        return None
    return stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp


def note_title(node) -> str:
    title = (node.title or "").strip()
    if not title:
        for line in (node.text or "").splitlines():
            if line.strip():
                title = line.strip()
                break
    if not title:
        when = note_updated(node) or datetime.now(timezone.utc)
        return f"Keep note {when.astimezone():%Y-%m-%d %H:%M}"
    return title[:TITLE_LIMIT]


def selected_notes(keep, since) -> list:
    """Non-trashed notes newer than `since`, optionally narrowed to a label."""
    label_name = env("KEEP_LABEL").strip()
    label = keep.findLabel(label_name) if label_name else None
    if label_name and label is None:
        log(f"KEEP_LABEL {label_name!r} does not exist in this account; skipping it")
        return []

    notes = []
    for node in keep.all():
        if node.trashed:
            continue
        if label is not None and node.labels.get(label.id) is None:
            continue
        if since is not None:
            updated = note_updated(node)
            if updated is None or updated < since:
                continue
        notes.append(node)
    return notes


# -- Notion ---------------------------------------------------------------


def text_blocks(text: str) -> list[dict]:
    blocks: list[dict] = []
    for line in (text or "").splitlines() or [""]:
        for start in range(0, max(len(line), 1), BLOCK_TEXT_LIMIT):
            chunk = line[start : start + BLOCK_TEXT_LIMIT]
            blocks.append(
                {
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {
                        "rich_text": (
                            [{"type": "text", "text": {"content": chunk}}] if chunk else []
                        )
                    },
                }
            )
    return blocks


def describe(exc: Exception) -> str:
    """Everything Notion told us, so one failure is enough to diagnose."""
    parts = [f"{type(exc).__name__}: {exc}"]
    for attribute in ("code", "status"):
        value = getattr(exc, attribute, None)
        if value is not None:
            parts.append(f"{attribute}={value}")
    body = getattr(exc, "body", None)
    if body:
        parts.append(f"body={str(body)[:400]}")
    return " | ".join(parts)


def create_row(client, node) -> str:
    blocks = text_blocks(node.text)
    properties = {
        "Note": {"title": [{"type": "text", "text": {"content": note_title(node)}}]},
        "Source": {"select": {"name": "Keep"}},
        "Status": {"select": {"name": "Unfiled"}},
        # Section, Actionable and Confidence stay empty -- the sweep fills those.
    }

    # notion_client 3.x speaks Notion-Version 2025-09-03, where a row's parent
    # is a data source. Older API versions want the database id, so try that
    # too rather than matching on the text of an error message.
    attempts = [
        {"type": "data_source_id", "data_source_id": DATA_SOURCE_ID},
        {"database_id": DATABASE_ID},
    ]
    errors = []
    page = None
    for parent in attempts:
        try:
            page = client.pages.create(
                parent=parent, properties=properties, children=blocks[:BLOCKS_PER_REQUEST]
            )
            break
        except Exception as exc:
            errors.append(f"parent={list(parent)[0]} -> {describe(exc)}")
    if page is None:
        raise RuntimeError("; ".join(errors))

    for start in range(BLOCKS_PER_REQUEST, len(blocks), BLOCKS_PER_REQUEST):
        client.blocks.children.append(
            block_id=page["id"], children=blocks[start : start + BLOCKS_PER_REQUEST]
        )
    return page["id"]


# -- run ------------------------------------------------------------------


def sync_account(email, client, state, args, budget) -> tuple[int, int, int]:
    keep = open_keep(email)
    if keep is None:
        return 0, 0, 1

    synced = state["notes"]
    created = skipped_edited = failed = 0
    consecutive = 0

    for node in selected_notes(keep, args.since):
        if budget is not None and budget[0] <= 0:
            break

        key = f"{email}::{node.id}"
        updated = note_updated(node)
        updated_iso = updated.isoformat() if updated else ""

        record = synced.get(key)
        if record:
            if updated_iso and record.get("keep_updated") and updated_iso > record["keep_updated"]:
                log(f"{email}: edited after it synced, skipped (v1 limitation): {note_title(node)!r}")
                skipped_edited += 1
            continue

        if args.dry_run:
            log(f"{email}: would create row for {note_title(node)!r}")
            created += 1
            if budget is not None:
                budget[0] -= 1
            continue

        try:
            page_id = create_row(client, node)
        except Exception as exc:
            failed += 1
            consecutive += 1
            log(f"{email}: could not create a row for {note_title(node)!r}: {describe(exc)}")
            if consecutive >= STOP_AFTER_FAILURES:
                log(
                    f"{email}: stopping after {consecutive} failures in a row -- this is a "
                    "systematic fault, not one awkward note. Fix it and run again."
                )
                break
            continue

        consecutive = 0
        synced[key] = {
            "notion_page_id": page_id,
            "keep_updated": updated_iso,
            "synced_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        # Saved per note: an interrupted run never re-creates what it just made.
        save_state(state)
        created += 1
        if budget is not None:
            budget[0] -= 1
        log(f"{email}: created row for {note_title(node)!r}")

    return created, skipped_edited, failed


def undo() -> int:
    """Remove every row this bridge created, and forget them.

    Scope is the state file: only pages this bridge recorded creating are
    touched, so nothing typed straight into the Inbox is at risk. Notion
    moves them to trash, where they can be restored for 30 days.
    """
    state = load_state()
    entries = [
        (key, record["notion_page_id"])
        for key, record in state["notes"].items()
        if record.get("notion_page_id")
    ]
    if not entries:
        log("nothing recorded as created; nothing to remove")
        return 0

    notion_token = env("NOTION_TOKEN").strip()
    if not notion_token:
        log("NOTION_TOKEN missing from .env")
        return 1

    from notion_client import Client

    client = Client(auth=notion_token)
    log(f"removing {len(entries)} rows this bridge created")

    removed = failed = 0
    for key, page_id in entries:
        try:
            try:
                client.pages.update(page_id=page_id, in_trash=True)
            except TypeError:
                client.pages.update(page_id=page_id, archived=True)
        except Exception as exc:
            failed += 1
            log(f"could not remove {page_id}: {describe(exc)}")
            continue
        state["notes"].pop(key, None)
        # Saved as we go, so an interrupted undo does not lose its place.
        save_state(state)
        removed += 1

    log(f"undo finished: {removed} removed, {failed} failed")
    return 1 if failed else 0


def check() -> int:
    """Read-only preflight: which Keep account is behind each token, and can
    Notion actually be written to.

    Creates nothing. Run this after adding an account or changing Notion
    sharing -- it answers both questions without putting a row in the Inbox.
    """
    emails = accounts()
    if not emails:
        log("no accounts configured; set KEEP_ACCOUNTS in .env")
        return 1

    problems = 0
    for email in emails:
        keep = open_keep(email)
        if keep is None:
            problems += 1
            continue
        live = [n for n in keep.all() if not n.trashed]
        recent = sorted(live, key=lambda n: note_updated(n) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        log(f"{email}: signed in, {len(live)} live notes")
        for node in recent[:3]:
            when = note_updated(node)
            stamp = when.astimezone().strftime("%Y-%m-%d %H:%M") if when else "unknown"
            log(f"    most recent: {stamp}  {note_title(node)!r}")
        if not recent:
            log("    no live notes -- check this is the account you meant")

    notion_token = env("NOTION_TOKEN").strip()
    if not notion_token:
        log("NOTION_TOKEN missing from .env")
        return 1

    from notion_client import Client

    client = Client(auth=notion_token)
    try:
        source = client.data_sources.retrieve(data_source_id=DATA_SOURCE_ID)
        title = "".join(t.get("plain_text", "") for t in source.get("title", []))
        log(f"Notion: can reach the data source ({title or DATA_SOURCE_ID}) -- writes should work")
    except Exception as exc:
        problems += 1
        log(f"Notion: cannot reach the target -- {describe(exc)}")
        log(
            "    Open the Notes Inbox database in Notion -> ... -> Connections -> "
            "add the integration. Connecting its parent page is not enough."
        )

    log("preflight found no problems" if not problems else f"preflight found {problems} problem(s)")
    return 1 if problems else 0


def run(args) -> int:
    emails = accounts()
    if not emails:
        log("no accounts configured; set KEEP_ACCOUNTS in .env and run auth_setup.py")
        return 1

    client = None
    if not args.dry_run:
        from notion_client import Client

        notion_token = env("NOTION_TOKEN").strip()
        if not notion_token:
            log("NOTION_TOKEN missing from .env")
            return 1
        client = Client(auth=notion_token)

    state = load_state()
    budget = [args.limit] if args.limit is not None else None
    created = skipped = failed = 0

    for email in emails:
        got, skip, fail = sync_account(email, client, state, args, budget)
        created, skipped, failed = created + got, skipped + skip, failed + fail

    verb = "would create" if args.dry_run else "created"
    if created == 0 and skipped == 0 and failed == 0:
        log(f"no new notes across {len(emails)} account(s)")
    else:
        log(
            f"run finished across {len(emails)} account(s): {created} {verb}, "
            f"{skipped} edited-skipped, {failed} failed"
        )
    return 1 if failed else 0


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Capture Google Keep notes into the Notes Inbox.")
    parser.add_argument(
        "--undo",
        action="store_true",
        help="move every row this bridge created to Notion's trash and forget "
        "them. Touches nothing else in the database.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="read-only preflight: which account each token opens, and whether "
        "Notion is reachable. Creates nothing.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would be created; contact Notion not at all",
    )
    parser.add_argument("--limit", type=int, help="create at most N rows this run, then stop")
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--since",
        metavar="YYYY-MM-DD",
        help="notes edited on or after this date, instead of the rolling window",
    )
    group.add_argument(
        "--all", action="store_true", help="every note, however old -- imports the backlog"
    )
    parser.add_argument(
        "--window-hours",
        type=float,
        default=float(env("KEEP_WINDOW_HOURS") or DEFAULT_WINDOW_HOURS),
        help=f"how far back the default window reaches (default {DEFAULT_WINDOW_HOURS}h)",
    )
    args = parser.parse_args(argv)

    if args.all:
        args.since = None
    elif args.since:
        try:
            args.since = datetime.strptime(args.since, "%Y-%m-%d").replace(
                tzinfo=datetime.now().astimezone().tzinfo
            )
        except ValueError:
            parser.error(f"--since needs YYYY-MM-DD, got {args.since!r}")
    else:
        # The default: today's notes, as a rolling window so nothing is lost
        # at midnight between one 15-minute run and the next.
        args.since = datetime.now(timezone.utc) - timedelta(hours=args.window_hours)
    return args


def main() -> int:
    args = parse_args()
    if args.undo:
        return undo()
    if args.check:
        return check()
    LOCK_PATH.touch(exist_ok=True)
    handle = os.open(LOCK_PATH, os.O_RDWR)
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            # launchd fired again while the previous run is still going.
            return 0
        return run(args)
    finally:
        os.close(handle)


if __name__ == "__main__":
    sys.exit(main())
