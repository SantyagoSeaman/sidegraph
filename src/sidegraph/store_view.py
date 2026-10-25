"""The effective records of a format-2 store, for the readers that do not open a ``Store``.

Doctor, ADR export, stats, integrity and the bootstrap catalog read the committed store
directly. On a packed store each asks this module, which builds the occurrence inventory and
applies ``store_rules.effective()``: none keeps its own precedence. A format-1 store is not
served here (:func:`effective_records` returns ``None``): every reader keeps its per-file
walk for it, byte for byte.

Pydantic-free apart from the model check handed to the inventory (``schema.validate_record_body``).

# see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§7.2)
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

from .schema import validate_record_body
from .store_format import Inventory, Occurrence, inventory_from_disk
from .store_layout import StoreRequirementError, store_marker
from .store_rules import effective_all

# record_type -> store sub-directory, the names ``store_format._RECORD_DIRS`` pairs
SUBDIR_OF = {
    "entity": "entities",
    "decision": "decisions",
    "fact": "facts",
    "domain": "domains",
    "bindings": "bindings",
}


_SHARED: ContextVar[dict[tuple[str, bool, bool], dict[tuple[str, str], EffectiveRecord]] | None] = (
    ContextVar("store_view_shared", default=None)
)


@contextmanager
def shared_view() -> Iterator[None]:
    """Within the block, :func:`effective_records` builds each store's view once. For a caller
    that asks many questions of one unchanging store (doctor's ``curate``) and would otherwise
    read every shard once per question."""
    token = _SHARED.set({})
    try:
        yield
    finally:
        _SHARED.reset(token)


@dataclass(frozen=True)
class EffectiveRecord:
    """One identity's effective record: its parsed body and the occurrence it came from."""

    record_type: str
    record_id: str
    raw: object
    source: Occurrence

    def source_path(self, store_dir: Path) -> Path:
        """Where the record is stored: its file, its shard or its archive segment."""
        return store_dir / self.source.path


def is_packed_store(store_dir: str | Path) -> bool:
    """Whether the marker of ``store_dir`` says major 2. A missing or malformed marker is
    major 1 here: readers that skip silently stay silent, verify reports the marker."""
    try:
        marker = store_marker(store_dir)
    except (StoreRequirementError, OSError, ValueError):
        return False
    return marker is not None and marker.major == 2


def inventory_of(
    store_dir: str | Path, on_unreadable: Callable[[str, OSError], None] | None = None
) -> Inventory:
    """The occurrence inventory of the store on disk, unreadable files counted as not regular.

    # see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§5.3)"""
    return inventory_from_disk(
        Path(store_dir),
        validate=validate_record_body,
        on_unreadable=on_unreadable or (lambda _rel, _exc: None),
    )


def effective_records(
    store_dir: str | Path, *, any_format: bool = False, lenient: bool = False
) -> dict[tuple[str, str], EffectiveRecord] | None:
    """Every identity with an effective record, by ``(record_type, record_id)``; ``None`` when
    the store is not format 2 (unless ``any_format``: a per-record store is then read through
    the same rules, its archive segments the only other source). Identities the rules
    quarantine (unreadable, conflicted or duplicate-packed) are absent, as for retrieval.

    ``lenient`` is for a reader that never refuses a store and reads whatever parses (doctor's
    silent-skip policy): an identity quarantined only because its one parseable body fails the
    model check is then served from that body. It never resolves a conflict, a duplicate or a
    body that does not parse.

    # see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§5.3, §7.2)"""
    if not any_format and not is_packed_store(store_dir):
        return None
    shared = _SHARED.get()
    key = (str(Path(store_dir).absolute()), any_format, lenient)
    if shared is not None and key in shared:
        return shared[key]
    out = effective_bodies(inventory_of(store_dir), lenient=lenient)
    if shared is not None:
        shared[key] = out
    return out


def effective_bodies(
    inv: Inventory, *, lenient: bool = False
) -> dict[tuple[str, str], EffectiveRecord]:
    """:func:`effective_records` over an inventory already built (a disk store's or a git
    tree's): the effective record of every identity that has one, ``lenient`` as there.

    # see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§5.3, §7.2)"""
    groups = inv.by_identity() if lenient else {}
    out: dict[tuple[str, str], EffectiveRecord] = {}
    for (record_type, record_id), eff in sorted(effective_all(inv).items()):
        if eff.raw is not None and eff.source is not None:
            out[(record_type, record_id)] = EffectiveRecord(
                record_type, record_id, eff.raw, eff.source
            )
        elif lenient and eff.reason == "unreadable":
            only = _only_parseable(groups[(record_type, record_id)])
            if only is not None:
                out[(record_type, record_id)] = EffectiveRecord(
                    record_type, record_id, only.raw, only
                )
    return out


def _only_parseable(occurrences: list[Occurrence]) -> Occurrence | None:
    """The one parseable, conflict-free file or shard occurrence of an identity whose every
    other occurrence is an archive line; ``None`` when there are several or none."""
    packed = [o for o in occurrences if o.kind != "archive"]
    if len(packed) != 1:
        return None
    occ = packed[0]
    return occ if occ.parseable and occ.raw is not None and not occ.in_conflict else None


def records_of(
    store_dir: str | Path, record_type: str, *, lenient: bool = False
) -> list[EffectiveRecord] | None:
    """The effective records of one type, sorted by id; ``None`` when the store is not format 2."""
    found = effective_records(store_dir, lenient=lenient)
    if found is None:
        return None
    return [rec for (t, _), rec in found.items() if t == record_type]
