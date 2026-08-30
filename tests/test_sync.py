"""End-to-end sync behaviour, driven against the in-memory Keep fake."""

from decimal import Decimal

from keep_bridge.merge import ConflictPolicy
from keep_bridge.model import Receipt
from keep_bridge.serialize import extract_marker, parse_note
from keep_bridge.syncstate import SyncState


def make_receipt(**overrides) -> Receipt:
    data = {
        "vendor": "Trader Joe's",
        "date": "2026-08-14",
        "total": "42.17",
        "category": "groceries",
        "tags": ["food"],
    }
    data.update(overrides)
    return Receipt.from_dict(data)


def note_for(env, receipt_id):
    state = SyncState(env.state_path)
    return env.backend.note(state.by_receipt(receipt_id).note_id)


# -- creation -------------------------------------------------------------


def test_new_local_receipt_is_pushed_to_keep(env):
    receipt = make_receipt()
    env.store.save(receipt)

    report = env.sync()

    assert report.count("created_remote") == 1
    note = note_for(env, receipt.id)
    assert "Trader Joe's" in note.title
    assert extract_marker(note.text) == receipt.id
    assert note.labels == ["receipts"]


def test_new_labelled_note_is_imported_as_a_receipt(env):
    env.backend.create_note("whatever", "vendor: Blue Bottle\ntotal: 8.75", "receipts")

    report = env.sync()

    assert report.count("created_local") == 1
    (receipt,) = env.store.load_all().values()
    assert receipt.vendor == "Blue Bottle"
    assert receipt.total == Decimal("8.75")


def test_imported_note_is_rewritten_with_its_receipt_id(env):
    note = env.backend.create_note("x", "vendor: Blue Bottle\ntotal: 8.75", "receipts")

    env.sync()

    (receipt,) = env.store.load_all().values()
    assert extract_marker(env.backend.note(note.id).text) == receipt.id


def test_unlabelled_notes_are_never_touched(env):
    env.backend.create_note("private", "not a receipt", "grocery-list")

    report = env.sync()

    assert report.outcomes == []
    assert env.store.load_all() == {}


# -- steady state ---------------------------------------------------------


def test_second_sync_is_a_no_op(env):
    env.store.save(make_receipt())
    env.sync()

    report = env.sync()

    assert report.changed == 0
    assert report.count("unchanged") == 1


def test_local_edit_is_pushed(env):
    receipt = make_receipt()
    env.store.save(receipt)
    env.sync()

    env.store.save(receipt.replace(category="household"))
    report = env.sync()

    assert report.count("updated_remote") == 1
    assert "Category: household" in note_for(env, receipt.id).text


def test_keep_edit_is_pulled(env):
    receipt = make_receipt()
    env.store.save(receipt)
    env.sync()

    note = note_for(env, receipt.id)
    env.backend.edit(note.id, note.text.replace("Total: 42.17", "Total: 50.00"))
    report = env.sync()

    assert report.count("updated_local") == 1
    assert env.store.load_all()[receipt.id].total == Decimal("50.00")


# -- the two-way case -----------------------------------------------------


def test_edits_to_different_fields_on_both_sides_both_survive(env):
    receipt = make_receipt()
    env.store.save(receipt)
    env.sync()

    env.store.save(receipt.replace(category="household"))
    note = note_for(env, receipt.id)
    env.backend.edit(note.id, note.text.replace("Total: 42.17", "Total: 50.00"))

    report = env.sync()

    assert report.count("updated_both") == 1
    merged = env.store.load_all()[receipt.id]
    assert merged.category == "household"  # local edit kept
    assert merged.total == Decimal("50.00")  # Keep edit kept
    assert not report.conflicts
    # and Keep now agrees
    assert parse_note(note_for(env, receipt.id).text, receipt.id).category == "household"


def test_tag_additions_from_both_sides_merge(env):
    receipt = make_receipt(tags=["food"])
    env.store.save(receipt)
    env.sync()

    env.store.save(receipt.replace(tags=["food", "reimbursable"]))
    note = note_for(env, receipt.id)
    env.backend.edit(note.id, note.text.replace("Tags: food", "Tags: food, deductible"))

    env.sync()

    assert env.store.load_all()[receipt.id].tags == ["deductible", "food", "reimbursable"]


# -- conflicts ------------------------------------------------------------


def test_same_field_edited_both_sides_conflicts_and_neither_side_loses(env):
    receipt = make_receipt()
    env.store.save(receipt)
    env.sync()

    env.store.save(receipt.replace(total=Decimal("11.00")))
    note = note_for(env, receipt.id)
    env.backend.edit(note.id, note.text.replace("Total: 42.17", "Total: 99.00"))

    report = env.sync()

    (outcome,) = report.conflicts
    assert [c.name for c in outcome.conflicts] == ["total"]
    # Neither side is clobbered while a human has not decided: each keeps its
    # own edit, and reverting both to the base would destroy both.
    assert env.store.load_all()[receipt.id].total == Decimal("11.00")
    assert "Total: 99.00" in note_for(env, receipt.id).text


def test_unresolved_conflict_is_reported_again_next_run(env):
    receipt = make_receipt()
    env.store.save(receipt)
    env.sync()
    env.store.save(receipt.replace(total=Decimal("11.00")))
    note = note_for(env, receipt.id)
    env.backend.edit(note.id, note.text.replace("Total: 42.17", "Total: 99.00"))
    env.sync()

    # A conflict must not be silently swallowed by recording a new base.
    assert env.sync().conflicts


def test_conflict_policy_local_wins(env):
    receipt = make_receipt()
    env.store.save(receipt)
    env.sync()
    env.store.save(receipt.replace(total=Decimal("11.00")))
    note = note_for(env, receipt.id)
    env.backend.edit(note.id, note.text.replace("Total: 42.17", "Total: 99.00"))

    report = env.sync(policy=ConflictPolicy.LOCAL)

    assert report.conflicts
    assert env.store.load_all()[receipt.id].total == Decimal("11.00")
    assert "Total: 11.00" in note_for(env, receipt.id).text
    assert not env.sync().conflicts  # resolved, so it settles


def test_conflict_policy_remote_wins(env):
    receipt = make_receipt()
    env.store.save(receipt)
    env.sync()
    env.store.save(receipt.replace(total=Decimal("11.00")))
    note = note_for(env, receipt.id)
    env.backend.edit(note.id, note.text.replace("Total: 42.17", "Total: 99.00"))

    env.sync(policy=ConflictPolicy.REMOTE)

    assert env.store.load_all()[receipt.id].total == Decimal("99.00")


# -- deletion -------------------------------------------------------------


def test_trashing_a_note_in_keep_trashes_the_receipt(env):
    receipt = make_receipt()
    env.store.save(receipt)
    env.sync()

    env.backend.set_trashed(note_for(env, receipt.id).id, True)
    env.sync()

    assert env.store.load_all()[receipt.id].trashed is True


def test_trashing_a_receipt_trashes_the_note(env):
    receipt = make_receipt()
    env.store.save(receipt)
    env.sync()

    env.store.save(receipt.replace(trashed=True))
    env.sync()

    assert note_for(env, receipt.id).trashed is True


def test_a_trashed_note_is_not_imported(env):
    note = env.backend.create_note("x", "vendor: Ghost\ntotal: 1.00", "receipts")
    env.backend.set_trashed(note.id, True)

    report = env.sync()

    assert env.store.load_all() == {}
    assert report.count("created_local") == 0


def test_a_note_purged_from_keep_is_rebuilt_not_deleted_locally(env):
    # Keep empties its own trash after a week. That auto-purge must never
    # erase committed receipt data.
    receipt = make_receipt()
    env.store.save(receipt)
    env.sync()
    state = SyncState(env.state_path)
    del env.backend._notes[state.by_receipt(receipt.id).note_id]

    report = env.sync()

    assert report.count("created_remote") == 1
    assert receipt.id in env.store.load_all()
    assert extract_marker(note_for(env, receipt.id).text) == receipt.id


def test_a_purged_note_for_an_already_trashed_receipt_just_drops_the_link(env):
    receipt = make_receipt(trashed=True)
    env.store.save(receipt)
    env.sync()  # trashed and never synced -> no note
    assert env.backend.list_notes("receipts") == []


# -- resilience -----------------------------------------------------------


def test_a_lost_ledger_relinks_instead_of_duplicating(env):
    receipt = make_receipt()
    env.store.save(receipt)
    env.sync()
    env.state_path.unlink()

    report = env.sync()

    assert len(env.backend.list_notes("receipts")) == 1
    assert len(env.store.load_all()) == 1
    assert report.count("created_remote") == 0
    assert report.count("created_local") == 0


def test_a_corrupt_ledger_is_survivable(env):
    receipt = make_receipt()
    env.store.save(receipt)
    env.sync()
    env.state_path.write_text("{ this is not json")

    env.sync()

    assert len(env.backend.list_notes("receipts")) == 1


def test_dry_run_writes_nothing_anywhere(env):
    receipt = make_receipt()
    env.store.save(receipt)

    report = env.sync(dry_run=True)

    assert report.count("created_remote") == 1
    assert env.backend.list_notes("receipts") == []
    assert not env.state_path.exists()
    assert env.backend.flushes == 0


def test_keep_is_flushed_once_when_there_are_writes(env):
    env.store.save(make_receipt())
    env.sync()
    assert env.backend.flushes == 1


def test_keep_is_not_flushed_when_nothing_changed(env):
    env.store.save(make_receipt())
    env.sync()
    env.sync()
    assert env.backend.flushes == 1


def test_two_notes_claiming_the_same_receipt_id_do_not_collide(env):
    receipt = make_receipt()
    env.store.save(receipt)
    env.sync()
    original = note_for(env, receipt.id)
    # A duplicated note in Keep carries the same marker as the original.
    env.backend.create_note("copy", original.text, "receipts")

    env.sync()

    # The duplicate must become its own receipt, not overwrite the first.
    assert len(env.store.load_all()) == 2


def test_a_conflict_does_not_block_the_other_fields_on_the_same_receipt(env):
    receipt = make_receipt()
    env.store.save(receipt)
    env.sync()

    # Same field contested, a different field changed cleanly on each side.
    env.store.save(receipt.replace(total=Decimal("11.00"), category="household"))
    note = note_for(env, receipt.id)
    env.backend.edit(
        note.id,
        note.text.replace("Total: 42.17", "Total: 99.00").replace(
            "Date: 2026-08-14", "Date: 2026-08-15"
        ),
    )

    report = env.sync()

    assert report.conflicts
    merged = env.store.load_all()[receipt.id]
    assert merged.total == Decimal("11.00")  # contested: local keeps its own
    assert merged.date == "2026-08-15"  # clean Keep edit still lands
    remote = parse_note(note_for(env, receipt.id).text, receipt.id)
    assert remote.total == Decimal("99.00")  # contested: Keep keeps its own
    assert remote.category == "household"  # clean local edit still lands
