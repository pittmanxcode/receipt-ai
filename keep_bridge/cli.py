"""Command line entry point: `keep-bridge`."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .backends.base import KeepBackend
from .backends.fake import FakeKeepBackend
from .backends.gkeep import EMAIL_ENV, TOKEN_ENV, GKeepBackend, KeepAuthError
from .merge import ConflictPolicy
from .model import Receipt
from .store import ReceiptStore
from .sync import DEFAULT_LABEL, SyncEngine, SyncReport
from .syncstate import SyncState

DEFAULT_DATA_DIR = Path("data/receipts")
DEFAULT_STATE_DIR = Path(".keep-bridge")


def _paths(args) -> tuple[ReceiptStore, SyncState, Path]:
    data_dir = Path(args.data_dir)
    state_dir = Path(args.state_dir)
    return (
        ReceiptStore(data_dir),
        SyncState(state_dir / "sync-state.json"),
        state_dir / "keep-session.json",
    )


def _backend(args, session_path: Path) -> KeepBackend:
    if args.offline:
        # A no-op Keep: lets `sync --offline --dry-run` exercise the engine and
        # validate local records without credentials or a network call.
        return FakeKeepBackend()
    return GKeepBackend(email=args.email, state_path=session_path)


def _print_report(report: SyncReport, verbose: bool) -> None:
    print(report.summary())
    if verbose:
        for outcome in report.outcomes:
            if outcome.action != "unchanged" or outcome.detail:
                detail = f" ({outcome.detail})" if outcome.detail else ""
                print(f"  {outcome.action:<16} {outcome.receipt_id}{detail}")
    for outcome in report.conflicts:
        print(f"  conflict {outcome.receipt_id}:", file=sys.stderr)
        for conflict in outcome.conflicts:
            print(f"    {conflict.describe()}", file=sys.stderr)


def cmd_sync(args) -> int:
    store, state, session_path = _paths(args)
    engine = SyncEngine(
        store,
        state,
        _backend(args, session_path),
        label=args.label,
        policy=ConflictPolicy(args.conflict),
    )
    try:
        report = engine.run(dry_run=args.dry_run)
    except KeepAuthError as exc:
        print(f"keep-bridge: {exc}", file=sys.stderr)
        return 2
    _print_report(report, args.verbose)
    # Unresolved conflicts are a non-zero exit so a cron job or CI step
    # surfaces them instead of reporting a clean run.
    return 1 if report.conflicts and args.conflict == ConflictPolicy.MANUAL.value else 0


def cmd_status(args) -> int:
    store, state, _ = _paths(args)
    receipts = store.load_all()
    linked = sum(1 for rid in receipts if state.by_receipt(rid))
    trashed = sum(1 for r in receipts.values() if r.trashed)
    print(f"receipts: {len(receipts)} ({linked} linked to Keep, {trashed} trashed)")
    print(f"ledger:   {state.path}")
    unlinked = [rid for rid in receipts if not state.by_receipt(rid)]
    for receipt_id in unlinked[:20]:
        print(f"  not yet synced: {receipt_id}")
    if len(unlinked) > 20:
        print(f"  ... and {len(unlinked) - 20} more")
    return 0


def cmd_add(args) -> int:
    """Create a receipt locally; the next sync pushes it to Keep."""
    store, _, _ = _paths(args)
    receipt = Receipt.from_dict(
        {
            "vendor": args.vendor,
            "date": args.date or "",
            "total": args.total,
            "currency": args.currency,
            "category": args.category or "",
            "payment_method": args.payment or "",
            "tags": args.tag,
            "notes": args.notes or "",
        }
    )
    path = store.save(receipt)
    print(f"{receipt.id}  {path}")
    return 0


def cmd_show(args) -> int:
    store, _, _ = _paths(args)
    receipts = store.load_all()
    receipt = receipts.get(args.receipt_id)
    if receipt is None:
        print(f"keep-bridge: no receipt {args.receipt_id}", file=sys.stderr)
        return 2
    print(json.dumps(receipt.to_dict(), indent=2, sort_keys=True))
    return 0


def _common_parser(suppress: bool) -> argparse.ArgumentParser:
    """The flags valid both before and after the subcommand."""
    default = argparse.SUPPRESS

    def d(value):
        return default if suppress else value

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--data-dir", default=d(str(DEFAULT_DATA_DIR)))
    parser.add_argument("--state-dir", default=d(str(DEFAULT_STATE_DIR)))
    parser.add_argument("--label", default=d(DEFAULT_LABEL), help="Keep label to sync")
    parser.add_argument("--email", default=d(None), help=f"Google account (or ${EMAIL_ENV})")
    parser.add_argument(
        "--offline",
        action="store_true",
        default=d(False),
        help="run against an empty in-memory Keep; no credentials, no network",
    )
    parser.add_argument("-v", "--verbose", action="store_true", default=d(False))
    return parser


def build_parser() -> argparse.ArgumentParser:
    # Global flags are accepted on either side of the subcommand. They are
    # built twice rather than shared via a single `parents=` parser: argparse
    # shares action *objects* across parents, so set_defaults on one parser
    # would rewrite the subparsers' defaults and clobber a flag given before
    # the subcommand. The subparser copies default to SUPPRESS so that an
    # unused flag there leaves the top-level value alone.
    top = _common_parser(suppress=False)
    common = _common_parser(suppress=True)

    parser = argparse.ArgumentParser(
        prog="keep-bridge",
        parents=[top],
        description="Two-way sync between receipt records and Google Keep notes.",
        epilog=(
            f"Credentials come from ${EMAIL_ENV} and ${TOKEN_ENV}; "
            "see the README for how to mint a master token."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_sync = sub.add_parser("sync", parents=[common], help="run a two-way sync")
    p_sync.add_argument("--dry-run", action="store_true", help="report, write nothing")
    p_sync.add_argument(
        "--conflict",
        choices=[p.value for p in ConflictPolicy],
        default=ConflictPolicy.MANUAL.value,
        help="who wins when both sides changed the same field (default: manual)",
    )
    p_sync.set_defaults(func=cmd_sync)

    sub.add_parser(
        "status", parents=[common], help="show local/ledger state"
    ).set_defaults(func=cmd_status)

    p_add = sub.add_parser("add", parents=[common], help="create a receipt locally")
    p_add.add_argument("vendor")
    p_add.add_argument("total")
    p_add.add_argument("--date")
    p_add.add_argument("--currency", default="USD")
    p_add.add_argument("--category")
    p_add.add_argument("--payment")
    p_add.add_argument("--notes")
    p_add.add_argument("--tag", action="append", default=[])
    p_add.set_defaults(func=cmd_add)

    p_show = sub.add_parser("show", parents=[common], help="print one receipt as JSON")
    p_show.add_argument("receipt_id")
    p_show.set_defaults(func=cmd_show)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (ValueError, OSError) as exc:
        print(f"keep-bridge: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
