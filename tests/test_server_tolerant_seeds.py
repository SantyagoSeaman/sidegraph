"""``get_task_context`` reads a wrong seed the way the agent meant it, and an empty answer says why.

The wiring of the seed ladder into the tool: the "How your seeds were read" block, the
not-in-graph block that now reports the ladder's leftovers, the "Why this is empty" block for a
call with no seeds, the telemetry paths, and the fix that keeps a seed whose name starts with
two dots in ``_normalized_seeds``. The ladder's own rules are in ``test_seed_ladder.py``.
# see design/superpowers/specs/2026-10-02-tolerant-seeds-design.md (D4-D8, tests A6, A7, A9, A10,
# A13-A16)
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from sidegraph import seed_ladder
from sidegraph.engine.reader import GraphifyReader
from sidegraph.retrieval import RetrievalBudget
from sidegraph.schema import (
    AnchorBinding,
    Decision,
    DecisionKind,
    DecisionStatus,
    Descriptor,
    Domain,
    Entity,
    Provenance,
    Scope,
)
from sidegraph.server import (
    _get_task_context_impl,
    _normalized_seeds,
    _retrieve,
    _seeds_from_args,
)
from sidegraph.store import Store
from tests.test_graph_freshness import stale_repo, write_graph
from tests.test_seed_ladder import make_reader

READ = "## How your seeds were read"
NOT_IN_GRAPH = "## Not in the code graph"
EMPTY = "## Why this is empty"


def _call(tmp_path, reader, files=None, entities=None, store=None) -> str:
    own = store is None
    store = store or Store(tmp_path / "t.db")
    try:
        return _get_task_context_impl(
            store,
            reader,
            files=files,
            entities=entities,
            structure_budget=4000,
            memory_budget=2000,
        )
    finally:
        if own:
            store.close()


def _bare_render(tmp_path, reader, files=None, entities=None) -> str:
    store = Store(tmp_path / "bare.db")
    try:
        seeds = _seeds_from_args(files, entities)
        return _retrieve(seeds, store, reader, RetrievalBudget(4000, 2000)).render()
    finally:
        store.close()


def _section(out: str, heading: str) -> list[str]:
    """The lines of one block, heading excluded, up to the next blank line."""
    assert heading in out, out
    return out.split(heading + "\n", 1)[1].split("\n\n", 1)[0].splitlines()


# -- the block --------------------------------------------------------------------------


def test_a_guessed_seed_is_read_and_the_reply_says_how(tmp_path):
    fx = stale_repo(tmp_path)
    reader = GraphifyReader(fx.graph)

    out = _call(tmp_path, reader, files=["app/m.py"])

    assert "No context found." not in out
    assert "fn_0()" in out  # the map of pkg/m.py
    assert _section(out, READ) == [
        "- `app/m.py` → read as `pkg/m.py` (guessed: the only file with that name)"
    ]
    assert NOT_IN_GRAPH not in out


def test_the_blocks_follow_the_render_in_order_a_blank_line_apart(tmp_path):
    fx = stale_repo(tmp_path)
    (fx.repo / "pkg" / "n.py").write_text("def b():\n    pass\n")
    reader = GraphifyReader(fx.graph)

    out = _call(tmp_path, reader, files=["app/m.py", "pkg/n.py"])

    assert out.index("fn_0()") < out.index(READ) < out.index(NOT_IN_GRAPH)
    assert "\n\n" + READ + "\n" in out and "\n\n" + NOT_IN_GRAPH + "\n" in out
    read_end = out.index("\n\n" + NOT_IN_GRAPH)
    assert out[:read_end].endswith("(guessed: the only file with that name)")


def test_the_block_is_for_get_task_context_alone(tmp_path):
    from sidegraph.server import _query_decisions_impl

    fx = stale_repo(tmp_path)
    reader = GraphifyReader(fx.graph)
    store = Store(tmp_path / "q.db")
    try:
        out = _query_decisions_impl(store, reader, ["app/m.py"], None, 6000)
    finally:
        store.close()

    assert READ not in out and EMPTY not in out


def test_a9_an_unresolved_name_only_entity_gets_its_own_line(tmp_path):
    reader = make_reader(tmp_path, ["pkg/m.py"])

    out = _call(tmp_path, reader, entities=[{"name": "Frobnicate"}])

    assert out.startswith("No context found.\n\n" + READ + "\n")
    assert _section(out, READ) == ["- `Frobnicate` → matches no symbol in the code graph"]
    assert NOT_IN_GRAPH not in out and EMPTY not in out


def test_a_name_missing_from_a_file_the_graph_holds_is_not_a_bare_empty_answer(tmp_path):
    reader = make_reader(tmp_path, ["pkg/m.py"])

    out = _call(tmp_path, reader, entities=[{"name": "NoSuchThing", "file_path": "pkg/m.py"}])

    assert out.startswith("No context found.\n\n" + READ + "\n")
    assert _section(out, READ) == ["- `NoSuchThing (pkg/m.py)` → matches no symbol in `pkg/m.py`"]
    assert NOT_IN_GRAPH not in out


def test_a_name_whose_matches_all_lack_a_file_is_not_expanded(tmp_path):
    """Two engine artifacts answer to `Ghost`, neither with a file, and one of them has a
    neighbour that does. An ambiguous seed is never expanded: the map must not grow that
    neighbour out of it."""
    reader = make_reader(
        tmp_path,
        ["x.py"],
        extra=[
            {"id": "g1", "label": "Ghost", "file": ""},
            {"id": "g2", "label": "Ghost", "file": ""},
            {"id": "nb", "label": "GhostNeighbour", "file": "x.py"},
        ],
        links=[{"source": "g1", "target": "nb", "relation": "calls"}],
    )

    out = _call(tmp_path, reader, entities=[{"name": "Ghost"}])

    assert "GhostNeighbour" not in out
    assert _section(out, READ) == [
        "- `Ghost` → matches 2 symbols with no source file in the code graph"
    ]


def test_a10_seeds_that_resolve_as_given_leave_the_bytes_alone(tmp_path):
    reader = make_reader(
        tmp_path, ["pkg/m.py"], extra=[{"id": "thing", "label": "Thing", "file": "pkg/m.py"}]
    )
    files, entities = ["pkg/m.py"], [{"name": "Thing", "file_path": "pkg/m.py"}, {"name": "thing"}]

    out = _call(tmp_path, reader, files=files, entities=entities)

    assert out == _bare_render(tmp_path, reader, files=files, entities=entities)
    assert READ not in out and NOT_IN_GRAPH not in out and EMPTY not in out


def test_a_ladder_that_fails_leaves_the_seeds_as_they_were(tmp_path, monkeypatch):
    fx = stale_repo(tmp_path)
    reader = GraphifyReader(fx.graph)

    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(seed_ladder, "tolerate", boom)

    out = _call(tmp_path, reader, files=["app/m.py"])

    assert out.startswith(_bare_render(tmp_path, reader, files=["app/m.py"]))
    assert READ not in out


def test_a_failure_in_the_block_leaves_the_text_untouched(tmp_path, monkeypatch):
    fx = stale_repo(tmp_path)
    reader = GraphifyReader(fx.graph)

    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(seed_ladder, "read_block", boom)

    out = _call(tmp_path, reader, files=["app/m.py"])

    assert "fn_0()" in out and READ not in out


# -- A6, A7: what the not-in-graph block still says -----------------------------------------


def test_a6_a_file_on_disk_the_graph_lacks_is_not_replaced_by_a_namesake(tmp_path):
    fx = stale_repo(tmp_path)
    (fx.repo / "pkg" / "n.py").write_text("def b():\n    pass\n")
    write_graph(fx.graph, fx.first, ["pkg/m.py", "other/n.py"])
    reader = GraphifyReader(fx.graph)

    out = _call(tmp_path, reader, files=["pkg/n.py"])

    assert READ not in out
    (line,) = _section(out, NOT_IN_GRAPH)
    assert line.startswith("1 of 1 seed path exists but is not in the code graph: pkg/n.py.")
    assert "The graph is stale" in line and "other/n.py" not in out


def test_a7_a_path_outside_the_repository_keeps_the_check_the_path_sentence(tmp_path):
    fx = stale_repo(tmp_path)
    reader = GraphifyReader(fx.graph)

    out = _call(tmp_path, reader, files=[".", "/somewhere/else/m.py", "../m.py"])

    (line,) = _section(out, NOT_IN_GRAPH)
    assert line.startswith(
        "3 of 3 seed paths are not repo-relative paths to files in this repository: "
        "., /somewhere/else/m.py, ../m.py."
    )
    assert "Check the path" in line
    assert READ not in out


def test_a_wrong_case_path_is_not_reported_as_a_file_that_exists(tmp_path, monkeypatch):
    """On a case-insensitive filesystem `PKG/n.py` "exists" because `pkg/n.py` does. The block
    asks the same case-exact question the ladder does: the spelling is wrong, so check the path."""
    from tests.test_seed_ladder import case_insensitive_fs

    fx = stale_repo(tmp_path)
    (fx.repo / "pkg" / "n.py").write_text("def b():\n    pass\n")
    case_insensitive_fs(monkeypatch, fx.repo.resolve())
    reader = GraphifyReader(fx.graph)

    out = _call(tmp_path, reader, files=["PKG/n.py"])

    (line,) = _section(out, NOT_IN_GRAPH)
    assert line.startswith("1 of 1 seed path is not a repo-relative path to a file")
    assert "exists but is not in the code graph" not in out


# -- A13: the denominator and the normalised spelling ----------------------------------------


def test_a13_the_denominator_stays_the_given_count_and_the_path_is_the_normalised_one(tmp_path):
    fx = stale_repo(tmp_path)
    (fx.repo / "pkg" / "n.py").write_text("def b():\n    pass\n")
    reader = GraphifyReader(fx.graph)

    out = _call(tmp_path, reader, files=["./pkg/n.py", "pkg/m.py"])

    (line,) = _section(out, NOT_IN_GRAPH)
    assert line.startswith("1 of 2 seed paths exists but is not in the code graph: pkg/n.py.")
    assert "The graph is stale" in line
    assert "Check the path" not in out


def test_a13_every_spelling_of_the_same_file_counts_toward_the_denominator(tmp_path):
    fx = stale_repo(tmp_path)
    (fx.repo / "pkg" / "n.py").write_text("def b():\n    pass\n")
    reader = GraphifyReader(fx.graph)
    absolute = str(fx.repo / "pkg" / "n.py")

    out = _call(tmp_path, reader, files=[absolute, "./pkg/n.py", "pkg/n.py:7"])

    (line,) = _section(out, NOT_IN_GRAPH)
    assert line.startswith("1 of 3 seed paths exists but is not in the code graph: pkg/n.py.")


# -- A14: an empty answer with no seeds ----------------------------------------------------


def _domain(store: Store, slug: str, title: str, *, accept: bool = True) -> Domain:
    d = store.add_domain(
        Domain(slug=slug, title=title, summary="s", provenance=Provenance(source="manual"))
    )
    if accept:
        store.ratify_domains(accept=[d.domain_id])
    return d


def test_a14_no_seeds_and_nothing_to_say_names_the_accepted_domains(tmp_path):
    reader = make_reader(tmp_path, ["pkg/m.py"])
    store = Store(tmp_path / "d.db")
    _domain(store, "payments", "Payments")
    _domain(store, "auth", "Auth")
    gone = _domain(store, "legacy", "Legacy")
    store.supersede_domain(
        gone.domain_id,
        Domain(
            slug="legacy",
            title="Legacy",
            summary="s",
            supersedes=gone.domain_id,
            provenance=Provenance(source="manual"),
        ),
    )
    _domain(store, "proposed-only", "Proposed only", accept=False)

    out = _call(tmp_path, reader, store=store)

    assert out.startswith("No context found.\n\n" + EMPTY + "\n")
    assert _section(out, EMPTY) == [
        "No files or entities were given, so nothing could be matched. Pass files=[…] with the "
        "repo-relative paths you are working on, or drill_down(<slug>) for a named area: "
        "Auth (auth), Payments (payments)."
    ]
    assert "Legacy" not in out and "Proposed only" not in out
    store.close()


def test_a14_with_no_accepted_domain_the_second_sentence_stops_at_working_on(tmp_path):
    reader = make_reader(tmp_path, ["pkg/m.py"])

    out = _call(tmp_path, reader)

    assert out == (
        "No context found.\n\n" + EMPTY + "\nNo files or entities were given, so nothing "
        "could be matched. Pass files=[…] with the repo-relative paths you are working on."
    )


def test_a14_at_most_twelve_domains_are_listed(tmp_path):
    reader = make_reader(tmp_path, ["pkg/m.py"])
    store = Store(tmp_path / "d.db")
    for i in range(14):
        _domain(store, f"area-{i:02d}", f"Area {i:02d}")

    out = _call(tmp_path, reader, store=store)

    (line,) = _section(out, EMPTY)
    assert line.endswith("Area 10 (area-10), Area 11 (area-11), and 2 more.")
    assert "Area 12" not in line and line.count("(area-") == 12
    store.close()


@pytest.mark.parametrize(
    ("files", "entities"),
    [([""], None), (None, [{}]), ([""], [{}, {"name": "", "file_path": ""}])],
    ids=["empty path", "empty entity", "both"],
)
def test_a14_a_degenerate_seed_counts_as_no_seed(tmp_path, files, entities):
    reader = make_reader(tmp_path, ["pkg/m.py"])

    out = _call(tmp_path, reader, files=files, entities=entities)

    assert out.startswith("No context found.\n\n" + EMPTY + "\n")
    assert READ not in out and NOT_IN_GRAPH not in out


def test_a14_a_blank_seed_counts_as_no_seed_too(tmp_path):
    reader = make_reader(tmp_path, ["pkg/m.py"])

    out = _call(
        tmp_path,
        reader,
        files=["  ", "\t"],
        entities=[{"name": " ", "file_path": "\n"}, {"name": "", "file_path": "  "}],
    )

    assert out.startswith("No context found.\n\n" + EMPTY + "\n")
    assert READ not in out and NOT_IN_GRAPH not in out


def test_a14_with_no_seeds_and_no_graph_it_says_to_build_the_graph_first(tmp_path):
    out = _call(tmp_path, None)

    (line,) = _section(out, EMPTY)
    assert line == (
        "No files or entities were given, so nothing could be matched. There is no code graph "
        "to match against either: build it from the repository root with `graphify update .` "
        "first. Pass files=[…] with the repo-relative paths you are working on."
    )
    assert "## No code graph" not in out  # that block is for a call that named a seed


def test_a14_a_seed_with_no_graph_still_gets_the_no_graph_block_alone(tmp_path):
    out = _call(tmp_path, None, files=["pkg/m.py"])

    assert "## No code graph" in out and EMPTY not in out


def test_a14_a_call_with_seeds_never_gets_the_block(tmp_path):
    fx = stale_repo(tmp_path)
    reader = GraphifyReader(fx.graph)

    out = _call(tmp_path, reader, files=["Nope.py"])

    assert EMPTY not in out


def test_a14_a_render_with_memory_in_it_is_left_alone(tmp_path):
    reader = make_reader(tmp_path, ["pkg/m.py"])
    store = Store(tmp_path / "g.db")
    store.add_decision(
        Decision(
            title="A global rule",
            kind=DecisionKind.CONSTRAINT,
            status=DecisionStatus.ACCEPTED,
            scope=Scope.GLOBAL,
            context="c",
            choice="ch",
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual"),
        )
    )

    out = _call(tmp_path, reader, store=store)

    assert "A global rule" in out and EMPTY not in out
    store.close()


# -- A12: a record stored under the original spelling ----------------------------------------


def test_a12_a_record_stored_under_the_original_file_still_surfaces_after_a_rewrite(tmp_path):
    """The record was written while the file was ``app/old/cache.py``; the graph now holds
    ``Cache`` in ``src/cache.py``. The seed is rewritten to the new file, and its original
    spelling still reaches the stored descriptor."""
    reader = make_reader(
        tmp_path,
        ["src/cache.py"],
        extra=[{"id": "cache", "label": "Cache", "file": "src/cache.py"}],
    )
    store = Store(tmp_path / "t.db")
    old = store.upsert_entity(
        Entity(
            canonical_name="Cache",
            descriptor=Descriptor(name="Cache", file_path="app/old/cache.py"),
        )
    )
    record = store.add_decision(
        Decision(
            title="Cache keeps three entries",
            kind=DecisionKind.GOTCHA,
            status=DecisionStatus.ACCEPTED,
            context="c",
            choice="ch",
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual"),
        )
    )
    store.add_binding(
        AnchorBinding(record_id=record.id, entity_id=old.entity_id, tier=2, status="live")
    )

    out = _call(
        tmp_path,
        reader,
        entities=[{"name": "Cache", "file_path": "app/old/cache.py"}],
        store=store,
    )

    assert "Cache keeps three entries" in out
    assert "- `Cache (app/old/cache.py)` → read as `Cache` in `src/cache.py`" in out
    store.close()


def test_a12_a_rewritten_entity_seed_keeps_its_exact_store_match_in_mistakes(tmp_path):
    """Beside a file seed that resolved, an entity seed whose file is wrong is rewritten. The
    original still matches the descriptor a gotcha was stored under, and that gotcha stays a
    mistake with its id, as it was before seeds were read tolerantly."""
    reader = make_reader(
        tmp_path,
        ["pkg/m.py", "src/cache.py"],
        extra=[{"id": "cache", "label": "Cache", "file": "src/cache.py"}],
    )
    store = Store(tmp_path / "t.db")
    old = store.upsert_entity(
        Entity(
            canonical_name="Cache",
            descriptor=Descriptor(name="Cache", file_path="app/old/cache.py"),
        )
    )
    gotcha = store.add_decision(
        Decision(
            title="Cache keeps three entries",
            kind=DecisionKind.GOTCHA,
            status=DecisionStatus.ACCEPTED,
            context="c",
            choice="ch",
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual"),
        )
    )
    store.add_binding(
        AnchorBinding(record_id=gotcha.id, entity_id=old.entity_id, tier=2, status="live")
    )

    out = _call(
        tmp_path,
        reader,
        files=["pkg/m.py"],
        entities=[{"name": "Cache", "file_path": "app/old/cache.py"}],
        store=store,
    )

    mistakes = out.split("## ⚠ Known mistakes & gotchas\n", 1)[1].split("\n\n", 1)[0]
    assert "Cache keeps three entries" in mistakes and f"(id: {gotcha.id})" in mistakes
    store.close()


def test_a12_a_record_stored_under_an_ambiguous_name_and_a_wrong_file_is_still_a_mistake(
    tmp_path,
):
    """`Dup` is ambiguous across two files, so nothing is read; but the gotcha stored under
    `(Dup, app/gone/dup.py)` is found by its own descriptor and keeps its tier."""
    reader = make_reader(
        tmp_path,
        ["a.py", "b.py"],
        extra=[
            {"id": "da", "label": "Dup", "file": "a.py"},
            {"id": "db", "label": "Dup", "file": "b.py"},
        ],
    )
    store = Store(tmp_path / "t.db")
    old = store.upsert_entity(
        Entity(
            canonical_name="Dup",
            descriptor=Descriptor(name="Dup", file_path="app/gone/dup.py"),
        )
    )
    gotcha = store.add_decision(
        Decision(
            title="Dup is not unique",
            kind=DecisionKind.GOTCHA,
            status=DecisionStatus.ACCEPTED,
            context="c",
            choice="ch",
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual"),
        )
    )
    store.add_binding(
        AnchorBinding(record_id=gotcha.id, entity_id=old.entity_id, tier=2, status="live")
    )

    out = _call(
        tmp_path,
        reader,
        entities=[{"name": "Dup", "file_path": "app/gone/dup.py"}],
        store=store,
    )

    assert "No context found." not in out
    mistakes = out.split("## ⚠ Known mistakes & gotchas\n", 1)[1].split("\n\n", 1)[0]
    assert f"(id: {gotcha.id})" in mistakes
    assert "→ ambiguous, did you mean `Dup (a.py)`, `Dup (b.py)`?" in out
    store.close()


def test_an_entity_in_a_file_that_exists_but_is_not_in_the_graph_gets_the_stale_advice(
    tmp_path,
):
    fx = stale_repo(tmp_path)
    (fx.repo / "pkg" / "n.py").write_text("def a():\n    pass\n")
    reader = GraphifyReader(fx.graph)  # holds `fn_0()` in pkg/m.py

    out = _call(tmp_path, reader, entities=[{"name": "fn_0", "file_path": "pkg/n.py"}])

    assert READ not in out
    (line,) = _section(out, NOT_IN_GRAPH)
    assert line.startswith("1 of 1 seed path exists but is not in the code graph: pkg/n.py.")
    assert "The graph is stale" in line


# -- A15: telemetry -----------------------------------------------------------------------


def test_a15_a_rewrite_records_the_path_that_was_read(tmp_path):
    reader = make_reader(tmp_path, ["pkg/m.py"])
    store = Store(tmp_path / "t.db")

    _call(tmp_path, reader, files=["app/m.py"], store=store)

    assert store.retrieval_seed_queries() == {"pkg/m.py": 1}
    store.close()


def test_a15_a_directory_records_its_own_key_not_its_files(tmp_path):
    reader = make_reader(tmp_path, ["pkg/a.py", "pkg/b.py", "pkg/c.py"])
    store = Store(tmp_path / "t.db")

    _call(tmp_path, reader, files=["pkg/"], store=store)

    assert store.retrieval_seed_queries() == {"pkg": 1}
    store.close()


def test_a15_an_unresolved_seed_records_as_it_always_did(tmp_path):
    reader = make_reader(tmp_path, ["pkg/m.py"])
    store = Store(tmp_path / "t.db")

    _call(
        tmp_path,
        reader,
        files=["Nope.py", "pkg/m.py"],
        entities=[{"name": "x", "file_path": "src/missing.py"}],
        store=store,
    )

    assert store.retrieval_seed_queries() == {"Nope.py": 1, "pkg/m.py": 1, "src/missing.py": 1}
    store.close()


def test_a15_exact_seeds_record_as_given(tmp_path):
    reader = make_reader(tmp_path, ["pkg/m.py"])
    store = Store(tmp_path / "t.db")

    _call(tmp_path, reader, files=["pkg/m.py"], store=store)

    assert store.retrieval_seed_queries() == {"pkg/m.py": 1}
    store.close()


# -- A16: a name that starts with two dots -------------------------------------------------


def test_a16_a_name_that_starts_with_two_dots_is_inside_the_root(tmp_path):
    store = Store(tmp_path / "t.db")

    kept = _normalized_seeds(store, ["..foo/x.py", "..hidden", "../x.py", "..", "a/../../x.py"])

    assert kept == ["..foo/x.py", "..hidden"]
    store.close()


@pytest.mark.parametrize("seed", ["..foo/x.py", "..env"])
def test_a16_such_a_seed_reaches_the_counter(tmp_path, seed):
    reader = make_reader(tmp_path, ["pkg/m.py"])
    store = Store(tmp_path / "t.db")

    _call(tmp_path, reader, files=[seed], store=store)

    assert store.retrieval_seed_queries() == {seed: 1}
    store.close()
