"""Only ``sidegraph-init`` may write the project-root ``.ignore``: no Store, sync, hook or
server code imports the helper (the standing explicit-command rule).

# see design/superpowers/specs/2026-11-07-root-ignore-for-store-design.md (D1)
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "sidegraph"


def _imports_the_helper(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module.endswith("search_ignore"):
                return True
            relative_or_package = module in ("", "sidegraph") or node.level
            if relative_or_package and any(a.name == "search_ignore" for a in node.names):
                return True
        elif isinstance(node, ast.Import) and any(
            a.name.endswith("search_ignore") for a in node.names
        ):
            return True
    return False


def test_only_the_cli_imports_the_helper():
    """Red target: a version that calls the helper from the Store, sync, hooks or the server."""
    importers = set()
    for path in SRC.rglob("*.py"):
        if path.name == "search_ignore.py":
            continue
        if _imports_the_helper(ast.parse(path.read_text(encoding="utf-8"))):
            importers.add(path.relative_to(SRC.parents[1]).as_posix())
    assert importers == {"src/sidegraph/cli.py"}
