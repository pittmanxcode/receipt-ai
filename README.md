# receipts

Receipt records kept as JSON in this repo, two-way synced with Google Keep so
they can be captured from a phone and reconciled from a laptop.

## Why a "bridge" and not just an API client

Google publishes **no consumer API for Keep**. The official
`keep.googleapis.com` API is Workspace-enterprise only, and even there it can
only see notes its own service account created or that were explicitly shared
with it — it cannot read the notes in a personal account.

So this bridge drives [`gkeepapi`](https://github.com/kiwiz/gkeepapi), which
speaks the Keep app's private protocol. That comes with real caveats:

- It is **unofficial and reverse-engineered**. Google can change the protocol
  and break it without notice.
- It authenticates with a **master token**, not a password (see below).
- It is not a supported integration; use it on an account you are willing to
  have locked out of, and keep this repo as the durable copy of your data.

Everything above `keep_bridge/backends/` is independent of `gkeepapi`, so the
sync engine is fully tested against an in-memory fake and does not need
credentials or a network to verify.

## Install

```sh
pip install -e '.[dev]'
```

## Authenticating

Keep has no password or OAuth-app login here — `gkeepapi` authenticates with a
**master token**, which is long-lived and grants full account access. Treat it
exactly like a password.

Minting one is a browser flow, because `gpsoauth.perform_master_login()` with a
password or app password now generally returns `BadAuthentication`:

1. Open <https://accounts.google.com/EmbeddedSetup> in a browser and sign in
   fully with the account you want to sync.
2. Accept the agreement prompt. The page may then appear to load forever —
   that is expected; carry on.
3. In devtools, copy the value of the **`oauth_token` cookie**. It starts with
   `oauth2_4/` or `oauth2_1/`.
4. Exchange it for a master token:

   ```python
   import gpsoauth
   print(gpsoauth.exchange_token("you@gmail.com", "oauth2_4/...", "0123456789abcdef")["Token"])
   ```

   The third argument is an arbitrary but **stable** 16-hex-character device id
   — reuse the same one, since Google ties the token to it.

The result starts with `aas_et/`. Put it in the environment; nothing writes it
to disk:

```sh
export KEEP_EMAIL='you@gmail.com'
export KEEP_MASTER_TOKEN='aas_et/...'
```

The token does not expire on a schedule, but it is revoked by a password
change, by signing the "device" out from your Google account's device list, and
sometimes by Google on its own. When that happens `keep-bridge` fails with a
message telling you to mint a new one rather than retrying a dead token.

## Going to production

Run these in order the first time against a real account. The first two write
nothing at all.

```sh
keep-bridge check                 # 1. read-only: proves auth, shows blast radius
keep-bridge sync --dry-run -v     # 2. exactly what would move, still no writes
keep-bridge sync                  # 3. for real
```

`check` reports how many notes under the label are already linked, how many
would be imported, and how many are **not receipt-shaped** and will be left
untouched. Read that last number before step 3.

Safety properties worth knowing:

- **A labelled note that shows no receipt fields is never touched.** Importing
  it would rewrite someone's text into a receipt template. `--adopt-unrecognized`
  overrides this; its text is preserved in the `Notes:` section either way.
- **Nothing that cannot be parsed is discarded.** Unrecognised lines land in
  `Notes:`, so a round-trip through Keep never erases text.
- **Writes are crash-safe.** The ledger and every receipt file are written to a
  temp file and renamed, so an interrupted run cannot truncate the merge base.
- **One run at a time.** A lock file in the state dir makes a second concurrent
  run exit `3` rather than racing the first.
- **Transient failures retry** with exponential backoff; a rejected token fails
  immediately with instructions instead.

Exit codes: `0` clean, `1` unresolved conflicts, `2` Keep/credential error,
`3` another run holds the lock.

For a cron job, `--conflict local` or `--conflict remote` avoids a job that
exits `1` and waits for a human. Prefer `manual` for anything interactive.

## How the sync works

Each run reads both sides, three-way merges every pair in memory, applies the
writes, and saves the ledger last — so an interrupted run re-syncs cleanly
rather than recording agreement that never happened.

The merge base is `.keep-bridge/sync-state.json`: what the two sides last
agreed on. Comparing against a base is what makes this a real two-way sync
instead of last-writer-wins — a field only changes on a side when the *other*
side is the only one that touched it. Edits to different fields of the same
receipt, made on both sides between runs, all survive.

**Conflicts** — both sides changed the same field to different values:

| `--conflict` | behaviour |
| --- | --- |
| `manual` (default) | each side keeps its own value, the conflict is reported, and `sync` exits `1`. Nothing is overwritten and it is re-reported until resolved. |
| `local` | the repo wins |
| `remote` | Keep wins |

Tags are the exception: they set-merge, so additions from both sides stick and
a removal on either side wins. Two people adding tags is not a collision.

**Deletion** — trashing a receipt trashes its note, and trashing a note in Keep
marks the receipt `trashed`. Trash is recoverable on both sides, so nothing is
destroyed. A note that *vanishes* from Keep entirely (Keep empties its own
trash after about a week, or the label was removed) is **rebuilt** rather than
treated as a delete: letting an automatic purge erase committed receipt data
would be data loss nobody asked for. A deliberate delete arrives as a trashed
note, which is seen and propagated before the purge.

**Lost ledger** — every note carries its receipt id in its body
(`[receipt:…]`). If the ledger is deleted or corrupted, the next run re-links
notes to records instead of duplicating them.

## The note format

The note body is canonical and is meant to be hand-edited in the Keep app; the
title is derived on every push and ignored on pull. Parsing is forgiving —
`merchant:`/`vendor:`, `amount:`/`total:`, `8/14/2026` or `2026-08-14`,
`$1,234.50`, bullets with or without a dash. A field that cannot be parsed
falls back to its default rather than failing the run.

```
Vendor: Trader Joe's
Date: 2026-08-14
Total: 42.17
Currency: USD
Category: groceries
Payment: visa-1234
Tags: food, reimbursable

Items:
- 2 x Bananas — 3.98
- Oat milk — 4.49

Notes:
split with Dana

[receipt:65488fe85b2a44999e1d90fbc63d053f]
```

Notes that land under the `receipts` label are rewritten into this shape on
first import, which is also what gives them their `[receipt:…]` marker.

## Layout

```
keep_bridge/
  model.py       receipt + line items, parsing and normalization
  serialize.py   receipt <-> Keep note body
  merge.py       three-way field merge and conflict policy
  syncstate.py   the ledger (merge base)
  store.py       local JSON receipts, one file each
  sync.py        the engine
  atomic.py      crash-safe file writes
  lock.py        one run at a time
  cli.py         keep-bridge
  backends/
    base.py      the five-method surface the engine needs
    fake.py      in-memory Keep, for tests and --offline
    gkeep.py     the live gkeepapi backend
data/receipts/   one JSON file per receipt
```

## Tests

```sh
pytest
```

The whole engine is covered offline against the fake backend: creation in both
directions, clean pushes and pulls, simultaneous edits, conflicts under all
three policies, trashing both ways, purged notes, a lost or corrupt ledger,
dry runs, non-receipt notes under the label, crash-safe writes and the run
lock.

The live `gkeepapi` backend is the one part not covered — it needs real
credentials. `keep-bridge check` is how you exercise it safely.
