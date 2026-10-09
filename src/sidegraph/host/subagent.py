"""SubagentStart hook entry point: tell a subagent that decision memory exists.

A subagent starts with no SessionStart context, and Explore and Plan agents load no
``CLAUDE.md`` either, so nothing standing tells one that memory exists or how to ask it. This
hook hands each a short brief: the call to make (``STANDING_SEARCH_INSTRUCTION``, the line
SessionStart gives the main agent, so the two never drift) and one sentence on what memory
holds. It carries no records: which ones matter depends on the files the subagent works on,
and that is what ``get_task_context`` answers.

It runs once per subagent spawn, so it stays as light as the PreToolUse hook: it reads the
index through ``HotIndex`` and never constructs a ``Store``. Where the index cannot be used, or
holds nothing a ``get_task_context`` call could return, it prints ``{}`` and writes nothing.
Claude Code and Codex use the same event name and the same ``hookSpecificOutput`` shape.

see design/superpowers/specs/2026-10-03-subagent-start-brief-design.md (D1, D2, D5)
"""

from __future__ import annotations

import json
import os
import sys

from ..hot_index import HotIndex
from .hooks import STANDING_SEARCH_INSTRUCTION, _read_payload, refuse_arguments

_EVENT = "SubagentStart"


def _noun(count: int, word: str) -> str:
    return word if count == 1 else f"{word}s"


def render_brief(records: int, files: int, mistakes: int) -> str:
    """The brief: the standing call, then what memory holds, in under 700 characters.

    Host-neutral by construction: Codex defers MCP tools too, so the text names no host's
    tool-loading mechanism, only the instruction's own "if the tool is listed only by name,
    load it first".
    see design/superpowers/specs/2026-10-03-subagent-start-brief-design.md (D2)
    """
    return (
        f"{STANDING_SEARCH_INSTRUCTION} This project keeps a decision memory (Sidegraph): "
        f"{records} {_noun(records, 'record')} anchored to code in {files} "
        f"{_noun(files, 'file')}, {mistakes} of them recorded mistakes."
    )


def _answer() -> dict:
    """The hook's output: the brief, or ``{}`` where there is nothing to ask about."""
    payload = _read_payload()
    if not isinstance(payload, dict):
        return {}
    # A payload that names another event means a mis-wired entry; empty stdin is ``{}`` and
    # names none, so it is not refused.
    if payload.get("hook_event_name", _EVENT) != _EVENT:
        return {}
    if os.environ.get("SIDEGRAPH_SUBAGENT_BRIEF") == "off":
        return {}

    from ..config import resolve_store_location

    # warn_on_create=False: a spawn never creates a store, so the notice would announce
    # something that is not going to happen.
    location = resolve_store_location(
        root=os.environ.get("CLAUDE_PROJECT_DIR"), warn_on_create=False, search_ancestors=True
    )
    index = HotIndex.open(location.path)
    if index is None:
        return {}
    try:
        records, files, mistakes = index.memory_counts()
    finally:
        index.close()
    if not records:
        return {}
    return {
        "hookSpecificOutput": {
            "hookEventName": _EVENT,
            "additionalContext": render_brief(records, files, mistakes),
        }
    }


def subagent_start() -> None:
    """SubagentStart hook: print the memory brief for a starting subagent, or ``{}``.

    Reads the stdin JSON payload (Claude Code's carries the parent's ``session_id``, an
    ``agent_id`` and an ``agent_type``; Codex's adds ``turn_id`` and ``model``; the brief uses
    none of them; empty stdin counts as ``{}``, and a payload whose ``hook_event_name`` is
    another event prints ``{}``), resolves the store like every other hook, and counts what
    ``get_task_context`` could return through ``HotIndex.memory_counts``. Disable with
    ``SIDEGRAPH_SUBAGENT_BRIEF=off``. Never crashes and never blocks the spawn: any failure, an
    index it cannot use, or a store with nothing anchored to code prints ``{}``.
    see design/superpowers/specs/2026-10-03-subagent-start-brief-design.md (D1, D5)
    """
    if len(sys.argv) > 1:
        refuse_arguments(
            "sidegraph-subagent-start", "Prints the memory brief for a starting subagent."
        )
    try:
        answer = _answer()
    except Exception:
        answer = {}
    print(json.dumps(answer))
