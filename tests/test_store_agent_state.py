"""Per-agent hook state, store side: the ``agent`` column on touch rows, the atomic one-shot
claim, and the meta-prefix prune.

A Claude Code subagent's hook payload carries its parent's ``session_id``, so a one-shot key
or a touch row keyed on the session alone covers the whole agent tree. The host passes an
opaque agent id and the store never interprets it.
See design/superpowers/specs/2026-10-01-per-agent-hook-state-design.md (D2, D3, D4).
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime, timedelta

import pytest

from sidegraph.store import Store

_LEGACY_RETRIEVAL_EVENTS = """CREATE TABLE retrieval_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    at         TEXT NOT NULL,
    kind       TEXT NOT NULL,
    key        TEXT NOT NULL,
    detail     TEXT
)"""


def _columns(store: Store) -> list[str]:
    return [row["name"] for row in store._conn.execute("PRAGMA table_info(retrieval_events)")]


def _make_legacy_index(path) -> None:
    """Rewrite an existing store's ``retrieval_events`` into the pre-column shape, with two
    old rows. Built from literal DDL, never from the code under test."""
    Store(path).close()
    conn = sqlite3.connect(str(path / "index.db"))
    try:
        conn.execute("DROP TABLE retrieval_events")
        conn.execute(_LEGACY_RETRIEVAL_EVENTS)
        conn.execute("CREATE INDEX idx_retrieval_events_session ON retrieval_events (session_id)")
        conn.executemany(
            "INSERT INTO retrieval_events (session_id, at, kind, key, detail) "
            "VALUES (?, ?, 'touch', ?, 'Read')",
            [("old-1", datetime.now(UTC).isoformat(), "a.py"), ("old-2", "2026-01-01", "b.py")],
        )
        conn.commit()
    finally:
        conn.close()


# -- T5: touch rows carry the agent ---------------------------------------------------------


def test_a_touch_by_a_subagent_records_the_agent_under_the_parent_session(tmp_path):
    store = Store(tmp_path / "s")
    store.record_touch("P", "a.py", "Read", agent="A")
    store.record_touch("P", "b.py", "Read")

    rows = store._conn.execute(
        "SELECT session_id, key, agent FROM retrieval_events ORDER BY id"
    ).fetchall()
    assert [(r["session_id"], r["key"], r["agent"]) for r in rows] == [
        ("P", "a.py", "A"),
        ("P", "b.py", None),
    ]
    # the public read is unchanged: session-scoped, the parent's id for every agent
    assert [e["key"] for e in store.retrieval_events("P")] == ["a.py", "b.py"]


# -- T6 / T6b: the column reaches an index that predates it ---------------------------------


def test_a_fresh_index_has_the_agent_column(tmp_path):
    assert "agent" in _columns(Store(tmp_path / "s"))


def test_an_index_that_predates_the_column_gains_it_and_old_rows_read_null(tmp_path):
    path = tmp_path / "s"
    _make_legacy_index(path)

    store = Store(path)
    assert "agent" in _columns(store)
    store.record_touch("P", "new.py", "Read", agent="A")

    rows = store._conn.execute("SELECT session_id, agent FROM retrieval_events ORDER BY id")
    assert [(r["session_id"], r["agent"]) for r in rows] == [
        ("old-1", None),
        ("old-2", None),
        ("P", "A"),
    ]


def test_two_openers_that_both_miss_the_column_do_not_fail_the_second_open(tmp_path, monkeypatch):
    """The race is real: two hook processes open one legacy index, both read ``table_info``
    before either ALTERs, and the loser's ALTER raises "duplicate column name". Forced here
    with two real connections: the moment the first opener's ``table_info`` read returns, a
    second, independent connection performs the ALTER, so the first opener's own ALTER hits
    the column that now exists."""
    path = tmp_path / "s"
    _make_legacy_index(path)
    real_connect = sqlite3.connect
    raced: list[bool] = []
    rival_errors: list[BaseException] = []

    class RacingConnection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            cursor = super().execute(sql, *args, **kwargs)
            if "table_info(retrieval_events)" not in sql or raced:
                return cursor
            rows = cursor.fetchall()  # finish the read: an open statement would hold a lock
            raced.append(True)
            rival = real_connect(str(path / "index.db"), timeout=1)
            try:
                rival.execute("ALTER TABLE retrieval_events ADD COLUMN agent TEXT")
                rival.commit()
            except BaseException as exc:
                rival_errors.append(exc)
            finally:
                rival.close()
            return iter(rows)

    monkeypatch.setattr(
        sqlite3, "connect", lambda *a, **kw: real_connect(*a, factory=RacingConnection, **kw)
    )

    store = Store(path)  # must not raise

    assert raced == [True], "the rival ALTER never ran: the test did not reach the window"
    assert rival_errors == [], "the rival opener failed to ALTER, so no race was staged"
    assert "agent" in _columns(store)


def test_a_migration_that_fails_for_another_reason_does_not_fail_the_open(tmp_path, monkeypatch):
    """A locked index, a read-only filesystem: the touch row is a convenience, never worth a
    crash on open. The failing ALTER is swallowed, the next open retries it, and the store
    serves reads in between."""
    path = tmp_path / "s"
    _make_legacy_index(path)
    real_connect = sqlite3.connect
    refusing = {"on": True}

    class LockedConnection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if refusing["on"] and "ADD COLUMN agent" in sql:
                raise sqlite3.OperationalError("database is locked")
            return super().execute(sql, *args, **kwargs)

    monkeypatch.setattr(
        sqlite3, "connect", lambda *a, **kw: real_connect(*a, factory=LockedConnection, **kw)
    )

    store = Store(path)  # must not raise
    assert "agent" not in _columns(store)
    assert [e["key"] for e in store.retrieval_events("old-1")] == ["a.py"]
    store.close()

    refusing["on"] = False
    assert "agent" in _columns(Store(path))


# -- claim_meta: atomic one-shot claim ------------------------------------------------------


def test_claim_meta_succeeds_once_and_keeps_the_first_value(tmp_path):
    store = Store(tmp_path / "s")

    assert store.claim_meta("k", "first") is True
    assert store.claim_meta("k", "second") is False
    assert store.get_meta("k") == "first"


def test_claim_meta_refuses_the_schema_version_key(tmp_path):
    store = Store(tmp_path / "s")
    with pytest.raises(ValueError):
        store.claim_meta("schema_version", "9")


def test_claim_meta_is_atomic_across_connections(tmp_path):
    """Eight real connections claim each key at the same instant: exactly one wins per key.
    A get-then-set claim lets several of them read "absent" before any writes; this loop
    fails it within the first few keys."""
    path = tmp_path / "s"
    Store(path).close()
    stores = [Store(path) for _ in range(8)]
    keys = [f"pretool_nudge:S:agent-{i}" for i in range(40)]
    wins: dict[str, int] = dict.fromkeys(keys, 0)
    guard = threading.Lock()
    errors: list[BaseException] = []

    def worker(store: Store) -> None:
        try:
            for key in keys:
                barrier.wait()
                if store.claim_meta(key, "v"):
                    with guard:
                        wins[key] += 1
        except BaseException as exc:  # a broken barrier must fail the test, not hang it
            errors.append(exc)
            barrier.abort()

    barrier = threading.Barrier(len(stores))
    threads = [threading.Thread(target=worker, args=(s,)) for s in stores]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert errors == []
    assert set(wins.values()) == {1}, {k: v for k, v in wins.items() if v != 1}


# -- prune_meta_prefixes (T8, store side) ---------------------------------------------------


def _iso(days_ago: float) -> str:
    return (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()


def test_prune_meta_prefixes_drops_stale_and_legacy_keys_and_keeps_the_rest(tmp_path):
    store = Store(tmp_path / "s")
    for key, value in {
        "pretool_nudge:old": _iso(31),
        "pretool_nudge:legacy": "1",
        "pretool_nudge:fresh": _iso(1),
        "pretool_nudge_path:old:agent": _iso(40),
        "pretool_nudge_path:fresh:agent": _iso(2),
        # `_` is a LIKE wildcard: a LIKE match on "pretool_nudge:" would delete this one too
        "pretoolXnudge:other": _iso(90),
        "unrelated": _iso(90),
        "unrelated_legacy": "1",
    }.items():
        store.set_meta(key, value)

    deleted = store.prune_meta_prefixes(
        ("pretool_nudge:", "pretool_nudge_path:"), older_than=timedelta(days=30)
    )

    assert deleted == 3
    for key in (
        "pretool_nudge:fresh",
        "pretool_nudge_path:fresh:agent",
        "pretoolXnudge:other",
        "unrelated",
        "unrelated_legacy",
    ):
        assert store.get_meta(key) is not None, key
    for key in ("pretool_nudge:old", "pretool_nudge:legacy", "pretool_nudge_path:old:agent"):
        assert store.get_meta(key) is None, key


def test_prune_meta_prefixes_keeps_a_value_that_is_neither_a_timestamp_nor_legacy(tmp_path):
    store = Store(tmp_path / "s")
    store.set_meta("pretool_nudge:odd", "0")
    store.set_meta("pretool_nudge:text", "not a timestamp")

    assert store.prune_meta_prefixes(("pretool_nudge:",), older_than=timedelta(days=30)) == 0
    assert store.get_meta("pretool_nudge:odd") == "0"


def test_prune_meta_prefixes_never_touches_the_schema_version_stamp(tmp_path):
    """The stamp's real value is neither ``"1"`` nor a timestamp, so the value test alone
    would spare it. Set it to the legacy ``"1"`` that the prune deletes elsewhere: only the
    key guard stands between it and the DELETE."""
    store = Store(tmp_path / "s")
    with store._mutation():
        store._conn.execute("UPDATE meta SET value = '1' WHERE key = 'schema_version'")
    assert store.schema_version == "1"

    store.prune_meta_prefixes(("schema",), older_than=timedelta(days=0))

    assert store.schema_version == "1"


def test_prune_meta_prefixes_refuses_an_empty_prefix(tmp_path):
    """An empty prefix matches every key; the caller must name what it prunes."""
    store = Store(tmp_path / "s")
    store.set_meta("pretool_nudge:legacy", "1")
    with pytest.raises(ValueError):
        store.prune_meta_prefixes(("",), older_than=timedelta(days=30))
    assert store.get_meta("pretool_nudge:legacy") == "1"
