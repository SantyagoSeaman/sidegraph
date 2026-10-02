"""``bindings_for_entity`` is an indexed lookup, on every kind of index.

The table's primary key is ``(record_id, entity_id)``, so a lookup by ``entity_id`` alone
scanned the whole table, and ``build_toc`` makes one per entity in a domain's communities.
Measured on a real 15-domain store that scan was 0.115 s of a 0.149 s TOC build.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sidegraph.schema import AnchorBinding, Decision, DecisionKind, Provenance
from sidegraph.store import Store

_INDEX = "idx_anchor_bindings_entity"


def _indexes(store: Store) -> set[str]:
    rows = store._conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'anchor_bindings'"
    ).fetchall()
    return {r["name"] for r in rows}


def _entity_lookup_plan(store: Store) -> str:
    rows = store._conn.execute(
        "EXPLAIN QUERY PLAN SELECT data FROM anchor_bindings WHERE entity_id = ?", ("x",)
    ).fetchall()
    return " | ".join(r["detail"] for r in rows)


def _bound_store(path) -> Store:
    store = Store(path)
    decision = store.add_decision(
        Decision(
            title="t",
            kind=DecisionKind.GOTCHA,
            context="c",
            choice="ch",
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual"),
        )
    )
    entity = store.get_or_create_abstract_entity("domain:payments")
    store.add_binding(
        AnchorBinding(record_id=decision.id, entity_id=entity.entity_id, tier=1, status="live")
    )
    return store


def test_a_fresh_store_indexes_bindings_by_entity(tmp_path):
    store = Store(tmp_path / "s")
    assert _INDEX in _indexes(store)
    assert _INDEX in _entity_lookup_plan(store)
    assert "SCAN" not in _entity_lookup_plan(store)


def test_an_existing_store_gets_the_index_on_its_next_open(tmp_path):
    """The statement runs on every open, so a store built before it existed is healed by
    simply being opened, with no reload and no migration."""
    path = tmp_path / "s"
    store = _bound_store(path)
    with store._mutation():
        store._conn.execute(f"DROP INDEX {_INDEX}")
    assert _INDEX not in _indexes(store)
    store.close()

    reopened = Store(path)
    try:
        assert _INDEX in _indexes(reopened)
        assert (
            len(
                reopened.bindings_for_entity(
                    reopened.find_abstract_entity("domain:payments").entity_id
                )
            )
            == 1
        )
    finally:
        reopened.close()


def test_the_index_survives_a_rebuild_from_the_canonical_files(tmp_path):
    """The rebuild DROPs ``anchor_bindings`` (which takes its indexes with it) and recreates
    the table from ``_SCHEMA_STATEMENTS``, so the index has to be in that list to come back."""
    store = _bound_store(tmp_path / "s")
    digest, _stats = store._compute_canonical_digest()
    store._reload_index_from_canonical(digest)
    assert _INDEX in _indexes(store)
    assert _INDEX in _entity_lookup_plan(store)
    store.close()
