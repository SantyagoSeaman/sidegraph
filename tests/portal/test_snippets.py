"""Bounded, checkout-relative source reads for Portal anchors (D3, D5)."""

import os
import signal
import sys
from pathlib import Path

import pytest

from sidegraph.portal.contract import Snippet

_POSIX = pytest.mark.skipif(sys.platform == "win32", reason="POSIX descriptor walk")
_MAX_BYTES = 2 * 1024 * 1024


def _read(
    root: Path | None, path: str, start: int = 1, end: int | None = None, *, full: bool = False
) -> Snippet:
    from sidegraph.portal.snippets import read_span

    return Snippet.model_validate(read_span(root, path, start, end, full=full))


@pytest.mark.parametrize("path", ["../outside.py", "pkg/../../x.py", "/path", "pkg//file.py"])
def test_invalid_relative_path_refused_before_open(tmp_path, monkeypatch, path) -> None:
    def unexpected_open(*args, **kwargs):
        pytest.fail("an invalid path reached os.open")

    with monkeypatch.context() as patch:
        patch.setattr(os, "open", unexpected_open)
        with pytest.raises(OSError):
            _read(tmp_path, path)


@_POSIX
@pytest.mark.parametrize("intermediate", [False, True])
def test_symlink_refused(tmp_path, intermediate) -> None:
    root = tmp_path / "checkout"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "file.py").write_text("outside marker\n")
    if intermediate:
        (root / "pkg").symlink_to(outside, target_is_directory=True)
        path = "pkg/file.py"
    else:
        (root / "file.py").symlink_to(outside / "file.py")
        path = "file.py"
    with pytest.raises(OSError):
        _read(root, path)


@_POSIX
def test_fifo_refused_within_two_seconds(tmp_path) -> None:
    os.mkfifo(tmp_path / "pipe")

    def deadline(*_args: object) -> None:
        pytest.fail("the source read blocked on a FIFO")

    previous = signal.signal(signal.SIGALRM, deadline)
    signal.alarm(2)
    try:
        with pytest.raises(OSError):
            _read(tmp_path, "pipe")
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


def test_large_file_refused(tmp_path) -> None:
    (tmp_path / "large.py").write_bytes(b"x" * (_MAX_BYTES + 1))
    with pytest.raises(OSError):
        _read(tmp_path, "large.py")


def test_growing_file_read_is_bounded(tmp_path, monkeypatch) -> None:
    file = tmp_path / "growing.py"
    file.write_bytes(b"x" * _MAX_BYTES)
    real_fstat = os.fstat

    def grow_after_measurement(fd: int):
        reported = real_fstat(fd)
        if reported.st_size == _MAX_BYTES:
            with file.open("ab") as handle:
                handle.write(b"unexpected extra text")
        return reported

    with monkeypatch.context() as patch:
        patch.setattr(os, "fstat", grow_after_measurement)
        result = _read(tmp_path, file.name)
    assert file.stat().st_size > _MAX_BYTES
    assert result.lines == ["x" * _MAX_BYTES]


@_POSIX
def test_intermediate_replacement_after_root_open_is_refused(tmp_path, monkeypatch) -> None:
    root = tmp_path / "checkout"
    root.mkdir()
    (root / "pkg").mkdir()
    (root / "pkg/file.py").write_text("inside marker\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "file.py").write_text("outside marker\n")
    real_open = os.open

    def replace_directory(path, flags, *args, **kwargs):
        fd = real_open(path, flags, *args, **kwargs)
        if path == root:
            (root / "pkg").rename(root / "original")
            (root / "pkg").symlink_to(outside, target_is_directory=True)
        return fd

    with monkeypatch.context() as patch:
        patch.setattr(os, "open", replace_directory)
        with pytest.raises(OSError):
            _read(root, "pkg/file.py")
    assert (root / "pkg").is_symlink()


def test_no_repository_root_refused() -> None:
    with pytest.raises(OSError):
        _read(None, "file.py")


def test_location_past_eof_clamped(tmp_path) -> None:
    (tmp_path / "short.py").write_text("first\nlast\n")
    snippet = _read(tmp_path, "short.py", 50, 100)
    assert (snippet.start, snippet.end, snippet.lines) == (2, 2, ["last"])


@pytest.mark.parametrize(("full", "limit"), [(False, 12), (True, 200)])
def test_snippet_line_limits(tmp_path, full, limit) -> None:
    (tmp_path / "long.py").write_text("".join(f"line {i}\n" for i in range(1, 400)))
    snippet = _read(tmp_path, "long.py", 5, full=full)
    assert (snippet.start, snippet.end) == (5, limit + 4)
    assert snippet.lines == [f"line {i}" for i in range(5, limit + 5)]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows realpath containment")
def test_windows_realpath_branch(tmp_path, monkeypatch) -> None:
    root = tmp_path / "checkout"
    root.mkdir()
    file = root / "file.py"
    file.write_text("inside marker\n")
    assert _read(root, file.name).lines == ["inside marker"]

    def unexpected_open(*args, **kwargs):
        pytest.fail("a Windows drive path reached os.open")

    with monkeypatch.context() as patch:
        patch.setattr(os, "open", unexpected_open)
        for path in ("D:/file.py", "pkg/D:relative.py"):
            with pytest.raises(OSError):
                _read(root, path)

    realpath = os.path.realpath
    outside = tmp_path / "outside.py"
    outside.write_text("outside marker\n")

    def redirected(path, *args, **kwargs):
        if Path(path) == file:
            return str(outside)
        return realpath(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os.path, "realpath", redirected)
        with pytest.raises(OSError):
            _read(root, file.name)


@_POSIX
def test_symlinked_checkout_root_is_supported(tmp_path) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "file.py").write_text("source\n")
    alias = tmp_path / "checkout-alias"
    alias.symlink_to(checkout, target_is_directory=True)
    assert _read(alias, "file.py").lines == ["source"]


@_POSIX
def test_posix_colon_in_file_name_is_readable(tmp_path) -> None:
    name = "a:b.py"
    (tmp_path / name).write_text("source\n")
    assert _read(tmp_path, name).lines == ["source"]


def test_windows_drive_parts_are_refused_before_path_construction(monkeypatch) -> None:
    from types import SimpleNamespace

    from sidegraph.portal import snippets

    monkeypatch.setattr(snippets, "sys", SimpleNamespace(platform="win32"))
    for path in ("D:/file.py", "pkg/D:relative.py"):
        with pytest.raises(OSError):
            snippets._parts(path)
