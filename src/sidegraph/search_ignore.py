"""Keep recursive search out of the decision store with a project-root ``.ignore``.

The store is committed plain text, so ``rg --hidden`` (and editors and agents that run it)
return raw store lines next to what the MCP returns. A root ``.ignore`` naming the store removes
those hits from ordinary recursive ripgrep searches. This is search hygiene, not access control.

The helper only ever appends one block or does nothing: the bytes that were already in the file
stay a byte-identical prefix, a file that already names the store is left alone, and a file that
is not a plain regular UTF-8 file is never touched. It never raises. Only ``sidegraph-init``
calls it (the explicit-command rule); nothing else may import this module.

Portable core: stdlib only, no host or engine imports.
# see design/superpowers/specs/2026-11-07-root-ignore-for-store-design.md (D2)
"""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path
from typing import NamedTuple

IGNORE_NAME = ".ignore"
MAX_IGNORE_BYTES = 64 * 1024
_DEFAULT_STORE = ".sidegraph"


class SearchIgnoreResult(NamedTuple):
    outcome: str  # created | appended | already_present | left_untouched | outside_project
    line: str | None  # the pattern written or found, else None
    reason: str | None  # for left_untouched / outside_project


def _relative_store(project_root: Path, store_path: Path) -> str | None:
    """The store's path relative to the project as posix text, or ``None`` when it is not
    strictly inside it. Ancestor symlinks are resolved and the store leaf stays lexical, so a
    store reached through a symlinked ancestor is judged by where it really lives."""
    try:
        store_abs = Path(os.path.abspath(store_path))
        resolved = Path(os.path.realpath(store_abs.parent)) / store_abs.name
        relative = resolved.relative_to(os.path.realpath(project_root))
    except (OSError, ValueError):
        return None
    text = relative.as_posix()
    return None if text in ("", ".") else text


def store_pattern(project_root: Path, store_path: Path) -> str | None:
    """The ``.ignore`` line for ``store_path``: the unanchored ``.sidegraph/`` for the default
    store (it also hides a nested per-package store), ``/<relative path>/`` for any other store
    inside the project, ``None`` for a store outside it."""
    relative = _relative_store(project_root, store_path)
    if relative is None:
        return None
    return f"{_DEFAULT_STORE}/" if relative == _DEFAULT_STORE else f"/{_escape(relative)}/"


def _escape(path: str) -> str:
    """``path`` as literal ignore-file text: ``[abc]``, ``*`` and ``?`` would otherwise be globs."""
    return "".join(f"\\{char}" if char in "\\*?[]" else char for char in path)


def _block(pattern: str, eol: str) -> str:
    where = pattern.strip("/") + "/"
    lines = [
        "# Sidegraph: keeps recursive search (rg, editor search) out of the decision store.",
        "# Read memory through the Sidegraph MCP; without it: sidegraph-export-adr --json, "
        "sidegraph-export-okf --out <dir>, sidegraph-doctor.",
        f"# Explicit paths still search it: rg PATTERN {where}",
        pattern,
    ]
    return eol.join(lines) + eol


def _names_the_store(text: str, relative: str) -> bool:
    """Whether a line already names the store, as written by hand or by this helper. Only the
    escaped spelling counts: a raw ``/memory[1]/`` is a character class, not the store
    ``memory[1]``. Trailing whitespace is dropped, as ripgrep does; leading whitespace is part of
    the pattern, so an indented line names nothing. A line without a leading ``/`` and without an
    inner ``/``, or one that starts with ``**/`` or ``/**/``, applies at every depth, so it also
    names a store nested under that name (``pkg/.sidegraph`` is named by ``.sidegraph/``);
    ``name/**`` has an inner slash and does not."""
    full = _escape(relative)
    anywhere = _escape(relative.rsplit("/", 1)[-1])
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line or line.startswith("#"):
            continue
        line = line.removeprefix("!")
        if line.startswith("/**/"):  # `/**/x` is `**/x`
            line = line[1:]
        every_depth = line.startswith("**/")
        line = line.removeprefix("**/")
        anchored = line.startswith("/") or line.endswith("/**")
        line = line.removeprefix("/")
        line = line.removesuffix("/**") if line.endswith("/**") else line.removesuffix("/")
        if line == full or ((every_depth or not anchored) and line == anywhere):
            return True
    return False


def _flags() -> int:
    # Read at call time: a platform without O_NOFOLLOW / O_BINARY must not disable the feature.
    return getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)


def _errno_name(error: OSError) -> str:
    return errno.errorcode.get(error.errno or 0, type(error).__name__)


def _send_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view) :]


def _left(reason: str) -> SearchIgnoreResult:
    return SearchIgnoreResult("left_untouched", None, reason)


def ensure_search_ignore(project_root: Path, store_path: Path) -> SearchIgnoreResult:
    """Add the store to ``<project_root>/.ignore`` (see the module docstring); never raises."""
    try:
        return _ensure(project_root, store_path)
    except OSError as error:
        return _left(_errno_name(error))
    except Exception as error:  # the helper must never fail init
        return _left(type(error).__name__)


def _ensure(project_root: Path, store_path: Path) -> SearchIgnoreResult:
    relative = _relative_store(project_root, store_path)
    pattern = store_pattern(project_root, store_path)
    if relative is None or pattern is None:
        return SearchIgnoreResult("outside_project", None, "store is outside the project")
    if "\n" in relative or "\r" in relative:
        return _left("the store path has a line break")
    target = Path(os.path.realpath(project_root)) / IGNORE_NAME

    try:
        info = os.lstat(target)
    except FileNotFoundError:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _flags(), 0o666)
        try:
            _send_all(fd, _block(pattern, "\n").encode())
        finally:
            os.close(fd)
        return SearchIgnoreResult("created", pattern, None)
    if stat.S_ISLNK(info.st_mode):
        return _left("it is a symlink")
    if not stat.S_ISREG(info.st_mode):
        return _left("it is not a regular file")

    # one descriptor for the checks, the read and the append, so no later path lookup can land on
    # another file; it must be the file the `lstat` saw even where O_NOFOLLOW is missing. A file
    # the person cannot write is still read: it may already name the store.
    denial: str | None = None
    try:
        fd = os.open(target, os.O_RDWR | os.O_APPEND | _flags())
    except PermissionError as denied:
        denial = _errno_name(denied)
        fd = os.open(target, os.O_RDONLY | _flags())
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            return _left("it changed while it was read")
        if not stat.S_ISREG(opened.st_mode):
            return _left("it is not a regular file")
        if opened.st_nlink != 1:
            return _left("it has another hard link")
        if opened.st_size > MAX_IGNORE_BYTES:
            return _left("it is larger than 64 KiB")
        raw = b""
        while chunk := os.read(fd, 65536):
            raw += chunk
            if len(raw) > MAX_IGNORE_BYTES:
                return _left("it is larger than 64 KiB")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return _left("it is not UTF-8")
        if _names_the_store(text, relative):
            return SearchIgnoreResult("already_present", pattern, None)
        if denial:
            return _left(denial)
        if not _unchanged(fd, target, opened, len(raw)):
            return _left("it changed while it was read")
        eol = "\r\n" if "\r\n" in text else "\n"
        separator = eol if raw and not raw.endswith(b"\n") else ""
        _send_all(fd, (separator + _block(pattern, eol)).encode())
    finally:
        os.close(fd)
    return SearchIgnoreResult("appended", pattern, None)


def _unchanged(fd: int, target: Path, opened: os.stat_result, size: int) -> bool:
    """Whether the file read through ``fd`` is still the one at ``target``, with one link and the
    bytes that were read, just before the append."""
    now = os.fstat(fd)
    if now.st_nlink != 1 or now.st_size != size:
        return False
    path_now = os.lstat(target)
    return (path_now.st_dev, path_now.st_ino) == (opened.st_dev, opened.st_ino)
