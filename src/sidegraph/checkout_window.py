"""Keep first-open marker backfills out of Git's checkout window.

Git holds its index lock while it unlinks the marker, smudges its replacement, then
exclusively creates it. HEAD can still name the commit being left. Store calls this
stdlib-only guard only when its marker is missing.
# see design/superpowers/specs/2026-10-28-format-marker-checkout-race-design.md
"""

from __future__ import annotations

import os
import shlex
import subprocess
import time
from collections.abc import Callable
from enum import Enum, auto
from pathlib import Path
from typing import Literal, NamedTuple

from .gitenv import git_env
from .store_layout import FORMAT_MARKER_NAME, StoreRequirementError

GIT_TIMEOUT_SECONDS = 1.0
MARKER_WAIT_SECONDS = 2.0
POLL_SECONDS = 0.02


class GitView(NamedTuple):
    """The committed marker's presence and the repository's actual index lock."""

    head_records_marker: bool
    index_lock: Path


class _Busy(Enum):
    GIT_BUSY = auto()


GIT_BUSY: Literal[_Busy.GIT_BUSY] = _Busy.GIT_BUSY
_View = GitView | None | Literal[_Busy.GIT_BUSY]


def git_view(store_dir: Path, *, timeout: float | None = None) -> _View:
    """Read the lock path and committed marker together; a timeout means Git is busy."""
    try:
        proc = subprocess.run(
            [
                "git",
                "-C",
                str(store_dir),
                "rev-parse",
                "--git-path",
                "index.lock",
                "--verify",
                "-q",
                "HEAD:./" + FORMAT_MARKER_NAME,
            ],
            env={**git_env(), "GIT_NO_LAZY_FETCH": "1"},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=GIT_TIMEOUT_SECONDS if timeout is None else timeout,
        )
    except subprocess.TimeoutExpired:
        return GIT_BUSY
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode not in (0, 1) or not proc.stdout.endswith(b"\n"):
        return None
    out = proc.stdout[:-1]
    if proc.returncode == 0:
        lock_b, sep, oid = out.rpartition(b"\n")
        if not sep or not oid:
            return None
    else:
        lock_b = out
    if not lock_b:
        return None
    lock = Path(os.fsdecode(lock_b))
    if not lock.is_absolute():
        lock = store_dir / lock
    return GitView(proc.returncode == 0, lock)


def _marker_size(marker: Path) -> int | None:
    try:
        return os.lstat(marker).st_size
    except (FileNotFoundError, NotADirectoryError):
        return None


def _present(size: int | None, held: bool) -> bool:
    return size is not None and (size > 0 or not held)


def await_marker(
    store_dir: Path,
    *,
    view: Callable[[Path], _View] | None = None,
    wait: float | None = None,
    poll: float | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    creating: bool = False,
) -> None:
    """Wait for Git's marker, or refuse before an ordinary open could backfill over it.

    Unavailable Git preserves the historical backfill. An unresolved timeout instead
    refuses, except when the caller explicitly declares creation of a new store.
    """
    probe = git_view if view is None else view
    wait = MARKER_WAIT_SECONDS if wait is None else wait
    poll = POLL_SECONDS if poll is None else poll
    deadline = clock() + wait
    current = probe(store_dir)
    known: GitView | None = None
    waited_on_lock = False
    marker = store_dir / FORMAT_MARKER_NAME
    while True:
        if current is None:
            return
        if current is GIT_BUSY:
            held = True
        else:
            known = current
            held = os.path.lexists(current.index_lock)
        # The lock is observed before the marker: Git writes before releasing its lock.
        if _present(_marker_size(marker), held):
            return
        if not held:
            assert current is not GIT_BUSY
            if waited_on_lock:
                refreshed = probe(store_dir)
                current = refreshed if refreshed is not None else current
                waited_on_lock = False
                continue
            if current.head_records_marker:
                quoted = f'"{store_dir}"' if os.name == "nt" else shlex.quote(str(store_dir))
                raise StoreRequirementError(
                    f"the store format marker {marker} is missing, but HEAD records it and "
                    "no git command is writing it; restore it with: "
                    f"git -C {quoted} checkout HEAD -- format "
                    "(to remove the marker on purpose, commit the removal first)",
                    kind="missing",
                )
            return
        if clock() >= deadline:
            if current is GIT_BUSY:
                if creating:
                    return
                raise StoreRequirementError(
                    "store format marker is missing and git did not answer in time "
                    f"({store_dir}); open the store again once git finishes",
                    kind="missing",
                    busy=True,
                )
            raise StoreRequirementError(
                f"the store format marker {marker} is missing while git holds "
                f"{current.index_lock} (a checkout or another git command is running); "
                "open the store again when it finishes, or delete that file if no git "
                "command is running",
                kind="missing",
                lock=current.index_lock,
            )
        if current is not GIT_BUSY:
            waited_on_lock = True
        sleep(poll)
        if current is GIT_BUSY:
            refreshed = probe(store_dir)
            current = refreshed if refreshed is not None else known
