"""The SessionStart dedupe across real processes.

The hook registered twice on one host (the plugin and a hand-written project hook) fires both
copies for one session at once. ``_session_start_duplicate`` used to read the ledger and write it
in two transactions, so both copies read the old value, both decided "not a duplicate", and both
injected the map. A same-process test cannot reach that: it needs two separate OS processes
released at the same instant. Measured on the unfixed code, two processes and a fresh session id
per round: 189, 191 and 193 of 200 rounds doubled with fork, 198 of 200 with spawn.

The fix reads lock-free first (a duplicate takes no lock) and decides inside one immediate
transaction otherwise (``Store.update_meta_if``).
see design/superpowers/specs/2026-10-02-session-start-dedupe-atomic-design.md (T1, T6)
"""

from __future__ import annotations

import io
import json
import multiprocessing as mp
import queue
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path

import sidegraph.host.hooks as hooks

_ROUNDS = 100
_WORKERS = 2
_BARRIER_TIMEOUT = 10
_RESULT_TIMEOUT = 60


def _context():
    """'fork' where available: spawn's per-child interpreter start-up jitter masks the race
    (198 of 200 rounds doubled instead of 189-193 -- both catch it, fork is the tighter probe).
    Falls back to 'spawn' where 'fork' is missing (Windows); the assertions hold either way."""
    start_method = "fork" if "fork" in mp.get_all_start_methods() else "spawn"
    return mp.get_context(start_method)


def _race_worker(store_dir: str, barrier, result_queue) -> None:
    """Module-level (picklable under 'spawn'): a long-lived worker with its own ``Store``. Every
    round it waits for its sibling, then asks the dedupe about the round's shared session id.
    Reports one ``emitted`` flag per round, or the failure; a broken barrier aborts the rest."""
    try:
        from sidegraph.store import Store

        store = Store(store_dir)
        emitted: list[bool] = []
        for round_no in range(_ROUNDS):
            barrier.wait(timeout=_BARRIER_TIMEOUT)
            duplicate = hooks._session_start_duplicate(store, f"race-{round_no}", datetime.now(UTC))
            emitted.append(not duplicate)
        store.close()
        result_queue.put(("ok", emitted))
    except BaseException as exc:  # report every failure mode, and free the sibling
        barrier.abort()
        result_queue.put(("error", f"{type(exc).__name__}: {exc}"))


def test_two_processes_starting_one_session_together_emit_exactly_once(tmp_path):
    """Two processes ask the dedupe about the same fresh session id at the same instant, a
    hundred rounds running: in every round exactly one of them emits (a round where neither
    does would be a lost map, so it fails too)."""
    from sidegraph.store import Store

    store_dir = tmp_path / "s"
    Store(store_dir).close()  # the store exists before the race, so only the dedupe is raced
    ctx = _context()
    barrier = ctx.Barrier(_WORKERS)
    result_queue = ctx.Queue()
    procs = [
        ctx.Process(target=_race_worker, args=(str(store_dir), barrier, result_queue))
        for _ in range(_WORKERS)
    ]
    outcomes: list[tuple[str, object]] = []
    try:
        for p in procs:
            p.start()
        for _ in procs:
            try:
                outcomes.append(result_queue.get(timeout=_RESULT_TIMEOUT))
            except queue.Empty:  # keep reading: the sibling's own error says why
                outcomes.append(("error", f"no result within {_RESULT_TIMEOUT}s (hung worker)"))
    finally:
        for p in procs:
            p.join(timeout=10)
            if p.is_alive():  # a hung worker fails the test instead of hanging CI
                p.terminate()
                p.join(timeout=5)

    errors = [detail for status, detail in outcomes if status == "error"]
    assert not errors, errors
    per_worker = [detail for _, detail in outcomes]
    emitters = [sum(flags[round_no] for flags in per_worker) for round_no in range(_ROUNDS)]
    wrong = {round_no: n for round_no, n in enumerate(emitters) if n != 1}
    assert not wrong, f"{len(wrong)} of {_ROUNDS} rounds did not have exactly one emitter: {wrong}"


def _hold_write_lock(index_path: str, ready, release) -> None:
    """Module-level helper process: takes the store's write lock (``BEGIN IMMEDIATE``) and holds
    it until the parent releases it, or for 30 s at the most."""
    conn = sqlite3.connect(index_path, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        ready.set()
        release.wait(timeout=30)
        conn.execute("ROLLBACK")
    finally:
        conn.close()


def _start_hook(monkeypatch, capsys, store_dir: Path, session_id: str) -> dict:
    monkeypatch.setenv("SIDEGRAPH_DIR", str(store_dir))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": session_id})))
    hooks.session_start()
    return json.loads(capsys.readouterr().out)


def test_a_duplicate_start_does_not_wait_for_another_process_holding_the_write_lock(
    tmp_path, monkeypatch, capsys
):
    """Another process holds ``BEGIN IMMEDIATE`` while the second copy of the hook starts for a
    session the ledger already holds a fresh stamp for. The duplicate is decided from a read that
    takes no lock: it returns at once and emits nothing, not after the store's 5 s busy timeout
    (and then, on the timeout, a double emit)."""
    store_dir = tmp_path / "s"
    first = _start_hook(monkeypatch, capsys, store_dir, "dup-locked")
    assert "hookSpecificOutput" in first  # the first start emits and stamps the ledger

    ctx = _context()
    ready, release = ctx.Event(), ctx.Event()
    holder = ctx.Process(
        target=_hold_write_lock, args=(str(store_dir / "index.db"), ready, release)
    )
    holder.start()
    try:
        assert ready.wait(timeout=10), "the helper process never took the write lock"
        started = time.monotonic()
        second = _start_hook(monkeypatch, capsys, store_dir, "dup-locked")
        elapsed = time.monotonic() - started
    finally:
        release.set()
        holder.join(timeout=10)
        if holder.is_alive():
            holder.terminate()
            holder.join(timeout=5)

    assert second == {}  # the duplicate exits silently
    assert elapsed < 1.0, f"the duplicate waited {elapsed:.2f}s on the write lock"
