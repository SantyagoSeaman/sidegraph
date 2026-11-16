"""The graph baseline's git cost and edge contracts: the exact incremental update on a forward
HEAD move, user git configuration, committed deletions, stale cache entries, memo keys, and the
failure modes of the git calls.
# see design/superpowers/specs/2026-11-16-drift-graph-commit-fallback-design.md (D2, D3, Cost)
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from datetime import UTC, datetime, timedelta

import pytest

from sidegraph import doctor as doctor_mod
from sidegraph.drift_memo import entry_key
from sidegraph.retrieval import DRIFT_CACHE_KEY, drifted_record_ids
from sidegraph.sync import build_cache, refresh_code_drift_cache, refresh_code_drift_snapshot
from tests.drift_groups_support import GroupRepo, git
from tests.test_drift_graph_baseline import (
    _commit,
    _decision,
    _entries,
    _evil_merge,
    _store_with,
)


@pytest.fixture
def calls(monkeypatch) -> list[list[str]]:
    """Every git command the doctor module starts (its ``_run_git``), as an argument list."""
    seen: list[list[str]] = []
    real = doctor_mod._run_git

    def spy(args, cwd, **kw):
        seen.append(list(args))
        return real(args, cwd, **kw)

    monkeypatch.setattr(doctor_mod, "_run_git", spy)
    return seen


def _logs(seen: list[list[str]]) -> list[list[str]]:
    return [c for c in seen if c and c[0] == "log"]


def _range(command: list[str]) -> str:
    return next(arg for arg in command if ".." in arg and not arg.startswith("-"))


def _graph_store(tmp_path):
    repo = GroupRepo(tmp_path)
    store = _store_with(repo.root)
    return repo, store


# -- R1: the exact incremental update ---------------------------------------------------------


def test_a_forward_move_runs_one_shared_log_and_no_per_batch_probe(tmp_path, calls):
    repo, store = _graph_store(tmp_path)
    build = repo.commit({"f.py": "a\n", "g.py": "g\n"}, "build")
    _decision(store, "f.py", "F", commit=None, graph=build)
    _decision(store, "g.py", "G", commit=None, graph=build)
    _commit(repo.root, "pre.py", "p\n", "after the build")
    assert refresh_code_drift_cache(store) == 0
    first = git(repo.root, "rev-parse", "HEAD")
    calls.clear()
    second = _commit(repo.root, "unrelated.py", "u\n", "unrelated")
    assert refresh_code_drift_cache(store) == 0
    logs = _logs(calls)
    assert [_range(c) for c in logs] == [f"{first}..{second}"], calls
    # one probe of the memo's head against this one; none asks about a build commit
    probes = [c for c in calls if c[0] == "merge-base"]
    assert probes == [["merge-base", "--is-ancestor", first, second]], calls


def test_a_file_touched_and_restored_after_the_memo_is_flagged_incrementally(tmp_path, calls):
    repo, store = _graph_store(tmp_path)
    build = _commit(repo.root, "f.py", "a\n", "build")
    record = _decision(store, "f.py", "F", commit=None, graph=build)
    _commit(repo.root, "pre.py", "p\n", "after the build")
    assert refresh_code_drift_cache(store) == 0
    first = git(repo.root, "rev-parse", "HEAD")
    _commit(repo.root, "f.py", "b\n", "touch")
    second = _commit(repo.root, "f.py", "a\n", "restore")
    calls.clear()
    assert refresh_code_drift_cache(store) == 1
    assert drifted_record_ids(store) == {record.id}
    assert [_range(c) for c in _logs(calls)] == [f"{first}..{second}"]
    # the extended answer is stored: the next scan at this head runs no log at all
    calls.clear()
    assert refresh_code_drift_cache(store) == 1
    assert _logs(calls) == []


def test_a_move_to_a_sibling_history_falls_back_to_the_full_path(tmp_path, calls):
    repo, store = _graph_store(tmp_path)
    build = _commit(repo.root, "f.py", "a\n", "build")
    main = git(repo.root, "rev-parse", "--abbrev-ref", "HEAD")
    record = _decision(store, "f.py", "F", commit=None, graph=build)
    git(repo.root, "checkout", "-q", "-b", "sibling")
    sibling = _commit(repo.root, "g.py", "g\n", "sibling work")
    git(repo.root, "checkout", "-q", main)
    _commit(repo.root, "f.py", "b\n", "main touches f")
    assert refresh_code_drift_cache(store) == 1  # the memo now holds f.py at this head
    calls.clear()
    git(repo.root, "checkout", "-q", sibling)  # not a descendant of the memo's head
    assert refresh_code_drift_cache(store) == 0
    assert drifted_record_ids(store) == set()
    assert [_range(c) for c in _logs(calls)] == [f"{build}..{sibling}"], calls
    assert any(c[0] == "merge-base" and build in c for c in calls)
    assert record.id not in drifted_record_ids(store)


# -- R2: user git configuration ---------------------------------------------------------------


def test_log_diff_merges_off_in_the_repository_does_not_hide_an_evil_merge(tmp_path):
    repo = GroupRepo(tmp_path)
    git(repo.root, "config", "log.diffMerges", "off")
    git(repo.root, "config", "log.showSignature", "true")
    build = _evil_merge(repo)
    record = repo.record(1, commit=None, graph=build, files=["f.py"])
    assert _entries(repo.scan()) == {record: ["f.py"]}


# -- R3: a scan that goes inactive retires its graph entries ----------------------------------


def _unrelated_history(repo: GroupRepo) -> None:
    git(repo.root, "checkout", "-q", "--orphan", "unrelated")
    git(repo.root, "rm", "-q", "-rf", "--cached", ".")
    repo.commit({"f.py": "z\n"}, "new root")


def test_an_inactive_scan_retires_the_cached_graph_marker_and_its_stamp(tmp_path):
    repo, store = _graph_store(tmp_path)
    build = _commit(repo.root, "f.py", "a\n", "build")
    record = _decision(store, "f.py", "F", commit=None, graph=build)
    now = datetime(2026, 11, 16, tzinfo=UTC)
    refresh_code_drift_snapshot(store, now=now)  # seeds the cache
    _commit(repo.root, "f.py", "b\n", "touch")
    refresh_code_drift_snapshot(store, now=now + timedelta(minutes=1))
    assert drifted_record_ids(store) == {record.id}
    _unrelated_history(repo)
    refresh = refresh_code_drift_snapshot(store, now=now + timedelta(minutes=2))
    assert refresh is not None and refresh.total == 0
    cache = json.loads(store.get_meta(DRIFT_CACHE_KEY))
    assert cache["by_commit"] == {}
    assert record.id not in cache["first_seen"]
    assert record.id in cache["first_seen_absent"]
    assert drifted_record_ids(store) == set()


def test_retiring_graph_entries_leaves_the_capture_entries_alone(tmp_path):
    repo, store = _graph_store(tmp_path)
    build = _commit(repo.root, "f.py", "a\n", "build")
    capture = _decision(store, "f.py", "Fc", commit=build, graph=None)
    graph = _decision(store, "f.py", "Fg", commit=None, graph=build)
    _commit(repo.root, "f.py", "b\n", "touch")
    assert refresh_code_drift_cache(store) == 2
    path = store.path / "decisions" / f"{capture.id}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["status"] = "rejected"
    path.write_text(json.dumps(payload), encoding="utf-8")
    store.refresh_if_stale()
    _unrelated_history(repo)
    refresh_code_drift_cache(store)
    cache = json.loads(store.get_meta(DRIFT_CACHE_KEY))
    assert cache["by_commit"] == {build: [capture.id]}
    assert graph.id not in cache["first_seen"]


def test_an_inactive_scan_leaves_a_capture_only_cache_byte_untouched(tmp_path):
    repo, store = _graph_store(tmp_path)
    commit = _commit(repo.root, "f.py", "a\n", "build")
    record = _decision(store, "f.py", "F", commit=commit, graph=None)
    _commit(repo.root, "f.py", "b\n", "touch")
    assert refresh_code_drift_cache(store) == 1
    before = store.get_meta(DRIFT_CACHE_KEY)
    path = store.path / "decisions" / f"{record.id}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["status"] = "rejected"
    path.write_text(json.dumps(payload), encoding="utf-8")
    store.refresh_if_stale()
    assert refresh_code_drift_cache(store) == 0
    assert store.get_meta(DRIFT_CACHE_KEY) == before


# -- R4: committed deletions with uncomputed statuses -----------------------------------------


def _scan_without_index(repo: GroupRepo):
    decisions, bindings, entities = repo.records()
    return doctor_mod._scan_code_drift(decisions, bindings, entities, repo.store, repo.root)


def test_a_file_modified_then_deleted_is_not_flagged_when_statuses_are_uncomputed(tmp_path):
    repo = GroupRepo(tmp_path)
    build = repo.commit({"f.py": "a\n", "keep.py": "k\n"})
    record = repo.record(1, commit=None, graph=build, files=["f.py"])
    repo.commit({"f.py": "b\n"}, "modify")
    repo.commit({"f.py": None}, "delete")
    scan = _scan_without_index(repo)
    assert scan.binding_statuses_uncomputed
    assert [(b.source, b.failed) for b in scan.batches] == [("graph", False)]
    assert _entries(scan) == {}
    # with computed statuses a deletion is drift, as for a capture record
    assert _entries(repo.scan()) == {record: ["f.py"]}


def test_a_deleted_then_restored_file_is_flagged_when_statuses_are_uncomputed(tmp_path):
    repo = GroupRepo(tmp_path)
    build = repo.commit({"f.py": "a\n"})
    record = repo.record(1, commit=None, graph=build, files=["f.py"])
    repo.commit({"f.py": None}, "delete")
    repo.commit({"f.py": "a\n"}, "restore")
    assert _entries(_scan_without_index(repo)) == {record: ["f.py"]}


# -- R5: guards for the memo key, path decoding and git failures ------------------------------


def test_a_capture_and_a_graph_record_on_one_path_keep_their_own_memo_answers(tmp_path):
    repo, store = _graph_store(tmp_path)
    shared = _commit(repo.root, "f.py", "a\n", "build")
    by_capture = _decision(store, "f.py", "Fc", commit=shared, graph=None)
    by_graph = _decision(store, "f.py", "Fg", commit=None, graph=shared)
    _commit(repo.root, "f.py", "b\n", "touch")
    _commit(repo.root, "f.py", "a\n", "restore")  # a two-tree diff sees nothing
    for _ in range(3):
        refresh_code_drift_cache(store)
        assert drifted_record_ids(store) == {by_graph.id}, by_capture.id


def test_a_capture_memo_key_keeps_its_bare_shape_and_a_log_key_differs():
    commit, paths = "a" * 40, ["f.py"]
    legacy = hashlib.sha256(json.dumps([commit, paths, False]).encode("ascii")).hexdigest()
    assert entry_key(commit, paths, False) == legacy
    assert entry_key(commit, paths, False, "diff") == legacy
    assert entry_key(commit, paths, False, "log") != legacy


def _plumbing_commit(root, name: str, content: str, message: str) -> str:
    """Commit ``name`` through git plumbing, so a path that is not UTF-8 needs no filesystem
    that accepts one."""
    blob = git(root, "hash-object", "-w", "--stdin", input_text=content)
    subprocess.run(
        [
            "git",
            "update-index",
            "--add",
            "--cacheinfo",
            f"100644,{blob},".encode() + os.fsencode(name),
        ],
        cwd=root,
        check=True,
    )
    git(root, "commit", "-q", "-m", message)
    return git(root, "rev-parse", "HEAD")


def test_a_path_with_raw_bytes_is_decoded_like_the_capture_diff(tmp_path):
    name = os.fsdecode(b"a\xff.py")
    repo = GroupRepo(tmp_path)
    build = _plumbing_commit(repo.root, name, "a\n", "build")
    record = repo.record(1, commit=None, graph=build, files=[name])
    _plumbing_commit(repo.root, name, "b\n", "touch")
    scan = repo.scan()
    assert [(b.source, b.failed) for b in scan.batches] == [("graph", False)]
    assert _entries(scan) == {record: [name]}


def _stub_git(monkeypatch, subcommand: str, returncode: int) -> None:
    real = doctor_mod._run_git

    def stub(args, cwd, **kw):
        if args and args[0] == subcommand:
            return subprocess.CompletedProcess(args, returncode, b"", b"synthetic failure")
        return real(args, cwd, **kw)

    monkeypatch.setattr(doctor_mod, "_run_git", stub)


def test_a_non_zero_log_exit_is_a_failed_batch_that_keeps_the_cached_warning(tmp_path, monkeypatch):
    repo = GroupRepo(tmp_path)
    build = repo.commit({"f.py": "a\n"})
    record = repo.record(1, commit=None, graph=build, files=["f.py"])
    repo.commit({"f.py": "b\n"})
    now = datetime(2026, 11, 16, tzinfo=UTC)
    first = build_cache(None, repo.scan(), now)
    _stub_git(monkeypatch, "log", 128)
    scan = repo.scan()
    assert [(b.source, b.failed) for b in scan.batches] == [("graph", True)]
    second = build_cache(json.dumps(first), scan, now + timedelta(minutes=1))
    assert second["by_commit"] == {f"graph:{build}": [record]}


def test_an_exit_128_that_is_not_a_missing_commit_is_a_failed_batch(tmp_path, monkeypatch):
    repo = GroupRepo(tmp_path)
    build = repo.commit({"f.py": "a\n"})
    repo.record(1, commit=None, graph=build, files=["f.py"])
    repo.commit({"f.py": "b\n"})
    _stub_git(monkeypatch, "merge-base", 128)  # the build exists: cat-file -e succeeds
    scan = repo.scan()
    assert [(b.source, b.failed) for b in scan.batches] == [("graph", True)]
    assert scan.unstamped == 0


def test_a_build_that_names_a_blob_is_unchecked_not_a_failed_batch(tmp_path):
    repo = GroupRepo(tmp_path)
    keeper = repo.commit({"keep.py": "k\n", "f.py": "a\n"})
    repo.record(9, commit=keeper, files=["keep.py"])
    blob = git(repo.root, "rev-parse", "HEAD:f.py")
    repo.record(1, commit=None, graph=blob, files=["f.py"])
    scan = repo.scan()
    assert [(b.source, b.failed) for b in scan.batches] == [("capture", False)]
    assert scan.unstamped == 1
