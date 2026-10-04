"""The read tools of a linked worktree read the main checkout's graph, and a missing graph is said.

``server._synced_reader`` hands back ``(reader, borrowed_from)``; with the store's own graph
missing it opens the main checkout's and syncs it index-only: the worktree's cold index is
derived, and no tracked file of its store is rewritten. ``get_task_context`` words what the
borrowed graph cannot hold (D3) and says so when there is no graph at all (D4). The repositories
are real ones from ``tests.test_config_borrowed_graph``.
see design/superpowers/specs/2026-10-01-worktree-borrowed-graph-design.md (D2-D4, T6-T11)
"""

from __future__ import annotations

import asyncio
import os
import shutil
import stat
from datetime import UTC, datetime
from pathlib import Path

import fastmcp
import pytest

from sidegraph import server
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
    Fact,
    Provenance,
    Scope,
)
from sidegraph.store import Store
from sidegraph.sync import LAST_SYNCED_KEY
from tests.test_config_borrowed_graph import GRAPH, add_worktree, make_main
from tests.test_graph_freshness import commit, git, head, write_graph

BORROWED = (
    "This worktree reads the main checkout's graph, which does not hold files that exist "
    "only on this branch."
)
NO_GRAPH = "## No code graph"


def remember(
    store_path: Path,
    title: str,
    file_path: str,
    name: str = "m_py",
    scope: Scope = Scope.GLOBAL,
) -> None:
    """One accepted gotcha anchored to ``name`` in ``file_path``, in the store at
    ``store_path``."""
    store = Store(store_path)
    try:
        d = store.add_decision(
            Decision(
                title=title,
                kind=DecisionKind.GOTCHA,
                status=DecisionStatus.ACCEPTED,
                context="c",
                choice="ch",
                scope=scope,
                valid_from=datetime.now(UTC),
                provenance=Provenance(source="manual"),
            )
        )
        e = store.upsert_entity(
            Entity(
                canonical_name=name,
                descriptor=Descriptor(name=name, file_path=file_path),
            )
        )
        store.add_binding(
            AnchorBinding(record_id=d.id, entity_id=e.entity_id, tier=2, status="live")
        )
    finally:
        store.close()


def porcelain(repo: Path) -> str:
    return git(repo, "status", "--porcelain")


def serve(monkeypatch, checkout: Path) -> Store:
    """The MCP server started in ``checkout``: its store, its cwd."""
    monkeypatch.chdir(checkout)
    monkeypatch.setenv("SIDEGRAPH_DIR", str(checkout / ".sidegraph"))
    store = Store(checkout / ".sidegraph")
    monkeypatch.setattr(server, "_store", store)
    return store


@pytest.fixture
def main(tmp_path: Path) -> Path:
    """``R``: two files in one community, a global gotcha, a repo-scoped decision anchored in
    ``pkg/n.py``, and an accepted domain ``pkg`` that claims the ``pkg/`` directory."""
    repo = make_main(tmp_path, files=("pkg/m.py", "pkg/n.py"))
    remember(repo / ".sidegraph", "m.py keeps a retry budget", "pkg/m.py")
    remember(
        repo / ".sidegraph", "n.py settles in batches", "pkg/n.py", name="fn_1()", scope=Scope.REPO
    )
    store = Store(repo / ".sidegraph")
    try:
        domain = store.add_domain(
            Domain(
                slug="pkg",
                title="Package",
                summary="Everything under pkg.",
                path_prefixes=["pkg/"],
                provenance=Provenance(source="manual"),
            )
        )
        store.ratify_domains(accept=[domain.domain_id])
    finally:
        store.close()
    commit(repo, "record", {})
    return repo


@pytest.fixture
def worktree(main: Path, monkeypatch) -> Path:
    w = add_worktree(main)
    serve(monkeypatch, w)
    assert porcelain(w) == ""
    return w


def _impl(store, reader, files=None, entities=None, **kwargs) -> str:
    return server._get_task_context_impl(
        store, reader, files, entities, structure_budget=4000, memory_budget=6000, **kwargs
    )


def _block(out: str, heading: str) -> list[str]:
    assert heading in out, out
    return out.split(heading + "\n", 1)[1].split("\n\n", 1)[0].splitlines()


# -- the read tools borrow, and sync index-only -------------------------------------------


def _domain_communities(store: Store) -> list[str]:
    (domain,) = [d for d in store.iter_domains() if d.slug == "pkg"]
    return sorted(domain.communities)


def test_t6_the_read_tools_open_the_main_checkouts_graph_and_sync_index_only(
    main, worktree, monkeypatch
):
    """Red against unfixed code (no reader at all); mutation M10 syncs with canonical writes
    allowed and M5 skips the sync."""
    calls: list[dict] = []
    real = server.maybe_sync
    monkeypatch.setattr(server, "maybe_sync", lambda *a, **k: calls.append(k) or real(*a, **k))

    reader, borrowed_from = server._synced_reader()

    assert reader is not None and Path(reader.path) == main / GRAPH
    assert borrowed_from == main
    assert calls == [{"canonical_writes": False}]
    store = server._get_store()
    assert store.get_meta(LAST_SYNCED_KEY) == f"index-only:{reader.sync_stamp()}"
    assert _domain_communities(store) == ["1"]  # derived index state, not a cold index
    assert porcelain(worktree) == ""


def test_the_main_checkout_and_a_worktree_with_its_own_graph_keep_syncing(
    main, worktree, monkeypatch
):
    synced: list[dict] = []
    monkeypatch.setattr(server, "maybe_sync", lambda *a, **k: synced.append(k))
    serve(monkeypatch, main)

    reader, borrowed_from = server._synced_reader()

    assert reader is not None and Path(reader.path) == main / GRAPH
    assert borrowed_from is None and synced == [{}]  # a full sync, canonical writes allowed

    write_graph(worktree / GRAPH, head(worktree), ["pkg/m.py"])
    serve(monkeypatch, worktree)
    reader, borrowed_from = server._synced_reader()

    assert reader is not None and Path(reader.path) == worktree / GRAPH
    assert borrowed_from is None and synced == [{}, {}]


def test_t7_sync_anchors_in_a_worktree_still_reads_no_graph(worktree):
    """Red against R1 (mutation M2): the write path must not borrow."""
    out = server._sync_anchors_impl(server._get_store(), server._load_reader())

    assert out["synced"] is False
    assert out["error"].startswith("graph not readable (")
    assert str(worktree / GRAPH) in out["error"]


def test_t8_get_task_context_in_a_worktree_returns_the_main_graphs_content(worktree):
    """Red against unfixed code: no reader, so nothing resolves."""
    out = server._get_task_context_with_sync(files=["pkg/m.py"])

    assert "fn_0" in out  # the structural map, from R's graph
    assert "m.py keeps a retry budget" in out  # the worktree's own tracked store
    assert "## Not in the code graph" not in out and NO_GRAPH not in out
    assert porcelain(worktree) == ""


def test_the_other_read_tools_borrow_too(worktree):
    assert "fn_0" in server.query_structure(files=["pkg/m.py"])
    assert "m.py keeps a retry budget" in server.query_decisions(files=["pkg/m.py"])
    assert porcelain(worktree) == ""


def test_a_borrowed_worktree_gets_the_main_checkouts_derived_state(main, monkeypatch):
    """Red against a borrowed graph that is not synced (mutation M5): the worktree's index
    starts cold, so no domain has members, no decision rides a community, and the ``## Related``
    bucket and ``drill_down`` come back empty where the main checkout fills them."""
    serve(monkeypatch, main)
    reader, _ = server._synced_reader()
    main_drill = server._drill_down_impl(server._get_store(), reader, "pkg")
    main_ctx = server._get_task_context_with_sync(files=["pkg/m.py"])
    assert main_drill["decisions"] and "## Related" in main_ctx  # the fixture bites

    worktree = add_worktree(main)
    serve(monkeypatch, worktree)
    reader, borrowed_from = server._synced_reader()
    assert borrowed_from == main
    drill = server._drill_down_impl(server._get_store(), reader, "pkg")
    ctx = server._get_task_context_with_sync(files=["pkg/m.py"])

    assert len(drill["decisions"]) == len(main_drill["decisions"])
    assert drill["members"] == main_drill["members"]
    assert "## Related" in ctx
    assert "n.py settles in batches" in ctx
    assert porcelain(worktree) == ""


def _entity_files(checkout: Path) -> dict[str, str]:
    return {
        f.name: f.read_text() for f in sorted((checkout / ".sidegraph" / "entities").glob("*.json"))
    }


@pytest.fixture
def moved_main(tmp_path: Path) -> Path:
    """The main checkout with a decision anchored at ``pkg/old.py``, then a commit that moves
    the file to ``pkg/m.py`` and a graph rebuilt after it: the moved rung has real history to
    adopt (the new path arrives in the very commit that removes the old one)."""
    repo = make_main(tmp_path, files=("pkg/old.py",))
    remember(repo / ".sidegraph", "old.py kept a cache", "pkg/old.py", name="fn_0()")
    commit(repo, "record the old path", {})
    commit(repo, "move old.py to m.py", {"pkg/old.py": None, "pkg/m.py": "def a():\n    pass\n"})
    write_graph(repo / GRAPH, head(repo), ["pkg/m.py"])
    return repo


def test_a_file_moved_between_the_store_and_the_borrowed_graph_rewrites_no_entity(
    moved_main, monkeypatch
):
    """The moved rung would adopt ``pkg/old.py -> pkg/m.py`` (the name is unique, the old path
    is gone and the main checkout's HEAD confirms it) and rewrite ``entities/<id>.json``. For a
    borrowed graph it abstains: its evidence is the main checkout's tree, not the branch's, and
    the worktree's tracked store must stay as git checked it out (mutation M1)."""
    main = moved_main
    worktree = add_worktree(main)
    serve(monkeypatch, worktree)
    before = _entity_files(worktree)
    assert any("pkg/old.py" in text for text in before.values())

    reader, borrowed_from = server._synced_reader()

    assert borrowed_from == main
    assert _entity_files(worktree) == before
    assert porcelain(worktree) == ""
    assert server._get_store().get_meta(LAST_SYNCED_KEY) == f"index-only:{reader.sync_stamp()}"

    # Control: the same store synced against its own graph does adopt the move.
    serve(monkeypatch, main)
    server._synced_reader()
    assert ".sidegraph/entities/" in porcelain(main)


def test_a_worktree_that_later_gets_an_identical_graph_of_its_own_syncs_it_normally(
    moved_main, monkeypatch
):
    """The index-only stamp is its own value: a byte-identical graph (a copy, a symlinked
    ``graphify-out``, a deterministic rebuild at the same commit) must not read as "already
    synced" to the default sync, which owns the moved rung's adoption (mutation M13)."""
    main = moved_main
    worktree = add_worktree(main)
    serve(monkeypatch, worktree)
    before = _entity_files(worktree)
    server._synced_reader()  # borrowed, index-only
    assert _entity_files(worktree) == before

    (worktree / GRAPH).parent.mkdir(exist_ok=True)
    shutil.copy(main / GRAPH, worktree / GRAPH)
    serve(monkeypatch, worktree)
    reader, borrowed_from = server._synced_reader()

    assert borrowed_from is None and Path(reader.path) == worktree / GRAPH
    assert server._get_store().get_meta(LAST_SYNCED_KEY) == reader.sync_stamp()
    assert _entity_files(worktree) != before  # the default sync adopted the move
    assert "pkg/m.py" in "".join(_entity_files(worktree).values())


# -- a read-only annotation is a promise about tracked files (tool-annotations D2) ----------


def _tracked_bytes(checkout: Path) -> dict[str, bytes | None]:
    """Every tracked file's bytes (``None`` for one deleted from the working tree)."""
    return {
        name: (checkout / name).read_bytes() if (checkout / name).exists() else None
        for name in git(checkout, "ls-files").splitlines()
    }


def test_t2_no_read_only_tool_changes_a_tracked_file(moved_main, monkeypatch):
    """The honesty guard for ``server._READ_ONLY``: on the fixture where a lazy sync adopts a
    moved symbol into ``entities/<id>.json``, every tool annotated ``readOnlyHint`` leaves each
    tracked file as it was. The store holds a fact, a proposed decision and an accepted domain,
    and each tool must return that data: a tool that never reaches its loops cannot show a sync
    hidden inside one (a lazy sync in ``_list_domains_impl``'s per-domain loop stayed green on a
    store with no domain). The control at the end runs a retrieval tool, which does rewrite the
    file, so the fixture holds the very write the guard is for.
    see design/superpowers/specs/2026-10-04-tool-annotations-and-argument-names-design.md
    (D2, T2, A2)
    """
    main = moved_main
    store = serve(monkeypatch, main)  # opens and initialises the store before the snapshot
    now = datetime.now(UTC)
    manual = Provenance(source="manual")
    store.add_fact(
        Fact(
            statement="The cache in old.py was measured at 40 ms per lookup.",
            source="benchmark run",
            valid_from=now,
            provenance=manual,
        )
    )
    store.add_decision(
        Decision(
            title="m.py should batch its retries",
            kind=DecisionKind.LESSON,
            status=DecisionStatus.PROPOSED,
            context="c",
            choice="ch",
            valid_from=now,
            provenance=manual,
        )
    )
    domain = store.add_domain(
        Domain(
            slug="docs",
            title="Documentation",
            summary="Everything under docs.",
            path_prefixes=["docs/"],
            provenance=manual,
        )
    )
    store.ratify_domains(accept=[domain.domain_id])
    commit(main, "a fact, a proposal and a domain", {})
    (entity,) = list(store.iter_concrete_entities())
    assert porcelain(main) == ""
    # Typical arguments, and what the answer must hold, for every tool that is read-only and for
    # the retrieval tool the control runs: a tool annotated read-only later needs a row here.
    typical: dict[str, tuple[dict, str]] = {
        "list_facts": ({}, "The cache in old.py was measured"),
        "find_entity": ({"name": "fn_0()", "file_path": "pkg/old.py"}, entity.entity_id),
        "get_entity_history": ({"entity_id": entity.entity_id}, "old.py kept a cache"),
        "list_proposed": ({}, "m.py should batch its retries"),
        "list_domains": ({}, "Everything under docs."),
        "list_domain_candidates": ({"min_members": 1}, "pkg"),
        "verify_store": ({}, '"clean":true'),
        "get_task_context": ({"files": ["pkg/m.py"]}, "old.py kept a cache"),
    }

    async def run() -> None:
        async with fastmcp.Client(server.mcp) as client:
            read_only = sorted(
                t.name
                for t in await client.list_tools()
                if t.annotations and t.annotations.readOnlyHint
            )
            assert read_only, "no tool is annotated read-only: the guard would check nothing"
            assert set(read_only) <= set(typical), sorted(set(read_only) - set(typical))
            for name in read_only:
                arguments, expected = typical[name]
                before = _tracked_bytes(main)
                result = await client.call_tool(name, arguments, raise_on_error=False)
                answer = "".join(getattr(block, "text", "") for block in result.content)
                assert not result.is_error, (name, answer)
                assert expected in answer, (name, answer)
                assert _tracked_bytes(main) == before, name
                assert porcelain(main) == "", name
            await client.call_tool("get_task_context", typical["get_task_context"][0])
            assert ".sidegraph/entities/" in porcelain(main)  # the fixture bites

    asyncio.run(run())


# -- what a borrowed graph cannot hold (D3) ----------------------------------------------


def test_t9_a_file_that_exists_only_on_the_branch_gets_the_borrowed_sentence(
    main, worktree, monkeypatch
):
    """Red against unfixed code; mutation M3 asks ``freshness()`` about it."""
    commit(worktree, "new on the branch", {"pkg/new.py": "def n():\n    pass\n"})
    calls: list[str] = []
    real = GraphifyReader.freshness
    monkeypatch.setattr(
        GraphifyReader, "freshness", lambda self, *a, **k: calls.append("f") or real(self, *a, **k)
    )

    out = _impl(
        server._get_store(), GraphifyReader(main / GRAPH), ["pkg/new.py"], borrowed_from=main
    )

    (line,) = _block(out, "## Not in the code graph")
    assert line.startswith("1 of 1 seed path exists but is not in the code graph: pkg/new.py. ")
    assert line.endswith(BORROWED)
    assert calls == []


def test_t9b_a_file_in_the_main_checkout_missing_from_its_stale_graph_names_the_main_checkout(
    tmp_path, monkeypatch
):
    """Red against rev 1 (the borrowed sentence for every seed)."""
    repo = make_main(tmp_path)
    commit(repo, "B", {"pkg/m.py": "def a():\n    return 1\n", "pkg/extra.py": "x = 1\n"})
    w = add_worktree(repo)
    serve(monkeypatch, w)

    out = _impl(
        server._get_store(), GraphifyReader(repo / GRAPH), ["pkg/extra.py"], borrowed_from=repo
    )

    (line,) = _block(out, "## Not in the code graph")
    assert line.startswith("1 of 1 seed path exists but is not in the code graph: pkg/extra.py. ")
    assert "The graph is stale (built at" in line
    assert f"rebuild it in {repo} with `graphify update .`" in line
    assert "This worktree reads" not in line


def test_t9b_a_current_main_graph_names_the_main_checkout_in_its_advice(main, worktree):
    commit(main, "C", {"pkg/extra.py": "x = 1\n"})
    git(worktree, "merge", "-q", "--no-edit", "main")
    write_graph(main / GRAPH, head(main), ["pkg/m.py"])

    out = _impl(
        server._get_store(), GraphifyReader(main / GRAPH), ["pkg/extra.py"], borrowed_from=main
    )

    (line,) = _block(out, "## Not in the code graph")
    assert "No committed change since the build explains it" in line
    assert f"rebuild it in {main} with `graphify update .`" in line


def test_a_seed_that_is_in_neither_tree_is_a_path_problem(main, worktree):
    out = _impl(
        server._get_store(), GraphifyReader(main / GRAPH), ["pkg/nope.py"], borrowed_from=main
    )

    (line,) = _block(out, "## Not in the code graph")
    assert "not a repo-relative path to a file in this repository" in line
    assert "This worktree reads" not in line


def test_each_seed_gets_the_sentence_for_its_own_cause(main, worktree):
    commit(worktree, "new on the branch", {"pkg/new.py": "def n():\n    pass\n"})
    commit(main, "B", {"pkg/m.py": "def a():\n    return 1\n", "pkg/extra.py": "x = 1\n"})
    git(worktree, "merge", "-q", "--no-edit", "main")

    out = _impl(
        server._get_store(),
        GraphifyReader(main / GRAPH),
        ["pkg/extra.py", "pkg/new.py", "pkg/nope.py"],
        borrowed_from=main,
    )

    in_main, only_here, other = _block(out, "## Not in the code graph")
    assert "pkg/extra.py" in in_main and "pkg/new.py" not in in_main
    assert "The graph is stale" in in_main
    assert "pkg/new.py" in only_here and only_here.endswith(BORROWED)
    assert "pkg/nope.py" in other


def test_t9c_an_absolute_graph_outside_the_project_is_not_a_borrowed_graph(
    tmp_path, main, monkeypatch
):
    """Red against R4 (a path predicate would call it borrowed)."""
    elsewhere = tmp_path / "elsewhere" / "graph.json"
    write_graph(elsewhere, head(main), ["other.py"])
    monkeypatch.setenv("SIDEGRAPH_GRAPH", str(elsewhere))
    serve(monkeypatch, main)

    reader, borrowed_from = server._synced_reader()
    out = _impl(server._get_store(), reader, ["pkg/m.py"], borrowed_from=borrowed_from)

    assert reader is not None and Path(reader.path) == elsewhere
    assert borrowed_from is None
    assert "This worktree reads" not in out


def test_without_borrowed_from_the_block_keeps_its_original_wording(main):
    commit(main, "B", {"pkg/m.py": "def a():\n    return 1\n", "pkg/extra.py": "x = 1\n"})
    store = Store(main / ".sidegraph")
    try:
        out = _impl(store, GraphifyReader(main / GRAPH), ["pkg/extra.py"])
    finally:
        store.close()

    (line,) = _block(out, "## Not in the code graph")
    assert "rebuild it from the repository root with `graphify update .`, then call again." in line
    assert "rebuild it in" not in line and "This worktree reads" not in line


# -- no graph at all is said (D4) --------------------------------------------------------


def _plain_clone(tmp_path: Path, main: Path, monkeypatch) -> Path:
    clone = tmp_path / "C"
    git(tmp_path, "clone", "-q", "--local", str(main), str(clone))
    serve(monkeypatch, clone)
    return clone


def test_t10_no_graph_anywhere_is_said_with_the_path_looked_at(tmp_path, main, monkeypatch):
    """Red against unfixed code: the answer is the bare render."""
    clone = _plain_clone(tmp_path, main, monkeypatch)

    out = server._get_task_context_with_sync(files=["pkg/m.py"])

    graph = clone / GRAPH
    assert out.endswith(
        f"{NO_GRAPH}\nNo code graph at {graph}: memory anchored to code cannot be looked up. "
        "Build it from the repository root with `graphify update .`."
    )


def test_t10_entity_seeds_count_as_seeds(tmp_path, main, monkeypatch):
    _plain_clone(tmp_path, main, monkeypatch)

    out = server._get_task_context_with_sync(entities=[{"name": "a", "file_path": "pkg/m.py"}])

    assert NO_GRAPH in out


def test_t10b_a_worktree_whose_main_checkout_has_no_graph_names_the_main_checkout(main, worktree):
    """Red against rev 1 (it named the worktree's own path)."""
    (main / GRAPH).unlink()

    out = server._get_task_context_with_sync(files=["pkg/m.py"])

    assert out.endswith(
        f"{NO_GRAPH}\nNo code graph at {main / GRAPH}: memory anchored to code cannot be "
        f"looked up. Build it in the main checkout {main} with `graphify update .`."
    )
    assert str(worktree / GRAPH) not in out


def test_t4f_a_worktree_of_a_bare_dot_git_is_not_told_to_build_in_a_main_checkout(
    tmp_path, main, monkeypatch
):
    holder = tmp_path / "bgit"
    holder.mkdir()
    git(tmp_path, "clone", "-q", "--bare", str(main), str(holder / ".git"))
    feature = holder / "feat"
    git(holder / ".git", "worktree", "add", "-q", str(feature), "main")
    serve(monkeypatch, feature)

    out = server._get_task_context_with_sync(files=["pkg/m.py"])

    assert f"No code graph at {feature / GRAPH}" in out
    assert "main checkout" not in out and str(holder / GRAPH) not in out


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a mode-000 file")
def test_t10c_an_unreadable_graph_is_not_called_missing(tmp_path, main, monkeypatch):
    """Red against rev 1 (every failure was "No code graph at")."""
    clone = _plain_clone(tmp_path, main, monkeypatch)
    write_graph(clone / GRAPH, head(clone), ["pkg/m.py"])
    original = stat.S_IMODE((clone / GRAPH).stat().st_mode)
    (clone / GRAPH).chmod(0)
    try:
        out = server._get_task_context_with_sync(files=["pkg/m.py"])
    finally:
        (clone / GRAPH).chmod(original)

    assert f"The code graph at {clone / GRAPH} is not readable" in out
    assert "No code graph at" not in out


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a mode-000 file")
def test_an_unreadable_main_graph_is_named_with_its_own_path(main, worktree):
    original = stat.S_IMODE((main / GRAPH).stat().st_mode)
    (main / GRAPH).chmod(0)
    try:
        out = server._get_task_context_with_sync(files=["pkg/m.py"])
    finally:
        (main / GRAPH).chmod(original)

    assert f"The code graph at {main / GRAPH} is not readable" in out
    assert f"in the main checkout {main}" in out


def test_t11_without_seeds_the_output_is_unchanged(tmp_path, main, monkeypatch):
    """Red against nothing: a call that named nothing gets no advice about the graph."""
    _plain_clone(tmp_path, main, monkeypatch)
    store = server._get_store()
    bare = server._retrieve([], store, None, RetrievalBudget(4000, 6000)).render()

    assert _impl(store, None) == bare
    assert NO_GRAPH not in bare


def test_a_failing_advice_never_costs_the_answer(tmp_path, main, monkeypatch):
    _plain_clone(tmp_path, main, monkeypatch)

    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(server, "path_state", boom)
    out = server._get_task_context_with_sync(files=["pkg/m.py"])

    assert NO_GRAPH not in out and out  # the render survives


def test_a_reader_present_leaves_the_no_graph_block_out(main):
    store = Store(main / ".sidegraph")
    try:
        out = _impl(store, GraphifyReader(main / GRAPH), ["pkg/m.py"])
    finally:
        store.close()

    assert NO_GRAPH not in out
