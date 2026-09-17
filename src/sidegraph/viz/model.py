"""Assemble a semantic graph over the owned Decision/Fact records and their anchored
entities. Pure and portable: reads only :class:`~sidegraph.store.Store` — no engine reader,
no host specifics, no writes. :mod:`sidegraph.viz.render` turns this into HTML/JSON.

# see design/superpowers/specs/2026-07-12-decision-graph-viz-design.md
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..store import Store

_TERMINAL_STATUSES = frozenset({"superseded", "rejected", "deprecated"})
_PROBLEM_BINDING_STATUSES = frozenset({"degraded", "orphaned"})


@dataclass
class VizNode:
    id: str
    type: str  # "decision" | "fact" | "entity"
    label: str
    detail: dict[str, object]  # full record fields for the click-to-inspect sidebar
    kind: str | None = None  # decision kind (adr/lesson/constraint/gotcha); else None
    status: str | None = None  # record status; None for entities
    tier: int | None = None  # entity primary tier (min over incident anchors); None for records
    community: str | None = None  # entity last_seen_community; None for records
    degree: int = 0
    dangling: bool = False  # record node with no anchor binding at all (any status)
    problem: bool = False  # dangling OR incident to a degraded/orphaned binding


@dataclass
class VizEdge:
    source: str
    target: str
    kind: str  # "anchor" | "supersedes" | "supports" | "domain-parent"
    status: str | None = None  # binding status for anchor edges
    relation: str | None = None  # binding relation for anchor edges
    weight: float = 1.0


@dataclass
class VizStats:
    decisions: int = 0
    facts: int = 0
    entities: int = 0
    orphaned_bindings: int = 0
    degraded_bindings: int = 0
    dangling_records: int = 0
    supersede_links: int = 0
    truncated: int = 0


@dataclass
class VizGraph:
    nodes: list[VizNode] = field(default_factory=list)
    edges: list[VizEdge] = field(default_factory=list)
    stats: VizStats = field(default_factory=VizStats)


def _short(text: str, limit: int = 60) -> str:
    stripped = text.strip()
    line = stripped.splitlines()[0] if stripped else ""
    return line if len(line) <= limit else line[: limit - 1] + "…"


def _recompute_degree(nodes: dict[str, VizNode], edges: list[VizEdge]) -> None:
    for n in nodes.values():
        n.degree = 0
    for e in edges:
        nodes[e.source].degree += 1
        nodes[e.target].degree += 1


def build_graph(
    store: Store,
    *,
    include_superseded: bool = True,
    only_problems: bool = False,
    max_nodes: int = 800,
) -> VizGraph:
    """Build a :class:`VizGraph` from the store. See module docstring."""
    nodes: dict[str, VizNode] = {}
    edges: list[VizEdge] = []
    referenced_entities: dict[str, int] = {}  # entity_id -> min tier seen

    def _keep(status: str) -> bool:
        return include_superseded or status not in _TERMINAL_STATUSES

    def _anchor_edges(record_id: str) -> None:
        for b in store.bindings_for_record(record_id):
            edges.append(
                VizEdge(
                    source=record_id,
                    target=b.entity_id,
                    kind="anchor",
                    status=b.status,
                    relation=b.relation,
                    weight=b.weight,
                )
            )
            prev = referenced_entities.get(b.entity_id)
            referenced_entities[b.entity_id] = b.tier if prev is None else min(prev, b.tier)

    # --- decision & fact record nodes + their anchor / structural edges ---
    for d in store.iter_decisions():
        if not _keep(d.status.value):
            continue
        nodes[d.id] = VizNode(
            id=d.id,
            type="decision",
            label=_short(d.title),
            kind=d.kind.value,
            status=d.status.value,
            detail={
                "title": d.title,
                "kind": d.kind.value,
                "status": d.status.value,
                "context": d.context,
                "choice": d.choice,
                "rejected": d.rejected,
                "consequences": d.consequences,
                "scope": d.scope.value,
            },
        )
        _anchor_edges(d.id)
        if d.supersedes:
            edges.append(VizEdge(source=d.id, target=d.supersedes, kind="supersedes"))

    for f in store.iter_facts():
        if not _keep(f.status.value):
            continue
        nodes[f.id] = VizNode(
            id=f.id,
            type="fact",
            label=_short(f.statement),
            status=f.status.value,
            detail={
                "statement": f.statement,
                "source": f.source,
                "supports": list(f.supports),
                "status": f.status.value,
            },
        )
        _anchor_edges(f.id)
        if f.supersedes:
            edges.append(VizEdge(source=f.id, target=f.supersedes, kind="supersedes"))
        for dec_id in f.supports:
            edges.append(VizEdge(source=f.id, target=dec_id, kind="supports"))

    # --- entity nodes (only those referenced by a binding) ---
    for entity_id, tier in referenced_entities.items():
        ent = store.get_entity(entity_id)
        if ent is None:
            continue
        label = ent.descriptor.name if ent.descriptor else ent.canonical_name
        nodes[entity_id] = VizNode(
            id=entity_id,
            type="entity",
            label=label,
            tier=tier,
            community=ent.last_seen_community,
            detail={
                "canonical_name": ent.canonical_name,
                "kind": ent.kind.value,
                "file_path": ent.descriptor.file_path if ent.descriptor else None,
                "last_seen_community": ent.last_seen_community,
            },
        )

    # --- domain parent hierarchy (between the domain: entities already drawn) ---
    domains = list(store.iter_domains())
    slug_by_domain_id = {dm.domain_id: dm.slug for dm in domains}
    entity_by_domain_slug: dict[str, str] = {}
    for nid, n in nodes.items():
        cname = n.detail.get("canonical_name")
        if n.type == "entity" and isinstance(cname, str) and cname.startswith("domain:"):
            entity_by_domain_slug[cname[len("domain:") :]] = nid
    for dm in domains:
        if not dm.parent_id or dm.parent_id not in slug_by_domain_id:
            continue
        child = entity_by_domain_slug.get(dm.slug)
        parent = entity_by_domain_slug.get(slug_by_domain_id[dm.parent_id])
        if child and parent:
            edges.append(VizEdge(source=child, target=parent, kind="domain-parent"))

    # --- prune edges to surviving endpoints ---
    edges = [e for e in edges if e.source in nodes and e.target in nodes]

    # --- dangling + problem flags ---
    # "dangling" = zero anchor bindings at all; a degraded/orphaned binding is a separate
    # "problem" signal (an anchored record whose binding decayed is not unanchored).
    has_binding: set[str] = set()
    incident_problem: set[str] = set()
    for e in edges:
        if e.kind == "anchor":
            has_binding.add(e.source)
            if e.status in _PROBLEM_BINDING_STATUSES:
                incident_problem.add(e.source)
                incident_problem.add(e.target)
    for nid, n in nodes.items():
        if n.type in ("decision", "fact"):
            n.dangling = nid not in has_binding
        n.problem = n.dangling or nid in incident_problem

    # --- only-problems filter (problem nodes + 1-hop context) ---
    if only_problems:
        problem_nodes = {nid for nid, n in nodes.items() if n.problem}
        keep = set(problem_nodes)
        for e in edges:
            if e.source in problem_nodes or e.target in problem_nodes:
                keep.add(e.source)
                keep.add(e.target)
        nodes = {nid: n for nid, n in nodes.items() if nid in keep}
        edges = [e for e in edges if e.source in nodes and e.target in nodes]

    _recompute_degree(nodes, edges)

    # --- max-nodes truncation (deterministic priority; never silent) ---
    stats = VizStats()
    if len(nodes) > max_nodes:

        def _priority(n: VizNode) -> tuple[int, int, int, int, str]:
            terminal = 1 if (n.status in _TERMINAL_STATUSES) else 0
            type_rank = 2 if n.type == "entity" else 0
            # `n.id` as the final tiebreak makes truncation deterministic across index
            # rebuilds -- otherwise ties fall back to dict/iteration order, which is not
            # reproducible.
            return (0 if n.problem else 1, type_rank, terminal, -n.degree, n.id)

        ranked = sorted(nodes.values(), key=_priority)
        keep_ids = {n.id for n in ranked[:max_nodes]}
        stats.truncated = len(nodes) - len(keep_ids)
        nodes = {nid: n for nid, n in nodes.items() if nid in keep_ids}
        edges = [e for e in edges if e.source in nodes and e.target in nodes]
        _recompute_degree(nodes, edges)

    # --- final stats over the surviving graph ---
    stats.decisions = sum(1 for n in nodes.values() if n.type == "decision")
    stats.facts = sum(1 for n in nodes.values() if n.type == "fact")
    stats.entities = sum(1 for n in nodes.values() if n.type == "entity")
    stats.orphaned_bindings = sum(1 for e in edges if e.kind == "anchor" and e.status == "orphaned")
    stats.degraded_bindings = sum(1 for e in edges if e.kind == "anchor" and e.status == "degraded")
    stats.dangling_records = sum(1 for n in nodes.values() if n.dangling)
    stats.supersede_links = sum(1 for e in edges if e.kind == "supersedes")

    return VizGraph(nodes=list(nodes.values()), edges=edges, stats=stats)
