"""Pins the one fact D6's whole branch rests on (design D6, spec §4 item 7's retrieval-level
pinning): a supports-only fact whose decision has gone SUPERSEDED must not appear in a
rendered decision-memory render, at all -- not even as inline evidence.

This is deliberately a ``retrieval.rank_decisions`` test, not a ``doctor``-only one: the
claim lives in ``retrieval.py`` (a superseded decision reaches a render only through the
one-liner at ``retrieval.py``'s "tried, reverted" line, which passes ``evidence=False`` --
its own comment states the rule outright, "evidence to buckets A-D only"), and a doctor-only
test would never notice this changing. ``rank_decisions`` is the exact function
``get_task_context`` composes for its decision-memory half (``ctx = rank_decisions(...)``) --
calling it directly here, the same way ``tests/test_retrieval_facts.py``'s sibling
``test_superseded_fact_never_renders_inline`` already does, exercises the identical code path
without needing a ``GraphifyReader`` fixture just to resolve seeds (seed resolution has
nothing to do with what this test is pinning: whether a fact can ride a TERMINAL decision's
evidence).
"""

from __future__ import annotations

from datetime import UTC, datetime

from sidegraph.retrieval import RetrievalBudget, rank_decisions
from sidegraph.schema import (
    AnchorBinding,
    Decision,
    DecisionKind,
    DecisionStatus,
    Entity,
    Fact,
    Provenance,
    Scope,
)
from sidegraph.store import Store


def _bound_decision(store: Store, entity_id: str, title: str) -> Decision:
    d = Decision(
        title=title,
        kind=DecisionKind.GOTCHA,
        context="c",
        choice=f"do {title}",
        scope=Scope.REPO,
        status=DecisionStatus.ACCEPTED,
        valid_from=datetime.now(UTC),
        provenance=Provenance(source="manual"),
    )
    # The public write path, not `_write_decision` + a bare `_conn.commit()` (a fixture idiom
    # this repo carries in several older test files). This wave's whole subject is that every
    # write goes through the guarded path, so its own new test must not be the one bypassing
    # it — PR #22 review. `add_decision` is usable here because nothing below supersedes.
    store.add_decision(d)
    store.add_binding(AnchorBinding(record_id=d.id, entity_id=entity_id, tier=2, status="live"))
    return d


def _standalone_fact(statement: str, supports: list[str]) -> Fact:
    return Fact(
        statement=statement,
        source="test",
        supports=supports,
        status=DecisionStatus.ACCEPTED,
        valid_from=datetime.now(UTC),
        provenance=Provenance(source="manual"),
    )


def test_supports_only_fact_of_a_superseded_decision_never_renders(tmp_path):
    store = Store(tmp_path / "t.db")
    entity = store.upsert_entity(Entity(canonical_name="Seed"))
    old = _bound_decision(store, entity.entity_id, "old gotcha")

    # A standalone fact -- NO anchor of its own, reachable (at write time) only through
    # `old`'s `supports` link. Exactly the shape design D8 tightens the write gate around.
    fact = store.add_fact(_standalone_fact("evidence for the old decision", supports=[old.id]))

    # Confirm it DOES render while `old` is still live -- the baseline this test's real
    # assertion (after superseding `old`) needs to be meaningfully different from.
    text_before = rank_decisions([entity], [], [], store, RetrievalBudget()).render()
    assert "evidence for the old decision" in text_before

    # Now supersede `old` -- ordinary supersession, closing it (status -> superseded).
    new = Decision(
        title="new gotcha",
        kind=DecisionKind.GOTCHA,
        context="c",
        choice="do new gotcha",
        scope=Scope.REPO,
        status=DecisionStatus.ACCEPTED,
        valid_from=datetime.now(UTC),
        supersedes=old.id,
        provenance=Provenance(source="manual"),
    )
    store.add_decision(new)
    store.add_binding(AnchorBinding(record_id=new.id, entity_id=entity.entity_id, tier=2))
    assert store.get_decision(old.id).status == DecisionStatus.SUPERSEDED

    text_after = rank_decisions([entity], [], [], store, RetrievalBudget()).render()
    assert fact.statement not in text_after
