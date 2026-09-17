"""Multi-anchor resolution at capture (portable core).

Given a decision and an anchor reference (name + file), resolve it through a GraphifyReader
and create AnchorBindings across tiers with graceful degradation (see
docs/concepts/anchoring.md):

- resolved   -> Tier-2 leaf (live) + Tier-1 domain/community (live) [+ Tier-0 initiative]
- ambiguous  -> Tier-1 domain/community only (degraded, unless a domain covers it — see
                below); no leaf
- unresolved -> Tier-2 leaf (orphaned); no community

Depends only on the engine-seam interface (reader.resolve -> ResolveResult) and the Store —
never on Graphify internals.
"""

from __future__ import annotations

from pathlib import PurePosixPath

from .engine.reader import GraphifyReader, ResolveResult
from .schema import AnchorBinding, Descriptor, Relation
from .store import Entity, Store


def _crosses_suffix(
    ref: Descriptor, entity: Entity, result: ResolveResult, reader: GraphifyReader
) -> bool:
    """True when a path-less ref adopted a path-carrying entity but resolved by NAME to a
    node in a different kind of file — the collision sync.py's rebind ladder refuses. False
    for every ordinary case: a ref that carries its own path, an entity without one, or a
    node whose suffix agrees (a same-suffix move IS a move, and sync heals the descriptor on
    its next pass). Unknown node -> False: no evidence of a collision is not evidence of one.
    """
    if ref.file_path or entity.descriptor is None or not entity.descriptor.file_path:
        return False
    node = reader.get_node(result.node_id) if result.node_id else None
    if node is None or not node.file_path:
        return False
    return PurePosixPath(node.file_path).suffix != PurePosixPath(entity.descriptor.file_path).suffix


class AnchorResolution(list[AnchorBinding]):
    """``resolve_and_bind``'s return value: behaves exactly like the ``list[AnchorBinding]``
    it always returned (every existing caller iterates/indexes/``len()``s it as a plain
    list — this stays a drop-in) but additionally carries the underlying
    ``reader.resolve()`` outcome, so a caller that wants per-anchor feedback (Gate-5 finding
    S3: "ambiguous" anchors reported back to the human/agent) doesn't have to re-resolve the
    same ref a second time just to learn WHY no leaf binding was created.

    ``status``/``candidates`` mirror ``engine.reader.ResolveResult`` exactly (``"resolved"``
    | ``"ambiguous"`` | ``"unresolved"``; ``candidates`` is only non-empty when ambiguous).
    """

    def __init__(
        self,
        bindings: list[AnchorBinding],
        status: str,
        candidates: list[str],
    ) -> None:
        super().__init__(bindings)
        self.status = status
        self.candidates = candidates


def resolve_and_bind(
    record_id: str,
    ref: Descriptor,
    reader: GraphifyReader,
    store: Store,
    initiative: str | None = None,
    relation: Relation | None = None,
) -> AnchorResolution:
    """``relation`` (optional) overrides the default "affects" on the leaf + Tier-1 bindings
    created for THIS anchor (see docs/concepts/anchoring.md#multi-anchor-at-capture); it
    never applies to the Tier-0 initiative binding, which is decision-level rather than
    per-anchor.

    Returns an :class:`AnchorResolution` — a ``list[AnchorBinding]`` in every respect a
    caller cares about, plus ``.status``/``.candidates`` for callers that want to report
    ambiguous anchors back without a second ``reader.resolve()`` call.
    """
    result = reader.resolve(ref)
    version = reader.graph_version()
    bindings: list[AnchorBinding] = []
    # Explicit kwarg rather than **{"relation": relation}-if-present: splatting a
    # dict[str, Relation] onto AnchorBinding's constructor is exactly as fragile as it looks
    # to a type checker (nothing pins the dict's key set to just "relation"). AnchorBinding's
    # own default is "affects", so passing it explicitly here changes nothing at runtime.
    effective_relation: Relation = relation if relation is not None else "affects"
    # Tier-1 draws on this rather than `result.community` directly: the cross-suffix rail
    # below may reject the resolved node, and a rejected node's community must not be spent.
    effective_community = result.community

    # Tier-2 leaf (concrete entity) — created when resolved or unresolved (orphaned).
    if result.status in ("resolved", "unresolved"):
        # find+mint half only (design D3): Store.get_or_create_entity runs the lookup and
        # the mint atomically (Task 2), closing finding 2's TOCTOU for this call site. It is
        # NOT a pure get-or-create here, though: on a resolved node the engine mapping
        # (last_seen_node_id/last_seen_graph_version/last_seen_community) must be refreshed
        # even when the entity already existed -- a rebuild routinely renumbers node ids and
        # communities for an entity capture already knows about, and that refresh has to
        # keep landing on the same durable id or sync's rebind logic would never see it move.
        # That refresh — and the second upsert it requires — stays HERE, not inside
        # get_or_create_entity: collapsing it away would silently stop sync's mapping from
        # updating (see tests/test_anchoring_mapping_refresh.py, a characterization test
        # pinning exactly this before this conversion).
        entity = store.get_or_create_entity(ref)
        # Cross-suffix rail, mirroring sync.py's ("a unique cross-suffix hit is a collision,
        # not a move"). A path-less ref that adopted a path-carrying entity can resolve by
        # NAME to a node in a different kind of file — a markdown heading standing in for a
        # code symbol that left the graph. sync's ladder refuses that; capture-time had no
        # counterpart, so adoption redirected the collision onto a REAL entity's engine
        # mapping, where the next rebind orphans without restoring it (external review,
        # finding 2 — pre-fix the same damage landed on a disposable twin, which is why it
        # was invisible). Withholding the refresh is the whole fix: the binding still lands
        # live on the adopted entity, because the decision does name this symbol. Deciding
        # that such an anchor is *unresolved* would be a policy change, not a repair.
        if _crosses_suffix(ref, entity, result, reader):
            # Rejecting the node's id while still spending its community would be half an
            # abstention, and the half that leaks is the durable one: sync re-adjudicates a
            # leaf, but nothing re-adjudicates a Tier-1 community binding — when the leaf
            # later orphans, `_repoint_off_path` sees the vanished file's community (None)
            # and repoints nothing, so the decision keeps surfacing in the DOCS community
            # forever (external review, round 3, probed). Fall back to the entity's own
            # last-known community: evidence this rail has not rejected.
            effective_community = entity.last_seen_community
        elif result.status == "resolved":
            entity.last_seen_node_id = result.node_id
            entity.last_seen_graph_version = version
            entity.last_seen_community = result.community
            store.upsert_entity(entity)
        leaf_status = "live" if result.status == "resolved" else "orphaned"
        bindings.append(
            store.add_binding(
                AnchorBinding(
                    record_id=record_id,
                    entity_id=entity.entity_id,
                    tier=2,
                    status=leaf_status,
                    relation=effective_relation,
                )
            )
        )

    # Tier-1: an ACCEPTED domain covering the anchor's current community wins over the bare
    # community entity — new memory lands on durable, named abstractions once domains are
    # ratified (see spec §1/§4). Domain-covered bindings are always "live": once a domain has
    # claimed the community, Tier-1 confidence comes from the domain's curation, not from
    # whether this particular leaf resolved cleanly. No accepted domain claims the community
    # (the case for every store without ratified domains) -> legacy `community:<id>`
    # fallback, unchanged from before this existed.
    if effective_community is not None:
        domain = store.find_domain_by_community(effective_community)
        if domain is not None:
            domain_entity = store.get_or_create_abstract_entity(f"domain:{domain.slug}")
            bindings.append(
                store.add_binding(
                    AnchorBinding(
                        record_id=record_id,
                        entity_id=domain_entity.entity_id,
                        tier=1,
                        status="live",
                        relation=effective_relation,
                    )
                )
            )
        else:
            comm = store.get_or_create_abstract_entity(f"community:{effective_community}")
            comm_status = "live" if result.status == "resolved" else "degraded"
            bindings.append(
                store.add_binding(
                    AnchorBinding(
                        record_id=record_id,
                        entity_id=comm.entity_id,
                        tier=1,
                        status=comm_status,
                        relation=effective_relation,
                    )
                )
            )

    # Tier-0 initiative — created only when the decision names one. Never takes the
    # per-anchor relation override (see docstring).
    if initiative:
        init = store.get_or_create_abstract_entity(f"initiative:{initiative}")
        bindings.append(
            store.add_binding(
                AnchorBinding(
                    record_id=record_id,
                    entity_id=init.entity_id,
                    tier=0,
                    status="live",
                )
            )
        )

    return AnchorResolution(bindings, result.status, result.candidates)


def orphan_reason(ref: Descriptor, reader: GraphifyReader) -> str:
    """Why an anchor resolved to nothing — the three causes need three different fixes.

    - ``"file-not-in-graph"``: the graph carries no node for ``ref.file_path`` at all. The
      NAME may be perfectly correct; the graph simply does not cover that file yet. On the
      airflow corpus this was 9 of 15 orphans — the graph was built 2026-07-29 and the files
      were written 2026-07-30. Fix: ``graphify update .``, then re-anchor. Also covers a
      typo'd path and a file type the engine does not index.
    - ``"name-not-in-file"``: the graph does carry that file, and it has no such name. Fix:
      the name (``find_entity``/``query_structure`` will say what is really there).
    - ``"no-file-path"``: the ref carried a bare name. A name-only ref that resolves to
      nothing cannot be diagnosed further. Fix: pass ``file_path``.

    Deliberately reader-only: no git call, no filesystem stat. "Is this file in the graph"
    is the question that decides the fix, and the reader answers it directly — reaching for
    a repo root would add a subprocess per capture to sharpen a distinction the author does
    not need.
    """
    if ref.file_path is None:
        return "no-file-path"
    return "name-not-in-file" if reader.nodes_in_file(ref.file_path) else "file-not-in-graph"


def entity_summaries(store: Store, bindings: list) -> list[dict]:
    """``{"entity_id", "canonical_name", "tier"}`` per binding.

    Lives here rather than in ``server.py`` because ``capture.py``'s propose pipeline needs
    the identical shape for its own ``anchors_orphaned`` bucket and cannot import from
    ``server`` (server imports capture). One definition, so the human-asked path and the
    agent-initiated path cannot drift into reporting the same fact two ways.

    Lets a caller chain straight into ``find_entity``/``get_entity_history`` without a raw
    store lookup. A binding whose entity has vanished is skipped rather than rendered as a
    hole.
    """
    out: list[dict] = []
    for b in bindings:
        entity = store.get_entity(b.entity_id)
        if entity is not None:
            out.append(
                {
                    "entity_id": entity.entity_id,
                    "canonical_name": entity.canonical_name,
                    "tier": b.tier,
                }
            )
    return out
