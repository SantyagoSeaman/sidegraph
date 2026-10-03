"""The two stranded-write checks: ``store-uncommitted`` and ``branch-only-records``.

A store is committed with the repository, so a record follows the branch it was written on. A
record left uncommitted, or sitting on a branch nobody merges, is invisible to every other
checkout. Both detectors are pure reads (git, run read-only through ``gitenv.git_env()``) over
real ``git init`` repositories in ``tmp_path``. Ages are set with ULIDs minted at a chosen time,
with ``os.utime`` and with ``GIT_COMMITTER_DATE``; nothing sleeps.
see design/superpowers/specs/2026-10-02-stranded-store-writes-design.md (D1, D2; T1-T14)
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from ulid import ULID

from sidegraph import integrity
from sidegraph.cli import doctor_main
from sidegraph.doctor import curate
from sidegraph.integrity import CHECKS, Inputs
from sidegraph.stats.model import HealthItem, build_report
from sidegraph.store import Store
from tests.test_doctor_integrity import _settled_healthy
from tests.test_host_integrity import NOTICE_KEY, context, start
from tests.test_integrity import add_record, settled

NOW = datetime.now(UTC)
UNCOMMITTED = "store-uncommitted"
BRANCH_ONLY = "branch-only-records"

_IDENTITY = {
    "GIT_AUTHOR_NAME": "T",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "T",
    "GIT_COMMITTER_EMAIL": "t@example.com",
}


# -- helpers --------------------------------------------------------------------------------


def git(cwd: Path, *args: str, date: datetime | None = None, check: bool = True) -> str:
    """One git call in ``cwd``. ``date`` sets the author and committer dates (no sleeping)."""
    env = {**os.environ, **_IDENTITY}
    if date is not None:
        stamp = date.strftime("%Y-%m-%dT%H:%M:%S+0000")
        env["GIT_COMMITTER_DATE"] = stamp
        env["GIT_AUTHOR_DATE"] = stamp
    done = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True)
    if check and done.returncode != 0:
        raise AssertionError(f"git {args} failed: {done.stderr}")
    return done.stdout


def ulid_at(hours_ago: float) -> str:
    """A ULID minted ``hours_ago`` before ``NOW`` (the name a record file gets at write time)."""
    return str(ULID.from_datetime(NOW - timedelta(hours=hours_ago)))


def age_file(path: Path, hours_ago: float) -> None:
    stamp = (NOW - timedelta(hours=hours_ago)).timestamp()
    os.utime(path, (stamp, stamp))


def write(path: Path, payload: object = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({} if payload is None else payload), encoding="utf-8")
    return path


def record(store: Path, kind: str, name: str, status: str = "proposed") -> Path:
    return write(store / kind / f"{name}.json", {"id": name, "status": status})


def make_repo(path: Path, branch: str = "main") -> Path:
    """A repository with one commit on ``branch``."""
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", branch)
    (path / "a.txt").write_text("a\n", encoding="utf-8")
    git(path, "add", "a.txt")
    git(path, "commit", "-q", "--no-verify", "-m", "initial")
    return path


def seed_store(store: Path) -> Path:
    """The store's two committed housekeeping files, committed (so they never show as new)."""
    store.mkdir(parents=True, exist_ok=True)
    (store / "format").write_text("sidegraph-store 0.6.0\n", encoding="utf-8")
    (store / ".gitignore").write_text("index.db*\n*.tmp\n", encoding="utf-8")
    top = Path(git(store, "rev-parse", "--show-toplevel").strip())
    git(top, "add", str(store))
    git(top, "commit", "-q", "--no-verify", "-m", "store")
    return store


def project(tmp_path: Path, name: str = "repo") -> tuple[Path, Path]:
    """``(repo, store)``: a repository whose ``.sidegraph/`` holds its two committed files."""
    repo = make_repo(tmp_path / name)
    return repo, seed_store(repo / ".sidegraph")


def check_for(check_id: str):
    return next(c for c in CHECKS if c.id == check_id)


def run_check(check_id: str, store: Path, now: datetime = NOW) -> integrity.RunResult:
    return integrity.run(Inputs(store_dir=store, now=now), "session", (check_for(check_id),))


def only_problem(check_id: str, store: Path) -> integrity.Problem:
    result = run_check(check_id, store)
    assert len(result.problems) == 1, result
    return result.problems[0]


def uncommitted_line(store: Path, n: int, age: str, kinds: str, where: str = ".sidegraph/") -> str:
    return (
        f"Sidegraph: {n} store file(s) in {store} are not committed, the oldest for {age} "
        f"({kinds}), so other checkouts and teammates do not see them. Commit {where} in a "
        "pull request."
    )


def branch_with(
    store: Path,
    name: str,
    records: dict[str, str] | None = None,
    *,
    age: timedelta = timedelta(days=1),
    base: str = "main",
) -> None:
    """A branch off ``base`` whose one commit adds ``records`` (``"decisions/ID.json"`` ->
    status) under ``store`` and is dated ``age`` before ``NOW``. Leaves ``base`` checked out."""
    top = Path(git(store, "rev-parse", "--show-toplevel").strip())
    git(top, "switch", "-q", "-c", name, base)
    for rel, status in (records or {}).items():
        write(store / rel, {"id": Path(rel).stem, "status": status})
    git(top, "add", "-A")
    git(top, "commit", "-q", "--no-verify", "--allow-empty", "-m", name, date=NOW - age)
    git(top, "switch", "-q", base)


def empty_branch(top: Path, name: str, age: timedelta) -> None:
    """A branch one empty commit ahead of ``main``, by plumbing (fast, and the worktree stays)."""
    tree = git(top, "rev-parse", "main^{tree}").strip()
    sha = git(top, "commit-tree", tree, "-p", "main", "-m", name, date=NOW - age).strip()
    git(top, "update-ref", f"refs/heads/{name}", sha)


# -- the registry ---------------------------------------------------------------------------


def test_the_two_checks_follow_refresh_hook_missing_and_have_their_surfaces():
    assert [c.id for c in CHECKS][-3:] == ["refresh-hook-missing", UNCOMMITTED, BRANCH_ONLY]
    assert check_for(UNCOMMITTED).surfaces == frozenset({"session", "doctor", "stats"})
    assert check_for(BRANCH_ONLY).surfaces == frozenset({"session"})


# -- D1: store-uncommitted -------------------------------------------------------------------


def test_t1_old_untracked_and_old_modified_files_give_the_kinds_and_the_age(tmp_path):
    """A ULID 25.5 h old (untracked decision) plus a binding file modified 25.5 h ago."""
    _repo, store = project(tmp_path)
    binding = write(store / "bindings" / f"{ulid_at(900)}.json")
    git(store, "add", "-A")
    git(store, "commit", "-q", "--no-verify", "-m", "binding")
    binding.write_text('{"changed": 1}', encoding="utf-8")
    age_file(binding, 25.5)
    record(store, "decisions", ulid_at(25.5))

    problem = only_problem(UNCOMMITTED, store)

    line = uncommitted_line(store, 2, "25 hours", "1 decision file(s), 1 binding file(s)")
    assert problem.line == line and problem.notice == line
    assert problem.severity == "degraded"
    assert problem.summary == "2 store file(s) uncommitted"
    assert problem.fix == "commit .sidegraph/"
    assert problem.findings == ((str(store), line.removeprefix("Sidegraph: ")),)


def test_t1_a_ulid_23_hours_old_is_clean(tmp_path):
    """Red against a check with no age gate (a day is the line between in-flight and stranded)."""
    _repo, store = project(tmp_path)
    record(store, "decisions", ulid_at(23.5))

    result = run_check(UNCOMMITTED, store)

    assert result.problems == [] and UNCOMMITTED in result.clean


def test_t1_an_age_of_two_days_or_more_is_given_in_days(tmp_path):
    _repo, store = project(tmp_path)
    record(store, "facts", ulid_at(60))

    assert "the oldest for 2 days (1 fact file(s))" in only_problem(UNCOMMITTED, store).line


def test_t2_the_problem_survives_stash_and_pop_because_untracked_files_are_aged_by_ulid(
    tmp_path,
):
    """``git stash -u`` / ``pop`` rewrites an untracked file's mtime; its name never changes."""
    repo, store = project(tmp_path)
    path = record(store, "decisions", ulid_at(30.5))
    git(repo, "stash", "-u", "-q")
    assert not path.exists()
    git(repo, "stash", "pop", "-q")
    assert abs(path.stat().st_mtime - NOW.timestamp()) < 3600  # the mtime is fresh again

    assert "the oldest for 30 hours (1 decision file(s))" in only_problem(UNCOMMITTED, store).line


def test_t3_a_fully_staged_file_is_not_counted_until_it_is_unstaged(tmp_path):
    """A ``doctor --check`` in a pre-commit hook must not block the commit that fixes it."""
    repo, store = project(tmp_path)
    path = record(store, "decisions", ulid_at(30.5))
    age_file(path, 30.5)  # old by mtime as well: a staged file has no other age to fall back on
    git(repo, "add", str(path))

    staged = run_check(UNCOMMITTED, store)
    assert staged.problems == [] and UNCOMMITTED in staged.clean

    git(repo, "reset", "-q")
    assert only_problem(UNCOMMITTED, store).line.startswith("Sidegraph: 1 store file(s)")

    # Staged, then edited again: the worktree column is set, so it still counts.
    git(repo, "add", str(path))
    path.write_text('{"edited": true}', encoding="utf-8")
    age_file(path, 30.5)
    assert only_problem(UNCOMMITTED, store).line.startswith("Sidegraph: 1 store file(s)")


def test_t4_a_rename_entry_consumes_its_source_field(tmp_path):
    """``R`` entries carry the old path as the next NUL field, with no status prefix."""
    repo, store = project(tmp_path)
    old = write(store / "decisions" / "OLD.json")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--no-verify", "-m", "old")
    git(repo, "mv", str(old), str(store / "decisions" / "NEW.json"))
    new = store / "decisions" / "NEW.json"
    new.write_text('{"edited": true}', encoding="utf-8")
    age_file(new, 30.5)
    raw = subprocess.run(
        ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        cwd=repo,
        capture_output=True,
        check=True,
    ).stdout

    assert integrity._parse_status(raw) == [("RM", ".sidegraph/decisions/NEW.json")]
    assert only_problem(UNCOMMITTED, store).line == uncommitted_line(
        store, 1, "30 hours", "1 decision file(s)"
    )


def test_t4_a_path_with_a_space_and_a_non_ascii_name_are_counted_unquoted(tmp_path):
    _repo, store = project(tmp_path)
    for name in ("with space.json", "ключ-é.json"):
        age_file(write(store / "decisions" / name), 30.5)

    assert only_problem(UNCOMMITTED, store).line == uncommitted_line(
        store, 2, "30 hours", "2 decision file(s)"
    )


def test_t4_deleted_hot_files_beside_a_new_archive_segment_read_as_a_compaction(tmp_path):
    repo, store = project(tmp_path)
    write(store / "decisions" / "A.json")
    write(store / "domains" / "D.json")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--no-verify", "-m", "records")
    (store / "decisions" / "A.json").unlink()
    (store / "domains" / "D.json").unlink()
    age_file(write(store / "archive" / "2026-10-01-1-abcdef123456.jsonl"), 30.5)
    for sub in ("decisions", "domains"):  # a deletion is aged by its own subdirectory
        age_file(store / sub, 30.5)

    assert only_problem(UNCOMMITTED, store).line == uncommitted_line(
        store, 3, "30 hours", "a compaction"
    )


def test_t4_a_deletion_without_an_archive_segment_is_a_deleted_file(tmp_path):
    repo, store = project(tmp_path)
    write(store / "decisions" / "A.json")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--no-verify", "-m", "records")
    (store / "decisions" / "A.json").unlink()
    age_file(store / "decisions", 30.5)

    assert only_problem(UNCOMMITTED, store).line == uncommitted_line(
        store, 1, "30 hours", "1 deleted file(s)"
    )


def test_t4_files_outside_the_store_layout_are_not_counted(tmp_path):
    _repo, store = project(tmp_path)
    age_file(write(store / "notes" / "scratch.json"), 90)
    (store / "index.db").write_text("derived", encoding="utf-8")  # ignored by the store's file

    result = run_check(UNCOMMITTED, store)

    assert result.problems == [] and UNCOMMITTED in result.clean


def test_t5_a_store_symlinked_inside_the_repository_maps_through_show_prefix(tmp_path):
    repo = make_repo(tmp_path / "repo")
    seed_store(repo / "data" / "store")
    link = repo / ".sidegraph"
    link.symlink_to(repo / "data" / "store", target_is_directory=True)
    record(repo / "data" / "store", "decisions", ulid_at(30.5))

    problem = only_problem(UNCOMMITTED, link)

    assert problem.line == uncommitted_line(
        link, 1, "30 hours", "1 decision file(s)", where="data/store/"
    )
    assert problem.fix == "commit data/store/"


def test_t5_a_store_symlinked_outside_any_repository_is_not_run(tmp_path):
    repo = make_repo(tmp_path / "repo")
    outside = tmp_path / "outside" / "store"
    record(outside, "decisions", ulid_at(30.5))
    (repo / ".sidegraph").symlink_to(outside, target_is_directory=True)

    result = run_check(UNCOMMITTED, repo / ".sidegraph")

    assert result.problems == [] and UNCOMMITTED not in result.clean


def test_t6_a_store_outside_a_git_repository_is_not_run(tmp_path):
    store = tmp_path / "plain" / ".sidegraph"
    record(store, "decisions", ulid_at(30.5))

    result = run_check(UNCOMMITTED, store)

    assert result.problems == [] and UNCOMMITTED not in result.clean


def test_a_budget_that_is_spent_is_not_run(tmp_path, monkeypatch):
    _repo, store = project(tmp_path)
    record(store, "decisions", ulid_at(30.5))
    monkeypatch.setattr(integrity, "_STRANDED_BUDGET", 0.0)

    result = run_check(UNCOMMITTED, store)

    assert result.problems == [] and UNCOMMITTED not in result.clean


def test_the_detector_writes_nothing_and_leaves_the_index_alone(tmp_path):
    """``--no-optional-locks``: a read must not refresh ``.git/index``."""
    repo, store = project(tmp_path)
    record(store, "decisions", ulid_at(30.5))
    age_file(repo / "a.txt", 2)  # stat-dirty: a plain `git status` would rewrite the index
    index = repo / ".git" / "index"
    stamp = (NOW - timedelta(hours=1)).timestamp()
    os.utime(index, (stamp, stamp))
    before = sorted(p for p in repo.rglob("*") if ".git" not in p.parts)

    only_problem(UNCOMMITTED, store)

    assert sorted(p for p in repo.rglob("*") if ".git" not in p.parts) == before
    assert index.stat().st_mtime == pytest.approx(stamp, abs=1)


# -- D2: branch-only-records -----------------------------------------------------------------


def test_t8_a_branch_of_unmerged_records_gives_the_line_with_the_awaiting_count(tmp_path):
    """2 proposed + 1 accepted count (3, 2 awaiting); a superseded one does not; a merged branch
    is not listed at all."""
    repo, store = project(tmp_path)
    branch_with(
        store,
        "feat",
        {
            "decisions/P1.json": "proposed",
            "decisions/P2.json": "proposed",
            "decisions/A1.json": "accepted",
            "decisions/S1.json": "superseded",
        },
    )
    branch_with(store, "done", {"decisions/M1.json": "proposed"})
    git(repo, "merge", "-q", "--no-ff", "-m", "merge done", "done")

    problem = only_problem(BRANCH_ONLY, store)

    assert problem.line == (
        "Sidegraph: 3 record(s) (2 awaiting ratification) exist only on branches not merged "
        "into main (feat: 3); they reach main when those merge."
    )
    assert problem.notice is None  # the tip is a day old
    assert problem.severity == "advisory"


def test_t8_the_awaiting_part_is_dropped_when_nothing_awaits_ratification(tmp_path):
    _repo, store = project(tmp_path)
    branch_with(store, "feat", {"decisions/A1.json": "accepted", "facts/A2.json": "accepted"})

    assert only_problem(BRANCH_ONLY, store).line == (
        "Sidegraph: 2 record(s) exist only on branches not merged into main (feat: 2); "
        "they reach main when those merge."
    )


def test_t8_a_branch_holding_only_terminal_records_is_clean(tmp_path):
    _repo, store = project(tmp_path)
    branch_with(
        store,
        "feat",
        {
            "decisions/S.json": "superseded",
            "decisions/R.json": "rejected",
            "domains/D.json": "dropped",
        },
    )

    result = run_check(BRANCH_ONLY, store)

    assert result.problems == [] and BRANCH_ONLY in result.clean


def test_t8_the_current_branch_is_excluded_by_full_refname_beside_a_tag_of_the_same_name(
    tmp_path,
):
    """``refname:short`` turns into ``heads/dup`` when a tag shares the name."""
    repo, store = project(tmp_path)
    branch_with(store, "dup", {"decisions/D1.json": "proposed"})
    branch_with(store, "other", {"decisions/O1.json": "proposed"})
    git(repo, "tag", "dup", "main")
    git(repo, "switch", "-q", "dup")

    assert only_problem(BRANCH_ONLY, store).line == (
        "Sidegraph: 1 record(s) (1 awaiting ratification) exist only on branches not merged "
        "into main (other: 1); they reach main when those merge."
    )


def test_t9_records_a_squash_merge_already_put_on_the_default_branch_are_not_counted(tmp_path):
    repo, store = project(tmp_path)
    branch_with(store, "sq", {"decisions/A.json": "proposed", "decisions/B.json": "proposed"})
    git(repo, "merge", "-q", "--squash", "sq")
    git(repo, "commit", "-q", "--no-verify", "-m", "squash sq")

    # `sq` is still "not merged" by ancestry, but both of its records are on main.
    clean = run_check(BRANCH_ONLY, store)
    assert clean.problems == [] and BRANCH_ONLY in clean.clean

    git(repo, "switch", "-q", "sq")
    record(store, "decisions", "C")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--no-verify", "-m", "more", date=NOW - timedelta(days=1))
    git(repo, "switch", "-q", "main")

    assert only_problem(BRANCH_ONLY, store).line == (
        "Sidegraph: 1 record(s) (1 awaiting ratification) exist only on branches not merged "
        "into main (sq: 1); they reach main when those merge."
    )


def test_t10_the_pathspec_is_relative_to_the_store_so_a_nested_store_is_scanned(tmp_path):
    repo = make_repo(tmp_path / "repo")
    store = seed_store(repo / "apps" / "web" / ".sidegraph")
    branch_with(store, "feat", {"decisions/N1.json": "proposed", "facts/N2.json": "accepted"})

    assert only_problem(BRANCH_ONLY, store).line == (
        "Sidegraph: 2 record(s) (1 awaiting ratification) exist only on branches not merged "
        "into main (feat: 2); they reach main when those merge."
    )


def test_t11_the_default_branch_is_origin_head_when_it_is_set(tmp_path):
    """A PR-only flow: local ``main`` is ahead, ``origin/main`` lags. ``feat`` is merged into
    local ``main`` and is still unmerged into the default."""
    repo, store = project(tmp_path)
    base = git(repo, "rev-parse", "HEAD").strip()
    git(repo, "update-ref", "refs/remotes/origin/main", base)
    git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    branch_with(store, "feat", {"decisions/F1.json": "proposed"})
    git(repo, "merge", "-q", "--no-ff", "-m", "merge feat", "feat")
    git(repo, "switch", "-q", "-c", "work", base)

    line = only_problem(BRANCH_ONLY, store).line

    # `feat` and the local `main` that merged it both hold F1: counted once, for the newer tip.
    assert line == (
        "Sidegraph: 1 record(s) (1 awaiting ratification) exist only on branches not merged "
        "into origin/main (main: 1); they reach origin/main when those merge."
    )


def test_t11_without_origin_head_main_is_the_default_and_wins_over_master(tmp_path):
    repo, store = project(tmp_path)
    git(repo, "branch", "master", "main")
    branch_with(store, "feat", {"decisions/F1.json": "proposed"})

    assert "not merged into main (feat: 1)" in only_problem(BRANCH_ONLY, store).line


@pytest.mark.parametrize("remote", ["main", "master"])
def test_t11_without_origin_head_origin_main_or_master_comes_before_a_lagging_local_main(
    tmp_path, remote
):
    """No ``origin/HEAD``: the remote branch the PR flow merges into still beats a local
    ``main`` that has not pulled."""
    repo, store = project(tmp_path)
    branch_with(store, "feat", {"decisions/F1.json": "proposed"})
    git(repo, "update-ref", f"refs/remotes/origin/{remote}", git(repo, "rev-parse", "feat").strip())

    result = run_check(BRANCH_ONLY, store)

    assert result.problems == [] and BRANCH_ONLY in result.clean


def test_t11_master_is_the_default_when_there_is_no_main(tmp_path):
    repo = make_repo(tmp_path / "repo", branch="master")
    store = seed_store(repo / ".sidegraph")
    branch_with(store, "feat", {"decisions/F1.json": "proposed"}, base="master")

    assert "not merged into master (feat: 1)" in only_problem(BRANCH_ONLY, store).line


def test_t11_no_default_branch_is_not_run(tmp_path):
    repo = make_repo(tmp_path / "repo", branch="trunk")
    store = seed_store(repo / ".sidegraph")
    branch_with(store, "feat", {"decisions/F1.json": "proposed"}, base="trunk")

    result = run_check(BRANCH_ONLY, store)

    assert result.problems == [] and BRANCH_ONLY not in result.clean


def test_t11_a_dangling_origin_head_falls_back_to_the_local_branch(tmp_path):
    repo, store = project(tmp_path)
    git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/gone")
    branch_with(store, "feat", {"decisions/F1.json": "proposed"})

    assert "not merged into main (feat: 1)" in only_problem(BRANCH_ONLY, store).line


def test_t12_every_branch_is_scanned_so_the_five_oldest_of_thirty_are_found(tmp_path, monkeypatch):
    """Red against rev 1's cap of the 20 newest branches: it dropped exactly the stale ones.
    The budget is not what is tested here: a loaded machine once spent the real 2 s on the
    ~45 git calls and got the (correct) partial line, so this test gives the scan time."""
    monkeypatch.setattr(integrity, "_STRANDED_BUDGET", 60.0)
    repo, store = project(tmp_path)
    for i in range(5):  # the oldest five hold records, oldest first
        branch_with(
            store, f"b{i:02d}", {f"decisions/R{i}.json": "accepted"}, age=timedelta(days=40 - i)
        )
    for i in range(5, 30):
        empty_branch(repo, f"b{i:02d}", timedelta(days=40 - i))

    problem = only_problem(BRANCH_ONLY, store)

    assert problem.line == (
        "Sidegraph: 5 record(s) exist only on branches not merged into main "
        "(b00: 1, b01: 1, b02: 1, and 2 more branches); they reach main when those merge."
    )
    assert problem.notice is not None and "b00, b01, b02, …" in problem.notice


def test_t13_a_spent_budget_before_any_hit_is_not_run_so_the_notice_key_survives(
    tmp_path, monkeypatch
):
    _repo, store = project(tmp_path)
    branch_with(store, "old", {"decisions/O.json": "proposed"}, age=timedelta(days=9))
    branch_with(store, "new", {"decisions/N.json": "proposed"}, age=timedelta(days=2))
    times_out_at_diff(monkeypatch, nth=1)

    result = run_check(BRANCH_ONLY, store)

    assert result.problems == [] and BRANCH_ONLY not in result.clean


def test_t13_a_spent_budget_after_a_hit_marks_the_result_partial(tmp_path, monkeypatch):
    _repo, store = project(tmp_path)
    branch_with(store, "old", {"decisions/O.json": "proposed"}, age=timedelta(days=9))
    branch_with(store, "new", {"decisions/N.json": "proposed"}, age=timedelta(days=2))
    times_out_at_diff(monkeypatch, nth=2)

    problem = only_problem(BRANCH_ONLY, store)

    assert problem.line == (
        "Sidegraph: at least 1 record(s) (1 awaiting ratification) exist only on branches not "
        "merged into main (old: 1); they reach main when those merge. The branch scan ran out "
        "of time before it looked at every branch."
    )


def times_out_at_diff(monkeypatch, nth: int) -> None:
    """Make the ``nth`` ``git diff`` of the scan time out, as a spent budget does."""
    real = subprocess.run
    seen = {"diff": 0}

    def fake(cmd, *args, **kwargs):
        if len(cmd) > 1 and cmd[1] == "diff":
            seen["diff"] += 1
            if seen["diff"] >= nth:
                raise subprocess.TimeoutExpired(cmd, 1)
        return real(cmd, *args, **kwargs)

    monkeypatch.setattr(integrity.subprocess, "run", fake)


def test_t14_a_tip_older_than_a_week_gives_the_notice_and_a_newer_one_does_not(tmp_path):
    _repo, store = project(tmp_path)
    branch_with(store, "stale", {"decisions/S.json": "proposed"}, age=timedelta(days=8))
    branch_with(store, "recent", {"decisions/R.json": "proposed"}, age=timedelta(days=6))

    problem = only_problem(BRANCH_ONLY, store)

    assert problem.line is not None and "stale: 1" in problem.line and "recent: 1" in problem.line
    assert problem.notice == (
        "Sidegraph: 1 record(s) sit on branches untouched for over a week (stale): merge those "
        "branches, or the records never reach main."
    )


def test_t14_six_days_is_no_notice(tmp_path):
    """Red against a notice with no tip-age gate."""
    _repo, store = project(tmp_path)
    branch_with(store, "recent", {"decisions/R.json": "proposed"}, age=timedelta(days=6))

    problem = only_problem(BRANCH_ONLY, store)

    assert problem.line is not None and problem.notice is None


def test_a_store_outside_a_repository_is_not_run_by_the_branch_check(tmp_path):
    store = tmp_path / "plain" / ".sidegraph"
    record(store, "decisions", "X")

    result = run_check(BRANCH_ONLY, store)

    assert result.problems == [] and BRANCH_ONLY not in result.clean


def test_the_branch_scan_writes_nothing(tmp_path):
    repo, store = project(tmp_path)
    branch_with(store, "feat", {"decisions/F.json": "proposed"})
    heads = git(repo, "for-each-ref").strip()
    before = sorted(p for p in repo.rglob("*") if ".git" not in p.parts)

    only_problem(BRANCH_ONLY, store)

    assert git(repo, "for-each-ref").strip() == heads
    assert sorted(p for p in repo.rglob("*") if ".git" not in p.parts) == before


# -- T7: doctor and stats --------------------------------------------------------------------


def healthy_store_in_a_repository(tmp_path: Path) -> tuple[Path, Path]:
    """``(repo, store)``: a healthy committed store at ``<repo>/store``, then one of its decision
    files edited (valid JSON) with an mtime 30.5 h old."""
    store = _settled_healthy(tmp_path)
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "--no-verify", "-m", "store")
    edited = next((store / "decisions").glob("*.json"))
    edited.write_text(edited.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    age_file(edited, 30.5)
    return tmp_path, store


def test_t7_doctor_has_one_finding_with_the_store_path_and_the_text_without_its_prefix(
    tmp_path,
):
    from sidegraph.doctor import STORE_UNCOMMITTED  # the new constant: ImportError when unfixed

    _repo, store = healthy_store_in_a_repository(tmp_path)

    findings = [f for f in curate(store).findings if f.code == STORE_UNCOMMITTED]

    assert STORE_UNCOMMITTED == "store-uncommitted"
    assert [(f.path, f.detail) for f in findings] == [
        (
            str(store),
            f"1 store file(s) in {store} are not committed, the oldest for 30 hours "
            "(1 decision file(s)), so other checkouts and teammates do not see them. "
            "Commit store/ in a pull request.",
        )
    ]


def test_t7_check_fails_on_old_uncommitted_files_and_passes_once_they_are_staged(tmp_path, capsys):
    """A ``doctor --check`` in a pre-commit hook must not block the commit that fixes it."""
    repo, store = healthy_store_in_a_repository(tmp_path)

    assert doctor_main(["--db", str(store), "--check"]) == 2
    assert "store-uncommitted" in capsys.readouterr().out

    git(repo, "add", "-A")
    assert doctor_main(["--db", str(store), "--check"]) == 0
    assert "store-uncommitted" not in capsys.readouterr().out


def test_t7_stats_has_a_health_item(tmp_path):
    _repo, store = healthy_store_in_a_repository(tmp_path)

    health = build_report(store, None, window_days=30, now=datetime.now(UTC)).health

    assert health == [
        HealthItem(
            check=UNCOMMITTED,
            severity="degraded",
            summary="1 store file(s) uncommitted",
            fix="commit store/",
        )
    ]


# -- SessionStart ----------------------------------------------------------------------------


def test_session_start_tells_the_model_and_the_human_about_both_stranded_writes(
    tmp_path, monkeypatch, capsys
):
    """An untracked record two days old on ``main``, and a proposal on a branch whose tip is
    eight days old."""
    repo = make_repo(tmp_path / "repo")
    store_dir = repo / ".sidegraph"
    settled(Store(store_dir)).close()
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--no-verify", "-m", "store")
    branch_with(
        store_dir, "old-work", {"decisions/BRANCHED.json": "proposed"}, age=timedelta(days=8)
    )
    store = Store(store_dir)
    add_record(store, age_days=2)
    store.close()

    out = start(monkeypatch, capsys, store_dir, project=repo)

    assert "are not committed, the oldest for 2 days" in context(out)
    assert "not merged into main (old-work: 1)" in context(out)
    assert "are not committed, the oldest for 2 days" in out["systemMessage"]
    assert "untouched for over a week (old-work)" in out["systemMessage"]


def test_t6_a_store_outside_git_keeps_a_recorded_uncommitted_notice(tmp_path, monkeypatch, capsys):
    """Not run is not clean: the key survives, so a pull that moves the store out of sight does
    not reset the once-a-day rule."""
    store_dir = tmp_path / ".sidegraph"
    store = settled(Store(store_dir))
    key = NOTICE_KEY + UNCOMMITTED
    stamp = f"degraded|{NOW.isoformat()}"
    store.set_meta(key, stamp)
    store.close()

    start(monkeypatch, capsys, store_dir)

    store = Store(store_dir)
    try:
        assert store.get_meta(key) == stamp
    finally:
        store.close()


# -- review amendments -----------------------------------------------------------------------


def test_a_record_on_stacked_branches_counts_once_for_the_newest_branch(tmp_path):
    """feat2 is cut from feat1, so R1 rides on both: it is one record, held by the live branch,
    and the stale feat1 holds nothing of its own."""
    repo, store = project(tmp_path)
    branch_with(store, "feat1", {"decisions/R1.json": "proposed"}, age=timedelta(days=10))
    branch_with(
        store, "feat2", {"decisions/R2.json": "proposed"}, age=timedelta(days=1), base="feat1"
    )
    git(repo, "switch", "-q", "main")

    problem = only_problem(BRANCH_ONLY, store)

    assert problem.line == (
        "Sidegraph: 2 record(s) (2 awaiting ratification) exist only on branches not merged "
        "into main (feat2: 2); they reach main when those merge."
    )
    assert problem.notice is None


def test_a_local_main_ahead_of_origin_counts_each_record_once(tmp_path):
    repo, store = project(tmp_path)
    base = git(repo, "rev-parse", "HEAD").strip()
    git(repo, "update-ref", "refs/remotes/origin/main", base)
    git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    branch_with(store, "feat1", {"decisions/A.json": "proposed", "decisions/B.json": "proposed"})
    branch_with(store, "feat2", {"decisions/C.json": "proposed"})
    git(repo, "merge", "-q", "--no-ff", "-m", "merge feat1", "feat1")
    git(repo, "merge", "-q", "--no-ff", "-m", "merge feat2", "feat2")
    git(repo, "switch", "-q", "-c", "work", base)

    assert only_problem(BRANCH_ONLY, store).line == (
        "Sidegraph: 3 record(s) (3 awaiting ratification) exist only on branches not merged "
        "into origin/main (main: 3); they reach origin/main when those merge."
    )


def test_a_record_that_is_terminal_on_the_newest_branch_is_not_counted(tmp_path):
    """The newest branch holding a record decides its status: superseded there, it is closed,
    though the older branch still holds it as proposed."""
    repo, store = project(tmp_path)
    branch_with(store, "feat1", {"decisions/R1.json": "proposed"}, age=timedelta(days=3))
    git(repo, "switch", "-q", "-c", "feat2", "feat1")
    write(store / "decisions" / "R1.json", {"id": "R1", "status": "superseded"})
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--no-verify", "-m", "supersede", date=NOW - timedelta(days=1))
    git(repo, "switch", "-q", "main")

    result = run_check(BRANCH_ONLY, store)

    assert result.problems == [] and BRANCH_ONLY in result.clean


def test_the_diff_does_not_follow_a_users_diff_relative_setting(tmp_path, monkeypatch):
    """With ``diff.relative`` set, paths come back store-relative and ``<rev>:<path>`` lookups
    miss: a false clean, which would also delete a recorded notice key."""
    _repo, store = project(tmp_path)
    branch_with(store, "feat", {"decisions/F1.json": "proposed"})
    config = tmp_path / "user.gitconfig"
    config.write_text("[diff]\n\trelative = true\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))

    assert "(feat: 1)" in only_problem(BRANCH_ONLY, store).line


def test_a_new_record_beside_a_deleted_similar_one_is_still_an_addition(tmp_path):
    """A supersede then a compact deletes X and adds a similar Y; rename detection would
    report ``R`` and ``--diff-filter=A`` would drop Y."""
    repo, store = project(tmp_path)
    body = {"title": "Retries live in the adapter", "context": "429 bursts " * 40, "kind": "lesson"}
    write(store / "decisions" / "X.json", dict(body, id="X", status="accepted"))
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--no-verify", "-m", "x")
    git(repo, "switch", "-q", "-c", "feat", "main")
    git(repo, "rm", "-q", str(store / "decisions" / "X.json"))
    write(store / "decisions" / "Y.json", dict(body, id="Y", status="proposed"))
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--no-verify", "-m", "y", date=NOW - timedelta(days=1))
    git(repo, "switch", "-q", "main")

    assert "(feat: 1)" in only_problem(BRANCH_ONLY, store).line


@pytest.mark.parametrize(
    "name",
    ["00000000000000000000000000", str(ULID.from_datetime(NOW + timedelta(days=3)))],
    ids=["before-2020", "days-in-the-future"],
)
def test_an_implausible_ulid_name_is_aged_by_mtime(tmp_path, name):
    """A 26-character Crockford name is not always a ULID this store minted."""
    _repo, store = project(tmp_path)
    age_file(write(store / "decisions" / f"{name}.json"), 30.5)

    assert only_problem(UNCOMMITTED, store).line == uncommitted_line(
        store, 1, "30 hours", "1 decision file(s)"
    )
