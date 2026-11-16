"""``--repo-root`` anchors to the project without loading pydantic when the host sets no variable.

Grok and Muse set no ``CLAUDE_PROJECT_DIR``, so every hook run takes the git-top-level branch of
``anchor_to_project``. That branch used to import ``claude_settings`` (pydantic, the engine
reader), about 75 ms on every hook launch. The answer must stay the one
``claude_settings.repo_root_for_settings()`` gives.
see design/superpowers/specs/2026-11-15-hook-launch-form-design.md (D3)
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from sidegraph.host.claude_settings import repo_root_for_settings

_PROBE = (
    "import os, sys\n"
    "from sidegraph.host.project_anchor import anchor_to_project\n"
    "anchor_to_project()\n"
    "heavy = sorted(m for m in sys.modules if m.split('.')[0].startswith('pydantic'))\n"
    "print('CWD=' + os.getcwd())\n"
    "print('HEAVY=' + ','.join(heavy))\n"
)


def _env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.pop("CLAUDE_PROJECT_DIR", None)
    return env


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        env=_env(),
    )


def _probe(cwd: Path) -> tuple[str, str]:
    """The cwd and the loaded pydantic modules after ``anchor_to_project`` ran in a fresh
    interpreter started in ``cwd`` with no ``CLAUDE_PROJECT_DIR``."""
    done = subprocess.run(
        [sys.executable, "-c", _PROBE],
        cwd=cwd,
        capture_output=True,
        text=True,
        env=_env(),
        check=True,
    )
    lines = dict(line.split("=", 1) for line in done.stdout.splitlines() if "=" in line)
    return lines["CWD"], lines["HEAVY"]


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "sub" / "deeper").mkdir(parents=True)
    _git(root, "init", "-q")
    _git(root, "commit", "-q", "--allow-empty", "-m", "first")
    return root


def test_anchoring_without_a_project_variable_loads_no_pydantic(repo: Path) -> None:
    """Red target: the unfixed code imports claude_settings, which loads pydantic."""
    cwd, heavy = _probe(repo / "sub")
    assert heavy == ""
    assert os.path.realpath(cwd) == os.path.realpath(repo)


def test_a_repository_gives_the_answer_of_repo_root_for_settings(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A guard: the stdlib walk and ``repo_root_for_settings`` must agree from a subdirectory."""
    start = repo / "sub" / "deeper"
    monkeypatch.chdir(start)
    expected = repo_root_for_settings()
    cwd, _ = _probe(start)
    assert Path(cwd) == expected


def test_a_worktree_gives_the_answer_of_repo_root_for_settings(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A guard: a linked worktree (its ``.git`` is a file) anchors to the worktree's own top."""
    linked = tmp_path / "linked"
    _git(repo, "worktree", "add", "-q", "-b", "other", str(linked))
    (linked / "sub").mkdir()
    monkeypatch.chdir(linked / "sub")
    expected = repo_root_for_settings()
    cwd, _ = _probe(linked / "sub")
    assert Path(cwd) == expected
    assert os.path.realpath(cwd) == os.path.realpath(linked)


def test_outside_a_repository_gives_the_answer_of_repo_root_for_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A guard: with no repository the process stays in (the resolved) cwd."""
    bare = tmp_path / "plain" / "dir"
    bare.mkdir(parents=True)
    monkeypatch.chdir(bare)
    expected = repo_root_for_settings()
    cwd, _ = _probe(bare)
    assert Path(cwd) == expected
    assert os.path.realpath(cwd) == os.path.realpath(bare)
