"""``import sidegraph.server`` must never touch the filesystem (N2 fold-in): the module
used to eagerly build a process-wide ``Store`` at import time
(``_store = Store(os.environ.get("SIDEGRAPH_DB", "sidegraph.db"))``), which materialized a
stray store as a side effect of a bare import. ``_get_store()`` is a lazy, memoized
accessor now -- nothing should appear on disk until a tool actually runs.

Run in a real subprocess (not just ``importlib.reload`` in-process) so this is a genuine
clean-room check: no other test's already-imported ``sidegraph.server`` module, cached
``_store`` global, or env var leaks into the result.
"""

from __future__ import annotations

import subprocess
import sys


def test_bare_import_creates_no_filesystem_entry(tmp_path):
    env = {"PATH": __import__("os").environ.get("PATH", "")}
    result = subprocess.run(
        [sys.executable, "-c", "import sidegraph.server"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert sorted(p.name for p in tmp_path.iterdir()) == []
    assert not (tmp_path / "sidegraph.db").exists()
    assert not (tmp_path / ".sidegraph").exists()


def test_bare_import_creates_no_filesystem_entry_with_legacy_sidegraph_db_set(tmp_path):
    """Even with the deprecated SIDEGRAPH_DB set, importing the module alone must not
    create anything -- only actually calling a tool (which resolves + opens the store)
    should."""
    import os

    env = {"PATH": os.environ.get("PATH", ""), "SIDEGRAPH_DB": "sub/decisions.db"}
    result = subprocess.run(
        [sys.executable, "-c", "import sidegraph.server"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert sorted(p.name for p in tmp_path.iterdir()) == []
