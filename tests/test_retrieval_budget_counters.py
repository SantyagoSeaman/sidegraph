"""Budget accounting on TaskContext (design 2026-09-18-usage-stats-design.md, D5).

The ranker degrades a detailed line to the tight tier before dropping it, so `degraded` and
`dropped_for_budget` are separate counters: a record that shrank and one that vanished are
different facts about the store.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sidegraph.retrieval import RetrievalBudget, _fmt_decision, _fmt_fact, rank_decisions
from sidegraph.schema import (
    AnchorBinding,
    Decision,
    DecisionKind,
    DecisionStatus,
    Descriptor,
    Fact,
    Provenance,
)
from sidegraph.store import Store


def _entity(store, name, path):
    """get_or_create_entity takes a Descriptor, not an Entity (store.py:1744)."""
    return store.get_or_create_entity(Descriptor(name=name, file_path=path))


def _decision(
    store, entity, title, size, *, kind=DecisionKind.LESSON, status=DecisionStatus.ACCEPTED
):
    d = store.add_decision(
        Decision(
            title=title,
            kind=kind,
            status=status,
            context="c" * size,
            choice="h" * size,
            rejected="r" * size,
            consequences="q" * size,
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual"),
        )
    )
    store.add_binding(AnchorBinding(record_id=d.id, entity_id=entity.entity_id, tier=2))
    return d


def _placed_chars(ctx):
    """Total length of every line the ranker placed. `used` is charged by `add_line` alone,
    and every placed line lands in one of these buckets (structure is built elsewhere), so
    `chars_used` must equal this exactly, not merely fall inside the budget.
    """
    lines = [*ctx.mistakes, *ctx.decisions, *ctx.facts, *ctx.related, *ctx.unratified]
    return sum(len(line) for line in lines)


def test_a_render_that_fits_counts_no_degrade_and_no_drop(tmp_path):
    store = Store(tmp_path / "s")
    e = _entity(store, "f", "a.py")
    _decision(store, e, "small", 20)
    ctx = rank_decisions([e], [], [], store, RetrievalBudget())
    assert (ctx.selected, ctx.emitted, ctx.degraded, ctx.dropped_for_budget) == (1, 1, 0, 0)
    assert ctx.chars_used == _placed_chars(ctx) > 0
    store.close()


def test_a_line_too_big_for_the_detailed_tier_counts_as_degraded(tmp_path):
    store = Store(tmp_path / "s")
    e = _entity(store, "f", "a.py")
    _decision(store, e, "fat", 1200)
    ctx = rank_decisions([e], [], [], store, RetrievalBudget(memory_chars=900))
    assert ctx.emitted == 1, "it still rendered, at the tight tier"
    assert (ctx.degraded, ctx.dropped_for_budget) == (1, 0)
    assert ctx.chars_used == _placed_chars(ctx) > 0
    store.close()


def test_a_line_that_does_not_fit_even_tight_counts_as_dropped(tmp_path):
    store = Store(tmp_path / "s")
    e = _entity(store, "f", "a.py")
    _decision(store, e, "fat", 1200)
    ctx = rank_decisions([e], [], [], store, RetrievalBudget(memory_chars=40))
    assert ctx.emitted == 0
    assert ctx.dropped_for_budget == 1
    assert ctx.degraded == 0, "refused at the degrade step is dropped, never also degraded"
    assert ctx.selected == 1, "selection happens before the budget refuses it"
    store.close()


def test_a_related_bucket_refusal_is_counted_too(tmp_path):
    """Covers the tight-tier refusal site, which the seed-only tests never reach: the
    related bucket renders at the tight tier from the start, so its refusal takes the
    `not detailed` branch rather than the degrade path.
    """
    store = Store(tmp_path / "s")
    seed = _entity(store, "seed", "a.py")
    peripheral = _entity(store, "peer", "b.py")
    _decision(store, seed, "seed-side", 20)
    _decision(store, peripheral, "related-side", 1200)
    ctx = rank_decisions([seed], [peripheral], [], store, RetrievalBudget(memory_chars=300))
    assert ctx.emitted >= 1, "the seed line fits"
    assert ctx.dropped_for_budget >= 1, "the related line does not"
    assert ctx.chars_used == _placed_chars(ctx) > 0
    store.close()


def test_a_record_refused_twice_counts_once(tmp_path):
    """`seen` is filled only on a successful placement, so a record the budget refuses is
    re-attempted by every later bucket that reaches it. The report renders these counters as
    records ("N dropped by budget"), so an attempt must not count twice: one decision bound
    to a seed AND a peripheral entity is one selected record and one dropped record.
    """
    store = Store(tmp_path / "s")
    seed = _entity(store, "seed", "a.py")
    peripheral = _entity(store, "peer", "b.py")
    d = _decision(store, seed, "both-sides", 1200)
    store.add_binding(AnchorBinding(record_id=d.id, entity_id=peripheral.entity_id, tier=2))
    ctx = rank_decisions([seed], [peripheral], [], store, RetrievalBudget(memory_chars=40))
    assert ctx.emitted == 0, "nothing fits, so both attempts were refused"
    assert (ctx.selected, ctx.dropped_for_budget) == (1, 1)
    assert ctx.degraded == 0, "both refusals were final, so nothing rendered degraded"
    store.close()


def test_a_record_refused_once_then_placed_is_not_counted_dropped(tmp_path):
    """The seed attempt renders detailed and degrades to the tight line PLUS the id suffix
    (design D4); the related pass renders the bare tight line. A budget of exactly the bare
    tight line refuses the first attempt and fits the second, so the record is delivered and
    must not also be reported as dropped.
    """
    store = Store(tmp_path / "s")
    seed = _entity(store, "seed", "a.py")
    peripheral = _entity(store, "peer", "b.py")
    d = _decision(store, seed, "both-sides", 1200)
    store.add_binding(AnchorBinding(record_id=d.id, entity_id=peripheral.entity_id, tier=2))
    tight = len(_fmt_decision(d, "- ", detailed=False, drifted=False))
    ctx = rank_decisions([seed], [peripheral], [], store, RetrievalBudget(memory_chars=tight))
    assert ctx.emitted == 1, "the related pass placed it"
    assert (ctx.selected, ctx.dropped_for_budget) == (1, 0)
    store.close()


# --- facts are records too (usage-stats fix wave, finding 3) ---------------------------------
#
# `dropped_for_budget` counts distinct RECORDS refused and never placed, decisions and facts
# alike. Every place that refuses a fact line is its own test below, so a site that stops
# recording its refusal is caught by name rather than by a shared assertion that another
# site happens to satisfy.

_BIG = "x" * 400  # clipped to 240 by the renderer: a fact line of ~270 chars


def _fact(
    store, statement=_BIG, *, status=DecisionStatus.ACCEPTED, supports=(), entity=None, source="s"
):
    f = store.add_fact(
        Fact(
            statement=statement,
            source=source,
            supports=list(supports),
            status=status,
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual"),
        )
    )
    if entity is not None:
        store.add_binding(AnchorBinding(record_id=f.id, entity_id=entity.entity_id, tier=2))
    return f


def _detailed_len(d):
    return len(_fmt_decision(d, "- ", detailed=True, drifted=False))


def test_a_fact_refused_by_the_budget_is_counted_dropped(tmp_path):
    """The reproduction: a standalone fact whose line the budget refuses. Before the fix the
    fact `add_line` site ignored the refusal, so this read dropped=0."""
    store = Store(tmp_path / "s")
    e = _entity(store, "f", "a.py")
    _fact(store, entity=e)
    ctx = rank_decisions([e], [], [], store, RetrievalBudget(memory_chars=100))
    assert ctx.facts == [], "the fact rendered nothing"
    assert ctx.dropped_for_budget == 1
    store.close()


def test_a_fact_that_fits_is_not_counted_dropped(tmp_path):
    store = Store(tmp_path / "s")
    e = _entity(store, "f", "a.py")
    _fact(store, "short", entity=e)
    ctx = rank_decisions([e], [], [], store, RetrievalBudget())
    assert len(ctx.facts) == 1
    assert ctx.dropped_for_budget == 0
    store.close()


def test_inline_evidence_refused_in_the_mistake_bucket_is_counted(tmp_path):
    """Site: phase 2 of bucket A, which inserts evidence after the placed mistake lines."""
    store = Store(tmp_path / "s")
    e = _entity(store, "f", "a.py")
    d = _decision(store, e, "m", 20, kind=DecisionKind.LESSON)
    _fact(store, supports=[d.id])
    ctx = rank_decisions([e], [], [], store, RetrievalBudget(memory_chars=_detailed_len(d) + 20))
    assert ctx.emitted == 1 and len(ctx.mistakes) == 1, "the decision fits, its evidence cannot"
    assert ctx.dropped_for_budget == 1
    store.close()


def test_inline_evidence_refused_under_an_adr_is_counted(tmp_path):
    """Site: `add()`'s own evidence loop, reached by buckets B-D."""
    store = Store(tmp_path / "s")
    e = _entity(store, "f", "a.py")
    d = _decision(store, e, "a", 20, kind=DecisionKind.ADR)
    _fact(store, supports=[d.id])
    ctx = rank_decisions([e], [], [], store, RetrievalBudget(memory_chars=_detailed_len(d) + 20))
    assert ctx.emitted == 1 and len(ctx.decisions) == 1
    assert ctx.dropped_for_budget == 1
    store.close()


def test_a_standalone_fact_on_a_peripheral_entity_is_counted(tmp_path):
    """Site: the Known-facts pass over peripheral entities (the seed pass is the first test)."""
    store = Store(tmp_path / "s")
    seed = _entity(store, "seed", "a.py")
    peripheral = _entity(store, "peer", "b.py")
    _fact(store, entity=peripheral)
    ctx = rank_decisions([seed], [peripheral], [], store, RetrievalBudget(memory_chars=100))
    assert ctx.facts == []
    assert ctx.dropped_for_budget == 1
    store.close()


def test_an_accepted_fact_of_a_proposed_decision_is_counted(tmp_path):
    """Site: the loop over proposed decisions, which places their non-proposed facts."""
    store = Store(tmp_path / "s")
    e = _entity(store, "f", "a.py")
    d = _decision(store, e, "p", 5, kind=DecisionKind.ADR, status=DecisionStatus.PROPOSED)
    _fact(store, supports=[d.id])
    ctx = rank_decisions([e], [], [], store, RetrievalBudget(memory_chars=_detailed_len(d) + 20))
    assert ctx.facts == [] and len(ctx.unratified) == 1, "the proposal fits, its fact cannot"
    assert ctx.dropped_for_budget == 1
    store.close()


def test_an_unratified_fact_refused_by_the_budget_is_counted(tmp_path):
    """Site: the quarantined `## Unratified` pass, which places proposed facts last."""
    store = Store(tmp_path / "s")
    e = _entity(store, "f", "a.py")
    _fact(store, status=DecisionStatus.PROPOSED, entity=e)
    ctx = rank_decisions([e], [], [], store, RetrievalBudget(memory_chars=100))
    assert ctx.unratified == []
    assert ctx.dropped_for_budget == 1
    store.close()


def test_a_fact_refused_inline_then_placed_standalone_is_not_dropped(tmp_path):
    """The evidence line carries a longer prefix than the Known-facts line, so a budget cut
    between the two refuses the fact under its decision and fits it as a standalone entry.
    It was delivered, so it is not dropped -- the fact analogue of the decision case above.
    """
    store = Store(tmp_path / "s")
    e = _entity(store, "f", "a.py")
    d = _decision(store, e, "a", 20, kind=DecisionKind.ADR)
    f = _fact(store, "y" * 80, supports=[d.id], entity=e)
    standalone = len(_fmt_fact(f))
    ctx = rank_decisions(
        [e], [], [], store, RetrievalBudget(memory_chars=_detailed_len(d) + standalone)
    )
    assert len(ctx.facts) == 1, "placed as a standalone fact"
    assert ctx.dropped_for_budget == 0
    store.close()


def test_a_fact_refused_twice_counts_once(tmp_path):
    """Two decisions supporting one fact attempt it twice; one record is one drop."""
    store = Store(tmp_path / "s")
    e = _entity(store, "f", "a.py")
    d1 = _decision(store, e, "a1", 5, kind=DecisionKind.ADR)
    d2 = _decision(store, e, "a2", 5, kind=DecisionKind.ADR)
    _fact(store, supports=[d1.id, d2.id])
    ctx = rank_decisions(
        [e], [], [], store, RetrievalBudget(memory_chars=_detailed_len(d1) + _detailed_len(d2) + 20)
    )
    assert ctx.emitted == 2
    assert ctx.dropped_for_budget == 1
    store.close()


def test_decisions_and_facts_are_summed_into_one_number(tmp_path):
    store = Store(tmp_path / "s")
    e = _entity(store, "f", "a.py")
    _decision(store, e, "fat", 1200)
    _fact(store, entity=e)
    ctx = rank_decisions([e], [], [], store, RetrievalBudget(memory_chars=100))
    assert ctx.dropped_for_budget == 2, "one decision refused + one fact refused"
    store.close()


# --- `emitted` counts records placed, decisions and facts alike (usage-stats fix wave 3) -----
#
# `emitted` is the render journal's statement of what reached the agent. It was decision-only,
# so a render that placed a single fact reported nothing delivered, and the report's
# `showings` read that as "asked and got nothing". A fact that rendered reached the agent. One
# named test per way a fact gets placed, so a route that stops counting is caught by name.


def test_a_standalone_fact_on_a_seed_is_emitted(tmp_path):
    """Site: the Known-facts pass over seed entities. `selected` stays decision-only."""
    store = Store(tmp_path / "s")
    e = _entity(store, "f", "a.py")
    _fact(store, "short", entity=e)
    ctx = rank_decisions([e], [], [], store, RetrievalBudget())
    assert len(ctx.facts) == 1
    assert (ctx.selected, ctx.emitted) == (0, 1)
    store.close()


def test_a_standalone_fact_on_a_peripheral_entity_is_emitted(tmp_path):
    """Site: the Known-facts pass over peripheral entities."""
    store = Store(tmp_path / "s")
    seed = _entity(store, "seed", "a.py")
    peripheral = _entity(store, "peer", "b.py")
    _fact(store, "short", entity=peripheral)
    ctx = rank_decisions([seed], [peripheral], [], store, RetrievalBudget())
    assert len(ctx.facts) == 1
    assert (ctx.selected, ctx.emitted) == (0, 1)
    store.close()


def test_inline_evidence_under_a_mistake_is_emitted(tmp_path):
    """Site: phase 2 of bucket A. The decision line and its evidence line are two records."""
    store = Store(tmp_path / "s")
    e = _entity(store, "f", "a.py")
    d = _decision(store, e, "m", 20, kind=DecisionKind.LESSON)
    _fact(store, "short", supports=[d.id])
    ctx = rank_decisions([e], [], [], store, RetrievalBudget())
    assert len(ctx.mistakes) == 2, "the decision line and its evidence line"
    assert (ctx.selected, ctx.emitted) == (1, 2)
    store.close()


def test_inline_evidence_under_an_adr_is_emitted(tmp_path):
    """Site: `add()`'s own evidence loop, reached by buckets B-D."""
    store = Store(tmp_path / "s")
    e = _entity(store, "f", "a.py")
    d = _decision(store, e, "a", 20, kind=DecisionKind.ADR)
    _fact(store, "short", supports=[d.id])
    ctx = rank_decisions([e], [], [], store, RetrievalBudget())
    assert len(ctx.decisions) == 2, "the decision line and its evidence line"
    assert (ctx.selected, ctx.emitted) == (1, 2)
    store.close()


def test_an_accepted_fact_of_a_proposed_decision_is_emitted(tmp_path):
    """Site: the loop over proposed decisions, which places their non-proposed facts."""
    store = Store(tmp_path / "s")
    e = _entity(store, "f", "a.py")
    d = _decision(store, e, "p", 5, kind=DecisionKind.ADR, status=DecisionStatus.PROPOSED)
    _fact(store, "short", supports=[d.id])
    ctx = rank_decisions([e], [], [], store, RetrievalBudget())
    assert len(ctx.facts) == 1 and len(ctx.unratified) == 1
    assert (ctx.selected, ctx.emitted) == (1, 2)
    store.close()


def test_an_unratified_fact_is_emitted(tmp_path):
    """Site: the quarantined `## Unratified` pass, which places proposed facts last. A proposal
    that rendered reached the agent as surely as an accepted record."""
    store = Store(tmp_path / "s")
    e = _entity(store, "f", "a.py")
    _fact(store, "short", status=DecisionStatus.PROPOSED, entity=e)
    ctx = rank_decisions([e], [], [], store, RetrievalBudget())
    assert len(ctx.unratified) == 1
    assert (ctx.selected, ctx.emitted) == (0, 1)
    store.close()


def test_a_fact_supporting_two_decisions_is_emitted_once(tmp_path):
    """Two decisions attempt the same evidence line; one record reached the agent once."""
    store = Store(tmp_path / "s")
    e = _entity(store, "f", "a.py")
    d1 = _decision(store, e, "a1", 5, kind=DecisionKind.ADR)
    d2 = _decision(store, e, "a2", 5, kind=DecisionKind.ADR)
    _fact(store, "short", supports=[d1.id, d2.id])
    ctx = rank_decisions([e], [], [], store, RetrievalBudget())
    assert (ctx.selected, ctx.emitted) == (2, 3)
    store.close()


def test_a_refused_fact_is_not_emitted(tmp_path):
    """The other side: a record the budget refused did not reach the agent."""
    store = Store(tmp_path / "s")
    e = _entity(store, "f", "a.py")
    d = _decision(store, e, "a", 20, kind=DecisionKind.ADR)
    _fact(store, supports=[d.id])
    ctx = rank_decisions([e], [], [], store, RetrievalBudget(memory_chars=_detailed_len(d) + 20))
    assert (ctx.emitted, ctx.dropped_for_budget) == (1, 1), "the decision fits, its evidence cannot"
    store.close()


def test_emitted_is_the_number_of_records_that_reached_the_render(tmp_path):
    """Every placed record, decision or fact, is in `shown_ids`, so the two agree. `emitted`
    keeps its own counter (the increment sits where the record is placed) and this pins the
    counter to what the render actually holds."""
    store = Store(tmp_path / "s")
    seed = _entity(store, "seed", "a.py")
    peripheral = _entity(store, "peer", "b.py")
    m = _decision(store, seed, "m", 5, kind=DecisionKind.LESSON)
    a = _decision(store, seed, "a", 5, kind=DecisionKind.ADR)
    _fact(store, "under-mistake", supports=[m.id])
    _fact(store, "under-adr", supports=[a.id])
    _fact(store, "standalone-seed", entity=seed)
    _fact(store, "standalone-peripheral", entity=peripheral)
    _fact(store, "proposed", status=DecisionStatus.PROPOSED, entity=seed)
    ctx = rank_decisions([seed], [peripheral], [], store, RetrievalBudget())
    assert ctx.emitted == 7 == len(ctx.shown_ids), "two decisions and five facts"
    assert ctx.selected == 2
    store.close()
