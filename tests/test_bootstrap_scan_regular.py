"""Synthetic regular-file, race, descriptor and byte-budget scanner regressions."""

from __future__ import annotations

import errno
import json
import os
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from sidegraph.bootstrap import scan
from sidegraph.bootstrap.model import Exclusion
from sidegraph.profiles import get_profile

# The child imports and exercises the actual installed scanner. A timeout is always a
# failing outcome; kill/reap is parent-side so OSError handlers cannot swallow it.
_FIFO_CHILD = r"""
import json, os, sys
from pathlib import Path
from sidegraph.bootstrap import scan
from sidegraph.profiles import get_profile
root = Path(sys.argv[1])
mode = sys.argv[2]
path = root / "docs/adr/source.md"
path.parent.mkdir(parents=True)
if mode == "regular":
    path.write_text("# Regular\n", encoding="utf-8")
elif mode == "alias":
    target = root / "pipe"
    os.mkfifo(target)
    path.symlink_to(target)
elif mode == "swap":
    path.write_text("# Before swap\n", encoding="utf-8")
    real_open = os.open
    real_fdopen = os.fdopen
    created = []
    def swap_open(name, flags, *args, **kwargs):
        if Path(name) == path:
            path.unlink()
            os.mkfifo(path)
            fd = real_open(name, flags, *args, **kwargs)
            created.append(fd)
            return fd
        return real_open(name, flags, *args, **kwargs)
    def no_fifo_read(fd, *args, **kwargs):
        if fd in created:
            raise AssertionError("nonregular descriptor reached reader")
        return real_fdopen(fd, *args, **kwargs)
    os.open = swap_open
    os.fdopen = no_fifo_read
else:
    os.mkfifo(path)
explicit = (path,) if mode in ("explicit", "swap") else ()
included = (path,) if mode == "include" else ()
result = scan.scan_sources(root, get_profile("generic-adr"), explicit,
                           included_files=included)
if mode == "swap":
    assert len(created) == 1, "scanner did not use the guarded open boundary"
    for fd in created:
        try:
            os.fstat(fd)
        except OSError as exc:
            assert exc.errno == 9
        else:
            raise AssertionError("descriptor leaked")
print(json.dumps({"files": result.files,
                  "exclusions": [(e.path, e.reason) for e in result.exclusions]}))
"""


def _child(root: Path, mode: str) -> dict:
    proc = subprocess.Popen(
        [sys.executable, "-c", _FIFO_CHILD, str(root), mode],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate()
        pytest.fail(f"scanner blocked for {mode}; child killed/reaped: {stderr}")
    assert proc.returncode == 0, stderr
    return json.loads(stdout)


def _write(root: Path, data: bytes, name: str = "docs/adr/source.md") -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _accept(root: Path, path: Path, *, include: bool = False, limit: int = 512_000):
    return scan._accept_file(
        root, path, included={path.resolve()} if include else set(), max_bytes=limit
    )


def test_subprocess_regular_positive_control(tmp_path: Path) -> None:
    assert _child(tmp_path, "regular") == {"files": ["docs/adr/source.md"], "exclusions": []}


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="mkfifo unavailable")
@pytest.mark.parametrize("mode", ["profile", "explicit", "include", "alias"])
def test_fifo_without_writer_is_excluded_without_blocking(tmp_path: Path, mode: str) -> None:
    assert _child(tmp_path, mode) == {
        "files": [],
        "exclusions": [["docs/adr/source.md", "non-regular-file"]],
    }


@pytest.mark.skipif(
    not hasattr(os, "mkfifo") or not hasattr(os, "O_NONBLOCK"),
    reason="FIFO/nonblocking open unavailable",
)
def test_regular_to_fifo_open_race_does_not_block_or_read(tmp_path: Path) -> None:
    assert _child(tmp_path, "swap") == {
        "files": [],
        "exclusions": [["docs/adr/source.md", "non-regular-file"]],
    }


def test_direct_directory_admission_is_nonregular(tmp_path: Path) -> None:
    folder = tmp_path / "docs"
    folder.mkdir()
    assert _accept(tmp_path, folder) == (None, Exclusion(path="docs", reason="non-regular-file"))


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="Unix sockets unavailable")
def test_socket_is_nonregular_even_when_included() -> None:
    # macOS limits AF_UNIX path length: pytest's nested fixture paths may exceed it.
    with tempfile.TemporaryDirectory(prefix="sg224-", dir="/tmp") as directory:
        root = Path(directory)
        path = root / "source.md"
        with socket.socket(socket.AF_UNIX) as sock:
            sock.bind(str(path))
            assert _accept(root, path, include=True) == (
                None,
                Exclusion(path="source.md", reason="non-regular-file"),
            )


def test_regular_alias_remains_eligible(tmp_path: Path) -> None:
    target = _write(tmp_path, "# Решение\n".encode(), "source.md")
    alias = tmp_path / "alias.md"
    alias.symlink_to(target)
    assert _accept(tmp_path, alias) == ("alias.md", None)


class _ReadTrace:
    """Wrap real file objects to observe physical offsets and deterministic leaf changes."""

    def __init__(self, handle, events: list, *, short: int | None = None, after=None, fail=False):
        self.handle = handle
        self.events = events
        self.short = short
        self.after = after
        self.fail = fail

    def read(self, size=-1):
        assert 0 < size <= 4096, "unbounded/nonpositive read"
        if self.fail:
            raise OSError(errno.EIO, "synthetic read failure")
        data = self.handle.read(min(size, self.short) if self.short else size)
        self.events.append((size, len(data), os.lseek(self.handle.fileno(), 0, os.SEEK_CUR)))
        if self.after is not None:
            action, self.after = self.after, None
            action()
        return data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.handle.close()


def _trace_reads(monkeypatch, *, short=None, after=None, fail=False) -> list:
    events: list = []
    real_fdopen = os.fdopen
    real_path_open = Path.open

    def fdopen(fd, *args, **kwargs):
        return _ReadTrace(
            real_fdopen(fd, *args, **kwargs), events, short=short, after=after, fail=fail
        )

    def path_open(path, *args, **kwargs):
        handle = real_path_open(path, *args, **kwargs)
        if args and args[0] == "rb":
            return _ReadTrace(handle, events, short=short, after=after, fail=fail)
        return handle

    monkeypatch.setattr(os, "fdopen", fdopen)
    monkeypatch.setattr(Path, "open", path_open)
    return events


def _assert_closed(fds: list[int]) -> None:
    assert fds, "scanner bypassed descriptor admission boundary"
    for fd in fds:
        with pytest.raises(OSError) as exc:
            os.fstat(fd)
        assert exc.value.errno == errno.EBADF


@pytest.mark.parametrize(
    ("data", "limit", "include", "want"),
    [
        (b"abcdefgh", 8, False, None),
        (b"abcdefghi", 8, False, "over-size-limit"),
        (b"x" * 9000, 8, True, None),
        (b"x" * 4095 + "€".encode() + b"end", 5000, False, None),
        (b"x" * 4096 + b"\xff", 5000, False, "unreadable"),
        (b"x" * 4096 + b"\xe2\x82", 5000, False, "unreadable"),
        (b"x" * 4096 + b"\0", 5000, False, None),
        (b"x" * 3000 + b"\0", 5000, False, "binary"),
    ],
)
def test_byte_and_utf8_contracts(tmp_path, data, limit, include, want) -> None:
    path = _write(tmp_path, data)
    result = _accept(tmp_path, path, include=include, limit=limit)
    assert result == (
        ("docs/adr/source.md", None)
        if want is None
        else (None, Exclusion(path="docs/adr/source.md", reason=want))
    )


def test_short_reads_preserve_full_binary_window(tmp_path, monkeypatch) -> None:
    path = _write(tmp_path, b"x" * 3000 + b"\0" + b"x" * 6000)
    events = _trace_reads(monkeypatch, short=11)
    assert _accept(tmp_path, path, include=True) == (
        None,
        Exclusion(path="docs/adr/source.md", reason="binary"),
    )
    assert events[-1][2] <= 4096


def test_short_reads_keep_utf8_decoder_state(tmp_path, monkeypatch) -> None:
    path = _write(tmp_path, ("€" * 2000).encode())
    events = _trace_reads(monkeypatch, short=7)
    assert _accept(tmp_path, path) == ("docs/adr/source.md", None)
    assert events and sum(e[1] for e in events) == 6000


def test_included_binary_only_physically_consumes_prefix(tmp_path, monkeypatch) -> None:
    path = _write(tmp_path, b"\0" + b"x" * 1_000_000)
    events = _trace_reads(monkeypatch)
    assert _accept(tmp_path, path, include=True, limit=8) == (
        None,
        Exclusion(path="docs/adr/source.md", reason="binary"),
    )
    assert events == [(4096, 4096, 4096)]


def test_tiny_file_read_does_not_prefetch_remainder(tmp_path, monkeypatch) -> None:
    path = _write(tmp_path, b"a")
    real_fstat = os.fstat

    def grow_after_metadata(fd):
        metadata = real_fstat(fd)
        with path.open("ab") as writer:
            writer.write(b"x" * 20_000)
        return metadata

    monkeypatch.setattr(os, "fstat", grow_after_metadata)
    events = _trace_reads(monkeypatch)
    assert _accept(tmp_path, path, include=True) == (
        None,
        Exclusion(path="docs/adr/source.md", reason="changed-during-scan"),
    )
    assert events == [(2, 2, 2)]


@pytest.mark.parametrize(
    "include,want,consumed", [(False, "over-size-limit", 9), (True, "changed-during-scan", 5)]
)
def test_growth_is_bounded_by_opened_metadata(tmp_path, monkeypatch, include, want, consumed):
    path = _write(tmp_path, b"abcd")
    real_fstat = os.fstat

    def grow_after_metadata(fd):
        metadata = real_fstat(fd)
        with path.open("ab") as writer:
            writer.write(b"x" * 20_000)
        return metadata

    monkeypatch.setattr(os, "fstat", grow_after_metadata)
    events = _trace_reads(monkeypatch)
    assert _accept(tmp_path, path, include=include, limit=8) == (
        None,
        Exclusion(path="docs/adr/source.md", reason=want),
    )
    assert sum(e[1] for e in events) == consumed
    assert events[-1][2] == consumed


def test_path_swap_after_prefix_validates_original_descriptor(tmp_path, monkeypatch) -> None:
    path = _write(tmp_path, b"x" * 6000)
    backup = tmp_path / "original.md"

    def replace_path():
        path.rename(backup)
        path.write_bytes(b"\xff")

    events = _trace_reads(monkeypatch, after=replace_path)
    assert _accept(tmp_path, path) == ("docs/adr/source.md", None)
    assert backup.read_bytes() == b"x" * 6000
    assert path.read_bytes() == b"\xff"
    assert sum(e[1] for e in events) == 6000


@pytest.mark.parametrize("failure", ["binary", "utf8", "read", "fdopen", "size", "success"])
def test_descriptor_is_closed_on_every_exit(tmp_path, monkeypatch, failure) -> None:
    data = {"binary": b"a\0", "utf8": b"\xff"}.get(failure, b"valid")
    path = _write(tmp_path, data)
    real_open, real_fstat = os.open, os.fstat
    created: list[int] = []

    def record_open(name, flags, *args, **kwargs):
        fd = real_open(name, flags, *args, **kwargs)
        created.append(fd)
        return fd

    def grow_before_metadata(fd):
        with path.open("ab") as writer:
            writer.write(b"x" * 30)
        return real_fstat(fd)

    monkeypatch.setattr(os, "open", record_open)
    if failure == "size":
        monkeypatch.setattr(os, "fstat", grow_before_metadata)
    elif failure == "fdopen":

        def broken_fdopen(*args, **kwargs):
            raise OSError(errno.EMFILE, "synthetic transfer failure")

        monkeypatch.setattr(os, "fdopen", broken_fdopen)
    elif failure == "read":
        _trace_reads(monkeypatch, fail=True)
    outcome = _accept(tmp_path, path, limit=8)
    want = {
        "binary": "binary",
        "utf8": "unreadable",
        "read": "unreadable",
        "fdopen": "unreadable",
        "size": "over-size-limit",
    }.get(failure)
    assert outcome == (
        ("docs/adr/source.md", None)
        if want is None
        else (None, Exclusion(path="docs/adr/source.md", reason=want))
    )
    # Restore fstat before inspecting closure so the failure injector does not run again.
    monkeypatch.setattr(os, "fstat", real_fstat)
    _assert_closed(created)
    assert len(created) == 1


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="no-follow open unavailable")
def test_resolved_leaf_replaced_by_outside_link_is_not_read(tmp_path, monkeypatch) -> None:
    path = _write(tmp_path, b"visible")
    outside = tmp_path.parent / "sg224-outside.md"
    outside.write_bytes(b"\0outside")
    real_open = os.open
    attempts: list = []

    def replace_before_open(name, flags, *args, **kwargs):
        attempts.append(Path(name))
        path.unlink()
        path.symlink_to(outside)
        return real_open(name, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", replace_before_open)
    assert _accept(tmp_path, path) == (
        None,
        Exclusion(path="docs/adr/source.md", reason="unreadable"),
    )
    assert attempts == [path]
    assert outside.read_bytes() == b"\0outside"


def test_alias_newly_resolved_outside_is_rejected_before_open(tmp_path, monkeypatch) -> None:
    target = _write(tmp_path, b"visible", "visible.md")
    alias = tmp_path / "alias.md"
    alias.symlink_to(target)
    outside = tmp_path.parent / "sg224-new-target.md"
    outside.write_bytes(b"\0outside")
    real_resolve = scan._resolve

    def resolve_after_switch(path):
        if path == alias:
            alias.unlink()
            alias.symlink_to(outside)
        return real_resolve(path)

    monkeypatch.setattr(scan, "_resolve", resolve_after_switch)
    events = _trace_reads(monkeypatch)
    assert _accept(tmp_path, alias) == (
        None,
        Exclusion(path="alias.md", reason="outside-repository"),
    )
    assert events == []


@pytest.mark.parametrize("include", [False, True])
def test_alias_switched_into_excluded_target_rechecks_exact_override(
    tmp_path, monkeypatch, include
) -> None:
    visible = _write(tmp_path, b"visible", "visible.md")
    hidden = _write(tmp_path, b"hidden", ".sidegraph/hidden.md")
    alias = tmp_path / "docs/adr/source.md"
    alias.parent.mkdir(parents=True)
    alias.symlink_to(visible)
    real_accept = scan._accept_file

    def switch_after_discovery(root, path, **kwargs):
        if path == alias:
            alias.unlink()
            alias.symlink_to(hidden)
        return real_accept(root, path, **kwargs)

    monkeypatch.setattr(scan, "_accept_file", switch_after_discovery)
    events = _trace_reads(monkeypatch)
    result = scan.scan_sources(
        tmp_path, get_profile("generic-adr"), included_files=(hidden,) if include else ()
    )
    if include:
        assert result.files == (".sidegraph/hidden.md", "docs/adr/source.md")
        assert result.exclusions == ()
    else:
        assert result.files == ()
        assert result.exclusions == (
            Exclusion(path="docs/adr/source.md", reason="excluded-directory"),
        )
        assert events == []


def test_fstat_failure_closes_owned_descriptor(tmp_path, monkeypatch) -> None:
    path = _write(tmp_path, b"valid")
    real_open, real_fstat = os.open, os.fstat
    created: list[int] = []

    def record_open(name, flags, *args, **kwargs):
        fd = real_open(name, flags, *args, **kwargs)
        created.append(fd)
        return fd

    def failed_metadata(fd):
        raise OSError(errno.EIO, "synthetic fstat failure")

    monkeypatch.setattr(os, "open", record_open)
    monkeypatch.setattr(os, "fstat", failed_metadata)
    assert _accept(tmp_path, path) == (
        None,
        Exclusion(path="docs/adr/source.md", reason="unreadable"),
    )
    monkeypatch.setattr(os, "fstat", real_fstat)
    _assert_closed(created)


@pytest.mark.parametrize("include", [False, True])
def test_valid_shrink_after_open_remains_eligible(tmp_path, monkeypatch, include) -> None:
    path = _write(tmp_path, b"original valid text")
    real_fstat = os.fstat

    def shrink_after_metadata(fd):
        metadata = real_fstat(fd)
        path.write_bytes(b"short")
        return metadata

    monkeypatch.setattr(os, "fstat", shrink_after_metadata)
    assert _accept(tmp_path, path, include=include) == ("docs/adr/source.md", None)


def test_preopen_oversize_does_not_create_descriptor(tmp_path, monkeypatch) -> None:
    path = _write(tmp_path, b"ninebytes")

    real_open = os.open

    def forbidden_open(name, *args, **kwargs):
        if Path(name) == path:
            raise AssertionError("over-size input must not be opened")
        return real_open(name, *args, **kwargs)

    monkeypatch.setattr(os, "open", forbidden_open)
    assert _accept(tmp_path, path, limit=8) == (
        None,
        Exclusion(path="docs/adr/source.md", reason="over-size-limit"),
    )


@pytest.mark.parametrize(
    "include,want", [(False, "over-size-limit"), (True, "changed-during-scan")]
)
def test_nul_overflow_reports_growth_before_binary(tmp_path, monkeypatch, include, want) -> None:
    path = _write(tmp_path, b"abcdefgh")
    real_fstat = os.fstat

    def append_nul_after_metadata(fd):
        metadata = real_fstat(fd)
        with path.open("ab") as writer:
            writer.write(b"\0")
        return metadata

    monkeypatch.setattr(os, "fstat", append_nul_after_metadata)
    events = _trace_reads(monkeypatch)
    assert _accept(tmp_path, path, include=include, limit=8) == (
        None,
        Exclusion(path="docs/adr/source.md", reason=want),
    )
    assert events == [(9, 9, 9)]


@pytest.mark.parametrize("phase", ["prefix", "remainder"])
def test_would_block_is_not_eof_and_closes_regular_descriptor(tmp_path, monkeypatch, phase):
    # Inject the documented raw-I/O return at one read boundary. This does not claim
    # that a normal local regular filesystem actually produces EAGAIN.
    path = _write(tmp_path, b"a" * 4096 + b"\xff")
    real_fdopen = os.fdopen
    owned: list[int] = []

    def fdopen(fd, *args, **kwargs):
        handle = real_fdopen(fd, *args, **kwargs)
        owned.append(fd)

        class WouldBlock:
            calls = 0

            def read(self, size):
                blocked_call = 0 if phase == "prefix" else 1
                if self.calls == blocked_call:
                    self.calls += 1
                    return None
                self.calls += 1
                return handle.read(size)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                handle.close()

        return WouldBlock()

    monkeypatch.setattr(os, "fdopen", fdopen)
    assert _accept(tmp_path, path) == (
        None,
        Exclusion(path="docs/adr/source.md", reason="unreadable"),
    )
    _assert_closed(owned)


@pytest.mark.parametrize("include", [False, True])
def test_empty_file_eof_remains_eligible(tmp_path, include) -> None:
    path = _write(tmp_path, b"")
    assert _accept(tmp_path, path, include=include) == ("docs/adr/source.md", None)
