"""No test file outside ``tests/private/`` imports from it.

``tests/private/`` is the release-only slow set: PR runs do not collect it and the public
snapshot does not ship it, so a public file importing from it would break both. Shared helpers
live in support modules directly under ``tests/``.
"""

from __future__ import annotations

import ast
from pathlib import Path

_TESTS = Path(__file__).resolve().parent
_PRIVATE = "tests.private"


def _imported_modules(tree: ast.AST) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            modules.add(base)
            # `from tests import private` names the package through an alias.
            modules.update(f"{base}.{alias.name}" for alias in node.names)
            if node.level:  # `from .private import x` inside tests/
                modules.add(f"tests.{base}")
    return modules


def _reaches_private(module: str) -> bool:
    return module == _PRIVATE or module.startswith(_PRIVATE + ".")


def test_no_file_outside_tests_private_imports_from_it() -> None:
    offenders = [
        path.relative_to(_TESTS).as_posix()
        for path in sorted(_TESTS.rglob("*.py"))
        if "private" not in path.relative_to(_TESTS).parts[:-1]
        if any(
            _reaches_private(m)
            for m in _imported_modules(ast.parse(path.read_text(encoding="utf-8")))
        )
    ]
    assert not offenders, f"imports from tests/private: {offenders}"


def test_the_scan_sees_the_import_forms_it_guards() -> None:
    """The guard bites: each spelling of an import from the package is recognised."""
    for source in (
        "import tests.private.x",
        "from tests.private import x",
        "from tests.private.x import y",
        "from tests import private",
    ):
        assert any(_reaches_private(m) for m in _imported_modules(ast.parse(source))), source
    assert not any(
        _reaches_private(m)
        for m in _imported_modules(ast.parse("from tests.store_support import x"))
    )
