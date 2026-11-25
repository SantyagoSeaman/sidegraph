"""Move a host entry point to the project root, which the plugin manifests ask for with a flag.

The flag is ``--repo-root``. ``sidegraph-mcp`` and the five hook programs share one copy.
Standard library only, through ``gitenv``: the hook hot path imports this on every call
and never loads pydantic for the git top level.
see design/superpowers/specs/2026-11-03-claude-plugin-bundled-runtime-design.md (§4.6),
design/superpowers/specs/2026-10-13-sh-free-plugin-launch-design.md (D3) and
design/superpowers/specs/2026-11-15-hook-launch-form-design.md (D3)
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from ..gitenv import run_git

REPO_ROOT_FLAG = "--repo-root"


def take_repo_root_flag(argv: list[str]) -> bool:
    """Remove every ``--repo-root`` from ``argv`` in place; say whether one was there.

    Called first by each entry, so the flag never reaches the entry's own argument handling
    (``refuse_arguments`` still refuses anything else).
    """
    present = REPO_ROOT_FLAG in argv
    if present:
        argv[:] = [a for a in argv if a != REPO_ROOT_FLAG]
    return present


def anchor_to_project(*, honor_claude_project_dir: bool = True) -> None:
    """Move the process to the project root.

    ``CLAUDE_PROJECT_DIR`` wins when honored, set, non-blank and an existing directory (what the
    old ``cd "${CLAUDE_PROJECT_DIR}"`` launcher did). Otherwise the git top-level of the cwd
    applies (what the old Codex ``cd "$(git rev-parse --show-toplevel)"`` did), and outside a
    repository nothing moves. Anchoring never raises: the entry runs wherever it is.
    """
    project = os.environ.get("CLAUDE_PROJECT_DIR", "").strip() if honor_claude_project_dir else ""
    try:
        target = Path(project) if project and Path(project).is_dir() else _git_top_level()
        os.chdir(target)
    except Exception:  # noqa: BLE001 - anchoring must never stop the entry point
        return


def _git_top_level() -> Path:
    """The git top level of the cwd, else the resolved cwd: the answer of
    ``claude_settings.repo_root_for_settings()`` without importing it.

    That module pulls in pydantic and the engine reader, about 75 ms on every hook run of a host
    that sets no ``CLAUDE_PROJECT_DIR``. The same ``git rev-parse --show-toplevel`` through the
    same standard-library launcher (``gitenv``) gives the same answer in a repository, a linked
    worktree and a ``.git`` file; a failed git, an unusable git or no repository leaves the
    process in its resolved cwd, as that function does. Like that function, it sets no timeout
    on git.
    see design/superpowers/specs/2026-11-15-hook-launch-form-design.md (D3)
    """
    try:
        done = run_git(
            ["rev-parse", "--show-toplevel"],
            Path.cwd(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="surrogateescape",
        )
        if done.returncode == 0 and done.stdout.strip():
            return Path(done.stdout.strip()).resolve()
    except (ValueError, OSError):
        pass
    return Path.cwd().resolve()
