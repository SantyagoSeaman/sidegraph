"""Shared fixtures and helpers for the graph refresh hook tests: real ``git init`` repositories in
``tmp_path``, a fake ``graphify`` on ``PATH``, and a git environment that never reads the real
``~/.gitconfig``. Not a test module: pytest collects only ``test_*.py``.
see design/superpowers/specs/2026-10-02-graph-refresh-hook-design.md (section 4)
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

HOOKS = ("post-commit", "post-merge", "post-checkout")
HELPER_NAME = "sidegraph-graph-refresh"

# The block's exact bytes (D2), pinned here so that no change to the code under test moves them.
BLOCK_LINE_2 = (
    "# Rebuilds the code graph in the background, main checkout only. "
    "Managed by sidegraph-init; remove with `sidegraph-init --remove-hooks`.\n"
)


def expected_block(hook: str) -> str:
    return (
        "# >>> sidegraph graph refresh >>>\n"
        + BLOCK_LINE_2
        + 'g="$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null)'
        + f'/hooks/sidegraph-graph-refresh"; [ ! -x "$g" ] || "$g" {hook} "$@" || true\n'
        + "# <<< sidegraph graph refresh <<<\n"
    )


CREATED_HEADER = "#!/bin/sh\n# Created by sidegraph-init.\n"

# The fake logs ``start <pid> <HEAD> <cwd> <args>`` and ``end <pid>``, and sleeps when
# FAKE_GRAPHIFY_SLEEP is set. It prints a line, which the helper writes to its own log.
FAKE_GRAPHIFY = """#!/bin/sh
printf 'start %s %s %s %s seed=%s\\n' "$$" "$(git rev-parse HEAD 2>/dev/null)" "$PWD" "$*" \\
  "$PYTHONHASHSEED" >> "$FAKE_GRAPHIFY_LOG"
echo "fake graphify ran: $*"
[ -z "$FAKE_GRAPHIFY_SLEEP" ] || sleep "$FAKE_GRAPHIFY_SLEEP"
printf 'end %s\\n' "$$" >> "$FAKE_GRAPHIFY_LOG"
"""


@dataclass(frozen=True)
class Start:
    pid: str
    head: str
    cwd: Path
    args: str


@dataclass
class Sandbox:
    root: Path
    log: Path
    fake_bin: Path
    home: Path

    def repo(self, name: str = "main", *, files: bool = True) -> Path:
        """A new repository with one commit (``a.txt``), on branch ``main``."""
        path = self.root / name
        path.mkdir()
        git(path, "init", "-q", "-b", "main")
        if files:
            (path / "a.txt").write_text("a\n")
            git(path, "add", "a.txt")
        git(path, "commit", "-q", "--no-verify", "-m", "initial", "--allow-empty")
        return path

    def starts(self) -> list[Start]:
        return read_starts(self.log)

    def max_concurrent(self) -> int:
        return max_concurrent(self.log)


def git(cwd: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=check)
    return result.stdout.strip()


def commit(cwd: Path, message: str = "c") -> str:
    """An empty commit (hooks run); returns the new HEAD."""
    git(cwd, "commit", "-q", "--allow-empty", "-m", message)
    return git(cwd, "rev-parse", "HEAD")


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Sandbox:
    """A hermetic git environment (HOME, global config and identity all inside ``tmp_path``) and a
    fake ``graphify`` first on ``PATH``, logging to ``sandbox.log``."""
    home = tmp_path / "home"
    home.mkdir()
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake = fake_bin / "graphify"
    fake.write_text(FAKE_GRAPHIFY)
    fake.chmod(0o755)
    work = tmp_path / "work"
    work.mkdir()
    log = tmp_path / "graphify.log"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(home / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "T")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "T")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@example.com")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_GRAPHIFY_LOG", str(log))
    monkeypatch.delenv("FAKE_GRAPHIFY_SLEEP", raising=False)
    return Sandbox(root=work, log=log, fake_bin=fake_bin, home=home)


def read_starts(log: Path) -> list[Start]:
    if not log.exists():
        return []
    out: list[Start] = []
    for line in log.read_text().splitlines():
        parts = line.split(" ")
        if parts[0] == "start":
            out.append(Start(parts[1], parts[2], Path(parts[3]), " ".join(parts[4:-1])))
    return out


def max_concurrent(log: Path) -> int:
    """The most rebuilds that were between their ``start`` and ``end`` at once."""
    running = peak = 0
    if not log.exists():
        return 0
    for line in log.read_text().splitlines():
        kind = line.split(" ", 1)[0]
        if kind == "start":
            running += 1
            peak = max(peak, running)
        elif kind == "end":
            running -= 1
    return peak


def wait_for(
    predicate: Callable[[], bool], *, timeout: float = 10.0, what: str = "condition"
) -> None:
    """Poll until ``predicate`` holds. Waits are for something to happen, never for nothing."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out after {timeout}s waiting for {what}")


def count_ends(log: Path) -> int:
    """How many rebuilds have finished."""
    if not log.exists():
        return 0
    return sum(1 for line in log.read_text().splitlines() if line.startswith("end "))


def idle(common: Path) -> bool:
    """No rebuild running or requested: the lock directory and the want flag are both gone."""
    return (
        not (common / f"{HELPER_NAME}.lock").exists()
        and not (common / f"{HELPER_NAME}.want").exists()
    )


def await_rebuilds(sb: Sandbox, common: Path, *, at_least: int = 1) -> list[Start]:
    """Wait until ``at_least`` rebuilds have run and the helper has gone idle; return the starts."""
    wait_for(
        lambda: len(sb.starts()) >= at_least and idle(common),
        what=f"{at_least} rebuild(s) to finish",
    )
    return sb.starts()


def dead_pid() -> int:
    """The pid of a process that has exited and been reaped."""
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


def have_shell(name: str) -> bool:
    return shutil.which(name) is not None
