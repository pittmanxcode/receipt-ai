#!/usr/bin/env python3
"""Keep -> Notion capture bridge (one-way, v1).

Reads Google Keep and creates one row per note in the Notes Inbox database.
Runs unattended every 15 minutes under launchd.

Read-only against Keep: this script never sets an attribute on a note and
never calls anything that would push a change back. Keep is the source of
truth for capture; Notion is where the OS picks the note up.

Each Keep note is written exactly once, tracked by Keep note id in
.sync_state.json. A note edited after it synced is logged and skipped -- a
documented v1 limitation, not an oversight.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENV_PATH = HERE / ".env"
STATE_PATH = HERE / ".sync_state.json"
LOG_PATH = HERE / "sync.log"
LOCK_PATH = HERE / ".sync.lock"


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

# The one database this bridge is allowed to write to. Nothing else in the
# workspace is ever touched.
DATA_SOURCE_ID = "0cb32494-0d57-4039-bb1d-1a6c5ed66fc1"
DATABASE_ID = "1eb54765d75c46cd8075a0c03f85b9a3"

TITLE_LIMIT = 200        # keep inbox rows scannable
BLOCK_TEXT_LIMIT = 1900  # Notion caps a rich_text run at 2000
BLOCKS_PER_REQUEST = 100


def log(message: str) -> None:
    stamp = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    line = f"{stamp}  {message}"
    print(line)
    with LOG_PATH.open("a") as fh:
        fh.write(line + "\n")


# -- state ----------------------------------------------------------------


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"version": 1, "notes": {}}
    try:
        data = json.loads(STATE_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        # A damaged state file must not cause a re-import of everything.
        log("state file unreadable; stopping so nothing is duplicated")
        raise SystemExit(1)
    data.setdefault("notes", {})
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


def open_keep():
    import gkeepapi

    email = env("EMAIL").strip()
    token = env("GOOGLE_KEEP_TOKEN").strip()
    android_id = env("KEEP_DEVICE_ID").strip() or None

    if not email or not token:
        log("EMAIL or GOOGLE_KEEP_TOKEN missing from .env; run auth_setup.py")
        raise SystemExit(1)

    keep = gkeepapi.Keep()
    try:
        keep.authenticate(email, token, device_id=android_id, sync=True)
    except Exception as exc:
        log(f"Keep sign-in was declined ({type(exc).__name__}); run auth_setup.py for a new token")
        raise SystemExit(1)
    return keep


def note_title(node) -> str:
    title = (node.title or "").strip()
    if not title:
        for line in (node.text or "").splitlines():
            if line.strip():
                title = line.strip()
                break
    if not title:
        when = getattr(node.timestamps, "created", None) or datetime.now(timezone.utc)
        return f"Keep note {when:%Y-%m-%d %H:%M}"
    return title[:TITLE_LIMIT]


def note_updated(node):
    stamp = getattr(node.timestamps, "updated", None)
    if stamp is None:
        return None
    if stamp.tzinfo is None:
        return stamp.replace(tzinfo=timezone.utc)
    return stamp


def selected_notes(keep, since=None) -> list:
    """Non-trashed notes, optionally narrowed to one Keep label and a date.

    KEEP_LABEL and --since are the staging valves. Neither is set in normal
    operation: full capture is the point. They exist so a first run against a
    large back catalogue can be taken in bounded steps.
    """
    label_name = env("KEEP_LABEL").strip()
    label = keep.findLabel(label_name) if label_name else None
    if label_name and label is None:
        log(f"KEEP_LABEL {label_name!r} does not exist in Keep; nothing to do")
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


def create_row(client, node) -> str:
    blocks = text_blocks(node.text)
    properties = {
        "Note": {"title": [{"type": "text", "text": {"content": note_title(node)}}]},
        "Source": {"select": {"name": "Keep"}},
        "Status": {"select": {"name": "Unfiled"}},
        # Section, Actionable and Confidence are left empty on purpose --
        # the cataloging sweep is what fills those in.
    }

    # notion_client 3.x speaks Notion-Version 2025-09-03, where a row's parent
    # is a data source. Fall back to the database id for older API versions.
    try:
        page = client.pages.create(
            parent={"type": "data_source_id", "data_source_id": DATA_SOURCE_ID},
            properties=properties,
            children=blocks[:BLOCKS_PER_REQUEST],
        )
    except Exception as exc:
        if "data_source" not in str(exc):
            raise
        page = client.pages.create(
            parent={"database_id": DATABASE_ID},
            properties=properties,
            children=blocks[:BLOCKS_PER_REQUEST],
        )

    for start in range(BLOCKS_PER_REQUEST, len(blocks), BLOCKS_PER_REQUEST):
        client.blocks.children.append(
            block_id=page["id"], children=blocks[start : start + BLOCKS_PER_REQUEST]
        )
    return page["id"]


# -- run ------------------------------------------------------------------


def run(args) -> int:
    from notion_client import Client

    notion_token = env("NOTION_TOKEN").strip()
    if not notion_token:
        log("NOTION_TOKEN missing from .env")
        return 1

    state = load_state()
    synced = state["notes"]

    keep = open_keep()
    client = None if args.dry_run else Client(auth=notion_token)

    created = skipped_edited = failed = 0
    for node in selected_notes(keep, since=args.since):
        if args.limit is not None and created >= args.limit:
            log(f"reached the limit of {args.limit}; stopping this run")
            break

        updated = note_updated(node)
        updated_iso = updated.isoformat() if updated else ""

        record = synced.get(node.id)
        if record:
            if updated_iso and record.get("keep_updated") and updated_iso > record["keep_updated"]:
                log(f"edited in Keep after it synced, skipped (v1 limitation): {note_title(node)!r}")
                skipped_edited += 1
            continue

        if args.dry_run:
            log(f"would create row for {note_title(node)!r}")
            created += 1
            continue

        try:
            page_id = create_row(client, node)
        except Exception as exc:
            log(f"could not create a row for {note_title(node)!r}: {type(exc).__name__}: {exc}")
            failed += 1
            continue

        synced[node.id] = {
            "notion_page_id": page_id,
            "keep_updated": updated_iso,
            "synced_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        # Saved per note: an interrupted run never re-creates what it just made.
        save_state(state)
        created += 1
        log(f"created row for {note_title(node)!r}")

    verb = "would create" if args.dry_run else "created"
    if created == 0 and skipped_edited == 0 and failed == 0:
        log("no new notes")
    else:
        log(f"run finished: {created} {verb}, {skipped_edited} edited-skipped, {failed} failed")
    return 1 if failed else 0


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Capture Google Keep notes into the Notes Inbox.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would be created; write nothing to Notion or to state",
    )
    parser.add_argument(
        "--limit", type=int, help="create at most N rows this run, then stop"
    )
    parser.add_argument(
        "--since",
        metavar="YYYY-MM-DD",
        help="only notes edited in Keep on or after this date",
    )
    args = parser.parse_args(argv)
    if args.since:
        try:
            args.since = datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            parser.error(f"--since needs YYYY-MM-DD, got {args.since!r}")
    return args


def main() -> int:
    args = parse_args()
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
