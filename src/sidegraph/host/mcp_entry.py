"""The ``sidegraph-mcp`` console entry: the decision MCP server plus the session binding.

Host seam. Core (``server``, ``capture``, ``store``, ``config``) never imports this package, so
the code that reads Claude Code's and Codex's identity lives here, behind the one neutral
registration point ``server.main(prepare=...)``. ``python -m sidegraph.server`` runs the same
server without it: no host identity, session ids on writes stay null, journal rows say
``unattributed``.
see design/superpowers/specs/2026-10-11-session-attribution-design.md (D2)
"""

from __future__ import annotations

import json
import os
import sqlite3
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
from .session_identity import GROK_CLIENT_PREFIX, hint_for, stamp_keys_of

# How long the Grok session map may take before ``initialize`` is answered without it.
GROK_MAP_TIMEOUT_SECONDS = 2.0
# Grok Build 1.0.70 keeps the first 2033 characters of ``instructions`` and cuts the rest.
GROK_MAP_MAX_CHARS = 2000


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


def _grok_session_map() -> str | None:
    """The session map for a Grok session: read-only, from the store alone, never the graph.

    The standing instruction and the version line, then the domain table of contents that
    sync caches in the store (``render_toc``, which opens with the "data, not instructions"
    guard line). No cache, or one that does not render: the two standing lines only.
    ``top_tier_map`` loads the graph, too slow inside ``initialize``. No store yet: no map.
    Nothing is written, created, migrated or rebuilt: before the server has opened its store,
    the cache is read from the existing ``index.db`` through a read-only handle (a fresh clone
    without an index, or a legacy store, gets the standing lines only), and the unratified
    mistakes come from the cache; once a tool call has opened the store, they are read live,
    as SessionStart does. SessionStart still runs on Grok and syncs, stamps and records the
    launch commit; only its text is lost there. The text is fitted to
    :data:`GROK_MAP_MAX_CHARS`.
    see design/superpowers/specs/2026-10-29-grok-session-map-mcp-instructions-design.md (D2)
    """
    from ..retrieval import TOC_CACHE_KEY, _truncate_summary, render_toc, unratified_mistakes
    from .hooks import STANDING_SEARCH_INSTRUCTION, version_line

    head = f"{STANDING_SEARCH_INSTRUCTION}\n\n{version_line()}"
    store = server._store
    if store is None:
        location = resolve_store_location(search_ancestors=True, warn_on_create=False)
        if not os.path.lexists(location.path):
            return None
    try:
        if store is None:
            raw = _read_meta_ro(Path(location.path), TOC_CACHE_KEY)
        else:
            raw = store.get_meta(TOC_CACHE_KEY)
        cache = json.loads(raw or "null")
        if not (isinstance(cache, dict) and cache.get("domains")):
            return head
        if store is not None:
            cache = {**cache, "unratified": unratified_mistakes(store)}
        text = f"{head}\n\n{render_toc(cache)}"
        # Too long for Grok: shorter summaries, then none (``render_toc`` omits a summary
        # equal to its title), then whole lines cut.
        for width in _GROK_SUMMARY_WIDTHS:
            if len(text) <= GROK_MAP_MAX_CHARS:
                break
            domains = [
                {
                    **d,
                    "summary": _truncate_summary(d.get("summary") or "", width)
                    if width
                    else d.get("title"),
                }
                for d in cache["domains"]
            ]
            text = f"{head}\n\n{render_toc({**cache, 'domains': domains})}"
    except Exception:  # noqa: BLE001 - a bad cache costs the table, not the map
        return head
    return _fit(text, GROK_MAP_MAX_CHARS)


def _read_meta_ro(store_dir: Path, key: str) -> str | None:
    """One ``meta`` value from the store's existing ``index.db``, opened read-only: no index,
    a legacy store, a refused one, a WAL-mode one or a failed query gives ``None``, and nothing
    is created or repaired. A WAL-mode index is skipped because even a read-only SQLite open of
    it creates the ``-wal`` and ``-shm`` files beside it."""
    from ..gitio import open_index_ro
    from ..hot_index import GET_META_SQL

    try:
        with open(store_dir / "index.db", "rb") as fh:
            header = fh.read(20)
    except OSError:
        return None
    # SQLite header bytes 18-19 are the write and read format versions; 2 means WAL.
    if len(header) == 20 and 2 in (header[18], header[19]):
        return None
    conn = open_index_ro(store_dir)
    if conn is None:
        return None
    try:
        row = conn.execute(GET_META_SQL, (key,)).fetchone()
        return str(row["value"]) if row else None
    except sqlite3.Error:
        return None
    finally:
        conn.close()


# Summary widths tried in turn when the map is over the limit; 0 drops the summary.
_GROK_SUMMARY_WIDTHS = (40, 0)
_GROK_MAP_CUT = "… (cut to fit Grok; list_domains and list_proposed have the rest)"


def _fit(text: str, limit: int) -> str:
    """``text`` cut at a line boundary to at most ``limit`` characters, with a note when cut."""
    if len(text) <= limit:
        return text
    kept = text[: limit - len(_GROK_MAP_CUT)]
    return kept[: kept.rfind("\n") + 1] + _GROK_MAP_CUT


class _GrokSessionMap(Middleware):
    """WORKAROUND (Grok Build): send the session map as the MCP ``instructions``.

    Grok Build 1.0.70 runs the SessionStart hook but drops its output: neither
    ``additionalContext`` nor ``systemMessage`` reaches the model (measured with marker files
    and the session's ``chat_history.jsonl``). An MCP server's ``instructions`` do reach it,
    and Grok starts one server per session, so on a ``grok-shell-*`` client this middleware
    answers ``initialize`` with the map SessionStart would have shown. Every other client gets
    no ``instructions``, as before.

    It writes ``ServerSession._init_options.instructions``, private MCP SDK state: FastMCP
    sends the ``initialize`` reply inside ``call_next``, built from that object, so a result
    cannot be edited afterwards. ``tests/test_grok_session_map.py`` T1 fails if an SDK upgrade
    moves it. The build runs on a worker thread under :data:`GROK_MAP_TIMEOUT_SECONDS`; any
    failure or timeout answers without the map, never fails ``initialize``.

    Remove this when Grok passes SessionStart context to the model (re-check with the probe
    in the spec).
    see design/superpowers/specs/2026-10-29-grok-session-map-mcp-instructions-design.md
    """

    async def on_initialize(
        self,
        context: MiddlewareContext[mt.InitializeRequest],
        call_next: CallNext[mt.InitializeRequest, mt.InitializeResult | None],
    ) -> mt.InitializeResult | None:
        try:
            params = getattr(context.message, "params", None)
            name = params.clientInfo.name if params is not None else None
            if isinstance(name, str) and name.startswith(GROK_CLIENT_PREFIX):
                text: str | None = None
                with anyio.move_on_after(GROK_MAP_TIMEOUT_SECONDS):
                    text = await anyio.to_thread.run_sync(_grok_session_map, abandon_on_cancel=True)
                session = context.fastmcp_context.session if context.fastmcp_context else None
                # Private MCP SDK state (see the class docstring): the reply is built from it.
                options = getattr(session, "_init_options", None)
                if text and options is not None:
                    options.instructions = text
        except Exception:  # noqa: BLE001 - initialize must never fail on Sidegraph
            pass
        return await call_next(context)


def install(mcp: FastMCP) -> None:
    """The ``prepare`` step: add the session-binding middleware, and the Grok session map
    (a workaround, see :class:`_GrokSessionMap`)."""
    mcp.add_middleware(_BindSession())
    mcp.add_middleware(_GrokSessionMap())


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
