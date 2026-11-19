"""Bounded repository source reads for the Portal (D3, D5)."""

import ntpath
import os
import stat
import sys
from pathlib import Path

from .contract import Snippet

_MAX_BYTES = 2 * 1024 * 1024


def _parts(rel_path: str) -> list[str]:
    parts = (
        rel_path.replace("\\", "/").split("/") if sys.platform == "win32" else rel_path.split("/")
    )
    if any(not part or part == ".." or "\x00" in part for part in parts):
        raise OSError("Source path must be checkout-relative.")
    if sys.platform == "win32" and any(
        ntpath.splitdrive(part)[0] or ntpath.isreserved(part) for part in parts if part != "."
    ):
        raise OSError("Source path contains a reserved name.")
    return parts


def _open_source(root: Path, parts: list[str]) -> int:
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
    if sys.platform == "win32":
        # Windows has no dir_fd walk. Containment and alias checks cannot pin the path
        # between realpath and open here (design D3).
        base = Path(os.path.realpath(root, strict=True))
        candidate = base.joinpath(*parts)
        actual = Path(os.path.realpath(candidate, strict=True))
        if not actual.is_relative_to(base) or actual != candidate:
            raise OSError("Source path resolves through a link.")
        return os.open(actual, flags | getattr(os, "O_BINARY", 0))

    # The checkout itself may have a trusted alias. Apply no-follow below its descriptor.
    parent = os.open(root, flags | os.O_DIRECTORY)
    flags |= os.O_NOFOLLOW
    directories = flags | os.O_DIRECTORY
    try:
        for part in parts[:-1]:
            child = os.open(part, directories, dir_fd=parent)
            os.close(parent)
            parent = child
        return os.open(parts[-1], flags, dir_fd=parent)
    finally:
        os.close(parent)


def _read_span(
    repo_root: Path | None, rel_path: str, start: int, end: int | None, *, full: bool
) -> tuple[Snippet, int]:
    """Read the span and report the mtime of the same checked file descriptor."""
    parts = _parts(rel_path)
    if repo_root is None:
        raise OSError("Source snippets require a repository root.")
    fd = _open_source(repo_root, parts)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_BYTES:
            raise OSError("Source must be a regular file of at most 2 MB.")
        with os.fdopen(fd, "rb", closefd=False) as source:
            data = source.read(_MAX_BYTES)
    finally:
        os.close(fd)

    if not data:
        raise OSError("Source file is empty.")
    text = data.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
    lines = text.removesuffix("\n").split("\n")
    first = max(1, min(start, len(lines)))
    last = min(len(lines), max(first, end) if end is not None else len(lines))
    last = min(last, first + (200 if full else 12) - 1)
    return (
        Snippet(file=rel_path, start=first, end=last, lines=lines[first - 1 : last], shifted=False),
        info.st_mtime_ns,
    )


def read_span(
    repo_root: Path | None, rel_path: str, start: int, end: int | None, *, full: bool
) -> Snippet:
    """Read up to 12 or 200 lines of a checked source file (Portal design, D3, D5)."""
    snippet, _ = _read_span(repo_root, rel_path, start, end, full=full)
    return snippet
