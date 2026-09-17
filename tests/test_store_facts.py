"""Store write-path invariants for facts — the contract, tests first.
see design/superpowers/specs/2026-07-10-facts-layer-design.md"""

import json
from datetime import UTC, datetime

import pytest

from sidegraph.schema import (
    AnchorBinding,
    Decision,
    DecisionKind,
    DecisionStatus,
    Entity,
    Fact,
    Provenance,
)
from sidegraph.store import Store

NOW = datetime.now(UTC)


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "s") as s:
        yield s


def _decision(**kw):
    base = dict(
        title="use httpx",
        kind=DecisionKind.ADR,
        context="c",
        choice="ch",
        valid_from=NOW,
        provenance=Provenance(source="test"),
    )
    base.update(kw)
    return Decision(**base)


def _fact(**kw):
    base = dict(
        statement="httpx has no built-in retry",
        source="httpx docs",
        valid_from=NOW,
        provenance=Provenance(source="test"),
    )
    base.update(kw)
    return Fact(**base)


def test_add_and_get_fact_roundtrip(store, tmp_path):
    f = store.add_fact(_fact())
    got = store.get_fact(f.id)
    assert got is not None and got.statement == f.statement
    # canonical file exists and matches the model dump
    path = tmp_path / "s" / "facts" / f"{f.id}.json"
    assert path.is_file()
    assert json.loads(path.read_text())["statement"] == f.statement


def test_add_fact_rejects_unknown_supports(store):
    with pytest.raises(ValueError, match="supports"):
        store.add_fact(_fact(supports=["01UNKNOWNDECISIONIDXXXXXXX"]))


def test_add_fact_accepts_existing_supports(store):
    d = store.add_decision(_decision())
    f = store.add_fact(_fact(supports=[d.id]))
    assert store.facts_for_decision(d.id)[0].id == f.id


def test_fact_supersession_closes_predecessor_append_only(store):
    old = store.add_fact(_fact(status=DecisionStatus.ACCEPTED))
    new = store.add_fact(
        _fact(
            statement="httpx ships retry via transport since 0.28",
            supersedes=old.id,
            status=DecisionStatus.ACCEPTED,
        )
    )
    closed = store.get_fact(old.id)
    assert closed.status == DecisionStatus.SUPERSEDED
    assert closed.valid_to is not None
    assert store.get_fact(new.id).supersedes == old.id


def test_add_fact_duplicate_id_raises(store):
    f = store.add_fact(_fact())
    with pytest.raises(ValueError):
        store.add_fact(f)


def test_binding_to_fact_allowed_and_queryable(store):
    e = store.upsert_entity(Entity(canonical_name="sync.py"))
    f = store.add_fact(_fact())
    store.add_binding(AnchorBinding(record_id=f.id, entity_id=e.entity_id, tier=2))
    got = store.valid_facts_for_entity(e.entity_id)
    assert [x.id for x in got] == [f.id]


def test_binding_to_unknown_record_raises(store):
    e = store.upsert_entity(Entity(canonical_name="sync.py"))
    with pytest.raises(ValueError):
        store.add_binding(
            AnchorBinding(record_id="01NOSUCHRECORDXXXXXXXXXXXX", entity_id=e.entity_id, tier=2)
        )


def test_facts_survive_index_reload(store, tmp_path):
    f = store.add_fact(_fact())
    (tmp_path / "s" / "index.db").unlink()
    with Store(tmp_path / "s") as reopened:
        assert reopened.get_fact(f.id) is not None


def test_pending_ratification_counts_decomposition(store):
    assert store.pending_ratification_counts() == (0, 0, 0)
    d = store.add_decision(_decision(status=DecisionStatus.PROPOSED))
    nested = _fact(supports=[d.id], status=DecisionStatus.PROPOSED)
    store.add_fact(nested)
    standalone = _fact(status=DecisionStatus.PROPOSED)
    store.add_fact(standalone)
    accepted = store.add_decision(_decision(status=DecisionStatus.ACCEPTED))
    assert accepted.status == DecisionStatus.ACCEPTED
    # nested fact rides d's cascade -- counted 0 times, not 2
    assert store.pending_ratification_counts() == (1, 1, 0)
