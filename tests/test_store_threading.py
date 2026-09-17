"""Thread-safety + parent-dir regressions (see CLAUDE.md release-blocker notes).

fastmcp 3 dispatches sync ``@mcp.tool`` calls onto worker threads via a thread pool, so a
single process-wide :class:`Store` (created in the main thread at import) is read/written
from other threads on every tool call. Before this fix that crashed every call with
"SQLite objects created in a thread can only be used in that same thread." These tests
exercise the store directly across real threads to catch any gap in the locking.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

from sidegraph.schema import Decision, DecisionKind, Provenance
from sidegraph.store import Store


def _decision(title: str) -> Decision:
    return Decision(
        title=title,
        kind=DecisionKind.ADR,
        context="c",
        choice="ch",
        valid_from=datetime.now(UTC),
        provenance=Provenance(source="manual"),
    )


def test_store_created_in_main_thread_usable_from_worker_thread(tmp_path):
    store = Store(tmp_path / "cross_thread.db")
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            d = store.add_decision(_decision("from worker thread"))
            assert store.get_decision(d.id) is not None
        except BaseException as e:  # noqa: BLE001 - captured to fail the test with detail
            errors.append(e)

    t = threading.Thread(target=worker)
    t.start()
    t.join()

    assert not errors, f"cross-thread store use raised: {errors}"


def test_store_usable_from_thread_pool_executor(tmp_path):
    store = Store(tmp_path / "cross_thread_pool.db")

    def worker() -> str:
        d = store.add_decision(_decision("from pool"))
        got = store.get_decision(d.id)
        assert got is not None
        return d.id

    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(lambda _: worker(), range(4)))

    assert len(ids) == 4
    assert len({*ids}) == 4  # each write landed distinctly


def test_concurrent_hammer_two_threads_twenty_upserts_each(tmp_path):
    """Two threads x 20 upserts, interleaved, to catch any gap in the serializing lock."""
    store = Store(tmp_path / "hammer.db")
    errors: list[BaseException] = []

    def hammer(prefix: str) -> None:
        try:
            for i in range(20):
                d = store.add_decision(_decision(f"{prefix}-{i}"))
                assert store.get_decision(d.id) is not None
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=hammer, args=(p,)) for p in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"concurrent hammer raised: {errors}"
    assert len(list(store.iter_decisions())) == 40


def test_store_creates_missing_parent_directories(tmp_path):
    db_path = tmp_path / "deep" / "nested" / "d.db"
    assert not db_path.parent.exists()

    store = Store(db_path)

    assert db_path.parent.is_dir()
    assert store.schema_version
