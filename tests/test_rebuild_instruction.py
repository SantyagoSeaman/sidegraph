"""Every rebuild instruction Sidegraph shows names GRAPHIFY_NO_AUTO_REFRESH=1, through one constant.

Recent Graphify releases rewrite the user's agent skill on every CLI run unless the variable is
set. Python text builds the instruction from ``githooks.GRAPH_REBUILD``; text that cannot import
it (an MCP tool docstring, a plugin skill) carries the same phrase verbatim. Comments are shown to
nobody and are not checked; the stats screen shows the bare ``GRAPH_REBUILD_COMMAND`` (80 columns).
# see design/superpowers/specs/2026-10-14-graphify-upgrade-design.md (D7)
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from sidegraph.githooks import GRAPH_REBUILD, GRAPH_REBUILD_COMMAND, GRAPH_REBUILD_ENV

_ROOT = Path(__file__).resolve().parents[1]
_OWNER = _ROOT / "src" / "sidegraph" / "githooks.py"
_MENTION = "graphify update"


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", text)


def _mentions_rebuild(node: ast.AST) -> bool:
    """Whether the constant text of a string (or f-string) node names the rebuild command."""
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str) and _MENTION in node.value
    if isinstance(node, ast.JoinedStr):
        literal = "".join(
            part.value
            for part in node.values
            if isinstance(part, ast.Constant) and isinstance(part.value, str)
        )
        return _MENTION in literal
    return False


def _carries_the_phrase(node: ast.AST) -> bool:
    """A plain string holds the whole phrase; an f-string interpolates ``GRAPH_REBUILD``."""
    if isinstance(node, ast.Constant):
        return _flat(GRAPH_REBUILD) in _flat(str(node.value))
    if isinstance(node, ast.JoinedStr):
        for part in node.values:
            if isinstance(part, ast.FormattedValue):
                target = part.value
                name = (
                    target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", "")
                )
                if name == "GRAPH_REBUILD":
                    return True
    return False


def _offenders_in(source: str) -> list[tuple[int, str]]:
    """``(line, why)`` for each string that names the rebuild without the phrase, and each use
    of ``GRAPH_REBUILD_COMMAND``. The AST joins adjacent literals, so ``"`graphify " "update .`"``
    is one string, and a docstring is reported on its first line."""
    tree = ast.parse(source)
    inner = {
        id(part)
        for node in ast.walk(tree)
        if isinstance(node, ast.JoinedStr)
        for part in node.values
    }
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if id(node) in inner:
            continue
        if _mentions_rebuild(node) and not _carries_the_phrase(node):
            found.append((node.lineno, "names the rebuild without the variable"))
        used = node.id if isinstance(node, ast.Name) else getattr(node, "attr", "")
        if isinstance(node, (ast.Name, ast.Attribute)) and used == "GRAPH_REBUILD_COMMAND":
            found.append((node.lineno, "uses GRAPH_REBUILD_COMMAND"))
    return sorted(found)


# The bare ``GRAPH_REBUILD_COMMAND`` is for compact human renders only: the 80-column stats screen
# (``stats/render.py``) and the ``fix`` field of the graph-stale problem (``integrity._GRAPH_FIX``).


def _python_offenders() -> list[str]:
    offenders: list[str] = []
    for path in sorted((_ROOT / "src" / "sidegraph").rglob("*.py")):
        if path == _OWNER:
            continue
        relative = path.relative_to(_ROOT / "src" / "sidegraph").as_posix()
        lines = path.read_text(encoding="utf-8").splitlines()
        for number, why in _offenders_in(path.read_text(encoding="utf-8")):
            if why.startswith("uses GRAPH_REBUILD_COMMAND"):
                if relative == "stats/render.py":
                    continue
                if relative == "integrity.py" and lines[number - 1].startswith("_GRAPH_FIX ="):
                    continue
            offenders.append(f"{path.relative_to(_ROOT)}:{number} {why}")
    return offenders


def _plugin_offenders() -> list[str]:
    offenders: list[str] = []
    for path in sorted((_ROOT / "plugin").rglob("*")):
        if not path.is_file() or path.suffix not in {".md", ".json"}:
            continue
        for block in re.split(r"\n[ \t]*\n", path.read_text(encoding="utf-8")):
            if _MENTION in block and _flat(GRAPH_REBUILD) not in _flat(block):
                offenders.append(f"{path.relative_to(_ROOT)}: {_flat(block.strip())[:70]}")
    return offenders


def test_the_phrase_names_the_variable_without_shell_syntax():
    assert GRAPH_REBUILD == (
        "`graphify update .` with the environment variable `GRAPHIFY_NO_AUTO_REFRESH=1` set"
    )
    assert f"`{GRAPH_REBUILD_COMMAND}`" in GRAPH_REBUILD
    assert f"`{GRAPH_REBUILD_ENV}`" in GRAPH_REBUILD


def test_every_python_string_that_names_the_rebuild_carries_the_variable():
    assert _python_offenders() == []


def test_every_plugin_paragraph_that_names_the_rebuild_carries_the_variable():
    assert _plugin_offenders() == []


def test_the_scanner_sees_a_split_literal_and_a_bare_command_use():
    split = 'x = ("`graphify " "update .`")\n'
    assert _offenders_in(split) == [(1, "names the rebuild without the variable")]
    bare = 'y = f"run `{GRAPH_REBUILD_COMMAND}`."\n'
    assert _offenders_in(bare) == [(1, "uses GRAPH_REBUILD_COMMAND")]
    good = 'y = f"by running {GRAPH_REBUILD} now"\nz = f"graphify update . {GRAPH_REBUILD}"\n'
    assert _offenders_in(good) == []
