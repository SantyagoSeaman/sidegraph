"""Behavioral checks for hook-owned SQLite handles under real lock contention."""

from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

import sidegraph.host.hooks as hooks
import sidegraph.host.launch as launch
import sidegraph.host.subagent as subagent
from sidegraph import doctor as doctor_mod
from sidegraph import gitio as gitio_mod
from sidegraph import hot_index as hot_index_mod
from sidegraph import store_layout as store_layout_mod
from sidegraph import sync as sync_mod
from sidegraph.delivery import Delivery
from sidegraph.gitio import open_index_ro
from sidegraph.hot_index import HotIndex, Shown
from sidegraph.retrieval import DRIFT_CACHE_KEY
from sidegraph.schema import (
    AnchorBinding,
    Decision,
    DecisionKind,
    DecisionStatus,
    Descriptor,
    Entity,
    Provenance,
)
from sidegraph.store import MEMBERSHIP_STAMP_KEY, Store
from sidegraph.store_layout import VOLATILE_STALE_KEY, LegacyStoreError


class _Held:
    """One held lock: `release()` ends it now, otherwise it ends after its delay."""

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._released = threading.Event()

    def release(self) -> None:
        self._stop.set()

    def is_set(self) -> bool:
        return self._released.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        return self._released.wait(timeout)


@pytest.fixture
def held_lock():
    """Hold a real index lock until the test ends or a delay passes, and always release it."""

    helds: list[_Held] = []

    def hold(
        store_dir: Path,
        statement: str = "BEGIN EXCLUSIVE",
        delay: float = 30.0,
        database_path: Path | None = None,
    ) -> _Held:
        conn = sqlite3.connect(
            database_path or store_dir / "index.db", timeout=0.1, check_same_thread=False
        )
        conn.execute(statement)
        held = _Held()
        helds.append(held)

        def release() -> None:
            try:
                held._stop.wait(delay)
                if conn.in_transaction:
                    conn.rollback()
            finally:
                conn.close()
                held._released.set()

        threading.Thread(target=release, daemon=True).start()
        return held

    yield hold
    for held in helds:
        held.release()
        assert held.wait(2), "lock holder did not clean up"


def _make_store(path: Path) -> None:
    store = Store(path)
    store.close()


def _seed_published_store(store_dir: Path, *, cold: bool) -> None:
    store = Store(store_dir)
    decision = store.add_decision(
        Decision(
            title="A useful gotcha",
            kind=DecisionKind.GOTCHA,
            context="The behavior matters.",
            choice="Keep the guard.",
            status=DecisionStatus.ACCEPTED,
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="agent"),
        )
    )
    entity = store.upsert_entity(
        Entity(
            canonical_name="example",
            descriptor=Descriptor(name="example", file_path="src/example.py"),
        )
    )
    store.add_binding(
        AnchorBinding(record_id=decision.id, entity_id=entity.entity_id, tier=2, status="live")
    )
    store.set_meta(VOLATILE_STALE_KEY, "1" if cold else "0")
    store.close()


def _hook_case(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    cold: bool,
    telemetry: str,
) -> tuple[Path, Path, Path, dict[Path, bytes]]:
    """Shared project/store/transcript fixture for locked and healthy hook controls."""
    (root / "src").mkdir(parents=True)
    source = root / "src" / "example.py"
    source.write_text("value = 1\n", encoding="utf-8")
    store_dir = root / ".sidegraph"
    _seed_published_store(store_dir, cold=cold)
    monkeypatch.setenv("SIDEGRAPH_DIR", str(store_dir))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(root))
    monkeypatch.setenv("SIDEGRAPH_GRAPH", str(root / "missing-graph.json"))
    monkeypatch.setenv("SIDEGRAPH_SUBAGENT_BRIEF", "on")
    monkeypatch.setenv("SIDEGRAPH_CAPTURE_NUDGE", "on")
    monkeypatch.setenv("SIDEGRAPH_TELEMETRY", telemetry)
    monkeypatch.chdir(root)
    monkeypatch.setattr(launch, "record_launch_commit", lambda **_kwargs: None)

    transcript = root / "stop-1.jsonl"
    transcript.write_text(
        "\n".join(
            [
                json.dumps({"type": "user", "message": {"role": "user", "content": "one"}}),
                json.dumps(
                    {"type": "assistant", "message": {"role": "assistant", "content": "ok"}}
                ),
                json.dumps({"type": "user", "message": {"role": "user", "content": "two"}}),
                json.dumps(
                    {"type": "assistant", "message": {"role": "assistant", "content": "ok"}}
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    canonical_before = {
        path.relative_to(store_dir): path.read_bytes() for path in store_dir.rglob("*.json")
    }
    return store_dir, source, transcript, canonical_before


def _hook_payload(entry: str, source: Path, transcript: Path) -> dict:
    return {
        "session": {"session_id": "session-1"},
        "stop": {"session_id": "stop-1", "transcript_path": str(transcript)},
        "pretool": {
            "session_id": "read-1",
            "tool_name": "Read",
            "tool_input": {"file_path": str(source)},
        },
        "subagent": {"hook_event_name": "SubagentStart", "session_id": "parent-1"},
    }[entry]


def _assert_hook_refusal_left_no_ledger_rows(
    store_dir: Path, canonical_before: dict[Path, bytes]
) -> None:
    conn = sqlite3.connect(store_dir / "index.db")
    try:
        assert conn.execute("SELECT COUNT(*) FROM capture_sessions").fetchone()[0] == 0
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM meta WHERE key LIKE 'capture_rearm:%' "
                "OR key LIKE 'pretool_file:%'"
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute("SELECT COUNT(*) FROM retrieval_events WHERE kind = 'inject'").fetchone()[
                0
            ]
            == 0
        )
        assert conn.execute("SELECT COUNT(*) FROM retrieval_shows").fetchone()[0] == 0
    finally:
        conn.close()
    assert canonical_before == {
        path.relative_to(store_dir): path.read_bytes() for path in store_dir.rglob("*.json")
    }


def test_store_open_does_not_wait_for_index_writer(tmp_path: Path, held_lock) -> None:
    """The earliest hook Store path must not wait for a held index reservation."""
    store_dir = tmp_path / ".sidegraph"
    _make_store(store_dir)
    held_lock(store_dir)

    started = time.monotonic()
    store = None
    with contextlib.suppress(sqlite3.OperationalError):
        store = Store(store_dir, busy_timeout_seconds=0.0)
    elapsed = time.monotonic() - started
    if store is not None:
        store.close()

    assert elapsed < 0.5


def test_hot_index_open_refuses_writer_without_waiting(tmp_path: Path, held_lock) -> None:
    store_dir = tmp_path / ".sidegraph"
    _make_store(store_dir)
    held_lock(store_dir)

    started = time.monotonic()
    index = HotIndex.open(store_dir, busy_timeout_seconds=0.0)
    elapsed = time.monotonic() - started

    assert index is None
    assert elapsed < 0.5


def test_hot_index_write_refuses_immediate_writer_without_waiting(
    tmp_path: Path, held_lock
) -> None:
    store_dir = tmp_path / ".sidegraph"
    _make_store(store_dir)
    held_lock(store_dir, "BEGIN IMMEDIATE")

    started = time.monotonic()
    index = HotIndex.open(store_dir, busy_timeout_seconds=0.0)
    assert index is not None
    try:
        with pytest.raises(sqlite3.OperationalError):
            index.record_touch("session", "src/example.py", "Read")
        assert index.lock_lost
    finally:
        index.close()

    assert time.monotonic() - started < 0.5


def test_read_only_peek_refuses_exclusive_writer_without_waiting(tmp_path: Path, held_lock) -> None:
    store_dir = tmp_path / ".sidegraph"
    _make_store(store_dir)
    held_lock(store_dir)

    started = time.monotonic()
    conn = open_index_ro(store_dir, busy_timeout_seconds=0.0)
    elapsed = time.monotonic() - started
    if conn is not None:
        conn.close()

    assert conn is None
    assert elapsed < 0.5


def test_non_hook_timeout_defaults_are_preserved(tmp_path: Path) -> None:
    store_dir = tmp_path / ".sidegraph"
    _make_store(store_dir)

    store = Store(store_dir)
    assert store._conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    store.close()

    index = HotIndex.open(store_dir)
    assert index is not None
    assert index._conn.execute("PRAGMA busy_timeout").fetchone()[0] == 1000
    index.close()

    conn = open_index_ro(store_dir)
    assert conn is not None
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 500
    conn.close()


def test_default_store_open_keeps_legacy_wait(tmp_path: Path, held_lock) -> None:
    store_dir = tmp_path / ".sidegraph"
    _make_store(store_dir)
    released = held_lock(store_dir, "BEGIN EXCLUSIVE", delay=0.15)

    started = time.monotonic()
    store = Store(store_dir)
    elapsed = time.monotonic() - started
    store.close()

    assert released.is_set()
    assert elapsed < 1.5


def test_store_zero_timeout_survives_reload_and_restores_after_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store_dir = tmp_path / ".sidegraph"
    _make_store(store_dir)
    (store_dir / "index.db").unlink()

    store = Store(store_dir, busy_timeout_seconds=0.0)
    assert store._conn.execute("PRAGMA busy_timeout").fetchone()[0] == 0

    def fail_requirement(*, force: bool = False) -> None:
        raise RuntimeError("injected reload failure")

    monkeypatch.setattr(store, "_check_requirement", fail_requirement)
    with pytest.raises(RuntimeError, match="injected reload failure"):
        store._reload_index_from_canonical("forced")
    assert store._conn.execute("PRAGMA busy_timeout").fetchone()[0] == 0
    store.close()


@pytest.mark.parametrize(
    ("requested_timeout", "before_ms", "rebuild_ms"),
    [(None, 5000, 30000), (0.25, 250, 250)],
    ids=["default", "explicit-override"],
)
def test_store_rebuild_restores_timeout_after_success_and_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    requested_timeout: float | None,
    before_ms: int,
    rebuild_ms: int,
) -> None:
    store_dir = tmp_path / ".sidegraph"
    _make_store(store_dir)
    store = Store(
        store_dir,
        **({} if requested_timeout is None else {"busy_timeout_seconds": requested_timeout}),
    )
    assert store._conn.execute("PRAGMA busy_timeout").fetchone()[0] == before_ms
    real_check = store._check_requirement
    observed: list[int] = []

    def observe_requirement(*, force: bool = False) -> None:
        observed.append(store._conn.execute("PRAGMA busy_timeout").fetchone()[0])
        real_check(force=force)

    monkeypatch.setattr(store, "_check_requirement", observe_requirement)
    store._reload_index_from_canonical("forced-success")
    assert observed == [rebuild_ms]
    assert store._conn.execute("PRAGMA busy_timeout").fetchone()[0] == before_ms

    observed.clear()

    def fail_requirement(*, force: bool = False) -> None:
        observed.append(store._conn.execute("PRAGMA busy_timeout").fetchone()[0])
        raise RuntimeError("injected reload failure")

    monkeypatch.setattr(store, "_check_requirement", fail_requirement)
    with pytest.raises(RuntimeError, match="injected reload failure"):
        store._reload_index_from_canonical("forced-failure")
    assert observed == [rebuild_ms]
    assert store._conn.execute("PRAGMA busy_timeout").fetchone()[0] == before_ms
    store.close()


def test_cold_store_rebuild_refuses_immediate_writer_without_30_second_wait(
    tmp_path: Path, held_lock
) -> None:
    store_dir = tmp_path / ".sidegraph"
    _seed_published_store(store_dir, cold=False)
    canonical = next((store_dir / "decisions").glob("*.json"))
    canonical.write_bytes(canonical.read_bytes() + b" ")
    before = canonical.read_bytes()
    released = held_lock(store_dir, "BEGIN IMMEDIATE")

    started = time.monotonic()
    with pytest.raises(sqlite3.OperationalError):
        Store(store_dir, busy_timeout_seconds=0.0)

    assert time.monotonic() - started < 0.5
    assert canonical.read_bytes() == before
    released.release()
    assert released.wait(2), "cold rebuild lock holder did not clean up"
    reopened = Store(store_dir)
    try:
        assert reopened.get_meta(VOLATILE_STALE_KEY) == "1"
        assert canonical.read_bytes() == before
    finally:
        reopened.close()


def test_session_start_cold_rebuild_gives_up_on_a_long_writer_within_its_wait(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    held_lock,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    store_dir = root / ".sidegraph"
    _seed_published_store(store_dir, cold=False)
    canonical = next((store_dir / "decisions").glob("*.json"))
    canonical.write_bytes(canonical.read_bytes() + b" ")
    before = canonical.read_bytes()
    monkeypatch.setenv("SIDEGRAPH_DIR", str(store_dir))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(root))
    monkeypatch.chdir(root)
    monkeypatch.setattr(launch, "record_launch_commit", lambda **_kwargs: None)
    monkeypatch.setattr("sys.stdin", io.StringIO('{"session_id":"cold-rebuild"}'))
    held_lock(store_dir, "BEGIN IMMEDIATE")

    started = time.monotonic()
    hooks.session_start()
    elapsed = time.monotonic() - started

    # SessionStart waits HOOK_BUSY_SECONDS, not the 30 s a default Store would.
    assert elapsed < 2.5
    assert json.loads(capsys.readouterr().out) == {}
    assert canonical.read_bytes() == before


def test_shared_reader_commit_refusal_rolls_back_claim_and_showing(tmp_path: Path) -> None:
    store_dir = tmp_path / ".sidegraph"
    _seed_published_store(store_dir, cold=False)
    canonical_before = {
        path.relative_to(store_dir): path.read_bytes() for path in store_dir.rglob("*.json")
    }
    index = HotIndex.open(store_dir, busy_timeout_seconds=0.0)
    assert index is not None
    reader = sqlite3.connect(store_dir / "index.db", timeout=0.1)
    try:
        reader.execute("BEGIN")
        cursor = reader.execute("SELECT key FROM meta")
        cursor.fetchall()
        cursor.close()

        with pytest.raises(sqlite3.OperationalError):
            index.claim_file(
                "claimed:",
                "src/example.py",
                "session-1",
                10,
                shown=Shown("session-1", "agent-1", ["displayed-record"]),
            )

        assert not index._conn.in_transaction
        assert index.lock_lost
        assert (
            index._conn.execute(
                "SELECT 1 FROM meta WHERE key = ?", ("claimed:src/example.py",)
            ).fetchone()
            is None
        )
        assert (
            index._conn.execute(
                "SELECT 1 FROM retrieval_events WHERE session_id = ? AND kind = 'inject'",
                ("session-1",),
            ).fetchone()
            is None
        )
        assert (
            index._conn.execute(
                "SELECT 1 FROM retrieval_shows WHERE record_id = ?", ("displayed-record",)
            ).fetchone()
            is None
        )
        assert canonical_before == {
            path.relative_to(store_dir): path.read_bytes() for path in store_dir.rglob("*.json")
        }
    finally:
        reader.close()
        index.close()


def test_shared_reader_commit_refusal_heals_canonical_add_on_next_open(tmp_path: Path) -> None:
    store_dir = tmp_path / ".sidegraph"
    store = Store(store_dir, busy_timeout_seconds=0.0)
    old_digest = store.canonical_digest()
    assert old_digest is not None
    decision = Decision(
        title="Published before refused commit",
        kind=DecisionKind.GOTCHA,
        context="A commit can refuse after canonical publication.",
        choice="Rebuild from canonical files on the next open.",
        status=DecisionStatus.ACCEPTED,
        valid_from=datetime.now(UTC),
        provenance=Provenance(source="agent"),
    )
    reader = sqlite3.connect(store_dir / "index.db", timeout=0.1)
    try:
        reader.execute("BEGIN")
        cursor = reader.execute("SELECT key FROM meta")
        cursor.fetchall()
        cursor.close()

        with pytest.raises(sqlite3.OperationalError, match="locked"):
            store.add_decision(decision)

        assert not store._conn.in_transaction
        canonical_path = store_dir / "decisions" / f"{decision.id}.json"
        assert canonical_path.is_file()
        assert store.canonical_digest() == old_digest
        current_digest, _stats = store._compute_canonical_digest()
        assert current_digest != old_digest
    finally:
        reader.execute("ROLLBACK")
        reader.close()
        store.close()

    healed = Store(store_dir)
    try:
        assert healed.get_decision(decision.id) == decision
        healed_digest, _stats = healed._compute_canonical_digest()
        assert healed.canonical_digest() == healed_digest
        assert healed.get_meta(VOLATILE_STALE_KEY) == "1"
        assert healed.get_meta(MEMBERSHIP_STAMP_KEY) is None
    finally:
        healed.close()


def test_hot_open_keeps_handle_when_late_journal_pragma_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store_dir = tmp_path / ".sidegraph"
    _make_store(store_dir)
    original_connect = sqlite3.connect
    setup = original_connect(store_dir / "index.db")
    assert setup.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower() == "wal"
    setup.close()
    statements: list[str] = []
    errors: list[Exception] = []
    holders: list[sqlite3.Connection] = []
    reached_pragma = threading.Event()

    class ObserveConnection:
        def __init__(self, connection: sqlite3.Connection) -> None:
            self._connection = connection

        def execute(self, statement: str, *args):
            statements.append(statement)
            if statement.strip().upper() == "PRAGMA JOURNAL_MODE=TRUNCATE":
                holder = original_connect(
                    store_dir / "index.db", timeout=0.1, check_same_thread=False
                )
                holder.execute("BEGIN EXCLUSIVE")
                holder.execute("INSERT INTO meta (key, value) VALUES ('late-pragma-lock', 'held')")
                holders.append(holder)
                reached_pragma.set()
            return self._connection.execute(statement, *args)

        def __getattr__(self, name: str):
            return getattr(self._connection, name)

        def __enter__(self):
            self._connection.__enter__()
            return self

        def __exit__(self, exc_type, exc, traceback):
            return self._connection.__exit__(exc_type, exc, traceback)

    def observed_connect(*args, **kwargs):
        return ObserveConnection(original_connect(*args, **kwargs))

    monkeypatch.setattr(hot_index_mod.sqlite3, "connect", observed_connect)
    index = None
    try:
        index = HotIndex.open(store_dir, on_error=errors.append, busy_timeout_seconds=0.0)
        assert reached_pragma.is_set()
        assert index is not None
        assert not index._conn.in_transaction
        assert errors and isinstance(errors[0], sqlite3.OperationalError)
        pragma_at = statements.index("PRAGMA journal_mode=TRUNCATE")
        assert "SELECT value FROM meta WHERE key = ?" in statements[:pragma_at]
        assert any(
            "table_info(retrieval_events)" in statement for statement in statements[:pragma_at]
        )
    finally:
        for holder in holders:
            if holder.in_transaction:
                holder.rollback()
            holder.close()
        if index is not None:
            assert index._conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
            assert index.get_meta("late-pragma-lock") is None
            assert index.claim_meta("after-late-pragma", "usable")
            assert index.get_meta("after-late-pragma") == "usable"
            index.close()


def test_session_start_lazy_sync_commit_refusal_keeps_membership_uncertified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    store_dir = root / ".sidegraph"
    _seed_published_store(store_dir, cold=True)
    setup = Store(store_dir)
    setup.set_meta("last_synced_graph_version", "previous-graph")
    setup.close()
    canonical_before = {
        path.relative_to(store_dir): path.read_bytes() for path in store_dir.rglob("*.json")
    }
    graph = Path(__file__).parent / "fixtures" / "mini_graph.json"
    monkeypatch.setenv("SIDEGRAPH_DIR", str(store_dir))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(root))
    monkeypatch.setenv("SIDEGRAPH_GRAPH", str(graph))
    monkeypatch.chdir(root)
    monkeypatch.setattr(launch, "record_launch_commit", lambda **_kwargs: None)
    monkeypatch.setattr("sys.stdin", io.StringIO('{"session_id":"lazy-sync"}'))

    opened: list[Store] = []
    readers: list[sqlite3.Connection] = []
    timeouts: list[int] = []
    sync_reports = []
    traced_sql: list[str] = []
    sync_trace_windows: list[list[str]] = []
    real_store = Store
    real_maybe_sync = sync_mod.maybe_sync

    def observed_sync(*args, **kwargs):
        store = args[0]
        trace_start = len(traced_sql)
        reader = sqlite3.connect(store.path / "index.db", timeout=0.1)
        reader.execute("BEGIN")
        cursor = reader.execute("SELECT key FROM meta")
        cursor.fetchall()
        cursor.close()
        readers.append(reader)
        try:
            report = real_maybe_sync(*args, **kwargs)
        except sqlite3.OperationalError as error:
            sync_reports.append(error)
            return None
        finally:
            sync_trace_windows.append(traced_sql[trace_start:])
        sync_reports.append(report)
        return report

    def store_with_shared_reader(path, **kwargs):
        store = real_store(path, **kwargs)
        store._conn.set_trace_callback(traced_sql.append)
        timeouts.append(store._conn.execute("PRAGMA busy_timeout").fetchone()[0])
        opened.append(store)
        return store

    monkeypatch.setattr("sidegraph.store.Store", store_with_shared_reader)
    monkeypatch.setattr(sync_mod, "maybe_sync", observed_sync)
    try:
        hooks.session_start()
        output = json.loads(capsys.readouterr().out)
        assert isinstance(output, dict)
        context = output["hookSpecificOutput"]["additionalContext"]
        assert "A useful gotcha" in context
        assert len(opened) == 1
        assert timeouts == [int(store_layout_mod.HOOK_BUSY_SECONDS * 1000)]
        assert len(sync_reports) == 1
        assert isinstance(sync_reports[0], sqlite3.OperationalError)
        assert len(sync_trace_windows) == 1
        sync_sql = [statement.strip().upper() for statement in sync_trace_windows[0]]
        assert "COMMIT" in sync_sql
        assert "ROLLBACK" in sync_sql
        assert not opened[0]._conn.in_transaction
    finally:
        for reader in readers:
            reader.rollback()
            reader.close()

    assert canonical_before == {
        path.relative_to(store_dir): path.read_bytes() for path in store_dir.rglob("*.json")
    }
    reopened = real_store(store_dir)
    try:
        assert reopened.get_meta(VOLATILE_STALE_KEY) == "1"
    finally:
        reopened.close()


def test_zero_timeout_hot_handle_keeps_optional_delivery_independent(
    tmp_path: Path, held_lock
) -> None:
    store_dir = tmp_path / ".sidegraph"
    _make_store(store_dir)
    index = HotIndex.open(store_dir, busy_timeout_seconds=0.0)
    assert index is not None
    delivery = Delivery(surface="hook_read")
    try:
        assert index.record_delivery(delivery)
        assert index._conn.execute("PRAGMA busy_timeout").fetchone()[0] == 0
        held_lock(store_dir, "BEGIN EXCLUSIVE")
        started = time.monotonic()
        assert not index.record_delivery(delivery)
        assert time.monotonic() - started < 0.5
        assert index._conn.execute("PRAGMA busy_timeout").fetchone()[0] == 0
        assert not index.lock_lost
    finally:
        index.close()


def test_late_exclusive_lock_refuses_operations_on_already_open_handles(
    tmp_path: Path, held_lock
) -> None:
    store_dir = tmp_path / ".sidegraph"
    _make_store(store_dir)
    store = Store(store_dir, busy_timeout_seconds=0.0)
    hot = HotIndex.open(store_dir, busy_timeout_seconds=0.0)
    read_only = open_index_ro(store_dir, busy_timeout_seconds=0.0)
    assert hot is not None
    assert read_only is not None
    held_lock(store_dir, "BEGIN EXCLUSIVE")

    started = time.monotonic()
    with pytest.raises(sqlite3.OperationalError):
        store.set_meta("late-lock", "value")
    with pytest.raises(sqlite3.OperationalError):
        hot.record_touch("session", "src/a.py", "Read")
    with pytest.raises(sqlite3.OperationalError):
        read_only.execute("SELECT key FROM meta").fetchall()

    assert time.monotonic() - started < 0.5
    assert hot.lock_lost
    read_only.close()
    hot.close()
    store.close()


def test_zero_wait_drift_scan_withholds_uncertified_snapshot_without_cache_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = Store(tmp_path / ".sidegraph")
    old_cache = '{"head":"old","by_commit":{"c":["d"]}}'
    store.set_meta(DRIFT_CACHE_KEY, old_cache)
    seen: dict[str, object] = {}

    def uncomputed(*args, **kwargs):
        seen.update(kwargs)
        return doctor_mod.CodeDriftScan([], 0, True, False, "new", True, True)

    monkeypatch.setattr(sync_mod, "scan_code_drift", uncomputed)
    assert sync_mod.refresh_code_drift_cache(store, busy_timeout_seconds=0.0) is None
    assert seen["busy_timeout_seconds"] == 0.0
    assert store.get_meta(DRIFT_CACHE_KEY) == old_cache
    store.close()


def test_doctor_binding_snapshot_uses_zero_timeout_under_exclusive_writer(
    tmp_path: Path, held_lock
) -> None:
    store_dir = tmp_path / ".sidegraph"
    _make_store(store_dir)
    held_lock(store_dir)

    started = time.monotonic()
    snapshot = doctor_mod._binding_status_snapshot(store_dir, busy_timeout_seconds=0.0)

    assert snapshot.uncomputed
    assert time.monotonic() - started < 0.5


def test_locked_legacy_store_is_refused_without_waiting(tmp_path: Path, held_lock) -> None:
    legacy = tmp_path / "legacy.db"
    conn = sqlite3.connect(legacy)
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute("INSERT INTO meta VALUES ('schema_version', '0.3.0')")
    conn.commit()
    conn.close()
    held_lock(tmp_path, database_path=legacy)

    started = time.monotonic()
    with pytest.raises(LegacyStoreError):
        Store(legacy, busy_timeout_seconds=0.0)

    assert time.monotonic() - started < 0.5


def _make_drift_repo(tmp_path: Path, *, cold: bool) -> tuple[Path, Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args: str) -> str:
        result = subprocess.run(
            ["git", *args], cwd=repo, capture_output=True, text=True, check=True
        )
        return result.stdout.strip()

    git("init", "-q")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    source = repo / "a.py"
    source.write_text("before = True\n", encoding="utf-8")
    git("add", "a.py")
    git("commit", "-q", "-m", "before")
    captured_commit = git("rev-parse", "HEAD")

    store_dir = repo / ".sidegraph"
    store = Store(store_dir)
    decision = store.add_decision(
        Decision(
            title="Test decision",
            kind=DecisionKind.GOTCHA,
            context="Context",
            choice="Choice",
            status=DecisionStatus.ACCEPTED,
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="agent", commit=captured_commit),
        )
    )
    entity = store.upsert_entity(
        Entity(canonical_name="a.py", descriptor=Descriptor(name="a.py", file_path="a.py"))
    )
    store.add_binding(
        AnchorBinding(record_id=decision.id, entity_id=entity.entity_id, tier=2, status="live")
    )
    store.set_meta(VOLATILE_STALE_KEY, "1" if cold else "0")
    old_cache = '{"head":"previous","by_commit":{"old":["id"]}}'
    store.set_meta(DRIFT_CACHE_KEY, old_cache)
    store.close()

    source.write_text("after = True\n", encoding="utf-8")
    git("add", "a.py")
    git("commit", "-q", "-m", "after")

    return repo, store_dir, old_cache


def test_late_doctor_lock_keeps_existing_drift_cache_bytes(tmp_path: Path, monkeypatch) -> None:
    repo, store_dir, original_cache = _make_drift_repo(tmp_path, cold=False)
    store = Store(store_dir)
    try:
        snapshot = doctor_mod._binding_status_snapshot(store_dir, busy_timeout_seconds=0.0)
        assert not snapshot.uncomputed
        # A real, readable snapshot with certified statuses proves the refresh can publish a
        # fresh result before the late-lock refusal case below.
        fresh_count = sync_mod.refresh_code_drift_cache(
            store, repo_root=repo, busy_timeout_seconds=0.0
        )
        assert fresh_count == 1
        assert store.get_meta(DRIFT_CACHE_KEY) != original_cache
        store.set_meta(DRIFT_CACHE_KEY, original_cache)
    finally:
        store.close()

    original_snapshot = doctor_mod._binding_status_snapshot
    snapshot_timeouts: list[float] = []
    refused_snapshots: list[doctor_mod._BindingStatusSnapshot] = []

    def snapshot_under_real_lock(store_dir: Path, *, busy_timeout_seconds: float = 5.0):
        writer = sqlite3.connect(store_dir / "index.db", timeout=0.1)
        writer.execute("BEGIN EXCLUSIVE")
        snapshot_timeouts.append(busy_timeout_seconds)
        try:
            snapshot = original_snapshot(store_dir, busy_timeout_seconds=busy_timeout_seconds)
            refused_snapshots.append(snapshot)
            return snapshot
        finally:
            writer.rollback()
            writer.close()

    monkeypatch.setattr(doctor_mod, "_binding_status_snapshot", snapshot_under_real_lock)
    reopened = Store(store_dir)
    try:
        started = time.monotonic()
        assert (
            sync_mod.refresh_code_drift_cache(reopened, repo_root=repo, busy_timeout_seconds=0.0)
            is None
        )
        assert time.monotonic() - started < 0.5
        assert snapshot_timeouts == [0.0]
        assert len(refused_snapshots) == 1 and refused_snapshots[0].uncomputed
        assert reopened.get_meta(DRIFT_CACHE_KEY) == original_cache
    finally:
        reopened.close()


def test_readable_cold_drift_snapshot_without_lock_still_withholds_cache(
    tmp_path: Path,
) -> None:
    repo, store_dir, old_cache = _make_drift_repo(tmp_path, cold=True)
    store = Store(store_dir)
    try:
        snapshot = doctor_mod._binding_status_snapshot(store_dir, busy_timeout_seconds=0.0)
        assert snapshot.uncomputed
        assert snapshot.statuses
        scan = doctor_mod.scan_code_drift(store_dir, repo_root=repo, busy_timeout_seconds=0.0)
        assert scan.active
        assert scan.binding_statuses_uncomputed
        assert not scan.check_skipped
        assert (
            sync_mod.refresh_code_drift_cache(store, repo_root=repo, busy_timeout_seconds=0.0)
            is None
        )
        assert store.get_meta(DRIFT_CACHE_KEY) == old_cache
    finally:
        store.close()


# Cold and telemetry only change a path for the entries that read them: cold decides the
# SessionStart and SubagentStart rebuild, telemetry decides the PreToolUse event row.
_LOCK_CASES = [
    (entry, lock, cold, telemetry)
    for lock in ("BEGIN IMMEDIATE", "BEGIN EXCLUSIVE")
    for entry, cold, telemetry in [
        ("session", False, "on"),
        ("session", True, "on"),
        ("subagent", False, "on"),
        ("subagent", True, "on"),
        ("stop", False, "on"),
        ("pretool", False, "on"),
        ("pretool", False, "off"),
    ]
]


@pytest.mark.parametrize(
    ("entry", "lock_statement", "cold", "telemetry"),
    _LOCK_CASES,
    ids=[
        f"{entry}-{lock.split()[1].lower()}-{'cold' if cold else 'published'}-{telemetry}"
        for entry, lock, cold, telemetry in _LOCK_CASES
    ],
)
def test_real_hook_entrypoints_give_up_on_held_index_locks_within_their_wait(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    held_lock,
    entry: str,
    lock_statement: str,
    cold: bool,
    telemetry: str,
) -> None:
    root = tmp_path / "project"
    store_dir, source, transcript, canonical_before = _hook_case(
        root, monkeypatch, cold=cold, telemetry=telemetry
    )
    monkeypatch.setattr(
        "sys.stdin", io.StringIO(json.dumps(_hook_payload(entry, source, transcript)))
    )
    released = held_lock(store_dir, lock_statement)

    started = time.monotonic()
    if entry == "session":
        hooks.session_start()
    elif entry == "stop":
        hooks.stop()
    elif entry == "pretool":
        hooks.pre_tool_use()
    else:
        subagent.subagent_start()
    elapsed = time.monotonic() - started

    output = capsys.readouterr().out
    decoded = json.loads(output)
    assert isinstance(decoded, dict)
    # Stop and the optional peeks refuse at once; the three delivering hooks wait
    # HOOK_BUSY_SECONDS for the lock, a little over once in the worst case.
    assert elapsed < (0.5 if entry == "stop" else 2.5)
    if lock_statement == "BEGIN IMMEDIATE" and entry == "session":
        context = decoded["hookSpecificOutput"]["additionalContext"]
        assert "A useful gotcha" in context
    if lock_statement == "BEGIN IMMEDIATE" and not cold and entry == "subagent":
        context = decoded["hookSpecificOutput"]["additionalContext"]
        assert "1 record anchored to code in 1 file" in context
    if entry in {"stop", "pretool"} or lock_statement == "BEGIN EXCLUSIVE":
        assert decoded == {}
    if entry == "subagent" and cold:
        assert decoded == {}
    if entry in {"stop", "pretool"}:
        released.release()
        assert released.wait(2), "lock holder did not release before ledger inspection"
        _assert_hook_refusal_left_no_ledger_rows(store_dir, canonical_before)


@pytest.mark.parametrize("entry", ["stop", "pretool"])
def test_unlocked_stop_and_pretool_keep_healthy_delivery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    entry: str,
) -> None:
    root = tmp_path / "project"
    store_dir, source, transcript, canonical_before = _hook_case(
        root, monkeypatch, cold=False, telemetry="on"
    )
    monkeypatch.setattr(
        "sys.stdin", io.StringIO(json.dumps(_hook_payload(entry, source, transcript)))
    )

    if entry == "stop":
        hooks.stop()
    else:
        hooks.pre_tool_use()
    decoded = json.loads(capsys.readouterr().out)

    if entry == "stop":
        assert decoded["decision"] == "block"
        assert "propose_decisions" in decoded["reason"]
        check = Store(store_dir)
        try:
            assert check.was_captured("stop-1")
        finally:
            check.close()
    else:
        assert "A useful gotcha" in json.dumps(decoded)
    assert canonical_before == {
        path.relative_to(store_dir): path.read_bytes() for path in store_dir.rglob("*.json")
    }


# -- the delivering hooks wait HOOK_BUSY_SECONDS for the index --------------------------
# see design/superpowers/specs/2026-11-05-hook-wait-and-digest-recheck.md (D1, D2)


def _run_entry(entry: str) -> None:
    if entry == "session":
        hooks.session_start()
    elif entry == "stop":
        hooks.stop()
    elif entry == "pretool":
        hooks.pre_tool_use()
    else:
        subagent.subagent_start()


def _idle_seconds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys, entry: str) -> float:
    """How long the entry takes on this machine with nothing locked, in a project of its own:
    the timing tests below compare against it, so a slow runner does not fail them."""
    root = tmp_path / "idle"
    _store_dir, source, transcript, _ = _hook_case(root, monkeypatch, cold=False, telemetry="on")
    monkeypatch.setattr(
        "sys.stdin", io.StringIO(json.dumps(_hook_payload(entry, source, transcript)))
    )
    started = time.monotonic()
    _run_entry(entry)
    elapsed = time.monotonic() - started
    capsys.readouterr()
    return elapsed


@pytest.mark.parametrize(
    ("entry", "lock_statement"),
    [
        # A writer that holds RESERVED refuses the claim write, not the reads.
        ("pretool", "BEGIN IMMEDIATE"),
        # A writer in COMMIT holds EXCLUSIVE, which refuses every opener.
        ("session", "BEGIN EXCLUSIVE"),
        ("subagent", "BEGIN EXCLUSIVE"),
    ],
)
def test_delivering_hooks_wait_out_a_brief_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    held_lock,
    entry: str,
    lock_statement: str,
) -> None:
    """A lock that is gone in 150 ms must not cost the agent its records. Red against
    the zero-wait handles: the hook prints `{}` at once while the lock is still held."""
    root = tmp_path / "project"
    store_dir, source, transcript, _ = _hook_case(root, monkeypatch, cold=False, telemetry="on")
    monkeypatch.setattr(
        "sys.stdin", io.StringIO(json.dumps(_hook_payload(entry, source, transcript)))
    )
    held_lock(store_dir, lock_statement, delay=0.15)

    _run_entry(entry)

    decoded = json.loads(capsys.readouterr().out)
    context = decoded["hookSpecificOutput"]["additionalContext"]
    if entry == "subagent":
        assert "1 record anchored to code in 1 file" in context
    else:
        assert "A useful gotcha" in context


@pytest.mark.parametrize("entry", ["session", "pretool", "subagent"])
def test_delivering_hooks_stay_bounded_under_a_long_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    held_lock,
    entry: str,
) -> None:
    """Passes before and after the wait fix: it guards the wait against growing without bound.
    A lock held for 30 s costs the hook at most about twice the 0.5 s wait."""
    root = tmp_path / "project"
    store_dir, source, transcript, _ = _hook_case(root, monkeypatch, cold=False, telemetry="on")
    monkeypatch.setattr(
        "sys.stdin", io.StringIO(json.dumps(_hook_payload(entry, source, transcript)))
    )
    held_lock(store_dir, "BEGIN EXCLUSIVE")

    started = time.monotonic()
    _run_entry(entry)
    elapsed = time.monotonic() - started

    assert isinstance(json.loads(capsys.readouterr().out), dict)
    assert elapsed < 2.5


def test_a_failed_touch_write_does_not_withhold_the_delivery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The touch row is telemetry; the records are the product (spec D2). Red against the
    touch-first order: the failed write sets `lock_lost`, the opener hands out no handle and
    the same run delivers nothing."""
    root = tmp_path / "project"
    store_dir, source, transcript, _ = _hook_case(root, monkeypatch, cold=False, telemetry="on")
    monkeypatch.setattr(
        "sys.stdin", io.StringIO(json.dumps(_hook_payload("pretool", source, transcript)))
    )

    def refused(self, *args, **kwargs):
        self.lock_lost = True
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(HotIndex, "record_touch", refused)

    hooks.pre_tool_use()

    assert "A useful gotcha" in json.dumps(json.loads(capsys.readouterr().out))


@pytest.mark.parametrize("entry", ["pretool", "subagent"])
def test_the_waiting_hooks_use_the_shared_constant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys, entry: str
) -> None:
    """PreToolUse and SubagentStart open the hot index with HOOK_BUSY_SECONDS (spec D1); the
    SessionStart Store has its own test below. Reads the timeout the entry passes."""
    root = tmp_path / "project"
    store_dir, source, transcript, _ = _hook_case(root, monkeypatch, cold=False, telemetry="on")
    seen: list[float | None] = []
    real_open = HotIndex.open.__func__

    def spy(cls, store_path, **kwargs):
        seen.append(kwargs.get("busy_timeout_seconds"))
        return real_open(cls, store_path, **kwargs)

    monkeypatch.setattr(HotIndex, "open", classmethod(spy))
    monkeypatch.setattr(
        "sys.stdin", io.StringIO(json.dumps(_hook_payload(entry, source, transcript)))
    )
    _run_entry(entry)
    capsys.readouterr()
    assert seen == [store_layout_mod.HOOK_BUSY_SECONDS]
    assert store_layout_mod.HOOK_BUSY_SECONDS == 0.5


def test_a_hook_that_lost_one_wait_stops_waiting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    held_lock,
) -> None:
    """SessionStart writes five times. Under a lock held for the whole run it must wait once,
    not five times (spec D1): red against waiting at every write, which took 2.2 s."""
    idle = _idle_seconds(tmp_path, monkeypatch, capsys, "session")
    root = tmp_path / "project"
    store_dir, source, transcript, _ = _hook_case(root, monkeypatch, cold=False, telemetry="on")
    monkeypatch.setattr(
        "sys.stdin", io.StringIO(json.dumps(_hook_payload("session", source, transcript)))
    )
    held_lock(store_dir, "BEGIN IMMEDIATE")

    started = time.monotonic()
    hooks.session_start()
    elapsed = time.monotonic() - started

    assert json.loads(capsys.readouterr().out)
    # One wait of 0.5 s on top of the idle time; five waits would add 2.5 s.
    assert elapsed < idle + 1.0, f"SessionStart waited {elapsed:.2f}s (idle {idle:.2f}s)"


def test_session_start_waits_once_when_the_writer_arrives_after_the_dedupe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    held_lock,
) -> None:
    """The first write succeeds and a writer takes the reservation right after it: the telemetry
    stamp and both prunes must not each wait again. Red against a give-up that only the first
    write triggers (1.8 s, found by the Sol review round 1)."""
    idle = _idle_seconds(tmp_path, monkeypatch, capsys, "session")
    root = tmp_path / "project"
    store_dir, source, transcript, _ = _hook_case(root, monkeypatch, cold=False, telemetry="on")
    monkeypatch.setattr(
        "sys.stdin", io.StringIO(json.dumps(_hook_payload("session", source, transcript)))
    )
    real_duplicate = hooks._session_start_duplicate

    def duplicate_then_a_writer_arrives(*args, **kwargs):
        verdict = real_duplicate(*args, **kwargs)
        held_lock(store_dir, "BEGIN IMMEDIATE")
        return verdict

    monkeypatch.setattr(hooks, "_session_start_duplicate", duplicate_then_a_writer_arrives)

    started = time.monotonic()
    hooks.session_start()
    elapsed = time.monotonic() - started

    assert json.loads(capsys.readouterr().out)
    assert elapsed < idle + 1.0, f"SessionStart waited {elapsed:.2f}s after a late writer"


def test_stop_waiting_after_busy_makes_the_next_reservation_immediate(
    tmp_path: Path, held_lock
) -> None:
    """With the option, the first refused reservation waits the handle's timeout and every later
    one is refused at once; without it each waits the full timeout."""
    store_dir = tmp_path / "s"
    Store(store_dir).close()
    held_lock(store_dir, "BEGIN IMMEDIATE")

    def two_refusals(**options) -> tuple[float, float]:
        store = Store(store_dir, busy_timeout_seconds=0.4, **options)
        try:
            times = []
            for key in ("a", "b"):
                started = time.monotonic()
                with pytest.raises(sqlite3.OperationalError):
                    store.set_meta(key, "v")
                times.append(time.monotonic() - started)
            return times[0], times[1]
        finally:
            store.close()

    first, second = two_refusals(stop_waiting_after_busy=True)
    assert first >= 0.3 and second < 0.2
    first, second = two_refusals()
    assert first >= 0.3 and second >= 0.3


@pytest.mark.parametrize("entry", ["session", "stop"])
def test_the_optional_peeks_and_stop_keep_the_zero_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], entry: str
) -> None:
    """Only the three delivering hooks wait: SessionStart's own Store does, but its status and
    drift peeks, and everything Stop opens, still refuse a held lock at once (spec D1). A peek
    that started waiting would add its wait to every session start."""
    root = tmp_path / "project"
    store_dir, source, transcript, _ = _hook_case(root, monkeypatch, cold=False, telemetry="on")
    monkeypatch.setattr(
        "sys.stdin", io.StringIO(json.dumps(_hook_payload(entry, source, transcript)))
    )
    seen: dict[str, list] = {"peek": [], "drift": [], "store": []}
    real_peek = gitio_mod.open_index_ro
    real_init = Store.__init__
    drift = {
        name: getattr(sync_mod, name)
        for name in ("refresh_code_drift_snapshot", "refresh_code_drift_cache")
    }

    def peek(*args, **kwargs):
        seen["peek"].append(kwargs.get("busy_timeout_seconds"))
        return real_peek(*args, **kwargs)

    def init(self, *args, **kwargs):
        seen["store"].append(
            (kwargs.get("busy_timeout_seconds"), kwargs.get("stop_waiting_after_busy", False))
        )
        return real_init(self, *args, **kwargs)

    def drift_spy(name):
        def call(*args, **kwargs):
            seen["drift"].append(kwargs.get("busy_timeout_seconds"))
            return drift[name](*args, **kwargs)

        return call

    monkeypatch.setattr(gitio_mod, "open_index_ro", peek)
    monkeypatch.setattr(Store, "__init__", init)
    for name in drift:
        monkeypatch.setattr(sync_mod, name, drift_spy(name))

    _run_entry(entry)
    capsys.readouterr()

    assert seen["peek"] and set(seen["peek"]) == {0.0}
    assert set(seen["drift"]) <= {0.0}
    first = seen["store"][0]
    if entry == "session":
        assert first == (store_layout_mod.HOOK_BUSY_SECONDS, True)
    else:
        assert first == (0.0, False)


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="measured on POSIX only: on the Windows CI runner the open took 1.16 s under an "
    "EXCLUSIVE lock and the cause was not isolated (Windows file locking differs); the "
    "hook-level bounds in this module hold there",
)
def test_opening_a_store_under_an_exclusive_lock_waits_once(tmp_path: Path, held_lock) -> None:
    """The best-effort journal-mode pragma does not spend the handle's wait: the open waits for
    the schema statement only. Red against a pragma that waits too (1.14 s, found by the Opus
    review round 1)."""
    store_dir = tmp_path / "s"
    Store(store_dir).close()
    held_lock(store_dir, "BEGIN EXCLUSIVE")

    started = time.monotonic()
    with pytest.raises(sqlite3.OperationalError):
        Store(store_dir, busy_timeout_seconds=0.5)
    elapsed = time.monotonic() - started

    assert 0.4 <= elapsed < 0.9, f"Store() waited {elapsed:.2f}s"


def test_a_refused_commit_also_stops_the_waiting(tmp_path: Path, held_lock) -> None:
    """A reader blocks a COMMIT while the reservation is granted; that refusal waited the whole
    timeout, so the next one must not (found by the Sol review round 2). Red against a give-up
    that only watches the reservation: the second commit waits again."""
    store_dir = tmp_path / "s"
    Store(store_dir).close()
    reader = sqlite3.connect(store_dir / "index.db", isolation_level=None, timeout=0.1)
    reader.execute("BEGIN")
    reader.execute("SELECT count(*) FROM meta").fetchall()  # a SHARED lock, kept
    store = Store(store_dir, busy_timeout_seconds=0.4, stop_waiting_after_busy=True)
    try:
        times = []
        for key in ("a", "b"):
            started = time.monotonic()
            with pytest.raises(sqlite3.OperationalError):
                store.set_meta(key, "v")
            times.append(time.monotonic() - started)
        assert times[0] >= 0.3, f"the first refusal did not wait: {times}"
        assert times[1] < 0.2, f"the second refusal waited again: {times}"
    finally:
        reader.execute("ROLLBACK")
        reader.close()
        store.close()


def test_optional_evidence_refusal_leaves_the_waiting_policy_alone(
    tmp_path: Path, held_lock
) -> None:
    """The optional evidence writes run with a zero connection timeout. Their refusal waited
    for nothing, so it must not consume the handle's wait (found by the Sol review round 2):
    after it the handle still waits for an ordinary write."""
    store_dir = tmp_path / "s"
    Store(store_dir).close()
    store = Store(store_dir, busy_timeout_seconds=0.4, stop_waiting_after_busy=True)
    try:
        held = held_lock(store_dir, "BEGIN IMMEDIATE", delay=0.1)
        assert store._delivery_write(lambda conn: None) is False
        assert held.wait(2)
        assert store._busy_timeout_seconds == 0.4
        assert store._conn.execute("PRAGMA busy_timeout").fetchone()[0] == 400
    finally:
        store.close()


def test_pretool_waits_for_a_held_lock_once_not_once_per_step(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    held_lock,
) -> None:
    """A read that loses its lock wait marks the handle like a refused write does, so the touch
    write after it is not attempted and the run waits once. Red against a flag that only a failed
    write sets: the touch write then waits a second time (measured: 0.55 s + 0.54 s)."""
    root = tmp_path / "project"
    store_dir, source, transcript, _ = _hook_case(root, monkeypatch, cold=False, telemetry="on")
    monkeypatch.setattr(
        "sys.stdin", io.StringIO(json.dumps(_hook_payload("pretool", source, transcript)))
    )
    touches: list[str] = []
    real_touch = HotIndex.record_touch

    def counting(self, *args, **kwargs):
        touches.append("touch")
        return real_touch(self, *args, **kwargs)

    monkeypatch.setattr(HotIndex, "record_touch", counting)
    real_open = HotIndex.open.__func__

    def open_then_lock(cls, store_path, **kwargs):
        index = real_open(cls, store_path, **kwargs)
        held_lock(store_dir, "BEGIN EXCLUSIVE")  # the writer arrives after the handle opened
        return index

    monkeypatch.setattr(HotIndex, "open", classmethod(open_then_lock))

    hooks.pre_tool_use()

    assert isinstance(json.loads(capsys.readouterr().out), dict)
    assert touches == []


def test_records_for_waits_for_a_held_lock_once_however_many_entities(
    tmp_path: Path, held_lock
) -> None:
    """A read that loses its wait marks the handle; the entities and tables after it are not
    read, so one call waits once. Red against a loop that carries on: three entities cost three
    refused waits (measured 1.7 s at a 0.5 s wait). The wait is 1.0 s so that three of them (3 s)
    stay apart from one plus a slow Windows runner (a 0.3 s wait took 0.8 s there)."""
    store_dir = tmp_path / ".sidegraph"
    _make_store(store_dir)
    index = HotIndex.open(store_dir, busy_timeout_seconds=1.0)
    assert index is not None
    errors: list[Exception] = []
    try:
        held_lock(store_dir)  # the writer arrives after the handle opened

        started = time.monotonic()
        records = index.records_for(["e1", "e2", "e3"], on_error=errors.append)
        elapsed = time.monotonic() - started

        assert records == []
        assert index.lock_lost
        assert len(errors) == 1
        assert elapsed < 2.0
        started = time.monotonic()
        assert index.records_for(["e4"], on_error=errors.append) == []
        assert time.monotonic() - started < 0.5  # a marked handle refuses at once, not after 1 s
    finally:
        index.close()


def test_records_for_skips_the_fact_read_after_the_decision_read_lost_its_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The writer can arrive after the binding query: the decision read loses its wait and the
    fact read after it must not wait again."""
    store_dir = tmp_path / ".sidegraph"
    _seed_published_store(store_dir, cold=False)
    index = HotIndex.open(store_dir)
    assert index is not None
    facts_read: list[str] = []

    def refused(self, record_ids, now):
        error = sqlite3.OperationalError("database is locked")
        error.sqlite_errorcode = sqlite3.SQLITE_BUSY
        raise error

    monkeypatch.setattr(HotIndex, "_decisions_for_bindings", refused)
    monkeypatch.setattr(
        HotIndex, "_facts_for_bindings", lambda self, ids: facts_read.append("facts") or []
    )
    try:
        entity_id = next(iter(index.anchored_files().values()))[0]
        errors: list[Exception] = []
        index.records_for([entity_id], on_error=errors.append)
        assert index.lock_lost
        assert len(errors) == 1
        assert facts_read == []
    finally:
        index.close()


def test_a_read_failing_for_another_reason_leaves_the_handle_usable(tmp_path: Path) -> None:
    """Only a lock refusal marks the handle: a missing table is a damaged or older index, and
    the reads after it still run."""
    store_dir = tmp_path / ".sidegraph"
    _make_store(store_dir)
    index = HotIndex.open(store_dir)
    assert index is not None
    try:
        index._conn.execute("DROP TABLE meta")
        with pytest.raises(sqlite3.OperationalError):
            index.get_meta("x")
        assert not index.lock_lost
    finally:
        index.close()


def test_record_shows_on_a_marked_handle_does_not_wait(tmp_path: Path, held_lock) -> None:
    """The brief's journal write after a read that lost its wait is skipped, not retried: a
    marked handle refuses it at once. Red against a write that ignores the mark (it waited out
    the timeout again: measured 0.55 s inside a 1.13 s hook)."""
    store_dir = tmp_path / ".sidegraph"
    _make_store(store_dir)
    index = HotIndex.open(store_dir, busy_timeout_seconds=0.3)
    assert index is not None
    try:
        held_lock(store_dir)  # the writer arrives after the handle opened
        index.lock_lost = True  # an earlier read of this run lost its wait

        started = time.monotonic()
        index.record_shows("s1", [("a.py", "r1")])
        assert time.monotonic() - started < 0.15
    finally:
        index.close()
