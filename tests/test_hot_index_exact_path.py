"""Exact-path reads depend on the index's sync and membership stamps."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import pytest

from sidegraph.hot_index import HotIndex
from sidegraph.schema import DecisionKind
from sidegraph.store import Store
from sidegraph.store_layout import GRAPH_FILES_STAMP_KEY, VOLATILE_STALE_KEY
from sidegraph.sync import LAST_SYNCED_KEY
from tests.test_hot_index import _Seeder


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    with Store(tmp_path / ".sidegraph") as store:
        seed = _Seeder(store)
        seed.anchor(seed.decision("Python record", DecisionKind.GOTCHA), "src/a.py")
        seed.anchor(
            seed.decision("Ignore record", DecisionKind.GOTCHA), ".gitignore", status="orphaned"
        )
        yield store


def test_never_synced_delivers_a_placeholder_by_exact_path(store: Store) -> None:
    assert store.get_meta(LAST_SYNCED_KEY) is None
    assert store.get_meta(VOLATILE_STALE_KEY) == "1"
    index = HotIndex.open(store.path)
    assert index is not None
    with closing(index):
        ids = index.anchored_entities(["src/a.py"])["src/a.py"]
        assert index.records_for(ids) == []
        assert index.exact_path_files(["src/a.py"]) == ({"src/a.py"}, True)
        assert [r.decision["title"] for r in index.records_for(ids, exact_path=True)] == [
            "Python record"
        ]
        orphan_ids = index.anchored_entities([".gitignore"])[".gitignore"]
        assert [r.decision["title"] for r in index.records_for(orphan_ids, exact_path=True)] == [
            "Ignore record"
        ]


@pytest.mark.parametrize("volatile_stale", ["0", "1"], ids=["warm", "cold-after-sync"])
def test_synced_delivers_an_outside_graph_orphan_by_exact_path(
    store: Store, volatile_stale: str
) -> None:
    """Guard a previously synced cold index against the flag-only definition of never synced."""
    store.set_meta(LAST_SYNCED_KEY, "v1")
    store.set_meta(VOLATILE_STALE_KEY, volatile_stale)
    store.replace_graph_files(["src/a.py"], "v1")
    index = HotIndex.open(store.path)
    assert index is not None
    with closing(index):
        assert index.exact_path_files(["src/a.py", ".gitignore"]) == ({".gitignore"}, False)
        ids = index.anchored_entities([".gitignore"])[".gitignore"]
        assert index.records_for(ids) == []
        assert [r.decision["title"] for r in index.records_for(ids, exact_path=True)] == [
            "Ignore record"
        ]


@pytest.mark.parametrize(
    ("sync_stamp", "files_stamp"),
    [("v1", None), ("v1", "old"), (None, None)],
    ids=["legacy-index", "different-stamp", "warm-unstamped"],
)
def test_unverified_warm_index_does_not_enable_exact_path(
    store: Store, sync_stamp: str | None, files_stamp: str | None
) -> None:
    """Guard missing and unequal graph stamps, and warm indexes that lack a sync stamp."""
    store.set_meta(VOLATILE_STALE_KEY, "0")
    if sync_stamp is not None:
        store.set_meta(LAST_SYNCED_KEY, sync_stamp)
    if files_stamp is not None:
        store.set_meta(GRAPH_FILES_STAMP_KEY, files_stamp)
    index = HotIndex.open(store.path)
    assert index is not None
    with closing(index):
        assert index.exact_path_files([".gitignore"]) == (set(), False)


def test_exact_path_rejects_a_non_leaf_binding_status(store: Store) -> None:
    """Guard the leaf-status filter even when the readiness gate is bypassed."""
    index = HotIndex.open(store.path)
    assert index is not None
    with closing(index):
        ids = index.anchored_entities(["src/a.py"])["src/a.py"]
        with store._mutation():
            (raw,) = store._conn.execute(
                "SELECT data FROM anchor_bindings WHERE entity_id = ?", (ids[0],)
            ).fetchone()
            data = json.loads(raw)
            data["status"] = "bogus"
            store._conn.execute(
                "UPDATE anchor_bindings SET data = ? WHERE entity_id = ?",
                (json.dumps(data), ids[0]),
            )
        assert index.records_for(ids, exact_path=True) == []


def test_missing_graph_files_table_falls_back_and_reports(store: Store) -> None:
    """Guard the fail-open path if the membership table cannot be read."""
    store.set_meta(LAST_SYNCED_KEY, "v1")
    store.replace_graph_files(["src/a.py"], "v1")
    with store._mutation():
        store._conn.execute("DROP TABLE graph_files")
    index = HotIndex.open(store.path)
    assert index is not None
    errors: list[Exception] = []
    with closing(index):
        assert index.exact_path_files([".gitignore"], on_error=errors.append) == (set(), False)
    assert len(errors) == 1
    assert isinstance(errors[0], sqlite3.OperationalError)
