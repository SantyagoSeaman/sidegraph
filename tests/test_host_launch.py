"""The launch-commit record: SessionStart notes the commit uv resolved for ``@main``, and the
hot-path hooks launch from it (only when their fallback is on the branch: the pinned case is
test_a1 in tests/test_public_hook_launch.py).
design/superpowers/specs/2026-10-03-launch-from-session-commit-design.md

T3 is the writer's contract: which installs write, which write nothing, what the file holds.
T4 is its place in ``session_start``: first, and never able to change the hook's output.
Every test sets ``HOME`` and ``XDG_CACHE_HOME`` under ``tmp_path``: no test touches the real
cache directory.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import threading
from importlib import metadata
from pathlib import Path
from typing import Any

import pytest

import sidegraph.host.hooks as hooks
from sidegraph.host import launch

COMMIT = "f5babfb86a9562140acef8ddc61e7fe4a5933f00"
OTHER_COMMIT = "0123456789abcdef0123456789abcdef01234567"
URL = "https://github.com/SantyagoSeaman/sidegraph.git"
FIXTURE = Path(__file__).parent / "fixtures" / "mini_graph.json"


def _git_direct_url(**overrides: Any) -> dict[str, Any]:
    vcs_info: dict[str, Any] = {"vcs": "git", "requested_revision": "main", "commit_id": COMMIT}
    vcs_info.update(overrides.pop("vcs_info", {}))
    return {"url": URL, "vcs_info": vcs_info, **overrides}


class _Dist:
    """The slice of ``importlib.metadata.Distribution`` the writer reads."""

    def __init__(self, text: str | None) -> None:
        self._text = text

    def read_text(self, filename: str) -> str | None:
        return self._text if filename == "direct_url.json" else None


@pytest.fixture
def cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The cache directory every test in this file writes under."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    return tmp_path / "cache" / "sidegraph"


def _install(monkeypatch: pytest.MonkeyPatch, direct_url: Any) -> None:
    text = (
        direct_url if isinstance(direct_url, str) or direct_url is None else json.dumps(direct_url)
    )
    monkeypatch.setattr(metadata, "distribution", lambda name: _Dist(text))


def test_canonical_ref_is_main() -> None:
    assert launch.CANONICAL_REF == "main"


def test_cache_path_follows_xdg_and_treats_empty_as_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    assert launch.launch_commit_path() == tmp_path / "xdg" / "sidegraph" / "launch-commit"
    monkeypatch.setenv("XDG_CACHE_HOME", "")  # the shell's `:-` treats empty as unset
    assert (
        launch.launch_commit_path() == tmp_path / "home" / ".cache" / "sidegraph" / "launch-commit"
    )
    monkeypatch.delenv("XDG_CACHE_HOME")
    assert (
        launch.launch_commit_path() == tmp_path / "home" / ".cache" / "sidegraph" / "launch-commit"
    )


def test_t3_an_at_main_install_records_exactly_the_forty_bytes(
    cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, _git_direct_url())
    launch.record_launch_commit()
    record = cache / "launch-commit"
    assert record.read_bytes() == COMMIT.encode("ascii")  # no newline
    assert sorted(p.name for p in cache.iterdir()) == ["launch-commit"]  # no tmp file left


def test_t3_a_changed_commit_replaces_the_record(
    cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, _git_direct_url())
    launch.record_launch_commit()
    _install(monkeypatch, _git_direct_url(vcs_info={"commit_id": OTHER_COMMIT}))
    launch.record_launch_commit()
    assert (cache / "launch-commit").read_bytes() == OTHER_COMMIT.encode("ascii")


def test_t3_an_unchanged_value_keeps_the_file(cache: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, _git_direct_url())
    launch.record_launch_commit()
    inode = os.stat(cache / "launch-commit").st_ino
    launch.record_launch_commit()
    assert os.stat(cache / "launch-commit").st_ino == inode


@pytest.mark.parametrize(
    "direct_url",
    [
        pytest.param(_git_direct_url(vcs_info={"requested_revision": "v0.7.0"}), id="tag"),
        pytest.param(_git_direct_url(vcs_info={"requested_revision": COMMIT}), id="sha"),
        pytest.param(_git_direct_url(vcs_info={"requested_revision": None}), id="no-revision"),
        pytest.param(_git_direct_url(vcs_info={"vcs": "hg"}), id="not-git"),
        pytest.param(_git_direct_url(url="https://github.com/someone/else.git"), id="other-url"),
        pytest.param(_git_direct_url(vcs_info={"commit_id": COMMIT[:39]}), id="short-commit"),
        pytest.param(_git_direct_url(vcs_info={"commit_id": COMMIT + "0"}), id="long-commit"),
        pytest.param(_git_direct_url(vcs_info={"commit_id": COMMIT.upper()}), id="upper-commit"),
        pytest.param(_git_direct_url(vcs_info={"commit_id": 7}), id="commit-not-a-string"),
        pytest.param(
            {"url": "file:///work/sidegraph", "dir_info": {"editable": True}}, id="editable"
        ),
        pytest.param({"url": "file:///work/sidegraph", "dir_info": {}}, id="file"),
        pytest.param(None, id="pypi-no-direct-url"),
        pytest.param("not json", id="malformed-json"),
        pytest.param("[]", id="not-an-object"),
    ],
)
def test_t3_anything_but_an_at_main_git_install_writes_nothing(
    cache: Path, monkeypatch: pytest.MonkeyPatch, direct_url: Any
) -> None:
    _install(monkeypatch, direct_url)
    launch.record_launch_commit()
    assert not cache.exists()


def test_t3_a_pinned_install_leaves_the_existing_record_alone(
    cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The record is shared by every project on the machine: a pinned install used for CI or
    a pilot must never repoint the plugin's hot hooks at an older commit."""
    _install(monkeypatch, _git_direct_url())
    launch.record_launch_commit()
    _install(
        monkeypatch,
        _git_direct_url(vcs_info={"requested_revision": "v0.7.0", "commit_id": OTHER_COMMIT}),
    )
    launch.record_launch_commit()
    assert (cache / "launch-commit").read_bytes() == COMMIT.encode("ascii")


def test_t3_a_missing_distribution_writes_nothing_and_never_raises(
    cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def missing(name: str) -> Any:
        raise metadata.PackageNotFoundError(name)

    monkeypatch.setattr(metadata, "distribution", missing)
    launch.record_launch_commit()
    assert not cache.exists()


def test_t3_an_unwritable_cache_never_raises_nor_prints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where the cache directory should be\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CACHE_HOME", str(blocker))
    _install(monkeypatch, _git_direct_url())
    launch.record_launch_commit()
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""


def _run_with_deadline(seconds: float) -> bool:
    """Run the writer in a thread; True when it came back within ``seconds``. A writer that
    blocks (on a FIFO) leaves the thread behind, and the caller fails the test instead of
    hanging the suite."""
    worker = threading.Thread(target=launch.record_launch_commit, daemon=True)
    worker.start()
    worker.join(seconds)
    return not worker.is_alive()


def test_t3_a_fifo_at_the_path_does_not_block_the_writer(
    cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``open`` on a FIFO with no writer blocks forever, and the writer runs first in
    SessionStart: it must never open anything that is not a regular file."""
    _install(monkeypatch, _git_direct_url())
    record = cache / "launch-commit"
    cache.mkdir(parents=True)
    os.mkfifo(record)
    try:
        returned = _run_with_deadline(2)
    finally:
        # A stuck writer is parked in open(2); give it a writer so the thread can end.
        with contextlib.suppress(OSError):
            os.close(os.open(record, os.O_WRONLY | os.O_NONBLOCK))
    assert returned, "record_launch_commit blocked on a FIFO"
    assert record.read_bytes() == COMMIT.encode("ascii")  # the FIFO was replaced
    assert sorted(p.name for p in cache.iterdir()) == ["launch-commit"]


class _OpenedPaths:
    """Every path ``open`` was called on while armed (a PEP 578 audit hook: a hook cannot be
    removed, so it records only between ``arm`` and ``disarm``)."""

    def __init__(self) -> None:
        self.paths: list[str] = []
        self._armed = False
        sys.addaudithook(self._hook)

    def _hook(self, event: str, args: tuple[Any, ...]) -> None:
        if self._armed and event == "open" and isinstance(args[0], str | bytes | os.PathLike):
            self.paths.append(os.fsdecode(args[0]))

    def arm(self) -> None:
        self.paths.clear()
        self._armed = True

    def disarm(self) -> None:
        self._armed = False


def test_t3_a_huge_file_at_the_path_is_replaced_without_being_read(
    cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The existing value is read only when the file is exactly 40 bytes: a 10 MB file is
    "differs" without being opened, and the atomic write replaces it."""
    _install(monkeypatch, _git_direct_url())
    record = cache / "launch-commit"
    cache.mkdir(parents=True)
    record.write_bytes(b"x" * (10 * 1024 * 1024))
    spy = _OpenedPaths()
    spy.arm()
    try:
        returned = _run_with_deadline(2)
    finally:
        spy.disarm()
    assert returned, "record_launch_commit did not return within 2 s"
    assert str(record) not in spy.paths, "the writer opened a 10 MB record to read it"
    assert record.read_bytes() == COMMIT.encode("ascii")


def test_t3_a_directory_at_the_path_is_given_up_on_silently(
    cache: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``os.replace`` cannot put a file over a directory: the writer leaves it, leaves no tmp
    file behind, and says nothing (the hot hooks then run the ``@main`` fallback)."""
    _install(monkeypatch, _git_direct_url())
    record = cache / "launch-commit"
    record.mkdir(parents=True)
    assert _run_with_deadline(2)
    assert record.is_dir()
    assert sorted(p.name for p in cache.iterdir()) == ["launch-commit"]
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""


def test_t3_a_symlink_to_the_same_value_is_left_alone(
    cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``stat`` follows the link, so a symlink to a regular file holding the same 40 bytes
    counts as "unchanged" and is neither read around nor replaced."""
    _install(monkeypatch, _git_direct_url())
    cache.mkdir(parents=True)
    target = cache / "elsewhere"
    target.write_bytes(COMMIT.encode("ascii"))
    record = cache / "launch-commit"
    record.symlink_to(target)
    launch.record_launch_commit()
    assert record.is_symlink()
    assert record.read_bytes() == COMMIT.encode("ascii")


def _run_session_start(
    db: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> str:
    monkeypatch.setenv("SIDEGRAPH_DB", str(db))
    monkeypatch.setenv("SIDEGRAPH_GRAPH", str(FIXTURE))
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    hooks.session_start()
    return capsys.readouterr().out


def test_t4_session_start_records_before_it_opens_the_store(
    cache: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import sidegraph.store as store_module

    events: list[str] = []
    real_store = store_module.Store

    class _Recording(real_store):  # type: ignore[valid-type, misc]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            events.append("store")
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(launch, "record_launch_commit", lambda: events.append("writer"))
    monkeypatch.setattr(store_module, "Store", _Recording)
    _run_session_start(tmp_path / "s.db", monkeypatch, capsys)
    assert events[:2] == ["writer", "store"]


def test_t4_a_writer_that_raises_changes_nothing_in_the_output(
    cache: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(launch, "record_launch_commit", lambda: None)
    baseline = _run_session_start(tmp_path / "a.db", monkeypatch, capsys)

    def boom() -> None:
        raise RuntimeError("writer exploded")

    monkeypatch.setattr(launch, "record_launch_commit", boom)
    out = _run_session_start(tmp_path / "b.db", monkeypatch, capsys)
    assert "additionalContext" in baseline  # the baseline is a real answer, not `{}`
    assert json.loads(out) == json.loads(baseline)
