"""The settled clock and the dirty set, against real git repositories (clock half of T-C1).

The git version is printed by ``test_records_git_version`` so a CI run shows what it ran on.
# see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§6.2, §9 T-C1)
"""

# ruff: noqa: F811  (the imported ``git_repo`` fixture is redefined as a test argument)
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import sidegraph.maintenance_clock as mc
from sidegraph.maintenance_clock import (
    ClockError,
    dirty_paths,
    git_ls_files_shards,
    landing_times,
    parse_log_z,
    read_head,
    resolve_work_tree,
)
from tests.store_format2_support import commit_all, git, git_env, git_repo  # noqa: F401

T1 = "2026-01-01T00:00:00+0000"
T2 = "2026-02-01T00:00:00+0000"
T3 = "2026-03-01T00:00:00+0000"
E1, E2, E3 = 1767225600, 1769904000, 1772323200


def _write(repo: Path, rel: str, text: str = "x\n") -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _commit(repo: Path, msg: str, date: str) -> str:
    return commit_all(repo, msg, date=date)


def _git_dated(repo: Path, date: str, *args: str) -> str:
    env = {**git_env(), "GIT_COMMITTER_DATE": date, "GIT_AUTHOR_DATE": date}
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        env=env,
    ).stdout


@pytest.fixture(autouse=True)
def _clean_git_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # The code under test builds its own env from os.environ; keep the user's config out.
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")


def test_parser_strips_leading_newline_before_first_path() -> None:  # red: no strip of the \n
    assert parse_log_z(b"\x011792450800\x00\n.sidegraph/decisions/A.json\x00") == {
        ".sidegraph/decisions/A.json": 1792450800
    }


def test_parser_keeps_newest_time_per_path() -> None:  # red: last header wins instead of newest
    stream = b"\x012000\x00\na.json\x00\x011000\x00\na.json\x00b.json\x00"
    assert parse_log_z(stream) == {"a.json": 2000, "b.json": 1000}


def test_parser_does_not_split_on_x01_inside_a_path() -> None:  # red: split on \x01
    stream = b"\x01500\x00\ndir\x01x/a.json\x00"
    assert parse_log_z(stream) == {"dir\x01x/a.json": 500}


# -- clock against real git ------------------------------------------------------------------------


def test_landing_times_basic_and_subdirectory(git_repo: Path) -> None:
    _write(git_repo, ".sidegraph/decisions/A.json")
    _commit(git_repo, "a", T1)
    _write(git_repo, ".sidegraph/decisions/A.json", "y\n")
    _write(git_repo, ".sidegraph/decisions/B.json")
    _commit(git_repo, "ab", T2)
    _write(git_repo, "other.txt")
    _commit(git_repo, "other", T3)
    wt = resolve_work_tree(git_repo / ".sidegraph")
    assert wt is not None and wt.store_rel == ".sidegraph" and not wt.shallow
    assert landing_times(wt) == {
        ".sidegraph/decisions/A.json": E2,
        ".sidegraph/decisions/B.json": E2,
    }
    assert wt.head == read_head(wt) == git(git_repo, "rev-parse", "HEAD").strip()


def _merge_setup(repo: Path) -> None:
    _write(repo, "seed.txt")
    _commit(repo, "seed", T1)
    git(repo, "checkout", "-q", "-b", "topic")
    _write(repo, ".sidegraph/decisions/TOPIC.json")
    _commit(repo, "topic file", T1)
    git(repo, "checkout", "-q", "main")
    _write(repo, ".sidegraph/decisions/MAIN.json")
    _commit(repo, "main file", T2)
    _git_dated(repo, T3, "merge", "-q", "--no-ff", "-m", "merge topic", "topic")


def test_noff_merge_dated_by_merge_under_diffmerges_off(git_repo: Path) -> None:
    # Guards TOPIC's merge date under the user's `log.diffMerges=off`. Red only when both
    # `--first-parent` and the explicit diff-merges flags are gone: git 2.54's `--first-parent`
    # implies `--diff-merges=first-parent`, so the explicit flags are insurance for older git.
    git(git_repo, "config", "log.diffMerges", "off")
    _merge_setup(git_repo)
    wt = resolve_work_tree(git_repo / ".sidegraph")
    assert wt is not None
    times = landing_times(wt)
    assert times[".sidegraph/decisions/TOPIC.json"] == E3
    assert times[".sidegraph/decisions/MAIN.json"] == E2


def test_fast_forward_file_dated_by_branch_commit(git_repo: Path) -> None:
    # recorded limit: a fast-forward creates no landing commit
    _write(git_repo, "seed.txt")
    _commit(git_repo, "seed", T1)
    git(git_repo, "checkout", "-q", "-b", "topic")
    _write(git_repo, ".sidegraph/decisions/F.json")
    _commit(git_repo, "topic file", T1)
    git(git_repo, "checkout", "-q", "main")
    _git_dated(git_repo, T3, "merge", "-q", "--ff-only", "topic")
    wt = resolve_work_tree(git_repo / ".sidegraph")
    assert wt is not None
    assert landing_times(wt) == {".sidegraph/decisions/F.json": E1}


def test_db_elsewhere_and_subdirectory_run(git_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # red: literal .sidegraph pathspec from cwd (a subdirectory lists nothing)
    _write(git_repo, "app/data/store/decisions/A.json")
    _write(git_repo, ".sidegraph/decisions/Z.json")
    _commit(git_repo, "a", T2)
    sub = git_repo / "sub"
    sub.mkdir()
    monkeypatch.chdir(sub)
    wt = resolve_work_tree(git_repo / "app" / "data" / "store")
    assert wt is not None and wt.store_rel == "app/data/store"
    assert landing_times(wt) == {"app/data/store/decisions/A.json": E2}
    wt2 = resolve_work_tree(git_repo / ".sidegraph")
    assert wt2 is not None
    assert landing_times(wt2) == {".sidegraph/decisions/Z.json": E2}


def test_non_ascii_store_dir_sole_path_of_commit(git_repo: Path) -> None:
    # NUL framing: core.quotePath would otherwise print "\321\205..." and a single-file
    # commit's path would be the one after the stripped \n
    git(git_repo, "config", "core.quotePath", "true")
    _write(git_repo, "хранилище/decisions/А.json")
    _commit(git_repo, "only", T2)
    wt = resolve_work_tree(git_repo / "хранилище")
    assert wt is not None
    assert landing_times(wt) == {"хранилище/decisions/А.json": E2}


def test_control_character_store_path_refused(git_repo: Path) -> None:
    weird = git_repo / "a\x01b"
    weird.mkdir()
    _write(git_repo, "a\x01b/x.json")
    with pytest.raises(ClockError):
        resolve_work_tree(weird)


def test_shallow_clone_flagged(git_repo: Path, tmp_path: Path) -> None:
    _write(git_repo, ".sidegraph/decisions/A.json")
    _commit(git_repo, "one", T1)
    _write(git_repo, ".sidegraph/decisions/B.json")
    _commit(git_repo, "two", T2)
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "-q", "--depth", "1", f"file://{git_repo}", str(clone))
    store = clone / ".sidegraph"
    wt = resolve_work_tree(store)
    assert wt is not None and wt.shallow
    full = resolve_work_tree(git_repo / ".sidegraph")
    assert full is not None and not full.shallow


def test_db_outside_current_repository_has_no_clock(
    tmp_path: Path, git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = tmp_path / "plain" / ".sidegraph"
    outside.mkdir(parents=True)
    monkeypatch.chdir(git_repo)  # the cwd is a repository; the store is not in it
    assert resolve_work_tree(outside) is None
    assert resolve_work_tree(tmp_path / "missing") is None


# -- dirty set -------------------------------------------------------------------------------------


def _committed_store(repo: Path) -> str:
    _write(repo, ".sidegraph/decisions/A.json")
    _write(repo, ".sidegraph/decisions/B.json")
    _write(repo, "outside.txt")
    return _commit(repo, "base", T1)


def test_dirty_set_from_subdirectory_with_diff_relative(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # red: dirty commands from cwd (diff.relative=true prints nothing for the store path)
    head = _committed_store(git_repo)
    git(git_repo, "config", "diff.relative", "true")
    _write(git_repo, ".sidegraph/decisions/A.json", "changed\n")
    _write(git_repo, "outside.txt", "changed\n")
    sub = git_repo / "sub"
    sub.mkdir()
    monkeypatch.chdir(sub)
    wt = resolve_work_tree(git_repo / ".sidegraph")
    assert wt is not None
    assert dirty_paths(wt, head) == {".sidegraph/decisions/A.json"}


def test_assume_unchanged_and_skip_worktree_are_dirty(git_repo: Path) -> None:
    # red: only `git diff` consulted (it reports both as clean)
    head = _committed_store(git_repo)
    wt = resolve_work_tree(git_repo / ".sidegraph")
    assert wt is not None
    assert dirty_paths(wt, head) == frozenset()
    git(git_repo, "update-index", "--assume-unchanged", ".sidegraph/decisions/A.json")
    git(git_repo, "update-index", "--skip-worktree", ".sidegraph/decisions/B.json")
    assert dirty_paths(wt, head) == {
        ".sidegraph/decisions/A.json",
        ".sidegraph/decisions/B.json",
    }


def test_untracked_is_dirty(git_repo: Path) -> None:  # red: ls-files --others omitted
    head = _committed_store(git_repo)
    _write(git_repo, ".sidegraph/decisions/NEW.json")
    wt = resolve_work_tree(git_repo / ".sidegraph")
    assert wt is not None
    assert dirty_paths(wt, head) == {".sidegraph/decisions/NEW.json"}


def test_ls_files_shards_keeps_git_case(git_repo: Path) -> None:
    _write(git_repo, ".sidegraph/shards/0A.SHARD", "")
    _write(git_repo, ".sidegraph/decisions/A.json")
    _commit(git_repo, "shard", T1)
    wt = resolve_work_tree(git_repo / ".sidegraph")
    assert wt is not None
    assert git_ls_files_shards(wt) == [".sidegraph/shards/0A.SHARD"]


# -- review round 1 --------------------------------------------------------------------------------


def test_skewed_dates_newest_commit_is_first_occurrence(git_repo: Path) -> None:
    # parent dated March, child dated February: the child is the newest commit, so Feb wins
    # red: keep-the-highest-time parser dates the file March
    _write(git_repo, ".sidegraph/decisions/A.json")
    _commit(git_repo, "parent", T3)
    _write(git_repo, ".sidegraph/decisions/A.json", "y\n")
    _commit(git_repo, "child", T2)
    wt = resolve_work_tree(git_repo / ".sidegraph")
    assert wt is not None
    assert landing_times(wt) == {".sidegraph/decisions/A.json": E2}
    assert parse_log_z(b"\x012000\x00\na.json\x00\x019000\x00\na.json\x00") == {"a.json": 2000}


def test_dubious_ownership_is_an_error_not_no_clock(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # red: every failure mapped to None (selects the no-clock rule)
    _write(git_repo, ".sidegraph/decisions/A.json")
    _commit(git_repo, "a", T1)
    monkeypatch.setenv("GIT_TEST_ASSUME_DIFFERENT_OWNER", "1")
    with pytest.raises(ClockError, match="dubious ownership"):
        resolve_work_tree(git_repo / ".sidegraph")


def _failing_run(exc: Exception | None, returncode: int = 128, stderr: bytes = b"fatal: boom"):
    def run(cmd, **kwargs):
        if exc is not None:
            raise exc
        return subprocess.CompletedProcess(cmd, returncode, b"", stderr)

    return run


@pytest.mark.parametrize(
    "exc", [subprocess.TimeoutExpired("git", 1), FileNotFoundError("git"), None]
)
def test_failing_git_raises_clock_error_everywhere(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, exc: Exception | None
) -> None:
    # red: resolve_work_tree / read_head swallow the failure into None / ""
    _write(git_repo, ".sidegraph/decisions/A.json")
    _commit(git_repo, "a", T1)
    wt = resolve_work_tree(git_repo / ".sidegraph")
    assert wt is not None
    monkeypatch.setattr(mc.subprocess, "run", _failing_run(exc))
    with pytest.raises(ClockError):
        resolve_work_tree(git_repo / ".sidegraph")
    with pytest.raises(ClockError):
        read_head(wt)
    with pytest.raises(ClockError):
        landing_times(wt)
    with pytest.raises(ClockError):
        dirty_paths(wt, "HEAD")
    with pytest.raises(ClockError):
        git_ls_files_shards(wt)


def test_unborn_branch_head_empty_clock_empty_everything_dirty(git_repo: Path) -> None:
    # red: unborn raises "bad revision" (clock) or diffs against ""
    _write(git_repo, ".sidegraph/decisions/A.json")
    git(git_repo, "add", "-A")  # staged, never committed
    _write(git_repo, ".sidegraph/decisions/U.json")
    wt = resolve_work_tree(git_repo / ".sidegraph")
    assert wt is not None and wt.head == "" and read_head(wt) == ""
    assert landing_times(wt) == {}
    assert dirty_paths(wt, "") == {".sidegraph/decisions/A.json", ".sidegraph/decisions/U.json"}


def test_root_directory_name_with_newline(tmp_path: Path) -> None:
    # red: --show-toplevel output split on newlines -> None
    root = tmp_path / "root\nnewline"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    _write(root, ".sidegraph/decisions/A.json")
    _commit(root, "a", T2)
    wt = resolve_work_tree(root / ".sidegraph")
    assert wt is not None and wt.root.name == "root\nnewline"
    assert landing_times(wt) == {".sidegraph/decisions/A.json": E2}


@pytest.mark.parametrize("name", [":(literal)store", "st*", "[a]", ":lead", "a?"])
def test_store_name_is_a_literal_pathspec(git_repo: Path, name: str) -> None:
    # red: interpreted pathspec -> the sibling's files appear
    siblings = ["store", "stx", "a", "lead", "ab", ":(literal)store"]
    for d in {name, *siblings}:
        _write(git_repo, f"{d}/decisions/A.json")
        _write(git_repo, f"{d}/shards/0A.SHARD", "")
    head = _commit(git_repo, "all", T2)
    git(git_repo, "update-index", "--assume-unchanged", f"{name}/decisions/A.json")
    for d in siblings:
        if d != name:
            _write(git_repo, f"{d}/decisions/NEW.json")
    wt = resolve_work_tree(git_repo / name)
    assert wt is not None and wt.store_rel == name
    assert set(landing_times(wt)) == {f"{name}/decisions/A.json", f"{name}/shards/0A.SHARD"}
    assert git_ls_files_shards(wt) == [f"{name}/shards/0A.SHARD"]
    assert dirty_paths(wt, head) == {f"{name}/decisions/A.json"}


def test_store_at_work_tree_root_is_refused(git_repo: Path) -> None:
    # red: allowing the root breaks store_rel + "/" and the header guard
    _write(git_repo, "decisions/A.json")
    _write(git_repo, "\x01123", "spoof")
    _commit(git_repo, "root store", T2)
    with pytest.raises(ClockError, match="subdirectory"):
        resolve_work_tree(git_repo)
