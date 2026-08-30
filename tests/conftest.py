import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from keep_bridge.backends import FakeKeepBackend  # noqa: E402
from keep_bridge.merge import ConflictPolicy  # noqa: E402
from keep_bridge.store import ReceiptStore  # noqa: E402
from keep_bridge.sync import DEFAULT_LABEL, SyncEngine  # noqa: E402
from keep_bridge.syncstate import SyncState  # noqa: E402


@pytest.fixture
def env(tmp_path):
    """A store, ledger and fake Keep wired into an engine factory."""

    class Env:
        def __init__(self):
            self.root = tmp_path
            self.store = ReceiptStore(tmp_path / "receipts")
            self.backend = FakeKeepBackend()
            self.state_path = tmp_path / "sync-state.json"

        def engine(self, policy=ConflictPolicy.MANUAL):
            # A fresh SyncState each time, as a real CLI invocation would.
            return SyncEngine(
                self.store,
                SyncState(self.state_path),
                self.backend,
                DEFAULT_LABEL,
                policy,
            )

        def sync(self, policy=ConflictPolicy.MANUAL, dry_run=False):
            return self.engine(policy).run(dry_run=dry_run)

    return Env()
