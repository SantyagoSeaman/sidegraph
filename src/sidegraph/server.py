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
import threading
from datetime import UTC, datetime, timedelta
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path
from typing import Literal, cast, get_args

from fastmcp import FastMCP

from .anchoring import entity_summaries as _entity_summaries
from .anchoring import orphan_reason, resolve_and_bind
from .capture import (
    AnchorDraft,
    RatifyPolicy,
    _bind_orphaned,
    _capture_commit,
    _resolves_to_live_decision,
    _session_id_fallback,
    format_communities_sample,
    format_fact_proposal,
    format_path_prefixes,
    format_proposal,
    format_seed_anchors_sample,
    parse_ratify_policy,
    propose,
    propose_facts,
    redact,
)
from .capture import propose_domains as _propose_domain_drafts
from .config import TELEMETRY_SESSION_KEY, resolve_store_path
from .domains import DEFAULT_CANDIDATE_LIMIT, collect_domain_candidates, community_group_path
from .engine.reader import GraphifyReader
from .retrieval import TOC_CACHE_KEY, RetrievalBudget, Seed, build_toc, proposal_surfaces
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
    slugify,
)
from .store import VOLATILE_STALE_KEY, Store
from .sync import activate_accepted_domain, maybe_sync, report_as_dict, sync
from .verify import verify_snapshot


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

# One process-wide store, created LAZILY on first use -- never as a side effect of merely
# importing this module. A bare eager `_store = Store(...)` at import time used to
# materialize a stray store (e.g. "sidegraph.db" in whatever the current working directory
# happened to be) just from `import sidegraph.server`, which is exactly the kind of
# import-time side effect a library module must not have. Path resolution is shared with
# the CLI and the Claude Code hooks via config.resolve_store_path (SIDEGRAPH_DIR primary,
# SIDEGRAPH_DB honored for back-compat, default ".sidegraph") -- see
# docs/reference/configuration.md.
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
                _store = Store(resolve_store_path())
    return _store


def _graph_path() -> str:
    """The graph path ``_load_reader()`` resolves and reads from -- factored out so a
    caller that gets back ``None`` (a bad/missing graph) can still explain WHERE it looked
    (``sync_anchors``'s unreadable-graph error), since ``_load_reader()`` itself degrades
    a bad path to a bare ``None`` with no path attached."""
    return os.environ.get("SIDEGRAPH_GRAPH", "graphify-out/graph.json")


def _load_reader() -> GraphifyReader | None:
    """Best-effort reader over $SIDEGRAPH_GRAPH (or graphify-out/graph.json). None if absent."""
    try:
        return GraphifyReader(_graph_path())
    except Exception:
        return None


# The only legal AnchorBinding.relation values (Relation is a Literal, not an enum) — used
# to validate `anchors[i]["relation"]` BEFORE any store write (see _validate_anchor_relations).
_VALID_RELATIONS = frozenset(get_args(Relation))


def _coerce_tags(tags: list[str] | str | None) -> list[str]:
    """Liberal-input tags: agents routinely pass a bare (often comma-separated) string on
    the first try — accept it instead of failing schema validation and forcing a retry
    (live finding, manual test 2026-07-08). A string splits on commas; blanks drop."""
    if tags is None:
        return []
    if isinstance(tags, str):
        return [part.strip() for part in tags.split(",") if part.strip()]
    return list(tags)


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


def _validate_anchor_relations(anchors: list[dict] | None) -> None:
    """Raise before ANY write when an anchor's `relation` isn't a legal value (M2 review
    fold-in): without this, an invalid relation only surfaced when ``resolve_and_bind``
    constructed the offending ``AnchorBinding`` — by which point the decision row (and any
    earlier anchors in the same call) were already written, leaving a half-anchored
    decision behind. Validating the whole batch up front keeps the write atomic: either
    every anchor is legal and the decision writes with all of them, or nothing writes at
    all.
    """
    for raw in anchors or []:
        relation = raw.get("relation")
        if relation is not None and relation not in _VALID_RELATIONS:
            raise ValueError(
                f"invalid relation {relation!r} for anchor {raw.get('name')!r}: must be "
                f"one of {sorted(_VALID_RELATIONS)}"
            )


def _require_fact_reachability(store, anchors: list[dict] | None, supports: list[str]) -> None:
    """Anchorless fact-write gate (design D8): a fact with no anchors must have at least one
    ``supports`` id resolving to a LIVE (accepted/proposed) decision, or it is unreachable the
    moment it lands — exactly the shape doctor's tightened ``dangling-record`` check (D4/D5/
    D6) would flag. A fact WITH an anchor is untouched.

    Raise before ANY write, same discipline as ``_validate_anchor_relations`` above. Shared by
    ``_add_fact_impl`` and ``_supersede_fact_impl``'s no-anchors path — the two human-asked
    fact-writing entry points — mirroring ``capture.py``'s own anchorless-fact gate for the
    agent-initiated path (``_resolves_to_live_decision``, imported from there, is the one
    shared definition of "live" all three write paths use).
    """
    if anchors:
        return
    if not _resolves_to_live_decision(store, supports):
        raise ValueError(
            "anchorless fact has no live supporting decision — add an anchor, or "
            "re-point supports at the successor of a superseded/rejected/deprecated one"
        )


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
    _validate_anchor_relations(anchors)
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

    anchors_skipped, anchors_orphaned = _resolve_anchors(
        decision.id, anchors, reader, store, initiative=initiative
    )
    for tag in _coerce_tags(tags):
        scrubbed, n = redact(tag)
        redactions += n
        slug = slugify(scrubbed)
        # Same rule as the propose pipeline: a tag whose entire text WAS the secret
        # slugifies to exactly "redacted" -- skip it, never mint a meaningless
        # `tag:redacted` entity.
        if not slug or slug == "redacted":
            continue
        tag_entity = store.get_or_create_abstract_entity(f"tag:{slug}")
        store.add_binding(
            AnchorBinding(
                record_id=decision.id,
                entity_id=tag_entity.entity_id,
                tier=0,
            )
        )
    bindings = store.bindings_for_record(decision.id)
    return {
        "id": decision.id,
        "status": decision.status.value,
        "bindings": len(bindings),
        "entities": _entity_summaries(store, bindings),
        "anchors_skipped": anchors_skipped,
        "anchors_orphaned": anchors_orphaned,
        "redactions": redactions,
    }


def _resolve_anchors(
    record_id: str,
    anchors: list[dict] | None,
    reader,
    store,
    initiative: str | None = None,
) -> tuple[list[dict], list[dict]]:
    """Resolve+bind every anchor ref, returning ``(ambiguous, orphaned)`` as feedback.

    ``ambiguous`` (Gate-5 finding S3) is
    ``[{"name", "reason": "ambiguous", "candidates": [...capped 5]}]`` — matched more than
    one node, so NO leaf was created.

    ``orphaned`` is the entity summary of every leaf bound for an anchor that resolved to
    NOTHING. That leaf IS written (``anchoring.resolve_and_bind``: "created when resolved
    or unresolved"), deliberately — but it is dead on arrival: ``valid_decisions_for_entity``
    skips orphaned bindings, so no retrieval path, no ``drill_down`` and no PreToolUse nudge
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
                initiative=initiative,
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


@mcp.tool
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
    and multi-anchored (leaf + domain/community [+ initiative]). Anchoring is best-effort:
    with no graph present, the decision still writes.

    Every text field (title/context/choice/rejected/consequences, and tag text before
    slugification) is redacted first — same secret patterns as the propose/import
    pipelines; the scrubbed text is the only text that reaches the repo-committed store.

    ``tags`` are free-form labels — a bare comma-separated string is accepted too —
    slugified (lowercase, spaces->'-', ``[a-z0-9-]`` only) into durable ``tag:<slug>``
    entities (tier-0, many-to-many — a decision can carry several, and
    ``get_entity_history`` finds it via any of them, same as an initiative).
    ``layer`` optionally marks the decision "business" or "technical" — a filter axis for
    mixed corpora.

    Returns ``{"id", "status", "bindings", "entities", "anchors_skipped", "redactions"}``
    (``redactions`` = secret replacements made across all text fields) — ``entities`` is
    ``[{"entity_id", "canonical_name", "tier"}, ...]``, one per binding created, so a caller
    can chain straight into ``find_entity``/``get_entity_history`` without touching the store.
    ``anchors_skipped`` is ``[{"name", "reason": "ambiguous", "candidates"}, ...]`` — the
    anchors whose name matched more than one graph node (candidates capped at 5), so no
    precise Tier-2 leaf was created for them; empty when every anchor resolved cleanly or no
    graph is present.
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
    return {
        "id": replacement.id,
        "supersedes": old_decision_id,
        "bindings": len(bindings),
        "entities": _entity_summaries(store, bindings),
        "anchors_skipped": anchors_skipped,
        "anchors_orphaned": anchors_orphaned,
        "redactions": redactions,
    }


@mcp.tool
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

    ``session_id``/``author`` (optional) and ``source`` (default ``"human"``, this tool's
    historical hardcoded value — pass ``"agent"`` when an agent calls this itself, e.g. off
    a ``neighbors`` or ``(id: ...)`` hint) are stamped onto the successor's provenance,
    alongside ``graph_version`` and a best-effort capture-time ``commit`` (same fields
    ``propose_decisions`` stamps).

    Returns ``{"id", "supersedes", "bindings", "entities", "anchors_skipped",
    "redactions"}`` — ``anchors_skipped`` is ``[{"name", "reason": "ambiguous",
    "candidates"}, ...]``, the same per-anchor feedback ``add_decision`` returns
    (candidates capped at 5): populated
    only on the explicit-``anchors`` path (an anchor whose name matched more than one graph
    node got no precise Tier-2 leaf), always ``[]`` when ``anchors`` is omitted since
    inheritance never resolves against the graph.
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
    _validate_anchor_relations(anchors)
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
    return {
        "id": fact.id,
        "statement": fact.statement,
        "status": fact.status.value,
        "redactions": redactions,
        "entities": _entity_summaries(store, bindings),
        "anchors_skipped": anchors_skipped,
        "anchors_orphaned": anchors_orphaned,
    }


@mcp.tool
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
    list of decision ids this fact informed (each must already exist — raises
    ``ValueError`` otherwise). ``anchors`` is the same ``{"name", "file_path", "relation"?}``
    ref shape ``add_decision`` takes; resolved against the current Graphify graph and bound
    when a graph is present. With no graph, an anchor still gets an ORPHANED Tier-2 leaf
    (unlike ``add_decision``, which silently skips anchors with no reader) — a fact must
    never write unreachable, so the binding heals once a graph exists.

    Every text field (statement/source) is redacted first, same secret patterns as
    ``add_decision``'s.

    Returns ``{"id", "statement", "status", "redactions", "entities", "anchors_skipped"}``
    — ``entities`` is ``[{"entity_id", "canonical_name", "tier"}, ...]``, one per binding
    created; ``anchors_skipped`` is ``[{"name", "reason": "ambiguous", "candidates"}, ...]``
    (candidates capped at 5) — populated only when a graph is present and an anchor's name
    matched more than one node, since there is nothing to be ambiguous against otherwise.
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
    predecessor = store.get_fact(old_fact_id)
    if predecessor is None:
        raise ValueError(f"unknown fact {old_fact_id!r}")
    _validate_anchor_relations(anchors)
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
    return {
        "id": replacement.id,
        "statement": replacement.statement,
        "status": replacement.status.value,
        "redactions": redactions,
        "entities": _entity_summaries(store, bindings),
        "anchors_skipped": anchors_skipped,
        "anchors_orphaned": anchors_orphaned,
        "supersedes": old_fact_id,
    }


@mcp.tool
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
    "supersedes"}`` — same shape as ``add_fact``'s plus ``supersedes`` (the predecessor's
    id).
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


@mcp.tool
def retrieve_decisions(include_superseded: bool = False) -> list[dict]:
    """Return decisions from the store, mistakes/gotchas ranked first.

    The default listing excludes superseded and rejected (dropped) records; pass
    ``include_superseded=True`` to see that history too.
    """
    return _retrieve_decisions_impl(_get_store(), include_superseded=include_superseded)


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


@mcp.tool
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
    return {
        "found": True,
        "entity_id": entity.entity_id,
        "canonical_name": entity.canonical_name,
        "descriptor": entity.descriptor.model_dump() if entity.descriptor else None,
        "last_seen_node_id": entity.last_seen_node_id,
        "bindings": [
            {
                "record_id": b.record_id,
                "record_type": "fact" if store.get_fact(b.record_id) else "decision",
                "tier": b.tier,
                "status": b.status,
            }
            for b in bindings
        ],
    }


@mcp.tool
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
    (``record_type`` is ``"decision"`` or ``"fact"``) when resolved to exactly one entity;
    ``{"found": False}`` when nothing matches; or
    ``{"found": False, "candidates": [{"entity_id", "canonical_name", "file_path"}, ...]}``
    when the name alone is ambiguous.
    """
    return _find_entity_impl(_get_store(), name, file_path)


def _get_entity_history_impl(store: Store, entity_id: str) -> list[dict]:
    """Testable core for get_entity_history (Gap 2, design/superpowers/specs/
    2026-07-10-ratification-ux-and-mcp-gaps-design.md): every decision AND fact anchored
    to ``entity_id``, newest first.

    Per binding: try ``get_decision(record_id)``, else ``get_fact(record_id)``, else skip
    (an unknown record kind stays skipped, same as before this wave). Previously this only
    ever tried ``get_decision`` -- a fact-only binding vanished from history with no trace.
    Every returned dict gains ``"record_type": "decision" | "fact"`` (additive -- existing
    consumers keyed on the pre-existing fields are unaffected); the merged list stays
    sorted ``valid_from`` desc, exactly as before.
    """
    bindings = store.bindings_for_entity(entity_id)
    records: list[tuple[str, Decision | Fact]] = []
    for b in bindings:
        decision = store.get_decision(b.record_id)
        if decision is not None:
            records.append(("decision", decision))
            continue
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


@mcp.tool
def get_entity_history(entity_id: str) -> list[dict]:
    """Return every decision AND fact anchored to a given entity, newest first.

    Per binding, tries a decision lookup then a fact lookup (an unknown record kind is
    skipped, as before). Every dict now carries ``"record_type": "decision" | "fact"`` so
    a caller can tell them apart without re-deriving it -- facts used to be silently
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


def _get_task_context_impl(
    store,
    reader,
    files: list[str] | None,
    entities: list[dict] | None,
    structure_budget: int,
    memory_budget: int,
) -> str:
    """Testable core: build seeds, run retrieval, return the rendered slice."""
    seeds = _seeds_from_args(files, entities)
    ctx = _retrieve(seeds, store, reader, RetrievalBudget(structure_budget, memory_budget))
    _record(store, ctx.shown_ids, [s.file_path for s in seeds if s.file_path])
    return ctx.render()


def _synced_reader() -> GraphifyReader | None:
    """Best-effort reader with a lazy sync attempt — shared by every retrieval-facing tool
    (get_task_context/query_structure/query_decisions/drill_down). Sync failure degrades
    to un-synced retrieval, never an error."""
    reader = _load_reader()
    with contextlib.suppress(Exception):
        maybe_sync(_get_store(), reader)
    return reader


def _get_task_context_with_sync(
    files: list[str] | None = None,
    entities: list[dict] | None = None,
    structure_budget: int = 4000,
    memory_budget: int = 6000,
) -> str:
    """Tool-shell core: lazy sync (best-effort), then retrieval."""
    reader = _synced_reader()
    return _get_task_context_impl(
        _get_store(), reader, files, entities, structure_budget, memory_budget
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
    _record(store, ctx.shown_ids, [s.file_path for s in seeds if s.file_path])
    return ctx.render(include_structure=False)


@mcp.tool
def get_task_context(
    files: list[str] | None = None,
    entities: list[dict] | None = None,
    structure_budget: int = 4000,
    memory_budget: int = 6000,
) -> str:
    """Task-aware context for the files/entities you're working on, mistakes ranked first.

    ``files`` are repo-relative paths; ``entities`` are ``{"name": ..., "file_path": ...}``
    refs. Returns a compact slice: known mistakes/gotchas, then decisions, then a structural
    map, then related decisions. Best-effort — degrades if the graph or store is absent.
    """
    return _get_task_context_with_sync(files, entities, structure_budget, memory_budget)


@mcp.tool
def query_structure(
    files: list[str] | None = None,
    entities: list[dict] | None = None,
    budget_chars: int = 4000,
) -> str:
    """The structural-map half of ``get_task_context`` alone (§5 FR8.2 thin tool) — a cheap
    follow-up once you already have decision memory and just need the code map.

    Same ``files``/``entities`` shape as ``get_task_context``. Never crashes: with no
    Graphify graph present, returns an explanatory note instead of a map.
    """
    return _query_structure_impl(_get_store(), _synced_reader(), files, entities, budget_chars)


@mcp.tool
def query_decisions(
    files: list[str] | None = None,
    entities: list[dict] | None = None,
    budget_chars: int = 6000,
) -> str:
    """The decision-memory half of ``get_task_context`` alone (§5 FR8.2 thin tool):
    mistakes, decisions, related — no structural map.

    Same ``files``/``entities`` shape as ``get_task_context``. Best-effort like every other
    tool here: degrades gracefully with no graph present (global-scope decisions still
    surface). This tool takes no ``structure_budget``, but internally the "related"
    (peripheral) bucket is still gathered by walking the structural subgraph with
    ``RetrievalBudget``'s DEFAULT ``structure_chars`` (the map itself is discarded — only
    the peripheral entities it surfaces feed decision ranking).
    """
    return _query_decisions_impl(_get_store(), _synced_reader(), files, entities, budget_chars)


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
    if datetime.now(UTC) - written > _SESSION_KEY_TTL:
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
        if rel == os.curdir or rel.startswith(os.pardir):
            continue
        out.append(rel)
    return out


def _record(store: Store, record_ids: list[str], seeds: list[str]) -> None:
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
    """
    if not _telemetry_enabled():
        return
    normalized_seeds = seeds
    with contextlib.suppress(Exception):
        normalized_seeds = _normalized_seeds(store, seeds)
    with contextlib.suppress(Exception):
        store.record_retrieval(record_ids, normalized_seeds)
    with contextlib.suppress(Exception):
        session_id = _session_key(store)
        if session_id is None:
            return
        shows = [
            (record_id, path)
            for record_id in dict.fromkeys(record_ids)
            for path in _anchor_paths(store, record_id)
        ]
        journal_seeds = [s for s in normalized_seeds if not s.startswith("domain:")]
        store.record_retrieval_events(session_id, journal_seeds, shows)


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
    return results


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


@mcp.tool
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
    The pipeline redacts secrets (including tag text), validates, dedups, writes as
    status=proposed (ratified later by a human — or at write time by an opt-in
    SIDEGRAPH_RATIFY_POLICY when the draft is eligible), and anchors best-effort —
    tags/layer/per-anchor relation carry through unchanged to ratification.

    Each result carries ``"anchors_skipped": [{"name", "reason": "ambiguous", "candidates"}]``
    — anchors whose name matched more than one graph node, so no precise Tier-2 leaf was
    created for them (capped at 5); empty when every anchor resolved cleanly.

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


@mcp.tool
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
        except ValueError as e:
            return f"error: {e}", []
    if store.get_fact(id_) is not None:
        try:
            if action == "accept":
                store.ratify_fact(id_)
            else:
                store.drop_fact(id_)
            return ("accepted" if action == "accept" else "dropped"), []
        except ValueError as e:
            return f"error: {e}", []
    if store.get_domain(id_) is not None:
        result = (
            store.ratify_domains(accept=[id_])
            if action == "accept"
            else store.ratify_domains(drop=[id_])
        )
        return result[id_], []
    return f"error: unknown id {id_!r} (not a pending decision, fact, or domain)", []


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
                    prefixes = ", ".join(repr(p) for p in domain.path_prefixes)
                    out[id_] += (
                        f" (path rule too broad: {prefixes} match "
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
        except ValueError as e:
            out[did] = f"error: {e}"
    for did in drop or []:
        if did in out:
            out[did] = f"{out[did]} (drop ignored)"
            continue
        try:
            _decision, _cascaded = store.drop(did)
            out[did] = "dropped"
        except ValueError as e:
            out[did] = f"error: {e}"
    return out


@mcp.tool
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


@mcp.tool
def ratify_decisions(
    accept: list[str] | None = None, drop: list[str] | None = None
) -> dict[str, str]:
    """Deprecated alias for ``ratify`` (kept for one release; despite the name, it now
    covers facts and domains too — identical behavior to ``ratify``). Prefer ``ratify``."""
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
    parent_id = None
    if parent_slug is not None:
        parent = store.find_domain_by_slug(parent_slug)
        if parent is None:
            raise ValueError(f"parent_slug {parent_slug!r} does not resolve to any domain")
        parent_id = parent.domain_id

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
    return {"domain_id": domain.domain_id, "status": domain.status.value}


@mcp.tool
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

    Returns ``{"domain_id", "status"}``.
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
    old = _resolve_domain_ref(store, old_slug_or_id)
    if old is None:
        raise ValueError(f"old_slug_or_id {old_slug_or_id!r} does not resolve to any domain")

    parent_id = None
    if parent_slug is not None:
        parent = store.find_domain_by_slug(parent_slug)
        if parent is None:
            raise ValueError(f"parent_slug {parent_slug!r} does not resolve to any domain")
        parent_id = parent.domain_id

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
    return {
        "domain_id": result.domain_id,
        "status": result.status.value,
        "supersedes": old.domain_id,
    }


@mcp.tool
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

    Returns ``{"domain_id": str, "status": str, "supersedes": str}`` — ``status`` is
    always ``"proposed"``, ``domain_id`` is the successor's, ``supersedes`` is the
    predecessor's resolved ``domain_id``.
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
    return [r.model_dump(mode="json") for r in results]


@mcp.tool
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


@mcp.tool
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


@mcp.tool
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
    so there is nothing to call a "show". ``decision_ids`` is popped before returning: it
    exists on the ``retrieval.drill_down`` result purely so this wrapper can record it, and
    is not part of the documented MCP tool contract (see the ``drill_down`` tool docstring).
    The seed recorded is the domain itself (``domain:<slug>``, the same key convention
    ``domain:<slug>`` abstract entities already use elsewhere in this store) — a drill-down
    has no file/entity seeds the way get_task_context/query_decisions do.
    """
    result = _drill_down(domain_slug, store, reader)
    decision_ids = result.pop("decision_ids", [])
    if result.get("found"):
        _record(store, decision_ids, [f"domain:{domain_slug}"])
    return result


@mcp.tool
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
    return _drill_down_impl(_get_store(), _synced_reader(), domain_slug)


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
        return {"synced": False, "error": f"graph not readable ({_graph_path()})"}

    report = sync(store, reader, force=force)
    return report_as_dict(report)


@mcp.tool
def sync_anchors(force: bool = False) -> dict:
    """Re-anchor the decision store against the current graph and report exactly what
    happened -- the diagnostic/heal MCP counterpart to ``sidegraph-sync`` (Gap 3).

    WRITES: this is not read-only. It runs the same rebind pass ``sidegraph-sync``/the
    lazy ``maybe_sync`` run -- every tracked entity's tier-2 leaf bindings transition
    (live/degraded/orphaned) per the deterministic resolve ladder, community (tier-1)
    bindings get re-pointed when Leiden renumbered, and every ACCEPTED domain's
    ``communities`` are refreshed from its ``path_prefixes``/``seed_anchors``. The
    entity's own canonical DESCRIPTOR is rewritten ONLY on a "moved" rung (a unique
    name-only match after the exact match missed); the node-id mapping
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
    (``graph_version`` unchanged, no ``force``, and no cold-reload flag pending) -- when
    skipped, every OTHER field is an EMPTY default (``outcomes: []``, ``counts: ""``,
    ``repointed: 0``, ``stale_decisions: []``, ``empty_domains: []``, ``overbroad_domains: []``,
    ``slug_conflicts: []``, ``domains_refreshed: 0``, ``domain_failures: []``) from a
    fresh, un-run ``SyncReport(skipped=True)`` -- NOT the prior (possibly stale) report --
    so a caller must never read a skipped pass as "everything's clean"; pass
    ``force=True`` (or wait for a real graph rebuild) to get an actual report. ``outcomes``
    carries only entities worth a human's attention -- moved/ambiguous/orphaned/error --
    never the "unchanged"/"rebound" majority, same filter ``sidegraph-sync``'s own printer
    applies. ``counts`` is ``report.counts()`` rendered as a string (e.g.
    ``"{'unchanged': 3}"``), ``""`` when nothing is tracked yet. ``domain_failures`` is the
    domain-refresh analog of an ``error`` outcome -- one entry per accepted domain whose
    refresh itself raised, isolated so one broken domain never costs any other domain its
    heal; it is an attention finding for ``--check``/``report_has_findings``, unlike the
    informational ``empty_domains``/``overbroad_domains``.

    With no Graphify graph present, returns ``{"synced": False, "error": "graph not
    readable (<resolved path>)"}`` instead of crashing -- explanatory, not silent, since
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


@mcp.tool
def verify_store() -> dict:
    """Lint the decision store's canonical files against its write-path invariants — the
    MCP counterpart to ``sidegraph-verify`` (design/superpowers/specs/
    2026-07-11-ci-integrity-design.md ruling 2).

    READ-ONLY: this never writes anything, never touches ``index.db``, and never migrates
    a legacy store -- it opens the canonical JSON files directly, the exact same pure-read
    pass ``sidegraph-verify`` runs without ``--against``.

    Checks (snapshot layer, always everything below): every hot record file parses
    against its schema; ``schema_version`` is present and known; ``valid_to >=
    valid_from``; a ``superseded`` record has a successor (its ``supersedes`` chain
    resolves); every ``supersedes`` target exists; every binding references an existing
    entity; every fact ``supports`` references an existing record; ULIDs are unique
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
    to. Relations are validated up front via ``_validate_anchor_relations``, before any
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
        return {"error": f"unknown record {record_id!r}"}
    _validate_anchor_relations(anchors)

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

    return {"record_id": record_id, "bound": bound, "orphaned": orphaned, "ambiguous": ambiguous}


@mcp.tool
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
    neither writes nothing and returns ``{"error": "unknown record '<id>'"}`` (never a
    guess). Relations are validated before anything is written — an invalid ``relation``
    raises, same as ``add_decision``/``add_fact``.

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


def main() -> None:
    """Console-script entry point (``sidegraph-mcp``)."""
    mcp.run()


if __name__ == "__main__":
    main()
