"""Sync owns the derived graph membership table and its stamp."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Iterator
from pathlib import Path

import pytest

from sidegraph import store_layout
from sidegraph.engine.reader import GraphifyReader
from sidegraph.retrieval import TaskContext
from sidegraph.store import Store
from sidegraph.store_layout import GRAPH_FILES_STAMP_KEY, VOLATILE_STALE_KEY
from sidegraph.sync import LAST_SYNCED_KEY, SyncReport, sync
from tests.test_hot_index import _reload_hot_pair, _synced_hot_pair
from tests.test_sync_run import GRAPH_B, _write_graph


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    with Store(tmp_path / ".sidegraph") as store:
        yield store


def _paths(store: Store) -> set[str]:
    return {row[0] for row in store._conn.execute("SELECT path FROM graph_files")}


def test_sync_fills_graph_files(store: Store, tmp_path: Path) -> None:
    reader = GraphifyReader(_write_graph(tmp_path, "graph-a.json", GRAPH_B))
    sync(store, reader)
    assert _paths(store) == reader.source_files()
    assert store.get_meta(GRAPH_FILES_STAMP_KEY) == store.get_meta(LAST_SYNCED_KEY)


def test_sync_does_not_rewrite_the_same_graph(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reader = GraphifyReader(_write_graph(tmp_path, "graph-a.json", GRAPH_B))
    sync(store, reader)
    calls = []
    replace = store.replace_graph_files

    def counted(paths: Iterable[str], stamp: str) -> None:
        calls.append(stamp)
        replace(paths, stamp)

    monkeypatch.setattr(store, "replace_graph_files", counted)
    assert sync(store, reader).skipped
    assert calls == []


def test_skipped_sync_populates_the_table_after_upgrade(store: Store, tmp_path: Path) -> None:
    """Guard the first lazy sync after upgrade against placing the write below the gate."""
    reader = GraphifyReader(_write_graph(tmp_path, "graph-a.json", GRAPH_B))
    store.set_meta(LAST_SYNCED_KEY, reader.sync_stamp())
    store.set_meta(VOLATILE_STALE_KEY, "0")
    assert store.get_meta(GRAPH_FILES_STAMP_KEY) is None
    assert sync(store, reader).skipped
    assert _paths(store) == reader.source_files()
    assert store.get_meta(GRAPH_FILES_STAMP_KEY) == reader.sync_stamp()


def test_sync_replaces_graph_files_after_removal(store: Store, tmp_path: Path) -> None:
    before = GraphifyReader(_write_graph(tmp_path, "graph-a.json", GRAPH_B))
    after = GraphifyReader(
        _write_graph(
            tmp_path,
            "graph-b.json",
            {**GRAPH_B, "built_at_commit": "vC", "nodes": GRAPH_B["nodes"][1:]},
        )
    )
    sync(store, before)
    assert "a.py" in _paths(store)
    sync(store, after)
    assert _paths(store) == after.source_files() == before.source_files() - {"a.py"}
    assert store.get_meta(GRAPH_FILES_STAMP_KEY) == store.get_meta(LAST_SYNCED_KEY)


def test_canonical_rebuild_keeps_graph_files(tmp_path: Path) -> None:
    root, reader, _, record_path = _synced_hot_pair(tmp_path)
    _reload_hot_pair(root, record_path)
    with Store(root) as store:
        assert _paths(store) == reader.source_files()
        assert store.get_meta(GRAPH_FILES_STAMP_KEY) == reader.sync_stamp()


def test_failed_graph_files_refresh_leaves_the_old_stamp(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = GraphifyReader(_write_graph(tmp_path, "graph-a.json", GRAPH_B))
    after = GraphifyReader(
        _write_graph(tmp_path, "graph-b.json", {**GRAPH_B, "built_at_commit": "vC"})
    )
    sync(store, before)

    def fail(*args: object) -> None:
        raise RuntimeError("Synthetic graph files refresh failure")

    monkeypatch.setattr(store, "replace_graph_files", fail)
    assert isinstance(sync(store, after), SyncReport)
    assert store.get_meta(LAST_SYNCED_KEY) == after.sync_stamp()
    assert store.get_meta(GRAPH_FILES_STAMP_KEY) == before.sync_stamp()
    assert store.get_meta(GRAPH_FILES_STAMP_KEY) != store.get_meta(LAST_SYNCED_KEY)


def test_unreadable_graph_files_stamp_does_not_fail_sync(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reader = GraphifyReader(_write_graph(tmp_path, "graph-a.json", GRAPH_B))
    get_meta = store.get_meta

    def read_meta(key: str) -> str | None:
        if key == GRAPH_FILES_STAMP_KEY:
            raise sqlite3.OperationalError("Synthetic graph files stamp read failure")
        return get_meta(key)

    monkeypatch.setattr(store, "get_meta", read_meta)
    assert isinstance(sync(store, reader), SyncReport)
    assert get_meta(LAST_SYNCED_KEY) == reader.sync_stamp()
    assert get_meta(GRAPH_FILES_STAMP_KEY) is None


def test_failed_nested_replacement_rolls_back_the_graph_files(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.replace_graph_files(["a.py"], "v1")

    def fail_stamp(key: str, value: str) -> None:
        raise sqlite3.OperationalError("Synthetic graph files stamp write failure")

    monkeypatch.setattr(store, "set_meta", fail_stamp)
    with store._mutation(), pytest.raises(sqlite3.OperationalError, match="stamp write failure"):
        store.replace_graph_files(["b.py"], "v2")
    assert _paths(store) == {"a.py"}
    assert store.get_meta(GRAPH_FILES_STAMP_KEY) == "v1"


def test_retrieval_keeps_the_file_bound_legend() -> None:
    """The guard pins the text across the move to store_layout."""
    _, legend, _ = TaskContext(file_bound_shown=True).render_parts()
    assert legend == store_layout.FILE_BOUND_LEGEND
    assert legend == (
        "[file-bound] = declared applicability to the exact requested file; "
        "the graph cannot verify its symbol anchors. No anchor was rebound."
    )
