"""The Codex plugin's public hooks launch the exact PyPI pin from the repository root
(design/superpowers/specs/2026-11-04-pinned-pypi-launch-design.md).

The Claude and Grok manifests are pinned word for word in tests/test_plugin_pinned_launch.py.
Here the Codex commands are checked in the shells that run them: every POSIX command is one
``sh -c '...'`` script (Codex runs hooks in PowerShell on native Windows, which cannot parse a
POSIX line), every ``commandWindows`` is a PowerShell command that needs no ``sh``, each launches
``uvx --from sidegraph==<V> --python 3.13 <entry>`` after moving to the repository root, and a
failed launch answers what the host accepts. Nothing here reads a launch record: the pin does
not depend on one.

This file ships, and release verification runs it on the public snapshot, so it reads only
what the snapshot has: the manifests under ``plugin/`` (the ``*.public.json`` twins are
renamed onto the base names there) and ``pyproject.toml``, never ``tools/``.

Every shell test puts a stub ``uvx`` first on ``PATH``: no real ``uvx`` runs here.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import tomllib
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_PLUGIN = _ROOT / "plugin" / "sidegraph"
_VERSION = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
    "version"
]
_LAUNCH = f"uvx --from sidegraph=={_VERSION} --python 3.13"
# Every Codex hook manifest that exists: the dev and public twins here, the renamed one on a
# snapshot.
_MANIFESTS = sorted((_PLUGIN / "codex").glob("hooks*.json"))

CODEX_CD = 'cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"'
ENTRY = {
    "SessionStart": "sidegraph-session-start",
    "Stop": "sidegraph-stop",
    "SubagentStart": "sidegraph-subagent-start",
}


def _commands(manifest: Path) -> dict[str, str]:
    """event -> its one command, decoded from the JSON."""
    hooks = json.loads(manifest.read_text(encoding="utf-8"))["hooks"]
    out: dict[str, str] = {}
    for event, groups in hooks.items():
        commands = {hook["command"] for group in groups for hook in group["hooks"]}
        assert len(commands) == 1, f"{manifest}: {event} carries {len(commands)} distinct commands"
        (out[event],) = commands
    return out


def _windows_commands(manifest: Path) -> dict[str, str]:
    """event -> its ``commandWindows``: what Codex runs on native Windows, instead of
    ``command``, through ``powershell.exe -NoProfile -Command`` (no ``sh`` is assumed there)."""
    hooks = json.loads(manifest.read_text(encoding="utf-8"))["hooks"]
    return {
        event: hook["commandWindows"]
        for event, groups in hooks.items()
        for group in groups
        for hook in group["hooks"]
    }


_CODEX_SESSION_MESSAGE = (
    "Sidegraph: the SessionStart hook could not start (uv/uvx, network or project path); "
    "run the hook command in a terminal to see the error"
)
_CODEX_SESSION_FALLBACK = json.dumps(
    {"systemMessage": _CODEX_SESSION_MESSAGE}, separators=(",", ":")
)
# The public manifests launch the pin; the development one runs the checkout.
_CODEX_PUBLIC = [m for m in _MANIFESTS if "uvx --from" in _commands(m)["SessionStart"]]


def test_the_scan_found_the_public_codex_manifest() -> None:
    assert _CODEX_PUBLIC, "no Codex manifest launches the pin"


@pytest.mark.parametrize("manifest", _CODEX_PUBLIC, ids=lambda m: m.name)
def test_every_codex_posix_command_launches_the_pin_from_the_repository_root(
    manifest: Path,
) -> None:
    for event, command in _commands(manifest).items():
        script_launch = f"{CODEX_CD} && "
        assert script_launch in command, (event, command)
        assert f"{_LAUNCH} {ENTRY[event]}" in command, (event, command)
        assert command.count("uvx --from") == 1, (event, command)
        for gone in ("launch-commit", "XDG_CACHE_HOME", "git+", "SIDEGRAPH_DIR", "SIDEGRAPH_GRAPH"):
            assert gone not in command, (event, gone)
    assert "SIDEGRAPH_HOST=codex uvx" in _commands(manifest)["SessionStart"]


_WRAPPED = re.compile(r"^sh -c '[^']*' \|\| printf( '[^']*')+$")


@pytest.mark.parametrize("manifest", _CODEX_PUBLIC, ids=lambda m: m.name)
def test_every_codex_hook_command_is_wrapped_in_sh_c(manifest: Path) -> None:
    """Codex runs hooks in PowerShell on native Windows, which cannot parse POSIX syntax, so
    each command is one ``sh -c '...'`` script plus the outer ``|| printf`` fallback."""
    for event, command in _commands(manifest).items():
        assert _WRAPPED.match(command), (
            f"{manifest.name}: {event} is not sh -c wrapped: {command!r}"
        )


@pytest.mark.parametrize("manifest", _CODEX_PUBLIC, ids=lambda m: m.name)
def test_codex_session_start_is_wrapped_in_sh_c_with_the_same_message_twice(
    manifest: Path,
) -> None:
    """A guard follows both the launch and the wrapper, each printing the same message."""
    command = _commands(manifest)["SessionStart"]
    assert command.startswith("sh -c '")
    outer = f" || printf '%s\\n' '{_CODEX_SESSION_FALLBACK}'"
    assert command.endswith("'" + outer)
    script = command[len("sh -c '") : -len("'" + outer)]
    assert "'" not in script, "a single quote would end the sh -c argument"
    assert script.endswith(' || printf "%s\\n" ' + json.dumps(_CODEX_SESSION_FALLBACK))


# Syntax Windows PowerShell 5.1 cannot parse: pipeline chains, null-coalescing and null-conditional.
_PS7_ONLY = ("&&", "||", "??", "?.", "?[")


@pytest.mark.parametrize("manifest", _CODEX_PUBLIC, ids=lambda m: m.name)
def test_every_codex_hook_has_a_powershell_command_windows_on_the_pin(manifest: Path) -> None:
    windows = _windows_commands(manifest)
    assert set(windows) == set(_commands(manifest)) == set(ENTRY)
    for event, command in windows.items():
        assert not any(token in command for token in _PS7_ONLY), (event, command)
        assert "sh -c" not in command and "printf" not in command, (event, command)
        # The repo-root move, the same one the POSIX command makes.
        assert (
            "$r=git rev-parse --show-cdup 2>$null; if ($r) { Set-Location -LiteralPath $r }"
            in command
        )
        assert command.count("uvx --from") == 1, (event, command)
        for gone in ("launch-commit", "XDG_CACHE_HOME", "git+", "SIDEGRAPH_DIR", "$u"):
            assert gone not in command, (event, gone)
        assert f"; {_LAUNCH} {ENTRY[event]}; if (-not $?) {{ " in command, (event, command)
    assert "$env:SIDEGRAPH_HOST='codex'" in windows["SessionStart"]
    assert windows["SessionStart"].endswith(f"{{ '{_CODEX_SESSION_FALLBACK}' }}")
    for event in ("Stop", "SubagentStart"):
        assert windows[event].endswith("{ '{}' }")


def _stub(path: Path, body: str) -> None:
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


# How long a launch may take before the test calls it stuck. A launch is a few shell forks and
# takes a fraction of a second, but on a loaded machine (a full suite on a busy runner) one was
# measured at 5 s.
_LAUNCH_TIMEOUT = 60.0


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell semantics")
@pytest.mark.parametrize("manifest", _CODEX_PUBLIC, ids=lambda m: m.name)
@pytest.mark.parametrize("shell", ["/bin/sh", "/bin/bash"])
@pytest.mark.parametrize("event", sorted(ENTRY))
def test_a_codex_hook_runs_the_pin_from_the_repository_root_under_a_posix_shell(
    manifest: Path, shell: str, event: str, tmp_path: Path
) -> None:
    """The stub ``uvx`` gets the pin's arguments from the repository root; its stdout reaches
    Codex, and the same message is printed, byte for byte, when it fails and when ``sh`` itself
    cannot start."""
    if not Path(shell).exists():
        pytest.skip(f"{shell} is not installed")
    command = _commands(manifest)[event]
    repo = tmp_path / "repo"
    (repo / "sub").mkdir(parents=True)
    fallback = _CODEX_SESSION_FALLBACK if event == "SessionStart" else "{}"

    def run(stubs: dict[str, str]) -> subprocess.CompletedProcess[str]:
        bin_dir = tmp_path / f"bin{len(list(tmp_path.glob('bin*')))}"
        bin_dir.mkdir()
        _stub(bin_dir / "git", f"printf '%s\\n' '{repo}'")
        for name, body in stubs.items():
            _stub(bin_dir / name, body)
        return subprocess.run(
            [shell, "-c", command],
            cwd=repo / "sub",
            env={"HOME": str(tmp_path), "PATH": f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin"},
            capture_output=True,
            text=True,
            timeout=_LAUNCH_TIMEOUT,
            check=False,
        )

    ok = run({"uvx": 'printf \'%s|%s|%s\\n\' "$*" "$SIDEGRAPH_HOST" "$(pwd)"'})
    host = "codex" if event == "SessionStart" else ""
    args = f"--from sidegraph=={_VERSION} --python 3.13 {ENTRY[event]}"
    assert ok.returncode == 0, ok
    assert ok.stdout == f"{args}|{host}|{repo.resolve()}\n", ok
    failed = run({"uvx": "exit 3"})
    assert failed.returncode == 0 and failed.stdout == fallback + "\n", failed
    no_sh = run({"uvx": 'printf "unreachable\\n"', "sh": "exit 127"})
    assert no_sh.returncode == 0 and no_sh.stdout == fallback + "\n", no_sh
