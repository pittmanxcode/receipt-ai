"""Crash-safety and concurrency guards."""

import json

import pytest

from keep_bridge import atomic
from keep_bridge.lock import LockBusy, run_lock
from keep_bridge.syncstate import SyncState


def test_lock_excludes_a_second_run(tmp_path):
    with run_lock(tmp_path / "lock"):
        with pytest.raises(LockBusy):
            with run_lock(tmp_path / "lock"):
                pass


def test_lock_is_released_after_the_run(tmp_path):
    with run_lock(tmp_path / "lock"):
        pass
    with run_lock(tmp_path / "lock"):  # must not raise
        pass


def test_lock_is_released_even_when_the_run_raises(tmp_path):
    with pytest.raises(ValueError):
        with run_lock(tmp_path / "lock"):
            raise ValueError("boom")
    with run_lock(tmp_path / "lock"):
        pass


def test_atomic_write_leaves_no_temp_files(tmp_path):
    target = tmp_path / "out.json"
    atomic.write_text(target, '{"a": 1}')
    assert json.loads(target.read_text()) == {"a": 1}
    assert [p.name for p in tmp_path.iterdir()] == ["out.json"]


def test_a_failed_atomic_write_leaves_the_original_intact(tmp_path, monkeypatch):
    target = tmp_path / "ledger.json"
    atomic.write_text(target, "original")

    def explode(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(atomic.os, "replace", explode)
    with pytest.raises(OSError):
        atomic.write_text(target, "replacement")

    # The merge base survives a failed write, and no debris is left behind.
    assert target.read_text() == "original"
    assert [p.name for p in tmp_path.iterdir()] == ["ledger.json"]


def test_ledger_permissions_are_not_world_writable(tmp_path):
    state = SyncState(tmp_path / "sync-state.json")
    state.record("abc", "note-1", {"vendor": "X"}, "2026-08-30T00:00:00+00:00")
    state.save()
    assert (tmp_path / "sync-state.json").stat().st_mode & 0o022 == 0


def test_ledger_survives_a_reload(tmp_path):
    path = tmp_path / "sync-state.json"
    state = SyncState(path)
    state.record("abc", "note-1", {"vendor": "X"}, "2026-08-30T00:00:00+00:00")
    state.save()

    reloaded = SyncState(path)
    assert reloaded.by_receipt("abc").note_id == "note-1"
    assert reloaded.by_note("note-1").base == {"vendor": "X"}
