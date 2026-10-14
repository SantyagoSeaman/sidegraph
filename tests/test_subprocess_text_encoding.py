"""No subprocess call decodes its output with the platform code page.

``subprocess.run(..., text=True)`` without ``encoding=`` decodes with the ANSI code page on
Windows (cp1252), while git writes UTF-8: a Cyrillic author name turns into mojibake, or raises
for bytes such as 0x81 and 0x8D. The guard parses every module under ``src/sidegraph``. A
``subprocess.<fn>`` call is text mode when it sets ``text=`` or ``universal_newlines=`` to
anything but a literal ``False``, or sets ``errors=`` at all (``errors`` alone also switches
text mode on); such a call must set ``encoding=`` to something other than a literal ``None``.
A non-literal value (``text=text``) is flagged too. The check keys on ``subprocess.<fn>``
attribute calls, so ``import subprocess as <alias>`` and ``from subprocess import ...`` are
flagged outright: they would hide a call from it.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src" / "sidegraph"


def _is_literal(node: ast.expr, value: object) -> bool:
    return isinstance(node, ast.Constant) and node.value is value


def _is_subprocess_call(node: ast.Call) -> bool:
    f = node.func
    return (
        isinstance(f, ast.Attribute)
        and isinstance(f.value, ast.Name)
        and f.value.id == "subprocess"
    )


def _violations(source: str) -> list[int]:
    """Line numbers in ``source`` that decode subprocess output with the platform default, or
    hide a subprocess call from this check."""
    found: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            if any(a.name == "subprocess" and a.asname for a in node.names):
                found.append(node.lineno)
        elif isinstance(node, ast.ImportFrom):
            if node.module == "subprocess":
                found.append(node.lineno)
        elif isinstance(node, ast.Call) and _is_subprocess_call(node):
            kws = {k.arg: k.value for k in node.keywords if k.arg}
            text_flags = [kws[n] for n in ("text", "universal_newlines") if n in kws]
            text_mode = "errors" in kws or any(not _is_literal(v, False) for v in text_flags)
            has_encoding = "encoding" in kws and not _is_literal(kws["encoding"], None)
            if text_mode and not has_encoding:
                found.append(node.lineno)
    return found


def _offenders() -> list[str]:
    found: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        found += [f"{path.relative_to(SRC.parent.parent)}:{n}" for n in _violations(source)]
    return found


def test_text_mode_subprocess_calls_name_an_encoding() -> None:
    assert _offenders() == [], "text mode without encoding= decodes with cp1252"


@pytest.mark.parametrize(
    "source",
    [
        "import subprocess\nsubprocess.run(['git'], text=True)\n",
        "import subprocess\nsubprocess.run(['git'], universal_newlines=True)\n",
        "import subprocess\nsubprocess.run(['git'], text=flag)\n",
        "import subprocess\nsubprocess.run(['git'], errors='replace')\n",
        "import subprocess\nsubprocess.run(['git'], text=True, encoding=None)\n",
        "import subprocess\nsubprocess.run(['git'], errors='strict', encoding=None)\n",
        "import subprocess as sp\nsp.run(['git'], text=True)\n",
        "from subprocess import run\nrun(['git'], text=True)\n",
        "from subprocess import PIPE\n",
    ],
    ids=[
        "text",
        "universal_newlines",
        "non-literal-text",
        "errors-without-encoding",
        "encoding-none",
        "errors-and-encoding-none",
        "import-alias",
        "from-import-call",
        "from-import-anything",
    ],
)
def test_the_checker_flags(source: str) -> None:
    assert _violations(source) != []


@pytest.mark.parametrize(
    "source",
    [
        "import subprocess\nsubprocess.run(['git'], text=True, encoding='utf-8')\n",
        "import subprocess\nsubprocess.run(['git'], encoding='utf-8', errors='strict')\n",
        "import subprocess\nsubprocess.run(['git'], text=False)\n",
        "import subprocess\nsubprocess.run(['git'], capture_output=True)\n",
        "import subprocess\nsubprocess.run(['git'], text=False, encoding=None)\n",
    ],
    ids=["text-and-encoding", "encoding-and-errors", "text-false", "bytes", "bytes-encoding-none"],
)
def test_the_checker_passes(source: str) -> None:
    assert _violations(source) == []
