from concurrent.futures import ThreadPoolExecutor

from sidegraph.schema import AnchorBinding, Descriptor, Entity, EntityKind
from sidegraph.store import Store


def test_find_entity_dedups_by_canonical_name_and_file(tmp_path):
    s = Store(tmp_path / "t.db")
    e = s.upsert_entity(
        Entity(
            canonical_name="BitfinexAdapter",
            descriptor=Descriptor(name="BitfinexAdapter", file_path="adapters/bitfinex.py"),
        )
    )
    # decorated / different-case query resolves to the same entity
    found = s.find_entity("bitfinexadapter()", "adapters/bitfinex.py")
    assert found is not None and found.entity_id == e.entity_id
    # different file -> not the same entity
    assert s.find_entity("BitfinexAdapter", "other.py") is None
    # no file on the stored entity is fine when query file is None
    assert s.find_entity("nope", None) is None


def test_get_or_create_abstract_entity_is_reused(tmp_path):
    s = Store(tmp_path / "t.db")
    a = s.get_or_create_abstract_entity("community:18")
    b = s.get_or_create_abstract_entity("community:18")
    assert a.entity_id == b.entity_id
    assert a.kind == EntityKind.ABSTRACT


def test_get_or_create_abstract_entity_no_toctou_duplicate_under_race(tmp_path):
    """N threads race the same canonical_name; exactly one abstract entity must land.

    Regression for the TOCTOU noted in the T1 Fable review: the old implementation released
    the lock between the existence check and the create, so two threads could each observe
    "not found" and both insert a row for the same ``canonical_name``.
    """
    s = Store(tmp_path / "t.db")
    name = "community:race"

    def worker() -> str:
        return s.get_or_create_abstract_entity(name).entity_id

    with ThreadPoolExecutor(max_workers=16) as pool:
        ids = list(pool.map(lambda _: worker(), range(50)))

    assert len(set(ids)) == 1  # every thread converged on the same entity_id

    with s._lock:
        rows = s._conn.execute(
            "SELECT data FROM entities WHERE canonical_name = ?", (name,)
        ).fetchall()
    assert len(rows) == 1  # exactly one row, not one per racing thread


def test_bindings_for_record(tmp_path):
    s = Store(tmp_path / "t.db")
    e = s.upsert_entity(Entity(canonical_name="x"))
    from datetime import UTC, datetime

    from sidegraph.schema import Decision, DecisionKind, Provenance

    d = s.add_decision(
        Decision(
            title="t",
            kind=DecisionKind.ADR,
            context="c",
            choice="ch",
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual"),
        )
    )
    s.add_binding(AnchorBinding(record_id=d.id, entity_id=e.entity_id, tier=2))
    got = s.bindings_for_record(d.id)
    assert len(got) == 1 and got[0].entity_id == e.entity_id
