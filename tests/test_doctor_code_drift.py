"""Staleness machinery D5: doctor's ``code-drift`` advisory check — a LIVE decision's
anchored file changed since its capture-time HEAD. Stores are hand-built minimal canonical
JSON files, same convention as ``test_doctor.py``; git-backed tests use a real ``tmp_path``
repo, same convention as ``test_verify_transitions.py``'s git-backed integration group.
# see design/superpowers/specs/2026-07-30-staleness-machinery-design.md (D5)
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest
from ulid import ULID

from sidegraph.doctor import CODE_DRIFT, curate


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


def _write(root: Path, rel: str, payload: object) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload), encoding="utf-8")


def _ulid() -> str:
    return str(ULID())


def _decision(root: Path, rid: str, *, commit: str | None, status: str = "accepted") -> None:
    _write(
        root,
        f"decisions/{rid}.json",
        {
            "id": rid,
            "status": status,
            "valid_to": None,
            "provenance": {"source": "agent", "commit": commit},
        },
    )


def _entity(root: Path, eid: str, name: str, file_path: str) -> None:
    _write(
        root,
        f"entities/{eid}.json",
        {
            "entity_id": eid,
            "canonical_name": name,
            "descriptor": {"name": name, "file_path": file_path},
        },
    )


def _bind(root: Path, rid: str, entity_id: str) -> None:
    _write(root, f"bindings/{rid}.json", [{"entity_id": entity_id, "tier": 2, "weight": 1.0}])


def _index_with_bindings(root: Path, rows: list[tuple[str, str, str]]) -> None:
    conn = sqlite3.connect(root / "index.db")
    conn.execute("CREATE TABLE anchor_bindings (record_id TEXT, entity_id TEXT, data TEXT)")
    for record_id, entity_id, status in rows:
        conn.execute(
            "INSERT INTO anchor_bindings VALUES (?, ?, ?)",
            (record_id, entity_id, json.dumps({"status": status})),
        )
    conn.commit()
    conn.close()


def _codes(report) -> list[str]:
    return [f.code for f in report.findings]


# -- finding on a moved/changed anchored file -----------------------------------------------


def test_code_drift_finding_on_file_changed_since_capture(git_repo):
    store_dir = git_repo / ".sidegraph"
    src = git_repo / "trader" / "exec.py"
    src.parent.mkdir(parents=True)
    src.write_text("original\n")
    _git(["add", "-A"], cwd=git_repo)
    _git(["commit", "-q", "-m", "initial"], cwd=git_repo)
    commit = _head(git_repo)

    rid, eid = _ulid(), _ulid()
    _decision(store_dir, rid, commit=commit)
    _entity(store_dir, eid, "Trader", "trader/exec.py")
    _bind(store_dir, rid, eid)

    src.write_text("changed\n")
    _git(["add", "-A"], cwd=git_repo)
    _git(["commit", "-q", "-m", "change trader"], cwd=git_repo)

    report = curate(store_dir)
    assert CODE_DRIFT in _codes(report)
    drift = next(
        f for f in report.findings if f.code == CODE_DRIFT and "trader/exec.py" in f.detail
    )
    assert commit[:8] in drift.detail
    assert drift.path == str(store_dir / "decisions" / f"{rid}.json")


def test_code_drift_no_finding_when_anchored_file_unchanged(git_repo):
    store_dir = git_repo / ".sidegraph"
    src = git_repo / "trader" / "exec.py"
    src.parent.mkdir(parents=True)
    src.write_text("original\n")
    _git(["add", "-A"], cwd=git_repo)
    _git(["commit", "-q", "-m", "initial"], cwd=git_repo)
    commit = _head(git_repo)

    rid, eid = _ulid(), _ulid()
    _decision(store_dir, rid, commit=commit)
    _entity(store_dir, eid, "Trader", "trader/exec.py")
    _bind(store_dir, rid, eid)

    # A second, unrelated commit -- trader/exec.py itself never changes.
    (git_repo / "other.py").write_text("x\n")
    _git(["add", "-A"], cwd=git_repo)
    _git(["commit", "-q", "-m", "unrelated"], cwd=git_repo)

    report = curate(store_dir)
    assert CODE_DRIFT not in _codes(report)


def test_terminal_decision_is_not_a_drift_target(git_repo):
    """Only LIVE (proposed/accepted) decisions are drift targets -- a superseded one
    predates a rewrite for a reason unrelated to this check's charter."""
    store_dir = git_repo / ".sidegraph"
    src = git_repo / "trader" / "exec.py"
    src.parent.mkdir(parents=True)
    src.write_text("original\n")
    _git(["add", "-A"], cwd=git_repo)
    _git(["commit", "-q", "-m", "initial"], cwd=git_repo)
    commit = _head(git_repo)

    rid, eid = _ulid(), _ulid()
    _decision(store_dir, rid, commit=commit, status="superseded")
    _entity(store_dir, eid, "Trader", "trader/exec.py")
    _bind(store_dir, rid, eid)

    src.write_text("changed\n")
    _git(["add", "-A"], cwd=git_repo)
    _git(["commit", "-q", "-m", "change"], cwd=git_repo)

    report = curate(store_dir)
    assert CODE_DRIFT not in _codes(report)


# -- unstamped records: counted once alongside real drift, silent when the store has no --
# -- commit-stamped decision at all (STOP-and-report: see the report to team-lead) --------


def test_store_with_no_commit_stamped_decision_at_all_is_silent(tmp_path):
    """Every pre-existing store (created before this wave shipped) has ZERO commit-stamped
    decisions -- an unconditional "N record(s) predate commit stamping" note would cost
    every one of them a clean sidegraph-doctor run forever, which contradicts the
    pre-existing CLI contract (test_cli_doctor.py's healthy-store fixtures, uncommented,
    have no ``provenance.commit`` and must stay ``clean``). The check stays a complete
    no-op until at least one commit-stamped, anchored decision exists to actually check."""
    store_dir = tmp_path / ".sidegraph"
    for _ in range(3):
        _decision(store_dir, _ulid(), commit=None)
    report = curate(store_dir, repo_root=tmp_path)
    assert not any(f.code == CODE_DRIFT for f in report.findings)


def test_unstamped_and_drift_findings_coexist(git_repo):
    store_dir = git_repo / ".sidegraph"
    src = git_repo / "trader" / "exec.py"
    src.parent.mkdir(parents=True)
    src.write_text("original\n")
    _git(["add", "-A"], cwd=git_repo)
    _git(["commit", "-q", "-m", "initial"], cwd=git_repo)
    commit = _head(git_repo)

    rid, eid = _ulid(), _ulid()
    _decision(store_dir, rid, commit=commit)
    _entity(store_dir, eid, "Trader", "trader/exec.py")
    _bind(store_dir, rid, eid)
    _decision(store_dir, _ulid(), commit=None)  # pre-wave record, no commit

    src.write_text("changed\n")
    _git(["add", "-A"], cwd=git_repo)
    _git(["commit", "-q", "-m", "change trader"], cwd=git_repo)

    report = curate(store_dir)
    findings = [f for f in report.findings if f.code == CODE_DRIFT]
    assert len(findings) == 2
    assert any("1 record(s) predate commit stamping" in f.detail for f in findings)
    assert any("trader/exec.py" in f.detail for f in findings)


# -- git-unavailable: skipped-note finding, never a crash, never a silent zero ---------------


def test_gitless_store_yields_skipped_note_never_silent_zero(tmp_path):
    """`store_dir` sits outside any git repository -- `_find_repo_root` fails, and that
    failure must surface as a Finding (never a crash, never an empty findings list that
    reads as a clean pass -- design ruling, the C1 silent-zero shape rejected)."""
    store_dir = tmp_path / ".sidegraph"
    rid, eid = _ulid(), _ulid()
    _decision(store_dir, rid, commit="deadbeef")
    _entity(store_dir, eid, "Trader", "trader/exec.py")
    _bind(store_dir, rid, eid)

    report = curate(store_dir)  # no repo_root override -- must resolve (and fail) itself
    findings = [f for f in report.findings if f.code == CODE_DRIFT]
    assert len(findings) == 1
    assert "git unavailable" in findings[0].detail


def test_gitless_store_with_only_unstamped_records_stays_silent(tmp_path):
    """No commit-stamped, anchored decision at all -> `by_commit` stays empty -> the whole
    check is a no-op (see the "silent when nothing stamped" test above), never touching
    git at all -- so a store outside any git repo doesn't even hit the git-unavailable
    path here, let alone raise."""
    store_dir = tmp_path / ".sidegraph"
    _decision(store_dir, _ulid(), commit=None)
    report = curate(store_dir)
    assert not any(f.code == CODE_DRIFT for f in report.findings)


# -- orphaned-binding exclusion ---------------------------------------------------------------


def test_orphaned_binding_excluded_from_code_drift(git_repo):
    store_dir = git_repo / ".sidegraph"
    src = git_repo / "trader" / "exec.py"
    src.parent.mkdir(parents=True)
    src.write_text("original\n")
    _git(["add", "-A"], cwd=git_repo)
    _git(["commit", "-q", "-m", "initial"], cwd=git_repo)
    commit = _head(git_repo)

    rid, eid = _ulid(), _ulid()
    _decision(store_dir, rid, commit=commit)
    _entity(store_dir, eid, "Trader", "trader/exec.py")
    _bind(store_dir, rid, eid)
    _index_with_bindings(store_dir, [(rid, eid, "orphaned")])

    src.write_text("changed\n")
    _git(["add", "-A"], cwd=git_repo)
    _git(["commit", "-q", "-m", "change trader"], cwd=git_repo)

    report = curate(store_dir)
    assert not any(f.code == CODE_DRIFT and "trader/exec.py" in f.detail for f in report.findings)


def test_degraded_binding_still_included_in_code_drift(git_repo):
    """Only ORPHANED is excluded -- a degraded binding is still live enough to check (same
    live/degraded-vs-orphaned split ``valid_decisions_for_entity`` itself draws)."""
    store_dir = git_repo / ".sidegraph"
    src = git_repo / "trader" / "exec.py"
    src.parent.mkdir(parents=True)
    src.write_text("original\n")
    _git(["add", "-A"], cwd=git_repo)
    _git(["commit", "-q", "-m", "initial"], cwd=git_repo)
    commit = _head(git_repo)

    rid, eid = _ulid(), _ulid()
    _decision(store_dir, rid, commit=commit)
    _entity(store_dir, eid, "Trader", "trader/exec.py")
    _bind(store_dir, rid, eid)
    _index_with_bindings(store_dir, [(rid, eid, "degraded")])

    src.write_text("changed\n")
    _git(["add", "-A"], cwd=git_repo)
    _git(["commit", "-q", "-m", "change trader"], cwd=git_repo)

    report = curate(store_dir)
    assert any(f.code == CODE_DRIFT and "trader/exec.py" in f.detail for f in report.findings)


# -- appended at the END of curate()'s listing order -----------------------------------------


def test_code_drift_finding_appended_after_every_other_check(git_repo):
    store_dir = git_repo / ".sidegraph"
    src = git_repo / "trader" / "exec.py"
    src.parent.mkdir(parents=True)
    src.write_text("original\n")
    _git(["add", "-A"], cwd=git_repo)
    _git(["commit", "-q", "-m", "initial"], cwd=git_repo)
    commit = _head(git_repo)

    rid, eid = _ulid(), _ulid()
    _decision(store_dir, rid, commit=commit)
    _entity(store_dir, eid, "Trader", "trader/exec.py")
    _bind(store_dir, rid, eid)
    # Also an unrelated dangling decision, to guarantee at least one earlier-listed finding.
    _decision(store_dir, _ulid(), commit=None)

    src.write_text("changed\n")
    _git(["add", "-A"], cwd=git_repo)
    _git(["commit", "-q", "-m", "change trader"], cwd=git_repo)

    report = curate(store_dir)
    assert report.findings[-1].code == CODE_DRIFT
