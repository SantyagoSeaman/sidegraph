"""The Related bucket is capped and its lines are short, with ids.

Related records reach a task only through community/domain/peripheral/global expansion or a
guessed seed, not through the files the agent named. A per-record relevance measurement on
real task-context calls found most of them off-task, so the bucket shows at most
``_RELATED_LINE_CAP`` of them, each with a short choice snippet and its id, and counts the rest
in one closing line. The seed's own superseded one-liners are history, not expansion, and stay
outside the cap.
"""

from datetime import UTC, datetime, timedelta

from sidegraph.retrieval import (
    _RELATED_CLIP_CHARS,
    _RELATED_LINE_CAP,
    RetrievalBudget,
    rank_decisions,
)
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

_NOW = datetime.now(UTC)


def _decision(store, entity_id, title, *, kind=DecisionKind.ADR, choice=None, age=0, **extra):
    d = Decision(
        title=title,
        kind=kind,
        context="c",
        choice=choice if choice is not None else f"do {title}",
        valid_from=_NOW - timedelta(days=age),
        provenance=Provenance(source="manual"),
        **{"status": DecisionStatus.ACCEPTED, **extra},
    )
    store._write_decision(d)
    store._conn.commit()
    store.add_binding(AnchorBinding(record_id=d.id, entity_id=entity_id, tier=2, status="live"))
    return d


def _related_records(ctx):
    return [line for line in ctx.related if line.startswith("- [")]


def test_related_line_carries_a_short_choice_snippet_and_the_id(tmp_path):
    s = Store(tmp_path / "t.db")
    peri = s.upsert_entity(Entity(canonical_name="Peri"))
    d = _decision(s, peri.entity_id, "router design", choice="Long router choice. " + "word " * 80)

    ctx = rank_decisions([], [peri], [], s, RetrievalBudget(memory_chars=10_000))

    (line,) = ctx.related
    assert line.endswith(f" (id: {d.id})")
    snippet = line.split("router design: ", 1)[1].removesuffix(f" (id: {d.id})")
    assert len(snippet) <= _RELATED_CLIP_CHARS
    assert snippet.startswith("Long router choice.")


def test_related_bucket_shows_at_most_the_cap_and_counts_the_rest(tmp_path):
    s = Store(tmp_path / "t.db")
    peri = s.upsert_entity(Entity(canonical_name="Peri"))
    extra = 3
    records = [
        _decision(s, peri.entity_id, f"note {i}", age=i) for i in range(_RELATED_LINE_CAP + extra)
    ]

    ctx = rank_decisions([], [peri], [], s, RetrievalBudget(memory_chars=50_000))

    shown = _related_records(ctx)
    assert len(shown) == _RELATED_LINE_CAP
    # Recency order inside the source is kept: the newest records are the ones shown.
    assert all(f"note {i}:" in shown[i] for i in range(_RELATED_LINE_CAP))
    assert ctx.related[-1].startswith(f"- (+{extra} more related")
    assert ctx.related_overflow == extra
    assert set(ctx.shown_ids) == {d.id for d in records[:_RELATED_LINE_CAP]}
    # Records left out by the cap were not refused by the budget.
    assert ctx.dropped_for_budget == 0


def test_cap_counts_records_across_every_related_source(tmp_path):
    s = Store(tmp_path / "t.db")
    peri = s.upsert_entity(Entity(canonical_name="Peri"))
    guessed = s.upsert_entity(Entity(canonical_name="Guessed"))
    for i in range(_RELATED_LINE_CAP):
        _decision(s, peri.entity_id, f"peri {i}", age=i)
    trailing = _decision(s, guessed.entity_id, "guessed note")

    ctx = rank_decisions(
        [], [peri], [], s, RetrievalBudget(memory_chars=50_000), trailing_entities=[guessed]
    )

    assert len(_related_records(ctx)) == _RELATED_LINE_CAP
    assert trailing.id not in ctx.shown_ids
    assert ctx.related_overflow == 1


def test_seed_superseded_one_liners_stay_outside_the_cap(tmp_path):
    s = Store(tmp_path / "t.db")
    seed = s.upsert_entity(Entity(canonical_name="Seed"))
    peri = s.upsert_entity(Entity(canonical_name="Peri"))
    for i in range(_RELATED_LINE_CAP + 2):
        _decision(s, peri.entity_id, f"peri {i}", age=i)
    old = _decision(s, seed.entity_id, "old approach", status=DecisionStatus.SUPERSEDED, age=400)

    ctx = rank_decisions([seed], [peri], [], s, RetrievalBudget(memory_chars=50_000))

    assert any(
        line.startswith("~ tried, reverted") and "old approach" in line for line in ctx.related
    )
    assert old.id in ctx.shown_ids
    assert len(_related_records(ctx)) == _RELATED_LINE_CAP


def test_related_lines_carry_no_inline_evidence(tmp_path):
    s = Store(tmp_path / "t.db")
    peri = s.upsert_entity(Entity(canonical_name="Peri"))
    d = _decision(s, peri.entity_id, "related with support")
    s.add_fact(
        Fact(
            statement="measured evidence",
            source="test",
            supports=[d.id],
            status=DecisionStatus.ACCEPTED,
            valid_from=_NOW,
            provenance=Provenance(source="manual"),
        )
    )

    ctx = rank_decisions([], [peri], [], s, RetrievalBudget(memory_chars=10_000))

    assert len(ctx.related) == 1
    assert "evidence:" not in ctx.related[0]


def test_cap_does_not_touch_seed_buckets(tmp_path):
    s = Store(tmp_path / "t.db")
    seed = s.upsert_entity(Entity(canonical_name="Seed"))
    for i in range(_RELATED_LINE_CAP + 3):
        _decision(s, seed.entity_id, f"gotcha {i}", kind=DecisionKind.GOTCHA, age=i)
        _decision(s, seed.entity_id, f"adr {i}", age=i)

    ctx = rank_decisions([seed], [], [], s, RetrievalBudget(memory_chars=100_000))

    assert len(ctx.mistakes) == _RELATED_LINE_CAP + 3
    assert len(ctx.decisions) == _RELATED_LINE_CAP + 3
    assert ctx.related == []
    assert ctx.related_overflow == 0


def test_global_scope_records_stay_outside_the_cap(tmp_path):
    from sidegraph.schema import Scope

    s = Store(tmp_path / "t.db")
    peri = s.upsert_entity(Entity(canonical_name="Peri"))
    for i in range(_RELATED_LINE_CAP + 2):
        _decision(s, peri.entity_id, f"peri {i}", age=i)
    rule = Decision(
        title="repo-wide rule",
        kind=DecisionKind.CONSTRAINT,
        context="c",
        choice="never do the thing",
        scope=Scope.GLOBAL,
        status=DecisionStatus.ACCEPTED,
        valid_from=_NOW,
        provenance=Provenance(source="manual"),
    )
    s._write_decision(rule)
    s._conn.commit()

    ctx = rank_decisions([], [peri], [], s, RetrievalBudget(memory_chars=50_000))

    assert rule.id in ctx.shown_ids
    assert any(
        line.split("] ", 1)[1] == "repo-wide rule: never do the thing" for line in ctx.related
    )
    assert ctx.related_overflow == 2


def test_a_global_record_also_bound_to_an_expanded_entity_is_never_capped(tmp_path):
    from sidegraph.schema import Scope

    s = Store(tmp_path / "t.db")
    peri = s.upsert_entity(Entity(canonical_name="Peri"))
    for i in range(_RELATED_LINE_CAP):
        _decision(s, peri.entity_id, f"peri {i}", age=i)
    rule = _decision(s, peri.entity_id, "bound rule", scope=Scope.GLOBAL, age=100)

    ctx = rank_decisions([], [peri], [], s, RetrievalBudget(memory_chars=50_000))

    assert rule.id in ctx.shown_ids
    assert any(line == "- [adr] bound rule: do bound rule" for line in ctx.related)
    assert len(_related_records(ctx)) == _RELATED_LINE_CAP + 1
    assert ctx.related_overflow == 0


def test_proposed_facts_of_a_capped_line_still_reach_the_unratified_queue(tmp_path):
    s = Store(tmp_path / "t.db")
    peri = s.upsert_entity(Entity(canonical_name="Peri"))
    records = [
        _decision(s, peri.entity_id, f"note {i}", age=i) for i in range(_RELATED_LINE_CAP + 1)
    ]
    s.add_fact(
        Fact(
            statement="pending observation",
            source="test",
            supports=[records[-1].id],
            status=DecisionStatus.PROPOSED,
            valid_from=_NOW,
            provenance=Provenance(source="manual"),
        )
    )

    ctx = rank_decisions([], [peri], [], s, RetrievalBudget(memory_chars=50_000))

    assert ctx.related_overflow == 1
    assert any("pending observation" in line for line in ctx.unratified)


def test_the_overflow_count_survives_a_tight_budget_in_its_short_form(tmp_path):
    s = Store(tmp_path / "t.db")
    peri = s.upsert_entity(Entity(canonical_name="Peri"))
    for i in range(_RELATED_LINE_CAP + 1):
        _decision(s, peri.entity_id, f"n{i}", age=i)
    probe = rank_decisions([], [peri], [], s, RetrievalBudget(memory_chars=50_000))
    lines = sum(len(line) for line in _related_records(probe))

    ctx = rank_decisions([], [peri], [], s, RetrievalBudget(memory_chars=lines + 40))

    assert len(_related_records(ctx)) == _RELATED_LINE_CAP
    assert ctx.related[-1] == "- (+1 more related not shown)"


def test_a_record_the_budget_refused_earlier_is_not_also_counted_as_overflow(tmp_path):
    s = Store(tmp_path / "t.db")
    seed = s.upsert_entity(Entity(canonical_name="Seed"))
    peri = s.upsert_entity(Entity(canonical_name="Peri"))
    for i in range(_RELATED_LINE_CAP):
        _decision(s, peri.entity_id, f"p{i}", age=i)
    big = _decision(s, seed.entity_id, "big", choice="long " * 400, age=50)
    s.add_binding(AnchorBinding(record_id=big.id, entity_id=peri.entity_id, tier=2, status="live"))
    probe = rank_decisions([], [peri], [], s, RetrievalBudget(memory_chars=50_000))
    room = sum(len(line) for line in _related_records(probe)[:_RELATED_LINE_CAP])

    ctx = rank_decisions([seed], [peri], [], s, RetrievalBudget(memory_chars=room + 10))

    assert big.id not in ctx.shown_ids
    assert ctx.dropped_for_budget == 1
    assert ctx.related_overflow == 0
