"""SessionStart tells the human, not only the model, when Sidegraph needs attention.

Everything SessionStart used to print went into one ``additionalContext`` string for the model.
The registry (``sidegraph.integrity``) now decides what is wrong; the hook prints each problem's
model-facing line as before and sends the human-facing notices as ``systemMessage``, at most
once a day per check and again at once when the severity rises. A store that cannot open is
reported instead of printing ``{}``.
see design/superpowers/specs/2026-10-02-integrity-self-check-design.md (D3, D5; T1-T10, T15, T17,
T19)
"""

from __future__ import annotations

import io
import json
import multiprocessing as mp
import queue
import sqlite3
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import sidegraph.host.hooks as hooks
from sidegraph import githooks, integrity
from sidegraph.schema import Decision, DecisionKind, DecisionStatus, Provenance
from sidegraph.store import Store
from tests.test_graph_freshness import commit, stale_repo, write_graph
from tests.test_integrity import add_record, settled

NOTICE_KEY = "integrity_notice:"


def _frozen(fixed: datetime) -> type[datetime]:
    """A ``datetime`` whose ``now()`` is ``fixed``: swapped in for the hook module's name only."""

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return fixed.astimezone(tz) if tz is not None else fixed

    return _Frozen


def start(
    monkeypatch,
    capsys,
    store_dir: Path,
    *,
    graph: Path | None = None,
    session: str = "s1",
    payload: dict | None = None,
    project: Path | None = None,
    now: datetime | None = None,
) -> dict:
    """Run the SessionStart hook over ``store_dir`` and return the JSON it printed. The graph
    defaults to a path that does not exist."""
    monkeypatch.setenv("SIDEGRAPH_DIR", str(store_dir))
    monkeypatch.setenv("SIDEGRAPH_GRAPH", str(graph or store_dir.parent / "no-graph.json"))
    if project is not None:
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    body = payload if payload is not None else {"session_id": session}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(body)))
    if now is not None:
        monkeypatch.setattr(hooks, "datetime", _frozen(now))
    hooks.session_start()
    return json.loads(capsys.readouterr().out)


def context(out: dict) -> str:
    return out["hookSpecificOutput"]["additionalContext"]


def _proposal(store: Store, age_days: float, title: str = "p") -> Decision:
    return store.add_decision(
        Decision(
            title=title,
            kind=DecisionKind.GOTCHA,
            status=DecisionStatus.PROPOSED,
            context="c",
            choice="ch",
            valid_from=datetime.now(UTC) - timedelta(days=age_days, minutes=5),
            provenance=Provenance(source="manual"),
        )
    )


def _meta(store_dir: Path, key: str) -> str | None:
    store = Store(store_dir)
    try:
        return store.get_meta(key)
    finally:
        store.close()


# -- T1, T2, T19: a store that cannot open --------------------------------------------------


def _conflicted_store(tmp_path: Path, *, unreadable: bool = False) -> Path:
    """A store whose one decision file cannot be indexed, and no index, so that the next open
    reloads from the canonical files. By default the file holds merge-conflict markers, which
    the reload skips and the store opens. With ``unreadable`` a directory stands where the file
    was: ``read_text`` raises ``IsADirectoryError``, an ``OSError`` the reload does not skip
    (and, unlike ``chmod 000``, that also fails under root), so the store cannot open.
    see design/superpowers/specs/2026-10-03-store-survives-a-bad-file-design.md (D1, §5 step 7)"""
    store_dir = tmp_path / ".sidegraph"
    store = Store(store_dir)
    rid = add_record(store)
    store.close()
    path = store_dir / "decisions" / f"{rid}.json"
    if unreadable:
        path.unlink()
        path.mkdir()
    else:
        path.write_text(
            '<<<<<<< HEAD\n{"id": "x"}\n=======\n{"id": "y"}\n>>>>>>> branch\n',
            encoding="utf-8",
        )
    (store_dir / "index.db").unlink()
    return store_dir


def test_t1_a_conflicted_record_file_is_reported_to_the_human_and_the_model(
    tmp_path, monkeypatch, capsys
):
    """Red against unfixed code, which prints ``{}``: memory switches off with no message."""
    store_dir = _conflicted_store(tmp_path, unreadable=True)

    out = start(monkeypatch, capsys, store_dir)

    assert set(out) == {"systemMessage", "hookSpecificOutput"}
    notice = out["systemMessage"]
    assert notice.startswith(f"Sidegraph cannot open its store at {store_dir}, so memory is off")
    assert "IsADirectoryError" in notice
    assert "`sidegraph-verify`" in notice
    assert context(out) == f"{notice} Sidegraph memory tools will fail until it is fixed."
    assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"


def test_t19_the_unreadable_store_path_runs_that_check_alone(tmp_path, monkeypatch, capsys):
    """No "no code graph" line, no stray-store line, no pending line: only the one check ran.
    The model sees exactly the notice and the clause, and the map is not attempted."""
    store_dir = _conflicted_store(tmp_path, unreadable=True)

    out = start(monkeypatch, capsys, store_dir)

    assert (
        context(out)
        == f"{out['systemMessage']} Sidegraph memory tools will fail until it is fixed."
    )
    assert "no code graph" not in context(out)
    assert "get_task_context" not in context(out)


def test_t13_a_conflicted_record_file_leaves_the_map_and_is_named_to_the_human(
    tmp_path, monkeypatch, capsys
):
    """Red against unfixed code: the store would not open, so the hook said "cannot open its
    store, memory is off" and printed no map.
    see design/superpowers/specs/2026-10-03-store-survives-a-bad-file-design.md (D7, T13)"""
    store_dir = tmp_path / ".sidegraph"
    store = Store(store_dir)
    broken = add_record(store)
    kept = store.get_decision(add_record(store))
    store.close()
    (store_dir / "decisions" / f"{broken}.json").write_text(
        '<<<<<<< HEAD\n{"id": "x"}\n=======\n{"id": "y"}\n>>>>>>> branch\n', encoding="utf-8"
    )
    (store_dir / "index.db").unlink()
    graph = tmp_path / "graph.json"
    write_graph(graph, None, ["pkg/m.py"])

    out = start(monkeypatch, capsys, store_dir, graph=graph)

    text = context(out)
    assert "# Sidegraph — project memory" in text
    assert "## Communities" in text
    assert kept is not None and kept.title in text
    assert "cannot open its store" not in text
    assert f"decisions/{broken}.json" in out["systemMessage"]
    assert out["systemMessage"].startswith("Sidegraph: 1 store file(s) could not be indexed")


def _busy_error(code: int) -> sqlite3.OperationalError:
    error = sqlite3.OperationalError("database is locked")
    error.sqlite_errorcode = code
    return error


@pytest.mark.parametrize("code", [sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED])
def test_t2_a_transient_lock_still_prints_nothing(tmp_path, monkeypatch, capsys, code):
    """A lock another process holds is not a broken store: the hook prints ``{}``, as before.
    Mutation M1 re-raises every ``OperationalError``, which is what the next test guards."""

    def locked(*args, **kwargs):
        raise _busy_error(code)

    monkeypatch.setattr("sidegraph.store.Store", locked)

    assert start(monkeypatch, capsys, tmp_path / ".sidegraph") == {}


def test_t2_an_extended_busy_code_is_transient_too(tmp_path, monkeypatch, capsys):
    """``sqlite_errorcode`` carries the extended result code (BUSY_SNAPSHOT is 517): the low
    byte is the primary one."""

    def locked(*args, **kwargs):
        raise _busy_error(sqlite3.SQLITE_BUSY | (2 << 8))

    monkeypatch.setattr("sidegraph.store.Store", locked)

    assert start(monkeypatch, capsys, tmp_path / ".sidegraph") == {}


def test_t2_a_read_only_index_that_needs_a_reload_gets_the_index_fix(tmp_path, monkeypatch, capsys):
    """``OperationalError`` "attempt to write a readonly database" is not a lock. Mutation M1
    (a catch-all re-raise of every ``OperationalError``) prints ``{}`` here."""
    store_dir = tmp_path / ".sidegraph"
    store = Store(store_dir)
    rid = add_record(store)
    store.close()
    record = store_dir / "decisions" / f"{rid}.json"
    record.write_text(record.read_text(encoding="utf-8") + "\n", encoding="utf-8")  # digest moves
    index = store_dir / "index.db"
    index.chmod(0o444)
    try:
        out = start(monkeypatch, capsys, store_dir)
    finally:
        index.chmod(0o644)

    assert "OperationalError" in out["systemMessage"]
    assert f"remove {store_dir}/index.db and start a new session." in out["systemMessage"]


def test_t2_a_garbage_index_gets_the_index_fix(tmp_path, monkeypatch, capsys):
    store_dir = tmp_path / ".sidegraph"
    Store(store_dir).close()
    (store_dir / "index.db").write_bytes(b"this is not a database " * 200)

    out = start(monkeypatch, capsys, store_dir)

    assert "DatabaseError: file is not a database" in out["systemMessage"]
    assert f"remove {store_dir}/index.db and start a new session." in out["systemMessage"]


def test_t2_an_incompatible_format_marker_gets_the_upgrade_fix(tmp_path, monkeypatch, capsys):
    store_dir = tmp_path / ".sidegraph"
    Store(store_dir).close()
    (store_dir / "format").write_text("sidegraph-store 99.0\n", encoding="utf-8")

    out = start(monkeypatch, capsys, store_dir)

    assert out["systemMessage"].endswith(
        "This store was written by a different Sidegraph version: upgrade Sidegraph (or use "
        "the version that wrote it)."
    )


# -- T3, T4, T6: lines and notices ----------------------------------------------------------


def test_t3_no_graph_is_a_line_for_the_model_and_no_notice(tmp_path, monkeypatch, capsys):
    store_dir = tmp_path / ".sidegraph"
    graph = tmp_path / "graphify-out" / "graph.json"

    out = start(monkeypatch, capsys, store_dir, graph=graph)

    assert "systemMessage" not in out
    assert (
        f"Sidegraph: no code graph at {graph}, so memory cannot match files to records or "
        "anchor new ones. Build it from the repository root: `graphify update .`"
    ) in context(out)


def test_t3_an_unreadable_graph_file_gets_its_own_wording(tmp_path, monkeypatch, capsys):
    graph = tmp_path / "graph.json"
    graph.write_text("{not json")

    out = start(monkeypatch, capsys, tmp_path / ".sidegraph", graph=graph)

    assert f"Sidegraph: the code graph at {graph} could not be read, so memory" in context(out)
    assert "systemMessage" not in out


def test_t4_a_stale_graph_is_a_notice_carrying_todays_text(tmp_path, monkeypatch, capsys):
    """Red against unfixed code: the stale line is for the model alone, no ``systemMessage``."""
    fx = stale_repo(tmp_path)

    out = start(monkeypatch, capsys, tmp_path / "store", graph=fx.graph, project=fx.repo)

    line = (
        f"Sidegraph: the code graph is stale (built at {fx.first[:7]}, 1 commit behind HEAD, "
        "1 file changed since), so memory cannot see or anchor to code added after the build. "
        "Rebuild it from the repository root: `graphify update .`"
    )
    assert out["systemMessage"] == line
    assert line in context(out)


def test_t4_a_borrowed_stale_graph_names_the_main_checkout_in_the_notice(
    tmp_path, monkeypatch, capsys
):
    from tests.test_config_borrowed_graph import add_worktree, make_main

    main = make_main(tmp_path)
    commit(main, "B", {"pkg/m.py": "def a():\n    return 1\n"})
    worktree = add_worktree(main)
    # This test is about the stale-graph notice. A worktree that borrows the main graph is also
    # told about a missing refresh hook, so the choice is recorded as already made.
    assert githooks.record_choice(main, declined=True)
    monkeypatch.chdir(worktree)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(worktree))
    monkeypatch.delenv("SIDEGRAPH_GRAPH", raising=False)
    monkeypatch.setenv("SIDEGRAPH_DIR", ".sidegraph")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "w1"})))

    hooks.session_start()
    out = json.loads(capsys.readouterr().out)

    assert out["systemMessage"].startswith(
        f"Sidegraph: the main checkout's code graph ({main}) is stale (built at "
    )
    assert out["systemMessage"].endswith("): rebuild it there with `graphify update .`")


def test_t6_skipped_store_files_are_a_line_and_a_notice_naming_the_first(
    tmp_path, monkeypatch, capsys
):
    """Red against unfixed code: ``Store`` warns on stderr, where a hook's user never looks."""
    store_dir = tmp_path / ".sidegraph"
    store = Store(store_dir)
    rid = add_record(store)
    store.close()
    record = json.loads((store_dir / "decisions" / f"{rid}.json").read_text(encoding="utf-8"))
    (store_dir / "decisions" / "A.json").write_text(
        json.dumps({**record, "id": "B"}), encoding="utf-8"
    )
    (store_dir / "index.db").unlink()

    out = start(monkeypatch, capsys, store_dir)

    assert "decisions/A.json" in out["systemMessage"]
    assert out["systemMessage"].startswith("Sidegraph: 1 store file(s) could not be indexed")
    assert out["systemMessage"] in context(out)


def test_t5_three_recent_orphaned_records_notify_and_old_ones_do_not(tmp_path, monkeypatch, capsys):
    recent = tmp_path / "recent" / ".sidegraph"
    store = settled(Store(recent))
    for _ in range(3):
        add_record(store, age_days=1)
    store.close()
    old = tmp_path / "old" / ".sidegraph"
    store = settled(Store(old))
    for _ in range(7):
        add_record(store, age_days=60)
    store.close()

    out = start(monkeypatch, capsys, recent, session="a")
    assert out["systemMessage"].startswith(
        "Sidegraph: 3 of 3 open record(s) have every code anchor"
    )

    out = start(monkeypatch, capsys, old, session="b")
    assert "systemMessage" not in out
    assert "Sidegraph: 7 of 7 open record(s) have every code anchor orphaned" in context(out)


# -- T7: pending ratification ---------------------------------------------------------------


PENDING_NOTICE = (
    "Sidegraph: 1 record(s) await ratification, the oldest for {age} days. Review them with "
    "`sidegraph-ratify`, or ask the agent to use the ratify tool."
)


def test_t7_a_thirty_day_old_proposal_notifies_and_a_29_day_old_one_does_not(
    tmp_path, monkeypatch, capsys
):
    """Mutation M4 compares with ``>`` instead of ``>=``."""
    for age, sub in ((30, "a"), (29, "b")):
        store = Store(tmp_path / sub / ".sidegraph")
        _proposal(store, age)
        store.close()

    out = start(monkeypatch, capsys, tmp_path / "a" / ".sidegraph", session="a")
    assert out["systemMessage"] == PENDING_NOTICE.format(age=30)
    assert "awaiting ratification" in context(out)  # the model line is unchanged

    out = start(monkeypatch, capsys, tmp_path / "b" / ".sidegraph", session="b")
    assert "systemMessage" not in out
    assert "(1 decisions, 0 facts, 0 domains; oldest 29 days)" in context(out)


def test_t7_the_ratify_switch_silences_the_line_and_the_notice(tmp_path, monkeypatch, capsys):
    store = Store(tmp_path / ".sidegraph")
    _proposal(store, 40)
    store.close()
    monkeypatch.setenv("SIDEGRAPH_RATIFY_NUDGE", "off")

    out = start(monkeypatch, capsys, tmp_path / ".sidegraph")

    assert "systemMessage" not in out
    assert "awaiting ratification" not in context(out)


# -- T8: noise control ----------------------------------------------------------------------


def _pending_store(tmp_path: Path) -> tuple[Path, Store]:
    store_dir = tmp_path / ".sidegraph"
    store = Store(store_dir)
    _proposal(store, 40)
    return store_dir, store


def test_t8_a_notice_comes_once_a_day(tmp_path, monkeypatch, capsys):
    """The second start within 24 h has the line and no notice; at 24 h the notice is back.
    Mutation M5 drops the 24 h comparison."""
    store_dir, _store = _pending_store(tmp_path)
    t0 = datetime.now(UTC)

    first = start(monkeypatch, capsys, store_dir, session="s1", now=t0)
    assert "systemMessage" in first
    stamp = _meta(store_dir, NOTICE_KEY + "pending-ratification")
    assert stamp == f"advisory|{t0.isoformat()}"

    soon = start(monkeypatch, capsys, store_dir, session="s2", now=t0 + timedelta(hours=1))
    assert "systemMessage" not in soon
    assert "awaiting ratification" in context(soon)
    assert _meta(store_dir, NOTICE_KEY + "pending-ratification") == stamp

    later = start(monkeypatch, capsys, store_dir, session="s3", now=t0 + timedelta(hours=24))
    assert "await ratification" in later["systemMessage"]


def test_t8_a_worse_severity_notifies_at_once(tmp_path, monkeypatch, capsys):
    """advisory then degraded inside the day: the notice returns. Mutation M6 removes the
    escalation rule. A stub check stands in: no real check changes severity while keeping its
    notice."""
    store_dir = tmp_path / ".sidegraph"
    severity = ["advisory"]

    def detect(inputs):
        return integrity.Problem(
            check="stub",
            severity=severity[0],
            summary="s",
            fix="f",
            line="Sidegraph: stub line",
            notice=f"Sidegraph: stub {severity[0]}",
        )

    stub = integrity.Check("stub", frozenset({"session"}), detect)
    monkeypatch.setattr(hooks, "host_checks", lambda location: (stub,))
    t0 = datetime.now(UTC)

    first = start(monkeypatch, capsys, store_dir, session="s1", now=t0)
    assert first["systemMessage"] == "Sidegraph: stub advisory"

    same = start(monkeypatch, capsys, store_dir, session="s2", now=t0 + timedelta(hours=1))
    assert "systemMessage" not in same

    severity[0] = "degraded"
    worse = start(monkeypatch, capsys, store_dir, session="s3", now=t0 + timedelta(hours=2))
    assert worse["systemMessage"] == "Sidegraph: stub degraded"

    again = start(monkeypatch, capsys, store_dir, session="s4", now=t0 + timedelta(hours=3))
    assert "systemMessage" not in again


def test_t8_a_clean_check_clears_its_key_and_a_recurrence_is_reported_at_once(
    tmp_path, monkeypatch, capsys
):
    """Mutation M7 never clears: the recurrence then waits out the 24 h of the first notice."""
    store_dir, store = _pending_store(tmp_path)
    t0 = datetime.now(UTC)
    start(monkeypatch, capsys, store_dir, session="s1", now=t0)
    assert _meta(store_dir, NOTICE_KEY + "pending-ratification") is not None

    (proposed,) = list(store.iter_proposed())
    store.ratify(proposed.id)
    start(monkeypatch, capsys, store_dir, session="s2", now=t0 + timedelta(hours=1))
    assert _meta(store_dir, NOTICE_KEY + "pending-ratification") is None

    _proposal(store, 40, title="another")
    back = start(monkeypatch, capsys, store_dir, session="s3", now=t0 + timedelta(hours=2))
    assert "await ratification" in back["systemMessage"]


def test_t8_notices_come_sorted_by_severity_highest_first(tmp_path, monkeypatch, capsys):
    store_dir, _store = _pending_store(tmp_path)  # advisory notice, registry position 1
    graph = tmp_path / "graph.json"
    graph.write_text("{not json")
    record = json.loads(next((store_dir / "decisions").glob("*.json")).read_text(encoding="utf-8"))
    (store_dir / "decisions" / "A.json").write_text(
        json.dumps({**record, "id": "B"}), encoding="utf-8"
    )  # a skipped file: degraded, registry position 8
    (store_dir / "index.db").unlink()

    out = start(monkeypatch, capsys, store_dir, graph=graph)

    first, second = out["systemMessage"].split("\n")
    assert first.startswith("Sidegraph: 1 store file(s) could not be indexed")
    assert second.startswith("Sidegraph: 1 record(s) await ratification")


def test_a_failing_claim_still_emits_the_notice(tmp_path, monkeypatch, capsys):
    """A duplicate is better than silence."""
    store_dir, _store = _pending_store(tmp_path)

    def broken(self, key, decide):
        if key.startswith(NOTICE_KEY):
            raise sqlite3.OperationalError("boom")
        return real(self, key, decide)

    real = Store.update_meta_if
    monkeypatch.setattr(Store, "update_meta_if", broken)

    out = start(monkeypatch, capsys, store_dir)

    assert "await ratification" in out["systemMessage"]


def test_a_failing_clear_costs_nothing(tmp_path, monkeypatch, capsys):
    store_dir = tmp_path / ".sidegraph"
    Store(store_dir).close()

    def broken(self, key):
        raise sqlite3.OperationalError("boom")

    monkeypatch.setattr(Store, "delete_meta", broken)

    out = start(monkeypatch, capsys, store_dir)

    assert "get_task_context" in context(out)


def test_a_duplicate_start_runs_no_check(tmp_path, monkeypatch, capsys):
    """The dedupe still returns first: a repeated session id within 60 s prints ``{}``."""
    store_dir, _store = _pending_store(tmp_path)
    first = start(monkeypatch, capsys, store_dir, session="same")
    assert "systemMessage" in first

    second = start(monkeypatch, capsys, store_dir, session="same")

    assert second == {}


# -- T9: two registrations of the hook race for one notice ----------------------------------

_ROUNDS = 100
_WORKERS = 2
_BARRIER_TIMEOUT = 10
_RESULT_TIMEOUT = 60


def _context():
    start_method = "fork" if "fork" in mp.get_all_start_methods() else "spawn"
    return mp.get_context(start_method)


def _claim_worker(store_dir: str, barrier, result_queue) -> None:
    """Module-level (picklable under 'spawn'): a long-lived worker with its own ``Store``. Every
    round it waits for its sibling, then claims the round's notice, one check id per round."""
    try:
        store = Store(store_dir)
        now = datetime.now(UTC)
        claimed: list[bool] = []
        for round_no in range(_ROUNDS):
            problem = integrity.Problem(
                check=f"race-{round_no}",
                severity="degraded",
                summary="s",
                fix="f",
                line="Sidegraph: l",
                notice="Sidegraph: n",
            )
            barrier.wait(timeout=_BARRIER_TIMEOUT)
            claimed.append(hooks._claim_notice(store, problem, now))
        store.close()
        result_queue.put(("ok", claimed))
    except BaseException as exc:  # report every failure mode, and free the sibling
        barrier.abort()
        result_queue.put(("error", f"{type(exc).__name__}: {exc}"))


def test_t9_two_starts_with_a_due_notice_emit_it_exactly_once(tmp_path):
    """Two processes claim the same due notice at the same instant, a hundred rounds running
    (the hook registered twice fires both copies, with different session ids, so the session
    dedupe does not apply): in every round exactly one of them emits. Mutation M8 reads and
    writes the stamp in two transactions, which lets both through."""
    store_dir = tmp_path / "s"
    Store(store_dir).close()
    ctx = _context()
    barrier = ctx.Barrier(_WORKERS)
    result_queue = ctx.Queue()
    procs = [
        ctx.Process(target=_claim_worker, args=(str(store_dir), barrier, result_queue))
        for _ in range(_WORKERS)
    ]
    outcomes: list[tuple[str, object]] = []
    try:
        for p in procs:
            p.start()
        for _ in procs:
            try:
                outcomes.append(result_queue.get(timeout=_RESULT_TIMEOUT))
            except queue.Empty:
                outcomes.append(("error", f"no result within {_RESULT_TIMEOUT}s (hung worker)"))
    finally:
        for p in procs:
            p.join(timeout=10)
            if p.is_alive():
                p.terminate()
                p.join(timeout=5)

    errors = [detail for status, detail in outcomes if status == "error"]
    assert not errors, errors
    per_worker = [detail for _, detail in outcomes]
    emitters = [sum(flags[n] for flags in per_worker) for n in range(_ROUNDS)]
    wrong = {n: count for n, count in enumerate(emitters) if count != 1}
    assert not wrong, f"{len(wrong)} of {_ROUNDS} rounds did not have exactly one emitter: {wrong}"


# -- T10: a failing detector costs nothing else ---------------------------------------------


def test_t10_a_detector_that_raises_leaves_the_map_and_the_other_lines(
    tmp_path, monkeypatch, capsys
):
    """Mutation M9 drops the per-detector ``try`` in ``run``."""
    store_dir, _store = _pending_store(tmp_path)

    def boom(inputs):
        raise RuntimeError("boom")

    broken = tuple(
        replace(c, detect=boom) if c.id == "graph-missing" else c for c in integrity.CHECKS
    )
    monkeypatch.setattr(integrity, "CHECKS", broken)

    out = start(monkeypatch, capsys, store_dir)

    assert "get_task_context" in context(out)
    assert "awaiting ratification" in context(out)
    assert "no code graph" not in context(out)
    assert "await ratification" in out["systemMessage"]


# -- T15: the output shape Codex accepts ----------------------------------------------------


def test_t15_a_codex_shaped_payload_with_a_due_notice_gets_only_known_top_level_keys(
    tmp_path, monkeypatch, capsys
):
    """Codex 0.159.3 rejects unknown top-level keys of a hook's output
    (``hooks/src/schema.rs``, ``deny_unknown_fields``). Mutation M11 adds one."""
    store_dir, _store = _pending_store(tmp_path)
    payload = {
        "session_id": "umbrella",
        "transcript_path": str(tmp_path / "rollout-2026-10-02T10-00-00-abc.jsonl"),
        "cwd": str(tmp_path),
        "hook_event_name": "SessionStart",
        "model": "gpt-5",
        "source": "startup",
    }

    out = start(monkeypatch, capsys, store_dir, payload=payload)

    assert "systemMessage" in out
    assert set(out) <= {"systemMessage", "hookSpecificOutput"}
    assert set(out["hookSpecificOutput"]) == {"hookEventName", "additionalContext"}


def test_no_notice_leaves_the_output_shape_it_always_had(tmp_path, monkeypatch, capsys):
    store_dir = tmp_path / ".sidegraph"
    Store(store_dir).close()

    out = start(monkeypatch, capsys, store_dir)

    assert set(out) == {"hookSpecificOutput"}


# -- T17: drift counts the refresh's return, never the old cache ----------------------------


def test_t17_a_refresh_that_could_not_run_prints_no_drift_line_whatever_the_cache_holds(
    tmp_path, monkeypatch, capsys
):
    """Outside git the refresh returns without touching the cache. A detector that counted
    ``drifted_record_ids`` would print a line from the old cache; today's hook prints none."""
    from sidegraph.retrieval import DRIFT_CACHE_KEY

    store_dir = tmp_path / ".sidegraph"
    store = Store(store_dir)
    ids = [add_record(store, leaves=("live",)) for _ in range(3)]
    store.set_meta(DRIFT_CACHE_KEY, json.dumps({"head": None, "by_commit": {"deadbeef": ids}}))
    store.close()

    out = start(monkeypatch, capsys, store_dir)

    assert "anchored to code that changed" not in context(out)
    assert _meta(store_dir, DRIFT_CACHE_KEY) is not None  # the cache itself was left alone


def test_the_drift_refresh_still_runs_when_the_nudge_is_off(tmp_path, monkeypatch, capsys):
    """``SIDEGRAPH_DRIFT_NUDGE=off`` gates the line, never the refresh that keeps the
    ``[drifted]`` markers' cache fresh."""
    import sidegraph.sync as sync_mod

    calls: list[object] = []

    def fake(store, **kwargs):
        calls.append(store)
        return 5

    monkeypatch.setattr(sync_mod, "refresh_code_drift_cache", fake)
    monkeypatch.setenv("SIDEGRAPH_DRIFT_NUDGE", "off")

    out = start(monkeypatch, capsys, tmp_path / ".sidegraph")

    assert len(calls) == 1
    assert "anchored to code that changed" not in context(out)


def test_a_failing_refresh_leaves_the_other_lines(tmp_path, monkeypatch, capsys):
    import sidegraph.sync as sync_mod

    def boom(store, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(sync_mod, "refresh_code_drift_cache", boom)
    store_dir, _store = _pending_store(tmp_path)

    out = start(monkeypatch, capsys, store_dir)

    assert "awaiting ratification" in context(out)


# -- T18: not run keeps the key -------------------------------------------------------------


def test_t18_an_unknown_freshness_keeps_a_recorded_stale_notice(tmp_path, monkeypatch, capsys):
    """Git failed this time: that is not evidence the graph is current. Mutation M12 treats a
    check that did not run as clean and deletes the key, so the next stale start repeats it."""
    fx = stale_repo(tmp_path)
    store_dir = tmp_path / "store"
    t0 = datetime.now(UTC)
    start(monkeypatch, capsys, store_dir, graph=fx.graph, project=fx.repo, session="s1", now=t0)
    key = NOTICE_KEY + "graph-stale"
    stamp = _meta(store_dir, key)
    assert stamp is not None

    write_graph(fx.graph, "abc123", ["pkg/m.py"])  # not a full commit id: freshness unknown
    start(
        monkeypatch,
        capsys,
        store_dir,
        graph=fx.graph,
        project=fx.repo,
        session="s2",
        now=t0 + timedelta(hours=1),
    )

    assert _meta(store_dir, key) == stamp


def test_t18_a_reloaded_index_keeps_a_recorded_orphaned_notice(tmp_path, monkeypatch, capsys):
    """A reload resets every binding to ``live`` until a sync recomputes them: the check is not
    run there, and the notice key survives the reload that every ``git pull`` can cause."""
    store_dir = tmp_path / ".sidegraph"
    store = settled(Store(store_dir))
    ids = [add_record(store, age_days=1) for _ in range(3)]
    store.close()
    t0 = datetime.now(UTC)
    first = start(monkeypatch, capsys, store_dir, session="s1", now=t0)
    assert "every code anchor orphaned" in first["systemMessage"]
    key = NOTICE_KEY + "orphaned-records"
    stamp = _meta(store_dir, key)
    assert stamp is not None

    # What a `git pull` does: a canonical file changes, so the next open reloads the index and
    # `volatile_stale` is "1". (Deleting index.db would delete the meta table with the key.)
    record = store_dir / "decisions" / f"{ids[0]}.json"
    record.write_text(record.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    start(monkeypatch, capsys, store_dir, session="s2", now=t0 + timedelta(hours=1))

    assert _meta(store_dir, key) == stamp


# -- the fix sentence names only what exists ------------------------------------------------

GENERIC_FIX = "Run `sidegraph-verify` from the repository root for details."


def test_a_store_path_that_is_a_plain_file_gets_the_generic_fix(tmp_path, monkeypatch, capsys):
    """The error is a ``DatabaseError`` and ``<store>/index.db`` cannot exist under a file: the
    advice to remove it would be impossible."""
    store_path = tmp_path / ".sidegraph"
    store_path.write_text("this is not a store, it is a file\n", encoding="utf-8")

    out = start(monkeypatch, capsys, store_path)

    assert out["systemMessage"].endswith(GENERIC_FIX)
    assert "index.db" not in out["systemMessage"]


def test_a_read_only_store_directory_with_no_index_gets_the_generic_fix(
    tmp_path, monkeypatch, capsys
):
    """``unable to open database file`` with no ``index.db`` to remove."""
    store_dir = tmp_path / ".sidegraph"
    Store(store_dir).close()
    (store_dir / "index.db").unlink()
    store_dir.chmod(0o555)
    try:
        out = start(monkeypatch, capsys, store_dir)
    finally:
        store_dir.chmod(0o755)

    assert "systemMessage" in out, out
    assert out["systemMessage"].endswith(GENERIC_FIX)
    assert "index.db" not in out["systemMessage"]


# -- notice housekeeping does not queue behind another process's lock -----------------------


class _WriteSpy:
    """Counts the store writes the notice machinery makes, keyed by what they touch."""

    def __init__(self, monkeypatch, *, claim_error: BaseException | None = None):
        self.claims: list[str] = []
        self.deletes: list[str] = []
        real_update, real_delete = Store.update_meta_if, Store.delete_meta

        def update(store, key, decide):
            if key.startswith(NOTICE_KEY):
                self.claims.append(key)
                if claim_error is not None:
                    raise claim_error
            return real_update(store, key, decide)

        def delete(store, key):
            self.deletes.append(key)
            return real_delete(store, key)

        monkeypatch.setattr(Store, "update_meta_if", update)
        monkeypatch.setattr(Store, "delete_meta", delete)


def test_a_healthy_start_makes_no_notice_writes(tmp_path, monkeypatch, capsys):
    """Nothing to claim and no key to clear: no ``BEGIN``, so no lock to queue for. Before, every
    clean check deleted its (absent) key, nine write transactions per start."""
    store_dir = tmp_path / ".sidegraph"
    Store(store_dir).close()
    spy = _WriteSpy(monkeypatch)

    out = start(monkeypatch, capsys, store_dir, payload={})  # no session id: no dedupe write

    assert "get_task_context" in context(out)
    assert spy.deletes == []
    assert spy.claims == []


def test_only_a_clean_check_that_has_a_key_deletes_it(tmp_path, monkeypatch, capsys):
    store_dir = tmp_path / ".sidegraph"
    store = Store(store_dir)
    store.set_meta(NOTICE_KEY + "stray-store", f"degraded|{datetime.now(UTC).isoformat()}")
    store.close()
    spy = _WriteSpy(monkeypatch)

    start(monkeypatch, capsys, store_dir, payload={})

    assert spy.deletes == [NOTICE_KEY + "stray-store"]
    assert _meta(store_dir, NOTICE_KEY + "stray-store") is None


def _two_due_notices(tmp_path: Path) -> Path:
    """A store with a 40-day-old proposal and a skipped file: two notices are due. A recorded
    key for a check that runs clean gives the clearing loop something to delete."""
    store_dir = tmp_path / ".sidegraph"
    store = Store(store_dir)
    _proposal(store, 40)
    rid = add_record(store)
    store.close()
    record = json.loads((store_dir / "decisions" / f"{rid}.json").read_text(encoding="utf-8"))
    (store_dir / "decisions" / "A.json").write_text(
        json.dumps({**record, "id": "B"}), encoding="utf-8"
    )
    (store_dir / "index.db").unlink()
    store = Store(store_dir)  # the reload that finds the file to skip
    store.set_meta(NOTICE_KEY + "stray-store", f"degraded|{datetime.now(UTC).isoformat()}")
    store.close()
    return store_dir


def test_a_lock_on_the_first_claim_stops_every_write_and_every_notice_is_still_sent(
    tmp_path, monkeypatch, capsys
):
    """After the first lock error, no further claim and no delete: each would wait out the busy
    timeout. The remaining due notices go out unclaimed (a duplicate beats silence)."""
    store_dir = _two_due_notices(tmp_path)
    spy = _WriteSpy(monkeypatch, claim_error=_busy_error(sqlite3.SQLITE_BUSY))

    out = start(monkeypatch, capsys, store_dir, payload={})

    assert len(spy.claims) == 1
    assert spy.deletes == []
    assert len(out["systemMessage"].split("\n")) == 2


def test_a_failure_that_is_not_a_lock_does_not_stop_the_other_claims(tmp_path, monkeypatch, capsys):
    """Only a lock makes later writes pointless; any other failure is that one claim's."""
    store_dir = _two_due_notices(tmp_path)
    spy = _WriteSpy(monkeypatch, claim_error=sqlite3.OperationalError("boom"))

    out = start(monkeypatch, capsys, store_dir, payload={})

    assert len(spy.claims) == 2
    assert len(out["systemMessage"].split("\n")) == 2


def _held_lock(store_dir: Path):
    """A process holding the index's write lock until released: (process, release event)."""
    from tests.test_host_session_start_concurrent import _hold_write_lock

    ctx = _context()
    ready, release = ctx.Event(), ctx.Event()
    holder = ctx.Process(
        target=_hold_write_lock, args=(str(store_dir / "index.db"), ready, release)
    )
    holder.start()
    assert ready.wait(timeout=10), "the helper process never took the write lock"
    return holder, release


def _release(holder, release) -> None:
    release.set()
    holder.join(timeout=10)
    if holder.is_alive():
        holder.terminate()
        holder.join(timeout=5)


def test_a_held_write_lock_costs_the_notice_machinery_no_wait_when_there_is_nothing_to_write(
    tmp_path,
):
    """Another process holds ``BEGIN IMMEDIATE`` and no notice key exists: ``_due_notices`` makes
    no write, so it returns at once. Before, each of the nine clean checks waited out the busy
    timeout (shortened here to 0.3 s: nine waits are 2.7 s, none is 0)."""
    store_dir = tmp_path / ".sidegraph"
    store = Store(store_dir)
    store._conn.execute("PRAGMA busy_timeout = 300")
    result = integrity.RunResult([], frozenset(c.id for c in integrity.CHECKS))
    holder, release = _held_lock(store_dir)
    try:
        started = time.monotonic()
        notices = hooks._due_notices(store, result, datetime.now(UTC))
        elapsed = time.monotonic() - started
    finally:
        _release(holder, release)
        store.close()

    assert notices == []
    assert elapsed < 0.2, f"waited {elapsed:.2f}s on a lock with nothing to write"


def test_a_held_write_lock_costs_one_wait_for_two_due_notices_and_both_are_sent(tmp_path):
    """The first claim waits out the (shortened) busy timeout and finds the lock held; that is
    the signal to stop, so the second claim and the clearing never wait. Real SQLite, so this
    also pins that a real lock error is read as one."""
    store_dir = tmp_path / ".sidegraph"
    store = Store(store_dir)
    store._conn.execute("PRAGMA busy_timeout = 300")
    store.set_meta(NOTICE_KEY + "stray-store", f"degraded|{datetime.now(UTC).isoformat()}")
    problems = [
        integrity.Problem(
            check=f"c{n}",
            severity="degraded",
            summary="s",
            fix="f",
            line="l",
            notice=f"Sidegraph: {n}",
        )
        for n in (1, 2)
    ]
    result = integrity.RunResult(problems, frozenset({"stray-store"}))
    holder, release = _held_lock(store_dir)
    try:
        started = time.monotonic()
        notices = hooks._due_notices(store, result, datetime.now(UTC))
        elapsed = time.monotonic() - started
    finally:
        _release(holder, release)
        store.close()

    assert notices == ["Sidegraph: 1", "Sidegraph: 2"]
    assert elapsed < 0.55, f"{elapsed:.2f}s: more than one wait for the lock"


# -- a stamp from the future ----------------------------------------------------------------


def test_a_stamp_more_than_a_day_in_the_future_notifies_once_and_is_reset_to_now(
    tmp_path, monkeypatch, capsys
):
    """``abs()`` in the 24 h rule: a recorded stamp that is ahead of the clock (a wrong clock, a
    hand edit) does not silence a notice for as long as it is ahead. Mutation M5x drops it."""
    store_dir, store = _pending_store(tmp_path)
    t0 = datetime.now(UTC)
    store.set_meta(
        NOTICE_KEY + "pending-ratification", f"advisory|{(t0 + timedelta(hours=25)).isoformat()}"
    )

    first = start(monkeypatch, capsys, store_dir, session="s1", now=t0)
    assert "await ratification" in first["systemMessage"]
    assert _meta(store_dir, NOTICE_KEY + "pending-ratification") == f"advisory|{t0.isoformat()}"

    again = start(monkeypatch, capsys, store_dir, session="s2", now=t0 + timedelta(hours=1))
    assert "systemMessage" not in again


def test_a_stamp_less_than_a_day_in_the_future_is_still_a_recent_notice(
    tmp_path, monkeypatch, capsys
):
    """Skew between two copies of the hook is milliseconds: a stamp slightly ahead is recent."""
    store_dir, store = _pending_store(tmp_path)
    t0 = datetime.now(UTC)
    store.set_meta(
        NOTICE_KEY + "pending-ratification", f"advisory|{(t0 + timedelta(seconds=2)).isoformat()}"
    )

    out = start(monkeypatch, capsys, store_dir, session="s1", now=t0)

    assert "systemMessage" not in out


def test_a_lock_on_the_first_delete_stops_the_remaining_deletes(tmp_path, monkeypatch, capsys):
    """Two clean checks have a recorded key; the first delete finds the lock held, so the second
    is never attempted (it would wait out the same timeout)."""
    store_dir = tmp_path / ".sidegraph"
    store = Store(store_dir)
    stamp = f"degraded|{datetime.now(UTC).isoformat()}"
    store.set_meta(NOTICE_KEY + "stray-store", stamp)
    store.set_meta(NOTICE_KEY + "graph-borrowed", stamp)
    store.close()
    attempts: list[str] = []

    def locked(self, key):
        attempts.append(key)
        raise _busy_error(sqlite3.SQLITE_BUSY)

    monkeypatch.setattr(Store, "delete_meta", locked)

    out = start(monkeypatch, capsys, store_dir, payload={})

    assert len(attempts) == 1
    assert "get_task_context" in context(out)
