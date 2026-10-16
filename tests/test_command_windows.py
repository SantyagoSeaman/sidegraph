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

Red targets, to be proven by the CI run (no Windows machine has run them yet): break the
``-not $?`` guard, break the pin regex, drop the ``Set-Location``. The classification of a
command as public or internal is checked on every platform by the unit tests below.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="runs PowerShell on Windows")

_ROOT = Path(__file__).resolve().parent.parent
_CODEX = _ROOT / "plugin" / "sidegraph" / "codex"
_URL = "git+https://github.com/SantyagoSeaman/sidegraph.git@"
_REF = re.compile(r"sidegraph\.git@([A-Za-z0-9._/-]+)")
_ENTRY = {
    "SessionStart": "sidegraph-session-start",
    "Stop": "sidegraph-stop",
    "SubagentStart": "sidegraph-subagent-start",
}
_SESSION_MESSAGE = (
    "Sidegraph: the SessionStart hook could not start (uv/uvx, network or project path); "
    "run the hook command in a terminal to see the error"
)
_COMMIT = "f5babfb86a9562140acef8ddc61e7fe4a5933f00"
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


def main():
    stdin = sys.stdin.buffer.read()
    record = {
        "argv": sys.argv[1:],
        "cwd": os.getcwd(),
        "env": {key: os.environ.get(key) for key in KEYS},
        "stdin": stdin.hex(),
    }
    with open(os.environ["STUB_RECORD"], "w", encoding="utf-8") as out:
        json.dump(record, out)
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
            (hook,) = [h for g in groups for h in g["hooks"]]
            command = hook["commandWindows"]
            kind = _kind(command)
            cases.append(pytest.param(kind, event, command, id=f"{manifest.name}-{event}"))
    return cases


_CASES = _cases()
_SHELLS = [
    pytest.param(name, id=name) for name in ("powershell", "pwsh") if shutil.which(name) is not None
]


def test_the_scan_found_every_command_windows() -> None:
    assert len(_CASES) >= 3


_DEV_SESSION_MESSAGE = (
    "uv run --package sidegraph --frozen --no-active sidegraph-session-start; "
    'if (-not $?) { \'{"systemMessage":"Sidegraph: ... (uv/uvx, network or project path) ..."}\' }'
)


def test_an_internal_command_is_not_taken_for_a_public_one() -> None:
    """The internal SessionStart fallback message says ``uv/uvx``; that once made it public."""
    assert _kind(_DEV_SESSION_MESSAGE) == "dev"
    assert _kind("uvx --from git+https://x.git@main sidegraph-stop") == "public"


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
    cache: bool = True,
) -> tuple[subprocess.CompletedProcess[bytes], Path]:
    record = tmp_path / "record.json"
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("SIDEGRAPH_") and k not in ("UV_PROJECT_ENVIRONMENT", "XDG_CACHE_HOME")
    }
    env["PATH"] = path
    env["USERPROFILE"] = str(tmp_path / "home")
    env["GIT_CEILING_DIRECTORIES"] = str(tmp_path.parent)
    env["STUB_RECORD"] = str(record)
    if cache:
        env["XDG_CACHE_HOME"] = str(tmp_path / "cache")
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


def _read(record: Path) -> dict[str, Any]:
    assert record.is_file(), "the stub did not run"
    return json.loads(record.read_text(encoding="utf-8"))


def _write_pin(tmp_path: Path, data: bytes, *, home: bool = False) -> None:
    base = tmp_path / "home" / ".cache" if home else tmp_path / "cache"
    target = base / "sidegraph" / "launch-commit"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


def _expected_argv(kind: str, event: str, command: str, ref: str | None = None) -> list[str]:
    entry = _ENTRY[event]
    if kind == "dev":
        return ["run", "--package", "sidegraph", "--frozen", "--no-active", entry]
    return ["--from", _URL + (ref or _REF.findall(command)[0]), entry]


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
    assert seen["env"]["SIDEGRAPH_DIR"] == ".sidegraph"
    assert seen["env"]["SIDEGRAPH_GRAPH"] == "graphify-out/graph.json"
    assert seen["env"]["SIDEGRAPH_HOST"] == ("codex" if event == "SessionStart" else None)
    assert seen["env"]["UV_PROJECT_ENVIRONMENT"] == (".venv" if kind == "dev" else None)
    assert bytes.fromhex(seen["stdin"]) == _PAYLOAD
    assert seen["argv"] == _expected_argv(kind, event, command)


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
    _read(record)


@windows_only
@pytest.mark.parametrize("shell", _SHELLS)
@pytest.mark.parametrize(("kind", "event", "command"), _CASES)
def test_a_launch_that_exits_non_zero_prints_the_fallback(
    kind: str, event: str, command: str, shell: str, repo: Path, stub_dir: Path, tmp_path: Path
) -> None:
    result, _record = _run(
        shell,
        command,
        cwd=repo,
        path=_path(stub=stub_dir),
        tmp_path=tmp_path,
        stub_env={"STUB_EXIT": "2"},
    )
    lines = result.stdout.decode().strip().splitlines()
    assert lines[0] == "STUB-OUT"
    expected = {"systemMessage": _SESSION_MESSAGE} if event == "SessionStart" else {}
    assert json.loads(lines[-1]) == expected


@windows_only
@pytest.mark.parametrize("shell", _SHELLS)
@pytest.mark.parametrize(("kind", "event", "command"), _CASES)
def test_the_recorded_commit_pins_only_the_public_hot_hooks(
    kind: str, event: str, command: str, shell: str, repo: Path, stub_dir: Path, tmp_path: Path
) -> None:
    _write_pin(tmp_path, _COMMIT.encode())
    result, record = _run(shell, command, cwd=repo, path=_path(stub=stub_dir), tmp_path=tmp_path)
    assert result.returncode == 0, result.stderr
    seen = _read(record)
    ref = _REF.findall(command)[0] if kind == "public" else None
    pinned = kind == "public" and event != "SessionStart" and ref == "main"
    assert seen["argv"] == _expected_argv(kind, event, command, _COMMIT if pinned else ref)


@pytest.mark.parametrize("shell", _SHELLS)
@pytest.mark.parametrize(
    "data",
    [
        pytest.param(_COMMIT.upper().encode(), id="uppercase"),
        pytest.param(b"g" * 40, id="not-hex"),
        pytest.param(_COMMIT[:39].encode(), id="short"),
        pytest.param(_COMMIT.encode() + b"\n", id="41-bytes"),
        pytest.param(b"", id="empty"),
    ],
)
@windows_only
def test_a_malformed_record_falls_back_to_the_branch(
    data: bytes, shell: str, repo: Path, stub_dir: Path, tmp_path: Path
) -> None:
    command = next(c for k, e, c in (p.values for p in _CASES) if k == "public" and e == "Stop")
    _write_pin(tmp_path, data)
    result, record = _run(shell, command, cwd=repo, path=_path(stub=stub_dir), tmp_path=tmp_path)
    assert result.returncode == 0, result.stderr
    assert _read(record)["argv"] == _expected_argv("public", "Stop", command)


@windows_only
@pytest.mark.parametrize("shell", _SHELLS)
def test_the_record_is_found_under_the_home_cache_without_xdg(
    shell: str, repo: Path, stub_dir: Path, tmp_path: Path
) -> None:
    command = next(c for k, e, c in (p.values for p in _CASES) if k == "public" and e == "Stop")
    ref = _REF.findall(command)[0]
    _write_pin(tmp_path, _COMMIT.encode(), home=True)
    result, record = _run(
        shell, command, cwd=repo, path=_path(stub=stub_dir), tmp_path=tmp_path, cache=False
    )
    assert result.returncode == 0, result.stderr
    expected = _COMMIT if ref == "main" else ref
    assert _read(record)["argv"] == _expected_argv("public", "Stop", command, expected)


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
    assert seen["env"]["SIDEGRAPH_DIR"] == ".sidegraph"
    assert seen["argv"] == _expected_argv(kind, event, command)


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
    assert seen["argv"] == _expected_argv(kind, event, command)


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
