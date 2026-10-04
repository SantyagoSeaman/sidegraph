"""The Stop hook's two early exits import nothing heavy (T7).

``stop`` used to import ``Store`` before the ``stop_hook_active`` check and the read-only
ledger peek, so a Stop that did nothing still paid for pydantic and the models. The import now
sits below the peek and the substance gate.

see design/superpowers/specs/2026-10-03-hot-path-light-index-design.md (D6)
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sidegraph.store import Store
from tests.test_hot_path_imports import _repo, _run
from tests.test_stop_rearm import _commit, _commits, _git


def test_stop_with_stop_hook_active_loads_nothing_heavy(tmp_path):
    """Red against ``from ..store import Store`` ahead of the ``stop_hook_active`` check."""
    root, store_dir = _repo(tmp_path)
    out, heavy = _run("stop", {"session_id": "s1", "stop_hook_active": True}, root, store_dir)
    assert out == {}
    assert heavy == []


def test_stop_for_an_already_captured_session_loads_nothing_heavy(tmp_path):
    root, store_dir = _repo(tmp_path)
    store = Store(store_dir)
    store.mark_captured("done-1")
    store.close()
    out, heavy = _run(
        "stop",
        {"session_id": "done-1", "transcript_path": "/nonexistent/done-1.jsonl"},
        root,
        store_dir,
    )
    assert out == {}
    assert heavy == []


def _captured_minutes_ago(store_dir, session, minutes_ago):
    store = Store(store_dir)
    store.mark_captured(session)
    store.set_meta(
        f"capture_rearm:{session}", (datetime.now(UTC) - timedelta(minutes=minutes_ago)).isoformat()
    )
    store.close()


def test_stop_inside_the_rearm_gap_loads_nothing_heavy(tmp_path):
    """The common Stop of a captured session, 10 minutes after its nudge, exits on the peek."""
    root, store_dir = _repo(tmp_path)
    _captured_minutes_ago(store_dir, "done-1", 10)
    out, heavy = _run(
        "stop",
        {"session_id": "done-1", "transcript_path": "/nonexistent/done-1.jsonl"},
        root,
        store_dir,
    )
    assert out == {}
    assert heavy == []


def test_stop_past_the_gap_below_the_threshold_loads_nothing_heavy(tmp_path):
    """The count is one ``git log``: it must not import the models, or ``verify``'s pull of
    them, to find out that nine commits are not ten."""
    root, store_dir = _repo(tmp_path)
    _git(root, "init", "-q", "-b", "main")
    _commit(root, author=180)
    _commits(root, 9, first=35, last=5)
    _captured_minutes_ago(store_dir, "done-1", 40)
    out, heavy = _run(
        "stop",
        {"session_id": "done-1", "transcript_path": "/nonexistent/done-1.jsonl"},
        root,
        store_dir,
    )
    assert out == {}
    assert heavy == []
