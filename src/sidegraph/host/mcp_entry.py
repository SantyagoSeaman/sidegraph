"""The ``sidegraph-mcp`` console entry: the decision MCP server plus the session binding.

Host seam. Core (``server``, ``capture``, ``store``, ``config``) never imports this package, so
the code that reads Claude Code's and Codex's identity lives here, behind the one neutral
registration point ``server.main(prepare=...)``. ``python -m sidegraph.server`` runs the same
server without it: no host identity, session ids on writes stay null, journal rows say
``unattributed``.
see design/superpowers/specs/2026-10-11-session-attribution-design.md (D2)
"""

from __future__ import annotations

import os
from typing import Any

import anyio
import mcp.types as mt
from fastmcp import FastMCP
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import ToolResult

from .. import server
from ..config import CURRENT_SESSION, TELEMETRY_SESSION_STAMP_PREFIX, SessionHint
from .session_identity import hint_for, stamp_keys_of


def _stored_stamp_keys() -> list[str]:
    """The session keys SessionStart stamped (Codex bridge). Runs on a worker thread; a store
    that fails to open raises and the caller falls back to the bare thread id."""
    store = server._get_store()
    return stamp_keys_of(store.meta_with_prefix(TELEMETRY_SESSION_STAMP_PREFIX))


def _request_facts(context: MiddlewareContext[Any]) -> tuple[str | None, dict[str, Any]]:
    """The client's name and the request ``_meta`` extras; both empty when unreachable."""
    fastmcp_context = context.fastmcp_context
    if fastmcp_context is None:
        return None, {}
    params = fastmcp_context.session.client_params
    name = params.clientInfo.name if params is not None else None
    meta = fastmcp_context.request_context.meta if fastmcp_context.request_context else None
    return name, meta.model_dump() if meta is not None else {}


class _BindSession(Middleware):
    """Set ``config.CURRENT_SESSION`` around each tool call from what the host announces."""

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        hint: SessionHint | None = None
        try:
            name, meta = _request_facts(context)
            # The Codex bridge reads the store; off the event loop like `_RefreshStore`, so
            # the loop never waits on the store's lock.
            keys: list[str] = []
            if name is not None and isinstance(meta.get("threadId"), str):
                try:
                    keys = await anyio.to_thread.run_sync(_stored_stamp_keys)
                except Exception:
                    keys = []
            hint = hint_for(name, meta, os.environ, lambda: keys)
        except Exception:
            hint = None
        token = CURRENT_SESSION.set(hint)
        try:
            return await call_next(context)
        finally:
            CURRENT_SESSION.reset(token)


def install(mcp: FastMCP) -> None:
    """The ``prepare`` step: add the session-binding middleware."""
    mcp.add_middleware(_BindSession())


def main() -> None:
    """Console-script entry point (``sidegraph-mcp``)."""
    server.main(prepare=install)


if __name__ == "__main__":
    main()
