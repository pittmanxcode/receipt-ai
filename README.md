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

Set the account and a master token in the environment. The token is read from
the environment and never written to disk.

```sh
export KEEP_EMAIL='you@gmail.com'
export KEEP_MASTER_TOKEN='aas_et/...'
```

To mint a master token, exchange an OAuth token for one with `gpsoauth`
(installed as a `gkeepapi` dependency). The usual route is to sign in on a
browser to Google's embedded setup endpoint, copy the resulting `oauth_token`,
and exchange it:

```python
import gpsoauth
print(gpsoauth.exchange_token('you@gmail.com', 'oauth2rt_...', 'any-device-id')['Token'])
```

A master token is **long-lived and account-wide** — treat it like a password.
Revoke it from your Google account's device list when you are done.

## Use

```sh
keep-bridge add "Blue Bottle" '$8.75' --date 3/2/2026 --category coffee --tag work
keep-bridge sync --dry-run -v     # show what would move, write nothing
keep-bridge sync                  # do it
keep-bridge status
```

`keep-bridge sync --offline --dry-run` runs the whole engine against an empty
in-memory Keep — useful for validating local records with no credentials.

Only notes carrying the `receipts` label are touched; nothing else in your Keep
account is read or written. Change it with `--label`.

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
three policies, trashing both ways, purged notes, a lost or corrupt ledger, and
dry runs.
