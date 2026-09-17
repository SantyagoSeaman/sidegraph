"""A store with a merged duplicate logical identity still opens (design D2's whole point,
entity-identity-uniqueness spec).

D2 REJECTS a UNIQUE index on logical identity: file-per-record exists so two branches merge
without a git conflict. Two branches that each mint ``tag:foo`` produce two ULIDs -> two
different filenames -> git merges them cleanly, and the canonical store then LEGALLY holds a
logical duplicate. A UNIQUE index's rebuild (``Store.__init__``) would raise ``UNIQUE
constraint failed`` on exactly that merge -- the store could never be opened again. This test
is the one that would have caught that design before it shipped; it exists so nobody
re-proposes it.

**Declared exception** (spec §4 item 8's first bullet): green before AND after this wave --
no UNIQUE index exists in unfixed code either. Its value is guarding the future, not proving
a bug. Not red-first evidence.
"""

from __future__ import annotations

import json

from sidegraph.store import Store

_ID_A = "01AAAAAAAAAAAAAAAAAAAAAAAA"
_ID_B = "01BBBBBBBBBBBBBBBBBBBBBBBB"


def _entity_payload(entity_id: str, canonical_name: str) -> dict:
    return {
        "entity_id": entity_id,
        "canonical_name": canonical_name,
        "kind": "abstract",
        "descriptor": None,
    }


def test_store_opens_reloads_and_answers_lookups_with_a_merged_duplicate(tmp_path):
    db = tmp_path / "s"
    # Bootstrap the canonical layout (directories, schema, an initial digest over zero
    # entities), then close -- the two duplicate files below are written directly to disk,
    # bypassing the write path entirely, standing in for a `git merge` landing both branches'
    # independently-minted files in the same working tree.
    Store(db).close()

    (db / "entities" / f"{_ID_A}.json").write_text(
        json.dumps(_entity_payload(_ID_A, "tag:merged-dup"))
    )
    (db / "entities" / f"{_ID_B}.json").write_text(
        json.dumps(_entity_payload(_ID_B, "tag:merged-dup"))
    )

    # Must not raise: the digest no longer matches the stamped one (two new files appeared
    # on disk outside the write path), so opening triggers a full reload straight over the
    # duplicate -- this is exactly the path a UNIQUE index would have raised inside.
    store = Store(db)
    try:
        found = store.find_abstract_entity("tag:merged-dup")
        assert found is not None
        assert found.entity_id in (_ID_A, _ID_B)

        # A second open (nothing changed, digest matches -- the fast path) must also still
        # answer, not just the reload path.
        again = Store(db)
        try:
            assert again.find_abstract_entity("tag:merged-dup") is not None
        finally:
            again.close()
    finally:
        store.close()
