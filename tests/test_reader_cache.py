"""The MCP server keeps one graph reader while ``graph.json`` is unchanged.

``ReaderCache`` (engine seam) is keyed on the graph's absolute path and validated by the file's
identity (device, inode, size, mtime, ctime) read before the build. The server routes both reader
openings (``_load_reader`` and the worktree's borrowed reader) through one cache, and ``NodeRef``
is frozen because a shared reader hands the same nodes to every call.
see design/superpowers/specs/2026-11-16-mcp-reader-reuse-design.md (T1-T15)
"""

from __future__ import annotations

import gc
import json
import os
import sys
import threading
import time
import weakref
from collections.abc import Callable
from pathlib import Path

import pytest
from pydantic import ValidationError

from sidegraph import server
from sidegraph.engine.reader import GraphifyReader, NodeRef, ReaderCache
from sidegraph.store import Store
from tests.graph_repo_support import GRAPH, add_worktree, make_main, serve

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX ctime semantics")


def write_graph(path: Path, labels: list[str], commit: str = "c") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    nodes = [
        {
            "id": f"n{i}",
            "label": label,
            "norm_label": label,
            "file_type": "code",
            "source_file": f"m{i}.py",
            "community": i,
        }
        for i, label in enumerate(labels)
    ]
    path.write_text(json.dumps({"built_at_commit": commit, "nodes": nodes, "links": []}))
    return path


def names(reader: GraphifyReader) -> list[str]:
    return [n.name for n in reader.list_nodes()]


class Counting:
    """A ``GraphifyReader`` factory that records every build."""

    def __init__(self) -> None:
        self.paths: list[str] = []

    def __call__(self, path: str | Path) -> GraphifyReader:
        self.paths.append(str(path))
        return GraphifyReader(path)


@pytest.fixture
def graph(tmp_path: Path) -> Path:
    return write_graph(tmp_path / "graphify-out" / "graph.json", ["alpha"])


# ---- T1, T6, T8, T11: the server's two openings --------------------------------------------


def serve_project(monkeypatch: pytest.MonkeyPatch, project: Path) -> None:
    Store(project / ".sidegraph").close()
    monkeypatch.chdir(project)
    monkeypatch.setenv("SIDEGRAPH_DIR", str(project / ".sidegraph"))
    monkeypatch.setattr(server, "_store", Store(project / ".sidegraph"))


def test_t1_load_reader_returns_the_same_reader_while_the_graph_is_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Red against unfixed code: every call built a fresh reader."""
    write_graph(tmp_path / "graphify-out" / "graph.json", ["alpha"])
    serve_project(monkeypatch, tmp_path)

    first = server._load_reader()

    assert first is not None
    assert server._load_reader() is first


def test_t6_a_missing_graph_is_not_cached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Red against caching a failure keyed by path."""
    serve_project(monkeypatch, tmp_path)
    assert server._load_reader() is None

    write_graph(tmp_path / "graphify-out" / "graph.json", ["alpha"])

    reader = server._load_reader()
    assert reader is not None and names(reader) == ["alpha"]


def test_t8_alternating_graphs_each_get_their_own_reader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A guard, red against nothing: a single slot compared by identity passes too. It pins that
    the cache never serves another path's reader, however the path was switched."""
    a, b = tmp_path / "A", tmp_path / "B"
    ga = write_graph(a / "graphify-out" / "graph.json", ["aa"])
    gb = write_graph(b / "graphify-out" / "graph.json", ["bb", "bc"])
    Store(a / ".sidegraph").close()
    Store(b / ".sidegraph").close()
    monkeypatch.chdir(a)

    # switched through SIDEGRAPH_GRAPH, the store fixed
    monkeypatch.setattr(server, "_store", Store(a / ".sidegraph"))
    monkeypatch.setenv("SIDEGRAPH_GRAPH", str(ga))
    first_a = server._load_reader()
    monkeypatch.setenv("SIDEGRAPH_GRAPH", str(gb))
    first_b = server._load_reader()
    monkeypatch.setenv("SIDEGRAPH_GRAPH", str(ga))
    assert first_a is not None and first_b is not None
    assert names(first_a) == ["aa"] and names(first_b) == ["bb", "bc"]
    assert server._load_reader() is first_a

    # switched through the store, SIDEGRAPH_GRAPH relative
    monkeypatch.setenv("SIDEGRAPH_GRAPH", "graphify-out/graph.json")
    monkeypatch.setattr(server, "_store", Store(a / ".sidegraph"))
    store_a = server._load_reader()
    monkeypatch.setattr(server, "_store", Store(b / ".sidegraph"))
    store_b = server._load_reader()
    monkeypatch.setattr(server, "_store", Store(a / ".sidegraph"))
    assert store_a is not None and store_b is not None
    assert names(store_a) == ["aa"] and names(store_b) == ["bb", "bc"]
    assert server._load_reader() is store_a
    assert store_a is first_a  # the same absolute path, so the same cache entry


def test_t11_the_borrowed_reader_is_reused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Red against unfixed code, and against a borrowed path built outside the cache."""
    main = make_main(tmp_path, files=("pkg/m.py",))
    worktree = add_worktree(main)
    serve(monkeypatch, worktree)

    first, borrowed_from = server._synced_reader()
    second, _ = server._synced_reader()

    assert first is not None and borrowed_from == main
    assert Path(first.path) == main / GRAPH
    assert second is first


# ---- T2-T5, T7, T9, T10, T15: the cache itself ------------------------------------------------


def test_t2_a_rewrite_of_another_size_builds_a_new_reader(graph: Path) -> None:
    """Red against a path-only key."""
    cache = ReaderCache(Counting())
    old = cache.get(graph)

    write_graph(graph, ["alpha", "beta"])
    new = cache.get(graph)

    assert new is not old and names(new) == ["alpha", "beta"]


def test_t3_a_moved_mtime_builds_a_new_reader(graph: Path) -> None:
    """Red against a key of path and size."""
    cache = ReaderCache(Counting())
    old = cache.get(graph)
    before = graph.stat()

    write_graph(graph, ["omega"])  # the same size
    assert graph.stat().st_size == before.st_size
    os.utime(graph, ns=(before.st_atime_ns, before.st_mtime_ns + 5_000_000_000))
    new = cache.get(graph)

    assert new is not old and names(new) == ["omega"]


def test_t4_a_replaced_file_with_the_same_size_and_mtime_builds_a_new_reader(graph: Path) -> None:
    """Red against a key of size and mtime (no inode)."""
    cache = ReaderCache(Counting())
    old = cache.get(graph)
    before = graph.stat()

    other = graph.with_name("graph.json.tmp")
    write_graph(other, ["omega"])
    assert other.stat().st_size == before.st_size
    os.utime(other, ns=(before.st_atime_ns, before.st_mtime_ns))
    os.replace(other, graph)
    assert graph.stat().st_mtime_ns == before.st_mtime_ns
    new = cache.get(graph)

    assert new is not old and names(new) == ["omega"]


def test_t5_a_change_during_the_build_is_seen_by_the_next_call(graph: Path) -> None:
    """Red against an identity read after the build: it would hide the rewrite."""
    seen: list[int] = []

    def rewriting_factory(path: str | Path) -> GraphifyReader:
        reader = GraphifyReader(path)
        if not seen:
            write_graph(graph, ["alpha", "beta", "gamma"])  # the file changes after the parse
        seen.append(1)
        return reader

    cache = ReaderCache(rewriting_factory)
    first = cache.get(graph)
    assert names(first) == ["alpha"]

    second = cache.get(graph)

    assert second is not first and names(second) == ["alpha", "beta", "gamma"]


def test_t7_a_failed_build_is_not_cached(graph: Path) -> None:
    """Red against caching a factory failure under the file's identity."""
    attempts: list[int] = []

    def flaky(path: str | Path) -> GraphifyReader:
        attempts.append(1)
        if len(attempts) == 1:
            raise OSError("transient")
        return GraphifyReader(path)

    cache = ReaderCache(flaky)
    with pytest.raises(OSError, match="transient"):
        cache.get(graph)

    reader = cache.get(graph)  # the metadata did not change

    assert len(attempts) == 2 and names(reader) == ["alpha"]
    assert cache.get(graph) is reader


def test_t9_the_least_recently_used_entry_is_evicted(tmp_path: Path) -> None:
    """Red against FIFO eviction: A, B, C alone cannot tell it from LRU."""
    paths = {k: write_graph(tmp_path / k / "graph.json", [k]) for k in "abc"}
    factory = Counting()
    cache = ReaderCache(factory, max_entries=2)

    a1 = cache.get(paths["a"])
    cache.get(paths["b"])
    assert cache.get(paths["a"]) is a1  # A is now the most recently used
    c1 = cache.get(paths["c"])  # evicts B, the least recently used
    assert len(cache) == 2

    assert cache.get(paths["a"]) is a1
    assert cache.get(paths["c"]) is c1
    built = len(factory.paths)
    cache.get(paths["b"])
    assert len(factory.paths) == built + 1  # B was rebuilt


def test_t10_cold_calls_share_one_build(graph: Path) -> None:
    """Red against building outside the lock: eight threads would parse the graph eight times."""
    builds: list[int] = []

    def slow(path: str | Path) -> GraphifyReader:
        builds.append(1)
        time.sleep(0.05)
        return GraphifyReader(path)

    cache = ReaderCache(slow)
    gate = threading.Barrier(8)
    got: list[GraphifyReader] = []

    def run() -> None:
        gate.wait()
        got.append(cache.get(graph))

    threads = [threading.Thread(target=run) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(builds) == 1
    assert len(got) == 8 and all(r is got[0] for r in got)


def test_t15_a_deleted_graph_raises_and_releases_its_reader(graph: Path) -> None:
    """Red against keeping the entry on a stat error."""
    cache = ReaderCache(Counting())
    cache.get(graph)
    assert len(cache) == 1

    graph.unlink()
    with pytest.raises(FileNotFoundError):
        cache.get(graph)

    assert len(cache) == 0


# ---- T12-T14: the model, the key and the identity ----------------------------------------------


def test_t12_node_refs_are_frozen() -> None:
    """Red against D5 not applied: a caller could mutate a node every call shares."""
    node = NodeRef(node_id="n", name="f", norm_name="f", file_type="code")
    with pytest.raises(ValidationError, match="frozen_instance"):
        node.name = "g"  # type: ignore[misc]


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need a privilege on Windows")
def test_t13_two_symlinks_to_the_same_bytes_keep_their_own_labels(tmp_path: Path) -> None:
    """Red against a resolved-path key: the second link would get the first one's reader and so
    its label sidecar."""
    real = write_graph(tmp_path / "real" / "graph.json", ["alpha"])
    links = []
    for key, label in (("one", "First"), ("two", "Second")):
        link = tmp_path / key / "graph.json"
        link.parent.mkdir()
        link.symlink_to(real)
        (link.parent / ".graphify_labels.json").write_text(json.dumps({"0": label}))
        links.append(link)
    cache = ReaderCache(Counting())

    first, second = (cache.get(link) for link in links)

    assert first is not second
    assert first.community_labels() == {"0": "First"}
    assert second.community_labels() == {"0": "Second"}


@posix_only
def test_t14_an_in_place_rewrite_with_the_old_mtime_builds_a_new_reader(graph: Path) -> None:
    """Red against an identity without ``st_ctime_ns``: size, inode and mtime are all unchanged.

    The rewrite repeats until the kernel's ctime moved: a coarse-tick kernel may not move it at
    once."""
    cache = ReaderCache(Counting())
    old = cache.get(graph)
    before = graph.stat()
    replacement = write_graph(graph.with_name("scratch.json"), ["omega"]).read_bytes()
    graph.with_name("scratch.json").unlink()
    assert len(replacement) == before.st_size

    for _ in range(200):
        with open(graph, "r+b") as handle:  # in place: the same inode
            handle.write(replacement)
        os.utime(graph, ns=(before.st_atime_ns, before.st_mtime_ns))
        after = graph.stat()
        if after.st_ctime_ns != before.st_ctime_ns:
            break
        time.sleep(0.005)
    else:
        pytest.fail("the kernel never moved st_ctime_ns")
    assert (after.st_ino, after.st_size, after.st_mtime_ns) == (
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    )

    new = cache.get(graph)

    assert new is not old and names(new) == ["omega"]


def test_a_third_key_evicts_before_it_builds(tmp_path: Path) -> None:
    """The bound holds during the build: the old entry is gone before the factory runs."""
    paths = [write_graph(tmp_path / k / "graph.json", [k]) for k in "abc"]
    sizes: list[int] = []
    holder: dict[str, ReaderCache] = {}

    def spying(path: str | Path) -> GraphifyReader:
        sizes.append(len(holder["cache"]))
        return GraphifyReader(path)

    holder["cache"] = cache = ReaderCache(spying, max_entries=2)
    for p in paths:
        cache.get(p)

    assert sizes == [0, 1, 1]


def test_a_stale_entry_is_dropped_before_its_rebuild(graph: Path) -> None:
    """The old reader is not alive beside the new one while the graph parses."""
    sizes: list[int] = []
    holder: dict[str, ReaderCache] = {}

    def spying(path: str | Path) -> GraphifyReader:
        sizes.append(len(holder["cache"]))
        return GraphifyReader(path)

    alive: list[bool] = []
    old: list[weakref.ref[GraphifyReader]] = []

    def watching(path: str | Path) -> GraphifyReader:
        alive.extend(ref() is not None for ref in old)
        return spying(path)

    holder["cache"] = cache = ReaderCache(watching)
    old.append(weakref.ref(cache.get(graph)))
    write_graph(graph, ["alpha", "beta"])
    gc.collect()
    cache.get(graph)

    assert sizes == [0, 0]
    assert alive == [False]  # the cache was the old reader's only owner, and let go of it


def test_the_key_is_the_absolute_path_and_the_reader_is_built_from_it(
    graph: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A relative path is made absolute once, for the stat, the key and the build alike: a
    reader built from the relative path would read its sidecars from wherever the cwd moves."""
    factory = Counting()
    cache = ReaderCache(factory)
    monkeypatch.chdir(graph.parent)

    relative = cache.get("graph.json")

    assert cache.get(graph) is relative
    assert factory.paths == [os.path.abspath(graph)]
    assert Path(relative.path).is_absolute()


def test_a_factory_may_be_any_callable(graph: Path) -> None:
    made: list[Callable[..., object]] = []
    cache = ReaderCache(lambda p: made.append(GraphifyReader) or GraphifyReader(p))
    assert cache.get(graph) is cache.get(graph)
    assert len(made) == 1
    cache.clear()
    assert len(cache) == 0
