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
from sidegraph.store import Store
from tests.test_graph_freshness import commit, git, head, write_graph

GRAPH = Path("graphify-out") / "graph.json"


def git_version() -> tuple[int, ...]:
    out = subprocess.run(["git", "--version"], capture_output=True, text=True, check=True).stdout
    match = re.search(r"(\d+)\.(\d+)", out)
    assert match is not None, out
    return int(match.group(1)), int(match.group(2))


def make_main(
    tmp_path: Path, *, nested: bool = False, files: tuple[str, ...] = ("pkg/m.py",)
) -> Path:
    """The main checkout ``R``: one commit holding ``files`` (default ``pkg/m.py``) and a store
    (at ``R/.sidegraph``, or ``R/.config/sidegraph`` when ``nested``), and a gitignored graph
    built at that commit and holding those files (at ``R/graphify-out/graph.json``, or
    ``R/.config/graphify-out/``)."""
    repo = tmp_path / "R"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "commit.gpgsign", "false")
    base = repo / ".config" if nested else repo
    Store(base / ("sidegraph" if nested else ".sidegraph")).close()
    first = commit(
        repo,
        "A",
        {".gitignore": "graphify-out/\n.worktrees/\n"}
        | {name: "def a():\n    pass\n" for name in files},
    )
    write_graph(base / GRAPH, first, list(files))
    return repo


def add_worktree(repo: Path, name: str = "w", *extra: str) -> Path:
    """``git worktree add R/.worktrees/<name>``: the tracked store, and no graph."""
    worktree = repo / ".worktrees" / name
    git(repo, "worktree", "add", "-q", *extra, str(worktree), "-b", f"wt-{name}")
    return worktree


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


@pytest.mark.skipif(
    git_version() < (2, 48), reason="git worktree add --relative-paths needs git 2.48+"
)
def test_t1b_a_relative_gitdir_resolves_against_the_real_path(tmp_path, main):
    """Red against a lexical gitdir resolution: git writes the relative ``gitdir:`` from the
    REAL path of the worktree, and the store is reached here through a symlink three levels
    deeper than the real directory."""
    real = tmp_path / "real" / "deep"
    real.mkdir(parents=True)
    link = tmp_path / "x" / "y" / "z" / "link"
    link.parent.mkdir(parents=True)
    link.symlink_to(real, target_is_directory=True)
    git(main, "worktree", "add", "-q", "--relative-paths", str(real / "w"), "-b", "wt-rel")
    assert (real / "w" / ".git").read_text().startswith("gitdir: ../")

    assert config.borrowed_graph_path(link / "w" / ".sidegraph") == main / GRAPH


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


def test_t4b_a_submodule_is_not_a_linked_worktree(tmp_path, main):
    """A submodule's ``.git`` file names ``.git/modules/<name>``: no ``commondir`` there."""
    super_repo = tmp_path / "S"
    super_repo.mkdir()
    git(super_repo, "init", "-q", "-b", "main")
    git(super_repo, "config", "user.email", "test@example.com")
    git(super_repo, "config", "user.name", "Test")
    git(super_repo, "config", "commit.gpgsign", "false")
    git(super_repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(main), "sub")
    sub = super_repo / "sub"
    write_graph(super_repo / GRAPH, "0" * 40, ["pkg/m.py"])

    assert (sub / ".git").is_file()
    assert (sub / ".sidegraph").is_dir()
    assert config.main_checkout_root(sub / ".sidegraph") is None
    assert config.borrowed_graph_path(sub / ".sidegraph") is None


def test_t4f_a_bare_repository_cloned_into_a_dot_git_directory_has_no_main_checkout(tmp_path, main):
    """Red against a name-only check: the common directory is named ``.git`` and its parent
    ``bgit`` looks like a checkout, but ``core.bare`` says it is a bare repository."""
    holder = tmp_path / "bgit"
    holder.mkdir()
    git(tmp_path, "clone", "-q", "--bare", str(main), str(holder / ".git"))
    feature = holder / "feat"
    git(holder / ".git", "worktree", "add", "-q", str(feature), "main")
    write_graph(holder / GRAPH, head(feature), ["pkg/m.py"])

    assert (feature / ".sidegraph").is_dir()
    assert config.main_checkout_root(feature / ".sidegraph") is None
    assert config.borrowed_graph_path(feature / ".sidegraph") is None
    assert config.borrowed_graph_candidate(feature / ".sidegraph") is None


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a mode-000 file")
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


def test_t4c_a_separate_git_dir_main_checkout_cannot_be_found(tmp_path):
    """Red against a missing name check (mutation M4): this common directory is not bare, so
    only its name, ``R2.gitdir``, keeps ``tmp_path`` from being called a main checkout."""
    repo = tmp_path / "R2"
    git(tmp_path, "init", "-q", "-b", "main", f"--separate-git-dir={tmp_path / 'R2.gitdir'}", "R2")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "commit.gpgsign", "false")
    Store(repo / ".sidegraph").close()
    first = commit(repo, "A", {".gitignore": "graphify-out/\n.worktrees/\n", "a.py": "x = 1\n"})
    write_graph(repo / GRAPH, first, ["a.py"])
    worktree = add_worktree(repo)

    assert (repo / ".git").is_file()
    assert config.main_checkout_root(worktree / ".sidegraph") is None
    assert config.borrowed_graph_path(worktree / ".sidegraph") is None


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


def test_a_main_checkout_without_a_graph_lends_nothing_but_names_where_it_would_be(main):
    worktree = add_worktree(main)
    (main / GRAPH).unlink()

    assert config.borrowed_graph_path(worktree / ".sidegraph") is None
    assert config.borrowed_graph_candidate(worktree / ".sidegraph") == main / GRAPH


def test_the_candidate_is_the_borrowed_path_while_the_main_graph_exists(main):
    worktree = add_worktree(main)

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
