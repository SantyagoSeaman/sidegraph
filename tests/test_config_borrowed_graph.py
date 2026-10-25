"""A linked worktree has the tracked store and no graph; its graph is the main checkout's.

``config.main_checkout_root`` and ``config.borrowed_graph_path`` find it from the filesystem
alone: the worktree's ``.git`` file, the ``gitdir`` it names, that directory's ``commondir``.
Every test builds a REAL repository with ``git init`` / ``git worktree add`` /
``git clone --local`` / a bare repository / a submodule; the shared fixture ``make_main`` and
``add_worktree`` are imported by the server and hook test modules, which drive the same
repositories.
see design/superpowers/specs/2026-10-01-worktree-borrowed-graph-design.md (D1, T1-T4e)
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

import sidegraph.config as config
from tests.graph_repo_support import GRAPH, add_worktree, git, head, make_main, write_graph


def git_version() -> tuple[int, ...]:
    out = subprocess.run(["git", "--version"], capture_output=True, text=True, check=True).stdout
    match = re.search(r"(\d+)\.(\d+)", out)
    assert match is not None, out
    return int(match.group(1)), int(match.group(2))


@pytest.fixture
def main(tmp_path: Path) -> Path:
    return make_main(tmp_path)


# -- the worktree borrows the main checkout's graph --------------------------------------


def test_t1_a_linked_worktree_borrows_the_main_checkouts_graph(main):
    """Red against unfixed code: there is no such API."""
    worktree = add_worktree(main)

    assert not (worktree / GRAPH).exists()
    assert config.main_checkout_root(worktree / ".sidegraph") == main
    assert config.borrowed_graph_path(worktree / ".sidegraph") == main / GRAPH
    assert config.borrowed_graph_path(str(worktree / ".sidegraph")) == main / GRAPH


def test_the_main_checkout_borrows_from_nobody(main):
    assert config.main_checkout_root(main / ".sidegraph") is None
    (main / GRAPH).unlink()
    assert config.borrowed_graph_path(main / ".sidegraph") is None


def test_t2_the_worktrees_own_graph_wins(main):
    worktree = add_worktree(main)
    write_graph(worktree / GRAPH, head(worktree), ["pkg/m.py"])

    assert config.borrowed_graph_path(worktree / ".sidegraph") is None


def test_t3_a_local_clone_has_no_link_to_the_checkout_it_came_from(tmp_path, main):
    clone = tmp_path / "C"
    git(tmp_path, "clone", "-q", "--local", str(main), str(clone))

    assert (clone / ".sidegraph").is_dir()
    assert config.main_checkout_root(clone / ".sidegraph") is None
    assert config.borrowed_graph_path(clone / ".sidegraph") is None


def test_t4_a_worktree_of_a_bare_repository_has_no_main_checkout(tmp_path, main):
    """Red against a blind ``commondir`` design (mutations M4 and M9 together): the common
    directory's parent is no checkout, and the graph put there must not be read. Either guard
    alone rejects this layout: the name check, and the ``core.bare`` check."""
    bare = tmp_path / "srv" / "bare.git"
    bare.parent.mkdir()
    git(tmp_path, "clone", "-q", "--bare", str(main), str(bare))
    worktree = tmp_path / "bw"
    git(bare, "worktree", "add", "-q", str(worktree), "main")
    write_graph(bare.parent / GRAPH, head(worktree), ["pkg/m.py"])

    assert (worktree / ".sidegraph").is_dir()
    assert config.main_checkout_root(worktree / ".sidegraph") is None
    assert config.borrowed_graph_path(worktree / ".sidegraph") is None


@pytest.mark.skipif(
    not hasattr(os, "geteuid") or os.geteuid() == 0,
    reason="needs POSIX permissions; root bypasses them",
)
def test_a_dot_git_common_directory_with_an_unreadable_config_is_not_trusted(main):
    worktree = add_worktree(main)
    config_file = main / ".git" / "config"
    original = stat.S_IMODE(config_file.stat().st_mode)
    config_file.chmod(0)
    try:
        assert config.main_checkout_root(worktree / ".sidegraph") is None
    finally:
        config_file.chmod(original)


def test_t4c_the_dot_bare_layout_has_no_main_checkout(tmp_path, main):
    project = tmp_path / "P"
    project.mkdir()
    git(tmp_path, "clone", "-q", "--bare", str(main), str(project / ".bare"))
    (project / ".git").write_text("gitdir: ./.bare\n")
    feature = project / "feature"
    git(project, "worktree", "add", "-q", str(feature), "main")
    write_graph(project / GRAPH, head(feature), ["pkg/m.py"])

    assert config.main_checkout_root(feature / ".sidegraph") is None
    assert config.borrowed_graph_path(feature / ".sidegraph") is None


def test_t4d_a_nested_store_maps_through_its_path_below_the_worktree_root(tmp_path):
    """Red against rev 1 (the ``.git`` parent joined with the bare graph value)."""
    repo = make_main(tmp_path, nested=True)
    worktree = add_worktree(repo)
    own = worktree / ".config" / "sidegraph"

    assert own.is_dir() and not (worktree / ".config" / "graphify-out").exists()
    assert config.borrowed_graph_path(own) == repo / ".config" / "graphify-out" / "graph.json"


def test_t4e_an_absolute_graph_value_is_never_borrowed(tmp_path, main, monkeypatch):
    worktree = add_worktree(main)
    monkeypatch.setenv("SIDEGRAPH_GRAPH", str(tmp_path / "elsewhere" / "graph.json"))

    assert config.default_graph_path(worktree / ".sidegraph") == tmp_path / "elsewhere/graph.json"
    assert config.borrowed_graph_path(worktree / ".sidegraph") is None


def test_a_relative_graph_value_maps_the_same_way(main, monkeypatch):
    worktree = add_worktree(main)
    write_graph(main / "out" / "g.json", head(main), ["pkg/m.py"])
    monkeypatch.setenv("SIDEGRAPH_GRAPH", "out/g.json")

    assert config.borrowed_graph_path(worktree / ".sidegraph") == main / "out" / "g.json"


def test_the_candidate_names_the_main_graph_whether_or_not_it_exists(main):
    worktree = add_worktree(main)

    assert config.borrowed_graph_candidate(worktree / ".sidegraph") == main / GRAPH
    assert config.borrowed_graph_path(worktree / ".sidegraph") == main / GRAPH

    (main / GRAPH).unlink()  # nothing to lend, but the candidate still says where it would be
    assert config.borrowed_graph_path(worktree / ".sidegraph") is None
    assert config.borrowed_graph_candidate(worktree / ".sidegraph") == main / GRAPH


def test_a_gitdir_file_that_is_not_a_gitdir_line_borrows_nothing(main):
    worktree = add_worktree(main)
    (worktree / ".git").write_text("not a gitdir line\n")

    assert config.main_checkout_root(worktree / ".sidegraph") is None
    assert config.borrowed_graph_path(worktree / ".sidegraph") is None


def test_a_worktree_whose_gitdir_is_gone_borrows_nothing(main):
    worktree = add_worktree(main)
    (worktree / ".git").write_text(f"gitdir: {main / 'nowhere'}\n")

    assert config.main_checkout_root(worktree / ".sidegraph") is None


def test_the_worktree_root_is_the_repository_boundary_of_the_store(main):
    worktree = add_worktree(main)

    assert config.repository_root(worktree / ".sidegraph") == worktree
    assert config.repository_root(main / ".sidegraph") == main
    assert os.path.isabs(str(config.repository_root(worktree / ".sidegraph")))
