"""Every public launch runs the exact PyPI pin: ``uvx --from sidegraph==<V> --python 3.13
<entry> ...``, with ``<V>`` the version of ``pyproject.toml``, on every host.

One test per host family pins the text of the public manifests word for word: the Claude MCP
entry and hook commands, Grok's hooks including the Bash pre-filter, Codex's MCP entry and its
POSIX and PowerShell hooks, Muse's manifest and hook adapter, and OpenCode's ``index.js``. None
carries a ``UV_*`` setting, the store paths, ``${CLAUDE_PLUGIN_ROOT}``, ``git+`` or ``uv run
--project``. The execution tests run each Claude and Grok command under ``sh -c`` with a stub
``uvx`` first on ``PATH`` and read back the arguments, the host variable and the working
directory. The development twins still launch the checkout.

This file ships, and release verification runs it on the public snapshot, so it reads only
``plugin/``, ``index.js`` and ``pyproject.toml``: the ``*.public.json`` twins are renamed onto
the base names there, and the development twins are absent. The launch text is duplicated here
on purpose, so a change to it shows in this file too.

see design/superpowers/specs/2026-11-04-pinned-pypi-launch-design.md (3)
"""

from __future__ import annotations

import importlib.util
import json
import stat
import subprocess
import tomllib
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_PLUGIN = _ROOT / "plugin" / "sidegraph"

_VERSION = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
    "version"
]
_SOURCE = f"sidegraph=={_VERSION}"
_LAUNCH = f"uvx --from {_SOURCE} --python 3.13"
_MCP_ARGS = ["--from", _SOURCE, "--python", "3.13", "sidegraph-mcp", "--repo-root"]
# Nothing a public launch carries: a uv setting, the store paths (the defaults hold), the plugin
# folder, a git source, a project run.
_FORBIDDEN = (
    "UV_",
    "SIDEGRAPH_DIR",
    "SIDEGRAPH_GRAPH",
    "${CLAUDE_PLUGIN_ROOT}",
    "git+",
    "uv run --project",
    "launch-commit",
    "XDG_CACHE_HOME",
)
_SESSION_GUARD = (
    'echo \'{"systemMessage":"Sidegraph: the SessionStart hook could not start (uv, the first '
    "download of Sidegraph, or no network); run the hook command in a terminal to see the "
    "error\"}'"
)
_EMPTY_GUARD = "echo '{}'"
# The Grok Bash pre-filter: a read word and a space after a non-identifier character, or after a
# JSON whitespace escape, never a bare substring.
_GROK_PATTERNS = (
    "*[!A-Za-z0-9_]'sed '*|*\\\\[ntr]'sed '*|*[!A-Za-z0-9_]'grep '*|*\\\\[ntr]'grep '*|"
    "*[!A-Za-z0-9_]'rg '*|*\\\\[ntr]'rg '*|*[!A-Za-z0-9_]'cat '*|*\\\\[ntr]'cat '*"
)
_BASH_IFS = ["Bash(sed *)", "Bash(grep *)", "Bash(rg *)", "Bash(cat *)"]
_ENTRY = {
    "SessionStart": "sidegraph-session-start",
    "Stop": "sidegraph-stop",
    "PreToolUse": "sidegraph-pre-tool-use",
    "SubagentStart": "sidegraph-subagent-start",
}


def _shipped(base: Path) -> Path:
    """The twin that reaches users: ``X.public.EXT`` where it exists, else ``X.EXT`` (the
    renamed one on a snapshot)."""
    public = base.with_name(base.name.replace(".json", ".public.json"))
    return public if public.is_file() else base


def _develops_checkout(base: Path) -> bool:
    """True when ``base`` is the development twin: the public one sits beside it."""
    return base.with_name(base.name.replace(".json", ".public.json")).is_file()


_CLAUDE_BASE = _PLUGIN / "hooks" / "hooks.json"
_GROK_BASE = _PLUGIN / "grok" / "hooks.json"
_MCP_BASE = _PLUGIN / ".mcp.json"
_CLAUDE_HOOKS = _shipped(_CLAUDE_BASE)
_GROK_HOOKS = _shipped(_GROK_BASE)
_MCP = _shipped(_MCP_BASE)
_CODEX_MCP = _shipped(_PLUGIN / "codex" / "mcp.json")
_CODEX_HOOKS = _shipped(_PLUGIN / "codex" / "hooks.json")
_MUSE_MANIFEST = _PLUGIN / ".muse-plugin" / "plugin.json"
_MUSE_HOOK = _PLUGIN / "muse" / "hook.py"
_INDEX_JS = _ROOT / "index.js"


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def _expected(host: str, event: str) -> str:
    """The public hook command for ``host`` (``claude`` or ``grok``) on ``event``."""
    guard = _SESSION_GUARD if event == "SessionStart" else _EMPTY_GUARD
    if host == "grok":
        prefix = "SIDEGRAPH_HOST=grok "
    else:
        prefix = "SIDEGRAPH_HOST=claude " if event == "SessionStart" else ""
    return f"{prefix}{_LAUNCH} {_ENTRY[event]} --repo-root || {guard}"


def _grok_filter(inner: str) -> str:
    return (
        f'p=$(cat); case "$p" in {_GROK_PATTERNS}) printf \'%s\\n\' "$p" | '
        f"{{ {inner} ; }} ;; *) echo '{{}}' ;; esac"
    )


def _commands(path: Path) -> list[tuple[str, str | None, str]]:
    """``(event, matcher, command)`` for every hook entry, in file order."""
    return [
        (event, group.get("matcher"), hook["command"])
        for event, groups in _load(path)["hooks"].items()
        for group in groups
        for hook in group["hooks"]
    ]


def _assert_no_forbidden(text: str, where: str) -> None:
    for token in _FORBIDDEN:
        assert token not in text, f"{where}: {token!r}"


def test_the_scan_found_the_manifests() -> None:
    for path in (_CLAUDE_HOOKS, _GROK_HOOKS, _MCP, _CODEX_MCP, _CODEX_HOOKS, _MUSE_MANIFEST):
        assert path.is_file(), path
    every = {"SessionStart", "Stop", "PreToolUse", "SubagentStart"}
    for path in (_CLAUDE_HOOKS, _GROK_HOOKS):
        assert {event for event, _m, _c in _commands(path)} == every
    assert {event for event, _m, _c in _commands(_CODEX_HOOKS)} == every - {"PreToolUse"}


# --- Claude ---------------------------------------------------------------------------------


def test_the_claude_mcp_entry_launches_the_pin() -> None:
    entry = _load(_MCP)["mcpServers"]["sidegraph"]
    assert entry == {"command": "uvx", "args": _MCP_ARGS}


def test_every_claude_hook_command_launches_the_pin() -> None:
    for event, _matcher, command in _commands(_CLAUDE_HOOKS):
        assert command == _expected("claude", event), (event, command)


def test_claude_commands_keep_their_shell_fallback_and_the_reworded_session_message() -> None:
    for event, _matcher, command in _commands(_CLAUDE_HOOKS):
        assert command.endswith(
            f" || {_SESSION_GUARD if event == 'SessionStart' else _EMPTY_GUARD}"
        ), command
    session = next(c for e, _m, c in _commands(_CLAUDE_HOOKS) if e == "SessionStart")
    assert "plugin runtime" not in session
    assert "read-only plugin folder" not in session


def test_no_claude_or_grok_public_file_carries_a_forbidden_token() -> None:
    for path in (_CLAUDE_HOOKS, _GROK_HOOKS, _MCP):
        _assert_no_forbidden(path.read_text(encoding="utf-8"), path.name)


# --- Grok -----------------------------------------------------------------------------------


def test_every_grok_hook_command_launches_the_pin() -> None:
    inner = _expected("grok", "PreToolUse")
    for event, matcher, command in _commands(_GROK_HOOKS):
        if matcher == "Bash":
            assert command == _grok_filter(inner), command
        else:
            assert command == _expected("grok", event), (event, command)


# --- Codex ----------------------------------------------------------------------------------


def test_the_codex_mcp_entry_launches_the_pin() -> None:
    assert _load(_CODEX_MCP) == {"sidegraph": {"command": "uvx", "args": _MCP_ARGS}}


def test_every_codex_hook_command_launches_the_pin_in_posix_and_powershell() -> None:
    commands: list[str] = []
    for path in (_CODEX_HOOKS,):
        for groups in _load(path)["hooks"].values():
            for group in groups:
                for hook in group["hooks"]:
                    commands += [hook["command"], hook["commandWindows"]]
    assert len(commands) == 6
    for command in commands:
        assert f"{_LAUNCH} sidegraph-" in command, command
        assert command.count("uvx --from") == 1, command
        _assert_no_forbidden(command, command[:60])
    posix = [c for c in commands if c.startswith("sh -c ")]
    assert len(posix) == 3
    for command in posix:
        assert "git rev-parse --show-toplevel" in command


# --- Muse -----------------------------------------------------------------------------------


def _hook_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_pinned_muse_hook", _MUSE_HOOK)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_muse_manifest_launches_the_pin() -> None:
    (server,) = _load(_MUSE_MANIFEST)["capabilities"]["mcpServers"]
    assert server == {
        "id": "sidegraph",
        "command": ["uvx", f"--from={_SOURCE}", "--python", "3.13", "sidegraph-mcp", "--repo-root"],
    }


def test_the_muse_hook_adapter_launches_the_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    hook = _hook_module()
    assert hook.SOURCE == _SOURCE
    assert not hasattr(hook, "_recorded_commit")
    seen: list[list[str]] = []

    class Stop(Exception):
        pass

    def fake_popen(argv: list[str], **_kwargs: Any) -> Any:
        seen.append(argv)
        raise Stop

    monkeypatch.setattr(hook.subprocess, "Popen", fake_popen)
    for event, entry in (
        ("SessionStart", "sidegraph-session-start"),
        ("SubagentStart", "sidegraph-subagent-start"),
    ):
        with pytest.raises(Stop):
            hook._run(event, entry, b"{}", {})
        assert seen[-1] == ["uvx", f"--from={_SOURCE}", "--python", "3.13", entry]
    # The adapter sets the store paths in the child's environment itself (a launcher script, not
    # a manifest); everything else a launch must not carry is absent.
    text = _MUSE_HOOK.read_text(encoding="utf-8")
    for token in ("UV_", "git+", "uv run --project", "launch-commit", "XDG_CACHE_HOME"):
        assert token not in text, token
    _assert_no_forbidden(_MUSE_MANIFEST.read_text(encoding="utf-8"), _MUSE_MANIFEST.name)


# --- OpenCode -------------------------------------------------------------------------------


def test_opencode_launches_the_pin() -> None:
    text = _INDEX_JS.read_text(encoding="utf-8")
    assert f'const RUNTIME_SOURCE = "{_SOURCE}";' in text
    assert (
        'command: ["uvx", "--from", RUNTIME_SOURCE, "--python", "3.13", "sidegraph-mcp", '
        '"--repo-root"],'
    ) in text
    assert "environment" not in text
    _assert_no_forbidden(text, "index.js")


# --- the launch-site gate sees the same files --------------------------------------------


def test_the_launch_site_gate_is_clean_on_every_public_manifest() -> None:
    """The release's own gate (tools/ does not ship, so a snapshot skips this) agrees."""
    tool = _ROOT / "tools" / "check_public_twins.py"
    if not tool.is_file():
        pytest.skip("tools/ is not in this tree")
    spec = importlib.util.spec_from_file_location("_pinned_twins_gate", tool)
    assert spec is not None and spec.loader is not None
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)
    for path in (
        _MCP,
        _CLAUDE_HOOKS,
        _GROK_HOOKS,
        _CODEX_MCP,
        _CODEX_HOOKS,
        _MUSE_MANIFEST,
        _MUSE_HOOK,
        _INDEX_JS,
    ):
        assert gate.launch_site_violations(path.read_text(encoding="utf-8"), _VERSION) == []


# --- T8: Bash is four ``if`` entries on one command, and Agent|Task one more group -----------


@pytest.mark.parametrize(
    "manifest",
    [p for p in (_CLAUDE_BASE, _shipped(_CLAUDE_BASE)) if p.is_file()],
    ids=lambda p: p.name,
)
def test_t8_the_claude_manifest_wires_bash_as_four_if_entries_on_one_command(
    manifest: Path,
) -> None:
    """Each ``if`` entry is one rule that Claude Code spawns on its own: ten read commands cost
    more processes than a plain ``Bash`` matcher, four cost fewer. The four share the command of
    the Read/Grep/Edit/Write entry byte for byte, so a host that predates ``if`` and ignores it
    dedupes them to one spawn. The ``Agent|Task`` group, which hands a subagent's brief its
    records, runs that same command."""
    groups = _load(manifest)["hooks"]["PreToolUse"]
    assert [g.get("matcher") for g in groups] == ["Read|Grep|Edit|Write", "Bash", "Agent|Task"]
    read_group, bash_group, agent_group = groups
    assert [h.get("if") for h in read_group["hooks"]] == [None]
    assert [h.get("if") for h in bash_group["hooks"]] == _BASH_IFS
    assert {h["type"] for h in bash_group["hooks"]} == {"command"}
    assert {h["command"] for h in bash_group["hooks"]} == {read_group["hooks"][0]["command"]}
    assert [h.get("if") for h in agent_group["hooks"]] == [None]
    assert agent_group["hooks"][0]["command"] == read_group["hooks"][0]["command"]


# --- the commands, run ---------------------------------------------------------------------

# The stub prints its arguments one per line, then marker lines, to stderr, and answers ``{}``.
_STUB = """#!/bin/sh
{
  printf '%s\\n' "$@"
  printf '@@host %s\\n' "$SIDEGRAPH_HOST"
  printf '@@pwd %s\\n' "$(pwd)"
} >&2
printf '{}\\n'
"""
_SED_PAYLOAD = '{"tool_name":"Bash","tool_input":{"command":"sed -n 1p f"}}\n'


def _run_cases() -> list[Any]:
    cases = []
    for host, path in (("claude", _CLAUDE_HOOKS), ("grok", _GROK_HOOKS)):
        seen: set[tuple[str, str]] = set()
        for event, _matcher, command in _commands(path):
            if (event, command) not in seen:
                seen.add((event, command))
                cases.append(pytest.param(host, event, command, id=f"{host}-{event}-{len(seen)}"))
    return cases


@pytest.mark.parametrize(("host", "event", "command"), _run_cases())
def test_every_public_command_runs_the_pin_from_the_project_directory(
    host: str, event: str, command: str, tmp_path: Path
) -> None:
    """The stub ``uvx`` receives ``--from sidegraph==<V> --python 3.13 sidegraph-<entry>
    --repo-root``, the host variable, and the directory the host started the hook in: the
    command does not ``cd``."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "uvx"
    stub.write_text(_STUB, encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    project = tmp_path / "project dir" / "sub"
    project.mkdir(parents=True)
    result = subprocess.run(
        ["sh", "-c", command],
        cwd=project,
        input=_SED_PAYLOAD,
        env={
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "HOME": str(tmp_path / "home"),
            "CLAUDE_PROJECT_DIR": str(tmp_path / "elsewhere"),
        },
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "{}\n"
    lines = result.stderr.splitlines()
    marker = next(i for i, line in enumerate(lines) if line.startswith("@@host "))
    assert lines[:marker] == [
        "--from",
        _SOURCE,
        "--python",
        "3.13",
        _ENTRY[event],
        "--repo-root",
    ]
    assert lines[marker] == (
        "@@host " + {"claude": "claude" if event == "SessionStart" else "", "grok": "grok"}[host]
    )
    assert Path(lines[marker + 1].removeprefix("@@pwd ")).resolve() == project.resolve()


def test_a_failing_uvx_falls_back_to_the_empty_answer(tmp_path: Path) -> None:
    """``|| echo '{}'`` keeps a hook whose launch cannot start from failing the host's turn."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "uvx"
    stub.write_text("#!/bin/sh\nexit 7\n", encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    command = next(c for e, _m, c in _commands(_CLAUDE_HOOKS) if e == "Stop")
    result = subprocess.run(
        ["sh", "-c", command],
        env={"PATH": f"{bin_dir}:/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0
    assert result.stdout == "{}\n"


# --- T18: the development twins ------------------------------------------------------------


def _dev_commands() -> list[Any]:
    return [
        pytest.param(event, command, id=f"{base.parent.name}-{event}-{i}")
        for base in (_CLAUDE_BASE, _GROK_BASE)
        if _develops_checkout(base)
        for i, (event, _matcher, command) in enumerate(_commands(base))
    ]


@pytest.mark.parametrize(("event", "command"), _dev_commands())
def test_t18_the_dev_hooks_keep_the_checkout_environment_and_do_not_cd(
    event: str, command: str
) -> None:
    """With the ``cd`` dropped, a hook started in a subdirectory still uses the checkout's
    ``.venv``: ``uv`` resolves the relative environment against ``--project``, not the working
    directory (measured in the spec, 3.5)."""
    assert "UV_PROJECT_ENVIRONMENT=.venv" in command
    assert '--project "${CLAUDE_PLUGIN_ROOT}/../.."' in command
    assert "cd " not in command
    assert f"{_ENTRY[event]} --repo-root" in command
