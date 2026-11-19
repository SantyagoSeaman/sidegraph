"""Hook output for a fresh session, with preparation separated from session writes."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from ..checkout_window import await_marker
from ..config import (
    StoreLocation,
    default_graph_path,
    missing_store_message,
    resolve_store_location,
)
from ..emulation_types import Preparation
from ..engine.reader import GraphifyReader
from ..retrieval import top_tier_sections
from ..server import prepare_reader
from ..store import Store, _has_existing_store
from ..store_layout import refuse_outside_store_link
from . import hooks, mcp_entry, subagent

type HookEvent = Literal[
    "session_start", "pre_tool_use", "agent_brief", "subagent_start", "stop", "mcp_instructions"
]

WIRED_ON: dict[str, frozenset[str]] = {
    "session_start": hooks._HOST_FAMILIES,
    "pre_tool_use": frozenset({"claude", "grok"}),
    "agent_brief": frozenset({"claude", "grok"}),
    "subagent_start": hooks._HOST_FAMILIES,
    "stop": frozenset({"claude", "codex", "grok"}),
    "mcp_instructions": frozenset({"grok"}),
}
_FIRING_CONDITIONS = {
    "session_start": ("The session starts, resumes or compacts.",),
    "pre_tool_use": ("A file read or edit reaches an anchored path.", "The file claim succeeds."),
    "agent_brief": (
        "The host starts an Agent or Task.",
        "The prompt names a file with records.",
    ),
    "subagent_start": ("A subagent starts in a repository with anchored decisions.",),
    "stop": ("The transcript gate passes.", "The fresh session has not been nudged."),
    "mcp_instructions": ("The Grok client initializes its MCP server.",),
}


@dataclass(frozen=True)
class HookEmulation:
    """The hook text, conditions, and preparation effects.
    See design/superpowers/specs/2026-11-17-portal-design.md (D10).
    """

    text: str
    emitted: bool
    not_emitted_reason: str | None
    firing_conditions: list[str]
    prepared: Preparation


def _session_start_text(
    location: StoreLocation,
    reader: GraphifyReader | None,
    borrowed_from: Path | None,
    host_family: str,
    sync: bool,
    prepared: Preparation,
) -> tuple[str, Preparation]:
    path = Path(location.path)
    try:
        refuse_outside_store_link(path)
        Store._refuse_interrupted_migration(path)
        if path.is_file():
            Store._refuse_legacy_store(path)
        if not _has_existing_store(path):
            if path.is_dir():
                legacy = path / "decisions.db"
                if legacy.is_file():
                    Store._refuse_legacy_store(legacy)
                await_marker(path)
            guidance = hooks._bounded_output(missing_store_message(path), None)
            return _session_answer_text(guidance), prepared
        store = Store(
            path,
            busy_timeout_seconds=hooks.HOOK_BUSY_SECONDS,
            stop_waiting_after_busy=True,
            existing_only=True,
        )
    except Exception as error:
        if hooks._is_transient_lock(error):
            return "", prepared
        diagnostic = hooks._render_unreadable_store(location, error, datetime.now(UTC))
        return _session_answer_text(diagnostic), prepared

    with store:
        if sync:
            reader, prepared = prepare_reader(store, reader, borrowed_from=borrowed_from, sync=True)
        sections = hooks._session_sections(store, reader, borrowed_from)
        if sections is None:
            sections = top_tier_sections(store, reader)
        drift = hooks._session_drift(store) if sync else None
        status: list[str] = []
        notices: list[str] = []
        try:
            result, _, _ = hooks._session_integrity(
                store,
                location,
                reader,
                default_graph_path(location.path) if sync else None,
                borrowed_from,
                drift,
                host_family,
            )
            status = [problem.line for problem in result.problems if problem.line]
            notices = [
                problem.notice for problem in hooks._ordered_notices(result) if problem.notice
            ]
        except Exception:
            pass  # As in SessionStart, an unavailable check must not cost the map.
        return _session_answer_text(
            hooks._render_session_start(sections, status, notices)
        ), prepared


def _session_answer_text(output: str) -> str:
    answer = json.loads(output)
    text = answer.get("hookSpecificOutput", {}).get("additionalContext")
    return text or answer.get("systemMessage", "")


def _hook_location(store_path: Path, host_family: str) -> tuple[StoreLocation, str]:
    """Keep the real hook's path spelling and anchor when it resolves to this store."""
    project = os.environ.get("CLAUDE_PROJECT_DIR") if host_family != "codex" else None
    location = resolve_store_location(root=project, warn_on_create=False, search_ancestors=True)
    if os.path.realpath(location.path) == os.path.realpath(store_path):
        root = location.base or project or os.getcwd()
        if host_family in ("claude", "muse"):
            # Both hosts provide a project directory, which anchors a relative store path.
            location = StoreLocation(os.path.join(root, location.path), location.base)
        return location, root
    # An explicit --db does not change the project a hook's file paths are relative to.
    return StoreLocation(str(store_path.absolute()), None), project or os.getcwd()


def _hot_hook_text(
    event: Literal["pre_tool_use", "agent_brief", "subagent_start"],
    params: dict,
    store_path: Path,
    root: str,
    host_family: str,
) -> str | None:
    hot = hooks._HotOpener()
    try:
        if event == "subagent_start":
            if os.environ.get("SIDEGRAPH_SUBAGENT_BRIEF") == "off":
                return None
            index = hot(store_path)
            return subagent._brief_text(index.memory_counts()) if index is not None else None

        if event == "agent_brief":
            prompt = params.get("prompt")
            if (
                os.environ.get("SIDEGRAPH_AGENT_BRIEF") == "off"
                or not isinstance(prompt, str)
                or hooks._BRIEF_OPEN in prompt
            ):
                return None
            index = hot(store_path)
            if index is None:
                return None
            addition, _ = hooks._build_agent_brief(prompt, root, root, index, hot)
            return prompt + addition if addition is not None else None

        if os.environ.get("SIDEGRAPH_GREP_NUDGE") == "off":
            return None
        payload = {
            "tool_name": params.get("tool_name", "Read"),
            "tool_input": {"file_path": params.get("file_path")},
            "cwd": root,
        }
        if host_family == "grok":
            payload = hooks._claude_shaped(payload)
        tool = payload["tool_name"]
        if not isinstance(tool, str) or tool not in hooks._POINT_OF_READ_TOOLS:
            return None
        files = hooks._files_named(tool, payload, payload["tool_input"], root)
        if not files or (index := hot(store_path)) is None:
            return None
        selected = hooks._select_delivery(files, index, hot)
        if selected is None:
            return None
        _, fresh_claims, left_out = selected
        return hooks._render_delivery(fresh_claims, left_out)
    finally:
        hot.close()


def emulate_hook(
    event: HookEvent,
    params: dict,
    *,
    store_path: Path,
    reader: GraphifyReader | None,
    borrowed_from: Path | None,
    host_family: Literal["claude", "codex", "grok", "muse"],
    sync: bool = True,
) -> HookEmulation:
    """Render one wired surface for a fresh session whose transcript gate passes.

    Files and prompts use the hook's project anchor as their working directory. The preview
    has no nested session directory or path for an unreadable CLI graph override.
    Relative store diagnostics assume the process was launched from the project's root.
    See design/superpowers/specs/2026-11-17-portal-design.md (D10).
    """
    conditions = list(_FIRING_CONDITIONS[event])
    prepared = Preparation(
        False, 0, None if sync else "The CLI graph differs from the agent's graph."
    )
    if host_family not in WIRED_ON[event]:
        return HookEmulation(
            "", False, f"{event} is not wired on {host_family}.", conditions, prepared
        )

    if event == "agent_brief" and host_family == "claude":
        conditions.append("Claude Code is outside auto mode.")
    if host_family == "grok":
        if event in ("session_start", "subagent_start"):
            conditions.append("Grok discards this output.")
        elif event == "stop":
            conditions.append("Grok ends a user turn with end_turn in a non-headless session.")
        elif event == "mcp_instructions":
            seconds = mcp_entry.GROK_MAP_TIMEOUT_SECONDS
            conditions.append(f"The map builds within the {seconds:g}-second initialize deadline.")

    location, root = _hook_location(store_path, host_family)
    path = Path(location.path)
    text: str | None = None
    try:
        if event == "mcp_instructions":
            text = mcp_entry._grok_session_map(path)
        elif event == "session_start":
            text, prepared = _session_start_text(
                location, reader, borrowed_from, host_family, sync, prepared
            )
        elif _has_existing_store(path):
            if event == "stop":
                if os.environ.get("SIDEGRAPH_CAPTURE_NUDGE") != "off":
                    with Store(path, busy_timeout_seconds=0.0, existing_only=True) as store:
                        drift_count = hooks._stop_drift(store) if sync else None
                    text = hooks._render_stop(
                        drift_count if os.environ.get("SIDEGRAPH_DRIFT_NUDGE") != "off" else None,
                        None,
                    )
            else:
                text = _hot_hook_text(event, params, path, root, host_family)
    except Exception:
        # Like the entry points, a failure after opening a store withholds the output.
        pass

    reason = None if text else "There is no hook output for these inputs and this store."
    return HookEmulation(text or "", bool(text), reason, conditions, prepared)
