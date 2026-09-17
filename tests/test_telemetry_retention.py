"""Opting out of telemetry must not EXTEND retention (practitioner re-review round 2).

`SIDEGRAPH_TELEMETRY=off` gated both the recording AND the 30-day prune, so a developer
who opted out kept their existing behavioural journal forever while everyone else's aged
out. That is the wrong way round for GDPR and for a works council: an opt-out that
freezes retention is worse for the person opting out than no opt-out at all.

Red target: unfixed code leaves the rows in place when the flag is off.
"""

from __future__ import annotations

import io
import json
import sys
from datetime import UTC, datetime, timedelta

from sidegraph.host import hooks
from sidegraph.store import Store


def _old_event(store: Store, days: int) -> None:
    ts = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    with store._lock:
        store._conn.execute(
            "INSERT INTO retrieval_events (session_id, at, kind, key, detail) VALUES (?,?,?,?,?)",
            ("s1", ts, "show", "01ABC", None),
        )
        store._conn.commit()


def _count(store: Store) -> int:
    with store._lock:
        return store._conn.execute("SELECT COUNT(*) FROM retrieval_events").fetchone()[0]


def _run_session_start(tmp_path, monkeypatch):
    monkeypatch.setenv("SIDEGRAPH_DB", str(tmp_path / "s.db"))
    monkeypatch.setenv("SIDEGRAPH_GRAPH", str(tmp_path / "no-graph.json"))
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"session_id": "abc"})))
    hooks.session_start()


def test_opting_out_still_prunes_expired_events(tmp_path, monkeypatch, capsys):
    store = Store(tmp_path / "s.db")
    _old_event(store, days=90)
    assert _count(store) == 1
    monkeypatch.setenv("SIDEGRAPH_TELEMETRY", "off")
    _run_session_start(tmp_path, monkeypatch)
    capsys.readouterr()
    assert _count(Store(tmp_path / "s.db")) == 0, (
        "opting out froze retention: the journal must still age out"
    )


def test_opting_out_still_records_nothing_new(tmp_path, monkeypatch, capsys):
    """Over-reach guard (declared): pruning while opted out must not resurrect recording."""
    store = Store(tmp_path / "s.db")
    _old_event(store, days=1)
    monkeypatch.setenv("SIDEGRAPH_TELEMETRY", "off")
    _run_session_start(tmp_path, monkeypatch)
    capsys.readouterr()
    assert _count(Store(tmp_path / "s.db")) == 1  # the fresh row survives, none added
