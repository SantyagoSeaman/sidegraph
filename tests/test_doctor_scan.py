"""Drift→supersede wave D1: the structured ``scan_code_drift`` extraction from doctor's
``code-drift`` check — batches in finding order, total-deadline plumbing, head resolved
only in the load-inclusive wrapper. Fixture conventions copied from
``test_doctor_code_drift.py`` (same wave family).
# see design/superpowers/specs/2026-07-30-drift-supersede-affordance-design.md (D1)
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest
from ulid import ULID

from sidegraph import doctor as doctor_mod
from sidegraph.doctor import CODE_DRIFT, curate, scan_code_drift


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


def _drifted_repo(git_repo: Path) -> tuple[Path, str, str]:
    """One drifted decision anchored to trader/exec.py; returns (store_dir, rid, commit)."""
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
    return store_dir, rid, commit


# -- the structured scan --------------------------------------------------------------------


def test_scan_returns_batch_with_record_id_and_files(git_repo):
    store_dir, rid, commit = _drifted_repo(git_repo)
    scan = scan_code_drift(store_dir)
    assert scan.active
    assert not scan.repo_root_failed
    assert len(scan.batches) == 1
    batch = scan.batches[0]
    assert batch.commit == commit
    assert not batch.failed
    assert [e.record_id for e in batch.entries] == [rid]
    assert batch.entries[0].files == ["trader/exec.py"]
    assert batch.entries[0].record_path == str(store_dir / "decisions" / f"{rid}.json")


def test_scan_resolves_head_in_wrapper(git_repo):
    store_dir, _rid, _commit = _drifted_repo(git_repo)
    scan = scan_code_drift(store_dir)
    assert scan.head == _head(git_repo)


def test_scan_inactive_store_makes_no_git_call(tmp_path, monkeypatch):
    """active=False (nothing commit-stamped and anchored) short-circuits BEFORE any git
    subprocess — head stays None (spec N5/N6)."""
    store_dir = tmp_path / ".sidegraph"
    _decision(store_dir, _ulid(), commit=None)
    calls: list[list[str]] = []
    real = doctor_mod._run_git

    def counting(args, cwd, **kw):
        calls.append(args)
        return real(args, cwd, **kw)

    monkeypatch.setattr(doctor_mod, "_run_git", counting)
    scan = scan_code_drift(store_dir)
    assert not scan.active
    assert scan.head is None
    assert calls == []


def test_curate_git_call_count_unchanged_by_refactor(git_repo, monkeypatch):
    """GUARD (spec N6): the shared scan must not add a `rev-parse HEAD` to the curate()
    path — doctor's observable git-call count stays exactly what today's code makes:
    one root resolution + one diff per distinct commit on this fixture. Patches BOTH
    module namespaces (review M-3): root resolution used to run through
    `verify._find_repo_root`, invisible to a doctor-only counter."""
    import sidegraph.verify as verify_mod

    store_dir, _rid, _commit = _drifted_repo(git_repo)
    calls: list[list[str]] = []
    real = verify_mod._run_git

    def counting(args, cwd, **kw):
        calls.append(args)
        return real(args, cwd, **kw)

    monkeypatch.setattr(doctor_mod, "_run_git", counting)
    monkeypatch.setattr(verify_mod, "_run_git", counting)
    curate(store_dir)
    assert not any(args[:2] == ["rev-parse", "HEAD"] for args in calls)
    toplevel_calls = [args for args in calls if args[:2] == ["rev-parse", "--show-toplevel"]]
    diff_calls = [args for args in calls if args and args[0] == "diff"]
    assert len(toplevel_calls) == 1
    assert len(diff_calls) == 1


def test_wrapper_makes_exactly_three_bounded_git_calls(git_repo, monkeypatch):
    """Review I-1: the hook-path wrapper makes exactly root + diff + head — no duplicate
    root resolution — and EVERY call carries a timeout drawn from the one deadline."""
    import sidegraph.verify as verify_mod

    store_dir, _rid, _commit = _drifted_repo(git_repo)
    calls: list[tuple[list[str], object]] = []
    real = verify_mod._run_git

    def counting(args, cwd, **kw):
        calls.append((args, kw.get("timeout")))
        return real(args, cwd, **kw)

    monkeypatch.setattr(doctor_mod, "_run_git", counting)
    monkeypatch.setattr(verify_mod, "_run_git", counting)
    scan = scan_code_drift(store_dir, deadline=10.0)
    assert scan.head == _head(git_repo)
    kinds = [args[0] if args[0] != "rev-parse" else args[1] for args, _ in calls]
    assert kinds == ["--show-toplevel", "diff", "HEAD"]
    assert all(t is not None for _args, t in calls)


def test_findings_interleave_git_note_per_failed_batch(git_repo, monkeypatch):
    """GUARD (spec N7/I1): a failed batch's git-note is emitted IN PLACE, mid-list —
    green against today's code by construction; red against a naive count-based
    extraction that appends notes at the end. Two commits; the diff for the FIRST
    commit fails, the second succeeds."""
    store_dir = git_repo / ".sidegraph"
    src_a = git_repo / "a.py"
    src_a.write_text("a\n")
    _git(["add", "-A"], cwd=git_repo)
    _git(["commit", "-q", "-m", "c1"], cwd=git_repo)
    commit_a = _head(git_repo)
    src_b = git_repo / "b.py"
    src_b.write_text("b\n")
    _git(["add", "-A"], cwd=git_repo)
    _git(["commit", "-q", "-m", "c2"], cwd=git_repo)
    commit_b = _head(git_repo)

    rid_a, eid_a = _ulid(), _ulid()
    _decision(store_dir, rid_a, commit=commit_a)
    _entity(store_dir, eid_a, "A", "a.py")
    _bind(store_dir, rid_a, eid_a)
    rid_b, eid_b = _ulid(), _ulid()
    _decision(store_dir, rid_b, commit=commit_b)
    _entity(store_dir, eid_b, "B", "b.py")
    _bind(store_dir, rid_b, eid_b)

    for p in (src_a, src_b):
        p.write_text("changed\n")
    _git(["add", "-A"], cwd=git_repo)
    _git(["commit", "-q", "-m", "change both"], cwd=git_repo)

    real = doctor_mod._git_diff_name_only

    def failing_for_a(repo_root, commit, paths, **kw):
        if commit == commit_a:
            raise ValueError("simulated diff failure")
        return real(repo_root, commit, paths, **kw)

    monkeypatch.setattr(doctor_mod, "_git_diff_name_only", failing_for_a)
    report = curate(store_dir)
    drift = [f for f in report.findings if f.code == CODE_DRIFT]
    # by_commit iteration order is insertion order: commit_a first (its decision file
    # sorts first only by ULID chance — so locate positions by content instead).
    note_idx = next(i for i, f in enumerate(drift) if "git unavailable" in f.detail)
    b_idx = next(i for i, f in enumerate(drift) if "b.py" in f.detail)
    assert len(drift) == 2
    # The note replaces commit_a's findings at commit_a's position in iteration order.
    order_by_commit = [c for c, _ in _iter_by_commit_order([rid_a, rid_b], store_dir)]
    if order_by_commit == [commit_a, commit_b]:
        assert note_idx < b_idx
    else:
        assert b_idx < note_idx


def _iter_by_commit_order(rids: list[str], store_dir: Path) -> list[tuple[str, str]]:
    """(commit, rid) in the order doctor's decision iteration (path-sorted) first sees each
    commit — mirrors `_iter_records`' path sort + by_commit insertion order."""
    pairs = []
    for path in sorted((store_dir / "decisions").glob("*.json")):
        payload = json.loads(path.read_text())
        commit = (payload.get("provenance") or {}).get("commit")
        if commit:
            pairs.append((commit, payload["id"]))
    seen: set[str] = set()
    out = []
    for commit, rid in pairs:
        if commit not in seen:
            seen.add(commit)
            out.append((commit, rid))
    return out


# -- deadline -------------------------------------------------------------------------------


def test_deadline_marks_hung_batch_failed_and_skips_unrun(git_repo, monkeypatch):
    """A per-scan total deadline: a git call that outlives the remaining budget becomes a
    failed batch, and batches that never got to run are marked failed too (spec N2)."""
    store_dir = git_repo / ".sidegraph"
    for name in ("a.py", "b.py"):
        (git_repo / name).write_text("x\n")
        _git(["add", "-A"], cwd=git_repo)
        _git(["commit", "-q", "-m", name], cwd=git_repo)
        commit = _head(git_repo)
        rid, eid = _ulid(), _ulid()
        _decision(store_dir, rid, commit=commit)
        _entity(store_dir, eid, name, name)
        _bind(store_dir, rid, eid)
    (git_repo / "a.py").write_text("changed\n")
    (git_repo / "b.py").write_text("changed\n")
    _git(["add", "-A"], cwd=git_repo)
    _git(["commit", "-q", "-m", "change"], cwd=git_repo)

    real = doctor_mod._run_git
    slow_done = {"n": 0}

    def slow(args, cwd, **kw):
        if args and args[0] == "diff":
            slow_done["n"] += 1
            # Simulate a hang that outlives the remaining budget: a real subprocess would
            # be killed at `timeout=`; the mock burns wall-clock past the deadline first
            # so the NEXT batch sees an exhausted budget.
            time.sleep(0.05)
            raise subprocess.TimeoutExpired(cmd=["git", *args], timeout=kw.get("timeout") or 0)
        return real(args, cwd, **kw)

    monkeypatch.setattr(doctor_mod, "_run_git", slow)
    scan = scan_code_drift(store_dir, deadline=0.02)
    assert scan.active
    assert len(scan.batches) == 2
    assert all(b.failed for b in scan.batches)
    # Deadline exhaustion must not run every batch's subprocess: after the first timeout
    # consumed the whole budget, the second batch is marked failed WITHOUT running.
    assert slow_done["n"] == 1


def test_no_deadline_means_no_timeout_kw(git_repo, monkeypatch):
    """curate()/CLI pass no deadline — subprocesses run without a timeout, exactly
    today's behavior. Patches both namespaces (review M-3) so no call is invisible."""
    import sidegraph.verify as verify_mod

    store_dir, _rid, _commit = _drifted_repo(git_repo)
    seen_kw: list[dict] = []
    real = verify_mod._run_git

    def spying(args, cwd, **kw):
        seen_kw.append(kw)
        return real(args, cwd, **kw)

    monkeypatch.setattr(doctor_mod, "_run_git", spying)
    monkeypatch.setattr(verify_mod, "_run_git", spying)
    curate(store_dir)
    assert seen_kw  # the spy actually saw the calls
    assert all(kw.get("timeout") is None for kw in seen_kw)
