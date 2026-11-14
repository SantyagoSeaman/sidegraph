"""A local, content-free tally of what each delivering hook did on each run.

One line per run of PreToolUse, SessionStart and SubagentStart is appended to
``<store>/index.db-hooks.log``: ``<epoch ms> <hook> <outcome> <elapsed ms>``, and a fifth field
on some lines: why an ``empty`` run delivered nothing, or ``partial`` on a ``lock`` line whose
run handed a record over before the lock refused the rest. The name starts with
``index.db`` so the ``.gitignore`` every store carries (``index.db*``) keeps it out of git; the
file is written only where that line is present. It is not an ``index.db`` write, so a held lock
does not stop it, and it never raises and never blocks.
see design/superpowers/specs/2026-11-05-hook-outcome-log.md
"""

from __future__ import annotations

import os
import sqlite3
import stat
import time
from collections.abc import Iterable
from typing import NamedTuple

from ..store_layout import FORMAT_MARKER_NAME, GITIGNORE_NAME

try:
    import fcntl
except ImportError:  # Windows: no flock; see ``_rotate``
    fcntl = None  # type: ignore[assignment]

LOG_NAME = "index.db-hooks.log"
ROTATED_NAME = LOG_NAME + ".1"
# The line in the store's committed ``.gitignore`` that keeps both names out of git. ``Store``
# writes it into every store (``store._GITIGNORE_CONTENT``); a store whose file lacks it gets no
# log.
IGNORE_PATTERN = b"index.db*"
# One rotation over the ``.1`` name when a write leaves the file past this size, so the pair holds
# between 4 and 8 MiB of history. see the spec (D2)
MAX_BYTES = 4 * 1024 * 1024

DELIVERED = "delivered"
EMPTY = "empty"
LOCK = "lock"
ERROR = "error"
OUTCOMES = (DELIVERED, EMPTY, LOCK, ERROR)

# The fifth field of a line, its detail: one lower-case word. On an ``empty`` line it is the
# cause, why the run delivered nothing.
NO_ENTITY = "no-entity"  # no file the call names has an anchored entity
NO_RECORD = "no-record"  # an entity is there, and no record that can be handed over
COLD = "cold"  # a record was left out: the index is cold and its binding's status not computed
REPEAT = "repeat"  # every file with records was already delivered to this agent
CAP = "cap"  # a file with records was refused: the agent already holds its ten files
DUPLICATE = "duplicate"  # a second SessionStart for one session inside the duplicate window
UNKNOWN = "unknown"  # the cause was not read: another process held the lock, and no run waits
CAUSES = (NO_ENTITY, NO_RECORD, COLD, REPEAT, CAP, DUPLICATE, UNKNOWN)
# On a ``lock`` line: the run handed over at least one record before the lock refused the rest.
PARTIAL = "partial"
# A reader keeps a detail it does not know (a newer writer's), within this shape.
_DETAIL_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz-")
_DETAIL_MAX = 24


def is_lock_error(error: BaseException) -> bool:
    """A refusal because another process holds the index's lock: ``SQLITE_BUSY`` or
    ``SQLITE_LOCKED``. ``sqlite_errorcode`` carries the extended result code (BUSY_SNAPSHOT is
    517), whose low byte is the primary one. A read-only or unreadable index is not a lock."""
    if not isinstance(error, sqlite3.OperationalError):
        return False
    code = getattr(error, "sqlite_errorcode", None)
    return isinstance(code, int) and (code & 0xFF) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)


_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
# How much of the store's ``.gitignore`` is read; a line cut at the limit is dropped, not matched.
_IGNORE_READ_LIMIT = 65536


def _ignored(store_dir: str) -> bool:
    """The store's own ``.gitignore`` holds the ``index.db*`` line, read the way git reads it.

    A missing file, or one edited down to ``index.db``, means the log would show up in
    ``git status`` and in ``git add -A``, so nothing is written (the hooks never rewrite a
    committed file). The file is opened like the log: no symlink is followed (git refuses a
    linked ``.gitignore`` too) and a named pipe cannot make the hook wait. Git ignores trailing
    spaces but not leading ones, so only the line's end is trimmed; any negation line (``!``)
    could re-include the log, so a file holding one gets no log either.
    """
    try:
        fd = os.open(os.path.join(store_dir, GITIGNORE_NAME), os.O_RDONLY | _NOFOLLOW)
    except OSError:
        return False
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return False
        data = os.read(fd, _IGNORE_READ_LIMIT)
    except OSError:
        return False
    finally:
        os.close(fd)
    if len(data) == _IGNORE_READ_LIMIT:
        data = data[: data.rfind(b"\n") + 1]
    found = False
    for line in data.splitlines():
        line = line.rstrip(b" \r")
        if line.startswith(b"!"):
            return False
        found = found or line == IGNORE_PATTERN
    return found


def _rotate(path: str, rotated: str, fd: int) -> None:
    """Move the full log aside once, even when many runs pass the limit together (POSIX).

    One process takes a non-blocking ``flock`` on the full file's descriptor and, under it,
    checks that the path still names that file: a run that was paused while another rotated
    sees a different file and leaves it alone, so the retained history is never replaced by a
    small new log. When the rotated name cannot be used (a directory) the log is truncated
    instead, but only once it is twice the limit, so it stays bounded without a run wiping a
    log another run has just emptied. A filesystem without ``flock`` rotates without it.
    """
    if fcntl is not None:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return  # another run is rotating
        except OSError:
            pass  # no flock on this filesystem: the inode check below still holds
    mine = os.fstat(fd)
    now = os.lstat(path)
    if (mine.st_dev, mine.st_ino) != (now.st_dev, now.st_ino):
        return  # already rotated: the path is a new file
    try:
        os.replace(path, rotated)
    except OSError:
        if mine.st_size > 2 * MAX_BYTES:
            os.ftruncate(fd, 0)


def _rotate_closed(path: str, rotated: str) -> None:
    """The rotation where there is no ``flock`` (Windows). The log is closed first: Windows
    refuses to rename a file another handle holds open, which would make every rotation fall
    back to truncation. The size is read again just before the rename, so a run that was paused
    while another rotated sees the new, small file and leaves the retained history alone."""
    if os.lstat(path).st_size <= MAX_BYTES:
        return
    try:
        os.replace(path, rotated)
    except OSError:
        if os.lstat(path).st_size > 2 * MAX_BYTES:
            os.truncate(path, 0)


def _is_detail(text: str) -> bool:
    return 0 < len(text) <= _DETAIL_MAX and set(text) <= _DETAIL_CHARS


def record(
    store_dir: str | os.PathLike[str],
    hook: str,
    outcome: str,
    elapsed_ms: int,
    detail: str | None = None,
) -> None:
    """Append one line to the store's hook log. Never raises and never blocks: it creates no
    ``store_dir``, writes only beside a store (its format marker exists: a directory that merely
    holds an unreadable ``index.db`` gets nothing) whose ``.gitignore`` ignores the file, follows
    no symlink, opens without waiting (a FIFO planted at the path cannot stop a hook), skips
    anything that is not a regular file or that has a second name (a hard link would carry the
    line into another file), and writes nothing when ``SIDEGRAPH_TELEMETRY=off``.
    see the spec (D2)."""
    try:
        from ..config import telemetry_enabled

        if not telemetry_enabled():
            return
        directory = os.fspath(store_dir)
        if not os.path.exists(os.path.join(directory, FORMAT_MARKER_NAME)):
            return
        if not _ignored(directory):
            return
        # The newline goes first: a row cut short (a full disk) or another run's fragment ends at
        # this run's newline, so no row is ever glued to a fragment, and a repair needs no second
        # write. The file therefore opens with an empty line and its last row has no newline.
        text = f"\n{int(time.time() * 1000)} {hook} {outcome} {elapsed_ms}"
        if detail is not None and outcome in (EMPTY, LOCK) and _is_detail(detail):
            text += f" {detail}"
        line = text.encode("ascii")
        path = os.path.join(directory, LOG_NAME)
        rotated = os.path.join(directory, ROTATED_NAME)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | _NOFOLLOW, 0o644)
        try:
            status = os.fstat(fd)
            if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1:
                return
            os.write(fd, line)
            full = os.fstat(fd).st_size > MAX_BYTES
            if full and fcntl is not None:
                _rotate(path, rotated, fd)
        finally:
            os.close(fd)
        if full and fcntl is None:
            _rotate_closed(path, rotated)
    except Exception:
        pass


class Run:
    """One hook invocation: what it saw while delivering, written once by :meth:`finish`.

    ``store_dir`` is set by the hook when it has resolved a store whose index it used or lost to a
    lock; a run that never did writes nothing. :meth:`note` is called for the errors of the
    delivery steps only (opening the index, selecting records, claiming a file), never for
    bookkeeping such as the touch row.
    """

    def __init__(self, hook: str) -> None:
        self.hook = hook
        self.store_dir: str | os.PathLike[str] | None = None
        self._started = time.monotonic()
        self._lock = False
        self._error = False
        # Why the run delivered nothing, when the hook knows (one of ``CAUSES``).
        self.cause: str | None = None

    @property
    def lock_seen(self) -> bool:
        return self._lock

    def note(self, error: BaseException) -> None:
        if is_lock_error(error):
            self._lock = True
        else:
            self._error = True

    def outcome(self, delivered: bool) -> str:
        if self._lock:
            return LOCK
        if delivered:
            return DELIVERED
        return ERROR if self._error else EMPTY

    def finish(self, delivered: bool) -> None:
        """Write the line for this run. Safe to call when no store was resolved."""
        try:
            if self.store_dir is None:
                return
            elapsed = int((time.monotonic() - self._started) * 1000)
            outcome = self.outcome(delivered)
            # A run that handed over a record and then lost a later step to a lock is still
            # ``lock`` (records may have been left out); the detail says something arrived.
            # A cause is written on an ``empty`` line only: a lock refused a read, so no
            # cause set before it is known.
            detail = (
                PARTIAL
                if outcome == LOCK and delivered
                else self.cause
                if outcome == EMPTY
                else None
            )
            record(self.store_dir, self.hook, outcome, elapsed, detail)
        except Exception:
            pass


class RunRow(NamedTuple):
    at_ms: int
    hook: str
    outcome: str
    elapsed_ms: int
    detail: str | None = None  # the fifth field: an ``empty`` row's cause, ``partial`` on a lock


def _read_tail(path: str) -> list[str]:
    """The lines of one log file, at most its last ``2 * MAX_BYTES`` (a file left oversized by an
    older rotation is not read whole). A tail is read from one byte earlier and cut after its
    first newline, so a row the tail starts inside is dropped and a row it starts on is kept.
    Opened like the writer opens it, and a link refused by name too: a link, a device or a
    pipe is no log, reads as empty, and no read asks for more than the tail."""
    limit = 2 * MAX_BYTES + 1
    try:
        # ``O_NOFOLLOW`` is missing on Windows, so the refusal rests on the name too: a link is
        # no log, and the file opened must be the one the name showed (no swap in between).
        named = os.lstat(path)
        if stat.S_ISLNK(named.st_mode):
            return []
        fd = os.open(path, os.O_RDONLY | _NOFOLLOW)
    except OSError:
        return []
    try:
        status = os.fstat(fd)
        if not stat.S_ISREG(status.st_mode) or not os.path.samestat(named, status):
            return []
        start = max(0, status.st_size - limit)
        os.lseek(fd, start, os.SEEK_SET)
        chunks: list[bytes] = []
        remaining = limit
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 1024 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
    except OSError:
        return []
    finally:
        os.close(fd)
    if start:
        data = data[data.find(b"\n") + 1 :] if b"\n" in data else b""
    return data.decode("ascii", errors="replace").splitlines()


def read_runs(store_dir: str | os.PathLike[str]) -> list[RunRow]:
    """The rows of the rotated file, then the current one. A line that does not parse (a torn
    last line, a hand edit, another tool's text, a number that is not plain digits) is dropped."""
    rows: list[RunRow] = []
    directory = os.fspath(store_dir)
    for name in (ROTATED_NAME, LOG_NAME):
        for line in _read_tail(os.path.join(directory, name)):
            parts = line.split(" ")
            if len(parts) not in (4, 5) or not parts[1] or parts[2] not in OUTCOMES:
                continue
            detail = parts[4] if len(parts) == 5 else None
            if detail is not None and (parts[2] not in (EMPTY, LOCK) or not _is_detail(detail)):
                continue
            stamp, elapsed = parts[0], parts[3]
            if not (
                stamp.isascii() and stamp.isdigit() and elapsed.isascii() and elapsed.isdigit()
            ):
                continue
            if len(stamp) > 15 or len(elapsed) > 9:
                continue
            rows.append(RunRow(int(stamp), parts[1], parts[2], int(elapsed), detail))
    return rows


def _percentile(sorted_values: list[int], percent: int) -> int:
    """Nearest rank: the smallest value with at least ``percent`` % of the sample at or below it."""
    rank = max(1, -(-percent * len(sorted_values) // 100))
    return sorted_values[min(rank, len(sorted_values)) - 1]


def summarize(rows: Iterable[RunRow]) -> dict[str, dict[str, int]]:
    """Per hook: ``runs``, one count per outcome, and the ``p50_ms`` / ``p95_ms`` elapsed time."""
    by_hook: dict[str, list[RunRow]] = {}
    for row in rows:
        by_hook.setdefault(row.hook, []).append(row)
    summary: dict[str, dict[str, int]] = {}
    for hook, hook_rows in sorted(by_hook.items()):
        times = sorted(r.elapsed_ms for r in hook_rows)
        entry = {"runs": len(hook_rows)}
        for outcome in OUTCOMES:
            entry[outcome] = sum(1 for r in hook_rows if r.outcome == outcome)
        entry["p50_ms"] = _percentile(times, 50)
        entry["p95_ms"] = _percentile(times, 95)
        summary[hook] = entry
    return summary


def empty_causes(rows: Iterable[RunRow]) -> dict[str, dict[str, int]]:
    """Per hook: how many ``empty`` runs each cause accounts for. A row without a cause (an
    older writer's line) is counted under ``unrecorded``."""
    causes: dict[str, dict[str, int]] = {}
    for row in rows:
        if row.outcome == EMPTY:
            by_cause = causes.setdefault(row.hook, {})
            key = row.detail or "unrecorded"
            by_cause[key] = by_cause.get(key, 0) + 1
    return {hook: dict(sorted(by_cause.items())) for hook, by_cause in sorted(causes.items())}
