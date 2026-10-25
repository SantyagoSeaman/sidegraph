"""Conflicting Store writers must reserve before checking or publishing canonical state."""

from __future__ import annotations

import asyncio
import multiprocessing
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import fastmcp
import pytest
from fastmcp.exceptions import ToolError

from sidegraph import server
from sidegraph.schema import DecisionStatus
from sidegraph.store import Store, StoreBusyError
from sidegraph.write_boundary import WRITE_FAILURE, safe_write
from tests.store_record_support import _decision, _domain, _fact


def canonical_bytes(root: Path) -> dict[str, bytes]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in root.rglob("*")
        if p.is_file() and not p.name.startswith("index.db") and not p.name.endswith(".tmp")
    }


def _writer(root, mode, role, record_id, ready, go, paused, release, published, events, outcomes):
    """Each spawned process opens its own Store before the race starts."""
    with Store(root) as store:
        method = {
            "decision": "_write_decision_canonical",
            "fact": "_write_fact_canonical",
            "domain": "_write_domain_canonical",
        }.get(mode, "_write_decision_canonical")
        original = getattr(store, method)

        def publish(record):
            if role == "first":
                paused.set()
                if not release.wait(10):
                    raise TimeoutError("writer release missing")
            else:
                published.set()
                events.put("publish")
            return original(record)

        setattr(store, method, publish)
        if role == "second":

            def trace(sql):
                if sql.upper().startswith("BEGIN"):
                    events.put("begin")

            store._conn.set_trace_callback(trace)
        ready.set()
        if not go.wait(10):
            raise TimeoutError("writer start missing")
        try:
            if mode == "decision":
                store.add_decision(_decision(role, id=record_id))
            elif mode == "fact":
                store.add_fact(_fact(role, id=record_id))
            elif mode == "domain":
                store.add_domain(_domain("shared-slug", title=role))
            else:
                verdict = mode.split("-")[0 if role == "first" else 1]
                getattr(store, verdict)(record_id)
            outcomes.put((role, "ok"))
        except Exception as exc:
            outcomes.put((role, type(exc).__name__))


def assert_only_the_winning_verdict_is_published(tmp_path, mode):
    """Two spawned processes race one verdict (``mode`` picks the record kind or the ratify/
    drop order); only the first to reserve the writer may publish, the second is refused."""
    root = tmp_path / "shared store"
    with Store(root) as initial:
        proposal = _decision(status=DecisionStatus.PROPOSED)
        if mode in {"ratify-drop", "drop-ratify"}:
            initial.add_decision(proposal)
    context = multiprocessing.get_context("spawn")
    ready = [context.Event(), context.Event()]
    go = [context.Event(), context.Event()]
    paused, release, published = context.Event(), context.Event(), context.Event()
    events, outcomes = context.Queue(), context.Queue()
    processes = [
        context.Process(
            target=_writer,
            args=(
                root,
                mode,
                role,
                proposal.id,
                ready[i],
                go[i],
                paused,
                release,
                published,
                events,
                outcomes,
            ),
        )
        for i, role in enumerate(("first", "second"))
    ]
    try:
        for process in processes:
            process.start()
        assert all(event.wait(10) for event in ready), "stores did not open"
        go[0].set()
        assert paused.wait(10), "first writer did not reach publication"
        go[1].set()
        # An old deferred writer reports publication first, before its implicit BEGIN.
        assert events.get(timeout=10) == "begin", "second published before reserving"
        assert not published.wait(0.1), "second published while first owned the reservation"
        release.set()
        for process in processes:
            process.join(10)
            assert process.exitcode == 0
        assert dict(outcomes.get(timeout=2) for _ in processes) == {
            "first": "ok",
            "second": "ValueError",
        }
        with Store(root) as reopened:
            if mode == "decision":
                assert reopened.get_decision(proposal.id).title == "first"
            elif mode == "fact":
                assert reopened.get_fact(proposal.id).statement == "first"
            elif mode == "domain":
                assert reopened.find_domain_by_slug("shared-slug").title == "first"
            else:
                expected = (
                    DecisionStatus.ACCEPTED
                    if mode.startswith("ratify")
                    else DecisionStatus.REJECTED
                )
                assert reopened.get_decision(proposal.id).status == expected
    finally:
        release.set()
        for event in go:
            event.set()
        for process in processes:
            if process.pid is not None:
                process.join(2)
                if process.is_alive():
                    process.terminate()
                    process.join(2)
        for queue in (events, outcomes):
            queue.close()
            queue.join_thread()


def test_conflicting_processes_publish_only_the_winning_verdict(tmp_path):
    """The one real-subprocess store test on the Windows core list; the other record kinds and
    the ratify/drop orders run in ``tests/private``."""
    assert_only_the_winning_verdict_is_published(tmp_path, "decision")


def test_busy_admission_does_not_publish_any_canonical_bytes(tmp_path):
    root = tmp_path / "store"
    with Store(root) as first, Store(root) as second:
        before = canonical_bytes(root)
        second._conn.execute("PRAGMA busy_timeout = 10")
        with first._mutation(immediate=True), pytest.raises(sqlite3.OperationalError) as caught:
            second.add_decision(_decision())
        assert canonical_bytes(root) == before
        assert type(caught.value).__name__ == "StoreBusyError"
        assert caught.value.sqlite_errorcode & 0xFF == sqlite3.SQLITE_BUSY
        assert caught.value.sqlite_errorname == "SQLITE_BUSY"
        assert not second._conn.in_transaction


def test_commit_busy_after_publication_keeps_unsafe_retry_guidance(tmp_path, monkeypatch):
    root = tmp_path / "store"
    with Store(root) as store:
        record = _decision()

        def fail_commit():
            exc = sqlite3.OperationalError("database is locked")
            exc.sqlite_errorcode = sqlite3.SQLITE_BUSY
            raise exc

        monkeypatch.setattr(store, "_commit", fail_commit)

        @safe_write
        def write():
            return store.add_decision(record)

        with pytest.raises(ToolError) as caught:
            write()
        assert str(caught.value) == WRITE_FAILURE
        assert not store._conn.in_transaction
    with Store(root) as reopened:
        assert reopened.get_decision(record.id).choice == record.choice


def test_second_step_busy_does_not_claim_the_whole_tool_wrote_nothing(tmp_path, monkeypatch):
    root = tmp_path / "store"
    with Store(root) as store:
        monkeypatch.setattr(server, "_store", store)

        def busy(*args, **kwargs):
            raise StoreBusyError("private-diagnostic-canary")

        monkeypatch.setattr(store, "get_or_create_abstract_entity", busy)

        async def run():
            async with fastmcp.Client(server.mcp) as client:
                return await client.call_tool(
                    "add_decision",
                    {
                        "title": "First step",
                        "kind": "adr",
                        "context": "Test",
                        "choice": "Preserve earlier work",
                        "tags": ["performance"],
                    },
                    raise_on_error=False,
                )

        result = asyncio.run(run())
        assert result.is_error
        message = result.content[0].text
        assert "Store is busy" in message
        assert "Earlier steps may have written" in message
        assert "private-diagnostic-canary" not in message
    with Store(root) as reopened:
        records = list(reopened.iter_decisions())
        assert len(records) == 1
        assert records[0].title == "First step"


def test_domain_parent_lookup_waits_for_the_publication_reservation(tmp_path):
    root = tmp_path / "store"
    with Store(root) as first, Store(root) as second, ThreadPoolExecutor() as pool:
        old = first.add_domain(_domain("parent"))
        attempted, looked_up = threading.Event(), threading.Event()
        second._conn.set_trace_callback(
            lambda sql: attempted.set() if sql.upper().startswith("BEGIN") else None
        )
        original = second.find_domain_by_slug

        def lookup(slug):
            looked_up.set()
            return original(slug)

        second.find_domain_by_slug = lookup
        with first._mutation(immediate=True):
            future = pool.submit(
                server._add_domain_impl, second, None, "child", "Child", "Summary", "parent"
            )
            replacement = _domain("parent", supersedes=old.domain_id)
            assert attempted.wait(1), "server did not reserve before parent lookup"
            assert not looked_up.is_set()
            first.supersede_domain(old.domain_id, replacement)
        result = future.result(timeout=5)
        assert second.get_domain(result["domain_id"]).parent_id == replacement.domain_id


def test_fact_reachability_is_checked_after_waiting_for_the_writer(tmp_path):
    root = tmp_path / "store"
    with Store(root) as first, Store(root) as second, ThreadPoolExecutor() as pool:
        support = first.add_decision(_decision(status=DecisionStatus.PROPOSED))
        old = first.add_fact(_fact(supports=[support.id]))
        before = {p.name: p.read_bytes() for p in (root / "facts").glob("*.json")}
        attempted = threading.Event()
        second._conn.set_trace_callback(
            lambda sql: attempted.set() if sql.upper().startswith("BEGIN") else None
        )
        with first._mutation(immediate=True):
            future = pool.submit(
                server._supersede_fact_impl, second, None, old.id, "Replacement", "test"
            )
            assert attempted.wait(1), "server did not reserve before reachability check"
            first.drop(support.id)
        with pytest.raises(ValueError):
            future.result(timeout=5)
        assert {p.name: p.read_bytes() for p in (root / "facts").glob("*.json")} == before
