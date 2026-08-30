"""In-memory Keep, for tests and --dry-run rehearsals."""

from __future__ import annotations

import itertools

from .base import KeepNote


class FakeKeepBackend:
    """A KeepBackend that records every call it receives."""

    def __init__(self, notes: list[KeepNote] | None = None) -> None:
        self._notes: dict[str, KeepNote] = {n.id: n for n in (notes or [])}
        self._ids = itertools.count(1)
        self.flushes = 0
        self.calls: list[tuple] = []

    def _next_id(self) -> str:
        while True:
            candidate = f"note-{next(self._ids)}"
            if candidate not in self._notes:
                return candidate

    def list_notes(self, label: str) -> list[KeepNote]:
        self.calls.append(("list_notes", label))
        return [
            KeepNote(n.id, n.title, n.text, list(n.labels), n.trashed)
            for n in self._notes.values()
            if label in n.labels
        ]

    def create_note(self, title: str, text: str, label: str) -> KeepNote:
        note = KeepNote(self._next_id(), title, text, [label], False)
        self._notes[note.id] = note
        self.calls.append(("create_note", note.id))
        return note

    def update_note(self, note_id: str, title: str, text: str) -> None:
        note = self._notes[note_id]
        note.title, note.text = title, text
        self.calls.append(("update_note", note_id))

    def set_trashed(self, note_id: str, trashed: bool) -> None:
        self._notes[note_id].trashed = trashed
        self.calls.append(("set_trashed", note_id, trashed))

    def flush(self) -> None:
        self.flushes += 1

    # -- test helpers -------------------------------------------------
    def note(self, note_id: str) -> KeepNote:
        return self._notes[note_id]

    def edit(self, note_id: str, text: str) -> None:
        """Simulate a human editing the note body in the Keep app."""
        self._notes[note_id].text = text
