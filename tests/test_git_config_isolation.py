"""Guards the hermetic git configuration (see ``conftest.py``): no test reads the developer's
real global or system git config. A ``sidegraph.graphRefresh=false`` or a ``core.hooksPath``
there changes what ``sidegraph-init`` installs and what the integrity checks report, so a test
that passes on a clean machine would fail on one that has either set.

The second test stands in for that developer: it hands a child pytest a global config holding
both, and the integrity replay (a real git repository, its own fixtures, no git config of its
own) must still pass.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_no_global_or_system_git_config_reaches_a_test():
    done = subprocess.run(
        ["git", "config", "--global", "--list"], capture_output=True, text=True, check=False
    )
    assert done.stdout == "", f"the developer's global git config leaked in:\n{done.stdout}"
    assert os.environ.get("GIT_CONFIG_NOSYSTEM") == "1"


def test_a_polluted_global_git_config_does_not_change_a_replay(tmp_path):
    polluted = tmp_path / "polluted-gitconfig"
    polluted.write_text(
        "[sidegraph]\n\tgraphRefresh = false\n[core]\n\thooksPath = /nonexistent/hooks\n"
    )
    env = {**os.environ, "GIT_CONFIG_GLOBAL": str(polluted)}
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "tests/test_integrity_replay.py",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stdout[-2000:]
