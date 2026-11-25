"""The second SessionStart handler and its shell gate (D1, K17)."""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from sidegraph.host.briefing import FLAG_NAMES

_PLUGIN = Path(__file__).resolve().parents[1] / "plugin" / "sidegraph"
_MANIFESTS = [
    _PLUGIN / folder / filename
    for folder in ("hooks", "grok", "codex")
    for filename in ("hooks.json", "hooks.public.json")
    if (_PLUGIN / folder / filename).exists()
]
_ENTRY = "sidegraph-session-briefing"
_SHELLS = [shell for name in ("sh", "bash", "dash", "zsh") if (shell := shutil.which(name))]


def _commands(path: Path) -> list[str]:
    groups = json.loads(path.read_text(encoding="utf-8"))["hooks"]["SessionStart"]
    return [h["command"] for group in groups for h in group["hooks"]]


def test_each_host_carries_the_handler_once_in_every_file_that_exists() -> None:
    assert all(
        _PLUGIN / folder / "hooks.json" in _MANIFESTS for folder in ("hooks", "grok", "codex")
    )
    for path in _MANIFESTS:
        commands = _commands(path)
        assert [i for i, command in enumerate(commands) if _ENTRY in command] == [1], path
        assert "sidegraph-session-start" in commands[0], path


@pytest.fixture(params=_MANIFESTS, ids=lambda path: str(path.relative_to(_PLUGIN)))
def command(request: pytest.FixtureRequest) -> str:
    commands = [command for command in _commands(request.param) if _ENTRY in command]
    assert len(commands) == 1, request.param
    return commands[0]


def test_the_case_names_every_flag_of_the_registry(command: str) -> None:
    gate = 'case "$flag" in on)' if "SIDEGRAPH_HOST=codex" in command else " in *on*)"
    prefix, sep, _ = command.partition(gate)
    assert sep
    assert re.findall(r"\$\{(SIDEGRAPH_BRIEFING_[A-Z_]+)\}", prefix) == list(FLAG_NAMES)


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key not in FLAG_NAMES}
    env.update(
        PATH=os.pathsep.join([str(tmp_path), os.defpath, os.environ.get("PATH", "")]),
        CLAUDE_PLUGIN_ROOT=str(_PLUGIN),
        LOG=str(tmp_path / "calls"),
        STATUS=str(tmp_path / "status"),
        BRIEFING_TEST_EXIT="0",
    )
    for name in ("uv", "uvx"):
        path = tmp_path / name
        path.write_text(
            '#!/bin/sh\nprintf "%s\\n" "$*" >> "$LOG"\ncat >/dev/null\n'
            '[ "$BRIEFING_TEST_EXIT" = 0 ] || exit "$BRIEFING_TEST_EXIT"\nprintf "{}\\n"\n',
            encoding="utf-8",
        )
        path.chmod(0o755)
    return env


def _run(shell: str, command: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    done = subprocess.run(
        [shell, "-c", command],
        env=env,
        input='{"session_id":"s1"}\n',
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout == "{}\n"
    return done


@pytest.mark.parametrize("shell", _SHELLS, ids=lambda shell: Path(shell).name)
def test_with_no_flag_nothing_starts_and_the_payload_is_read(
    shell: str,
    command: str,
    env: dict[str, str],
) -> None:
    # More than one pipe buffer, so head can succeed only when the command drains stdin.
    _run(shell, f'( head -c 1000000 /dev/zero; echo $? > "$STATUS" ) | ( {command} )', env)
    assert Path(env["STATUS"]).read_text().strip() == "0"
    assert not Path(env["LOG"]).exists()


@pytest.mark.skipif(not _SHELLS, reason="requires a POSIX shell")
@pytest.mark.parametrize("flag", FLAG_NAMES)
def test_each_flag_alone_starts_the_entry_point(
    flag: str,
    command: str,
    env: dict[str, str],
) -> None:
    _run(_SHELLS[0], command, env | {flag: "on"})
    [call] = Path(env["LOG"]).read_text().splitlines()
    assert call.endswith(_ENTRY if "SIDEGRAPH_HOST=codex" in command else _ENTRY + " --repo-root")
    if "uvx --from" in command:
        assert call.startswith("--from sidegraph==") and " --offline " in call
    else:
        assert " --package sidegraph --frozen --no-active " in call


@pytest.mark.skipif(not _SHELLS, reason="requires a POSIX shell")
@pytest.mark.parametrize("value", ["ON", "off", "1", ""])
def test_a_value_without_on_starts_nothing(
    value: str,
    command: str,
    env: dict[str, str],
) -> None:
    _run(_SHELLS[0], command, env | {FLAG_NAMES[0]: value})
    assert not Path(env["LOG"]).exists()


@pytest.mark.skipif(not _SHELLS, reason="requires a POSIX shell")
def test_the_value_one_starts_the_process_which_then_declines(
    command: str,
    env: dict[str, str],
) -> None:
    _run(_SHELLS[0], command, env | {FLAG_NAMES[0]: "one"})
    log = Path(env["LOG"])
    if "SIDEGRAPH_HOST=codex" in command:
        assert not log.exists()
    else:
        assert _ENTRY in log.read_text()


@pytest.mark.skipif(not _SHELLS, reason="requires a POSIX shell")
def test_a_failing_launch_answers_empty_json(command: str, env: dict[str, str]) -> None:
    _run(_SHELLS[0], command, env | {FLAG_NAMES[0]: "on", "BRIEFING_TEST_EXIT": "2"})
    calls = Path(env["LOG"]).read_text().splitlines()
    assert len(calls) == (2 if "uvx --from" in command else 1)


@pytest.mark.skipif(
    not _SHELLS or _PLUGIN / "codex/hooks.public.json" not in _MANIFESTS,
    reason="requires the internal manifest and a POSIX shell",
)
def test_the_internal_codex_launcher_discards_stdout_on_failure(env: dict[str, str]) -> None:
    uv = Path(env["LOG"]).with_name("uv")
    uv.write_text(
        '#!/bin/sh\ncat >/dev/null\nprintf "partial output\\n"\nexit 7\n', encoding="utf-8"
    )
    _, command = _commands(_PLUGIN / "codex/hooks.json")

    _run(_SHELLS[0], command, env | {FLAG_NAMES[0]: "on"})
    assert command.startswith("sh -c '")


@pytest.mark.skipif(not _SHELLS, reason="requires a POSIX shell")
def test_the_manual_recipe_uses_the_flag_gate_and_launch_fallback(env: dict[str, str]) -> None:
    path = _PLUGIN.parents[1] / "docs/reference/hooks.md"
    text = path.read_text(encoding="utf-8")
    section = text.split("## `sidegraph-session-briefing`\n", 1)[1].split("\n## ", 1)[0]
    recipe = section.split("```sh\n", 1)[1].split("\n```", 1)[0]
    _run(_SHELLS[0], recipe, env)
    log = Path(env["LOG"])
    assert not log.exists()
    _run(_SHELLS[0], recipe, env | {FLAG_NAMES[0]: "on", "BRIEFING_TEST_EXIT": "2"})
    assert len(log.read_text().splitlines()) == 2
