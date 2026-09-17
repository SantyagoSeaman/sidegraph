"""Deterministic duplicate resolution: lowest ``entity_id`` wins (design D4,
entity-identity-uniqueness spec).

A duplicate logical identity is LEGAL (D2 rejects a UNIQUE index because it would brick a
legally git-merged store — see ``test_store_opens_with_merged_duplicate.py``). Without a
tie-break rule, ``find_abstract_entity``/``find_entity`` return whichever row SQLite happens
to hand back first for an un-ordered scan, and ``get_or_create_abstract_entity``'s own inline
lookup could disagree with the read path about which duplicate wins. D4 makes "lowest id" the
one and only rule — ULIDs sort lexicographically by mint time, so it is also the intuitive
"the one that existed first", and it is stable across processes and index rebuilds.

**The construction below is deliberately the INVERSION, not the natural shape.** The natural
test — write a duplicate to disk, reopen, check the winner — PASSES against unfixed code: a
rebuild inserts canonical files in sorted-glob (ULID) order, and a plain table scan returns
rows in that same (rowid) order, so first-match already happens to return the lowest ULID.
That would be false red-first evidence. The real inversion is live-minting the HIGHER id
first (an ordinary, real-time ULID) and then ``upsert_entity``-ing a hand-made LOWER id in
the SAME process afterwards (its row lands with a LATER rowid despite its ULID sorting
earlier) — unfixed code's un-ordered scan then returns the higher one first.
"""

from __future__ import annotations

from sidegraph.schema import Descriptor, Entity, EntityKind
from sidegraph.store import Store

# Lexicographically below any real ULID (Crockford base32, 26 chars) minted "now" -- stands in
# for a duplicate that arrived from an earlier-timestamped branch, merged in later.
_HAND_MADE_LOWER_ID = "00000000000000000000000000"


def test_find_abstract_entity_returns_the_lowest_id_under_a_duplicate(tmp_path):
    store = Store(tmp_path / "s")

    higher = store.get_or_create_abstract_entity("tag:dup")
    assert higher.entity_id > _HAND_MADE_LOWER_ID  # sanity: a real ULID sorts above our stand-in

    lower = Entity(
        entity_id=_HAND_MADE_LOWER_ID, canonical_name="tag:dup", kind=EntityKind.ABSTRACT
    )
    store.upsert_entity(lower)

    found = store.find_abstract_entity("tag:dup")
    assert found is not None
    assert found.entity_id == _HAND_MADE_LOWER_ID, (
        f"expected the LOWEST id ({_HAND_MADE_LOWER_ID!r}) to win, got {found.entity_id!r}"
    )

    # get_or_create's own inline lookup must agree with the read path (D4's whole point --
    # otherwise the create path and the read path could disagree about which duplicate wins).
    via_get_or_create = store.get_or_create_abstract_entity("tag:dup")
    assert via_get_or_create.entity_id == _HAND_MADE_LOWER_ID

    # Stable across a close-and-reopen (index rebuilt fresh from the canonical files).
    reopened = Store(tmp_path / "s")
    try:
        assert reopened.find_abstract_entity("tag:dup").entity_id == _HAND_MADE_LOWER_ID
    finally:
        reopened.close()


def test_find_entity_returns_the_lowest_id_under_a_concrete_duplicate(tmp_path):
    store = Store(tmp_path / "s")
    descriptor = Descriptor(name="widget.Frobnicator", file_path="src/widget.py")

    higher = store.get_or_create_entity(descriptor)
    assert higher.entity_id > _HAND_MADE_LOWER_ID

    lower = Entity(
        entity_id=_HAND_MADE_LOWER_ID,
        canonical_name=descriptor.name,
        kind=EntityKind.CONCRETE,
        descriptor=descriptor,
    )
    store.upsert_entity(lower)

    found = store.find_entity(descriptor.name, descriptor.file_path)
    assert found is not None
    assert found.entity_id == _HAND_MADE_LOWER_ID, (
        f"expected the LOWEST id ({_HAND_MADE_LOWER_ID!r}) to win, got {found.entity_id!r}"
    )

    reopened = Store(tmp_path / "s")
    try:
        refound = reopened.find_entity(descriptor.name, descriptor.file_path)
        assert refound is not None
        assert refound.entity_id == _HAND_MADE_LOWER_ID
    finally:
        reopened.close()
