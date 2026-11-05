"""The hook outcome log: one content-free line per run of the three delivering hooks.

see design/superpowers/specs/2026-11-05-hook-outcome-log.md
"""

from __future__ import annotations

import fnmatch
import io
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

import sidegraph.host.hook_log as hook_log
import sidegraph.host.hooks as hooks
import sidegraph.host.subagent as subagent
from sidegraph.hot_index import HotIndex
from sidegraph.store import _GITIGNORE_CONTENT, Store
from sidegraph.store_layout import FORMAT_MARKER_NAME, GITIGNORE_NAME
from sidegraph.verify import verify_snapshot
from tests.test_hook_contention import _hook_case, _hook_payload, held_lock  # noqa: F401

_LINE = re.compile(
    r"^\d{13} (PreToolUse|SessionStart|SubagentStart) (delivered|empty|lock|error) \d+$"
)


def _rows(store_dir: Path) -> list[hook_log.RunRow]:
    return hook_log.read_runs(store_dir)


def _store_dir(path: Path) -> Path:
    """A directory the log accepts: a store's format marker and the committed `.gitignore`."""
    path.mkdir(parents=True, exist_ok=True)
    (path / FORMAT_MARKER_NAME).write_text("1\n", encoding="utf-8")
    (path / GITIGNORE_NAME).write_text(_GITIGNORE_CONTENT, encoding="utf-8")
    return path


def _busy_error() -> sqlite3.OperationalError:
    error = sqlite3.OperationalError("database is locked")
    error.sqlite_errorcode = sqlite3.SQLITE_BUSY  # type: ignore[attr-defined]
    return error


def _entry(name: str):
    return {
        "pretool": hooks.pre_tool_use,
        "session": hooks.session_start,
        "subagent": subagent.subagent_start,
    }[name]


# ---- the file -----------------------------------------------------------------------------


def test_record_appends_one_content_free_line(tmp_path: Path) -> None:
    store = _store_dir(tmp_path / "store")
    hook_log.record(store, "PreToolUse", "delivered", 12)
    hook_log.record(store, "SessionStart", "lock", 530)

    text = (store / hook_log.LOG_NAME).read_text(encoding="ascii")
    lines = text.splitlines()

    assert text.startswith("\n") and not text.endswith("\n")  # each write is newline, then row
    assert lines[0] == "" and len(lines) == 3
    assert all(_LINE.match(line) for line in lines[1:])
    assert lines[1].endswith("PreToolUse delivered 12")


def test_a_fragment_another_run_left_does_not_swallow_the_next_row(tmp_path: Path) -> None:
    """A run that was cut short (a full disk, a crash mid-write) leaves a fragment with no newline.
    The next row starts on its own line because each write begins with the newline. Red against
    rows written as ``row + newline`` (the fragment and the row become one unparseable line)."""
    store = _store_dir(tmp_path / "store")
    hook_log.record(store, "PreToolUse", "empty", 1)
    with open(store / hook_log.LOG_NAME, "ab") as handle:
        handle.write(b"\n1730000000000 PreTo")  # another run's torn row

    hook_log.record(store, "SessionStart", "delivered", 2)

    assert [(r.hook, r.outcome) for r in _rows(store)] == [
        ("PreToolUse", "empty"),
        ("SessionStart", "delivered"),
    ]


def test_record_never_creates_the_store_directory_and_never_raises(tmp_path: Path) -> None:
    missing = tmp_path / "no-such-store"
    hook_log.record(missing, "PreToolUse", "empty", 1)
    assert not missing.exists()

    a_file = tmp_path / "a-file"
    a_file.write_text("x", encoding="utf-8")
    hook_log.record(a_file, "PreToolUse", "empty", 1)  # a directory that is a file: no raise


def test_record_writes_only_beside_a_store(tmp_path: Path) -> None:
    """A directory holding nothing but an unreadable ``index.db`` is not a store: the hook that
    found it unusable must not leave a file there. Red against a version that writes anywhere."""
    not_a_store = tmp_path / "project"
    not_a_store.mkdir()
    (not_a_store / "index.db").write_bytes(b"not a database")

    hook_log.record(not_a_store, "SubagentStart", "error", 1)

    assert sorted(p.name for p in not_a_store.iterdir()) == ["index.db"]


@pytest.mark.parametrize(
    "gitignore", [None, "", "index.db\n", "*.tmp\n", "# index.db*\n"], ids=lambda g: repr(g)
)
def test_record_writes_nothing_where_the_store_does_not_ignore_it(
    tmp_path: Path, gitignore: str | None
) -> None:
    """The hooks never rewrite a committed file, so where the store's `.gitignore` is missing or
    was edited down, the log would show in `git status` and in `git add -A`: nothing is written.
    Red against a version that checks only the format marker."""
    store = _store_dir(tmp_path / "store")
    if gitignore is None:
        (store / GITIGNORE_NAME).unlink()
    else:
        (store / GITIGNORE_NAME).write_text(gitignore, encoding="utf-8")

    hook_log.record(store, "PreToolUse", "delivered", 1)

    assert not (store / hook_log.LOG_NAME).exists()


@pytest.mark.parametrize(
    "gitignore",
    [
        "index.db*\n!index.db-hooks.log\n",  # a negation re-includes the log
        "index.db*\n!*.log*\n",
        "  index.db*\n",  # git keeps leading whitespace: this ignores nothing
        "\\ index.db*\n",
        "x" * 65526 + "\nindex.db*xyz\n",  # the read limit falls right after `index.db*`
    ],
    ids=["negate-log", "negate-glob", "leading-space", "escaped-space", "cut-at-limit"],
)
def test_record_writes_nothing_where_git_would_not_ignore_the_log(
    tmp_path: Path, gitignore: str
) -> None:
    """Each content is checked against git itself below: git would show the log, so the hook must
    not write it. Red against a check that strips the line or looks only for a matching line."""
    repo = tmp_path / "repo"
    store = _store_dir(repo / ".sidegraph")
    (store / GITIGNORE_NAME).write_text(gitignore, encoding="utf-8")

    hook_log.record(store, "PreToolUse", "delivered", 1)

    assert not (store / hook_log.LOG_NAME).exists()
    (store / hook_log.LOG_NAME).write_text("\n1 PreToolUse empty 1", encoding="ascii")
    git = ["git", "-c", "core.hooksPath=/dev/null", "-C", str(repo)]
    subprocess.run([*git, "init", "-q"], check=True)
    ignored = subprocess.run([*git, "check-ignore", "-q", f".sidegraph/{hook_log.LOG_NAME}"])
    assert ignored.returncode == 1  # the premise: git would not ignore it


@pytest.mark.parametrize(
    "gitignore", ["index.db*\r\n*.tmp\r\n", "index.db*   \n", "*.tmp\nindex.db*"], ids=repr
)
def test_record_writes_where_git_ignores_the_log(tmp_path: Path, gitignore: str) -> None:
    store = _store_dir(tmp_path / "store")
    (store / GITIGNORE_NAME).write_text(gitignore, encoding="utf-8", newline="")

    hook_log.record(store, "PreToolUse", "delivered", 1)

    assert [r.outcome for r in _rows(store)] == ["delivered"]


@pytest.mark.skipif(sys.platform == "win32", reason="symlink creation needs a privilege")
def test_a_symlinked_gitignore_gets_no_log(tmp_path: Path) -> None:
    """Git refuses to read a `.gitignore` that is a symlink, so the log would show in
    `git status`. Red against a check that follows the link."""
    store = _store_dir(tmp_path / "store")
    real = tmp_path / "real-gitignore"
    real.write_text(_GITIGNORE_CONTENT, encoding="utf-8")
    (store / GITIGNORE_NAME).unlink()
    (store / GITIGNORE_NAME).symlink_to(real)

    hook_log.record(store, "PreToolUse", "delivered", 1)

    assert not (store / hook_log.LOG_NAME).exists()


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no named pipes here")
def test_a_named_pipe_at_the_gitignore_cannot_stop_a_hook(tmp_path: Path) -> None:
    """Reading the ignore file must not wait either. Red against a blocking open (the run below
    is killed by the timeout)."""
    store = _store_dir(tmp_path / "store")
    (store / GITIGNORE_NAME).unlink()
    os.mkfifo(store / GITIGNORE_NAME)
    code = (
        "import sys, sidegraph.host.hook_log as h; "
        "h.record(sys.argv[1], 'PreToolUse', 'delivered', 1); print('done')"
    )

    result = subprocess.run(
        [sys.executable, "-B", "-c", code, str(store)], capture_output=True, text=True, timeout=20
    )

    assert result.stdout.strip() == "done"
    assert not (store / hook_log.LOG_NAME).exists()


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no named pipes here")
def test_a_named_pipe_at_the_gitignore_is_not_read_even_with_a_writer(tmp_path: Path) -> None:
    """A pipe whose other end feeds it the ignore line is still not the committed file git reads.
    Red against a check that reads whatever it opened."""
    store = _store_dir(tmp_path / "store")
    pipe = store / GITIGNORE_NAME
    pipe.unlink()
    os.mkfifo(pipe)
    reader = os.open(pipe, os.O_RDONLY | os.O_NONBLOCK)  # lets the writer open
    writer = os.open(pipe, os.O_WRONLY | os.O_NONBLOCK)
    try:
        os.write(writer, _GITIGNORE_CONTENT.encode())
        hook_log.record(store, "PreToolUse", "delivered", 1)
    finally:
        os.close(writer)
        os.close(reader)

    assert not (store / hook_log.LOG_NAME).exists()


def test_a_hard_link_at_the_log_path_is_not_written_through(tmp_path: Path) -> None:
    """``O_NOFOLLOW`` does not stop a hard link; the line would land in the other file. Red against
    a version that checks only the file type."""
    store = _store_dir(tmp_path / "store")
    other = tmp_path / "other-file"
    other.write_text("keep\n", encoding="utf-8")
    os.link(other, store / hook_log.LOG_NAME)

    hook_log.record(store, "PreToolUse", "delivered", 1)

    assert other.read_text(encoding="utf-8") == "keep\n"


def test_an_extended_busy_code_is_a_lock() -> None:
    """``sqlite_errorcode`` carries the extended code: BUSY_SNAPSHOT is 517, LOCKED_SHAREDCACHE
    262. Red against a check that compares the whole code to the primary one."""
    for code in (517, 262, sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
        error = sqlite3.OperationalError("locked")
        error.sqlite_errorcode = code  # type: ignore[attr-defined]
        assert hook_log.is_lock_error(error), code
    readonly = sqlite3.OperationalError("readonly")
    readonly.sqlite_errorcode = 8  # type: ignore[attr-defined]
    assert not hook_log.is_lock_error(readonly)


def test_the_required_ignore_line_is_the_one_the_store_writes() -> None:
    """Couples ``IGNORE_PATTERN`` to ``Store``'s `.gitignore` content: if the store stops writing
    the line, every log would silently stop."""
    assert hook_log.IGNORE_PATTERN.decode() in _GITIGNORE_CONTENT.split()
    assert fnmatch.fnmatch(hook_log.LOG_NAME, hook_log.IGNORE_PATTERN.decode())
    assert fnmatch.fnmatch(hook_log.ROTATED_NAME, hook_log.IGNORE_PATTERN.decode())


def test_record_writes_nothing_when_telemetry_is_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SIDEGRAPH_TELEMETRY", "off")
    store = _store_dir(tmp_path / "store")
    hook_log.record(store, "PreToolUse", "delivered", 1)
    assert not (store / hook_log.LOG_NAME).exists()


def test_record_rotates_once_the_file_passes_the_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store_dir(tmp_path / "store")
    monkeypatch.setattr(hook_log, "MAX_BYTES", 100)  # a line is about 34 bytes: the 4th passes it
    for _ in range(5):
        hook_log.record(store, "PreToolUse", "empty", 1)

    rotated = (store / hook_log.ROTATED_NAME).read_text(encoding="ascii").split()
    current = (store / hook_log.LOG_NAME).read_text(encoding="ascii").split()
    assert (len(rotated), len(current)) == (4 * 4, 1 * 4)  # four fields a row
    assert len(_rows(store)) == 5  # the history survives the rotation, oldest first


def test_the_rotation_without_flock_renames_a_closed_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows has no ``flock`` and refuses to rename a file another handle holds open, so there
    the log is closed before the rename. Here the rename is made to fail while any descriptor of
    the log is open, the way Windows does. Red against a rotation made with the descriptor open
    (it falls back to truncation and keeps no history)."""
    store = _store_dir(tmp_path / "store")
    monkeypatch.setattr(hook_log, "MAX_BYTES", 100)
    monkeypatch.setattr(hook_log, "fcntl", None)
    open_fds: set[int] = set()
    real_open, real_close, real_replace = os.open, os.close, os.replace

    def tracking_open(path, flags, *args, **kwargs):  # type: ignore[no-untyped-def]
        fd = real_open(path, flags, *args, **kwargs)
        if str(path).endswith(hook_log.LOG_NAME):
            open_fds.add(fd)
        return fd

    def tracking_close(fd: int) -> None:
        open_fds.discard(fd)
        real_close(fd)

    def windows_replace(src, dst):  # type: ignore[no-untyped-def]
        if open_fds and str(src).endswith(hook_log.LOG_NAME):
            raise PermissionError(32, "The process cannot access the file")
        real_replace(src, dst)

    monkeypatch.setattr(os, "open", tracking_open)
    monkeypatch.setattr(os, "close", tracking_close)
    monkeypatch.setattr(os, "replace", windows_replace)
    for _ in range(5):
        hook_log.record(store, "PreToolUse", "empty", 1)

    assert (store / hook_log.ROTATED_NAME).exists()
    assert len(_rows(store)) == 5


def test_a_paused_run_without_flock_leaves_a_fresh_log_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without ``flock`` the size is read again just before the rename: a run that passed the
    limit, paused while another rotated, finds a small new log and does not replace the retained
    history with it. Red against a rename that trusts the size it saw before closing."""
    store = _store_dir(tmp_path / "store")
    monkeypatch.setattr(hook_log, "MAX_BYTES", 100)
    path, rotated = store / hook_log.LOG_NAME, store / hook_log.ROTATED_NAME
    rotated.write_text("\n1 PreToolUse empty 1" * 5, encoding="ascii")  # history
    path.write_text("\n2 PreToolUse empty 2", encoding="ascii")  # the new, small log

    hook_log._rotate_closed(str(path), str(rotated))

    assert rotated.read_text(encoding="ascii").count("\n") == 5
    assert path.read_text(encoding="ascii") == "\n2 PreToolUse empty 2"


@pytest.mark.skipif(sys.platform == "win32", reason="flock is POSIX")
def test_a_filesystem_without_flock_still_rotates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``flock`` failing with ENOLCK (some network filesystems) is not another run rotating. Red
    against a rotation that gives up on any ``flock`` error (the log then never rotates)."""
    import errno
    import fcntl

    def no_locks(fd: int, op: int) -> None:
        raise OSError(errno.ENOLCK, "No locks available")

    monkeypatch.setattr(fcntl, "flock", no_locks)
    store = _store_dir(tmp_path / "store")
    monkeypatch.setattr(hook_log, "MAX_BYTES", 100)
    for _ in range(5):
        hook_log.record(store, "PreToolUse", "empty", 1)

    assert (store / hook_log.ROTATED_NAME).exists()


@pytest.mark.skipif(sys.platform == "win32", reason="inode identity and flock are POSIX")
def test_a_run_that_was_paused_does_not_rotate_a_log_another_run_already_rotated(
    tmp_path: Path,
) -> None:
    """Run A holds the old file open and is paused before it rotates; run B rotates and a new log
    starts. A must see that the path now names a different file and leave the retained history
    alone. Red against a rotation that replaces whatever the path names."""
    store = _store_dir(tmp_path / "store")
    path = str(store / hook_log.LOG_NAME)
    rotated = str(store / hook_log.ROTATED_NAME)
    Path(path).write_text("1 PreToolUse empty 1\n" * 3, encoding="ascii")
    paused = os.open(path, os.O_WRONLY | os.O_APPEND)
    try:
        os.replace(path, rotated)  # run B rotates
        Path(path).write_text("2 PreToolUse empty 2\n", encoding="ascii")  # a new log

        hook_log._rotate(path, rotated, paused)  # run A wakes up
    finally:
        os.close(paused)

    assert Path(rotated).read_text(encoding="ascii").count("\n") == 3  # history kept
    assert Path(path).read_text(encoding="ascii") == "2 PreToolUse empty 2\n"


@pytest.mark.skipif(sys.platform == "win32", reason="flock is POSIX")
def test_only_one_run_rotates_at_a_time(tmp_path: Path) -> None:
    store = _store_dir(tmp_path / "store")
    path = str(store / hook_log.LOG_NAME)
    rotated = str(store / hook_log.ROTATED_NAME)
    Path(path).write_text("1 PreToolUse empty 1\n", encoding="ascii")
    first = os.open(path, os.O_WRONLY | os.O_APPEND)
    second = os.open(path, os.O_WRONLY | os.O_APPEND)
    try:
        import fcntl

        fcntl.flock(first, fcntl.LOCK_EX | fcntl.LOCK_NB)  # another run is rotating
        hook_log._rotate(path, rotated, second)
        assert not os.path.exists(rotated)  # the second run left it to the first
    finally:
        os.close(first)
        os.close(second)


def test_the_log_stays_bounded_when_the_rotated_name_cannot_be_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A directory at the rotated name makes the replace fail every time; the log is truncated
    once it is twice the limit instead of growing without bound. Red against a rotation that only
    replaces."""
    store = _store_dir(tmp_path / "store")
    (store / hook_log.ROTATED_NAME).mkdir()
    monkeypatch.setattr(hook_log, "MAX_BYTES", 100)

    sizes = []
    for _ in range(20):
        hook_log.record(store, "PreToolUse", "empty", 1)
        sizes.append((store / hook_log.LOG_NAME).stat().st_size)

    assert max(sizes) <= 2 * 100 + 40
    assert 0 in sizes  # it was truncated at least once


def test_a_log_just_past_the_limit_is_not_truncated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the rename fails a run truncates only a log twice the limit, so a run that was paused
    does not wipe the few rows written since another run truncated. Red against truncating as soon
    as the limit is passed."""
    store = _store_dir(tmp_path / "store")
    (store / hook_log.ROTATED_NAME).mkdir()
    monkeypatch.setattr(hook_log, "MAX_BYTES", 100)
    for _ in range(4):  # about 140 bytes: past the limit, under twice it
        hook_log.record(store, "PreToolUse", "empty", 1)

    assert len(_rows(store)) == 4


@pytest.mark.skipif(sys.platform == "win32", reason="symlink creation needs a privilege")
def test_record_does_not_follow_a_planted_symlink(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere"
    target.write_text("keep\n", encoding="utf-8")
    store = _store_dir(tmp_path / "store")
    (store / hook_log.LOG_NAME).symlink_to(target)

    hook_log.record(store, "PreToolUse", "delivered", 1)

    assert target.read_text(encoding="utf-8") == "keep\n"


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no named pipes here")
def test_a_named_pipe_at_the_path_cannot_stop_a_hook(tmp_path: Path) -> None:
    """``open`` on a FIFO with no reader blocks forever unless it is non-blocking; the hook has
    printed its answer but the process would never exit. Red against a blocking open (the run
    below is killed by the timeout)."""
    store = _store_dir(tmp_path / "store")
    os.mkfifo(store / hook_log.LOG_NAME)
    code = (
        "import sys, sidegraph.host.hook_log as h; "
        "h.record(sys.argv[1], 'PreToolUse', 'delivered', 1); print('done')"
    )

    result = subprocess.run(
        [sys.executable, "-B", "-c", code, str(store)],
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert result.stdout.strip() == "done"


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no named pipes here")
def test_nothing_is_written_to_a_file_that_is_not_a_regular_file(tmp_path: Path) -> None:
    """With a reader on the other end a named pipe opens without blocking; the line must still not
    be pushed into it. Red against a version that writes to whatever it opened."""
    store = _store_dir(tmp_path / "store")
    pipe = store / hook_log.LOG_NAME
    os.mkfifo(pipe)
    reader = os.open(pipe, os.O_RDONLY | os.O_NONBLOCK)
    try:
        hook_log.record(store, "PreToolUse", "delivered", 1)
        assert os.read(reader, 100) == b""  # nothing was written (the writer end closed: EOF)
    finally:
        os.close(reader)


def test_a_directory_at_the_log_path_is_skipped(tmp_path: Path) -> None:
    store = _store_dir(tmp_path / "store")
    (store / hook_log.LOG_NAME).mkdir()

    hook_log.record(store, "PreToolUse", "delivered", 1)  # no raise

    assert (store / hook_log.LOG_NAME).is_dir()


def test_a_short_write_does_not_poison_the_next_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``os.write`` can return fewer bytes than asked (a full disk); the next run's row must stay
    whole without any repair write. Red against rows written as ``row + newline``."""
    store = _store_dir(tmp_path / "store")
    real_write = os.write
    calls = {"n": 0}

    def short_once(fd: int, data: bytes) -> int:
        calls["n"] += 1
        if calls["n"] == 1:
            return real_write(fd, data[:5])
        return real_write(fd, data)

    monkeypatch.setattr(os, "write", short_once)
    hook_log.record(store, "PreToolUse", "empty", 1)
    hook_log.record(store, "SessionStart", "delivered", 2)

    assert [(r.hook, r.outcome) for r in _rows(store)] == [("SessionStart", "delivered")]


def test_the_log_is_never_committed(tmp_path: Path) -> None:
    """The name starts with ``index.db``, which every store's committed `.gitignore` already
    ignores, so no committed file has to change. Red against a renamed file."""
    repo = tmp_path / "repo"
    store = _store_dir(repo / ".sidegraph")
    hook_log.record(store, "PreToolUse", "delivered", 1)
    (store / hook_log.ROTATED_NAME).write_text("1 PreToolUse empty 1\n", encoding="utf-8")
    git = ["git", "-c", "core.hooksPath=/dev/null", "-C", str(repo)]
    subprocess.run([*git, "init", "-q"], check=True)

    ignored = subprocess.run([*git, "check-ignore", "-q", f".sidegraph/{hook_log.LOG_NAME}"])
    status = subprocess.run(
        [*git, "status", "--porcelain", "--untracked-files=all"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout

    assert ignored.returncode == 0
    assert "index.db-hooks" not in status


def test_a_store_holding_the_log_is_checked_and_opened_unchanged(tmp_path: Path) -> None:
    store_dir = tmp_path / ".sidegraph"
    Store(store_dir).close()
    before = verify_snapshot(store_dir)
    hook_log.record(store_dir, "PreToolUse", "delivered", 3)
    hook_log.record(store_dir, "SessionStart", "empty", 3)
    kept = (store_dir / hook_log.LOG_NAME).read_bytes()

    after = verify_snapshot(store_dir)
    Store(store_dir).close()

    assert after == before == []
    assert (store_dir / hook_log.LOG_NAME).read_bytes() == kept


# ---- reading ------------------------------------------------------------------------------


def test_read_runs_drops_torn_and_foreign_lines_and_summarize_counts(tmp_path: Path) -> None:
    (tmp_path / hook_log.ROTATED_NAME).write_text(
        "1000 PreToolUse delivered 10\n1001 PreToolUse lock 600\n", encoding="ascii"
    )
    (tmp_path / hook_log.LOG_NAME).write_text(
        "1002 PreToolUse empty 20\n"
        "not a line\n"
        "1003 PreToolUse bogus 5\n"  # unknown outcome
        "x PreToolUse empty 5\n"  # not a number
        "-7 PreToolUse empty 5\n"  # a sign is not plain digits
        "1_000 PreToolUse empty 5\n"  # nor is an underscore
        "1006  empty 5\n"  # an empty hook name
        f"{'9' * 40} PreToolUse empty 5\n"  # absurd magnitude
        "1004 SessionStart delivered 40\n"
        "1005 PreToolUse deliv",  # torn last line
        encoding="ascii",
    )

    rows = _rows(tmp_path)
    summary = hook_log.summarize(rows)

    assert [r.at_ms for r in rows] == [1000, 1001, 1002, 1004]  # rotated file first
    assert summary["PreToolUse"] == {
        "runs": 3,
        "delivered": 1,
        "empty": 1,
        "lock": 1,
        "error": 0,
        "p50_ms": 20,
        "p95_ms": 600,
    }
    assert summary["SessionStart"]["runs"] == 1
    assert hook_log.read_runs(tmp_path / "missing") == []


def test_summarize_percentiles_are_not_the_extremes() -> None:
    rows = [hook_log.RunRow(i, "PreToolUse", "empty", i) for i in range(1, 101)]  # 1..100 ms

    summary = hook_log.summarize(rows)["PreToolUse"]

    assert (summary["p50_ms"], summary["p95_ms"]) == (50, 95)


def test_read_runs_reads_only_the_tail_of_an_oversized_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tail starts two bytes into row 36, so its first line is "00036 PreToolUse empty 36":
    it parses, but it is not a row. Red against a reader that keeps the tail's first line."""
    monkeypatch.setattr(hook_log, "MAX_BYTES", 53)  # the tail read is 106 bytes
    rows_text = "".join(f"\n{100000 + i} PreToolUse empty {i:02d}" for i in range(40))
    assert len(rows_text) == 40 * 27 and (len(rows_text) - 106) % 27 == 2
    (tmp_path / hook_log.LOG_NAME).write_text(rows_text, encoding="ascii")

    assert [r.at_ms for r in _rows(tmp_path)] == [100037, 100038, 100039]


def test_read_runs_keeps_a_whole_first_row_of_the_tail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tail starts on the first byte of row 37, just after its newline: the row is whole and
    kept. Red against a reader that always drops the tail's first line."""
    monkeypatch.setattr(hook_log, "MAX_BYTES", 40)  # the tail read is 80 bytes
    rows_text = "".join(f"\n{100000 + i} PreToolUse empty {i:02d}" for i in range(40))
    assert (len(rows_text) - 80) % 27 == 1
    (tmp_path / hook_log.LOG_NAME).write_text(rows_text, encoding="ascii")

    assert [r.at_ms for r in _rows(tmp_path)] == [100037, 100038, 100039]


def test_percentile_is_nearest_rank_for_every_sample_size() -> None:
    """Against an independent definition: the smallest value with at least ``percent`` % of the
    sample at or below it."""
    for n in range(1, 301):
        values = list(range(1, n + 1))
        for percent in (50, 95):
            expected = next(v for v in values if v * 100 >= percent * n)
            assert hook_log._percentile(values, percent) == expected, (n, percent)


def test_a_run_reports_its_outcome_by_precedence(tmp_path: Path) -> None:
    run = hook_log.Run("PreToolUse")
    assert run.outcome(delivered=False) == "empty"
    assert run.outcome(delivered=True) == "delivered"
    run.note(RuntimeError("boom"))
    assert not run.lock_seen
    assert run.outcome(delivered=False) == "error"
    assert run.outcome(delivered=True) == "delivered"  # an error in a step that did not matter
    run.note(_busy_error())
    assert run.lock_seen
    assert run.outcome(delivered=True) == "lock"  # a refusal in a delivery step wins
    assert run.outcome(delivered=False) == "lock"

    store = _store_dir(tmp_path / "store")
    run.store_dir = None
    run.finish(False)  # no store resolved: writes nothing, raises nothing
    assert not (store / hook_log.LOG_NAME).exists()


# ---- the three hooks ----------------------------------------------------------------------


def _run_hook(
    entry: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], payload: dict
) -> dict:
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    _entry(entry)()
    return json.loads(capsys.readouterr().out)


def _case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path]:
    store_dir, source, transcript, _ = _hook_case(
        tmp_path / "project", monkeypatch, cold=False, telemetry="on"
    )
    return store_dir, source, transcript


def test_pre_tool_use_logs_delivered_and_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store_dir, source, transcript = _case(tmp_path, monkeypatch)
    payload = _hook_payload("pretool", source, transcript)

    delivered = _run_hook("pretool", monkeypatch, capsys, payload)
    other = source.with_name("other.py")
    other.write_text("x = 1\n", encoding="utf-8")
    empty = _run_hook(
        "pretool",
        monkeypatch,
        capsys,
        {**payload, "tool_input": {"file_path": str(other)}, "session_id": "read-2"},
    )

    assert "A useful gotcha" in json.dumps(delivered) and empty == {}
    assert [(r.hook, r.outcome) for r in _rows(store_dir)] == [
        ("PreToolUse", "delivered"),
        ("PreToolUse", "empty"),
    ]


def test_pre_tool_use_logs_lock_when_the_index_is_held(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    held_lock,  # noqa: F811
) -> None:
    store_dir, source, transcript = _case(tmp_path, monkeypatch)
    held_lock(store_dir, "BEGIN EXCLUSIVE")

    output = _run_hook("pretool", monkeypatch, capsys, _hook_payload("pretool", source, transcript))

    assert output == {}
    assert [r.outcome for r in _rows(store_dir)] == ["lock"]


def test_pre_tool_use_logs_lock_when_only_the_claim_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    held_lock,  # noqa: F811
) -> None:
    """A writer holding the reservation lets the hook open and read the index but refuses its
    claim. Red against a version that notes only the open."""
    store_dir, source, transcript = _case(tmp_path, monkeypatch)
    held_lock(store_dir, "BEGIN IMMEDIATE")

    output = _run_hook("pretool", monkeypatch, capsys, _hook_payload("pretool", source, transcript))

    assert output == {}
    assert [r.outcome for r in _rows(store_dir)] == ["lock"]


def test_a_refused_touch_after_a_delivery_leaves_the_run_delivered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The touch row is bookkeeping. Red against an outcome taken from the handle's
    ``write_failed``, which the refused touch sets."""
    store_dir, source, transcript = _case(tmp_path, monkeypatch)

    def refused(self: HotIndex, *args: object, **kwargs: object) -> None:
        self.write_failed = True
        raise _busy_error()

    monkeypatch.setattr(HotIndex, "record_touch", refused)

    output = _run_hook("pretool", monkeypatch, capsys, _hook_payload("pretool", source, transcript))

    assert "A useful gotcha" in json.dumps(output)
    assert [r.outcome for r in _rows(store_dir)] == ["delivered"]


def test_a_lock_met_only_by_the_touch_row_is_not_a_withheld_delivery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    held_lock,  # noqa: F811
) -> None:
    """With delivery switched off the touch row is the first caller to open the index. A lock it
    meets is bookkeeping: no ``lock`` line. Red against a version that notes every open."""
    store_dir, source, transcript = _case(tmp_path, monkeypatch)
    monkeypatch.setenv("SIDEGRAPH_GREP_NUDGE", "off")
    held_lock(store_dir, "BEGIN EXCLUSIVE")

    output = _run_hook("pretool", monkeypatch, capsys, _hook_payload("pretool", source, transcript))

    assert output == {}
    assert _rows(store_dir) == []


def test_a_run_whose_delivery_never_opened_the_index_is_not_counted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """With delivery off a locked run writes nothing (above), so an unlocked one must not write
    ``empty`` either, or the runs that did not contend would inflate the count. Red against a
    version that binds the store on the touch row's open."""
    store_dir, source, transcript = _case(tmp_path, monkeypatch)
    monkeypatch.setenv("SIDEGRAPH_GREP_NUDGE", "off")

    output = _run_hook("pretool", monkeypatch, capsys, _hook_payload("pretool", source, transcript))

    assert output == {}
    assert _rows(store_dir) == []


@pytest.mark.parametrize("entry", ["pretool", "subagent"])
def test_an_open_that_reports_an_error_but_returns_a_handle_withheld_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    entry: str,
) -> None:
    """The journal pragma is refused when another connection holds the file in WAL: the open
    reports a busy error to its callback and still returns a working handle. Red against a version
    that notes every error the open reports."""
    store_dir, source, transcript = _case(tmp_path, monkeypatch)
    real_open = HotIndex.open.__func__  # type: ignore[attr-defined]

    def open_with_pragma_refused(cls, store_path, *, on_error=None, **kwargs):
        if on_error is not None:
            on_error(_busy_error())
        return real_open(cls, store_path, on_error=on_error, **kwargs)

    monkeypatch.setattr(HotIndex, "open", classmethod(open_with_pragma_refused))

    output = _run_hook(entry, monkeypatch, capsys, _hook_payload(entry, source, transcript))

    assert output != {}
    assert [r.outcome for r in _rows(store_dir)] == ["delivered"]


@pytest.mark.parametrize("entry", ["pretool", "subagent"])
def test_an_index_that_cannot_be_opened_for_a_reason_other_than_a_lock_is_left_as_it_was(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    entry: str,
) -> None:
    """A corrupt index is not contention data, and a hook that cannot use an index writes nothing
    beside it (the existing refusal tests promise the same for a missing or newer-format one). The
    hook reports it on stderr; the run log stays silent. Red against a version that binds the
    store for any open failure."""
    store_dir, source, transcript = _case(tmp_path, monkeypatch)
    (store_dir / "index.db").write_bytes(b"this is not a database" * 100)

    output = _run_hook(entry, monkeypatch, capsys, _hook_payload(entry, source, transcript))

    assert output == {}
    assert not (store_dir / hook_log.LOG_NAME).exists()


def test_pre_tool_use_logs_the_outcome_of_failures_after_the_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Four places the run learns of a failure after it holds a handle: the file query raising
    (the handler around the delivery), record selection on the Read path, record selection on the
    Agent path, and a non-lock error. Each is red against a version that drops that note."""
    store_dir, source, transcript = _case(tmp_path, monkeypatch)
    payload = _hook_payload("pretool", source, transcript)

    def agent_payload(session: str) -> dict:
        return {
            "session_id": session,
            "tool_name": "Agent",
            "tool_input": {"prompt": f"Fix {source}."},
            "cwd": str(source.parent.parent),
        }

    def raising(error: BaseException):
        def query(self: HotIndex, *args: object, **kwargs: object) -> None:
            raise error

        return query

    with monkeypatch.context() as patch:  # the query raises a lock error
        patch.setattr(HotIndex, "anchored_entities", raising(_busy_error()))
        _run_hook("pretool", monkeypatch, capsys, {**payload, "session_id": "a"})
    with monkeypatch.context() as patch:  # the query raises something else
        patch.setattr(HotIndex, "anchored_entities", raising(ValueError("x")))
        _run_hook("pretool", monkeypatch, capsys, {**payload, "session_id": "b"})

    def records_refused(self: HotIndex, ids, *, on_error=None):
        if on_error is not None:
            on_error(_busy_error())
        return []

    with monkeypatch.context() as patch:  # selection on the Read path
        patch.setattr(HotIndex, "records_for", records_refused)
        _run_hook("pretool", monkeypatch, capsys, {**payload, "session_id": "c"})
    with monkeypatch.context() as patch:  # selection on the Agent path
        patch.setattr(HotIndex, "records_for", records_refused)
        _run_hook("pretool", monkeypatch, capsys, agent_payload("d"))

    assert [r.outcome for r in _rows(store_dir)] == ["lock", "error", "lock", "lock"]


def test_pre_tool_use_without_a_store_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "bare"
    root.mkdir()
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(root))
    monkeypatch.setenv("SIDEGRAPH_DIR", str(root / ".sidegraph"))
    monkeypatch.chdir(root)

    output = _run_hook(
        "pretool",
        monkeypatch,
        capsys,
        {"session_id": "s", "tool_name": "Read", "tool_input": {"file_path": str(root / "a.py")}},
    )

    assert output == {}
    assert not (root / ".sidegraph").exists()


def test_session_start_logs_delivered_duplicate_and_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    held_lock,  # noqa: F811
) -> None:
    store_dir, source, transcript = _case(tmp_path, monkeypatch)
    payload = _hook_payload("session", source, transcript)

    first = _run_hook("session", monkeypatch, capsys, payload)
    second = _run_hook("session", monkeypatch, capsys, payload)  # the same session again
    held_lock(store_dir, "BEGIN EXCLUSIVE")
    third = _run_hook("session", monkeypatch, capsys, {**payload, "session_id": "session-2"})

    assert first != {} and second == {} and third == {}
    assert [(r.hook, r.outcome) for r in _rows(store_dir)] == [
        ("SessionStart", "delivered"),
        ("SessionStart", "empty"),
        ("SessionStart", "lock"),
    ]


def test_session_start_logs_error_for_a_store_that_cannot_open_and_for_a_failed_render(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Two failures that are not a lock: a store that refuses to open (the hook prints its own
    message and returns) and an exception after the open (the outer handler). Each is red against
    a version that notes only a lock."""
    store_dir, source, transcript = _case(tmp_path, monkeypatch)
    payload = _hook_payload("session", source, transcript)

    def refuses(*args: object, **kwargs: object) -> None:
        raise RuntimeError("cannot open")

    with monkeypatch.context() as patch:
        patch.setattr("sidegraph.store.Store", refuses)
        _run_hook("session", monkeypatch, capsys, payload)
    with monkeypatch.context() as patch:
        patch.setattr(hooks, "_fit_session_output", refuses)
        _run_hook("session", monkeypatch, capsys, {**payload, "session_id": "session-2"})

    assert [(r.hook, r.outcome) for r in _rows(store_dir)] == [
        ("SessionStart", "error"),
        ("SessionStart", "error"),
    ]


def test_session_start_leaves_a_store_that_needs_a_newer_sidegraph_as_it_was(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The store refuses this release: SessionStart says so and writes nothing there, like the two
    hooks that open the index through ``HotIndex``. Red against a version that logs the refusal."""
    from tests.test_store_requirement import above_running, write_requirement_marker

    store_dir, source, transcript = _case(tmp_path, monkeypatch)
    write_requirement_marker(store_dir, above_running())
    before = sorted(p.name for p in store_dir.iterdir())

    output = _run_hook("session", monkeypatch, capsys, _hook_payload("session", source, transcript))

    assert "systemMessage" in output
    assert sorted(p.name for p in store_dir.iterdir()) == before
    assert not (store_dir / hook_log.LOG_NAME).exists()


def test_subagent_start_logs_delivered_and_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    held_lock,  # noqa: F811
) -> None:
    store_dir, source, transcript = _case(tmp_path, monkeypatch)
    payload = _hook_payload("subagent", source, transcript)

    delivered = _run_hook("subagent", monkeypatch, capsys, payload)
    held_lock(store_dir, "BEGIN EXCLUSIVE")
    refused = _run_hook("subagent", monkeypatch, capsys, payload)

    assert delivered != {} and refused == {}
    assert [(r.hook, r.outcome) for r in _rows(store_dir)] == [
        ("SubagentStart", "delivered"),
        ("SubagentStart", "lock"),
    ]


def test_subagent_start_logs_a_failure_after_the_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The count query raising a lock error is ``lock``, anything else ``error``. Red against a
    version that drops the note in the entry's outer handler."""
    store_dir, source, transcript = _case(tmp_path, monkeypatch)
    payload = _hook_payload("subagent", source, transcript)

    def raising(error: BaseException):
        def query(self: HotIndex) -> None:
            raise error

        return query

    with monkeypatch.context() as patch:
        patch.setattr(HotIndex, "memory_counts", raising(_busy_error()))
        _run_hook("subagent", monkeypatch, capsys, payload)
    with monkeypatch.context() as patch:
        patch.setattr(HotIndex, "memory_counts", raising(ValueError("x")))
        _run_hook("subagent", monkeypatch, capsys, payload)

    assert [r.outcome for r in _rows(store_dir)] == ["lock", "error"]


def test_subagent_start_with_nothing_to_ask_about_logs_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store_dir, source, transcript = _case(tmp_path, monkeypatch)
    monkeypatch.setattr(HotIndex, "memory_counts", lambda self: (0, 0, 0))

    payload = _hook_payload("subagent", source, transcript)
    output = _run_hook("subagent", monkeypatch, capsys, payload)

    assert output == {}
    assert [r.outcome for r in _rows(store_dir)] == ["empty"]


def test_subagent_start_switched_off_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store_dir, source, transcript = _case(tmp_path, monkeypatch)
    monkeypatch.setenv("SIDEGRAPH_SUBAGENT_BRIEF", "off")

    payload = _hook_payload("subagent", source, transcript)
    output = _run_hook("subagent", monkeypatch, capsys, payload)

    assert output == {}
    assert not (store_dir / hook_log.LOG_NAME).exists()


class _EventStdout(io.StringIO):
    """A stdout that records its writes and flushes in order, to see them against the log write."""

    def __init__(self, events: list[str]) -> None:
        super().__init__()
        self._events = events

    def write(self, text: str) -> int:
        self._events.append("write")
        return super().write(text)

    def flush(self) -> None:
        self._events.append("flush")
        super().flush()


@pytest.mark.parametrize("entry", ["pretool", "session", "subagent"])
def test_the_answer_is_flushed_before_the_line_is_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    """A hook that blocks while it logs must already have handed its answer to the host. Red
    against a version that writes the line before flushing stdout."""
    _, source, transcript = _case(tmp_path, monkeypatch)
    events: list[str] = []
    payload = _hook_payload(entry, source, transcript)
    monkeypatch.setattr(sys, "stdout", _EventStdout(events))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    real_finish = hook_log.Run.finish

    def finish(self: hook_log.Run, delivered: bool) -> None:
        events.append("log")
        real_finish(self, delivered)

    monkeypatch.setattr(hook_log.Run, "finish", finish)
    _entry(entry)()

    assert "log" in events
    assert events.index("write") < events.index("flush") < events.index("log")


@pytest.mark.parametrize("entry", ["pretool", "session", "subagent"])
def test_the_elapsed_time_includes_the_entry_points_own_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], entry: str
) -> None:
    """Project anchoring runs before the hook's body; the run starts before it. Red against a run
    started after the argument handling (the logged time was 2 ms for a 200 ms anchoring)."""
    store_dir, source, transcript = _case(tmp_path, monkeypatch)

    def slow_anchor() -> None:
        time.sleep(0.2)

    monkeypatch.setattr(hooks, "take_repo_root_flag", lambda argv: True)
    monkeypatch.setattr(hooks, "anchor_to_project", slow_anchor)
    monkeypatch.setattr(subagent, "take_repo_root_flag", lambda argv: True)
    monkeypatch.setattr(subagent, "anchor_to_project", slow_anchor)

    _run_hook(entry, monkeypatch, capsys, _hook_payload(entry, source, transcript))

    assert _rows(store_dir)[-1].elapsed_ms >= 190
