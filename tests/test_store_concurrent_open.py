"""Cross-process first-open race tolerance (review residual on 9c1e69f): N processes
opening the SAME fresh store directory simultaneously must never crash and must always
converge on identical format-marker/.gitignore content — see
``_atomic_write_text_race_tolerant`` and ``Store._sweep_stale_tmp_files`` in store.py.

The reviewer's stress probe (20 rounds x 4 simultaneous processes doing ``Store(fresh_dir)``)
measured a 17.5% per-open failure rate (14/80) pre-fix across two distinct races that a
same-process/same-thread test can't reach (both need genuinely separate OS processes).
Reproducing this test's own probe (also via 'fork', see below) surfaced a THIRD race the
original review didn't describe, on top of those two:

- sweep TOCTOU: ``if f.is_file(): f.unlink()`` racing another process's ``os.replace``
  consuming the tmp file between the check and the unlink. Fixed with
  ``Path.unlink(missing_ok=True)`` everywhere the sweep removes a file.
- sweeper eats a live buffer: process A's open-time sweep (debris cleanup) deletes
  process B's freshly-written, not-yet-replaced tmp file; B's ``os.replace`` then fails,
  and at that moment the real winner (A, still mid-write) hasn't produced the target yet
  either -- a single-shot re-verify-and-swallow isn't enough. Fixed with a bounded retry
  of the whole write cycle in ``_atomic_write_text_race_tolerant`` (each opener sweeps
  only once, at its own open start, so this converges within a few attempts).
- shared-tmp content corruption (found via THIS test, not in the original review): the
  old fixed tmp filename (``format.tmp``) meant two concurrent writers could share the
  exact same inode. Process C's ``open(tmp, "w")`` truncates that inode in place; if
  process D's ``os.replace(tmp, marker)`` fires in the narrow window after C's truncate
  but before C's own write completes, D atomically installs C's momentarily-EMPTY tmp
  file as the "committed" marker -- silent content corruption, observed as
  ``ValueError: unrecognized store format marker ''`` on a later open (not an exception
  at write time -- the write appeared to succeed). Fixed by giving every write attempt a
  per-process, per-attempt UNIQUE tmp filename (pid + a random token), so no two writers
  ever share the same inode to truncate out from under each other.
"""

from __future__ import annotations

import multiprocessing as mp

from sidegraph.schema import SCHEMA_VERSION


def _open_and_close_store(path_str: str, barrier, result_queue) -> None:
    """Module-level (picklable under the 'spawn' start method) worker: wait for every
    sibling process to be ready, then race them all into ``Store(path_str)``."""
    try:
        barrier.wait(timeout=10)
        from sidegraph.store import Store

        store = Store(path_str)
        store.close()
        result_queue.put(("ok", None))
    except Exception as e:  # report every failure mode back to the parent, not just crash
        result_queue.put(("error", f"{type(e).__name__}: {e}"))


def test_concurrent_first_open_across_processes_never_crashes(tmp_path):
    """N simultaneous, genuinely separate processes opening a brand-new store directory:
    zero exceptions, and the format marker + .gitignore land with their exact expected
    content (both writers always produce identical bytes, so there's exactly one correct
    outcome regardless of who "wins").

    Uses the 'fork' start method where available: 'spawn' re-imports the interpreter per
    child, and the resulting startup jitter was enough to mask both races entirely in
    manual reproduction (0/256 opens) -- 'fork' reproduced them reliably (~10% of opens,
    close to the reviewer's measured 17.5%). Falls back to 'spawn' where 'fork' isn't
    available (e.g. Windows); the assertions below hold either way, just with lower
    odds of exercising the race on such a platform."""
    rounds = 12
    processes_per_round = 4
    start_method = "fork" if "fork" in mp.get_all_start_methods() else "spawn"
    ctx = mp.get_context(start_method)

    for round_no in range(rounds):
        store_dir = tmp_path / f"round-{round_no}"
        barrier = ctx.Barrier(processes_per_round)
        result_queue = ctx.Queue()

        procs = [
            ctx.Process(
                target=_open_and_close_store,
                args=(str(store_dir), barrier, result_queue),
            )
            for _ in range(processes_per_round)
        ]
        for p in procs:
            p.start()
        for p in procs:
            p.join(timeout=30)

        outcomes = [result_queue.get(timeout=5) for _ in range(processes_per_round)]
        errors = [detail for status, detail in outcomes if status == "error"]
        exit_problems = [f"pid exited {p.exitcode}" for p in procs if p.exitcode != 0]
        assert not errors, f"round {round_no}: {errors}"
        assert not exit_problems, f"round {round_no}: {exit_problems}"

        marker = store_dir / "format"
        gitignore = store_dir / ".gitignore"
        assert marker.is_file(), f"round {round_no}: format marker missing"
        assert marker.read_text() == f"sidegraph-store {SCHEMA_VERSION}\n"
        assert gitignore.is_file(), f"round {round_no}: .gitignore missing"
        assert gitignore.read_text() == "index.db*\n*.tmp\n"
