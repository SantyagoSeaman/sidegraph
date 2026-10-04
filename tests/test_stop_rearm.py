"""The capture nudge comes back after more work (T2-T10).

The Stop nudge used to be one-shot per session. A session that had already been nudged is now
nudged again when at least 30 minutes have passed since the last nudge AND at least 10 commits
authored since then are reachable from a local branch or ``HEAD``. Every scenario here is a
real git repository in ``tmp_path`` with explicit author and committer dates; "40 minutes
later" is a ``capture_rearm`` value backdated by 40 minutes, because nothing sleeps.

see design/superpowers/specs/2026-10-03-capture-rearm-design.md (D2-D5; T2-T11, M2-M6)
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
import subprocess
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import NamedTuple

import pytest

import sidegraph.host.hooks as hooks
import sidegraph.sync as sync_mod
from sidegraph.store import Store
from tests.test_host_stop import _run, _substantial

SESSION = "s1"
KEY = f"capture_rearm:{SESSION}"
SENTENCE = "Since the last capture prompt: {n} commits."

_IDENTITY = {
    "GIT_AUTHOR_NAME": "T",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "T",
    "GIT_COMMITTER_EMAIL": "t@example.com",
}


# -- helpers --------------------------------------------------------------------------------


class World(NamedTuple):
    repo: Path
    db: Path
    transcript: str


def _stamp(minutes_ago: float) -> str:
    """Git's raw date format: epoch seconds and a zone."""
    return f"{int((datetime.now(UTC) - timedelta(minutes=minutes_ago)).timestamp())} +0000"


def _git(repo: Path, *args: str, author: float = 0, committer: float | None = None) -> str:
    """One git call; both dates are explicit (``author`` and ``committer`` minutes ago)."""
    env = {
        **os.environ,
        **_IDENTITY,
        "GIT_AUTHOR_DATE": _stamp(author),
        "GIT_COMMITTER_DATE": _stamp(author if committer is None else committer),
    }
    done = subprocess.run(["git", *args], cwd=repo, env=env, capture_output=True, text=True)
    assert done.returncode == 0, f"git {args} failed: {done.stderr}"
    return done.stdout


def _commit(repo: Path, author: float, committer: float | None = None) -> None:
    _git(repo, "commit", "--allow-empty", "-q", "-m", "work", author=author, committer=committer)


def _commits(repo: Path, n: int, first: float, last: float) -> None:
    """``n`` commits authored evenly between ``first`` and ``last`` minutes ago."""
    step = (first - last) / max(n - 1, 1)
    for i in range(n):
        _commit(repo, first - i * step)


def _world(tmp_path: Path) -> World:
    """A project with one old commit, a store at ``.sidegraph`` and a substantial transcript."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _commit(repo, author=180)
    return World(repo, repo / ".sidegraph", _substantial(tmp_path, name=f"{SESSION}.jsonl"))


def _stop(monkeypatch, capsys, world: World) -> dict:
    payload = {
        "session_id": SESSION,
        "stop_hook_active": False,
        "transcript_path": world.transcript,
    }
    return _run(monkeypatch, capsys, payload, world.db)


def _backdate(world: World, minutes_ago: float) -> None:
    """Pretend the last nudge was ``minutes_ago`` minutes ago."""
    store = Store(world.db)
    store.set_meta(KEY, (datetime.now(UTC) - timedelta(minutes=minutes_ago)).isoformat())
    store.close()


def _nudged(monkeypatch, capsys, world: World, minutes_ago: float) -> None:
    """The first nudge, through the hook, then moved back to ``minutes_ago``."""
    out = _stop(monkeypatch, capsys, world)
    assert out["decision"] == "block"
    _backdate(world, minutes_ago)


def _stored(world: World) -> str | None:
    store = Store(world.db)
    try:
        return store.get_meta(KEY)
    finally:
        store.close()


# -- T2: the threshold ----------------------------------------------------------------------


def test_the_first_nudge_stamps_the_rearm_key(tmp_path, monkeypatch, capsys):
    """Red against a first nudge that writes only the ``capture_sessions`` row: the fallback to
    ``captured_at`` would hide it from every other test."""
    world = _world(tmp_path)
    before = datetime.now(UTC)
    assert _stop(monkeypatch, capsys, world)["decision"] == "block"
    stamped = _stored(world)
    assert stamped is not None
    assert before - timedelta(seconds=1) <= datetime.fromisoformat(stamped) <= datetime.now(UTC)


def test_nine_new_commits_do_not_rearm_and_ten_do(tmp_path, monkeypatch, capsys):
    world = _world(tmp_path)
    _nudged(monkeypatch, capsys, world, minutes_ago=40)
    _commits(world.repo, 9, first=35, last=5)
    assert _stop(monkeypatch, capsys, world) == {}
    _commit(world.repo, author=2)
    out = _stop(monkeypatch, capsys, world)
    assert out["decision"] == "block"
    assert out["reason"].startswith(hooks.CAPTURE_NUDGE)
    assert out["reason"].endswith(SENTENCE.format(n=10))
    assert out["suppressOutput"] is True


def test_a_rearmed_nudge_restarts_the_gap(tmp_path, monkeypatch, capsys):
    world = _world(tmp_path)
    _nudged(monkeypatch, capsys, world, minutes_ago=40)
    _commits(world.repo, 12, first=35, last=5)
    assert _stop(monkeypatch, capsys, world)["decision"] == "block"
    stamped = datetime.fromisoformat(_stored(world))
    assert datetime.now(UTC) - stamped < timedelta(minutes=1)
    assert _stop(monkeypatch, capsys, world) == {}


def test_the_first_nudge_has_no_commit_sentence(tmp_path, monkeypatch, capsys):
    world = _world(tmp_path)
    _commits(world.repo, 12, first=35, last=5)
    out = _stop(monkeypatch, capsys, world)
    assert out["reason"] == hooks.CAPTURE_NUDGE


# -- T3: the gap ----------------------------------------------------------------------------


def test_the_gap_holds_back_a_rearm_that_the_commits_would_allow(tmp_path, monkeypatch, capsys):
    """The same 15 commits: quiet 20 minutes after the nudge, a nudge 40 minutes after it.
    Red against a one-shot hook (second half) and against the mutation that drops the gap
    (first half)."""
    world = _world(tmp_path)
    _nudged(monkeypatch, capsys, world, minutes_ago=20)
    _commits(world.repo, 15, first=19, last=3)
    assert _stop(monkeypatch, capsys, world) == {}
    _backdate(world, 40)
    out = _stop(monkeypatch, capsys, world)
    assert out["decision"] == "block"
    assert out["reason"].endswith(SENTENCE.format(n=15))


def test_a_held_back_stop_leaves_the_stamp_alone(tmp_path, monkeypatch, capsys):
    world = _world(tmp_path)
    _nudged(monkeypatch, capsys, world, minutes_ago=20)
    before = _stored(world)
    _commits(world.repo, 15, first=19, last=3)
    assert _stop(monkeypatch, capsys, world) == {}
    assert _stored(world) == before


# -- T4: branches ---------------------------------------------------------------------------


def test_commits_on_a_branch_that_is_not_checked_out_count(tmp_path, monkeypatch, capsys):
    """Red against counting ``HEAD`` only (M3): the work is on ``topic``, ``main`` is checked
    out and has none of it."""
    world = _world(tmp_path)
    _nudged(monkeypatch, capsys, world, minutes_ago=40)
    _git(world.repo, "checkout", "-q", "-b", "topic")
    _commits(world.repo, 10, first=35, last=5)
    _git(world.repo, "checkout", "-q", "main")
    out = _stop(monkeypatch, capsys, world)
    assert out["decision"] == "block"
    assert out["reason"].endswith(SENTENCE.format(n=10))


# -- T5: parallel Stops ---------------------------------------------------------------------


def test_two_parallel_stops_past_the_threshold_nudge_exactly_once(tmp_path, monkeypatch):
    """Both Stops read the old stamp, count their commits and meet at a barrier before either
    writes, as two hook processes can. Red against a write without compare-and-swap (M4):
    both would nudge."""
    world = _world(tmp_path)
    store = Store(world.db)
    store.mark_captured(SESSION)
    store.set_meta(KEY, (datetime.now(UTC) - timedelta(minutes=40)).isoformat())
    store.close()
    _commits(world.repo, 12, first=35, last=5)

    payload = {
        "session_id": SESSION,
        "stop_hook_active": False,
        "transcript_path": world.transcript,
    }
    monkeypatch.setenv("SIDEGRAPH_DIR", str(world.db))
    monkeypatch.setattr(hooks, "_read_payload", lambda: payload)
    printed: list[str] = []
    guard = threading.Lock()

    def record(text: str) -> None:
        with guard:
            printed.append(text)

    monkeypatch.setattr(hooks, "print", record, raising=False)
    barrier = threading.Barrier(2)
    broken: list[bool] = []
    real_count = hooks._commits_since

    def meet_then_count(*args, **kwargs):
        try:
            barrier.wait(timeout=20)
        except threading.BrokenBarrierError:
            broken.append(True)
            raise
        return real_count(*args, **kwargs)

    monkeypatch.setattr(hooks, "_commits_since", meet_then_count)
    threads = [threading.Thread(target=hooks.stop) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert broken == []
    outs = [json.loads(text) for text in printed]
    assert len(outs) == 2
    assert sorted("decision" in out for out in outs) == [False, True], outs


# -- T6: author dates -----------------------------------------------------------------------


def test_a_rebase_of_old_commits_does_not_rearm(tmp_path, monkeypatch, capsys):
    """Red against counting by committer date (M5): the rebase gives ten old commits new
    committer dates and leaves their author dates alone."""
    world = _world(tmp_path)
    _git(world.repo, "checkout", "-q", "-b", "old")
    _commits(world.repo, 10, first=170, last=160)
    _git(world.repo, "checkout", "-q", "main")
    _commit(world.repo, author=150)
    _nudged(monkeypatch, capsys, world, minutes_ago=40)
    _git(world.repo, "checkout", "-q", "old")
    _git(world.repo, "rebase", "-q", "main", author=170, committer=10)
    log = _git(world.repo, "log", "-10", "--format=%at %ct").split("\n")[:-1]
    now = datetime.now(UTC).timestamp()
    assert len(log) == 10
    for line in log:
        authored, committed = (int(part) for part in line.split())
        assert now - authored > 150 * 60, "the producer: the author dates stay old"
        assert now - committed < 20 * 60, "the producer: the committer dates are new"
    assert _stop(monkeypatch, capsys, world) == {}


def test_commits_authored_after_the_nudge_count_whatever_their_committer_date(
    tmp_path, monkeypatch, capsys
):
    """T11. Red against ``--since`` set to the nudge itself (M6): git bounds the walk by committer
    dates and stops at the first older one, so ten commits authored after the nudge whose
    committer date is two hours before it (clock skew, an explicit ``GIT_COMMITTER_DATE``) were
    never reached. The walk bound has a margin; the count stays by author date."""
    world = _world(tmp_path)
    _nudged(monkeypatch, capsys, world, minutes_ago=40)
    for author in range(35, 5, -3):  # ten commits authored 35 .. 8 minutes ago
        _commit(world.repo, author=author, committer=40 + 120)
    log = _git(world.repo, "log", "-10", "--format=%at %ct").split("\n")[:-1]
    now = datetime.now(UTC).timestamp()
    assert len(log) == 10
    for line in log:
        authored, committed = (int(part) for part in line.split())
        assert now - authored < 40 * 60, "the producer: authored after the nudge"
        assert now - committed > 150 * 60, "the producer: committed two hours before it"
    out = _stop(monkeypatch, capsys, world)
    assert out["decision"] == "block"
    assert out["reason"].endswith(SENTENCE.format(n=10))


# -- T7: pruning ----------------------------------------------------------------------------


def test_session_start_prunes_stale_rearm_keys(tmp_path, monkeypatch, capsys):
    """Red against a prune list without ``capture_rearm:``. ``_`` is a ``LIKE`` wildcard, so a
    key of the same shape under another prefix must survive."""
    db = tmp_path / "db"
    iso = lambda days: (datetime.now(UTC) - timedelta(days=days)).isoformat()  # noqa: E731
    store = Store(db)
    for key, value in {
        "capture_rearm:old": iso(31),
        "capture_rearm:fresh": iso(1),
        "capture_rearm:edge": iso(29),
        "capture_rearmX:other": iso(90),
    }.items():
        store.set_meta(key, value)
    store.close()

    monkeypatch.setenv("SIDEGRAPH_DIR", str(db))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "next"})))
    hooks.session_start()
    capsys.readouterr()

    store = Store(db)
    kept = {
        key
        for key in ("capture_rearm:old", "capture_rearm:fresh", "capture_rearm:edge")
        if store.get_meta(key) is not None
    }
    other = store.get_meta("capture_rearmX:other")
    store.close()
    assert kept == {"capture_rearm:fresh", "capture_rearm:edge"}
    assert other is not None


# -- T8: the switch -------------------------------------------------------------------------


def test_the_off_switch_silences_the_rearmed_nudge_too(tmp_path, monkeypatch, capsys):
    world = _world(tmp_path)
    _nudged(monkeypatch, capsys, world, minutes_ago=40)
    _commits(world.repo, 12, first=35, last=5)
    before = _stored(world)
    monkeypatch.setenv("SIDEGRAPH_CAPTURE_NUDGE", "off")
    assert _stop(monkeypatch, capsys, world) == {}
    assert _stored(world) == before
    monkeypatch.delenv("SIDEGRAPH_CAPTURE_NUDGE")
    assert _stop(monkeypatch, capsys, world)["decision"] == "block"


# -- T9: the length bound -------------------------------------------------------------------


def test_a_rearmed_nudge_with_the_drift_clause_stays_within_850(tmp_path, monkeypatch, capsys):
    """The nudge, the clause for five drifted records and the commit sentence: the old 800
    bound is too tight for it, the new one pins it."""
    monkeypatch.setattr(sync_mod, "refresh_code_drift_cache", lambda store, **kw: 5)
    world = _world(tmp_path)
    first = _stop(monkeypatch, capsys, world)
    assert "Also: 5 drifted record(s)" in first["reason"]
    _backdate(world, 40)
    _commits(world.repo, 12, first=35, last=5)
    out = _stop(monkeypatch, capsys, world)
    assert out["decision"] == "block"
    assert "Also: 5 drifted record(s)" in out["reason"]
    assert out["reason"].endswith(SENTENCE.format(n=12))
    assert len(out["reason"]) <= 850


# -- T10: sessions captured before the upgrade ----------------------------------------------


def _capture_row_at(world: World, minutes_ago: float) -> None:
    """The ``capture_sessions`` row alone, no ``capture_rearm`` key: what an older version left."""
    Store(world.db).close()
    conn = sqlite3.connect(world.db / "index.db")
    conn.execute(
        "INSERT OR REPLACE INTO capture_sessions (session_id, captured_at) VALUES (?, ?)",
        (SESSION, (datetime.now(UTC) - timedelta(minutes=minutes_ago)).isoformat()),
    )
    conn.commit()
    conn.close()


def test_a_session_captured_before_the_upgrade_rearms_from_its_captured_at(
    tmp_path, monkeypatch, capsys
):
    world = _world(tmp_path)
    _capture_row_at(world, 40)
    _commits(world.repo, 10, first=35, last=5)
    assert _stored(world) is None
    out = _stop(monkeypatch, capsys, world)
    assert out["decision"] == "block"
    assert out["reason"].endswith(SENTENCE.format(n=10))
    assert _stored(world) is not None


def test_a_session_captured_before_the_upgrade_waits_out_the_gap(tmp_path, monkeypatch, capsys):
    world = _world(tmp_path)
    _capture_row_at(world, 10)
    _commits(world.repo, 10, first=9, last=1)
    assert _stop(monkeypatch, capsys, world) == {}
    assert _stored(world) is None


# -- D3: a git failure is no re-arm ---------------------------------------------------------


def test_a_git_timeout_or_a_missing_repository_is_no_rearm(tmp_path, monkeypatch, capsys):
    """Each failure is quiet and leaves the stamp alone, and the same state with git working
    nudges: so the quiet is the failure's doing."""
    world = _world(tmp_path)
    _nudged(monkeypatch, capsys, world, minutes_ago=40)
    _commits(world.repo, 12, first=35, last=5)
    before = _stored(world)

    real_run = subprocess.run

    def timing_out(cmd, *args, **kwargs):
        if cmd and cmd[0] == "git":
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 0))
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", timing_out)
    assert _stop(monkeypatch, capsys, world) == {}
    assert _stored(world) == before
    monkeypatch.setattr(subprocess, "run", real_run)

    elsewhere = tmp_path / "plain"
    elsewhere.mkdir()
    moved = World(elsewhere, elsewhere / ".sidegraph", world.transcript)
    store = Store(moved.db)
    store.mark_captured(SESSION)
    store.set_meta(KEY, before)
    store.close()
    assert _stop(monkeypatch, capsys, moved) == {}

    assert _stop(monkeypatch, capsys, world)["decision"] == "block"


def test_the_commit_count_runs_git_in_the_store_project_with_a_short_timeout(
    tmp_path, monkeypatch, capsys
):
    """D3's pins: cwd at the project the store belongs to, git's repository-local variables
    dropped, a 2 s timeout, author time as the format, local branches and ``HEAD`` as tips."""
    world = _world(tmp_path)
    _nudged(monkeypatch, capsys, world, minutes_ago=40)
    _commits(world.repo, 10, first=35, last=5)
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "elsewhere"))
    seen: list[dict] = []
    real_run = subprocess.run

    def spy(cmd, *args, **kwargs):
        if cmd and cmd[0] == "git":
            seen.append({"cmd": cmd, **kwargs})
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy)
    assert _stop(monkeypatch, capsys, world)["decision"] == "block"
    assert len(seen) == 1
    call = seen[0]
    assert Path(call["cwd"]).resolve() == world.repo.resolve()
    assert "GIT_DIR" not in call["env"]
    assert call["timeout"] == 2
    assert call["cmd"][:2] == ["git", "log"]
    assert "--branches" in call["cmd"]
    assert "HEAD" in call["cmd"]
    assert "--format=%at" in call["cmd"]


def test_a_stop_without_a_substantial_transcript_still_rearms(tmp_path, monkeypatch, capsys):
    """The re-arm is a property of a captured session: it does not parse the transcript again."""
    world = _world(tmp_path)
    _nudged(monkeypatch, capsys, world, minutes_ago=40)
    _commits(world.repo, 10, first=35, last=5)
    seen: list[str] = []

    def recorder(path):
        seen.append(path)
        raise RuntimeError("must not be parsed")

    monkeypatch.setattr(hooks, "_transcript_stats", recorder)
    assert _stop(monkeypatch, capsys, world)["decision"] == "block"
    assert seen == []


@pytest.mark.parametrize("stamp", ["not a timestamp", ""])
def test_a_corrupt_stamp_falls_back_to_the_captured_at_and_is_replaced(
    tmp_path, monkeypatch, capsys, stamp
):
    world = _world(tmp_path)
    _capture_row_at(world, 40)
    store = Store(world.db)
    store.set_meta(KEY, stamp)
    store.close()
    _commits(world.repo, 10, first=35, last=5)
    assert _stop(monkeypatch, capsys, world)["decision"] == "block"
    assert datetime.fromisoformat(_stored(world)) > datetime.now(UTC) - timedelta(minutes=1)
