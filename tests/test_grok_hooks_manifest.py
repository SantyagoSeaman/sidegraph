"""Grok Build reads its own hooks file from the plugin, one without ``if``.

Grok skips every hook handler that sets ``if``, so the Claude manifest's four Bash
``PreToolUse`` handlers fail there. ``.grok-plugin/plugin.json`` points Grok at
``grok/hooks.json`` instead: the Claude file with four differences (no ``if``, one Bash handler
behind a shell pre-filter, ``SIDEGRAPH_HOST=grok``, and explicit ``timeout`` values, because
Grok's default is 5 seconds and a first SessionStart on a cold ``uvx`` cache measured 5.4-7.0 s).
These tests pin that file against the Claude one and run its Bash handler for real.

This file ships, and release verification runs it on the public snapshot, where the
``*.public.json`` twins are renamed onto the base names: it reads only ``plugin/`` and takes
whichever twin variants exist. Every shell test puts a stub ``uvx`` and ``uv`` first on ``PATH``,
so no real launcher runs, and points ``XDG_CACHE_HOME`` and ``HOME`` under ``tmp_path``.
"""

from __future__ import annotations

import copy
import json
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_PLUGIN = _ROOT / "plugin" / "sidegraph"
_VARIANTS = [
    name for name in ("hooks.json", "hooks.public.json") if (_PLUGIN / "grok" / name).is_file()
]

# The Bash pre-filter (design D3): a read word and a space after a non-identifier character,
# or after a JSON whitespace escape (`\\n`, `\\t`, `\\r`), never a bare substring.
# ``{cmd}`` is the Read group's command.
_FILTER = (
    "p=$(cat); case \"$p\" in *[!A-Za-z0-9_]'sed '*|*\\\\[ntr]'sed '*|"
    "*[!A-Za-z0-9_]'grep '*|*\\\\[ntr]'grep '*|*[!A-Za-z0-9_]'rg '*|*\\\\[ntr]'rg '*|"
    "*[!A-Za-z0-9_]'cat '*|*\\\\[ntr]'cat '*) printf '%s\\n' \"$p\" | "
    "{{ {cmd} ; }} ;; *) printf '{{}}\\n' ;; esac"
)


# Grok kills a hook at its `timeout`, 5 s by default (600 s for Stop). Measured on 0.21.0 with
# the public commands: SessionStart 5.4-7.0 s on a cold uvx cache, a hot hook 2.8-3.8 s after
# the uv cache was emptied, 0.3-0.5 s warm. Stop keeps Grok's 600 s default.
_GROK_TIMEOUTS = {"SessionStart": 30, "PreToolUse": 10, "SubagentStart": 10}


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def _all_commands(node: object) -> list[str]:
    if isinstance(node, dict):
        found = [node["command"]] if isinstance(node.get("command"), str) else []
        return found + [c for v in node.values() for c in _all_commands(v)]
    if isinstance(node, list):
        return [c for v in node for c in _all_commands(v)]
    return []


def _keys(node: object) -> set[str]:
    if isinstance(node, dict):
        return set(node) | {k for v in node.values() for k in _keys(v)}
    if isinstance(node, list):
        return {k for v in node for k in _keys(v)}
    return set()


def _to_grok(claude: dict[str, Any]) -> dict[str, Any]:
    """The expected Grok file, built from the Claude one by wrapping, never by unwrapping."""
    out = copy.deepcopy(claude)
    for groups in out["hooks"].values():
        for group in groups:
            for handler in group["hooks"]:
                command = handler["command"]
                if "SIDEGRAPH_HOST=claude" in command:
                    command = command.replace("SIDEGRAPH_HOST=claude", "SIDEGRAPH_HOST=grok")
                else:
                    command = command.replace(
                        "SIDEGRAPH_DIR=.sidegraph",
                        "SIDEGRAPH_HOST=grok SIDEGRAPH_DIR=.sidegraph",
                        1,
                    )
                handler["command"] = command
    for event, seconds in _GROK_TIMEOUTS.items():
        for group in out["hooks"][event]:
            for handler in group["hooks"]:
                handler["timeout"] = seconds
    groups = out["hooks"]["PreToolUse"]
    read_group = next(g for g in groups if g["matcher"] == "Read|Grep|Edit|Write")
    bash_group = next(g for g in groups if g["matcher"] == "Bash")
    wrapped = _FILTER.format(cmd=read_group["hooks"][0]["command"])
    bash_group["hooks"] = [
        {"type": "command", "command": wrapped, "timeout": _GROK_TIMEOUTS["PreToolUse"]}
    ]
    return out


@pytest.fixture(params=_VARIANTS)
def variant(request: pytest.FixtureRequest) -> str:
    return str(request.param)


def _bash_command(variant: str) -> str:
    groups = _load(_PLUGIN / "grok" / variant)["hooks"]["PreToolUse"]
    (bash,) = [g for g in groups if g.get("matcher") == "Bash"]
    (handler,) = bash["hooks"]
    return str(handler["command"])


def test_the_grok_files_exist() -> None:
    assert _VARIANTS, "plugin/sidegraph/grok/hooks*.json is missing"


def test_t1_no_if_key_at_any_depth(variant: str) -> None:
    assert "if" not in _keys(_load(_PLUGIN / "grok" / variant))


def test_t2_the_grok_file_is_the_claude_file_with_four_differences(variant: str) -> None:
    claude = _load(_PLUGIN / "hooks" / variant)
    assert _load(_PLUGIN / "grok" / variant) == _to_grok(claude)


def test_t8_every_command_sets_the_grok_host(variant: str) -> None:
    commands = _all_commands(_load(_PLUGIN / "grok" / variant))
    assert len(commands) == 6
    assert all("SIDEGRAPH_HOST=grok " in c for c in commands)
    assert not any("SIDEGRAPH_HOST=claude" in c for c in commands)


def test_t10_the_manifest_points_grok_at_its_hooks_file() -> None:
    manifest = _load(_PLUGIN / ".grok-plugin" / "plugin.json")
    assert manifest["hooks"] == "./grok/hooks.json"
    assert (_PLUGIN / manifest["hooks"]).is_file()
    claude = _load(_PLUGIN / ".claude-plugin" / "plugin.json")
    assert {k: v for k, v in manifest.items() if k != "hooks"} == claude


# --- T3, T4: the Bash handler, run in a real shell ---------------------------------------------

_STUB = """#!/bin/sh
printf '%s\\n' "$0 $*" >> "$STUB_DIR/calls"
cat > "$STUB_DIR/stdin"
printf '{}\\n'
"""


def _run_handler(command: str, payload: bytes, tmp_path: Path) -> tuple[str, Path, list[str]]:
    bindir = tmp_path / "bin"
    stub_dir = tmp_path / "stub"
    bindir.mkdir()
    stub_dir.mkdir()
    for name in ("uvx", "uv"):
        stub = bindir / name
        stub.write_text(_STUB, encoding="utf-8")
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    project = tmp_path / "project"
    project.mkdir()
    env = {
        "PATH": f"{bindir}:/usr/bin:/bin",
        "HOME": str(tmp_path / "home"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "CLAUDE_PROJECT_DIR": str(project),
        "CLAUDE_PLUGIN_ROOT": str(tmp_path / "plugin" / "sidegraph"),
        "STUB_DIR": str(stub_dir),
    }
    result = subprocess.run(
        ["sh", "-c", command], input=payload, env=env, capture_output=True, check=False, timeout=60
    )
    assert result.returncode == 0, result.stderr
    calls = (stub_dir / "calls").read_text().splitlines() if (stub_dir / "calls").exists() else []
    return result.stdout.decode(), stub_dir / "stdin", calls


# The shape of a real Grok PreToolUse payload for a shell command (captured from Grok Build 1.0.70,
# paths replaced): every key twice, camelCase and snake_case. ``toolInputTruncated`` contains
# ``cat``, which a filter matching a bare substring would take for a read.
_GROK_PAYLOAD = {
    "hookEventName": "pre_tool_use",
    "sessionId": "01a18755-1e44-79d3-aaf5-cf53ffa558a6",
    "cwd": "{tmp}/project/src",
    "workspaceRoot": "{tmp}/project",
    "timestamp": "2026-10-29T11:04:32.948579+00:00",
    "transcriptPath": "{tmp}/grok-home/sessions/01a18755/updates.jsonl",
    "permissionMode": "bypassPermissions",
    "toolName": "run_terminal_command",
    "toolUseId": "call-e449ac01-52c1-4435-9597-c564ac3b54dc-3",
    "toolInput": {"command": "{command}", "description": "{description}"},
    "toolInputTruncated": False,
    "hook_event_name": "PreToolUse",
    "session_id": "01a18755-1e44-79d3-aaf5-cf53ffa558a6",
    "transcript_path": "{tmp}/grok-home/sessions/01a18755/updates.jsonl",
    "permission_mode": "bypassPermissions",
    "tool_name": "run_terminal_command",
    "tool_input": {"command": "{command}", "description": "{description}"},
    "tool_use_id": "call-e449ac01-52c1-4435-9597-c564ac3b54dc-3",
}


def _payload(command: str, tmp_path: Path) -> bytes:
    text = json.dumps(_GROK_PAYLOAD)
    for key, value in (
        ("{tmp}", str(tmp_path)),
        ("{command}", command),
        ("{description}", "Run a shell command"),
    ):
        text = text.replace(key, json.dumps(value)[1:-1])
    return (text + "\n").encode()


@pytest.mark.parametrize(
    "shell_command",
    [
        "git status --short",
        "ls -la",
        "echo concatenate",
        'git commit -m "Fix the bug based on review"',
        "git commit -m 'proposed wording'",
        "echo files used by the test",
        "echo see example.org now",
    ],
)
def test_t3_a_command_that_reads_no_file_starts_nothing(
    variant: str, shell_command: str, tmp_path: Path
) -> None:
    out, _stdin, calls = _run_handler(
        _bash_command(variant), _payload(shell_command, tmp_path), tmp_path
    )
    assert out == "{}\n"
    assert calls == []


@pytest.mark.parametrize(
    "shell_command",
    [
        "sed -n 1p f",
        "cd sub && cat f",
        "ls; grep x f",
        "rg x f",
        # A newline, tab or carriage return reaches the filter as a JSON escape (`\\n`), so
        # the byte before the read word is a letter: the filter must still start the hook.
        "cd sub\nsed -n 1p f",
        "cd sub\ncat f",
        "\tgrep x f",
        "cd sub && \\\nrg x f",
        "cd sub\r\ncat f",
    ],
)
def test_t4_a_read_command_starts_the_hook_with_the_payload_unchanged(
    variant: str, shell_command: str, tmp_path: Path
) -> None:
    payload = _payload(shell_command, tmp_path)
    out, stdin, calls = _run_handler(_bash_command(variant), payload, tmp_path)
    assert out == "{}\n"
    assert len(calls) == 1
    assert stdin.read_bytes() == payload
