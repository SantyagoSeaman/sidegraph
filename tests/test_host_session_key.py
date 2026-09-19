"""SessionStart publishes the telemetry session key and prunes the journal (spec D2/D7)."""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime, timedelta

import sidegraph.host.hooks as hooks
from sidegraph.store import Store


def _run_session_start(monkeypatch, capsys, payload, db):
    # SIDEGRAPH_DIR, not the deprecated SIDEGRAPH_DB: config._dispatch_sidegraph_db rescues a
    # nonexistent path with a directory component to its PARENT -- exactly `db`'s shape on a
    # test's first-ever write, before anything else has created it. SIDEGRAPH_DIR has no such
    # rescue: the literal `db` path is what both this hook call and the test's own direct
    # Store(db) must agree on (same reasoning as test_host_touch_events.py's `_run`).
    monkeypatch.setenv("SIDEGRAPH_DIR", str(db))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    hooks.session_start()
    capsys.readouterr()


def test_session_start_publishes_the_key_with_a_timestamp(tmp_path, monkeypatch, capsys):
    db = tmp_path / "db"
    _run_session_start(monkeypatch, capsys, {"session_id": "abc-123"}, db)

    store = Store(db)
    raw = store.get_meta(hooks.TELEMETRY_SESSION_KEY)
    store.close()

    assert raw is not None
    session_id, _, stamp = raw.partition("|")
    assert session_id == "abc-123"
    written = datetime.fromisoformat(stamp)
    assert abs((datetime.now(UTC) - written).total_seconds()) < 60


def test_a_payload_without_a_session_id_publishes_no_key(tmp_path, monkeypatch, capsys):
    db = tmp_path / "db"
    _run_session_start(monkeypatch, capsys, {}, db)

    store = Store(db)
    assert store.get_meta(hooks.TELEMETRY_SESSION_KEY) is None
    store.close()

    _run_session_start(monkeypatch, capsys, {"session_id": "abc-123"}, db)
    store = Store(db)
    assert store.get_meta(hooks.TELEMETRY_SESSION_KEY) is not None
    store.close()


def test_session_start_prunes_old_events_and_keeps_fresh_ones(tmp_path, monkeypatch, capsys):
    db = tmp_path / "db"
    store = Store(db)
    store.record_touch("fresh-session", "fresh.py", "Read")
    old = (datetime.now(UTC) - timedelta(days=31)).isoformat()
    store._conn.execute(
        "INSERT INTO retrieval_events (session_id, at, kind, key, detail) "
        "VALUES ('old-session', ?, 'touch', 'old.py', 'Read')",
        (old,),
    )
    store._conn.commit()
    store.close()

    _run_session_start(monkeypatch, capsys, {"session_id": "abc-123"}, db)

    store = Store(db)
    assert [e["key"] for e in store.retrieval_events()] == ["fresh.py"]
    store.close()


def _seed_one_old_event(db):
    store = Store(db)
    old = (datetime.now(UTC) - timedelta(days=31)).isoformat()
    store._conn.execute(
        "INSERT INTO retrieval_events (session_id, at, kind, key, detail) "
        "VALUES ('old-session', ?, 'touch', 'old.py', 'Read')",
        (old,),
    )
    store._conn.commit()
    store.close()


def test_telemetry_off_still_publishes_key_and_still_prunes(tmp_path, monkeypatch, capsys):
    """D6, applied to the session-key path specifically: the opt-out was already covered for
    touches (test_host_touch_events.py) and for server-side events (test_server_telemetry.py).

    Revised for D7.3 (staleness-machinery wave): the key WRITE is unconditional --
    capture._propose_one falls back to it for provenance.session_id when the caller passed
    none, and that fallback must not silently go dark just because a project opted out of
    retrieval telemetry (this is provenance attribution, not the telemetry feature this
    env var actually gates).

    Revised again 2026-08-04 (practitioner re-review round 2), and this file previously
    PINNED the defect: pruning was telemetry-gated, so opting out froze the 30-day
    retention of the journal that already existed. An opt-out that makes personal
    behavioural data live LONGER is backwards for GDPR and for a works council. Recording
    stays off; expiry now runs regardless, so opting out strictly reduces retention."""
    db = tmp_path / "db"
    _seed_one_old_event(db)

    monkeypatch.setenv("SIDEGRAPH_TELEMETRY", "off")
    _run_session_start(monkeypatch, capsys, {"session_id": "abc-123"}, db)

    store = Store(db)
    assert store.get_meta(hooks.TELEMETRY_SESSION_KEY) is not None, (
        "off must still publish the key (D7.3: unconditional, feeds capture's fallback)"
    )
    assert [e["key"] for e in store.retrieval_events()] == [], (
        "off must still prune: opting out must never extend retention"
    )
    store.close()


def test_telemetry_on_publishes_key_and_prunes(tmp_path, monkeypatch, capsys):
    """Paired with the off case above: same setup, opt-out unset, both effects fire."""
    db = tmp_path / "db"
    _seed_one_old_event(db)

    _run_session_start(monkeypatch, capsys, {"session_id": "abc-123"}, db)

    store = Store(db)
    assert store.get_meta(hooks.TELEMETRY_SESSION_KEY) is not None, "on must publish a key"
    assert store.retrieval_events() == [], "on must prune the stale event"
    store.close()


def test_a_prune_failure_does_not_cost_the_session_map(tmp_path, monkeypatch, capsys):
    """SessionStart's contract is that nothing it does may block a session
    ("Must never crash the session" — session_start's own docstring)."""
    db = tmp_path / "db"

    def boom(*args, **kwargs):
        raise RuntimeError("prune exploded")

    monkeypatch.setattr(Store, "prune_telemetry_events", boom)
    # SIDEGRAPH_DIR, not the deprecated SIDEGRAPH_DB: see `_run_session_start`'s comment —
    # a nonexistent `db` path would otherwise get rescued to its PARENT directory.
    monkeypatch.setenv("SIDEGRAPH_DIR", str(db))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "abc-123"})))
    hooks.session_start()
    out = json.loads(capsys.readouterr().out)

    assert "hookSpecificOutput" in out, "the map must survive a telemetry failure"


# -- Codex: session identity comes from the transcript, not the host's session_id ----------
#
# Measured 2026-09-19 on two live stores. Claude Code's `session_id` is per session and its
# transcript is named after it: all 74 session ids in this repo's own store are exactly
# transcript stems. Codex's `session_id` is the umbrella workspace session — it spans days,
# survives resume, and covers every thread beneath it: 1928 of 1929 events in a second live
# store landed in ONE bucket across 27 hours, while each thread had its own rollout file.
# Codex's SessionStart payload carries no thread id and no agent_type (schema
# `session-start.command.input`), so `transcript_path` is the only field that identifies a
# session on both hosts.


def _codex_payload(rollout: str, umbrella: str = "01a0b3e2-39d2-7180-86a5-408a6f9ce058"):
    """A Codex SessionStart payload: one umbrella id, one per-thread rollout file."""
    return {
        "session_id": umbrella,
        "source": "startup",
        "transcript_path": f"/w/.codex/sessions/2026/09/19/{rollout}.jsonl",
        "cwd": "/w/project",
    }


def test_two_codex_threads_sharing_one_umbrella_id_are_two_sessions(tmp_path, monkeypatch, capsys):
    """The defect, red against reading payload['session_id']: two Codex threads report the
    same umbrella `session_id`, so every event from both was attributed to one session and
    the second thread's SessionStart was swallowed by the 60s double-injection dedupe."""
    db = tmp_path / "db"
    keys = []
    for rollout in (
        "rollout-2026-09-19T12-00-31-01a0b953-2d7c-76e3-9dac-7d375dad7954",
        "rollout-2026-09-19T12-16-06-01a0b961-7463-7b21-bd3b-bab64d9d5d8b",
    ):
        _run_session_start(monkeypatch, capsys, _codex_payload(rollout), db)
        store = Store(db)
        raw = store.get_meta(hooks.TELEMETRY_SESSION_KEY)
        store.close()
        assert raw is not None
        keys.append(raw.partition("|")[0])

    assert keys[0] != keys[1], "two threads under one umbrella id must be two sessions"
    assert keys[1].endswith("01a0b961-7463-7b21-bd3b-bab64d9d5d8b")


def test_the_umbrella_id_is_kept_so_codex_threads_can_be_grouped(tmp_path, monkeypatch, capsys):
    """The workspace session is the only link between a Codex thread and its siblings, so it
    is recorded beside the key rather than discarded."""
    db = tmp_path / "db"
    _run_session_start(
        monkeypatch, capsys, _codex_payload("rollout-2026-09-19T12-16-06-01a0b961-7463"), db
    )

    store = Store(db)
    group = store.get_meta(hooks.TELEMETRY_SESSION_GROUP_KEY)
    store.close()

    assert group == "01a0b3e2-39d2-7180-86a5-408a6f9ce058"


def test_claude_code_keeps_the_session_id_it_already_recorded(tmp_path, monkeypatch, capsys):
    """No-regression control, and the reason this change is safe to ship: under Claude Code
    the transcript stem IS the session id, so the recorded key does not move and no group
    key is written (there is no umbrella above a Claude Code session)."""
    db = tmp_path / "db"
    sid = "134960fe-2641-48d5-aaf7-223d9129df38"
    _run_session_start(
        monkeypatch,
        capsys,
        {"session_id": sid, "transcript_path": f"/w/.claude/projects/repo/{sid}.jsonl"},
        db,
    )

    store = Store(db)
    raw = store.get_meta(hooks.TELEMETRY_SESSION_KEY)
    group = store.get_meta(hooks.TELEMETRY_SESSION_GROUP_KEY)
    store.close()

    assert raw is not None and raw.partition("|")[0] == sid
    assert group is None


def test_a_null_transcript_path_falls_back_to_the_session_id(tmp_path, monkeypatch, capsys):
    """`transcript_path` is nullable in Codex's own schema, so its absence must leave the
    previous behaviour exactly as it was rather than record nothing."""
    db = tmp_path / "db"
    _run_session_start(monkeypatch, capsys, {"session_id": "abc-123", "transcript_path": None}, db)

    store = Store(db)
    raw = store.get_meta(hooks.TELEMETRY_SESSION_KEY)
    store.close()

    assert raw is not None and raw.partition("|")[0] == "abc-123"
