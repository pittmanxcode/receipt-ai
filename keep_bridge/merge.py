"""Three-way merge of receipt content.

Every sync decision comes from comparing three versions of a record: the
`base` (what both sides agreed on at the end of the last successful sync), the
current `local` copy, and the current `remote` copy parsed out of Keep.

Comparing against a base is what makes this a real two-way sync rather than a
last-writer-wins overwrite: a field only loses its value when the side that
changed it is the only side that changed it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .model import SYNCED_FIELDS


class ConflictPolicy(str, Enum):
    """What to do when both sides changed the same field to different values."""

    MANUAL = "manual"  # keep the base value, report it, touch nothing
    LOCAL = "local"    # local wins
    REMOTE = "remote"  # Keep wins


@dataclass(frozen=True)
class FieldConflict:
    name: str
    base: object
    local: object
    remote: object
    resolution: str  # "manual" | "local" | "remote"

    def describe(self) -> str:
        return (
            f"{self.name}: local={self.local!r} remote={self.remote!r} "
            f"(base={self.base!r}) -> {self.resolution}"
        )


@dataclass
class MergeResult:
    """The merged content, as each side should end up seeing it.

    The two differ only on unresolved conflicts. Under MANUAL policy a
    conflicted field keeps *each side's own* value rather than collapsing to
    one: reverting both sides to the base would quietly destroy both edits,
    which is the one outcome a two-way sync must never produce.
    """

    local_content: dict
    remote_content: dict
    conflicts: list[FieldConflict] = field(default_factory=list)

    @property
    def has_conflicts(self) -> bool:
        return bool(self.conflicts)


def _merge_tags(base: list, local: list, remote: list) -> list:
    """Set-merge tags: additions from either side stick, removals win.

    Tags are the one field where both sides editing is normal rather than a
    collision, so they merge structurally instead of conflicting.
    """
    base_set, local_set, remote_set = set(base or []), set(local or []), set(remote or [])
    added = (local_set - base_set) | (remote_set - base_set)
    removed = (base_set - local_set) | (base_set - remote_set)
    return sorted((base_set | added) - removed)


def merge_content(
    base: dict | None,
    local: dict,
    remote: dict,
    policy: ConflictPolicy = ConflictPolicy.MANUAL,
) -> MergeResult:
    """Merge one receipt's synced fields.

    With no base (a record newly linked on both sides) there is nothing to
    diff against, so any disagreement is a conflict rather than a guess.
    """
    local_out: dict = {}
    remote_out: dict = {}
    conflicts: list[FieldConflict] = []
    have_base = base is not None
    base = base or {}

    for name in SYNCED_FIELDS:
        base_value = base.get(name)
        local_value = local.get(name)
        remote_value = remote.get(name)

        def agree(value) -> None:
            local_out[name] = value
            remote_out[name] = value

        if local_value == remote_value:
            agree(local_value)
            continue

        if name == "tags" and have_base:
            agree(_merge_tags(base_value or [], local_value or [], remote_value or []))
            continue

        if have_base and local_value == base_value:
            agree(remote_value)  # only Keep changed it
            continue
        if have_base and remote_value == base_value:
            agree(local_value)  # only local changed it
            continue

        # Both sides moved this field, and they disagree.
        if policy is ConflictPolicy.LOCAL:
            agree(local_value)
        elif policy is ConflictPolicy.REMOTE:
            agree(remote_value)
        else:
            # Leave both sides exactly as their owner left them.
            local_out[name] = local_value
            remote_out[name] = remote_value
        conflicts.append(
            FieldConflict(
                name=name,
                base=base_value,
                local=local_value,
                remote=remote_value,
                resolution=policy.value,
            )
        )

    return MergeResult(
        local_content=local_out, remote_content=remote_out, conflicts=conflicts
    )
