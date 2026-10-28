"""Real Git smudge windows preserve the marker Git is checking out.

The timeout shapes cover an empty committed store and a clone whose shards have not yet
arrived; neither is proof of explicit new-store creation.
# see design/superpowers/specs/2026-10-28-format-marker-checkout-race-design.md
"""

# ruff: noqa: F811

from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from sidegraph import checkout_window as cw
from sidegraph.store import Store
from sidegraph.store_format import marker_text
from sidegraph.store_layout import StoreRequirementError, store_marker, store_requirement
from tests.store_format2_support import (
    commit_all,
    entity_body,
    git,
    git_env,
    git_repo,  # noqa: F401
    make_store,
    put_file,
    put_shard_line,
)

_DATE = "2026-10-01T00:00:00Z"
_FILTER = """import os
import sys
import time
from pathlib import Path

data = sys.stdin.buffer.read()
probe = Path(os.environ["SG_PROBE_DIR"])
(probe / "entered").write_text("entered\\n")
deadline = time.monotonic() + 10
while not (probe / "release").exists():
    if time.monotonic() >= deadline:
        raise SystemExit("smudge release timed out")
    time.sleep(0.01)
sys.stdout.buffer.write(data)
"""


def _wait_for(predicate):
    deadline = time.monotonic() + 10
    while not predicate():
        assert time.monotonic() < deadline, "timed out waiting for the checkout window"
        time.sleep(0.01)


@pytest.mark.slow
@pytest.mark.parametrize("mode", ["rewrite", "switch", "clone", "empty-rewrite", "packed-clone"])
def test_open_in_git_checkout_window_preserves_its_marker(git_repo, tmp_path, monkeypatch, mode):
    forced_timeout = mode in ("empty-rewrite", "packed-clone")
    clone = mode in ("clone", "packed-clone")
    (git_repo / "README").write_text("root\n")
    commit_all(git_repo, "root", date=_DATE)
    git(git_repo, "branch", "nostore")
    store = make_store(git_repo, fmt=2, n=256 if mode == "packed-clone" else 64)
    if mode == "packed-clone":
        (store / "format").write_text(marker_text(2, "0.19.0", 256))
        put_shard_line(store, "entity", "E0001", entity_body("E0001"))
    elif mode != "empty-rewrite":
        put_file(store, "entity", entity_body("E0001"))
    Store(store).close()
    commit_all(git_repo, "store A", date=_DATE)
    expected = (store / "format").read_bytes()
    filter_script = tmp_path / "filter.py"
    filter_script.write_text(_FILTER)
    command = f'"{Path(sys.executable).as_posix()}" "{filter_script.as_posix()}"'
    if mode in ("rewrite", "empty-rewrite"):
        expected = marker_text(2, "0.19.0" if forced_timeout else "0.18.0", 64).encode()
        (store / "format").write_bytes(expected)
        commit_all(git_repo, "store B", date=_DATE)
        git(git_repo, "checkout", "-q", "HEAD~1")
    elif mode == "switch":
        git(git_repo, "checkout", "-q", "nostore")
    if clone:
        (git_repo / ".gitattributes").write_text(".sidegraph/format filter=probe\n")
        commit_all(git_repo, "filter", date=_DATE)
        worktree = tmp_path / "clone"
        target = worktree / ".sidegraph"
        argv = [
            "git",
            "-c",
            f"filter.probe.smudge={command}",
            "clone",
            "-q",
            "--config",
            f"filter.probe.smudge={command}",
            str(git_repo),
            str(worktree),
        ]
    else:
        git(git_repo, "config", "filter.probe.smudge", command)
        (git_repo / ".git/info/attributes").write_text(".sidegraph/format filter=probe\n")
        worktree = git_repo
        target = store
        argv = ["git", "-C", str(git_repo), "checkout", "-q", "main"]
    entered = tmp_path / "entered"
    release = tmp_path / "release"
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**git_env(), "SG_PROBE_DIR": str(tmp_path)},
    )
    thread = None
    errors = []
    observed = threading.Event()
    finished = threading.Event()
    try:
        _wait_for(entered.exists)
        assert not (target / "format").exists(), "the smudge window must really be markerless"
        if mode == "packed-clone":
            assert not (target / "shards").exists(), "shards must not yet have arrived"
        real_view = cw.git_view
        if forced_timeout:
            monkeypatch.setattr(cw, "MARKER_WAIT_SECONDS", 0.02)
            real_run = subprocess.run

            def timeout(args, **kwargs):
                if args[0] == "git" and "rev-parse" in args:
                    raise subprocess.TimeoutExpired(args, kwargs["timeout"])
                return real_run(args, **kwargs)

            monkeypatch.setattr(cw.subprocess, "run", timeout)

        def observe(path, **kwargs):
            try:
                return real_view(path, **kwargs)
            finally:
                observed.set()

        monkeypatch.setattr(cw, "git_view", observe)

        def open_store():
            try:
                Store(target).close()
            except Exception as error:
                errors.append(error)
            finally:
                finished.set()

        thread = threading.Thread(target=open_store)
        thread.start()
        _wait_for(lambda: observed.is_set() or finished.is_set())
        time.sleep(0.05)
    finally:
        release.touch()
        if thread is not None:
            thread.join(timeout=10)
        try:
            stdout, stderr = proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            raise
    actual = (target / "format").read_bytes() if (target / "format").exists() else None
    observation = (mode, proc.returncode, stdout, stderr, actual, errors)
    assert thread is not None and not thread.is_alive(), observation
    if forced_timeout:
        assert len(errors) == 1 and isinstance(errors[0], StoreRequirementError), observation
        assert errors[0].kind == "missing" and errors[0].busy, observation
    else:
        assert errors == [], observation
    assert proc.returncode == 0, observation
    assert b"unable to create file" not in stderr, observation
    assert actual == expected, observation
    assert git(worktree, "status", "--porcelain", "--", ".sidegraph/format") == ""
    if mode == "packed-clone":
        assert store_marker(target).shards == 256
        assert store_requirement(target) == "0.19.0"
    elif mode == "empty-rewrite":
        assert store_requirement(target) == "0.19.0"
