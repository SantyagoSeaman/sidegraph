"""``sync(canonical_writes=False)`` refreshes derived state and writes no tracked file.

A linked worktree syncs the main checkout's graph into its own, cold index. The one thing a
sync can write to a committed file is the moved rung's adoption (it rewrites
``entities/<id>.json`` with the new ``descriptor.file_path``); with ``canonical_writes=False``
that rung abstains entirely, in the full pass and in the narrow re-verification pass, because
its evidence (the old path gone from disk, the move in HEAD's tree) would be read from the
checkout that holds the graph, not from the branch the store belongs to. Everything index-only
(engine mappings, binding statuses, domain communities, the TOC cache, the version stamp) is
refreshed as ever.
see design/superpowers/specs/2026-10-01-worktree-borrowed-graph-design.md (D2)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sidegraph.engine.reader import GraphifyReader
from sidegraph.retrieval import TOC_CACHE_KEY
from sidegraph.store import Store
from sidegraph.sync import LAST_SYNCED_KEY, PENDING_MOVES_KEY, maybe_sync, rebind_entity, sync
from tests.test_config_borrowed_graph import GRAPH, make_main
from tests.test_graph_freshness import commit, git, head, write_graph
from tests.test_server_borrowed_graph import porcelain, remember


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repository whose store holds an entity that names ``pkg/old.py``; a later commit moves
    that file to ``pkg/m.py`` and the graph is rebuilt after it (a unique name, the old path
    gone, the new one added by the very commit that removed it: the moved rung adopts), and an
    exact entity in ``pkg/n.py``."""
    root = make_main(tmp_path, files=("pkg/old.py", "pkg/n.py"))
    remember(root / ".sidegraph", "old.py kept a cache", "pkg/old.py", name="fn_0()")
    remember(root / ".sidegraph", "n.py settles in batches", "pkg/n.py", name="fn_1()")
    commit(root, "record", {})
    commit(root, "move old.py to m.py", {"pkg/old.py": None, "pkg/m.py": "def a():\n    pass\n"})
    write_graph(root / GRAPH, head(root), ["pkg/m.py", "pkg/n.py"])
    return root


def _entities(root: Path) -> dict[str, str]:
    return {
        f.name: f.read_text() for f in sorted((root / ".sidegraph" / "entities").glob("*.json"))
    }


def test_the_moved_rung_abstains_and_the_default_still_adopts(repo):
    """Red against unfixed code: no such keyword. Mutation M1 ignores it."""
    store = Store(repo / ".sidegraph")
    reader = GraphifyReader(repo / GRAPH)
    before = _entities(repo)
    try:
        report = sync(store, reader, canonical_writes=False)

        assert _entities(repo) == before
        assert porcelain(repo) == ""
        assert {o.canonical_name: o.status for o in report.outcomes}["fn_0()"] == "orphaned"
        assert not any(o.status in ("moved", "moved_uncommitted") for o in report.outcomes)

        # Control, on the same fixture: the default sync adopts the move and rewrites the file.
        sync(store, reader, force=True)
        assert _entities(repo) != before
        assert ".sidegraph/entities/" in porcelain(repo)
    finally:
        store.close()


def test_the_ladder_abstains_even_when_it_is_handed_a_repo_root(repo):
    """``sync`` also withholds the repo root, which alone would make the moved rung fail closed;
    the ladder's own gate must hold when a caller hands it a real root (mutation M1)."""
    store = Store(repo / ".sidegraph")
    reader = GraphifyReader(repo / GRAPH)
    before = _entities(repo)
    try:
        entity = store.find_entity("fn_0()", "pkg/old.py")
        assert entity is not None

        outcome = rebind_entity(entity, store, reader, repo, canonical_writes=False)

        assert outcome.status == "orphaned" and _entities(repo) == before

        assert rebind_entity(entity, store, reader, repo).status == "moved"  # control
        assert _entities(repo) != before
    finally:
        store.close()


def test_every_index_only_refresh_still_runs(repo):
    store = Store(repo / ".sidegraph")
    reader = GraphifyReader(repo / GRAPH)
    try:
        report = sync(store, reader, canonical_writes=False)

        assert report.skipped is False
        assert store.get_meta(LAST_SYNCED_KEY) == f"index-only:{reader.sync_stamp()}"
        assert store.get_meta(TOC_CACHE_KEY) is not None
        entity = store.find_entity("fn_1()", "pkg/n.py")
        assert entity is not None
        assert (entity.last_seen_node_id, entity.last_seen_community) == ("n1", "1")
        assert porcelain(repo) == ""
    finally:
        store.close()


def test_an_index_only_stamp_does_not_stand_in_for_a_normal_sync(repo):
    """Red against a shared stamp (mutation M13): the second call finds the version it just
    wrote and skips, so the moved entity stays orphaned in the index."""
    store = Store(repo / ".sidegraph")
    reader = GraphifyReader(repo / GRAPH)
    before = _entities(repo)
    try:
        assert maybe_sync(store, reader, canonical_writes=False).skipped is False  # type: ignore[union-attr]
        assert maybe_sync(store, reader, canonical_writes=False).skipped is True  # type: ignore[union-attr]

        report = maybe_sync(store, reader)

        assert report is not None and report.skipped is False
        assert store.get_meta(LAST_SYNCED_KEY) == reader.sync_stamp()
        assert {o.canonical_name: o.status for o in report.outcomes}["fn_0()"] == "moved"
        assert _entities(repo) != before
    finally:
        store.close()


def test_maybe_sync_passes_the_keyword_through(repo):
    store = Store(repo / ".sidegraph")
    reader = GraphifyReader(repo / GRAPH)
    before = _entities(repo)
    try:
        report = maybe_sync(store, reader, canonical_writes=False)

        assert report is not None and not report.skipped
        assert _entities(repo) == before
        assert maybe_sync(None, None, canonical_writes=False) is None  # type: ignore[arg-type]
    finally:
        store.close()


def test_the_narrow_reverification_pass_abstains_too(tmp_path):
    """A move left ``moved_uncommitted`` by an earlier full pass is adopted by the narrow pass
    once HEAD moves; without canonical writes it must not be (mutation M8)."""
    root = make_main(tmp_path, files=("pkg/old.py",))
    remember(root / ".sidegraph", "old.py kept a cache", "pkg/old.py", name="fn_0()")
    commit(root, "record", {})
    # The rename is in the working tree only; the graph was built on that tree.
    git(root, "mv", "pkg/old.py", "pkg/new.py")
    write_graph(root / GRAPH, head(root), ["pkg/new.py"])
    store = Store(root / ".sidegraph")
    reader = GraphifyReader(root / GRAPH)
    try:
        first = sync(store, reader)
        assert {o.canonical_name: o.status for o in first.outcomes}["fn_0()"] == "moved_uncommitted"
        assert store.get_meta(PENDING_MOVES_KEY) is not None
        before = _entities(root)

        git(root, "commit", "-q", "-m", "commit the rename")  # HEAD moves, the graph does not

        assert sync(store, reader, canonical_writes=False).skipped is False  # its own stamp
        skipped = sync(store, reader, canonical_writes=False)  # gated now: no narrow pass
        assert skipped.skipped is True and _entities(root) == before
        assert store.get_meta(PENDING_MOVES_KEY) is not None  # left as it was
        assert porcelain(root) == ""

        narrow = sync(store, reader)  # control: the default narrow pass adopts it
        assert {o.canonical_name: o.status for o in narrow.outcomes}["fn_0()"] == "moved"
        assert _entities(root) != before
    finally:
        store.close()
