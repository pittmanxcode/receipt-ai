"""The two-way sync engine.

One run pairs every local receipt with its Keep note, three-way merges the
pair, and writes the result back to whichever side is behind. Ordering is:
read both sides -> merge everything in memory -> apply writes -> flush Keep ->
save the ledger last, so an interrupted run re-syncs cleanly instead of
recording agreement that never happened.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from .backends.base import KeepBackend, KeepNote
from .merge import ConflictPolicy, FieldConflict, merge_content
from .model import Receipt, content_differs, new_id
from .serialize import extract_marker, looks_like_receipt, note_text, note_title, parse_note
from .store import ReceiptStore
from .syncstate import SyncState

DEFAULT_LABEL = "receipts"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class ReceiptOutcome:
    receipt_id: str
    action: str  # created_local | created_remote | updated_local | updated_remote
                 # | updated_both | dropped_link | skipped_unrecognized | unchanged
    conflicts: list[FieldConflict] = field(default_factory=list)
    detail: str = ""


@dataclass
class SyncReport:
    outcomes: list[ReceiptOutcome] = field(default_factory=list)
    dry_run: bool = False

    def add(self, *args, **kwargs) -> None:
        self.outcomes.append(ReceiptOutcome(*args, **kwargs))

    def count(self, action: str) -> int:
        return sum(1 for o in self.outcomes if o.action == action)

    @property
    def conflicts(self) -> list[ReceiptOutcome]:
        return [o for o in self.outcomes if o.conflicts]

    @property
    def changed(self) -> int:
        return sum(1 for o in self.outcomes if o.action != "unchanged")

    def summary(self) -> str:
        prefix = "would sync" if self.dry_run else "synced"
        bits = [
            f"{self.count('created_remote')} new to Keep",
            f"{self.count('created_local')} new from Keep",
            f"{self.count('updated_remote')} pushed",
            f"{self.count('updated_local')} pulled",
            f"{self.count('updated_both')} merged both ways",
            f"{self.count('unchanged')} unchanged",
        ]
        skipped = self.count("skipped_unrecognized")
        if skipped:
            bits.append(f"{skipped} skipped (not receipt-shaped)")
        line = f"{prefix}: " + ", ".join(bits)
        if self.conflicts:
            line += f" -- {len(self.conflicts)} with conflicts"
        return line


class SyncEngine:
    def __init__(
        self,
        store: ReceiptStore,
        state: SyncState,
        backend: KeepBackend,
        label: str = DEFAULT_LABEL,
        policy: ConflictPolicy = ConflictPolicy.MANUAL,
        adopt_unrecognized: bool = False,
    ) -> None:
        self.store = store
        self.state = state
        self.backend = backend
        self.label = label
        self.policy = policy
        self.adopt_unrecognized = adopt_unrecognized

    # -- pairing ------------------------------------------------------
    def _pair(
        self, local: dict[str, Receipt], notes: list[KeepNote]
    ) -> dict[str, tuple[Receipt | None, KeepNote | None]]:
        """Match receipts to notes: by ledger link first, then by body marker.

        The marker fallback is what makes a lost or corrupt ledger a non-event
        -- notes carry their receipt id, so they re-link rather than duplicate.
        """
        by_note_id = {n.id: n for n in notes}
        by_marker: dict[str, KeepNote] = {}
        for note in notes:
            marker = extract_marker(note.text)
            if marker and marker not in by_marker:
                by_marker[marker] = note

        pairs: dict[str, tuple[Receipt | None, KeepNote | None]] = {}
        claimed: set[str] = set()

        for receipt_id, receipt in local.items():
            note = None
            link = self.state.by_receipt(receipt_id)
            if link:
                note = by_note_id.get(link.note_id)
            if note is None:
                note = by_marker.get(receipt_id)
            if note is not None:
                claimed.add(note.id)
            pairs[receipt_id] = (receipt, note)

        for note in notes:
            if note.id in claimed:
                continue
            marker = extract_marker(note.text)
            receipt_id = marker if marker and marker not in pairs else new_id()
            pairs[receipt_id] = (None, note)

        return pairs

    # -- run ----------------------------------------------------------
    def run(self, dry_run: bool = False) -> SyncReport:
        report = SyncReport(dry_run=dry_run)
        local = self.store.load_all()
        notes = self.backend.list_notes(self.label)
        pairs = self._pair(local, notes)
        touched_remote = False
        now = _now()

        for receipt_id, (receipt, note) in sorted(pairs.items()):
            if receipt is not None and note is None:
                touched_remote |= self._local_only(receipt, report, dry_run, now)
            elif receipt is None and note is not None:
                touched_remote |= self._remote_only(receipt_id, note, report, dry_run, now)
            elif receipt is not None and note is not None:
                touched_remote |= self._both(receipt, note, report, dry_run, now)

        if not dry_run:
            if touched_remote:
                self.backend.flush()
            # The ledger is written last: it may only claim agreement that the
            # writes above actually reached both sides.
            self.state.save()
        return report

    # -- cases --------------------------------------------------------
    def _local_only(
        self, receipt: Receipt, report: SyncReport, dry_run: bool, now: str
    ) -> bool:
        """A receipt with no note: either brand new, or its note is gone."""
        link = self.state.by_receipt(receipt.id)

        if link is not None:
            # The note vanished from Keep entirely -- purged from trash, or the
            # label was removed. Neither is a delete we honour: Keep empties
            # trash on its own after a week, and letting that erase committed
            # receipt data would be data loss the user never asked for. A real
            # delete arrives as a *trashed* note, which we see and propagate
            # before the purge. So we rebuild the note, unless the receipt is
            # itself trashed -- then the deletion already happened on purpose.
            if receipt.trashed:
                if not dry_run:
                    self.state.forget(receipt.id)
                report.add(receipt.id, "dropped_link", detail="note gone; receipt trashed")
                return False
            if not dry_run:
                created = self.backend.create_note(
                    note_title(receipt), note_text(receipt), self.label
                )
                self.state.record(receipt.id, created.id, receipt.content(), now)
            report.add(receipt.id, "created_remote", detail="note missing in Keep; recreated")
            return True

        if receipt.trashed:
            report.add(receipt.id, "unchanged", detail="trashed locally, never synced")
            return False

        if not dry_run:
            created = self.backend.create_note(
                note_title(receipt), note_text(receipt), self.label
            )
            self.state.record(receipt.id, created.id, receipt.content(), now)
        report.add(receipt.id, "created_remote")
        return True

    def _remote_only(
        self, receipt_id: str, note: KeepNote, report: SyncReport, dry_run: bool, now: str
    ) -> bool:
        """A labelled note with no receipt: import it, unless it is trash."""
        if note.trashed:
            report.add(receipt_id, "unchanged", detail="trashed note, not imported")
            return False

        if not self.adopt_unrecognized and not looks_like_receipt(note.text):
            # Someone else's note that happens to carry the label. Importing it
            # would rewrite their text into a receipt template, so leave it be.
            report.add(
                receipt_id,
                "skipped_unrecognized",
                detail=f"note {note.id}: no receipt fields; left untouched",
            )
            return False

        receipt = self._receipt_from_note(note, receipt_id)
        if not dry_run:
            self.store.save(receipt)
            # Rewrite the note in canonical form so it carries its receipt id.
            self.backend.update_note(note.id, note_title(receipt), note_text(receipt))
            self.state.record(receipt.id, note.id, receipt.content(), now)
        report.add(receipt_id, "created_local")
        return True

    def _both(
        self, receipt: Receipt, note: KeepNote, report: SyncReport, dry_run: bool, now: str
    ) -> bool:
        link = self.state.by_receipt(receipt.id)
        base = link.base if link and link.base else None

        remote = self._receipt_from_note(note, receipt.id)
        local_content = receipt.content()
        remote_content = remote.content()

        result = merge_content(base, local_content, remote_content, self.policy)

        local_stale = content_differs(result.local_content, local_content)
        remote_stale = content_differs(result.remote_content, remote_content)

        if not dry_run:
            if local_stale:
                self.store.save(receipt.with_content(result.local_content))
            if remote_stale:
                merged_receipt = receipt.with_content(result.remote_content)
                self.backend.update_note(
                    note.id, note_title(merged_receipt), note_text(merged_receipt)
                )
                remote_trashed = bool(result.remote_content.get("trashed"))
                if remote_trashed != note.trashed:
                    self.backend.set_trashed(note.id, remote_trashed)
            # Conflicts left for a human are not agreement: keeping the old
            # base means the next run still sees both edits and re-reports.
            if result.has_conflicts and self.policy is ConflictPolicy.MANUAL:
                if link is None:
                    self.state.record(receipt.id, note.id, {}, now)
                else:
                    link.note_id = note.id
            else:
                self.state.record(receipt.id, note.id, result.local_content, now)

        if local_stale and remote_stale:
            action = "updated_both"
        elif local_stale:
            action = "updated_local"
        elif remote_stale:
            action = "updated_remote"
        else:
            action = "unchanged"

        report.add(receipt.id, action, conflicts=result.conflicts)
        return remote_stale

    def _receipt_from_note(self, note: KeepNote, receipt_id: str) -> Receipt:
        """Parse a note body, taking `trashed` from Keep rather than the text.

        Trash is note metadata in Keep, not something written in the body, so
        it has to be layered on after parsing or every pull would clear it.
        """
        parsed = parse_note(note.text, receipt_id)
        return parsed.replace(trashed=note.trashed)
