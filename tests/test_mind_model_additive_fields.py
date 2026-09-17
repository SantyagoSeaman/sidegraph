"""Recovered FR1.4 fields (mind-model layer, §2/§3): tags via existing abstract-entity
machinery (no new store code — this proves it), plus Decision.layer and
AnchorBinding.relation round-tripping through the store unchanged."""

from __future__ import annotations

from datetime import UTC, datetime

from sidegraph.schema import AnchorBinding, Decision, DecisionKind, Entity, EntityKind, Provenance
from sidegraph.store import Store


def _decision(**overrides) -> Decision:
    base = dict(
        title="Use SQLite for the store",
        kind=DecisionKind.ADR,
        context="Need a repo-committable, serverless store.",
        choice="SQLite via stdlib sqlite3.",
        valid_from=datetime.now(UTC),
        provenance=Provenance(source="manual"),
    )
    base.update(overrides)
    return Decision(**base)


def test_tag_entity_and_binding_round_trip(tmp_path) -> None:
    """`tag:<slug>` is just a durable abstract entity (get-or-create), exactly like
    `initiative:<x>` / `community:<id>` — no Domain machinery, no new store code."""
    s = Store(tmp_path / "t.db")
    tag = s.get_or_create_abstract_entity("tag:security")
    assert tag.kind == EntityKind.ABSTRACT

    d = s.add_decision(_decision())
    s.add_binding(AnchorBinding(record_id=d.id, entity_id=tag.entity_id, tier=0))

    bindings = s.bindings_for_entity(tag.entity_id)
    assert len(bindings) == 1 and bindings[0].record_id == d.id

    valid = s.valid_decisions_for_entity(tag.entity_id)
    assert [v.id for v in valid] == [d.id]

    # get-or-create is idempotent: re-tagging another decision reuses the same entity
    same_tag = s.get_or_create_abstract_entity("tag:security")
    assert same_tag.entity_id == tag.entity_id


def test_decision_layer_round_trips_through_store(tmp_path) -> None:
    s = Store(tmp_path / "t.db")
    d = s.add_decision(_decision(layer="business"))
    assert s.get_decision(d.id).layer == "business"

    d2 = s.add_decision(_decision(title="no layer set"))
    assert s.get_decision(d2.id).layer is None  # optional, default None


def test_provenance_commit_round_trips_through_store(tmp_path) -> None:
    """Staleness machinery D1: ``Provenance.commit`` is purely additive (no SCHEMA_VERSION
    bump, same ``seed_anchors`` precedent this module's other tests exercise) — a decision
    written with it round-trips, and one written without it (the pre-wave shape) loads with
    ``None`` rather than failing."""
    s = Store(tmp_path / "t.db")
    with_commit = s.add_decision(
        _decision(
            title="with commit",
            provenance=Provenance(source="agent", commit="abc123def456"),
        )
    )
    assert s.get_decision(with_commit.id).provenance.commit == "abc123def456"

    without_commit = s.add_decision(_decision(title="without commit"))
    assert s.get_decision(without_commit.id).provenance.commit is None


def test_provenance_commit_absent_key_loads_as_none(tmp_path) -> None:
    """A canonical file written by pre-wave code has no ``commit`` key in its ``provenance``
    object at all (not even ``null``) -- Pydantic's default must still fill in ``None``,
    the same tolerant-reload guarantee ``seed_anchors`` documents in schema.py."""
    import json

    s = Store(tmp_path / "t.db")
    d = s.add_decision(_decision(title="pre-wave shape"))
    path = tmp_path / "t.db" / "decisions" / f"{d.id}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    del payload["provenance"]["commit"]
    path.write_text(json.dumps(payload), encoding="utf-8")

    reloaded = Store(tmp_path / "t.db")
    assert reloaded.get_decision(d.id).provenance.commit is None


def test_binding_relation_round_trips_through_store(tmp_path) -> None:
    s = Store(tmp_path / "t.db")
    e = s.upsert_entity(Entity(canonical_name="sidegraph.store"))
    d = s.add_decision(_decision())

    s.add_binding(AnchorBinding(record_id=d.id, entity_id=e.entity_id, tier=1))
    default_binding = s.bindings_for_record(d.id)[0]
    assert default_binding.relation == "affects"  # additive default

    e2 = s.upsert_entity(Entity(canonical_name="sidegraph.other"))
    s.add_binding(
        AnchorBinding(record_id=d.id, entity_id=e2.entity_id, tier=2, relation="deprecates")
    )
    relations = {b.entity_id: b.relation for b in s.bindings_for_record(d.id)}
    assert relations[e2.entity_id] == "deprecates"
