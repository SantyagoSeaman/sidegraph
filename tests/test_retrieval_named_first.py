"""The records of a symbol the caller named take the memory budget before the records of the
other symbols of the named files; evidence still never takes the budget of a mistake line.
see design/superpowers/specs/2026-11-12-task-context-named-symbol-first-design.md
"""

import json
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from sidegraph.engine.reader import GraphifyReader
from sidegraph.retrieval import (
    RetrievalBudget,
    Seed,
    _fmt_decision,
    get_task_context,
    query_decisions,
    resolve_seeds,
)
from sidegraph.schema import (
    AnchorBinding,
    Decision,
    DecisionKind,
    DecisionStatus,
    Descriptor,
    Entity,
    Fact,
    Provenance,
)
from sidegraph.store import Store

FILE = "src/mod.py"
OTHER_FILE = "src/other.py"
THIRD_FILE = "src/third.py"
GRAPH = {
    "built_at_commit": "abc123",
    "nodes": [
        {"id": "file", "label": "mod.py", "file_type": "code", "source_file": FILE},
        {"id": "foo", "label": "foo()", "file_type": "code", "source_file": FILE},
        {"id": "bar", "label": "bar()", "file_type": "code", "source_file": FILE},
        {"id": "dup1", "label": "dup()", "file_type": "code", "source_file": FILE},
        {"id": "dup2", "label": "dup()", "file_type": "code", "source_file": OTHER_FILE},
        {"id": "run", "label": "run()", "file_type": "code", "source_file": THIRD_FILE},
    ],
    "links": [],
}
BASE = datetime(2026, 1, 1, tzinfo=UTC)


def _world(tmp_path):
    """A store and a reader over one file with three symbols; `dup` is also in a second file.
    `run` is stored as `Klass.run` (its node is labelled `run()`), and `ghost` is a stored
    entity with a file path that no graph node carries."""
    path = tmp_path / "graph.json"
    path.write_text(json.dumps(GRAPH))
    store = Store(tmp_path / "store")
    entities = {}
    for node, name, file_path in [
        ("file", "mod.py", FILE),
        ("foo", "foo", FILE),
        ("bar", "bar", FILE),
        ("dup1", "dup", FILE),
        ("dup2", "dup", OTHER_FILE),
        ("run", "Klass.run", THIRD_FILE),
    ]:
        entities[node] = store.upsert_entity(
            Entity(
                canonical_name=name,
                descriptor=Descriptor(name=name, file_path=file_path),
                last_seen_node_id=node,
            )
        )
    entities["ghost"] = store.upsert_entity(
        Entity(canonical_name="ghost", descriptor=Descriptor(name="ghost", file_path=FILE))
    )
    return store, GraphifyReader(path), entities


def _record(store, entities, title, kind, *, age_days=0):
    """An accepted decision bound to every entity of ``entities``; more ``age_days`` is older."""
    decision = store.add_decision(
        Decision(
            title=title,
            kind=kind,
            status=DecisionStatus.ACCEPTED,
            context="c",
            choice=f"do {title}",
            valid_from=BASE - timedelta(days=age_days),
            provenance=Provenance(source="manual"),
        )
    )
    for entity in entities:
        store.add_binding(
            AnchorBinding(record_id=decision.id, entity_id=entity.entity_id, tier=2, status="live")
        )
    return decision


def _evidence(store, decision, statement="m"):
    """An accepted fact that supports ``decision``. The default line is shorter than any
    decision line of these tests, so it fits wherever one of them would."""
    return store.add_fact(
        Fact(
            statement=statement,
            source="s",
            status=DecisionStatus.ACCEPTED,
            supports=[decision.id],
            valid_from=BASE,
            provenance=Provenance(source="manual"),
        )
    )


def _room_for(*decisions) -> RetrievalBudget:
    """A memory budget with room for exactly the detailed lines of ``decisions``."""
    return RetrievalBudget(
        memory_chars=sum(len(_fmt_decision(d, detailed=True)) for d in decisions)
    )


NAMED = [Seed(file_path=FILE), Seed(name="foo", file_path=FILE)]
FILES_ONLY = [Seed(file_path=FILE)]


def test_resolve_seeds_reports_the_entities_a_name_seed_reached(tmp_path):
    store, reader, e = _world(tmp_path)
    res = resolve_seeds(NAMED, reader, store)
    assert res.named_entity_ids == frozenset({e["foo"].entity_id})
    assert {x.entity_id for x in res.seed_entities} == {
        e["file"].entity_id,
        e["foo"].entity_id,
        e["bar"].entity_id,
        e["dup1"].entity_id,
    }
    assert resolve_seeds(FILES_ONLY, reader, store).named_entity_ids == frozenset()
    # The two routes to a named symbol each stand alone: an entity stored under another name
    # than its node's label is reached through the node, and one with no node through its
    # stored descriptor.
    by_node = resolve_seeds([Seed(name="run", file_path=THIRD_FILE)], reader, store)
    assert by_node.named_entity_ids == frozenset({e["run"].entity_id})
    by_descriptor = resolve_seeds([Seed(name="ghost", file_path=FILE)], reader, store)
    assert by_descriptor.named_entity_ids == frozenset({e["ghost"].entity_id})


def test_name_with_no_file_names_a_symbol_only_when_the_graph_holds_it_once(tmp_path):
    store, reader, e = _world(tmp_path)
    once = resolve_seeds([Seed(file_path=FILE), Seed(name="foo")], reader, store)
    assert once.named_entity_ids == frozenset({e["foo"].entity_id})
    twice = resolve_seeds([Seed(file_path=FILE), Seed(name="dup")], reader, store)
    assert twice.named_entity_ids == frozenset()
    # The candidates are still seed entities, as before: only their rank is not raised.
    assert e["dup2"].entity_id in {x.entity_id for x in twice.seed_entities}
    with_file = resolve_seeds([Seed(name="dup", file_path=OTHER_FILE)], reader, store)
    assert with_file.named_entity_ids == frozenset({e["dup2"].entity_id})


def test_ambiguous_name_with_no_file_does_not_outrank_the_named_files_records(tmp_path):
    store, reader, e = _world(tmp_path)
    # A stored `dup` with no file: what the bare name resolves to while two files hold one.
    loose = store.upsert_entity(Entity(canonical_name="dup", descriptor=Descriptor(name="dup")))
    mistake = _record(store, [e["bar"]], "bar-gotcha", DecisionKind.GOTCHA)
    elsewhere = _record(store, [e["dup2"]], "dup-adr", DecisionKind.ADR, age_days=5)
    pathless = _record(store, [loose], "loose-adr", DecisionKind.ADR, age_days=7)
    seeds = [Seed(file_path=FILE), Seed(name="dup")]
    assert resolve_seeds(seeds, reader, store).named_entity_ids == frozenset()
    ctx = get_task_context(seeds, store, reader)
    assert ctx.shown_ids[0] == mistake.id
    assert set(ctx.shown_ids[1:]) == {elsewhere.id, pathless.id}


def test_named_symbol_adr_takes_the_budget_before_another_symbols_mistake(tmp_path):
    store, reader, e = _world(tmp_path)
    mistake = _record(store, [e["bar"]], "bar-gotcha", DecisionKind.GOTCHA)
    adr = _record(store, [e["foo"]], "foo-adr", DecisionKind.ADR, age_days=5)
    ctx = get_task_context(NAMED, store, reader, _room_for(adr))
    assert ctx.shown_ids == [adr.id]
    assert ctx.mistakes == []
    # With room for both, each is in its own section and the named one was placed first.
    roomy = get_task_context(NAMED, store, reader)
    assert roomy.shown_ids == [adr.id, mistake.id]
    assert any("bar-gotcha" in line for line in roomy.mistakes)
    assert any("foo-adr" in line for line in roomy.decisions)


def test_record_bound_to_the_named_symbol_and_another_symbol_is_a_named_record(tmp_path):
    store, reader, e = _world(tmp_path)
    _record(store, [e["bar"]], "bar-gotcha", DecisionKind.GOTCHA)
    shared = _record(store, [e["bar"], e["foo"]], "shared-adr", DecisionKind.ADR, age_days=5)
    ctx = get_task_context(NAMED, store, reader, _room_for(shared))
    assert ctx.shown_ids == [shared.id]


def test_named_symbols_mistake_is_placed_before_its_adr(tmp_path):
    store, reader, e = _world(tmp_path)
    adr = _record(store, [e["foo"]], "foo-adr", DecisionKind.ADR)
    mistake = _record(store, [e["foo"]], "foo-gotcha", DecisionKind.GOTCHA, age_days=5)
    ctx = get_task_context(NAMED, store, reader, _room_for(mistake))
    assert ctx.shown_ids == [mistake.id]
    assert get_task_context(NAMED, store, reader).shown_ids == [mistake.id, adr.id]


def test_evidence_of_a_named_mistake_never_takes_another_symbols_mistake_line(tmp_path):
    store, reader, e = _world(tmp_path)
    named = _record(store, [e["foo"]], "foo-gotcha", DecisionKind.GOTCHA)
    other = _record(store, [e["bar"]], "bar-gotcha", DecisionKind.GOTCHA, age_days=1)
    _evidence(store, named)
    ctx = get_task_context(NAMED, store, reader, _room_for(named, other))
    assert ctx.full_body_ids == [named.id, other.id]
    assert not any("evidence:" in line for line in ctx.mistakes)


def test_evidence_of_a_named_adr_never_takes_another_symbols_mistake_line(tmp_path):
    store, reader, e = _world(tmp_path)
    other = _record(store, [e["bar"]], "bar-gotcha", DecisionKind.GOTCHA)
    adr = _record(store, [e["foo"]], "foo-adr", DecisionKind.ADR, age_days=5)
    _evidence(store, adr)
    ctx = get_task_context(NAMED, store, reader, _room_for(adr, other))
    assert ctx.full_body_ids == [adr.id, other.id]
    assert not any("evidence:" in line for line in [*ctx.mistakes, *ctx.decisions])


def test_evidence_of_a_named_adr_follows_its_line_and_waits_for_mistake_evidence(tmp_path):
    store, reader, e = _world(tmp_path)
    other = _record(store, [e["bar"]], "bar-gotcha", DecisionKind.GOTCHA)
    older = _record(store, [e["foo"]], "foo-adr-old", DecisionKind.ADR, age_days=9)
    newer = _record(store, [e["foo"]], "foo-adr-new", DecisionKind.ADR, age_days=5)
    rest = _record(store, [e["bar"]], "bar-adr", DecisionKind.ADR, age_days=7)
    of_older = _evidence(store, older, "about the older adr")
    of_newer = _evidence(store, newer, "about the newer adr")
    of_mistake = _evidence(store, other, "about the gotcha")
    ctx = get_task_context(NAMED, store, reader)
    assert [line.split(":")[0].strip() for line in ctx.decisions] == [
        "- [adr] foo-adr-new",
        "evidence",
        "- [adr] foo-adr-old",
        "evidence",
        "- [adr] bar-adr",
    ]
    assert "about the newer adr" in ctx.decisions[1] and "about the older adr" in ctx.decisions[3]
    # Placement order: the named lines, the other mistake, then evidence, mistakes' first.
    assert ctx.shown_ids[:4] == [newer.id, older.id, other.id, rest.id]
    assert ctx.shown_ids[4:] == [of_mistake.id, of_newer.id, of_older.id]


def test_call_with_no_named_symbol_keeps_the_old_order(tmp_path):
    store, reader, e = _world(tmp_path)
    adr = _record(store, [e["foo"]], "foo-adr", DecisionKind.ADR)
    older = _record(store, [e["bar"]], "bar-gotcha", DecisionKind.GOTCHA, age_days=5)
    newer = _record(store, [e["foo"]], "foo-gotcha", DecisionKind.GOTCHA, age_days=1)
    whole = _record(store, [e["file"]], "file-adr", DecisionKind.ADR, age_days=9)
    ctx = get_task_context(FILES_ONLY, store, reader)
    # Mistakes of every symbol first, then the ADRs: nothing is a named record.
    assert set(ctx.shown_ids[:2]) == {older.id, newer.id}
    assert set(ctx.shown_ids[2:]) == {adr.id, whole.id}


def test_blank_file_path_on_a_shared_name_names_nothing(tmp_path):
    store, reader, e = _world(tmp_path)
    seeds = [Seed(file_path=FILE), Seed(name="dup", file_path=" ")]
    assert resolve_seeds(seeds, reader, store).named_entity_ids == frozenset()
    # A blank path is no path: the name that the graph holds once still names its symbol.
    once = resolve_seeds([Seed(file_path=FILE), Seed(name="foo", file_path=" ")], reader, store)
    assert once.named_entity_ids == frozenset({e["foo"].entity_id})


def test_a_name_that_is_not_a_string_fails_with_the_validation_message(tmp_path):
    store, reader, _ = _world(tmp_path)
    with pytest.raises(ValidationError, match="valid string"):
        query_decisions([Seed(file_path=FILE), Seed(name=["foo"], file_path=FILE)], store, reader)


def test_guessed_seeds_that_act_as_the_seeds_name_the_symbol(tmp_path):
    store, reader, e = _world(tmp_path)
    _record(store, [e["bar"]], "bar-gotcha", DecisionKind.GOTCHA)
    adr = _record(store, [e["foo"]], "foo-adr", DecisionKind.ADR, age_days=5)
    # The exact seeds hold no graph node, so the guessed ones, which the seed ladder spelled
    # right, act as the seeds and their named symbol leads.
    exact = [Seed(file_path="mod.py"), Seed(name="foo", file_path="src/wrong.py")]
    ctx = get_task_context(exact, store, reader, _room_for(adr), guessed=NAMED)
    assert ctx.shown_ids == [adr.id]


def test_evidence_follows_each_of_two_mistakes_at_the_default_budget(tmp_path):
    store, reader, e = _world(tmp_path)
    named = _record(store, [e["foo"]], "foo-gotcha", DecisionKind.GOTCHA, age_days=5)
    other = _record(store, [e["bar"]], "bar-gotcha", DecisionKind.GOTCHA)
    _evidence(store, named, "about the named gotcha")
    _evidence(store, other, "about the other gotcha")
    ctx = get_task_context(NAMED, store, reader)
    assert [line.split(":")[0].strip() for line in ctx.mistakes] == [
        "- [gotcha] foo-gotcha",
        "evidence",
        "- [gotcha] bar-gotcha",
        "evidence",
    ]
    assert "about the named gotcha" in ctx.mistakes[1]
    assert "about the other gotcha" in ctx.mistakes[3]
