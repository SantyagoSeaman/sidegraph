"""The package root imports nothing heavy (T1).

Importing any submodule runs ``sidegraph/__init__.py`` first. It used to import ``schema``
(pydantic, ulid) and ``store`` eagerly, so a hook that needed only ``sidegraph.config`` paid
for the models on every tool call. The re-exported names now resolve on first access.

see design/superpowers/specs/2026-10-03-hot-path-light-index-design.md (D1)
"""

from __future__ import annotations

import json
import subprocess
import sys


def test_importing_the_hooks_module_leaves_the_heavy_modules_out():
    """Red against an eager ``__init__``: importing any submodule ran ``sidegraph/__init__``,
    which imported ``schema`` (pydantic, ulid) and ``store`` eagerly."""
    probe = (
        "import json, sys\n"
        "import sidegraph.host.hooks\n"
        "loaded = [m for m in ('pydantic', 'ulid', 'sidegraph.schema', 'sidegraph.store')\n"
        "          if m in sys.modules]\n"
        "sys.stdout.write(json.dumps(loaded))\n"
    )
    done = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == []


def test_the_re_exported_names_still_resolve_through_the_lazy_root():
    probe = (
        "import sidegraph\n"
        "from sidegraph import Store, Decision, SCHEMA_VERSION\n"
        "import sidegraph.schema, sidegraph.store\n"
        "assert Store is sidegraph.store.Store\n"
        "assert Decision is sidegraph.schema.Decision\n"
        "assert SCHEMA_VERSION == sidegraph.schema.SCHEMA_VERSION\n"
        "assert set(sidegraph.__all__) <= set(dir(sidegraph))\n"
        "try:\n"
        "    sidegraph.no_such_name\n"
        "except AttributeError:\n"
        "    pass\n"
        "else:\n"
        "    raise SystemExit('a missing name must raise AttributeError')\n"
    )
    done = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
