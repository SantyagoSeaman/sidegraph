"""The Codex hooks' ``commandWindows`` strings, run in real PowerShell on a Windows machine.

On native Windows Codex runs a hook's ``commandWindows`` instead of ``command``, through
``<powershell> -NoProfile -Command``. A default Git for Windows install puts only ``Git\\cmd`` on
PATH, which has no ``sh``, so the strings are PowerShell that both Windows PowerShell 5.1 and
PowerShell 7 parse. Here every one of them, the public twin (``uvx``) and the internal one
(``uv run``), runs under ``powershell.exe`` and ``pwsh`` from a subdirectory of a repository,
with PATH holding only the system, Git's ``cmd`` and a stub directory.

The stub is a real ``.exe``, a console script built into a temporary venv: it records its argv,
cwd, environment and stdin to a file and prints a marker. A ``.cmd`` stub would test cmd.exe's
stdin handling, not the hook's. Windows only; the ``windows-core`` CI job runs it.

The public twin tries ``uvx --offline`` first with its output captured, and runs the online
launch only when the exit code says the offline one failed; the online output is captured too
(``$p = uvx ...``) and written only when its exit code is 0, so a failed online attempt prints
the fallback alone. The stub records every call (one JSON line each), so a test sees how many
launches ran, with which arguments and with which stdin.

Red targets, to be proven by the CI run (no Windows machine has run them yet): write the online
output uncaptured (``uvx ...; if (-not $?) ...``: the non-zero test then sees ``STUB-OUT`` before
the fallback), break the pinned argv, drop the ``Set-Location``, drop ``--offline``, decide the
offline attempt with ``$?`` (the stderr test then sees a second launch on Windows PowerShell 5.1).
The classification of a command as public or internal is checked on every platform by the unit
tests below.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="runs PowerShell on Windows")

_ROOT = Path(__file__).resolve().parent.parent
_CODEX = _ROOT / "plugin" / "sidegraph" / "codex"
_VERSION = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
    "version"
]
_ENTRY = {
    "SessionStart": "sidegraph-session-start",
    "SessionBriefing": "sidegraph-session-briefing",
    "Stop": "sidegraph-stop",
    "SubagentStart": "sidegraph-subagent-start",
}
_SESSION_MESSAGE = (
    "Sidegraph: the SessionStart hook could not start (uv/uvx, network or project path); "
    "run the hook command in a terminal to see the error"
)
# ASCII on purpose: the hook never reads stdin itself, so the child inherits the handle.
_PAYLOAD = (
    json.dumps({"hook_event_name": "Stop", "session_id": "s-1", "n": [1, 2]}) + "\n"
).encode()
_RECORDED_ENV = (
    "SIDEGRAPH_HOST",
    "SIDEGRAPH_DIR",
    "SIDEGRAPH_GRAPH",
    "UV_PROJECT_ENVIRONMENT",
)

_STUB_SOURCE = """\
import json
import os
import sys

KEYS = ("SIDEGRAPH_HOST", "SIDEGRAPH_DIR", "SIDEGRAPH_GRAPH", "UV_PROJECT_ENVIRONMENT")


def record_call(stdin):
    record = {
        "argv": sys.argv[1:],
        "cwd": os.getcwd(),
        "env": {key: os.environ.get(key) for key in KEYS},
        "stdin": None if stdin is None else stdin.hex(),
    }
    with open(os.environ["STUB_RECORD"], "a", encoding="utf-8") as out:
        out.write(json.dumps(record) + "\\n")


def main():
    if "--offline" in sys.argv and os.environ.get("STUB_OFFLINE_EXIT"):
        # uv failing before it starts the entry: stdin is left unread, nothing is answered but
        # a line the hook must not print
        record_call(None)
        sys.stdout.write("OFFLINE-OUT\\n")
        sys.stdout.flush()
        sys.exit(int(os.environ["STUB_OFFLINE_EXIT"]))
    record_call(sys.stdin.buffer.read())
    sys.stdout.write("STUB-OUT\\n")
    sys.stdout.flush()
    if os.environ.get("STUB_STDERR"):
        sys.stderr.write(os.environ["STUB_STDERR"] + "\\n")
        sys.stderr.flush()
    sys.exit(int(os.environ.get("STUB_EXIT", "0")))
"""

_STUB_PYPROJECT = """\
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "stubpkg"
version = "0"

[project.scripts]
uvx = "stubpkg:main"
uv = "stubpkg:main"

[tool.hatch.build.targets.wheel]
packages = ["stubpkg"]
"""


def _kind(command: str) -> str:
    """``dev`` for an internal ``uv run --package`` command, else ``public``. Read from the
    command text: the public twin is renamed onto ``hooks.json`` in a snapshot, so a file name
    says nothing, and the internal SessionStart message mentions ``uvx`` too."""
    return "dev" if "uv run --package" in command else "public"


def _cases() -> list[Any]:
    cases = []
    for manifest in sorted(_CODEX.glob("hooks*.json")):
        hooks = json.loads(manifest.read_text(encoding="utf-8"))["hooks"]
        for event, groups in hooks.items():
            for hook in (h for g in groups for h in g["hooks"]):
                command = hook["commandWindows"]
                label = "SessionBriefing" if _ENTRY["SessionBriefing"] in command else event
                cases.append(
                    pytest.param(_kind(command), label, command, id=f"{manifest.name}-{label}")
                )
    return cases


_CASES = _cases()
_SHELLS = [
    pytest.param(name, id=name) for name in ("powershell", "pwsh") if shutil.which(name) is not None
]


def test_the_scan_found_every_command_windows() -> None:
    assert {case.values[1] for case in _CASES} == set(_ENTRY)


_DEV_SESSION_MESSAGE = (
    "uv run --package sidegraph --frozen --no-active sidegraph-session-start; "
    'if (-not $?) { \'{"systemMessage":"Sidegraph: ... (uv/uvx, network or project path) ..."}\' }'
)


def test_a_public_command_windows_decides_by_exit_code_alone() -> None:
    """Both attempts are decided by ``$LASTEXITCODE``; ``-not $?`` would let a failed online
    attempt's output reach the host before the fallback, and Windows PowerShell 5.1 sets ``$?``
    to false after a redirected stderr even at exit 0. Runs on every platform."""
    public = [c for c in _CASES if c.values[0] == "public"]
    assert len(public) >= 3
    for case in public:
        _kind_, _event, command = case.values
        assert "-not $?" not in command, case.id
        assert command.count("if ($LASTEXITCODE -eq 0)") == 2, case.id
        assert command.count("$global:LASTEXITCODE = 1;") == 2, case.id


def test_an_internal_command_is_not_taken_for_a_public_one() -> None:
    """The internal SessionStart fallback message says ``uv/uvx``; that once made it public."""
    assert _kind(_DEV_SESSION_MESSAGE) == "dev"
    assert _kind(f"uvx --from sidegraph=={_VERSION} --python 3.13 sidegraph-stop") == "public"


def test_every_manifest_command_is_classified_by_its_own_text() -> None:
    for case in _CASES:
        kind, _event, command = case.values
        assert (kind == "dev") == ("uv run --package" in command), case.id
        assert (kind == "public") == ("uvx --from" in command), case.id


@pytest.fixture(scope="session")
def stub_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A directory whose ``uvx.exe`` and ``uv.exe`` are console scripts of one stub."""
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is not installed")
    base = tmp_path_factory.mktemp("stub")
    project = base / "project"
    (project / "stubpkg").mkdir(parents=True)
    (project / "pyproject.toml").write_text(_STUB_PYPROJECT, encoding="utf-8")
    (project / "stubpkg" / "__init__.py").write_text(_STUB_SOURCE, encoding="utf-8")
    venv = base / "venv"
    subprocess.run([uv, "venv", str(venv)], check=True, capture_output=True)
    python = venv / "Scripts" / "python.exe"
    subprocess.run(
        [uv, "pip", "install", "--python", str(python), str(project)],
        check=True,
        capture_output=True,
    )
    scripts = venv / "Scripts"
    assert (scripts / "uvx.exe").is_file() and (scripts / "uv.exe").is_file()
    return scripts


def _git_dir() -> Path:
    """Git's ``cmd`` directory: what a default install puts on PATH, with ``git`` and no ``sh``."""
    found = shutil.which("git")
    if found is None:
        pytest.skip("git is not installed")
    parent = Path(found).parent
    cmd = parent.parent / "cmd"
    return cmd if (cmd / "git.exe").is_file() else parent


def _path(*, stub: Path | None, git: bool = True) -> str:
    root = Path(os.environ["SYSTEMROOT"])
    parts = [root / "System32", root, root / "System32" / "WindowsPowerShell" / "v1.0"]
    pwsh = shutil.which("pwsh")
    if pwsh:
        parts.append(Path(pwsh).parent)
    if git:
        parts.append(_git_dir())
    if stub is not None:
        parts.append(stub)
    path = os.pathsep.join(str(p) for p in parts)
    if shutil.which("sh", path=path) is not None:
        pytest.skip("this machine's Git puts sh on PATH next to git")
    return path


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "sub" / "deeper").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    return root


def _run(
    shell: str,
    command: str,
    *,
    cwd: Path,
    path: str,
    tmp_path: Path,
    stub_env: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[bytes], Path]:
    record = tmp_path / "record.json"
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("SIDEGRAPH_") and k != "UV_PROJECT_ENVIRONMENT"
    }
    env["PATH"] = path
    env["USERPROFILE"] = str(tmp_path / "home")
    env["GIT_CEILING_DIRECTORIES"] = str(tmp_path.parent)
    env["STUB_RECORD"] = str(record)
    env["SIDEGRAPH_BRIEFING_PROTOCOL"] = "on"
    env.update(stub_env or {})
    result = subprocess.run(
        [shutil.which(shell) or shell, "-NoProfile", "-Command", command],
        cwd=cwd,
        env=env,
        input=_PAYLOAD,
        capture_output=True,
        timeout=120,
        check=False,
    )
    return result, record


def _read_all(record: Path) -> list[dict[str, Any]]:
    """Every call the stub saw, oldest first."""
    assert record.is_file(), "the stub did not run"
    lines = record.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]


def _read(record: Path) -> dict[str, Any]:
    """The last call the stub saw."""
    return _read_all(record)[-1]


def _expected_argv(kind: str, event: str, *, offline: bool = True) -> list[str]:
    """The arguments of the last launch that ran: for the public twin the offline attempt (the
    only one when it succeeds), or the online one when ``offline`` is false."""
    entry = _ENTRY[event]
    if kind == "dev":
        return ["run", "--package", "sidegraph", "--frozen", "--no-active", entry]
    flag = ["--offline"] if offline else []
    return ["--from", f"sidegraph=={_VERSION}", "--python", "3.13", *flag, entry]


@windows_only
@pytest.mark.parametrize("shell", _SHELLS)
@pytest.mark.parametrize(("kind", "event", "command"), _CASES)
def test_runs_from_the_repo_root_with_the_environment_and_the_payload(
    kind: str, event: str, command: str, shell: str, repo: Path, stub_dir: Path, tmp_path: Path
) -> None:
    result, record = _run(
        shell,
        command,
        cwd=repo / "sub" / "deeper",
        path=_path(stub=stub_dir),
        tmp_path=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.decode().strip() == "STUB-OUT"
    seen = _read(record)
    assert os.path.samefile(seen["cwd"], repo), seen["cwd"]
    # The pinned launch sets no store path (the defaults hold); the internal one aims at the
    # checkout's.
    assert seen["env"]["SIDEGRAPH_DIR"] == (".sidegraph" if kind == "dev" else None)
    assert seen["env"]["SIDEGRAPH_GRAPH"] == ("graphify-out/graph.json" if kind == "dev" else None)
    host = "codex" if event in ("SessionStart", "SessionBriefing") else None
    assert seen["env"]["SIDEGRAPH_HOST"] == host
    assert seen["env"]["UV_PROJECT_ENVIRONMENT"] == (".venv" if kind == "dev" else None)
    assert bytes.fromhex(seen["stdin"]).rstrip() == _PAYLOAD.rstrip()
    assert seen["argv"] == _expected_argv(kind, event)


@windows_only
@pytest.mark.parametrize("shell", _SHELLS)
@pytest.mark.parametrize(("kind", "event", "command"), _CASES)
def test_a_launch_that_logs_to_stderr_and_exits_0_prints_no_fallback(
    kind: str, event: str, command: str, shell: str, repo: Path, stub_dir: Path, tmp_path: Path
) -> None:
    """uv and uvx write their install log to stderr on success. Windows PowerShell 5.1 sets
    ``$?`` to false after a native program writes to a redirected stderr, whatever its exit
    code, so a ``$?`` guard would append the fallback to a working hook's output."""
    result, record = _run(
        shell,
        command,
        cwd=repo,
        path=_path(stub=stub_dir),
        tmp_path=tmp_path,
        stub_env={"STUB_STDERR": "Installed 1 package in 5ms"},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.decode().strip() == "STUB-OUT"
    # One launch only. On Windows PowerShell 5.1 a ``$?`` decision after ``2>$null`` calls the
    # offline attempt failed, runs the online one on a stdin the first already read, and loses the
    # answer; the exit code alone does not.
    (call,) = _read_all(record)
    assert call["argv"] == _expected_argv(kind, event)
    assert bytes.fromhex(call["stdin"]).rstrip() == _PAYLOAD.rstrip()


@windows_only
@pytest.mark.parametrize("shell", _SHELLS)
@pytest.mark.parametrize(("kind", "event", "command"), _CASES)
def test_a_launch_that_exits_non_zero_prints_the_fallback(
    kind: str, event: str, command: str, shell: str, repo: Path, stub_dir: Path, tmp_path: Path
) -> None:
    result, record = _run(
        shell,
        command,
        cwd=repo,
        path=_path(stub=stub_dir),
        tmp_path=tmp_path,
        stub_env={"STUB_EXIT": "2"},
    )
    lines = result.stdout.decode().strip().splitlines()
    expected = {"systemMessage": _SESSION_MESSAGE} if event == "SessionStart" else {}
    assert json.loads(lines[-1]) == expected
    # The internal command writes the run's own output and then the fallback. The public twin
    # captures both attempts and writes an attempt's output only on exit 0: here both exit 2, so
    # the fallback is all the host gets.
    assert lines[:-1] == (["STUB-OUT"] if kind == "dev" and event != "SessionBriefing" else [])
    # The public twin made two launches (offline, then online), the internal one a single run.
    calls = _read_all(record)
    assert [c["argv"] for c in calls] == (
        [_expected_argv(kind, event)]
        if kind == "dev"
        else [_expected_argv(kind, event), _expected_argv(kind, event, offline=False)]
    )


_PUBLIC_CASES = [c for c in _CASES if c.values[0] == "public"]


@windows_only
@pytest.mark.parametrize("shell", _SHELLS)
@pytest.mark.parametrize(
    ("kind", "event", "command"), [c for c in _CASES if c.values[1] == "SessionBriefing"]
)
def test_briefing_with_no_enabled_flag_starts_nothing(
    kind: str, event: str, command: str, shell: str, repo: Path, stub_dir: Path, tmp_path: Path
) -> None:
    result, record = _run(
        shell,
        command,
        cwd=repo,
        path=_path(stub=stub_dir),
        tmp_path=tmp_path,
        stub_env={"SIDEGRAPH_BRIEFING_PROTOCOL": "one"},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.decode().strip() == "{}"
    assert not record.exists()


@windows_only
@pytest.mark.parametrize("shell", _SHELLS)
@pytest.mark.parametrize(("kind", "event", "command"), _PUBLIC_CASES)
def test_a_failed_offline_attempt_is_followed_by_one_online_attempt_that_gets_the_payload(
    kind: str, event: str, command: str, shell: str, repo: Path, stub_dir: Path, tmp_path: Path
) -> None:
    """The offline launch exits 1 before it starts the entry (as uv does with an empty cache),
    leaving stdin unread; the online launch then succeeds with the whole payload. The host gets
    the online answer once and none of the offline attempt's output.

    Red target (to be proven by the Windows CI run): a command with no online retry records one
    call and prints the fallback; one that writes the offline output shows ``OFFLINE-OUT``."""
    result, record = _run(
        shell,
        command,
        cwd=repo,
        path=_path(stub=stub_dir),
        tmp_path=tmp_path,
        stub_env={"STUB_OFFLINE_EXIT": "1"},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.decode().strip().splitlines() == ["STUB-OUT"]
    offline, online = _read_all(record)
    assert offline["argv"] == _expected_argv(kind, event)
    assert online["argv"] == _expected_argv(kind, event, offline=False)
    assert bytes.fromhex(online["stdin"]).rstrip() == _PAYLOAD.rstrip()


@windows_only
@pytest.mark.parametrize("shell", _SHELLS)
@pytest.mark.parametrize(("kind", "event", "command"), _CASES)
def test_a_missing_launcher_prints_the_fallback_and_exits_zero(
    kind: str, event: str, command: str, shell: str, repo: Path, tmp_path: Path
) -> None:
    result, record = _run(
        shell, command, cwd=repo / "sub", path=_path(stub=None), tmp_path=tmp_path
    )
    assert result.returncode == 0, result.stderr
    assert not record.exists()
    expected = {"systemMessage": _SESSION_MESSAGE} if event == "SessionStart" else {}
    assert json.loads(result.stdout.decode().strip()) == expected


@windows_only
@pytest.mark.parametrize("shell", _SHELLS)
@pytest.mark.parametrize(("kind", "event", "command"), _CASES)
def test_outside_a_repository_the_launcher_still_runs_where_it_is(
    kind: str, event: str, command: str, shell: str, stub_dir: Path, tmp_path: Path
) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    result, record = _run(shell, command, cwd=plain, path=_path(stub=stub_dir), tmp_path=tmp_path)
    assert result.returncode == 0, result.stderr
    seen = _read(record)
    assert os.path.samefile(seen["cwd"], plain), seen["cwd"]
    assert seen["env"]["SIDEGRAPH_DIR"] == (".sidegraph" if kind == "dev" else None)
    assert seen["argv"] == _expected_argv(kind, event)


@windows_only
@pytest.mark.parametrize("shell", _SHELLS)
@pytest.mark.parametrize(("kind", "event", "command"), _CASES)
def test_without_git_the_launcher_still_runs_where_it_is(
    kind: str, event: str, command: str, shell: str, repo: Path, stub_dir: Path, tmp_path: Path
) -> None:
    where = repo / "sub"
    result, record = _run(
        shell,
        command,
        cwd=where,
        path=_path(stub=stub_dir, git=False),
        tmp_path=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    seen = _read(record)
    assert os.path.samefile(seen["cwd"], where), seen["cwd"]
    assert seen["argv"] == _expected_argv(kind, event)


@windows_only
@pytest.mark.parametrize("shell", _SHELLS)
@pytest.mark.parametrize(("kind", "event", "command"), _CASES)
def test_a_cyrillic_repo_path_is_still_the_cwd_the_launcher_sees(
    kind: str, event: str, command: str, shell: str, stub_dir: Path, tmp_path: Path
) -> None:
    """The move to the repo root works from a subdirectory of a non-ASCII repo path.

    The manifests use ``git rev-parse --show-cdup`` (ASCII only), so the move does not depend on
    the console code page. This test passes on the CI runner even with ``--show-toplevel``,
    whose console environment decodes UTF-8; it guards the end-to-end behaviour, not the
    code-page independence (that is the ASCII-only output)."""
    root = tmp_path / "репо-проект"
    (root / "sub").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    result, record = _run(
        shell, command, cwd=root / "sub", path=_path(stub=stub_dir), tmp_path=tmp_path
    )
    assert result.returncode == 0, result.stderr
    seen = _read(record)
    assert os.path.samefile(seen["cwd"], root), seen["cwd"]
