"""Move a host entry point to the project root, which the plugin manifests ask for with a flag.

The flag is ``--repo-root``. Shared by ``sidegraph-mcp`` and the four hook entries, so there is
one copy. Standard library only at module level: the hook hot path imports this on every call,
and the settings helper (which pulls in pydantic) is imported inside the one branch that needs it.
see design/superpowers/specs/2026-11-03-claude-plugin-bundled-runtime-design.md (§4.6) and
design/superpowers/specs/2026-10-13-sh-free-plugin-launch-design.md (D3)
"""

from __future__ import annotations

import os
from pathlib import Path

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


def anchor_to_project() -> None:
    """Move the process to the project root.

    ``CLAUDE_PROJECT_DIR`` wins when it is set, non-blank and an existing directory (what the
    old ``cd "${CLAUDE_PROJECT_DIR}"`` launcher did). Otherwise the git top-level of the cwd
    applies (what the old Codex ``cd "$(git rev-parse --show-toplevel)"`` did), and outside a
    repository nothing moves. Anchoring never raises: the entry runs wherever it is.
    """
    project = os.environ.get("CLAUDE_PROJECT_DIR", "").strip()
    try:
        if project and Path(project).is_dir():
            target = Path(project)
        else:
            # Lazy: pydantic is behind this import, and the hook hot path avoids it.
            from .claude_settings import repo_root_for_settings

            target = repo_root_for_settings()
        os.chdir(target)
    except Exception:  # noqa: BLE001 - anchoring must never stop the entry point
        return
