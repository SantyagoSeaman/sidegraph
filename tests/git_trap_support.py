"""Build git traps for the trust-boundary tests. Everything stays under ``tmp_path``: the
planted command is the test interpreter running a script that creates one marker file."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from sidegraph.gitenv import git_env


def plant_marker(tmp_path: Path) -> tuple[str, Path]:
    """The command line to plant and the file it creates when git runs it."""
    marker = tmp_path / "trap-fired"
    script = tmp_path / "trap.py"
    script.write_text(
        "import pathlib, sys\n"
        f"pathlib.Path({str(marker)!r}).write_text('fired', encoding='utf-8')\n"
        "sys.exit(1)\n",
        encoding="utf-8",
    )
    command = f'"{Path(sys.executable).as_posix()}" "{script.as_posix()}"'
    return command, marker


def plant_git_dir(directory: Path, command: str) -> None:
    """Give ``directory`` the files of a git directory whose configuration runs ``command``."""
    (directory / "objects" / "info").mkdir(parents=True, exist_ok=True)
    (directory / "refs" / "heads").mkdir(parents=True, exist_ok=True)
    (directory / "objects" / "info" / "keep").write_text("", encoding="utf-8")
    (directory / "refs" / "heads" / ".keep").write_text("", encoding="utf-8")
    (directory / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    quoted = command.replace('"', '\\"')  # keep the quotes through git's config parser
    (directory / "config").write_text(
        "[core]\n\trepositoryformatversion = 0\n\tbare = false\n\tworktree = ..\n"
        f"\tfsmonitor = {quoted}\n[user]\n\tname = Planted Name\n",
        encoding="utf-8",
    )


def raw_git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Git with no protection at all: for fixtures and for the control run of a trap."""
    return subprocess.run(
        ["git", *args], cwd=cwd, env=git_env(), capture_output=True, text=True, check=False
    )


def trap_is_armed(directory: Path, marker: Path) -> bool:
    """Run plain ``git status`` in ``directory`` and say whether the trap fired. Removes the
    marker again, so the test starts clean."""
    raw_git(directory, "--no-optional-locks", "status", "--porcelain=v1")
    fired = marker.exists()
    marker.unlink(missing_ok=True)
    return fired


def git_version() -> tuple[int, int]:
    """``(major, minor)`` of the git on PATH; ``(0, 0)`` when it cannot be read."""
    found = re.search(r"(\d+)\.(\d+)", raw_git(Path.cwd(), "--version").stdout)
    return (int(found.group(1)), int(found.group(2))) if found else (0, 0)
