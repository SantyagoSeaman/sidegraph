"""Who is calling this MCP request: the host's own session, from what the host announces.

Host seam. The MCP server process learns its session from its host, with no store round trip:

- Claude Code (``clientInfo.name == "claude-code"``): ``CLAUDE_CODE_SESSION_ID`` in the server's
  own environment (Claude Code >= 2.1.154; stable across ``--resume`` from 2.1.163).
- Codex (``"codex-mcp-client"``): ``threadId`` in the request ``_meta``. The hook side keys a
  Codex session by its rollout file stem, ``rollout-<ts>-<threadId>``, so the id is bridged to
  the SessionStart stamp whose key ends in ``-<threadId>``. Probe P1 (2026-11-08,
  codex-cli 0.167.0) verified that a spawned subagent announces its own ``_meta.threadId``
  and its parent's ``_meta.sessionId``: a thread identifies the calling agent exactly.
  An explicit ``thread/compact/start`` via ``codex app-server`` fires SessionStart with
  ``source: "compact"``, the bare thread as ``session_id`` and ``rollout-<ts>-<threadId>`` as
  transcript stem; the MCP thread id stays unchanged. On codex-cli 0.171.0, automatic
  compaction fires the same SessionStart with the same keys, so the existing reset covers it.
- Grok Build (``"grok-shell-<server name>"``): ``GROK_SESSION_ID`` in the server's own
  environment, the session that started the server. Grok starts one server per session, and
  the hook side keys that session by the same id (measured on Grok Build 1.0.70). A subagent
  reuses its parent's server, so its MCP calls are attributed to the parent, while its own
  hooks carry its own id: the request names no child id to recover it from.
- Anything else (an MCP-only client): no identity and no family. An env id is trusted only
  together with the client name, because a child process inherits its parent's: a Grok started
  from a Claude shell carries ``CLAUDE_CODE_SESSION_ID``, and the reverse.

A matching Claude PreToolUse bridge row identifies the main or child agent for one call.

Never raises: a surprise in any host detail resolves to "no identity".
see design/superpowers/specs/2026-10-11-session-attribution-design.md (D3, D4)
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from contextlib import suppress
from datetime import datetime

from ..config import TELEMETRY_SESSION_STAMP_PREFIX, SessionHint

CLAUDE_CLIENT = "claude-code"
CODEX_CLIENT = "codex-mcp-client"
CLAUDE_FAMILY = "claude"
CODEX_FAMILY = "codex"
GROK_CLIENT_PREFIX = "grok-shell-"
GROK_FAMILY = "grok"
_CLAUDE_ENV = "CLAUDE_CODE_SESSION_ID"
_GROK_ENV = "GROK_SESSION_ID"
MCP_CALLER_KEY_PREFIX = "mcp_caller:"
CLAUDE_TOOL_USE_KEY = "claudecode/toolUseId"
SENT_SET_TOOLS = frozenset({"get_task_context", "query_decisions", "retrieve_decisions"})


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def bridge_codex_thread(thread_id: str, stamp_keys: Iterable[str]) -> str:
    """The SessionStart key for a Codex thread: the stamp key equal to ``thread_id`` or ending
    in ``-<thread_id>`` (the rollout stem), whatever its age; else the bare ``thread_id``."""
    for key in stamp_keys:
        if key == thread_id or key.endswith("-" + thread_id):
            return key
    return thread_id


def caller_row(session: str, agent: str | None, now: datetime) -> str:
    return f"{now.isoformat()}|{session}|{agent or '-'}"


def parse_caller_row(raw: object) -> tuple[str, str | None] | None:
    parts = raw.split("|") if isinstance(raw, str) else []
    if len(parts) == 3 and parts[1] and parts[2]:
        return parts[1], None if parts[2] == "-" else parts[2]
    return None


def hint_for(
    client_name: str | None,
    meta: Mapping[str, object] | None,
    env: Mapping[str, str],
    stamp_keys: Callable[[], Iterable[str]],
    caller: Callable[[str], str | None] = lambda _id: None,
) -> SessionHint:
    """The :class:`SessionHint` for one request.

    ``stamp_keys`` is called only for a Codex request that names a thread, and any failure in
    it yields the bare thread id.
    """
    if client_name == CLAUDE_CLIENT:
        session = _text(env.get(_CLAUDE_ENV))
        if session and (tool_use_id := _text((meta or {}).get(CLAUDE_TOOL_USE_KEY))):
            with suppress(Exception):
                if (row := parse_caller_row(caller(tool_use_id))) and row[0] == session:
                    # C1 (2026-11-20, Claude Code 2.1.329) verified child compaction identity.
                    return SessionHint(session, CLAUDE_FAMILY, agent=row[1], agent_exact=True)
        return SessionHint(session, CLAUDE_FAMILY)
    if client_name == CODEX_CLIENT:
        thread_id = _text((meta or {}).get("threadId"))
        if thread_id is None:
            return SessionHint(None, CODEX_FAMILY)
        try:
            return SessionHint(
                bridge_codex_thread(thread_id, stamp_keys()),
                CODEX_FAMILY,
                agent=None,
                agent_exact=True,
            )
        except Exception:
            return SessionHint(thread_id, CODEX_FAMILY, agent=None, agent_exact=True)
    if isinstance(client_name, str) and client_name.startswith(GROK_CLIENT_PREFIX):
        return SessionHint(_text(env.get(_GROK_ENV)), GROK_FAMILY)
    return SessionHint(None, None)


def stamp_keys_of(rows: Mapping[str, str]) -> list[str]:
    """Session keys out of ``Store.meta_with_prefix(TELEMETRY_SESSION_STAMP_PREFIX)`` rows."""
    return [key[len(TELEMETRY_SESSION_STAMP_PREFIX) :] for key in rows]
