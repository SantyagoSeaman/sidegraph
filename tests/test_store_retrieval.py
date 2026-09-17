from datetime import UTC, datetime, timedelta

from sidegraph.schema import (
    AnchorBinding,
    Decision,
    DecisionKind,
    DecisionStatus,
    Descriptor,
    Entity,
    Initiative,
    Provenance,
    Scope,
)
from sidegraph.store import Store


def _dec(
    store,
    title,
    kind=DecisionKind.ADR,
    scope=Scope.REPO,
    status=DecisionStatus.ACCEPTED,
    valid_to=None,
    valid_from=None,
):
    d = Decision(
        title=title,
        kind=kind,
        status=status,
        context="c",
        choice="ch",
        scope=scope,
        valid_from=valid_from or datetime.now(UTC),
        valid_to=valid_to,
        provenance=Provenance(source="manual"),
    )
    store._write_decision(d)  # persist directly (bypasses supersede handling)
    store._conn.commit()
    return d


def test_valid_decisions_for_entity_filters(tmp_path):
    s = Store(tmp_path / "t.db")
    e = s.upsert_entity(
        Entity(canonical_name="X", descriptor=Descriptor(name="X", file_path="x.py"))
    )
    live = _dec(s, "live adr")
    gone = _dec(
        s,
        "expired",
        valid_from=datetime.now(UTC) - timedelta(days=2),
        valid_to=datetime.now(UTC) - timedelta(days=1),
    )
    sup = _dec(s, "superseded", status=DecisionStatus.SUPERSEDED)
    s.add_binding(AnchorBinding(record_id=live.id, entity_id=e.entity_id, tier=2, status="live"))
    s.add_binding(AnchorBinding(record_id=gone.id, entity_id=e.entity_id, tier=2, status="live"))
    s.add_binding(AnchorBinding(record_id=sup.id, entity_id=e.entity_id, tier=2, status="live"))
    ids = {d.id for d in s.valid_decisions_for_entity(e.entity_id)}
    assert ids == {live.id}  # expired + superseded excluded


def test_valid_decisions_skips_orphaned_binding(tmp_path):
    s = Store(tmp_path / "t.db")
    e = s.upsert_entity(Entity(canonical_name="X"))
    d = _dec(s, "adr")
    s.add_binding(AnchorBinding(record_id=d.id, entity_id=e.entity_id, tier=2, status="orphaned"))
    assert s.valid_decisions_for_entity(e.entity_id) == []


def test_superseded_for_entity(tmp_path):
    s = Store(tmp_path / "t.db")
    e = s.upsert_entity(Entity(canonical_name="X"))
    sup = _dec(s, "old", status=DecisionStatus.SUPERSEDED)
    s.add_binding(AnchorBinding(record_id=sup.id, entity_id=e.entity_id, tier=2, status="live"))
    assert [d.id for d in s.superseded_for_entity(e.entity_id)] == [sup.id]


def test_decisions_by_scope(tmp_path):
    s = Store(tmp_path / "t.db")
    g = _dec(s, "global rule", scope=Scope.GLOBAL)
    _dec(s, "repo rule", scope=Scope.REPO)
    ids = {d.id for d in s.decisions_by_scope(Scope.GLOBAL)}
    assert ids == {g.id}


def test_find_abstract_entity(tmp_path):
    s = Store(tmp_path / "t.db")
    a = s.get_or_create_abstract_entity("community:18")
    assert s.find_abstract_entity("community:18").entity_id == a.entity_id
    assert s.find_abstract_entity("community:99") is None


def test_iter_initiatives(tmp_path):
    s = Store(tmp_path / "t.db")
    s.upsert_initiative(Initiative(name="Metadata Platform"))
    names = [i.name for i in s.iter_initiatives()]
    assert names == ["Metadata Platform"]
