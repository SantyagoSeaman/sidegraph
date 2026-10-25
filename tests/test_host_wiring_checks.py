"""Host wiring that silently switches memory off becomes visible: the SessionStart version line,
the ``version-skew`` check and the ``plugin-off-in-subdirectories`` check that reaches doctor.

User settings are read through ``HOME``, which every test points at ``tmp_path``, and
``CLAUDE_CONFIG_DIR`` is unset: nothing here reads the real ``~/.claude/settings.json`` or the
real config directory. The repositories are real (``git init``), because the
directory walk starts from ``git ls-files``. A clean case asserts the check RAN (its id is in the
result's ``clean`` set), not merely that it produced no problem.
see design/superpowers/specs/2026-10-03-host-wiring-checks-design.md (T1-T5)
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

import sidegraph
import sidegraph.host.hooks as hooks
from sidegraph import integrity
from sidegraph.cli import doctor_main
from sidegraph.config import StoreLocation
from sidegraph.store import Store
from tests.store_format2_support import format1_store

VERSION_SKEW = "version-skew"
PLUGIN_OFF = "plugin-off-in-subdirectories"


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The user's home for the test: ``Path.home()`` reads ``HOME``. No test reads the real one."""
    path = tmp_path / "home"
    path.mkdir()
    monkeypatch.setenv("HOME", str(path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    monkeypatch.delenv("PLUGIN_ROOT", raising=False)
    return path


def _write(path: Path, text: str = "x\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _settings(path: Path, plugins: dict[str, bool] | None = None, **other: object) -> None:
    """A Claude Code settings file; ``plugins`` becomes ``enabledPlugins``."""
    body: dict[str, object] = dict(other)
    if plugins is not None:
        body["enabledPlugins"] = plugins
    _write(path, json.dumps(body) + "\n")


def _user(home: Path, plugins: dict[str, bool]) -> None:
    _settings(home / ".claude" / "settings.json", plugins)


def _repo(tmp_path: Path, *, files: tuple[str, ...] = ("src/a.py", "pkg/b.py")) -> Path:
    """A real git repository with ``files`` added to its index."""
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    for rel in files:
        _write(root / rel)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    return root


def _run(repo: Path, surface: str = "doctor") -> integrity.RunResult:
    """The registry over the host checks only, for a store at ``<repo>/.sidegraph``."""
    store_dir = repo / ".sidegraph"
    location = StoreLocation(str(store_dir), str(repo))
    inputs = integrity.Inputs(store_dir=store_dir, now=datetime.now(UTC))
    return integrity.run(inputs, surface, hooks.host_checks(location))  # type: ignore[arg-type]


def _problem(result: integrity.RunResult, check: str) -> integrity.Problem:
    (problem,) = [p for p in result.problems if p.check == check]
    return problem


def _details(problem: integrity.Problem) -> list[str]:
    return [detail for _path, detail in problem.findings]


# -- T1: the SessionStart version line ------------------------------------------------------


def _session_start_text(tmp_path: Path, monkeypatch, capsys) -> str:
    monkeypatch.setenv("SIDEGRAPH_DB", str(tmp_path / "s.db"))
    monkeypatch.setenv("SIDEGRAPH_GRAPH", str(tmp_path / "no-graph.json"))
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    hooks.session_start()
    out = json.loads(capsys.readouterr().out)
    return out["hookSpecificOutput"]["additionalContext"]


def test_t1_session_start_names_its_version_once_after_the_standing_instruction(
    tmp_path, monkeypatch, capsys
):
    """Red: the line is missing."""
    ctx = _session_start_text(tmp_path, monkeypatch, capsys)

    line = f"Sidegraph {sidegraph.__version__}"
    assert ctx.count(line) == 1
    assert ctx.startswith(f"{hooks.STANDING_SEARCH_INSTRUCTION}\n\n{line}\n\n")


# -- T2: version-skew ------------------------------------------------------------------------


def _plugin_root(tmp_path: Path, version: str, *, folder: str = ".claude-plugin", name="sidegraph"):
    root = tmp_path / "plugin"
    _write(root / folder / "plugin.json", json.dumps({"name": name, "version": version}))
    return root


def _skew(
    tmp_path: Path, monkeypatch, *, package: str, plugin: str, **kwargs
) -> integrity.RunResult:
    monkeypatch.setattr(sidegraph, "__version__", package)
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(_plugin_root(tmp_path, plugin, **kwargs)))
    return _run(tmp_path / "repo", "session")


def test_t2_equal_versions_are_clean_and_the_check_ran(tmp_path, monkeypatch):
    _repo(tmp_path)

    result = _skew(tmp_path, monkeypatch, package="0.9.0", plugin="0.9.0")

    assert VERSION_SKEW in result.clean
    assert [p.check for p in result.problems if p.check == VERSION_SKEW] == []


def test_t2_a_plugin_newer_than_the_package_says_to_restart_or_drop_the_pinned_install(
    tmp_path, monkeypatch
):
    """0.10.0 is newer than 0.9.0: a string comparison says the opposite (mutation M1)."""
    _repo(tmp_path)

    result = _skew(tmp_path, monkeypatch, package="0.9.0", plugin="0.10.0")

    problem = _problem(result, VERSION_SKEW)
    assert problem.severity == "advisory"
    assert problem.line is not None and problem.notice is not None
    assert "plugin is 0.10.0" in problem.line and "package is 0.9.0" in problem.line
    assert "restart the session" in problem.line.lower()
    assert "pinned install" in problem.line
    assert "marketplace" not in problem.line


def test_t2_a_package_newer_than_the_plugin_says_to_update_the_plugin(tmp_path, monkeypatch):
    _repo(tmp_path)

    result = _skew(tmp_path, monkeypatch, package="0.10.0", plugin="0.9.0")

    problem = _problem(result, VERSION_SKEW)
    assert "package is 0.10.0" in problem.line and "plugin is 0.9.0" in problem.line
    assert "stale" in problem.line
    assert "marketplace" in problem.line
    assert "restart the session" not in problem.line.lower()


def test_t2_a_local_label_is_ignored(tmp_path, monkeypatch):
    _repo(tmp_path)

    result = _skew(tmp_path, monkeypatch, package="0.9.0+local", plugin="0.9.0")

    assert VERSION_SKEW in result.clean


def test_t2_codex_sets_plugin_root_alone_and_ships_the_codex_manifest(tmp_path, monkeypatch):
    _repo(tmp_path)
    monkeypatch.setattr(sidegraph, "__version__", "0.9.0")
    root = _plugin_root(tmp_path, "0.10.0", folder=".codex-plugin")
    monkeypatch.setenv("PLUGIN_ROOT", str(root))

    result = _run(tmp_path / "repo", "session")

    assert "0.10.0" in _problem(result, VERSION_SKEW).line


def test_t2_claude_plugin_root_wins_over_plugin_root(tmp_path, monkeypatch):
    _repo(tmp_path)
    monkeypatch.setattr(sidegraph, "__version__", "0.9.0")
    ours = _plugin_root(tmp_path, "0.9.0")
    other = tmp_path / "other"
    _write(
        other / ".claude-plugin" / "plugin.json",
        json.dumps({"name": "sidegraph", "version": "0.1.0"}),
    )
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(ours))
    monkeypatch.setenv("PLUGIN_ROOT", str(other))

    assert VERSION_SKEW in _run(tmp_path / "repo", "session").clean


@pytest.mark.parametrize(
    "case", ["no-root", "no-manifest", "malformed", "other-name", "no-version"]
)
def test_t2_without_evidence_the_check_is_not_run(tmp_path, monkeypatch, case):
    """Not run is neither a problem nor a clean id."""
    _repo(tmp_path)
    monkeypatch.setattr(sidegraph, "__version__", "0.9.0")
    root = tmp_path / "plugin"
    root.mkdir()
    if case != "no-root":
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(root))
    if case == "malformed":
        _write(root / ".claude-plugin" / "plugin.json", "{not json")
    elif case == "other-name":
        _write(
            root / ".claude-plugin" / "plugin.json", json.dumps({"name": "x", "version": "0.1.0"})
        )
    elif case == "no-version":
        _write(root / ".claude-plugin" / "plugin.json", json.dumps({"name": "sidegraph"}))

    result = _run(tmp_path / "repo", "session")

    assert VERSION_SKEW not in result.clean
    assert all(p.check != VERSION_SKEW for p in result.problems)


def test_t2_the_session_hook_puts_the_skew_line_in_the_context_and_a_notice_for_the_human(
    tmp_path, monkeypatch, capsys
):
    repo = _repo(tmp_path)
    monkeypatch.setattr(sidegraph, "__version__", "0.9.0")
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(_plugin_root(tmp_path, "0.10.0")))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(repo))
    Store(format1_store(repo / ".sidegraph")).close()
    monkeypatch.setenv("SIDEGRAPH_DIR", str(repo / ".sidegraph"))
    monkeypatch.setenv("SIDEGRAPH_GRAPH", str(tmp_path / "no-graph.json"))
    monkeypatch.setattr("sys.stdin", io.StringIO(""))

    hooks.session_start()

    out = json.loads(capsys.readouterr().out)
    assert "plugin is 0.10.0" in out["hookSpecificOutput"]["additionalContext"]
    assert "plugin is 0.10.0" in out["systemMessage"]


# -- path-skew: a foreign `sidegraph-*` on PATH beside the plugin
# see design/superpowers/specs/2026-10-11-stale-path-sidegraph-design.md

PATH_SKEW = "path-skew"
_REAL_PATH_INSTALL = hooks._path_install


@pytest.fixture
def real_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """The conftest stubs the PATH probe (so no session-start test sees the developer's own
    ``PATH``); the path-skew tests put the real one back."""
    monkeypatch.setattr(hooks, "_path_install", _REAL_PATH_INSTALL)


def _tool_env(root: Path, version: str | None, *, receipt: bool = True, name: str = "tool") -> Path:
    """A ``uv tool``-shaped environment: ``<root>/<name>/bin/sidegraph-*``, its dist-info and
    receipt. Returns the ``bin`` directory a person would have on ``PATH`` (symlinks into it)."""
    venv = root / name
    (venv / "bin").mkdir(parents=True)
    for command in ("sidegraph-session-start", "sidegraph-mcp", "sidegraph-doctor"):
        _write(venv / "bin" / command, "#!/bin/sh\n")
        (venv / "bin" / command).chmod(0o755)
    if version is not None:
        _write(
            venv
            / "lib"
            / "python3.13"
            / "site-packages"
            / f"sidegraph-{version}.dist-info"
            / "METADATA"
        )
    if receipt:
        _write(venv / "uv-receipt.toml")
    link = root / f"{name}-link-bin"
    link.mkdir()
    for command in (venv / "bin").iterdir():
        (link / command.name).symlink_to(command)
    return link


def _skew_on_path(monkeypatch, *path_dirs: Path, running: str = "0.11.1") -> integrity.RunResult:
    monkeypatch.setattr(sidegraph, "__version__", running)
    monkeypatch.setenv("PATH", os.pathsep.join(str(d) for d in path_dirs))
    monkeypatch.setattr(hooks, "_own_bin_dirs", lambda: set())
    return _run(Path(os.environ["REPO_FOR_PATH_SKEW"]), "session")


@pytest.fixture
def repo_dir(tmp_path: Path, monkeypatch) -> Path:
    repo = _repo(tmp_path)
    monkeypatch.setenv("REPO_FOR_PATH_SKEW", str(repo))
    return repo


def test_path_skew_older_path_install_is_reported(tmp_path, monkeypatch, repo_dir, real_probe):
    link = _tool_env(tmp_path, "0.6.0")

    result = _skew_on_path(monkeypatch, link)

    problem = _problem(result, PATH_SKEW)
    assert problem.severity == "advisory"
    assert problem.line == problem.notice
    assert problem.line == (
        f"Sidegraph: `sidegraph-session-start` on PATH ({link}) is 0.6.0 but this session runs "
        "0.11.1. Plugin skills and hooks use the session's version through uvx; your own shell "
        "commands use the old one. Update it with `uv tool install --reinstall "
        "git+https://github.com/SantyagoSeaman/sidegraph.git@main`, or "
        "remove it with `uv tool uninstall sidegraph` if you only use the plugin."
    )


def test_path_skew_a_newer_path_install_is_reported_too(
    tmp_path, monkeypatch, repo_dir, real_probe
):
    link = _tool_env(tmp_path, "0.12.0")

    problem = _problem(_skew_on_path(monkeypatch, link), PATH_SKEW)

    assert problem.line is not None
    assert "is 0.12.0 but this session runs 0.11.1" in problem.line
    assert "your own shell commands use the newer one." in problem.line


def test_path_skew_without_a_uv_receipt_the_tail_is_generic(
    tmp_path, monkeypatch, repo_dir, real_probe
):
    link = _tool_env(tmp_path, "0.6.0", receipt=False)

    problem = _problem(_skew_on_path(monkeypatch, link), PATH_SKEW)

    assert problem.line is not None
    assert problem.line.endswith("Upgrade it, or remove it if you only use the plugin.")
    assert "uv tool" not in problem.line


def _pretend_running(monkeypatch, tmp_path: Path) -> None:
    """Make ``<tmp>/running`` the running environment through the two inputs the real
    ``_own_bin_dirs()`` reads, both reached by a symlink so that its ``realpath`` is exercised."""
    prefix = tmp_path / "prefix-link"
    prefix.symlink_to(tmp_path / "running")
    scripts = tmp_path / "scripts-link"
    scripts.symlink_to(tmp_path / "running" / "bin")
    monkeypatch.setattr(sys, "prefix", str(prefix))
    monkeypatch.setattr(hooks.sysconfig, "get_path", lambda name, *a, **k: str(scripts))


def test_path_skew_skips_the_running_environments_own_bin(
    tmp_path, monkeypatch, repo_dir, real_probe
):
    """uvx and `uv run` put their environment's bin first on PATH: the check must look past it
    to the stale copy (review H1), not stop at its own binary and go clean."""
    _tool_env(tmp_path, "0.11.1", name="running")
    own = tmp_path / "running" / "bin"  # uvx / uv run put the venv's own bin on PATH
    stale = _tool_env(tmp_path, "0.6.0", name="stale")
    monkeypatch.setattr(sidegraph, "__version__", "0.11.1")
    monkeypatch.setenv("PATH", os.pathsep.join([str(own), str(stale)]))
    _pretend_running(monkeypatch, tmp_path)

    problem = _problem(_run(repo_dir, "session"), PATH_SKEW)

    assert problem.line is not None
    assert f"({stale})" in problem.line and "is 0.6.0" in problem.line


def test_path_skew_the_running_environment_alone_is_not_run(
    tmp_path, monkeypatch, repo_dir, real_probe
):
    _tool_env(tmp_path, "0.11.1", name="running")
    own = tmp_path / "running" / "bin"
    monkeypatch.setattr(sidegraph, "__version__", "0.11.1")
    monkeypatch.setenv("PATH", str(own))
    _pretend_running(monkeypatch, tmp_path)

    result = _run(repo_dir, "session")

    assert PATH_SKEW not in result.clean
    assert all(p.check != PATH_SKEW for p in result.problems)


def test_path_skew_equal_version_is_clean_and_ran(tmp_path, monkeypatch, repo_dir, real_probe):
    link = _tool_env(tmp_path, "0.11.1")

    result = _skew_on_path(monkeypatch, link)

    assert PATH_SKEW in result.clean
    assert all(p.check != PATH_SKEW for p in result.problems)


@pytest.mark.parametrize(
    "case", ["no-binary", "no-dist-info", "two-dist-infos", "non-numeric", "broken-symlink"]
)
def test_path_skew_without_evidence_is_not_run(tmp_path, monkeypatch, repo_dir, real_probe, case):
    link = _tool_env(tmp_path, "0.6.0")
    site = tmp_path / "tool" / "lib" / "python3.13" / "site-packages"
    if case == "no-binary":
        link = tmp_path / "empty"
        link.mkdir()
    elif case == "no-dist-info":
        (site / "sidegraph-0.6.0.dist-info" / "METADATA").unlink()
        (site / "sidegraph-0.6.0.dist-info").rmdir()
    elif case == "two-dist-infos":
        _write(site / "sidegraph-0.5.0.dist-info" / "METADATA")
    elif case == "non-numeric":
        (site / "sidegraph-0.6.0.dist-info" / "METADATA").unlink()
        (site / "sidegraph-0.6.0.dist-info").rmdir()
        _write(site / "sidegraph-dev.dist-info" / "METADATA")
    elif case == "broken-symlink":
        for command in list(link.iterdir()):
            command.unlink()
            command.symlink_to(tmp_path / "gone")

    result = _skew_on_path(monkeypatch, link)

    assert PATH_SKEW not in result.clean
    assert all(p.check != PATH_SKEW for p in result.problems)


def test_path_skew_reads_no_subprocess(tmp_path, monkeypatch, repo_dir, real_probe):
    """The probe reads the install; running `<binary> --version` costs a second and no released
    entry point answers it."""
    link = _tool_env(tmp_path, "0.6.0")

    def boom(*args, **kwargs):
        raise AssertionError("the probe must not start a process")

    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(os, "execv", boom)
    monkeypatch.setattr(os, "execve", boom)

    assert PATH_SKEW in {p.check for p in _skew_on_path(monkeypatch, link).problems}


def test_path_skew_belongs_to_the_session_surface_only(tmp_path, monkeypatch, repo_dir, real_probe):
    link = _tool_env(tmp_path, "0.6.0")
    monkeypatch.setattr(hooks, "_own_bin_dirs", lambda: set())
    monkeypatch.setenv("PATH", str(link))
    monkeypatch.setattr(sidegraph, "__version__", "0.11.1")

    assert all(p.check != PATH_SKEW for p in _run(repo_dir, "doctor").problems)


def test_path_skew_session_hook_adds_the_line_and_the_notice_once(
    tmp_path, monkeypatch, capsys, repo_dir, real_probe
):
    link = _tool_env(tmp_path, "0.6.0")
    monkeypatch.setattr(sidegraph, "__version__", "0.11.1")
    monkeypatch.setenv("PATH", os.pathsep.join([str(link), os.environ["PATH"]]))
    monkeypatch.setattr(hooks, "_own_bin_dirs", lambda: set())
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(repo_dir))
    Store(format1_store(repo_dir / ".sidegraph")).close()
    monkeypatch.setenv("SIDEGRAPH_DIR", str(repo_dir / ".sidegraph"))
    monkeypatch.setenv("SIDEGRAPH_GRAPH", str(tmp_path / "no-graph.json"))

    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    hooks.session_start()
    first = json.loads(capsys.readouterr().out)
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    hooks.session_start()
    second = json.loads(capsys.readouterr().out)

    assert (
        "is 0.6.0 but this session runs 0.11.1" in first["hookSpecificOutput"]["additionalContext"]
    )
    assert "is 0.6.0 but this session runs 0.11.1" in first["systemMessage"]
    assert (
        "is 0.6.0 but this session runs 0.11.1" in second["hookSpecificOutput"]["additionalContext"]
    )
    assert "systemMessage" not in second or "0.6.0" not in second["systemMessage"]


# -- T3: plugin-off-in-subdirectories, the model -------------------------------------------


def test_t3_enabled_only_in_the_roots_project_settings_is_case_a(tmp_path):
    repo = _repo(tmp_path)
    _settings(repo / ".claude" / "settings.json", {"sidegraph@x": True})

    result = _run(repo)

    problem = _problem(result, PLUGIN_OFF)
    assert problem.severity == "advisory"
    assert PLUGIN_OFF not in result.clean
    (detail,) = _details(problem)
    assert detail.startswith(
        "Claude sessions started below the repository root run without Sidegraph"
    )
    assert "enabled only in `.claude/settings.json`" in detail
    assert "`.claude/settings.local.json` at the root" in detail
    assert problem.line is not None and detail in problem.line


def test_t3_enabled_in_the_roots_local_file_is_clean(tmp_path):
    repo = _repo(tmp_path)
    _settings(repo / ".claude" / "settings.local.json", {"sidegraph@x": True})

    result = _run(repo)

    assert PLUGIN_OFF in result.clean
    assert all(p.check != PLUGIN_OFF for p in result.problems)


def test_t3_enabled_at_user_scope_is_clean(tmp_path, home):
    repo = _repo(tmp_path)
    _user(home, {"sidegraph@x": True})

    assert PLUGIN_OFF in _run(repo).clean


def test_t3_enabled_both_in_the_roots_project_and_at_user_scope_is_clean(tmp_path, home):
    repo = _repo(tmp_path)
    _user(home, {"sidegraph@x": True})
    _settings(repo / ".claude" / "settings.json", {"sidegraph@x": True})

    assert PLUGIN_OFF in _run(repo).clean


def test_t3_not_enabled_anywhere_is_clean(tmp_path):
    """Nothing to switch off: the person never turned the plugin on for this repository."""
    repo = _repo(tmp_path)
    _settings(repo / "pkg" / ".claude" / "settings.json", {"other@x": True})

    assert PLUGIN_OFF in _run(repo).clean


def test_t3_a_nested_file_enabling_only_other_plugins_is_clean_when_user_scope_enables_it(
    tmp_path, home
):
    """The maps merge key by key: the nested file adds ``other@x`` and removes nothing."""
    repo = _repo(tmp_path)
    _user(home, {"sidegraph@x": True})
    _settings(repo / "pkg" / ".claude" / "settings.json", {"other@x": True})

    assert PLUGIN_OFF in _run(repo).clean


_DO_NOT_CHANGE = "do not change their settings unasked"


def test_t3_case_a_alone_has_a_model_line_and_no_human_notice(tmp_path):
    repo = _repo(tmp_path)
    _settings(repo / ".claude" / "settings.json", {"sidegraph@x": True})

    problem = _problem(_run(repo, "session"), PLUGIN_OFF)

    assert problem.notice is None
    assert problem.line is not None
    assert "Claude sessions started below the repository root" in problem.line
    assert "Tell the user" in problem.line and problem.line.endswith(_DO_NOT_CHANGE + ".")
    (detail,) = _details(problem)  # doctor still lists it, without the model-facing clause
    assert _DO_NOT_CHANGE not in detail


def test_t3_case_a_with_b_has_a_notice_for_the_human(tmp_path):
    repo = _repo(tmp_path)
    _settings(repo / ".claude" / "settings.json", {"sidegraph@x": True})
    _settings(repo / "pkg" / ".claude" / "settings.json", {"other@x": True})

    problem = _problem(_run(repo, "session"), PLUGIN_OFF)

    assert problem.notice is not None
    assert "Claude sessions started below the repository root" in problem.notice
    assert "`pkg`" in problem.notice
    assert _DO_NOT_CHANGE not in problem.notice  # the clause speaks to the model
    assert problem.line is not None and problem.line.endswith(_DO_NOT_CHANGE + ".")


def test_t3_case_b_alone_has_a_notice_for_the_human(tmp_path, home):
    repo = _repo(tmp_path)
    _user(home, {"sidegraph@y": True})
    _settings(repo / "pkg" / ".claude" / "settings.json", {"sidegraph@y": False})

    problem = _problem(_run(repo, "session"), PLUGIN_OFF)

    assert problem.notice is not None and "`pkg`" in problem.notice


def test_t3_the_same_nested_file_with_sidegraph_only_at_the_roots_project_is_a_and_b(tmp_path):
    repo = _repo(tmp_path)
    _settings(repo / ".claude" / "settings.json", {"sidegraph@x": True})
    _settings(repo / "pkg" / ".claude" / "settings.json", {"other@x": True})

    problem = _problem(_run(repo), PLUGIN_OFF)

    a, b = _details(problem)
    assert a.startswith("Claude sessions started below the repository root")
    assert "`pkg`" in b and "1 directory" in b
    assert [path for path, _detail in problem.findings][1] == str(repo / "pkg")


# The merge order, key by key and later wins: user, project, local. A nested directory adds its
# own project file, its own local file, then the repository root's local file.


def test_t3_the_roots_local_true_beats_a_nested_project_false(tmp_path):
    repo = _repo(tmp_path)
    _settings(repo / ".claude" / "settings.local.json", {"sidegraph@x": True})
    _settings(repo / "pkg" / ".claude" / "settings.json", {"sidegraph@x": False})

    assert PLUGIN_OFF in _run(repo).clean


@pytest.mark.parametrize(
    ("project", "local", "off"),
    [(True, False, True), (False, True, False)],
    ids=["local-false-beats-project-true", "local-true-beats-project-false"],
)
def test_t3_a_nested_local_file_beats_the_nested_project_file(tmp_path, home, project, local, off):
    repo = _repo(tmp_path)
    _user(home, {"sidegraph@x": True})
    _settings(repo / "pkg" / ".claude" / "settings.json", {"sidegraph@x": project})
    _settings(repo / "pkg" / ".claude" / "settings.local.json", {"sidegraph@x": local})

    result = _run(repo)

    if off:
        (detail,) = _details(_problem(result, PLUGIN_OFF))
        assert "`pkg`" in detail
    else:
        assert PLUGIN_OFF in result.clean


@pytest.mark.parametrize(
    ("user", "root_project", "root_local", "case_a"),
    [
        (None, True, False, False),  # local false beats project true: off at the root, no report
        (False, True, None, True),  # project true beats user false: on at the root, project only
        (False, True, True, False),  # local true beats user false, and project is not alone
    ],
    ids=["local-beats-project", "project-beats-user", "local-beats-user"],
)
def test_t3_the_root_check_merges_user_then_project_then_local(
    tmp_path, home, user, root_project, root_local, case_a
):
    repo = _repo(tmp_path)
    if user is not None:
        _user(home, {"sidegraph@x": user})
    _settings(repo / ".claude" / "settings.json", {"sidegraph@x": root_project})
    if root_local is not None:
        _settings(repo / ".claude" / "settings.local.json", {"sidegraph@x": root_local})

    result = _run(repo)

    if case_a:
        (detail,) = _details(_problem(result, PLUGIN_OFF))
        assert detail.startswith("Claude sessions started below the repository root")
    else:
        assert PLUGIN_OFF in result.clean


def test_t3_a_nested_file_without_enabled_plugins_is_not_b(tmp_path):
    """Such a file cannot change the merge, so its directory is on or off exactly as (a) says,
    and (a) already reports that: here only (a), though the directory holds a settings file."""
    repo = _repo(tmp_path)
    _settings(repo / ".claude" / "settings.json", {"sidegraph@x": True})
    _settings(repo / "pkg" / ".claude" / "settings.json", None, permissions={"allow": []})
    _settings(repo / "pkg" / ".claude" / "settings.local.json", None, model="opus")

    (detail,) = _details(_problem(_run(repo), PLUGIN_OFF))

    assert detail.startswith("Claude sessions started below the repository root")


def test_t3_only_the_nested_files_that_mention_enabled_plugins_are_counted(tmp_path, home):
    repo = _repo(tmp_path)
    _user(home, {"sidegraph@y": True})
    _settings(repo / "pkg" / ".claude" / "settings.json", None, permissions={"allow": []})
    _settings(repo / "src" / ".claude" / "settings.json", {"sidegraph@y": False})

    (detail,) = _details(_problem(_run(repo), PLUGIN_OFF))

    assert "`src`" in detail and "1 directory" in detail


def test_t3_an_explicit_false_in_a_nested_local_file_over_user_scope_is_b(tmp_path, home):
    """Mutation M2 counts any ``sidegraph`` key as enabled, whatever its value."""
    repo = _repo(tmp_path)
    _user(home, {"sidegraph@y": True})
    _settings(repo / "pkg" / ".claude" / "settings.local.json", {"sidegraph@y": False})

    problem = _problem(_run(repo), PLUGIN_OFF)

    (detail,) = _details(problem)
    assert "pkg" in detail
    assert not detail.startswith("Claude sessions started below the repository root")


def test_t3_the_config_dir_variable_replaces_home_for_user_settings_on(tmp_path, home, monkeypatch):
    """``CLAUDE_CONFIG_DIR`` enables the plugin and HOME's settings do not: it is on at user scope,
    so the root's project enabling is no case (a)."""
    repo = _repo(tmp_path)
    _settings(repo / ".claude" / "settings.json", {"sidegraph@x": True})
    config = tmp_path / "config"
    _settings(config / "settings.json", {"sidegraph@x": True})
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))

    assert PLUGIN_OFF in _run(repo).clean


def test_t3_the_config_dir_variable_replaces_home_for_user_settings_off(
    tmp_path, home, monkeypatch
):
    """HOME's settings enable the plugin and ``CLAUDE_CONFIG_DIR``'s do not: Claude Code reads
    only the latter, so the root's project enabling is case (a)."""
    repo = _repo(tmp_path)
    _user(home, {"sidegraph@x": True})
    _settings(repo / ".claude" / "settings.json", {"sidegraph@x": True})
    config = tmp_path / "config"
    _settings(config / "settings.json", {"other@x": True})
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))

    problem = _problem(_run(repo), PLUGIN_OFF)

    (detail,) = _details(problem)
    assert detail.startswith("Claude sessions started below the repository root")


def test_t3_an_empty_config_dir_variable_falls_back_to_home(tmp_path, home, monkeypatch):
    repo = _repo(tmp_path)
    _user(home, {"sidegraph@x": True})
    _settings(repo / ".claude" / "settings.json", {"sidegraph@x": True})
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "")

    assert PLUGIN_OFF in _run(repo).clean


def test_t3_b_names_the_first_directory_and_counts_them_all(tmp_path, home):
    repo = _repo(tmp_path, files=("alpha/x.py", "beta/x.py", "gamma/x.py"))
    _user(home, {"sidegraph@y": True})
    for name in ("beta", "gamma"):
        _settings(repo / name / ".claude" / "settings.json", {"sidegraph@y": False})

    (detail,) = _details(_problem(_run(repo), PLUGIN_OFF))

    assert "`beta`" in detail and "gamma" not in detail
    assert "2 directories" in detail


def test_t3_a_nested_directory_that_enables_it_itself_is_not_off(tmp_path):
    repo = _repo(tmp_path)
    _settings(repo / ".claude" / "settings.json", {"sidegraph@x": True})
    _settings(repo / "pkg" / ".claude" / "settings.json", {"sidegraph@x": True})
    _settings(repo / "src" / ".claude" / "settings.local.json", {"sidegraph@x": True})

    problem = _problem(_run(repo), PLUGIN_OFF)

    (detail,) = _details(problem)  # case (a) only: the two directories enable it themselves
    assert detail.startswith("Claude sessions started below the repository root")


def test_t3_a_repository_that_ignores_dot_claude_is_still_found(tmp_path):
    """The field repository ignores every ``.claude/``, so no settings file is in ``ls-files``
    (mutation M3 takes the files from there)."""
    repo = _repo(tmp_path)
    _write(repo / ".gitignore", ".claude/\n")
    _settings(repo / ".claude" / "settings.json", {"sidegraph@x": True})
    _settings(repo / "pkg" / ".claude" / "settings.json", {"other@x": True})
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    listed = subprocess.run(
        ["git", "ls-files", "--cached"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert ".claude" not in listed  # the premise: git does not list a settings file

    problem = _problem(_run(repo), PLUGIN_OFF)

    a, b = _details(problem)
    assert a.startswith("Claude sessions started below the repository root")
    assert "pkg" in b


def test_t3_unreadable_or_odd_settings_files_count_as_empty(tmp_path, home):
    repo = _repo(tmp_path)
    _write(home / ".claude" / "settings.json", "{not json")
    _write(repo / ".claude" / "settings.json", json.dumps(["not", "an", "object"]))
    _write(repo / "pkg" / ".claude" / "settings.json", json.dumps({"enabledPlugins": "oops"}))

    assert PLUGIN_OFF in _run(repo).clean


def test_t3_outside_a_repository_the_check_is_not_run(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()

    result = _run(plain)

    assert PLUGIN_OFF not in result.clean
    assert all(p.check != PLUGIN_OFF for p in result.problems)


def test_t3_it_belongs_to_the_session_and_doctor_surfaces_only(tmp_path):
    repo = _repo(tmp_path)
    _settings(repo / ".claude" / "settings.json", {"sidegraph@x": True})

    assert _problem(_run(repo, "session"), PLUGIN_OFF)
    assert _problem(_run(repo, "doctor"), PLUGIN_OFF)
    assert all(p.check != PLUGIN_OFF for p in _run(repo, "stats").problems)


def _session_start_out(repo: Path, tmp_path: Path, monkeypatch, capsys) -> dict:
    Store(repo / ".sidegraph").close()
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(repo))
    monkeypatch.setenv("SIDEGRAPH_DIR", str(repo / ".sidegraph"))
    monkeypatch.setenv("SIDEGRAPH_GRAPH", str(tmp_path / "no-graph.json"))
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    hooks.session_start()
    return json.loads(capsys.readouterr().out)


def test_t3_the_session_hook_carries_the_line_and_the_notice_when_b_is_present(
    tmp_path, monkeypatch, capsys
):
    """A nested ``enabledPlugins`` is evidence that someone launches there: the human hears it."""
    repo = _repo(tmp_path)
    _settings(repo / ".claude" / "settings.json", {"sidegraph@x": True})
    _settings(repo / "pkg" / ".claude" / "settings.json", {"other@x": True})

    out = _session_start_out(repo, tmp_path, monkeypatch, capsys)

    assert (
        "Claude sessions started below the repository root"
        in (out["hookSpecificOutput"]["additionalContext"])
    )
    assert "Claude sessions started below the repository root" in out["systemMessage"]
    assert "`pkg`" in out["systemMessage"]


def test_t3_the_session_hook_gives_case_a_alone_to_the_model_and_no_notice_to_the_human(
    tmp_path, monkeypatch, capsys
):
    """A project-scoped install gives every collaborator (a), and the fix is per person: the
    model line carries it, the human is not told every day."""
    repo = _repo(tmp_path)
    _settings(repo / ".claude" / "settings.json", {"sidegraph@x": True})

    out = _session_start_out(repo, tmp_path, monkeypatch, capsys)

    assert (
        "Claude sessions started below the repository root"
        in (out["hookSpecificOutput"]["additionalContext"])
    )
    assert "Claude sessions started below the repository root" not in out.get("systemMessage", "")


# -- T4: doctor ------------------------------------------------------------------------------


def _doctor_repo(tmp_path: Path) -> tuple[Path, Path]:
    repo = _repo(tmp_path)
    _settings(repo / ".claude" / "settings.json", {"sidegraph@x": True})
    store_dir = repo / ".sidegraph"
    Store(store_dir).close()
    return repo, store_dir


def test_t4_doctor_lists_the_finding_with_its_pinned_code(tmp_path, capsys):
    """Red today: ``findings: []``."""
    repo, store_dir = _doctor_repo(tmp_path)

    doctor_main(["--db", str(store_dir), "--json"])

    findings = json.loads(capsys.readouterr().out)["findings"]
    mine = [f for f in findings if f["code"] == PLUGIN_OFF]
    assert len(mine) == 1
    assert mine[0]["path"] == str(repo / ".claude" / "settings.json")
    assert mine[0]["detail"].startswith("Claude sessions started below the repository root")


def test_t4_check_exits_two_on_the_advisory_finding(tmp_path, capsys):
    _repo_root, store_dir = _doctor_repo(tmp_path)

    assert doctor_main(["--db", str(store_dir), "--check"]) == 2
    assert PLUGIN_OFF in capsys.readouterr().out


def test_t4_doctor_without_the_finding_stays_clean_of_it(tmp_path, capsys):
    repo, store_dir = _doctor_repo(tmp_path)
    _settings(repo / ".claude" / "settings.local.json", {"sidegraph@x": True})

    doctor_main(["--db", str(store_dir), "--json"])

    findings = json.loads(capsys.readouterr().out)["findings"]
    assert [f for f in findings if f["code"] == PLUGIN_OFF] == []


# -- T5: one git call ------------------------------------------------------------------------


def test_t5_the_walk_runs_one_git_ls_files_and_no_other_subprocess(tmp_path, monkeypatch, home):
    repo = _repo(tmp_path, files=("a/x.py", "a/b/x.py", "c/x.py", "d/e/f/x.py"))
    _user(home, {"sidegraph@x": True})
    _settings(repo / "a" / ".claude" / "settings.json", {"other@x": True})
    spawned: list[list[str]] = []

    class Spy(subprocess.Popen):
        def __init__(self, args, *a, **k):
            spawned.append(list(args))
            super().__init__(args, *a, **k)

    monkeypatch.setattr(subprocess, "Popen", Spy)

    result = _run(repo)

    assert PLUGIN_OFF in result.clean
    assert [argv[:2] for argv in spawned] == [["git", "ls-files"]]


def test_t5_the_listing_is_the_index_alone_with_no_scan_of_untracked_files(
    tmp_path, monkeypatch, home
):
    """``--others`` made git walk the whole work tree on every SessionStart (about 130 ms of a
    180 ms walk on a 22,800-file repository); the field case comes from tracked files' parents."""
    repo = _repo(tmp_path)
    _user(home, {"sidegraph@x": True})
    spawned: list[list[str]] = []

    class Spy(subprocess.Popen):
        def __init__(self, args, *a, **k):
            spawned.append(list(args))
            super().__init__(args, *a, **k)

    monkeypatch.setattr(subprocess, "Popen", Spy)

    _run(repo)

    (argv,) = spawned
    assert "--cached" in argv
    assert "--others" not in argv and "--exclude-standard" not in argv
