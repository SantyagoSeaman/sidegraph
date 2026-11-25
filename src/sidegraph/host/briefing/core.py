"""The experimental SessionStart briefing entry point and composer.

See design/superpowers/specs/2026-11-20-sessionstart-briefing-design.md (D2–D4, D7).
"""

import json
import os
import sqlite3
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

from ...config import _present_by_lstat, resolve_store_location
from ...hot_index import HotIndex
from ...store_layout import HOOK_BUSY_SECONDS, MEMORY_GUARD_LINE, VOLATILE_STALE_KEY
from .. import hook_log
from ..diagnostics import HookDiagnostics
from ..hooks import (
    _SESSION_START_DEDUPE_SECONDS,
    HOOK_CONTEXT_MAX_CHARS,
    _host_family,
    _read_payload,
    _session_identity,
    _session_line,
    _touch_root,
    refuse_arguments,
)
from ..project_anchor import anchor_to_project, take_repo_root_flag
from . import context
from .blocks import BLOCKS, Block
from .context import BriefingContext
from .git_files import changed_files
from .lines import DRIFT_TAG, size, units

BRIEFING_MAX_UNITS = HOOK_CONTEXT_MAX_CHARS
DRIFT_LEGEND = (
    "[drifted]: the code changed since this record was written; check it against the code."
)
MARK_KEY = "briefing_session"


def compose(
    ctx: BriefingContext,
    enabled: frozenset[str],
    *,
    blocks: Sequence[Block],
    max_units: int = BRIEFING_MAX_UNITS,
    report: Callable[[str, Exception], None] | None = None,
) -> str:
    """Give enabled blocks room in registry order and keep their results whole."""
    room = max_units
    if enabled - {"protocol"}:
        room -= units(MEMORY_GUARD_LINE) + 2
    if ctx.drifted:
        room -= units(DRIFT_LEGEND) + 2
    parts: list[str] = []
    guard_placed = False
    for block in blocks:
        if block.name not in enabled:
            continue
        separator = 2 if parts else 0
        budget = min(block.budget, room - separator)
        if budget <= 0:
            continue
        try:
            lines = block.render(ctx, budget)
            if not lines:
                continue
            used = size(lines)
            if used > budget:
                raise ValueError(f"{block.name} returned {used} units for a budget of {budget}")
        except Exception as error:
            if report is not None:
                report(block.name, error)
            continue
        if block.name != "protocol" and not guard_placed:
            parts.append(MEMORY_GUARD_LINE)
            guard_placed = True
        parts.append("\n".join(lines))
        room -= used + separator
    if ctx.drifted and any(DRIFT_TAG in part for part in parts):
        parts.append(DRIFT_LEGEND)
    return "\n\n".join(parts)


def enabled_blocks() -> frozenset[str]:
    """Read the experimental opt-ins."""
    return frozenset(block.name for block in BLOCKS if os.environ.get(block.flag) == "on")


def _decide_mark(
    current: str | None, session_id: str, source: object, now: datetime
) -> tuple[str | None, bool, datetime | None]:
    at: datetime | None
    start: datetime | None
    try:
        mark = json.loads(current or "null")
        if not isinstance(mark, dict):
            raise ValueError("The briefing mark needs a JSON object")
        at = datetime.fromisoformat(mark["at"])
        start = datetime.fromisoformat(mark["start"])
        if at.utcoffset() is None or start.utcoffset() is None:
            raise ValueError("The briefing mark needs timestamps with offsets")
    except (KeyError, TypeError, ValueError):
        mark = {}
        at = start = None

    prev_start: datetime | None
    if mark.get("id") == session_id and at is not None:
        try:
            prev_start = datetime.fromisoformat(mark["prev_start"])
            if prev_start.utcoffset() is None:
                prev_start = None
        except (KeyError, TypeError, ValueError):
            prev_start = None
        if source not in ("compact", "clear") and (
            abs((now - at).total_seconds()) < _SESSION_START_DEDUPE_SECONDS
        ):
            return None, True, prev_start
    else:
        prev_start = start
        start = now

    assert start is not None
    return (
        json.dumps(
            {
                "id": session_id,
                "at": now.isoformat(),
                "start": start.isoformat(),
                "prev_start": prev_start.isoformat() if prev_start is not None else None,
            }
        ),
        False,
        prev_start,
    )


def main() -> None:
    """Print the SessionStart briefing as one JSON line."""
    rooted = take_repo_root_flag(sys.argv)
    if sys.argv[1:]:
        refuse_arguments("sidegraph-session-briefing", "Prints the SessionStart briefing.")
    enabled = enabled_blocks()
    host = _host_family()
    if not enabled or host not in {"claude", "codex"}:
        print("{}")
        return

    run = hook_log.Run("SessionBriefing")
    diagnostics = HookDiagnostics("SessionBriefing")
    index = None
    text = ""
    stage = "payload"
    try:
        if rooted:
            anchor_to_project(honor_claude_project_dir=host == "claude")
        payload = _read_payload()
        session_id, _ = _session_identity(payload)
        if session_id is None:
            return

        stage = "store"
        location = resolve_store_location(
            root=os.environ.get("CLAUDE_PROJECT_DIR") if host == "claude" else None,
            warn_on_create=False,
            search_ancestors=True,
        )
        if not _present_by_lstat(location.path):
            return
        stage = "index"
        open_errors: list[Exception] = []

        def open_error(error: Exception) -> None:
            open_errors.append(error)
            diagnostics.report("index", error)

        index = HotIndex.open(
            location.path, on_error=open_error, busy_timeout_seconds=HOOK_BUSY_SECONDS
        )
        if index is None:
            for error in open_errors:
                run.note(error)
        if index is not None or run.lock_seen:
            run.store_dir = location.path
        if index is None:
            return

        def on_error(error: Exception) -> None:
            run.note(error)
            diagnostics.report("index", error)

        needs = frozenset(part for block in BLOCKS if block.name in enabled for part in block.needs)
        now = datetime.now(UTC)
        with index.snapshot():
            if index.get_meta(VOLATILE_STALE_KEY) == "1":
                run.cause = hook_log.COLD
                return
            if not context.has_accepted_anchor(index, on_error):
                run.cause = hook_log.NO_RECORD
                return
            parts = context.read_parts(index, needs, now, on_error)
        if index.lock_lost:
            return

        stage = "mark"
        duplicate = False
        prev_start: datetime | None = None

        def decide(current: str | None) -> str | None:
            nonlocal duplicate, prev_start
            new, duplicate, prev_start = _decide_mark(
                current, session_id, payload.get("source"), now
            )
            return new

        try:
            index.update_meta(MARK_KEY, decide)
        except sqlite3.OperationalError as error:
            diagnostics.report("mark", error)
            duplicate = False
            prev_start = None
        if duplicate:
            run.cause = hook_log.DUPLICATE
            return

        stage = "git"
        files: tuple[str, ...] = ()
        if "git" in needs:
            git_root = (location.base or os.getcwd()) if host == "codex" else _touch_root(location)
            files = changed_files(Path(git_root), Path(location.path))
        stage = "briefing"
        ctx = context.build(parts, now, prev_start, files)
        text = compose(ctx, enabled, blocks=BLOCKS, report=diagnostics.report)
        if not text:
            run.cause = hook_log.NO_RECORD
    except Exception as error:
        run.note(error)
        diagnostics.report(stage, error)
    finally:
        delivered = False
        try:
            print(_session_line(None, text or None), flush=True)
            delivered = bool(text)
        except OSError as error:
            run.note(error)
            diagnostics.report("output", error)
        finally:
            if index is not None:
                try:
                    index.close()
                except Exception as error:
                    diagnostics.report("cleanup", error)
            run.finish(delivered)
