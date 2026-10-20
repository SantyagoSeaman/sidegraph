"""Behavioral checks for hook-owned SQLite handles under real lock contention."""

from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import subprocess
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

import sidegraph.host.hooks as hooks
import sidegraph.host.launch as launch
import sidegraph.host.subagent as subagent
from sidegraph import doctor as doctor_mod
from sidegraph import hot_index as hot_index_mod
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
from sidegraph.store_layout import VOLATILE_STALE_KEY


@pytest.fixture
def held_lock():
    """Hold a real index lock briefly and always release it if the opener regresses."""

    handles: list[sqlite3.Connection] = []
    releases: list[threading.Event] = []

    def hold(
        store_dir: Path,
        statement: str = "BEGIN EXCLUSIVE",
        delay: float = 0.8,
        database_path: Path | None = None,
    ) -> threading.Event:
        conn = sqlite3.connect(
            database_path or store_dir / "index.db", timeout=0.1, check_same_thread=False
        )
        conn.execute(statement)
        released = threading.Event()
        handles.append(conn)
        releases.append(released)

        def release() -> None:
            try:
                time.sleep(delay)
                if conn.in_transaction:
                    conn.rollback()
            finally:
                conn.close()
                released.set()

        threading.Thread(target=release, daemon=True).start()
        return released

    yield hold
    for released in releases:
        assert released.wait(2), "lock holder did not clean up"


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
        assert index.write_failed
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
    assert elapsed >= 0.05
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
    assert released.wait(2), "cold rebuild lock holder did not clean up"
    reopened = Store(store_dir)
    try:
        assert reopened.get_meta(VOLATILE_STALE_KEY) == "1"
        assert canonical.read_bytes() == before
    finally:
        reopened.close()


def test_session_start_cold_rebuild_refuses_immediate_writer_promptly(
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

    assert elapsed < 0.5
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
        assert index.write_failed
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
        assert timeouts == [0]
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
        assert not index.write_failed
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
    assert hot.write_failed
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


def test_locked_legacy_store_read_uses_zero_timeout(tmp_path: Path, held_lock) -> None:
    legacy = tmp_path / "legacy.db"
    conn = sqlite3.connect(legacy)
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute("INSERT INTO meta VALUES ('schema_version', '0.3.0')")
    conn.commit()
    conn.close()
    held_lock(tmp_path, database_path=legacy)

    started = time.monotonic()
    with pytest.raises(sqlite3.OperationalError):
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


@pytest.mark.parametrize("entry", ["session", "stop", "pretool", "subagent"])
@pytest.mark.parametrize("lock_statement", ["BEGIN IMMEDIATE", "BEGIN EXCLUSIVE"])
@pytest.mark.parametrize("cold", [False, True], ids=["published", "cold"])
@pytest.mark.parametrize("telemetry", ["on", "off"])
def test_real_hook_entrypoints_refuse_held_index_locks_promptly(
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
    assert elapsed < 0.5
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
