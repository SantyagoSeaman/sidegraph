"""A hook command that cannot start must not block the host.

Both hosts feed a hook's stderr back to the model when the hook exits 2: Codex continues the
turn with a ``<hook_prompt>`` on Stop, Claude Code keeps the conversation going on Stop and
blocks the tool call on PreToolUse. ``uv`` exits 2 on its own internal errors (a missing
project, an unwritable cache) and ``dash`` exits 2 when ``cd`` fails, so a hook command that
never reaches Python could loop a session. Every shipped hook command therefore ends with a
guard that turns any failed start into exit 0 and an answer the host accepts.

The tests iterate over the manifests that exist, because the public release snapshot renames
the ``*.public.json`` twins onto the base names: nothing here may hard-code four file names.

# see design/superpowers/specs/2026-10-01-hook-spawn-guard-design.md
"""

from __future__ import annotations

import json
import re
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest

from sidegraph.bootstrap.model import HostKind

_ROOT = Path(__file__).resolve().parent.parent
_PLUGIN = _ROOT / "plugin" / "sidegraph"

# The guard each event ends with. Stop and PreToolUse answer an empty object, which both hosts
# read as "no decision". SessionStart answers a systemMessage the user sees, so a broken
# install is not silent.
_EMPTY_GUARD = " || printf '{}\\n'"
_SESSION_MESSAGE = (
    "Sidegraph: the SessionStart hook could not start (uv/uvx, network or project path); "
    "run the hook command in a terminal to see the error"
)
_SESSION_GUARD = (
    " || printf '%s\\n' '"
    + json.dumps({"systemMessage": _SESSION_MESSAGE}, separators=(",", ":"))
    + "'"
)
_ENTRY_GUARDS = {
    "sidegraph-session-start": _SESSION_GUARD,
    "sidegraph-stop": _EMPTY_GUARD,
    "sidegraph-pre-tool-use": _EMPTY_GUARD,
}
_EVENT_GUARDS = {
    "SessionStart": _SESSION_GUARD,
    "Stop": _EMPTY_GUARD,
    "PreToolUse": _EMPTY_GUARD,
}

_ANSWER = '{"decision":"block","reason":"x"}'


def _manifest_paths() -> list[Path]:
    """Every hook manifest that exists: the Claude Code ones and the Codex ones."""
    return sorted((_PLUGIN / "hooks").glob("*.json")) + sorted(
        (_PLUGIN / "codex").glob("hooks*.json")
    )


def _label(path: Path) -> str:
    return path.relative_to(_PLUGIN).as_posix()


def _hook_entries() -> list[Any]:
    entries = []
    for path in _manifest_paths():
        for event, groups in json.loads(path.read_text(encoding="utf-8"))["hooks"].items():
            for group in groups:
                for hook in group["hooks"]:
                    entries.append(
                        pytest.param(event, hook["command"], id=f"{_label(path)}:{event}")
                    )
    return entries


def _shells() -> list[Any]:
    """``/bin/sh`` always, then ``bash``, ``zsh`` and ``dash`` when present and a different
    program. Codex runs a hook under ``$SHELL`` and falls back to ``/bin/sh``, so any of them can
    be the one that runs the command; a failed ``cd`` exits 2 under ``dash`` and 1 under the
    others."""
    shells = [pytest.param("/bin/sh", id="sh")]
    seen = {Path("/bin/sh").resolve()}
    for name in ("bash", "zsh", "dash"):
        found = shutil.which(name)
        if found and Path(found).resolve() not in seen:
            seen.add(Path(found).resolve())
            shells.append(pytest.param(found, id=name))
    return shells


_ENTRIES = _hook_entries()
_STOP_AND_PRETOOL = [e for e in _ENTRIES if e.values[0] in ("Stop", "PreToolUse")]
_SESSION_START = [e for e in _ENTRIES if e.values[0] == "SessionStart"]


def _script(path: Path, body: str) -> None:
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _run(
    shell: str,
    command: str,
    tmp_path: Path,
    *,
    uv: str,
    project: Path,
) -> subprocess.CompletedProcess[str]:
    """Run one hook command under ``shell -c``.

    A host runs ``$SHELL -lc <command>``, a login shell. These tests leave out the ``-l`` so a
    developer's profile cannot change the result; the command is what is under test, not the
    profile.

    PATH holds a directory of fakes first, then ``/bin:/usr/bin`` for what the shell itself
    needs, so a real ``uv``, ``uvx`` or ``git`` can never shadow a fake. ``uv`` selects what
    the fake ``uv`` and ``uvx`` do: ``fail`` prints an error to stderr and exits 2, the way
    uv does on an internal error; ``answer`` prints a real hook answer and exits 0. The fake
    ``git`` reports ``project`` as the repository root, and ``CLAUDE_PROJECT_DIR`` is the
    same directory, so both manifest styles aim at it.
    """
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir(exist_ok=True)
    if uv == "fail":
        body = "printf 'error: Failed to spawn: boom\\n' >&2\nexit 2"
    else:
        body = f"printf '%s\\n' '{_ANSWER}'"
    _script(bin_dir / "uv", body)
    _script(bin_dir / "uvx", body)
    _script(bin_dir / "git", f"printf '%s\\n' '{project}'")
    cwd = tmp_path / "cwd"
    cwd.mkdir(exist_ok=True)
    return subprocess.run(
        [shell, "-c", command],
        cwd=cwd,
        env={"PATH": f"{bin_dir}:/bin:/usr/bin", "CLAUDE_PROJECT_DIR": str(project)},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _system_message(stdout: str) -> str:
    payload = json.loads(stdout)
    assert set(payload) == {"systemMessage"}, stdout
    return payload["systemMessage"]


def test_the_scan_found_the_manifests() -> None:
    """The parametrised tests below are only as good as this scan: an empty one would pass
    every one of them."""
    labels = {_label(p) for p in _manifest_paths()}
    assert {"hooks/hooks.json", "codex/hooks.json"} <= labels
    events = {e.values[0] for e in _ENTRIES}
    assert events == {"SessionStart", "Stop", "PreToolUse"}
    assert _STOP_AND_PRETOOL and _SESSION_START


@pytest.mark.parametrize(("event", "command"), _ENTRIES)
def test_t1_every_hook_command_ends_with_the_guard_for_its_event(event: str, command: str) -> None:
    guard = _EVENT_GUARDS[event]
    assert command.endswith(guard), f"{event} command lacks its guard: {command!r}"
    assert command.count(guard) == 1


@pytest.mark.parametrize("shell", _shells())
@pytest.mark.parametrize(("event", "command"), _STOP_AND_PRETOOL)
def test_t2_a_failed_start_on_stop_or_pretooluse_answers_empty_json(
    shell: str, event: str, command: str, tmp_path: Path
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    result = _run(shell, command, tmp_path, uv="fail", project=project)
    assert result.returncode == 0, (result.returncode, result.stderr)
    assert result.stdout == "{}\n"


@pytest.mark.parametrize("shell", _shells())
@pytest.mark.parametrize(("event", "command"), _SESSION_START)
def test_t2b_a_failed_start_on_session_start_tells_the_user(
    shell: str, event: str, command: str, tmp_path: Path
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    result = _run(shell, command, tmp_path, uv="fail", project=project)
    assert result.returncode == 0, (result.returncode, result.stderr)
    assert _system_message(result.stdout) == _SESSION_MESSAGE


@pytest.mark.parametrize("shell", _shells())
@pytest.mark.parametrize(("event", "command"), _ENTRIES)
def test_t3_a_missing_project_directory_is_caught(
    shell: str, event: str, command: str, tmp_path: Path
) -> None:
    """The failing ``cd`` is part of the chain: ``dash`` exits 2 on it and the others 1, and
    neither may reach the host. uv succeeds here, but is never reached."""
    result = _run(shell, command, tmp_path, uv="answer", project=tmp_path / "no-such-dir")
    assert result.returncode == 0, (result.returncode, result.stderr)
    if event == "SessionStart":
        assert _system_message(result.stdout) == _SESSION_MESSAGE
    else:
        assert result.stdout == "{}\n"


@pytest.mark.parametrize("shell", _shells())
@pytest.mark.parametrize(("event", "command"), _ENTRIES)
def test_t4_the_guard_leaves_a_real_answer_alone(
    shell: str, event: str, command: str, tmp_path: Path
) -> None:
    """Red against nothing: the guard must not swallow, wrap or repeat what the hook says."""
    project = tmp_path / "project"
    project.mkdir()
    result = _run(shell, command, tmp_path, uv="answer", project=project)
    assert result.returncode == 0, (result.returncode, result.stderr)
    assert result.stdout == _ANSWER + "\n"


@pytest.mark.parametrize("path", _manifest_paths(), ids=_label)
def test_t5_the_bootstrap_verifier_still_recognises_a_guarded_command(
    path: Path, tmp_path: Path
) -> None:
    """Red against nothing: ``shlex.split`` still yields ``sidegraph-<hook>`` as a token."""
    from sidegraph.bootstrap.integrations import verify_integration

    codex = path.parent.name == "codex"
    dest = tmp_path / (".codex/hooks.json" if codex else ".claude/settings.json")
    dest.parent.mkdir()
    dest.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")

    result = verify_integration(tmp_path, HostKind.CODEX if codex else HostKind.CLAUDE_CODE)

    assert result.session_start == "verified"
    assert result.stop == "verified"
    if not codex:
        assert result.pretool_read_grep == "verified"


# The manual hook recipes. T6 scans every doc rather than naming three, so a recipe added
# later is held to the same rule; the counts only stop the scan from passing on nothing.
_RECIPE_DOCS = {
    "docs/integrations/codex.md": 2,
    "docs/getting-started/codex-setup.md": 2,
    "docs/getting-started/claude-code-setup.md": 3,
}
_COMMAND_RE = re.compile(r'"command":\s*("(?:[^"\\]|\\.)*")')
_ENTRY_RE = re.compile(r"sidegraph-(?:session-start|stop|pre-tool-use)\b")


def _doc_files() -> list[Path]:
    """Every markdown file a reader could copy a hook command from: the docs, the README, the
    contributor guide, the plugin's skills and the ``*.public.md`` twins (which the release
    renames onto the base names, so they are absent from the public tree)."""
    files = [
        *sorted((_ROOT / "docs").rglob("*.md")),
        *sorted((_PLUGIN / "skills").rglob("*.md")),
        *sorted(_ROOT.glob("*.public.md")),
        *sorted(_ROOT.glob("CONTRIBUTING*.md")),
        _ROOT / "README.md",
    ]
    return [f for f in dict.fromkeys(files) if f.is_file()]


def _doc_recipes() -> dict[str, list[str]]:
    """Hook commands written in fenced JSON in those files, by file."""
    found: dict[str, list[str]] = {}
    for doc in _doc_files():
        for match in _COMMAND_RE.finditer(doc.read_text(encoding="utf-8")):
            command = json.loads(match.group(1))
            if _ENTRY_RE.search(command):
                found.setdefault(doc.relative_to(_ROOT).as_posix(), []).append(command)
    return found


def test_t6_the_doc_recipes_carry_the_guard() -> None:
    recipes = _doc_recipes()
    for doc, expected in _RECIPE_DOCS.items():
        assert len(recipes.get(doc, [])) >= expected, f"{doc}: fewer than {expected} recipes found"
    for doc, commands in recipes.items():
        for command in commands:
            entry = _ENTRY_RE.search(command)
            assert entry is not None
            guard = _ENTRY_GUARDS[entry.group(0)]
            assert command.endswith(guard), f"{doc}: recipe lacks its guard: {command!r}"
