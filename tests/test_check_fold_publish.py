"""The check-fold publisher: raise and flush the requirement, publish the packed image, then
unlink the folded check files, each step only after the one before is durable.

# see design/superpowers/specs/2026-10-29-packed-drift-checks-design.md (§6.3, §6.4, §10)
"""

# ruff: noqa: F811  (imported fixtures are redefined as test arguments)
from __future__ import annotations

import os
import shutil
import threading
from pathlib import Path

import pytest

import sidegraph
from sidegraph import maintenance, migrate
from sidegraph.maintenance import MaintenanceReport
from sidegraph.store import Store
from sidegraph.store_format import marker_text
from sidegraph.store_layout import StoreRequirementError
from sidegraph.verify import verify_snapshot
from tests.store_format2_support import (
    OLD,
    Killed,
    archived_decision,
    check_body,
    commit_all,
    decision_body,
    format1_committed,
    git,
    git_repo,  # noqa: F401  (pytest fixture)
    make_store,
    pin_version_0_22,  # noqa: F401  (pytest fixture)
    put_archive,
    put_check,
    put_file,
    put_packed,
    record_checkpoints,
    record_fsyncs,
    record_publishes,
    run_with_kill,
)

C1 = "01ARZ3NDEKTSV4RRFFQ69G5F01"
C2 = "01ARZ3NDEKTSV4RRFFQ69G5F02"
C3 = "01ARZ3NDEKTSV4RRFFQ69G5F03"
REL1 = f"verifications/{C1}.json"
REL2 = f"verifications/{C2}.json"
PACKED = "verifications/packed.shard"


def stage_hook(monkeypatch: pytest.MonkeyPatch, label: str, action) -> list[str]:
    """Run ``action()`` once, the first time the run reaches ``stage(label)``."""
    fired: list[str] = []
    real = maintenance.stage

    def hook(seen: str) -> None:
        real(seen)
        if seen == label and not fired:
            fired.append(seen)
            action()

    monkeypatch.setattr(maintenance, "stage", hook)
    return fired


def committed(repo: Path, *, checks=((C1, "D1"),), packed=None, extra=None) -> Path:
    """A committed format-2 store: accepted decision D1, the given check files and packed lines."""
    store = make_store(repo)
    put_file(store, "decision", decision_body("D1", status="accepted"))
    for cid, did in checks:
        put_check(store, check_body(cid, did))
    if packed is not None:
        put_packed(store, packed)
    if extra is not None:
        extra(store)
    commit_all(repo, "store", date=OLD)
    return store


def fold(store: Path) -> MaintenanceReport:
    with Store(store) as s:
        return s.maintain(settled_days=0, dry_run=False, any_branch=True, fold_checks=True)


def verifications(store: Path) -> dict[str, bytes]:
    folder = store / "verifications"
    if not folder.is_dir():
        return {}
    return {p.name: p.read_bytes() for p in sorted(folder.iterdir()) if p.is_file()}


def new_commit(repo: Path) -> None:
    git(repo, "commit", "-q", "--allow-empty", "-m", "moved")


def test_fold_packs_and_unlinks(git_repo: Path, pin_version_0_22: None) -> None:  # A1, R9
    store = committed(git_repo)
    report = fold(store)
    assert report.stopped is None and report.checks_stopped is None
    assert report.checks_packed == [REL1]
    assert not (store / REL1).exists()
    assert (store / PACKED).read_bytes().count(b"\n") == 1
    assert report.requirement_raised == "0.22.0"
    assert "requires-sidegraph >= 0.22.0" in (store / "format").read_text()
    assert {"format", PACKED, REL1} <= set(report.wrote)
    assert verify_snapshot(store) == []


def test_archived_terminal_drops(git_repo: Path, pin_version_0_22: None) -> None:  # A5
    def extra(store: Path) -> None:
        put_archive(store, "seg1", [archived_decision("ARCH1")])
        put_check(store, check_body(C1, "ARCH1"))

    store = committed(git_repo, checks=(), extra=extra)
    report = fold(store)
    assert report.checks_dropped == [REL1]
    assert not (store / REL1).exists()
    assert not (store / PACKED).exists()


def test_raise_is_flushed_before_publish(
    git_repo: Path, pin_version_0_22: None, monkeypatch: pytest.MonkeyPatch
) -> None:  # P1
    store = committed(git_repo)
    calls = record_publishes(monkeypatch, store)
    seen: list[bool] = []
    stage_hook(monkeypatch, "checks:after_raise", lambda: seen.append((store / PACKED).exists()))
    fold(store)
    assert seen == [False]
    replaced = calls.index(("replace", "format"))
    flushed = calls.index(("fsync_tmp", "format"), replaced)
    synced = calls.index(("fsync_dir", "store"), flushed)
    assert synced < calls.index(("replace", PACKED))


def test_packed_changed_between_plan_and_publish(
    git_repo: Path, pin_version_0_22: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = committed(git_repo, checks=((C2, "D1"),), packed=[check_body(C1, "D1")])
    extra = check_body("01ARZ3NDEKTSV4RRFFQ69G5F04", "D1")

    def edit() -> None:
        put_packed(store, [check_body(C1, "D1"), extra])

    stage_hook(monkeypatch, "checks:after_raise", edit)
    report = fold(store)
    assert report.checks_stopped is not None and report.stopped is None
    assert b"F04" in (store / PACKED).read_bytes()
    assert (store / REL2).exists()


def test_no_work_no_raise(git_repo: Path, pin_version_0_22: None) -> None:  # P2 (passes before)
    store = committed(git_repo, checks=())
    before = (store / "format").read_bytes()
    report = fold(store)
    assert (store / "format").read_bytes() == before
    assert report.requirement_raised is None and report.checks_stopped is None


def test_higher_requirement_untouched(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:  # P3
    monkeypatch.setattr(sidegraph, "__version__", "0.23.0")
    store = committed(git_repo)
    (store / "format").write_text(marker_text(2, "0.23.0", 64), encoding="utf-8")
    commit_all(git_repo, "marker", date=OLD)
    before = (store / "format").read_bytes()
    report = fold(store)
    assert (store / "format").read_bytes() == before
    assert report.checks_packed == [REL1] and report.checks_stopped is None
    assert not (store / REL1).exists()


def test_migrate_leaves_checks(
    git_repo: Path, pin_version_0_22: None
) -> None:  # M1 (passes before)
    store = format1_committed(git_repo)
    put_check(store, check_body(C1, "D2"))
    commit_all(git_repo, "check", date=OLD)
    before = verifications(store)
    migrate.migrate(store, dry_run=False, any_branch=True)
    assert verifications(store) == before
    assert "requires-sidegraph >= 0.17.0" in (store / "format").read_text()


def test_e2e_dirty_and_untracked_kept(git_repo: Path, pin_version_0_22: None) -> None:  # A2
    store = committed(git_repo, checks=((C1, "D1"), (C3, "D1")))
    put_check(store, check_body(C3, "D1", rationale="edited"))  # dirty
    put_check(store, check_body(C2, "D1"))  # untracked
    report = fold(store)
    assert report.checks_packed == [REL1]
    assert (REL2, "untracked or modified") in report.checks_kept
    assert (f"verifications/{C3}.json", "untracked or modified") in report.checks_kept
    assert (store / REL2).exists()


def test_e2e_dirty_superseder_does_not_drop(git_repo: Path, pin_version_0_22: None) -> None:  # A4
    store = committed(git_repo)
    put_check(store, check_body(C2, "D1"))  # newer, untracked
    report = fold(store)
    assert report.checks_packed == [REL1] and report.checks_dropped == []
    assert C1.encode() in (store / PACKED).read_bytes()


def test_e2e_untracked_packed_shard_does_not_drop(git_repo: Path, pin_version_0_22: None) -> None:
    """The real dirty-path scan sees an untracked packed.shard: its newer check for the same
    decision kills nothing, so the committed check is packed, not dropped."""
    store = make_store(git_repo)
    put_file(store, "decision", decision_body("D1", status="accepted"))
    put_check(store, check_body(C1, "D1"))
    commit_all(git_repo, "s", date=OLD)
    put_packed(store, [check_body(C2, "D1")])
    report = fold(store)
    assert report.checks_packed == [REL1] and report.checks_dropped == []
    assert C1.encode() in (store / PACKED).read_bytes()
    assert verify_snapshot(store) == []


def test_e2e_modified_packed_shard_does_not_drop(git_repo: Path, pin_version_0_22: None) -> None:
    """The same through a committed packed.shard modified in the working tree."""
    store = make_store(git_repo)
    put_file(store, "decision", decision_body("D1", status="accepted"))
    put_file(store, "decision", decision_body("D2", status="accepted"))
    put_check(store, check_body(C1, "D1"))
    put_packed(store, [check_body(C3, "D2")])
    commit_all(git_repo, "s", date=OLD)
    put_packed(store, [check_body(C3, "D2"), check_body(C2, "D1")])
    report = fold(store)
    assert report.checks_packed == [REL1] and report.checks_dropped == []
    assert C1.encode() in (store / PACKED).read_bytes()


def test_e2e_dirty_decision_does_not_drop(git_repo: Path, pin_version_0_22: None) -> None:  # A6
    store = committed(git_repo)
    put_file(
        store,
        "decision",
        decision_body("D1", status="rejected", valid_to="2026-02-01T00:00:00Z"),
    )  # an uncommitted rejection
    report = fold(store)
    assert report.checks_packed == [REL1] and report.checks_dropped == []


@pytest.mark.parametrize("kind", ["damaged", "fifo", "directory"])
def test_blocking_line_keeps_record_fold(
    git_repo: Path, pin_version_0_22: None, kind: str
) -> None:  # A8
    def extra(store: Path) -> None:
        put_file(store, "decision", decision_body("D9", status="accepted"))
        if kind == "damaged":
            put_packed(store, [b"{not json"])

    store = committed(git_repo, extra=extra)
    if kind == "fifo":
        os.mkfifo(store / PACKED)
    elif kind == "directory":
        (store / PACKED).mkdir()
    packed_before = (store / PACKED).read_bytes() if kind == "damaged" else None
    report = fold(store)
    assert report.checks_blocked is not None
    assert report.checks_stopped is None and report.stopped is None
    assert not (store / "decisions" / "D9.json").exists()  # the record fold completed
    assert (store / REL1).exists()
    if packed_before is not None:
        assert (store / PACKED).read_bytes() == packed_before


def test_first_drop_only_fold_without_packed_file(
    git_repo: Path, pin_version_0_22: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def extra(store: Path) -> None:
        put_archive(store, "seg1", [archived_decision("ARCH1")])
        put_check(store, check_body(C1, "ARCH1"))

    store = committed(git_repo, checks=(), extra=extra)
    calls = record_fsyncs(monkeypatch, store)
    report = fold(store)
    assert report.checks_dropped == [REL1] and not (store / PACKED).exists()
    assert not [c for c in calls if c[0] == "file" and "packed" in c[1]]
    assert ("unlink", REL1) in calls
    assert PACKED not in report.wrote


def test_absent_packed_must_stay_absent(
    git_repo: Path, pin_version_0_22: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def extra(store: Path) -> None:
        put_archive(store, "seg1", [archived_decision("ARCH1")])
        put_check(store, check_body(C1, "ARCH1"))

    store = committed(git_repo, checks=(), extra=extra)
    stage_hook(
        monkeypatch,
        f"checks:before_unlink:{C1}.json",
        lambda: put_packed(store, [check_body(C3, "D1")]),
    )
    report = fold(store)
    assert report.checks_stopped is not None
    assert (store / REL1).exists()


def test_old_runtime_stops_check_fold(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:  # P4 store half
    monkeypatch.setattr(sidegraph, "__version__", "0.21.0")
    store = committed(git_repo)
    before = verifications(store)
    report = fold(store)
    assert report.checks_stopped is not None and report.stopped is None
    assert verifications(store) == before
    assert report.requirement_raised is None


@pytest.mark.parametrize("seam", ["checks:after_raise", "checks:after_publish"])
def test_head_moved(
    git_repo: Path, pin_version_0_22: None, monkeypatch: pytest.MonkeyPatch, seam: str
) -> None:  # P5
    store = committed(git_repo)
    stage_hook(monkeypatch, seam, lambda: new_commit(git_repo))
    report = fold(store)
    assert report.checks_stopped is not None and report.stopped is None
    if seam == "checks:after_raise":
        assert not (store / PACKED).exists()
    assert (store / REL1).exists()


def test_record_run_stopped_skips_check_fold(
    git_repo: Path, pin_version_0_22: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = committed(git_repo)

    def occupy() -> None:
        (store / "shards").mkdir(exist_ok=True)
        (store / "shards" / "00.shard").write_bytes(b"")

    stage_hook(monkeypatch, "after_head_reread", occupy)
    before = verifications(store)
    marker = (store / "format").read_bytes()
    report = fold(store)
    assert report.stopped is not None
    assert verifications(store) == before and (store / "format").read_bytes() == marker
    assert report.checks_packed == []


def test_packed_changed_stops(
    git_repo: Path, pin_version_0_22: None, monkeypatch: pytest.MonkeyPatch
) -> None:  # P6
    store = committed(git_repo)
    stage_hook(
        monkeypatch,
        f"checks:before_unlink:{C1}.json",
        lambda: put_packed(store, [check_body(C3, "D1")]),
    )
    report = fold(store)
    assert report.checks_stopped is not None
    assert (store / REL1).exists() and b"F03" in (store / PACKED).read_bytes()
    assert report.checks_packed == []


def test_check_file_changed_is_kept(
    git_repo: Path, pin_version_0_22: None, monkeypatch: pytest.MonkeyPatch
) -> None:  # P6
    store = committed(git_repo, checks=((C1, "D1"), (C2, "D1")))
    stage_hook(
        monkeypatch,
        f"checks:before_unlink:{C1}.json",
        lambda: put_check(store, check_body(C1, "D1", rationale="edited")),
    )
    report = fold(store)
    assert (REL1, "changed since the plan") in report.checks_kept
    assert (store / REL1).exists() and not (store / REL2).exists()
    assert report.checks_packed == [REL2] and report.checks_stopped is None


def test_crash_after_publish_heals(
    git_repo: Path, pin_version_0_22: None, tmp_path: Path
) -> None:  # P7
    store = committed(git_repo)
    twin = tmp_path / "twin"
    shutil.copytree(git_repo, twin)
    labels = record_checkpoints(twin / ".sidegraph", fold_checks=True)
    k = labels.index("checks:publish")
    with pytest.raises(Killed):
        run_with_kill(store, k, fold_checks=True)
    assert (store / REL1).exists() and C1.encode() in (store / PACKED).read_bytes()
    assert verify_snapshot(store) == []
    report = fold(store)
    assert report.checks_packed == [REL1] and not (store / REL1).exists()


def test_unchanged_image_is_flushed_before_unlinks(
    git_repo: Path, pin_version_0_22: None, monkeypatch: pytest.MonkeyPatch
) -> None:  # P8 durability half
    store = committed(git_repo, packed=[check_body(C2, "D1")])
    calls = record_fsyncs(monkeypatch, store)
    published = record_publishes(monkeypatch, store)
    report = fold(store)
    assert report.checks_dropped == [REL1] and PACKED not in report.wrote
    assert ("replace", PACKED) not in published
    unlink = calls.index(("unlink", REL1))
    assert ("file", PACKED) in calls[:unlink]
    assert ("dir", "verifications") in calls[calls.index(("file", PACKED)) : unlink]


def test_older_runtime_refuses_after_fold(
    git_repo: Path, pin_version_0_22: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = committed(git_repo)
    fold(store)
    monkeypatch.setattr(sidegraph, "__version__", "0.21.0")
    with pytest.raises(StoreRequirementError):
        Store(store)
    assert "requirement-above-verifier" in {v.code for v in verify_snapshot(store)}


def _returns_within(seconds: float, call, release) -> bool:
    """Run ``call`` in a daemon thread; whether it returned in time. A stuck call is released."""
    thread = threading.Thread(target=call, daemon=True)
    thread.start()
    thread.join(seconds)
    if thread.is_alive():
        release()  # unblock an open stuck on a FIFO so the thread can end
        thread.join(2)
        return False
    return True


def test_fifo_replacing_unchanged_packed_stops_without_blocking(
    git_repo: Path, pin_version_0_22: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = committed(git_repo, packed=[check_body(C2, "D1")])  # C1 dead: the image is unchanged
    packed = store / PACKED

    def swap() -> None:
        packed.unlink()
        os.mkfifo(packed)

    stage_hook(monkeypatch, "checks:after_raise", swap)
    reports: list[MaintenanceReport] = []

    def release() -> None:
        os.close(os.open(packed, os.O_RDWR | os.O_NONBLOCK))

    assert _returns_within(20, lambda: reports.append(fold(store)), release)
    assert reports[0].checks_stopped is not None and reports[0].stopped is None
    assert (store / REL1).exists()


def test_check_file_replaced_by_directory_is_kept_and_the_rest_unlinked(
    git_repo: Path, pin_version_0_22: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = committed(git_repo, checks=((C1, "D1"), (C2, "D1")))

    def swap() -> None:
        (store / REL1).unlink()
        (store / REL1).mkdir()

    stage_hook(monkeypatch, f"checks:before_unlink:{C1}.json", swap)
    report = fold(store)
    assert (REL1, "changed since the plan") in report.checks_kept
    assert report.checks_stopped is None
    assert report.checks_packed == [REL2] and not (store / REL2).exists()


def test_file_vanishing_before_unlink_is_already_gone(
    git_repo: Path, pin_version_0_22: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = committed(git_repo, checks=((C1, "D1"), (C2, "D1")))
    real = os.unlink

    def unlink(path, *args, **kwargs) -> None:
        if str(path).endswith(f"{C1}.json"):
            real(path)  # someone else removed it between the witness read and now
        real(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", unlink)
    report = fold(store)
    assert report.stopped is None and report.checks_stopped is None
    assert report.checks_packed == [REL2] and report.checks_dropped == []
    assert not (store / REL1).exists() and not (store / REL2).exists()


def test_head_moved_during_the_unlinks_keeps_the_drops(
    git_repo: Path, pin_version_0_22: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A drop has no packed copy, so a branch switch between two unlinks can make it live again:
    ``HEAD`` is re-read before each drop, and the drop and every later file are kept."""

    def extra(store: Path) -> None:
        put_file(store, "decision", decision_body("D2", status="accepted"))

    rel3 = f"verifications/{C3}.json"
    store = committed(git_repo, checks=((C1, "D2"), (C2, "D1"), (C3, "D1")), extra=extra)
    stage_hook(monkeypatch, f"checks:before_unlink:{C2}.json", lambda: new_commit(git_repo))
    report = fold(store)
    assert report.checks_stopped == "HEAD moved during the unlinks; remaining files kept"
    assert report.checks_packed == [REL1] and report.checks_dropped == []
    assert (REL2, "HEAD moved") in report.checks_kept and (rel3, "HEAD moved") in report.checks_kept
    assert (store / REL2).exists() and (store / rel3).exists() and not (store / REL1).exists()
