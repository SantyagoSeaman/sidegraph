"""``index.db`` runs in ``journal_mode=TRUNCATE`` on every connection that writes.

SQLite's default rollback mode (DELETE) creates ``index.db-journal`` at the start of every
write transaction and unlinks it at the end; the hooks write on every tool call, so the file
system saw hundreds of thousands of create/delete events an hour from that one file. TRUNCATE
keeps the same rollback journal, the same locking and the same crash safety, and leaves one
0-byte ``index.db-journal`` in place instead of recreating it. Readers (``mode=ro``) write
nothing either way.
"""

from __future__ import annotations

import multiprocessing
import os
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sidegraph.hot_index import HotIndex
from sidegraph.schema import Decision, DecisionKind, Provenance
from sidegraph.store import Store


def _mode(conn: sqlite3.Connection) -> str:
    return str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()


def test_store_connection_is_truncate(tmp_path: Path) -> None:
    store = Store(tmp_path / "s")
    try:
        assert _mode(store._conn) == "truncate"
    finally:
        store.close()


def test_hot_path_write_connection_is_truncate(tmp_path: Path) -> None:
    Store(tmp_path / "s").close()
    index = HotIndex.open(tmp_path / "s")
    assert index is not None
    try:
        assert _mode(index._conn) == "truncate"
    finally:
        index.close()


def test_journal_is_kept_empty_and_not_recreated(tmp_path: Path) -> None:
    """After N writes the journal exists, is 0 bytes and is the same inode throughout."""
    root = tmp_path / "s"
    store = Store(root)
    journal = root / "index.db-journal"
    try:
        store.set_meta("k0", "v")
        assert journal.is_file(), "TRUNCATE leaves the journal in place after a commit"
        inode = journal.stat().st_ino
        for n in range(1, 40):
            store.set_meta(f"k{n}", "v")
            assert journal.stat().st_size == 0
            assert journal.stat().st_ino == inode, "the journal was unlinked and recreated"
    finally:
        store.close()
    index = HotIndex.open(root)
    assert index is not None
    try:
        for n in range(20):
            index.claim_meta(f"h{n}", "v")
            assert journal.stat().st_size == 0
            assert journal.stat().st_ino == inode
    finally:
        index.close()


def _hold_write_lock(path: str, ready: object, release: object) -> None:
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("PRAGMA journal_mode=TRUNCATE")
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("INSERT INTO meta (key, value) VALUES ('held', 'x')")
    ready.set()  # type: ignore[attr-defined]
    release.wait(30)  # type: ignore[attr-defined]
    conn.execute("ROLLBACK")
    conn.close()


def test_opening_while_another_process_holds_a_write_lock(tmp_path: Path) -> None:
    """The pragma must not fail or block when a writer holds the lock: setting TRUNCATE on a
    database already in rollback mode takes no lock of its own."""
    root = tmp_path / "s"
    Store(root).close()
    ctx = multiprocessing.get_context("spawn")
    ready, release = ctx.Event(), ctx.Event()
    proc = ctx.Process(target=_hold_write_lock, args=(str(root / "index.db"), ready, release))
    proc.start()
    try:
        assert ready.wait(30)
        started = time.monotonic()
        index = HotIndex.open(root)
        assert index is not None, "the hot path gave up opening under a held write lock"
        assert _mode(index._conn) == "truncate"
        index.close()
        store = Store(root)
        assert _mode(store._conn) == "truncate"
        store.close()
        assert time.monotonic() - started < 4.0, "opening waited on the held lock"
    finally:
        release.set()
        proc.join(30)


def _die_mid_transaction(path: str, ready: object) -> None:
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("PRAGMA journal_mode=TRUNCATE")
    conn.execute("PRAGMA cache_size=1")  # force dirty pages out so the journal is non-empty
    conn.execute("BEGIN IMMEDIATE")
    for n in range(2000):
        conn.execute("INSERT INTO meta (key, value) VALUES (?, ?)", (f"crash{n}", "x" * 500))
    ready.set()  # type: ignore[attr-defined]
    time.sleep(60)


@pytest.mark.skipif(os.name == "nt", reason="kill -9 semantics")
def test_kill_mid_transaction_leaves_a_hot_journal_the_next_writer_rolls_back(
    tmp_path: Path,
) -> None:
    """Same as DELETE mode: a killed writer leaves a NON-empty journal, a ``mode=ro`` reader
    does not write, and the next writer rolls the half-written transaction back."""
    root = tmp_path / "s"
    Store(root).close()
    journal = root / "index.db-journal"
    ctx = multiprocessing.get_context("spawn")
    ready = ctx.Event()
    proc = ctx.Process(target=_die_mid_transaction, args=(str(root / "index.db"), ready))
    proc.start()
    try:
        assert ready.wait(60)
        os.kill(proc.pid, 9)  # type: ignore[arg-type]
        proc.join(30)
    finally:
        if proc.is_alive():
            proc.kill()
    assert journal.stat().st_size > 0, "a killed writer must leave a hot journal"
    before = journal.stat().st_size
    ro = sqlite3.connect(f"file:{root / 'index.db'}?mode=ro", uri=True)
    try:
        assert ro.execute("SELECT count(*) FROM meta WHERE key LIKE 'crash%'").fetchone()[0] == 0
    except sqlite3.OperationalError:
        pass  # a reader that cannot roll back may refuse; it must not write
    finally:
        ro.close()
    assert journal.exists()
    assert journal.stat().st_size == before
    store = Store(root)
    try:
        assert store.get_meta("crash0") is None
        assert store._conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        # hot-journal recovery deletes the file; any later write leaves it at 0 bytes
        assert not journal.exists() or journal.stat().st_size == 0
        store.set_meta("after", "v")
        assert journal.stat().st_size == 0
    finally:
        store.close()


def test_a_brief_exclusive_lock_is_waited_out_not_failed(tmp_path: Path) -> None:
    """Setting the pragma reads the file header, so an EXCLUSIVE lock (a writer mid-commit)
    blocks it like any other read. It waits the connection's busy timeout, the same as the
    schema read before it, so opening during a short commit succeeds instead of giving up."""
    import threading

    root = tmp_path / "s"
    Store(root).close()
    holder = sqlite3.connect(str(root / "index.db"), isolation_level=None, check_same_thread=False)
    holder.execute("BEGIN EXCLUSIVE")
    threading.Timer(0.3, lambda: holder.execute("ROLLBACK")).start()
    index = HotIndex.open(root)
    assert index is not None
    try:
        assert _mode(index._conn) == "truncate"
    finally:
        index.close()
        holder.close()


def test_a_reopen_that_refreshes_the_index_keeps_the_same_journal(tmp_path: Path) -> None:
    """The pragma must run before the schema and freshness writes: a canonical record touched
    since the last open makes the next open reload the index, and that write transaction must
    already be in TRUNCATE mode (create-and-delete on every such reopen otherwise)."""
    root = tmp_path / "s"
    store = Store(root)
    store.set_meta("k", "v")
    store.close()
    journal = root / "index.db-journal"
    inode = journal.stat().st_ino
    store = Store(root)
    store.add_decision(_decision())
    store.close()
    inode = journal.stat().st_ino
    record = next((root / "decisions").iterdir())
    stat = record.stat()
    os.utime(record, ns=(stat.st_atime_ns, stat.st_mtime_ns + 2_000_000_000))
    store = Store(root)
    store.close()
    assert journal.stat().st_ino == inode, "the reopen's refresh recreated the journal"
    assert journal.stat().st_size == 0


def _decision() -> Decision:
    return Decision(
        title="t",
        valid_from=datetime.now(UTC),
        kind=DecisionKind.GOTCHA,
        context="c",
        choice="x",
        provenance=Provenance(source="manual"),
    )


def _put_in_wal(index: Path, *, pin: bool = True) -> sqlite3.Connection:
    holder = sqlite3.connect(str(index), isolation_level=None, check_same_thread=False)
    assert str(holder.execute("PRAGMA journal_mode=WAL").fetchone()[0]).lower() == "wal"
    # an open read transaction pins the WAL: leaving WAL needs exclusive access, so it is refused
    if pin:
        holder.execute("BEGIN")
        holder.execute("SELECT count(*) FROM meta").fetchall()
    return holder


def test_an_index_another_tool_holds_in_wal_still_opens_and_works(tmp_path: Path) -> None:
    """Switching out of WAL needs exclusive access, so with another WAL connection open the
    pragma fails with 'database is locked'. That must not refuse the open (it worked before
    the pragma existed): both handles stay in the file's mode and write normally."""
    root = tmp_path / "s"
    Store(root).close()
    holder = _put_in_wal(root / "index.db")
    try:
        store = Store(root)
        try:
            store.set_meta("w", "1")
            assert store.get_meta("w") == "1"
            assert _mode(store._conn) == "wal"
        finally:
            store.close()
        index = HotIndex.open(root)
        assert index is not None, "a pragma that cannot run refused an otherwise usable open"
        try:
            assert index.claim_meta("hw", "1")
            assert _mode(index._conn) == "wal"
        finally:
            index.close()
    finally:
        holder.close()


def test_a_refused_hot_open_leaves_the_journal_mode_alone(tmp_path: Path) -> None:
    """The pragma comes after every refusal check: an index the hot path will not use (here a
    foreign schema_version) must not be switched out of WAL by merely being looked at."""
    root = tmp_path / "s"
    Store(root).close()
    holder = _put_in_wal(root / "index.db", pin=False)
    holder.execute("UPDATE meta SET value = 'other' WHERE key = 'schema_version'")
    holder.close()  # last connection: the file stays in WAL mode, no -wal left behind
    assert HotIndex.open(root) is None
    check = sqlite3.connect(str(root / "index.db"))
    try:
        assert _mode(check) == "wal"
    finally:
        check.close()
