"""Coverage-telemetry journal (design/superpowers/specs/
2026-07-26-retrieval-coverage-telemetry-design.md, D1/D7).

Index-only by construction: the whole point is that reading the store never dirties git.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta

from sidegraph.store import Store


def test_record_touch_writes_one_event(tmp_path):
    store = Store(tmp_path / "s")
    store.record_touch("sess-1", "src/sidegraph/store.py", "Read")
    events = store.retrieval_events()
    assert len(events) == 1
    assert events[0]["session_id"] == "sess-1"
    assert events[0]["kind"] == "touch"
    assert events[0]["key"] == "src/sidegraph/store.py"
    assert events[0]["detail"] == "Read"
    store.close()


def test_record_retrieval_events_writes_seeds_and_shows(tmp_path):
    store = Store(tmp_path / "s")
    store.record_retrieval_events(
        "sess-1",
        seeds=["a.py", "a.py", "b.py"],
        shows=[("rec-1", "a.py"), ("rec-1", "c.py")],
    )
    kinds = [(e["kind"], e["key"], e["detail"]) for e in store.retrieval_events()]
    assert ("seed", "a.py", None) in kinds
    assert ("seed", "b.py", None) in kinds
    assert kinds.count(("seed", "a.py", None)) == 1, "seeds dedup per call"
    assert ("show_anchor", "a.py", "rec-1") in kinds
    assert ("show_anchor", "c.py", "rec-1") in kinds
    store.close()


def test_events_filter_by_session_and_keep_insertion_order(tmp_path):
    store = Store(tmp_path / "s")
    store.record_touch("sess-1", "a.py", "Read")
    store.record_touch("sess-2", "b.py", "Edit")
    store.record_touch("sess-1", "c.py", "Write")
    keys = [e["key"] for e in store.retrieval_events("sess-1")]
    assert keys == ["a.py", "c.py"]
    store.close()


def test_prune_deletes_old_events_and_keeps_fresh_ones(tmp_path):
    store = Store(tmp_path / "s")
    store.record_touch("sess-fresh", "fresh.py", "Read")
    old = (datetime.now(UTC) - timedelta(days=31)).isoformat()
    store._conn.execute(
        "INSERT INTO retrieval_events (session_id, at, kind, key, detail) "
        "VALUES ('sess-old', ?, 'touch', 'old.py', 'Read')",
        (old,),
    )
    store._conn.commit()

    deleted = store.prune_retrieval_events()

    assert deleted == 1
    assert [e["key"] for e in store.retrieval_events()] == ["fresh.py"]
    store.close()


def test_events_survive_a_canonical_reload(tmp_path):
    """GUARD (declared exception, spec row 14): passes before and after this feature,
    because `_reload_index_from_canonical` drops an explicit list of six record tables plus
    `canonical_stat` and never touches anything else. The test exists so a later change
    cannot add `retrieval_events` to that list — losing the journal on every `git pull`
    would make the counts meaningless for the long-lived question they answer."""
    store = Store(tmp_path / "s")
    store.record_touch("sess-1", "a.py", "Read")
    digest, _stats = store._compute_canonical_digest()
    store._reload_index_from_canonical(digest)
    assert [e["key"] for e in store.retrieval_events()] == ["a.py"]
    store.close()


def test_recording_never_dirties_git(tmp_path):
    """The sync-clean invariant: a read path must not produce a canonical diff."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    store = Store(repo / ".sidegraph")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=repo,
        check=True,
    )

    store.record_touch("sess-1", "a.py", "Read")
    store.record_retrieval_events("sess-1", ["a.py"], [("rec-1", "b.py")])
    store.close()

    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True, check=True
    )
    assert status.stdout == "", f"telemetry dirtied git: {status.stdout}"
