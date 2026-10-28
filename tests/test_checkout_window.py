"""Missing-marker checkout decisions, including actual Git repository discovery.

Unit cases target the named checkout-rule mutants; non-git/fresh-store cases guard scope.
# see design/superpowers/specs/2026-10-28-format-marker-checkout-race-design.md
"""

# ruff: noqa: F811

from __future__ import annotations

import os
import subprocess

import pytest

from sidegraph import checkout_window as cw
from sidegraph.store_layout import StoreRequirementError
from tests.store_format2_support import commit_all, git, git_repo  # noqa: F401

_DATE = "2026-10-01T00:00:00Z"
_TEXT = "sidegraph-store 0.6.0\n"


class Window:
    def __init__(self, tmp_path):
        self.store = tmp_path / ".sidegraph"
        self.store.mkdir()
        self.lock = tmp_path / "index.lock"
        self.marker = self.store / "format"
        self.answers = [cw.GitView(False, self.lock)]
        self.steps = []
        self.now = 0.0
        self.sleeps = 0

    def view(self, path):
        assert path == self.store
        return self.answers.pop(0)

    def sleep(self, seconds):
        self.now += seconds
        self.sleeps += 1
        if self.steps:
            self.steps.pop(0)()

    def run(self, **kwargs):
        return cw.await_marker(
            self.store,
            view=self.view,
            sleep=self.sleep,
            clock=lambda: self.now,
            wait=2.0,
            poll=0.02,
            **kwargs,
        )

    def write_marker(self):
        self.marker.write_text(_TEXT, encoding="utf-8")


@pytest.fixture
def window(tmp_path):
    return Window(tmp_path)


def test_unanswered_git_returns(window):
    """Guard: unavailable git leaves the old backfill path available."""
    window.answers = [None]
    window.run()
    assert window.sleeps == 0
    assert not window.marker.exists()


def test_head_records_marker_and_no_lock_refuses(window):
    window.answers = [cw.GitView(True, window.lock)]
    with pytest.raises(StoreRequirementError) as caught:
        window.run()
    assert caught.value.kind == "missing"
    assert "checkout HEAD -- format" in str(caught.value)
    assert str(window.store) in str(caught.value)
    assert not window.marker.exists()


def test_head_lacks_marker_and_no_lock_returns(window):
    """Guard: a marker never committed still gets its old backfill."""
    window.run()
    assert window.sleeps == 0


def test_present_marker_returns_without_waiting(window):
    window.write_marker()
    window.run()
    assert window.sleeps == 0


def test_lock_held_marker_arrives(window):
    window.lock.touch()
    window.steps = [window.write_marker]
    window.run()
    assert window.sleeps == 1


def test_empty_marker_while_lock_held_keeps_waiting(window):
    window.lock.touch()
    window.steps = [window.marker.touch, window.write_marker]
    window.run()
    assert window.sleeps == 2
    assert window.marker.read_text() == _TEXT


def test_lock_held_past_the_wait_refuses_naming_the_lock(window):
    window.lock.touch()
    with pytest.raises(StoreRequirementError) as caught:
        window.run()
    assert caught.value.kind == "missing"
    assert caught.value.lock == window.lock
    assert not caught.value.busy
    assert str(window.lock) in str(caught.value)
    assert abs(window.sleeps - 100) <= 1
    assert not window.marker.exists()


@pytest.mark.parametrize("head", [False, True])
def test_lock_released_without_marker_refreshes_head(window, head):
    window.lock.touch()
    window.answers = [cw.GitView(False, window.lock), cw.GitView(head, window.lock)]
    window.steps = [window.lock.unlink]
    if head:
        with pytest.raises(StoreRequirementError, match="HEAD records it"):
            window.run()
    else:
        window.run()
    assert window.answers == []
    assert window.sleeps == 1


@pytest.mark.parametrize("head", [False, True])
def test_second_lookup_unanswered_uses_the_first(window, head):
    window.lock.touch()
    window.answers = [cw.GitView(head, window.lock), None]
    window.steps = [window.lock.unlink]
    if head:
        with pytest.raises(StoreRequirementError, match="HEAD records it"):
            window.run()
    else:
        window.run()
    assert window.answers == []


def test_lock_released_with_marker_returns(window):
    window.lock.touch()

    def finish():
        window.write_marker()
        window.lock.unlink()

    window.steps = [finish]
    window.run()
    assert window.sleeps == 1


def test_second_lookup_busy_retries_instead_of_backfilling(window):
    window.lock.touch()
    window.answers = [cw.GitView(False, window.lock), cw.GIT_BUSY, cw.GitView(True, window.lock)]
    window.steps = [window.lock.unlink]
    with pytest.raises(StoreRequirementError, match="HEAD records it"):
        window.run()
    assert window.answers == []
    assert window.sleeps == 2


@pytest.mark.parametrize("head", [False, True])
def test_busy_refresh_unavailable_keeps_the_known_head(window, head):
    window.lock.touch()
    window.answers = [cw.GitView(head, window.lock), cw.GIT_BUSY, None]
    window.steps = [window.lock.unlink]
    if head:
        with pytest.raises(StoreRequirementError, match="HEAD records it"):
            window.run()
    else:
        window.run()
    assert window.answers == []
    assert window.sleeps == 2
    assert not window.marker.exists()


@pytest.mark.parametrize("creating", [False, True])
def test_unresolved_busy_requires_explicit_creation(window, creating):
    window.answers = [cw.GIT_BUSY] * 150
    if creating:
        window.run(creating=True)
    else:
        with pytest.raises(StoreRequirementError) as caught:
            window.run()
        assert caught.value.kind == "missing"
        assert caught.value.busy and caught.value.lock is None
        assert str(caught.value) == (
            "store format marker is missing and git did not answer in time "
            f"({window.store}); open the store again once git finishes"
        )
    assert abs(window.sleeps - 100) <= 1
    assert not window.marker.exists()


@pytest.mark.parametrize("has_record", [False, True])
def test_real_timeout_never_backfills_an_ordinary_open(tmp_path, monkeypatch, has_record):
    store = tmp_path / "s"
    store.mkdir()
    if has_record:
        (store / "entities").mkdir()
        (store / "entities" / "existing.json").write_text("{}\n")

    def timeout(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(cw.subprocess, "run", timeout)
    now = [0.0]
    with pytest.raises(StoreRequirementError) as caught:
        cw.await_marker(
            store,
            wait=0.04,
            poll=0.02,
            clock=lambda: now[0],
            sleep=lambda seconds: now.__setitem__(0, now[0] + seconds),
        )
    assert caught.value.busy
    assert not (store / "format").exists()


def test_git_view_outside_a_repository_is_none(tmp_path):
    store = tmp_path / "s"
    store.mkdir()
    assert cw.git_view(store) is None


def test_git_view_unborn_repository(git_repo):
    store = git_repo / ".sidegraph"
    store.mkdir()
    view = cw.git_view(store)
    assert isinstance(view, cw.GitView) and not view.head_records_marker
    assert os.path.realpath(view.index_lock) == os.path.realpath(git_repo / ".git/index.lock")


def test_git_view_committed_marker_at_root_and_in_a_subdirectory(git_repo):
    for relative in (".sidegraph", "sub/.sidegraph"):
        store = git_repo / relative
        store.mkdir(parents=True)
        (store / "format").write_text(_TEXT)
    commit_all(git_repo, "markers", date=_DATE)
    for relative in (".sidegraph", "sub/.sidegraph"):
        view = cw.git_view(git_repo / relative)
        assert isinstance(view, cw.GitView) and view.head_records_marker
    other = git_repo / "other/.sidegraph"
    other.mkdir(parents=True)
    (other / "format").write_text(_TEXT)
    view = cw.git_view(other)
    assert isinstance(view, cw.GitView) and not view.head_records_marker


def _committed_marker(repo):
    store = repo / ".sidegraph"
    store.mkdir()
    (store / "format").write_text(_TEXT)
    commit_all(repo, "marker", date=_DATE)
    return store


def test_git_view_linked_worktree(git_repo, tmp_path):
    _committed_marker(git_repo)
    worktree = tmp_path / "wt"
    git(git_repo, "worktree", "add", "--detach", str(worktree))
    view = cw.git_view(worktree / ".sidegraph")
    assert isinstance(view, cw.GitView) and view.head_records_marker
    assert view.index_lock.as_posix().endswith("worktrees/wt/index.lock")


def test_git_view_bare_repository_with_a_commit_is_none(git_repo, tmp_path):
    _committed_marker(git_repo)
    bare = tmp_path / "b.git"
    git(git_repo, "clone", "--bare", "-q", str(git_repo), str(bare))
    store = bare / ".sidegraph"
    store.mkdir()
    assert cw.git_view(store) is None


def test_git_view_ignores_an_inherited_git_dir(git_repo, tmp_path, monkeypatch):
    store = _committed_marker(git_repo)
    other = tmp_path / "other"
    other.mkdir()
    git(other, "init", "-q", "-b", "main")
    commit_all(other, "empty", date=_DATE)
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    view = cw.git_view(store)
    assert isinstance(view, cw.GitView) and view.head_records_marker


def test_git_view_env_and_failures(tmp_path, monkeypatch):
    store = tmp_path / "s"
    store.mkdir()
    real_run = subprocess.run
    captured = {}
    commands = []
    monkeypatch.setattr(cw, "GIT_TIMEOUT_SECONDS", 0.123)
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "wrong"))

    def capture(command, **kwargs):
        captured.update(kwargs)
        commands.append(command)
        return real_run(command, **kwargs)

    monkeypatch.setattr(cw.subprocess, "run", capture)
    assert cw.git_view(store) is None
    assert commands == [
        [
            "git",
            "-C",
            str(store),
            "rev-parse",
            "--git-path",
            "index.lock",
            "--verify",
            "-q",
            "HEAD:./format",
        ]
    ]
    assert captured["env"]["GIT_NO_LAZY_FETCH"] == "1"
    assert "GIT_DIR" not in captured["env"]
    assert captured["stdin"] == subprocess.DEVNULL
    assert captured["capture_output"] is True
    assert captured["timeout"] == cw.GIT_TIMEOUT_SECONDS

    def timeout(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(cw.subprocess, "run", timeout)
    assert cw.git_view(store) is cw.GIT_BUSY

    def failed(command, **kwargs):
        raise subprocess.SubprocessError("git failed")

    monkeypatch.setattr(cw.subprocess, "run", failed)
    assert cw.git_view(store) is None
    monkeypatch.setattr(cw.subprocess, "run", real_run)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert cw.git_view(store) is None


@pytest.mark.parametrize(
    "code,output",
    [(0, b"lock\n"), (0, b"\noid\n"), (1, b""), (1, b"lock"), (1, b"\n"), (128, b"lock\n")],
)
def test_git_view_rejects_unparseable_output(tmp_path, monkeypatch, code, output):
    monkeypatch.setattr(
        cw.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, code, output),
    )
    assert cw.git_view(tmp_path) is None
