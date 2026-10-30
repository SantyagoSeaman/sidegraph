"""The check fold is planned inside ``plan_run``: read order, one packed read, opt-in only.

# see design/superpowers/specs/2026-10-29-packed-drift-checks-design.md (§6.1)
"""

# ruff: noqa: F811  (imported fixtures are redefined as test arguments)
from __future__ import annotations

import time
from pathlib import Path

import pytest

from sidegraph import check_fold, drift_checks, maintenance_clock, migrate
from sidegraph.maintenance import MaintenanceReport, Plan, plan_run
from sidegraph.store_format import inventory_from_disk
from tests.store_format2_support import (
    OLD,
    check_body,
    commit_all,
    decision_body,
    format1_committed,
    git_repo,  # noqa: F401  (pytest fixture)
    make_store,
    pin_version_0_22,  # noqa: F401  (pytest fixture)
    put_check,
    put_file,
    put_packed,
    validator,
)

CID = "01ARZ3NDEKTSV4RRFFQ69G5F01"


def committed_store(repo: Path) -> tuple[Path, Path]:
    store = make_store(repo)
    put_file(store, "decision", decision_body("D1", status="accepted"))
    put_check(store, check_body(CID, "D1"))
    commit_all(repo, "store", date=OLD)
    return repo, store


def run_plan(store: Path, *, fold_checks: bool = True) -> tuple[object, Plan]:
    wt = maintenance_clock.resolve_work_tree(store)
    head0 = maintenance_clock.read_head(wt)
    report = MaintenanceReport(dry_run=True)
    planned = plan_run(
        store,
        wt,
        head0,
        maintenance_clock.landing_times(wt),
        int(time.time()),
        settled_days=0,
        n=64,
        validate=validator,
        report=report,
        head_moved=lambda step: False,
        fold_checks=fold_checks,
    )
    assert planned is not None, report.stopped
    return planned


def test_migrate_never_plans_checks(
    git_repo: Path, pin_version_0_22: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args: object, **kwargs: object) -> object:
        raise AssertionError("migrate must not plan the check fold")

    monkeypatch.setattr(check_fold, "plan_check_fold", boom)
    store = format1_committed(git_repo)
    put_file(store, "decision", decision_body("D1", status="accepted"))
    put_check(store, check_body(CID, "D1"))
    commit_all(git_repo, "checks", date=OLD)
    report = migrate.migrate(store, dry_run=False, any_branch=True)
    assert report.stopped is None


def test_plan_without_fold_checks_has_no_check_plan(git_repo: Path, pin_version_0_22: None) -> None:
    _, store = committed_store(git_repo)
    _, plan = run_plan(store, fold_checks=False)
    assert plan.checks is None


def test_plan_with_fold_checks_packs_the_committed_check(
    git_repo: Path, pin_version_0_22: None
) -> None:
    _, store = committed_store(git_repo)
    _, plan = run_plan(store)
    assert plan.checks is not None
    assert plan.checks.packed == [f"verifications/{CID}.json"]


def test_check_inventory_read_before_dirty_set(
    git_repo: Path, pin_version_0_22: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, store = committed_store(git_repo)
    order: list[str] = []
    real_load = drift_checks.load_check_inventory
    real_dirty = maintenance_clock.dirty_paths

    def load(path: Path) -> object:
        order.append("load")
        return real_load(path)

    def dirty(*args: object, **kwargs: object) -> object:
        order.append("dirty")
        return real_dirty(*args, **kwargs)

    monkeypatch.setattr(drift_checks, "load_check_inventory", load)
    monkeypatch.setattr(maintenance_clock, "dirty_paths", dirty)
    run_plan(store)
    assert order == ["load", "dirty"]


def test_plan_reads_packed_once(
    git_repo: Path, pin_version_0_22: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, store = committed_store(git_repo)
    put_packed(store, [check_body("01ARZ3NDEKTSV4RRFFQ69G5F02", "D1")])
    commit_all(repo, "packed", date=OLD)
    opened: list[str] = []
    real_open = Path.open

    def counting(self: Path, *args: object, **kwargs: object) -> object:
        if self.name == "packed.shard":
            opened.append(str(self))
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", counting)
    _, plan = run_plan(store)
    monkeypatch.undo()
    assert len(opened) == 1
    assert plan.checks is not None
    assert plan.checks.current == (store / "verifications" / "packed.shard").read_bytes()


def test_outside_git_plan_keeps_every_file(git_repo: Path, pin_version_0_22: None) -> None:
    store = make_store(git_repo)
    put_file(store, "decision", decision_body("D1", status="accepted"))
    put_check(store, check_body(CID, "D1"))
    report = MaintenanceReport(dry_run=True)
    planned = plan_run(
        store,
        None,
        None,
        None,
        int(time.time()),
        settled_days=0,
        n=64,
        validate=validator,
        report=report,
        head_moved=lambda step: False,
        fold_checks=True,
    )
    assert planned is not None
    plan = planned[1]
    assert plan.checks is not None
    assert plan.checks.packed == [] and plan.checks.kept == [
        (f"verifications/{CID}.json", check_fold.OUTSIDE_GIT)
    ]


def test_maintain_accepts_fold_checks(git_repo: Path, pin_version_0_22: None) -> None:
    repo, store = committed_store(git_repo)
    from sidegraph.store import Store

    with Store(store) as s:
        report = s.maintain(settled_days=0, dry_run=True, any_branch=True, fold_checks=True)
    assert report.stopped is None
    assert inventory_from_disk(store, validate=validator) is not None


def test_a_linked_check_file_is_reported_kept(git_repo: Path, pin_version_0_22: None) -> None:
    """A check file that is a symlink is never read, and the fold report names it."""
    _, store = committed_store(git_repo)
    real = git_repo / "elsewhere.json"
    real.write_text("{}", encoding="utf-8")
    other = "01ARZ3NDEKTSV4RRFFQ69G5F02"
    try:
        (store / "verifications" / f"{other}.json").symlink_to(real)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    commit_all(git_repo, "link", date=OLD)
    _, plan = run_plan(store)
    assert plan.checks is not None
    assert (f"verifications/{other}.json", "not a regular file") in plan.checks.kept
    assert plan.checks.packed == [f"verifications/{CID}.json"]
