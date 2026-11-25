"""Real-git coverage for the briefing working set (D6, K12)."""

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from sidegraph.host.briefing import git_files
from sidegraph.host.briefing.git_files import changed_files


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-b", "main")
    (tmp_path / "base.py").write_text("base\n")
    (tmp_path / ".gitignore").write_text("ignored.txt\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "base")
    return tmp_path


def test_a_local_main_without_a_remote_is_the_base(repo: Path) -> None:
    _git(repo, "switch", "-c", "feat")
    (repo / "committed.py").touch()
    _git(repo, "add", "committed.py")
    _git(repo, "commit", "-m", "feature")
    (repo / "staged.py").touch()
    _git(repo, "add", "staged.py")
    (repo / "base.py").write_text("working tree\n")
    (repo / "new.txt").touch()
    (repo / "ignored.txt").touch()
    assert changed_files(repo, repo / ".sidegraph") == (
        "base.py",
        "committed.py",
        "new.txt",
        "staged.py",
    )


def test_a_file_staged_and_then_restored_is_in_the_set(repo: Path) -> None:
    (repo / "base.py").write_text("staged\n")
    _git(repo, "add", "base.py")
    (repo / "base.py").write_text("base\n")
    assert changed_files(repo, repo / ".sidegraph") == ("base.py",)


def test_a_clean_default_branch_has_no_working_set(repo: Path) -> None:
    assert changed_files(repo, repo / ".sidegraph") == ()


def test_a_detached_head_diffs_against_the_default_branch(repo: Path) -> None:
    _git(repo, "switch", "--detach")
    (repo / "base.py").write_text("detached\n")
    _git(repo, "commit", "-am", "detached")
    assert changed_files(repo, repo / ".sidegraph") == ("base.py",)


def test_origin_head_pointing_at_another_branch_is_the_base(repo: Path) -> None:
    _git(repo, "branch", "-m", "trunk")
    _git(repo, "branch", "main")
    (repo / "trunk.py").touch()
    _git(repo, "add", "trunk.py")
    _git(repo, "commit", "-m", "trunk")
    _git(repo, "clone", ".", "clone")
    clone = repo / "clone"
    (clone / "feature.py").touch()
    _git(clone, "add", "feature.py")
    _git(clone, "commit", "-m", "feature")
    assert changed_files(clone, clone / ".sidegraph") == ("feature.py",)


def test_outside_a_repository_and_without_git_the_set_is_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert changed_files(tmp_path, tmp_path / ".sidegraph") == ()
    _git(tmp_path, "init", "-b", "main")
    (tmp_path / "new.py").touch()
    monkeypatch.setenv("PATH", str(tmp_path))
    assert changed_files(tmp_path, tmp_path / ".sidegraph") == ()


def test_each_call_gets_what_is_left_of_the_deadline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    timeouts: list[float] = []

    def run(args: list[str], root: Path, *, capture_output: bool, timeout: float):
        timeouts.append(timeout)
        return subprocess.CompletedProcess(args, 0, b"")

    monkeypatch.setattr(git_files, "run_git", run)
    monkeypatch.setattr(git_files, "time", SimpleNamespace(monotonic=lambda: len(timeouts) * 0.7))
    assert changed_files(tmp_path, tmp_path / ".sidegraph") == ()
    assert timeouts == pytest.approx([1.0, 1.0, 0.6])


def test_paths_are_relative_to_the_root_and_the_store_is_left_out(repo: Path) -> None:
    root = repo / "sub"
    store = root / "conf" / "sg"
    sibling = root / "conf" / "sg-notes"
    store.mkdir(parents=True)
    sibling.mkdir()
    (store / "index.db").touch()
    (sibling / "n.md").touch()
    (root / "a.py").touch()
    inside = root / "tracked.py"
    outside = repo / "other.py"
    for path in (inside, outside):
        path.write_text("before\n")
    _git(repo, "add", "sub/tracked.py", "other.py")
    _git(repo, "commit", "-m", "tracked paths")
    for path in (inside, outside):
        path.write_text("after\n")
    assert changed_files(root, store) == ("a.py", "conf/sg-notes/n.md", "tracked.py")


def test_a_relative_root_gives_the_same_set(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (repo / "new.py").touch()
    monkeypatch.chdir(repo)
    assert changed_files(Path("."), Path(".sidegraph")) == ("new.py",)


def test_a_branch_with_no_commit_keeps_its_staged_file(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-b", "main")
    (tmp_path / "staged.py").touch()
    _git(tmp_path, "add", "staged.py")
    assert changed_files(tmp_path, tmp_path / ".sidegraph") == ("staged.py",)


def test_an_orphan_branch_beside_main_keeps_its_staged_file(repo: Path) -> None:
    _git(repo, "switch", "--orphan", "feat")
    (repo / "staged.py").touch()
    _git(repo, "add", "staged.py")
    assert changed_files(repo, repo / ".sidegraph") == ("staged.py",)


def test_at_most_five_git_processes(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run_git = git_files.run_git
    calls = []

    def run(args, *rest, **options):
        calls.append(args)
        return run_git(args, *rest, **options)

    monkeypatch.setattr(git_files, "run_git", run)
    assert changed_files(repo, repo / ".sidegraph") == ()
    assert len(calls) == 5


def test_a_git_timeout_discards_the_working_set(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def run(args, *rest, **options):
        raise subprocess.TimeoutExpired(args, options["timeout"])

    monkeypatch.setattr(git_files, "run_git", run)
    assert changed_files(repo, repo / ".sidegraph") == ()
