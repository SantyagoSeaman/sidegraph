"""``record_drift_check`` on a record whose only baseline is the graph's build commit.

Ledger T10, T11, T18 and the checked-finding half of T12.
# see design/superpowers/specs/2026-11-16-drift-graph-commit-fallback-design.md (D4, D5)
"""

from __future__ import annotations

import json

import pytest

from sidegraph.doctor import CODE_DRIFT, curate, scan_code_drift
from sidegraph.retrieval import drifted_record_ids
from sidegraph.sync import refresh_code_drift_cache
from tests.drift_checks_support import (  # noqa: F401
    drift_ids,
    git,
    project,
    record_check,
    require_check,
)
from tests.test_drift_checks import commit_file

# ruff: noqa: F811  (the imported ``project`` fixture is redefined as a test argument)


def graph_only(store, decision, graph_oid: str | None) -> None:
    """Rewrite the record's provenance: no capture commit, the given graph build commit."""
    path = store.path / "decisions" / f"{decision.id}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["provenance"]["commit"] = None
    payload["provenance"]["graph_version"] = None if graph_oid is None else f"{graph_oid}:abc123"
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    store.refresh_if_stale()


def first_commit(root) -> str:
    return git(root, "rev-list", "--max-parents=0", "HEAD")


# -- T10 --------------------------------------------------------------------------------------


def test_a_graph_baseline_record_can_be_checked_and_rearms_on_a_later_change(project):
    root, store, decision = project
    graph_only(store, decision, first_commit(root))
    assert drift_ids(store) == {decision.id}
    assert refresh_code_drift_cache(store) == 1
    assert drifted_record_ids(store) == {decision.id}

    require_check(root, decision.id)
    assert drift_ids(store) == set()
    assert drifted_record_ids(store) == set()

    commit_file(root, "code.py", "def policy():\n    return 2\n")
    assert drift_ids(store) == {decision.id}
    assert refresh_code_drift_cache(store) == 1


# -- T11 / T18 --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "graph_version",
    [None, "content:abc123", "a" * 39 + ":abc", "A" * 40 + ":abc", "a" * 40],
)
def test_a_record_with_no_usable_baseline_is_still_refused(project, graph_version):
    root, store, decision = project
    graph_only(store, decision, None)
    if graph_version is not None:
        path = store.path / "decisions" / f"{decision.id}.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["provenance"]["graph_version"] = graph_version
        path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        store.refresh_if_stale()
    result = record_check(root, decision.id)
    assert result.is_error
    assert not (store.path / "verifications").exists()


def test_a_graph_build_that_is_not_an_ancestor_of_head_is_refused(project):
    root, store, decision = project
    main = git(root, "rev-parse", "--abbrev-ref", "HEAD")
    git(root, "checkout", "-q", "-b", "elsewhere")
    commit_file(root, "code.py", "def policy():\n    return 3\n")
    built_there = git(root, "rev-parse", "HEAD")
    git(root, "checkout", "-q", main)
    graph_only(store, decision, built_there)
    result = record_check(root, decision.id)
    assert result.is_error
    assert not (store.path / "verifications").exists()


def test_a_graph_build_missing_from_the_repository_is_refused(project):
    root, store, decision = project
    graph_only(store, decision, "d" * 40)
    result = record_check(root, decision.id)
    assert result.is_error
    assert not (store.path / "verifications").exists()


# -- T12: the checked finding -----------------------------------------------------------------


def _code_drift_details(store) -> list[str]:
    return [f.detail for f in curate(store.path).findings if f.code == CODE_DRIFT]


def test_a_checked_graph_record_that_changed_again_names_the_graph_build(project):
    root, store, decision = project
    build = first_commit(root)
    graph_only(store, decision, build)
    check = require_check(root, decision.id)
    commit_file(root, "code.py", "def policy():\n    return 2\n")
    [detail] = [d for d in _code_drift_details(store) if "checked files changed" in d]
    assert (
        f"(graph build {build[:8]}, check {check['id']}, checked {check['checked_commit'][:8]})"
        in detail
    )
    entry = next(e for b in scan_code_drift(store.path).batches for e in b.entries)
    assert entry.commit_source == "graph"
    assert entry.basis == "check"


def test_a_checked_graph_record_whose_subject_changed_names_the_graph_build(project):
    root, store, decision = project
    build = first_commit(root)
    graph_only(store, decision, build)
    require_check(root, decision.id)
    path = store.path / "decisions" / f"{decision.id}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["choice"] = "A different choice, so the checked subject no longer matches."
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    [detail] = [d for d in _code_drift_details(store) if "checked subject" in d]
    assert f"(graph build {build[:8]}, check " in detail
    entry = next(e for b in scan_code_drift(store.path).batches for e in b.entries)
    assert entry.commit_source == "graph"
    assert entry.basis == "scope"
