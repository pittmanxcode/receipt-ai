"""Local receipt storage: one JSON file per receipt, committed to the repo.

One file per record rather than a single blob so that two receipts edited in
different sessions do not collide in git, and so a diff shows which receipt
changed.
"""

from __future__ import annotations

import json
from pathlib import Path

from .model import Receipt


class ReceiptStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def path_for(self, receipt_id: str) -> Path:
        return self.root / f"{receipt_id}.json"

    def load_all(self) -> dict[str, Receipt]:
        receipts: dict[str, Receipt] = {}
        if not self.root.exists():
            return receipts
        for path in sorted(self.root.glob("*.json")):
            try:
                data = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError) as exc:
                raise ValueError(f"{path}: unreadable receipt ({exc})") from exc
            # The filename is authoritative: it is what the sync state keys on.
            data.setdefault("id", path.stem)
            receipt = Receipt.from_dict(data)
            receipts[receipt.id] = receipt
        return receipts

    def save(self, receipt: Receipt) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.path_for(receipt.id)
        path.write_text(json.dumps(receipt.to_dict(), indent=2, sort_keys=True) + "\n")
        return path

    def delete(self, receipt_id: str) -> None:
        self.path_for(receipt_id).unlink(missing_ok=True)
