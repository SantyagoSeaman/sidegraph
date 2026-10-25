"""Content-free diagnostics must preserve the hooks' actual delivery contract."""

from __future__ import annotations

import io
import json
import sqlite3
from contextlib import closing

import pytest

from sidegraph.host import hooks, launch, subagent
from sidegraph.store import Store


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    monkeypatch.setenv("SIDEGRAPH_DIR", str(tmp_path / ".sidegraph"))
    monkeypatch.setattr(launch, "_resolved_main_commit", lambda: None)
    return tmp_path / ".sidegraph"


def _input(monkeypatch, payload):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))


def _signal(captured, hook, stage, exception):
    assert len(captured.err.encode("ascii")) <= 256
    assert captured.err.count("\n") == 1
    assert json.loads(captured.err) == {
        "sidegraph_hook_error": True,
        "hook": hook,
        "stage": stage,
        "exception": exception,
    }
    return json.loads(captured.out)


@pytest.mark.parametrize(
    ("entry", "name"),
    [
        (hooks.session_start, "SessionStart"),
        (hooks.stop, "Stop"),
        (hooks.pre_tool_use, "PreToolUse"),
        (subagent.subagent_start, "SubagentStart"),
    ],
)
def test_invalid_utf8_is_content_free_and_fail_open(project, monkeypatch, capfd, entry, name):
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(b"secret/path/\xff")))
    entry()
    assert _signal(capfd.readouterr(), name, "payload", "UnicodeDecodeError") == {}
    assert not project.exists()


def test_stop_missing_explicit_transcript_is_visible(project, monkeypatch, capfd):
    Store(project).close()
    _input(monkeypatch, {"session_id": "private-id", "transcript_path": "secret/missing"})
    hooks.stop()
    assert _signal(capfd.readouterr(), "Stop", "transcript", "FileNotFoundError") == {}


def test_pretool_delivery_failure_and_cleanup_keep_one_answer(project, monkeypatch, capfd):
    def fail(*args, **kwargs):
        raise RuntimeError("secret query and /private/path")

    _input(monkeypatch, {})
    monkeypatch.setattr(hooks, "_hook_output", fail)
    monkeypatch.setattr(hooks._HotOpener, "close", fail)
    hooks.pre_tool_use()
    assert _signal(capfd.readouterr(), "PreToolUse", "delivery", "RuntimeError") == {}


def test_pretool_success_survives_close_failure(project, monkeypatch, capfd):
    output = {"hookSpecificOutput": {"additionalContext": "already delivered"}}
    _input(monkeypatch, {})
    monkeypatch.setattr(hooks, "_hook_output", lambda *args: output)

    def close(self):
        raise OSError("private store path")

    monkeypatch.setattr(hooks._HotOpener, "close", close)
    hooks.pre_tool_use()
    assert _signal(capfd.readouterr(), "PreToolUse", "cleanup", "OSError") == output


def test_subagent_index_error_is_visible_without_creating_store(project, monkeypatch, capfd):
    project.mkdir()
    (project / "index.db").write_bytes(b"not a database; private source text")
    before = (project / "index.db").read_bytes()
    _input(monkeypatch, {})
    subagent.subagent_start()
    assert _signal(capfd.readouterr(), "SubagentStart", "index", "DatabaseError") == {}
    assert (project / "index.db").read_bytes() == before
    assert sorted(p.name for p in project.iterdir()) == ["index.db"]


def test_session_partial_map_survives_sync_error(project, monkeypatch, capfd):
    Store(project).close()
    _input(monkeypatch, {})

    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("private locked database")

    monkeypatch.setattr("sidegraph.sync.maybe_sync", fail)
    hooks.session_start()
    output = _signal(capfd.readouterr(), "SessionStart", "sync", "OperationalError")
    assert "get_task_context" in output["hookSpecificOutput"]["additionalContext"]


_STDERR_MODES = [
    "valid",
    "windows_newlines",
    "broken",
    "closed_exec",
    "none",
    "closed_stream",
    "replaced",
]
# Every fd mode on one entry point; the other three get the healthy control and fd reuse. The
# stderr guard is one reporter shared by all four hooks, so a mode that fails for one fails for all.
_STDERR_CASES = [("SessionStart", mode) for mode in _STDERR_MODES] + [
    (name, mode)
    for name in ("Stop", "PreToolUse", "SubagentStart")
    for mode in ("valid", "replaced")
]


@pytest.mark.parametrize(("name", "mode"), _STDERR_CASES)
def test_real_process_stderr_faults_never_change_exit_or_data(tmp_path, name, mode):
    """Exercise shutdown and fd reuse, which Python-level capture cannot establish."""
    import os
    import subprocess
    import sys
    import textwrap

    if mode == "closed_exec" and os.name != "posix":
        pytest.skip("Closing fd2 at exec uses POSIX preexec_fn; other guards run on Windows")
    data = tmp_path / "synthetic-index.db"
    with closing(sqlite3.connect(data)) as conn, conn:
        conn.execute("CREATE TABLE sentinel(value TEXT)")
        conn.execute("INSERT INTO sentinel VALUES ('private contents')")
    before = data.read_bytes()
    script = textwrap.dedent("""
        import os, sys
        from sidegraph.host import hooks, launch, subagent
        name, mode, data = sys.argv[1:]
        sys.argv = ["hook"]
        launch.record_launch_commit = lambda **kwargs: None
        held = []
        if mode == "windows_newlines":
            sys.stdout.reconfigure(newline="\\r\\n")
        def reuse():
            fd = os.open(data, os.O_WRONLY | os.O_APPEND)
            assert fd == 2, fd
            held.append(fd)
        if mode == "closed_exec":
            assert sys.stderr is None
            reuse()  # Before reporter construction: fstat alone would adopt this file.
        elif mode in ("none", "closed_stream"):
            sys.stderr.close()
            os.close(2)
            if mode == "none":
                sys.stderr = None
            reuse()
        def fail():
            if mode in ("broken", "replaced"):
                os.close(2)
                if mode == "replaced":
                    reuse()  # After reporter construction: identity must reject the file.
            raise UnicodeError("private/path secret session query")
        hooks._read_payload = subagent._read_payload = fail
        entries = {"SessionStart": hooks.session_start, "Stop": hooks.stop,
                   "PreToolUse": hooks.pre_tool_use, "SubagentStart": subagent.subagent_start}
        entries[name]()
        for fd in held:
            os.close(fd)
    """)
    env = {k: v for k, v in os.environ.items() if not k.startswith("SIDEGRAPH_")}
    env.update(SIDEGRAPH_DIR=str(tmp_path / "absent"), CLAUDE_PROJECT_DIR=str(tmp_path))
    kwargs = {"preexec_fn": lambda: os.close(2)} if mode == "closed_exec" else {}
    result = subprocess.run(
        [sys.executable, "-c", script, name, mode, str(data)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        timeout=15,
        **kwargs,
    )
    assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)
    assert result.stdout.replace(b"\r\n", b"\n") == b"{}\n"
    if mode in ("valid", "windows_newlines"):
        assert json.loads(result.stderr) == {
            "sidegraph_hook_error": True,
            "hook": name,
            "stage": "payload",
            "exception": "UnicodeError",
        }
        assert result.stderr.count(b"\n") == 1 and len(result.stderr) <= 256
    else:
        assert result.stderr == b""
    assert data.read_bytes() == before
    with closing(sqlite3.connect(data)) as conn, conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert not (tmp_path / "absent").exists()


def test_reporter_bounds_hostile_class_and_is_per_invocation(capfd):
    from sidegraph.host.diagnostics import HookDiagnostics

    class PrivateError(Exception):
        def __str__(self):
            raise AssertionError("Must never stringify")

        def __repr__(self):
            raise AssertionError("Must never repr")

    PrivateError.__name__ = 'X\n\r"é' * 100
    for _ in range(2):
        reporter = HookDiagnostics("H\n" * 100)
        reporter.report("S\n" * 100, PrivateError())
        reporter.report("second", RuntimeError("secret"))
    captured = capfd.readouterr()
    assert captured.out == ""
    lines = captured.err.splitlines(keepends=True)
    assert len(lines) == 2
    for line in lines:
        assert len(line.encode("ascii")) <= 256
        parsed = json.loads(line)
        assert parsed["hook"] == "H_" * 12
        assert parsed["stage"] == "S_" * 16
        assert parsed["exception"] == ("X____" * 13)[:64]


@pytest.mark.parametrize("failure", [OSError, BlockingIOError])
def test_reporter_does_not_retry_a_failed_write(monkeypatch, capfd, failure):
    from sidegraph.host.diagnostics import HookDiagnostics

    reporter = HookDiagnostics("PreToolUse")
    attempts = []

    def fail(fd, line):
        attempts.append((fd, line))
        raise failure("secret sink path")

    monkeypatch.setattr("sidegraph.host.diagnostics.os.write", fail)
    reporter.report("payload", ValueError("secret"))
    reporter.report("cleanup", RuntimeError("another secret"))
    assert len(attempts) == 1
    assert capfd.readouterr() == ("", "")


@pytest.mark.parametrize("surface", ["pretool", "subagent"])
@pytest.mark.parametrize("refusal", ["missing", "stamp", "agent", "format", "symlink"])
def test_ordinary_index_refusals_are_quiet(project, monkeypatch, capfd, surface, refusal):
    from tests.test_store_requirement import above_running, write_requirement_marker

    Store(project).close()
    if refusal == "missing":
        (project / "index.db").unlink()
    elif refusal in ("stamp", "agent"):
        with closing(sqlite3.connect(project / "index.db")) as conn, conn:
            if refusal == "stamp":
                conn.execute("UPDATE meta SET value='unsupported' WHERE key='schema_version'")
            else:
                conn.execute("DROP TABLE retrieval_events")
                conn.execute("CREATE TABLE retrieval_events(session_id TEXT)")
    elif refusal == "format":
        write_requirement_marker(project, above_running())
    else:
        original = project / "index.db"
        moved = project.parent / "elsewhere.db"
        original.rename(moved)
        try:
            original.symlink_to(moved)
        except OSError:
            pytest.skip("Symlinks unavailable on this Windows runner")
    before = {p.name: p.read_bytes() for p in project.iterdir() if p.is_file()}
    _input(
        monkeypatch,
        {
            "session_id": "s",
            "tool_name": "Read",
            "tool_input": {"file_path": str(project.parent / "a.py")},
        },
    )
    (hooks.pre_tool_use if surface == "pretool" else subagent.subagent_start)()
    captured = capfd.readouterr()
    assert json.loads(captured.out) == {}
    assert captured.err == ""
    assert {p.name: p.read_bytes() for p in project.iterdir() if p.is_file()} == before


@pytest.mark.parametrize("opener", ["hot", "ro"])
def test_index_callback_failure_is_isolated_and_original_error_wins(project, monkeypatch, opener):
    from sidegraph import gitio, hot_index

    project.mkdir()
    (project / "index.db").write_bytes(b"placeholder")
    seen = []

    class BrokenConnection:
        def execute(self, *args):
            raise sqlite3.DatabaseError("original secret")

        def close(self):
            raise OSError("cleanup secret")

    def observe(error):
        seen.append(type(error))
        raise RuntimeError("callback secret")

    monkeypatch.setattr(sqlite3, "connect", lambda *args, **kwargs: BrokenConnection())
    open_index = hot_index.HotIndex.open if opener == "hot" else gitio.open_index_ro
    assert open_index(project, on_error=observe) is None
    assert seen == [sqlite3.DatabaseError]
    assert (project / "index.db").read_bytes() == b"placeholder"


def test_subagent_close_error_keeps_existing_empty_fallback(project, monkeypatch, capfd):
    from sidegraph.hot_index import HotIndex

    class Index:
        def memory_counts(self):
            return 2, 1, 1

        def close(self):
            raise OSError("secret")

    _input(monkeypatch, {})
    monkeypatch.setattr(HotIndex, "open", lambda *args, **kwargs: Index())
    subagent.subagent_start()
    assert _signal(capfd.readouterr(), "SubagentStart", "cleanup", "OSError") == {}


def test_session_dedupe_failure_does_not_hide_map_or_error(project, monkeypatch, capfd):
    Store(project).close()
    real_get = Store.get_meta

    def get_meta(self, key):
        if key == hooks._SESSION_START_KEY:
            raise sqlite3.OperationalError("secret dedupe lock")
        return real_get(self, key)

    monkeypatch.setattr(Store, "get_meta", get_meta)
    _input(monkeypatch, {"session_id": "s"})
    hooks.session_start()
    output = _signal(capfd.readouterr(), "SessionStart", "session", "OperationalError")
    assert "hookSpecificOutput" in output


@pytest.mark.parametrize("event", ["Stop", "SubagentStart"])
def test_whole_hook_disabled_is_quiet_even_with_bad_input(project, monkeypatch, capfd, event):
    monkeypatch.setenv(
        "SIDEGRAPH_CAPTURE_NUDGE" if event == "Stop" else "SIDEGRAPH_SUBAGENT_BRIEF", "off"
    )
    monkeypatch.setattr("sys.stdin", io.StringIO("{broken private input"))
    (hooks.stop if event == "Stop" else subagent.subagent_start)()
    captured = capfd.readouterr()
    assert captured.out == "{}\n" and captured.err == ""
    assert not project.exists()


def test_stop_ledger_sql_failure_reports_but_still_reads_gate(project, monkeypatch, capfd):
    Store(project).close()
    with closing(sqlite3.connect(project / "index.db")) as conn, conn:
        conn.execute("DROP TABLE capture_sessions")
    _input(monkeypatch, {"session_id": "s"})
    hooks.stop()
    assert _signal(capfd.readouterr(), "Stop", "capture_peek", "OperationalError") == {}


def test_session_no_graph_and_optional_bad_cache_are_quiet(project, monkeypatch, capfd):
    from sidegraph.retrieval import TOC_CACHE_KEY

    store = Store(project)
    try:
        store.set_meta(TOC_CACHE_KEY, '{"domains": ["untrusted-shape"]}')
    finally:
        store.close()
    monkeypatch.setenv("SIDEGRAPH_GRAPH", str(project.parent / "absent-graph.json"))
    _input(monkeypatch, {})
    hooks.session_start()
    captured = capfd.readouterr()
    assert "hookSpecificOutput" in json.loads(captured.out)
    assert captured.err == ""


def test_session_unreadable_graph_still_delivers_store_map(project, monkeypatch, capfd):
    Store(project).close()
    graph = project.parent / "broken-graph.json"
    graph.write_text("private invalid graph")
    monkeypatch.setenv("SIDEGRAPH_GRAPH", str(graph))
    _input(monkeypatch, {})
    hooks.session_start()
    output = _signal(capfd.readouterr(), "SessionStart", "graph", "JSONDecodeError")
    assert "hookSpecificOutput" in output


@pytest.mark.parametrize("hook", ["session", "stop"])
def test_store_format_refusal_is_quiet_on_stderr(project, monkeypatch, capfd, hook):
    from tests.test_store_requirement import above_running, write_requirement_marker

    Store(project).close()
    write_requirement_marker(project, above_running())
    before = {p.relative_to(project): p.read_bytes() for p in project.rglob("*.json")}
    _input(monkeypatch, {"session_id": "s", "transcript_path": "secret-missing"})
    (hooks.session_start if hook == "session" else hooks.stop)()
    captured = capfd.readouterr()
    output = json.loads(captured.out)
    assert captured.err == ""
    assert ("systemMessage" in output) if hook == "session" else output == {}
    assert {p.relative_to(project): p.read_bytes() for p in project.rglob("*.json")} == before


def test_rearm_subprocess_failure_remains_fail_open_and_visible(project, monkeypatch, capfd):
    import subprocess

    store = Store(project)
    store.mark_captured("s")
    store.set_meta(hooks._CAPTURE_REARM_KEY_PREFIX + "s", "2000-01-01T00:00:00+00:00")
    store.close()
    _input(monkeypatch, {"session_id": "s"})
    real_run = subprocess.run

    def run(args, *a, **kwargs):
        if args[:2] == ["git", "log"]:
            raise subprocess.TimeoutExpired(args, 2)
        return real_run(args, *a, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    hooks.stop()
    assert _signal(capfd.readouterr(), "Stop", "rearm", "TimeoutExpired") == {}


def test_actual_launch_cache_failure_is_observable(project, monkeypatch, capfd):
    blocked = project.parent / "blocked-cache"
    blocked.write_text("private synthetic cache content")
    monkeypatch.setenv("XDG_CACHE_HOME", str(blocked))
    monkeypatch.setattr(launch, "_resolved_main_commit", lambda: "a" * 40)
    errors: list[str] = []
    real_write = launch._record_main_commit

    def observe_write():
        try:
            real_write()
        except OSError as error:
            errors.append(type(error).__name__)
            raise

    monkeypatch.setattr(launch, "_record_main_commit", observe_write)
    _input(monkeypatch, {})
    hooks.session_start()
    assert len(errors) == 1
    output = _signal(capfd.readouterr(), "SessionStart", "launch", errors[0])
    assert "systemMessage" in output
    assert blocked.read_text() == "private synthetic cache content"
    assert not project.exists()


def test_delivery_sql_failure_is_not_mistaken_for_a_bad_row(project, monkeypatch, capfd):
    from tests.test_hot_index import FILE_A, _fixture_store

    (project.parent / FILE_A).parent.mkdir(parents=True, exist_ok=True)
    (project.parent / FILE_A).write_text("x = 1\n")
    _fixture_store(project).close()
    with closing(sqlite3.connect(project / "index.db")) as conn, conn:
        conn.execute("DROP TABLE decisions")
    before = {p.relative_to(project): p.read_bytes() for p in project.rglob("*.json")}
    monkeypatch.setenv("SIDEGRAPH_TELEMETRY", "off")
    _input(
        monkeypatch,
        {
            "session_id": "s",
            "tool_name": "Read",
            "tool_input": {"file_path": str(project.parent / FILE_A)},
        },
    )
    hooks.pre_tool_use()
    assert _signal(capfd.readouterr(), "PreToolUse", "delivery", "OperationalError") == {}
    assert {p.relative_to(project): p.read_bytes() for p in project.rglob("*.json")} == before
