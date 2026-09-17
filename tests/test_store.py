"""Store write-path invariants — these ARE the contract (see CLAUDE.md, docs/data-model.md)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from sidegraph import (
    SCHEMA_VERSION,
    AnchorBinding,
    Decision,
    DecisionKind,
    DecisionStatus,
    Entity,
    Provenance,
    Store,
)


@pytest.fixture
def store(tmp_path) -> Store:
    with Store(tmp_path / "test.db") as s:
        yield s


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


def test_store_stamps_schema_version(store: Store) -> None:
    assert store.schema_version == SCHEMA_VERSION


def test_add_and_get_decision(store: Store) -> None:
    d = store.add_decision(_decision())
    assert store.get_decision(d.id) is not None
    assert store.get_decision(d.id).title == "Use SQLite for the store"


def test_valid_to_before_valid_from_rejected() -> None:
    now = datetime.now(UTC)
    with pytest.raises(ValueError, match="valid_to must be >= valid_from"):
        _decision(valid_from=now, valid_to=now - timedelta(days=1))


def test_supersede_closes_predecessor(store: Store) -> None:
    old = store.add_decision(_decision())
    new = store.add_decision(_decision(title="Switch to Kùzu", supersedes=old.id))

    reloaded_old = store.get_decision(old.id)
    assert reloaded_old.status == DecisionStatus.SUPERSEDED
    assert reloaded_old.valid_to is not None  # a superseded decision must be closed
    assert store.get_decision(new.id).supersedes == old.id


def test_supersede_unknown_decision_rejected(store: Store) -> None:
    with pytest.raises(ValueError, match="unknown decision"):
        store.add_decision(_decision(supersedes="01JUNKULIDDOESNOTEXIST00"))


def test_add_decision_existing_id_rejected(store: Store) -> None:
    """append-only: add_decision must never silently rewrite an existing row (that hole
    would erase history through a method documented as append-only). Ratify/supersede
    flip status via _write_decision directly — nothing legitimate re-adds the same id."""
    d = store.add_decision(_decision())
    with pytest.raises(ValueError, match="already exists"):
        store.add_decision(_decision(id=d.id, title="Rewritten in place"))
    # the original row must be untouched
    assert store.get_decision(d.id).title == "Use SQLite for the store"


def test_binding_requires_existing_entity(store: Store) -> None:
    d = store.add_decision(_decision())
    with pytest.raises(ValueError, match="unknown entity"):
        store.add_binding(AnchorBinding(record_id=d.id, entity_id="nope", tier=1))


def test_binding_roundtrip(store: Store) -> None:
    e = store.upsert_entity(Entity(canonical_name="sidegraph.store"))
    d = store.add_decision(_decision())
    store.add_binding(AnchorBinding(record_id=d.id, entity_id=e.entity_id, tier=1))

    bindings = store.bindings_for_entity(e.entity_id)
    assert len(bindings) == 1
    assert bindings[0].record_id == d.id
