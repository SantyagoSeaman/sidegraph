"""Render-event journal (design/superpowers/specs/2026-09-18-usage-stats-design.md, D4/D5).

Index-only by construction: recording a render must never dirty git.
"""

from __future__ import annotations

import subprocess
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


def test_render_events_survive_a_canonical_reload(tmp_path):
    """The DROP list names record tables explicitly; telemetry survives by ABSENCE from it
    (spec D4). That property is invisible at the call site, so it is pinned here.

    The digest argument is required — see tests/test_store_telemetry.py:88 for the
    established call shape.
    """
    store = Store(tmp_path / "s")
    _record(store)
    store._reload_index_from_canonical("forced-digest")
    assert len(store.render_events()) == 1
    store.close()


def test_the_index_is_ignored_so_recording_cannot_dirty_git(tmp_path):
    """`git status --porcelain` collapses an untracked directory to one line, so asserting
    'index.db' is absent from it would pass even if the file were tracked. Ask git directly.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    store = Store(repo / ".sidegraph")
    _record(store)
    store.close()
    ignored = subprocess.run(
        ["git", "check-ignore", ".sidegraph/index.db"], cwd=repo, capture_output=True, text=True
    )
    assert ignored.returncode == 0, "index.db must be ignored by the store's own .gitignore"
