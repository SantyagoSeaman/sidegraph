"""The host surfaces look a missing relative store up inside the repository (spec D1).

A session started in ``R/sub`` anchors ``.sidegraph`` to ``R/sub``. Where that path is absent
and the repository's store sits above, ``resolve_store_location(search_ancestors=True)`` returns
the repository's store instead of the path a second, empty store would be created at. The CLI
keeps today's rule: the flag defaults to off.
see design/superpowers/specs/2026-10-01-subdirectory-launch-design.md
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import sidegraph.config as config
from sidegraph.store import Store


def _open_once(path: Path) -> Path:
    """A store directory that exists and is initialised."""
    Store(path).close()
    return path


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """``R`` with ``R/.git/``, a store at ``R/.sidegraph`` and ``R/sub``. ``tmp_path`` sits
    under no repository, so ``R`` is the only boundary."""
    root = tmp_path / "R"
    (root / ".git").mkdir(parents=True)
    (root / "sub").mkdir()
    _open_once(root / ".sidegraph")
    return root


def _location(anchor: Path, **kwargs):
    return config.resolve_store_location(root=str(anchor), search_ancestors=True, **kwargs)


# -- the lookup finds the repository's store ---------------------------------------------


def test_t1_relative_sidegraph_dir_finds_the_repository_store(repo, monkeypatch):
    monkeypatch.setenv("SIDEGRAPH_DIR", ".sidegraph")
    location = _location(repo / "sub")
    assert location.path == str(repo / ".sidegraph")
    assert location.base == str(repo)


def test_t2_default_store_finds_the_repository_store(repo, capsys):
    location = _location(repo / "sub")
    assert location.path == str(repo / ".sidegraph")
    assert location.base == str(repo)
    assert capsys.readouterr().err == ""  # found, so nothing is created and nothing is warned


def test_t3b_the_cli_rule_is_unchanged_without_the_flag(repo, monkeypatch):
    """Red against design R4 (M7): the flag defaults to off, so ``resolve_store_path`` keeps
    anchoring to where it is run from."""
    monkeypatch.chdir(repo / "sub")
    resolved = config.resolve_store_path(warn_on_create=False)
    assert Path(resolved).resolve() == (repo / "sub" / ".sidegraph").resolve()
    assert config.resolve_store_location(warn_on_create=False).path == resolved


# -- nearest wins, and nothing found keeps today's result --------------------------------


def test_t4_a_store_at_the_anchor_wins_over_the_repository_store(repo):
    """Guards nearest-wins (M1: search before checking the anchor)."""
    pkg_store = _open_once(repo / "pkg" / ".sidegraph")
    location = _location(repo / "pkg")
    assert location.path == str(pkg_store)
    assert location.base == str(repo / "pkg")


def test_t5_no_store_anywhere_returns_the_anchor_and_warns(tmp_path, capsys):
    root = tmp_path / "R"
    (root / ".git").mkdir(parents=True)
    (root / "sub").mkdir()
    location = _location(root / "sub")
    assert location.path == str(root / "sub" / ".sidegraph")
    assert location.base == str(root / "sub")
    assert "creating new store" in capsys.readouterr().err


# -- the boundary ------------------------------------------------------------------------


def test_t6_the_walk_stops_at_the_repository_boundary(tmp_path):
    """Red against design R2 (M2): a store above the repository belongs to someone else."""
    outer = _open_once(tmp_path / "O" / ".sidegraph")
    assert outer.is_dir()
    inner = tmp_path / "O" / "R"
    (inner / ".git").mkdir(parents=True)
    (inner / "sub").mkdir()
    location = _location(inner / "sub", warn_on_create=False)
    assert location.path == str(inner / "sub" / ".sidegraph")
    assert location.base == str(inner / "sub")


def test_t6b_an_anchor_that_is_the_boundary_visits_nothing_above(tmp_path):
    """Red against an off-by-one walk (M2b): the boundary's own parent is out of bounds."""
    _open_once(tmp_path / "O" / ".sidegraph")
    root = tmp_path / "O" / "R"
    (root / ".git").mkdir(parents=True)
    location = _location(root, warn_on_create=False)
    assert location.path == str(root / ".sidegraph")
    assert location.base == str(root)


def test_t7_a_git_file_marks_the_boundary_too(tmp_path):
    """A linked worktree or a submodule has a ``.git`` FILE."""
    root = tmp_path / "R"
    root.mkdir()
    (root / ".git").write_text("gitdir: /elsewhere/.git/worktrees/R\n")
    (root / "sub").mkdir()
    _open_once(root / ".sidegraph")
    location = _location(root / "sub")
    assert location.path == str(root / ".sidegraph")
    assert location.base == str(root)


def test_t8b_outside_a_repository_nothing_is_looked_up(tmp_path):
    """Red against design R2 (M4): no ``.git`` entry on the way up means no boundary."""
    _open_once(tmp_path / "O" / ".sidegraph")
    (tmp_path / "O" / "sub").mkdir()
    location = _location(tmp_path / "O" / "sub", warn_on_create=False)
    assert location.path == str(tmp_path / "O" / "sub" / ".sidegraph")
    assert location.base == str(tmp_path / "O" / "sub")


# -- what the lookup never touches ---------------------------------------------------------


def test_t8_an_absolute_missing_path_is_returned_as_given(repo, monkeypatch):
    target = str(repo.parent / "elsewhere" / "store")
    monkeypatch.setenv("SIDEGRAPH_DIR", target)
    location = _location(repo / "sub")
    assert location.path == target
    assert location.base is None


def test_t8c_a_value_with_a_parent_part_is_not_searched(tmp_path, monkeypatch):
    """Red against a walk that allows ``..`` (M5). The anchor is ``O/R/sub``, so the value
    resolves to ``O/R/shared`` there (absent) and to ``O/shared`` from ``R``, which exists."""
    repo_root = tmp_path / "O" / "R"
    (repo_root / ".git").mkdir(parents=True)
    (repo_root / "sub").mkdir()
    (tmp_path / "O" / "shared").mkdir()
    monkeypatch.setenv("SIDEGRAPH_DIR", "../shared")
    location = _location(repo_root / "sub")
    assert location.path == os.path.join(str(repo_root / "sub"), "../shared")
    assert location.base == str(repo_root / "sub")


def test_t8d_a_dangling_symlink_at_the_anchor_means_as_given(repo, monkeypatch):
    """Red against a walk that asks ``path_state`` (M6): ``os.stat`` follows the link and
    calls it missing, ``os.lstat`` does not."""
    (repo / "sub" / ".sidegraph").symlink_to(repo / "nowhere")
    monkeypatch.setenv("SIDEGRAPH_DIR", ".sidegraph")
    location = _location(repo / "sub")
    assert location.path == str(repo / "sub" / ".sidegraph")
    assert location.base == str(repo / "sub")


def test_the_other_ways_to_name_a_store_are_never_searched(repo, monkeypatch):
    """``--db`` and ``SIDEGRAPH_DB`` (and its ``.sidegraph`` fallback) stay as given."""
    explicit = config.resolve_store_location(
        ".sidegraph", root=str(repo / "sub"), search_ancestors=True
    )
    assert explicit.path == str(repo / "sub" / ".sidegraph")
    assert explicit.base is None

    monkeypatch.setenv("SIDEGRAPH_DB", "sidegraph.db")  # a bare name falls back to `.sidegraph`
    monkeypatch.setattr(config, "_deprecation_warned", True)
    legacy = _location(repo / "sub")
    assert legacy.path == str(repo / "sub" / ".sidegraph")


def test_a_nested_relative_store_value_is_found_in_the_ancestors(tmp_path, monkeypatch):
    """The value is joined onto each ancestor, not only its first segment."""
    root = tmp_path / "R"
    (root / ".git").mkdir(parents=True)
    (root / "sub").mkdir()
    _open_once(root / ".config" / "sidegraph")
    monkeypatch.setenv("SIDEGRAPH_DIR", ".config/sidegraph")
    location = _location(root / "sub")
    assert location.path == str(root / ".config" / "sidegraph")
    assert location.base == str(root)  # the base is the ancestor the value was joined to


def test_ancestor_store_names_the_first_store_above_a_base(repo):
    """The D4 helper: the same walk, starting one level above the given base."""
    found = config.ancestor_store(str(repo / "sub"), ".sidegraph")
    assert found == config.StoreLocation(str(repo / ".sidegraph"), str(repo))
    assert config.ancestor_store(str(repo), ".sidegraph") is None  # the base is the boundary
    assert config.ancestor_store(str(repo / "sub"), "../shared") is None
    assert config.ancestor_store(str(repo / "sub"), str(repo / ".sidegraph")) is None


# -- a `.git` at the home directory is no boundary ---------------------------------------


def test_a_git_entry_at_the_home_directory_is_no_boundary(tmp_path, monkeypatch):
    """A dotfiles repository at ``$HOME`` must not make every non-git project beneath it share
    ``~/.sidegraph``. The ``.git`` entry at home, or above it, is ignored; with no other
    boundary found there is no walk (M9)."""
    home = tmp_path / "home"
    (home / ".git").mkdir(parents=True)
    (tmp_path / ".git").mkdir()  # and one above home
    _open_once(home / ".sidegraph")
    notrepo = home / "projects" / "notrepo"
    notrepo.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    location = _location(notrepo, warn_on_create=False)
    assert location.path == str(notrepo / ".sidegraph")
    assert location.base == str(notrepo)
    assert config.ancestor_store(str(notrepo), ".sidegraph") is None


def test_a_repository_below_the_home_directory_is_still_a_boundary(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".git").mkdir(parents=True)
    root = home / "projects" / "repo"
    (root / ".git").mkdir(parents=True)
    (root / "sub").mkdir()
    _open_once(root / ".sidegraph")
    _open_once(home / ".sidegraph")
    monkeypatch.setenv("HOME", str(home))
    location = _location(root / "sub")
    assert location.path == str(root / ".sidegraph")
    assert location.base == str(root)


def test_without_a_home_directory_there_is_no_special_case(tmp_path, monkeypatch):
    """``Path.home()`` raises when no home can be found: the ``.git`` entry counts as usual."""
    root = tmp_path / "R"
    (root / ".git").mkdir(parents=True)
    (root / "sub").mkdir()
    _open_once(root / ".sidegraph")

    def no_home():
        raise RuntimeError("Could not determine home directory.")

    monkeypatch.setattr(config.Path, "home", staticmethod(no_home))
    assert _location(root / "sub").path == str(root / ".sidegraph")


# -- a symlink in an ancestor is a store, as given ---------------------------------------


def test_a_dangling_symlink_in_an_ancestor_is_found_as_given(tmp_path):
    """The rule at the anchor, one level up: the link means "a store lives here", live or
    not. Skipping it would have a launch in ``R/sub`` create a new stray (M10)."""
    root = tmp_path / "R"
    (root / ".git").mkdir(parents=True)
    (root / "sub").mkdir()
    (root / ".sidegraph").symlink_to("../unmounted/store")  # dangling
    location = _location(root / "sub")
    assert location.path == str(root / ".sidegraph")
    assert location.base == str(root)
    assert not os.path.lexists(root / "sub" / ".sidegraph")
