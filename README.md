# Capture bridge → Notion

The capture leg of the daily OS. Notes from **Google Keep** and **Apple Notes**
land automatically as rows in the **📥 Notes Inbox** Notion database, no taps.
A separate system sweeps that Inbox and files them; this bridge does not.

One way, into Notion. This code never modifies a note at either source.

Rows carry `Source = Keep` or `Source = Apple Notes`. Run all sources (the
default) or one at a time with `--source keep` / `--source applenotes`.

## Apple Notes

Apple ships no API, but Notes.app is scriptable, so `applenotes_probe.js` reads
it through `osascript` and hands back JSON. Notes in *Recently Deleted* are
skipped twice over — once in the script and once in Python, because the folder
name is localized on a non-English Mac and a deleted note reaching the Inbox is
worse than one missed. Add more exclusions with `APPLENOTES_SKIP_FOLDERS` in
`.env` (comma-separated folder names).

**The automation permission is the thing that breaks this.** The first time
anything scripts Notes.app, macOS asks for approval. Under launchd there is
nobody to click it and the call fails with error `-1743`. So grant it by hand
first:

```sh
python sync.py --source applenotes --dry-run
```

Approve the prompt, then confirm it stuck under System Settings → Privacy &
Security → Automation. Scripting Notes.app also launches it if it is not
already running.

## Files

| file | what it does |
| --- | --- |
| `auth_setup.py` | one-time interactive Google auth; mints and verifies the master token |
| `sync.py` | the scheduled run: every non-trashed Keep note becomes one Inbox row, once |
| `applenotes_probe.js` | reads Apple Notes via Notes.app scripting; prints JSON |
| `com.michael.keepbridge.plist` | LaunchAgent that runs `sync.py` every 15 minutes |

Three files, no framework. Each script reads `.env` itself rather than pulling
in `python-dotenv`.

## Setup

`.env` holds everything and is never committed:

```
KEEP_ACCOUNTS=a@gmail.com,b@icloud.com   # every Keep account to sync
KEEP_TOKEN_A_GMAIL_COM=                  # auth_setup.py fills these in,
KEEP_DEVICE_ID_A_GMAIL_COM=              #   one pair per account
NOTION_TOKEN=...                         # integration "google keep bridge"
KEEP_WINDOW_HOURS=24                     # optional: how far back a run looks
KEEP_LABEL=                              # optional: restrict to one Keep label
```

A single-account `.env` using `EMAIL` / `GOOGLE_KEEP_TOKEN` / `KEEP_DEVICE_ID`
still works unchanged — that account is treated as the first entry and its
existing token is reused, so there is no need to re-authenticate it.

### 1. Authenticate (once per account)

```sh
python auth_setup.py     # run again for each additional account
```

It lists the accounts already in `.env`, asks which one you are setting up,
and stores that account's token and device id under its own keys. Each account
needs its own browser sign-in — **sign in to the account you typed**, not
whichever Google account the browser already has open.

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

### 2. Preflight

```sh
python sync.py --check
```

Read-only, creates nothing. For each account it reports how many live notes
the token actually opens and the three most recently edited titles — the way
to confirm a token belongs to the account you think it does, since the browser
sign-in is easy to do against the wrong Google account. It then checks Notion
is reachable.

If Notion reports it cannot find the database, the integration has not been
shared with it: open **📥 Notes Inbox** in Notion → `•••` → **Connections** →
add the integration. Connecting its parent page is *not* enough.

### 3. First sync

```sh
python sync.py --dry-run   # see what would be created, contact Notion not at all
python sync.py             # create the rows
python sync.py             # run again: creates zero duplicates
```

**A run that would create more than 25 rows stops and asks.** It prints the
count and writes nothing; look with `--dry-run`, then `--yes` to proceed.

**Pulling an update:** `git checkout <ref> -- sync.py` reads your *local* copy
of that ref, which only moves when you fetch. Always fetch first, or you will
silently re-check-out an old file:

```sh
git fetch bridge claude/google-keep-bridge-xhc8u3
git checkout bridge/claude/google-keep-bridge-xhc8u3 -- sync.py auth_setup.py README.md
```

**Today's notes only, no backlog.** A plain run considers notes edited in the
last 24 hours across every configured account. It is a rolling window rather
than "since midnight" on purpose: a note written at 23:58 would otherwise fall
outside the window by the time the next 15-minute run fires, and never be
captured. Duplicates are impossible regardless, because state is keyed by
account and Keep note id.

Older notes stay in Keep. `--since YYYY-MM-DD` reaches further back and
`--all` takes everything; `--limit N` caps a run. launchd runs `sync.py` with
no arguments, so the schedule always gets the rolling window.

**Why no backlog by default:** `Captured` in the Notes Inbox is a Notion
`created_time` — it records when the *row* was made, not when the note was
written, and nothing can set it. Importing a year of old notes would stamp
them all as captured today, and the sweep files by that.

If a run fails the same way three times running, it stops and says so rather
than repeating one systematic fault once per note.

Dedupe is by Keep note id in `.sync_state.json`, written after **each** row, so
an interrupted run never re-creates what it just made. A note edited in Keep
after it synced is logged and skipped — a documented v1 limitation.

### 4. Schedule

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
