"""The other checkouts of a repository, for seeds that name a file the server's checkout lacks.

The decision MCP is rooted at one checkout but is shared by subagents working in sibling linked
worktrees, so a well-formed path may be a real file in another checkout. :func:`linked_worktrees`
lists them once (``git worktree list --porcelain``); :func:`locate` finds which hold a file and
:func:`rebase` maps an absolute path to the checkout that contains it.

Stdlib plus ``gitenv`` only: no ``seed_ladder`` import, the existence test is a predicate.
# see design/superpowers/specs/2026-10-11-worktree-only-seeds-design.md (D2, D4)
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

from .gitenv import git_env

WINDOWS = os.name == "nt"
_CAP = 32
_TIMEOUT = 5
_DRIVE_RE = re.compile(r"^(?:[A-Za-z]:|[\\/]{2}[^\\/])")


@dataclass(frozen=True)
class Worktree:
    """One checkout git lists: its directory and its branch (``None`` when detached)."""

    path: Path
    branch: str | None


def has_drive(p: str) -> bool:
    """Whether ``p`` starts with a drive letter (``C:\\x``, ``C:/x``, ``C:x``) or a UNC prefix.
    A relative file literally named ``a:b.py`` reads as drive-lettered, an accepted cost."""
    return bool(_DRIVE_RE.match(p))


def parse_porcelain(text: str) -> list[Worktree]:
    """The checkouts in ``git worktree list --porcelain`` output, skipping prunable and bare
    entries. Paths are taken as git prints them (forward slashes on Windows)."""
    out: list[Worktree] = []
    for block in re.split(r"\n\s*\n", text.strip()):
        path: str | None = None
        branch: str | None = None
        skip = False
        for line in block.splitlines():
            if line.startswith("worktree "):
                path = line[len("worktree ") :]
            elif line.startswith("branch "):
                branch = line[len("branch ") :].removeprefix("refs/heads/")
            elif line == "bare" or line.startswith("prunable"):
                skip = True
        if path and not skip:
            out.append(Worktree(Path(path), branch))
    return out


def linked_worktrees(root: Path) -> list[Worktree]:
    """The checkouts of ``root``'s repository other than ``root``, at most 32; any failure
    (no git, a timeout, not a repository) is ``[]``."""
    try:
        done = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=root,
            env=git_env(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if done.returncode != 0:
        return []
    here = os.path.realpath(root)
    found: list[Worktree] = []
    for w in parse_porcelain(done.stdout):
        if os.path.realpath(w.path) == here or not w.path.is_dir():
            continue
        found.append(w)
        if len(found) >= _CAP:
            break
    return found


def locate(
    rel: str, worktrees: Iterable[Worktree], exists: Callable[[Path, str], bool]
) -> list[Worktree]:
    """The checkouts in which ``rel`` is a file, per the ``exists(checkout, rel)`` predicate."""
    return [w for w in worktrees if exists(w.path, rel)]


def _parts(p: str, windows: bool) -> tuple[str, ...]:
    if windows:
        return tuple(x.casefold() for x in PureWindowsPath(p).parts)
    return PurePosixPath(p).parts


def rebase(
    abs_path: str, root: Path, worktrees: Iterable[Worktree], *, windows: bool | None = None
) -> tuple[Worktree | None, str] | None:
    """Map an absolute ``abs_path`` to ``(checkout, repo-relative path)``: the checkout among
    ``root`` (``None`` in the result) and ``worktrees`` that contains it with the shortest
    remainder, so a worktree nested under ``root`` wins over ``root``. ``None`` when no
    checkout contains it. Compared on realpath on POSIX; on Windows (or ``windows=True``) case
    insensitively on the path as written, and a drive path is never rebased elsewhere."""
    win = WINDOWS if windows is None else windows
    if has_drive(abs_path) and not win:
        return None
    subject = abs_path if win else os.path.realpath(abs_path)
    best: tuple[int, Worktree | None] | None = None
    candidates: list[tuple[Worktree | None, Path]] = [
        (None, root),
        *((w, w.path) for w in worktrees),
    ]
    target = _parts(subject, win)
    for owner, base in candidates:
        base_s = str(base) if win else os.path.realpath(base)
        base_parts = _parts(base_s, win)
        if len(target) > len(base_parts) and target[: len(base_parts)] == base_parts:
            remainder = len(target) - len(base_parts)
            if best is None or remainder < best[0]:
                best = (remainder, owner)
    if best is None:
        return None
    n = (
        len(_parts(str(root) if win else os.path.realpath(root), win))
        if best[1] is None
        else len(_parts(str(best[1].path) if win else os.path.realpath(best[1].path), win))
    )
    original = PureWindowsPath(abs_path).parts if win else PurePosixPath(subject).parts
    return best[1], "/".join(original[n:])
