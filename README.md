# Keep → Notion capture bridge

The capture leg of the daily OS. Notes spoken or typed into Google Keep land
automatically as rows in the **📥 Notes Inbox** Notion database, with no taps.
A separate system sweeps that Inbox and files them; this bridge does not.

One way, Keep → Notion. This code never modifies a Keep note.

## Files

| file | what it does |
| --- | --- |
| `auth_setup.py` | one-time interactive Google auth; mints and verifies the master token |
| `sync.py` | the scheduled run: every non-trashed Keep note becomes one Inbox row, once |
| `com.michael.keepbridge.plist` | LaunchAgent that runs `sync.py` every 15 minutes |

Three files, no framework. Each script reads `.env` itself rather than pulling
in `python-dotenv`.

## Setup

`.env` holds everything and is never committed:

```
EMAIL=...                      # the Google account
NOTION_TOKEN=...               # integration "google keep bridge"
NOTION_PARENT_PAGE_ID=3cb540deb7fe81d2a25eebd078e5360f
GOOGLE_KEEP_TOKEN=             # auth_setup.py fills this in
KEEP_DEVICE_ID=                # auth_setup.py fills this in
KEEP_LABEL=                    # leave empty: every non-trashed note is captured
```

### 1. Authenticate (once)

```sh
python3 auth_setup.py
```

It walks you through getting an `oauth_token` cookie from
<https://accounts.google.com/EmbeddedSetup>, exchanges it for a master token,
**verifies it by signing in to Keep**, then writes it to `.env` (mode `600`).

The single most common failure is a spent code. The browser code is
**single-use and expires in minutes** — use a fresh incognito window and come
straight back. `Authentication error: Unknown` means the code was already used
or had expired, not that anything is misconfigured. The script says exactly
what to redo on every failure path.

`KEEP_DEVICE_ID` is generated once and must never change: Google ties the
master token to it.

### 2. First sync

```sh
python3 sync.py     # run 1: creates rows
python3 sync.py     # run 2: creates zero duplicates
```

Dedupe is by Keep note id in `.sync_state.json`, written after **each** row, so
an interrupted run never re-creates what it just made. A note edited in Keep
after it synced is logged and skipped — a documented v1 limitation.

### 3. Schedule

```sh
sed -e "s|__PROJECT_DIR__|$PWD|g" \
    -e "s|__VENV_PYTHON__|$PWD/.venv/bin/python3|g" \
    com.michael.keepbridge.plist > ~/Library/LaunchAgents/com.michael.keepbridge.plist

launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.michael.keepbridge.plist
launchctl kickstart -p gui/$UID/com.michael.keepbridge   # fire one now
```

Verify — two timestamped entries 15 minutes apart:

```sh
launchctl print gui/$UID/com.michael.keepbridge | head -20
tail -f sync.log
```

To pause and resume:

```sh
launchctl bootout gui/$UID/com.michael.keepbridge     # pause
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.michael.keepbridge.plist
```

## Scope and safety

- **Every non-trashed Keep note is captured.** `KEEP_LABEL` can narrow a first
  run to one label, but empty is the intended setting: failing to capture is
  the failure mode that matters.
- **Writes touch only** data source `0cb32494-0d57-4039-bb1d-1a6c5ed66fc1`
  (📥 Notes Inbox). No other Notion page is ever written.
- **Keep is read-only.** No attribute is set on a note and nothing is pushed back.
- `Section`, `Actionable` and `Confidence` are left empty for the OS to fill.
- Credentials live only in `.env`. Nothing prints or logs a token; `.env`,
  `.sync_state.json` and the logs are gitignored.
- Overlapping runs are prevented by a lock, so a slow run cannot race the next
  15-minute firing.

## Notes on the Notion API

`notion_client` 3.1.0 pins Notion-Version `2025-09-03`, where a row's parent is
a **data source**, not a database — hence `data_source_id` above. `sync.py`
falls back to `database_id` if it ever runs against an older API version.
