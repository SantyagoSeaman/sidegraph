"""``sidegraph-compact`` on a format-2 store: flag dispatch by marker major, the default-branch
condition, exit codes and the report text (the CLI half of T-C1).

Every test runs the real command over a store on disk.
# see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§6.2, §9 T-C1)
"""

# ruff: noqa: F811  (the imported ``git_repo`` fixture is redefined as a test argument)

from __future__ import annotations

import functools
import sqlite3
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

import sidegraph
from sidegraph import cli as cli_module
from sidegraph import maintenance, maintenance_clock
from sidegraph import store as store_module
from sidegraph.cli import compact_main
from sidegraph.store import Store
from tests.store_format2_support import (
    archived_decision,
    build,
    check_body,
    commit_all,
    decision_body,
    format1_store,
    git,
    git_env,
    git_repo,  # noqa: F401  (pytest fixture)
    ids_in_bucket,
    make_store,
    pin_version_0_22,  # noqa: F401  (pytest fixture)
    put_archive,
    put_check,
    put_file,
    put_packed,
    put_shard_line,
    shard_bytes,
)

OLD = "2026-01-01T00:00:00Z"


def committed_store(repo: Path, *, date: str = OLD) -> Path:
    store = make_store(repo)
    put_file(store, "decision", decision_body("D1"))
    put_file(store, "decision", decision_body("D2", status="accepted"))
    (store / ".gitignore").write_text("index.db*\n*.tmp\n", encoding="utf-8")
    commit_all(repo, "store", date=date)
    return store


def run(store: Path, *args: str) -> int:
    return compact_main(["--db", str(store), *args])


def test_older_than_refused_on_format2(git_repo, capsys):
    store = committed_store(git_repo)
    before = shard_bytes(store)
    assert run(store, "--older-than", "3") == 2
    assert "--settled-days" in capsys.readouterr().out
    assert shard_bytes(store) == before  # refused before any write


def test_settled_days_refused_on_format1(tmp_path, capsys):
    store = format1_store(tmp_path / "s")
    assert run(store, "--settled-days", "0") == 2
    assert "--older-than" in capsys.readouterr().out


def test_any_branch_refused_on_format1(tmp_path, capsys):
    store = format1_store(tmp_path / "s")
    assert run(store, "--any-branch") == 2
    assert "format 2" in capsys.readouterr().out


@pytest.mark.parametrize("dry", [False, True])
def test_format1_compact_adds_migration_advice(tmp_path, capsys, dry):
    store = format1_store(tmp_path / "s")
    Store(store).close()
    assert run(store, *(["--dry-run"] if dry else [])) == 0
    out = capsys.readouterr().out
    assert "nothing to compact" in out  # today's line is unchanged
    assert "sidegraph-migrate" in out


def test_negative_settled_days_refused(git_repo, capsys):
    store = committed_store(git_repo)
    assert run(store, "--settled-days", "-1") == 2
    assert "--settled-days must be >= 0" in capsys.readouterr().out


def test_default_branch_required_unless_any_branch(git_repo, capsys):
    store = committed_store(git_repo)
    git(git_repo, "switch", "-q", "-c", "topic")
    before = shard_bytes(store)
    assert run(store, "--settled-days", "0") == 2
    out = capsys.readouterr().out
    assert "topic" in out and "default branch" in out and "--any-branch" in out
    assert shard_bytes(store) == before and (store / "decisions" / "D1.json").exists()
    assert run(store, "--settled-days", "0", "--dry-run") == 2  # dry run is judged the same
    capsys.readouterr()
    assert run(store, "--settled-days", "0", "--any-branch") == 0
    assert not (store / "decisions" / "D1.json").exists()


def test_detached_head_refused(git_repo, capsys):
    store = committed_store(git_repo)
    git(git_repo, "checkout", "-q", "--detach")
    assert run(store, "--settled-days", "0") == 2
    assert "detached" in capsys.readouterr().out


def test_origin_head_names_the_default_branch(git_repo, tmp_path, capsys):
    committed_store(git_repo)
    git(git_repo, "branch", "-m", "trunk")
    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", "-q", str(git_repo), str(clone)],
        check=True,
        env=git_env(),
        capture_output=True,
    )
    assert run(clone / ".sidegraph", "--settled-days", "0") == 0  # origin/HEAD says trunk
    capsys.readouterr()
    git(clone, "switch", "-q", "-c", "main")  # a name that is no longer the default here
    assert run(clone / ".sidegraph", "--settled-days", "0") == 2


def test_committed_marker_switch_completed_by_settled_days_0(git_repo, capsys):  # T-C1
    store = committed_store(
        git_repo, date=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    )  # marker major 2, no shards/, files "new"
    assert not (store / "shards").exists()
    assert run(store) == 0  # default 7 days: nothing has settled, the shards are still made
    capsys.readouterr()
    assert (store / "decisions" / "D1.json").exists()
    assert run(store, "--settled-days", "0") == 0
    assert not (store / "decisions" / "D1.json").exists()
    assert len(shard_bytes(store)) == 64


def test_report_names_skips_kept_and_commit_instruction(git_repo, capsys):
    store = committed_store(git_repo)
    put_file(store, "decision", decision_body("D3"))  # untracked: stays, reported
    assert run(store) == 0
    out = capsys.readouterr().out
    assert "per-record files deleted:" in out and "decisions/D1.json" in out
    assert "files and segments kept:" in out and "decisions/D3.json" in out
    assert "commit them yourself" in out


def test_dry_run_says_planned_and_writes_nothing(git_repo, capsys):
    store = committed_store(git_repo)
    assert run(store, "--dry-run") == 0
    out = capsys.readouterr().out
    assert "Dry run: nothing was written." in out
    assert "2 per-record files would be deleted:" in out
    assert "files deleted" not in out  # a plan is never reported as done
    assert "commit them yourself" not in out
    assert not (store / "shards").exists() and (store / "decisions" / "D1.json").exists()


def test_stopped_run_exits_2_and_prints_reason(git_repo, capsys):
    store = committed_store(git_repo)
    (store / "shards").mkdir()
    (store / "shards" / "00.shard").write_bytes(b"not json\n")  # a bad-shard-line refuses
    commit_all(git_repo, "bad shard", date=OLD)
    assert run(store) == 2
    out = capsys.readouterr().out
    assert "Stopped: refused" in out and "bad-shard-line" in out


def test_shallow_clone_prints_notice(git_repo, tmp_path, capsys):
    committed_store(git_repo)
    commit_all(git_repo, "second", date=OLD)
    clone = tmp_path / "shallow"
    subprocess.run(
        ["git", "clone", "-q", "--depth", "1", f"file://{git_repo}", str(clone)],
        check=True,
        env=git_env(),
        capture_output=True,
    )
    assert run(clone / ".sidegraph", "--dry-run") == 0
    assert "shallow" in capsys.readouterr().out


def test_outside_git_needs_settled_days_0_and_has_no_branch_rule(tmp_path, capsys):
    store = make_store(tmp_path)
    put_file(store, "decision", decision_body("D1"))
    assert run(store) == 2
    assert "--settled-days 0" in capsys.readouterr().out
    assert run(store, "--settled-days", "0") == 0
    assert not (store / "decisions" / "D1.json").exists()


def test_publish_oserror_exits_1_with_message(git_repo, capsys, monkeypatch):
    store = committed_store(git_repo)

    def boom(self, **kw):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(store_module.Store, "maintain", boom)
    assert run(store) == 1
    assert "No space left" in capsys.readouterr().out


def tree_state(root: Path) -> dict[str, tuple[int, int, bytes]]:
    """Every file under ``root`` but the derived ``index.db*``: (mtime_ns, size, bytes).
    Empty directories are skipped: git does not track them and opening a store makes them."""
    out: dict[str, tuple[int, int, bytes]] = {}
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        if p.name.startswith("index.db"):
            continue
        if p.is_file():
            out[rel] = (p.stat().st_mtime_ns, p.stat().st_size, p.read_bytes())
    return out


def test_dry_run_leaves_the_store_directory_untouched(git_repo, capsys):
    store = committed_store(git_repo)
    before = tree_state(store)
    assert run(store, "--dry-run") == 0
    capsys.readouterr()
    assert tree_state(store) == before  # names, bytes and mtimes, index.db* excepted


def test_dry_run_refuses_a_store_that_opening_would_write_into(git_repo, capsys):
    store = committed_store(git_repo)
    (store / ".gitignore").unlink()
    commit_all(git_repo, "no gitignore", date=OLD)
    before = tree_state(store)
    assert run(store, "--dry-run") == 2
    assert ".gitignore" in capsys.readouterr().out
    assert tree_state(store) == before


@pytest.mark.parametrize("dry", [True, False])
def test_missing_and_empty_paths_are_refused_not_created(tmp_path, capsys, dry):
    flags = ["--settled-days", "0", *(["--dry-run"] if dry else [])]
    missing = tmp_path / "nowhere"
    assert run(missing, *flags) == 2
    assert not missing.exists()
    empty = tmp_path / "empty"
    empty.mkdir()
    assert run(empty, *flags) == 2
    assert list(empty.iterdir()) == []
    assert "never creates" in capsys.readouterr().out


def test_older_than_negative_is_refused_by_format_first(git_repo, tmp_path, capsys):
    store = committed_store(git_repo)
    assert run(store, "--older-than", "-1") == 2
    assert "--settled-days" in capsys.readouterr().out
    f1 = format1_store(tmp_path / "f1")
    assert run(f1, "--older-than", "-1") == 1
    assert "--older-than must be >= 0" in capsys.readouterr().out


def test_busy_store_exits_1_without_traceback_and_retry_succeeds(git_repo, capsys, monkeypatch):
    store = committed_store(git_repo)
    monkeypatch.setattr(cli_module, "Store", functools.partial(Store, busy_timeout_seconds=0.2))
    Store(store).close()  # build the index, so opening needs no write
    rival = sqlite3.connect(store / "index.db", isolation_level=None)
    rival.execute("BEGIN IMMEDIATE")
    try:
        started = time.monotonic()
        assert run(store) == 1
        assert time.monotonic() - started < 120
    finally:
        rival.execute("ROLLBACK")
        rival.close()
    out = capsys.readouterr().out
    assert "busy" in out and "Traceback" not in out
    assert (store / "decisions" / "D1.json").exists()  # nothing was done
    assert run(store) == 0
    assert not (store / "decisions" / "D1.json").exists()


def test_branch_named_like_a_tag_is_still_the_default(git_repo, capsys):
    store = committed_store(git_repo)
    git(git_repo, "tag", "main")  # `symbolic-ref --short HEAD` would print heads/main
    assert run(store, "--settled-days", "0") == 0


def test_default_branch_names_fails_on_a_git_error(git_repo, monkeypatch):
    wt = maintenance_clock.resolve_work_tree(committed_store(git_repo))
    real = maintenance_clock._run

    def failing(root, args, pre=()):
        proc = real(root, args, pre)
        if args[:2] == ["symbolic-ref", "-q"] and args[2] == "refs/remotes/origin/HEAD":
            return subprocess.CompletedProcess(args, 128, b"", b"fatal: broken refs")
        return proc

    monkeypatch.setattr(maintenance_clock, "_run", failing)
    with pytest.raises(maintenance_clock.ClockError):
        maintenance_clock.default_branch_names(wt)


def test_report_names_a_skipped_identity_and_its_finding(tmp_path, capsys):
    store = build("stray_skipped_beside_eligible", tmp_path)
    assert run(store, "--settled-days", "0") == 0
    out = capsys.readouterr().out
    assert "identities skipped" in out and "shadow-divergence" in out


def test_commit_instruction_only_when_something_was_written(git_repo, capsys):
    store = committed_store(git_repo)
    assert run(store) == 0
    assert "commit them yourself" in capsys.readouterr().out
    assert run(store) == 0  # second run: nothing left to write
    out = capsys.readouterr().out
    assert "No records to fold or move." in out and "commit them yourself" not in out
    # a stop that wrote nothing
    (store / "shards" / "00.shard").write_bytes(b"not json\n")
    commit_all(git_repo, "bad shard", date=OLD)
    assert run(store) == 2
    assert "commit them yourself" not in capsys.readouterr().out


def test_help_describes_both_formats(capsys):
    with pytest.raises(SystemExit):
        compact_main(["--help"])
    out = capsys.readouterr().out
    flat = " ".join(out.split())
    assert "On a format-2 (packed) store the command is maintenance" in flat
    assert "--settled-days" in flat and "--any-branch" in flat
    assert "packs committed drift-check files into verifications/packed.shard" in flat
    assert "raises the store's requirement to Sidegraph 0.22.0" in flat


def packed_repo(repo: Path) -> Path:
    """A committed format-2 repository whose records are already folded: every bucket exists
    and nothing is left to do."""
    store = committed_store(repo)
    assert run(store) == 0
    commit_all(repo, "folded", date=OLD)
    return store


def make_missing_bucket(repo: Path, store: Path) -> list[str]:
    (store / "shards" / "05.shard").unlink()
    return ["shards/05.shard"]


def make_empty_stray(repo: Path, store: Path) -> list[str]:
    (store / "shards" / "ff.shard").write_bytes(b"")
    return ["shards/ff.shard"]


def make_segment_only(repo: Path, store: Path) -> list[str]:
    rid = ids_in_bucket(9, 1, prefix="SG")[0]
    arch = archived_decision(rid)
    body = {k: v for k, v in arch.items() if k != "record_type"}
    put_shard_line(store, "decision", rid, body)
    put_archive(store, "seg1", [arch])
    return ["archive/seg1.jsonl"]


def written_list(out: str) -> list[str]:
    lines = out.splitlines()
    head = next(i for i, ln in enumerate(lines) if "files " in ln and "written or removed" in ln)
    listed = []
    for ln in lines[head + 1 :]:
        if not ln.startswith("  "):
            break
        listed.append(ln.strip())
    return listed


TRACKED_ONLY_CASES = [
    make_missing_bucket,
    make_empty_stray,
    make_segment_only,
]


@pytest.mark.parametrize("make", TRACKED_ONLY_CASES, ids=lambda f: f.__name__)
def test_real_run_with_only_a_tracked_write_prints_the_commit_instruction(git_repo, capsys, make):
    store = packed_repo(git_repo)
    expected = make(git_repo, store)
    commit_all(git_repo, "state", date=OLD)
    capsys.readouterr()
    assert run(store) == 0
    out = capsys.readouterr().out
    assert written_list(out) == expected
    assert "commit them yourself" in out and "No records to fold or move" not in out
    assert git(git_repo, "status", "--porcelain", "--", ".sidegraph/shards", ".sidegraph/archive")


@pytest.mark.parametrize("make", TRACKED_ONLY_CASES, ids=lambda f: f.__name__)
def test_dry_run_names_the_tracked_write_it_would_make(git_repo, capsys, make):
    store = packed_repo(git_repo)
    expected = make(git_repo, store)
    commit_all(git_repo, "state", date=OLD)
    capsys.readouterr()
    before = tree_state(store)
    assert run(store, "--dry-run") == 0
    out = capsys.readouterr().out
    assert "files would be written or removed:" in out
    assert written_list(out) == expected
    assert "No records to fold or move" not in out and "commit them yourself" not in out
    assert tree_state(store) == before


def test_a_true_no_op_prints_no_commit_line_and_no_write_list(git_repo, capsys):
    store = packed_repo(git_repo)
    capsys.readouterr()
    for flags in ([], ["--dry-run"]):
        assert run(store, *flags) == 0
        out = capsys.readouterr().out
        assert "No records to fold or move." in out
        assert "commit them yourself" not in out and "written or removed" not in out


def test_open_while_a_rival_holds_the_writer_lock_is_busy_not_unreadable(
    git_repo, capsys, monkeypatch
):
    store = committed_store(git_repo)
    (store / "index.db").unlink(missing_ok=True)  # no index yet: opening must write
    monkeypatch.setattr(cli_module, "Store", functools.partial(Store, busy_timeout_seconds=0.2))
    rival = sqlite3.connect(store / "index.db", isolation_level=None)
    rival.execute("BEGIN IMMEDIATE")
    try:
        assert run(store) == 1
    finally:
        rival.execute("ROLLBACK")
        rival.close()
    out = capsys.readouterr().out
    assert "busy" in out and "not readable" not in out and "Traceback" not in out
    assert run(store) == 0


def test_first_fold_collapses_the_empty_bucket_creations(git_repo, capsys):
    store = committed_store(git_repo)
    for flags in (["--dry-run"], []):
        assert run(store, *flags) == 0
        out = capsys.readouterr().out
        assert "empty shard files" in out and out.count(".shard") < 64
        listed = [ln for ln in out.splitlines() if ln.startswith("  shards/")]
        assert len(listed) < 64  # the buckets that received a record are named, the rest folded


def test_stopped_run_that_already_wrote_says_so(git_repo, capsys, monkeypatch):
    store = committed_store(git_repo)

    def stage(label):
        if label == "before_step4":  # the preparation and the folds are on disk by now
            commit_all(git_repo, "moved head", date=OLD)

    monkeypatch.setattr(maintenance, "stage", stage)
    assert run(store) == 2
    out = capsys.readouterr().out
    assert "Stopped:" in out and "written or removed" in out
    assert "The run stopped after writing these files" in out
    assert "commit them yourself" not in out


# -- the drift-check fold --------------------------------------------------------------------------

C1 = "01ARZ3NDEKTSV4RRFFQ69G5F01"
C2 = "01ARZ3NDEKTSV4RRFFQ69G5F02"
C3 = "01ARZ3NDEKTSV4RRFFQ69G5F03"


def store_with_checks(repo: Path, checks=((C1, "D1"),), packed=None) -> Path:
    store = make_store(repo)
    put_file(store, "decision", decision_body("D1", status="accepted"))
    for cid, did in checks:
        put_check(store, check_body(cid, did))
    if packed is not None:
        put_packed(store, packed)
    Store(store).close()  # writes the .gitignore a first open leaves
    commit_all(repo, "store", date=OLD)
    return store


def test_check_fold_stop_exits_2(git_repo, capsys, monkeypatch):  # P4 CLI half
    monkeypatch.setattr(sidegraph, "__version__", "0.21.0")
    store = store_with_checks(git_repo)
    assert run(store, "--settled-days", "0") == 2
    out = capsys.readouterr().out
    assert "Drift checks stopped" in out
    assert (store / "verifications" / f"{C1}.json").exists()


def test_blocked_check_fold_exits_0(git_repo, capsys, pin_version_0_22):
    store = store_with_checks(git_repo, packed=[b"not json"])
    assert run(store, "--settled-days", "0") == 0
    out = capsys.readouterr().out
    assert "Drift checks not folded" in out
    assert (store / "verifications" / f"{C1}.json").exists()


def test_outside_git_exits_0_with_checks(tmp_path, capsys, pin_version_0_22):  # A10
    store = make_store(tmp_path)
    put_file(store, "decision", decision_body("D1", status="accepted"))
    put_check(store, check_body(C1, "D1"))
    assert run(store, "--settled-days", "0") == 0
    out = capsys.readouterr().out
    assert f"verifications/{C1}.json: outside a git work tree" in out
    assert (store / "verifications" / f"{C1}.json").exists()
    assert not (store / "verifications" / "packed.shard").exists()


def test_default_settled_days_packs_fresh_check(git_repo, capsys, pin_version_0_22):  # A11
    store = make_store(git_repo)
    put_file(store, "decision", decision_body("D1", status="accepted"))
    put_check(store, check_body(C1, "D1"))
    commit_all(git_repo, "store", date=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))
    assert run(store) == 0
    assert (store / "verifications" / "packed.shard").exists()
    assert not (store / "verifications" / f"{C1}.json").exists()


def test_report_lines(git_repo, capsys, pin_version_0_22):
    store = store_with_checks(git_repo, checks=((C1, "D1"), (C2, "D1")))
    put_check(store, check_body(C3, "D1"))  # untracked: kept, and it supersedes nothing
    assert run(store, "--settled-days", "0") == 0
    out = capsys.readouterr().out
    assert f"verifications/{C2}.json" in out and "packed into" in out
    assert f"verifications/{C1}.json" in out and "dropped" in out
    assert f"verifications/{C3}.json: untracked or modified" in out
    assert "0.22.0" in out and "every teammate" in out


def test_dry_report_says_would(git_repo, capsys, pin_version_0_22):
    store = store_with_checks(git_repo)
    assert run(store, "--settled-days", "0", "--dry-run") == 0
    out = capsys.readouterr().out
    assert "would be packed" in out
    assert "would require Sidegraph >= 0.22.0" in out
    assert (store / "verifications" / f"{C1}.json").exists()


def test_no_records_line_with_only_kept_checks(git_repo, capsys, pin_version_0_22):
    store = store_with_checks(git_repo, checks=())
    assert run(store, "--settled-days", "0") == 0  # the record fold; nothing left to write
    commit_all(git_repo, "folded", date=OLD)
    put_check(store, check_body(C1, "D1"))  # untracked: kept
    capsys.readouterr()
    assert run(store, "--settled-days", "0") == 0
    out = capsys.readouterr().out
    assert "files written or removed" not in out
    assert "untracked or modified" in out
    assert "No records to fold or move." not in out


def test_unlanded_checks_warn(git_repo, tmp_path, capsys, pin_version_0_22):
    store = make_store(git_repo)
    put_file(store, "decision", decision_body("D1", status="accepted"))
    commit_all(git_repo, "base", date=OLD)
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    git(git_repo, "remote", "add", "origin", str(bare))
    git(git_repo, "push", "-q", "origin", "main")
    put_check(store, check_body(C1, "D1"))
    commit_all(git_repo, "check", date=OLD)
    assert run(store, "--settled-days", "0") == 0
    out = capsys.readouterr().out
    assert "packed-check-without-source" in out and f"verifications/{C1}.json" in out


def _origin_at_head(repo: Path, tmp_path: Path) -> None:
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    git(repo, "remote", "add", "origin", str(bare))
    git(repo, "push", "-q", "origin", "main")


UNCHECKED = "Could not check whether the packed drift checks reached the default branch"


def test_unlanded_unchecked_names_a_missing_origin_ref(git_repo, capsys, pin_version_0_22):
    store = store_with_checks(git_repo)
    assert run(store, "--settled-days", "0") == 0
    assert f"{UNCHECKED} (no origin/<default> ref to compare with)." in capsys.readouterr().out


def test_unlanded_unchecked_names_a_git_failure(git_repo, capsys, monkeypatch, pin_version_0_22):
    store = store_with_checks(git_repo)

    def boom(wt, paths):
        raise maintenance_clock.ClockError("git merge-base failed (128): boom")

    monkeypatch.setattr(maintenance_clock, "unlanded_paths", boom)
    assert run(store, "--settled-days", "0") == 0
    out = capsys.readouterr().out
    assert f"{UNCHECKED} (git merge-base failed (128): boom)." in out
    assert "no origin/<default>" not in out


def test_dry_run_lists_unlanded_checks(git_repo, tmp_path, capsys, pin_version_0_22):
    store = make_store(git_repo)
    put_file(store, "decision", decision_body("D1", status="accepted"))
    Store(store).close()
    commit_all(git_repo, "base", date=OLD)
    _origin_at_head(git_repo, tmp_path)
    put_check(store, check_body(C1, "D1"))
    commit_all(git_repo, "check", date=OLD)
    assert run(store, "--settled-days", "0", "--dry-run") == 0
    out = capsys.readouterr().out
    assert "packed-check-without-source" in out and f"verifications/{C1}.json" in out
    assert (store / "verifications" / f"{C1}.json").exists()


def test_dry_and_real_stop_text_for_a_too_old_runtime_agree(git_repo, capsys, monkeypatch):
    monkeypatch.setattr(sidegraph, "__version__", "0.21.0")
    store = store_with_checks(git_repo)
    assert run(store, "--settled-days", "0", "--dry-run") == 2
    dry = capsys.readouterr().out
    assert run(store, "--settled-days", "0") == 2
    real = capsys.readouterr().out
    line = next(x for x in dry.splitlines() if x.startswith("Drift checks stopped"))
    assert line in real.splitlines()


def test_verifications_file_blocks_checks_and_records_still_fold(
    git_repo, capsys, pin_version_0_22
):
    """A ``verifications`` that is a regular file blocks the check fold only: the record fold of
    the same run completes and the run exits 0, as before the check fold existed."""
    store = committed_store(git_repo)
    (store / "verifications").write_bytes(b"not a folder\n")
    commit_all(git_repo, "odd", date=OLD)
    assert run(store, "--settled-days", "0") == 0
    out = capsys.readouterr().out
    assert "Drift checks not folded" in out and "unreadable" in out
    assert not (store / "decisions" / "D1.json").exists()  # the record fold ran
    assert shard_bytes(store)
