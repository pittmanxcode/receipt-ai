"""The narrow surface the sync engine needs from Google Keep.

Keeping this to five methods is what lets the entire engine be tested offline
against an in-memory fake -- gkeepapi is unofficial and cannot be exercised in
CI, so nothing above this line is allowed to depend on it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass
class KeepNote:
    """A Keep note, reduced to the fields a receipt actually needs."""

    id: str
    title: str = ""
    text: str = ""
    labels: list[str] = field(default_factory=list)
    trashed: bool = False


@runtime_checkable
class KeepBackend(Protocol):
    def list_notes(self, label: str) -> list[KeepNote]:
        """Every note carrying `label`, trashed ones included.

        Trashed notes must be returned: a note trashed in Keep is a delete the
        engine has to propagate, and an invisible note is an undetectable one.
        """

    def create_note(self, title: str, text: str, label: str) -> KeepNote: ...

    def update_note(self, note_id: str, title: str, text: str) -> None: ...

    def set_trashed(self, note_id: str, trashed: bool) -> None: ...

    def flush(self) -> None:
        """Push buffered writes upstream. Called once at the end of a run."""
