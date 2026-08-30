import json

from keep_bridge.cli import build_parser, main


def run(tmp_path, *argv) -> int:
    return main(
        [
            "--data-dir",
            str(tmp_path / "receipts"),
            "--state-dir",
            str(tmp_path / "state"),
            *argv,
        ]
    )


def test_add_then_show(tmp_path, capsys):
    assert run(tmp_path, "add", "Blue Bottle", "$8.75", "--date", "3/2/2026") == 0
    receipt_id = capsys.readouterr().out.split()[0]

    assert run(tmp_path, "show", receipt_id) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["vendor"] == "Blue Bottle"
    assert payload["total"] == "8.75"
    assert payload["date"] == "2026-03-02"


def test_show_unknown_receipt_exits_nonzero(tmp_path):
    assert run(tmp_path, "show", "nope") == 2


def test_offline_dry_run_needs_no_credentials(tmp_path, capsys):
    run(tmp_path, "add", "Blue Bottle", "8.75")
    capsys.readouterr()

    assert run(tmp_path, "sync", "--offline", "--dry-run") == 0
    assert "1 new to Keep" in capsys.readouterr().out
    # The lock file may exist; the ledger must not -- a dry run agrees to nothing.
    assert not (tmp_path / "state" / "sync-state.json").exists()


def test_sync_without_credentials_fails_cleanly(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("KEEP_EMAIL", raising=False)
    monkeypatch.delenv("KEEP_MASTER_TOKEN", raising=False)

    assert run(tmp_path, "sync") == 2
    assert "KEEP_EMAIL" in capsys.readouterr().err


def test_global_flags_are_accepted_on_either_side_of_the_subcommand():
    before = build_parser().parse_args(["--offline", "-v", "sync"])
    after = build_parser().parse_args(["sync", "--offline", "-v"])
    assert (before.offline, before.verbose) == (True, True)
    assert (after.offline, after.verbose) == (True, True)


def test_status_reports_unsynced_receipts(tmp_path, capsys):
    run(tmp_path, "add", "Blue Bottle", "8.75")
    capsys.readouterr()

    assert run(tmp_path, "status") == 0
    out = capsys.readouterr().out
    assert "receipts: 1 (0 linked to Keep" in out
    assert "not yet synced" in out
