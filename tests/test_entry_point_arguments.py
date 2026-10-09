"""Every console script answers ``--help`` without side effects, and the hook entry points
refuse any other argument instead of running the hook.

The hook scripts read their payload from stdin and used to ignore their arguments, so
``sidegraph-session-start --help`` ran the SessionStart hook in the current directory and
wrote into the real store. These tests run each script against a scratch repository, store
and HOME, and compare a byte-for-byte snapshot of all three before and after.
"""

from __future__ import annotations

import os
import subprocess
import sys
from importlib import metadata
from pathlib import Path

import pytest

from sidegraph.store import Store

# Marked per test: the tests that spawn a console script are `slow`; the in-process ones are not.

HOOKS = (
    "sidegraph-session-start",
    "sidegraph-stop",
    "sidegraph-pre-tool-use",
    "sidegraph-subagent-start",
)
HOOK_FUNCTIONS = {
    "sidegraph-session-start": ("sidegraph.host.hooks", "session_start"),
    "sidegraph-stop": ("sidegraph.host.hooks", "stop"),
    "sidegraph-pre-tool-use": ("sidegraph.host.hooks", "pre_tool_use"),
    "sidegraph-subagent-start": ("sidegraph.host.subagent", "subagent_start"),
}
TIMEOUT_SECONDS = 30


def _console_scripts() -> list[str]:
    """Every console script this distribution registers, from package metadata."""
    return sorted(
        ep.name
        for ep in metadata.distribution("sidegraph").entry_points
        if ep.group == "console_scripts"
    )


def _script_path(name: str) -> Path:
    path = Path(sys.executable).parent / name
    assert path.exists(), f"console script {name} is not installed next to {sys.executable}"
    return path


class Sandbox:
    """A scratch repository with a store in it, plus a scratch HOME and cache directory."""

    def __init__(self, root: Path) -> None:
        self.repo = root / "repo"
        self.home = root / "home"
        self.cache = root / "cache"
        self.store = self.repo / "store"
        for directory in (self.repo, self.home, self.cache, self.store):
            directory.mkdir(parents=True)
        (self.store / "marker").write_text("keep\n")
        # A real, initialised store: a hook that runs for real writes into it (index.db,
        # ledger), which the snapshot then catches. An empty directory hides that.
        Store(self.store).close()
        (self.repo / "README").write_text("scratch\n")

    @property
    def env(self) -> dict[str, str]:
        env = {
            "PATH": "/usr/bin:/bin",
            "HOME": str(self.home),
            "XDG_CACHE_HOME": str(self.cache),
            "SIDEGRAPH_DIR": str(self.store),
            "CLAUDE_PROJECT_DIR": str(self.repo),
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
        }
        # An exported tree is tested with PYTHONPATH=<export>/src; without it the subprocess
        # would silently import the editable install instead.
        if "PYTHONPATH" in os.environ:
            env["PYTHONPATH"] = os.environ["PYTHONPATH"]
        return env

    def snapshot(self) -> dict[str, bytes | None]:
        """Path -> contents for every file and directory under the repo, HOME and cache."""
        tree: dict[str, bytes | None] = {}
        for base in (self.repo, self.home, self.cache):
            tree[str(base)] = None
            for path in sorted(base.rglob("*")):
                tree[str(path)] = path.read_bytes() if path.is_file() else None
        return tree


@pytest.fixture
def sandbox(tmp_path: Path) -> Sandbox:
    return Sandbox(tmp_path)


def _run(name: str, args: list[str], sandbox: Sandbox) -> subprocess.CompletedProcess[str]:
    """Run the installed console script with a stdin that is open but never written to: a
    script that reads it blocks until the timeout and fails the test."""
    process = subprocess.Popen(
        [str(_script_path(name)), *args],
        cwd=sandbox.repo,
        env=sandbox.env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    # stdin stays open and unwritten: communicate() would close it, and a script that reads
    # stdin first would see EOF and pass.
    try:
        try:
            process.wait(timeout=TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            pytest.fail(
                f"{name} {' '.join(args)} did not exit: it is waiting on stdin or a server loop"
            )
        assert process.stdout is not None and process.stderr is not None
        return subprocess.CompletedProcess(
            process.args, process.returncode, process.stdout.read(), process.stderr.read()
        )
    finally:
        if process.poll() is None:
            process.kill()
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()
        process.wait()


def test_discovery_finds_the_hooks_and_every_script() -> None:
    scripts = _console_scripts()
    assert set(HOOKS) <= set(scripts)
    assert "sidegraph-prepare-commit-msg" in scripts
    assert "sidegraph-mcp" in scripts


@pytest.mark.slow
@pytest.mark.parametrize("name", _console_scripts())
def test_help_prints_usage_and_touches_nothing(name: str, sandbox: Sandbox) -> None:
    before = sandbox.snapshot()
    result = _run(name, ["--help"], sandbox)
    assert result.returncode == 0, result.stderr
    assert name in result.stdout, (result.stdout, result.stderr)
    assert "usage" in result.stdout.lower()
    assert sandbox.snapshot() == before


@pytest.mark.slow
@pytest.mark.parametrize("name", _console_scripts())
def test_short_help_prints_usage_and_touches_nothing(name: str, sandbox: Sandbox) -> None:
    before = sandbox.snapshot()
    result = _run(name, ["-h"], sandbox)
    assert result.returncode == 0, result.stderr
    assert name in result.stdout, (result.stdout, result.stderr)
    assert "usage" in result.stdout.lower()
    assert sandbox.snapshot() == before


@pytest.mark.slow
@pytest.mark.parametrize("name", HOOKS)
def test_hook_treats_help_anywhere_in_the_arguments_as_help(name: str, sandbox: Sandbox) -> None:
    before = sandbox.snapshot()
    result = _run(name, ["extra", "--help"], sandbox)
    assert result.returncode == 0, result.stderr
    assert "usage" in result.stdout.lower()
    assert sandbox.snapshot() == before


@pytest.mark.slow
def test_prepare_commit_msg_bad_argument_is_fail_open(sandbox: Sandbox) -> None:
    """git commit must not fail because of a stray `args:` entry: error on stderr, rc 0, the
    message file byte-identical."""
    message = sandbox.repo / "COMMIT_EDITMSG"
    message.write_bytes(b"subject\n")
    before = sandbox.snapshot()
    result = _run("sidegraph-prepare-commit-msg", [str(message), "--verbose"], sandbox)
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert "sidegraph-prepare-commit-msg: error: unexpected argument '--verbose'" in result.stderr
    assert message.read_bytes() == b"subject\n"
    assert sandbox.snapshot() == before


@pytest.mark.slow
@pytest.mark.parametrize("name", HOOKS)
@pytest.mark.parametrize("args", [["--bogus"], ["extra"], ["--help-me", "extra"]])
def test_hook_refuses_an_unexpected_argument(name: str, args: list[str], sandbox: Sandbox) -> None:
    before = sandbox.snapshot()
    result = _run(name, args, sandbox)
    assert result.returncode == 2
    assert result.stdout == ""
    assert f"{name}: error: unexpected argument '{args[0]}'" in result.stderr
    assert "usage" in result.stderr.lower()
    assert sandbox.snapshot() == before


@pytest.mark.parametrize("name", HOOKS)
@pytest.mark.parametrize("args", [["--help"], ["-h"], ["--bogus"]])
def test_hook_function_never_reads_stdin_for_an_argument(
    name: str,
    args: list[str],
    sandbox: Sandbox,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Called in-process, inside the sandbox: a stdin that fails the test when read."""
    import importlib

    for key, value in sandbox.env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.chdir(sandbox.repo)
    before = sandbox.snapshot()

    class _Forbidden(BaseException):
        pass

    class _Stdin:
        def read(self, *_args: object) -> str:
            raise _Forbidden("stdin was read")

        readline = readlines = __iter__ = read

    module_name, function_name = HOOK_FUNCTIONS[name]
    function = getattr(importlib.import_module(module_name), function_name)
    monkeypatch.setattr(sys, "argv", [name, *args])
    monkeypatch.setattr(sys, "stdin", _Stdin())
    with pytest.raises(SystemExit) as exit_info:
        function()
    assert exit_info.value.code == (0 if any(a in ("-h", "--help") for a in args) else 2)
    captured = capsys.readouterr()
    assert name in captured.out + captured.err
    assert sandbox.snapshot() == before


@pytest.mark.slow
@pytest.mark.parametrize("name", HOOKS)
def test_hook_without_arguments_still_reads_the_payload(name: str, sandbox: Sandbox) -> None:
    """The hot path is unchanged: no argument, empty stdin, a JSON answer, exit 0."""
    process = subprocess.run(
        [str(_script_path(name))],
        cwd=sandbox.repo,
        env=sandbox.env,
        input="",
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
    )
    assert process.returncode == 0, process.stderr
    assert process.stdout.strip().startswith("{")
