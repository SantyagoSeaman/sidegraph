"""Render-event journal (design/superpowers/specs/2026-09-18-usage-stats-design.md, D4/D5).

Index-only by construction: recording a render must never dirty git.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sidegraph.store import Store


def _record(store, **over):
    kwargs = dict(
        intent=None,
        selected=5,
        emitted=3,
        degraded=1,
        dropped_for_budget=2,
        chars_used=4200,
        had_rejected=True,
        had_superseded=False,
    )
    kwargs.update(over)
    store.record_render_event("sess-1", **kwargs)


def test_record_render_event_roundtrips_every_column(tmp_path):
    store = Store(tmp_path / "s")
    _record(store, intent="check-plan")
    e = store.render_events()[0]
    assert e["session_id"] == "sess-1"
    assert e["intent"] == "check-plan"
    assert (e["selected"], e["emitted"], e["degraded"], e["dropped_for_budget"]) == (5, 3, 1, 2)
    assert e["chars_used"] == 4200
    assert (e["had_rejected"], e["had_superseded"]) == (1, 0)
    store.close()


def test_render_events_filter_by_session(tmp_path):
    store = Store(tmp_path / "s")
    _record(store)
    store.record_render_event(
        "sess-2",
        intent=None,
        selected=1,
        emitted=1,
        degraded=0,
        dropped_for_budget=0,
        chars_used=10,
        had_rejected=False,
        had_superseded=False,
    )
    assert len(store.render_events("sess-2")) == 1
    assert len(store.render_events()) == 2
    store.close()


def test_prune_sweeps_render_events_too(tmp_path):
    store = Store(tmp_path / "s")
    _record(store)
    store.record_touch("sess-1", "a.py", "Read")
    old = (datetime.now(UTC) - timedelta(days=90)).isoformat()
    with store._mutation():
        store._conn.execute("UPDATE render_events SET at = ?", (old,))
        store._conn.execute("UPDATE retrieval_events SET at = ?", (old,))
    assert store.prune_telemetry_events(older_than_days=30) == 2
    assert store.render_events() == []
    assert store.retrieval_events() == []
    store.close()
