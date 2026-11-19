"""Read-only Portal routes over the store and derived index (D4–D6, D9)."""

import contextlib
import hashlib
import heapq
import os
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from sidegraph.config import (
    DEFAULT_GRAPH,
    default_graph_path,
    graph_path_for_store,
    repository_root,
)
from sidegraph.engine.reader import GraphifyReader
from sidegraph.retrieval import (
    _domain_covers_file,
    _entity_in_domain,
    _entity_pool,
    drifted_record_ids,
)
from sidegraph.schema import Decision, DecisionStatus, Domain, DomainStatus, Fact
from sidegraph.server import (
    QueryMatch,
    _anchors_holding,
    _in_history,
    _list_facts_impl,
    _retrieve_decisions_impl,
    _searched_text,
    match_query,
)
from sidegraph.store import Store
from sidegraph.store_format import UnencodableBody, encode_body
from sidegraph.store_layout import VOLATILE_STALE_KEY
from sidegraph.store_view import effective_bodies, inventory_of
from sidegraph.sync import _settled, membership_stamp
from sidegraph.viz.model import build_graph
from sidegraph.viz.render import to_json

from . import contract, snippets
from .context import PortalContext, _same_graph_path
from .status_words import word_for
from .writes import _exclusive_evidence

# The portal draws a store of a few thousand nodes whole; the static export keeps its 800.
GRAPH_MAX_NODES = 3000


def _record_domains(store: Store, reader: GraphifyReader | None) -> dict[str, list[str]]:
    """Join the same domain anchors and member entities as drill_down (D4)."""
    domains = list(store.iter_domains(status=DomainStatus.ACCEPTED))
    if not domains:
        return {}
    by_record: dict[str, set[str]] = defaultdict(set)
    pool = _entity_pool(store, reader)
    for domain in domains:
        member_ids = store.domain_member_ids(domain.domain_id)
        communities = set(domain.communities)
        tagged = store.find_abstract_entity(f"domain:{domain.slug}")
        entity_ids = {tagged.entity_id} if tagged is not None else set()
        entity_ids.update(
            e.entity_id
            for e in pool.concrete
            if _entity_in_domain(e, communities, member_ids, pool)
        )
        if reader is not None:
            entity_ids.update(
                e.entity_id
                for e in pool.documents
                if e.descriptor is not None
                and e.descriptor.file_path is not None
                and _domain_covers_file(domain, reader, e.descriptor.file_path, member_ids)
            )
        for entity_id in entity_ids:
            for binding in store.bindings_for_entity_group(entity_id):
                if binding.status in ("live", "degraded"):
                    by_record[binding.record_id].add(domain.slug)
    # The records example lists unanchored evidence under its supporting decision's domain.
    for fact in store.iter_facts():
        for supporter in fact.supports:
            if supporter in by_record:
                by_record[fact.id].update(by_record[supporter])
    return {record_id: sorted(slugs) for record_id, slugs in by_record.items()}


def _search_pool(store: Store) -> list[dict]:
    return [
        *_retrieve_decisions_impl(store, include_superseded=True, all_proposals=True),
        *_list_facts_impl(store, include_superseded=True, all_proposals=True),
    ]


def _history_requested(request: Request) -> bool:
    return request.query_params.get("history") in ("1", "true")


def _query_matches(
    store: Store, pool: list[dict], query: str, *, include_history: bool
) -> tuple[list[tuple[str, list[str]]], QueryMatch]:
    history = []
    if not include_history:
        history = [row for row in pool if _in_history(row)]
        pool = [row for row in pool if not _in_history(row)]
    match = match_query(store, pool, history, query)
    return [(record_id, match.words) for record_id in match.full] or match.partial, match


def _row(
    store: Store, row: dict, domains: dict[str, list[str]], *, index_cold: bool
) -> contract.RecordRow:
    if "domain_id" in row:
        record_id, record_type = row["domain_id"], "domain"
    else:
        record_id = row["id"]
        record_type = "decision" if "kind" in row else "fact"
    bindings = store.bindings_for_record(record_id)
    return contract.RecordRow.model_validate(
        {
            "id": record_id,
            "type": record_type,
            "kind": row.get("kind"),
            "status": row["status"],
            "domains": [row["slug"]] if record_type == "domain" else domains.get(record_id, []),
            "title": row.get("title", row.get("statement")),
            "layer": row.get("layer"),
            "anchor_tiers": [b.tier for b in bindings],
            "anchor_statuses": [word_for(b.status, index_cold=index_cold) for b in bindings],
        }
    )


def _source_prefix(ctx: PortalContext, root: Path | None) -> Path:
    # Relative configured graphs, including borrowed ones, describe the store's project.
    # An explicit graph elsewhere continues to use checkout-relative paths.
    if root is None or ctx.graph_path is None:
        return Path()
    graph_root = ctx.borrowed_from or root
    graph = Path(os.path.abspath(ctx.graph_path))
    output_directory = Path(DEFAULT_GRAPH).parent.name
    if graph.parent.name == output_directory and graph.parent.parent.is_relative_to(graph_root):
        return graph.parent.parent.relative_to(graph_root)
    with contextlib.suppress(OSError, RuntimeError):
        directory = graph.parent.resolve(strict=True)
        checkout = graph_root.resolve(strict=True)
        if directory.name == output_directory and directory.parent.is_relative_to(checkout):
            return directory.parent.relative_to(checkout)

    project = graph_path_for_store(".", ctx.store_path)
    if ctx.borrowed_from is not None or graph.is_relative_to(project):
        return project.relative_to(root)
    with contextlib.suppress(OSError, RuntimeError):
        if graph.parent.resolve(strict=True).is_relative_to(project.resolve(strict=True)):
            return project.relative_to(root)
        configured = Path(os.environ.get("SIDEGRAPH_GRAPH") or DEFAULT_GRAPH)
        if not configured.is_absolute() and _same_graph_path(
            ctx.graph_path, default_graph_path(ctx.store_path)
        ):
            return project.relative_to(root)
    return Path()


def _anchors(
    store: Store,
    record_id: str,
    reader: GraphifyReader | None,
    root: Path | None,
    source_prefix: Path,
    *,
    index_cold: bool,
) -> list[contract.Anchor]:
    anchors = []
    for binding in store.bindings_for_record(record_id):
        entity = store.get_entity(binding.entity_id)
        if entity is None:
            continue
        descriptor = entity.descriptor
        file = descriptor.file_path if descriptor is not None else None
        span = None
        if reader is not None and descriptor is not None and binding.tier == 2:
            resolved = reader.resolve(descriptor, collapse=False)
            if resolved.node_id is not None:
                span = reader.symbol_span(resolved.node_id)
        if span is not None:
            file = span[0]
        if file is not None:
            try:
                file = source_prefix.joinpath(*snippets._parts(file)).as_posix()
            except OSError:
                file, span = None, None
        editor_path = str(root / file) if root is not None and file and span is not None else None
        anchors.append(
            contract.Anchor.model_validate(
                {
                    "entity_id": entity.entity_id,
                    "name": descriptor.name if descriptor is not None else entity.canonical_name,
                    "tier": binding.tier,
                    "file": file,
                    "start": span[1] if span is not None else None,
                    "end": span[2] if span is not None else None,
                    "status_word": word_for(binding.status, index_cold=index_cold),
                    "internal_status": binding.status,
                    "community": entity.last_seen_community,
                    "editor_path": editor_path,
                }
            )
        )
    return anchors


def _record(store: Store, record_id: str) -> Decision | Fact | Domain | None:
    return store.get_decision(record_id) or store.get_fact(record_id) or store.get_domain(record_id)


def _history(store: Store, record: Decision | Fact | Domain) -> list[Decision | Fact | Domain]:
    records: dict[str, Decision | Fact | Domain]
    if isinstance(record, Decision):
        records = {item.id: item for item in store.iter_decisions()}
        record_id = record.id
    elif isinstance(record, Fact):
        records = {item.id: item for item in store.iter_facts()}
        record_id = record.id
    else:
        records = {item.domain_id: item for item in store.iter_domains()}
        record_id = record.domain_id

    records[record_id] = record
    successors: dict[str, list[str]] = defaultdict(list)
    for item_id, item in records.items():
        if item.supersedes is not None:
            successors[item.supersedes].append(item_id)
    root = record_id
    seen: set[str] = set()
    while root not in seen and (previous := records[root].supersedes) in records:
        seen.add(root)
        root = previous

    def order(item_id: str) -> tuple[datetime, str]:
        item = records[item_id]
        return (
            datetime.min.replace(tzinfo=UTC) if isinstance(item, Domain) else item.valid_from,
            item_id,
        )

    # Offer each version when its predecessor has been emitted, then take the oldest
    # of the available versions. Domains carry their creation order in their IDs.
    pending = [order(root)]
    seen.clear()
    history = []
    while pending:
        _, current = heapq.heappop(pending)
        if current not in seen:
            seen.add(current)
            history.append(records[current])
            for successor in successors[current]:
                heapq.heappush(pending, order(successor))
    return history


def routes(ctx: PortalContext) -> list[Route]:
    """Register the read views (Portal design, D4–D6, D9)."""

    def graph(request: Request) -> JSONResponse:
        with ctx.request_store() as store:
            store.refresh_if_stale()
            reader = ctx.reader()
            data = build_graph(
                store, include_superseded=_history_requested(request), max_nodes=GRAPH_MAX_NODES
            )
            domains = _record_domains(store, reader)
            certified = False
            if reader is not None:
                settled, failure = _settled(store, membership_stamp(store, reader))
                certified = settled and failure is None
            value = contract.Graph.model_validate(
                {
                    **to_json(data),
                    "record_domains": {n.id: domains[n.id] for n in data.nodes if n.id in domains},
                    "membership_certified": certified,
                }
            )
        return JSONResponse(value.model_dump(mode="json"))

    def search(request: Request) -> JSONResponse:
        from .app import _error

        raw_limit = request.query_params.get("limit")
        try:
            limit = int(raw_limit) if raw_limit is not None else None
        except ValueError:
            return _error("invalid", "Expected an integer search limit.", field="limit")
        if limit is not None and limit < 1:
            return _error("invalid", "Expected a positive search limit.", field="limit")
        query = request.query_params.get("q", "").strip()
        with ctx.request_store() as store:
            store.refresh_if_stale()
            pool = _search_pool(store)
            ids = [row["id"] for row in pool]
            prefix = query.lower()
            matching_ids = (
                [
                    record_id
                    for record_id in (*ids, *(d.domain_id for d in store.iter_domains()))
                    if record_id.lower().startswith(prefix)
                ]
                if len(prefix) >= 8
                else []
            )
            open_id = matching_ids[0] if len(matching_ids) == 1 else None
            matches, match = _query_matches(
                store, pool, query, include_history=_history_requested(request)
            )
            by_id = {row["id"]: row for row in pool}
            domains = _record_domains(store, ctx.reader()) if matches else {}
            history_anchors = store.anchor_names() if match.history_replacements else {}
            hits = []
            for record_id, words in matches[:limit]:
                row = by_id[record_id]
                predecessors = match.history_replacements.get(record_id, [])
                source = by_id[predecessors[0]] if predecessors else row
                anchors = match.via_anchor.get(record_id, [])
                if predecessors:
                    body = _searched_text(source)
                    missing = [word for word in words if word not in body]
                    if missing:
                        anchors = _anchors_holding(history_anchors.get(source["id"], ()), missing)
                fields = ("choice", "statement", "context", "rejected", "consequences", "source")
                excerpts = [source[field] for field in fields if source.get(field)]
                if predecessors and source.get("title"):
                    excerpts.append(source["title"])
                excerpts.extend(anchors[1:])
                title = row.get("title", row.get("statement"))
                shown_anchor = next(iter(anchors), None)
                displayed = f"{title} {shown_anchor or ''}".lower()
                remaining = {word for word in words if word not in displayed}
                shown = []
                while remaining and excerpts:
                    best = max(
                        excerpts, key=lambda text: sum(word in text.lower() for word in remaining)
                    )
                    held = {word for word in remaining if word in best.lower()}
                    if not held:
                        break
                    shown.append(best)
                    excerpts.remove(best)
                    remaining -= held
                snippet = "\n".join(shown) if shown else next(iter(excerpts), "")
                hits.append(
                    {
                        "id": record_id,
                        "type": "decision" if "kind" in row else "fact",
                        "kind": row.get("kind"),
                        "status": row["status"],
                        "domains": domains.get(record_id, []),
                        "title": title,
                        "match": {"words": words, "via_anchor": shown_anchor},
                        "snippet": snippet,
                    }
                )
            value = contract.SearchResult.model_validate({"open_id": open_id, "hits": hits})
        return JSONResponse(value.model_dump(mode="json"))

    def records(request: Request) -> JSONResponse:
        params = request.query_params
        status, kind, domain, layer, anchors = (
            params.get(key) for key in ("status", "kind", "domain", "layer", "anchors")
        )
        include_history = _history_requested(request)
        with ctx.request_store() as store:
            store.refresh_if_stale()
            index_cold = store.get_meta(VOLATILE_STALE_KEY) == "1"
            domains = _record_domains(store, ctx.reader())
            by_id = {}
            pool = []
            for item in _search_pool(store):
                if (status and item["status"] != status) or (kind and item.get("kind") != kind):
                    continue
                if (layer and item.get("layer") != layer) or (
                    domain and domain not in domains.get(item["id"], [])
                ):
                    continue
                row = _row(store, item, domains, index_cold=index_cold)
                if anchors and (
                    bool(row.anchor_statuses)
                    if anchors == "none"
                    else anchors not in row.anchor_statuses
                ):
                    continue
                by_id[row.id] = row
                pool.append(item)
            query = params.get("q", "").strip()
            if query:
                matches, _ = _query_matches(store, pool, query, include_history=include_history)
                rows = [by_id[record_id] for record_id, _ in matches]
            else:
                rows = [by_id[r["id"]] for r in pool if include_history or not _in_history(r)]
        return JSONResponse(contract.Records(rows=rows).model_dump(mode="json"))

    def record_card(request: Request) -> JSONResponse:
        from .app import _error

        record_id = request.path_params["id"]
        with ctx.request_store() as store:
            store.refresh_if_stale()
            record = _record(store, record_id)
            if record is None:
                return _error(
                    "not_found", "This record is not on this branch.", reason="not_on_this_branch"
                )
            reader = ctx.reader()
            root = repository_root(ctx.store_path)
            cold = store.get_meta(VOLATILE_STALE_KEY) == "1"
            domains = _record_domains(store, reader)
            history = _history(store, record)
            evidence = (
                sorted(
                    (fact for fact in store.iter_facts() if record_id in fact.supports),
                    key=lambda fact: (fact.valid_from, fact.id),
                )
                if isinstance(record, Decision)
                else []
            )
            pending = [
                item.domain_id if isinstance(item, Domain) else item.id
                for item in history
                if item.supersedes == record_id and item.status == "proposed"
            ]
            value = contract.RecordCard.model_validate(
                {
                    "record": record,
                    "history": [
                        _row(store, item.model_dump(), domains, index_cold=cold) for item in history
                    ],
                    "evidence": evidence,
                    "keepable_evidence": [
                        fact.id
                        for fact in evidence
                        if record.status == DecisionStatus.PROPOSED
                        and fact.status == DecisionStatus.PROPOSED
                        and _exclusive_evidence(store, fact, record_id)
                    ],
                    "drifted": record_id in drifted_record_ids(store),
                    "pending_successor": min(pending, default=None),
                    "pending_evidence": [
                        fact.id for fact in evidence if fact.status == DecisionStatus.PROPOSED
                    ],
                    "anchors": _anchors(
                        store, record_id, reader, root, _source_prefix(ctx, root), index_cold=cold
                    ),
                }
            )
        return JSONResponse(value.model_dump(mode="json"))

    def snippet(request: Request) -> JSONResponse:
        from .app import _error

        root = repository_root(ctx.store_path)
        if root is None:
            return _error(
                "refused", "Source snippets require a repository root.", reason="no_repository_root"
            )
        record_id = request.path_params["id"]
        entity_id = request.path_params["entity_id"]
        with ctx.request_store() as store:
            store.refresh_if_stale()
            if _record(store, record_id) is None:
                return _error(
                    "not_found", "This record is not on this branch.", reason="not_on_this_branch"
                )
            reader = ctx.reader()
            cold = store.get_meta(VOLATILE_STALE_KEY) == "1"
            source_prefix = _source_prefix(ctx, root)
            # Re-select if the graph was replaced between selecting the reader and
            # sampling its timestamp. The selected span and stamp must agree.
            for _ in range(2):
                if reader is None:
                    break
                try:
                    graph_mtime = reader.path.stat().st_mtime_ns
                    current_reader = ctx.reader()
                    if current_reader is not reader:
                        reader = current_reader
                        continue
                    anchor = next(
                        (
                            a
                            for a in _anchors(
                                store, record_id, reader, root, source_prefix, index_cold=cold
                            )
                            if a.entity_id == entity_id
                        ),
                        None,
                    )
                    if anchor is None or not anchor.file or anchor.start is None:
                        break
                    value, source_mtime = snippets._read_span(
                        root,
                        anchor.file,
                        anchor.start,
                        anchor.end,
                        full=request.query_params.get("full") in ("true", "1"),
                    )
                except OSError:
                    break
                value.shifted = source_mtime > graph_mtime
                return JSONResponse(value.model_dump(mode="json"))
        return _error(
            "not_found",
            "No source snippet is available for this anchor.",
            reason="snippet_unavailable",
        )

    def queue(_request: Request) -> JSONResponse:
        with ctx.request_store() as store:
            store.refresh_if_stale()
            reader = ctx.reader()
            root = repository_root(ctx.store_path)
            source_prefix = _source_prefix(ctx, root)
            cold = store.get_meta(VOLATILE_STALE_KEY) == "1"
            domains = _record_domains(store, reader)
            pool = _search_pool(store)
            by_id = {row["id"]: row for row in pool}
            # Classify proposed evidence from the same index snapshot as its parent.
            # Finish the read transaction before the heavier canonical and graph work.
            with store._lock:
                store._conn.execute("BEGIN DEFERRED")
                try:
                    drafts = list(store.iter_proposed())
                    proposed_facts = list(store.iter_proposed_facts())
                    draft_domains = list(store.iter_domains(status=DomainStatus.PROPOSED))
                finally:
                    store._conn.rollback()
            draft_ids = {draft.id for draft in drafts}
            evidence: dict[str, list[Fact]] = defaultdict(list)
            standalone = []
            for fact in proposed_facts:
                supporters = draft_ids.intersection(fact.supports)
                if supporters:
                    for supporter in supporters:
                        evidence[supporter].append(fact)
                else:
                    standalone.append(fact)
            advice_eligible = (
                draft_ids | {f.id for f in standalone} | {d.domain_id for d in draft_domains}
            )
            advice = {row["record_id"]: row for row in store.advice_rows()}
            canonical = {}
            if advice_eligible.intersection(advice):
                inventory = inventory_of(store.path)
                if inventory.marker is None or inventory.marker.major < 2:
                    # Match the advice writer: a skipped format-1 file cannot shadow its archive.
                    inventory.occurrences = [
                        entry
                        for entry in inventory.occurrences
                        if entry.kind != "file" or entry.readable
                    ]
                canonical = effective_bodies(inventory)
            summaries: dict[str, contract.RecordRow] = {}

            def summary(record_id: str) -> contract.RecordRow:
                if record_id not in summaries:
                    summaries[record_id] = _row(store, by_id[record_id], domains, index_cold=cold)
                return summaries[record_id]

            def item(record: Decision | Fact | Domain) -> dict:
                if isinstance(record, Fact):
                    record_type, record_id, title = "fact", record.id, record.statement
                elif isinstance(record, Domain):
                    record_type, record_id, title = "domain", record.domain_id, record.title
                else:
                    record_type, record_id, title = "decision", record.id, record.title
                row = advice.get(record_id)
                recommendation = None
                saved = canonical.get((record_type, record_id)) if row is not None else None
                if row is not None and saved is not None and isinstance(saved.raw, dict):
                    excluded = {"communities"} if record_type == "domain" else set()
                    body = {key: val for key, val in saved.raw.items() if key not in excluded}
                    # Compare both the text we will ship and the advice hash to the same body.
                    if type(record).model_validate(body).model_dump(
                        mode="json", exclude=excluded
                    ) == record.model_dump(mode="json", exclude=excluded):
                        try:
                            digest = hashlib.sha256(encode_body(body).encode("utf-8")).hexdigest()
                        except UnencodableBody:
                            digest = None
                        if digest is not None and digest == row["record_hash"]:
                            recommendation = {"verdict": row["verdict"], "reason": row["reason"]}
                matches, _ = _query_matches(
                    store, [r for r in pool if r["id"] != record_id], title, include_history=False
                )
                return {
                    "record": record,
                    "advice": recommendation,
                    "signals": {
                        "anchors": _anchors(
                            store, record_id, reader, root, source_prefix, index_cold=cold
                        ),
                        "similar": [summary(match_id) for match_id, _ in matches],
                    },
                }

            value = contract.Queue.model_validate(
                {
                    "decisions": [
                        {**item(draft), "evidence": evidence[draft.id]} for draft in drafts
                    ],
                    "facts": [item(fact) for fact in standalone],
                    "domains": [item(d) for d in draft_domains],
                }
            )
        return JSONResponse(value.model_dump(mode="json"))

    handlers = {
        "/api/graph": graph,
        "/api/search": search,
        "/api/records": records,
        "/api/records/{id}": record_card,
        "/api/records/{id}/anchors/{entity_id}/snippet": snippet,
        "/api/queue": queue,
    }
    return [
        Route(route.path, handlers[route.path], methods=[route.method])
        for route in contract.ROUTES
        if route.path in handlers
    ]
