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
import sys
from pathlib import Path
from typing import Any

import anyio
import mcp.types as mt
from fastmcp import FastMCP
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import ToolResult

from .. import server
from ..config import (
    CURRENT_SESSION,
    TELEMETRY_SESSION_STAMP_PREFIX,
    SessionHint,
    _store_project_root,
    repository_root,
    resolve_store_location,
)
from .claude_settings import RATIFY_POLICY_ENV_VAR, current_ratify_policy, repo_root_for_settings
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


def apply_project_ratify_policy() -> None:
    """Apply the selected store project's committed policy when the environment sets none.

    `sidegraph-init` commits the policy to `.claude/settings.json`'s `env` block. Claude Code
    applies that block to its MCP servers, Codex does not, so without this a Codex session
    proposed every record whatever the team chose. An explicit, non-blank environment value
    wins; the core keeps reading only the environment. A settings problem never stops the
    server: anything but a readable committed value leaves the default.
    """
    if os.environ.get(RATIFY_POLICY_ENV_VAR, "").strip():
        return
    try:
        store_path = resolve_store_location(search_ancestors=True, warn_on_create=False).path
        project_root = repository_root(store_path) or _store_project_root(store_path)
        state = current_ratify_policy(project_root)
    except Exception:  # noqa: BLE001 - a settings problem must never stop the MCP server
        return
    if state.outcome == "already_set" and state.existing_value:
        os.environ[RATIFY_POLICY_ENV_VAR] = state.existing_value


REPO_ROOT_FLAG = "--repo-root"


def anchor_to_project() -> None:
    """Move the process to the project root; the plugin manifests ask for it with ``--repo-root``.

    ``CLAUDE_PROJECT_DIR`` wins when it is set, non-blank and an existing directory (what the
    old ``cd "${CLAUDE_PROJECT_DIR}"`` launcher did). Otherwise the git top-level of the cwd
    applies (what the old Codex ``cd "$(git rev-parse --show-toplevel)"`` did), and outside a
    repository nothing moves. Anchoring never raises: the server starts wherever it is.
    see design/superpowers/specs/2026-10-13-sh-free-plugin-launch-design.md (D3)
    """
    project = os.environ.get("CLAUDE_PROJECT_DIR", "").strip()
    try:
        target = Path(project) if project and Path(project).is_dir() else repo_root_for_settings()
        os.chdir(target)
    except Exception:  # noqa: BLE001 - anchoring must never stop the MCP server
        return


def main() -> None:
    """Console-script entry point (``sidegraph-mcp``)."""
    if REPO_ROOT_FLAG in sys.argv[1:]:
        sys.argv = [a for a in sys.argv if a != REPO_ROOT_FLAG]
        anchor_to_project()
    apply_project_ratify_policy()
    server.main(prepare=install)


if __name__ == "__main__":
    main()
