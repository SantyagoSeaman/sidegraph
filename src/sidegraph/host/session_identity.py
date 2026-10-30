"""Who is calling this MCP request: the host's own session, from what the host announces.

Host seam. The MCP server process learns its session from its host, with no store round trip:

- Claude Code (``clientInfo.name == "claude-code"``): ``CLAUDE_CODE_SESSION_ID`` in the server's
  own environment (Claude Code >= 2.1.154; stable across ``--resume`` from 2.1.163).
- Codex (``"codex-mcp-client"``): ``threadId`` in the request ``_meta``. The hook side keys a
  Codex session by its rollout file stem, ``rollout-<ts>-<threadId>``, so the id is bridged to
  the SessionStart stamp whose key ends in ``-<threadId>``.
- Grok Build (``"grok-shell-<server name>"``): ``GROK_SESSION_ID`` in the server's own
  environment, the session that started the server. Grok starts one server per session, and
  the hook side keys that session by the same id (measured on Grok Build 1.0.70). A subagent
  reuses its parent's server, so its MCP calls are attributed to the parent, while its own
  hooks carry its own id: the request names no child id to recover it from.
- Anything else (an MCP-only client): no identity and no family. An env id is trusted only
  together with the client name, because a child process inherits its parent's: a Grok started
  from a Claude shell carries ``CLAUDE_CODE_SESSION_ID``, and the reverse.

Never raises: a surprise in any host detail resolves to "no identity".
see design/superpowers/specs/2026-10-11-session-attribution-design.md (D3, D4)
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping

from ..config import TELEMETRY_SESSION_STAMP_PREFIX, SessionHint

CLAUDE_CLIENT = "claude-code"
CODEX_CLIENT = "codex-mcp-client"
CLAUDE_FAMILY = "claude"
CODEX_FAMILY = "codex"
GROK_CLIENT_PREFIX = "grok-shell-"
GROK_FAMILY = "grok"
_CLAUDE_ENV = "CLAUDE_CODE_SESSION_ID"
_GROK_ENV = "GROK_SESSION_ID"


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def bridge_codex_thread(thread_id: str, stamp_keys: Iterable[str]) -> str:
    """The SessionStart key for a Codex thread: the stamp key equal to ``thread_id`` or ending
    in ``-<thread_id>`` (the rollout stem), whatever its age; else the bare ``thread_id``."""
    for key in stamp_keys:
        if key == thread_id or key.endswith("-" + thread_id):
            return key
    return thread_id


def hint_for(
    client_name: str | None,
    meta: Mapping[str, object] | None,
    env: Mapping[str, str],
    stamp_keys: Callable[[], Iterable[str]],
) -> SessionHint:
    """The :class:`SessionHint` for one request.

    ``stamp_keys`` is called only for a Codex request that names a thread, and any failure in
    it yields the bare thread id.
    """
    if client_name == CLAUDE_CLIENT:
        return SessionHint(_text(env.get(_CLAUDE_ENV)), CLAUDE_FAMILY)
    if client_name == CODEX_CLIENT:
        thread_id = _text((meta or {}).get("threadId"))
        if thread_id is None:
            return SessionHint(None, CODEX_FAMILY)
        try:
            return SessionHint(bridge_codex_thread(thread_id, stamp_keys()), CODEX_FAMILY)
        except Exception:
            return SessionHint(thread_id, CODEX_FAMILY)
    if isinstance(client_name, str) and client_name.startswith(GROK_CLIENT_PREFIX):
        return SessionHint(_text(env.get(_GROK_ENV)), GROK_FAMILY)
    return SessionHint(None, None)


def stamp_keys_of(rows: Mapping[str, str]) -> list[str]:
    """Session keys out of ``Store.meta_with_prefix(TELEMETRY_SESSION_STAMP_PREFIX)`` rows."""
    return [key[len(TELEMETRY_SESSION_STAMP_PREFIX) :] for key in rows]
