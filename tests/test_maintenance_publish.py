"""The maintenance publisher: crash safety at every checkpoint (T-M1), fsync order (T-M2), the
publisher half of T-C1, and the publish-path review focuses.

Every test runs the real ``Store.maintain`` over a store on disk; crashes are simulated by
raising at the ``maintenance.checkpoint`` seam, interleavings by the ``maintenance.stage`` seam.
# see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§6.2, §9 T-M1, T-M2, T-C1)
"""

# ruff: noqa: F811  (the imported ``git_repo`` fixture is redefined as a test argument)

from __future__ import annotations

import errno
import json
import os
import sys
from pathlib import Path

import pytest

try:  # no fcntl on Windows: only the F_FULLFSYNC tests need it
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

from sidegraph import maintenance, maintenance_clock
from sidegraph.maintenance import (
    Eligibility,
    StoreChanged,
    fsync_dir,
    fsync_fd,
    may_unlink,
    plan_maintenance,
    publish_bytes,
    render_report,
)
from sidegraph.store import Store
from sidegraph.store_format import (
    GITATTRIBUTES_LINE,
    bucket,
    bucket_name,
    encode_line,
    inventory_from_disk,
    render_shard,
)
from tests.store_format2_support import (
    Killed,
    archived_decision,
    build,
    canonical_bytes,
    commit_all,
    decision_body,
    disk_summaries,
    effective_snapshot,
    fact_body,
    first_unlink,
    git,
    git_repo,  # noqa: F401  (pytest fixture)
    ids_in_bucket,
    make_store,
    put_archive,
    put_file,
    put_shard_line,
    record_checkpoints,
    record_fsyncs,
    record_publishes,
    run_with_kill,
    shard_bytes,
    store_snapshot,
    tree_bytes,
    validator,
)

needs_fcntl = pytest.mark.skipif(fcntl is None, reason="no fcntl on this platform")
N = 64
OLD = "2026-01-01T00:00:00Z"
FIXTURES = [
    "swap_two_shards",
    "three_shard_cycle",
    "misplaced_with_eligible_newer_file",
    "swap_with_phase_c_replacement",
    "stray_into_two_buckets",
    "stray_skipped_beside_eligible",
    "bucket_two_equal_lines",
    "equal_bindings_bodies_one_in_dest_other_misplaced_or_stray",
]


def maintain(store: Path, *, settled_days: int = 0):
    return Store(store).maintain(settled_days=settled_days, dry_run=False, any_branch=True)


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


def lines_of(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_bytes().splitlines() if line.strip()]


# -- T-M1: killed anywhere -----------------------------------------------------------------------


FIXTURES_PLANNED = ["swap_two_shards", "stray_skipped_beside_eligible", "bucket_two_equal_lines"]
SAMPLED_FIXTURES = ["swap_two_shards", "misplaced_with_eligible_newer_file"]


def is_bucket_creation(label: str) -> bool:
    return label.startswith("prep:") and label.endswith(".shard")


def sampled_checkpoints(labels: list[str]) -> list[int]:
    """Every non-bucket checkpoint, plus the first, middle and last empty-bucket creation: those
    labels all come from one loop iteration of ``_prepare``."""
    buckets = [i for i, label in enumerate(labels) if is_bucket_creation(label)]
    keep = {buckets[0], buckets[len(buckets) // 2], buckets[-1]}
    return [i for i, label in enumerate(labels) if not is_bucket_creation(label) or i in keep]


def killed_at_every_checkpoint(tmp_path, fixture, *, sample: bool):  # T-M1
    uninterrupted = build(fixture, tmp_path / "base")
    maintain(uninterrupted)
    finished = canonical_bytes(uninterrupted)  # shards, per-record files and segments
    labels = record_checkpoints(build(fixture, tmp_path / "probe"))
    assert labels  # the fixture reaches the publisher
    for k in sampled_checkpoints(labels) if sample else range(len(labels)):
        store = build(fixture, tmp_path / f"k{k}")
        before = effective_snapshot(store)
        with pytest.raises(Killed):
            run_with_kill(store, k)
        assert effective_snapshot(store) == before, labels[k]
        assert store_snapshot(store) == disk_summaries(store), labels[k]  # served by the index
        report = maintain(store)
        assert report.stopped is None, labels[k]
        first = shard_bytes(store)
        maintain(store)
        assert shard_bytes(store) == first, labels[k]
        assert canonical_bytes(store) == finished, labels[k]  # the whole tree, not only shards
        assert effective_snapshot(store) == before, labels[k]
    # mutants: delete before publish; one-phase receiver-first; newer body folded in phase A;
    # B or C from pre-run bytes; unlink stray in phase A; phase A adds a line its bucket holds;
    # stray kept whole with a skipped line; no collapse of equal lines; body-only comparison


@pytest.mark.parametrize("fixture", SAMPLED_FIXTURES)
def test_killed_at_a_sample_of_checkpoints_reopens_and_converges(tmp_path, fixture):  # T-M1
    """A sample of the crash matrix; the release run kills every fixture at every label."""
    killed_at_every_checkpoint(tmp_path, fixture, sample=True)


def test_misplaced_with_newer_file_killed_after_A(tmp_path):
    store = build("misplaced_with_eligible_newer_file", tmp_path / "s")
    labels = record_checkpoints(build("misplaced_with_eligible_newer_file", tmp_path / "p"))
    after_a = next(i for i, label in enumerate(labels) if label.startswith("A:"))
    with pytest.raises(Killed):
        run_with_kill(store, after_a)
    xid = next(
        i for i in ids_in_bucket(3, 3, prefix="XA") if (store / "decisions" / f"{i}.json").exists()
    )
    held = [d for d in lines_of(store / "shards" / bucket_name(3)) if d["id"] == xid]
    assert [d["body"]["status"] for d in held] == ["proposed"]  # phase A added the OLD body
    maintain(store)
    final = [d for d in lines_of(store / "shards" / bucket_name(3)) if d["id"] == xid]
    assert [d["body"]["status"] for d in final] == ["accepted"]  # one line, the newer body
    assert not (store / "decisions" / f"{xid}.json").exists()


def test_phase_a_adds_nothing_a_bucket_already_holds(tmp_path):
    """After a crash between A and B the receiving bucket holds the equal line; a re-run must
    not add it again (rev 7: two differing copies after the re-run)."""
    store = build("swap_two_shards", tmp_path / "s")
    labels = record_checkpoints(build("swap_two_shards", tmp_path / "p"))
    killed_at = next(i for i, label in enumerate(labels) if label.startswith("A:"))
    with pytest.raises(Killed):
        run_with_kill(store, killed_at)
    held = labels[killed_at].removeprefix("A:")
    rerun: list[str] = []
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(maintenance, "checkpoint", rerun.append)
        maintain(store)
    assert f"A:{held}" not in rerun  # phase A of the re-run left the bucket alone
    assert any(label.startswith("A:") for label in rerun)  # and still added the other line


def test_equal_lines_of_one_identity_collapse_to_one(tmp_path):
    store = build("bucket_two_equal_lines", tmp_path)
    xid = next(
        i
        for i in ids_in_bucket(3, 3, prefix="XA")
        if effective_snapshot(store).get(("decision", i))
    )
    assert len([d for d in lines_of(store / "shards" / bucket_name(3)) if d["id"] == xid]) == 2
    maintain(store)
    assert len([d for d in lines_of(store / "shards" / bucket_name(3)) if d["id"] == xid]) == 1


def test_equal_bindings_bodies_all_survive(tmp_path):
    store = build("equal_bindings_bodies_one_in_dest_other_misplaced_or_stray", tmp_path)
    before = effective_snapshot(store)
    assert len([k for k in before if k[0] == "bindings"]) == 3
    maintain(store)
    after = effective_snapshot(store)
    assert after == before
    assert sorted(d["id"] for d in lines_of(store / "shards" / bucket_name(3))) == sorted(
        i for t, i in before if t == "bindings"
    )


def test_case_sensitive_stray_test_never_unlinks_bucket():
    assert not may_unlink("0A.shard", n=64)
    assert not may_unlink("0a.shard", n=64)
    assert may_unlink("extra.shard", n=64)
    assert may_unlink("ff.shard", n=64)  # out of range: not a bucket of this store


@pytest.mark.parametrize("fixture", FIXTURES_PLANNED)
def test_final_bytes_equal_the_planner_target(tmp_path, fixture):
    """The planner's target is a second source of truth beside the publisher's edit lists."""
    store = build(fixture, tmp_path / "s")
    served = disk_summaries(store)
    inv = inventory_from_disk(store, validate=validator)
    plan = plan_maintenance(inv, Eligibility(0, 0, None, frozenset(), ""), N)
    maintain(store)
    assert store_snapshot(store) == served  # the rebuilt index, not the files, answers
    assert all(value is not None for value in served.values())
    on_disk = shard_bytes(store)
    for b in range(N):
        assert on_disk[bucket_name(b)] == render_shard(plan.target[b]), bucket_name(b)
    strays = set(on_disk) - {bucket_name(b) for b in range(N)}
    held_by_skipped = {"stray_skipped_beside_eligible": {"extra.shard"}}.get(fixture, set())
    assert strays == held_by_skipped


# -- T-M2: fsync order ---------------------------------------------------------------------------


def migration_shaped(tmp_path: Path) -> Path:
    """Per-record files only, ``shards/`` absent, ``.gitattributes`` already correct."""
    store = make_store(tmp_path)
    put_file(store, "decision", decision_body("D1"))
    put_file(store, "fact", fact_body("F1"))
    (store / ".gitattributes").write_text(GITATTRIBUTES_LINE + "\n", encoding="utf-8")
    return store


def test_fsync_order_migration_shaped_fixture(tmp_path, monkeypatch):  # T-M2
    store = migration_shaped(tmp_path)
    calls = record_fsyncs(monkeypatch, store)
    maintain(store)  # no misplaced lines, shards/ absent
    assert calls.index(("dir", "shards")) < first_unlink(calls)
    assert calls.index(("dir", "store")) < first_unlink(calls)  # red: store fsync only in phase A


def test_fsync_every_existing_shard_file_in_prep(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    receiver, republished_b, source = 3, 17, 40
    x = ids_in_bucket(receiver, 1, prefix="XR")[0]
    w = ids_in_bucket(receiver, 2, prefix="XR")[1]
    y = ids_in_bucket(republished_b, 1, prefix="YR")[0]
    # a receiving bucket phase B relies on and phase C republishes (a replacement lands in it)
    put_shard_line(store, "decision", w, decision_body(w), bucket=bucket_name(receiver))
    put_file(store, "decision", decision_body(w, status="accepted"))
    # a bucket phase B republishes after the source removal (it holds a misplaced line)
    put_shard_line(store, "decision", x, decision_body(x), bucket=bucket_name(republished_b))
    # a bucket nothing republishes, and a stray
    z = ids_in_bucket(source, 1, prefix="ZR")[0]
    put_shard_line(store, "decision", z, decision_body(z), bucket=bucket_name(source))
    put_shard_line(store, "decision", y, decision_body(y), bucket="extra.shard")
    existing = [f"shards/{p.name}" for p in sorted((store / "shards").glob("*.shard"))]
    assert len(existing) == 4
    calls = record_fsyncs(monkeypatch, store)
    maintain(store)
    first_dir = calls.index(("dir", "shards"))
    for name in existing:
        assert ("file", name) in calls[:first_dir], name  # red: rev 8 and rev 9 variants
    assert ("file", "format") in calls[:first_dir]


def test_shards_dir_fsync_after_each_phase(tmp_path, monkeypatch):  # red: none between A and B
    store = build("swap_with_phase_c_replacement", tmp_path)
    calls = record_publishes(monkeypatch, store)
    real = maintenance.checkpoint

    def tag(label: str) -> None:
        calls.append(("cp", label))
        real(label)

    monkeypatch.setattr(maintenance, "checkpoint", tag)
    maintain(store)

    def idx(prefix: str, *, last: bool = False) -> int:
        hits = [i for i, c in enumerate(calls) if c[0] == "cp" and c[1].startswith(prefix)]
        assert hits, prefix
        return hits[-1] if last else hits[0]

    for before, after in (("A:", "B:"), ("B:", "C:")):
        between = calls[idx(before, last=True) : idx(after)]
        assert ("fsync_dir", "shards") in between, (before, after)
    tail = calls[idx("C:", last=True) :]
    assert ("fsync_dir", "shards") in tail


def test_dir_fsync_after_deletions(tmp_path, monkeypatch):  # T-M2: each directory after its unlinks
    store = build("swap_two_shards", tmp_path)
    calls = record_fsyncs(monkeypatch, store)
    maintain(store)
    unlinks = [i for i, c in enumerate(calls) if c[0] == "unlink" and c[1].startswith("decisions/")]
    assert unlinks
    assert calls.index(("dir", "decisions")) > max(unlinks)
    archive = [i for i, c in enumerate(calls) if c[0] == "unlink" and c[1].startswith("archive/")]
    assert archive and calls.index(("dir", "archive")) > max(archive)


def test_tmp_fsync_before_replace(tmp_path, monkeypatch):
    store = build("swap_two_shards", tmp_path)
    calls = record_publishes(monkeypatch, store)
    maintain(store)
    flushed: set[str] = set()
    published = 0
    for kind, name in calls:
        if kind == "fsync_tmp":
            flushed.add(name)
        elif kind == "replace" and (name.startswith("shards/") or name == ".gitattributes"):
            assert name in flushed, name  # the tmp of this very replace was flushed first
            flushed.discard(name)
            published += 1
    assert published


def test_gitattributes_gains_line_keeps_others(
    tmp_path, monkeypatch
):  # red: written only if missing
    store = build("swap_two_shards", tmp_path)
    (store / ".gitattributes").write_text("*.png binary\n", encoding="utf-8")
    calls = record_publishes(monkeypatch, store)
    real = maintenance.checkpoint

    def tag(label: str) -> None:
        calls.append(("cp", label))
        real(label)

    monkeypatch.setattr(maintenance, "checkpoint", tag)
    maintain(store)
    text = (store / ".gitattributes").read_text(encoding="utf-8")
    assert text.splitlines() == ["*.png binary", GITATTRIBUTES_LINE]
    first_a = next(i for i, c in enumerate(calls) if c[0] == "cp" and c[1].startswith("A:"))
    assert calls.index(("fsync_tmp", ".gitattributes")) < first_a
    maintain(store)  # a second run leaves it alone
    assert (store / ".gitattributes").read_text(encoding="utf-8") == text


def test_gitattributes_line_present_is_not_rewritten(tmp_path, monkeypatch):
    store = migration_shaped(tmp_path)
    calls = record_publishes(monkeypatch, store)
    maintain(store)
    assert ("replace", ".gitattributes") not in calls


def test_missing_buckets_created_in_prep(tmp_path, monkeypatch):  # red: created after phase C
    store = build("swap_two_shards", tmp_path)
    labels: list[str] = []
    monkeypatch.setattr(maintenance, "checkpoint", labels.append)
    maintain(store)
    created = [
        i for i, label in enumerate(labels) if label.startswith("prep:") and ".shard" in label
    ]
    first_a = next(i for i, label in enumerate(labels) if label.startswith("A:"))
    assert len(created) == N - 2  # every bucket the fixture lacks
    assert max(created) < first_a
    assert sorted(shard_bytes(store)) == [bucket_name(b) for b in range(N)]


@needs_fcntl
def test_full_fsync_on_darwin(monkeypatch):
    seen: list[tuple[int, int]] = []
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(fcntl, "F_FULLFSYNC", 51, raising=False)
    monkeypatch.setattr(fcntl, "fcntl", lambda fd, cmd, *a: seen.append((fd, cmd)) or 0)
    fsync_fd(7)
    assert seen == [(7, 51)]


@needs_fcntl
@pytest.mark.parametrize("code", [errno.ENOTSUP])
def test_fsync_falls_back_to_plain_when_full_flush_unsupported(monkeypatch, code):
    synced: list[int] = []
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(fcntl, "F_FULLFSYNC", 51, raising=False)

    def refuse(fd, cmd, *a):
        raise OSError(code, "unsupported")

    monkeypatch.setattr(fcntl, "fcntl", refuse)
    monkeypatch.setattr(os, "fsync", synced.append)
    fsync_fd(9)
    assert synced == [9]


# -- publish_bytes ---------------------------------------------------------------------------------


def test_publish_bytes_refuses_a_changed_target_and_leaves_no_tmp(tmp_path):
    target = tmp_path / "a.shard"
    target.write_bytes(b"old\n")
    with pytest.raises(StoreChanged):
        publish_bytes(target, b"new\n", expect=b"something else\n")
    assert target.read_bytes() == b"old\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.shard"]
    with pytest.raises(StoreChanged):  # expect=None: the target must be absent
        publish_bytes(target, b"new\n", expect=None)
    publish_bytes(target, b"new\n", expect=b"old\n")
    assert target.read_bytes() == b"new\n"


def test_publish_bytes_refuses_to_replace_a_symlink(tmp_path):
    outside = tmp_path / "outside"
    outside.write_bytes(b"keep")
    link = tmp_path / "00.shard"
    link.symlink_to(outside)
    with pytest.raises(StoreChanged):
        publish_bytes(link, b"", expect=None)
    assert link.is_symlink() and outside.read_bytes() == b"keep"


# -- T-C1 (publisher half) ------------------------------------------------------------------------


def committed_store(git_repo: Path) -> Path:
    """A store in the repository with five record types as per-record files, committed long
    ago (so settled under ``--settled-days 7``)."""
    store = make_store(git_repo)
    put_file(store, "decision", decision_body("D1"))
    put_file(store, "fact", fact_body("F1"))
    put_file(store, "decision", decision_body("D2", status="accepted"))
    (store / ".gitignore").write_text("index.db*\n*.tmp\n", encoding="utf-8")
    commit_all(git_repo, "store", date=OLD)
    return store


def test_folds_settled_files_verbatim_second_run_noop(git_repo):  # T-C1
    store = committed_store(git_repo)
    before = effective_snapshot(store)
    originals = {
        p.name: json.loads(p.read_text(encoding="utf-8"))
        for d in ("decisions", "facts")
        for p in (store / d).glob("*.json")
    }
    report = maintain(store, settled_days=7)
    assert report.stopped is None
    assert sorted(report.deleted_files) == [
        "decisions/D1.json",
        "decisions/D2.json",
        "facts/F1.json",
    ]
    assert not list(store.glob("decisions/*.json")) and not list(store.glob("facts/*.json"))
    assert sorted(shard_bytes(store)) == [bucket_name(b) for b in range(N)]  # all N exist
    assert effective_snapshot(store) == before
    packed = {
        f"{d['id']}.json": d["body"]
        for b in range(N)
        for d in lines_of(store / "shards" / bucket_name(b))
    }
    assert packed == originals  # verbatim bodies
    first = shard_bytes(store)
    again = maintain(store, settled_days=7)
    assert shard_bytes(store) == first
    assert not again.deleted_files and not again.moved and not again.folded


def test_unsettled_untracked_and_modified_files_stay(git_repo):
    store = make_store(git_repo)
    put_file(store, "decision", decision_body("OLD1"))
    put_file(store, "decision", decision_body("MOD1"))
    commit_all(git_repo, "old", date=OLD)
    put_file(store, "decision", decision_body("NEW1"))
    commit_all(git_repo, "new", date="2099-01-01T00:00:00Z")  # not yet settled
    put_file(store, "decision", decision_body("MOD1", status="accepted"))  # modified
    put_file(store, "decision", decision_body("UNTR1"))  # untracked
    report = maintain(store, settled_days=7)
    assert report.deleted_files == ["decisions/OLD1.json"]
    kept = dict(report.kept)
    assert kept["decisions/NEW1.json"] == "not settled"
    assert kept["decisions/MOD1.json"] == "untracked or modified"
    assert kept["decisions/UNTR1.json"] == "untracked or modified"


def test_hook_write_between_clock_and_lock_stays(
    git_repo, monkeypatch
):  # red: dirty set with the clock pass
    store = committed_store(git_repo)

    def hook_writes() -> None:
        put_file(store, "decision", decision_body("D1", status="accepted"))  # a settled file edited
        put_file(store, "fact", fact_body("F1", statement="rewritten"))
        put_file(store, "decision", decision_body("FRESH1"))  # a folded path re-created

    stage_hook(monkeypatch, "between_pass_and_mutation", hook_writes)
    report = maintain(store, settled_days=7)
    assert report.stopped is None
    assert (store / "decisions" / "D1.json").exists()
    assert (store / "facts" / "F1.json").exists()
    assert (store / "decisions" / "FRESH1.json").exists()
    assert not (store / "decisions" / "D2.json").exists()  # the untouched one folded


def test_file_rewritten_between_steps_3_and_4_not_deleted(git_repo, monkeypatch):
    store = committed_store(git_repo)
    stage_hook(
        monkeypatch,
        "before_step4",
        lambda: put_file(store, "decision", decision_body("D1", status="accepted")),
    )
    report = maintain(store, settled_days=7)
    assert (store / "decisions" / "D1.json").exists()
    assert ("decisions/D1.json", "changed since the plan") in report.kept
    assert not (store / "decisions" / "D2.json").exists()
    snapshot = effective_snapshot(store)
    assert snapshot[("decision", "D1")]["status"] == "accepted"  # the writer's edit is the record


def append_line(store: Path, b: int, record_id: str) -> None:
    """What a pull or checkout does to a bucket: a new line the run never inventoried."""
    path = store / "shards" / bucket_name(b)
    path.write_bytes(
        path.read_bytes() + encode_line("decision", record_id, decision_body(record_id)) + b"\n"
    )


def test_witness_bucket_changed_before_step4_keeps_file(git_repo, monkeypatch):  # §28 Opus 1
    store = committed_store(git_repo)
    newcomer = ids_in_bucket(bucket("D1", N), 1, prefix="PULL")[0]
    stage_hook(monkeypatch, "before_step4", lambda: append_line(store, bucket("D1", N), newcomer))
    report = maintain(store, settled_days=7)
    assert (store / "decisions" / "D1.json").exists()
    assert ("decisions/D1.json", "shard changed since it was written") in report.kept
    assert ("decision", newcomer) in effective_snapshot(store)


@pytest.mark.parametrize("when", ["after_step1", "after_step2", "between_pass_and_mutation"])
def test_head_move_refuses(git_repo, monkeypatch, when):  # red: HEAD re-read only once
    store = committed_store(git_repo)
    Store(store).close()
    before = tree_bytes(store)
    stage_hook(monkeypatch, when, lambda: git(git_repo, "commit", "-q", "--allow-empty", "-m", "x"))
    report = maintain(store, settled_days=7)
    assert report.stopped and "HEAD moved" in report.stopped
    assert tree_bytes(store) == before  # no shards/, bucket or .gitattributes write


def test_writers_wait_only_while_publishing(git_repo, monkeypatch):
    store = committed_store(git_repo)
    wrote: list[str] = []
    real = maintenance_clock.landing_times

    def slow_pass(wt):
        out = real(wt)
        from datetime import UTC, datetime

        from sidegraph.schema import Decision, DecisionKind, Provenance

        with Store(store, busy_timeout_seconds=0.2) as other:  # fails fast if the lock is held
            other.add_decision(
                Decision(
                    title="written during the clock pass",
                    kind=DecisionKind.ADR,
                    context="c",
                    choice="ch",
                    valid_from=datetime.now(UTC),
                    provenance=Provenance(source="manual"),
                )
            )
        wrote.append("done")
        return out

    monkeypatch.setattr(maintenance_clock, "landing_times", slow_pass)
    report = maintain(store, settled_days=7)
    assert wrote == ["done"] and report.stopped is None


def broken_states(git_repo: Path, state: str) -> Path:
    store = make_store(git_repo)
    put_file(store, "decision", decision_body("D1"))
    if state == "gap":
        (store / "decisions" / "BAD1.json").write_text("{not json", encoding="utf-8")
    elif state == "bad_line_unrecoverable_id":
        (store / "shards").mkdir()
        (store / "shards" / "00.shard").write_bytes(b"garbage, not an envelope\n")
    elif state == "conflict_region":
        put_shard_line(store, "decision", "D9", decision_body("D9"))
        shard = store / "shards" / bucket_name(bucket("D9", N))
        shard.write_bytes(b"<<<<<<< HEAD\n" + shard.read_bytes() + b"=======\n>>>>>>> x\n")
    else:
        (store / "shards").mkdir()
        (store / "shards" / "0A.shard").write_bytes(b"")
    commit_all(git_repo, "state", date=OLD)
    return store


@pytest.mark.parametrize(
    "state", ["gap", "bad_line_unrecoverable_id", "conflict_region", "case_collision_listing"]
)
def test_entry_refusal_writes_nothing(git_repo, state):  # T-C3 owner: red: prep before check_entry
    store = broken_states(git_repo, state)
    opened = Store(store)
    before = tree_bytes(store)
    report = opened.maintain(settled_days=0, dry_run=False, any_branch=True)
    assert report.stopped and report.stopped.startswith("refused")
    assert tree_bytes(store) == before  # no shards/, bucket or .gitattributes write
    assert not (store / ".gitattributes").exists()


def test_outside_git_requires_settled_days_0(tmp_path):
    store = migration_shaped(tmp_path)
    Store(store).close()
    before = tree_bytes(store)
    report = maintain(store, settled_days=7)
    assert report.stopped and "--settled-days 0" in report.stopped
    assert tree_bytes(store) == before
    assert maintain(store, settled_days=0).stopped is None


def test_dry_run_writes_nothing_lists_folds(git_repo):
    store = committed_store(git_repo)
    Store(store).close()
    before = tree_bytes(store)
    report = Store(store).maintain(settled_days=7, dry_run=True, any_branch=True)
    assert tree_bytes(store) == before
    assert not (store / "shards").exists() and not (store / ".gitattributes").exists()
    text = render_report(report)
    assert "decisions/D1.json" in text and "decisions/" in text and "Dry run" in text
    assert report.dry_run
    assert report.deleted_files == ["decisions/D1.json", "decisions/D2.json", "facts/F1.json"]


# -- publish-path review focuses -----------------------------------------------------------------


def test_maintenance_canonicalises_blank_and_unterminated_shard(tmp_path):  # Review Focus 1
    store = make_store(tmp_path)
    a, b = ids_in_bucket(3, 2, prefix="CAN")
    first = encode_line("decision", a, decision_body(a))
    second = encode_line("decision", b, decision_body(b))
    (store / "shards").mkdir()
    ordered = sorted([(a, first), (b, second)])
    messy = ordered[1][1] + b"\r\n\n   \n" + ordered[0][1]  # unsorted, CRLF, blank, no final LF
    (store / "shards" / bucket_name(3)).write_bytes(messy)
    maintain(store)
    assert (store / "shards" / bucket_name(3)).read_bytes() == b"".join(
        line + b"\n" for _i, line in ordered
    )


def test_maintenance_refuses_symlinked_shard(tmp_path, monkeypatch):  # Review Focus 3
    store = make_store(tmp_path)
    put_file(store, "decision", decision_body("D1"))
    outside = tmp_path / "outside.shard"
    outside.write_bytes(b"keep")
    Store(store).close()

    def appears() -> None:
        (store / "shards").mkdir(exist_ok=True)
        (store / "shards" / "05.shard").symlink_to(outside)

    stage_hook(monkeypatch, "after_head_reread", appears)
    report = maintain(store)
    assert report.stopped and "symlink" in report.stopped
    assert outside.read_bytes() == b"keep"
    assert (store / "decisions" / "D1.json").exists()
    with pytest.raises(ValueError, match="symlink"):  # and a cold open refuses it outright
        Store(store)


def test_publish_oserror_leaves_store_loadable(tmp_path, monkeypatch):  # Review Focus 4
    store = build("swap_two_shards", tmp_path)
    before = effective_snapshot(store)
    seen = 0
    real = os.replace
    target = str(store / "shards" / bucket_name(17))

    def replace(src, dst):
        nonlocal seen
        if str(dst) == target:
            seen += 1
            if seen == 2:  # the phase-B publish of the bucket phase A also published
                raise OSError(errno.ENOSPC, "No space left on device")
        real(src, dst)

    monkeypatch.setattr(os, "replace", replace)
    with pytest.raises(OSError):
        maintain(store)
    monkeypatch.undo()
    assert effective_snapshot(store) == before
    assert not list((store / "shards").glob("*.tmp"))
    with Store(store):  # loadable
        pass
    maintain(store)  # converges
    again = shard_bytes(store)
    maintain(store)
    assert shard_bytes(store) == again and effective_snapshot(store) == before


@pytest.mark.parametrize("code", [errno.EACCES])
def test_dir_fsync_unsupported_is_noop(tmp_path, monkeypatch, code):  # Review Focus 5
    store = build("swap_two_shards", tmp_path)
    before = effective_snapshot(store)
    real = os.open

    def deny_dirs(path, flags, *a, **kw):
        if os.path.isdir(path):
            raise OSError(code, "cannot open a directory")
        return real(path, flags, *a, **kw)

    monkeypatch.setattr(os, "open", deny_dirs)
    monkeypatch.setattr(maintenance, "_is_windows", lambda: True)
    fsync_dir(tmp_path)  # returns
    report = maintain(store)
    assert report.stopped is None
    monkeypatch.undo()
    assert effective_snapshot(store) == before


def test_report_names_moves_and_kept(tmp_path):
    store = build("stray_skipped_beside_eligible", tmp_path)
    report = maintain(store)
    text = render_report(report)
    assert "shadow-divergence" in text and "extra.shard" in text and "never commits" in text
    assert report.skipped and report.moved


# -- fix round 1 -----------------------------------------------------------------------------------


def test_witness_rewritten_in_place_same_size_and_mtime_keeps_second_file(tmp_path, monkeypatch):
    """§28: the witness bucket is read for EACH deletion. A stat-keyed cache would pass a bucket
    rewritten with the same size and a restored mtime."""
    store = make_store(tmp_path)
    first, second = sorted(ids_in_bucket(3, 2, prefix="W"))
    put_file(store, "decision", decision_body(first))
    put_file(store, "decision", decision_body(second))
    path = store / "shards" / bucket_name(3)
    real = maintenance.checkpoint
    fired: list[str] = []

    def rewrite_after_first_delete(label: str) -> None:
        real(label)
        if label.startswith("delete:") and not fired:
            fired.append(label)
            st = path.stat()
            out = []
            for line in path.read_bytes().split(b"\n"):
                if f'"id":"{second}"'.encode() in line:
                    line = line.replace(b'"context":"ctx"', b'"context":"xtc"')  # same length
                out.append(line)
            path.write_bytes(b"\n".join(out))
            os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
            assert path.stat().st_size == st.st_size

    monkeypatch.setattr(maintenance, "checkpoint", rewrite_after_first_delete)
    report = maintain(store)
    assert report.stopped is None and fired == [f"delete:decisions/{first}.json"]
    assert (store / "decisions" / f"{second}.json").exists()  # kept: its line no longer matches
    assert (f"decisions/{second}.json", "shard changed since it was written") in report.kept
    assert not (store / "decisions" / f"{first}.json").exists()


def _linked(store: Path, tmp_path: Path, what: str) -> Path:
    """Turn one entry of the store into a symlink to a place outside it; returns that place."""
    outside = tmp_path / "outside"
    if what == "shards_dir":
        outside.mkdir()
        (store / "shards").symlink_to(outside, target_is_directory=True)
    elif what == "decisions_dir":
        os.rename(store / "decisions", outside)
        (store / "decisions").symlink_to(outside, target_is_directory=True)
    elif what == "archive_dir":
        outside.mkdir()
        (store / "archive").symlink_to(outside, target_is_directory=True)
    elif what == "gitattributes":
        outside.write_text("*.png binary\n", encoding="utf-8")
        (store / ".gitattributes").symlink_to(outside)
    else:  # bucket_file
        (store / "shards").mkdir()
        outside.write_bytes(b"keep")
        (store / "shards" / "05.shard").symlink_to(outside)
    return outside


def _outside_state(path: Path) -> dict[str, bytes]:
    if path.is_file():
        return {".": path.read_bytes()}
    return {p.name: p.read_bytes() for p in sorted(path.rglob("*")) if p.is_file()}


@pytest.mark.parametrize(
    "what", ["shards_dir", "decisions_dir", "archive_dir", "gitattributes", "bucket_file"]
)
def test_maintenance_refuses_a_symlink_made_after_the_store_opened(tmp_path, what):
    store = make_store(tmp_path / "w")
    put_file(store, "decision", decision_body("D1"))
    opened = Store(store)  # the link appears AFTER this handle opened
    outside = _linked(store, tmp_path, what)
    before = _outside_state(outside)
    report = opened.maintain(settled_days=0, dry_run=False, any_branch=True)
    assert report.stopped and report.stopped.startswith("refused: symlinked store entries")
    named = {
        "shards_dir": "shards",
        "decisions_dir": "decisions",
        "archive_dir": "archive",
        "gitattributes": ".gitattributes",
        "bucket_file": "shards/05.shard",
    }[what]
    assert named in report.stopped.split(": ", 2)[2].split(", ")
    assert _outside_state(outside) == before  # nothing written through the link
    if what != "decisions_dir":
        assert (store / "decisions" / "D1.json").exists()  # nothing was deleted
    if what != "shards_dir":
        assert not (store / "shards").is_dir() or not list((store / "shards").glob("0[0-46-9]*"))


def _enable_full_sync(monkeypatch, behaviour) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(fcntl, "F_FULLFSYNC", 51, raising=False)
    monkeypatch.setattr(fcntl, "fcntl", behaviour)


@pytest.mark.parametrize("code", [errno.EIO])
def test_dir_open_device_error_propagates_before_any_deletion(tmp_path, monkeypatch, code):
    store = build("swap_two_shards", tmp_path)
    before = effective_snapshot(store)
    files = canonical_bytes(store)
    opened = Store(store)
    real = os.open

    def failing(path, flags, *a, **kw):
        if os.path.isdir(path):
            raise OSError(code, "device error")
        return real(path, flags, *a, **kw)

    monkeypatch.setattr(os, "open", failing)
    with pytest.raises(OSError) as raised:
        opened.maintain(settled_days=0, dry_run=False, any_branch=True)
    monkeypatch.undo()
    assert raised.value.errno == code
    assert effective_snapshot(store) == before
    assert all(
        (store / rel).exists() for rel in files if rel.startswith(("decisions/", "archive/"))
    )
    assert not list(store.rglob("*.tmp"))
    with Store(store):  # loadable
        pass


@needs_fcntl
@pytest.mark.parametrize("code", [errno.EIO])
def test_full_fsync_device_error_propagates_before_any_deletion(tmp_path, monkeypatch, code):
    store = build("swap_two_shards", tmp_path)
    before = effective_snapshot(store)
    opened = Store(store)

    def failing(fd, cmd, *a):
        raise OSError(code, "device error")

    _enable_full_sync(monkeypatch, failing)
    with pytest.raises(OSError) as raised:
        opened.maintain(settled_days=0, dry_run=False, any_branch=True)
    monkeypatch.undo()
    assert raised.value.errno == code
    assert effective_snapshot(store) == before
    assert list((store / "decisions").glob("*.json"))  # nothing was deleted
    assert (store / "archive" / "seg1.jsonl").exists()
    assert not list(store.rglob("*.tmp"))
    with Store(store):
        pass


@needs_fcntl
def test_full_fsync_unsupported_runs_to_completion(tmp_path, monkeypatch):
    store = build("swap_two_shards", tmp_path)
    before = effective_snapshot(store)
    opened = Store(store)

    def unsupported(fd, cmd, *a):
        raise OSError(errno.ENOTSUP, "no full flush here")

    _enable_full_sync(monkeypatch, unsupported)
    report = opened.maintain(settled_days=0, dry_run=False, any_branch=True)
    monkeypatch.undo()
    assert report.stopped is None and report.deleted_files
    assert effective_snapshot(store) == before


@pytest.mark.parametrize("content", [b"", b"   \n\n \r\n"])
def test_empty_stray_shard_is_removed(tmp_path, content):
    store = make_store(tmp_path)
    put_file(store, "decision", decision_body("D1"))
    (store / "shards").mkdir()
    (store / "shards" / "extra.shard").write_bytes(content)
    report = maintain(store)
    assert report.stopped is None
    assert not (store / "shards" / "extra.shard").exists()
    assert sorted(shard_bytes(store)) == [bucket_name(b) for b in range(N)]
    maintain(store)  # and a second run is a no-op


def test_stopped_run_reports_nothing_as_done(tmp_path, monkeypatch):
    store = build("swap_two_shards", tmp_path)
    stage_hook(
        monkeypatch,
        "after_head_reread",
        lambda: (
            (store / "shards").mkdir(exist_ok=True),
            (store / "shards" / "00.shard").write_bytes(b""),
        ),
    )  # someone creates a bucket the preparation was about to publish
    report = maintain(store)
    assert report.stopped and "changed under the run" in report.stopped
    assert report.moved == [] and report.folded == []
    assert report.deleted_files == [] and report.deleted_segments == []
    assert report.planned_moved and report.planned_folded
    text = render_report(report)
    assert "NOT applied" in text and "were" not in text.split("Stopped:")[1].split("\n", 1)[1]
    assert "shard lines moved" not in text


def test_completed_run_reports_what_it_did(tmp_path):
    store = build("swap_two_shards", tmp_path)
    report = maintain(store)
    assert report.stopped is None
    assert len(report.moved) == 2 and report.folded and report.deleted_files
    assert set(report.deleted_files) <= set(report.folded)
    assert "NOT applied" not in render_report(report)


def test_index_is_rebuilt_in_the_same_connection(tmp_path, monkeypatch):
    from datetime import UTC, datetime

    from sidegraph.schema import Decision, DecisionKind, Provenance

    store = make_store(tmp_path)
    put_file(store, "decision", decision_body("D1"))
    opened = Store(store)
    written: list[str] = []

    def other_writer() -> None:
        with Store(store) as other:
            made = other.add_decision(
                Decision(
                    title="added between the clock pass and the lock",
                    kind=DecisionKind.ADR,
                    context="c",
                    choice="ch",
                    valid_from=datetime.now(UTC),
                    provenance=Provenance(source="manual"),
                )
            )
        written.append(made.id)

    stage_hook(monkeypatch, "between_pass_and_mutation", other_writer)
    report = opened.maintain(settled_days=0, dry_run=False, any_branch=True)
    assert report.stopped is None and written
    assert not (
        store / "decisions" / "D1.json"
    ).exists()  # folded, so the index must come from shards
    assert not (store / "decisions" / f"{written[0]}.json").exists()
    for rid in ("D1", written[0]):
        assert opened.get_decision(rid) is not None, rid
    digest, stats = opened._compute_canonical_digest()
    assert opened.canonical_digest() == digest  # certified against the post-run files
    assert not opened._canonical_stat_mismatch(stats)  # and every stat row describes them
    with Store(store) as reopened:
        assert reopened.get_decision("D1") is not None
        assert reopened.get_decision(written[0]) is not None


def test_a_file_with_no_shard_line_is_never_unlinked(tmp_path):
    """Even if a plan lists it: the deletion itself needs the shard line to exist and equal."""
    store = make_store(tmp_path)
    put_file(store, "decision", decision_body("ONLY1"))
    inv = inventory_from_disk(store, validate=validator)
    dirty = frozenset({".sidegraph/decisions/ONLY1.json"})
    plan = plan_maintenance(inv, Eligibility(0, 0, None, dirty, ".sidegraph"), N)
    assert plan.file_deletions == [] and plan.inserts == []  # the planner keeps it
    plan.file_deletions.append("decisions/ONLY1.json")  # a bad plan
    expected = {store / rel: data for rel, data in inv.images.items()}
    report = maintenance.publish_plan(store, plan, expected, None, None)
    assert (store / "decisions" / "ONLY1.json").exists()
    assert ("decisions/ONLY1.json", "no shard line") in report.kept
    assert report.deleted_files == []


def test_open_sweeps_stale_gitattributes_tmp(tmp_path):
    import time

    store = make_store(tmp_path)
    stale = store / ".gitattributes.123.abcdef012345.tmp"
    fresh = store / ".gitattributes.456.fedcba543210.tmp"
    for path in (stale, fresh):
        path.write_text("*.shard -text\n", encoding="utf-8")
    old = time.time() - 3600
    os.utime(stale, (old, old))
    Store(store).close()
    assert not stale.exists() and fresh.exists()  # age-gated like the other root debris


# -- fix round 2 -----------------------------------------------------------------------------------


def test_dry_run_names_segment_and_file_deletions(tmp_path):
    store = make_store(tmp_path)
    arch = archived_decision("ARCH1")
    put_archive(store, "seg1", [arch])
    body = {k: v for k, v in arch.items() if k != "record_type"}
    put_shard_line(store, "decision", "ARCH1", body)  # already represented in its bucket
    put_file(store, "decision", decision_body("D1"))
    Store(store).close()
    before = tree_bytes(store)
    dry = Store(store).maintain(settled_days=0, dry_run=True, any_branch=True)
    assert tree_bytes(store) == before
    text = render_report(dry)
    assert "archive/seg1.jsonl" in text and "decisions/D1.json" in text
    assert "Nothing to fold" not in text
    real = maintain(store)
    assert real.deleted_segments == dry.deleted_segments == ["archive/seg1.jsonl"]
    assert real.deleted_files == dry.deleted_files == ["decisions/D1.json"]


def test_segment_of_two_records_is_folded_once_in_the_report(tmp_path):
    store = make_store(tmp_path)
    put_archive(store, "seg2", [archived_decision("ARCHA"), archived_decision("ARCHB")])
    report = maintain(store)
    assert report.folded == ["archive/seg2.jsonl"]
    assert report.deleted_segments == ["archive/seg2.jsonl"]
    assert "1 per-record files and segments folded" in render_report(report)


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot open a directory to fsync it")
@pytest.mark.parametrize("code", [errno.EIO])
def test_dir_fd_fsync_device_error_propagates_before_any_deletion(tmp_path, monkeypatch, code):
    import stat as statmod

    store = build("swap_two_shards", tmp_path)
    before = effective_snapshot(store)
    opened = Store(store)
    real = maintenance.fsync_fd

    def only_dirs_fail(fd: int) -> None:
        if statmod.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(code, "device error on a directory")
        real(fd)  # the tmp files and shard files flush fine

    monkeypatch.setattr(maintenance, "fsync_fd", only_dirs_fail)
    with pytest.raises(OSError) as raised:
        opened.maintain(settled_days=0, dry_run=False, any_branch=True)
    monkeypatch.undo()
    assert raised.value.errno == code
    assert effective_snapshot(store) == before
    assert list((store / "decisions").glob("*.json"))
    assert (store / "archive" / "seg1.jsonl").exists()
    with Store(store):
        pass


def test_dir_fd_fsync_unsupported_is_tolerated(tmp_path, monkeypatch):
    import stat as statmod

    store = build("swap_two_shards", tmp_path)
    real = maintenance.fsync_fd

    def only_dirs_unsupported(fd: int) -> None:
        if statmod.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, "directories cannot be fsynced here")
        real(fd)

    monkeypatch.setattr(maintenance, "fsync_fd", only_dirs_unsupported)
    assert maintain(store).stopped is None


@pytest.mark.parametrize("code", [errno.EACCES])
def test_dir_open_errors_propagate_on_posix(tmp_path, monkeypatch, code):
    def deny(path, flags, *a, **kw):
        raise OSError(code, "cannot open")

    monkeypatch.setattr(maintenance, "_is_windows", lambda: False)
    monkeypatch.setattr(os, "open", deny)
    with pytest.raises(OSError) as raised:
        fsync_dir(tmp_path)
    assert raised.value.errno == code
    monkeypatch.setattr(maintenance, "_is_windows", lambda: True)
    fsync_dir(tmp_path)  # on Windows the same error means "cannot open a directory"


# -- Windows file semantics (text-mode os.open; a flush needs a writable handle) -----------------


def test_publish_bytes_opens_the_tmp_binary(tmp_path, monkeypatch):
    """Without O_BINARY, Windows turns the LF in ``os.write`` into CRLF and the witness
    comparison rejects the publisher's own shard (maintenance then never deletes anything)."""
    fake_binary = 0x40000000
    monkeypatch.setattr(os, "O_BINARY", fake_binary, raising=False)
    real = os.open
    seen: list[int] = []

    def record(path, flags, *a, **kw):
        if str(path).endswith(".tmp"):
            seen.append(flags)
        return real(path, flags & ~fake_binary, *a, **kw)

    monkeypatch.setattr(os, "open", record)
    publish_bytes(tmp_path / "a.shard", b"x\n", expect=None)
    assert seen and all(flags & fake_binary for flags in seen)


def test_prep_flushes_existing_files_through_writable_handles_on_windows(tmp_path, monkeypatch):
    """Windows' flush fails (EBADF) on a read-only handle, which stranded migrate with the
    marker already switched. On Windows every file the prep flushes is opened writable."""
    store = build("swap_two_shards", tmp_path)
    monkeypatch.setattr(maintenance, "_is_windows", lambda: True)
    real_open, real_fsync = os.open, maintenance.fsync_fd
    read_only_files: set[int] = set()

    def record(path, flags, *a, **kw):
        fd = real_open(path, flags, *a, **kw)
        # os.O_ACCMODE is POSIX-only; the three access modes are the low two bits everywhere
        if flags & (os.O_WRONLY | os.O_RDWR) == 0 and not os.path.isdir(path):
            read_only_files.add(fd)
        return fd

    def windows_flush(fd: int) -> None:
        if fd in read_only_files:
            raise OSError(errno.EBADF, "flush needs write access")
        real_fsync(fd)

    monkeypatch.setattr(os, "open", record)
    monkeypatch.setattr(maintenance, "fsync_fd", windows_flush)
    report = maintain(store)
    assert report.stopped is None
