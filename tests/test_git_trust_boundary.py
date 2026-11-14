"""The git choke point: the discovery guard, the protective settings, the argv it sends.

Every trap here plants a harmless marker script under ``tmp_path``; see
``tests/git_trap_support.py``.
# see design/superpowers/specs/2026-11-12-trust-boundary-clone-content-design.md (D1 to D3)
"""

from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from sidegraph import gitenv, gitio
from sidegraph import store as store_mod
from sidegraph.cli import doctor_main
from sidegraph.engine.reader import GraphifyReader
from sidegraph.gitenv import (
    PROTECTIVE_CONFIG,
    UnsafeGitDirectory,
    embedded_git_dir,
    protected_env,
    run_git,
)
from sidegraph.stats.model import build_report
from sidegraph.store import Store
from tests.git_trap_support import (
    git_version,
    plant_git_dir,
    plant_marker,
    raw_git,
    trap_is_armed,
)
from tests.test_host_integrity import start

_IDENTITY = ("-c", "user.name=t", "-c", "user.email=t@example.invalid")


def _repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    raw_git(path, "init", "-q", "-b", "main")
    return path


def _planted(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A repository whose ``.sidegraph`` holds the files of a git directory; the marker file
    the planted command creates is returned too."""
    command, marker = plant_marker(tmp_path)
    proj = _repo(tmp_path / "proj")
    planted = proj / ".sidegraph"
    planted.mkdir()
    plant_git_dir(planted, command)
    return proj, planted, marker


class _Spy:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        self.calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, b"", b"")


def test_embedded_git_dir_finds_a_planted_directory(tmp_path):
    _, planted, _ = _planted(tmp_path)
    assert embedded_git_dir(planted) == os.path.realpath(planted)
    assert embedded_git_dir(planted / "refs") == os.path.realpath(planted)


def test_embedded_git_dir_finds_a_commondir_layout(tmp_path):
    d = tmp_path / "d"
    d.mkdir()
    (d / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (d / "commondir").write_text("../elsewhere\n", encoding="utf-8")
    assert embedded_git_dir(d) == os.path.realpath(d)


def test_embedded_git_dir_accepts_ordinary_directories(tmp_path, monkeypatch):
    proj = _repo(tmp_path / "proj")
    (proj / "sub").mkdir()
    raw_git(proj, *_IDENTITY, "commit", "-q", "--allow-empty", "-m", "first")
    linked = tmp_path / "linked"
    raw_git(proj, "worktree", "add", "-q", str(linked), "-b", "side")
    only_head = tmp_path / "only-head"
    only_head.mkdir()
    (only_head / "HEAD").write_text("x", encoding="utf-8")
    assert embedded_git_dir(proj) is None
    assert embedded_git_dir(proj / "sub") is None
    assert embedded_git_dir(linked) is None
    assert embedded_git_dir(only_head) is None
    assert embedded_git_dir(tmp_path / "missing") is None
    monkeypatch.chdir(proj)
    assert embedded_git_dir(None) is None


def test_a_dot_git_entry_ends_the_walk(tmp_path):
    """A repository below a directory that looks like a bare repository is an ordinary one."""
    bare = tmp_path / "bare"
    (bare / "objects").mkdir(parents=True)
    (bare / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    work = _repo(bare / "work")
    assert embedded_git_dir(work) is None
    assert embedded_git_dir(bare) == os.path.realpath(bare)


def test_embedded_git_dir_follows_links(tmp_path):
    _, planted, _ = _planted(tmp_path)
    link = tmp_path / "link"
    try:
        link.symlink_to(planted / "refs", target_is_directory=True)
    except OSError:
        pytest.skip("symlinks cannot be created here")
    assert embedded_git_dir(link) == os.path.realpath(planted)


def test_embedded_git_dir_answers_none_for_an_unusable_path(tmp_path):
    assert embedded_git_dir("a\0b") is None
    assert embedded_git_dir(os.fsencode(tmp_path)) is None
    spy = _Spy()
    run_git(["status"], "a\0b", run=spy)  # not refused: the launcher fails as it always did
    assert len(spy.calls) == 1


def test_embedded_git_dir_accepts_a_cwd_inside_dot_git(tmp_path):
    proj = _repo(tmp_path / "proj")
    raw_git(proj, *_IDENTITY, "commit", "-q", "--allow-empty", "-m", "first")
    raw_git(proj, "worktree", "add", "-q", str(tmp_path / "linked"), "-b", "side")
    assert (proj / ".git" / "HEAD").is_file() and (proj / ".git" / "objects").is_dir()
    assert embedded_git_dir(proj / ".git") is None
    assert embedded_git_dir(proj / ".git" / "hooks") is None
    assert embedded_git_dir(proj / ".git" / "worktrees" / "linked") is None


def test_run_git_refuses_before_launching(tmp_path):
    _, planted, _ = _planted(tmp_path)
    spy = _Spy()
    with pytest.raises(UnsafeGitDirectory):
        run_git(["status"], planted, run=spy)
    with pytest.raises(UnsafeGitDirectory):  # the four sites that pass the directory as -C
        run_git(["status"], planted, dash_c=True, run=spy)
    assert spy.calls == []
    assert issubclass(UnsafeGitDirectory, OSError)


def test_protected_env_appends_after_inherited_pairs(monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "safe.directory")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "*")
    env = protected_env()
    assert env["GIT_CONFIG_COUNT"] == "3"
    assert (env["GIT_CONFIG_KEY_0"], env["GIT_CONFIG_VALUE_0"]) == ("safe.directory", "*")
    for offset, (key, value) in enumerate(PROTECTIVE_CONFIG, start=1):
        assert env[f"GIT_CONFIG_KEY_{offset}"] == key
        assert env[f"GIT_CONFIG_VALUE_{offset}"] == value
    monkeypatch.delenv("GIT_CONFIG_COUNT")
    monkeypatch.delenv("GIT_CONFIG_KEY_0")
    monkeypatch.delenv("GIT_CONFIG_VALUE_0")
    env = protected_env()
    assert env["GIT_CONFIG_COUNT"] == "2"
    fsmonitor = [n for n in (0, 1) if env[f"GIT_CONFIG_KEY_{n}"] == "core.fsmonitor"]
    assert len(fsmonitor) == 1
    assert env[f"GIT_CONFIG_VALUE_{fsmonitor[0]}"] == ""


@pytest.mark.parametrize("count", ["abc", "-1"])
def test_protected_env_leaves_a_bogus_count_alone(monkeypatch, count):
    monkeypatch.setenv("GIT_CONFIG_COUNT", count)
    env = protected_env()
    assert env["GIT_CONFIG_COUNT"] == count
    assert not [k for k in env if k.startswith("GIT_CONFIG_KEY_")]


@pytest.mark.parametrize("count", [" 1", "+1", "\t1"])
def test_protected_env_appends_after_a_count_git_still_reads(monkeypatch, count):
    """git reads the count with strtoul, so leading whitespace and a sign are numbers."""
    monkeypatch.setenv("GIT_CONFIG_COUNT", count)
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "safe.directory")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "*")
    env = protected_env()
    assert env["GIT_CONFIG_COUNT"] == "3"
    for offset, (key, value) in enumerate(PROTECTIVE_CONFIG, start=1):
        assert (env[f"GIT_CONFIG_KEY_{offset}"], env[f"GIT_CONFIG_VALUE_{offset}"]) == (key, value)


def test_protected_env_repository_variables(monkeypatch):
    monkeypatch.setenv("GIT_DIR", "/somewhere")
    assert "GIT_DIR" not in protected_env()
    assert protected_env(keep_repository_env=True)["GIT_DIR"] == "/somewhere"
    assert protected_env({"LC_ALL": "C"})["LC_ALL"] == "C"


def test_run_git_argv_and_cwd(tmp_path):
    spy = _Spy()
    run_git(["status", "-s"], tmp_path, run=spy, timeout=3)
    argv, kwargs = spy.calls[-1]
    assert argv == ["git", "status", "-s"]
    assert kwargs["cwd"] == tmp_path
    assert kwargs["timeout"] == 3
    assert kwargs["env"] == protected_env()
    run_git(["status"], None, run=spy)
    assert spy.calls[-1][1]["cwd"] is None
    run_git(["status", "-s"], tmp_path, dash_c=True, run=spy)
    argv, kwargs = spy.calls[-1]
    assert argv == ["git", "-C", str(tmp_path), "status", "-s"]
    assert "cwd" not in kwargs
    run_git(["status"], None, dash_c=True, run=spy)
    argv, kwargs = spy.calls[-1]
    assert argv == ["git", "status"]
    assert "cwd" not in kwargs


def test_run_git_uses_subprocess_run_at_call_time(tmp_path, monkeypatch):
    spy = _Spy()
    monkeypatch.setattr(subprocess, "run", spy)
    run_git(["--version"], tmp_path)
    assert [argv for argv, _ in spy.calls] == [["git", "--version"]]


def test_settings_alone_stop_the_trap(tmp_path):
    """Layer 2 without the guard: the two settings keep the planted command from running."""
    _, planted, marker = _planted(tmp_path)
    if not trap_is_armed(planted, marker):
        pytest.skip("this git does not run the planted command")
    if git_version() < (2, 31):
        pytest.skip("this git does not read the settings from the environment")
    done = subprocess.run(
        ["git", "status", "--porcelain=v1"],
        cwd=planted,
        env=protected_env(),
        capture_output=True,
        check=False,
    )
    assert not marker.exists()
    if git_version() >= (2, 38):
        assert done.returncode == 128
        assert b"bare repository" in done.stderr  # git refused it, it did not just fail


def test_guard_alone_stops_the_trap(tmp_path, monkeypatch):
    """Layer 1 without the settings: the guard refuses on every git."""
    _, planted, marker = _planted(tmp_path)
    if not trap_is_armed(planted, marker):
        pytest.skip("this git does not run the planted command")
    monkeypatch.setattr(gitenv, "protected_env", lambda *a, **k: gitenv.git_env())
    with pytest.raises(OSError):
        run_git(["status"], planted, capture_output=True)
    assert not marker.exists()


def _commit_all(repo: Path, message: str = "first") -> None:
    raw_git(repo, "add", "-A")
    raw_git(repo, *_IDENTITY, "commit", "-q", "-m", message)


def _planted_clone_at(tmp_path: Path, clone: Path) -> tuple[Path, Path, Path]:
    """A clone at ``clone`` whose committed ``.sidegraph/`` is a real store plus the files of a
    planted git directory. Returns the clone, its store directory and the marker file."""
    command, marker = plant_marker(tmp_path)
    origin = _repo(tmp_path / "origin")
    (origin / "README.md").write_text("project\n", encoding="utf-8")
    Store(origin / ".sidegraph").close()
    plant_git_dir(origin / ".sidegraph", command)
    _commit_all(origin)
    clone.parent.mkdir(parents=True, exist_ok=True)
    done = raw_git(tmp_path, "clone", "-q", str(origin), str(clone))
    assert done.returncode == 0, done.stderr
    store_dir = clone / ".sidegraph"
    assert (store_dir / "HEAD").is_file()  # the planted files survived the clone
    if not trap_is_armed(store_dir, marker):
        pytest.skip("this git does not run the planted command")
    return clone, store_dir, marker


@pytest.fixture
def planted_clone(tmp_path: Path) -> tuple[Path, Path, Path]:
    return _planted_clone_at(tmp_path, tmp_path / "clone")


def test_session_start_does_not_run_a_planted_command(planted_clone, monkeypatch, capsys):
    clone, store_dir, marker = planted_clone
    start(monkeypatch, capsys, store_dir, project=clone)
    assert not marker.exists()


def test_doctor_does_not_run_a_planted_command(planted_clone):
    _, store_dir, marker = planted_clone
    doctor_main(["--db", str(store_dir)])
    assert not marker.exists()


def test_stats_does_not_run_a_planted_command(planted_clone):
    _, store_dir, marker = planted_clone
    Store(store_dir).close()  # a clone has no index.db (it is gitignored)
    assert not marker.exists()  # opening the store is not what fires the trap
    build_report(store_dir, None, window_days=30, now=datetime.now(UTC))
    assert not marker.exists()


def test_a_submodule_fsmonitor_is_not_run(tmp_path, monkeypatch, capsys):
    """Guards the empty ``core.fsmonitor`` reaching the ``git status`` that git starts inside
    a checked-out submodule. A filter or a hook in the nested repository's own configuration
    still runs; that case needs someone who can already write on the machine."""
    command, marker = plant_marker(tmp_path)
    repo = _repo(tmp_path / "repo")
    (repo / "README.md").write_text("project\n", encoding="utf-8")
    store_dir = repo / ".sidegraph"
    Store(store_dir).close()
    nested = _repo(store_dir / "x")
    (nested / "f.txt").write_text("f\n", encoding="utf-8")
    _commit_all(nested)
    raw_git(nested, "config", "core.fsmonitor", command)
    _commit_all(repo)
    assert "160000" in raw_git(repo, "ls-files", "-s").stdout
    if not trap_is_armed(store_dir, marker):
        pytest.skip("this git does not run the planted command")
    if git_version() < (2, 31):
        pytest.skip("this git does not read the settings from the environment")
    start(monkeypatch, capsys, store_dir, project=repo)
    doctor_main(["--db", str(store_dir)])
    build_report(store_dir, None, window_days=30, now=datetime.now(UTC))
    assert not marker.exists()


def test_a_planted_user_name_is_not_stamped(planted_clone, monkeypatch):
    _, store_dir, _ = planted_clone
    monkeypatch.chdir(store_dir)
    assert store_mod._ratifier_identity() != "Planted Name"


@pytest.mark.parametrize("below", ["modules", "worktrees", "plain"])
def test_a_clone_below_a_dot_git_path_is_refused(tmp_path, monkeypatch, capsys, below):
    """The user may clone into a folder whose path has a ``.git`` component, ``.git/modules/``
    and ``.git/worktrees/`` included, where git's own bare-repository check is exempt too.
    The planted directory inside such a clone is still refused."""
    where = tmp_path / "x" / ".git" / below / "clone"
    clone, store_dir, marker = _planted_clone_at(tmp_path, where)
    assert embedded_git_dir(store_dir) == os.path.realpath(store_dir)
    with pytest.raises(UnsafeGitDirectory):
        run_git(["status"], store_dir, capture_output=True)
    # layer 2 off (what an older git leaves): only the guard stands between the clone and git
    monkeypatch.setattr(gitenv, "protected_env", lambda *a, **k: gitenv.git_env())
    start(monkeypatch, capsys, store_dir, project=clone)
    doctor_main(["--db", str(store_dir)])
    assert not marker.exists()


def test_the_dot_git_exemption_keeps_git_directories_usable(tmp_path):
    """The four places git itself accepts without a ``.git`` entry stay open to Sidegraph."""
    proj = _repo(tmp_path / "super")
    raw_git(proj, *_IDENTITY, "commit", "-q", "--allow-empty", "-m", "first")
    raw_git(proj, "worktree", "add", "-q", str(tmp_path / "linked"), "-b", "side")
    sub = _repo(tmp_path / "sub-origin")
    raw_git(sub, *_IDENTITY, "commit", "-q", "--allow-empty", "-m", "first")
    done = raw_git(
        proj, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub), "sub"
    )
    assert done.returncode == 0, done.stderr
    assert embedded_git_dir(proj / ".git") is None
    assert embedded_git_dir(proj / ".git" / "hooks") is None
    assert embedded_git_dir(proj / ".git" / "worktrees" / "linked") is None
    assert (proj / ".git" / "modules" / "sub" / "HEAD").is_file()
    assert embedded_git_dir(proj / ".git" / "modules" / "sub") is None


def test_run_git_succeeds_in_an_ordinary_repository_and_a_linked_worktree(tmp_path):
    """Positive control, on every platform: the protective settings must not break git itself.
    Every other assertion here is "the marker is absent", which a git that fails every call
    would also satisfy."""
    proj = _repo(tmp_path / "proj")
    raw_git(proj, *_IDENTITY, "commit", "-q", "--allow-empty", "-m", "first")
    raw_git(proj, "worktree", "add", "-q", str(tmp_path / "linked"), "-b", "side")
    for where in (proj, tmp_path / "linked"):
        done = run_git(
            ["rev-parse", "--is-inside-work-tree"], where, capture_output=True, text=True
        )
        assert done.returncode == 0, done.stderr
        assert done.stdout.strip() == "true"
        done = run_git(
            ["rev-parse", "--is-inside-work-tree"],
            where,
            dash_c=True,
            capture_output=True,
            text=True,
        )
        assert done.returncode == 0, done.stderr
        assert done.stdout.strip() == "true"


def test_git_sees_the_protective_values(tmp_path, monkeypatch):
    """Positive control: the effective configuration, read through the same environment,
    carries both values, the empty ``core.fsmonitor`` included."""
    if git_version() < (2, 31):
        pytest.skip("this git does not read the settings from the environment")
    proj = _repo(tmp_path / "proj")
    monkeypatch.setenv("GIT_CONFIG_COUNT", " 1")  # also through a count git reads loosely
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "user.name")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "Someone")
    done = run_git(["config", "--list"], proj, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    lines = [line.lower() for line in done.stdout.splitlines()]  # git lowercases key names
    assert "user.name=someone" in lines
    assert "safe.barerepository=explicit" in lines
    assert "core.fsmonitor=" in lines


def test_the_two_gitio_sites_keep_the_repository_variables(tmp_path, monkeypatch):
    """Git starts these calls as a hook and sets ``GIT_DIR``; they must still see it."""
    monkeypatch.setenv("GIT_DIR", "/hook/git-dir")
    spy = _Spy()
    monkeypatch.setattr(subprocess, "run", spy)
    gitio._run_git(["status"], tmp_path)
    gitio._run_git_bytes(["status"], tmp_path)
    assert len(spy.calls) == 2
    assert all(kwargs["env"]["GIT_DIR"] == "/hook/git-dir" for _, kwargs in spy.calls)
    spy.calls.clear()
    run_git(["status"], tmp_path, run=spy)
    assert "GIT_DIR" not in spy.calls[0][1]["env"]
    run_git(["status"], tmp_path, run=spy, keep_repository_env=True)
    assert spy.calls[1][1]["env"]["GIT_DIR"] == "/hook/git-dir"


def test_the_ratifier_lookup_drops_the_repository_variables(monkeypatch):
    monkeypatch.setenv("GIT_DIR", "/elsewhere/.git")
    calls: list[dict[str, Any]] = []

    def spy(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(kwargs)
        return subprocess.CompletedProcess(argv, 0, "Someone\n", "")

    monkeypatch.setattr(subprocess, "run", spy)
    assert store_mod._ratifier_identity() == "Someone"
    assert "GIT_DIR" not in calls[0]["env"]


def test_changed_files_hands_the_directory_to_git_as_dash_c(tmp_path, monkeypatch):
    graph = tmp_path / "graphify-out" / "graph.json"
    graph.parent.mkdir()
    graph.write_text('{"nodes": [], "links": []}', encoding="utf-8")
    spy = _Spy()
    monkeypatch.setattr(subprocess, "run", spy)
    GraphifyReader(graph).changed_files()
    argv, kwargs = spy.calls[0]
    assert argv[:3] == ["git", "-C", str(tmp_path)]
    assert "cwd" not in kwargs
