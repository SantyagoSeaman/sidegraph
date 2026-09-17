"""Drift→supersede wave D2: ``sync.refresh_code_drift_cache`` — rescan-always, per-commit
merge on partial failure, untouched cache on an unscanned result, silence on an inactive
store. Git fixtures follow ``test_doctor_scan.py``'s conventions.
# see design/superpowers/specs/2026-07-30-drift-supersede-affordance-design.md (D2)
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sidegraph import doctor as doctor_mod
from sidegraph import sync as sync_mod
from sidegraph.retrieval import DRIFT_CACHE_KEY
from sidegraph.schema import (
    AnchorBinding,
    Decision,
    DecisionKind,
    DecisionStatus,
    Descriptor,
    Entity,
    Provenance,
)
from sidegraph.store import Store
from sidegraph.sync import refresh_code_drift_cache


def _git(args: list[str], cwd: Path) -> None:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"


def _head(cwd: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def git_repo(tmp_path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], cwd=repo)
    _git(["config", "user.email", "test@example.com"], cwd=repo)
    _git(["config", "user.name", "Test"], cwd=repo)
    return repo


def _stamped_decision(store: Store, commit: str, file_path: str, name: str) -> Decision:
    d = store.add_decision(
        Decision(
            title=f"About {name}",
            kind=DecisionKind.GOTCHA,
            context="c",
            choice="ch",
            status=DecisionStatus.ACCEPTED,
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="agent", commit=commit),
        )
    )
    e = store.upsert_entity(
        Entity(canonical_name=name, descriptor=Descriptor(name=name, file_path=file_path))
    )
    store.add_binding(AnchorBinding(record_id=d.id, entity_id=e.entity_id, tier=2, status="live"))
    return d


def _commit_file(repo: Path, rel: str, content: str, msg: str) -> str:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    _git(["add", "-A"], cwd=repo)
    _git(["commit", "-q", "-m", msg], cwd=repo)
    return _head(repo)


def _cache(store: Store) -> dict | None:
    raw = store.get_meta(DRIFT_CACHE_KEY)
    return None if raw is None else json.loads(raw)


def test_refresh_on_inactive_store_returns_zero_and_writes_nothing(git_repo):
    store = Store(git_repo / ".sidegraph")
    n = refresh_code_drift_cache(store)
    assert n == 0
    assert _cache(store) is None


def test_refresh_writes_cache_with_head_and_drifted_ids(git_repo):
    commit = _commit_file(git_repo, "a.py", "a\n", "c1")
    store = Store(git_repo / ".sidegraph")
    d = _stamped_decision(store, commit, "a.py", "A")
    _commit_file(git_repo, "a.py", "changed\n", "c2")
    n = refresh_code_drift_cache(store)
    assert n == 1
    cache = _cache(store)
    assert cache["head"] == _head(git_repo)
    assert cache["by_commit"] == {commit: [d.id]}


def test_refresh_clean_rescan_writes_empty_list_and_drops_departed_commit(git_repo):
    """After a supersede, the drifted record's capture commit leaves the join (dropped
    from by_commit) while a surviving clean record keeps the scan active — its commit is
    written with an EMPTY list, a normal write (spec D2)."""
    commit_a = _commit_file(git_repo, "a.py", "a\n", "c1")
    commit_b = _commit_file(git_repo, "b.py", "b\n", "c2")
    store = Store(git_repo / ".sidegraph")
    d_a = _stamped_decision(store, commit_a, "a.py", "A")
    _stamped_decision(store, commit_b, "b.py", "B")  # never drifts (b.py unchanged)
    _commit_file(git_repo, "a.py", "changed\n", "c3")
    assert refresh_code_drift_cache(store) == 1

    store.add_decision(
        Decision(
            title="Successor",
            kind=DecisionKind.GOTCHA,
            context="c",
            choice="ch",
            status=DecisionStatus.ACCEPTED,
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="agent"),
            supersedes=d_a.id,
        )
    )
    n = refresh_code_drift_cache(store)
    assert n == 0
    cache = _cache(store)
    assert cache["by_commit"] == {commit_b: []}


def test_refresh_gone_inactive_returns_zero_and_reads_stay_empty(git_repo):
    """When the LAST stamped+anchored record goes terminal the scan turns inactive —
    per spec N5 the refresh writes nothing (stale cache bytes may remain), and the READ
    side's live filter is what keeps markers and counts honest."""
    from sidegraph.retrieval import drifted_record_ids

    commit = _commit_file(git_repo, "a.py", "a\n", "c1")
    store = Store(git_repo / ".sidegraph")
    d = _stamped_decision(store, commit, "a.py", "A")
    _commit_file(git_repo, "a.py", "changed\n", "c2")
    assert refresh_code_drift_cache(store) == 1

    store.add_decision(
        Decision(
            title="Successor",
            kind=DecisionKind.GOTCHA,
            context="c",
            choice="ch",
            status=DecisionStatus.ACCEPTED,
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="agent"),
            supersedes=d.id,
        )
    )
    assert refresh_code_drift_cache(store) == 0
    assert drifted_record_ids(store) == frozenset()


def test_refresh_partial_failure_retains_failed_commit_and_replaces_successful(
    git_repo, monkeypatch
):
    """One batch fails, one succeeds: the failed commit's cached ids are RETAINED, the
    successful commit's entry is REPLACED with the fresh scan — including non-empty →
    empty (review M-4: the cache is doctored with a stale id for the clean commit, so a
    retain-successful-too mutant cannot pass)."""
    commit_a = _commit_file(git_repo, "a.py", "a\n", "c1")
    commit_b = _commit_file(git_repo, "b.py", "b\n", "c2")
    store = Store(git_repo / ".sidegraph")
    d_a = _stamped_decision(store, commit_a, "a.py", "A")
    d_b = _stamped_decision(store, commit_b, "b.py", "B")
    _commit_file(git_repo, "a.py", "changed\n", "c3")  # a drifts; b.py never changes
    assert refresh_code_drift_cache(store) == 1

    # Doctor the cache: pretend commit_b had a drifted id from an earlier scan — the
    # fresh (successful, clean) scan must replace it with [], not retain it.
    cache = _cache(store)
    cache["by_commit"][commit_b] = [d_b.id]
    store.set_meta(DRIFT_CACHE_KEY, json.dumps(cache))

    real = doctor_mod._git_diff_name_only

    def failing_for_a(repo_root, commit, paths, **kw):
        if commit == commit_a:
            raise ValueError("simulated failure")
        return real(repo_root, commit, paths, **kw)

    monkeypatch.setattr(doctor_mod, "_git_diff_name_only", failing_for_a)
    n = refresh_code_drift_cache(store)
    assert n == 1
    cache = _cache(store)
    assert cache["by_commit"][commit_a] == [d_a.id]  # retained from previous good scan
    assert cache["by_commit"][commit_b] == []  # freshly scanned: stale id replaced by empty


def test_refresh_unscanned_leaves_cache_untouched_and_returns_none(tmp_path):
    """Store outside any git repo: repo-root resolution fails, nothing ran — the previous
    cache bytes stay exactly as they were (spec D2 'unscanned')."""
    store = Store(tmp_path / ".sidegraph")
    d = store.add_decision(
        Decision(
            title="t",
            kind=DecisionKind.GOTCHA,
            context="c",
            choice="ch",
            status=DecisionStatus.ACCEPTED,
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="agent", commit="deadbeef"),
        )
    )
    e = store.upsert_entity(
        Entity(canonical_name="A", descriptor=Descriptor(name="A", file_path="a.py"))
    )
    store.add_binding(AnchorBinding(record_id=d.id, entity_id=e.entity_id, tier=2, status="live"))
    sentinel = json.dumps({"head": "old", "computed_at": "x", "by_commit": {"c0": [d.id]}})
    store.set_meta(DRIFT_CACHE_KEY, sentinel)
    assert refresh_code_drift_cache(store) is None
    assert store.get_meta(DRIFT_CACHE_KEY) == sentinel


def test_refresh_rescan_catches_add_anchors_at_unchanged_head(git_repo):
    """The rev-1 head-stamp no-op's blind spot (spec I2): a bindings-only write anchors an
    old stamped record to a file that changed since its capture commit, at unchanged HEAD.
    Rescan-always must catch it. Red-first vs a deliberately re-introduced no-op mutant;
    kept as the guard that the no-op never returns."""
    commit = _commit_file(git_repo, "a.py", "a\n", "c1")
    _commit_file(git_repo, "b.py", "b\n", "c2")
    store = Store(git_repo / ".sidegraph")
    d = _stamped_decision(store, commit, "a.py", "A")
    _commit_file(git_repo, "b.py", "changed b\n", "c3")
    assert refresh_code_drift_cache(store) == 0  # a.py unchanged so far

    # add_anchors-shaped: bind the same record to b.py (changed since `commit`) — no new
    # git commit, HEAD identical to the first refresh.
    e2 = store.upsert_entity(
        Entity(canonical_name="B", descriptor=Descriptor(name="B", file_path="b.py"))
    )
    store.add_binding(AnchorBinding(record_id=d.id, entity_id=e2.entity_id, tier=2, status="live"))
    n = refresh_code_drift_cache(store)
    assert n == 1
    assert _cache(store)["by_commit"] == {commit: [d.id]}


def test_refresh_head_failure_alone_still_writes_with_null_head(git_repo, monkeypatch):
    """Review M-2: a failed `rev-parse HEAD` after fully successful batches must NOT
    discard the scan — `head` is provenance only and gates nothing; the cache is written
    with head=null."""
    commit = _commit_file(git_repo, "a.py", "a\n", "c1")
    store = Store(git_repo / ".sidegraph")
    d = _stamped_decision(store, commit, "a.py", "A")
    _commit_file(git_repo, "a.py", "changed\n", "c2")

    import sidegraph.verify as verify_mod

    real = verify_mod._run_git

    def failing_head(args, cwd, **kw):
        if args[:2] == ["rev-parse", "HEAD"]:
            raise ValueError("simulated head failure")
        return real(args, cwd, **kw)

    monkeypatch.setattr(doctor_mod, "_run_git", failing_head)
    n = refresh_code_drift_cache(store)
    assert n == 1
    cache = _cache(store)
    assert cache["head"] is None
    assert cache["by_commit"] == {commit: [d.id]}


def test_refresh_never_raises(git_repo, monkeypatch):
    store = Store(git_repo / ".sidegraph")

    def boom(*a, **k):
        raise RuntimeError("scan exploded")

    monkeypatch.setattr(sync_mod, "scan_code_drift", boom)
    assert refresh_code_drift_cache(store) is None
