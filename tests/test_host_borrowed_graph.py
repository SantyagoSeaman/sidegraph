"""SessionStart in a linked worktree reads the main checkout's graph and says so.

The worktree has the tracked store and no graph. The hook opens the main checkout's graph, syncs
it index-only (the worktree's index starts cold, and a sync that wrote canonical files would
rewrite the tracked store from code that is not the branch's), renders the domain map in memory
from the store, and adds one line saying whose graph it is reading. The repositories are
real ones from ``tests.test_config_borrowed_graph``.
see design/superpowers/specs/2026-10-01-worktree-borrowed-graph-design.md (D2, T12-T12c)
"""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

import sidegraph.host.hooks as hooks
from sidegraph.engine.reader import GraphifyReader
from sidegraph.retrieval import TOC_CACHE_KEY
from sidegraph.schema import (
    Decision,
    DecisionKind,
    DecisionStatus,
    Domain,
    Provenance,
    Scope,
)
from sidegraph.store import Store
from sidegraph.sync import LAST_SYNCED_KEY
from tests.test_config_borrowed_graph import GRAPH, add_worktree, make_main
from tests.test_graph_freshness import commit, git
from tests.test_server_borrowed_graph import remember

BORROWED_LINE = "Sidegraph: this worktree has no code graph of its own, so memory reads the main"


@pytest.fixture
def main(tmp_path: Path) -> Path:
    """``R`` with one accepted domain in its committed store."""
    repo = make_main(tmp_path)
    store = Store(repo / ".sidegraph")
    try:
        domain = store.add_domain(
            Domain(
                slug="payments",
                title="Payments",
                summary="Handles settlement and refunds.",
                path_prefixes=["pkg/"],
                provenance=Provenance(source="manual"),
            )
        )
        store.ratify_domains(accept=[domain.domain_id])
    finally:
        store.close()
    commit(repo, "domain", {})
    return repo


def _session_start(checkout: Path, monkeypatch, capsys) -> str:
    monkeypatch.chdir(checkout)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(checkout))
    monkeypatch.setenv("SIDEGRAPH_DIR", ".sidegraph")
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    hooks.session_start()
    out = json.loads(capsys.readouterr().out)
    return out["hookSpecificOutput"]["additionalContext"]


def _stamp(checkout: Path):
    store = Store(checkout / ".sidegraph")
    try:
        return store.get_meta(LAST_SYNCED_KEY)
    finally:
        store.close()


def _communities(checkout: Path) -> list[str]:
    store = Store(checkout / ".sidegraph")
    try:
        (domain,) = [d for d in store.iter_domains() if d.slug == "payments"]
        return sorted(domain.communities)
    finally:
        store.close()


def test_t12_a_worktree_reads_the_main_graph_renders_its_domains_and_syncs_index_only(
    main, monkeypatch, capsys
):
    """Red against unfixed code (no line, no domain map); mutation M5 skips the sync."""
    worktree = add_worktree(main)
    assert git(worktree, "status", "--porcelain") == ""

    ctx = _session_start(worktree, monkeypatch, capsys)

    assert "## Domains" in ctx and "Payments" in ctx
    (line,) = [ln for ln in ctx.splitlines() if ln.startswith(BORROWED_LINE)]
    assert line == (
        "Sidegraph: this worktree has no code graph of its own, so memory reads the main "
        f"checkout's ({main / GRAPH}); code that exists only on this branch is not in it."
    )
    assert _stamp(worktree) == f"index-only:{GraphifyReader(main / GRAPH).sync_stamp()}"
    assert _communities(worktree) == ["1"]  # derived by the sync, not a cold index
    assert git(worktree, "status", "--porcelain") == ""


def test_t12e_a_moved_file_rewrites_no_entity_when_session_start_syncs_a_borrowed_graph(
    main, monkeypatch, capsys
):
    """The hook's twin of the server's moved-file test: the store names ``pkg/old.py``, R's
    graph holds the symbol in ``pkg/m.py``, and R's HEAD confirms the move, so a sync with
    canonical writes allowed would rewrite ``entities/<id>.json`` in the worktree (mutation
    M10h)."""
    remember(main / ".sidegraph", "old.py kept a cache", "pkg/old.py", name="fn_0()")
    commit(main, "record the old path", {})
    worktree = add_worktree(main)
    entities = worktree / ".sidegraph" / "entities"
    before = {f.name: f.read_text() for f in sorted(entities.glob("*.json"))}
    assert any("pkg/old.py" in text for text in before.values())

    ctx = _session_start(worktree, monkeypatch, capsys)

    assert BORROWED_LINE in ctx
    assert {f.name: f.read_text() for f in sorted(entities.glob("*.json"))} == before
    assert git(worktree, "status", "--porcelain") == ""
    assert _stamp(worktree) is not None  # the sync did run


def test_the_domain_map_of_a_borrowed_graph_is_built_from_the_store_every_time(
    main, monkeypatch, capsys
):
    """A cached map would go stale (a sync that failed, or a record added since): the worktree
    renders ``build_toc`` over the live store. Red against a borrowed branch that trusts the
    cache (mutation M7); the sync is switched off so the cache is the only other source."""
    worktree = add_worktree(main)
    store = Store(worktree / ".sidegraph")
    try:
        store.set_meta(
            TOC_CACHE_KEY,
            json.dumps(
                {
                    "domains": [
                        {
                            "slug": "payments",
                            "title": "Stale cached title",
                            "summary": "Cached.",
                            "parent_slug": None,
                            "mistakes": 0,
                            "subdomains": 0,
                        }
                    ],
                    "initiatives": [],
                    "global_mistakes": [],
                }
            ),
        )
        store.add_decision(
            Decision(
                title="Never retry the settlement call",
                kind=DecisionKind.GOTCHA,
                status=DecisionStatus.ACCEPTED,
                context="c",
                choice="ch",
                scope=Scope.GLOBAL,
                valid_from=datetime.now(UTC),
                provenance=Provenance(source="manual"),
            )
        )
    finally:
        store.close()
    monkeypatch.setattr("sidegraph.sync.maybe_sync", lambda *a, **k: None)

    ctx = _session_start(worktree, monkeypatch, capsys)

    assert "Never retry the settlement call" in ctx
    assert "Stale cached title" not in ctx and "Payments" in ctx


def test_the_domain_map_of_a_borrowed_graph_is_counted_with_the_borrowed_reader(
    main, monkeypatch, capsys
):
    """The worktree builds its map in memory on every start, and its reader is open: with it
    the mistake count includes the decisions anchored to a whole document, the same number the
    main checkout shows. Red against a build that passes no reader."""
    import sidegraph.retrieval as retrieval

    worktree = add_worktree(main)
    readers = []
    real_build_toc = retrieval.build_toc

    def spy(store, reader=None):
        readers.append(reader)
        return real_build_toc(store, reader)

    monkeypatch.setattr(retrieval, "build_toc", spy)

    ctx = _session_start(worktree, monkeypatch, capsys)

    assert "Payments" in ctx
    assert len(readers) == 1 and readers[0] is not None
    assert Path(readers[0].path).resolve() == (main / GRAPH).resolve()


def test_t12_the_map_still_lists_the_graphs_communities_without_a_domain(
    tmp_path, monkeypatch, capsys
):
    """No accepted domain, so the community map: the worktree's reader is the borrowed one."""
    repo = make_main(tmp_path)
    worktree = add_worktree(repo)

    ctx = _session_start(worktree, monkeypatch, capsys)

    assert "## Communities" in ctx
    assert BORROWED_LINE in ctx
    assert git(worktree, "status", "--porcelain") == ""


def test_t12b_a_stale_main_graph_names_the_main_checkout(main, monkeypatch, capsys):
    """Red against the unconditional wording ("the code graph is stale"): no checkout named."""
    commit(main, "B", {"pkg/m.py": "def a():\n    return 1\n"})
    worktree = add_worktree(main)

    ctx = _session_start(worktree, monkeypatch, capsys)

    assert f"Sidegraph: the main checkout's code graph ({main}) is stale (built at " in ctx
    assert "): rebuild it there with `graphify update .`" in ctx
    assert "Sidegraph: the code graph is stale" not in ctx


def test_a_fresh_main_graph_adds_no_stale_line(main, monkeypatch, capsys):
    worktree = add_worktree(main)

    ctx = _session_start(worktree, monkeypatch, capsys)

    assert "is stale" not in ctx


def test_t12c_the_main_checkout_gets_no_borrowed_line_and_today_s_wording(
    main, monkeypatch, capsys
):
    """Red against nothing: the main checkout behaves as before this issue."""
    commit(main, "B", {"pkg/m.py": "def a():\n    return 1\n"})

    ctx = _session_start(main, monkeypatch, capsys)

    assert BORROWED_LINE not in ctx and "this worktree" not in ctx
    assert "Sidegraph: the code graph is stale (built at " in ctx
    assert "the main checkout's code graph" not in ctx
    assert "## Domains" in ctx  # the main checkout syncs, so its cache is built


def test_a_worktree_with_its_own_graph_is_not_borrowing(main, monkeypatch, capsys):
    worktree = add_worktree(main)
    from tests.test_graph_freshness import head, write_graph

    write_graph(worktree / GRAPH, head(worktree), ["pkg/m.py"])

    ctx = _session_start(worktree, monkeypatch, capsys)

    assert BORROWED_LINE not in ctx
    assert _stamp(worktree) is not None  # it synced its own graph


def test_a_worktree_whose_main_checkout_has_no_graph_says_nothing_about_borrowing(
    main, monkeypatch, capsys
):
    worktree = add_worktree(main)
    (main / GRAPH).unlink()

    ctx = _session_start(worktree, monkeypatch, capsys)

    assert BORROWED_LINE not in ctx
    assert "get_task_context" in ctx  # the map still renders


def test_a_failing_borrow_never_costs_the_map(main, monkeypatch, capsys):
    worktree = add_worktree(main)

    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr("sidegraph.engine.reader.borrowed_graph_path", boom)
    ctx = _session_start(worktree, monkeypatch, capsys)

    assert "get_task_context" in ctx
    assert BORROWED_LINE not in ctx
