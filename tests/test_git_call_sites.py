"""Every git command goes through ``gitenv.run_git``: nothing else may spell a git argv or
start a process in ``gitenv.py``.

The scan is static and looks for the literal word ``git``. It follows ``import subprocess as
sp`` and ``from subprocess import run``, covers every function that starts a process
(``subprocess``, ``os.system``, ``os.popen``, ``os.exec*``, ``os.spawn*``,
``asyncio.create_subprocess_*``) and reads a string that contains ``git`` as a command, a
``shlex.split("git …")`` and a ``shutil.which("git")`` as well as an argv list. Declared limit:
an argv assembled at run time without the literal (``tool = "git"; [tool, …]``, a name read
from a setting) is invisible to it.
# see design/superpowers/specs/2026-11-12-trust-boundary-clone-content-design.md (D6)"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "sidegraph"
PLUGIN = Path(__file__).resolve().parents[1] / "plugin"
LAUNCHERS = {
    "subprocess": {
        "run",
        "Popen",
        "call",
        "check_call",
        "check_output",
        "getoutput",
        "getstatusoutput",
    },
    "os": {"system", "popen", "posix_spawn", "posix_spawnp"},
    "asyncio": {"create_subprocess_exec", "create_subprocess_shell"},
}
_GIT_COMMAND = re.compile(r"(^|[\s;&|(`])git(\s|$)")
# The modules that start git today. A module may be added; removing one means its git
# calls are gone, so remove it here in the same change.
CALLERS = {
    "capture.py",
    "checkout_window.py",
    "drift_checks.py",
    "engine/reader.py",
    "githooks.py",
    "gitio.py",
    "host/claude_settings.py",
    "host/hooks.py",
    "integrity.py",
    "maintenance_clock.py",
    "store.py",
    "store_format.py",
    "verify.py",
    "worktree_seeds.py",
}


def _trees() -> dict[str, ast.Module]:
    return {
        p.relative_to(SRC).as_posix(): ast.parse(p.read_text(encoding="utf-8"))
        for p in sorted(SRC.rglob("*.py"))
    }


def _run_git_lines(trees: dict[str, ast.Module]) -> range:
    """The line span of the function ``run_git`` in ``gitenv.py``: the one allowed place."""
    for node in trees.get("gitenv.py", ast.Module(body=[], type_ignores=[])).body:
        if isinstance(node, ast.FunctionDef) and node.name == "run_git":
            return range(node.lineno, (node.end_lineno or node.lineno) + 1)
    return range(0)


def _is_launcher_name(module: str, name: str) -> bool:
    return name in LAUNCHERS[module] or (module == "os" and name.startswith(("exec", "spawn")))


def _launcher_names(tree: ast.Module) -> tuple[dict[str, str], set[str]]:
    """The local names that stand for a launcher module (``sp`` for ``subprocess``) and the
    launcher functions imported by name (``run``, ``system as s``)."""
    modules: dict[str, str] = {}
    direct: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in LAUNCHERS:
                    modules[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module in LAUNCHERS:
            for alias in node.names:
                if _is_launcher_name(node.module, alias.name):
                    direct.add(alias.asname or alias.name)
    return modules, direct


def _is_launcher(node: ast.AST, names: tuple[dict[str, str], set[str]]) -> bool:
    modules, direct = names
    if isinstance(node, ast.Name):
        return node.id in direct
    return (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id in modules
        and _is_launcher_name(modules[node.value.id], node.attr)
    )


def _mentions_git(node: ast.AST) -> bool:
    """A string constant anywhere below ``node`` that is the word ``git`` or a command line
    that runs it."""
    return any(
        isinstance(n, ast.Constant) and isinstance(n.value, str) and _GIT_COMMAND.search(n.value)
        for n in ast.walk(node)
    )


def git_argv_displays(trees: dict[str, ast.Module]) -> list[tuple[str, int]]:
    found = []
    for name, tree in trees.items():
        for node in ast.walk(tree):
            if isinstance(node, (ast.List, ast.Tuple)) and node.elts:
                first = node.elts[0]
                if isinstance(first, ast.Constant) and first.value == "git":
                    found.append((name, node.lineno))
    return found


def outside_run_git(trees: dict[str, ast.Module]) -> list[str]:
    allowed = _run_git_lines(trees)
    return [
        f"{name}:{line}"
        for name, line in git_argv_displays(trees)
        if not (name == "gitenv.py" and line in allowed)
    ]


def test_only_run_git_spells_a_git_argv():
    trees = _trees()
    assert _run_git_lines(trees), "gitenv.run_git is gone"
    assert git_argv_displays(trees), "run_git no longer spells the argv: the scan is blind"
    assert not outside_run_git(trees), outside_run_git(trees)


def launches_git(trees: dict[str, ast.Module]) -> list[str]:
    """Calls of a process launcher whose arguments mention git as a command, anywhere."""
    allowed = _run_git_lines(trees)
    found = []
    for name, tree in trees.items():
        names = _launcher_names(tree)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and _is_launcher(node.func, names)):
                continue
            arguments = [*node.args, *(k.value for k in node.keywords)]
            if any(_mentions_git(a) for a in arguments) and not (
                name == "gitenv.py" and node.lineno in allowed
            ):
                found.append(f"{name}:{node.lineno}")
    return found


def launchers_in_gitenv(trees: dict[str, ast.Module]) -> list[str]:
    allowed = _run_git_lines(trees)
    names = _launcher_names(trees["gitenv.py"])
    return [
        f"gitenv.py:{node.lineno}"
        for node in ast.walk(trees["gitenv.py"])
        if _is_launcher(node, names) and node.lineno not in allowed
    ]


def test_no_launcher_is_given_a_git_command():
    assert not launches_git(_trees()), launches_git(_trees())


def test_gitenv_starts_a_process_only_inside_run_git():
    trees = _trees()
    assert not launchers_in_gitenv(trees), launchers_in_gitenv(trees)


_EMPTY_GITENV = ast.Module(body=[], type_ignores=[])
BYPASSES = {
    "shell string": 'import subprocess\nsubprocess.run("git status", shell=True)',
    "imported by name": 'from subprocess import run\nrun("git status", shell=True)',
    "module alias": 'import subprocess as sp\nsp.run("git status".split())',
    "shlex": 'import shlex, subprocess\nsubprocess.run(shlex.split("git status"))',
    "which": 'import shutil, subprocess\nsubprocess.run([shutil.which("git") or "g", "status"])',
    "sh -c": 'import subprocess\nsubprocess.run(["sh", "-c", "git status"])',
    "asyncio": 'import asyncio\nasyncio.create_subprocess_exec("git", "status")',
    "asyncio shell": 'import asyncio\nasyncio.create_subprocess_shell("git status")',
    "os.execvp": 'import os\nos.execvp("git", ["git", "status"])',
    "os.spawnlp": 'import os\nos.spawnlp(os.P_WAIT, "git", "git", "status")',
    "os.system": 'import os\nos.system(f"git status {x}")',
    "os.popen": 'import os\nos.popen("git log")',
    "getoutput": 'import subprocess\nsubprocess.getoutput("git status")',
    "getstatusoutput": 'import subprocess\nsubprocess.getstatusoutput("git status")',
    "check_output in a pipeline": (
        'import subprocess\nsubprocess.check_output(f"cd {d} && git status", shell=True)'
    ),
    "imported and renamed": 'from os import system as s\ns("git log")',
    "Popen": 'import subprocess\nsubprocess.Popen(["git", "status"])',
}
NOT_GIT = {
    "another tool": 'import subprocess\nsubprocess.run(["graphify", "update"])',
    "a word that contains git": 'import subprocess\nsubprocess.run("digit gitk")',
    "git outside the launcher": (
        'import subprocess\nsubprocess.run(["tool"], env={"X": "x"})\nprint("git")'
    ),
}


@pytest.mark.parametrize("source", BYPASSES.values(), ids=BYPASSES.keys())
def test_the_scan_sees_a_bypass(source):
    assert launches_git({"x.py": ast.parse(source), "gitenv.py": _EMPTY_GITENV})


@pytest.mark.parametrize("source", NOT_GIT.values(), ids=NOT_GIT.keys())
def test_the_scan_leaves_other_launches_alone(source):
    assert not launches_git({"x.py": ast.parse(source), "gitenv.py": _EMPTY_GITENV})


def test_no_shipped_plugin_script_starts_git_itself():
    """The plugin's Python scripts run outside the package, so they get the same scan: a git
    launch there would read configuration a clone brings."""
    trees = {
        p.relative_to(PLUGIN).as_posix(): ast.parse(p.read_text(encoding="utf-8"))
        for p in sorted(PLUGIN.rglob("*.py"))
    }
    assert "sidegraph/skills/health-check/scripts/collect.py" in trees, "the scan is blind"
    assert not launches_git(trees), launches_git(trees)


def test_every_git_caller_uses_run_git():
    users = set()
    for name, tree in _trees().items():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                f = node.func
                called = f.id if isinstance(f, ast.Name) else getattr(f, "attr", "")
                if called == "run_git":
                    users.add(name)
    assert users >= CALLERS, sorted(CALLERS - users)
