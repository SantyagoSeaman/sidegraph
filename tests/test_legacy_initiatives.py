"""``Initiative`` records are gone (0.24): nothing writes ``initiatives/``, nothing reads it.

A store written by an older Sidegraph may still hold ``initiatives/*.json``. Those files are
committed history under the append-only rule: every command must open, read, verify, sync,
compact and migrate such a store without a finding about them and without touching a byte of
them. (Deleting one in a commit stays an ``illegal-deletion`` for ``verify --against``; see
``test_verify_transitions``.)
"""

# ruff: noqa: F811  (the imported ``git_repo`` fixture is redefined as a test argument)

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

from sidegraph.cli import compact_main, migrate_main
from sidegraph.engine.reader import GraphifyReader
from sidegraph.store import Store
from sidegraph.store_layout import require_listable_record_dirs
from sidegraph.sync import sync
from sidegraph.verify import verify_snapshot
from tests.store_format2_support import (
    OLD,
    commit_all,
    decision_body,
    format1_committed,
    format1_store,
    git_repo,  # noqa: F401  (pytest fixture)
    put_file,
)

GOOD = "01J0000000000000000000GOOD"
BAD = "01J00000000000000000000BAD"
LEGACY = {
    f"initiatives/{GOOD}.json": json.dumps({"id": GOOD, "name": "Q3 push", "tags": []}).encode(),
    f"initiatives/{BAD}.json": b"{not json",
    "initiatives/UNSAFE.json": json.dumps({"id": "../x", "name": "n"}).encode(),
}


def plant_legacy(store: Path) -> dict[str, bytes]:
    """Write the legacy initiative files byte for byte and return them."""
    (store / "initiatives").mkdir(exist_ok=True)
    for rel, data in LEGACY.items():
        (store / rel).write_bytes(data)
    return dict(LEGACY)


def legacy_bytes(store: Path) -> dict[str, bytes]:
    return {p.relative_to(store).as_posix(): p.read_bytes() for p in store.glob("initiatives/*")}


def test_a_new_store_has_no_initiatives_dir_and_no_table(tmp_path):
    store = tmp_path / ".sidegraph"
    with Store(store) as s:
        tables = {r[0] for r in s._conn.execute("SELECT name FROM sqlite_master")}
    assert not (store / "initiatives").exists()
    assert "initiatives" not in tables


def test_the_initiative_api_is_gone():
    import sidegraph
    from sidegraph import schema

    assert not hasattr(schema, "Initiative")
    assert not hasattr(Store, "upsert_initiative") and not hasattr(Store, "iter_initiatives")
    assert "Initiative" not in sidegraph.__all__


def test_an_old_store_opens_reads_and_leaves_the_files_alone(tmp_path, capsys):
    store = format1_store(tmp_path / ".sidegraph")
    put_file(store, "decision", decision_body("D1"))
    planted = plant_legacy(store)
    with Store(store) as s:
        assert s.get_decision("D1") is not None
        skipped = s._conn.execute(
            "SELECT value FROM meta WHERE key = 'skipped_canonical_files'"
        ).fetchone()
    assert skipped is None  # the retired directory is not a record directory: nothing to skip
    assert capsys.readouterr().err == ""
    assert legacy_bytes(store) == planted


def test_a_stale_initiatives_table_in_the_index_is_dropped_on_reload(tmp_path):
    store = format1_store(tmp_path / ".sidegraph")
    put_file(store, "decision", decision_body("D1"))
    Store(store).close()
    plant_legacy(store)
    con = sqlite3.connect(store / "index.db")
    con.execute("CREATE TABLE initiatives (id TEXT PRIMARY KEY, name TEXT NOT NULL, data TEXT)")
    con.commit()
    con.close()
    put_file(store, "decision", decision_body("D2"))  # a canonical change forces the reload
    with Store(store) as s:
        assert s.get_decision("D2") is not None
        tables = {r[0] for r in s._conn.execute("SELECT name FROM sqlite_master")}
    assert "initiatives" not in tables


def test_verify_snapshot_has_no_finding_about_legacy_initiative_files(tmp_path):
    store = format1_store(tmp_path / ".sidegraph")
    put_file(store, "decision", decision_body("D1"))
    plant_legacy(store)
    assert verify_snapshot(store) == []


def test_sync_leaves_legacy_initiative_files_alone(tmp_path):
    store = format1_store(tmp_path / ".sidegraph")
    put_file(store, "decision", decision_body("D1"))
    planted = plant_legacy(store)
    graph = tmp_path / "graph.json"
    graph.write_text(json.dumps({"built_at_commit": "v1", "nodes": [], "links": []}))
    mtimes = {p: p.stat().st_mtime_ns for p in store.glob("initiatives/*")}
    with Store(store) as s:
        sync(s, GraphifyReader(graph))
    assert legacy_bytes(store) == planted
    assert {p: p.stat().st_mtime_ns for p in store.glob("initiatives/*")} == mtimes


def test_compact_on_a_format_1_store_leaves_legacy_initiative_files_alone(tmp_path):
    store = format1_store(tmp_path / ".sidegraph")
    old = put_file(
        store,
        "decision",
        decision_body("D1", status="superseded", valid_to="2026-01-02T00:00:00Z"),
    )
    put_file(store, "decision", decision_body("D2", status="accepted", supersedes="D1"))
    planted = plant_legacy(store)
    with Store(store) as s:
        report = s.compact()
    assert report.total_compacted == 1  # the decision moved to an archive segment ...
    assert not old.exists()
    assert legacy_bytes(store) == planted  # ... and the initiative files stayed as they were


@pytest.mark.parametrize("command", ["migrate", "compact"])
def test_migrate_and_format_2_compact_leave_legacy_initiative_files_alone(git_repo, command):
    store = format1_committed(git_repo)
    planted = plant_legacy(store)
    commit_all(git_repo, "legacy initiatives", date=OLD)
    assert migrate_main(["--db", str(store), "--any-branch"]) == 0
    if command == "compact":
        assert compact_main(["--db", str(store), "--settled-days", "0"]) == 0
    assert (store / "shards").is_dir()  # the store really was packed
    assert legacy_bytes(store) == planted
    assert verify_snapshot(store) == []
    with Store(store) as s:
        assert s.get_decision("D2") is not None


def test_a_store_holding_only_legacy_initiatives_is_not_new(tmp_path):
    store = tmp_path / ".sidegraph"
    store.mkdir()
    plant_legacy(store)
    Store(store).close()
    assert not (store / "stamping_live_since").exists()  # it already held data


def test_a_symlinked_initiatives_dir_is_refused(tmp_path):
    store = format1_store(tmp_path / ".sidegraph")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    os.symlink(elsewhere, store / "initiatives")
    with pytest.raises(Exception, match="symlink"):
        Store(store)


@pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="modes do not stop root or Windows",
)
def test_an_unreadable_initiatives_dir_does_not_fail_the_listability_probe(tmp_path):
    store = format1_store(tmp_path / ".sidegraph")
    (store / "initiatives").mkdir()
    (store / "initiatives").chmod(0)
    try:
        require_listable_record_dirs(store)  # nothing reads the retired directory
    finally:
        (store / "initiatives").chmod(0o755)
