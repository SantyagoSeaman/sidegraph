"""Staleness machinery D4: ids in the retrieval render (detailed tier only), plus the one
standing supersede hint appended by ``TaskContext.render``.
# see design/superpowers/specs/2026-07-30-staleness-machinery-design.md (D4)
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from sidegraph.engine.reader import GraphifyReader
from sidegraph.retrieval import (
    _STANDING_SUPERSEDE_HINT,
    RetrievalBudget,
    Seed,
    _fmt_decision,
    get_task_context,
    query_decisions,
    rank_decisions,
)
from sidegraph.schema import (
    AnchorBinding,
    Decision,
    DecisionKind,
    DecisionStatus,
    Descriptor,
    Entity,
    Provenance,
    Scope,
)
from sidegraph.store import Store

FIXTURE = Path(__file__).parent / "fixtures" / "mini_graph.json"


def _bound_decision(store, entity_id, title, kind, scope=Scope.REPO, **overrides):
    base = dict(
        title=title,
        kind=kind,
        context="c",
        choice=f"do {title}",
        scope=scope,
        status=DecisionStatus.ACCEPTED,
        valid_from=datetime.now(UTC),
        provenance=Provenance(source="manual"),
    )
    base.update(overrides)
    d = Decision(**base)
    store._write_decision(d)
    store._conn.commit()
    store.add_binding(AnchorBinding(record_id=d.id, entity_id=entity_id, tier=2, status="live"))
    return d


# -- _fmt_decision: detailed carries the id, tight does not --------------------------------


def test_detailed_tier_line_carries_full_ulid_id_suffix():
    d = Decision(
        title="T",
        kind=DecisionKind.GOTCHA,
        context="c",
        choice="ch",
        valid_from=datetime.now(UTC),
        provenance=Provenance(source="agent"),
    )
    line = _fmt_decision(d, detailed=True)
    assert f"(id: {d.id})" in line


def test_tight_tier_line_carries_no_id():
    d = Decision(
        title="T",
        kind=DecisionKind.GOTCHA,
        context="c",
        choice="ch",
        valid_from=datetime.now(UTC),
        provenance=Provenance(source="agent"),
    )
    line = _fmt_decision(d, detailed=False)
    assert d.id not in line
    assert "(id:" not in line


# -- rank_decisions: mistakes/decisions (detailed) carry ids, related does not -------------


def test_rank_mistakes_and_direct_decisions_carry_ids_related_does_not(tmp_path):
    s = Store(tmp_path / "t.db")
    seed = s.upsert_entity(Entity(canonical_name="Seed"))
    peri = s.upsert_entity(Entity(canonical_name="Peri"))
    mistake = _bound_decision(s, seed.entity_id, "gotcha1", DecisionKind.GOTCHA)
    direct = _bound_decision(s, seed.entity_id, "adr1", DecisionKind.ADR)
    related = _bound_decision(s, peri.entity_id, "peri-adr", DecisionKind.ADR)

    ctx = rank_decisions([seed], [peri], [], s, RetrievalBudget())
    assert f"(id: {mistake.id})" in ctx.mistakes[0]
    assert f"(id: {direct.id})" in ctx.decisions[0]
    assert "(id:" not in ctx.related[0]
    assert related.id not in ctx.related[0]


def test_get_task_context_direct_seeded_records_gain_ids(tmp_path):
    """The target surface (design D4): get_task_context's direct-seeded records gain ids."""
    r = GraphifyReader(FIXTURE)
    s = Store(tmp_path / "t.db")
    e = s.upsert_entity(
        Entity(
            canonical_name="Trader",
            descriptor=Descriptor(name="Trader", file_path="trader/exec.py"),
        )
    )
    d = _bound_decision(s, e.entity_id, "adr1", DecisionKind.ADR)
    ctx = get_task_context([Seed(name="Trader", file_path="trader/exec.py")], s, r)
    assert any(f"(id: {d.id})" in line for line in ctx.decisions)


# -- degrade-before-drop keeps the id on the degraded (tight) line -------------------------


def test_degrade_path_keeps_id_on_tight_rendered_line(tmp_path):
    """A detailed line with maxed-out fields can itself exceed an explicit small budget;
    it degrades to the tight tier but keeps the id suffix (design D4 -- the budget-pressured
    session is exactly the one this affordance targets)."""
    s = Store(tmp_path / "t.db")
    seed = s.upsert_entity(Entity(canonical_name="Seed"))
    long_choice = "Long choice. " + ("word " * 300)
    d = _bound_decision(
        s,
        seed.entity_id,
        "big gotcha",
        DecisionKind.GOTCHA,
        choice=long_choice,
        rejected="Rejected. " + ("also " * 150),
        consequences="Consequence. " + ("cons " * 150),
    )
    # Budget large enough for a tight one-liner, far too small for the full detailed render.
    small_budget = RetrievalBudget(memory_chars=len(_fmt_decision(d, detailed=False)) + 60)
    ctx = rank_decisions([seed], [], [], s, small_budget)
    assert len(ctx.mistakes) == 1
    assert f"(id: {d.id})" in ctx.mistakes[0]
    assert "(rejected:" not in ctx.mistakes[0]  # degraded to the tight render


# -- standing hint: iff mistakes-or-decisions non-empty, present in query_decisions too ----


def test_hint_present_when_mistakes_non_empty(tmp_path):
    s = Store(tmp_path / "t.db")
    seed = s.upsert_entity(Entity(canonical_name="Seed"))
    _bound_decision(s, seed.entity_id, "gotcha1", DecisionKind.GOTCHA)
    ctx = rank_decisions([seed], [], [], s, RetrievalBudget())
    assert _STANDING_SUPERSEDE_HINT in ctx.render()


def test_hint_present_when_decisions_non_empty(tmp_path):
    s = Store(tmp_path / "t.db")
    seed = s.upsert_entity(Entity(canonical_name="Seed"))
    _bound_decision(s, seed.entity_id, "adr1", DecisionKind.ADR)
    ctx = rank_decisions([seed], [], [], s, RetrievalBudget())
    assert _STANDING_SUPERSEDE_HINT in ctx.render()


def test_hint_literal_substring_and_exact_length(tmp_path):
    """CORRECTION-4 (code review): every other hint test compares against
    `_STANDING_SUPERSEDE_HINT` itself -- a constant-self-comparison that can't catch the
    constant's own text drifting wrong (the same tautology D3's exact-text test had). This
    one asserts a literal substring plus the exact pinned length (131 chars, design D4/§3
    rev 2.1 — the verbatim string won over the earlier, self-contradicting "<=120" bound)."""
    s = Store(tmp_path / "t.db")
    seed = s.upsert_entity(Entity(canonical_name="Seed"))
    _bound_decision(s, seed.entity_id, "gotcha1", DecisionKind.GOTCHA)
    ctx = rank_decisions([seed], [], [], s, RetrievalBudget())
    rendered = ctx.render()
    assert "invalidates a record above" in rendered
    assert len(_STANDING_SUPERSEDE_HINT) == 131


def test_hint_absent_when_only_related_populated(tmp_path):
    """related-only must NOT trigger the hint -- its lines carry no id for the hint to
    point at (design D4)."""
    s = Store(tmp_path / "t.db")
    peri = s.upsert_entity(Entity(canonical_name="Peri"))
    _bound_decision(s, peri.entity_id, "peri-adr", DecisionKind.ADR)
    ctx = rank_decisions([], [peri], [], s, RetrievalBudget())
    assert ctx.related and not ctx.mistakes and not ctx.decisions
    assert _STANDING_SUPERSEDE_HINT not in ctx.render()


def test_hint_absent_when_context_is_entirely_empty(tmp_path):
    s = Store(tmp_path / "t.db")
    ctx = rank_decisions([], [], [], s, RetrievalBudget())
    rendered = ctx.render()
    assert rendered == "No context found."
    assert _STANDING_SUPERSEDE_HINT not in rendered


def test_hint_appears_in_query_decisions_render(tmp_path):
    """query_decisions reuses TaskContext.render (via rank_decisions), so it must show the
    same standing hint (design D4) whenever mistakes/decisions land -- same seeded shape as
    get_task_context's own render."""
    r = GraphifyReader(FIXTURE)
    s = Store(tmp_path / "t.db")
    e = s.upsert_entity(
        Entity(
            canonical_name="Trader",
            descriptor=Descriptor(name="Trader", file_path="trader/exec.py"),
        )
    )
    d = _bound_decision(s, e.entity_id, "race", DecisionKind.GOTCHA)
    rendered = query_decisions([Seed(name="Trader", file_path="trader/exec.py")], s, r)
    assert f"(id: {d.id})" in rendered
    assert _STANDING_SUPERSEDE_HINT in rendered
