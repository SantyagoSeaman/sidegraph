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

    monkeypatch.setattr(Store, "prune_retrieval_events", boom)
    # SIDEGRAPH_DIR, not the deprecated SIDEGRAPH_DB: see `_run_session_start`'s comment —
    # a nonexistent `db` path would otherwise get rescued to its PARENT directory.
    monkeypatch.setenv("SIDEGRAPH_DIR", str(db))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "abc-123"})))
    hooks.session_start()
    out = json.loads(capsys.readouterr().out)

    assert "hookSpecificOutput" in out, "the map must survive a telemetry failure"
