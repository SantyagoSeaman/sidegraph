"""Muse lifecycle hooks for Sidegraph: forward a hook payload to the public runtime.

Muse starts a hook as ``python3 muse/<entry>.py`` with a cleared environment and no shell, so
this module does what the Claude and Codex hook command strings do: launch the pinned runtime,
set the project variables, run the entry point in the project directory, and fail open. It is
plain Python 3.9 standard library (the stock macOS ``python3``), imported by the entry files by
plain name; it is never a declared hook source.

# see design/superpowers/specs/2026-10-28-muse-hooks-design.md (Adapter, Environment mapping)
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys

# The exact PyPI pin the adapter launches: the version bump writes it, with the manifests. The
# interpreter flag follows the package, as in every other host's launch.
SOURCE = "sidegraph==0.26.0rc1"

# Muse's ``timeoutMs`` per event, pinned to the manifest by a test. The child gets the rest of
# the budget minus the margin, so a slow runtime ends as ``{}`` and not as a Muse ``timed_out``.
TIMEOUTS_MS = {"SessionStart": 15000, "SubagentStart": 5000}
CHILD_MARGIN_MS = 1000

# Muse discards a hook's output over 16 KiB; stay a KiB under it.
OUTPUT_LIMIT = 15 * 1024


def _child_env(payload: dict) -> dict:
    """The adapter's own environment plus the five keys Muse cannot set through a command. A key
    whose source is absent, empty or invalid is deleted, so neither an inherited value nor an
    empty string reaches the child."""
    env = dict(os.environ)
    env["SIDEGRAPH_HOST"] = "muse"
    env["SIDEGRAPH_DIR"] = ".sidegraph"
    env["SIDEGRAPH_GRAPH"] = "graphify-out/graph.json"
    cwd = payload.get("cwd")
    if isinstance(cwd, str) and os.path.isdir(cwd):
        env["CLAUDE_PROJECT_DIR"] = cwd
    else:
        env.pop("CLAUDE_PROJECT_DIR", None)
    root = env.get("MUSE_PLUGIN_ROOT")
    if root:
        env["CLAUDE_PLUGIN_ROOT"] = root
    else:
        env.pop("CLAUDE_PLUGIN_ROOT", None)
    return env


def _emit(data: bytes) -> None:
    # A closed stdout (sys.stdout is None, or the write fails) has nobody to tell: exit 0 quietly.
    with contextlib.suppress(OSError, ValueError, AttributeError):
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()


def _guarded(event: str, out: bytes) -> bytes:
    """The child's stdout when it is a JSON object within the limit; a visible size message when
    it is over (Muse would discard it silently); ``{}`` when it is not usable."""
    try:
        parsed = json.loads(out.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return b"{}\n"
    if not isinstance(parsed, dict):
        return b"{}\n"
    if len(out) <= OUTPUT_LIMIT:
        return out
    message = (
        f"Sidegraph: the {event} output is {len(out)} bytes, over the 16 KiB Muse allows a hook; "
        "it was dropped"
    )
    return (json.dumps({"systemMessage": message}) + "\n").encode("utf-8")


def _run(event: str, entry_point: str, raw: bytes, payload: dict) -> bytes:
    env = _child_env(payload)
    cwd = payload.get("cwd")
    argv = ["uvx", "--from=" + SOURCE, "--python", "3.13", entry_point]
    timeout = (TIMEOUTS_MS[event] - CHILD_MARGIN_MS) / 1000.0
    child = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=cwd if isinstance(cwd, str) and os.path.isdir(cwd) else None,
        start_new_session=True,
    )
    try:
        # Both output streams are drained together: reading stdout alone would deadlock on a
        # child that fills the stderr pipe. The stderr is dropped.
        out, _ = child.communicate(raw, timeout=timeout)
    except subprocess.TimeoutExpired:
        # start_new_session makes the child's pid its process-group id: the kill reaches the
        # runtime uvx started, not just uvx.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(child.pid, signal.SIGKILL)
        # Bounded: a runtime helper that left the group may still hold the pipes open.
        try:
            child.communicate(timeout=1)
        except subprocess.TimeoutExpired:
            child.wait()
        return b"{}\n"
    return _guarded(event, out) if child.returncode == 0 else b"{}\n"


def main(event: str, entry_point: str) -> None:
    """Read the payload once and print the runtime's answer, or ``{}``. Never raises. Stdin that
    is empty or not JSON, and JSON that is not an object, answer ``{}`` without a spawn; an
    object without ``hook_event_name`` is the entry's own event."""
    try:
        raw = sys.stdin.buffer.read()
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            payload = None
        if not isinstance(payload, dict) or payload.get("hook_event_name", event) != event:
            answer = b"{}\n"
        else:
            answer = _run(event, entry_point, raw, payload)
    except Exception:
        answer = b"{}\n"
    _emit(answer)
