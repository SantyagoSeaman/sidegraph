from datetime import UTC, datetime

import pytest

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


def _bound_decision(store, entity_id, title, kind, scope=None):
    d = Decision(
        title=title,
        kind=kind,
        context="c",
        choice=f"do {title}",
        scope=scope or Scope.REPO,
        # accepted: these tests exercise budget/evidence rendering, not ratification
        status=DecisionStatus.ACCEPTED,
        valid_from=datetime.now(UTC),
        provenance=Provenance(source="manual"),
    )
    store._write_decision(d)
    store._conn.commit()
    store.add_binding(AnchorBinding(record_id=d.id, entity_id=entity_id, tier=2, status="live"))
    return d


def _fact(
    statement,
    source="test",
    supports=None,
    supersedes=None,
    status=DecisionStatus.PROPOSED,
    valid_from=None,
):
    return Fact(
        statement=statement,
        source=source,
        supports=supports or [],
        supersedes=supersedes,
        status=status,
        valid_from=valid_from or datetime.now(UTC),
        provenance=Provenance(source="manual"),
    )


@pytest.fixture
def populated(tmp_path):
    """A store with one entity and one ACCEPTED gotcha bound to it."""
    store = Store(tmp_path / "t.db")
    entity = store.upsert_entity(Entity(canonical_name="Seed"))
    _bound_decision(store, entity.entity_id, "gotcha1", DecisionKind.GOTCHA)
    return store, entity


@pytest.fixture
def populated_many_mistakes(tmp_path):
    """A store with one entity and enough ACCEPTED gotchas bound to it to exceed a
    memory_chars=600 budget on their own (proving budget pressure exists BEFORE any facts
    are added — see test_facts_never_displace_mistakes)."""
    store = Store(tmp_path / "t.db")
    entity = store.upsert_entity(Entity(canonical_name="Seed"))
    for i in range(40):
        _bound_decision(store, entity.entity_id, f"gotcha{i}", DecisionKind.GOTCHA)
    return store, entity


def test_inline_evidence_renders_under_decision(populated):
    store, entity = populated  # helper: entity + ACCEPTED gotcha bound to it
    d = store.valid_decisions_for_entity(entity.entity_id)[0]
    store.add_fact(
        _fact(supports=[d.id], statement="observed 3x slowdown", status=DecisionStatus.ACCEPTED)
    )
    ctx = rank_decisions([entity], [], [], store, RetrievalBudget())
    text = ctx.render()
    assert "evidence: observed 3x slowdown" in text
    # Adjacency, not mere presence: the evidence line must render on the line DIRECTLY
    # AFTER its decision's line, not merely somewhere in the rendered text.
    lines = text.splitlines()
    decision_idx = next(
        i for i, line in enumerate(lines) if "gotcha1" in line and not line.startswith("  ")
    )
    assert "evidence: observed 3x slowdown" in lines[decision_idx + 1]


def test_superseded_fact_never_renders_inline(populated):
    store, entity = populated
    d = store.valid_decisions_for_entity(entity.entity_id)[0]
    old = store.add_fact(
        _fact(supports=[d.id], statement="stale fact", status=DecisionStatus.ACCEPTED)
    )
    store.add_fact(
        _fact(
            supports=[d.id],
            statement="fresh fact",
            supersedes=old.id,
            status=DecisionStatus.ACCEPTED,
        )
    )
    text = rank_decisions([entity], [], [], store, RetrievalBudget()).render()
    assert "stale fact" not in text and "fresh fact" in text


def test_standalone_fact_surfaces_in_known_facts_bucket(populated):
    store, entity = populated
    f = store.add_fact(_fact(statement="vendor limit is 100 rps", status=DecisionStatus.ACCEPTED))
    store.add_binding(AnchorBinding(record_id=f.id, entity_id=entity.entity_id, tier=2))
    text = rank_decisions([entity], [], [], store, RetrievalBudget()).render()
    assert "## Known facts" in text
    assert "vendor limit is 100 rps" in text


def test_proposed_fact_tagged_unratified(populated):
    store, entity = populated
    f = store.add_fact(_fact(statement="tentative"))
    store.add_binding(AnchorBinding(record_id=f.id, entity_id=entity.entity_id, tier=2))
    text = rank_decisions([entity], [], [], store, RetrievalBudget()).render()
    assert "[unratified]" in text and "tentative" in text


def test_fact_rendered_inline_not_repeated_in_bucket(populated):
    store, entity = populated
    d = store.valid_decisions_for_entity(entity.entity_id)[0]
    f = store.add_fact(
        _fact(supports=[d.id], statement="once only", status=DecisionStatus.ACCEPTED)
    )
    store.add_binding(AnchorBinding(record_id=f.id, entity_id=entity.entity_id, tier=2))
    text = rank_decisions([entity], [], [], store, RetrievalBudget()).render()
    assert text.count("once only") == 1


def test_fact_supporting_two_rendered_decisions_renders_once(populated):
    # Self-review follow-up: the brief's inline-evidence pseudocode has no de-dup guard of
    # its own — a fact whose `supports` lists TWO decisions that both land in the output
    # (here: two mistakes bound to the same seed) must still render only once, not once per
    # supported decision.
    store, entity = populated
    d1 = store.valid_decisions_for_entity(entity.entity_id)[0]
    d2 = _bound_decision(store, entity.entity_id, "gotcha2", DecisionKind.GOTCHA)
    store.add_fact(
        _fact(
            supports=[d1.id, d2.id],
            statement="shared evidence",
            status=DecisionStatus.ACCEPTED,
        )
    )
    text = rank_decisions([entity], [], [], store, RetrievalBudget()).render()
    assert text.count("shared evidence") == 1


def test_facts_only_context_renders_known_facts_not_empty_fallback(tmp_path):
    # MINOR follow-up: a context with a standalone fact and NO decisions at all must still
    # render the Known-facts bucket, never fall through to the "No context found." fallback.
    store = Store(tmp_path / "t.db")
    entity = store.upsert_entity(Entity(canonical_name="Seed"))
    f = store.add_fact(_fact(statement="facts-only context", status=DecisionStatus.ACCEPTED))
    store.add_binding(AnchorBinding(record_id=f.id, entity_id=entity.entity_id, tier=2))
    text = rank_decisions([entity], [], [], store, RetrievalBudget()).render()
    assert "## Known facts" in text
    assert "facts-only context" in text
    assert "No context found." not in text


def test_standalone_facts_never_displace_mistakes(populated_many_mistakes):
    # Standalone facts (bound directly to the entity, supporting no decision) go through
    # the Known-facts bucket, which runs strictly after every decision bucket — helper:
    # enough ACCEPTED gotchas on the seed entity to exhaust memory_chars, plus 10 anchored
    # standalone facts. Mistake DECISION-LINE count must be identical with and without them.
    store, entity = populated_many_mistakes
    budget = RetrievalBudget(memory_chars=600)
    ctx = rank_decisions([entity], [], [], store, budget)
    baseline_mistakes = len(ctx.mistakes)
    assert baseline_mistakes < 40  # budget pressure genuinely exists before facts exist
    for i in range(10):
        f = store.add_fact(_fact(statement=f"filler fact {i}", status=DecisionStatus.ACCEPTED))
        store.add_binding(AnchorBinding(record_id=f.id, entity_id=entity.entity_id, tier=2))
    ctx2 = rank_decisions([entity], [], [], store, budget)
    assert len(ctx2.mistakes) == baseline_mistakes


def test_inline_evidence_never_displaces_mistakes(populated_many_mistakes):
    # Spec-level ruling (plan amended in commit f496b39): facts that DO support the mistake
    # decisions themselves — rendering as inline evidence inside bucket A — must not reduce
    # how many mistake DECISION lines fit under a tight budget either. Two-phase bucket A
    # (see rank_decisions' docstring) places every mistake decision line before any evidence
    # is even considered, so evidence for an early mistake can never starve a later mistake's
    # own decision line of budget.
    store, entity = populated_many_mistakes
    budget = RetrievalBudget(memory_chars=600)

    def _decision_line_count(ctx):
        return sum(1 for line in ctx.mistakes if not line.startswith("  "))

    ctx = rank_decisions([entity], [], [], store, budget)
    baseline_decision_lines = _decision_line_count(ctx)
    assert baseline_decision_lines < 40  # budget pressure exists before any fact exists

    # Attach a supporting fact to EVERY mistake, not just an arbitrary slice:
    # `store.valid_decisions_for_entity` returns SQLite's natural (insertion) order, which
    # is the OLDEST-first — the exact opposite of `rank_decisions`' recency-descending walk
    # — so slicing the first N here would only ever hit decisions budget already drops,
    # exercising nothing. Covering every mistake guarantees overlap with whichever subset
    # actually gets PLACED under the tight budget, so the old immediate-inline bug (an
    # early-placed mistake's evidence eating the budget a later-placed mistake's decision
    # line needed) would have shown up here before the two-phase fix.
    mistakes = store.valid_decisions_for_entity(entity.entity_id)
    for i, d in enumerate(mistakes):
        store.add_fact(
            _fact(supports=[d.id], statement=f"evidence {i}", status=DecisionStatus.ACCEPTED)
        )

    ctx2 = rank_decisions([entity], [], [], store, budget)
    assert _decision_line_count(ctx2) == baseline_decision_lines
