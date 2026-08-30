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
import html
import json
import os
import re
import subprocess
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

# Every capture source, and the Source value its rows carry in Notion.
SOURCE_LABELS = {"keep": "Keep", "applenotes": "Apple Notes", "drive": "Recorder"}
APPLENOTES_SCRIPT = "applenotes_probe.js"
# Folders never captured. The JXA filters these too; this is the second line
# of defence, since the folder name is localized on a non-English Mac and a
# deleted note reaching the Inbox is worse than one missed.
APPLENOTES_SKIP_FOLDERS = {"recently deleted", "deleted", "trash"}

# Recorder shares a transcript as a Google Doc named like "Aug 29 at 4:43 PM".
# The audio it exports separately uses a hyphen ("11-10 AM"), so accept both.
RECORDER_TITLE_RE = re.compile(
    r"^[A-Z][a-z]{2}\s+\d{1,2}\s+at\s+\d{1,2}[:\-]\d{2}\s*(AM|PM)$", re.I
)
DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive"]
DRIVE_CLIENT_SECRET = "credentials.json"
DRIVE_TOKEN = "drive_token.json"
CAPTURED_FOLDER = "Recorder Captured"

DEFAULT_WINDOW_HOURS = 24
TITLE_LIMIT = 200        # keep inbox rows scannable
BLOCK_TEXT_LIMIT = 1900  # Notion caps a rich_text run at 2000
BLOCKS_PER_REQUEST = 100
STOP_AFTER_FAILURES = 3  # a systematic fault should report once, not 342 times
BULK_CONFIRM = 25        # above this, a run stops and asks rather than flooding


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


class Item:
    """One capturable note, whatever produced it."""

    def __init__(self, source: str, uid: str, title: str, text: str, updated, origin: str = "", on_captured=None):
        self.source = source
        self.uid = uid          # already namespaced by source; the state key
        self.title = title
        self.text = text
        self.updated = updated
        self.origin = origin    # account or folder, for the log line
        self.on_captured = on_captured

    on_captured = None  # optional callable, run once the row exists

    @property
    def label(self) -> str:
        return SOURCE_LABELS[self.source]


def log(message: str) -> None:
    stamp = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    line = f"{stamp}  {message}"
    print(line)
    with LOG_PATH.open("a") as fh:
        fh.write(line + "\n")


# -- state ----------------------------------------------------------------


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"version": 3, "notes": {}}
    try:
        data = json.loads(STATE_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        # A damaged state file must not cause a re-import of everything.
        log("state file unreadable; stopping so nothing is duplicated")
        raise SystemExit(1)
    data.setdefault("notes", {})
    if data.get("version") == 1:
        # v1 keyed by bare Keep note id; v2 prefixed the account so two
        # accounts could not collide on an id.
        primary = (accounts() or [""])[0]
        data["notes"] = {f"{primary}::{k}": v for k, v in data["notes"].items()}
        data["version"] = 2
    if data.get("version") == 2:
        # v3 prefixes the source, now that Keep is not the only one.
        data["notes"] = {f"keep::{k}": v for k, v in data["notes"].items()}
        data["version"] = 3
    return data


def atomic_json(path: Path, payload) -> None:
    """Write JSON via temp-file-and-rename, 0600.

    Used for sync state and for the Drive token: both are things an
    interrupted write must not leave half-formed.
    """
    handle, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(handle, "w") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def save_state(state: dict) -> None:
    atomic_json(STATE_PATH, state)


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


def _keep_notes(keep, since) -> list:
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


def keep_items(since) -> tuple[list, int]:
    """Every Keep note newer than `since`, across all configured accounts."""
    items, problems = [], 0
    for email in accounts():
        keep = open_keep(email)
        if keep is None:
            problems += 1
            continue
        for node in _keep_notes(keep, since):
            items.append(
                Item(
                    source="keep",
                    uid=f"keep::{email}::{node.id}",
                    title=note_title(node),
                    text=node.text or "",
                    updated=note_updated(node),
                    origin=email,
                )
            )
    return items, problems


# -- Apple Notes ----------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")


def _plain(text: str) -> str:
    """Fall back to stripping HTML when `plaintext` was unavailable."""
    if "<" not in text:
        return text
    text = re.sub(r"<(br|/p|/div|/h[1-6])[^>]*>", "\n", text, flags=re.I)
    return html.unescape(_TAG_RE.sub("", text)).strip()


def applenotes_items(since) -> tuple[list, int]:
    """Apple Notes modified since `since`, read through Notes.app scripting.

    Apple exposes no file format worth parsing here, so this drives the app.
    The first run needs the macOS automation permission granted; under launchd
    there is nobody to click that prompt, so run it once by hand first.
    """
    script = HERE / APPLENOTES_SCRIPT
    if not script.exists():
        log(f"{script.name} is missing; cannot read Apple Notes")
        return [], 1

    hours = max((datetime.now(timezone.utc) - since).total_seconds() / 3600, 1) if since else 24 * 3650
    try:
        result = subprocess.run(
            ["osascript", "-l", "JavaScript", str(script), str(hours)],
            capture_output=True,
            text=True,
            timeout=300,
        )
    except FileNotFoundError:
        log("osascript not found; Apple Notes capture only works on macOS")
        return [], 1
    except subprocess.TimeoutExpired:
        log("Apple Notes did not answer within 5 minutes")
        return [], 1

    if result.returncode != 0:
        detail = (result.stderr or "").strip()
        log(f"could not read Apple Notes: {detail[:300]}")
        if "-1743" in detail or "not authorized" in detail.lower():
            log(
                "    macOS has not granted automation access. Run this once from "
                "Terminal and approve the prompt:  python sync.py --source applenotes --dry-run"
            )
            log("    then check System Settings -> Privacy & Security -> Automation.")
        return [], 1

    try:
        rows = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        log("Apple Notes returned something that is not JSON; skipping this run")
        return [], 1

    items, skipped_folders = [], 0
    for row in rows:
        folder = (row.get("folder") or "").strip()
        extra = env("APPLENOTES_SKIP_FOLDERS")
        deny = APPLENOTES_SKIP_FOLDERS | {
            f.strip().lower() for f in extra.split(",") if f.strip()
        }
        if folder.lower() in deny:
            skipped_folders += 1
            continue
        text = _plain(row.get("text") or "")
        title = (row.get("name") or "").strip() or next(
            (line.strip() for line in text.splitlines() if line.strip()), ""
        )
        updated = None
        stamp = row.get("modified")
        if stamp:
            try:
                updated = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            except ValueError:
                updated = None
        if not title:
            when = updated or datetime.now(timezone.utc)
            title = f"Apple note {when.astimezone():%Y-%m-%d %H:%M}"
        items.append(
            Item(
                source="applenotes",
                uid=f"applenotes::{row.get('id')}",
                title=title[:TITLE_LIMIT],
                text=text,
                updated=updated,
                origin=folder,
            )
        )
    if skipped_folders:
        log(f"Apple Notes: skipped {skipped_folders} note(s) in excluded folders")
    return items, 0


# -- Recorder, via Drive --------------------------------------------------


def drive_service():
    """An authorised Drive client, or None with the reason logged.

    The consent flow opens a browser, which launchd cannot answer, so it only
    runs from an interactive terminal. Scheduled runs with no stored token
    report that and move on rather than stalling.
    """
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError:
        log("Drive libraries missing; run: pip install google-api-python-client google-auth-oauthlib")
        return None

    token_path = HERE / DRIVE_TOKEN
    secret_path = HERE / DRIVE_CLIENT_SECRET
    creds = None
    if token_path.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(token_path), DRIVE_SCOPES)
        except Exception:
            creds = None

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception as exc:
            log(f"Drive token could not be refreshed ({exc}); re-authorise interactively")
            creds = None

    if not creds or not creds.valid:
        if not sys.stdin.isatty():
            log("Drive is not authorised yet, and consent needs a browser.")
            log("    Run once from Terminal:  python sync.py --source drive --dry-run")
            return None
        if not secret_path.exists():
            log(f"{DRIVE_CLIENT_SECRET} is missing -- see README: authorising Drive")
            return None
        flow = InstalledAppFlow.from_client_secrets_file(str(secret_path), DRIVE_SCOPES)
        creds = flow.run_local_server(port=0)
        atomic_json(token_path, json.loads(creds.to_json()))
        log(f"Drive authorised; token saved to {DRIVE_TOKEN}")

    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _captured_folder(service) -> str | None:
    """The folder finished transcripts are moved into, created on first use."""
    safe = CAPTURED_FOLDER.replace("'", "\\'")
    query = (
        "mimeType='application/vnd.google-apps.folder' and trashed=false "
        f"and name='{safe}'"
    )
    try:
        found = service.files().list(q=query, fields="files(id)", pageSize=1).execute()
        if found.get("files"):
            return found["files"][0]["id"]
        created = service.files().create(
            body={"name": CAPTURED_FOLDER, "mimeType": "application/vnd.google-apps.folder"},
            fields="id",
        ).execute()
        return created["id"]
    except Exception as exc:
        log(f"could not prepare the {CAPTURED_FOLDER!r} folder: {exc}")
        return None


def drive_items(since) -> tuple[list, int]:
    """Recorder transcripts shared into Drive as Google Docs."""
    service = drive_service()
    if service is None:
        return [], 1

    query = [
        "mimeType='application/vnd.google-apps.document'",
        "trashed=false",
        "'root' in parents",
    ]
    if since:
        query.append(f"modifiedTime > '{since.astimezone(timezone.utc):%Y-%m-%dT%H:%M:%SZ}'")

    try:
        listed = service.files().list(
            q=" and ".join(query),
            fields="files(id,name,modifiedTime)",
            pageSize=100,
            orderBy="modifiedTime desc",
        ).execute()
    except Exception as exc:
        log(f"could not list Drive: {describe(exc)}")
        return [], 1

    folder_id = None
    items = []
    for row in listed.get("files", []):
        name = row.get("name", "")
        # Only files shaped like a Recorder export. Everything else in root is
        # someone's actual document and none of this bridge's business.
        if not RECORDER_TITLE_RE.match(name.strip()):
            continue
        try:
            text = service.files().export(fileId=row["id"], mimeType="text/plain").execute()
        except Exception as exc:
            log(f"could not read {name!r} from Drive: {describe(exc)}")
            continue
        if isinstance(text, bytes):
            text = text.decode("utf-8", "replace")

        updated = None
        try:
            updated = datetime.fromisoformat(row["modifiedTime"].replace("Z", "+00:00"))
        except (KeyError, ValueError):
            pass

        if folder_id is None:
            folder_id = _captured_folder(service) or ""

        file_id = row["id"]

        def file_away(fid=file_id, label=name):
            if not folder_id:
                return
            try:
                service.files().update(
                    fileId=fid, addParents=folder_id, removeParents="root", fields="id"
                ).execute()
            except Exception as exc:
                # The row exists either way; tidying is best effort.
                log(f"captured {label!r} but could not move it: {exc}")

        items.append(
            Item(
                source="drive",
                uid=f"drive::{file_id}",
                title=name.strip()[:TITLE_LIMIT],
                text=(text or "").strip(),
                updated=updated,
                origin="Drive",
                on_captured=file_away,
            )
        )
    return items, 0


def gather(sources, since) -> tuple[list, int]:
    items, problems = [], 0
    for name in sources:
        reader = {"keep": keep_items, "applenotes": applenotes_items, "drive": drive_items}[name]
        got, bad = reader(since)
        items += got
        problems += bad
    return items, problems


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


def create_row(client, item) -> str:
    blocks = text_blocks(item.text)
    properties = {
        "Note": {"title": [{"type": "text", "text": {"content": item.title}}]},
        "Source": {"select": {"name": item.label}},
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


def sync_items(items, client, state, args, budget) -> tuple[int, int, int]:
    synced = state["notes"]
    created = skipped_edited = failed = 0
    consecutive = 0

    for item in items:
        if budget is not None and budget[0] <= 0:
            break

        where = f"{item.source}/{item.origin}" if item.origin else item.source
        updated_iso = item.updated.isoformat() if item.updated else ""

        record = synced.get(item.uid)
        if record:
            if updated_iso and record.get("updated") and updated_iso > record["updated"]:
                log(f"{where}: edited after it synced, skipped (v1 limitation): {item.title!r}")
                skipped_edited += 1
            continue

        if args.dry_run:
            log(f"{where}: would create row for {item.title!r}")
            created += 1
            if budget is not None:
                budget[0] -= 1
            continue

        try:
            page_id = create_row(client, item)
        except Exception as exc:
            failed += 1
            consecutive += 1
            log(f"{where}: could not create a row for {item.title!r}: {describe(exc)}")
            if consecutive >= STOP_AFTER_FAILURES:
                log(
                    f"stopping after {consecutive} failures in a row -- this is a "
                    "systematic fault, not one awkward note. Fix it and run again."
                )
                break
            continue

        consecutive = 0
        synced[item.uid] = {
            "notion_page_id": page_id,
            "updated": updated_iso,
            "synced_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        # Saved per item: an interrupted run never re-creates what it just made.
        save_state(state)
        created += 1
        if budget is not None:
            budget[0] -= 1
        log(f"{where}: created row for {item.title!r}")
        # Only after the row exists and is recorded -- tidying must never run
        # for something that failed to land in Notion.
        if item.on_captured:
            item.on_captured()

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


def check(args) -> int:
    """Read-only preflight across every source, plus Notion. Creates nothing."""
    problems = 0

    if "keep" in args.sources:
        emails = accounts()
        if not emails:
            log("no Keep accounts configured; set KEEP_ACCOUNTS in .env")
            problems += 1
        for email in emails:
            keep = open_keep(email)
            if keep is None:
                problems += 1
                continue
            live = [n for n in keep.all() if not n.trashed]
            recent = sorted(
                live,
                key=lambda n: note_updated(n) or datetime.min.replace(tzinfo=timezone.utc),
                reverse=True,
            )
            log(f"{email}: signed in, {len(live)} live notes")
            for node in recent[:3]:
                when = note_updated(node)
                stamp = when.astimezone().strftime("%Y-%m-%d %H:%M") if when else "unknown"
                log(f"    most recent: {stamp}  {note_title(node)!r}")

    if "applenotes" in args.sources:
        items, bad = applenotes_items(args.since)
        problems += bad
        if not bad:
            folders = sorted({i.origin for i in items if i.origin})
            log(f"Apple Notes: readable, {len(items)} in the window")
            if folders:
                log(f"    folders seen: {', '.join(folders[:8])}")
            for item in sorted(
                items, key=lambda i: i.updated or datetime.min.replace(tzinfo=timezone.utc),
                reverse=True,
            )[:3]:
                stamp = item.updated.astimezone().strftime("%Y-%m-%d %H:%M") if item.updated else "unknown"
                log(f"    most recent: {stamp}  {item.title!r}")

    if "drive" in args.sources:
        items, bad = drive_items(args.since)
        problems += bad
        if not bad:
            log(f"Drive: authorised, {len(items)} Recorder transcript(s) in the window")
            for item in items[:3]:
                log(f"    {item.title!r}")

    notion_token = env("NOTION_TOKEN").strip()
    if not notion_token:
        log("NOTION_TOKEN missing from .env")
        return 1

    from notion_client import Client

    try:
        source = Client(auth=notion_token).data_sources.retrieve(data_source_id=DATA_SOURCE_ID)
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
    state = load_state()
    budget = [args.limit] if args.limit is not None else None

    # Read every source before writing anything, so the size of the run is
    # known while it can still be stopped.
    items, failed = gather(args.sources, args.since)
    pending = sum(1 for i in items if i.uid not in state["notes"])

    if not args.dry_run and not args.yes and pending > BULK_CONFIRM:
        log(f"this run would create {pending} rows, over the safety limit of {BULK_CONFIRM}")
        log("  look at them first:  python sync.py --dry-run")
        log("  go ahead anyway:     python sync.py --yes")
        log("nothing was written")
        return 1

    client = None
    if not args.dry_run:
        from notion_client import Client

        notion_token = env("NOTION_TOKEN").strip()
        if not notion_token:
            log("NOTION_TOKEN missing from .env")
            return 1
        client = Client(auth=notion_token)

    created, skipped, more_failed = sync_items(items, client, state, args, budget)
    failed += more_failed

    verb = "would create" if args.dry_run else "created"
    where = "+".join(args.sources)
    if created == 0 and skipped == 0 and failed == 0:
        log(f"no new notes ({where})")
    else:
        log(
            f"run finished ({where}): {created} {verb}, "
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
    parser.add_argument(
        "--source",
        action="append",
        choices=sorted(SOURCE_LABELS),
        help="capture from just this source; repeatable. Default: all of them.",
    )
    parser.add_argument("--limit", type=int, help="create at most N rows this run, then stop")
    parser.add_argument(
        "--yes",
        action="store_true",
        help=f"go ahead even when more than {BULK_CONFIRM} rows would be created",
    )
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
    args.sources = args.source or sorted(SOURCE_LABELS)

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
        return check(args)
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
