"""Code drift falls back to the graph's build commit for a record with no capture commit.

Ledger T1-T9, T12 (doctor and grouped output), T14-T17 of the spec. The record-write tool
(T10, T11, T18) and the ADR export (T13) have their own files.
# see design/superpowers/specs/2026-11-16-drift-graph-commit-fallback-design.md
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sidegraph import doctor as doctor_mod
from sidegraph import drift_groups
from sidegraph.doctor import CODE_DRIFT, curate
from sidegraph.retrieval import DRIFT_CACHE_KEY, RetrievalBudget, drifted_record_ids, rank_decisions
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
from sidegraph.store_layout import stamp_cache
from sidegraph.sync import build_cache, refresh_code_drift_cache
from tests.drift_groups_support import GroupRepo, git, rid

NOTE = "code-drift: git unavailable — check skipped"


@pytest.fixture
def repo(tmp_path) -> GroupRepo:
    return GroupRepo(tmp_path)


def _detail(repo: GroupRepo) -> list[str]:
    repo.write_index()
    return [f.detail for f in curate(repo.store).findings if f.code == CODE_DRIFT]


def _entries(scan) -> dict[str, list[str]]:
    return {e.record_id: e.files for b in scan.batches for e in b.entries}


# -- T1 ---------------------------------------------------------------------------------------


def test_graph_baseline_flags_a_file_touched_since_the_build(repo):
    build = repo.commit({"f.py": "a\n"})
    record = repo.record(1, commit=None, graph=build, files=["f.py"])
    repo.commit({"f.py": "b\n"})
    details = _detail(repo)
    assert (
        f"anchored file(s) touched since the graph build (commit {build[:8]}): f.py" in details
    ), details
    scan = repo.scan()
    assert _entries(scan) == {record: ["f.py"]}


# -- T2 ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "version, expected",
    [
        ("content:abc", None),
        (f"{'a' * 39}:abc", None),
        (f"{'A' * 40}:abc", None),
        (f"{'a' * 41}:abc", None),
        (f"{'g' * 40}:abc", None),
        (":abc", None),
        ("", None),
        (None, None),
        ("a" * 40, None),
        (f"{'a' * 40}:abc", "a" * 40),
        (f"{'0f' * 32}:abc", "0f" * 32),
    ],
)
def test_built_at_commit_of_takes_only_a_full_lowercase_object_id(version, expected):
    from sidegraph.engine.reader import built_at_commit_of

    assert built_at_commit_of(version) == expected


def test_drift_baseline_prefers_the_capture_commit_and_falls_back_to_the_graph():
    from sidegraph.doctor import drift_baseline

    graph = f"{'b' * 40}:abc"
    assert drift_baseline({"commit": "c" * 40, "graph_version": graph}) == ("c" * 40, "capture")
    assert drift_baseline({"commit": None, "graph_version": graph}) == ("b" * 40, "graph")
    assert drift_baseline({"commit": "", "graph_version": graph}) == ("b" * 40, "graph")
    assert drift_baseline({"graph_version": graph}) == ("b" * 40, "graph")
    assert drift_baseline({"commit": None, "graph_version": "content:abc"}) is None
    assert drift_baseline({"commit": None, "graph_version": f"{'b' * 39}:abc"}) is None
    assert drift_baseline({"commit": None}) is None
    assert drift_baseline({}) is None
    assert drift_baseline(None) is None
    assert drift_baseline("not a mapping") is None
    # the pydantic model the record-write tool and ADR export hold
    model = Provenance(source="agent", commit=None, graph_version=graph)
    assert drift_baseline(model) == ("b" * 40, "graph")
    assert drift_baseline(Provenance(source="agent", commit="c" * 40)) == ("c" * 40, "capture")


# -- T3 ---------------------------------------------------------------------------------------


def test_a_capture_commit_wins_over_the_graph_commit(repo):
    built = repo.commit({"f.py": "a\n"})
    captured = repo.commit({"f.py": "b\n"})  # changed between the build and the capture
    record = repo.record(1, commit=captured, graph=built, files=["f.py"])
    scan = repo.scan()
    assert [(b.commit, b.source) for b in scan.batches] == [(captured, "capture")]
    assert _entries(scan) == {}
    repo.commit({"f.py": "c\n"})
    scan = repo.scan()
    assert _entries(scan) == {record: ["f.py"]}
    assert [(b.commit, b.source) for b in scan.batches] == [(captured, "capture")]


# -- T4 / T4b ---------------------------------------------------------------------------------


def test_a_file_changed_then_restored_is_flagged(repo):
    build = repo.commit({"f.py": "a\n"})
    record = repo.record(1, commit=None, graph=build, files=["f.py"])
    repo.commit({"f.py": "b\n"})
    repo.commit({"f.py": "a\n"})
    assert git(repo.root, "diff", "--name-only", f"{build}..HEAD") == ""  # a two-tree diff is blind
    assert _entries(repo.scan()) == {record: ["f.py"]}


def _side_change_and_restore(repo: GroupRepo) -> str:
    """The build, then a side branch that changes ``f.py`` and restores it, merged."""
    build = repo.commit({"f.py": "a\n", "o.txt": "0\n"})
    git(repo.root, "checkout", "-q", "-b", "side")
    repo.commit({"f.py": "b\n"}, "side change")
    repo.commit({"f.py": "a\n"}, "side restore")
    git(repo.root, "checkout", "-q", "-")
    repo.commit({"o.txt": "1\n"}, "main work")
    git(repo.root, "merge", "-q", "--no-ff", "-m", "merge side", "side")
    return build


def _ours_merge(repo: GroupRepo) -> str:
    """A side change that an ``-s ours`` merge drops."""
    build = repo.commit({"f.py": "a\n", "o.txt": "0\n"})
    git(repo.root, "checkout", "-q", "-b", "side")
    repo.commit({"f.py": "b\n"}, "side change")
    git(repo.root, "checkout", "-q", "-")
    repo.commit({"o.txt": "1\n"}, "main work")
    git(repo.root, "merge", "-q", "--no-ff", "-s", "ours", "-m", "merge ours", "side")
    return build


def _evil_merge(repo: GroupRepo) -> str:
    """A merge that itself edits ``f.py``, which neither parent changed."""
    build = repo.commit({"f.py": "a\n", "o.txt": "0\n"})
    git(repo.root, "checkout", "-q", "-b", "side")
    repo.commit({"o.txt": "side\n"}, "side work")
    git(repo.root, "checkout", "-q", "-")
    repo.commit({"o.txt": "main\n"}, "main work")
    git(repo.root, "merge", "-q", "--no-ff", "--no-commit", "-X", "ours", "side")
    (repo.root / "f.py").write_text("evil\n", encoding="utf-8")
    git(repo.root, "add", "--", "f.py")
    git(repo.root, "commit", "-q", "-m", "evil merge")
    return build


@pytest.mark.parametrize("topology", [_side_change_and_restore, _ours_merge, _evil_merge])
def test_history_that_simplification_would_hide_is_still_flagged(repo, topology):
    build = topology(repo)
    record = repo.record(1, commit=None, graph=build, files=["f.py"])
    assert _entries(repo.scan()) == {record: ["f.py"]}


# -- T5 / T6 / T7 / T8 / T17 ------------------------------------------------------------------


def _with_a_capture_record(repo: GroupRepo) -> tuple[str, str]:
    """A capture record that keeps the scan active, and its id."""
    commit = repo.commit({"keep.py": "k\n"})
    return commit, repo.record(9, commit=commit, files=["keep.py"])


def test_a_build_that_is_not_an_ancestor_leaves_the_record_unchecked(repo):
    _, keeper = _with_a_capture_record(repo)
    base = repo.commit({"f.py": "a\n"})
    git(repo.root, "checkout", "-q", "-b", "other")
    built_there = repo.commit({"f.py": "b\n"}, "built on another branch")
    git(repo.root, "checkout", "-q", base)
    repo.commit({"f.py": "c\n"}, "main moves")
    repo.record(1, commit=None, graph=built_there, files=["f.py"])
    scan = repo.scan()
    assert [b.source for b in scan.batches] == ["capture"]
    assert scan.unstamped == 1
    assert scan.active
    assert not any(b.failed for b in scan.batches)
    assert keeper not in _entries(scan)
    details = _detail(repo)
    assert NOTE not in details
    assert "1 record(s) have no usable commit to compare against — not checkable" in details


def test_a_build_missing_from_the_repository_leaves_the_record_unchecked(repo):
    _with_a_capture_record(repo)
    repo.record(1, commit=None, graph="d" * 40, files=["keep.py"])
    scan = repo.scan()
    assert [b.source for b in scan.batches] == ["capture"]
    assert scan.unstamped == 1
    assert not any(b.failed for b in scan.batches)
    assert NOTE not in _detail(repo)


def _fail_git(monkeypatch, subcommand: str) -> None:
    real = doctor_mod._run_git

    def failing(args, cwd, **kw):
        if args and args[0] == subcommand:
            raise ValueError(f"could not run git ({' '.join(args)}): synthetic failure")
        return real(args, cwd, **kw)

    monkeypatch.setattr(doctor_mod, "_run_git", failing)


def test_a_failed_ancestry_probe_is_a_failed_batch_and_the_cache_keeps_its_entry(repo, monkeypatch):
    build = repo.commit({"f.py": "a\n"})
    record = repo.record(1, commit=None, graph=build, files=["f.py"])
    repo.commit({"f.py": "b\n"})
    now = datetime(2026, 11, 16, tzinfo=UTC)
    first = build_cache(None, repo.scan(), now)
    assert first["by_commit"] == {f"graph:{build}": [record]}
    _fail_git(monkeypatch, "merge-base")
    scan = repo.scan()
    assert [(b.source, b.failed) for b in scan.batches] == [("graph", True)]
    assert scan.unstamped == 0
    assert NOTE in _detail(repo)
    second = build_cache(json.dumps(first), scan, now + timedelta(minutes=1))
    assert second["by_commit"] == {f"graph:{build}": [record]}


def test_an_exhausted_budget_is_a_failed_graph_batch(repo):
    build = repo.commit({"f.py": "a\n"})
    record = repo.record(1, commit=None, graph=build, files=["f.py"])
    repo.commit({"f.py": "b\n"})
    now = datetime(2026, 11, 16, tzinfo=UTC)
    first = build_cache(None, repo.scan(), now)
    repo.write_index()
    decisions, bindings, entities = repo.records()
    scan = doctor_mod._scan_code_drift(
        decisions, bindings, entities, repo.store, repo.root, deadline=0.0
    )
    assert [(b.source, b.failed) for b in scan.batches] == [("graph", True)]
    assert scan.unstamped == 0
    second = build_cache(json.dumps(first), scan, now + timedelta(minutes=1))
    assert second["by_commit"] == {f"graph:{build}": [record]}


def test_a_capture_batch_with_failing_git_is_still_a_failed_batch(repo, monkeypatch):
    commit = repo.commit({"f.py": "a\n"})
    repo.record(1, commit=commit, files=["f.py"])
    repo.commit({"f.py": "b\n"})
    _fail_git(monkeypatch, "diff")
    scan = repo.scan()
    assert [b.failed for b in scan.batches] == [True]
    assert scan.unstamped == 0
    assert NOTE in _detail(repo)


def test_a_store_whose_only_stamped_records_are_non_ancestor_builds_stays_silent(repo):
    base = repo.commit({"f.py": "a\n"})
    git(repo.root, "checkout", "-q", "-b", "other")
    built_there = repo.commit({"f.py": "b\n"}, "built on another branch")
    git(repo.root, "checkout", "-q", base)
    repo.record(1, commit=None, graph=built_there, files=["f.py"])
    scan = repo.scan()
    assert not scan.active
    assert scan.batches == []
    assert [d for d in _detail(repo)] == []


# -- T9 ---------------------------------------------------------------------------------------


def test_a_capture_batch_and_a_graph_batch_on_one_commit_stay_two_batches(repo):
    shared = repo.commit({"f.py": "a\n", "g.py": "a\n"})
    by_capture = repo.record(1, commit=shared, files=["f.py"])
    by_graph = repo.record(2, commit=None, graph=shared, files=["g.py"])
    repo.commit({"f.py": "b\n", "g.py": "b\n"})
    scan = repo.scan()
    assert sorted((b.commit, b.source, [e.record_id for e in b.entries]) for b in scan.batches) == [
        (shared, "capture", [by_capture]),
        (shared, "graph", [by_graph]),
    ]
    assert {e.record_id: e.commit_source for b in scan.batches for e in b.entries} == {
        by_capture: "capture",
        by_graph: "graph",
    }
    cache = build_cache(None, scan, datetime(2026, 11, 16, tzinfo=UTC))
    assert cache["by_commit"] == {shared: [by_capture], f"graph:{shared}": [by_graph]}


def _store_with(repo_root: Path) -> Store:
    # The store stays out of every commit, so a later checkout keeps the records.
    (repo_root / ".git" / "info" / "exclude").write_text(".sidegraph/\n", encoding="utf-8")
    return Store(repo_root / ".sidegraph")


def _decision(
    store: Store, file_path: str, name: str, *, commit: str | None, graph: str | None
) -> Decision:
    d = store.add_decision(
        Decision(
            title=f"About {name}",
            kind=DecisionKind.GOTCHA,
            context="c",
            choice="ch",
            status=DecisionStatus.ACCEPTED,
            valid_from=datetime.now(UTC),
            provenance=Provenance(
                source="agent",
                commit=commit,
                graph_version=None if graph is None else f"{graph}:0123456789ab",
            ),
        )
    )
    e = store.upsert_entity(
        Entity(canonical_name=name, descriptor=Descriptor(name=name, file_path=file_path))
    )
    store.add_binding(AnchorBinding(record_id=d.id, entity_id=e.entity_id, tier=2, status="live"))
    return d


def _commit(repo_root: Path, rel: str, content: str, message: str) -> str:
    (repo_root / rel).write_text(content, encoding="utf-8")
    git(repo_root, "add", "--", rel)
    git(repo_root, "commit", "-q", "-m", message)
    return git(repo_root, "rev-parse", "HEAD")


def test_the_refreshed_cache_lists_both_records_of_a_shared_commit(tmp_path):
    repo = GroupRepo(tmp_path)
    store = _store_with(repo.root)
    shared = repo.commit({"f.py": "a\n", "g.py": "a\n"}, "one")
    by_capture = _decision(store, "f.py", "F", commit=shared, graph=None)
    by_graph = _decision(store, "g.py", "G", commit=None, graph=shared)
    _commit(repo.root, "f.py", "b\n", "three")
    _commit(repo.root, "g.py", "b\n", "four")
    assert refresh_code_drift_cache(store) == 2
    cache = json.loads(store.get_meta(DRIFT_CACHE_KEY))
    assert cache["by_commit"] == {shared: [by_capture.id], f"graph:{shared}": [by_graph.id]}


# -- T12 --------------------------------------------------------------------------------------


def test_the_grouped_view_names_a_graph_build_and_keeps_the_json_contract(repo):
    build = repo.commit({"f.py": "a\n"})
    record = repo.record(1, commit=None, graph=build, files=["f.py"])
    repo.commit({"f.py": "b\n"})
    repo.commit({"f.py": "a\n"})  # touched and restored: the blobs are identical now
    _scan, groups = repo.build()
    assert groups is not None
    [entry] = groups.to_dict()["groups"][0]["records"]
    assert entry["record_id"] == record
    assert entry["commit_source"] == "graph"
    assert entry["capture_commit"] == build
    assert entry["diff"]["change"] == "none"
    capture = repo.record(2, commit=build, files=["f.py"])
    repo.commit({"f.py": "c\n"})
    _scan, groups = repo.build()
    assert groups is not None
    by_id = {r["record_id"]: r for g in groups.to_dict()["groups"] for r in g["records"]}
    assert by_id[capture]["commit_source"] == "capture"
    assert by_id[record]["commit_source"] == "graph"


def test_grouped_text_names_the_graph_build_beside_a_check(repo):
    build = repo.commit({"f.py": "a\n"})
    record = drift_groups.DriftRecordEntry(
        record_id=rid(1),
        kind="gotcha",
        title="t",
        files=["f.py"],
        basis="check",
        reason="checked files changed",
        check_id="CHK",
        checked_commit=build,
        capture_commit=build,
        commit_source="graph",
        diff={
            "command": None,
            "unavailable": None,
            "change": None,
            "old_blob": None,
            "new_blob": None,
            "old_mode": None,
            "new_mode": None,
        },
    )
    groups = drift_groups.DriftGroups(
        partial=False, notes=[], groups=[drift_groups.DriftGroup("f.py", [record])]
    )
    text = "\n".join(drift_groups.render_groups_text(groups))
    assert f"(check CHK, graph build {build[:8]})" in text
    as_capture = record.__class__(**{**record.__dict__, "commit_source": "capture"})
    groups = drift_groups.DriftGroups(
        partial=False, notes=[], groups=[drift_groups.DriftGroup("f.py", [as_capture])]
    )
    assert f"(check CHK, capture {build[:8]})" in "\n".join(drift_groups.render_groups_text(groups))


# -- T14 / T15 / T16 --------------------------------------------------------------------------


@pytest.fixture
def logs(monkeypatch) -> list[list[str]]:
    """Every ``git log`` the doctor module starts, as its argument list."""
    calls: list[list[str]] = []
    real = doctor_mod._run_git

    def counting(args, cwd, **kw):
        if args and args[0] == "log":
            calls.append(list(args))
        return real(args, cwd, **kw)

    monkeypatch.setattr(doctor_mod, "_run_git", counting)
    return calls


def test_the_memo_serves_a_graph_batch_only_at_the_head_it_was_computed_for(tmp_path, logs):
    repo = GroupRepo(tmp_path)
    store = _store_with(repo.root)
    build = _commit(repo.root, "f.py", "a\n", "build")
    _decision(store, "f.py", "F", commit=None, graph=build)
    assert refresh_code_drift_cache(store) == 0
    assert len(logs) == 1
    assert refresh_code_drift_cache(store) == 0
    assert len(logs) == 1, "a second scan at the same HEAD must reuse the stored answer"
    _commit(repo.root, "f.py", "b\n", "touch")
    _commit(repo.root, "f.py", "a\n", "restore")
    assert refresh_code_drift_cache(store) == 1
    assert len(logs) == 2, "a moved HEAD reruns the graph batch"
    assert refresh_code_drift_cache(store) == 1
    assert len(logs) == 2


def test_a_flagged_graph_record_is_tagged_drifted_in_retrieval(tmp_path):
    repo = GroupRepo(tmp_path)
    store = _store_with(repo.root)
    build = _commit(repo.root, "f.py", "a\n", "build")
    record = _decision(store, "f.py", "F", commit=None, graph=build)
    _commit(repo.root, "f.py", "b\n", "touch")
    assert refresh_code_drift_cache(store) == 1
    assert drifted_record_ids(store) == {record.id}
    entity = store.find_entity("F", "f.py")
    assert entity is not None
    ctx = rank_decisions([entity], [], [], store, RetrievalBudget())
    line = next(ln for ln in ctx.mistakes if record.id in ln)
    assert "[drifted]" in line


def test_a_cache_with_stamps_stamps_a_new_record_now_and_a_cold_one_leaves_its_age_unknown():
    earlier = datetime(2026, 11, 1, tzinfo=UTC)
    now = datetime(2026, 11, 16, tzinfo=UTC)
    established = {
        "first_seen_since": earlier.isoformat(),
        "first_seen": {"old": earlier.isoformat()},
        "first_seen_absent": {},
        "by_commit": {"c1": ["old"]},
    }
    _since, first_seen, _absent = stamp_cache(established, {"old", "new"}, set(), now)
    assert first_seen["old"] == "2026-11-01T00:00:00+00:00"
    assert first_seen["new"] == "2026-11-16T00:00:00+00:00"
    legacy = {"by_commit": {"c1": ["old"]}}  # a cache with no stamps at all
    _since, first_seen, _absent = stamp_cache(legacy, {"old", "new"}, set(), now)
    assert first_seen == {}
    _since, first_seen, _absent = stamp_cache(None, {"old", "new"}, set(), now)
    assert first_seen == {}


def test_a_record_that_left_the_scan_becomes_a_tombstone(repo):
    build = repo.commit({"f.py": "a\n"})
    record = repo.record(1, commit=None, graph=build, files=["f.py"])
    other = repo.commit({"g.py": "a\n"})
    repo.record(2, commit=other, files=["g.py"])
    now = datetime(2026, 11, 16, tzinfo=UTC)
    first = build_cache(None, repo.scan(), now)  # seeds the cache: nothing has drifted yet
    repo.commit({"f.py": "b\n"})
    second = build_cache(json.dumps(first), repo.scan(), now + timedelta(minutes=1))
    assert second["first_seen"] == {record: "2026-11-16T00:01:00+00:00"}
    # the build is no longer an ancestor of HEAD: the record is unchecked, its stamp a tombstone
    git(repo.root, "checkout", "-q", "--orphan", "fresh")
    git(repo.root, "rm", "-q", "-rf", "--cached", ".")
    repo.commit({"f.py": "z\n", "g.py": "z\n"}, "unrelated history")
    scan = repo.scan()
    assert scan.unstamped == 1
    third = build_cache(json.dumps(second), scan, now + timedelta(minutes=2))
    assert record not in third["first_seen"]
    assert record in third["first_seen_absent"]
