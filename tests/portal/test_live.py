"""Poller signals on the authenticated SSE stream (D9, D11)."""

import asyncio
import os
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, aclosing, contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from queue import Empty, Queue
from socket import socket
from threading import Event, Thread

import httpx
import pytest
import uvicorn
from httpx_sse import connect_sse

from sidegraph import store as store_module
from sidegraph.portal.app import create_app
from sidegraph.portal.context import PortalContext
from sidegraph.portal.live import Broadcaster, Poller
from sidegraph.portal.settings import PortalSettings
from sidegraph.schema import AnchorBinding, Descriptor, Entity
from sidegraph.store import Store
from sidegraph.store_layout import REBIND_DONE_KEY, VOLATILE_STALE_KEY
from sidegraph.sync import LAST_SYNCED_KEY, sync
from tests.graph_fixtures import node, reader_of
from tests.store_record_support import _decision
from tests.test_sync_rebind import (
    _commit_the_move,
    _community_move_setup,
    _fail_the_reconcile_once,
    _status_of,
)

_POLL_SECONDS = 2


@pytest.fixture
def ctx(portal_ctx) -> PortalContext:
    return portal_ctx(settings=PortalSettings(poll_seconds=_POLL_SECONDS))


@pytest.fixture
def polls(monkeypatch: pytest.MonkeyPatch) -> Queue[None]:
    observations: Queue[None] = Queue()
    original = Store.advice_fingerprint

    def read(store: Store) -> str:
        fingerprint = original(store)
        observations.put(None)
        return fingerprint

    monkeypatch.setattr(Store, "advice_fingerprint", read)
    return observations


@contextmanager
def _serve(ctx: PortalContext) -> Iterator[httpx.Client]:
    # A real stream lets HTTPX bound reads even if an expected event never arrives.
    with socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        app = create_app(replace(ctx, port=port))
        server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
        thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        origin = f"http://127.0.0.1:{port}"
        try:
            with httpx.Client(
                base_url=origin,
                headers={"Origin": origin, "X-Sidegraph-Token": ctx.token},
                timeout=_POLL_SECONDS + 2.0,
                trust_env=False,
            ) as client:
                yield client
        finally:
            server.should_exit = True
            thread.join(timeout=5)
            assert not thread.is_alive()


@contextmanager
def _stream(client: httpx.Client) -> Iterator[Iterator[tuple[str, str]]]:
    ticket = client.post("/api/live-ticket").raise_for_status().json()["ticket"]
    with connect_sse(client, "GET", "/live", params={"ticket": ticket}) as source:
        source.response.raise_for_status()
        yield ((frame.event, frame.data) for frame in source.iter_sse())


def _baseline(polls: Queue[None]) -> None:
    try:
        polls.get(timeout=1.5)
    except Empty:
        pytest.fail("the subscribed stream did not take an initial snapshot")


@pytest.mark.parametrize(
    "first_snapshot_seconds", [0.0, 0.95, 3.2], ids=["ordinary", "slow-snapshot", "overrun"]
)
def test_record_change_by_other_process_within_interval(
    ctx, polls, monkeypatch: pytest.MonkeyPatch, first_snapshot_seconds: float
) -> None:
    started: list[float] = []
    if first_snapshot_seconds:
        original = Store.refresh_if_stale
        delay = first_snapshot_seconds

        def refresh(store: Store) -> None:
            nonlocal delay
            started.append(time.monotonic())
            time.sleep(delay)
            delay = 0.95
            original(store)

        monkeypatch.setattr(Store, "refresh_if_stale", refresh)
    with _serve(ctx) as client, _stream(client) as events:
        _baseline(polls)
        decision = _decision()
        path = ctx.store_path / "decisions" / f"{decision.id}.json"
        subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; from pathlib import Path; Path(sys.argv[1]).write_text(sys.argv[2])",
                str(path),
                decision.model_dump_json() + "\n",
            ],
            check=True,
        )
        written_at = time.monotonic()

        assert next(events) == ("changed", '{"what":"records"}')
        assert time.monotonic() - written_at < _POLL_SECONDS + 0.6
        if first_snapshot_seconds > _POLL_SECONDS:
            assert started[1] - started[0] >= 2 * _POLL_SECONDS - 0.4


def test_immediate_resubscribe_preserves_the_next_poll_deadline(ctx, polls) -> None:
    async def run() -> None:
        broadcaster = Broadcaster()
        poller = Poller(ctx, broadcaster)
        async with aclosing(broadcaster.stream(poller)) as first:
            await anext(first)
            polls.get_nowait()
        async with aclosing(broadcaster.stream(poller)) as second:
            await anext(second)
            with pytest.raises(Empty):
                await asyncio.to_thread(polls.get, True, 0.25)
            with Store(ctx.store_path) as writer:
                writer.add_decision(_decision())
            frame = await asyncio.wait_for(anext(second), _POLL_SECONDS + 0.5)
            assert (frame.event, frame.data) == ("changed", '{"what":"records"}')

    asyncio.run(run())


def test_stream_opens_after_the_initial_snapshot(ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    snapshot_started = Event()
    let_snapshot_finish = Event()
    stream_opened = Event()
    original = Store.refresh_if_stale

    def refresh(store: Store) -> None:
        if not snapshot_started.is_set():
            snapshot_started.set()
            assert let_snapshot_finish.wait(timeout=3)
        original(store)

    monkeypatch.setattr(Store, "refresh_if_stale", refresh)

    def subscribe(client: httpx.Client) -> tuple[str, str]:
        with _stream(client) as events:
            stream_opened.set()
            with Store(ctx.store_path) as writer:
                writer.add_decision(_decision())
            return next(events)

    with _serve(ctx) as client, ThreadPoolExecutor(max_workers=1) as requests:
        event = requests.submit(subscribe, client)
        try:
            assert snapshot_started.wait(timeout=2)
            assert not stream_opened.wait(timeout=0.3)
        finally:
            let_snapshot_finish.set()
        assert event.result() == ("changed", '{"what":"records"}')


def test_first_successful_snapshot_after_stale_refetches(ctx, monkeypatch) -> None:
    first = True
    original = Store.refresh_if_stale

    def refresh(store: Store) -> None:
        nonlocal first
        if first:
            first = False
            raise sqlite3.OperationalError("database is locked")
        original(store)

    monkeypatch.setattr(Store, "refresh_if_stale", refresh)
    with _serve(ctx) as client, _stream(client) as events:
        assert next(events) == ("stale", "")
        with Store(ctx.store_path) as writer:
            writer.add_decision(_decision())
        assert next(events) == ("changed", '{"what":"records"}')


def test_live_recovers_when_an_invalid_index_is_replaced(ctx) -> None:
    index = ctx.store_path / "index.db"
    replacement = ctx.store_path / "replacement.db"
    replacement.write_bytes(index.read_bytes())
    index.write_bytes(b"not a SQLite database\n")
    with _serve(ctx) as client, _stream(client) as events:
        assert next(events) == ("stale", "")
        os.replace(replacement, index)
        assert next(events) == ("changed", '{"what":"records"}')


def test_live_reopens_a_failed_store_handle(ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    first_handle: Store | None = None
    original = Store.refresh_if_stale

    def refresh(store: Store) -> None:
        nonlocal first_handle
        if first_handle is None:
            first_handle = store
        elif store is first_handle:
            raise sqlite3.DatabaseError("file is not a database")
        original(store)

    monkeypatch.setattr(Store, "refresh_if_stale", refresh)
    with _serve(ctx) as client, _stream(client) as events:
        assert next(events) == ("stale", "")
        assert first_handle is not None
        with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
            first_handle.canonical_digest()
        with Store(ctx.store_path) as writer:
            writer.add_decision(_decision())
        assert next(events) == ("changed", '{"what":"records"}')


@pytest.mark.parametrize("pending", [False, True], ids=["status-only", "same-graph-pending"])
def test_status_only_sync_and_same_graph_rebind_send_changed_sync(
    ctx, polls, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pending: bool
) -> None:
    with ExitStack() as stack:
        if pending:
            reader, writer, decision, entity = _community_move_setup(tmp_path, with_sibling=False)
            stack.enter_context(writer)
            _commit_the_move(tmp_path)
            _fail_the_reconcile_once(monkeypatch)
            sync(writer, reader, force=True)
            assert _status_of(writer, decision, entity) == "live"
        else:
            reader = reader_of(tmp_path, node("new-node", "symbol", "code.py", community=None))
            writer = stack.enter_context(Store(ctx.store_path))
            decision = writer.add_decision(_decision())
            entity = writer.upsert_entity(
                Entity(
                    canonical_name="symbol",
                    descriptor=Descriptor(name="symbol", file_path="code.py"),
                )
            )
            binding = AnchorBinding(record_id=decision.id, entity_id=entity.entity_id, tier=2)
            writer.add_binding(binding)
            sync(writer, reader)
            writer.add_binding(binding.model_copy(update={"status": "orphaned"}))
        digest = writer.canonical_digest()
        keys = (LAST_SYNCED_KEY, VOLATILE_STALE_KEY, REBIND_DONE_KEY)
        before = [writer.get_meta(key) for key in keys]
        ctx = replace(ctx, store_path=writer.path, graph_path=reader.path)

        with _serve(ctx) as client, _stream(client) as events:
            _baseline(polls)
            sync(writer, reader, force=not pending)
            assert writer.canonical_digest() == digest
            assert [writer.get_meta(key) for key in keys] == before
            if pending:
                assert _status_of(writer, decision, entity) == "orphaned"
            else:
                assert writer.bindings_for_record(decision.id)[0].status == "live"
            assert next(events) == ("changed", '{"what":"sync"}')


@pytest.fixture
def set_advice_clock(monkeypatch: pytest.MonkeyPatch) -> Callable[[datetime], None]:
    at = datetime.now(UTC)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return at.astimezone(tz)

    monkeypatch.setattr(store_module, "datetime", Clock)

    def set_at(value: datetime) -> None:
        nonlocal at
        at = value

    return set_at


def test_advice_older_at_sends_changed_advice(ctx, polls, set_advice_clock) -> None:
    now = datetime.now(UTC)
    set_advice_clock(now)
    with Store(ctx.store_path) as writer:
        decision = writer.add_decision(_decision(status="proposed"))
        newest = writer.add_decision(_decision(status="proposed"))
        assert writer.write_advice(
            [(decision.id, "accept", "first"), (newest.id, "accept", "keep")], None
        ) == {decision.id: "recorded", newest.id: "recorded"}
        with _serve(ctx) as client, _stream(client) as events:
            _baseline(polls)
            set_advice_clock(now - timedelta(seconds=1))
            assert writer.write_advice([(decision.id, "reject", "older")], None) == {
                decision.id: "recorded"
            }
            assert max(row["at"] for row in writer.advice_rows()) == now.isoformat()
            assert next(events) == ("changed", '{"what":"advice"}')


def test_advice_prune_sends_changed_advice(ctx, polls, set_advice_clock) -> None:
    now = datetime.now(UTC)
    set_advice_clock(now - timedelta(days=31))
    with Store(ctx.store_path) as writer:
        decision = writer.add_decision(_decision(status="proposed"))
        assert writer.write_advice([(decision.id, "accept", "old")], None) == {
            decision.id: "recorded"
        }
        set_advice_clock(now)
        newest = writer.add_decision(_decision(status="proposed"))
        assert writer.write_advice([(newest.id, "accept", "keep")], None) == {newest.id: "recorded"}
        with _serve(ctx) as client, _stream(client) as events:
            _baseline(polls)
            assert writer.prune_telemetry_events() == 1
            assert [row["record_id"] for row in writer.advice_rows()] == [newest.id]
            assert next(events) == ("changed", '{"what":"advice"}')


def test_no_stream_no_store_calls(ctx, polls, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: Queue[None] = Queue()
    original = PortalContext.poller_store

    def open_store(context: PortalContext) -> Store:
        calls.put(None)
        return original(context)

    monkeypatch.setattr(PortalContext, "poller_store", open_store)
    with _serve(ctx) as client:
        assert client.get("/api/session").status_code == 200
        with pytest.raises(Empty):
            calls.get(timeout=_POLL_SECONDS + 0.15)
        with _stream(client):
            _baseline(polls)
            assert calls.get_nowait() is None
        with pytest.raises(Empty):
            polls.get(timeout=_POLL_SECONDS + 0.15)


def test_held_reservation_poll_returns_in_about_1s_sends_stale_recovers(
    ctx, polls, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts: Queue[float] = Queue()
    original = Store.refresh_if_stale

    def refresh(store: Store) -> None:
        if store._busy_timeout_seconds == 1.0:
            attempts.put(time.monotonic())
        original(store)

    monkeypatch.setattr(Store, "refresh_if_stale", refresh)
    with _serve(ctx) as client, _stream(client) as events, ThreadPoolExecutor() as requests:
        _baseline(polls)
        attempts.get_nowait()
        with subprocess.Popen(
            [
                sys.executable,
                "-u",
                "-c",
                "import sqlite3, sys; db = sqlite3.connect(sys.argv[1]); "
                'db.execute("BEGIN IMMEDIATE"); print("held"); sys.stdin.readline(); db.rollback()',
                str(ctx.store_path / "index.db"),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        ) as writer:
            assert writer.stdout is not None and writer.stdin is not None
            try:
                assert writer.stdout.readline().strip() == "held"
                decision = _decision()
                (ctx.store_path / "decisions" / f"{decision.id}.json").write_text(
                    decision.model_dump_json() + "\n"
                )
                started = attempts.get(timeout=_POLL_SECONDS + 0.75)
                session = requests.submit(client.get, "/api/session")
                assert client.post("/api/live-ticket", timeout=0.75).status_code == 200
                assert next(events) == ("stale", "")
                assert 0.8 <= time.monotonic() - started < 1.75
                assert session.result().status_code == 503
            finally:
                writer.stdin.close()
        assert writer.returncode == 0
        assert next(events) == ("changed", '{"what":"records"}')
        assert client.get("/api/session").status_code == 200


def test_graph_replaced_sends_changed_graph(ctx, polls, tmp_path: Path) -> None:
    graph = tmp_path / "graph.json"
    graph.write_text('{"nodes":[],"links":[]}\n')
    replacement = tmp_path / "graph-new.json"
    replacement.write_bytes(graph.read_bytes())
    stamp = graph.stat()
    os.utime(replacement, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    ctx = replace(ctx, graph_path=graph)

    with _serve(ctx) as client, _stream(client) as events:
        _baseline(polls)
        os.replace(replacement, graph)
        assert next(events) == ("changed", '{"what":"graph"}')


def test_branch_switch_mass_change(ctx, polls, tmp_path: Path, monkeypatch) -> None:
    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q", "-b", "first")
    files = ctx.store_path / "decisions"
    for number in range(300):
        decision = _decision(title=f"first branch {number}")
        (files / f"{decision.id}.json").write_text(decision.model_dump_json() + "\n")
    git("add", "--", ".sidegraph")
    git(
        "-c",
        "user.name=Portal Test",
        "-c",
        "user.email=portal@example.test",
        "commit",
        "-qm",
        "first",
    )
    git("switch", "-qc", "second")
    for path in files.glob("*.json"):
        path.unlink()
    for number in range(300):
        decision = _decision(title=f"second branch {number}")
        (files / f"{decision.id}.json").write_text(decision.model_dump_json() + "\n")
    git("add", "--", ".sidegraph")
    git(
        "-c",
        "user.name=Portal Test",
        "-c",
        "user.email=portal@example.test",
        "commit",
        "-qm",
        "second",
    )
    git("switch", "-q", "first")

    switched = Event()
    initial = True
    original = Store.refresh_if_stale

    def refresh(store: Store) -> None:
        nonlocal initial
        if not initial:
            # This case checks one settled mass replacement, even if Git is slow under load.
            assert switched.wait(timeout=10)
        initial = False
        original(store)

    monkeypatch.setattr(Store, "refresh_if_stale", refresh)
    with _serve(ctx) as client, _stream(client) as events:
        _baseline(polls)
        try:
            git("switch", "-q", "second")
        finally:
            switched.set()
        assert next(events) == ("changed", '{"what":"records"}')
        with Store(ctx.store_path) as reader:
            titles = [record.title for record in reader.iter_decisions()]
        assert len(titles) == 300
        assert all(title.startswith("second branch") for title in titles)
        with pytest.raises(httpx.ReadTimeout):
            next(events)
