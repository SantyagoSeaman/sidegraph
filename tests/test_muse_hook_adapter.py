"""The Muse hook adapter: ``plugin/sidegraph/muse/`` forwards a Muse lifecycle hook to the public
runtime (``uvx --from=… sidegraph-<entry>``) with the environment Muse does not set, and fails
open: every failure prints ``{}`` and exits 0.

The runtime is a stub ``uvx`` on a temporary ``PATH`` that records its argv, environment, working
directory and stdin and prints a chosen stdout; nothing here reaches the network. The adapter runs
as a subprocess, the way Muse starts it. The adapter is plain Python 3.9 standard library (the
stock macOS ``python3``): see ``test_the_adapter_runs_under_the_stock_python3``.
see design/superpowers/specs/2026-10-28-muse-hooks-design.md (Adapter, Environment mapping, Check 2)
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugin" / "sidegraph"
MUSE = PLUGIN / "muse"
URL = "git+https://github.com/SantyagoSeaman/sidegraph.git@"
COMMIT = "0123456789abcdef0123456789abcdef01234567"
OTHER_COMMIT = "fedcba9876543210fedcba9876543210fedcba98"

EVENTS = {
    "SessionStart": ("session_start.py", "sidegraph-session-start"),
    "SubagentStart": ("subagent_start.py", "sidegraph-subagent-start"),
}
_STUB = """#!{python}
import json, os, sys, time
raw = sys.stdin.buffer.read()
log = os.environ["STUB_LOG"]
with open(log, "a") as handle:
    handle.write(json.dumps({{
        "argv": sys.argv[1:], "env": dict(os.environ), "cwd": os.getcwd(),
        "stdin": raw.decode("latin-1"), "pid": os.getpid(),
    }}) + "\\n")
pidfile = os.environ.get("STUB_PIDFILE")
if pidfile:
    with open(pidfile, "w") as handle:
        handle.write(str(os.getpid()))
if os.environ.get("STUB_GRANDCHILD"):
    import subprocess
    grandchild = subprocess.Popen(["sleep", "30"])
    with open(os.environ["STUB_GRANDCHILD"], "w") as handle:
        handle.write(str(grandchild.pid))
if os.environ.get("STUB_DETACHED"):
    import subprocess
    sleeper = subprocess.Popen(["sleep", "30"], start_new_session=True)
    with open(os.environ["STUB_DETACHED"], "w") as handle:
        handle.write(str(sleeper.pid))
if os.environ.get("STUB_STDERR_BYTES"):
    sys.stderr.write("e" * int(os.environ["STUB_STDERR_BYTES"]))
    sys.stderr.flush()
time.sleep(float(os.environ.get("STUB_SLEEP", "0")))
out = os.environ.get("STUB_OUT")
if out:
    sys.stdout.buffer.write(open(out, "rb").read())
    sys.stdout.buffer.flush()
sys.exit(int(os.environ.get("STUB_EXIT", "0")))
"""


class Rig:
    """One temporary world: a stub ``uvx`` on ``PATH``, a home, a project, a log of the calls."""

    def __init__(self, tmp_path: Path, python: str = sys.executable) -> None:
        self.python = python
        self.tmp = tmp_path
        self.stubs = tmp_path / "stubs"
        self.stubs.mkdir()
        self.home = tmp_path / "home"
        self.home.mkdir()
        self.project = tmp_path / "project"
        self.project.mkdir()
        self.adapter_cwd = tmp_path / "adapter-cwd"
        self.adapter_cwd.mkdir()
        self.log = tmp_path / "calls.jsonl"
        self.out = tmp_path / "stub-out"
        self.write_uvx()
        self.set_stdout(b"{}\n")

    def write_uvx(self) -> None:
        uvx = self.stubs / "uvx"
        uvx.write_text(_STUB.format(python=sys.executable))
        uvx.chmod(uvx.stat().st_mode | stat.S_IXUSR)

    def set_stdout(self, data: bytes) -> None:
        self.out.write_bytes(data)

    def env(self, **extra: str) -> dict[str, str]:
        env = {
            "PATH": f"{self.stubs}:/usr/bin:/bin",
            "HOME": str(self.home),
            "LANG": "en_US.UTF-8",
            "MUSE_PLUGIN_ROOT": str(PLUGIN),
            "STUB_LOG": str(self.log),
            "STUB_OUT": str(self.out),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        env.update(extra)
        return env

    def payload(self, event: str = "SessionStart", **extra: Any) -> bytes:
        body: dict[str, Any] = {
            "hook_event_name": event,
            "session_id": "s-1",
            "cwd": str(self.project),
            "transcript_path": None,
        }
        body.update(extra)
        return json.dumps(body).encode()

    def run(
        self,
        stdin: bytes,
        *,
        entry: str = "session_start.py",
        env: dict[str, str] | None = None,
        python: str | None = None,
        muse_dir: Path = MUSE,
        timeout: float = 20,
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            [python or self.python, str(muse_dir / entry)],
            input=stdin,
            capture_output=True,
            env=self.env() if env is None else env,
            cwd=self.adapter_cwd,
            timeout=timeout,
            check=False,
        )

    def calls(self) -> list[dict[str, Any]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def record(self, content: bytes, *, base: Path | None = None) -> Path:
        path = (base or self.home / ".cache") / "sidegraph" / "launch-commit"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path


# tests/conftest.py strips every SIDEGRAPH_* variable before each test, so the variable CI sets
# is read once, at import, as that file documents for the sandbox variables.
_CONFIGURED_PY39 = os.environ.get("SIDEGRAPH_TEST_PY39")
_ON_CI = os.environ.get("GITHUB_ACTIONS") == "true"


def _py39(configured: str | None, on_ci: bool) -> str | None:
    """The Python 3.9 the adapter must run on. ``configured`` (``$SIDEGRAPH_TEST_PY39``, which CI
    sets) is used and must be right: a missing path or another version fails the run. Unset on
    CI also fails, so the gate cannot pass by skipping. Unset elsewhere, the stock macOS
    ``python3`` when it reports 3.9, else ``None``."""
    if configured:
        probe = [configured, "-c", "import sys; print(sys.version_info[:2] == (3, 9))"]
        try:
            ok = subprocess.run(probe, capture_output=True, text=True).stdout.strip() == "True"
        except OSError:
            ok = False
        if not ok:
            pytest.fail(f"SIDEGRAPH_TEST_PY39={configured} is missing or is not Python 3.9")
        return configured
    if on_ci:
        pytest.fail("SIDEGRAPH_TEST_PY39 is not set on CI: the Python 3.9 gate would be skipped")
    stock = "/usr/bin/python3"
    if os.path.exists(stock):
        out = subprocess.run([stock, "--version"], capture_output=True, text=True, check=False)
        if (out.stdout or out.stderr).strip().startswith("Python 3.9."):
            return stock
    return None


@pytest.fixture
def py39() -> str:
    found = _py39(_CONFIGURED_PY39, _ON_CI)
    if found is None:
        pytest.skip("no Python 3.9: set SIDEGRAPH_TEST_PY39 or use the stock /usr/bin/python3")
    return found


@pytest.fixture(params=["host", "py39"])
def rig(request: pytest.FixtureRequest, tmp_path: Path) -> Rig:
    """Every test using this runs the adapter under the host interpreter and under 3.9."""
    python = sys.executable if request.param == "host" else request.getfixturevalue("py39")
    return Rig(tmp_path, python)


def _from(call: dict[str, Any]) -> str:
    (flag,) = [a for a in call["argv"] if a.startswith("--from=")]
    return flag.removeprefix("--from=")


# -- the shipped surface ---------------------------------------------------------------------


def test_entry_files_are_the_three_lines_of_the_spec() -> None:
    for event, (name, entry_point) in EVENTS.items():
        assert (
            MUSE / name
        ).read_text() == f'import hook\n\nhook.main("{event}", "{entry_point}")\n'


def test_the_shared_module_is_not_an_entry_and_there_is_no_package_marker() -> None:
    assert (MUSE / "hook.py").is_file()
    assert not (MUSE / "__init__.py").exists()
    assert (MUSE / "hook.py").read_text().startswith(("from __future__ import annotations", '"""'))
    assert "from __future__ import annotations" in (MUSE / "hook.py").read_text()


def test_the_runtime_url_is_spelled_once_in_the_shared_module() -> None:
    """``release-public.sh --ref`` rewrites ``sidegraph.git@main`` by text: one spelling moves."""
    text = (MUSE / "hook.py").read_text()
    assert text.count("sidegraph.git@main") == 1
    for name, _ in EVENTS.values():
        assert "sidegraph.git@" not in (MUSE / name).read_text()


# -- forwarding ------------------------------------------------------------------------------


@pytest.mark.parametrize("event", EVENTS)
def test_the_adapter_forwards_payload_argv_and_stdout_verbatim(rig: Rig, event: str) -> None:
    name, entry_point = EVENTS[event]
    answer = (
        b'{"hookSpecificOutput": {"hookEventName": "%s", "additionalContext": "caf\xc3\xa9"}}\n'
    )
    rig.set_stdout(answer % event.encode())
    stdin = rig.payload(event, note="café ☃").rstrip()  # no trailing newline

    result = rig.run(stdin, entry=name)

    assert result.returncode == 0, result.stderr
    assert result.stdout == answer % event.encode()
    (call,) = rig.calls()
    assert call["argv"] == [f"--from={URL}main", entry_point]
    assert call["stdin"].encode("latin-1") == stdin


def test_the_child_environment_is_the_adapters_own_plus_five_keys(rig: Rig) -> None:
    env = rig.env(
        SIDEGRAPH_DIR="elsewhere",
        SIDEGRAPH_HOST="claude",
        SIDEGRAPH_GRAPH="elsewhere.json",
        CLAUDE_PROJECT_DIR="/elsewhere",
        CLAUDE_PLUGIN_ROOT="/elsewhere",
    )

    result = rig.run(rig.payload(), env=env)

    assert result.stdout == b"{}\n"
    (call,) = rig.calls()
    child = call["env"]
    assert child["PATH"] == env["PATH"] and child["HOME"] == env["HOME"]
    assert child["LANG"] == "en_US.UTF-8" and child["MUSE_PLUGIN_ROOT"] == str(PLUGIN)
    assert child["SIDEGRAPH_HOST"] == "muse"
    assert child["SIDEGRAPH_DIR"] == ".sidegraph"
    assert child["SIDEGRAPH_GRAPH"] == "graphify-out/graph.json"
    assert child["CLAUDE_PROJECT_DIR"] == str(rig.project)
    assert child["CLAUDE_PLUGIN_ROOT"] == str(PLUGIN)


def test_the_child_runs_in_the_payload_cwd(rig: Rig) -> None:
    nested = rig.project / "pkg" / "deep"
    nested.mkdir(parents=True)

    rig.run(rig.payload(cwd=str(nested)))

    (call,) = rig.calls()
    assert Path(call["cwd"]).resolve() == nested.resolve()
    assert call["env"]["CLAUDE_PROJECT_DIR"] == str(nested)


@pytest.mark.parametrize("kind", ["missing", "file", "absent-key"])
def test_a_missing_or_non_directory_cwd_leaves_the_adapters_cwd(rig: Rig, kind: str) -> None:
    body: dict[str, Any] = {"hook_event_name": "SessionStart", "session_id": "s-1"}
    if kind == "missing":
        body["cwd"] = str(rig.tmp / "nope")
    elif kind == "file":
        (rig.tmp / "plain").write_text("x")
        body["cwd"] = str(rig.tmp / "plain")

    result = rig.run(json.dumps(body).encode())

    assert result.returncode == 0 and result.stdout == b"{}\n"
    (call,) = rig.calls()
    assert Path(call["cwd"]).resolve() == rig.adapter_cwd.resolve()


def test_a_payload_without_an_event_name_still_forwards(rig: Rig) -> None:
    stdin = json.dumps({"session_id": "s-1", "cwd": str(rig.project)}).encode()

    rig.run(stdin)

    assert len(rig.calls()) == 1


@pytest.mark.parametrize(
    ("entry", "other"),
    [("session_start.py", "SubagentStart"), ("subagent_start.py", "SessionStart")],
)
def test_a_payload_naming_another_event_prints_empty_json_and_spawns_nothing(
    rig: Rig, entry: str, other: str
) -> None:
    result = rig.run(rig.payload(other), entry=entry)

    assert result.returncode == 0 and result.stdout.strip() == b"{}"
    assert rig.calls() == []


# -- the launch source -----------------------------------------------------------------------

_SHORT = COMMIT[:39]
_BAD_RECORDS: list[Any] = [
    pytest.param(None, id="missing"),
    pytest.param(b"abc", id="short"),
    pytest.param(_SHORT.encode(), id="39-hex"),
    pytest.param((COMMIT + "0").encode(), id="41-hex"),
    pytest.param((COMMIT + "0" * 5000).encode(), id="overlong-file"),
    pytest.param(COMMIT.upper().encode(), id="uppercase"),
    pytest.param(b"x" * 40, id="40-non-hex"),
    pytest.param("é".encode() * 20, id="e-acute-x20"),
    pytest.param(COMMIT.encode() + b"\r\n", id="crlf"),
    pytest.param("é".encode() * 40, id="e-acute-x40"),
    pytest.param(b"g" * 40, id="g-x40"),
    pytest.param(COMMIT.encode() + b"\x00", id="nul-terminated"),
    pytest.param(COMMIT.encode() + b"\x00" + b"a" * 10, id="nul-then-more-hex"),
    pytest.param(b"", id="empty"),
]


def _subagent(rig: Rig, **kwargs: Any) -> str:
    result = rig.run(rig.payload("SubagentStart"), entry="subagent_start.py", **kwargs)
    assert result.returncode == 0, result.stderr
    (call,) = rig.calls()
    return _from(call)


def test_subagent_start_launches_from_the_recorded_commit(rig: Rig) -> None:
    rig.record(COMMIT.encode())

    assert _subagent(rig) == f"{URL}{COMMIT}"


def test_a_record_with_one_trailing_lf_is_accepted(rig: Rig) -> None:
    """The shell's ``$(head -c 41 …)`` drops the newline, so the reader does too."""
    rig.record(COMMIT.encode() + b"\n")

    assert _subagent(rig) == f"{URL}{COMMIT}"


def test_session_start_always_launches_from_main_whatever_is_recorded(rig: Rig) -> None:
    rig.record(COMMIT.encode())

    rig.run(rig.payload("SessionStart"))

    (call,) = rig.calls()
    assert _from(call) == f"{URL}main"


@pytest.mark.parametrize("content", _BAD_RECORDS)
def test_a_record_that_is_not_exactly_forty_lowercase_hex_launches_from_main(
    rig: Rig, content: bytes | None
) -> None:
    if content is not None:
        rig.record(content)

    assert _subagent(rig) == f"{URL}main"


def test_a_directory_at_the_record_path_falls_back(rig: Rig) -> None:
    (rig.home / ".cache" / "sidegraph" / "launch-commit").mkdir(parents=True)

    assert _subagent(rig) == f"{URL}main"


def test_a_fifo_at_the_record_path_falls_back_without_blocking(rig: Rig) -> None:
    """``[ -f ]`` refuses a FIFO before any read; opening one would block forever. The run has a
    deadline so a regression fails this test instead of hanging the suite."""
    path = rig.home / ".cache" / "sidegraph" / "launch-commit"
    path.parent.mkdir(parents=True)
    os.mkfifo(path)

    assert _subagent(rig, timeout=10) == f"{URL}main"


def test_xdg_cache_home_decides_where_the_record_is_and_empty_means_unset(rig: Rig) -> None:
    xdg = rig.tmp / "xdg"
    rig.record(COMMIT.encode(), base=xdg)
    rig.record(OTHER_COMMIT.encode())

    assert _subagent(rig, env=rig.env(XDG_CACHE_HOME=str(xdg))) == f"{URL}{COMMIT}"
    rig.log.unlink()
    assert _subagent(rig, env=rig.env(XDG_CACHE_HOME="")) == f"{URL}{OTHER_COMMIT}"


def test_a_fallback_pinned_to_another_ref_ignores_the_record(rig: Rig, tmp_path: Path) -> None:
    """A snapshot cut with ``release-public.sh --ref`` rewrites the one URL spelling; the copy
    here is rewritten the same way and must keep its ref whatever the record holds."""
    pinned = tmp_path / "pinned-muse"
    shutil.copytree(MUSE, pinned, ignore=shutil.ignore_patterns("__pycache__"))
    hook = pinned / "hook.py"
    pinned_ref = "v0.9.0"
    hook.write_text(hook.read_text().replace("sidegraph.git@main", f"sidegraph.git@{pinned_ref}"))
    rig.record(COMMIT.encode())

    assert _subagent(rig, muse_dir=pinned) == f"{URL}v0.9.0"


# -- failing open ----------------------------------------------------------------------------


def test_a_missing_uvx_prints_empty_json_and_exits_zero(rig: Rig) -> None:
    (rig.stubs / "uvx").unlink()

    result = rig.run(rig.payload())

    assert result.returncode == 0 and result.stdout.strip() == b"{}"


@pytest.mark.parametrize(
    ("stdout", "code"),
    [
        (b'{"systemMessage": "partial"}\n', 3),
        (b"not json\n", 0),
        (b"", 0),
        (b"[1, 2]\n", 0),
        (b"\xff\xfe", 0),
    ],
    ids=["non-zero-exit", "unparseable", "empty", "not-an-object", "not-utf8"],
)
def test_a_failing_or_unparseable_child_prints_empty_json(
    rig: Rig, stdout: bytes, code: int
) -> None:
    rig.set_stdout(stdout)

    result = rig.run(rig.payload(), env=rig.env(STUB_EXIT=str(code)))

    assert result.returncode == 0 and result.stdout.strip() == b"{}"


# -- the output guard ------------------------------------------------------------------------

_CAP = 15 * 1024


def _json_of_size(size: int) -> bytes:
    filler = size - len(b'{"hookSpecificOutput":{"additionalContext":""}}')
    body = b'{"hookSpecificOutput":{"additionalContext":"' + b"a" * filler + b'"}}'
    assert len(body) == size
    return body


def test_output_at_the_cap_is_forwarded_byte_for_byte(rig: Rig) -> None:
    body = _json_of_size(_CAP)
    rig.set_stdout(body)

    assert rig.run(rig.payload()).stdout == body


@pytest.mark.parametrize("size", [_CAP + 1, 16000, 20 * 1024])
@pytest.mark.parametrize("event", EVENTS)
def test_output_over_the_cap_is_replaced_by_a_message_naming_event_and_size(
    rig: Rig, event: str, size: int
) -> None:
    rig.set_stdout(_json_of_size(size))

    result = rig.run(rig.payload(event), entry=EVENTS[event][0])

    out = json.loads(result.stdout)
    assert result.returncode == 0 and list(out) == ["systemMessage"]
    assert event in out["systemMessage"] and str(size) in out["systemMessage"]
    assert "16 KiB" in out["systemMessage"]
    assert len(out["systemMessage"]) < 1000


def test_child_stderr_is_never_forwarded(rig: Rig) -> None:
    body = _json_of_size(100)
    rig.set_stdout(body)

    started = time.monotonic()
    result = rig.run(rig.payload(), env=rig.env(STUB_STDERR_BYTES=str(256 * 1024)))

    assert result.stdout == body and result.stderr == b""
    assert time.monotonic() - started < 10, "a full stderr pipe stalled the child"


# -- the timeout -----------------------------------------------------------------------------


def _load_hook() -> ModuleType:
    spec = importlib.util.spec_from_file_location("sg_muse_hook", MUSE / "hook.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _with_session_start_timeout(tmp_path: Path, ms: int) -> Path:
    """A copy of the adapter whose SessionStart table entry is ``ms``: the child's budget is
    ``ms`` minus the margin, so the test does not wait out the real 14 s."""
    copy = tmp_path / "short-muse"
    shutil.copytree(MUSE, copy, ignore=shutil.ignore_patterns("__pycache__"))
    hook = copy / "hook.py"
    text = hook.read_text()
    assert '"SessionStart": 15000' in text
    hook.write_text(text.replace('"SessionStart": 15000', f'"SessionStart": {ms}'))
    return copy


def test_a_child_that_outlives_the_timeout_is_killed_and_the_adapter_answers_empty_json(
    rig: Rig,
) -> None:
    """The table is 2 s, so the child's budget is 1 s; the stub sleeps 30 s."""
    short = _with_session_start_timeout(rig.tmp, 2000)
    pidfile = rig.tmp / "stub.pid"

    started = time.monotonic()
    result = rig.run(
        rig.payload(),
        env=rig.env(STUB_SLEEP="30", STUB_PIDFILE=str(pidfile)),
        muse_dir=short,
    )
    elapsed = time.monotonic() - started

    assert result.returncode == 0 and result.stdout.strip() == b"{}"
    pid = int(pidfile.read_text())
    try:
        assert elapsed < 2.0 + 1.0, "the adapter answered after Muse would have killed it"
        _assert_gone(pid)
    finally:
        _kill(pid)


def _assert_gone(pid: int) -> None:
    """Poll to a deadline: a killed process is a zombie until its reaper (PID 1 on Linux) runs.
    A process still alive after it is killed here, so a red run leaves nothing behind."""
    deadline = time.monotonic() + 5
    while _alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _alive(pid), f"process {pid} was left running"


def _kill(pid: int) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGKILL)


def test_a_timeout_kills_the_whole_process_group(rig: Rig) -> None:
    """A plain ``subprocess`` timeout kills ``uvx`` only; the runtime it started would live on,
    and holding the pipe open would also stall the adapter past the budget."""
    short = _with_session_start_timeout(rig.tmp, 2000)
    pidfile = rig.tmp / "grandchild.pid"

    started = time.monotonic()
    result = rig.run(
        rig.payload(),
        env=rig.env(STUB_SLEEP="30", STUB_GRANDCHILD=str(pidfile)),
        muse_dir=short,
    )
    elapsed = time.monotonic() - started

    pid = int(pidfile.read_text())
    try:
        assert result.stdout.strip() == b"{}"
        assert elapsed < 4, "the grandchild held the pipe open: the group was not killed"
        _assert_gone(pid)
    finally:
        _kill(pid)


def test_each_manifest_timeout_equals_the_adapters_table() -> None:
    hook = _load_hook()
    manifest = json.loads((PLUGIN / ".muse-plugin" / "plugin.json").read_text())
    declared = {h["event"]: h["timeoutMs"] for h in manifest["capabilities"]["hooks"]}
    assert declared == dict(hook.TIMEOUTS_MS)
    assert hook.CHILD_MARGIN_MS == 1000


# -- environment fallbacks and the whole-main guard -----------------------------------------


def test_an_invalid_cwd_or_plugin_root_deletes_the_inherited_keys(rig: Rig) -> None:
    """One rule: a source that is absent, empty or invalid deletes the key from the copy."""
    stdin = json.dumps({"hook_event_name": "SessionStart", "cwd": str(rig.tmp / "nope")}).encode()
    env = rig.env(CLAUDE_PROJECT_DIR="/inherited", CLAUDE_PLUGIN_ROOT="")
    del env["MUSE_PLUGIN_ROOT"]

    rig.run(stdin, env=env)

    (call,) = rig.calls()
    assert "CLAUDE_PROJECT_DIR" not in call["env"]
    assert "CLAUDE_PLUGIN_ROOT" not in call["env"]
    rig.log.unlink()

    rig.run(stdin, env=rig.env(MUSE_PLUGIN_ROOT="", CLAUDE_PLUGIN_ROOT="/inherited"))

    (call,) = rig.calls()
    assert "CLAUDE_PLUGIN_ROOT" not in call["env"]


def test_an_exception_anywhere_in_main_prints_empty_json(rig: Rig) -> None:
    """White-box: no input reaches the env builder's failure, so the builder is patched."""
    wrapper = rig.tmp / "wrapper.py"
    wrapper.write_text(
        "import sys\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "import hook\n"
        "def boom(payload):\n"
        "    raise RuntimeError('env building failed')\n"
        "hook._child_env = boom\n"
        "hook.main('SessionStart', 'sidegraph-session-start')\n"
    )

    result = subprocess.run(
        [rig.python, str(wrapper), str(MUSE)],
        input=rig.payload(),
        capture_output=True,
        env=rig.env(),
        timeout=20,
        check=False,
    )

    assert result.returncode == 0 and result.stdout.strip() == b"{}"
    assert rig.calls() == []


# -- Python 3.9 ------------------------------------------------------------------------------
# The whole module above runs under 3.9 too (the ``rig`` fixture); this is the syntax half.


def test_every_adapter_file_compiles_under_python39(py39: str, tmp_path: Path) -> None:
    check = (
        "import sys, py_compile; py_compile.compile(sys.argv[1], cfile=sys.argv[2], doraise=True)"
    )
    for path in sorted(MUSE.glob("*.py")):
        out = subprocess.run(
            [py39, "-c", check, str(path), str(tmp_path / (path.name + "c"))],
            capture_output=True,
            check=False,
        )
        assert out.returncode == 0, (path.name, out.stderr)


# -- the interpreter selection, the closed stdout, the bounded reap, unusable stdin ----------


def test_the_interpreter_selection_cannot_pass_by_skipping(tmp_path: Path) -> None:
    from _pytest.outcomes import Failed

    with pytest.raises(Failed):
        _py39("/bin/false", False)
    with pytest.raises(Failed):
        _py39(str(tmp_path / "missing"), False)
    with pytest.raises(Failed):
        _py39(None, True)


def test_a_configured_interpreter_that_is_3_9_is_accepted(py39: str) -> None:
    assert _py39(py39, False) == py39


def test_a_closed_stdout_still_exits_zero(rig: Rig) -> None:
    result = subprocess.run(
        ["/bin/sh", "-c", 'exec "$@" >&-', "sh", rig.python, str(MUSE / "session_start.py")],
        input=rig.payload("SubagentStart"),
        capture_output=True,
        env=rig.env(),
        timeout=20,
        check=False,
    )

    assert result.returncode == 0 and b"Traceback" not in result.stderr


def test_a_detached_grandchild_holding_the_pipes_cannot_stall_the_adapter(rig: Rig) -> None:
    short = _with_session_start_timeout(rig.tmp, 2000)
    pidfile = rig.tmp / "sleeper.pid"

    started = time.monotonic()
    try:
        result = rig.run(
            rig.payload(),
            env=rig.env(STUB_SLEEP="30", STUB_DETACHED=str(pidfile)),
            muse_dir=short,
            timeout=25,
        )
        elapsed = time.monotonic() - started
    finally:
        if pidfile.exists():
            _kill(int(pidfile.read_text()))

    assert result.returncode == 0 and result.stdout.strip() == b"{}"
    assert elapsed < 2.0 + 3.0, "the reap after the kill was not bounded"


@pytest.mark.parametrize("stdin", [b"", b"  \n", b"not json", b"{"], ids=repr)
def test_unusable_stdin_prints_empty_json_without_spawning(rig: Rig, stdin: bytes) -> None:
    result = rig.run(stdin)

    assert result.returncode == 0 and result.stdout.strip() == b"{}"
    assert rig.calls() == []


def test_the_configured_interpreter_is_captured_before_conftest_strips_it() -> None:
    """A child pytest with a wrong ``SIDEGRAPH_TEST_PY39`` must fail its [py39] case. Were the
    variable read at fixture time, conftest's scrub would hide it and the case would fall back
    to the stock 3.9 (or skip), passing."""
    target = f"{Path(__file__)}::test_output_at_the_cap_is_forwarded_byte_for_byte[py39]"
    env = dict(os.environ, SIDEGRAPH_TEST_PY39="/bin/false", PYTHONDONTWRITEBYTECODE="1")
    env.pop("GITHUB_ACTIONS", None)

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", target],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert result.returncode != 0, result.stdout
    assert "is missing or is not Python 3.9" in result.stdout
