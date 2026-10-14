"""The public plugin's hot-path hooks launch from the commit SessionStart recorded
(design/superpowers/specs/2026-10-03-launch-from-session-commit-design.md).

T1 pins the shape of every hot command in the plugin's hook manifests. T2 runs each one in a
real shell against a stub ``uvx`` and reads back the ``--from`` value it was given. A1 and A2
pin that the recorded commit replaces the fallback only when the fallback's ref is the branch:
a pinned plugin keeps its ref.

This file ships, and release verification runs it on the public snapshot, so it reads only
what the snapshot has: the manifests under ``plugin/`` (the ``*.public.json`` twins are
renamed onto the base names there), never ``tools/``, and it never names the ref. A hot
command is a command that contains ``launch-commit``; the dev manifests have none. The
fallback ref comes from the command itself, because ``release-public.sh --ref X`` rewrites it
and the snapshot's SessionStart to ``X``. The release-side pins (T5, T6) are in
tests/test_public_hook_launch_release.py.

Every shell test sets ``HOME`` and ``XDG_CACHE_HOME`` under ``tmp_path``, and puts a stub
``uvx`` first on ``PATH``: no real ``uvx`` runs here and the real cache is never read.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_PLUGIN = _ROOT / "plugin" / "sidegraph"
# Every hook manifest that exists: the dev and public twins here, the renamed one on a snapshot.
_MANIFESTS = sorted((_PLUGIN / "hooks").glob("hooks*.json")) + sorted(
    (_PLUGIN / "codex").glob("hooks*.json")
)

# The fallback URL up to the ref. A literal ending in ``@`` is not an install ref to the
# release script's scan; the ref itself is read from the command under test.
_URL = "git+https://github.com/SantyagoSeaman/sidegraph.git@"
_REF = re.compile(r"sidegraph\.git@([A-Za-z0-9._/-]+)")

CLAUDE_CD = 'cd "${CLAUDE_PROJECT_DIR}"'
CODEX_CD = 'cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"'
ENV = "SIDEGRAPH_DIR=.sidegraph SIDEGRAPH_GRAPH=graphify-out/graph.json"
# PreToolUse never reads the graph, and the word "graphify" in a command makes Graphify's installer
# and uninstaller remove a hand-wired Bash group (tests/test_release_allowlist.py).
PRETOOL_ENV = "SIDEGRAPH_DIR=.sidegraph"
GUARD = " || printf '{}\\n'"  # the Python literal is the two characters backslash and n
COMMIT = "f5babfb86a9562140acef8ddc61e7fe4a5933f00"
# The ref whose commit the record holds: the only fallback that takes it.
_BRANCH = "main"
ENTRY = {
    "Stop": "sidegraph-stop",
    "PreToolUse": "sidegraph-pre-tool-use",
    "SubagentStart": "sidegraph-subagent-start",
}


def _commands(manifest: Path) -> dict[str, str]:
    """event -> its one command, decoded from the JSON.

    An event may carry several hook entries (PreToolUse has the Read/Grep/Edit/Write group and
    the Bash group with one entry per ``if``); they are one command, byte for byte, which is
    asserted here, so a host older than 2.1.85 that ignores ``if`` runs one process for them."""
    hooks = json.loads(manifest.read_text(encoding="utf-8"))["hooks"]
    out: dict[str, str] = {}
    for event, groups in hooks.items():
        commands = {hook["command"] for group in groups for hook in group["hooks"]}
        assert len(commands) == 1, f"{manifest}: {event} carries {len(commands)} distinct commands"
        (out[event],) = commands
    return out


def _hot(manifest: Path) -> dict[str, str]:
    """The commands that launch from the record: the calls that happen many times per
    session. Picked by content, so a new hot event is covered the day it is added."""
    return {e: c for e, c in _commands(manifest).items() if "launch-commit" in c}


def _host(manifest: Path) -> str:
    return "codex" if manifest.parent.name == "codex" else "claude"


def _ref(command: str) -> str:
    """The ref of the fallback: the one ``sidegraph.git@<ref>`` literal in a hot command."""
    refs = _REF.findall(command)
    assert len(refs) == 1, f"a hot command carries exactly one literal fallback ref: {command!r}"
    return refs[0]


def _prefix(ref: str) -> str:
    """The D3 prefix, word for word from the spec, for a fallback on ``ref``."""
    return (
        'f="${XDG_CACHE_HOME:-$HOME/.cache}/sidegraph/launch-commit"; c=; '
        '[ -f "$f" ] && c=$(head -c 41 "$f" 2>/dev/null); '
        'case "$c" in *[!0123456789abcdef]*) c= ;; esac; '
        '[ "${#c}" -eq 40 ] || c=; '
        f"u={_URL}{ref}; "
        '[ -n "$c" ] && [ "${u##*@}" = main ] && u=${u%@*}@$c'
    )


_HOT_CASES = [
    pytest.param(_host(manifest), event, command, manifest, id=f"{_host(manifest)}-{event}")
    for manifest in _MANIFESTS
    for event, command in _hot(manifest).items()
]


def test_the_scan_found_every_hot_hook() -> None:
    """The parametrised tests below are only as good as this scan."""
    assert {(c.values[0], c.values[1]) for c in _HOT_CASES} >= {
        ("claude", "Stop"),
        ("claude", "PreToolUse"),
        ("claude", "SubagentStart"),
        ("codex", "Stop"),
        ("codex", "SubagentStart"),
    }


# --- T8: Bash is four ``if`` entries on one command, and Agent|Task one more group -------------

_BASH_IFS = ["Bash(sed *)", "Bash(grep *)", "Bash(rg *)", "Bash(cat *)"]


@pytest.mark.parametrize(
    "manifest",
    [m for m in _MANIFESTS if _host(m) == "claude"],
    ids=lambda m: m.name,
)
def test_t8_the_claude_manifest_wires_bash_as_four_if_entries_on_one_command(
    manifest: Path,
) -> None:
    """Each ``if`` entry is one rule that Claude Code spawns on its own: ten read commands cost
    more processes than a plain ``Bash`` matcher, four cost fewer. The four share the command of
    the Read/Grep/Edit/Write entry byte for byte (``_commands`` asserts it), so a host that
    predates ``if`` and ignores it dedupes them to one spawn. The ``Agent|Task`` group, which
    hands a subagent's brief its records, runs that same command."""
    groups = json.loads(manifest.read_text(encoding="utf-8"))["hooks"]["PreToolUse"]
    assert [g.get("matcher") for g in groups] == ["Read|Grep|Edit|Write", "Bash", "Agent|Task"]
    read_group, bash_group, agent_group = groups
    assert [h.get("if") for h in read_group["hooks"]] == [None]
    assert [h.get("if") for h in bash_group["hooks"]] == _BASH_IFS
    assert {h["type"] for h in bash_group["hooks"]} == {"command"}
    assert {h["command"] for h in bash_group["hooks"]} == {read_group["hooks"][0]["command"]}
    # The subagent-brief group (tests/test_agent_brief_manifests.py) is one more entry on it.
    assert [h.get("if") for h in agent_group["hooks"]] == [None]
    assert agent_group["hooks"][0]["command"] == read_group["hooks"][0]["command"]


# --- T1: the shape -----------------------------------------------------------------------


@pytest.mark.parametrize(("host", "event", "command", "manifest"), _HOT_CASES)
def test_t1_a_hot_command_carries_the_prefix_and_launches_from_the_variable(
    host: str, event: str, command: str, manifest: Path
) -> None:
    entry = ENTRY[event]
    ref = _ref(command)
    # The snapshot rewrites one ref everywhere: the fallback is SessionStart's ref.
    assert _REF.findall(_commands(manifest)["SessionStart"]) == [ref]
    prefix = _prefix(ref)
    if host == "claude":
        env = PRETOOL_ENV if event == "PreToolUse" else ENV
        expected = f'{prefix}; {CLAUDE_CD} && {env} uvx --from "$u" {entry}{GUARD}'
        assert command == expected
        return
    assert command.startswith("sh -c '") and command.endswith("'" + GUARD)
    script = command[len("sh -c '") : -len("'" + GUARD)]
    assert "'" not in script, "a single quote would end the sh -c argument"
    assert script == f'{prefix}; {CODEX_CD} && {ENV} uvx --from "$u" {entry} || printf "{{}}\\n"'


@pytest.mark.parametrize("manifest", sorted({c.values[3] for c in _HOT_CASES}), ids=_host)
def test_t1_session_start_keeps_the_branch_ref_and_no_prefix(manifest: Path) -> None:
    command = _commands(manifest)["SessionStart"]
    assert f"uvx --from {_URL}" in command
    assert re.search(
        rf"uvx --from {re.escape(_URL)}[A-Za-z0-9._/-]+ sidegraph-session-start", command
    )
    assert "launch-commit" not in command and '"$u"' not in command


_CODEX_SESSION_MESSAGE = (
    "Sidegraph: the SessionStart hook could not start (uv/uvx, network or project path); "
    "run the hook command in a terminal to see the error"
)
_CODEX_SESSION_FALLBACK = json.dumps(
    {"systemMessage": _CODEX_SESSION_MESSAGE}, separators=(",", ":")
)
_CODEX_PUBLIC = [
    m for m in _MANIFESTS if _host(m) == "codex" and _URL in _commands(m)["SessionStart"]
]


def test_the_scan_found_the_public_codex_session_start() -> None:
    assert _CODEX_PUBLIC, "no Codex manifest launches SessionStart from the public URL"


@pytest.mark.parametrize("manifest", _CODEX_PUBLIC, ids=lambda m: m.name)
def test_codex_session_start_is_wrapped_in_sh_c(manifest: Path) -> None:
    """On native Windows Codex runs a hook in a shell that cannot parse ``VAR=value command``,
    so a bare POSIX line failed there while the ``sh -c``-wrapped Stop and SubagentStart ran.
    The wrapper keeps the env assignments inside ``sh``, the launch URL occurs once (the
    release script rewrites that literal), and a guard follows both the launch and the
    wrapper, each printing the same message."""
    command = _commands(manifest)["SessionStart"]
    assert command.startswith("sh -c '")
    outer = f" || printf '%s\\n' '{_CODEX_SESSION_FALLBACK}'"
    assert command.endswith("'" + outer)
    script = command[len("sh -c '") : -len("'" + outer)]
    assert "'" not in script, "a single quote would end the sh -c argument"
    assert script.startswith(CODEX_CD + " && SIDEGRAPH_HOST=codex ")
    assert f"{ENV} uvx --from {_URL}" in script
    assert command.count(_URL) == 1
    assert script.endswith(' || printf "%s\\n" ' + json.dumps(_CODEX_SESSION_FALLBACK))


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


# Syntax Windows PowerShell 5.1 cannot parse: pipeline chains, null-coalescing and null-conditional.
_PS7_ONLY = ("&&", "||", "??", "?.", "?[")


@pytest.mark.parametrize("manifest", _CODEX_PUBLIC, ids=lambda m: m.name)
def test_every_codex_hook_has_a_powershell_command_windows(manifest: Path) -> None:
    windows = _windows_commands(manifest)
    assert set(windows) == set(_commands(manifest)) == {"SessionStart", "Stop", "SubagentStart"}
    for event, command in windows.items():
        assert not any(token in command for token in _PS7_ONLY), (event, command)
        assert "sh -c" not in command and "printf" not in command, (event, command)
        # The repo-root move, the same one the POSIX command makes.
        assert (
            "$r=git rev-parse --show-cdup 2>$null; if ($r) { Set-Location -LiteralPath $r }"
            in command
        )
        assert "$env:SIDEGRAPH_DIR='.sidegraph'" in command, event
        assert "$env:SIDEGRAPH_GRAPH='graphify-out/graph.json'" in command, event


@pytest.mark.parametrize("manifest", _CODEX_PUBLIC, ids=lambda m: m.name)
def test_codex_session_start_command_windows_names_the_url_once_and_guards_the_launch(
    manifest: Path,
) -> None:
    """The release script rewrites the one literal URL, and the message after a failed launch
    is the one the POSIX hook prints."""
    command = _windows_commands(manifest)["SessionStart"]
    assert command.count(_URL) == 1
    assert "$env:SIDEGRAPH_HOST='codex'" in command
    assert f"uvx --from {_URL}" in command
    assert command.endswith(
        f"sidegraph-session-start; if (-not $?) {{ '{_CODEX_SESSION_FALLBACK}' }}"
    )


@pytest.mark.parametrize("manifest", _CODEX_PUBLIC, ids=lambda m: m.name)
@pytest.mark.parametrize("event", ["Stop", "SubagentStart"])
def test_codex_hot_command_windows_pins_the_record_only_on_the_branch(
    manifest: Path, event: str
) -> None:
    command = _windows_commands(manifest)[event]
    ref = _ref(command)
    assert command.count(_URL) == 1
    assert command.startswith(f"$u='{_URL}{ref}'; ")
    # The recorded commit takes over only a fallback that is the branch, never a pinned ref.
    assert "-and $u.EndsWith('@main')" in command
    assert "$u=$u.Substring(0, $u.LastIndexOf('@') + 1) + $c" in command
    assert "-cmatch '^[0123456789abcdef]{40}$'" in command and "$i.Length -eq 40" in command
    assert command.endswith(f"uvx --from $u {ENTRY[event]}; if (-not $?) {{ '{{}}' }}")


_WRAPPED = re.compile(r"^sh -c '[^']*' \|\| printf( '[^']*')+$")


@pytest.mark.parametrize("manifest", _CODEX_PUBLIC, ids=lambda m: m.name)
def test_every_codex_hook_command_is_wrapped_in_sh_c(manifest: Path) -> None:
    """Codex runs hooks in PowerShell on native Windows, which cannot parse POSIX syntax, so
    each command is one ``sh -c '...'`` script plus the outer ``|| printf`` fallback."""
    for event, command in _commands(manifest).items():
        assert _WRAPPED.match(command), (
            f"{manifest.name}: {event} is not sh -c wrapped: {command!r}"
        )


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell semantics")
@pytest.mark.parametrize("manifest", _CODEX_PUBLIC, ids=lambda m: m.name)
@pytest.mark.parametrize("shell", ["/bin/sh", "/bin/bash"])
def test_codex_session_start_runs_under_a_posix_shell(
    manifest: Path, shell: str, tmp_path: Path
) -> None:
    """The hook's stdout reaches Codex when uvx works; the same message is printed, byte for
    byte, when it fails and when ``sh`` itself cannot start."""
    if not Path(shell).exists():
        pytest.skip(f"{shell} is not installed")
    command = _commands(manifest)["SessionStart"]
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {"HOME": str(tmp_path), "XDG_CACHE_HOME": str(tmp_path / "cache")}

    def run(stubs: dict[str, str]) -> subprocess.CompletedProcess[str]:
        bin_dir = tmp_path / f"bin{len(list(tmp_path.glob('bin*')))}"
        bin_dir.mkdir()
        for name, body in stubs.items():
            _stub(bin_dir / name, body)
        return _run_in_group(
            [shell, "-c", command],
            cwd=repo,
            env={**env, "PATH": f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin"},
            timeout=30,
        )

    ok = run({"uvx": 'printf \'{"host":"%s","dir":"%s"}\\n\' "$SIDEGRAPH_HOST" "$SIDEGRAPH_DIR"'})
    assert ok.stdout == '{"host":"codex","dir":".sidegraph"}\n', ok
    failed = run({"uvx": "exit 3"})
    assert failed.returncode == 0 and failed.stdout == _CODEX_SESSION_FALLBACK + "\n", failed
    no_sh = run({"uvx": 'printf "unreachable\\n"', "sh": "exit 127"})
    assert no_sh.returncode == 0 and no_sh.stdout == _CODEX_SESSION_FALLBACK + "\n", no_sh


# --- T2: run them ------------------------------------------------------------------------

_SHELLS = [pytest.param("/bin/sh", id="sh")] + [
    pytest.param(found, id=name)
    for name in ("bash", "zsh")
    if (found := shutil.which(name)) and Path(found).resolve() != Path("/bin/sh").resolve()
]


def _stub(path: Path, body: str) -> None:
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _run_in_group(
    argv: list[str], *, cwd: Path, env: dict[str, str], timeout: float
) -> subprocess.CompletedProcess[str]:
    """``subprocess.run`` that cannot leave a process behind.

    ``subprocess.run`` kills only the shell on a timeout. A command that blocked in a child
    (``head`` on a FIFO inside ``$(...)``) outlives the test, and a regression that hangs 20
    cases leaves 20 stuck processes. The command runs in its own session, and on a timeout (or
    any other interruption) the whole process group is killed.
    """
    proc = subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        out, err = proc.communicate(timeout=timeout)
    except BaseException:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate(timeout=5)
        raise
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


@pytest.mark.slow  # runs a real shell command and waits out its timeout
def test_t2_a_timed_out_command_leaves_no_process_behind(tmp_path: Path) -> None:
    """The cleanup the FIFO case relies on, tested directly: the grandchild of a command that
    timed out is gone, not just the shell."""
    pidfile = tmp_path / "grandchild.pid"
    script = f"sleep 60 & echo $! > '{pidfile}'; wait"
    with pytest.raises(subprocess.TimeoutExpired):
        _run_in_group(
            ["/bin/sh", "-c", script], cwd=tmp_path, env={"PATH": "/bin:/usr/bin"}, timeout=1
        )
    pid = int(pidfile.read_text())
    for _ in range(40):  # SIGKILL is asynchronous
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    os.kill(pid, signal.SIGKILL)  # do not leave it either
    pytest.fail(f"the grandchild {pid} survived the timeout")


def _from_value(
    shell: str,
    command: str,
    tmp_path: Path,
    *,
    xdg: bool = True,
    locale: str | None = None,
) -> str:
    """Run ``command`` under ``shell -c`` and return the ``--from`` value the stub ``uvx`` saw.

    A host runs ``$SHELL -lc``. ``-l`` is left out so a developer's profile cannot move a real
    ``uvx`` ahead of the stub; the parse of the command is what is under test, not the profile
    (the same reasoning as tests/test_hook_spawn_guard.py). PATH is the stub directory and
    ``/bin:/usr/bin``, so no real ``uvx`` is on it.
    """
    stubs = tmp_path / "stubs"
    stubs.mkdir(exist_ok=True)
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    _stub(stubs / "uvx", "printf '%s\\n' \"$@\"")
    _stub(stubs / "git", f"printf '%s\\n' '{project}'")
    env = {
        "PATH": f"{stubs}:/bin:/usr/bin",
        "HOME": str(tmp_path / "home"),
        "CLAUDE_PROJECT_DIR": str(project),
    }
    if xdg:
        env["XDG_CACHE_HOME"] = str(tmp_path / "cache")
    if locale:
        env["LC_ALL"] = locale
    result = _run_in_group(
        [shell, "-c", command],
        cwd=tmp_path,
        env=env,
        timeout=5,  # a FIFO the command opened would block forever
    )
    assert result.returncode == 0, (result.returncode, result.stderr)
    lines = result.stdout.splitlines()
    assert lines[0] == "--from" and len(lines) == 3, result.stdout
    return lines[1]


def _record(tmp_path: Path, content: bytes | None, *, home: bool = False) -> None:
    root = tmp_path / "home" / ".cache" if home else tmp_path / "cache"
    record = root / "sidegraph" / "launch-commit"
    record.parent.mkdir(parents=True)
    if content is not None:
        record.write_bytes(content)


def _expected(command: str, commit: str | None) -> str:
    """The ``--from`` value the command must pass: the recorded commit when the fallback is on
    the branch, or else the fallback on the ref the command itself carries (a snapshot cut with
    ``--ref`` is pinned, and ignores the record: test_a1)."""
    ref = _ref(command)
    return _URL + (commit if commit and ref == _BRANCH else ref)


# (id, file content, the commit the command must launch from; None = the fallback)
_RECORDS: list[Any] = [
    pytest.param(COMMIT.encode(), COMMIT, id="valid-commit"),
    pytest.param(COMMIT.encode() + b"\n", COMMIT, id="valid-commit-with-newline"),
    pytest.param(b"abc", None, id="too-short"),
    pytest.param((COMMIT + "0").encode(), None, id="41-hex"),
    pytest.param((COMMIT[:39]).encode(), None, id="39-hex"),
    pytest.param(b"ABCDE" * 8, None, id="ABCDE-x8"),
    pytest.param(COMMIT.upper().encode(), None, id="uppercase-commit"),
    pytest.param("é".encode() * 40, None, id="e-acute-x40"),
    pytest.param(COMMIT.encode() + b"\r\n", None, id="crlf"),
    pytest.param(b"x" * 40, None, id="40-non-hex"),
    pytest.param(b"", None, id="empty"),
    pytest.param(b"$(touch pwned)" + b"a" * 26, None, id="substitution-attempt"),
]


@pytest.mark.slow  # runs the hot command in a real shell
@pytest.mark.parametrize("shell", _SHELLS)
@pytest.mark.parametrize(("host", "event", "command", "manifest"), _HOT_CASES)
def test_t2_no_record_launches_the_branch(
    shell: str, host: str, event: str, command: str, manifest: Path, tmp_path: Path
) -> None:
    assert _from_value(shell, command, tmp_path) == _expected(command, None)


# Every record in /bin/sh, the shell Claude Code runs hooks in and the one the Codex wrap
# starts. bash and zsh, which only parse the Codex wrapper, get the cases where a shell can
# differ: a valid commit, a short value, and the two that leak through a locale-aware range.
_CORE_RECORD_IDS = {"valid-commit", "too-short", "ABCDE-x8", "e-acute-x40"}
_SHELL_RECORDS: list[Any] = [
    pytest.param(sh.values[0], *rec.values, id=f"{sh.id}-{rec.id}")
    for sh in _SHELLS
    for rec in _RECORDS
    if sh.id == "sh" or rec.id in _CORE_RECORD_IDS
]


@pytest.mark.slow  # runs the hot command in a real shell
@pytest.mark.parametrize(("host", "event", "command", "manifest"), _HOT_CASES)
@pytest.mark.parametrize(("shell", "content", "want"), _SHELL_RECORDS)
def test_t2_the_record_decides_the_launch_ref(
    shell: str,
    host: str,
    event: str,
    command: str,
    manifest: Path,
    content: bytes,
    want: str | None,
    tmp_path: Path,
) -> None:
    _record(tmp_path, content)
    assert _from_value(shell, command, tmp_path) == _expected(command, want)
    assert not (tmp_path / "project" / "pwned").exists()
    assert not (tmp_path / "pwned").exists()


# A snapshot cut with ``release-public.sh --ref X`` rewrites the literal ref of every command to X.
_PINNED = "v0.9.0"


@pytest.mark.slow  # runs the hot command in a real shell
@pytest.mark.parametrize(("host", "event", "command", "manifest"), _HOT_CASES)
def test_a1_a_pinned_ref_keeps_its_ref_whatever_the_record_holds(
    host: str, event: str, command: str, manifest: Path, tmp_path: Path
) -> None:
    """The record is the commit an install of the branch resolved, written by any such session
    on the machine. A pinned plugin (the ref rewritten as ``release-public.sh --ref`` does) must
    not be repointed at it: that would run a branch commit under a tagged release, and a newer
    one than the tag after a machine's other session updated. Only a fallback on the branch takes
    the record. /bin/sh, the shell the host runs and the Codex wrap starts."""
    pinned = _REF.sub(f"sidegraph.git@{_PINNED}", command)
    assert _ref(pinned) == _PINNED
    _record(tmp_path, COMMIT.encode())
    assert _from_value("/bin/sh", pinned, tmp_path) == _URL + _PINNED


@pytest.mark.slow  # runs the hot command in a real shell
@pytest.mark.parametrize(("host", "event", "command", "manifest"), _HOT_CASES)
def test_a2_a_fallback_on_the_branch_takes_the_record(
    host: str, event: str, command: str, manifest: Path, tmp_path: Path
) -> None:
    """The other half of A1, on a command whose ref is rewritten to the branch whatever the
    manifest carries, so a snapshot run exercises it too: the branch install is the one the
    record exists for."""
    on_branch = _REF.sub(f"sidegraph.git@{_BRANCH}", command)
    assert _ref(on_branch) == _BRANCH
    _record(tmp_path, COMMIT.encode())
    assert _from_value("/bin/sh", on_branch, tmp_path) == _URL + COMMIT


@pytest.mark.slow  # runs the hot command in a real shell
@pytest.mark.parametrize("shell", _SHELLS)
@pytest.mark.parametrize(("host", "event", "command", "manifest"), _HOT_CASES)
def test_t2_a_fifo_or_a_directory_is_not_read(
    shell: str, host: str, event: str, command: str, manifest: Path, tmp_path: Path
) -> None:
    """``cat`` on a FIFO blocks forever: `[ -f ]` must refuse it before any read."""
    record = tmp_path / "cache" / "sidegraph" / "launch-commit"
    record.parent.mkdir(parents=True)
    os.mkfifo(record)
    assert _from_value(shell, command, tmp_path) == _expected(command, None)
    record.unlink()
    record.mkdir()
    assert _from_value(shell, command, tmp_path) == _expected(command, None)


@pytest.mark.slow  # runs the hot command in a real shell
@pytest.mark.parametrize("shell", _SHELLS)
@pytest.mark.parametrize(("host", "event", "command", "manifest"), _HOT_CASES)
def test_t2_without_xdg_the_record_is_under_home(
    shell: str, host: str, event: str, command: str, manifest: Path, tmp_path: Path
) -> None:
    _record(tmp_path, COMMIT.encode(), home=True)
    assert _from_value(shell, command, tmp_path, xdg=False) == _expected(command, COMMIT)


def _utf8_locale() -> str | None:
    listed = subprocess.run(["locale", "-a"], capture_output=True, text=True, check=False).stdout
    for name in ("en_US.UTF-8", "en_US.utf8"):
        if name in listed.split():
            return name
    return None


@pytest.mark.slow  # runs the hot command in a real shell
@pytest.mark.parametrize("shell", _SHELLS)
@pytest.mark.parametrize(("host", "event", "command", "manifest"), _HOT_CASES)
@pytest.mark.parametrize(
    "content", [b"ABCDE" * 8, "é".encode() * 40], ids=["ABCDE-x8", "e-acute-x40"]
)
def test_t2_the_hex_check_does_not_follow_the_locale(
    shell: str, host: str, event: str, command: str, manifest: Path, content: bytes, tmp_path: Path
) -> None:
    """In macOS ``/bin/sh`` under a UTF-8 locale ``[0-9a-f]`` also matches ``A-E`` and letters
    like ``é``; the command lists the digits and letters instead."""
    locale = _utf8_locale()
    if locale is None:
        pytest.skip("no en_US UTF-8 locale on this platform")
    _record(tmp_path, content)
    assert _from_value(shell, command, tmp_path, locale=locale) == _expected(command, None)
