"""Seed paths across checkouts, on literal strings: no git binary, runs on Windows too.

``worktree_seeds`` maps an absolute seed to the checkout that holds it, and ``seed_ladder`` and
the not-in-graph block treat a drive-lettered path as malformed unless it sits under a checkout.
see design/superpowers/specs/2026-10-11-worktree-only-seeds-design.md (D2, D4, test 15)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sidegraph import nearest_anchored, seed_ladder, server, worktree_seeds
from sidegraph.worktree_seeds import Worktree

POSIX_LIST = """worktree /r
HEAD aaaa
branch refs/heads/main

worktree /r/.worktrees/feat
HEAD bbbb
branch refs/heads/wt-feat

worktree /r/.worktrees/det
HEAD cccc
detached

worktree /r/.worktrees/gone
HEAD dddd
branch refs/heads/gone
prunable gitdir file points to non-existent location

worktree /bare.git
bare
"""

WIN_LIST = (
    "worktree C:/work/me/R\nHEAD a\nbranch refs/heads/main\n\n"
    "worktree C:/work/me/R-feat\nHEAD b\nbranch refs/heads/f\n"
)


def test_parse_porcelain_posix_skips_prunable_and_bare():
    got = worktree_seeds.parse_porcelain(POSIX_LIST)
    assert [(w.path.as_posix(), w.branch) for w in got] == [
        ("/r", "main"),
        ("/r/.worktrees/feat", "wt-feat"),
        ("/r/.worktrees/det", None),
    ]


def test_parse_porcelain_handles_forward_slash_drive_paths():
    got = worktree_seeds.parse_porcelain(WIN_LIST)
    assert [w.branch for w in got] == ["main", "f"]
    assert [w.path.as_posix() for w in got] == ["C:/work/me/R", "C:/work/me/R-feat"]


@pytest.mark.parametrize(
    "p", ["C:\\x\\y.py", "C:/x/y.py", "c:x.py", "\\\\srv\\share\\x", "//srv/share/x"]
)
def test_has_drive_true(p):
    assert worktree_seeds.has_drive(p)


@pytest.mark.parametrize("p", ["pkg/new.py", "/abs/x.py", "pkg\\new.py", "./a", ".", ""])
def test_has_drive_false(p):
    assert not worktree_seeds.has_drive(p)


def test_is_repo_relative_rejects_drive_paths_in_both_copies():
    for fn in (server._is_repo_relative, nearest_anchored._is_repo_relative):
        assert not fn("C:\\x\\y.py")
        assert not fn("C:/x/y.py")
        assert fn("pkg/new.py")
        assert fn("pkg\\new.py")  # a literal filename character on POSIX


def test_rebase_windows_picks_the_shortest_remainder_case_insensitively():
    root = Path("C:/work/me/R")
    feat = Worktree(Path("C:/work/me/R/.worktrees/feat"), "f")
    sib = Worktree(Path("C:/work/me/R-feat"), "g")
    got = worktree_seeds.rebase(
        "c:/WORK/ME/r/.worktrees/feat/pkg/new.py", root, [feat, sib], windows=True
    )
    assert got == (feat, "pkg/new.py")
    assert worktree_seeds.rebase(
        "C:\\work\\me\\R-feat\\pkg\\new.py", root, [feat, sib], windows=True
    ) == (sib, "pkg/new.py")
    assert worktree_seeds.rebase("C:/work/me/R/pkg/m.py", root, [feat, sib], windows=True) == (
        None,
        "pkg/m.py",
    )
    assert worktree_seeds.rebase("C:/elsewhere/x.py", root, [feat, sib], windows=True) is None
    assert worktree_seeds.rebase("C:/work/me/R", root, [feat], windows=True) is None


def test_rebase_drive_path_is_malformed_off_windows():
    root = Path("/r")
    assert worktree_seeds.rebase("C:/r/pkg/m.py", root, [], windows=False) is None


class _Reader:
    def __init__(self, files):
        self._files = frozenset(files)

    def source_files(self):
        return self._files


def _files(held, root, worktrees=()):
    return seed_ladder._Files(frozenset(held), root, lambda: list(worktrees))


def test_normalise_drive_seed_under_an_injected_root(monkeypatch):
    monkeypatch.setattr(worktree_seeds, "WINDOWS", True)
    feat = Worktree(Path("C:/work/me/R-feat"), "f")
    files = _files({"pkg/m.py"}, Path("C:/work/me/R"), [feat])
    assert seed_ladder._normalise("C:/work/me/R/pkg/m.py", files) == "pkg/m.py"
    assert seed_ladder._normalise("C:\\work\\me\\R-feat\\pkg\\new.py", files) == "pkg/new.py"
    assert seed_ladder._normalise("C:/elsewhere/x.py", files) is None


def test_normalise_drive_seed_is_malformed_off_windows(monkeypatch):
    monkeypatch.setattr(worktree_seeds, "WINDOWS", False)
    files = _files({"pkg/m.py"}, Path("C:/work/me/R"))
    assert seed_ladder._normalise("C:/work/me/R/pkg/m.py", files) is None


def test_backslash_is_a_filename_character_on_posix_and_a_separator_on_windows(monkeypatch):
    files = _files({"pkg/new.py"}, Path("/r"))
    monkeypatch.setattr(worktree_seeds, "WINDOWS", False)
    assert seed_ladder._normalise("pkg\\new.py", files) == "pkg\\new.py"
    monkeypatch.setattr(worktree_seeds, "WINDOWS", True)
    assert seed_ladder._normalise("pkg\\new.py", files) == "pkg/new.py"


def test_an_in_root_drive_seed_that_is_a_graph_file_makes_no_listing(monkeypatch):
    monkeypatch.setattr(worktree_seeds, "WINDOWS", True)
    calls: list[int] = []
    files = seed_ladder._Files(
        frozenset({"pkg/m.py"}),
        Path("C:/work/me/R"),
        lambda: calls.append(1) or [],
    )

    assert seed_ladder._normalise("C:/work/me/R/pkg/m.py", files) == "pkg/m.py"
    assert calls == []
    # A drive seed the graph lacks does list, to look for a worktree that holds it.
    assert seed_ladder._normalise("C:/work/me/R/pkg/new.py", files) == "pkg/new.py"
    assert calls == [1]
