"""`sidegraph-mcp --repo-root` anchors the plugin's server to the project before it starts.

Only the plugin manifests pass the flag; every other launch keeps the directory it was started
in. With the flag, ``CLAUDE_PROJECT_DIR`` wins when it names a directory, else the git top-level
of the cwd applies, else nothing moves. Anchoring never raises and the flag never reaches the
server's own argument handling.
see host/mcp_entry.py (`main`)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from sidegraph import server
from sidegraph.host import mcp_entry

VAR = "SIDEGRAPH_RATIFY_POLICY"


@pytest.fixture
def seen(monkeypatch):
    """Stand in for the server: record the cwd and argv it would start with."""
    record: dict[str, object] = {}

    def fake_main(prepare=None):
        record["cwd"] = Path.cwd().resolve()
        record["argv"] = list(sys.argv)
        record["policy"] = os.environ.get(VAR)

    monkeypatch.setattr(server, "main", fake_main)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.delenv(VAR, raising=False)
    return record


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path


def _launch(monkeypatch, *args: str) -> None:
    monkeypatch.setattr(sys, "argv", ["sidegraph-mcp", *args])
    mcp_entry.main()


def test_the_git_top_level_applies_from_a_subdirectory(tmp_path, monkeypatch, seen):
    repo = _git_repo(tmp_path / "repo")
    sub = repo / "a" / "b"
    sub.mkdir(parents=True)
    monkeypatch.chdir(sub)
    _launch(monkeypatch, "--repo-root")
    assert seen["cwd"] == repo.resolve()


def test_claude_project_dir_wins_over_the_git_top_level(tmp_path, monkeypatch, seen):
    repo = _git_repo(tmp_path / "repo")
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(repo)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    _launch(monkeypatch, "--repo-root")
    assert seen["cwd"] == project.resolve()


@pytest.mark.parametrize("value", ["", "   ", "MISSING"])
def test_a_blank_or_missing_project_dir_falls_through_to_git(tmp_path, monkeypatch, seen, value):
    repo = _git_repo(tmp_path / "repo")
    sub = repo / "sub"
    sub.mkdir()
    monkeypatch.chdir(sub)
    if value == "MISSING":
        value = str(tmp_path / "does-not-exist")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", value)
    _launch(monkeypatch, "--repo-root")
    assert seen["cwd"] == repo.resolve()


def test_a_project_dir_that_is_a_file_falls_through_to_git(tmp_path, monkeypatch, seen):
    repo = _git_repo(tmp_path / "repo")
    afile = tmp_path / "afile"
    afile.write_text("x", encoding="utf-8")
    monkeypatch.chdir(repo)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(afile))
    _launch(monkeypatch, "--repo-root")
    assert seen["cwd"] == repo.resolve()


def test_outside_any_repo_the_cwd_stays(tmp_path, monkeypatch, seen):
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.chdir(plain)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    _launch(monkeypatch, "--repo-root")
    assert seen["cwd"] == plain.resolve()


def test_without_the_flag_the_cwd_never_changes(tmp_path, monkeypatch, seen):
    repo = _git_repo(tmp_path / "repo")
    sub = repo / "sub"
    sub.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(sub)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    _launch(monkeypatch)
    assert seen["cwd"] == sub.resolve()


def test_a_failing_chdir_is_swallowed(tmp_path, monkeypatch, seen):
    repo = _git_repo(tmp_path / "repo")
    sub = repo / "sub"
    sub.mkdir()
    monkeypatch.chdir(sub)

    def boom(_path):
        raise PermissionError("no")

    monkeypatch.setattr(os, "chdir", boom)
    _launch(monkeypatch, "--repo-root")
    assert seen["cwd"] == sub.resolve()


def test_the_flag_is_gone_from_argv_when_the_server_starts(tmp_path, monkeypatch, seen):
    monkeypatch.chdir(tmp_path)
    _launch(monkeypatch, "--repo-root", "--other")
    assert seen["argv"] == ["sidegraph-mcp", "--other"]


def test_anchoring_precedes_the_ratify_policy_read(tmp_path, monkeypatch, seen):
    project = tmp_path / "project"
    (project / ".claude").mkdir(parents=True)
    (project / ".claude" / "settings.json").write_text(
        json.dumps({"env": {VAR: "auto-low-risk"}}), encoding="utf-8"
    )
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    _launch(monkeypatch, "--repo-root")
    assert seen["policy"] == "auto-low-risk"
