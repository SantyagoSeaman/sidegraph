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
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any, cast

import anyio
import mcp.types as mt
from fastmcp import FastMCP
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import ToolResult

from .. import server
from ..capture import parse_ratify_policy, stricter_ratify_policy
from ..config import (
    CURRENT_SESSION,
    TELEMETRY_SESSION_STAMP_PREFIX,
    SessionHint,
    _store_project_root,
    repository_root,
    resolve_store_location,
)
from ..hot_index import GET_META_SQL
from ..schema import Decision
from ..store import _has_existing_store
from ..store_layout import refuse_outside_store_link, symlinked_internals
from . import session_identity as bridge
from .claude_settings import RATIFY_POLICY_ENV_VAR, current_ratify_policy
from .project_anchor import (  # noqa: F401  (REPO_ROOT_FLAG is re-exported)
    REPO_ROOT_FLAG,
    anchor_to_project,
    take_repo_root_flag,
)
from .session_identity import GROK_CLIENT_PREFIX, hint_for, stamp_keys_of

# How long the Grok session map may take before ``initialize`` is answered without it.
GROK_MAP_TIMEOUT_SECONDS = 2.0
# Grok Build 1.0.70 keeps the first 2033 characters of ``instructions`` and cuts the rest.
GROK_MAP_MAX_CHARS = 2000


def _stored_stamp_keys() -> list[str]:
    """Read the Codex bridge stamps without opening a Store or creating an index."""
    prefix = TELEMETRY_SESSION_STAMP_PREFIX
    store = server._store
    if store is not None:
        return stamp_keys_of(store.meta_with_prefix(prefix))

    store_dir = Path(resolve_store_location(search_ancestors=True, warn_on_create=False).path)
    refuse_outside_store_link(store_dir)
    if (
        not store_dir.is_dir()
        or symlinked_internals(store_dir)
        or not _has_existing_store(store_dir)
    ):
        return []

    def read(conn: sqlite3.Connection) -> list[str]:
        rows = conn.execute("SELECT key, value FROM meta WHERE key GLOB ?", (prefix + "*",))
        return stamp_keys_of({row["key"]: row["value"] for row in rows})

    return _query_index_ro(store_dir, read) or []


def _stored_caller(tool_use_id: str) -> str | None:
    key = bridge.MCP_CALLER_KEY_PREFIX + tool_use_id
    if (store := server._store) is not None:
        return value if isinstance(value := store.get_meta(key), str) else None
    path = Path(resolve_store_location(search_ancestors=True, warn_on_create=False).path)
    refuse_outside_store_link(path)
    if not path.is_dir() or symlinked_internals(path) or not _has_existing_store(path):
        return None
    row = _query_index_ro(path, lambda conn: conn.execute(GET_META_SQL, (key,)).fetchone())
    return value if row is not None and isinstance(value := row["value"], str) else None


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
            # Read Codex stamps off the event loop, like `_RefreshStore`.
            keys: list[str] = []
            if name is not None and isinstance(meta.get("threadId"), str):
                try:
                    keys = await anyio.to_thread.run_sync(_stored_stamp_keys)
                except Exception:
                    keys = []
            row = None
            use_id = meta.get(bridge.CLAUDE_TOOL_USE_KEY) or None
            claude = name == bridge.CLAUDE_CLIENT and os.environ.get("CLAUDE_CODE_SESSION_ID")
            if claude and context.message.name in bridge.SENT_SET_TOOLS and isinstance(use_id, str):
                with suppress(Exception):
                    row = await anyio.to_thread.run_sync(_stored_caller, use_id)
            hint = hint_for(name, meta, os.environ, lambda: keys, caller=lambda _id: row)
        except Exception:
            hint = None
        token = CURRENT_SESSION.set(hint)
        try:
            return await call_next(context)
        finally:
            CURRENT_SESSION.reset(token)


def _render_grok_map(cache: dict | None) -> str:
    from ..retrieval import render_toc
    from .hooks import STANDING_SEARCH_INSTRUCTION, version_line

    head = f"{STANDING_SEARCH_INSTRUCTION}\n\n{version_line()}"
    if cache is None:
        return head
    return _fit(f"{head}\n\n{render_toc(cache)}", GROK_MAP_MAX_CHARS)


def _grok_session_map(store_path: Path | None = None) -> str | None:
    """The session map for a Grok session: read-only, from the store alone, never the graph.

    The standing instruction and the version line, then the map that
    sync caches in the store (``render_toc``, which opens with the "data, not instructions"
    guard line). No cache, or one that does not render: the two standing lines only.
    ``top_tier_map`` loads the graph, too slow inside ``initialize``. No store yet: no map.
    Nothing is written, created or rebuilt: before the server has opened its store,
    the cache is read from the existing ``index.db`` through a read-only handle (a fresh clone
    without an index, or a legacy store, gets the standing lines only), and the global
    mistakes (accepted and unratified) from the current decision rows through the same kind of
    handle (rows it cannot read: no global sections, never the cache's, which may still list
    a retired one). The rows are trusted only while the index is current, that is, while its
    certified digest equals the record files' (:func:`_index_is_current`); after a ``git pull``
    that changed a record they are not, and the map carries no global sections until a tool
    call opens the store and refreshes the index. Once a tool call has opened the store, they
    are read from it. Either way they are live, as SessionStart reads them. SessionStart still
    runs on Grok and syncs, stamps and records the launch commit; only its text is lost there.
    The text is fitted to :data:`GROK_MAP_MAX_CHARS` by cutting whole lines.
    see design/superpowers/specs/2026-10-29-grok-session-map-mcp-instructions-design.md (D2)
    """
    from ..retrieval import (
        TOC_CACHE_KEY,
        global_mistakes_from,
        live_global_mistakes,
    )

    store = server._store if store_path is None else None
    if store is None and store_path is None:
        store_path = Path(resolve_store_location(search_ancestors=True, warn_on_create=False).path)
    if store_path is not None and not os.path.lexists(store_path):
        return None
    try:
        raw = (
            _read_meta_ro(cast(Path, store_path), TOC_CACHE_KEY)
            if store is None
            else store.get_meta(TOC_CACHE_KEY)
        )
        cache = json.loads(raw or "null")
        if not (isinstance(cache, dict) and cache.get("domains")):
            return _render_grok_map(None)
        if store is not None:
            cache = {**cache, **live_global_mistakes(store)}
        elif store_path is not None and _index_is_current(store_path):
            rows = _read_global_decisions_ro(store_path) or []
            cache = {**cache, **global_mistakes_from(rows)}
        else:
            # The record files changed since this index was built (a `git pull`), so its rows
            # and the cache's own lists may still hold a retired rule. Serve no global
            # sections; the first tool call opens the store and refreshes the index.
            cache = {k: v for k, v in cache.items() if k not in _GLOBAL_SECTIONS}
        return _render_grok_map(cache)
    except Exception:  # noqa: BLE001 - a bad cache costs the table, not the map
        return _render_grok_map(None)


# The cache keys that carry global rules; the cold map drops them when the index is behind.
_GLOBAL_SECTIONS = ("global_mistakes", "unratified")


def _index_is_current(store_dir: Path) -> bool:
    """True iff the digest the index was last certified against equals the record files' now.
    A read-only walk (a stat per file, a few milliseconds for a few hundred records) and one
    meta read; a missing or cleared digest, or any error, reads as not current."""
    from ..store import _CANONICAL_DIGEST_KEY, canonical_digest

    stored = _read_meta_ro(store_dir, _CANONICAL_DIGEST_KEY)
    if not stored:
        return False
    try:
        current, _stats = canonical_digest(store_dir)
    except Exception:  # noqa: BLE001 - an unreadable tree is not a current index
        return False
    return stored == current


def _read_meta_ro(store_dir: Path, key: str) -> str | None:
    """One ``meta`` value from the store's existing ``index.db``, opened read-only: no index,
    a legacy store, a refused one, a WAL-mode one or a failed query gives ``None``, and nothing
    is created or repaired. A WAL-mode index is skipped because even a read-only SQLite open of
    it creates the ``-wal`` and ``-shm`` files beside it."""
    from ..hot_index import GET_META_SQL

    def read(conn: sqlite3.Connection) -> str | None:
        row = conn.execute(GET_META_SQL, (key,)).fetchone()
        return str(row["value"]) if row else None

    return _query_index_ro(store_dir, read)


def _read_global_decisions_ro(store_dir: Path) -> list[Decision] | None:
    """The accepted and proposed decision rows of the existing ``index.db`` that may be global
    mistakes, read as :func:`_read_meta_ro` reads (``None`` when it cannot). A row that does
    not parse is skipped alone; the caller applies the lifecycle and proposal filters."""
    from ..store_layout import MISTAKE_KINDS

    def read(conn: sqlite3.Connection) -> list[Decision]:
        out: list[Decision] = []
        for row in conn.execute(
            "SELECT data FROM decisions WHERE status IN ('accepted', 'proposed')"
        ):
            try:
                raw = json.loads(row["data"])
                if raw.get("scope") != "global" or raw.get("kind") not in MISTAKE_KINDS:
                    continue
                out.append(Decision.model_validate(raw))
            except (ValueError, TypeError, AttributeError, RecursionError):
                continue
        return out

    return _query_index_ro(store_dir, read)


def _query_index_ro[T](store_dir: Path, read: Callable[[sqlite3.Connection], T]) -> T | None:
    """``read`` over a read-only handle on the existing ``index.db``; ``None`` for no index, a
    legacy, refused or WAL-mode one, or a query error. Nothing is created or repaired."""
    from ..gitio import open_index_ro

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
        return read(conn)
    except sqlite3.Error:
        return None
    finally:
        conn.close()


_GROK_MAP_CUT = "… (cut to fit Grok; retrieve_decisions and list_proposed have the rest)"


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
    """Let the selected store project's committed policy tighten the environment's, never loosen it.

    `sidegraph-init` commits the policy to `.claude/settings.json`'s `env` block. Claude Code
    applies that block to its MCP servers itself; Codex, Grok and Muse do not. For them the
    committed value counts only when it is stricter than the environment's policy
    (`manual` < `auto-low-risk` < `auto-all`): the stricter of the two is written back to the
    environment. A committed file therefore never supplies a policy when the environment sets
    none (the core default stays `manual`) and never loosens one. Unknown, empty and blank
    values, in either place, parse as `manual`. A settings problem never stops the server:
    anything but a readable committed value leaves the environment as it was.
    see design/superpowers/specs/2026-11-15-0310-batch-a-design.md (D1)
    """
    raw = os.environ.get(RATIFY_POLICY_ENV_VAR, "")
    if not raw.strip():
        return
    try:
        store_path = resolve_store_location(search_ancestors=True, warn_on_create=False).path
        project_root = repository_root(store_path) or _store_project_root(store_path)
        state = current_ratify_policy(project_root)
    except Exception:  # noqa: BLE001 - a settings problem must never stop the MCP server
        return
    # A key that is present always counts, whatever it holds: an empty, blank or unreadable
    # value is a team saying something the parser reads as `manual`, and that only tightens.
    if state.outcome != "already_set" or state.existing_value is None:
        return
    environment = parse_ratify_policy(raw)
    effective = stricter_ratify_policy(environment, parse_ratify_policy(state.existing_value))
    if effective is not environment:
        os.environ[RATIFY_POLICY_ENV_VAR] = effective.value


def main() -> None:
    """Console-script entry point (``sidegraph-mcp``)."""
    if take_repo_root_flag(sys.argv):
        anchor_to_project()
    apply_project_ratify_policy()
    server.main(prepare=install)


if __name__ == "__main__":
    main()
