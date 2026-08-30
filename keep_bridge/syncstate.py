"""The sync ledger: what local and Keep last agreed on.

This file is the merge base. Without it every sync degrades to last-writer-
wins, so it is written only after a run's writes have actually landed.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

STATE_VERSION = 1


@dataclass
class LinkState:
    """One receipt's link to one Keep note, plus the agreed-on content."""

    receipt_id: str
    note_id: str
    base: dict = field(default_factory=dict)
    last_synced: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "LinkState":
        return cls(
            receipt_id=data["receipt_id"],
            note_id=data["note_id"],
            base=data.get("base") or {},
            last_synced=data.get("last_synced", ""),
        )


class SyncState:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.links: dict[str, LinkState] = {}
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            # A lost ledger is recoverable: notes carry their receipt id in the
            # body, so the next run re-links them instead of duplicating.
            return
        if data.get("version") != STATE_VERSION:
            return
        self.links = {
            rid: LinkState.from_dict(entry)
            for rid, entry in (data.get("links") or {}).items()
        }

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": STATE_VERSION,
            "links": {rid: asdict(link) for rid, link in sorted(self.links.items())},
        }
        self.path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")

    # -- lookups ------------------------------------------------------
    def by_receipt(self, receipt_id: str) -> LinkState | None:
        return self.links.get(receipt_id)

    def by_note(self, note_id: str) -> LinkState | None:
        for link in self.links.values():
            if link.note_id == note_id:
                return link
        return None

    def record(self, receipt_id: str, note_id: str, base: dict, when: str) -> None:
        self.links[receipt_id] = LinkState(receipt_id, note_id, dict(base), when)

    def forget(self, receipt_id: str) -> None:
        self.links.pop(receipt_id, None)
