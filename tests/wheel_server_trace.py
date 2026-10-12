"""Opt-in test bootstrap for fixed first-write stages in an installed stdio server.

Run with the selected wheel interpreter and -I. This is diagnostic infrastructure;
ordinary wheel validation still launches the unmodified sidegraph.server module.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from collections.abc import Callable
from functools import wraps
from pathlib import Path
from types import ModuleType

_PHASES = frozenset(
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
_started = time.monotonic()
_trace_lock = threading.Lock()
_active: dict[str, int] = {}


def _mark(phase: str, *, entering: bool) -> None:
    """Flush one fixed record; an inner return never masks outstanding outer work."""
    with _trace_lock:
        count = _active.get(phase, 0) + (1 if entering else -1)
        _active[phase] = count
        data = {
            "phase": phase,
            "elapsed_ms": round((time.monotonic() - _started) * 1000),
            "finished": count == 0,
        }
        try:
            sys.stderr.write("SIDEGRAPH_PROBE_DIAGNOSTIC=" + json.dumps(data) + "\n")
            sys.stderr.flush()
        except (OSError, ValueError):
            # A broken diagnostic sink must not alter the traced operation's outcome.
            pass


def trace_call[**P, R](phase: str, operation: Callable[P, R]) -> Callable[P, R]:
    """Preserve the call and its exception while reporting only its boundary state."""
    if phase not in _PHASES:
        raise ValueError("Unknown server diagnostic phase.")

    @wraps(operation)
    def traced(*args: P.args, **kwargs: P.kwargs) -> R:
        _mark(phase, entering=True)
        try:
            return operation(*args, **kwargs)
        finally:
            _mark(phase, entering=False)

    return traced


def install_diagnostics(server: ModuleType) -> None:
    """Wrap point-of-use globals/class methods before this same server starts."""
    from sidegraph import capture, verify

    targets = (
        (server.Store, "__init__", "server-store-open"),
        (server.Store, "add_decision", "server-store-add"),
        (server, "_get_store", "server-get-store"),
        (server, "_refresh_store", "server-refresh"),
        (server, "_load_reader", "server-reader"),
        (server, "_add_decision_impl", "server-add-body"),
        (server, "_capture_commit", "server-capture-commit"),
        (server, "_with_commit_hint", "server-response"),
        (capture, "_run_git", "server-capture-git"),
        (verify, "_run_git", "server-verify-git"),
    )
    for target, name, phase in targets:
        setattr(target, name, trace_call(phase, getattr(target, name)))


def _import_server() -> ModuleType:
    import sidegraph
    from sidegraph import server

    assert sys.flags.isolated and sys.prefix != sys.base_prefix, "Isolated wheel env required."
    for module in (sidegraph, server):
        assert module.__file__ is not None, "Installed file module required."
        origin = Path(module.__file__).resolve()
        assert origin.is_relative_to(Path(sys.prefix).resolve()), "Installed origin required."
        assert "site-packages" in origin.parts, "Installed site-packages required."
    return server


def main() -> None:
    """Run the genuine registered server, with diagnostics confined to stderr."""
    server = trace_call("server-bootstrap", _import_server)()
    trace_call("server-trace-installed", install_diagnostics)(server)
    server.mcp.run()


if __name__ == "__main__":
    main()
