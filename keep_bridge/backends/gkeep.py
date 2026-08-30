"""The live Google Keep backend, built on the unofficial `gkeepapi`.

Google publishes no consumer API for Keep -- the official keep.googleapis.com
API is Workspace-only and can only see notes its own service account created.
So this backend drives `gkeepapi`, which speaks the app's private protocol. It
is reverse-engineered and can break when Google changes that protocol; every
failure mode here is surfaced as KeepAuthError rather than a stack trace.

Authentication uses a *master token*, not a password. See README for how to
mint one. The token is read from the environment and never written to disk.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .base import KeepNote

TOKEN_ENV = "KEEP_MASTER_TOKEN"
EMAIL_ENV = "KEEP_EMAIL"


class KeepAuthError(RuntimeError):
    """Raised when Keep cannot be reached or the master token is rejected."""


class GKeepBackend:
    """A KeepBackend backed by a live Keep account.

    Writes are buffered by gkeepapi and only leave the machine on `flush()`,
    which means a run that raises partway through does not leave Keep holding
    half of a sync.
    """

    def __init__(
        self,
        email: str | None = None,
        master_token: str | None = None,
        state_path: Path | None = None,
    ) -> None:
        self.email = email or os.environ.get(EMAIL_ENV, "")
        self._token = master_token or os.environ.get(TOKEN_ENV, "")
        self.state_path = state_path
        self._keep = None
        self._label_cache: dict[str, object] = {}

    # -- connection ---------------------------------------------------
    def connect(self) -> None:
        if self._keep is not None:
            return
        try:
            import gkeepapi
        except ImportError as exc:  # pragma: no cover - dependency missing
            raise KeepAuthError(
                "gkeepapi is not installed; run: pip install gkeepapi"
            ) from exc

        if not self.email:
            raise KeepAuthError(f"no account email; set ${EMAIL_ENV}")
        if not self._token:
            raise KeepAuthError(
                f"no master token; set ${TOKEN_ENV} (see README: obtaining a master token)"
            )

        keep = gkeepapi.Keep()
        state = self._load_state()
        try:
            keep.authenticate(self.email, self._token, state=state, sync=True)
        except Exception as exc:  # gkeepapi raises a wide range of errors
            raise KeepAuthError(f"Keep authentication failed: {exc}") from exc
        self._keep = keep

    def _load_state(self) -> dict | None:
        if not self.state_path or not self.state_path.exists():
            return None
        try:
            return json.loads(self.state_path.read_text())
        except (OSError, json.JSONDecodeError):
            return None  # a corrupt cache costs a full resync, nothing more

    def _save_state(self) -> None:
        if not self.state_path or self._keep is None:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(self._keep.dump()))

    def _label(self, name: str):
        """Fetch the sync label, creating it the first time the bridge runs."""
        if name in self._label_cache:
            return self._label_cache[name]
        label = self._keep.findLabel(name)
        if label is None:
            label = self._keep.createLabel(name)
        self._label_cache[name] = label
        return label

    # -- KeepBackend --------------------------------------------------
    def list_notes(self, label: str) -> list[KeepNote]:
        self.connect()
        target = self._label(label)
        notes: list[KeepNote] = []
        # trashed=None matches both trashed and live notes; a note trashed in
        # the Keep app is a delete we have to see, not a note that vanished.
        for node in self._keep.find(labels=[target], trashed=None):
            notes.append(
                KeepNote(
                    id=node.id,
                    title=node.title or "",
                    text=node.text or "",
                    labels=[lbl.name for lbl in node.labels.all()],
                    trashed=bool(node.trashed),
                )
            )
        return notes

    def create_note(self, title: str, text: str, label: str) -> KeepNote:
        self.connect()
        node = self._keep.createNote(title, text)
        node.labels.add(self._label(label))
        return KeepNote(id=node.id, title=title, text=text, labels=[label], trashed=False)

    def update_note(self, note_id: str, title: str, text: str) -> None:
        self.connect()
        node = self._keep.get(note_id)
        if node is None:
            raise KeepAuthError(f"note {note_id} is no longer in Keep")
        node.title, node.text = title, text

    def set_trashed(self, note_id: str, trashed: bool) -> None:
        self.connect()
        node = self._keep.get(note_id)
        if node is None:
            return  # already gone from Keep; nothing to propagate
        node.trash() if trashed else node.untrash()

    def flush(self) -> None:
        if self._keep is None:
            return
        try:
            self._keep.sync()
        except Exception as exc:
            raise KeepAuthError(f"pushing changes to Keep failed: {exc}") from exc
        self._save_state()
