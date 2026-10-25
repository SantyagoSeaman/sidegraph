"""Synthetic ledger units, optional failure isolation, retention and privacy."""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import UTC, datetime, timedelta

import pytest

from sidegraph import delivery
from sidegraph.delivery import Delivery, Placement, Snapshot
from sidegraph.hot_index import HotIndex, Shown
from sidegraph.store import Store
from tests.test_delivery_surfaces import rows


def sample(*, surface="query_decisions", groups=(None,)):
    return Delivery(
        surface,
        [Placement(Snapshot("synthetic-fact", "fact", "fact", "accepted"), p) for p in groups],
    )


def test_pathless_and_repeated_file_placements_are_different_units(tmp_path):
    store = Store(tmp_path / "s")
    assert store.record_delivery(sample(groups=(None, "a.py", "b.py")))
    header = rows(store, "delivery_occurrences")[0]
    assert (
        header["unique_record_count"],
        header["placement_count"],
        header["anchor_link_count"],
    ) == (1, 3, 2)
    assert len(rows(store, "delivery_placements")) == 3
    assert {r["path"] for r in rows(store, "delivery_anchor_links")} == {"a.py", "b.py"}
    store.close()


def test_same_timestamp_does_not_merge_independent_answers(tmp_path):
    store = Store(tmp_path / "s")
    now = datetime.now(UTC)
    for _ in range(2):
        with store._mutation():
            delivery.append(store._conn, sample(), now=now)
    headers = rows(store, "delivery_occurrences")
    assert len({r["at"] for r in headers}) == 1
    assert len({r["occurrence_id"] for r in headers}) == 2
    store.close()


def test_refused_then_emitted_wins_and_degraded_is_emitted_subset(tmp_path):
    store = Store(tmp_path / "s")
    fact = ("fact", "synthetic-fact")
    considered = frozenset((fact, ("decision", "rank-refused"), ("fact", "hard-cut")))
    assert store.record_delivery(
        sample()._replace(
            considered=considered,
            placed=frozenset((fact, ("fact", "hard-cut"))),
            degraded=frozenset((fact, ("decision", "rank-refused"))),
        )
    )
    header = rows(store, "delivery_occurrences")[0]
    assert (
        header["considered"],
        header["rank_refused"],
        header["hard_cap_removed"],
        header["degraded"],
    ) == (3, 1, 1, 1)
    store.close()


def test_optional_append_rollback_keeps_committed_claim_and_legacy_show(tmp_path):
    store = Store(tmp_path / "s")
    hot = HotIndex.open(store.path)
    assert hot is not None
    assert hot.claim_file(
        "claim:", "a.py", "now", 3, shown=Shown("synthetic-session", None, ["record"])
    )
    hot._conn.set_authorizer(
        lambda action, table, *_: (
            sqlite3.SQLITE_DENY
            if action == sqlite3.SQLITE_INSERT and table == "delivery_placements"
            else sqlite3.SQLITE_OK
        )
    )
    errors = []
    assert not hot.record_delivery(sample(), on_error=lambda e: errors.append(type(e).__name__))
    hot._conn.set_authorizer(None)
    assert hot.get_meta("claim:a.py") == "now"
    assert (
        hot._conn.execute("SELECT COUNT(*) FROM retrieval_events WHERE kind='inject'").fetchone()[0]
        == 1
    )
    assert rows(store, "delivery_occurrences") == []
    assert not hot.write_failed
    assert errors == ["DatabaseError"]
    hot.close()
    store.close()


@pytest.mark.parametrize("kind", ["store", "hot"])
def test_optional_append_busy_never_uses_legacy_timeout(tmp_path, kind):
    store = Store(tmp_path / "s")
    hot = HotIndex.open(store.path)
    assert hot is not None
    handle = store if kind == "store" else hot
    before = handle._conn.execute("PRAGMA busy_timeout").fetchone()[0]
    locker = sqlite3.connect(store.path / "index.db")
    locker.execute("BEGIN EXCLUSIVE")
    try:
        start = time.monotonic()
        assert not handle.record_delivery(sample())
        assert time.monotonic() - start < 0.75
    finally:
        locker.rollback()
        locker.close()
    assert handle._conn.execute("PRAGMA busy_timeout").fetchone()[0] == before
    assert rows(store, "delivery_occurrences") == []
    assert not hot.write_failed
    hot.close()
    store.close()


def test_optional_append_skips_unrelated_outer_mutation(tmp_path):
    store = Store(tmp_path / "s")
    with store._mutation():
        store._conn.execute("INSERT INTO meta(key,value) VALUES ('outer-owner','keep')")
        assert not store.record_delivery(sample())
        assert store._conn.in_transaction
    assert store.get_meta("outer-owner") == "keep"
    assert rows(store, "delivery_occurrences") == []
    store.close()


def test_old_index_optional_absence_does_not_refuse_hot_delivery(tmp_path):
    store = Store(tmp_path / "s")
    for table in ("delivery_anchor_links", "delivery_placements", "delivery_occurrences"):
        store._conn.execute(f"DROP TABLE {table}")
    store._conn.commit()
    hot = HotIndex.open(store.path)
    assert hot is not None
    assert hot.claim_file("claim:", "a.py", "now", 3, shown=Shown("s", None, ["record"]))
    assert not hot.record_delivery(sample())
    assert not hot.write_failed
    assert hot.get_meta("claim:a.py") == "now"
    hot.close()
    store.close()
    reopened = Store(tmp_path / "s")
    assert reopened.record_delivery(sample())
    reopened.close()


def test_legacy_prune_handles_children_without_at_and_reload_keeps_new_journal(tmp_path):
    store = Store(tmp_path / "s")
    old = datetime.now(UTC) - timedelta(days=40)
    with store._mutation():
        delivery.append(store._conn, sample(groups=("a.py",)), now=old)
    store.record_retrieval_events("synthetic-session", ["a.py"], [], at=old.isoformat())
    canonical = {str(p): p.read_bytes() for p in store.path.rglob("*.json")}
    store._reload_index_from_canonical(store._compute_canonical_digest()[0])
    assert len(rows(store, "delivery_occurrences")) == 1
    assert store.prune_telemetry_events() == 1
    for table in ("delivery_occurrences", "delivery_placements", "delivery_anchor_links"):
        assert rows(store, table) == []
    assert store.get_meta(delivery.META_KEY) is not None
    assert canonical == {str(p): p.read_bytes() for p in store.path.rglob("*.json")}
    store.close()


def test_capacity_caps_evict_whole_occurrences_and_keep_surviving_disclosure(tmp_path, monkeypatch):
    for name, value in [("MAX_OCCURRENCES", 12), ("MAX_PLACEMENTS", 25), ("MAX_LINKS", 40)]:
        monkeypatch.setattr(delivery, name, value)
    store = Store(tmp_path / "s")
    for _ in range(25):
        assert store.record_delivery(sample(groups=("a.py", "b.py", "c.py")))
    first_pages = store._conn.execute("PRAGMA page_count").fetchone()[0]
    for _ in range(25):
        assert store.record_delivery(sample(groups=("a.py", "b.py", "c.py")))
    for table, cap in [
        ("delivery_occurrences", 12),
        ("delivery_placements", 25),
        ("delivery_anchor_links", 40),
    ]:
        assert len(rows(store, table)) <= cap
    headers = {r["id"] for r in rows(store, "delivery_occurrences")}
    for table in ("delivery_placements", "delivery_anchor_links"):
        assert {r["occurrence"] for r in rows(store, table)} <= headers
    assert store._conn.execute("PRAGMA page_count").fetchone()[0] <= first_pages + 6
    meta = json.loads(store.get_meta(delivery.META_KEY))
    assert meta["capacity_occurrences"] > 0
    assert meta["capacity_placements"] == meta["capacity_occurrences"] * 3
    assert datetime.fromisoformat(meta["capacity_evicted_through"]).tzinfo is not None
    store._reload_index_from_canonical(store._compute_canonical_digest()[0])
    assert json.loads(store.get_meta(delivery.META_KEY)) == meta
    store.close()


def test_metadata_limits_disclose_true_units_without_storing_invalid_text(tmp_path, monkeypatch):
    monkeypatch.setattr(delivery, "PLACEMENTS_PER_OCCURRENCE", 2)
    monkeypatch.setattr(delivery, "LINKS_PER_OCCURRENCE", 1)
    store = Store(tmp_path / "s")
    assert store.record_delivery(
        sample(groups=("a.py", "b.py", "c.py"))._replace(
            session_id="PRIVATE PROMPT CANARY",
            host_call_id="x" * 257,
        )
    )
    header = rows(store, "delivery_occurrences")[0]
    assert (
        header["placement_count"],
        header["anchor_link_count"],
        header["retained_placements"],
        header["retained_links"],
    ) == (3, 3, 2, 1)
    assert header["metadata_omitted"] == 1
    assert header["session_id"] is None and header["host_call_id"] is None
    assert "PRIVATE PROMPT CANARY" not in "\n".join(store._conn.iterdump())
    store.close()


def test_partial_optional_schema_failure_keeps_core_open_and_rolls_back_ddl(tmp_path, monkeypatch):
    def fail(conn):
        conn.execute("CREATE TABLE delivery_occurrences (id INTEGER PRIMARY KEY)")
        raise sqlite3.OperationalError("synthetic optional failure")

    monkeypatch.setattr(delivery, "create_schema", fail)
    store = Store(tmp_path / "s")
    store.set_meta("core-still-usable", "yes")
    assert store.get_meta("core-still-usable") == "yes"
    assert rows(store, "delivery_occurrences") == []
    assert not store.record_delivery(sample())
    assert (
        store._conn.execute(
            "SELECT name FROM sqlite_master WHERE name='delivery_occurrences'"
        ).fetchone()
        is None
    )
    store.close()


def test_optional_store_lock_is_nonblocking(tmp_path):
    import threading

    store = Store(tmp_path / "s")
    held, release = threading.Event(), threading.Event()

    def own_lock():
        with store._lock:
            held.set()
            release.wait(3)

    thread = threading.Thread(target=own_lock)
    thread.start()
    assert held.wait(1)
    try:
        start = time.monotonic()
        assert not store.record_delivery(sample())
        assert time.monotonic() - start < 0.75
    finally:
        release.set()
        thread.join()
    assert rows(store, "delivery_occurrences") == []
    store.close()


def test_concurrent_optional_migration_leaves_old_index_usable(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    store = Store(tmp_path / "s")
    for table in ("delivery_anchor_links", "delivery_placements", "delivery_occurrences"):
        store._conn.execute(f"DROP TABLE {table}")
    store._conn.commit()
    path = store.path
    canonical = {str(p): p.read_bytes() for p in path.rglob("*.json")}
    store.close()

    def open_core(_):
        opened = Store(path)
        assert list(opened.iter_decisions()) == []
        opened.close()

    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(open_core, range(3)))
    reopened = Store(path)
    assert reopened.record_delivery(sample())
    assert canonical == {str(p): p.read_bytes() for p in path.rglob("*.json")}
    reopened.close()


def test_unknown_unresolved_evidence_cannot_be_reported_as_valid_empty(tmp_path):
    store = Store(tmp_path / "s")
    assert store.record_delivery(Delivery("query_decisions", structure=False, unresolved=None))
    header = rows(store, "delivery_occurrences")[0]
    assert header["outcome"] == "unknown"
    assert header["unresolved"] is None
    store.close()
