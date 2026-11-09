"""Full bodies alone qualify for in-place per-agent back-references."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from sidegraph.retrieval import (
    DRIFT_CACHE_KEY,
    RetrievalBudget,
    Seed,
    get_task_context,
    rank_decisions,
)
from sidegraph.schema import AnchorBinding, Decision, Descriptor, Entity, Fact, Provenance
from sidegraph.store import Store


@pytest.fixture
def memory(tmp_path):
    with Store(tmp_path / ".sidegraph") as store:
        entity = store.upsert_entity(
            Entity(canonical_name="Seed", descriptor=Descriptor(name="Seed", file_path="seed.py"))
        )
        records = []
        for i, kind in enumerate(["gotcha", "gotcha", "constraint", "adr", "adr"]):
            d = store.add_decision(
                Decision(
                    title=f"Record {i}",
                    kind=kind,
                    context="Context " * 30,
                    choice="Choose this " * 30,
                    status="accepted",
                    valid_from=datetime.now(UTC) - timedelta(seconds=i),
                    provenance=Provenance(source="manual"),
                )
            )
            store.add_binding(
                AnchorBinding(record_id=d.id, entity_id=entity.entity_id, tier=2, status="live")
            )
            records.append(d)
        yield store, entity, records


def test_backrefs_keep_position_and_only_full_bodies_are_eligible(memory):
    store, entity, records = memory
    seeds = [Seed(name="Seed", file_path="seed.py")]
    before = get_task_context(seeds, store, None, RetrievalBudget(memory_chars=10000))
    sent = frozenset({records[0].id, records[3].id})
    after = get_task_context(
        seeds, store, None, RetrievalBudget(memory_chars=10000), already_sent=sent
    )
    assert after.shown_ids == before.shown_ids
    assert after.backref_ids == [records[0].id, records[3].id]
    assert after.full_body_ids == [d.id for d in records if d.id not in sent]
    for bucket in ("mistakes", "decisions"):
        old, new = getattr(before, bucket), getattr(after, bucket)
        assert len(old) == len(new)
        for i, line in enumerate(old):
            d = next(d for d in records if d.id in line)
            if d.id in sent:
                assert new[i] == f"- [{d.kind.value}] [sent earlier] {d.title} (id: {d.id})"
            else:
                assert new[i] == line
    assert after.render_parts()[1].count("[sent earlier] =") == 1


def test_degraded_related_and_proposed_records_are_never_sent_bodies(memory):
    store, entity, records = memory
    small = rank_decisions([entity], [], [], store, RetrievalBudget(memory_chars=330))
    assert small.degraded_ids
    assert not set(small.degraded_ids) & set(small.full_body_ids)
    assert not small.backref_ids
    related = rank_decisions(
        [],
        [entity],
        [],
        store,
        RetrievalBudget(memory_chars=10000),
        already_sent=frozenset(d.id for d in records),
    )
    assert related.related and not related.full_body_ids and not related.backref_ids
    proposal = store.add_decision(
        Decision(
            title="Proposed decision",
            kind="gotcha",
            context="Context",
            choice="Full proposed body",
            status="proposed",
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual"),
        )
    )
    store.add_binding(
        AnchorBinding(record_id=proposal.id, entity_id=entity.entity_id, tier=2, status="live")
    )
    ctx = rank_decisions(
        [entity],
        [],
        [],
        store,
        RetrievalBudget(memory_chars=10000),
        already_sent=frozenset({proposal.id}),
    )
    assert any("Full proposed body" in line for line in ctx.unratified)
    assert proposal.id not in ctx.full_body_ids + ctx.backref_ids


def test_backref_skips_evidence_but_keeps_proposals_flags_and_order(memory):
    store, entity, records = memory
    gotcha = records[0]
    for status in ("accepted", "proposed"):
        store.add_fact(
            Fact(
                statement=f"{status} supporting evidence",
                source="experiment",
                supports=[gotcha.id],
                status=status,
                valid_from=datetime.now(UTC),
                provenance=Provenance(source="manual"),
            )
        )
    store.set_meta(DRIFT_CACHE_KEY, json.dumps({"by_commit": {"abc": [gotcha.id]}}))
    ctx = rank_decisions(
        [entity],
        [],
        [],
        store,
        RetrievalBudget(memory_chars=10000),
        already_sent=frozenset({gotcha.id}),
        file_bound_entity_ids=frozenset({entity.entity_id}),
    )
    assert "[drifted] [file-bound] [sent earlier]" in ctx.mistakes[0]
    assert not any("accepted supporting evidence" in line for line in ctx.mistakes)
    assert any("proposed supporting evidence" in line for line in ctx.unratified)
    assert ctx.drift_shown and ctx.file_bound_shown
    assert records[1].id in ctx.mistakes[1]


def test_freed_budget_admits_new_records(memory):
    store, entity, records = memory
    budget = RetrievalBudget(memory_chars=1100)
    first = rank_decisions([entity], [], [], store, budget)
    assert first.full_body_ids and len(first.shown_ids) < len(records)
    second = rank_decisions(
        [entity], [], [], store, budget, already_sent=frozenset(first.full_body_ids)
    )
    assert set(second.shown_ids) - set(first.shown_ids)
    common = set(first.shown_ids) & set(second.shown_ids)
    assert [i for i in first.shown_ids if i in common] == [
        i for i in second.shown_ids if i in common
    ]
