"""Probe an exact, non-editable wheel through an explicit isolated interpreter.

Usage: python tests/wheel_smoke.py --wheel dist/package.whl --python /venv/bin/python
Only the child imports Sidegraph. The parent bounds the child and its stdio server tree.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import os
import platform
import signal
import subprocess
import sys
import tempfile
import time
import zipfile
from contextlib import suppress
from pathlib import Path

_PHASES = frozenset(
    {
        "package-import",
        "provenance",
        "console",
        "server-import",
        "stdio-start",
        "initialize",
        "list-tools",
        "add-decision",
        "retrieve",
        "text-contract",
        "error-contract",
        "ping",
        "stdio-close",
        "store-reopen",
        "attribution",
    }
)
_ERRORS = frozenset({"TimeoutError", "McpError", "AssertionError", "OSError", "other"})
_SERVER_PHASES = frozenset(
    {
        "server-bootstrap",
        "server-trace-installed",
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
    }
)
_DIAGNOSTIC_PREFIX = "SIDEGRAPH_PROBE_DIAGNOSTIC="


class ProbeProgress:
    """Emit fixed phases and numeric timings, never exception messages or server text."""

    def __init__(self) -> None:
        self.phase = "package-import"
        self.started = time.monotonic()
        self.returncode: int | None = None

    def emit(self, *, error: str | None = None, returncode: int | None = None) -> None:
        data = {"phase": self.phase, "elapsed_ms": round((time.monotonic() - self.started) * 1000)}
        if error is not None:
            data["error"] = error
        if returncode is not None:
            self.returncode = returncode
        if self.returncode is not None:
            data["returncode"] = self.returncode
        print(_DIAGNOSTIC_PREFIX + json.dumps(data), file=sys.stderr, flush=True)

    def start(self, phase: str) -> None:
        if phase not in _PHASES:
            raise ValueError("Unknown probe phase.")
        self.emit()
        self.phase = phase
        self.started = time.monotonic()
        self.returncode = None
        self.emit()


_progress = ProbeProgress()


def _report_failure(stderr: bytes) -> None:
    """Extract only validated diagnostic fields from otherwise discarded child stderr."""
    phases = {}
    for line in stderr.decode("utf-8", errors="replace").splitlines():
        if not line.startswith(_DIAGNOSTIC_PREFIX) or len(line) > 1024:
            continue
        try:
            data = json.loads(line[len(_DIAGNOSTIC_PREFIX) :])
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        phase = data.get("phase")
        if not isinstance(phase, str) or phase not in _PHASES | _SERVER_PHASES:
            continue
        elapsed = data.get("elapsed_ms")
        if type(elapsed) is not int or not 0 <= elapsed <= 120_000:
            continue
        clean = {"phase": data["phase"], "elapsed_ms": elapsed}
        error = data.get("error")
        if error is not None:
            clean["error"] = error if isinstance(error, str) and error in _ERRORS else "other"
        code = data.get("returncode")
        if type(code) is int and -(2**31) <= code < 2**32:
            clean["returncode"] = code
        if phase in _SERVER_PHASES and type(data.get("finished")) is bool:
            clean["finished"] = data["finished"]
        phases[phase] = clean
    for diagnostic in phases.values():
        print("Installed-wheel probe diagnostic: " + json.dumps(diagnostic), file=sys.stderr)


def _error_kind(exc: BaseException) -> str:
    if isinstance(exc, BaseExceptionGroup):
        kinds = [_error_kind(item) for item in exc.exceptions]
        return next((kind for kind in kinds if kind != "other"), "other")
    if isinstance(exc, subprocess.TimeoutExpired):
        return "TimeoutError"
    for kind in (TimeoutError, AssertionError, OSError):
        if isinstance(exc, kind):
            return kind.__name__
    return "McpError" if type(exc).__name__ == "McpError" else "other"


def verify_origin(origin: Path, prefix: Path, direct_url: str | None) -> None:
    """Refuse a checkout import or editable install even when the CLI starts."""
    if not origin.resolve().is_relative_to(prefix.resolve()):
        raise ValueError("Package origin is outside the target environment.")
    if "site-packages" not in origin.parts:
        raise ValueError("Package origin is outside environment site-packages.")
    if direct_url and json.loads(direct_url).get("dir_info", {}).get("editable"):
        raise ValueError("An editable install cannot validate a wheel.")


def verify_wheel_contents(wheel: Path, site_packages: Path) -> None:
    """Compare package bytes: uv local-wheel provenance need not include a hash."""
    with zipfile.ZipFile(wheel) as archive:
        members = [
            name
            for name in archive.namelist()
            if name.startswith("sidegraph/") and not name.endswith("/")
        ]
        if not members:
            raise ValueError("Wheel artifact has no Sidegraph package.")
        for name in members:
            relative = Path(name)
            if ".." in relative.parts or relative.is_absolute():
                raise ValueError("Unsafe artifact member.")
            if (site_packages / relative).read_bytes() != archive.read(name):
                raise ValueError("Installed package differs from wheel artifact.")


def run_bounded(
    command: list[str],
    *,
    cwd: Path,
    timeout: float = 60,
    start=subprocess.Popen,
    report_diagnostics: bool = False,
) -> str:
    """Reap the probe and descendants on timeout without exposing child diagnostics."""
    options = (
        {"start_new_session": True}
        if os.name != "nt"
        else {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    )
    process = start(command, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **options)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    capture_output=True,
                    timeout=10,
                    check=False,
                )
            finally:
                if process.poll() is None:
                    process.kill()
        else:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
        _, stderr = process.communicate(timeout=10)
        _report_failure(stderr)
        raise TimeoutError("Installed-wheel probe timed out.") from None
    if process.returncode:
        _report_failure(stderr)
        raise RuntimeError(f"Installed-wheel probe failed with exit code {process.returncode}.")
    if report_diagnostics:
        _report_failure(stderr)
    return stdout.decode("utf-8")


async def probe_stdio(
    root: Path, *, timeout: float = 30, diagnose_first_write: bool = False
) -> str:
    """Initialize and round-trip benign data through actual SDK stdio pipes."""
    _progress.start("stdio-start")
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    environment = {k: v for k, v in os.environ.items() if not k.startswith("SIDEGRAPH_")}
    environment.update(
        SIDEGRAPH_DIR=str(root / ".sidegraph"), SIDEGRAPH_GRAPH=str(root / "absent-graph.json")
    )
    parameters = StdioServerParameters(
        command=sys.executable,
        args=(
            ["-I", str(Path(__file__).with_name("wheel_server_trace.py").resolve())]
            if diagnose_first_write
            else ["-I", "-m", "sidegraph.server"]
        ),
        cwd=str(root),
        env=environment,
    )
    async with (
        asyncio.timeout(timeout),
        stdio_client(parameters) as (read, write),
        ClientSession(read, write) as client,
    ):
        _progress.start("initialize")
        initialized = await client.initialize()
        assert initialized.serverInfo.version == importlib.metadata.version("sidegraph")
        _progress.start("list-tools")
        listed = await client.list_tools()
        assert len(listed.tools) == 24
        tools = {tool.name: tool for tool in listed.tools}
        assert tools["get_task_context"].outputSchema is None
        _progress.start("add-decision")
        added = await client.call_tool(
            "add_decision",
            {
                "title": "Wheel café",
                "kind": "adr",
                "context": "Native smoke",
                "choice": "Persist UTF-8 data.",
            },
        )
        assert not added.isError
        record_id = added.structuredContent["id"]
        _progress.start("retrieve")
        retrieved = await client.call_tool("retrieve_decisions", {})
        assert not retrieved.isError and retrieved.structuredContent is None
        assert record_id in retrieved.content[0].text
        _progress.start("text-contract")
        text = await client.call_tool("get_task_context", {})
        assert not text.isError and text.structuredContent is None
        _progress.start("error-contract")
        failed = await client.call_tool("add_anchors", {"record_id": "missing", "anchors": []})
        assert failed.isError and failed.structuredContent == {"error": "unknown record"}
        _progress.start("ping")
        await client.send_ping()
        _progress.start("stdio-close")
    return record_id


async def probe_attribution(root: Path, *, timeout: float = 30) -> set[str]:
    """Launch the installed `sidegraph-mcp` console script as `claude-code` and return the
    session ids its journal rows carry, so the host entry's middleware runs against the SDK
    this job installed. The script is `Scripts\\sidegraph-mcp.exe` on Windows."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from mcp.types import Implementation

    from sidegraph.store import Store

    scripts = Path(sys.executable).parent
    command = scripts / ("sidegraph-mcp.exe" if os.name == "nt" else "sidegraph-mcp")
    environment = {k: v for k, v in os.environ.items() if not k.startswith("SIDEGRAPH_")}
    environment.update(
        SIDEGRAPH_DIR=str(root / ".sidegraph"),
        SIDEGRAPH_GRAPH=str(root / "absent-graph.json"),
        SIDEGRAPH_TELEMETRY="on",
        CLAUDE_CODE_SESSION_ID="wheel-claude-session",
    )
    parameters = StdioServerParameters(
        command=str(command), args=[], cwd=str(root), env=environment
    )
    async with (
        asyncio.timeout(timeout),
        stdio_client(parameters) as (read, write),
        ClientSession(
            read, write, client_info=Implementation(name="claude-code", version="0")
        ) as client,
    ):
        await client.initialize()
        result = await client.call_tool("get_task_context", {"files": ["src/attribution.py"]})
        assert not result.isError
    with Store(root / ".sidegraph") as store:
        return {str(e["session_id"]) for e in store.retrieval_events()}


def probe_child(wheel: Path, *, diagnose_first_write: bool = False) -> dict:
    """Check install provenance, console script and reopened canonical truth."""
    _progress.emit()
    import sidegraph
    from sidegraph.store import Store

    _progress.start("provenance")
    distribution = importlib.metadata.distribution("sidegraph")
    direct_url = distribution.read_text("direct_url.json")
    verify_origin(Path(sidegraph.__file__), Path(sys.prefix), direct_url)
    assert sys.prefix != sys.base_prefix
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    installed = json.loads(direct_url or "{}")
    assert installed.get("url") == wheel.as_uri(), "Different artifact installed"
    recorded_hash = installed.get("archive_info", {}).get("hashes", {}).get("sha256")
    assert recorded_hash is None or recorded_hash == digest
    verify_wheel_contents(wheel, Path(sidegraph.__file__).resolve().parent.parent)
    root = Path.cwd()
    scripts = Path(sys.executable).parent
    cli = scripts / ("sidegraph-doctor.exe" if os.name == "nt" else "sidegraph-doctor")
    _progress.start("console")
    subprocess.run([str(cli), "--help"], cwd=root, check=True, capture_output=True, timeout=10)
    _progress.start("server-import")
    environment = {k: v for k, v in os.environ.items() if not k.startswith("SIDEGRAPH_")}
    environment.update(
        SIDEGRAPH_DIR=str(root / ".sidegraph"), SIDEGRAPH_GRAPH=str(root / "absent-graph.json")
    )
    imported = subprocess.run(
        [sys.executable, "-I", "-c", "import sidegraph.server"],
        cwd=root,
        env=environment,
        capture_output=True,
        timeout=10,
    )
    server_import_ms = round((time.monotonic() - _progress.started) * 1000)
    _progress.emit(returncode=imported.returncode)
    imported.check_returncode()
    stdio_started = time.monotonic()
    record_id = asyncio.run(probe_stdio(root, diagnose_first_write=diagnose_first_write))
    stdio_ms = round((time.monotonic() - stdio_started) * 1000)
    _progress.start("store-reopen")
    with Store(root / ".sidegraph") as store:
        assert store.get_decision(record_id).title == "Wheel café"
    _progress.start("attribution")
    attributed = asyncio.run(probe_attribution(root))
    assert attributed == {"wheel-claude-session"}, attributed
    return {
        "os": platform.system(),
        "python": platform.python_version(),
        "sidegraph": distribution.version,
        "fastmcp": importlib.metadata.version("fastmcp"),
        "mcp": importlib.metadata.version("mcp"),
        "wheel_sha256": digest,
        "installed_origin": str(Path(sidegraph.__file__).resolve()),
        "stdio": "passed",
        "server_import_ms": server_import_ms,
        "stdio_ms": stdio_ms,
    }


def main() -> int:
    """Keep failed output fixed and successful evidence machine-readable."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--diagnose-first-write",
        action="store_true",
        help="Trace a separate first-write run; never emits release evidence.",
    )
    args = parser.parse_args()
    try:
        wheel = args.wheel.resolve(strict=True)
        interpreter = args.python.absolute()
        if not interpreter.is_file():
            raise ValueError("Target interpreter is absent.")
        if args.child:
            result = probe_child(wheel, diagnose_first_write=args.diagnose_first_write)
            if args.diagnose_first_write:
                print("Diagnostic probe completed; ordinary validation remains required.")
            else:
                print(json.dumps(result))
        else:
            with tempfile.TemporaryDirectory(prefix="sidegraph wheel café ") as temporary:
                output = run_bounded(
                    [
                        str(interpreter),
                        "-I",
                        str(Path(__file__).resolve()),
                        "--child",
                        "--wheel",
                        str(wheel),
                        "--python",
                        str(interpreter),
                    ]
                    + (["--diagnose-first-write"] if args.diagnose_first_write else []),
                    cwd=Path(temporary),
                    report_diagnostics=args.diagnose_first_write,
                )
                print(output.strip())
    except Exception as exc:
        if args.child:
            _progress.emit(error=_error_kind(exc))
        print("Installed-wheel validation failed; no release evidence produced.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
