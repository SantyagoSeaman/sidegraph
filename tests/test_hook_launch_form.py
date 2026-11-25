"""The shipped hook commands, run in a real shell: one Bash launch at most, offline first.

Claude Code starts a hook for every ``if`` entry whose rule matches, and it starts them all when it
cannot split a Bash command into subcommands (``echo "$PATH"``, a loop body, ``${HOME}``, a heredoc
with a redirect). The four ``if`` entries of the Bash matcher therefore became four launches of
``uvx`` on a fail-open command. The manifest now has one ``Bash`` entry with no ``if`` that
starts ``uvx`` only when the payload holds a read word, the filter Grok already shipped.

Every ``uvx`` hook launch also tries ``--offline`` first and goes online only when that fails: a
warm cache never revalidates the index, and a machine with no network never waits for uv's
retries (5.8 s and 10 s measured with an aged index cache). A stub ``uvx`` on ``PATH`` records
each call; the manifests' own command strings run through ``sh -c``.

This file ships and release verification runs it on the public snapshot, so it reads only
``plugin/`` and the public sources: the ``*.public.json`` twins are renamed onto the base names
there.
see design/superpowers/specs/2026-11-15-hook-launch-form-design.md (D1, D2)
"""

from __future__ import annotations

import json
import re
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest

from sidegraph import bash_paths
from tests.test_bash_paths import CASES, OUTSIDE, ROOT

_PLUGIN = Path(__file__).resolve().parent.parent / "plugin" / "sidegraph"

# A PreToolUse payload as Claude Code 2.1.325 sends it (captured in a probe of the hook payload).
_PROBE_PAYLOAD: dict[str, Any] = {
    "session_id": "54df5703-5f17-4a8b-ab80-d497a9b3d073",
    "transcript_path": "/tmp/probe/54df5703.jsonl",
    "cwd": "/tmp/probe/proj/d",
    "prompt_id": "0571b6eb-54a5-48e6-9c0e-7aac8c16d8dd",
    "permission_mode": "bypassPermissions",
    "effort": {"level": "medium"},
    "hook_event_name": "PreToolUse",
    "tool_name": "Bash",
    "tool_input": {"command": "{command}", "description": "Run the command"},
    "tool_use_id": "toolu_015Ms471vp7YvAy1RpA5QseP",
}

_STUB = """#!/bin/sh
n=$(ls "$STUB_DIR" | grep -c '^call-')
printf '%s\\n' "$*" > "$STUB_DIR/call-$n"
cat > "$STUB_DIR/stdin-$n"
case " $* " in
  *" --offline "*) out="$STUB_OFFLINE_OUT"; code="${STUB_OFFLINE_EXIT:-0}" ;;
  *) out="$STUB_ONLINE_OUT"; code="${STUB_ONLINE_EXIT:-0}" ;;
esac
if [ -n "$out" ]; then printf '%s\\n' "$out"; fi
exit "$code"
"""

_SESSION_MESSAGE = (
    "Sidegraph: the SessionStart hook could not start (uv, the first download of Sidegraph, "
    "or no network); run the hook command in a terminal to see the error"
)
_CODEX_SESSION_MESSAGE = (
    "Sidegraph: the SessionStart hook could not start (uv/uvx, network or project path); "
    "run the hook command in a terminal to see the error"
)


def _shipped(path: Path) -> Path:
    public = path.with_name(path.name.replace(".json", ".public.json"))
    return public if public.is_file() else path


_CLAUDE = _shipped(_PLUGIN / "hooks" / "hooks.json")
_GROK = _shipped(_PLUGIN / "grok" / "hooks.json")
_CODEX = _shipped(_PLUGIN / "codex" / "hooks.json")
_DEV_TWINS = [
    p
    for p in (_PLUGIN / "hooks" / "hooks.json", _PLUGIN / "grok" / "hooks.json")
    if p.with_name(p.name.replace(".json", ".public.json")).is_file()
]


def _hooks(path: Path) -> list[tuple[str, str | None, dict[str, Any]]]:
    data = json.loads(path.read_text(encoding="utf-8"))["hooks"]
    return [
        (event, group.get("matcher"), hook)
        for event, groups in data.items()
        for group in groups
        for hook in group["hooks"]
    ]


def _commands(path: Path) -> list[Any]:
    return [
        pytest.param(
            path.parent.name, event, matcher, hook["command"], id=f"{path.parent.name}-{i}"
        )
        for i, (event, matcher, hook) in enumerate(_hooks(path))
    ]


# Every public command that starts uvx: Claude, Grok and Codex (POSIX). Muse is a Python adapter,
# tested in test_muse_hook_adapter.py; the PowerShell form is text, in test_command_windows.py.
_PUBLIC = _commands(_CLAUDE) + _commands(_GROK) + _commands(_CODEX)


class Rig:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        self.stub_dir = tmp_path / "stub"
        self.stub_dir.mkdir()
        stub = self.bin / "uvx"
        stub.write_text(_STUB, encoding="utf-8")
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
        self.project = tmp_path / "project dir"
        self.project.mkdir()

    def run(self, command: str, payload: str, **stub_env: str) -> subprocess.CompletedProcess[str]:
        env = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "HOME": str(self.tmp / "home"),
            "STUB_DIR": str(self.stub_dir),
            "SIDEGRAPH_BRIEFING_PROTOCOL": "on",
            **stub_env,
        }
        return subprocess.run(
            ["sh", "-c", command],
            cwd=self.project,
            input=payload,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    def calls(self) -> list[str]:
        files = sorted(self.stub_dir.glob("call-*"), key=lambda p: int(p.name.split("-")[1]))
        return [f.read_text(encoding="utf-8").strip() for f in files]

    def stdins(self) -> list[str]:
        files = sorted(self.stub_dir.glob("stdin-*"), key=lambda p: int(p.name.split("-")[1]))
        return [f.read_text(encoding="utf-8") for f in files]


def _fresh(tmp_path: Path, name: str) -> Rig:
    """A rig in its own folder, for a test that runs several commands."""
    folder = tmp_path / name
    folder.mkdir()
    return Rig(folder)


def _bash_payload(command: str) -> str:
    payload = json.loads(json.dumps(_PROBE_PAYLOAD))
    payload["tool_input"]["command"] = command
    return json.dumps(payload) + "\n"


_SED_PAYLOAD = _bash_payload("sed -n 1p src/a.py")


def _bash_entry(path: Path) -> str:
    (command,) = [h["command"] for _e, m, h in _hooks(path) if m == "Bash"]
    return str(command)


# --- D1: one Bash entry, no `if` ---------------------------------------------------------------


@pytest.mark.parametrize(
    "manifest",
    [p for p in (_PLUGIN / "hooks" / "hooks.json", _CLAUDE) if p.is_file()],
    ids=lambda p: p.name,
)
def test_the_claude_manifest_has_exactly_one_bash_entry_with_no_if(manifest: Path) -> None:
    """Red target: the unfixed manifest has four Bash entries, each with an ``if``."""
    groups = json.loads(manifest.read_text(encoding="utf-8"))["hooks"]["PreToolUse"]
    bash = [g for g in groups if g.get("matcher") == "Bash"]
    assert len(bash) == 1
    (hook,) = bash[0]["hooks"]
    assert "if" not in hook


_PATTERNS = re.compile(r'case "\$p" in (.*?)\) (?:printf|o=)')
_FILTER_WORDS = re.compile(r"'([a-z]+(?: [a-z]+)?)(?: |\\t)'")


def test_filter_word_extractor_accepts_git_show() -> None:
    assert "git show" in _FILTER_WORDS.findall("*[!A-Za-z0-9_]'git show '*")


def _filter_patterns(command: str) -> str:
    match = _PATTERNS.search(command)
    assert match is not None, command
    return match.group(1)


def test_the_claude_and_grok_bash_filters_list_the_same_words() -> None:
    """A drift guard, red against nothing: the two filters share word list and boundaries."""
    assert _filter_patterns(_bash_entry(_CLAUDE)) == _filter_patterns(_bash_entry(_GROK))


def test_the_filter_words_are_the_commands_bash_paths_answers() -> None:
    """A drift guard: ``read_paths`` answers only for the commands in its spec table, so the
    filter that stands in front of the hook must name exactly those."""
    words = set(_FILTER_WORDS.findall(_filter_patterns(_bash_entry(_CLAUDE))))
    assert words == set(bash_paths._SPECS) | {"git show"}


# Spellings read_paths answers that the textual filter cannot match.
_UNFILTERED_READ_CASES = {
    "cat-word-split-by-quotes",
    "git-show-two-spaces",
    "git-show-tab",
    "git-show-quoted",
}


def _read_cases() -> list[Any]:
    return [
        pytest.param(c, id=i)
        for i, c, _d, expected in CASES
        if expected and i not in _UNFILTERED_READ_CASES
    ]


@pytest.mark.parametrize("command", _read_cases())
def test_every_command_bash_paths_answers_passes_the_claude_filter(
    command: str, tmp_path: Path
) -> None:
    """A superset guard, red against nothing: if ``read_paths`` finds a file in the command, the
    filter must let the payload through to ``uvx``."""
    rig = Rig(tmp_path)
    line = command.replace(ROOT, "/proj").replace(OUTSIDE, "/outside")
    done = rig.run(_bash_entry(_CLAUDE), _bash_payload(line), STUB_OFFLINE_OUT="{}")
    assert done.returncode == 0, done.stderr
    assert len(rig.calls()) == 1, (command, rig.calls())


@pytest.mark.parametrize("manifest", [_CLAUDE, _GROK], ids=["claude", "grok"])
@pytest.mark.parametrize(
    "command",
    [
        "'cat' src/a.py",
        '"/bin/cat" src/a.py',
        '"cat" src/a.py',
        "'grep' x src/a.py",
        '"sed" -n 1p src/a.py',
        "cat\rsrc/a.py",
        "sed\r-n 1p src/a.py",
    ],
)
def test_a_quoted_read_word_or_a_carriage_return_after_it_passes_the_filter(
    manifest: Path, command: str, tmp_path: Path
) -> None:
    """A quote after the read word (``'cat' f``, ``"/bin/cat" f``) and a carriage return in place
    of the space (``cat\\rf``) name files for ``read_paths`` and must start the hook.

    Red target: the filter before this fix needed a space or ``\\t`` after the word, so each of
    these payloads printed ``{}`` and started nothing.
    """
    rig = Rig(tmp_path)
    done = rig.run(_bash_entry(manifest), _bash_payload(command), STUB_OFFLINE_OUT="{}")
    assert done.returncode == 0, done.stderr
    assert len(rig.calls()) == 1, (command, rig.calls())


@pytest.mark.parametrize(
    "command", [pytest.param(c, id=i) for i, c, _d, _e in CASES if i in _UNFILTERED_READ_CASES]
)
def test_the_documented_filter_exceptions_start_nothing(command: str, tmp_path: Path) -> None:
    """Guard the documented split-word and nonliteral ``git show`` spellings."""
    rig = Rig(tmp_path)
    done = rig.run(_bash_entry(_CLAUDE), _bash_payload(command), STUB_OFFLINE_OUT="X")
    assert done.stdout == "{}\n"
    assert rig.calls() == []


@pytest.mark.parametrize(
    "command",
    [
        "cat\tsrc/a.py",
        "sed -n 1p\tsrc/a.py",
        "grep\tx src/a.py",
        "cd sub &&\trg x src/a.py",
    ],
)
def test_a_tab_after_the_read_word_still_passes_the_filter(command: str, tmp_path: Path) -> None:
    """``read_paths`` splits on any whitespace, and a tab reaches the filter as the two bytes
    ``\\t``: the word must be accepted before a space and before that escape."""
    rig = Rig(tmp_path)
    done = rig.run(_bash_entry(_CLAUDE), _bash_payload(command), STUB_OFFLINE_OUT="{}")
    assert done.returncode == 0, done.stderr
    assert len(rig.calls()) == 1


def test_the_superset_guard_bites_when_a_word_leaves_the_filter(tmp_path: Path) -> None:
    """The guard above is only worth something if it fails for a filter that lost a word."""
    command = _bash_entry(_CLAUDE)
    crippled = re.sub(r"\|?\*(?:\[!A-Za-z0-9_\]|\\\\\[ntr\])'(?:cat|rg) [^']*'\*", "", command)
    assert crippled != command
    missed = 0
    for i, line in enumerate(("cat src/a.py", "rg x src/a.py")):
        rig = _fresh(tmp_path, str(i))
        rig.run(crippled, _bash_payload(line), STUB_OFFLINE_OUT="{}")
        missed += len(rig.calls()) == 0
    assert missed == 2


@pytest.mark.parametrize(
    "command",
    [
        'for f in *; do echo "$f"; done',
        'echo "${HOME}"',
        'echo "$PATH"',
        'ls "$UNDEF"',
        "echo $(date)",
        "git status --short",
        "tail -n 5 src/a.py",
    ],
)
def test_a_command_with_no_read_word_prints_empty_json_and_starts_nothing(
    command: str, tmp_path: Path
) -> None:
    """Red target: the unfixed manifest's ``if`` entries each start ``uvx`` on a fail-open
    command (a quoted ``$VAR``, a loop body, ``${…}``), where the filter starts nothing."""
    for i, (_e, matcher, hook) in enumerate(_hooks(_CLAUDE)):
        if matcher != "Bash":
            continue
        hook_command = hook["command"]
        rig = _fresh(tmp_path, str(i))
        done = rig.run(hook_command, _bash_payload(command), STUB_OFFLINE_OUT="X")
        assert done.returncode == 0, done.stderr
        assert done.stdout == "{}\n"
        assert rig.calls() == []


# --- D2: offline first, then online, then the fallback ------------------------------------------


@pytest.mark.parametrize(("host", "event", "matcher", "command"), _PUBLIC)
def test_a_failed_offline_attempt_falls_to_one_online_run_that_prints_once(
    host: str, event: str, matcher: str | None, command: str, tmp_path: Path
) -> None:
    """Red target: the unfixed manifests make no offline attempt (one call, no ``--offline``)."""
    rig = Rig(tmp_path)
    done = rig.run(
        command, _SED_PAYLOAD, STUB_OFFLINE_EXIT="1", STUB_OFFLINE_OUT="BAD", STUB_ONLINE_OUT="X"
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout == "X\n"
    calls = rig.calls()
    assert len(calls) == 2, calls
    assert "--offline" in calls[0]
    assert "--offline" not in calls[1]
    # The first attempt may have read the payload; the second must still get all of it.
    assert [s.rstrip("\n") for s in rig.stdins()] == [_SED_PAYLOAD.rstrip("\n")] * 2


@pytest.mark.parametrize(("host", "event", "matcher", "command"), _PUBLIC)
def test_a_successful_offline_attempt_prints_its_answer_and_goes_no_further(
    host: str, event: str, matcher: str | None, command: str, tmp_path: Path
) -> None:
    """Red target: the unfixed manifests make no offline attempt, so the stub never sees
    ``--offline``."""
    rig = Rig(tmp_path)
    done = rig.run(command, _SED_PAYLOAD, STUB_OFFLINE_OUT="Y", STUB_ONLINE_OUT="BAD")
    assert done.returncode == 0, done.stderr
    assert done.stdout == "Y\n"
    (call,) = rig.calls()
    assert "--offline" in call
    assert call.index("--python 3.13 --offline ") > call.index("--from sidegraph==")


@pytest.mark.parametrize(("host", "event", "matcher", "command"), _PUBLIC)
def test_when_both_attempts_fail_the_original_fallback_prints(
    host: str, event: str, matcher: str | None, command: str, tmp_path: Path
) -> None:
    """A guard, red against nothing: the fallback answer is the one the host got before."""
    rig = Rig(tmp_path)
    done = rig.run(command, _SED_PAYLOAD, STUB_OFFLINE_EXIT="1", STUB_ONLINE_EXIT="3")
    assert done.returncode == 0, done.stderr
    assert len(rig.calls()) == 2
    if event == "SessionStart" and "sidegraph-session-briefing" not in command:
        message = _CODEX_SESSION_MESSAGE if host == "codex" else _SESSION_MESSAGE
        assert json.loads(done.stdout) == {"systemMessage": message}
    else:
        assert done.stdout == "{}\n"


@pytest.mark.parametrize(("host", "event", "matcher", "command"), _PUBLIC)
def test_an_online_attempt_that_prints_and_then_fails_leaves_the_host_one_document(
    host: str, event: str, matcher: str | None, command: str, tmp_path: Path
) -> None:
    """The offline attempt fails, the online one prints a document and exits non-zero: the host
    gets the fallback alone, one JSON document, never the partial output followed by the fallback.

    Red target: the online attempt's stdout streamed straight to the host, so the partial
    document was printed and the fallback appended after it (two documents).
    """
    rig = Rig(tmp_path)
    partial = json.dumps({"partial": "online"})
    done = rig.run(
        command,
        _SED_PAYLOAD,
        STUB_OFFLINE_EXIT="1",
        STUB_ONLINE_EXIT="3",
        STUB_ONLINE_OUT=partial,
    )
    assert done.returncode == 0, done.stderr
    assert len(rig.calls()) == 2
    assert partial not in done.stdout
    (document,) = [json.loads(line) for line in done.stdout.splitlines() if line.strip()]
    if event == "SessionStart" and "sidegraph-session-briefing" not in command:
        message = _CODEX_SESSION_MESSAGE if host == "codex" else _SESSION_MESSAGE
        assert document == {"systemMessage": message}
    else:
        assert document == {}


@pytest.mark.parametrize(("host", "event", "matcher", "command"), _PUBLIC)
def test_a_successful_online_attempt_prints_its_answer_once(
    host: str, event: str, matcher: str | None, command: str, tmp_path: Path
) -> None:
    """A guard, red against nothing: capturing the online output keeps a good answer whole."""
    rig = Rig(tmp_path)
    answer = json.dumps({"answer": "online", "text": "ü — ok"}, ensure_ascii=False)
    done = rig.run(
        command, _SED_PAYLOAD, STUB_OFFLINE_EXIT="1", STUB_ONLINE_OUT=answer, STUB_ONLINE_EXIT="0"
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout == answer + "\n"


def test_the_offline_attempt_keeps_the_host_variable(tmp_path: Path) -> None:
    """Both attempts of a Grok hook carry ``SIDEGRAPH_HOST``: it is a prefix of each launch."""
    command = next(h["command"] for e, _m, h in _hooks(_GROK) if e == "Stop")
    seen = tmp_path / "hosts"
    stub = tmp_path / "bin" / "uvx"
    rig = Rig(tmp_path)
    stub.write_text(
        _STUB.replace('exit "$code"', f'echo "$SIDEGRAPH_HOST" >> {seen}\nexit "$code"'),
        encoding="utf-8",
    )
    rig.run(command, _SED_PAYLOAD, STUB_OFFLINE_EXIT="1")
    assert seen.read_text(encoding="utf-8").split() == ["grok", "grok"]
