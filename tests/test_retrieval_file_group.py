"""A call that names files and no symbol places the records of the file as a whole first:
the records bound to a named file's own graph node and to no other seed entity.
see design/superpowers/specs/2026-11-14-task-context-files-only-design.md (part A)
"""

import json

import pytest

from sidegraph.engine.reader import GraphifyReader
from sidegraph.retrieval import (
    Seed,
    _fmt_decision,
    get_task_context,
    query_decisions,
    resolve_seeds,
)
from sidegraph.schema import DecisionKind, Descriptor, Entity
from sidegraph.store import Store
from tests.test_retrieval_named_first import (
    FILE,
    FILES_ONLY,
    NAMED,
    _evidence,
    _record,
    _room_for,
    _world,
)

NOTES = "docs/notes.md"  # a file the graph does not hold
A, B, C, D = "pkg/a.py", "pkg/b.py", "pkg/c.py", "pkg/d.py"


def _files_world(tmp_path, a_node="fa"):
    """Four files. ``a.py`` and ``d.py`` each have a file node with a stored entity. ``b.py``
    has a file node and a symbol node labelled like it (``b.py()``); the one stored entity
    spelled ``b.py`` was last seen on that symbol. ``c.py`` holds one symbol. ``a_node`` is the
    node the store last saw ``a.py``'s entity on."""
    nodes = [
        ("fa", "a.py", A),
        ("bar", "bar()", A),
        ("fb", "b.py", B),
        ("b_sym", "b.py()", B),
        ("qux", "qux()", B),
        ("baz", "baz()", C),
        ("fd", "d.py", D),
    ]
    path = tmp_path / "files.json"
    path.write_text(
        json.dumps(
            {
                "built_at_commit": "abc123",
                "nodes": [
                    {"id": node, "label": label, "file_type": "code", "source_file": file_path}
                    for node, label, file_path in nodes
                ],
                "links": [],
            }
        )
    )
    store = Store(tmp_path / "files-store")
    entities = {}
    for node, name, file_path in [
        (a_node, "a.py", A),
        ("bar", "bar", A),
        ("b_sym", "b.py", B),
        ("qux", "qux", B),
        ("baz", "baz", C),
        ("fd", "d.py", D),
    ]:
        entities[node if node != a_node else "fa"] = store.upsert_entity(
            Entity(
                canonical_name=name,
                descriptor=Descriptor(name=name, file_path=file_path),
                last_seen_node_id=node,
            )
        )
    return store, GraphifyReader(path), entities


def test_resolve_seeds_reports_the_entities_on_a_files_own_node(tmp_path):
    store, reader, e = _world(tmp_path)
    res = resolve_seeds(FILES_ONLY, reader, store)
    assert res.whole_file_entity_ids == frozenset({e["file"].entity_id})
    # A name seed alone reaches no file node.
    assert resolve_seeds(
        [Seed(name="foo", file_path=FILE)], reader, store
    ).whole_file_entity_ids == (frozenset())


def test_an_entity_the_store_last_saw_on_the_file_node_is_the_file_whatever_its_name(tmp_path):
    store, reader, e = _world(tmp_path)
    # Stored with no path and another name, but the store last saw it on the file's own node.
    legacy = store.upsert_entity(
        Entity(canonical_name="mod", descriptor=Descriptor(name="mod"), last_seen_node_id="file")
    )
    res = resolve_seeds(FILES_ONLY, reader, store)
    assert res.whole_file_entity_ids == frozenset({e["file"].entity_id, legacy.entity_id})


def test_a_symbol_named_like_its_file_is_not_the_file_as_a_whole(tmp_path):
    store, reader, e = _files_world(tmp_path)
    adr = _record(store, [e["b_sym"]], "symbol-adr", DecisionKind.ADR, age_days=5)
    mistake = _record(store, [e["qux"]], "qux-gotcha", DecisionKind.GOTCHA)
    seeds = [Seed(file_path=B)]
    # The file node's label reaches the entity, but the store last saw it on a symbol's node.
    res = resolve_seeds(seeds, reader, store)
    assert e["b_sym"].entity_id in {x.entity_id for x in res.seed_entities}
    assert res.whole_file_entity_ids == frozenset()
    # Nothing is a file record, so the plain order holds, also under a budget for one line.
    assert get_task_context(seeds, store, reader).shown_ids == [mistake.id, adr.id]
    assert get_task_context(seeds, store, reader, _room_for(adr)).shown_ids == [mistake.id]


def test_a_record_bound_to_the_own_nodes_of_two_named_files_is_a_file_record(tmp_path):
    store, reader, e = _files_world(tmp_path)
    mistake = _record(store, [e["bar"]], "bar-gotcha", DecisionKind.GOTCHA)
    both = _record(store, [e["fa"], e["fd"]], "both-adr", DecisionKind.ADR, age_days=5)
    ctx = get_task_context([Seed(file_path=A), Seed(file_path=D)], store, reader)
    assert ctx.shown_ids == [both.id, mistake.id]


def test_a_record_bound_to_the_file_and_to_a_symbol_of_an_unnamed_file_is_a_file_record(
    tmp_path,
):
    store, reader, e = _files_world(tmp_path)
    mistake = _record(store, [e["bar"]], "bar-gotcha", DecisionKind.GOTCHA)
    across = _record(store, [e["fa"], e["baz"]], "across-adr", DecisionKind.ADR, age_days=5)
    ctx = get_task_context([Seed(file_path=A)], store, reader)
    assert ctx.shown_ids == [across.id, mistake.id]


def test_file_record_takes_the_budget_before_a_symbols_mistake(tmp_path):
    store, reader, e = _world(tmp_path)
    mistake = _record(store, [e["bar"]], "bar-gotcha", DecisionKind.GOTCHA)
    whole = _record(store, [e["file"]], "file-adr", DecisionKind.ADR, age_days=5)
    ctx = get_task_context(FILES_ONLY, store, reader, _room_for(whole))
    assert ctx.shown_ids == [whole.id]
    assert ctx.mistakes == []
    # With room for both, each is in its own section and the file's record was placed first.
    roomy = get_task_context(FILES_ONLY, store, reader)
    assert roomy.shown_ids == [whole.id, mistake.id]
    assert any("bar-gotcha" in line for line in roomy.mistakes)
    assert any("file-adr" in line for line in roomy.decisions)


def test_files_only_call_places_file_records_then_mistakes_then_adrs(tmp_path):
    store, reader, e = _world(tmp_path)
    adr = _record(store, [e["foo"]], "foo-adr", DecisionKind.ADR)
    older = _record(store, [e["bar"]], "bar-gotcha", DecisionKind.GOTCHA, age_days=5)
    newer = _record(store, [e["foo"]], "foo-gotcha", DecisionKind.GOTCHA, age_days=1)
    whole = _record(store, [e["file"]], "file-adr", DecisionKind.ADR, age_days=9)
    ctx = get_task_context(FILES_ONLY, store, reader)
    assert ctx.shown_ids[0] == whole.id
    assert set(ctx.shown_ids[1:3]) == {older.id, newer.id}
    assert ctx.shown_ids[3] == adr.id
    # The sections are unchanged: the file's ADR leads the decisions section.
    assert [line.split(":")[0] for line in ctx.decisions] == ["- [adr] file-adr", "- [adr] foo-adr"]


def test_record_bound_to_the_file_and_to_a_symbol_is_not_in_the_group(tmp_path):
    store, reader, e = _world(tmp_path)
    mistake = _record(store, [e["bar"]], "bar-gotcha", DecisionKind.GOTCHA)
    shared = _record(store, [e["file"], e["foo"]], "shared-adr", DecisionKind.ADR, age_days=5)
    ctx = get_task_context(FILES_ONLY, store, reader)
    assert ctx.shown_ids == [mistake.id, shared.id]


def test_file_mistake_is_placed_before_the_file_adr(tmp_path):
    store, reader, e = _world(tmp_path)
    adr = _record(store, [e["file"]], "file-adr", DecisionKind.ADR)
    mistake = _record(store, [e["file"]], "file-gotcha", DecisionKind.GOTCHA, age_days=5)
    ctx = get_task_context(FILES_ONLY, store, reader, _room_for(mistake))
    assert ctx.shown_ids == [mistake.id]
    assert get_task_context(FILES_ONLY, store, reader).shown_ids == [mistake.id, adr.id]


def test_evidence_of_a_file_adr_never_takes_a_symbols_mistake_line(tmp_path):
    store, reader, e = _world(tmp_path)
    other = _record(store, [e["bar"]], "bar-gotcha", DecisionKind.GOTCHA)
    whole = _record(store, [e["file"]], "file-adr", DecisionKind.ADR, age_days=5)
    _evidence(store, whole)
    ctx = get_task_context(FILES_ONLY, store, reader, _room_for(whole, other))
    assert ctx.full_body_ids == [whole.id, other.id]
    assert not any("evidence:" in line for line in [*ctx.mistakes, *ctx.decisions])


def test_evidence_of_a_file_adr_follows_its_line_after_the_mistakes_evidence(tmp_path):
    store, reader, e = _world(tmp_path)
    other = _record(store, [e["bar"]], "bar-gotcha", DecisionKind.GOTCHA)
    whole = _record(store, [e["file"]], "file-adr", DecisionKind.ADR, age_days=5)
    of_whole = _evidence(store, whole, "about the file adr")
    of_mistake = _evidence(store, other, "about the gotcha")
    ctx = get_task_context(FILES_ONLY, store, reader)
    assert [line.split(":")[0].strip() for line in ctx.decisions] == [
        "- [adr] file-adr",
        "evidence",
    ]
    assert ctx.shown_ids == [whole.id, other.id, of_mistake.id, of_whole.id]


def test_a_call_that_names_a_symbol_keeps_the_named_order(tmp_path):
    store, reader, e = _world(tmp_path)
    mistake = _record(store, [e["bar"]], "bar-gotcha", DecisionKind.GOTCHA)
    named = _record(store, [e["foo"]], "foo-adr", DecisionKind.ADR, age_days=5)
    whole = _record(store, [e["file"]], "file-adr", DecisionKind.ADR, age_days=9)
    ctx = get_task_context(NAMED, store, reader)
    assert ctx.shown_ids == [named.id, mistake.id, whole.id]


def test_a_file_the_graph_does_not_hold_keeps_the_old_order(tmp_path):
    store, reader, _ = _world(tmp_path)
    as_file = store.upsert_entity(
        Entity(canonical_name="notes.md", descriptor=Descriptor(name="notes.md", file_path=NOTES))
    )
    section = store.upsert_entity(
        Entity(canonical_name="Setup", descriptor=Descriptor(name="Setup", file_path=NOTES))
    )
    whole = _record(store, [as_file], "notes-adr", DecisionKind.ADR, age_days=5)
    mistake = _record(store, [section], "setup-gotcha", DecisionKind.GOTCHA)
    ctx = get_task_context([Seed(file_path=NOTES)], store, reader)
    # No graph node, so no entity of this file is the file as a whole: mistakes first.
    assert ctx.shown_ids == [mistake.id, whole.id]


def test_query_decisions_shares_the_file_group(tmp_path):
    store, reader, e = _world(tmp_path)
    _record(store, [e["bar"]], "bar-gotcha", DecisionKind.GOTCHA)
    whole = _record(store, [e["file"]], "file-adr", DecisionKind.ADR, age_days=5)
    text = query_decisions(
        FILES_ONLY, store, reader, budget_chars=len(_fmt_decision(whole, detailed=True))
    )
    assert "file-adr" in text and "bar-gotcha" not in text


@pytest.mark.parametrize(
    "mapping", [None, "gone-node"], ids=["unmapped", "mapped-to-a-missing-node"]
)
def test_an_entity_reached_only_by_the_label_is_the_file_when_the_store_holds_no_node_for_it(
    tmp_path, mapping
):
    store, reader, e = _files_world(tmp_path, a_node=mapping)
    mistake = _record(store, [e["bar"]], "bar-gotcha", DecisionKind.GOTCHA)
    whole = _record(store, [e["fa"]], "file-adr", DecisionKind.ADR, age_days=5)
    seeds = [Seed(file_path=A)]
    # No node maps to the entity, so only the file node's label reaches it, and it is the file.
    res = resolve_seeds(seeds, reader, store)
    assert e["fa"].entity_id in res.whole_file_entity_ids
    assert get_task_context(seeds, store, reader).shown_ids == [whole.id, mistake.id]
    assert get_task_context(seeds, store, reader, _room_for(whole)).shown_ids == [whole.id]


def test_a_record_bound_to_a_file_and_to_an_entity_of_another_named_file_is_not_a_file_record(
    tmp_path,
):
    store, reader, e = _files_world(tmp_path)
    section = store.upsert_entity(
        Entity(canonical_name="Setup", descriptor=Descriptor(name="Setup", file_path=NOTES))
    )
    mistake = _record(store, [e["bar"]], "bar-gotcha", DecisionKind.GOTCHA)
    across = _record(store, [e["fa"], section], "across-adr", DecisionKind.ADR, age_days=5)
    # `notes.md` is a file the graph does not hold, so its entity is no file as a whole and
    # disqualifies a record that also names it.
    ctx = get_task_context([Seed(file_path=A), Seed(file_path=NOTES)], store, reader)
    assert ctx.shown_ids == [mistake.id, across.id]


def test_file_records_of_one_kind_are_placed_newest_first(tmp_path):
    store, reader, e = _world(tmp_path)
    older = _record(store, [e["file"]], "older-adr", DecisionKind.ADR, age_days=9)
    newer = _record(store, [e["file"]], "newer-adr", DecisionKind.ADR, age_days=2)
    ctx = get_task_context(FILES_ONLY, store, reader, _room_for(newer))
    assert ctx.shown_ids == [newer.id]
    assert get_task_context(FILES_ONLY, store, reader).shown_ids == [newer.id, older.id]
