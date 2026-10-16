"""Multi-anchor resolution at capture (portable core).

Given a decision and an anchor reference (name + file), resolve it through a GraphifyReader
and create AnchorBindings across tiers with graceful degradation (see
docs/concepts/anchoring.md):

- resolved   -> Tier-2 leaf (live) + Tier-1 domain/community (live)
- ambiguous  -> Tier-1 domain/community only (degraded, unless a domain covers it — see
                below); no leaf
- unresolved -> Tier-2 leaf (orphaned); no community

Depends only on the engine-seam interface (reader.resolve -> ResolveResult) and the Store —
never on Graphify internals.
"""

from __future__ import annotations

import posixpath
from collections.abc import Sequence
from pathlib import Path, PurePosixPath

from . import worktree_seeds
from .config import _store_project_root, repository_root
from .engine.reader import GraphifyReader, ResolveResult
from .schema import AnchorBinding, Descriptor, Domain, Relation, file_anchor_name
from .store import Entity, Store
from .sync import ensure_domain_membership, membership_exclusions, membership_stamp


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


def _is_absolute(path: str) -> bool:
    return posixpath.isabs(path) or worktree_seeds.has_drive(path)


def normalise_anchor_path(
    file_path: str | None,
    root: Path | Sequence[Path] | None,
    reader: GraphifyReader | None = None,
) -> str | None:
    """An anchor ``file_path`` as the graph spells it: ``\\`` becomes ``/`` on Windows, a leading
    ``./`` goes, and an absolute path under ``root`` becomes repo-relative
    (``worktree_seeds.rebase``, the rule the seed ladder uses). Blank is no path; anything else
    comes back as given.

    ``root`` may be several candidates (:func:`anchor_roots`). When more than one yields a path
    and a ``reader`` is given, the first one whose path the graph holds wins; otherwise the first.
    """
    if file_path is None or not file_path.strip():
        return None
    path = file_path.strip()
    if worktree_seeds.WINDOWS and "\\" in path:
        path = path.replace("\\", "/")
    roots = [root] if isinstance(root, Path) else list(root or [])
    if roots and _is_absolute(path):
        found: list[str] = []
        for candidate_root in roots:
            hit = worktree_seeds.rebase(path, candidate_root, [])
            if hit is not None and hit[1] and hit[1] not in found:
                found.append(hit[1])
        if found:
            path = next((c for c in found if reader and reader.nodes_in_file(c)), found[0])
    while path.startswith("./"):
        path = path[2:]
    return path or None


def anchor_roots(store_path: str | Path) -> list[Path]:
    """What an absolute anchor path may be made relative to, in order of preference: the
    store's repository root, then its project when that is a different directory (a nested
    store whose graph belongs to its own project). Lexical: no git call on the capture path."""
    project = _store_project_root(store_path)
    repo = repository_root(store_path)
    return [repo, project] if repo is not None and repo != project else [project]


def anchor_descriptor(
    ref: Descriptor,
    reader: GraphifyReader | None,
    store: Store,
    *,
    result: ResolveResult | None = None,
    root: Path | Sequence[Path] | None = None,
) -> Descriptor:
    """The descriptor an anchor is looked up and stored under. A path-less ``ref`` the graph
    resolves to one node with a file becomes ``Descriptor(file_anchor_name(ref.name, file),
    file)``, so no path-less entity is minted and later lookups key on one identity. ``ref``
    itself comes back, in this order, when:

    - it has a file, there is no graph, or the answer is not one node with a file;
    - the store adopts it: one same-named entity carries a path (``Store.resolve_descriptor``).
      The cross-suffix rail and sync's moved rung keep deciding a carrier whose file differs;
    - a path-less entity of that name exists and none sits at the node's file: its bindings and
      dedup stay where they are.

    ``result`` is the caller's own ``reader.resolve(ref)`` when it has one.
    # see design/superpowers/specs/2026-10-14-graphify-upgrade-design.md (D2)"""
    original = ref.file_path
    path = normalise_anchor_path(original, root, reader)
    if path != original:
        # A name that was the path itself is the same path in its new spelling, so it still folds
        # to the file's basename; an answer computed on the old spelling no longer holds.
        name = path if path and original and ref.name.strip() == original.strip() else ref.name
        ref = Descriptor(name=name, file_path=path)
        result = None
    if ref.file_path or reader is None:
        return ref
    answer = result if result is not None else reader.resolve(ref)
    if answer.status != "resolved" or not answer.file_path:
        return ref
    known = store.resolve_descriptor(ref.name, None)
    if known is not None and known.descriptor is not None:
        if known.descriptor.file_path:
            return ref  # adoption
        upgraded = Descriptor(
            name=file_anchor_name(ref.name, answer.file_path), file_path=answer.file_path
        )
        if store.find_entity(upgraded.name, upgraded.file_path) is None:
            return ref  # a path-less entity keeps its history
        return upgraded
    return Descriptor(name=file_anchor_name(ref.name, answer.file_path), file_path=answer.file_path)


class AnchorResolution(list[AnchorBinding]):
    """``resolve_and_bind``'s return value: behaves exactly like the ``list[AnchorBinding]``
    it always returned (every existing caller iterates/indexes/``len()``s it as a plain
    list — this stays a drop-in) but additionally carries the underlying
    ``reader.resolve()`` outcome, so a caller that wants per-anchor feedback (Gate-5 finding
    S3: "ambiguous" anchors reported back to the human/agent) doesn't have to re-resolve the
    same ref a second time just to learn WHY no leaf binding was created.

    ``status`` is ``engine.reader.ResolveResult``'s (``"resolved"`` | ``"ambiguous"`` |
    ``"unresolved"``); ``candidates`` is only non-empty when ambiguous, so a collapsed stub
    answer (resolved) reports no candidates.
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
    relation: Relation | None = None,
    *,
    collapse: bool = True,
) -> AnchorResolution:
    """``relation`` (optional) overrides the default "affects" on the leaf + Tier-1 bindings
    created for THIS anchor (see docs/concepts/anchoring.md#multi-anchor-at-capture). The
    Tier-0 initiative binding is decision-level, not per-anchor: the write path makes it.

    Returns an :class:`AnchorResolution` — a ``list[AnchorBinding]`` in every respect a
    caller cares about, plus ``.status``/``.candidates`` for callers that want to report
    ambiguous anchors back without a second ``reader.resolve()`` call. ``collapse=False`` asks
    for the answer a reading surface gets, where identical path-less stubs stay ambiguous.
    """
    # Normalise the path (and give a path-less ref its node's file) BEFORE resolving: an answer
    # computed on a backslash or absolute spelling would orphan a ref that resolves once spelled
    # the way the graph does.
    roots = anchor_roots(store.path) if ref.file_path and _is_absolute(ref.file_path) else None
    ref = anchor_descriptor(ref, reader, store, root=roots)
    result = reader.resolve(ref, collapse=collapse)
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
    node_rejected = False

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
            node_rejected = True
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
    if node_rejected:
        probe: list[str] = []
    elif result.collapsed:
        # Identical path-less stubs (D4) stand behind one answer: a domain is picked only when
        # it holds every one of them, never because it holds the representative.
        probe = list(result.candidates)
    elif result.node_id is not None:
        probe = [result.node_id]
    else:
        probe = list(result.candidates)
    if probe or effective_community is not None:
        # One writer reservation from the membership check to the binding (the caller's when it
        # holds one, as the fact tools do): no status change or membership pass lands between
        # the pick and the write.
        with store._mutation():
            domain, identity = _covering_domain(store, reader, probe, effective_community)
            domain_entity = None
            if domain is not None:
                domain_entity = store.get_or_create_abstract_entity(f"domain:{domain.slug}")
                # git takes no lock, so a pull can rewrite the snapshot at any moment, and back
                # again. The pick came from rows certified for ``identity``: bind its domain only
                # when that is still the identity on disk, right before the binding.
                if identity is not None and membership_stamp(store, reader) != identity:
                    domain_entity = None
            if domain_entity is not None:
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
            elif effective_community is not None:
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

    return AnchorResolution(
        bindings, result.status, result.candidates if result.status == "ambiguous" else []
    )


def _covering_domain(
    store: Store, reader: GraphifyReader, node_ids: list[str], community: str | None
) -> tuple[Domain | None, str | None]:
    """The newest ACCEPTED domain that holds every node in ``node_ids`` (the membership index,
    whatever the nodes' communities), or, when ``community`` is given, whose
    ``communities`` list it. Called inside the writer reservation of ``resolve_and_bind``'s
    Tier-1 block, which its own ``_mutation()`` joins: the index is rebuilt when it is not
    certified for the current identity and read in the same reservation as the binding is
    written, so no status change lands in between. Only membership certified for that identity
    is read (``membership_exclusions``): none at all when nothing is (a pull landed during the
    rebuild, or the pass failed as a whole), so no domain is guessed. A listed community holds
    only member nodes, so the two halves never disagree. A reader that cannot tell its identity
    (the rebuild raised) leaves the pick to ``Domain.communities`` alone, the rule before the
    index. Loss of the owning transaction propagates instead of taking that fallback.
    Returns the pick and the identity whose certified rows it came from (``None`` for a
    pick from ``Domain.communities`` alone), which the caller compares with the identity on
    disk right before it binds.
    # see design/superpowers/specs/2026-10-14-graphify-upgrade-design.md (D5.3)"""
    with store._mutation():
        try:
            ensure_domain_membership(store, reader)
            identity = membership_stamp(store, reader)
            excluded = membership_exclusions(store, reader, identity)
        except Exception:  # best-effort only while this reservation still exists
            if not store._conn.in_transaction:
                raise  # preserve the owning caller's transaction loss through this fallback
            return _pick_domain(store, [], community, frozenset()), None
        if excluded is None:
            return None, identity
        return _pick_domain(store, node_ids, community, excluded), identity


def _pick_domain(
    store: Store, node_ids: list[str], community: str | None, excluded: frozenset[str]
) -> Domain | None:
    """The newest accepted domain holding every node of ``node_ids`` (by rows), or listing
    ``community``, never one in ``excluded``."""
    found: dict[str, Domain] = {}
    if node_ids:
        common: dict[str, Domain] | None = None
        for nid in node_ids:
            here = {d.domain_id: d for d in store.find_domains_for_node(nid)}
            common = here if common is None else {k: v for k, v in common.items() if k in here}
        found.update({k: v for k, v in (common or {}).items() if k not in excluded})
    if community is not None:
        for d in store.find_domains_by_community(community):
            if d.domain_id not in excluded:
                found.setdefault(d.domain_id, d)
    return found[max(found)] if found else None


def orphan_reason(ref: Descriptor, reader: GraphifyReader) -> str:
    """Why an anchor resolved to nothing — the three causes need three different fixes.

    - ``"file-not-in-graph"``: the graph carries no node for ``ref.file_path`` at all. The
      NAME may be perfectly correct; the graph simply does not cover that file yet. On the
      airflow corpus this was 9 of 15 orphans — the graph was built 2026-07-29 and the files
      were written 2026-07-30. Fix: rebuild the graph (``githooks.GRAPH_REBUILD``), then re-anchor.
      Also covers a typo'd path and a file type the engine does not index.
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
