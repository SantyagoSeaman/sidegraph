"""The wheel probe rejects source installs and bounds its whole child process tree."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import zipfile

import pytest


def test_editable_install_is_rejected(tmp_path):
    from tests.wheel_smoke import verify_origin

    prefix = tmp_path / "environment"
    origin = prefix / "lib" / "site-packages" / "sidegraph" / "__init__.py"
    origin.parent.mkdir(parents=True)
    origin.touch()
    with pytest.raises(ValueError, match="editable"):
        verify_origin(origin, prefix, json.dumps({"dir_info": {"editable": True}}))


def test_checkout_origin_is_rejected(tmp_path):
    from tests.wheel_smoke import verify_origin

    with pytest.raises(ValueError, match="environment"):
        verify_origin(tmp_path / "checkout" / "sidegraph.py", tmp_path / "venv", None)


def test_installed_origin_is_accepted(tmp_path):
    from tests.wheel_smoke import verify_origin

    prefix = tmp_path / "venv"
    verify_origin(prefix / "Lib" / "site-packages" / "sidegraph.py", prefix, None)


def test_timeout_terminates_and_reaps_child(tmp_path):
    from tests.wheel_smoke import run_bounded

    marker = tmp_path / "pid"
    program = (
        "import os,time; from pathlib import Path; "
        f"Path({str(marker)!r}).write_text(str(os.getpid())); time.sleep(60)"
    )
    processes = []

    def start(*args, **kwargs):
        process = subprocess.Popen(*args, **kwargs)
        processes.append(process)
        return process

    with pytest.raises(TimeoutError, match="timed out"):
        run_bounded([sys.executable, "-c", program], cwd=tmp_path, timeout=1, start=start)
    assert processes[0].poll() is not None
    assert marker.is_file()


def test_child_failure_does_not_expose_exception_output(tmp_path):
    from tests.wheel_smoke import run_bounded

    with pytest.raises(RuntimeError, match="exit code 1") as caught:
        run_bounded(
            [sys.executable, "-c", "raise RuntimeError('private-probe-canary')"],
            cwd=tmp_path,
        )
    assert "private-probe-canary" not in str(caught.value)


@pytest.mark.parametrize("hang", [False, True])
def test_failed_probe_reports_only_allowlisted_diagnostics(tmp_path, capsys, hang):
    from tests.wheel_smoke import run_bounded

    marker = 'SIDEGRAPH_PROBE_DIAGNOSTIC={"phase":"initialize","elapsed_ms":12}'
    unknown = 'SIDEGRAPH_PROBE_DIAGNOSTIC={"phase":"private-probe-canary","elapsed_ms":12}'
    program = (
        "import sys,time; "
        f"sys.stderr.write({(marker + chr(10) + unknown + chr(10) + 'private-probe-canary')!r}); "
        "sys.stderr.flush(); " + ("time.sleep(60)" if hang else "sys.exit(1)")
    )
    error = TimeoutError if hang else RuntimeError
    with pytest.raises(error):
        run_bounded([sys.executable, "-c", program], cwd=tmp_path, timeout=2)
    output = capsys.readouterr()
    assert '"phase": "initialize"' in output.err
    assert '"elapsed_ms": 12' in output.err
    assert "private-probe-canary" not in output.err + output.out


@pytest.mark.parametrize(
    "diagnostic",
    [
        {"phase": [], "elapsed_ms": 1},
        {"phase": "initialize", "elapsed_ms": True},
        {"phase": "initialize", "elapsed_ms": -1},
        {"phase": "initialize", "elapsed_ms": 120_001},
        {"phase": "server-store-open", "elapsed_ms": True, "finished": False},
        {"phase": "server-store-open", "elapsed_ms": -1, "finished": False},
        {"phase": "server-store-open", "elapsed_ms": 120_001, "finished": False},
        {"phase": "private-canary", "elapsed_ms": 1, "finished": True},
    ],
)
def test_invalid_diagnostics_are_discarded(capsys, diagnostic):
    from tests.wheel_smoke import _report_failure

    _report_failure(("SIDEGRAPH_PROBE_DIAGNOSTIC=" + json.dumps(diagnostic)).encode())
    assert capsys.readouterr().err == ""


def test_subprocess_timeout_has_the_fixed_timeout_class():
    from tests.wheel_smoke import _error_kind

    assert _error_kind(subprocess.TimeoutExpired("private-probe-canary", 10)) == "TimeoutError"


@pytest.mark.parametrize("code", [7, True, 2**32])
def test_diagnostic_error_class_and_returncode_are_sanitized(capsys, code):
    from tests.wheel_smoke import _report_failure

    data = {
        "phase": "server-import",
        "elapsed_ms": 12,
        "error": "private-probe-canary",
        "returncode": code,
        "message": "private-probe-canary",
    }
    _report_failure(("SIDEGRAPH_PROBE_DIAGNOSTIC=" + json.dumps(data)).encode())
    output = capsys.readouterr().err
    assert '"error": "other"' in output
    if type(code) is int and code == 7:
        assert '"returncode": 7' in output
    else:
        assert '"returncode"' not in output
    assert "private-probe-canary" not in output


def test_installed_package_bytes_match_the_exact_wheel(tmp_path):
    from tests.wheel_smoke import verify_wheel_contents

    wheel = tmp_path / "probe.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("sidegraph/__init__.py", "original\n")
    installed = tmp_path / "site-packages"
    (installed / "sidegraph").mkdir(parents=True)
    (installed / "sidegraph" / "__init__.py").write_bytes(b"changed\n")
    with pytest.raises(ValueError, match="artifact"):
        verify_wheel_contents(wheel, installed)
    (installed / "sidegraph" / "__init__.py").write_bytes(b"original\n")
    verify_wheel_contents(wheel, installed)


@pytest.mark.skipif(os.name == "nt", reason="POSIX SDK detached session; Windows uses a Job Object")
def test_stdio_timeout_reaps_the_detached_sdk_server(tmp_path, monkeypatch):
    import anyio
    from mcp import StdioServerParameters
    from mcp.client import stdio

    from tests.wheel_smoke import probe_stdio

    processes = []
    original_open = anyio.open_process
    original_stdio = stdio.stdio_client

    async def open_process(*args, **kwargs):
        process = await original_open(*args, **kwargs)
        processes.append(process)
        return process

    def hang_server(parameters):
        return original_stdio(
            StdioServerParameters(
                command=sys.executable, args=["-c", "import time; time.sleep(60)"]
            )
        )

    monkeypatch.setattr(anyio, "open_process", open_process)
    monkeypatch.setattr(stdio, "stdio_client", hang_server)
    monkeypatch.setattr(stdio, "PROCESS_TERMINATION_TIMEOUT", 0.1)
    with pytest.raises(TimeoutError):
        asyncio.run(probe_stdio(tmp_path, timeout=0.2))
    assert len(processes) == 1
    assert processes[0].returncode is not None


def test_package_import_marker_precedes_the_import(monkeypatch, capsys, tmp_path):
    import builtins

    from tests import wheel_smoke

    original_import = builtins.__import__
    before_import = []

    def stop_import(name, *args, **kwargs):
        if name == "sidegraph":
            before_import.append(capsys.readouterr().err)
            raise RuntimeError("Injected import stop")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(wheel_smoke, "_progress", wheel_smoke.ProbeProgress())
    monkeypatch.setattr(builtins, "__import__", stop_import)
    with pytest.raises(RuntimeError, match="Injected import stop"):
        wheel_smoke.probe_child(tmp_path / "unused.whl")
    assert '"phase": "package-import"' in before_import[0]


def test_server_diagnostic_survives_without_private_fields(capsys):
    from tests.wheel_smoke import _report_failure

    _report_failure(
        b'SIDEGRAPH_PROBE_DIAGNOSTIC={"phase":"server-store-open",'
        b'"elapsed_ms":0,"finished":false,"message":"private-canary","path":"private-canary"}'
    )
    output = capsys.readouterr().err
    assert '"phase": "server-store-open"' in output
    assert '"finished": false' in output
    assert "private-canary" not in output


@pytest.mark.parametrize("finished", [1, None, "true", []])
def test_server_diagnostic_finished_requires_exact_bool(capsys, finished):
    from tests.wheel_smoke import _report_failure

    data = {"phase": "server-store-open", "elapsed_ms": 1, "finished": finished}
    _report_failure(("SIDEGRAPH_PROBE_DIAGNOSTIC=" + json.dumps(data)).encode())
    output = capsys.readouterr().err
    assert '"phase": "server-store-open"' in output
    assert '"finished"' not in output


def test_trace_preserves_exception_identity_and_flushes_before_work(capsys):
    from tests.wheel_server_trace import trace_call

    failure = RuntimeError("private-canary")
    entered = []

    def fail(value, *, named):
        entered.append(capsys.readouterr().err)
        assert value == 17 and named == "argument"
        raise failure

    with pytest.raises(RuntimeError) as caught:
        trace_call("server-store-open", fail)(17, named="argument")
    assert caught.value is failure
    assert '"finished": false' in entered[0]
    output = capsys.readouterr().err
    assert '"finished": true' in output
    assert "private-canary" not in output + entered[0]


def test_inner_trace_completion_does_not_mask_active_outer_call(capsys):
    from tests.wheel_server_trace import trace_call

    inner = trace_call("server-store-open", lambda: 17)
    observations = []

    def outer():
        assert inner() == 17
        observations.append(capsys.readouterr().err)
        return 29

    assert trace_call("server-store-open", outer)() == 29
    records = [json.loads(line.split("=", 1)[1]) for line in observations[0].splitlines()]
    assert len(records) == 3
    assert all(record["finished"] is False for record in records)
    finished = json.loads(capsys.readouterr().err.strip().split("=", 1)[1])
    assert finished["finished"] is True
    assert [r["elapsed_ms"] for r in records] == sorted(r["elapsed_ms"] for r in records)
    assert finished["elapsed_ms"] >= records[-1]["elapsed_ms"]


def test_forced_kill_retains_the_real_helper_enter_marker(tmp_path, capsys):
    from pathlib import Path

    from tests.wheel_smoke import run_bounded

    helper = Path(__file__).with_name("wheel_server_trace.py")
    program = (
        "import runpy,time; "
        f"helpers=runpy.run_path({str(helper)!r}); "
        "helpers['trace_call']('server-store-open', lambda: time.sleep(60))()"
    )
    with pytest.raises(TimeoutError):
        run_bounded([sys.executable, "-I", "-c", program], cwd=tmp_path, timeout=2)
    output = capsys.readouterr().err
    assert '"phase": "server-store-open"' in output
    assert '"finished": false' in output


def test_installed_trace_boundaries_fire_on_a_real_decision(tmp_path, monkeypatch, capsys):
    from fastmcp import Client

    from sidegraph import capture, server, verify
    from sidegraph.gitenv import git_env
    from sidegraph.store import Store
    from tests.wheel_server_trace import install_diagnostics

    for args in (
        ["init", "--quiet"],
        [
            "-c",
            "user.name=Probe",
            "-c",
            "user.email=probe@example.invalid",
            "commit",
            "--quiet",
            "--allow-empty",
            "-m",
            "probe",
        ],
    ):
        subprocess.run(
            ["git", *args], cwd=tmp_path, env=git_env(), check=True, capture_output=True, timeout=10
        )
    monkeypatch.setenv("SIDEGRAPH_DIR", str(tmp_path / ".sidegraph"))
    monkeypatch.setenv("SIDEGRAPH_GRAPH", str(tmp_path / "absent.json"))
    monkeypatch.setattr(server, "_store", None)
    targets = (
        (Store, "__init__"),
        (Store, "add_decision"),
        (server, "_get_store"),
        (server, "_refresh_store"),
        (server, "_load_reader"),
        (server, "_add_decision_impl"),
        (server, "_capture_commit"),
        (server, "_with_commit_hint"),
        (capture, "_run_git"),
        (verify, "_run_git"),
    )
    # Restore genuine module/class identities after the helper installs its wrappers.
    for target, name in targets:
        monkeypatch.setattr(target, name, getattr(target, name))
    install_diagnostics(server)

    async def call():
        async with Client(server.mcp) as client:
            result = await client.call_tool(
                "add_decision",
                {
                    "title": "Wheel café",
                    "kind": "adr",
                    "context": "Native smoke",
                    "choice": "Persist UTF-8.",
                },
            )
            return result.data

    try:
        result = asyncio.run(call())
        assert server._store.get_decision(result["id"]).title == "Wheel café"
        assert "commit_hint" in result
    finally:
        if server._store is not None:
            server._store.close()
    records = [
        json.loads(line.split("=", 1)[1])
        for line in capsys.readouterr().err.splitlines()
        if line.startswith("SIDEGRAPH_PROBE_DIAGNOSTIC=")
    ]
    for phase in (
        "server-store-open",
        "server-get-store",
        "server-refresh",
        "server-reader",
        "server-add-body",
        "server-capture-commit",
        "server-store-add",
        "server-response",
        "server-capture-git",
        "server-verify-git",
    ):
        observed = [r["finished"] for r in records if r["phase"] == phase]
        assert False in observed and observed[-1] is True, phase


def test_diagnostic_success_never_emits_release_json(tmp_path, monkeypatch, capsys):
    from tests import wheel_smoke

    wheel = tmp_path / "fixture.whl"
    wheel.touch()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "wheel_smoke",
            "--wheel",
            str(wheel),
            "--python",
            sys.executable,
            "--child",
            "--diagnose-first-write",
        ],
    )
    monkeypatch.setattr(wheel_smoke, "probe_child", lambda *a, **k: {"stdio": "passed"})
    assert wheel_smoke.main() == 0
    output = capsys.readouterr().out
    assert output.strip() == "Diagnostic probe completed; ordinary validation remains required."
    with pytest.raises(ValueError):
        json.loads(output)


@pytest.mark.skipif(os.name == "nt", reason="Native stdio has a separate exact-wheel CI gate")
def test_normal_stdio_keeps_the_unmodified_module_entrypoint(tmp_path, monkeypatch):
    import mcp

    from sidegraph.store import Store
    from tests.wheel_smoke import probe_stdio

    original = mcp.StdioServerParameters
    commands = []

    def parameters(**kwargs):
        commands.append(kwargs["args"])
        return original(**kwargs)

    monkeypatch.setattr(mcp, "StdioServerParameters", parameters)
    record_id = asyncio.run(probe_stdio(tmp_path))
    assert commands == [["-I", "-m", "sidegraph.server"]]
    with Store(tmp_path / ".sidegraph") as store:
        assert store.get_decision(record_id).title == "Wheel café"


def test_successful_diagnostic_stderr_is_still_sanitized(tmp_path, capsys):
    from tests.wheel_smoke import run_bounded

    marker = (
        'SIDEGRAPH_PROBE_DIAGNOSTIC={"phase":"server-store-open",'
        '"elapsed_ms":12,"finished":true,"message":"private-canary"}\n'
    )
    program = f"import sys; sys.stderr.write({marker!r}); print('diagnostic complete')"
    output = run_bounded([sys.executable, "-c", program], cwd=tmp_path, report_diagnostics=True)
    assert output.strip() == "diagnostic complete"
    stderr = capsys.readouterr().err
    assert '"finished": true' in stderr
    assert "private-canary" not in stderr


def test_every_emitted_server_stage_remains_observable_by_the_parent(capsys):
    from tests import wheel_server_trace
    from tests.wheel_smoke import _report_failure

    for phase in wheel_server_trace._PHASES:
        assert wheel_server_trace.trace_call(phase, lambda: 17)() == 17
    emitted = capsys.readouterr().err
    _report_failure(emitted.encode())
    retained = [json.loads(line.split(": ", 1)[1]) for line in capsys.readouterr().err.splitlines()]
    assert {r["phase"] for r in retained} == wheel_server_trace._PHASES
    assert all(r["finished"] is True for r in retained)


def test_diagnostic_parent_propagates_flag_and_uses_fresh_scratch(tmp_path, monkeypatch, capsys):
    from pathlib import Path

    from tests import wheel_smoke

    wheel = tmp_path / "fixture.whl"
    wheel.touch()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "wheel_smoke",
            "--wheel",
            str(wheel),
            "--python",
            sys.executable,
            "--diagnose-first-write",
        ],
    )
    roots = []

    def completed(command, *, cwd, report_diagnostics):
        assert report_diagnostics is True
        assert command[-1] == "--diagnose-first-write" and "--child" in command
        assert not (cwd / ".sidegraph").exists()
        (cwd / ".sidegraph").mkdir()
        roots.append(cwd)
        return "Diagnostic probe completed; ordinary validation remains required.\n"

    monkeypatch.setattr(wheel_smoke, "run_bounded", completed)
    assert wheel_smoke.main() == 0
    assert wheel_smoke.main() == 0
    assert roots[0] != roots[1]
    assert all(not Path(root).exists() for root in roots)
    with pytest.raises(ValueError):
        json.loads(capsys.readouterr().out)
