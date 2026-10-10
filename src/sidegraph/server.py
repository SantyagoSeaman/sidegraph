"""The decision MCP — the tools agents call (Stage 1).

Engine-independent: this server exposes the owned store over MCP so decisions can be added,
superseded, and retrieved with no engine present yet. Anchor resolution against Graphify
arrives in Stage 3; retrieval merge + budgeting in Stage 4.

Run with ``uv run sidegraph-mcp`` (stdio transport).
"""

from __future__ import annotations

import contextlib
import json
import os
import posixpath
import re
import sys
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path, PurePosixPath
from typing import Literal, cast, get_args

import anyio.to_thread
import mcp.types as mt
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import FunctionTool, Tool, ToolResult
from pydantic import ValidationError

from . import nearest_anchored, seed_ladder
from .anchoring import entity_summaries as _entity_summaries
from .anchoring import orphan_reason, resolve_and_bind
from .capture import (
    AnchorDraft,
    RatifyPolicy,
    _bind_initiative,
    _bind_orphaned,
    _capture_commit,
    _clean_initiative,
    _resolves_to_live_decision,
    _session_id_fallback,
    format_communities_sample,
    format_fact_proposal,
    format_path_prefixes,
    format_proposal,
    format_seed_anchors_sample,
    normalize_tags,
    parse_ratify_policy,
    propose,
    propose_facts,
    redact,
)
from .capture import propose_domains as _propose_domain_drafts
from .config import (
    DEFAULT_GRAPH,
    TELEMETRY_SESSION_KEY,
    borrowed_graph_candidate,
    default_graph_path,
    main_checkout_root,
    path_is_file,
    path_state,
    repository_root,
    resolve_store_location,
)
from .domains import DEFAULT_CANDIDATE_LIMIT, collect_domain_candidates, community_group_path
from .engine.reader import GraphifyReader, open_borrowed_reader
from .freshness import staleness_phrase
from .input_limits import MAX_DRAFTS, InputLimitError, preflight_direct, preflight_drafts
from .retrieval import (
    _STANDING_SUPERSEDE_HINT,
    MEMORY_GUARD_LINE,
    TOC_CACHE_KEY,
    RetrievalBudget,
    Seed,
    TaskContext,
    _entities_by_node,
    build_toc,
    proposal_surfaces,
    resolve_seeds,
)
from .retrieval import drill_down as _drill_down
from .retrieval import get_task_context as _retrieve
from .retrieval import query_structure as _query_structure
from .schema import (
    AnchorBinding,
    Decision,
    DecisionKind,
    DecisionStatus,
    Descriptor,
    Domain,
    DomainStatus,
    Fact,
    Provenance,
    Relation,
)
from .store import VOLATILE_STALE_KEY, Store, skipped_refusal_text
from .sync import activate_accepted_domain, maybe_sync, report_as_dict, sync
from .validation_errors import SafeWriteError
from .verify import verify_snapshot
from .write_boundary import CONFIGURATION_ERROR, is_safe_write, safe_write, validate_write_arguments


def _server_version() -> str:
    """Sidegraph's own installed package version, for FastMCP's ``serverInfo.version`` (Gate-5
    finding N1) — the ``initialize`` handshake previously fell through to fastmcp's default
    (its OWN package version, not ours), which misidentified sidegraph to any MCP client that
    surfaces server version. ``PackageNotFoundError`` (e.g. running from a source checkout
    with no installed distribution metadata) falls back to a clearly-synthetic placeholder
    rather than crashing server startup over a cosmetic field."""
    try:
        return _pkg_version("sidegraph")
    except PackageNotFoundError:
        return "0.0.0-dev"


mcp = FastMCP("sidegraph", version=_server_version())

# What every tool tells a host about itself. A host with an auto-reviewer (Codex) reviews an MCP
# call unless the tool is read-only, or non-destructive and closed-world: an unannotated tool
# takes the MCP defaults (destructive, open-world) and has had memory reads denied. No tool
# sends anything off the machine (``openWorldHint`` false) and none deletes a record
# (``destructiveHint`` false: the store is append-only, a write adds a record or closes one with
# ``valid_to``). ``readOnlyHint`` is true only for a tool that never changes a tracked file:
# a write to ``.sidegraph/index.db`` (derived index, statistics, ledgers; gitignored, never sent)
# and the first open of a store do not count. The tools that run the lazy sync
# (``_synced_reader``) are not read-only: its moved rung can adopt a moved symbol into a tracked
# entity file. ``tests/test_server_borrowed_graph.py::test_t2_*`` holds the read-only set to it.
# see design/superpowers/specs/2026-10-04-tool-annotations-and-argument-names-design.md (D1, D2)
_READ_ONLY = mt.ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
_LOCAL_WRITE = mt.ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)

# One process-wide store, created LAZILY on first use -- never as a side effect of merely
# importing this module. A bare eager `_store = Store(...)` at import time used to
# materialize a stray store (e.g. "sidegraph.db" in whatever the current working directory
# happened to be) just from `import sidegraph.server`, which is exactly the kind of
# import-time side effect a library module must not have. Path resolution is shared with
# the CLI and the Claude Code hooks via config.resolve_store_location (SIDEGRAPH_DIR primary,
# SIDEGRAPH_DB honored for back-compat, default ".sidegraph") -- see
# docs/reference/configuration.md. The server is a host surface: launched in a subdirectory,
# a relative store that is absent there is looked up in the ancestors, inside the repository
# (design/superpowers/specs/2026-10-01-subdirectory-launch-design.md D3).
_store: Store | None = None

# Guards `_get_store()`'s memoization (review Important-2b): fastmcp 3 dispatches sync
# @mcp.tool calls onto worker threads (see store.py's own threading note), so a cold-start
# process can have several requests race the check-then-set below at once. A bare
# `if _store is None: _store = Store(...)` is not atomic -- two threads can both observe
# None, both construct a Store (leaking the loser's open sqlite connection), and callers
# end up disagreeing on which instance is "the" store.
_store_lock = threading.Lock()


def _get_store() -> Store:
    """Lazily create and memoize the process-wide Store on first actual use.

    Double-checked locking: the lock is only taken on the (rare) cold-start race window:
    once `_store` is set, every later call reads it lock-free.
    """
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = Store(resolve_store_location(search_ancestors=True).path)
    return _store


def _refresh_store() -> None:
    """Re-check the process's store against the record files, if it is open yet.

    A store that is not open yet is not refreshed: the tool body that needs it opens it, and a
    fresh open is current by construction. A filesystem error (``OSError``) does not fail the
    tool call: a git checkout, pull or compact creates and deletes record files while the digest
    walk lists and stats them, and the walk can lose that race. The reload is one transaction,
    so the previous index is intact; the tool answers from it, one line says so on stderr, and
    the next call checks again. Anything else (a schema mismatch, an index failure) propagates:
    a tool call fails loudly rather than answer from an index it could not verify.
    see design/superpowers/specs/2026-10-03-hot-path-light-index-design.md (D4)
    """
    store = _store
    if store is None:
        return
    try:
        store.refresh_if_stale()
    except OSError as exc:
        print(
            f"sidegraph: could not re-check the record files ({exc}); "
            "answering from the previous index",
            file=sys.stderr,
        )


class _RefreshStore(Middleware):
    """Before every tool call, bring the open store up to date with the record files on disk.

    ``_get_store()`` memoizes one ``Store`` for the life of the process and a ``Store`` checks
    the record files only when it is constructed, so a record that arrives mid-session (a
    ``git pull``, a teammate's write through another process) would stay invisible to every tool
    until a restart. Middleware, not a call inside ``_get_store()``: that accessor runs two or
    three times per tool, once after ``maybe_sync`` in ``get_task_context``, where a rebuild
    would reset the synced bindings right before retrieval. Middleware also covers every tool,
    including one added later, without a decorator anyone could forget. The check is a digest
    walk with a rebuild only when the files changed; it runs on a worker thread so the event loop
    never waits on the store's lock.
    see design/superpowers/specs/2026-10-03-hot-path-light-index-design.md (D4)
    """

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        await anyio.to_thread.run_sync(_refresh_store)
        return await call_next(context)


mcp.add_middleware(_RefreshStore())


# The tools that take seeds (``files`` / ``entities``): the only ones that also accept
# ``paths`` and ``seeds``. Broader aliasing would hide a real mistake on a tool that has no
# such parameter.
_SEED_TOOLS = frozenset({"get_task_context", "query_decisions", "query_structure"})
# The extensions that make a seed without a ``/`` a file path. A list, not "any short suffix":
# ``Store.close`` and ``View.body`` end in what looks like a suffix and are symbols. Lowercase;
# a seed's extension is lowercased before the lookup. Single letters that are also common member
# names (``m``, ``r``, ``d``) are left out.
_FILE_EXTENSIONS = frozenset(
    """
    py pyi pyx ipynb js jsx mjs cjs ts tsx vue svelte java kt kts scala groovy gradle swift go rs
    rb php cs c h cc cpp cxx hh hpp mm dart lua pl ex exs erl hs ml clj zig nim jl sql sh bash zsh
    ps1 bat proto graphql tf cmake mk
    json jsonc yaml yml toml ini cfg conf env properties xml plist lock csv tsv
    md mdx rst adoc txt tex html htm css scss sass less svg pdf log
    """.split()  # noqa: SIM905 -- a word list reads better than 100 quoted strings
)
_NO_FREE_TEXT_QUERY = (
    "There is no free-text query: pass files or entities. intent is only a label for statistics."
)


def _seed_strings(name: str, value: object) -> list[str]:
    """The strings in a ``paths`` / ``seeds`` argument: a list of them, or one bare string."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return value
    raise ToolError(f"`{name}` must be a list of strings, or one string.")


def _looks_like_a_path(seed: str) -> bool:
    """A seed with no whitespace that holds a ``/``, or ends in a known file extension
    (:data:`_FILE_EXTENSIONS`), is a file path; a symbol (``RetryPolicy``, ``Store.close``), an
    issue id (``ABC-42``) or free text is not.
    see design/superpowers/specs/2026-10-04-tool-annotations-and-argument-names-design.md (D3, A1)
    """
    if re.search(r"\s", seed) is not None:
        return False
    if "/" in seed:
        return True
    _, dot, extension = seed.rpartition(".")
    return bool(dot) and extension.lower() in _FILE_EXTENSIONS


def _extend(args: dict, key: str, routed: list) -> None:
    """Append ``routed`` to ``args[key]``, order kept and duplicates dropped. A value of any
    other type than a list is left for the tool's own validation to refuse."""
    if not routed:
        return
    current = args.get(key)
    if current is not None and not isinstance(current, list):
        return
    merged: list = []
    for item in [*(current or []), *routed]:
        if item not in merged:
            merged.append(item)
    args[key] = merged


def _route_seed_aliases(args: dict) -> dict:
    """``paths`` is ``files``; each ``seeds`` item goes to ``files`` or, as ``{"name": seed}``,
    to ``entities`` by its shape. Both are appended to any explicit ``files`` / ``entities``."""
    args = dict(args)
    files: list[str] = []
    entities: list[dict] = []
    if "paths" in args:
        files += _seed_strings("paths", args.pop("paths"))
    if "seeds" in args:
        for seed in _seed_strings("seeds", args.pop("seeds")):
            if _looks_like_a_path(seed):
                files.append(seed)
            else:
                entities.append({"name": seed})
    _extend(args, "files", files)
    _extend(args, "entities", entities)
    return args


def _unknown_argument_error(tool: str, unknown: list[str], parameters: list[str]) -> ToolError:
    """The error for an argument that is not a parameter: it names the argument and lists the
    ones the tool has, so an agent can correct itself in one retry."""
    names = ", ".join(f"`{u}`" for u in unknown)
    noun = "argument" if len(unknown) == 1 else "arguments"
    listed = (
        f"Its parameters are: {', '.join(parameters)}." if parameters else "It takes no parameters."
    )
    message = f"Unknown {noun} {names} for `{tool}`. {listed}"
    if "task" in unknown and tool in _SEED_TOOLS:
        message += f"\n{_NO_FREE_TEXT_QUERY}"
    return ToolError(message)


_WRITE_TOOLS = frozenset(
    {
        "add_decision",
        "supersede_decision",
        "add_fact",
        "supersede_fact",
        "propose_decisions",
        "ratify",
        "ratify_decisions",
        "add_domain",
        "supersede_domain",
        "propose_domains",
        "sync_anchors",
        "add_anchors",
    }
)
_READ_TOOLS = frozenset(
    {
        "list_facts",
        "find_entity",
        "get_entity_history",
        "list_proposed",
        "list_domain_candidates",
        "list_domains",
        "verify_store",
    }
)
_LAZY_READ_TOOLS = frozenset(
    {
        "retrieve_decisions",
        "get_task_context",
        "query_structure",
        "query_decisions",
        "drill_down",
    }
)


def _classify_tool(tool: Tool) -> bool:
    """Classify only audited names, annotations and exact registered callable identities."""
    if not isinstance(tool, FunctionTool):
        raise ToolError(CONFIGURATION_ERROR)
    expected = _REGISTERED_CALLABLES.get(tool.name)
    fn = tool.fn
    annotations = tool.annotations
    if (
        expected is None
        or fn is not expected
        or annotations is None
        or annotations.destructiveHint is not False
        or annotations.openWorldHint is not False
    ):
        raise ToolError(CONFIGURATION_ERROR)
    if tool.name in _READ_TOOLS and annotations.readOnlyHint is True:
        return False
    if tool.name in _LAZY_READ_TOOLS and annotations.readOnlyHint is False:
        return False
    if tool.name in _WRITE_TOOLS and annotations.readOnlyHint is False and is_safe_write(fn):
        return True
    raise ToolError(CONFIGURATION_ERROR)


class _ArgumentNames(Middleware):
    """Accept the argument names agents try, and refuse the rest with a message they can act on.

    The seed tools take ``paths`` as ``files`` and route each ``seeds`` item by shape (a path to
    ``files``, anything else to ``entities``). ``task`` is not an alias: ``intent`` is a
    statistics label that never affects the answer, and the field's ``task`` values were
    queries, so renaming one into the other would turn a query into a seedless call that looks
    successful; it gets the unknown-argument error with a line saying there is no free-text
    query. Read tools name an unknown argument and list their real parameters. Logical
    writes keep parameter help but never repeat the unknown name. Their identity-bound
    registration, raw admission and argument-only validation run before dispatch; guarded
    callables stop arbitrary exception text from reaching framework warning/error logging.
    Nothing is dropped silently.
    see design/superpowers/specs/2026-10-04-tool-annotations-and-argument-names-design.md (D3)
    """

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        fastmcp_context = context.fastmcp_context
        if fastmcp_context is None:
            raise ToolError(CONFIGURATION_ERROR)
        try:
            tool = await fastmcp_context.fastmcp.get_tool(context.message.name)
        except Exception:
            raise ToolError(CONFIGURATION_ERROR) from None
        if tool is None:
            raise ToolError(CONFIGURATION_ERROR)
        write = _classify_tool(tool)
        sent = context.message.arguments or {}
        args = _route_seed_aliases(sent) if tool.name in _SEED_TOOLS else sent
        parameters = list((tool.parameters or {}).get("properties", {}))
        unknown = [name for name in args if name not in parameters]
        if unknown:
            if write:
                raise ToolError(
                    "Unknown write argument. Its parameters are: " + ", ".join(parameters) + "."
                )
            raise _unknown_argument_error(tool.name, unknown, parameters)
        if write:
            validate_write_arguments(tool, args)
        if args is sent or args == sent:
            return await call_next(context)
        message = context.message.model_copy(update={"arguments": args})
        return await call_next(context.copy(message=message))


mcp.add_middleware(_ArgumentNames())


def _commit_hint(store: Store) -> str | None:
    """The sentence a write tool adds to its result: the store sits inside a git repository, so
    a record in it is stranded on this checkout until the change that produced it is committed.
    ``None`` outside a repository, and ``None`` for a symlinked store whose target lies outside
    it: git commits the link, not the records behind it, so the advice would be false. The prefix
    is the store's path relative to the repository root, lexical like ``repository_root``: no
    subprocess, and a symlinked store keeps the name its project knows it by.
    # see design/superpowers/specs/2026-10-02-stranded-store-writes-design.md (D3)"""
    try:
        root = repository_root(store.path)
        if root is None:
            return None
        if not Path(store.path).resolve().is_relative_to(root.resolve()):
            return None
        prefix = Path(os.path.abspath(store.path)).relative_to(root).as_posix()
    except (OSError, ValueError):
        return None
    return (
        f"Commit {prefix}/ in the same change as the work that produced this record, so it "
        "reaches other checkouts and teammates."
    )


def _with_commit_hint(store: Store, result: dict) -> dict:
    """``result`` with a ``commit_hint`` key when the store is inside a repository."""
    hint = _commit_hint(store)
    if hint is not None:
        result["commit_hint"] = hint
    return result


def _hint_elements(store: Store, results: list[dict], status: str) -> list[dict]:
    """``results`` with a ``commit_hint`` on each element whose ``status`` is ``status`` (the one
    that wrote a record), when the store is inside a repository."""
    hint = _commit_hint(store)
    if hint is not None:
        for element in results:
            if element.get("status") == status:
                element["commit_hint"] = hint
    return results


def _graph_path(store: Store | None = None) -> str:
    """The graph ``_load_reader()`` reads: ``$SIDEGRAPH_GRAPH`` (empty counts as unset) or
    ``graphify-out/graph.json``, a relative value resolved against the store's project --
    the same rule as the CLI, so both always read the graph belonging to the store they
    write to. Factored out so ``sync_anchors`` can name where it looked."""
    return str(default_graph_path((store or _get_store()).path))


def _load_reader(store: Store | None = None) -> GraphifyReader | None:
    """Best-effort reader over the store's own graph (see :func:`_graph_path`). None if absent."""
    try:
        return GraphifyReader(_graph_path(store))
    except Exception:
        return None


def _unreadable_graph_error(store: Store) -> str:
    """``sync_anchors``'s error when no reader could be built: names the resolved path and,
    when the same relative value exists beside the process cwd, says how to use it."""
    value = os.environ.get("SIDEGRAPH_GRAPH") or DEFAULT_GRAPH
    try:
        graph = _graph_path(store)
    except OSError:  # a relative store path resolves against the cwd, which is unreadable
        return f"graph not readable ({value})"
    msg = f"graph not readable ({graph})"
    if os.path.isabs(value):
        return msg
    try:
        here = Path(os.path.abspath(value))  # getcwd() raises when the cwd is unreadable
    except OSError:
        return msg
    if (
        str(here) != graph
        and path_state(Path(graph))
        == "missing"  # a corrupt or unreadable graph is not a missing one
        and path_is_file(here)
    ):
        msg += (
            f"; {here} exists beside the server's cwd -- "
            "set SIDEGRAPH_GRAPH to an absolute path to use it"
        )
    return msg


# The only legal AnchorBinding.relation values (Relation is a Literal, not an enum) — used
# to validate `anchors[i]["relation"]` BEFORE any store write (see _validate_anchors).
_VALID_RELATIONS = frozenset(get_args(Relation))


def _redact_fields(*fields: str | None) -> tuple[list[str | None], int]:
    """Scrub every text field through ``capture.redact`` (``None`` passes through).

    The direct write paths (``add_decision``/``supersede_decision``) used to commit their
    text verbatim while the propose/import pipelines redacted first — a gap against the
    redact-first rule for a repo-committed store (2026-07-10 audit). Returns
    ``(clean_fields, total_replacement_count)``.
    """
    out: list[str | None] = []
    total = 0
    for field in fields:
        if field is None:
            out.append(None)
        else:
            clean, n = redact(field)
            out.append(clean)
            total += n
    return out, total


def _validate_anchors(anchors: list[dict] | None) -> None:
    """Raise ``ValueError`` before any store write if the anchor list is invalid: an anchor's
    ``relation`` is not a legal ``Relation``; a named anchor's ``(name, file_path)`` is not a
    valid ``Descriptor``; or a non-empty list has no named anchor at all. Every anchor-taking
    write path calls this first, so a call either writes with its anchors or writes nothing
    (see design/superpowers/specs/2026-09-22-pre-write-anchor-validation-design.md).
    """
    if not anchors:
        return
    for raw in anchors:
        relation = raw.get("relation")
        if relation is not None and relation not in _VALID_RELATIONS:
            raise SafeWriteError("relation", "enum")
        name = raw.get("name")
        if name:
            try:
                Descriptor(name=name, file_path=raw.get("file_path"))
            except ValidationError:
                raise SafeWriteError("anchors", "string_type") from None
    if not any(raw.get("name") for raw in anchors):
        raise SafeWriteError("anchors", "no_named_anchor")


def _require_fact_reachability(store, anchors: list[dict] | None, supports: list[str]) -> None:
    """Anchorless fact-write gate (design D8): a fact with no anchors must have at least one
    ``supports`` id resolving to a LIVE (accepted/proposed) decision, or it is unreachable the
    moment it lands — exactly the shape doctor's tightened ``dangling-record`` check (D4/D5/
    D6) would flag. A fact WITH an anchor is untouched.

    Raise before ANY write, same discipline as ``_validate_anchors`` above. Shared by
    ``_add_fact_impl`` and ``_supersede_fact_impl``'s no-anchors path — the two human-asked
    fact-writing entry points — mirroring ``capture.py``'s own anchorless-fact gate for the
    agent-initiated path (``_resolves_to_live_decision``, imported from there, is the one
    shared definition of "live" all three write paths use).
    """
    if anchors:
        return
    if not _resolves_to_live_decision(store, supports):
        raise SafeWriteError("supports", "no_live_support")


def _add_decision_impl(
    store,
    reader,
    title: str,
    kind: str,
    context: str,
    choice: str,
    rejected: str | None = None,
    consequences: str | None = None,
    author: str | None = None,
    session_id: str | None = None,
    anchors: list[dict] | None = None,
    initiative: str | None = None,
    tags: list[str] | str | None = None,
    layer: str | None = None,
) -> dict:
    """Testable core: redact, write the decision, then best-effort multi-anchor it (+ tag it)."""
    preflight_direct(
        title,
        kind,
        context,
        choice,
        rejected,
        consequences,
        author,
        session_id,
        anchors,
        initiative,
        tags,
        layer,
    )
    _validate_anchors(anchors)
    # title/context/choice are required (non-Optional) here, so they're redacted directly
    # (keeps them typed `str`, not the `str | None` `_redact_fields` returns uniformly);
    # only the genuinely optional pair goes through `_redact_fields`.
    title, n1 = redact(title)
    context, n2 = redact(context)
    choice, n3 = redact(choice)
    (rejected, consequences), n4 = _redact_fields(rejected, consequences)
    redactions = n1 + n2 + n3 + n4
    graph_version = reader.graph_version() if reader is not None else None
    # I1 (R1 improvement wave §1): the D7.3 marker fallback, extended to the "add" pair --
    # same rule _supersede_decision_impl already applies (see its own comment): explicit
    # param wins, fallback only fills absence. No design rationale on record for why a
    # direct add made mid-session deserved worse attribution than a propose.
    if session_id is None:
        session_id = _session_id_fallback(store)
    decision = Decision(
        title=title,
        kind=DecisionKind(kind),
        status=DecisionStatus.ACCEPTED,
        context=context,
        choice=choice,
        rejected=rejected,
        consequences=consequences,
        # `layer` is a free-form MCP-tool string; Decision.layer is the strict Literal —
        # pydantic validates/rejects at construction (same as `DecisionKind(kind)` above),
        # this cast only satisfies the static type, it changes no runtime behavior.
        layer=cast(Literal["business", "technical"] | None, layer),
        valid_from=datetime.now(UTC),
        provenance=Provenance(
            source="human",
            author=author,
            session_id=session_id,
            graph_version=graph_version,
            # П0 (git-bindings design, Blocker 1): the "add" pair's commit stamp, same
            # helper _supersede_decision_impl already uses -- mechanical I1 twin.
            commit=_capture_commit(store),
        ),
    )
    store.add_decision(decision)

    anchors_skipped, anchors_orphaned = _resolve_anchors(decision.id, anchors, reader, store)
    # Decision-level, so it binds whatever the reader and the anchors: cleaned like every
    # other prose field, and before the bindings summary below so the result includes it.
    clean_initiative, n5 = _clean_initiative(initiative)
    redactions += n5
    if clean_initiative:
        _bind_initiative(store, decision.id, clean_initiative)
    tag_slugs, tag_redactions = normalize_tags(tags or [])
    redactions += tag_redactions
    for slug in tag_slugs:
        tag_entity = store.get_or_create_abstract_entity(f"tag:{slug}")
        store.add_binding(
            AnchorBinding(
                record_id=decision.id,
                entity_id=tag_entity.entity_id,
                tier=0,
            )
        )
    bindings = store.bindings_for_record(decision.id)
    return _with_commit_hint(
        store,
        {
            "id": decision.id,
            "status": decision.status.value,
            "bindings": len(bindings),
            "entities": _entity_summaries(store, bindings),
            "anchors_skipped": anchors_skipped,
            "anchors_orphaned": anchors_orphaned,
            "redactions": redactions,
        },
    )


def _resolve_anchors(
    record_id: str,
    anchors: list[dict] | None,
    reader,
    store,
) -> tuple[list[dict], list[dict]]:
    """Resolve+bind every anchor ref, returning ``(ambiguous, orphaned)`` as feedback.

    ``ambiguous`` (Gate-5 finding S3) is
    ``[{"name", "reason": "ambiguous", "candidates": [...capped 5]}]`` — matched more than
    one node, so NO leaf was created.

    ``orphaned`` is the entity summary of every leaf bound for an anchor that resolved to
    NOTHING. That leaf IS written (``anchoring.resolve_and_bind``: "created when resolved
    or unresolved"), deliberately — but it is dead on arrival: ``valid_decisions_for_entity``
    skips orphaned bindings, so no retrieval path, no ``drill_down`` and no PreToolUse hook
    can ever deliver the record through it, and no Tier-1 community fallback is created
    either (there is no resolved node to take a community from). Reporting it is the whole
    point: an unresolved anchor used to come back as ``bindings: 1``, an entity summary and
    an empty ``anchors_skipped`` — indistinguishable from success. Measured cost of that
    silence: 29% of Tier-2 bindings orphaned-at-birth on the airflow corpus against 0-4%
    everywhere else (``design/testing/2026-08-03-delivery-gap-remeasure.md``).

    The bucket names mirror ``add_anchors``, which already reports
    ``bound``/``orphaned``/``ambiguous`` separately — one vocabulary for one fact.

    ``resolve_and_bind`` already carries the ``reader.resolve()`` outcome on its return
    value (``anchoring.AnchorResolution``), so this never re-resolves a ref just to learn
    why no leaf binding was created. No-op (``([], [])``) when there's no reader — anchoring
    is best-effort throughout this module, and neither "ambiguous" nor "orphaned" is
    meaningful with no graph to resolve against.
    """
    skipped: list[dict] = []
    orphaned: list[dict] = []
    if anchors and reader is not None:
        for raw in anchors:
            name = raw.get("name")
            if not name:
                continue
            ref = Descriptor(name=name, file_path=raw.get("file_path"))
            result = resolve_and_bind(
                record_id,
                ref,
                reader,
                store,
                relation=raw.get("relation"),
            )
            if result.status == "ambiguous":
                skipped.append(
                    {
                        "name": name,
                        "reason": "ambiguous",
                        "candidates": result.candidates[:5],
                    }
                )
            elif result.status == "unresolved":
                # Summarize only the leaves THIS anchor just produced, never the record's
                # whole binding set: a record can carry earlier live anchors, and a bucket
                # that reported those as orphaned would be worse than no bucket at all.
                reason = orphan_reason(ref, reader)
                orphaned.extend(
                    {**s, "reason": reason}
                    for s in _entity_summaries(store, [b for b in result if b.tier == 2])
                )
    return skipped, orphaned


@mcp.tool(annotations=_LOCAL_WRITE)
@safe_write
def add_decision(
    title: str,
    kind: str,
    context: str,
    choice: str,
    rejected: str | None = None,
    consequences: str | None = None,
    author: str | None = None,
    session_id: str | None = None,
    anchors: list[dict] | None = None,
    initiative: str | None = None,
    tags: list[str] | str | None = None,
    layer: str | None = None,
) -> dict:
    """Append a decision (ADR / lesson / constraint / gotcha) to the store.

    ``rejected`` is what was tried and abandoned, and why. ``anchors`` is a list of
    ``{"name": ..., "file_path": ..., "relation": ...}`` refs to the code entities the
    decision is about (``relation`` optional: creates|modifies|affects|deprecates|
    considered, defaults to "affects"); each is resolved against the current Graphify graph
    and multi-anchored (leaf + domain/community). Anchoring is best-effort: with no graph
    present, the decision still writes. ``initiative``, when given, is bound as a Tier-0
    ``initiative:<name>`` entity on every call, with or without a graph or anchors. A
    malformed anchor list — an invalid ``relation``, a non-string ``name``/``file_path``, or
    a non-empty list with no named anchor — is rejected before anything is written, graph or not.

    Every text field (title/context/choice/rejected/consequences, tag text before
    slugification, and ``initiative``; a blank or all-secret initiative binds nothing) is
    redacted first — same secret patterns as the propose/import
    pipelines; the scrubbed text is the only text that reaches the repo-committed store.

    ``tags`` are free-form labels; a bare comma-separated string is accepted too. If the raw
    string contains both a matched secret and a comma, all its tags are omitted conservatively.
    Use a list for explicit boundaries. Safe tags are slugified (lowercase, spaces->'-',
    ``[a-z0-9-]`` only) into durable ``tag:<slug>`` entities (tier-0, many-to-many — a decision
    can carry several, and ``get_entity_history`` finds it via any of them, same as an
    initiative).
    ``layer`` optionally marks the decision "business" or "technical" — a filter axis for
    mixed corpora.

    Returns ``{"id", "status", "bindings", "entities", "anchors_skipped", "anchors_orphaned",
    "redactions"}`` (``redactions`` = secret replacements made across all text fields) —
    ``entities`` is ``[{"entity_id", "canonical_name", "tier"}, ...]``, one per binding created,
    so a caller can chain straight into ``find_entity``/``get_entity_history`` without touching
    the store.
    ``anchors_skipped`` is ``[{"name", "reason": "ambiguous", "candidates"}, ...]`` — the
    anchors whose name matched more than one graph node (candidates capped at 5), so no
    precise Tier-2 leaf was created for them; empty when every anchor resolved cleanly or no
    graph is present. ``anchors_orphaned`` is
    ``[{"entity_id", "canonical_name", "tier": 2, "reason"}, ...]`` — the anchors that resolved
    to nothing (always ``[]`` when no graph is present).
    The leaf is still written but is dead on arrival: retrieval, ``drill_down`` and the
    PreToolUse hook skip it. Read ``reason`` first: ``file-not-in-graph`` usually means a
    stale graph (run ``graphify update .``, then re-anchor), ``name-not-in-file`` means the
    name is wrong (``find_entity`` says what is there), ``no-file-path`` means pass
    ``file_path``. Repair with ``add_anchors`` — no duplicate record, no content-free
    supersession.
    """
    return _add_decision_impl(
        _get_store(),
        _load_reader(),
        title,
        kind,
        context,
        choice,
        rejected=rejected,
        consequences=consequences,
        author=author,
        session_id=session_id,
        anchors=anchors,
        initiative=initiative,
        tags=tags,
        layer=layer,
    )


def _supersede_decision_impl(
    store,
    reader,
    old_decision_id: str,
    title: str,
    kind: str,
    context: str,
    choice: str,
    rejected: str | None = None,
    consequences: str | None = None,
    anchors: list[dict] | None = None,
    session_id: str | None = None,
    author: str | None = None,
    source: str = "human",
) -> dict:
    """Testable core: write the successor, then anchor it (explicit anchors, or inherit).

    See CLAUDE.md gap notes: an unanchored successor was invisible to task-seeded retrieval
    exactly where a reversal matters most. Two paths, never both:

    - ``anchors`` given -> resolve_and_bind the successor to ONLY those refs (same as
      add_decision; best-effort, skipped if no reader). Reuses ``_resolve_anchors`` so this
      path reports the same per-anchor ``anchors_skipped`` feedback ``add_decision`` does
      (Gate finding: this used to discard ``resolve_and_bind``'s per-anchor result outright,
      so an ambiguous explicit anchor on a supersede silently produced no Tier-2 leaf and no
      feedback about why).
    - ``anchors`` omitted -> copy the predecessor's existing bindings verbatim (same
      entity_id/tier/weight/relation/status) onto the successor. This is the obviously-right
      default: a reversal concerns the same entities the original decision did, so retrieval
      should find the successor everywhere it found the predecessor. Nothing is "skipped" on
      this path (inheritance never resolves against the graph), so ``anchors_skipped`` is
      always ``[]`` here.

    ``session_id``/``author``/``source`` (design D6, all optional/additive): stamped onto
    the successor's ``Provenance`` the same way ``propose`` stamps a captured decision's.
    ``source`` defaults to ``"human"`` — this tool's own historical hardcoded value, so an
    existing caller that never passes it keeps stamping exactly what it always has; an
    agent-initiated caller (e.g. a future supersede-from-neighbors flow) passes
    ``source="agent"`` instead. ``graph_version``/``commit`` are stamped the way ``propose``
    stamps them too — ``graph_version`` from the reader when present, ``commit`` via the
    same best-effort ``git rev-parse HEAD`` (:func:`sidegraph.capture._capture_commit`).
    """
    preflight_direct(
        old_decision_id,
        title,
        kind,
        context,
        choice,
        rejected,
        consequences,
        anchors,
        session_id,
        author,
        source,
    )
    _validate_anchors(anchors)
    if not anchors and store.is_skipped("bindings", old_decision_id):
        # Inheritance reads the predecessor's bindings from the index, and a bindings file the
        # reload left out has no rows there, so the successor would be written with no anchors.
        # see design/superpowers/specs/2026-10-03-store-survives-a-bad-file-design.md D5
        raise ValueError(skipped_refusal_text("bindings", old_decision_id))
    # See _add_decision_impl: title/context/choice are required, redacted directly (stays
    # `str`); only the optional pair goes through `_redact_fields` (returns `str | None`).
    title, n1 = redact(title)
    context, n2 = redact(context)
    choice, n3 = redact(choice)
    (rejected, consequences), n4 = _redact_fields(rejected, consequences)
    redactions = n1 + n2 + n3 + n4
    graph_version = reader.graph_version() if reader is not None else None
    # D7.3, extended post-E9b: the marker fallback lived in _propose_one only, so every
    # supersede-path successor landed session_id=None even mid-session (measured in the
    # E9b run). Same rule as propose: explicit param wins, fallback only fills absence.
    if session_id is None:
        session_id = _session_id_fallback(store)
    replacement = Decision(
        title=title,
        kind=DecisionKind(kind),
        status=DecisionStatus.ACCEPTED,
        context=context,
        choice=choice,
        rejected=rejected,
        consequences=consequences,
        valid_from=datetime.now(UTC),
        supersedes=old_decision_id,
        provenance=Provenance(
            source=source,
            author=author,
            session_id=session_id,
            graph_version=graph_version,
            commit=_capture_commit(store),
        ),
    )
    store.add_decision(replacement)

    if anchors:
        anchors_skipped, anchors_orphaned = _resolve_anchors(replacement.id, anchors, reader, store)
    else:
        # Inheritance resolves nothing against the graph, so neither bucket can speak here
        # -- including when a predecessor binding being copied is ITSELF already orphaned.
        # Surfacing inherited orphans is a real gap (see docs/guides/surviving-refactors.md
        # on omitting `anchors`), but it is a different question from "the anchor you just
        # passed did not resolve", and answering it here would report a state this call
        # neither created nor could fix.
        anchors_skipped, anchors_orphaned = [], []
        for b in store.bindings_for_record(old_decision_id):
            store.add_binding(
                AnchorBinding(
                    record_id=replacement.id,
                    entity_id=b.entity_id,
                    tier=b.tier,
                    weight=b.weight,
                    status=b.status,
                    relation=b.relation,
                )
            )

    bindings = store.bindings_for_record(replacement.id)
    return _with_commit_hint(
        store,
        {
            "id": replacement.id,
            "supersedes": old_decision_id,
            "bindings": len(bindings),
            "entities": _entity_summaries(store, bindings),
            "anchors_skipped": anchors_skipped,
            "anchors_orphaned": anchors_orphaned,
            "redactions": redactions,
        },
    )


@mcp.tool(annotations=_LOCAL_WRITE)
@safe_write
def supersede_decision(
    old_decision_id: str,
    title: str,
    kind: str,
    context: str,
    choice: str,
    rejected: str | None = None,
    consequences: str | None = None,
    anchors: list[dict] | None = None,
    session_id: str | None = None,
    author: str | None = None,
    source: str = "human",
) -> dict:
    """Reverse a decision: close the old one and append a replacement that supersedes it.

    Call this when work has made a recorded decision false, too broad, or reversed — the
    situations retrieval renders as ``(id: ...)`` lines and ``propose_decisions`` reports as
    ``neighbors``. Never leave a new record contradicting a live old one.

    The predecessor is not deleted — it stays retrievable as "tried before, abandoned".
    Every text field is redacted first, exactly like ``add_decision``'s.

    Anchoring: pass ``anchors`` (same shape as ``add_decision``'s — a list of
    ``{"name": ..., "file_path": ...}`` refs) to resolve and bind the successor to ONLY
    those refs. Omit ``anchors`` (the default) to INHERIT the predecessor's bindings
    verbatim instead — the successor concerns the same entities the original decision did,
    so it should be reachable via task-seeded retrieval everywhere the predecessor was.
    Passing ``anchors`` replaces inheritance; it never adds to it.
    Explicit ``anchors`` are validated like ``add_decision``'s before anything is written.

    ``session_id``/``author`` (optional) and ``source`` (default ``"human"``, this tool's
    historical hardcoded value — pass ``"agent"`` when an agent calls this itself, e.g. off
    a ``neighbors`` or ``(id: ...)`` hint) are stamped onto the successor's provenance,
    alongside ``graph_version`` and a best-effort capture-time ``commit`` (same fields
    ``propose_decisions`` stamps).

    Returns ``{"id", "supersedes", "bindings", "entities", "anchors_skipped",
    "anchors_orphaned", "redactions"}`` — ``anchors_skipped`` is ``[{"name", "reason": "ambiguous",
    "candidates"}, ...]``, the same per-anchor feedback ``add_decision`` returns
    (candidates capped at 5): populated
    only on the explicit-``anchors`` path (an anchor whose name matched more than one graph
    node got no precise Tier-2 leaf), always ``[]`` when ``anchors`` is omitted since
    inheritance never resolves against the graph. ``anchors_orphaned`` is
    ``[{"entity_id", "canonical_name", "tier": 2, "reason"}, ...]`` — the anchors that resolved
    to nothing (always ``[]`` when no graph is present).
    The leaf is still written but is dead on arrival: retrieval, ``drill_down`` and the
    PreToolUse hook skip it. Read ``reason`` first: ``file-not-in-graph`` usually means a
    stale graph (run ``graphify update .``, then re-anchor), ``name-not-in-file`` means the
    name is wrong (``find_entity`` says what is there), ``no-file-path`` means pass
    ``file_path``. Repair with ``add_anchors`` — no duplicate record, no content-free
    supersession.
    """
    return _supersede_decision_impl(
        _get_store(),
        _load_reader(),
        old_decision_id,
        title,
        kind,
        context,
        choice,
        rejected=rejected,
        consequences=consequences,
        anchors=anchors,
        session_id=session_id,
        author=author,
        source=source,
    )


def _bind_fact_anchors(
    fact_id: str,
    anchors: list[dict] | None,
    reader,
    store,
) -> tuple[list[dict], list[dict]]:
    """Anchor a fact's explicit ``anchors``, returning ``(ambiguous, orphaned)`` — the same
    two buckets ``_resolve_anchors`` gives ``add_decision``.

    With a reader, this IS ``_resolve_anchors``. With no reader, bind an orphaned Tier-2
    leaf per anchor instead of ``_resolve_anchors``'s no-op — facts must not repeat
    ``add_decision``'s no-graph anchors-silently-dropped asymmetry
    (design/superpowers/specs/2026-07-10-facts-layer-design.md) — and report those leaves
    in the orphaned bucket too: a graph-less run produces a dead anchor exactly as an
    unresolved name does, and the caller has the same reason to know. Shared by
    ``_add_fact_impl`` and ``_supersede_fact_impl``'s explicit-anchors path so both give the
    same guarantee.
    """
    if not anchors:
        return [], []
    if reader is not None:
        return _resolve_anchors(fact_id, anchors, reader, store)
    orphaned: list[dict] = []
    for raw in anchors:
        if not raw.get("name"):
            continue
        before = {b.entity_id for b in store.bindings_for_record(fact_id)}
        _bind_orphaned(
            fact_id,
            AnchorDraft.model_validate(raw),
            store,
            relation=raw.get("relation"),
        )
        orphaned.extend(
            _entity_summaries(
                store,
                [
                    b
                    for b in store.bindings_for_record(fact_id)
                    if b.tier == 2 and b.entity_id not in before
                ],
            )
        )
    return [], orphaned


def _add_fact_impl(
    store,
    reader,
    statement: str,
    source: str,
    supports: list[str] | None = None,
    anchors: list[dict] | None = None,
    author: str | None = None,
    session_id: str | None = None,
) -> dict:
    """Testable core: redact, write the fact, then best-effort multi-anchor it.

    Human-asked path: lands ``status=accepted`` directly (the asking human was the gate —
    same rationale as ``add_decision``, no ``proposed``-then-ratify hop) with
    ``provenance.source="human"``.

    This is the PRIMARY fact-writing path (what the ``record-fact`` skill drives) and, before
    design D8, had no reachability gate at all: a bare ``add_fact(statement, source)`` — no
    anchors, no supports — wrote a record no retrieval surface could ever find, and
    ``add_fact(..., supports=[<terminal id>])`` wrote one born flagged by doctor's tightened
    ``dangling-record`` check. ``_require_fact_reachability`` closes both.
    """
    preflight_direct(statement, source, supports, anchors, author, session_id)
    _validate_anchors(anchors)
    _require_fact_reachability(store, anchors, supports or [])
    # statement/source are both required (non-Optional) — redact directly, same reasoning
    # as _add_decision_impl (keeps them typed `str`, not `_redact_fields`'s `str | None`).
    statement, n1 = redact(statement)
    source, n2 = redact(source)
    redactions = n1 + n2
    graph_version = reader.graph_version() if reader is not None else None
    # I1 (R1 improvement wave §1): same "add" pair extension as _add_decision_impl's.
    if session_id is None:
        session_id = _session_id_fallback(store)
    fact = Fact(
        statement=statement,
        source=source,
        supports=supports or [],
        status=DecisionStatus.ACCEPTED,
        valid_from=datetime.now(UTC),
        provenance=Provenance(
            source="human",
            author=author,
            session_id=session_id,
            graph_version=graph_version,
            # П0 (git-bindings design, Blocker 1): same "add" pair extension as
            # _add_decision_impl's.
            commit=_capture_commit(store),
        ),
    )
    store.add_fact(fact)

    anchors_skipped, anchors_orphaned = _bind_fact_anchors(fact.id, anchors, reader, store)
    bindings = store.bindings_for_record(fact.id)
    return _with_commit_hint(
        store,
        {
            "id": fact.id,
            "statement": fact.statement,
            "status": fact.status.value,
            "redactions": redactions,
            "entities": _entity_summaries(store, bindings),
            "anchors_skipped": anchors_skipped,
            "anchors_orphaned": anchors_orphaned,
        },
    )


@mcp.tool(annotations=_LOCAL_WRITE)
@safe_write
def add_fact(
    statement: str,
    source: str,
    supports: list[str] | None = None,
    anchors: list[dict] | None = None,
    author: str | None = None,
    session_id: str | None = None,
) -> dict:
    """Append a hard-won fact — human-asked, lands ``accepted`` immediately (no ratify hop).

    Only facts the code graph cannot derive belong here: empirics (benchmarks, observed
    behavior), external constraints (API limits, library capabilities), trial-learned
    knowledge — never 'the code does X'.

    ``statement`` is the fact itself (1-2 sentences, hard-compact); ``source`` is the
    epistemics — how we know ("benchmark run 2026-07-09", "httpx docs"). ``supports`` is a
    list of decision ids this fact informed (each must already exist; missing references
    refuse the write with controlled MCP diagnostics). ``anchors`` uses the same
    ``{"name", "file_path", "relation"?}`` ref shape ``add_decision`` takes; resolved against
    the current Graphify graph and bound
    when a graph is present. With no graph, an anchor still gets an ORPHANED Tier-2 leaf
    (unlike ``add_decision``, which silently skips anchors with no reader) — a fact must
    never write unreachable, so the binding heals once a graph exists.

    Every text field (statement/source) is redacted first, same secret patterns as
    ``add_decision``'s.

    Returns ``{"id", "statement", "status", "redactions", "entities", "anchors_skipped",
    "anchors_orphaned"}`` — ``entities`` is ``[{"entity_id", "canonical_name", "tier"}, ...]``,
    one per binding created; ``anchors_skipped`` is
    ``[{"name", "reason": "ambiguous", "candidates"}, ...]``
    (candidates capped at 5) — populated only when a graph is present and an anchor's name
    matched more than one node, since there is nothing to be ambiguous against otherwise.
    ``anchors_orphaned`` is
    ``[{"entity_id", "canonical_name", "tier": 2, "reason"}, ...]`` — the anchors that resolved
    to nothing against a graph. With no graph present every anchor lands here instead, as
    ``{"entity_id", "canonical_name", "tier": 2}`` with no ``reason`` key; it heals on the
    next sync once a graph exists.
    The leaf is still written but is dead on arrival: retrieval, ``drill_down`` and the
    PreToolUse hook skip it. When ``reason`` is present, read it first:
    ``file-not-in-graph`` usually means a stale graph (run ``graphify update .``, then
    re-anchor), ``name-not-in-file`` means the name is wrong (``find_entity`` says what is
    there), ``no-file-path`` means pass
    ``file_path``. Repair with ``add_anchors`` — no duplicate record, no content-free
    supersession.
    """
    return _add_fact_impl(
        _get_store(),
        _load_reader(),
        statement,
        source,
        supports=supports,
        anchors=anchors,
        author=author,
        session_id=session_id,
    )


def _supersede_fact_impl(
    store,
    reader,
    old_fact_id: str,
    statement: str,
    source: str,
    supports: list[str] | None = None,
    anchors: list[dict] | None = None,
    session_id: str | None = None,
    author: str | None = None,
) -> dict:
    """Testable core: write the successor, then anchor it (explicit anchors, or inherit).

    Mirrors ``_supersede_decision_impl`` exactly (falsification, not deletion): the
    predecessor must already exist; ``supports`` defaults to the PREDECESSOR's ``supports``
    when omitted (a superseding fact informs the same decisions unless told otherwise).
    ``anchors`` given -> resolve fresh via ``_bind_fact_anchors`` (same no-graph-orphans
    guarantee ``add_fact`` gives). ``anchors`` omitted -> copy the predecessor's bindings
    verbatim (same entity_id/tier/weight/relation/status, including ``orphaned``) onto the
    successor — nothing is "skipped" on this path since inheritance never resolves against
    the graph.

    ``session_id``/``author`` (I1, R1 improvement wave §1 — design D6 shape, same fields
    ``_supersede_decision_impl`` takes): stamped onto the successor's ``Provenance``, plus
    the same D7.3 fallback when the caller passes no ``session_id``. Unlike
    ``_supersede_decision_impl``, there is no ``source`` override param here — ``source``
    already names the FACT's own epistemics text (this function's positional ``source``
    argument, e.g. "benchmark run"); provenance ``source`` stays hardcoded ``"human"``,
    the same choice ``_add_fact_impl``/``add_fact`` already make for the identical reason.

    Reachability gate (design D8), no-anchors path only: this path used to inherit the
    predecessor's ``supports`` verbatim with no re-check, so a predecessor whose sole
    supporting decision has since gone terminal produced a successor born flagged by
    doctor's tightened ``dangling-record`` check. Gated only when the predecessor has NO
    binding to inherit either — when it does, the binding-inheritance loop below carries a
    real anchor forward regardless of ``supports``, and that already-reachable ordinary case
    must not be rejected (``anchors`` requested is what "anchorless" means here, per D8's
    residual note, but a predecessor's inherited BINDING is not a request — it is the same
    reachability the predecessor already had).
    """
    preflight_direct(old_fact_id, statement, source, supports, anchors, session_id, author)
    predecessor = store.get_fact(old_fact_id)
    if predecessor is None:
        raise ValueError(f"unknown fact {old_fact_id!r}")
    _validate_anchors(anchors)
    if not anchors and store.is_skipped("bindings", old_fact_id):
        # Same as _supersede_decision_impl: inheriting from a skipped bindings file copies nothing.
        raise ValueError(skipped_refusal_text("bindings", old_fact_id))
    effective_supports = supports if supports is not None else predecessor.supports
    if not anchors and not store.bindings_for_record(old_fact_id):
        _require_fact_reachability(store, anchors, effective_supports)
    # statement/source are both required (non-Optional) — redact directly, same reasoning
    # as _add_decision_impl (keeps them typed `str`, not `_redact_fields`'s `str | None`).
    statement, n1 = redact(statement)
    source, n2 = redact(source)
    redactions = n1 + n2
    graph_version = reader.graph_version() if reader is not None else None
    if session_id is None:
        session_id = _session_id_fallback(store)
    replacement = Fact(
        statement=statement,
        source=source,
        supports=effective_supports,
        status=DecisionStatus.ACCEPTED,
        valid_from=datetime.now(UTC),
        supersedes=old_fact_id,
        provenance=Provenance(
            source="human",
            author=author,
            session_id=session_id,
            graph_version=graph_version,
            # П0 (git-bindings design, Blocker 1): same best-effort HEAD stamp every
            # other write path in the mirror now applies.
            commit=_capture_commit(store),
        ),
    )
    store.add_fact(replacement)

    if anchors:
        anchors_skipped, anchors_orphaned = _bind_fact_anchors(
            replacement.id, anchors, reader, store
        )
    else:
        # Inheritance resolves nothing — same reasoning as _supersede_decision_impl's.
        anchors_skipped, anchors_orphaned = [], []
        for b in store.bindings_for_record(old_fact_id):
            store.add_binding(
                AnchorBinding(
                    record_id=replacement.id,
                    entity_id=b.entity_id,
                    tier=b.tier,
                    weight=b.weight,
                    status=b.status,
                    relation=b.relation,
                )
            )

    bindings = store.bindings_for_record(replacement.id)
    return _with_commit_hint(
        store,
        {
            "id": replacement.id,
            "statement": replacement.statement,
            "status": replacement.status.value,
            "redactions": redactions,
            "entities": _entity_summaries(store, bindings),
            "anchors_skipped": anchors_skipped,
            "anchors_orphaned": anchors_orphaned,
            "supersedes": old_fact_id,
        },
    )


@mcp.tool(annotations=_LOCAL_WRITE)
@safe_write
def supersede_fact(
    old_fact_id: str,
    statement: str,
    source: str,
    supports: list[str] | None = None,
    anchors: list[dict] | None = None,
    session_id: str | None = None,
    author: str | None = None,
) -> dict:
    """Falsify a fact: close the old one and append a replacement that supersedes it.

    The predecessor is not deleted — it stays retrievable as "believed before, corrected
    because…". Every text field is redacted first, exactly like ``add_fact``'s.
    ``supports`` defaults to the predecessor's ``supports`` when omitted.

    Anchoring: pass ``anchors`` (same shape as ``add_fact``'s) to resolve and bind the
    successor to ONLY those refs (best-effort with a graph, orphaned-leaf fallback without
    one — same as ``add_fact``). Omit ``anchors`` (the default) to INHERIT the
    predecessor's bindings VERBATIM instead — same entity_id/tier/weight/relation/status,
    including any ``orphaned`` ones carried as-is. Passing ``anchors`` replaces
    inheritance; it never adds to it.

    ``session_id``/``author`` (optional, I1 — R1 improvement wave §1) are stamped onto the
    successor's provenance, same as ``add_decision``'s/``supersede_decision``'s; an
    unpassed ``session_id`` falls back to the fresh Stop-channel marker when one exists
    (design D7.3). Provenance ``source`` always stamps ``"human"`` here — same as
    ``add_fact``'s.

    Returns ``{"id", "statement", "status", "redactions", "entities", "anchors_skipped",
    "anchors_orphaned", "supersedes"}`` — same shape as ``add_fact``'s plus ``supersedes`` (the
    predecessor's id); ``anchors_orphaned`` entries follow ``add_fact``'s rule (no ``reason``
    key when no graph is present) and the same ``add_anchors`` repair.
    """
    return _supersede_fact_impl(
        _get_store(),
        _load_reader(),
        old_fact_id,
        statement,
        source,
        supports=supports,
        anchors=anchors,
        session_id=session_id,
        author=author,
    )


def _retrieve_decisions_impl(store, include_superseded: bool = False) -> list[dict]:
    decisions = list(store.iter_decisions())
    if not include_superseded:
        decisions = [
            d
            for d in decisions
            if d.status not in (DecisionStatus.SUPERSEDED, DecisionStatus.REJECTED)
        ]
    # The proposal-surfacing policy applies HERE too (practitioner re-review round 2). This
    # raw listing is deliberately unranked — but "unranked" is a ranking exemption, not a
    # policy exemption: regulated mode and the surfacing window exist to keep unreviewed
    # text away from an agent, and an MCP tool that hands it over anyway is a documented
    # bypass of a security control. Accepted records are untouched.
    decisions = [
        d for d in decisions if d.status != DecisionStatus.PROPOSED or proposal_surfaces(d)
    ]
    # Mistakes first: gotchas and lessons before ADRs/constraints.
    rank = {DecisionKind.GOTCHA: 0, DecisionKind.LESSON: 1}
    decisions.sort(key=lambda d: rank.get(d.kind, 2))
    return [d.model_dump(mode="json") for d in decisions]


# -- the bounded decision listing -----------------------------------------------------------
# see design/superpowers/specs/2026-10-04-bounded-decision-listing-design.md (D1-D5)

_LISTING_LIMIT_MAX = 100
_LISTING_BUDGET_MIN = 4000
_LISTING_BUDGET_MAX = 60000
# The statuses a listing hides unless it is asked for them (``include_superseded``, ``status``).
_HISTORY = frozenset({DecisionStatus.SUPERSEDED.value, DecisionStatus.REJECTED.value})
_ROW_KEYS = ("id", "kind", "status", "title", "valid_from")
_SEARCHED_FIELDS = ("title", "context", "choice", "rejected", "consequences")
_MISTAKE_RANK = {DecisionKind.GOTCHA.value: 0, DecisionKind.LESSON.value: 1}
# What an answer echoes of the caller's own text, counted as JSON writes it, so an echo cannot
# break the budget.
_ECHO_CHARS = 80
_UNRESOLVED_LISTED = 10
_CANDIDATES_LISTED = 3
_OVERVIEW_HINT = (
    "Full records come back only for a narrowed call: status=..., kind=..., files=[...], "
    "query=..., ids=[...]."
)


def _dump(value) -> str:
    """The wire form of an answer: compact JSON, and the unit ``budget_chars`` counts in."""
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def _listing_values(value, enum, name: str) -> set[str] | None:
    """``status`` or ``kind`` as a set of lowercase enum values; ``None`` when absent or empty.
    An unknown value is an error that names the valid ones."""
    items = [value] if isinstance(value, str) else list(value or [])
    wanted = {text.lower() for item in items if (text := str(item).strip())}
    if not wanted:
        return None
    valid = [member.value for member in enum]
    unknown = sorted(wanted - set(valid))
    if unknown:
        raise ValueError(
            f"unknown {name} {', '.join(repr(u) for u in unknown)}; "
            f"valid values: {', '.join(valid)}"
        )
    return wanted


def _listing_texts(values: list[str] | None) -> list[str]:
    """``files`` or ``ids`` without blank entries; an empty list is an absent filter."""
    return [text for item in values or [] if (text := str(item).strip())]


def _listing_entities(
    store, reader, files: list[str], borrowed_from: Path | None
) -> tuple[set[str], list[dict]]:
    """The entities each ``files`` entry names, and the entries nothing could read.

    Read the way ``get_task_context`` reads file seeds. With a graph, a path it does not hold
    goes through the seed ladder (a basename, a suffix, a case slip, a directory), the resulting
    seeds through ``resolve_seeds`` (the node mapping and the descriptor), and every entity whose
    descriptor ``file_path`` equals one of the paths read is added: that reaches a file that is
    no longer in the graph. Without a graph only that last step runs, on the spelling
    ``_normalized_seeds`` gives. An entry is judged alone, so each can be reported alone.

    An entry that matched no entity is reported with the reason the ladder gave (``ambiguous``,
    ``directory-too-large``, ``not-in-graph``) or ``no-graph``, and the candidates it offered.
    A file the graph holds with nothing anchored to it is not reported: it was read, and
    ``matched`` says it has no memory.
    see design/superpowers/specs/2026-10-04-bounded-decision-listing-design.md (D3, A2)
    """
    entities = list(store.iter_concrete_entities())
    by_path: dict[str, list[str]] = {}
    for entity in entities:
        if entity.descriptor is not None and entity.descriptor.file_path:
            by_path.setdefault(entity.descriptor.file_path, []).append(entity.entity_id)
    # One pass over the entities per call, not one per entry.
    by_node = _entities_by_node(store, entities) if reader is not None else None
    root_of = _once(
        lambda: (
            (repository_root(store.path) if borrowed_from is not None else None)
            or (reader.repo_root() if reader is not None else None)
        )
    )
    found: set[str] = set()
    unresolved: list[dict] = []
    for entry in files:
        entry_ids: set[str] = set()
        spelled: list[str] = []
        note: seed_ladder.SeedNote | None = None
        if reader is not None:
            read = [Seed(file_path=entry)]
            # Own try/except, as in get_task_context: a ladder that fails leaves the seed as given.
            try:
                if seed_ladder.needs_tolerance(read, reader):
                    ladder = seed_ladder.tolerate(read, reader, root_of)
                    read, spelled = ladder.exact + ladder.guessed, ladder.unresolved
                    note = next(
                        (
                            n
                            for n in ladder.notes
                            if n.outcome in ("ambiguous", "directory-too-large")
                        ),
                        None,
                    )
            except Exception:
                read, spelled, note = [Seed(file_path=entry)], [], None
            resolved = resolve_seeds(read, reader, store, entities_by_node=by_node)
            entry_ids = {e.entity_id for e in resolved.seed_entities}
            paths = {s.file_path for s in read if s.file_path} | set(spelled)
        else:
            paths = set(_normalized_seeds(store, [entry]))
        for path in paths:
            entry_ids.update(by_path.get(path, ()))
        if entry_ids:
            found |= entry_ids
            continue
        reason: str
        if note is not None:
            reason, shown, candidates = note.outcome, entry, list(note.read_as[:_CANDIDATES_LISTED])
        elif spelled:
            reason, shown, candidates = "not-in-graph", spelled[0], []
        elif reader is None:
            reason, shown, candidates = (
                "no-graph",
                next(iter(paths)) if len(paths) == 1 else entry,
                [],
            )
        else:
            continue
        unresolved.append(
            {
                "path": _clip(shown),
                "reason": reason,
                "candidates": [_clip(c) for c in candidates],
            }
        )
    return found, unresolved


def _newest_first(record: dict) -> tuple[datetime, str]:
    """Sort key, newest ``valid_from`` first when reversed, ``id`` descending as the tie-break.
    The stamp is parsed: ``...51Z`` and ``...51.5Z`` do not sort as text."""
    return datetime.fromisoformat(record["valid_from"]), record["id"]


def _take(
    count: int, budget: int, size_of: Callable[[int], int], envelope: Callable[[int], int]
) -> int:
    """How many of ``count`` items fit: ``size_of(i)`` is the serialized length of item ``i``
    and ``envelope(k)`` the length of the answer around ``k`` of them, empty list included. An
    item is never cut; the first one that does not fit ends the list."""
    used = 0
    taken = 0
    while taken < count:
        item = size_of(taken) + (1 if taken else 0)  # the comma before every item but the first
        if envelope(taken + 1) + used + item > budget:
            break
        used += item
        taken += 1
    return taken


def _clip(text: str) -> str:
    """``text`` cut, with a ``…``, so that it takes at most ``_ECHO_CHARS`` once JSON-escaped:
    a quote or a backslash costs two characters and a control character six."""
    if len(text) <= _ECHO_CHARS and len(_dump(text)) - 2 <= _ECHO_CHARS:
        return text
    used = kept = 0
    for char in text:
        width = len(_dump(char)) - 2
        if used + width > _ECHO_CHARS - 1:
            break
        used += width
        kept += 1
    return text[:kept] + "…"


def _listing_overview(
    everything: list[dict], include_superseded: bool, limit: int, budget: int
) -> str:
    """The answer to a call with no narrowing filter: what the store holds, and the newest few
    as compact rows. ``total`` and ``counts`` cover every record the proposal policy lets
    through, history included, whatever ``include_superseded`` says."""

    def tally(key: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        for record in everything:
            counts[record[key]] = counts.get(record[key], 0) + 1
        return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))

    admitted = [d for d in everything if include_superseded or d["status"] not in _HISTORY]
    admitted.sort(key=_newest_first, reverse=True)
    rows = [{key: d[key] for key in _ROW_KEYS} for d in admitted[:limit]]
    counts = {"status": tally("status"), "kind": tally("kind")}
    cut_hint = f"{_OVERVIEW_HINT} The newest list was cut by the character budget."

    def answer(shown: list[dict], hint: str) -> dict:
        return {
            "overview": True,
            "total": len(everything),
            "counts": counts,
            "newest": shown,
            "hint": hint,
        }

    # An envelope is measured as it will read if the list ends there: with the plain hint after
    # the last row, with the one that says the budget cut it otherwise.
    taken = _take(
        len(rows),
        budget,
        lambda i: len(_dump(rows[i])),
        lambda k: len(_dump(answer([], _OVERVIEW_HINT if k == len(rows) else cut_hint))),
    )
    return _dump(answer(rows[:taken], _OVERVIEW_HINT if taken == len(rows) else cut_hint))


def _listing_hint(
    *,
    matched: int,
    returned: int,
    cut: str | None,
    limit: int,
    budget: int,
    unresolved: int,
    unlisted: int,
    echo: str | None,
    has_query: bool,
    history_hint: bool,
) -> str | None:
    """Why a filtered answer is empty, cut, or short of a file: ``None`` only when something
    matched, nothing was left out and every file was read. ``cut`` is the bound that ended
    the list, ``"limit"`` or ``"budget"``; ``unresolved`` counts the files nothing could read
    and ``unlisted`` those ``unresolved_files`` leaves out. No file is named here: the list is
    the one place that says which and why. ``echo`` is the caller's filters, ``None`` when the
    answer had no room for them."""
    parts: list[str] = []
    if matched == 0:
        text = f"Nothing matched {echo or 'the filters'}."
        if has_query:
            text += " Every word of query must occur in a record: try one or two distinctive words."
        if history_hint:
            text += " Superseded and rejected records are searched only with status=[...] or"
            text += " include_superseded=True."
        parts.append(text)
    elif cut is not None and returned == 0:
        text = f"{matched} matched, but the first one does not fit in budget_chars={budget}."
        if budget < _LISTING_BUDGET_MAX:
            text += f" Raise budget_chars (max {_LISTING_BUDGET_MAX})."
        else:
            text += " It is larger than the biggest budget_chars allowed."
        parts.append(text)
    elif cut == "limit":
        raise_it = (
            f", or raise limit (max {_LISTING_LIMIT_MAX})" if limit < _LISTING_LIMIT_MAX else ""
        )
        parts.append(
            f"{matched - returned} more matched; limit={limit} cut the list. "
            f"Narrow with files=[...] or query=...{raise_it}."
        )
    elif cut == "budget":
        raise_it = (
            f", or raise budget_chars (max {_LISTING_BUDGET_MAX})"
            if budget < _LISTING_BUDGET_MAX
            else ""
        )
        parts.append(
            f"{matched - returned} more matched; the character budget cut the list. "
            f"Narrow with files=[...] or query=...{raise_it}."
        )
    if unresolved:
        text = "Some files could not be read: unresolved_files says which, why, and what to try."
        if unlisted:
            text += f" {unlisted} more are not listed."
        parts.append(text)
    return " ".join(parts) or None


def _decision_listing(
    store,
    reader,
    *,
    include_superseded: bool = False,
    status: str | list[str] | None = None,
    kind: str | list[str] | None = None,
    files: list[str] | None = None,
    query: str | None = None,
    ids: list[str] | None = None,
    limit: int = 25,
    budget_chars: int = 24000,
    borrowed_from: Path | None = None,
) -> str:
    """The text ``retrieve_decisions`` sends: an overview, or, with a narrowing filter, full
    records. The length of the text never exceeds the clamped ``budget_chars``.

    Built from ``_retrieve_decisions_impl(store, include_superseded=True)``, so the proposal
    policy lives in one place. The filters (``status``, ``kind``, ``files``, ``query``, ``ids``)
    combine with AND; an empty value is an absent one. ``reader`` is the code graph, ``None``
    without one; ``borrowed_from`` is the main checkout a linked worktree borrowed it from.
    Raises ``ValueError`` naming the valid values for an unknown ``status`` or ``kind``.
    see design/superpowers/specs/2026-10-04-bounded-decision-listing-design.md (D2-D5)
    """
    limit = max(1, min(_LISTING_LIMIT_MAX, limit))
    budget = max(_LISTING_BUDGET_MIN, min(_LISTING_BUDGET_MAX, budget_chars))
    statuses = _listing_values(status, DecisionStatus, "status")
    kinds = _listing_values(kind, DecisionKind, "kind")
    wanted_files = _listing_texts(files)
    wanted_ids = _listing_texts(ids)
    terms = (query or "").lower().split()
    everything = _retrieve_decisions_impl(store, include_superseded=True)
    if not (statuses or kinds or wanted_files or wanted_ids or terms):
        return _listing_overview(everything, include_superseded, limit, budget)

    pool = everything
    if wanted_ids:
        id_set = set(wanted_ids)
        pool = [d for d in pool if d["id"] in id_set]
    # `status` replaces the default exclusion, and so does asking for a record by id.
    history_hidden = not (statuses or wanted_ids or include_superseded)
    if statuses:
        pool = [d for d in pool if d["status"] in statuses]
    elif history_hidden:
        pool = [d for d in pool if d["status"] not in _HISTORY]
    if kinds:
        pool = [d for d in pool if d["kind"] in kinds]
    unresolved: list[dict] = []
    if wanted_files:
        entity_ids, unresolved = _listing_entities(store, reader, wanted_files, borrowed_from)
        # Any binding counts, an orphaned one too: the question is where the record was
        # anchored, not whether it is still delivered there.
        anchored = {b.record_id for e in entity_ids for b in store.bindings_for_entity(e)}
        pool = [d for d in pool if d["id"] in anchored]
    if terms:
        pool = [
            d
            for d in pool
            if all(
                term in "\n".join(str(d.get(f) or "") for f in _SEARCHED_FIELDS).lower()
                for term in terms
            )
        ]

    # Mistakes first, then newest first; the sort is stable, so the rank keeps the recency order.
    pool.sort(key=_newest_first, reverse=True)
    pool.sort(key=lambda d: _MISTAKE_RANK.get(d["kind"], 2))
    matched = len(pool)
    echo = ", ".join(
        f"{name}={_clip(value)}"
        for name, value in (
            ("status", ",".join(sorted(statuses)) if statuses else ""),
            ("kind", ",".join(sorted(kinds)) if kinds else ""),
            ("files", f"[{', '.join(wanted_files)}]" if wanted_files else ""),
            ("query", f'"{" ".join(terms)}"' if terms else ""),
            ("ids", f"[{', '.join(wanted_ids)}]" if wanted_ids else ""),
        )
        if value
    )
    # Unreadable files are the whole story of an empty answer only when no file could be read.
    history_hint = history_hidden and not (wanted_files and len(unresolved) == len(wanted_files))
    sizes: dict[int, int] = {}

    def size_of(i: int) -> int:
        if i not in sizes:
            sizes[i] = len(_dump(pool[i]))
        return sizes[i]

    def ended_by(k: int) -> str | None:
        """The bound that ended a list of ``k`` records, as it will read if it ends there."""
        if k >= matched:
            return None
        return "limit" if k >= limit else "budget"

    def render(*, echoed: bool, listed: int) -> str:
        """The answer with ``listed`` unresolved entries and, or not, the echo of the filters."""
        shown = unresolved[:listed]

        def answer(k: int, decisions: list[dict]) -> dict:
            out: dict = {
                "overview": False,
                "matched": matched,
                "returned": k,
                "omitted": matched - k,
                "decisions": decisions,
            }
            if wanted_files:
                out["unresolved_files"] = shown
                out["unresolved_omitted"] = len(unresolved) - len(shown)
            out["hint"] = _listing_hint(
                matched=matched,
                returned=k,
                cut=ended_by(k),
                limit=limit,
                budget=budget,
                unresolved=len(unresolved),
                unlisted=len(unresolved) - len(shown),
                echo=echo if echoed else None,
                has_query=bool(terms),
                history_hint=history_hint,
            )
            return out

        taken = _take(min(matched, limit), budget, size_of, lambda k: len(_dump(answer(k, []))))
        return _dump(answer(taken, pool[:taken]))

    # The ceiling is the contract: when the caller's own text leaves no room, drop the echo of
    # the filters first, then the unresolved entries, last one first.
    listed = min(len(unresolved), _UNRESOLVED_LISTED)
    text = render(echoed=True, listed=listed)
    if len(text) > budget:
        text = render(echoed=False, listed=listed)
    while len(text) > budget and listed:
        listed -= 1
        text = render(echoed=False, listed=listed)
    return text


@mcp.tool(annotations=_LOCAL_WRITE)
def retrieve_decisions(
    include_superseded: bool = False,
    status: str | list[str] | None = None,
    kind: str | list[str] | None = None,
    files: list[str] | None = None,
    query: str | None = None,
    ids: list[str] | None = None,
    limit: int = 25,
    budget_chars: int = 24000,
) -> ToolResult:
    """List decisions from the store, bounded. The answer is one JSON object in one text block.

    With no narrowing filter you get an overview: the total, counts by status and kind, and the
    newest records as compact rows. Pass a filter to get full records, gotchas and lessons
    first, then newest first, cut at ``limit`` (default 25, max 100) or ``budget_chars``
    (default 24000, max 60000), whichever comes first; ``hint`` says what cut the list or why
    it is empty.

    Filters combine with AND, and an empty one is ignored. ``status`` and ``kind`` take one
    value or a list (status: proposed, accepted, superseded, rejected, deprecated; kind: adr,
    lesson, constraint, gotcha) and ``status`` replaces the default, which hides superseded
    and rejected records. ``files`` are repo-relative paths, read the way ``get_task_context``
    reads them (a basename or a directory works); records anchored to them match, and
    ``unresolved_files`` says, for each path nothing could read, why and what to try instead
    (``reason``, ``candidates``). ``query`` words must all occur
    in a record's text. ``ids`` returns those records, superseded ones too. History of one
    topic: ``query="<words>", include_superseded=True``.
    """
    # A `ToolResult` return is what keeps FastMCP from declaring an output schema and sending the
    # JSON a second time as `structuredContent` (design D1; the smoke test reads the wire).
    # The code graph is read only when a path has to be matched against it.
    reader, borrowed_from = _synced_reader() if _listing_texts(files) else (None, None)
    text = _decision_listing(
        _get_store(),
        reader,
        include_superseded=include_superseded,
        status=status,
        kind=kind,
        files=files,
        query=query,
        ids=ids,
        limit=limit,
        budget_chars=budget_chars,
        borrowed_from=borrowed_from,
    )
    return ToolResult(content=text)


def _list_facts_impl(store, include_superseded: bool = False) -> list[dict]:
    """Testable core for list_facts (Gap 1, design/superpowers/specs/
    2026-07-10-ratification-ux-and-mcp-gaps-design.md) -- mirrors
    ``_retrieve_decisions_impl``'s default filtering (excludes SUPERSEDED/REJECTED) and
    full model-dump contract. Sort differs: facts carry no ``kind``, so there is no
    mistakes-first ranking analogue -- sorted purely newest-first (``valid_from`` desc,
    ``id`` desc as a deterministic tiebreak)."""
    facts = list(store.iter_facts())
    if not include_superseded:
        facts = [
            f for f in facts if f.status not in (DecisionStatus.SUPERSEDED, DecisionStatus.REJECTED)
        ]
    # Same policy application as `_retrieve_decisions_impl` — see its comment.
    facts = [f for f in facts if f.status != DecisionStatus.PROPOSED or proposal_surfaces(f)]
    facts.sort(key=lambda f: (f.valid_from, f.id), reverse=True)
    return [f.model_dump(mode="json") for f in facts]


@mcp.tool(annotations=_READ_ONLY)
def list_facts(include_superseded: bool = False) -> list[dict]:
    """Return facts from the store, newest first (the ``retrieve_decisions`` counterpart
    for the facts layer -- Gap 1, previously only reachable via ``get_entity_history``,
    which itself silently dropped facts until this same wave closed Gap 2).

    The default listing excludes superseded and rejected (dropped) records; pass
    ``include_superseded=True`` to see that history too. Facts have no ``kind`` (no
    mistakes-first ranking, unlike ``retrieve_decisions``'s gotchas/lessons-first order)
    -- sorted by ``valid_from`` descending, ``id`` descending as a deterministic tiebreak.
    Full ``Fact`` model dumps.
    """
    return _list_facts_impl(_get_store(), include_superseded=include_superseded)


def _find_entity_impl(store, name: str, file_path: str | None = None) -> dict:
    """Testable core: exact descriptor match first, then a name-only fallback scan.

    Never guesses: a name reused across files with no ``file_path`` to disambiguate comes
    back as ``candidates`` rather than an arbitrary pick.
    """
    entity = store.find_entity(name, file_path)
    if entity is None:
        candidates = store.find_entities_by_name(name)
        if len(candidates) == 1:
            entity = candidates[0]
        elif len(candidates) > 1:
            return {
                "found": False,
                "candidates": [
                    {
                        "entity_id": c.entity_id,
                        "canonical_name": c.canonical_name,
                        "file_path": c.descriptor.file_path if c.descriptor else None,
                    }
                    for c in candidates
                ],
            }
    if entity is None:
        return {"found": False}

    bindings = store.bindings_for_entity(entity.entity_id)
    types = store.record_types(b.record_id for b in bindings)
    return {
        "found": True,
        "entity_id": entity.entity_id,
        "canonical_name": entity.canonical_name,
        "descriptor": entity.descriptor.model_dump() if entity.descriptor else None,
        "last_seen_node_id": entity.last_seen_node_id,
        "bindings": [
            {
                "record_id": b.record_id,
                "record_type": types.get(b.record_id, "unknown"),
                "tier": b.tier,
                "status": b.status,
            }
            for b in bindings
        ],
    }


@mcp.tool(annotations=_READ_ONLY)
def find_entity(name: str, file_path: str | None = None) -> dict:
    """Look up an entity_id by name (+ optional file_path) — the missing link that lets an
    agent chain ``add_decision``/``propose_decisions`` output into ``get_entity_history``
    without reading the store directly.

    Tries an exact descriptor match (canonicalized name + file_path) first; if that misses,
    falls back to a name-only scan across all entities. A single name-only match is
    returned as found; multiple matches are ambiguous and returned as ``candidates``
    (never guessed at) — pass ``file_path`` to disambiguate.

    Returns ``{"found": True, "entity_id", "canonical_name", "descriptor", ...
    "last_seen_node_id", "bindings": [{"record_id", "record_type", "tier", "status"}, ...]}``
    (``record_type`` is ``"decision"`` or ``"fact"``, or ``"unknown"`` for a binding whose record
    exists in neither table) when resolved to exactly one entity;
    ``{"found": False}`` when nothing matches; or
    ``{"found": False, "candidates": [{"entity_id", "canonical_name", "file_path"}, ...]}``
    when the name alone is ambiguous.
    """
    return _find_entity_impl(_get_store(), name, file_path)


def _get_entity_history_impl(store: Store, entity_id: str) -> list[dict]:
    """Testable core for get_entity_history (Gap 2, design/superpowers/specs/
    2026-07-10-ratification-ux-and-mcp-gaps-design.md): every decision AND fact anchored
    to ``entity_id``, newest first.

    The record types of all the entity's bindings come from one ``store.record_types`` read;
    each record is then loaded with the getter its type names. A binding whose record exists
    in neither table (an unknown record kind) stays skipped, same as before this wave.
    Previously this only ever tried ``get_decision`` -- a fact-only binding vanished from
    history with no trace. Every returned dict gains ``"record_type": "decision" | "fact"``
    (additive -- existing consumers keyed on the pre-existing fields are unaffected); the
    merged list stays sorted ``valid_from`` desc, exactly as before.
    """
    bindings = store.bindings_for_entity(entity_id)
    types = store.record_types(b.record_id for b in bindings)
    records: list[tuple[str, Decision | Fact]] = []
    for b in bindings:
        kind = types.get(b.record_id)
        if kind == "decision":
            decision = store.get_decision(b.record_id)
            if decision is not None:
                records.append(("decision", decision))
        elif kind == "fact":
            fact = store.get_fact(b.record_id)
            if fact is not None:
                records.append(("fact", fact))
    records.sort(key=lambda pair: pair[1].valid_from, reverse=True)
    out = []
    for record_type, record in records:
        dump = record.model_dump(mode="json")
        dump["record_type"] = record_type
        out.append(dump)
    return out


@mcp.tool(annotations=_READ_ONLY)
def get_entity_history(entity_id: str) -> list[dict]:
    """Return every decision AND fact anchored to a given entity, newest first.

    Resolves every binding's record type in one read, then loads each record (a binding whose
    record exists nowhere is skipped, as before). Every dict now carries
    ``"record_type": "decision" | "fact"`` so a caller can tell them apart without
    re-deriving it -- facts used to be silently
    dropped here (this tool only ever called ``get_decision``; see ``list_facts`` for the
    facts-only counterpart of ``retrieve_decisions``).
    """
    return _get_entity_history_impl(_get_store(), entity_id)


def _seeds_from_args(files: list[str] | None, entities: list[dict] | None) -> list[Seed]:
    """Shared seed-building for get_task_context/query_structure/query_decisions (§5 FR8.2:
    the thin tools reuse this instead of re-deriving seeds from files/entities each time)."""
    seeds: list[Seed] = [Seed(file_path=f) for f in (files or [])]
    seeds += [Seed(name=e.get("name"), file_path=e.get("file_path")) for e in (entities or [])]
    return seeds


# `_not_in_graph_block`: at most this many seed paths are named per sentence.
_NOT_IN_GRAPH_LISTED = 10


def _listed(paths: list[str]) -> str:
    """``a, b`` up to :data:`_NOT_IN_GRAPH_LISTED` paths, then ``…, and N more``."""
    shown = ", ".join(paths[:_NOT_IN_GRAPH_LISTED])
    extra = len(paths) - _NOT_IN_GRAPH_LISTED
    return f"{shown}, and {extra} more" if extra > 0 else shown


def _is_repo_relative(p: str) -> bool:
    """Whether ``p`` is written the way the graph writes ``source_file``: relative, already
    normalized, and inside the repository. ``PurePosixPath("/abs/x")`` and
    ``posixpath.normpath("./x") != "./x"`` are what ``root / p`` would silently accept, and
    ``normpath`` leaves a leading ``..`` alone, so ``../x`` needs its own check."""
    pure = PurePosixPath(p)
    return not pure.is_absolute() and ".." not in pure.parts and posixpath.normpath(p) == p


def _not_in_graph_block(
    reader,
    files: list[str] | None,
    entities: list[dict] | None,
    borrowed_from: Path | None = None,
    worktree_root: Path | None = None,
    *,
    ladder: seed_ladder.Ladder | None = None,
    root_of: Callable[[], Path | None] | None = None,
) -> str:
    """The "## Not in the code graph" block: what happened to each seed path the graph does
    not hold, or ``""`` when the graph holds every one (then neither ``freshness()`` nor
    ``repo_root()`` is called: no git cost on the common path).

    The cause differs by seed. A repo-relative path that exists as a file but is not in the
    graph gets the graph-state advice (rebuild a stale graph; a current one may simply not
    cover that file); a directory, a path that does not exist, any path when graph.json is
    outside a git repository, and a path that is not written as a normalized repo-relative
    one (absolute, ``./x``, ``a/../b``, a trailing slash: ``root / p`` would accept them, but
    the graph never holds them) gets "check the path". ``freshness()`` runs only when some
    seed is an existing file.

    ``borrowed_from`` is the main checkout whose graph a linked worktree reads, and
    ``worktree_root`` that worktree: "exists" is then checked in the worktree, and a file there
    that the main checkout lacks gets the borrowed sentence instead of a verdict on the graph
    (the graph was never built from it, so its freshness says nothing).

    ``ladder`` is the seed ladder's verdict (:func:`seed_ladder.tolerate`): the paths named are
    then its ``unresolved`` ones, in their normalised spelling, so ``./pkg/n.py`` gets the advice
    for ``pkg/n.py``; a seed the ladder read, or reported in its own block, is not named. The
    ``N`` of "N of M" stays the number of distinct paths as given. ``root_of`` is the one lookup
    of the repository root the call shares with the ladder.
    # see design/superpowers/specs/2026-10-01-stale-graph-visible-design.md (D5)
    # see design/superpowers/specs/2026-10-01-worktree-borrowed-graph-design.md (D3)
    # see design/superpowers/specs/2026-10-02-tolerant-seeds-design.md (D5)
    """
    paths = [*(files or []), *(e.get("file_path") for e in (entities or []))]
    seeds = list(dict.fromkeys(p for p in paths if isinstance(p, str) and p.strip()))
    if ladder is not None:
        missing = list(ladder.unresolved)
    else:
        held = reader.source_files()
        missing = [p for p in seeds if p not in held]
    if not missing:
        return ""
    borrowed = borrowed_from is not None and worktree_root is not None
    candidates = [p for p in missing if _is_repo_relative(p)]
    lookup = root_of if root_of is not None else reader.repo_root
    root = worktree_root if borrowed else (lookup() if candidates else None)
    present = [p for p in candidates if root is not None and seed_ladder.is_file_exact(root, p)]
    only_here = (
        [
            p
            for p in present
            if borrowed_from is not None and not seed_ladder.is_file_exact(borrowed_from, p)
        ]
        if borrowed
        else []
    )
    existing = [p for p in present if p not in set(only_here)]
    other = [p for p in missing if p not in set(present)]
    noun = "seed path" if len(seeds) == 1 else "seed paths"
    into = f" in {borrowed_from}" if borrowed else ""
    lines = ["## Not in the code graph"]
    if existing:
        n = len(existing)
        verb = "exists but is" if n == 1 else "exist but are"
        sentence = f"{n} of {len(seeds)} {noun} {verb} not in the code graph: {_listed(existing)}. "
        state = reader.freshness(root=None if borrowed else root)
        if state.state == "stale":
            sentence += (
                f"The graph is stale ({staleness_phrase(state)}): rebuild it"
                f"{into or ' from the repository root'} with `graphify update .`, then call again."
            )
        elif state.state == "fresh":
            sentence += (
                "No committed change since the build explains it: the file may be newer than "
                f"the build and not committed yet (rebuild{' it' + into if borrowed else ''} "
                "with `graphify update .`), sit under an excluded path, or be a file type "
                "Graphify skips."
            )
        else:
            sentence += (
                f"Could not tell whether the graph is current ({state.reason}): rebuild it"
                f"{into} with `graphify update .` if these files are new."
            )
        lines.append(sentence)
    if only_here:
        n = len(only_here)
        verb = "exists but is" if n == 1 else "exist but are"
        lines.append(
            f"{n} of {len(seeds)} {noun} {verb} not in the code graph: {_listed(only_here)}. "
            f"{_BORROWED_SENTENCE}"
        )
    if other:
        n = len(other)
        verb = (
            "is not a repo-relative path to a file"
            if n == 1
            else "are not repo-relative paths to files"
        )
        lines.append(
            f"{n} of {len(seeds)} {noun} {verb} in this repository: {_listed(other)}. "
            "Check the path: it must be repo-relative, from the repository root, written "
            "without `./`, `..` or a trailing slash, and name a file, not a directory."
        )
    return "\n".join(lines)


# What a linked worktree is told about a file that exists only on its branch: the main
# checkout's graph was built without it.
_BORROWED_SENTENCE = (
    "This worktree reads the main checkout's graph, which does not hold files that exist "
    "only on this branch."
)


def _no_graph_block(store) -> str:
    """The "## No code graph" block: no reader could be opened, and the call named a seed.

    Names the graph looked at: the store's own (:func:`_graph_path`), or, for a linked
    worktree with none of its own, the main checkout's (:func:`borrowed_graph_candidate`)
    with the advice to build it there. A path that is there but could not be read (permissions,
    a corrupt file) is "not readable", never "missing".
    # see design/superpowers/specs/2026-10-01-worktree-borrowed-graph-design.md (D4)
    """
    candidate = borrowed_graph_candidate(store.path)
    main = main_checkout_root(store.path) if candidate is not None else None
    graph = candidate if candidate is not None and main is not None else None
    path = graph if graph is not None else Path(_graph_path(store))
    if path_state(path) == "missing":
        lead = f"No code graph at {path}"
        build = (
            f"Build it in the main checkout {main} with `graphify update .`."
            if graph is not None
            else "Build it from the repository root with `graphify update .`."
        )
    else:
        lead = f"The code graph at {path} is not readable"
        build = (
            f"Make it readable, or rebuild it in the main checkout {main} with `graphify update .`."
            if graph is not None
            else "Make it readable, or rebuild it from the repository root with "
            "`graphify update .`."
        )
    return f"## No code graph\n{lead}: memory anchored to code cannot be looked up. {build}"


# `TaskContext.render()` when nothing was found. The "Why this is empty" block is for exactly
# this text; `test_retrieval_seeds.py` pins the literal on the render side.
_NO_CONTEXT = "No context found."

# "Why this is empty": at most this many accepted domains are named.
_EMPTY_DOMAINS_LISTED = 12
_EMPTY_HEADING = "## Why this is empty"


def _once(fn: Callable[[], Path | None]) -> Callable[[], Path | None]:
    """``fn`` called at most once, on first use; later calls return its first answer."""
    box: list[Path | None] = []

    def call() -> Path | None:
        if not box:
            box.append(fn())
        return box[0]

    return call


def _empty_block(store, *, graph_missing: bool = False) -> str:
    """The "## Why this is empty" block: a call that gave no files and no entities, and so had
    nothing to match. Names the accepted domains (name and slug) as places to start. With no
    code graph to match against it also says to build one first.
    # see design/superpowers/specs/2026-10-02-tolerant-seeds-design.md (D6)
    """
    domains = sorted(
        store.iter_domains(DomainStatus.ACCEPTED), key=lambda d: (d.title.lower(), d.slug)
    )
    text = "No files or entities were given, so nothing could be matched."
    if graph_missing:
        text += (
            " There is no code graph to match against either: build it from the repository "
            "root with `graphify update .` first."
        )
    text += " Pass files=[…] with the repo-relative paths you are working on"
    if domains:
        named = ", ".join(f"{d.title} ({d.slug})" for d in domains[:_EMPTY_DOMAINS_LISTED])
        extra = len(domains) - _EMPTY_DOMAINS_LISTED
        named += f", and {extra} more" if extra > 0 else ""
        text += f", or drill_down(<slug>) for a named area: {named}"
    return f"{_EMPTY_HEADING}\n{text}."


def _get_task_context_impl(
    store,
    reader,
    files: list[str] | None,
    entities: list[dict] | None,
    structure_budget: int,
    memory_budget: int,
    intent: str | None = None,
    borrowed_from: Path | None = None,
) -> str:
    """Testable core: build seeds, run retrieval, return the rendered slice.

    A seed the graph does not hold is first read through the seed ladder
    (:func:`seed_ladder.tolerate`): a path with a stray ``./``, a wrong prefix or a bare name, a
    directory, a ``Type.member`` with no file. Each rewrite is a guess and is said so in a
    "How your seeds were read" block after the rendered text; an ambiguous seed is reported and
    never read. What the ladder cannot read gets a trailing "Not in the code graph" block saying
    why (stale graph, a file the engine skips, a bad path), instead of a bare "No context
    found."; with no reader and a seed, a trailing "No code graph" block says the graph is
    missing; with no seed at all, a "Why this is empty" block names where to start.
    ``TaskContext.render()`` itself is untouched. ``borrowed_from`` is the main checkout whose
    graph ``reader`` was opened on, for a linked worktree with none of its own
    (:func:`_synced_reader`); it changes only the wording of the not-in-graph block and the
    root the ladder reads paths against.
    see design/superpowers/specs/2026-10-01-worktree-borrowed-graph-design.md (D3, D4)
    see design/superpowers/specs/2026-10-02-tolerant-seeds-design.md (D1, D4-D7)
    """
    # A seed with neither a name nor a path (`files=[""]`, `entities=[{}]`, blanks) names
    # nothing: the call is one with no seeds.
    seeds = [
        s
        for s in _seeds_from_args(files, entities)
        if (s.name or "").strip() or (s.file_path or "").strip()
    ]
    worktree_of = _once(lambda: repository_root(store.path) if borrowed_from is not None else None)

    def root_of() -> Path | None:
        # Where "does this file exist?" is asked: the worktree for a borrowed graph, else the
        # git root of the graph. One lookup per call, shared by the ladder and the block.
        return worktree_of() or reader.repo_root()

    root_once = _once(root_of)
    ladder: seed_ladder.Ladder | None = None
    exact: list[Seed] = seeds
    guessed: list[Seed] = []
    if reader is not None and seeds:
        # Own try/except: a ladder that fails must leave the seeds exactly as they were.
        try:
            if seed_ladder.needs_tolerance(seeds, reader):
                ladder = seed_ladder.tolerate(seeds, reader, root_once)
                exact, guessed = ladder.exact, ladder.guessed
        except Exception:
            ladder, exact, guessed = None, seeds, []
    budget = RetrievalBudget(structure_budget, memory_budget)
    ctx = _retrieve(exact, store, reader, budget, guessed=guessed)
    text = ctx.render()
    # The nearest anchored files (spec D10-D12): a sentence per file seed that has no record of
    # its own, and, when the main answer has no memory at all, the records of those files. Each
    # step has its own try/except: advice must never cost the answer, nor each other.
    near = nearest_anchored.Plan()
    with contextlib.suppress(Exception):
        if reader is not None and seeds:
            near = nearest_anchored.plan(seeds, ladder, store, reader, shown_ids=ctx.shown_ids)
    near_ctx: TaskContext | None = None
    recorded = ctx
    with contextlib.suppress(Exception):
        if near.files and not nearest_anchored.has_memory(ctx):
            second = _retrieve([Seed(file_path=f) for f in near.files], store, reader, budget)
            merged = nearest_anchored.combine(ctx, second)
            near_ctx, recorded = second, merged
    _record(
        store,
        recorded.shown_ids,
        ladder.seed_paths if ladder is not None else [s.file_path for s in seeds if s.file_path],
        ctx=recorded,
        intent=_caller_intent(intent),
    )
    # Each block has its own try/except: advice about the seeds must never cost the answer it
    # follows, nor each other.
    blocks: list[str] = []
    with contextlib.suppress(Exception):
        if ladder is not None:
            blocks.append(seed_ladder.read_block(ladder.notes))
    with contextlib.suppress(Exception):
        if reader is not None:
            worktree = worktree_of()
            blocks.append(
                _not_in_graph_block(
                    reader,
                    files,
                    entities,
                    borrowed_from,
                    worktree,
                    ladder=ladder,
                    root_of=root_once,
                )
            )
        elif seeds:
            blocks.append(_no_graph_block(store))
    with contextlib.suppress(Exception):
        if not seeds and text == _NO_CONTEXT:
            blocks.append(_empty_block(store, graph_missing=reader is None))
    with contextlib.suppress(Exception):
        if near.sentences:
            heading = _EMPTY_HEADING if text == _NO_CONTEXT else nearest_anchored.NEAREST_HEADING
            blocks.append("\n".join([heading, *near.lines()]))
    ids_shown = False
    with contextlib.suppress(Exception):
        if near_ctx is not None:
            records, ids_shown = nearest_anchored.records_block(
                near_ctx, guarded=MEMORY_GUARD_LINE in text
            )
            blocks.append(records)
    out = "\n\n".join([text, *(b for b in blocks if b)])
    # The records carried ids, so the reply ends with the line that says what to do about one
    # that is wrong: once, at the very end, as the render puts it.
    return f"{out}\n\n{_STANDING_SUPERSEDE_HINT}" if ids_shown else out


def _synced_reader() -> tuple[GraphifyReader | None, Path | None]:
    """Best-effort reader with a lazy sync attempt — shared by every retrieval-facing tool
    (get_task_context/query_structure/query_decisions/drill_down). Sync failure degrades
    to un-synced retrieval, never an error.

    Returns ``(reader, borrowed_from)``. A linked worktree has the tracked store and no graph:
    when the store's own graph is missing, the reader is opened on the main checkout's and
    ``borrowed_from`` is that checkout's root. The borrowed graph is synced index-only
    (``canonical_writes=False``): the worktree's index starts cold, and without the derived
    state (domain communities, Tier-1 community bindings, ``last_seen_*``) retrieval loses its
    domain members and its Related bucket. That sync never rewrites a tracked file, and its moved
    rung abstains. Otherwise ``borrowed_from`` is ``None``.
    see design/superpowers/specs/2026-10-01-worktree-borrowed-graph-design.md (D2)"""
    reader = _load_reader()
    if reader is None:
        borrowed = open_borrowed_reader(_get_store().path)
        if borrowed is None:
            return None, None
        with contextlib.suppress(Exception):
            maybe_sync(_get_store(), borrowed[0], canonical_writes=False)
        return borrowed
    with contextlib.suppress(Exception):
        maybe_sync(_get_store(), reader)
    return reader, None


def _get_task_context_with_sync(
    files: list[str] | None = None,
    entities: list[dict] | None = None,
    structure_budget: int = 4000,
    memory_budget: int = 6000,
    intent: str | None = None,
) -> str:
    """Tool-shell core: lazy sync (best-effort), then retrieval."""
    reader, borrowed_from = _synced_reader()
    return _get_task_context_impl(
        _get_store(),
        reader,
        files,
        entities,
        structure_budget,
        memory_budget,
        intent,
        borrowed_from=borrowed_from,
    )


def _query_structure_impl(
    store,
    reader,
    files: list[str] | None,
    entities: list[dict] | None,
    budget_chars: int,
) -> str:
    """Testable core for the query_structure thin tool (§5 FR8.2).

    Records nothing at all: the never-surfaced denominator counts opportunities for a
    decision to surface, and this tool returns no decision memory, so it never offers one.
    Counting its seeds would inflate that denominator with non-opportunities — an area
    explored only structurally could then get flagged "never surfaced" when no decision
    could possibly have fired there (fix-wave review, spec correction over the original
    design's "record seeds to prove an area was visited").
    """
    return _query_structure(_seeds_from_args(files, entities), store, reader, budget_chars)


def _query_decisions_impl(
    store,
    reader,
    files: list[str] | None,
    entities: list[dict] | None,
    budget_chars: int,
    intent: str | None = None,
) -> str:
    """Testable core for the query_decisions thin tool (§5 FR8.2).

    Reuses ``retrieval.get_task_context`` (aliased ``_retrieve``) rather than
    ``retrieval.query_decisions`` (a render-only wrapper that discards its ``TaskContext``)
    — same ``resolve_seeds`` -> ``_gather_structure`` -> ``rank_decisions`` pipeline,
    equivalent budget (``memory_chars=budget_chars``, default ``structure_chars`` since this
    tool takes none), just with the ``ctx`` kept around long enough to read
    ``ctx.shown_ids`` for telemetry before rendering with ``include_structure=False``.
    """
    seeds = _seeds_from_args(files, entities)
    ctx = _retrieve(seeds, store, reader, RetrievalBudget(memory_chars=budget_chars))
    _record(
        store,
        ctx.shown_ids,
        [s.file_path for s in seeds if s.file_path],
        ctx=ctx,
        intent=_caller_intent(intent),
    )
    return ctx.render(include_structure=False)


@mcp.tool(annotations=_LOCAL_WRITE)
def get_task_context(
    files: list[str] | None = None,
    entities: list[dict] | None = None,
    structure_budget: int = 4000,
    memory_budget: int = 6000,
    intent: str | None = None,
) -> str:
    """Task-aware context for the files/entities you're working on, mistakes ranked first.

    ``files`` are repo-relative paths; ``entities`` are ``{"name": ..., "file_path": ...}``
    refs. Returns a compact slice: known mistakes/gotchas, then decisions, then a structural
    map, then related decisions. Best-effort — degrades if the graph or store is absent.

    Also accepted: ``paths`` (the same as ``files``) and ``seeds``, a list or one string. A seed
    with a ``/``, or ending in a known file extension (``.py``, ``.toml``), is read as a file;
    every other seed (a symbol such as ``Store.close``, an issue id) as an entity ``name``.
    There is no free-text query argument.

    ``intent``: optional label for what asked (e.g. a skill name). Recorded for local
    statistics only; never affects what is returned.
    """
    return _get_task_context_with_sync(files, entities, structure_budget, memory_budget, intent)


@mcp.tool(annotations=_LOCAL_WRITE)
def query_structure(
    files: list[str] | None = None,
    entities: list[dict] | None = None,
    budget_chars: int = 4000,
) -> str:
    """The structural-map half of ``get_task_context`` alone (§5 FR8.2 thin tool) — a cheap
    follow-up once you already have decision memory and just need the code map.

    Same ``files``/``entities`` shape as ``get_task_context``, and the same ``paths`` and
    ``seeds`` aliases. Never crashes: with no Graphify graph present, returns an explanatory
    note instead of a map.
    """
    reader, _ = _synced_reader()
    return _query_structure_impl(_get_store(), reader, files, entities, budget_chars)


@mcp.tool(annotations=_LOCAL_WRITE)
def query_decisions(
    files: list[str] | None = None,
    entities: list[dict] | None = None,
    budget_chars: int = 6000,
    intent: str | None = None,
) -> str:
    """The decision-memory half of ``get_task_context`` alone (§5 FR8.2 thin tool):
    mistakes, decisions, related — no structural map.

    Same ``files``/``entities`` shape as ``get_task_context``, and the same ``paths`` and
    ``seeds`` aliases. Best-effort like every other tool here: degrades gracefully with no
    graph present (global-scope decisions still surface). This tool takes no
    ``structure_budget``, but internally the "related" (peripheral) bucket is still gathered
    by walking the structural subgraph with ``RetrievalBudget``'s DEFAULT ``structure_chars``
    (the map itself is discarded — only the peripheral entities it surfaces feed decision
    ranking).

    ``intent``: optional label for what asked (e.g. a skill name). Recorded for local
    statistics only; never affects what is returned.
    """
    reader, _ = _synced_reader()
    return _query_decisions_impl(_get_store(), reader, files, entities, budget_chars, intent)


def _auto_accept() -> bool:
    """True iff ``SIDEGRAPH_AUTO_ACCEPT=on`` (point-of-use env read — never cached at
    import, and never read inside ``capture.py``, which stays pure and takes the resolved
    bool as a keyword instead). Any value other than the literal ``"on"`` (including unset)
    is off. When on, agent-proposed decisions and facts (``propose_decisions``) land
    ``status=accepted`` directly instead of ``proposed``, bypassing the human ratification
    queue — provenance still stamps ``source="agent"``, so history never lies about
    authorship, only about whether a human reviewed it. Domains are always exempt
    (``propose_domains``/``_add_domain_impl`` never consult this). Opt-in, off by default:
    it removes the store's only noise filter, so it's recommended for solo use, not team
    stores (see design/superpowers/specs/2026-07-10-ratification-ux-and-mcp-gaps-design.md).
    """
    return os.environ.get("SIDEGRAPH_AUTO_ACCEPT") == "on"


def _ratify_policy() -> RatifyPolicy:
    """Point-of-use resolver for ``SIDEGRAPH_RATIFY_POLICY`` (design D1) — mirrors
    ``_auto_accept``'s shape: a fresh env read at the point of use (never cached at
    import), never performed inside ``capture.py`` (which stays pure and takes the
    resolved ``RatifyPolicy`` as a keyword instead). Unknown/empty/unset values fail safe
    to ``RatifyPolicy.MANUAL`` via the pure ``capture.parse_ratify_policy`` this function
    wraps with the actual env read.

    Called exactly ONCE per MCP request — inside ``propose_decisions`` and
    ``propose_domains`` — and the returned object is threaded through unchanged to every
    core call the request makes (``propose_decisions`` passes the SAME object to both
    ``capture.propose`` and ``capture.propose_facts`` via ``_propose_decisions_impl``), so
    a single batch samples the policy once, never once per core call.
    # see design/superpowers/specs/2026-09-11-auto-ratification-policy-design.md D1
    """
    return parse_ratify_policy(os.environ.get("SIDEGRAPH_RATIFY_POLICY"))


def _telemetry_enabled() -> bool:
    """Opt-out, one definition shared with the PreToolUse hook (see config)."""
    from .config import telemetry_enabled

    return telemetry_enabled()


# D2: this process has no idea what the host calls the current session; SessionStart wrote
# it into `meta` before any tool ran.
_SESSION_KEY_TTL = timedelta(hours=12)


def _session_key(store: Store) -> str | None:
    """The current host session id, or None when there is no trustworthy one.

    Absent, unparsable, or older than the TTL all mean the same thing: record nothing.
    `meta` never expires on its own, so without the TTL check a key left behind by the last
    session would silently attribute every later CLI or pytest retrieval to it — including
    handing seed events to a dead session that had only touches.
    """
    raw = store.get_meta(TELEMETRY_SESSION_KEY)
    if not raw:
        return None
    session_id, separator, stamp = raw.partition("|")
    if not session_id or not separator:
        return None
    try:
        written = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if written.tzinfo is None:
        return None
    # A stamp ahead of the clock (negative age) is not a live session either.
    if not timedelta(0) <= datetime.now(UTC) - written <= _SESSION_KEY_TTL:
        return None
    return session_id


def _anchor_paths(store: Store, record_id: str) -> list[str]:
    """File paths a record is anchored to, resolved through the INDEX.

    Doctor resolves the same relation by walking canonical JSON (`doctor.py:482-485`), which
    is right for a one-shot check and wrong here, where this runs on every retrieval.
    **All bindings count regardless of status**: doctor's canonical read has no status to
    filter on, so dropping degraded/orphaned ones here would make the two resolutions
    disagree about what a record is anchored to.
    """
    paths: list[str] = []
    for binding in store.bindings_for_record(record_id):
        entity = store.get_entity(binding.entity_id)
        file_path = entity.descriptor.file_path if entity and entity.descriptor else None
        if file_path:
            paths.append(file_path)
    return paths


def _normalized_seeds(store: Store, seeds: list[str]) -> list[str]:
    """Seed keys must join anchors, so they get the same realpath+relpath treatment touches
    get — seeds arrive verbatim from agent arguments (`_seeds_from_args`), and an agent that
    passes absolute paths would otherwise write absolute keys that match no anchor.

    The consequence is not a missed join but a WRONG NUMBER: the redirect metric is
    `(shown anchors - seeds) & touched`, so a seed set that fails to match inflates the
    deliverable in the flattering direction. For the same reason this normalizes rather than
    drops — a dropped seed shrinks the set and over-counts too. Only an out-of-root seed is
    discarded, and only because no anchor can equal it in any form.

    Shared by BOTH of `_record`'s writes (fix-wave finding): the aggregate `retrieval_seeds`
    counter and the `retrieval_events` journal used to see different shapes of the same call
    — an absolute-path retrieval landed relative in the journal but absolute in the
    aggregate, which permanently zeroed doctor's `never-surfaced` "people asked there"
    signal for that file (the counter is cumulative and never resets). A `domain:<slug>`
    seed (drill_down's, with no file to normalize against) passes through unchanged here —
    it is filtered out only where `_record` builds the journal-bound list, never here.
    """
    root = os.path.realpath(Path(store.path).parent)
    out: list[str] = []
    for seed in seeds:
        try:
            target = os.path.realpath(seed if os.path.isabs(seed) else os.path.join(root, seed))
            rel = os.path.relpath(target, root)
        except (OSError, ValueError):
            continue
        if rel == os.curdir or rel == os.pardir or rel.startswith(os.pardir + os.sep):
            continue
        out.append(rel)
    return out


# The `intent` a `drill_down` render row is written under (usage-stats design D5/D11). A
# drill-down delivers records and applies no budget, so its row carries `emitted` and zeros for
# every budget field, and the report reads this label to keep those zeros out of the budget
# figures. `stats/model.py` mirrors the literal (it imports nothing from here) and a test pins
# the two together.
DRILL_DOWN_INTENT = "drill_down"


def _caller_intent(intent: str | None) -> str | None:
    """The label a budgeted lookup's caller passed, with the reserved one removed.

    ``DRILL_DOWN_INTENT`` tells the report a row applied no budget. A caller that passed it to
    ``get_task_context`` would have that lookup's real budget counts read as no budget at all,
    so the label is not recorded for a caller; every other label passes through unchanged.
    """
    if intent is not None and intent.strip() == DRILL_DOWN_INTENT:
        return None
    return intent


def _record(
    store: Store,
    record_ids: list[str],
    seeds: list[str],
    *,
    ctx: TaskContext | None = None,
    intent: str | None = None,
) -> None:
    """Best-effort telemetry. Swallows everything: a retrieval that failed because a
    counter could not be written would be strictly worse than no counters (D9).

    Seeds are normalized ONCE, up front, so the aggregate `retrieval_seeds` counter and the
    `retrieval_events` journal agree on the same key shape for the SAME call (fix-wave
    finding: they used to disagree — raw seeds into the counter, normalized into the
    journal — which left an absolute-path retrieval's aggregate entry permanently unable to
    join `descriptor.file_path` and silently zeroed doctor's `never-surfaced` signal for that
    file). The normalization itself is wrapped in its own suppress: a failure there must
    degrade to the pre-fix (raw) seeds rather than losing telemetry entirely, per D9.

    Two independent writes after that. The aggregate counters answer "which memory is dead"
    and need no session; the journal answers "did memory arrive when it was for" and is
    useless without one, so a missing session key skips the journal alone and never the
    counters. The journal write additionally drops `domain:<slug>` seeds (drill_down's,
    spec §4: "a seed with no file writes no event") — the aggregate keeps them, since
    `retrieval_seeds` has always counted that key (see `retrieval_seed_queries`'s pinned
    `{"domain:payments": 1}`) and only the journal's storage contract excludes pathless keys.

    A third write, the render journal, runs only when the caller passes the `TaskContext`
    it rendered from; see the last block. `drill_down` passes one of its own.
    """
    if not _telemetry_enabled():
        return
    normalized_seeds = seeds
    with contextlib.suppress(Exception):
        normalized_seeds = _normalized_seeds(store, seeds)
    with contextlib.suppress(Exception):
        store.record_retrieval(record_ids, normalized_seeds)
    # Sampled ONCE for this call and passed to both journal writes. The key is a shared `meta`
    # row that a `SessionStart` overwrites, so reading it once per write let a start landing
    # between them stamp one call's show rows and render row with two different sessions, which
    # the report counted as two sessions and two showings. The writes keep their own suppress
    # blocks below: sharing the value must not couple their failures.
    session_id: str | None = None
    with contextlib.suppress(Exception):
        session_id = _session_key(store)
    if session_id is None:
        return
    with contextlib.suppress(Exception):
        shows = [
            (record_id, path)
            for record_id in dict.fromkeys(record_ids)
            for path in _anchor_paths(store, record_id)
        ]
        journal_seeds = [s for s in normalized_seeds if not s.startswith("domain:")]
        store.record_retrieval_events(session_id, journal_seeds, shows)
    # Render accounting (2026-09-18-usage-stats-design.md, D5). Its own suppress, after the
    # two writes above, so a failure here costs only this journal — and only when a
    # TaskContext was in hand, so a caller with no render to account for writes no row.
    if ctx is None:
        return
    with contextlib.suppress(Exception):
        had_rejected = had_superseded = False
        for record_id in dict.fromkeys(ctx.shown_ids):
            decision = store.get_decision(record_id)
            if decision is None:
                continue
            if (decision.rejected or "").strip():
                had_rejected = True
            if decision.status == DecisionStatus.SUPERSEDED:
                had_superseded = True
        store.record_render_event(
            session_id,
            intent=intent,
            selected=ctx.selected,
            emitted=ctx.emitted,
            degraded=ctx.degraded,
            dropped_for_budget=ctx.dropped_for_budget,
            chars_used=ctx.chars_used,
            had_rejected=had_rejected,
            had_superseded=had_superseded,
        )


def _propose_decisions_impl(
    store,
    reader,
    drafts: list[dict],
    session_id: str | None = None,
    author: str | None = None,
    facts: list[dict] | None = None,
    auto_accept: bool = False,
    ratify_policy: RatifyPolicy = RatifyPolicy.MANUAL,
) -> list[dict]:
    """Testable core for propose_decisions (see capture.propose/propose_facts).

    ``facts`` are STANDALONE fact drafts (as opposed to a ``DraftDecision.facts`` entry,
    which rides its own decision draft and is handled inside ``capture.propose`` already) —
    run through ``capture.propose_facts`` after every decision draft has been processed, and
    their result dicts appended after the decision results, never interleaved.

    ``auto_accept`` (default ``False``) is the resolved ``SIDEGRAPH_AUTO_ACCEPT`` bool (see
    ``_auto_accept``/design/superpowers/specs/2026-07-10-ratification-ux-and-mcp-gaps-design.md)
    — passed through to both ``propose`` and ``propose_facts`` unchanged, so decision drafts,
    their attached facts, and standalone facts all land ``accepted`` together when it's on.

    ``ratify_policy`` (default ``RatifyPolicy.MANUAL``) is the resolved
    ``SIDEGRAPH_RATIFY_POLICY`` value (design D1, ``server._ratify_policy()``) — the SAME
    object is passed through to both ``propose`` and ``propose_facts`` unchanged, the whole
    point being that one MCP ``propose_decisions`` request samples the policy exactly once,
    not once per core call.
    # see design/superpowers/specs/2026-09-11-auto-ratification-policy-design.md D1
    """
    if not isinstance(drafts, (list, tuple)) or (
        facts is not None and not isinstance(facts, (list, tuple))
    ):
        raise InputLimitError()
    if len(drafts) + len(facts or []) > MAX_DRAFTS:
        raise InputLimitError()
    preflight_drafts(
        [*drafts, *(facts or [])],
        decision_indices=range(len(drafts)),
        # The core decision path also visits its default ref=None, even for no drafts.
        metadata=(session_id, author, None),
    )
    results = [
        r.model_dump(mode="json")
        for r in propose(
            drafts,
            store,
            reader,
            session_id=session_id,
            author=author,
            auto_accept=auto_accept,
            ratify_policy=ratify_policy,
        )
    ]
    if facts:
        results.extend(
            r.model_dump(mode="json")
            for r in propose_facts(
                facts,
                store,
                reader,
                session_id=session_id,
                author=author,
                auto_accept=auto_accept,
                ratify_policy=ratify_policy,
            )
        )
    return _hint_elements(store, results, "written")


def _format_domain_proposal_line(d: Domain) -> str:
    """One-line domain-draft render for ``_list_proposed_impl`` (id, slug, title, summary,
    membership rule) — deliberately more compact than ``capture.format_domain_proposal``'s
    multi-line CLI render, since ``list_proposed`` packs everything pending into one
    MCP-tool response. Always renders the membership rule — ``path_prefixes``/
    ``communities`` (via the shared ``format_path_prefixes``/``format_communities_sample``
    helpers, Gate-5 finding) and ``seed_anchors`` (via ``format_seed_anchors_sample``,
    Gate-6 finding) — so the human gate has the same rule visibility here as on the CLI.
    The ``anchors:`` segment is the one exception: omitted entirely when ``seed_anchors``
    is empty, rather than printing a third empty segment."""
    summary = d.summary.strip().splitlines()[0] if d.summary.strip() else ""
    paths = format_path_prefixes(d.path_prefixes)
    communities = format_communities_sample(d.communities)
    line = (
        f"{d.domain_id}  [domain] {d.slug} — {d.title}: {summary} "
        f"(paths: {paths}; communities: {communities}"
    )
    anchors = format_seed_anchors_sample(d.seed_anchors)
    if anchors:
        line += f"; anchors: {anchors}"
    return line + ")"


def _nested_fact_ids(store: Store, proposals: list[Decision]) -> set[str]:
    """Ids of still-``PROPOSED`` facts that support one of ``proposals`` -- these ride
    their decision's ratify verdict (cascade; see ``Store.ratify``/``Store.drop``) and are
    rendered nested under that decision in the queue, never listed again in the standalone
    "Facts:" section. Shared by ``_list_proposed_impl``, ``ratify_main``'s bare-run render,
    and ``--all``'s id collection so none of the three double-count a cascaded fact.
    """
    return {
        f.id
        for d in proposals
        for f in store.facts_for_decision(d.id)
        if f.status == DecisionStatus.PROPOSED
    }


_NOT_SURFACING = "[not surfacing]"


def _format_proposed_decision_block(store: Store, d: Decision) -> str:
    """``format_proposal(d)`` plus one indented ``  evidence: ...`` line per still-
    ``PROPOSED`` fact supporting it (``store.facts_for_decision``) -- a preview of the
    cascade: ratifying this decision also ratifies these facts. Shared by
    ``_list_proposed_impl`` (MCP) and ``ratify_main``'s bare-run print so the two stay in
    lockstep."""
    # Round-2 practitioner review: mark what has already stopped being delivered. The
    # surfacing window is only humane if the queue says which items it has stopped
    # serving — otherwise a reviewer cannot tell an urgent backlog from an inert one, and
    # the window reads as a silent drop. The record stays listed and ratifiable either way.
    head = format_proposal(d)
    if not proposal_surfaces(d):
        # On the TITLE line, where the eye lands — not at the end of a multi-line block.
        first, _, rest = head.partition("\n")
        head = f"{first}  {_NOT_SURFACING}" + (f"\n{rest}" if rest else "")
    lines = [head]
    for f in store.facts_for_decision(d.id):
        if f.status == DecisionStatus.PROPOSED:
            lines.append(f"  evidence: {f.statement} [{f.source}]  ({f.id})")
    return "\n".join(lines)


def _list_proposed_impl(store) -> str:
    """Pending decisions, facts, AND domains (§2/§4 review PINNED I2; facts layer
    2026-07-10), sectioned like ``sidegraph-ratify``'s bare listing (``cli.ratify_main``):
    "Decisions:" (each block carries its still-proposed supporting facts nested as
    ``  evidence: ...`` lines) then "Facts:" (standalone proposed facts -- ones NOT nested
    under any decision above) then "Domains:", each printed only when non-empty."""
    proposals = list(store.iter_proposed())
    domains = list(store.iter_domains(status=DomainStatus.PROPOSED))
    nested = _nested_fact_ids(store, proposals)
    standalone_facts = [f for f in store.iter_proposed_facts() if f.id not in nested]
    if not proposals and not domains and not standalone_facts:
        return "No proposed decisions, facts, or domains pending ratification."
    sections: list[str] = []
    if proposals:
        sections.append(
            "Decisions:\n"
            + "\n\n".join(_format_proposed_decision_block(store, d) for d in proposals)
        )
    if standalone_facts:
        sections.append("Facts:\n" + "\n\n".join(format_fact_proposal(f) for f in standalone_facts))
    if domains:
        sections.append("Domains:\n" + "\n".join(_format_domain_proposal_line(d) for d in domains))
    return "\n\n".join(sections)


@mcp.tool(annotations=_LOCAL_WRITE)
@safe_write
def propose_decisions(
    drafts: list[dict],
    session_id: str | None = None,
    author: str | None = None,
    facts: list[dict] | None = None,
) -> list[dict]:
    """Propose distilled decisions from this session (What/Why/Where/Learned drafts).

    Each draft: {"title", "kind": adr|lesson|constraint|gotcha, "context", "choice",
    "rejected"?, "consequences"?, "anchors": [{"name", "file_path", "relation"?}],
    "initiative"?, "supersedes"?, "tags"? ([str], free text; slugified and redacted),
    "layer"? ("business"|"technical"), "facts"? ([DraftFact], attached — see below)}.
    Each anchor's ``relation`` (optional): creates|modifies|affects|deprecates|considered,
    defaults to "affects" — same five literals ``add_decision`` enumerates.
    The pipeline redacts secrets (including tag and initiative text), validates, dedups, writes as
    status=proposed (ratified later by a human — or at write time by an opt-in
    SIDEGRAPH_RATIFY_POLICY when the draft is eligible), and anchors best-effort —
    tags/layer/per-anchor relation carry through unchanged to ratification.

    Each result carries ``"anchors_skipped": [{"name", "reason": "ambiguous", "candidates"}]``
    — anchors whose name matched more than one graph node, so no precise Tier-2 leaf was
    created for them (capped at 5); empty when every anchor resolved cleanly. Each decision
    result also carries ``"anchors_orphaned": [{"entity_id", "canonical_name", "tier": 2,
    "reason"}, ...]`` — anchors that resolved to nothing (the leaf is written but dead on
    arrival for retrieval; ``reason`` is ``"no-graph"`` when no graph is present). Repair
    with ``add_anchors`` once the record is written — never by superseding it.

    Each result also carries ``neighbors`` — up to 3 live records anchored to the same code
    (deduplicated; on a ``deduped`` result, the existing record itself). If your new record
    CHANGES, NARROWS or INVALIDATES one of them, do not leave both alive: call
    ``supersede_decision(old_decision_id=...)`` with the successor content.

    ``facts`` (optional, top-level) proposes STANDALONE facts — non-derivable knowledge
    that doesn't attach to any decision drafted in this same call. Each is a DraftFact:
    {"statement", "source", "anchors"? ([{"name", "file_path", "relation"?}]), "supports"?
    ([decision id, ...])}. A standalone fact needs at least one anchor or one ``supports``
    id — otherwise it would be unreachable and is rejected with a reason. Compare: a
    draft's OWN ``"facts"`` list (inside a decision draft, not this top-level param) is
    ATTACHED — it always supports that decision and, absent its own anchors, inherits the
    decision's anchors; that path already runs inside each decision draft, unchanged by
    this parameter.

    Standalone-fact results (``ProposeFactResult``-shaped: "status", "fact_id", "reason",
    "redactions", "anchors_skipped", "anchors_orphaned", "ratified_by", "auto_ratify_error")
    are appended to the returned list AFTER every decision draft's result, in ``facts``
    order — never interleaved with the decision results.

    Each result also carries ``ratified_by`` (the ``auto:<policy>`` stamp when an
    auto-ratification policy accepted the record at write time, else null — including
    nested attached facts accepted through the cascade) and ``auto_ratify_error`` (null
    unless an attempt failed); ``status`` keeps its write-action meaning.

    A ``written`` result may carry a ``reason``: the record is on disk, but a step after
    the write (anchors, initiative, tags, attached facts, auto-ratify) failed and the later
    steps did not run. The text names the step and the remedy. A ``rejected`` result whose
    reason starts with "internal error" may have left the record on disk; do not re-propose
    it in the same session.

    Auto-accept: when the ``SIDEGRAPH_AUTO_ACCEPT`` environment variable is set to ``"on"``
    (off by default), every decision draft, its attached facts, and every standalone fact
    land ``status=accepted`` directly instead of ``proposed`` — the pending-ratification
    queue is bypassed for this call. Provenance still stamps ``source="agent"`` regardless,
    so history never lies about authorship. Domain drafts (``propose_domains``) are NEVER
    affected by this flag. When both it and SIDEGRAPH_RATIFY_POLICY are set, this flag
    wins. See design/superpowers/specs/
    2026-07-10-ratification-ux-and-mcp-gaps-design.md for the trade-off (auto-accept removes
    the store's only noise filter; recommended for solo use, not team stores).
    """
    return _propose_decisions_impl(
        _get_store(),
        _load_reader(),
        drafts,
        session_id=session_id,
        author=author,
        facts=facts,
        auto_accept=_auto_accept(),
        ratify_policy=_ratify_policy(),
    )


@mcp.tool(annotations=_READ_ONLY)
def list_proposed() -> str:
    """List decisions, facts, AND domains awaiting ratification, human-readably.

    Sectioned like ``sidegraph-ratify``'s bare listing: a "Decisions:" section (each
    decision's still-proposed supporting facts nested under it as ``  evidence: ...``
    lines), a "Facts:" section for standalone proposed facts, then a "Domains:" section --
    each printed only when non-empty (see ``_list_proposed_impl``).
    """
    return _list_proposed_impl(_get_store())


def _ratify_one(store: Store, id_: str, action: str) -> tuple[str, list[Fact]]:
    """Route one id to a decision, a fact, or a domain by lookup (decision first, then
    fact, then domain) and apply ``action`` ("accept" | "drop"). Unknown ids get a generic
    error entry — never guess which kind an id belongs to. The known-decision branch
    mirrors ``_ratify_decisions_impl``'s per-id try/except exactly, so existing error text
    for a decision id is unchanged; a fact id's ``ratify_fact``/``drop_fact`` ValueError
    (not proposed) surfaces the same way. Only a genuinely unknown id's wording differs.

    Returns ``(result, cascaded)`` — ``cascaded`` is the list of :class:`Fact` records that
    rode a DECISION's verdict in this call (``Store.ratify``/``Store.drop``'s own cascade;
    facts layer 2026-07-10), always empty for a fact or domain id since neither has
    anything of its own to cascade. The caller (``_ratify_impl``) turns this into per-fact
    result-dict entries.
    """
    if store.get_decision(id_) is not None:
        try:
            if action == "accept":
                _decision, cascaded = store.ratify(id_)
                return "accepted", cascaded
            _decision, cascaded = store.drop(id_)
            return "dropped", cascaded
        except ValueError:
            return (
                "error: ratification failed; reopen the store and inspect status before retrying",
                [],
            )
    if store.get_fact(id_) is not None:
        try:
            if action == "accept":
                store.ratify_fact(id_)
            else:
                store.drop_fact(id_)
            return ("accepted" if action == "accept" else "dropped"), []
        except ValueError:
            return (
                "error: ratification failed; reopen the store and inspect status before retrying",
                [],
            )
    if store.get_domain(id_) is not None:
        result = (
            store.ratify_domains(accept=[id_])
            if action == "accept"
            else store.ratify_domains(drop=[id_])
        )
        return (
            "error: ratification failed; reopen the store and inspect status before retrying"
            if result[id_].startswith("error")
            else result[id_]
        ), []
    return "error: unknown id (not a pending decision, fact, or domain)", []


def _ratified_domain(store: Store, id_: str, result: str) -> bool:
    """True iff ``id_`` was routed to (and actually landed on) a domain, not a decision or
    an unknown id — used to gate the TOC cache refresh below to real domain changes only."""
    if result.startswith("error"):
        return False
    return store.get_decision(id_) is None and store.get_domain(id_) is not None


def _ratify_impl(
    store: Store,
    accept: list[str] | None = None,
    drop: list[str] | None = None,
    reader: GraphifyReader | None = None,
) -> dict[str, str]:
    """Testable core for the unified ``ratify`` tool: one gate covering decisions, facts,
    AND domains (§4, "one gate, no exceptions"; facts layer 2026-07-10). Accept-before-drop,
    same id in both -> drop is ignored (mirrors ``_ratify_decisions_impl``'s convention).

    Cascade reporting: when an accepted/dropped id routes to a decision, every fact that
    rode its verdict (``_ratify_one``'s ``cascaded`` return) gets its OWN entry in the
    result dict too — ``f"accepted (evidence of {decision_id})"`` /
    ``f"dropped (evidence of {decision_id})"`` — so a caller sees every record this call
    actually touched, not just the ids it was explicitly given.

    Accept order-independence (Task 7 fix pass, Important-1): the accept loop below runs
    in TWO passes — every decision id in ``accept`` first, regardless of its position in
    the caller's list, then everything else. A fact nested under one of these decisions
    must always be swept by ITS cascade, never independently re-ratified first just
    because it happened to be listed earlier — without this, ``accept=[d.id, f.id]`` and
    ``accept=[f.id, d.id]`` disagreed: the first order re-processed ``f.id`` after the
    cascade had already flipped it, raising a spurious ``"fact ... is not proposed"``
    error; the second silently produced a plain ``"accepted"`` instead of the
    cascade-attributed string, for the exact same final state. Both orders now produce
    identical output. A second-pass id already present in ``out`` (because a decision
    processed in pass one cascaded it) is skipped outright — same guard the drop loop
    below already relies on for its own cross-list (accept vs drop) dedup.

    Fix: lazy sync alone keeps ``last_synced_graph_version`` current without ever
    recomputing the TOC cache, so "bootstrap -> ratify -> SessionStart TOC comes alive"
    did nothing until the next real graph rebuild. Rebuild the cache here, immediately,
    whenever >= 1 domain id was actually accepted or dropped in this call — a
    decisions-only ratify leaves the cache untouched (it wouldn't change the TOC anyway).

    ``reader`` (the ``ratify``/``ratify_decisions`` tools' normal call, via
    ``_load_reader()``): when a domain is actually ACCEPTED in this call and a reader is
    present, its ``communities`` are resolved immediately from ``seed_anchors``/
    ``path_prefixes`` (``sync.refresh_domain_communities_now`` — §2a amendment) so
    ``drill_down`` shows membership the instant the human accepts a set, instead of
    waiting for the next graph-rebuild-gated ``sync`` pass. Best-effort: a resolution
    failure here must never fail the ratify call itself (mirrors every other best-effort
    engine touch in this module).
    """
    out: dict[str, str] = {}
    domain_changed = False
    accept_ids = accept or []
    # Pass 1: every decision id first (see docstring's "Accept order-independence"). No
    # domain-refresh check here -- a decision id can never satisfy `_ratified_domain`
    # (it requires `store.get_decision(id_) is None`, and this pass only ever routes ids
    # that ARE decisions), so that check lives solely in pass 2 below.
    for id_ in accept_ids:
        if id_ in out or store.get_decision(id_) is None:
            continue
        result, cascaded = _ratify_one(store, id_, "accept")
        out[id_] = result
        for f in cascaded:
            out[f.id] = f"accepted (evidence of {id_})"
    # Pass 2: everything else (facts, domains, unknown ids) -- an id already reported by a
    # pass-1 cascade is skipped, never re-processed against a record that no longer exists.
    for id_ in accept_ids:
        if id_ in out:
            continue
        result, cascaded = _ratify_one(store, id_, "accept")
        out[id_] = result
        for f in cascaded:
            out[f.id] = f"accepted (evidence of {id_})"
        if _ratified_domain(store, id_, out[id_]):
            domain_changed = True
            domain = store.get_domain(id_)
            if domain is not None:
                # sync.activate_accepted_domain (design D2 shared helper): resolves
                # membership now, or schedules the VOLATILE_STALE_KEY heal itself when
                # there is no reader or the refresh raises -- never fails this ratify
                # either way. Rendering the "path rule too broad" sentence stays HERE
                # (a literal trigger phrase for the heal-anchors skill), not in the helper.
                activation = activate_accepted_domain(domain, store, reader)
                if activation.overbroad is not None:
                    out[id_] += (
                        " (path rule too broad: path_prefixes match "
                        f"{activation.overbroad['matched']}/{activation.overbroad['total']} "
                        "communities — not applied; seed_anchors, if any, still applied)"
                    )
            else:
                # The domain vanished between _ratified_domain's check and here (can only
                # happen under concurrent mutation) -- same unresolved-membership fallback
                # as a failed/absent-reader activation.
                store.set_meta(VOLATILE_STALE_KEY, "1")
    for id_ in drop or []:
        if id_ in out:
            out[id_] = f"{out[id_]} (drop ignored)"
            continue
        result, cascaded = _ratify_one(store, id_, "drop")
        out[id_] = result
        for f in cascaded:
            out[f.id] = f"dropped (evidence of {id_})"
        domain_changed = domain_changed or _ratified_domain(store, id_, out[id_])
    if domain_changed:
        store.set_meta(TOC_CACHE_KEY, json.dumps(build_toc(store)))
    return out


def _ratify_decisions_impl(
    store, accept: list[str] | None = None, drop: list[str] | None = None
) -> dict[str, str]:
    out: dict[str, str] = {}
    for did in accept or []:
        try:
            _decision, _cascaded = store.ratify(did)
            out[did] = "accepted"
        except ValueError:
            out[did] = (
                "error: ratification failed; reopen the store and inspect status before retrying"
            )
    for did in drop or []:
        if did in out:
            out[did] = f"{out[did]} (drop ignored)"
            continue
        try:
            _decision, _cascaded = store.drop(did)
            out[did] = "dropped"
        except ValueError:
            out[did] = (
                "error: ratification failed; reopen the store and inspect status before retrying"
            )
    return out


@mcp.tool(annotations=_LOCAL_WRITE)
@safe_write
def ratify(accept: list[str] | None = None, drop: list[str] | None = None) -> dict[str, str]:
    """Ratify pending proposals of ANY kind — decisions, facts, and domains share one gate.

    Each id in ``accept``/``drop`` is routed by lookup: a pending decision flips
    proposed->accepted (or rejected on drop, append-only); a pending fact flips the same
    way directly; a pending domain flips proposed->accepted and mints its paired
    ``domain:<slug>`` entity (or ->dropped, no entity minted). An id present in both lists
    is accepted; the drop is ignored (not a conflict — reported as ``"accepted (drop
    ignored)"``/etc). Unknown ids get an ``"error: ..."`` entry; one bad id never aborts the
    rest of the batch.

    Cascade: accepting/dropping a decision id also flips every still-proposed fact that
    supports it (facts layer 2026-07-10) — each cascaded fact id gets its OWN entry in the
    returned dict too, ``f"accepted (evidence of {decision_id})"`` /
    ``f"dropped (evidence of {decision_id})"``, so nothing this call touched goes
    unreported.
    """
    return _ratify_impl(_get_store(), accept=accept, drop=drop, reader=_load_reader())


@mcp.tool(annotations=_LOCAL_WRITE)
@safe_write
def ratify_decisions(
    accept: list[str] | None = None, drop: list[str] | None = None
) -> dict[str, str]:
    """Deprecated alias for ``ratify`` (deprecated since 0.1.0 and still kept; despite the
    name, it now covers facts and domains too — identical behavior to ``ratify``). Prefer
    ``ratify``."""
    return _ratify_impl(_get_store(), accept=accept, drop=drop, reader=_load_reader())


def _add_domain_impl(
    store: Store,
    reader,
    slug: str,
    title: str,
    summary: str,
    parent_slug: str | None = None,
    path_prefixes: list[str] | None = None,
    communities: list[str] | None = None,
    seed_anchors: list[dict] | None = None,
    author: str | None = "agent",
) -> dict:
    """Testable core for add_domain (§4.3, manual path). Always lands `status=proposed` —
    manual authoring is not an exception to the ratification gate (§4: "one gate, no
    exceptions")."""
    preflight_direct(
        slug, title, summary, parent_slug, path_prefixes, communities, seed_anchors, author
    )
    parent_id = None
    if parent_slug is not None:
        parent = store.find_domain_by_slug(parent_slug)
        if parent is None:
            raise SafeWriteError("parent_slug")
        parent_id = parent.domain_id

    # Title and summary are prose in a repo-committed store: same gate as propose_domains.
    # The slug is an identifier and stays as given (redacting it would break lookup).
    title, n1 = redact(title)
    summary, n2 = redact(summary)
    graph_version = reader.graph_version() if reader is not None else None
    domain = Domain(
        slug=slug,
        title=title,
        summary=summary,
        parent_id=parent_id,
        communities=communities or [],
        path_prefixes=path_prefixes or [],
        # raw MCP JSON dicts -> Descriptor; pydantic validates/coerces each on construction.
        seed_anchors=[Descriptor(**d) for d in seed_anchors] if seed_anchors else [],
        provenance=Provenance(source="manual", author=author, graph_version=graph_version),
    )
    store.add_domain(domain)
    return _with_commit_hint(
        store, {"domain_id": domain.domain_id, "status": domain.status.value, "redactions": n1 + n2}
    )


@mcp.tool(annotations=_LOCAL_WRITE)
@safe_write
def add_domain(
    slug: str,
    title: str,
    summary: str,
    parent_slug: str | None = None,
    path_prefixes: list[str] | None = None,
    communities: list[str] | None = None,
    seed_anchors: list[dict] | None = None,
    author: str | None = "agent",
) -> dict:
    """Manually author a Domain — a named area of the system with WHY-IT-EXISTS prose
    (§4.3, manual path). Always lands ``status=proposed``: manual authoring is not an
    exception to the ratification gate — ``ratify``/``sidegraph-ratify`` accepts it like any
    other draft.

    ``parent_slug``, when given, must resolve to an existing (non-superseded) domain via
    ``find_domain_by_slug``; anything else is a hard error (never guess a parent).
    ``path_prefixes`` is a static stabilizer rule, set here and never touched again;
    ``seed_anchors`` (``[{"name", "file_path"?}, ...]``) is the durable, entity-anchored
    counterpart (§2a amendment) — both are resolved into ``communities`` by
    ratify/sync, never the other way around. ``communities`` remains as a separate,
    optional immediate seed for a direct-write caller that already knows current
    (volatile) community ids and wants them visible before the next resolve pass.

    ``title`` and ``summary`` are redacted first, like every other prose field; the slug is
    an identifier and is not.

    Returns ``{"domain_id", "status", "redactions"}`` (``redactions`` = secret replacements
    made across title and summary).
    """
    return _add_domain_impl(
        _get_store(),
        _load_reader(),
        slug,
        title,
        summary,
        parent_slug=parent_slug,
        path_prefixes=path_prefixes,
        communities=communities,
        seed_anchors=seed_anchors,
        author=author,
    )


def _resolve_domain_ref(store: Store, slug_or_id: str) -> Domain | None:
    """``old_slug_or_id`` may be either a ``domain_id`` (ULID) or a ``slug`` — try the id
    lookup first (exact, cheap), then fall back to ``find_domain_by_slug`` (which already
    prefers accepted > proposed > dropped, newest first) so a caller of
    ``supersede_domain`` doesn't need to know or track which shape it's holding."""
    domain = store.get_domain(slug_or_id)
    if domain is not None:
        return domain
    return store.find_domain_by_slug(slug_or_id)


def _supersede_domain_impl(
    store: Store,
    reader,
    old_slug_or_id: str,
    new_slug: str,
    new_title: str,
    new_summary: str,
    path_prefixes: list[str] | None = None,
    seed_anchors: list[dict] | None = None,
    parent_slug: str | None = None,
    author: str | None = "agent",
) -> dict:
    """Testable core for supersede_domain: the lineage-correct rename/re-scope path.

    Wraps the existing ``Store.supersede_domain`` primitive (append-only reversal: close
    the old domain, write a new one with ``supersedes`` set, in one transaction) with the
    same manual-authoring shape ``_add_domain_impl`` uses — ``parent_slug`` resolution,
    ``path_prefixes``/``seed_anchors`` as the successor's membership-rule seed, manual
    provenance. Like every other domain-authoring path, the successor lands
    ``status=proposed`` -- domains have no exception to the one ratification gate (see
    ``_add_domain_impl``'s own docstring): closing the predecessor happens immediately
    (that's what "supersede" means), but the new name/scope still needs a human `ratify`
    before it's TOC-visible.
    """
    preflight_direct(
        old_slug_or_id,
        new_slug,
        new_title,
        new_summary,
        path_prefixes,
        seed_anchors,
        parent_slug,
        author,
    )
    old = _resolve_domain_ref(store, old_slug_or_id)
    if old is None:
        raise SafeWriteError("old_slug_or_id")

    parent_id = None
    if parent_slug is not None:
        parent = store.find_domain_by_slug(parent_slug)
        if parent is None:
            raise SafeWriteError("parent_slug")
        parent_id = parent.domain_id

    new_title, n1 = redact(new_title)
    new_summary, n2 = redact(new_summary)
    graph_version = reader.graph_version() if reader is not None else None
    new_domain = Domain(
        slug=new_slug,
        title=new_title,
        summary=new_summary,
        parent_id=parent_id,
        path_prefixes=path_prefixes or [],
        # raw MCP JSON dicts -> Descriptor; pydantic validates/coerces each on construction.
        seed_anchors=[Descriptor(**d) for d in seed_anchors] if seed_anchors else [],
        supersedes=old.domain_id,
        provenance=Provenance(source="manual", author=author, graph_version=graph_version),
    )
    result = store.supersede_domain(old.domain_id, new_domain)
    return _with_commit_hint(
        store,
        {
            "domain_id": result.domain_id,
            "status": result.status.value,
            "supersedes": old.domain_id,
            "redactions": n1 + n2,
        },
    )


@mcp.tool(annotations=_LOCAL_WRITE)
@safe_write
def supersede_domain(
    old_slug_or_id: str,
    new_slug: str,
    new_title: str,
    new_summary: str,
    path_prefixes: list[str] | None = None,
    seed_anchors: list[dict] | None = None,
    parent_slug: str | None = None,
    author: str | None = "agent",
) -> dict:
    """Close an old Domain and write its replacement — the lineage-correct rename/re-scope
    path (mirrors ``supersede_decision`` for the domain side; wraps the existing
    ``Store.supersede_domain`` primitive, which previously had no MCP surface).

    ``old_slug_or_id`` resolves either a ``domain_id`` or a ``slug`` (tries the id lookup
    first, then ``find_domain_by_slug``) — never a guess: an id/slug that resolves to
    nothing is a hard error. ``parent_slug``, when given, must resolve to an existing
    (non-superseded) domain, same as ``add_domain``'s. ``path_prefixes``/``seed_anchors``
    seed the SUCCESSOR's membership rule from scratch (nothing is inherited from the
    predecessor — pass the old domain's own values back if you want them carried over).

    The predecessor is flipped to ``superseded`` immediately (append-only: the record
    stays, fully retrievable, never deleted) in the same transaction that writes the
    successor. The successor itself always lands ``status=proposed`` — same "one gate, no
    exceptions" rule every other domain-authoring tool follows (``add_domain``,
    ``propose_domains``): a human still calls ``ratify(accept=[...])`` before the new
    name/scope is TOC-visible.

    Raises (before anything is written) if: ``old_slug_or_id`` doesn't resolve to any
    domain; ``parent_slug`` is given but doesn't resolve to any domain; or ``new_slug``
    collides with some OTHER still-live (proposed/accepted) domain (the predecessor itself
    is excluded from that check, so reusing the same slug is fine).

    ``new_title`` and ``new_summary`` are redacted first, like every other prose field.

    Returns ``{"domain_id": str, "status": str, "supersedes": str, "redactions": int}`` —
    ``status`` is always ``"proposed"``, ``domain_id`` is the successor's, ``supersedes`` is
    the predecessor's resolved ``domain_id``, ``redactions`` counts the secret replacements
    made in the new title and summary.
    """
    return _supersede_domain_impl(
        _get_store(),
        _load_reader(),
        old_slug_or_id,
        new_slug,
        new_title,
        new_summary,
        path_prefixes=path_prefixes,
        seed_anchors=seed_anchors,
        parent_slug=parent_slug,
        author=author,
    )


def _propose_domains_impl(
    store,
    reader,
    drafts: list[dict],
    session_id: str | None = None,
    author: str | None = None,
    ratify_policy: RatifyPolicy = RatifyPolicy.MANUAL,
) -> list[dict]:
    """Testable core for propose_domains (see capture.propose_domains).

    ``ratify_policy`` (default ``RatifyPolicy.MANUAL``) is the resolved
    ``SIDEGRAPH_RATIFY_POLICY`` value (design D1, ``server._ratify_policy()``), forwarded
    unchanged to ``capture.propose_domains``.
    # see design/superpowers/specs/2026-09-11-auto-ratification-policy-design.md D1
    """
    results = _propose_domain_drafts(
        drafts, store, reader, session_id=session_id, author=author, ratify_policy=ratify_policy
    )
    return _hint_elements(store, [r.model_dump(mode="json") for r in results], "proposed")


@mcp.tool(annotations=_LOCAL_WRITE)
@safe_write
def propose_domains(
    drafts: list[dict],
    session_id: str | None = None,
    author: str | None = None,
) -> list[dict]:
    """Propose Domain drafts recognized during this session (§4.2, agent in-session path) —
    mirrors ``propose_decisions`` for the domain side.

    Each draft: {"slug", "title", "summary", "parent_slug"?, "path_prefixes"?,
    "seed_anchors"?}. ``seed_anchors`` (``[{"name", "file_path"?}, ...]``) is a durable
    entity-anchor seed (§2a amendment; mirrors ``add_domain``'s own param) for an
    agent-curated merge that has no single clean shared path prefix to rely on — resolves
    into ``communities`` on ratify (immediately) and on every later ``sync`` pass, so
    membership survives a fresh clone or a graph rebuild instead of evaporating like a raw
    community id would. May be given alongside ``path_prefixes``, in place of it, or
    omitted. The pipeline redacts secrets from title/summary, skips (never overwrites) when
    a non-superseded domain already claims the slug, resolves ``parent_slug`` (error if it
    doesn't resolve), and writes as status=proposed — a human ratifies later via
    ``ratify``/``sidegraph-ratify``, unless SIDEGRAPH_RATIFY_POLICY=auto-all ratifies an
    eligible draft at write time (``ratified_by`` carries the ``auto:<policy>`` stamp) and
    resolves its membership immediately. When that membership step hits a problem, the domain
    stays accepted anyway, and ``auto_ratify_error`` opens with ``activation:`` followed by
    one of two things: the error that stopped membership from resolving, or a ``path rule
    too broad`` notice, which means the ``path_prefixes`` claim was rejected and only the
    ``seed_anchors`` that resolve, if any, are still applied.

    Each result also carries ``warnings`` (design D7.4, deterministic domain lint) — a
    ``path_prefix`` matching no file in the current graph ("dead prefix"), or one that
    would subsume another ACCEPTED domain's own ``seed_anchors`` file. Advisory only,
    never blocks the write; under ``auto-all`` any warning keeps the draft proposed; empty
    when ``path_prefixes`` is empty or every prefix passes both checks.
    """
    return _propose_domains_impl(
        _get_store(),
        _load_reader(),
        drafts,
        session_id=session_id,
        author=author,
        ratify_policy=_ratify_policy(),
    )


def _compact_candidate(candidate) -> dict:
    """One ``DomainCandidate`` rendered as ``list_domain_candidates``'s per-candidate
    output shape (§1 design) — deliberately field-renamed/thinned from the internal model
    (``community_id`` -> ``community``, ``member_count`` -> ``members``) to match the
    tool's public contract, not the collector's internal one."""
    return {
        "community": candidate.community_id,
        "suggested_slug": candidate.suggested_slug,
        "suggested_title": candidate.suggested_title,
        "members": candidate.member_count,
        "top_members": candidate.top_members,
        "top_file": candidate.top_file,
        "has_label": candidate.has_label,
        # Durable anchor (§2a amendment): the community's god-node as a name+file_path
        # Descriptor -- feed this back as a Domain.seed_anchors entry instead of the
        # volatile `community` id above, which does not survive a fresh clone/rebuild.
        "anchor": candidate.anchor.model_dump() if candidate.anchor else None,
    }


def _list_domain_candidates_impl(
    store: Store,
    reader,
    min_members: int = 5,
    paths: list[str] | None = None,
    limit: int | None = None,
) -> dict:
    """Testable core for list_domain_candidates (§1 design). Pure read: builds on
    ``collect_domain_candidates`` (the exact selection ``bootstrap_domains`` would write)
    and only reads ``reader.communities()`` again to derive each candidate's presentational
    grouping path — never touches the store's write path.

    ``limit`` here is already resolved to ``collect_domain_candidates``'s own convention
    (``None`` = unlimited) — the public ``0``-means-unlimited sentinel and the scale-aware
    default (``DEFAULT_CANDIDATE_LIMIT``, finding B) are the outer ``list_domain_candidates``
    tool's job to apply/translate, so this "testable core" stays a thin, default-agnostic
    pass-through, same division of labor as ``collect_domain_candidates`` itself.

    Best-effort like every other MCP tool here: with no graph present, returns an
    all-empty shape (a ``"note"`` explains why) instead of erroring.
    """
    if reader is None:
        return {
            "graph_version": None,
            "total_candidates": 0,
            "total_significant": 0,
            "truncated": False,
            "already_claimed": 0,
            "skipped": {"below_threshold": 0, "filtered": 0},
            "groups": [],
            "ungrouped": [],
            "note": "no graphify graph present",
        }

    candidates, stats = collect_domain_candidates(
        store, reader, min_members=min_members, paths=paths, limit=limit
    )
    # Built once, reused per candidate — community_group_path's own per-call fallback
    # would otherwise re-walk reader.communities() for every candidate needing a fallback.
    communities_by_id = {c.community_id: c for c in reader.communities()}

    groups: dict[str, list[dict]] = {}
    ungrouped: list[dict] = []
    for c in candidates:
        compact = _compact_candidate(c)
        path = c.path_prefixes[0] if c.path_prefixes else None
        if path is None:
            path = community_group_path(c.community_id, reader, communities_by_id)
        if path is None:
            ungrouped.append(compact)
        else:
            groups.setdefault(path, []).append(compact)

    groups_out = [
        {
            "path": path,
            "member_total": sum(c["members"] for c in members),
            "candidates": members,
        }
        for path, members in sorted(groups.items())
    ]

    # finding B: `limit` truncates when it's set AND there were more significant
    # candidates than it let through -- independent of `already_claimed`, which only ever
    # narrows the (possibly already-limited) survivor set further, never the reverse.
    truncated = limit is not None and stats.total_before_limit > limit
    result = {
        "graph_version": reader.graph_version(),
        "total_candidates": stats.total,
        "total_significant": stats.total_before_limit,
        "already_claimed": stats.already_claimed,
        "skipped": {"below_threshold": stats.below_threshold, "filtered": stats.filtered},
        "groups": groups_out,
        "ungrouped": ungrouped,
        "truncated": truncated,
    }
    if truncated:
        result["note"] = (
            f"showing the top {limit} of {stats.total_before_limit} significant candidates "
            "(community-id order) -- widen with an explicit limit=N, limit=0 for the full "
            "list, or narrow with min_members/paths"
        )
    return result


@mcp.tool(annotations=_READ_ONLY)
def list_domain_candidates(
    min_members: int = 5,
    paths: list[str] | None = None,
    limit: int = DEFAULT_CANDIDATE_LIMIT,
) -> dict:
    """Read-only projection of the bootstrap candidate machinery (§1 domain-onboarding
    design) — the machine half of the ``name-domains`` skill. WRITES NOTHING, EVER; safe to
    call repeatedly.

    Presents every significant, not-yet-claimed community as a naming candidate,
    pre-grouped by shared top-level path (a structure hint the agent is free to regroup,
    merge, or rename). Built on ``collect_domain_candidates`` — the exact same selection
    ``sidegraph-domains bootstrap`` would write — so this tool always shows exactly what
    the CLI would propose, with every one of bootstrap's guards already applied (label-
    mismatch rejection, well-known-shared-dir/breadth veto on ``path_prefixes``, claim
    skip, within-run slug dedup, redact-before-slugify).

    ``min_members``/``paths`` mirror ``sidegraph-domains bootstrap``'s own knobs to
    pre-narrow when wanted; the ``name-domains`` skill's default call omits both and lets
    the agent narrow in conversation instead.

    ``limit`` (finding B, scale-robustness hardening) defaults to 100 — the top 100
    significant communities, in deterministic community-id order, same ordering
    ``sidegraph-domains bootstrap`` applies its own ``--limit`` in. On a monorepo-scale
    corpus the unbounded list is a ~276K-token dump (Airflow: 2,578 candidates); 100 is the
    measured sweet spot (~10K tokens). Pass an explicit ``limit=N`` to widen it, or
    ``limit=0`` for the full, unbounded list when you really want everything (the "all"
    convention — mirrors ``sidegraph-domains bootstrap --limit 0``). When the effective
    limit actually cuts candidates, the response's ``truncated`` is ``True`` and
    ``total_significant`` names the FULL count so it's never mistaken for the whole graph
    — narrow with ``min_members``/``paths`` instead, or widen ``limit``, rather than assume
    this is everything. Note: re-running with the SAME default/explicit limit only ever
    proposes the same community-id-sorted window (communities beyond it are never reached
    until you widen).

    Returns ``{"graph_version", "total_candidates", "total_significant", "truncated",
    "already_claimed", "skipped": {"below_threshold", "filtered"}, "groups": [{"path",
    "member_total", "candidates": [{"community", "suggested_slug", "suggested_title",
    "members", "top_members" (<=3), "top_file", "has_label", "anchor"}, ...]}],
    "ungrouped": [...same candidate shape...]}``. ``total_candidates`` is how many
    candidates THIS response actually includes (post-limit, post-already_claimed);
    ``total_significant`` is how many significant communities exist in total, before
    ``limit`` truncated them AND before the separate ``already_claimed`` skip — the two can
    differ even when ``truncated`` is ``False`` (some of what ``limit`` let through was
    already claimed); ``truncated`` specifically means "the limit itself cut candidates you
    never even got to see."

    ``anchor`` (``{"name", "file_path"}`` or ``null``, §2a amendment) is the community's
    god-node resolved to a durable Descriptor — feed it back as a ``Domain.seed_anchors``
    entry (via ``propose_domains``/``add_domain``) instead of the volatile ``community`` id
    alone, which does NOT survive a fresh clone or a graph rebuild.

    A candidate groups under its own derived ``path_prefixes`` when it has one; else under
    a clear (>=80%) majority top-level directory among its members — the same majority
    calc ``path_prefixes`` derivation uses, minus its two stabilizer-only vetoes (this is a
    display hint, never a membership rule). A candidate with neither lands in
    ``ungrouped``, never silently dropped. ``already_claimed`` counts communities excluded
    because a non-superseded domain (or a slug collision) already claims them — never
    listed in ``groups``/``ungrouped``.
    """
    resolved_limit = None if limit == 0 else limit
    return _list_domain_candidates_impl(
        _get_store(), _load_reader(), min_members=min_members, paths=paths, limit=resolved_limit
    )


def _list_domains_impl(store: Store, status: str | None = None) -> list[dict]:
    """Testable core for list_domains: every domain in the store (optionally filtered by
    status), sorted by slug. Pure read -- no reader/graph needed, writes nothing.

    Parent/child relationships are computed from the FULL, unfiltered domain set (never
    just the filtered slice being returned) so e.g. ``status="accepted"`` still reports an
    accepted child's proposed parent correctly, instead of silently losing the link.
    """
    status_enum = DomainStatus(status) if status is not None else None
    all_domains = list(store.iter_domains())
    by_id = {d.domain_id: d for d in all_domains}
    children_by_parent: dict[str, list[str]] = {}
    for d in all_domains:
        if d.parent_id:
            children_by_parent.setdefault(d.parent_id, []).append(d.slug)

    selected = (
        all_domains if status_enum is None else [d for d in all_domains if d.status == status_enum]
    )

    out = []
    for d in sorted(selected, key=lambda d: d.slug):
        parent = by_id.get(d.parent_id) if d.parent_id else None
        out.append(
            {
                "id": d.domain_id,
                "slug": d.slug,
                "title": d.title,
                "summary": d.summary,
                "status": d.status.value,
                "member_count": len(d.communities),
                "path_prefixes": d.path_prefixes,
                "seed_anchor_count": len(d.seed_anchors),
                "parent_slug": parent.slug if parent else None,
                "child_slugs": sorted(children_by_parent.get(d.domain_id, [])),
            }
        )
    return out


@mcp.tool(annotations=_READ_ONLY)
def list_domains(status: str | None = None) -> list[dict]:
    """List every Domain in the store — the full-listing counterpart to ``list_proposed``
    (proposed-only) and ``list_domain_candidates`` (unclaimed-only): the tool that
    actually answers "show me all domains".

    ``status``, when given, filters to one of ``"proposed"``/``"accepted"``/
    ``"dropped"``/``"superseded"``; omitted (the default) returns every domain regardless
    of status. Read-only — writes nothing, ever; safe to call repeatedly.

    Returns a list sorted by ``slug``, one dict per domain: ``{"id": str, "slug": str,
    "title": str, "summary": str, "status": str, "member_count": int, "path_prefixes":
    list[str], "seed_anchor_count": int, "parent_slug": str | None, "child_slugs":
    list[str]}``. ``member_count`` is ``len(domain.communities)`` — the current, engine-
    derived membership size (0 until the next `ratify`/`sidegraph-sync` resolves
    `path_prefixes`/`seed_anchors`, for a freshly proposed domain). ``parent_slug``/
    ``child_slugs`` reflect the FULL domain set regardless of the ``status`` filter, so a
    filtered call still reports accurate lineage.
    """
    return _list_domains_impl(_get_store(), status=status)


def _drill_down_impl(store: Store, reader, domain_slug: str) -> dict:
    """Testable core for drill_down (§5 Axis-1 operation).

    Records telemetry only on a found domain — an unknown slug renders no decision memory,
    so there is nothing to call a "show" and nothing delivered. ``decision_ids`` is popped
    before returning: it exists on the ``retrieval.drill_down`` result purely so this wrapper
    can record it, and is not part of the documented MCP tool contract (see the ``drill_down``
    tool docstring).
    The seed recorded is the domain itself (``domain:<slug>``, the same key convention
    ``domain:<slug>`` abstract entities already use elsewhere in this store) — a drill-down
    has no file/entity seeds the way get_task_context/query_decisions do.
    """
    result = _drill_down(domain_slug, store, reader)
    decision_ids = result.pop("decision_ids", [])
    if result.get("found"):
        # A drill-down delivers records, so it writes a render row like any other lookup: the
        # report counts a session's showings from that journal, and a drill-down absent from
        # it would drop out of the count as soon as the session made an ordinary lookup too.
        # It applies no budget, so the row carries the records returned and zeros elsewhere,
        # under the reserved intent that keeps those zeros out of the budget figures.
        delivered = TaskContext(
            shown_ids=list(decision_ids), selected=len(decision_ids), emitted=len(decision_ids)
        )
        _record(
            store,
            decision_ids,
            [f"domain:{domain_slug}"],
            ctx=delivered,
            intent=DRILL_DOWN_INTENT,
        )
    return result


@mcp.tool(annotations=_LOCAL_WRITE)
def drill_down(domain_slug: str) -> dict:
    """Walk one domain: its WHY-IT-EXISTS summary, its accepted subdomains (title +
    one-liner), a capped member sample (current communities ∪ path_prefixes), and its
    decisions (mistakes first) — the union, deduped, of decisions tagged to the
    ``domain:<slug>`` entity, decisions anchored to an entity in one of the domain's
    communities, AND decisions anchored to a document whose file the domain covers (so an
    imported ADR surfaces under the domain covering that doc's headings, even on a doc
    corpus where the file node hubs into a different community) — the Axis-1 counterpart to
    the flat SessionStart TOC (call this after spotting a domain there to go one level deeper).

    Returns ``{"found": True, "domain": {"slug", "title", "summary", "parent_slug",
    "status"}, "subdomains": [{"slug", "title", "summary"}, ...], "members": [rendered
    node lines], "decisions": [rendered decision lines, mistakes first]}``. ``status`` is
    the resolved domain's own status (proposed|accepted|dropped — ``find_domain_by_slug``
    never resolves to a superseded row) since a caller may drill into a not-yet-ratified
    domain.

    Unknown ``domain_slug`` -> ``{"found": False, "candidates": [...]}`` with up to 10
    currently-accepted slugs to retry with (never a guess). ``members`` is empty (with a
    ``"note"`` key) when no Graphify graph is present — everything else still returns.

    A decision line may carry a ``[drifted]`` tag (the code it is anchored to changed
    after it was captured); when at least one does, the result also carries a ``"legend"``
    key explaining the tag — verify such records against the current code and
    ``supersede_decision`` any that no longer hold. (Deliberate contract addition,
    drift→supersede wave N3.)
    """
    reader, _ = _synced_reader()
    return _drill_down_impl(_get_store(), reader, domain_slug)


def _sync_anchors_impl(store: Store, reader: GraphifyReader | None, force: bool = False) -> dict:
    """Testable core for sync_anchors (Gap 3, design/superpowers/specs/
    2026-07-10-ratification-ux-and-mcp-gaps-design.md) -- the diagnostic/heal path.

    Unlike ``_synced_reader`` (the silent lazy path every retrieval tool -- get_task_context/
    query_structure/query_decisions/drill_down -- shares: exceptions suppressed, no
    report), this never swallows a sync failure quietly. The caller builds ``reader`` via
    its own ``_load_reader()`` and hands it in explicitly; ``None`` means the graph
    couldn't be read at all, reported as an explanatory error rather than degrading.

    Runs the SAME ``sync(store, reader, force=force)`` ``sidegraph-sync`` runs (see
    ``cli.sync_main``) and hands its ``SyncReport`` to ``sync.report_as_dict`` -- the
    shared shape ``sidegraph-sync --json`` also prints (design/superpowers/specs/
    2026-07-11-ci-integrity-design.md ruling 1) -- instead of building it inline.
    """
    if reader is None:
        return {"synced": False, "error": _unreadable_graph_error(store)}

    report = sync(store, reader, force=force)
    return report_as_dict(report)


@mcp.tool(annotations=_LOCAL_WRITE)
@safe_write
def sync_anchors(force: bool = False) -> dict:
    """Re-anchor the decision store against the current graph and report exactly what
    happened -- the diagnostic/heal MCP counterpart to ``sidegraph-sync`` (Gap 3).

    WRITES: this is not read-only. It runs the same rebind pass ``sidegraph-sync``/the
    lazy ``maybe_sync`` run -- every tracked entity's tier-2 leaf bindings transition
    (live/degraded/orphaned) per the deterministic resolve ladder, community (tier-1)
    bindings get re-pointed when Leiden renumbered, and every ACCEPTED domain's
    ``communities`` are refreshed from its ``path_prefixes``/``seed_anchors``. The
    entity's own canonical DESCRIPTOR is rewritten ONLY on a "moved" rung (a unique
    name-only match after the exact match missed, AND the move independently confirmed by
    COMMITTED git history -- an unconfirmed dirty-tree hit reports "moved_uncommitted"
    instead and touches nothing; see ``SIDEGRAPH_TRUST_DIRTY_TREE`` in
    docs/guides/surviving-refactors.md for the off-by-default escape hatch); the node-id
    mapping
    (``last_seen_node_id``/``last_seen_community``/``last_seen_graph_version``, via
    ``sync.py``'s ``_adopt``) updates on that same "moved" rung AND on an exact-match
    "rebound" rung (same ``name``+``file_path`` descriptor match as last sync, but the
    resolved node id CHANGED since -- see the rebind ladder in
    docs/guides/surviving-refactors.md) -- never guessed on "ambiguous" or "orphaned".
    These are the SAME writes ``sidegraph-sync`` makes; this tool just surfaces the
    report as data instead of printing it to stdout.

    This is the diagnostic path -- unlike every retrieval tool here (get_task_context/
    query_structure/query_decisions/drill_down), which sync lazily and SILENTLY (a sync
    failure there just degrades to un-synced retrieval; nothing is ever reported), call
    this after a Graphify rebuild when you want to SEE the rebind ladder's outcomes, not
    just quietly benefit from them.

    Gated on ``graph_version`` vs the store's last-synced stamp, same as
    ``sidegraph-sync`` -- but also reruns on its own, even when the version already
    matches, the first time it's called after a canonical reload (``git pull``, merge,
    branch switch) leaves the store's volatile state cold; ``force=True`` still forces an
    unconditional rerun (e.g. after hand-editing a domain's ``path_prefixes``).

    Returns ``{"synced": bool, "from_version": str | None, "to_version": str, "counts":
    str, "repointed": int, "outcomes": [{"status", "canonical_name", "detail"}, ...],
    "stale_decisions": [...], "empty_domains": [...], "overbroad_domains": [...],
    "slug_conflicts": [...], "domains_refreshed": int, "domain_failures": [{"slug",
    "title", "error"}, ...]}``. ``synced`` is ``False`` when the pass was skipped outright
    (``graph_version`` unchanged, no ``force``, no cold-reload flag pending, no
    remembered ``moved_uncommitted`` entity to re-verify after ``HEAD`` moved, and no
    remembered Tier-1 community reconcile whose retry repaired the record or failed again;
    a retry that abstains reports nothing) -- when skipped, every OTHER field is an EMPTY default
    (``outcomes: []``, ``counts: ""``,
    ``repointed: 0``, ``stale_decisions: []``, ``empty_domains: []``, ``overbroad_domains: []``,
    ``slug_conflicts: []``, ``domains_refreshed: 0``, ``domain_failures: []``) from a
    fresh, un-run ``SyncReport(skipped=True)`` -- NOT the prior (possibly stale) report --
    so a caller must never read a skipped pass as "everything's clean"; pass
    ``force=True`` (or wait for a real graph rebuild) to get an actual report. A third,
    NARROW kind: when a prior pass left entities ``moved_uncommitted`` and git HEAD has since
    moved with the graph unchanged, only those entities are re-verified -- ``synced`` is
    ``True``, ``from_version == to_version``, ``outcomes`` covers just them, and the domain
    fields stay empty because domain membership was not recomputed. A remembered Tier-1
    community reconcile that is retried and repairs the record (a ``moved`` outcome,
    ``community rows of record <id>``) or fails again (an ``error`` outcome, kept and
    retried on the next sync) also returns ``synced: true`` with that outcome. ``outcomes``
    carries only entities worth a human's attention -- moved/moved_uncommitted/ambiguous/
    orphaned/error -- never the "unchanged"/"rebound" majority, same filter
    ``sidegraph-sync``'s own printer
    applies. ``counts`` is ``report.counts()`` rendered as a string (e.g.
    ``"{'unchanged': 3}"``), ``""`` when nothing is tracked yet. ``domain_failures`` is the
    domain-refresh analog of an ``error`` outcome -- one entry per accepted domain whose
    refresh itself raised, isolated so one broken domain never costs any other domain its
    heal; it is an attention finding for ``--check``/``report_has_findings``, unlike the
    informational ``empty_domains``/``overbroad_domains``.

    With no Graphify graph present, returns ``{"synced": False, "error": "graph not
    readable (<resolved path>)"}`` (the path is the store-anchored one; when the same
    relative value exists beside the server's cwd the message adds ``; <cwd path> exists
    beside the server's cwd -- set SIDEGRAPH_GRAPH to an absolute path to use it``)
    instead of crashing -- explanatory, not silent, since
    this IS the diagnostic tool (contrast every other tool's best-effort, no-graph-present
    degrade, which never surfaces an error at all).
    """
    return _sync_anchors_impl(_get_store(), _load_reader(), force=force)


def _verify_store_impl(store: Store) -> dict:
    """Testable core for verify_store (design/superpowers/specs/
    2026-07-11-ci-integrity-design.md ruling 2, snapshot layer). Takes the already-open
    ``store`` and reads its own ``.path`` rather than re-resolving ``SIDEGRAPH_DIR`` or
    constructing a second ``Store`` -- the MCP tool below already went through
    ``_get_store()`` for every other tool in this module, and that Store object already
    knows its own root.

    Delegates straight to ``verify.verify_snapshot`` -- a pure read over the canonical
    JSON files (never ``index.db``, never a write) -- and reshapes its
    ``list[Violation]`` into the tool's public dict contract.
    """
    violations = verify_snapshot(store.path)
    return {
        "clean": not violations,
        "violations": [{"code": v.code, "path": v.path, "detail": v.detail} for v in violations],
    }


@mcp.tool(annotations=_READ_ONLY)
def verify_store() -> dict:
    """Lint the decision store's canonical files against its write-path invariants — the
    MCP counterpart to ``sidegraph-verify`` (design/superpowers/specs/
    2026-07-11-ci-integrity-design.md ruling 2).

    The snapshot pass itself is a pure read over canonical files. This MCP wrapper obtains
    the ordinary already-open ``Store`` first, so store opening may create/rebuild
    ``index.db`` or run a supported legacy migration before verification begins.

    Checks (snapshot layer, always everything below): every hot record file parses
    against its schema; ``schema_version`` is present and known; ``valid_to >=
    valid_from``; a ``superseded`` record has a successor (its ``supersedes`` chain
    resolves); every ``supersedes`` target exists; every binding references an existing
    entity; every fact ``supports`` references an existing decision; every domain's
    ``parent_id`` names an existing domain and no parent chain loops; ULIDs are unique
    across hot files AND archive segments (byte-IDENTICAL archive-archive duplicates from
    a sanctioned cross-branch ``sidegraph-compact`` merge are exempt); archive segments
    parse as JSONL; every hot record file is named ``<its own internal id>.json``.

    Snapshot-only in v1 — this tool takes no git ref. CI users who also want the
    transition layer (classify every store file that changed vs a git ref against the
    store's OWN write rules -- what's legally mutable per record kind) should run
    ``sidegraph-verify --against <git-ref>`` on the command line instead; that layer
    needs git plumbing this MCP surface deliberately doesn't carry.

    Returns ``{"clean": bool, "violations": [{"code", "path", "detail"}, ...]}`` —
    ``violations`` is empty iff ``clean`` is ``True``.
    """
    return _verify_store_impl(_get_store())


def _anchor_leaf_summary(store: Store, name: str, file_path: str | None) -> dict | None:
    """``{"entity_id", "canonical_name", "tier": 2}`` for the Tier-2 leaf entity an anchor
    was just resolved/orphan-bound to — looked up post-write via ``resolve_descriptor`` (the
    same identity rule both ``resolve_and_bind`` and ``_bind_orphaned`` key their entity
    lookup on, INCLUDING path-less adoption), matching ``_entity_summaries``'s per-binding
    shape (``add_decision``/``add_fact``'s own vocabulary). ``None`` only if the entity
    somehow isn't findable right after being upserted (defensive; not expected in practice).

    Plain ``find_entity`` here would report ``None`` for every path-less anchor the write
    just adopted onto a carrier — the read/write split this whole change exists to close."""
    entity = store.resolve_descriptor(name, file_path)
    if entity is None:
        return None
    return {"entity_id": entity.entity_id, "canonical_name": entity.canonical_name, "tier": 2}


def _add_anchors_impl(
    store: Store,
    reader,
    record_id: str,
    anchors: list[dict],
) -> dict:
    """Testable core for add_anchors (design/superpowers/specs/
    2026-07-11-ci-integrity-design.md ruling 3): append bindings to an EXISTING decision
    or fact — generalizes ``_bind_fact_anchors``'s resolve-or-orphan ladder (never the
    silent no-op ``_resolve_anchors`` gives a no-reader ``add_decision`` call) to either
    record kind.

    Routing: ``store.get_decision(record_id)``, else ``store.get_fact(record_id)``, else
    an error dict — never a raised exception, never a guess at which kind an id belongs
    to. Anchors are validated up front via ``_validate_anchors``, before any
    binding is written — the same atomic-batch guarantee ``add_decision``/``add_fact``
    give: either every anchor in the call is legal and all of them bind, or nothing does.

    This is a BINDINGS-ONLY write: only ``bindings/<record_id>.json`` (and any newly
    minted ``entities/<id>.json``) changes — the decision/fact's own record file is never
    touched, so this stays legal under verify's transition rules (a record's content
    fields are otherwise immutable outside real status/``valid_to`` transitions).

    Per anchor: resolved against the graph via ``resolve_and_bind`` when ``reader`` is
    present (same ladder every other anchoring tool here uses) — an ambiguous name is
    reported, never guessed, and creates no Tier-2 leaf; a resolved name lands a live
    Tier-2 leaf (+ Tier-1 domain/community). With no reader, or a name that resolves to
    nothing, the anchor still binds — an orphaned Tier-2 leaf via ``_bind_orphaned`` — so
    a re-anchor request is never silently dropped for lack of a graph.
    """
    if store.get_decision(record_id) is None and store.get_fact(record_id) is None:
        return {"error": "unknown record"}
    # A bindings file the last reload left out (a merge conflict, say) cannot be written to:
    # ``add_binding`` refuses. Say so BEFORE resolving the anchors, whose entities are minted
    # and committed one mutation at a time and would stay behind as orphans.
    # see design/superpowers/specs/2026-10-03-store-survives-a-bad-file-design.md D5
    if store.is_skipped("bindings", record_id):
        raise ValueError(skipped_refusal_text("bindings", record_id))
    _validate_anchors(anchors)

    bound: list[dict] = []
    orphaned: list[dict] = []
    ambiguous: list[dict] = []
    for raw in anchors or []:
        name = raw.get("name")
        if not name:
            continue
        file_path = raw.get("file_path")
        relation = raw.get("relation")
        ref = Descriptor(name=name, file_path=file_path)
        if reader is not None:
            result = resolve_and_bind(record_id, ref, reader, store, relation=relation)
            if result.status == "ambiguous":
                ambiguous.append(
                    {"name": name, "reason": "ambiguous", "candidates": result.candidates[:5]}
                )
                continue
            summary = _anchor_leaf_summary(store, name, file_path)
            if summary is not None:
                (bound if result.status == "resolved" else orphaned).append(summary)
        else:
            _bind_orphaned(record_id, ref, store, relation=relation)
            summary = _anchor_leaf_summary(store, name, file_path)
            if summary is not None:
                orphaned.append(summary)

    return _with_commit_hint(
        store,
        {"record_id": record_id, "bound": bound, "orphaned": orphaned, "ambiguous": ambiguous},
    )


@mcp.tool(annotations=_LOCAL_WRITE)
@safe_write
def add_anchors(record_id: str, anchors: list[dict]) -> dict:
    """Append bindings to an EXISTING decision or fact — in-place re-anchoring for the
    triage flow (design/superpowers/specs/2026-07-11-ci-integrity-design.md ruling 3).

    Use this when triage (after ``sync_anchors``) finds "code moved, decision still
    valid": it heals an orphaned/stale anchor in place instead of forcing a
    content-free ``supersede_decision``/``supersede_fact`` — which would pollute history
    with a successor that says nothing new. Reach for supersede instead when the CONTENT
    actually changed (the choice/rejected/consequences text), not just where the code
    that decision is about now lives.

    ``anchors`` is the same ``{"name", "file_path"?, "relation"?}`` ref shape every other
    anchoring tool here takes. This is BINDINGS-ONLY: the decision/fact's own record file
    is never rewritten — only its bindings (and any newly minted entity) — so append-only
    history and verify's transition rules stay intact.

    Routing tries ``record_id`` as a decision, then as a fact; an id that resolves to
    neither writes nothing and returns ``{"error": "unknown record"}`` (never a
    guess). A record whose bindings file the store could not read (it is left out of the
    index until restored or fixed) is refused before any entity is minted. MCP omits raw
    file names and causes; use ``sidegraph-doctor`` to identify the binding file to repair.
    Anchors are validated before anything is written — an invalid ``relation``, a
    non-string ``name``/``file_path``, or a list with no named anchor raises, same as
    ``add_decision``/``add_fact``.

    Returns ``{"record_id", "bound": [...], "orphaned": [...], "ambiguous": [...]}`` —
    ``bound``/``orphaned`` entries are ``{"entity_id", "canonical_name", "tier": 2}``
    entity summaries (same shape ``add_decision``/``add_fact`` return per binding):
    ``bound`` for anchors that resolved to exactly one live graph node, ``orphaned`` for
    anchors bound with no graph present or that resolved to nothing (never dropped either
    way). ``ambiguous`` is ``{"name", "reason": "ambiguous", "candidates"}`` (capped at 5)
    for anchor names that matched more than one graph node — no leaf created, the same
    per-anchor feedback ``add_decision``'s ``anchors_skipped`` gives.
    """
    return _add_anchors_impl(_get_store(), _load_reader(), record_id, anchors)


# Capture registration-time identities: rebinding a familiar name is not a read exemption.
_REGISTERED_CALLABLES = {
    name: globals()[name] for name in _READ_TOOLS | _LAZY_READ_TOOLS | _WRITE_TOOLS
}


def main() -> None:
    """Console-script entry point (``sidegraph-mcp``).

    ``-h``/``--help`` prints the usage and exits before the server starts; the transport's
    stdin loop would otherwise wait on it. Other arguments are still ignored.
    """
    if any(a in ("-h", "--help") for a in sys.argv[1:]):
        sys.stdout.write(
            "usage: sidegraph-mcp [-h]\n"
            "The Sidegraph decision MCP server, speaking MCP over stdio. An MCP client\n"
            "starts it, not you. Configuration is by environment variable; see\n"
            "docs/reference/configuration.md.\n"
        )
        raise SystemExit(0)
    mcp.run()


if __name__ == "__main__":
    main()
