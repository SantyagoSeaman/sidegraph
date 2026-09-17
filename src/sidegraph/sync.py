"""Sync / rebinding job (Stage 6): memory that stays anchored as code evolves.

After a Graphify rebuild, node ids shift. ``sync`` re-resolves every tracked concrete
entity with a deterministic ladder — exact (name+file) -> moved (unique name-only AND the
old path confirmed gone from disk; the descriptor follows the file) -> ambiguous ->
orphaned — healing or degrading tier-2 leaf bindings (never deleting) and refreshing the
durable->engine mapping. Gated on
``graph_version`` vs the store's ``last_synced_graph_version`` meta stamp, so the lazy
read-path invocation is a cheap no-op in the common case. No LLM, no fuzzy matching
(deferred). See docs/guides/surviving-refactors.md for the rebinding mechanics.

Mind-model layer (see docs/concepts/mind-model.md): the same pass also refreshes each
accepted ``Domain``'s ``communities`` mapping from BOTH its ``path_prefixes`` (a directory
sweep) and its ``seed_anchors`` (durable entity anchors resolved via ``reader.resolve`` —
§2a amendment, design/superpowers/specs/2026-07-08-domain-onboarding-design.md: a curated
domain anchors to entities, not volatile Leiden community ids, so membership survives a
fresh clone/rebuild), conservative and abstain-on-ambiguity throughout (same style as
``_observed_community_for_orphan``), flags domains that recomputed to empty, caps and flags
a claim that would newly cover more than 20% of all current communities rather than
writing it (Gate-5 blocker — see
``_DOMAIN_CLAIM_CAP``/``_recompute_domain_communities``), and precomputes the SessionStart
TOC cache. ``refresh_domain_communities_now`` resolves a single domain immediately
(ratify-time, bypassing the ``graph_version`` gate) so a newly-accepted domain's membership
shows up without waiting for the next graph rebuild.

Git-native store wave (see docs/reference/store-format.md): the report also surfaces
``Store.domain_slug_conflicts()`` — cross-branch domain-slug races — as
``SyncReport.slug_conflicts``. Sync neither detects nor caches these itself, only calls
the store's method, which recomputes live on every call (a single indexed query).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from pydantic import BaseModel

from .doctor import scan_code_drift
from .engine.reader import ANCHORABLE_FILE_TYPES, GraphifyReader, NodeRef
from .retrieval import DRIFT_CACHE_KEY, TOC_CACHE_KEY, build_toc, drifted_record_ids
from .schema import (
    AnchorBinding,
    DecisionStatus,
    Descriptor,
    Domain,
    DomainStatus,
    Entity,
    matches_path_prefix,
)
from .store import VOLATILE_STALE_KEY, Store
from .verify import _run_git

LAST_SYNCED_KEY = "last_synced_graph_version"


class RebindOutcome(BaseModel):
    entity_id: str
    canonical_name: str
    status: str  # unchanged | rebound | moved | ambiguous | orphaned | error
    node_id: str | None = None
    detail: str | None = None
    repointed: int = 0


def _set_leaf_status(entity_id: str, store: Store, status: str) -> None:
    """Transition all tier-2 (leaf) bindings on an entity. Status change only, never delete."""
    for b in store.bindings_for_entity(entity_id):
        if b.tier == 2 and b.status != status:
            b.status = status
            store.add_binding(b)


def _repoint_communities(entity: Entity, new_community: str | None, store: Store) -> int:
    """Re-point Tier-1 bindings when Leiden renumbered this entity's community.

    Community ids are snapshot labels, not identities — every rebuild may renumber them
    (dogfood: 149 -> 19). For each decision leaf-bound to this entity: ensure a live Tier-1
    binding to the current community and flip the binding to the old baseline community to
    orphaned (status-only; append-only preserved). Returns the number of decisions re-pointed.
    """
    old = entity.last_seen_community
    if new_community is None or new_community == old:
        return 0
    old_entity = store.find_abstract_entity(f"community:{old}") if old is not None else None
    new_entity = store.get_or_create_abstract_entity(f"community:{new_community}")

    repointed = 0
    for b in store.bindings_for_entity(entity.entity_id):
        if b.tier != 2:
            continue
        repointed += 1
        store.add_binding(
            AnchorBinding(
                record_id=b.record_id,
                entity_id=new_entity.entity_id,
                tier=1,
                status="live",
            )
        )
        if old_entity is not None:
            for tb in store.bindings_for_record(b.record_id):
                if (
                    tb.tier == 1
                    and tb.entity_id == old_entity.entity_id
                    and tb.status != "orphaned"
                ):
                    tb.status = "orphaned"
                    store.add_binding(tb)
    return repointed


def _observed_community_for_orphan(desc: Descriptor, reader: GraphifyReader) -> str | None:
    """The community of an orphaned entity's surviving file, when unambiguous.

    A symbol rename orphans the entity but usually leaves its file in place — and the same
    rebuild may renumber every community (dogfood: 149 -> 19 -> 256 across three rebuilds).
    The file's cluster is a deterministic, no-guessing locus for re-pointing: we are not
    guessing which node the entity became, only where its code lives now.
    """
    if desc.file_path is None:
        return None
    communities = {
        n.community for n in reader.nodes_in_file(desc.file_path) if n.community is not None
    }
    return communities.pop() if len(communities) == 1 else None


def _repoint_off_path(entity: Entity, community: str | None, store: Store) -> int:
    """Re-point on a non-adopted rung: bindings + baseline only; the node mapping is
    NEVER touched here (never guess the entity)."""
    if community is None:
        return 0
    repointed = _repoint_communities(entity, community, store)
    if entity.last_seen_community != community:
        entity.last_seen_community = community
        store.upsert_entity(entity)
    return repointed


def _adopt(entity: Entity, node_id: str, version: str, store: Store, community: str | None) -> int:
    repointed = _repoint_communities(entity, community, store)
    entity.last_seen_node_id = node_id
    entity.last_seen_graph_version = version
    entity.last_seen_community = community
    store.upsert_entity(entity)
    _set_leaf_status(entity.entity_id, store, "live")
    return repointed


def _resolve_repo_root(reader: GraphifyReader) -> Path | None:
    """Best-effort git worktree root containing ``reader``'s ``graph.json``, used ONLY by
    the "moved" rung below (see ``rebind_entity``) to check whether an entity's OLD
    ``file_path`` is still sitting on disk before trusting a name-only hit as a move. Same
    ``git rev-parse --show-toplevel`` call ``verify._find_repo_root``/
    ``doctor.scan_code_drift`` already use to answer this same class of question ("is this
    repo-relative path real"), but never raises: a graph outside any git working tree, or
    git being unavailable, degrades to ``None`` (mirrors ``doctor.py``'s own
    ``repo_root_failed`` tolerance) -- see the fail-closed consequence of that below.

    Deliberately keyed off the READER's location, not the store's: entity ``file_path``
    descriptors are relative to the checkout the graph was built from, and the store (a
    directory of small JSON files) is routinely copied elsewhere for safe inspection --
    e.g. to sync a copy against the real, uncopied graph without touching the committed
    store -- which would make a store-rooted lookup report "not a git repo" and silently
    fall back to treating every candidate as unverifiable even when the real checkout is
    right there. Called once per real (non-skipped) sync pass, not per entity -- one
    subprocess call, not O(entities).

    A ``None`` return means the moved rung CANNOT verify anything for this whole pass --
    every candidate that would otherwise adopt instead fails closed to orphaned (see the
    rung's own comment for why unverifiable is treated as "not gone", not "gone"). This
    matters for non-git corpora: ``engine/reader.py``'s own ``graph_version()`` already
    treats "no ``built_at_commit``" as an ordinary, supported case (falls back to a content
    hash), so a document corpus with no ``.git`` at all is expected to reach this code path
    routinely, not just on a misconfigured git checkout.
    """
    try:
        result = _run_git(["rev-parse", "--show-toplevel"], cwd=reader.path.parent, timeout=5.0)
    except ValueError:
        return None
    if result.returncode != 0:
        return None
    return Path(result.stdout.strip()).resolve()


def rebind_entity(
    entity: Entity, store: Store, reader: GraphifyReader, repo_root: Path | None = None
) -> RebindOutcome:
    """One entity through the deterministic rebind ladder.

    ``repo_root`` backs the "moved" rung's file-still-on-disk guard (see the comment at
    that rung) -- ``sync()`` resolves it once per pass via ``_resolve_repo_root`` and
    threads it through every call. Direct callers (tests, one-off scripts) that omit it
    get the FAIL-CLOSED default: the moved rung cannot verify the old path is gone, so it
    never adopts, regardless of how unique or same-suffix the name-only hit is -- see the
    rung's own comment for why "can't tell" and "confirmed gone" must not be conflated.
    """
    version = reader.graph_version()
    # concrete entities always carry a descriptor — see Store.iter_concrete_entities, the
    # only real-world source of entities passed here (tests construct the same invariant by
    # hand). Narrowing once here, rather than at every `entity.descriptor` use below, keeps
    # the ladder's control flow readable.
    assert entity.descriptor is not None
    desc = entity.descriptor
    base = {"entity_id": entity.entity_id, "canonical_name": entity.canonical_name}

    exact = reader.resolve(desc)
    if exact.status == "resolved":
        # GraphifyReader.resolve() only returns status="resolved" with node_id set (see
        # engine/reader.py) — never both "resolved" and node_id=None.
        assert exact.node_id is not None
        unchanged = exact.node_id == entity.last_seen_node_id
        repointed = _adopt(entity, exact.node_id, version, store, exact.community)
        return RebindOutcome(
            **base,
            status="unchanged" if unchanged else "rebound",
            node_id=exact.node_id,
            repointed=repointed,
        )
    if exact.status == "ambiguous":
        _set_leaf_status(entity.entity_id, store, "degraded")
        repointed = _repoint_off_path(entity, exact.community, store)
        return RebindOutcome(
            **base,
            status="ambiguous",
            detail=f"{len(exact.candidates)} candidates",
            repointed=repointed,
        )

    # Exact miss: the file may have moved — retry name-only and follow a unique hit,
    # but only when BOTH hold:
    #  - same file type (a unique cross-suffix hit, e.g. a vanished code symbol colliding
    #    with a doc heading, is a collision, not a move, and is never adopted), and
    #  - the OLD path is CONFIRMED gone from disk. An exact miss has two distinct causes,
    #    not one: the file genuinely moved (old path gone, the name now lives elsewhere),
    #    or the old path was never in the graph's scope at all (e.g. a directory Graphify
    #    doesn't index) and the miss is permanent and has nothing to do with a move. Both
    #    look IDENTICAL to the graph — it has no nodes at the old path either way — so the
    #    graph alone cannot tell them apart; only the filesystem can (bug, observed live on
    #    this repo: design/whitepaper/figures/{render.py,README.md} — out of Graphify's
    #    scope, never moved — got silently re-anchored onto unrelated same-named files
    #    elsewhere in the repo).
    #
    #    "Confirmed gone" requires repo_root AND the path's absence there — it is NOT the
    #    same as "unknown". When repo_root is unknown (direct rebind_entity callers with no
    #    repo_root, or a non-git store — see _resolve_repo_root), this rung FAILS CLOSED:
    #    it never adopts, same as a cross-suffix collision. Conflating "can't verify" with
    #    "verified gone" is exactly the bug this rung exists to fix, just one level up —
    #    the asymmetry is deliberate: an unadopted real move degrades to orphaned, which is
    #    visible (doctor/heal-anchors surfaces it) and repairable; a wrong adoption is
    #    silent and makes a record describe code it has nothing to do with. Same
    #    abstain-over-guess discipline this codebase already applies elsewhere (e.g.
    #    doc_import.py's ambiguous-anchor handling, the community-derivation cap above).
    if desc.file_path is not None:
        loose = reader.resolve(Descriptor(name=desc.name))
        if loose.status == "resolved":
            # same resolve() invariant as the exact-match branch above.
            assert loose.node_id is not None
            node = reader.get_node(loose.node_id)
            new_file = node.file_path if node is not None else None
            same_suffix = (
                new_file is not None
                and PurePosixPath(new_file).suffix == PurePosixPath(desc.file_path).suffix
            )
            old_confirmed_gone = repo_root is not None and not (repo_root / desc.file_path).exists()
            if same_suffix and old_confirmed_gone:
                entity.descriptor = Descriptor(name=desc.name, file_path=new_file)
                repointed = _adopt(entity, loose.node_id, version, store, loose.community)
                return RebindOutcome(
                    **base,
                    status="moved",
                    node_id=loose.node_id,
                    detail=f"{desc.file_path} -> {new_file}",
                    repointed=repointed,
                )
            # fall through to orphan: a cross-suffix hit (collision, not a move), the old
            # path is still on disk (out-of-scope file that never moved), or repo_root is
            # unknown and the old path's fate can't be verified either way (fail closed)
        if loose.status == "ambiguous":
            _set_leaf_status(entity.entity_id, store, "degraded")
            repointed = _repoint_off_path(entity, loose.community, store)
            return RebindOutcome(
                **base,
                status="ambiguous",
                detail=f"{len(loose.candidates)} candidates",
                repointed=repointed,
            )

    observed = _observed_community_for_orphan(desc, reader)
    repointed = _repoint_off_path(entity, observed, store)
    _set_leaf_status(entity.entity_id, store, "orphaned")
    return RebindOutcome(**base, status="orphaned", repointed=repointed)


# Refresh claim cap (Gate-5 blocker): a path-derived candidate community set that would
# newly claim more than this fraction of ALL current communities is never written — see
# _recompute_domain_communities. Same 20% figure AND, since Option A (owner-approved
# 2026-09-14), the same ratio by construction as domains._PREFIX_BREADTH_CAP (the
# bootstrap-time derivation guard): both count the claimed set with the candidate's own
# community included, over the same total-community denominator. Applied here on the
# refresh side as defense in depth: bootstrap's guard keeps an AUTO-DERIVED rule from ever
# producing a candidate this cap would reject AGAINST THE GRAPH IT WAS DERIVED FROM — that
# guarantee says nothing about a LATER rebuild (a directory one community owns exclusively
# today can be shared by five tomorrow, and this cap rejecting the rule then is the design
# working, not a contradiction of bootstrap's own guard). A human can still hand-author
# (`sidegraph-domains add --path`) or hand-edit a rule that is overbroad from the start,
# and this cap must never trust ANY rule's blast radius at face value regardless of who
# wrote it or when — see the
# docstring below and docs/guides/naming-your-domains.md.
_DOMAIN_CLAIM_CAP = 0.2


def _recompute_domain_communities(
    domain: Domain, nodes: list[NodeRef], current_community_ids: set[str], reader: GraphifyReader
) -> tuple[list[str], int, dict[str, int] | None]:
    """One domain's recomputed ``communities`` + the count of current anchorable nodes its
    ``path_prefixes`` matched, + an overbroad-claim marker (§6, conservative
    abstain-on-ambiguity style mirroring ``_observed_community_for_orphan``).

    Two INDEPENDENT sources of direct evidence feed the final candidate set (§2a
    amendment — design/superpowers/specs/2026-07-08-domain-onboarding-design.md):

    - ``path_prefixes``: the communities that >= 1 current anchorable node under one of
      those prefixes sits in (unchanged from before this amendment).
    - ``seed_anchors``: each :class:`Descriptor` resolved via ``reader.resolve(desc)`` —
      only a ``"resolved"`` (unambiguous) hit contributes its ``.community``; an
      ``"unresolved"`` or ``"ambiguous"`` anchor is skipped outright (never guess which
      community an ambiguous or vanished entity belongs to — same abstain discipline
      ``rebind_entity`` uses for a decision's own leaf anchors).

    ``_DOMAIN_CLAIM_CAP`` (20% of all current communities) applies to the ``path_prefixes``
    contribution ONLY, never to ``seed_anchors`` (fix, found dogfooding on a big C++
    monorepo: a domain with a broad path AND precise seed_anchors — e.g. `path_prefixes=
    ["src"]` sweeping 385 of 899 communities alongside a handful of hand-picked anchors —
    used to have its ENTIRE membership zeroed, including the anchors, because the old code
    unioned both contributions first and capped the union. The cap exists to stop a FUZZY
    directory sweep from silently claiming half the graph (Gate-5 blocker: a path rule that
    legitimately-per-the-old-code expanded a domain from its own community to 146 of 304 —
    silent, append-only-permanent corruption of every decision anchored to it since);
    ``seed_anchors`` are deliberate, per-entity authoring — as trustworthy as a hand-picked
    list — and must never be discarded just because an accompanying path rule turned out to
    be too broad):

    - If the ``path_prefixes`` candidate alone would newly claim more than
      ``_DOMAIN_CLAIM_CAP`` of all current communities, that contribution is REJECTED and
      the caller is told via the third tuple element (``{"matched": <path candidate
      community count>, "total": <all current communities>}``, surfaced through
      ``SyncReport.overbroad_domains`` — "your path rule is too broad", regardless of
      whether anchors happened to rescue membership this pass) — but ``seed_anchors``'
      resolved communities are ALWAYS still included.
    - The final candidate is (the path contribution, if under the cap) UNION (the anchor
      contribution, always). When non-empty, it is deterministic, direct evidence and fully
      REPLACES the recorded set (never a blind union with stale ids) — the anchors/
      stabilizer rules win over memory.
    - When the final candidate is empty (no path_prefixes/seed_anchors, none of them match/
      resolve anything today, or the path was capped and there are no anchors to rescue it),
      fall back to survivors: the subset of the domain's previously recorded community ids
      that still exist somewhere in the current graph — "add nothing speculative", never
      guessing a NEW community without direct evidence. A capped path with no anchors keeps
      the domain's previous mapping exactly as before this fix.

    This cap is unconditional over the ``path_prefixes`` contribution — it applies
    regardless of how that domain was authored (bootstrap-derived, ``add --path``,
    ``propose_domains``, or hand-edited). Bootstrap's own derivation guard
    (``domains._derive_path_prefixes``) already keeps an auto-derived rule from ever being
    this broad against the graph it was derived from — a later rebuild can still grow a
    directory's share past the cap, and this refresh guard capping the rule then is that
    design working, not bootstrap's guarantee failing. A manually-authored rule is
    explicit human intent and is never blocked at write time (see ``domains.py``'s module
    docstring / ``docs/guides/naming-your-domains.md``) — refresh is the one place that
    must still protect against a rule (of ANY provenance) whose blast radius turns out to
    be this large once it meets the live graph, since a human authoring `--path tests` has
    no way to know in advance
    how many communities that will resolve to on a 300-community repo.

    Single-community floor: a PATH candidate that resolves to exactly ONE community is
    NEVER capped, regardless of ratio — one community cannot possibly "swallow" anything
    else, so the cap has nothing to protect against there. Without this floor there is a
    dead zone on small graphs: with few total communities, `1/total` can legitimately
    exceed 20% for a perfectly ordinary single-community claim (e.g. 1 of 3), and capping
    it would freeze the domain's stale pre-renumber mapping forever with no way to ever
    heal (every future recompute re-derives the same single-community candidate and gets
    capped again). On a tiny graph, a genuine MULTI-community PATH claim can still
    legitimately trip the cap — when it does, the warning means re-scope the domain
    (supersede) or narrow `path_prefixes`, not a signal that the cap itself is wrong for
    that graph size. ``seed_anchors`` have no floor to worry about — they are never capped
    at all, of any size.
    """
    matched_count = 0
    prefix_candidate: set[str] = set()
    if domain.path_prefixes:
        matched = [
            n
            for n in nodes
            if n.file_type in ANCHORABLE_FILE_TYPES
            and n.file_path is not None
            and any(matches_path_prefix(n.file_path, p) for p in domain.path_prefixes)
        ]
        matched_count = len(matched)
        prefix_candidate = {n.community for n in matched if n.community is not None}

    anchor_candidate: set[str] = set()
    for desc in domain.seed_anchors:
        result = reader.resolve(desc)
        if result.status == "resolved" and result.community is not None:
            anchor_candidate.add(result.community)

    # Cap the PATH contribution alone (never the anchors): an overbroad sweep is rejected
    # and flagged, but seed_anchors — precise, hand-picked evidence — are never discarded
    # just because an accompanying path rule turned out to be too wide.
    total = len(current_community_ids)
    cap_hit: dict[str, int] | None = None
    if (
        prefix_candidate
        and total > 0
        and len(prefix_candidate) > 1
        and len(prefix_candidate) / total > _DOMAIN_CLAIM_CAP
    ):
        cap_hit = {"matched": len(prefix_candidate), "total": total}
        prefix_candidate = set()  # rejected; anchor_candidate still counts below

    candidate = sorted(prefix_candidate | anchor_candidate)
    if candidate:
        return candidate, matched_count, cap_hit

    if cap_hit is not None:
        # Path was overbroad and no seed_anchors rescued it: keep the previous mapping
        # unchanged (never zero it, never trust an over-claim just because refresh
        # computed it) — same never-guess outcome as before this fix.
        return sorted(domain.communities), matched_count, cap_hit

    survivors = sorted(cid for cid in domain.communities if cid in current_community_ids)
    return survivors, matched_count, None


def _refresh_domains(
    store: Store, reader: GraphifyReader
) -> tuple[int, list[dict], list[dict], list[dict]]:
    """Refresh ``communities`` on every ACCEPTED domain; return (refreshed count, empty
    domains, overbroad domains, failures). A domain is flagged empty when its recomputed
    set is empty AND its ``path_prefixes`` matched zero current nodes — a domain whose
    stabilizer still points at live code is never flagged even if none of those nodes
    carry a community label. A domain whose ``path_prefixes`` claim tripped the refresh
    cap (see ``_recompute_domain_communities``/``_DOMAIN_CLAIM_CAP``) is always flagged
    overbroad — "your path rule is too broad" — regardless of outcome: if
    ``seed_anchors`` rescued the domain, its (anchor-only) communities are still written
    and counted as refreshed; if nothing rescued it, its previous mapping was kept, not
    zeroed, so it is never also flagged empty. Domains never orphan (they're owned); both
    empty and overbroad are routed to a human via the report, status is never
    auto-changed (never guess). A domain whose refresh raises is isolated (see the loop
    below) and reported in ``failures`` instead of aborting the pass.
    """
    nodes = reader.list_nodes()
    current_community_ids = {n.community for n in nodes if n.community is not None}

    refreshed = 0
    empty: list[dict] = []
    overbroad: list[dict] = []
    failures: list[dict] = []
    # `reader.list_nodes()` above stays outside any try on purpose: a reader that
    # constructed has already parsed its graph, and list_nodes is a list copy.
    for domain in store.iter_domains(status=DomainStatus.ACCEPTED):
        # The try encloses the WRITE as well as the recompute. refresh_domain_communities
        # raises ValueError if the domain vanished mid-pass (concurrent supersede — it
        # re-fetches by id) and sqlite3.OperationalError under cross-process lock
        # contention, which the widened gate makes ordinary: a SessionStart hook and the
        # MCP server now both heal after the same pull. Wrapping only the recompute lets
        # those escape, abort the pass, skip every remaining domain, and appear in no
        # report — the exact invisibility this task exists to kill.
        try:
            communities, matched, cap_hit = _recompute_domain_communities(
                domain, nodes, current_community_ids, reader
            )
            if cap_hit is not None:
                overbroad.append({"slug": domain.slug, "title": domain.title, **cap_hit})
            if communities != sorted(domain.communities):
                store.refresh_domain_communities(domain.domain_id, communities)
                refreshed += 1
            if not communities and matched == 0:
                empty.append({"slug": domain.slug, "title": domain.title})
        except Exception as exc:  # one bad domain must not cost the others their heal
            failures.append(
                {
                    "slug": domain.slug,
                    "title": domain.title,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            continue
    return refreshed, empty, overbroad, failures


def refresh_domain_communities_now(
    domain: Domain, store: Store, reader: GraphifyReader
) -> tuple[list[str], dict | None]:
    """Resolve ONE domain's ``communities`` immediately, bypassing sync's ``graph_version``
    gate (§2a amendment) — called right after a domain is ACCEPTED (see
    ``server._ratify_impl``) so ``seed_anchors``/``path_prefixes`` membership shows up in
    ``drill_down`` the instant a human picks a set, instead of waiting for the next
    graph-rebuild-triggered ``sync`` pass. Reuses ``_recompute_domain_communities`` — same
    conservative, abstain-on-ambiguity resolution and the same ``_DOMAIN_CLAIM_CAP`` guard
    as the full pass, capping only the ``path_prefixes`` contribution: an overbroad path
    claim is rejected here exactly like it would be during a real sync, never written just
    because this call is immediate — but a domain's ``seed_anchors`` resolve and get
    written immediately regardless of the path cap, same as ``_refresh_domains``. No-op (no
    write) when the recomputed value doesn't change anything, same convention as
    ``Store.refresh_domain_communities``. Returns ``(communities, overbroad)`` — the
    (possibly unchanged) communities list, and ``{"slug", "title", "matched", "total"}`` (or
    ``None``) when the ``path_prefixes`` claim tripped ``_DOMAIN_CLAIM_CAP``. Callers wanting
    the flag no longer need the full ``sync`` pass's ``SyncReport.overbroad_domains`` — this
    immediate path returns it too, so the caller who just accepted the domain can tell the
    author their path rule was rejected right away.
    """
    nodes = reader.list_nodes()
    current_community_ids = {n.community for n in nodes if n.community is not None}
    communities, _matched, cap_hit = _recompute_domain_communities(
        domain, nodes, current_community_ids, reader
    )
    overbroad = (
        {"slug": domain.slug, "title": domain.title, **cap_hit} if cap_hit is not None else None
    )
    if communities == sorted(domain.communities):
        return sorted(domain.communities), overbroad
    store.refresh_domain_communities(domain.domain_id, communities)
    return communities, overbroad


@dataclass(frozen=True)
class DomainActivation:
    """Result of :func:`activate_accepted_domain` (design D2's shared per-domain
    post-accept step). ``resolved`` is the caller's cue for the pre-existing
    never-fail-a-ratify heal flag (``VOLATILE_STALE_KEY``, already set by this function
    when it applies — a caller never needs to set it itself); ``overbroad`` is
    ``refresh_domain_communities_now``'s own cap-hit dict (or ``None``); ``error`` is the
    exception text when the refresh raised, else ``None``. Deliberately renders NOTHING —
    the "path rule too broad" sentence is a literal trigger phrase for the
    ``sidegraph:heal-anchors`` skill and stays in the two human callers (``server.py``,
    ``cli.py``), which build their own message from ``overbroad``.
    """

    resolved: bool
    overbroad: dict | None
    error: str | None


def activate_accepted_domain(
    domain: Domain, store: Store, reader: GraphifyReader | None
) -> DomainActivation:
    """Resolve ONE just-accepted domain's membership immediately (design D2) — the shared
    helper extracted, behavior-preserving, from ``server._ratify_impl``/``cli.ratify_main``
    so MCP, CLI, and the two auto callers (capture's domain propose path,
    ``domains.bootstrap_domains``) all resolve membership through the same code. Contract,
    pinned so the extraction is provably pure:

    - one domain per call, so per-domain failure isolation survives (a caller loops over
      its own accepted-this-batch domain ids);
    - ``reader is None`` -> ``resolved=False, error=None``, and schedules the heal
      (``VOLATILE_STALE_KEY="1"``) without inventing a new human-facing error where there
      was none before (the pre-existing "no graph to resolve against" branch);
    - a raising ``refresh_domain_communities_now`` sets the SAME flag and returns the
      exception text as ``error`` — the pre-existing never-fail-a-ratify rule: membership
      could not be resolved, but the ratify/accept itself must never fail because of it;
    - never rebuilds the ``TOC_CACHE_KEY`` cache — the once-per-batch rebuild stays at the
      caller (``server.py``, ``cli.py``, and both auto callers rebuild once at the end of
      their own batch), so accepting N domains in one pass never pays N ``build_toc`` calls.
    # see design/superpowers/specs/2026-09-11-auto-ratification-policy-design.md D2
    """
    if reader is None:
        store.set_meta(VOLATILE_STALE_KEY, "1")
        return DomainActivation(resolved=False, overbroad=None, error=None)
    try:
        _communities, overbroad = refresh_domain_communities_now(domain, store, reader)
    except Exception as e:  # never fail a ratify because membership could not be resolved
        store.set_meta(VOLATILE_STALE_KEY, "1")
        return DomainActivation(resolved=False, overbroad=None, error=str(e))
    return DomainActivation(resolved=True, overbroad=overbroad, error=None)


class SyncReport(BaseModel):
    from_version: str | None
    to_version: str
    skipped: bool = False
    outcomes: list[RebindOutcome] = []
    stale_decisions: list[dict] = []  # {"id": ..., "title": ...}
    domains_refreshed: int = 0
    empty_domains: list[dict] = []  # {"slug": ..., "title": ...}
    overbroad_domains: list[dict] = []  # {"slug", "title", "matched", "total"} — see
    # _DOMAIN_CLAIM_CAP; the path_prefixes contribution was dropped (any seed_anchors
    # still applied, so a rescued domain IS written); the path is flagged as noise
    slug_conflicts: list[dict] = []  # {"slug": ..., "domain_ids": [...]} — design §6's
    # cross-branch slug race; see
    # Store.domain_slug_conflicts (computed live on
    # every call, not cached)
    domain_failures: list[dict] = []  # {"slug", "title", "error"} — a domain whose refresh
    # raised. Isolated per domain so one malformed seed cannot cost every other domain its
    # heal, and reported because both lazy-sync callers suppress exceptions: a failure that
    # is not in this list is invisible, and the store silently never heals.

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for o in self.outcomes:
            out[o.status] = out.get(o.status, 0) + 1
        return out


def sync(store: Store, reader: GraphifyReader, force: bool = False) -> SyncReport:
    """Full rebind pass, gated on graph_version AND the volatile-reload flag. Stamps
    last_synced, and clears the reload flag, only on completion.

    Entities that error during rebind are not retried until the next graph-version change
    (or force=True) — the stamp records a completed visit, not universal success. The gate
    below skips the rebind ladder itself when the version already matches, the store's
    volatile state isn't cold (`VOLATILE_STALE_KEY`), and `force` wasn't passed — but the
    gate does NOT skip the TOC cache refresh: a skipped pass still rewrites `toc_cache` when
    at least one accepted domain exists, since that cache can go stale from content-only
    changes the graph never sees (see the gate's own comment below).
    """
    to_version = reader.graph_version()
    from_version = store.get_meta(LAST_SYNCED_KEY)
    # A canonical reload (pull, merge, branch switch, hand edit — even a bare touch, since
    # the digest hashes mtime_ns) resets every derived table but deliberately PRESERVES the
    # meta table, so graph_version alone cannot see that volatile state went cold. This is
    # the flag git-native design §3 introduced for exactly that event ("the next
    # sidegraph-sync / lazy sync rebuilds volatile state") — set on every reload since, and
    # read here for the first time.
    volatile_stale = store.get_meta(VOLATILE_STALE_KEY) == "1"
    if to_version == from_version and not volatile_stale and not force:
        # TOC cache is volatile state (§5/§6), and `sync` is the store's one volatile-refresh
        # verb (see docs/reference/store-format.md) — a content-only change (e.g. `add_decision`
        # bound to an already-accepted domain, via capture or import) never moves
        # graph_version, so the version gate above must not ALSO gate the cache refresh: doing
        # so left the cache stale until an unrelated domain accept/drop happened to rebuild it
        # (cli.py/server.py's own explicit ratify-time refresh), which real sessions hit (fix-
        # wave B, B6). `build_toc` is store-only (no reader dependency for its fields today) and
        # cheap, so it's safe to recompute on every sync call; gated on "at least one accepted
        # domain exists" purely to avoid a pointless write when there is nothing to refresh.
        if next(store.iter_domains(status=DomainStatus.ACCEPTED), None) is not None:
            store.set_meta(TOC_CACHE_KEY, json.dumps(build_toc(store, reader)))
        return SyncReport(from_version=from_version, to_version=to_version, skipped=True)

    outcomes: list[RebindOutcome] = []
    # Resolved once for the whole pass (one subprocess call, not one per entity) and
    # threaded through every rebind_entity call — see _resolve_repo_root / the moved
    # rung's own comment for what this guards against and how it degrades when unknown.
    repo_root = _resolve_repo_root(reader)
    for entity in store.iter_concrete_entities():
        try:
            outcomes.append(rebind_entity(entity, store, reader, repo_root))
        except Exception as e:  # one bad entity must not abort the pass
            outcomes.append(
                RebindOutcome(
                    entity_id=entity.entity_id,
                    canonical_name=entity.canonical_name,
                    status="error",
                    detail=str(e),
                )
            )

    domains_refreshed, empty_domains, overbroad_domains, domain_failures = _refresh_domains(
        store, reader
    )

    stale: list[dict] = []
    for d in store.iter_decisions():
        if d.status in (DecisionStatus.SUPERSEDED, DecisionStatus.REJECTED):
            continue
        # Leaf-based: surviving Tier-1/Tier-0 bindings keep the decision retrievable
        # (the escalation), but do not vouch for the claim — the concrete code is gone.
        leaves = [b for b in store.bindings_for_record(d.id) if b.tier == 2]
        if leaves and all(b.status == "orphaned" for b in leaves):
            stale.append({"id": d.id, "title": d.title})

    store.set_meta(LAST_SYNCED_KEY, to_version)
    # Cleared HERE, beside the version stamp and BEFORE build_toc: volatile state has been
    # rebuilt by this point (the ladder, _refresh_domains and the stale scan all ran), and
    # the TOC is a render of that state, not part of it. Clearing after build_toc instead
    # let a persistent TOC failure leave the half-state "version stamped but flag set",
    # which the gate below reads as "heal me" on every single retrieval call.
    store.set_meta(VOLATILE_STALE_KEY, "0")
    # TOC precompute (§5/§6): recomputed AFTER the domain-community refresh above and the
    # last_synced stamp, so it reflects this pass's own updates; written on every completed
    # (non-skipped) pass, fresh or forced — a skipped pass returns above and never reaches
    # here, leaving the cache untouched.
    store.set_meta(TOC_CACHE_KEY, json.dumps(build_toc(store, reader)))
    return SyncReport(
        from_version=from_version,
        to_version=to_version,
        outcomes=outcomes,
        stale_decisions=stale,
        domains_refreshed=domains_refreshed,
        empty_domains=empty_domains,
        overbroad_domains=overbroad_domains,
        slug_conflicts=store.domain_slug_conflicts(),
        domain_failures=domain_failures,
    )


# Outcome statuses worth a human's attention -- "unchanged" (nothing happened) and
# "rebound" (same name+file_path descriptor match as last sync, but the resolved node id
# changed -- the mapping just heals itself) are noise; everything else is a heads-up. One
# filter, two consumers: ``report_as_dict``'s "outcomes" field below (shared by both
# ``sidegraph-sync --json`` and the ``sync_anchors`` MCP tool) and ``sidegraph-sync``'s own
# prose printer (``cli.sync_main``).
_SYNC_OUTCOME_NOTEWORTHY = ("moved", "orphaned", "ambiguous", "error")

# The subset of ``_SYNC_OUTCOME_NOTEWORTHY`` that makes ``--check``/``report_has_findings``
# fail (design/superpowers/specs/2026-07-11-ci-live-findings-design.md ruling 1). "moved" is
# noteworthy (surfaced in "outcomes") but not itself a finding -- the mapping healed itself,
# nothing needs a human's hand. "orphaned"/"ambiguous" are ALSO informational-only, not a
# finding: a live GitHub Actions experiment showed a legitimate rename+heal (triage adds a
# live anchor to the decision) leaves the renamed-away entity's own leaf orphaned for good --
# an append-only store has no retirement path -- so gating on the outcome kept the healing
# PR, and the default branch after it merged, red forever. When an orphaned/ambiguous anchor
# actually costs reachability (it was a decision's ONLY live tier-2 leaf), the decision goes
# stale and ``stale_decisions`` already fires -- the failure signal is redundant where it
# matters and harmful (permanently red) where it doesn't. Only "error" (the rebind ladder
# itself raised on an entity) stays a genuine failing outcome.
_FINDING_OUTCOME_STATUSES = ("error",)


def report_as_dict(report: SyncReport) -> dict:
    """The shared report shape both ``sidegraph-sync --json`` (``cli.sync_main``) and the
    ``sync_anchors`` MCP tool (``server._sync_anchors_impl``) return -- one dict shape, two
    callers (design/superpowers/specs/2026-07-11-ci-integrity-design.md ruling 1).

    ``synced`` is ``False`` when the pass was skipped outright (``graph_version``
    unchanged, no ``force``) -- every OTHER field is then an EMPTY default (``outcomes:
    []``, ``counts: ""``, ``repointed: 0``, ``stale_decisions: []``, ``empty_domains:
    []``, ``overbroad_domains: []``, ``slug_conflicts: []``, ``domains_refreshed: 0``,
    ``domain_failures: []``) from a fresh, un-run ``SyncReport(skipped=True)`` -- NOT a
    prior (possibly stale) report --
    so a caller must never read a skipped pass as "everything's clean" on its own;
    ``report_has_findings`` below happens to still read a skipped dict as clean (nothing to
    find), which is exactly the "version-skip is exit 0" contract ``--check`` wants.

    ``outcomes`` carries only entities worth a human's attention -- moved/orphaned/
    ambiguous/error -- filtered via ``_SYNC_OUTCOME_NOTEWORTHY``, never the "unchanged"/
    "rebound" majority, same filter ``sidegraph-sync``'s own printer applies. ``counts`` is
    ``report.counts()`` rendered as a string (e.g. ``"{'unchanged': 3}"``), ``""`` when
    nothing is tracked yet. ``repointed`` sums EVERY outcome's ``repointed``, not just the
    filtered ones.
    """
    outcomes = [
        {"status": o.status, "canonical_name": o.canonical_name, "detail": o.detail}
        for o in report.outcomes
        if o.status in _SYNC_OUTCOME_NOTEWORTHY
    ]
    counts = report.counts()
    return {
        "synced": not report.skipped,
        "from_version": report.from_version,
        "to_version": report.to_version,
        "counts": str(counts) if counts else "",
        "repointed": sum(o.repointed for o in report.outcomes),
        "outcomes": outcomes,
        "stale_decisions": report.stale_decisions,
        "empty_domains": report.empty_domains,
        "overbroad_domains": report.overbroad_domains,
        "slug_conflicts": report.slug_conflicts,
        "domains_refreshed": report.domains_refreshed,
        "domain_failures": report.domain_failures,
    }


def report_has_findings(d: dict) -> bool:
    """True iff a ``report_as_dict`` dict has an attention finding -- design/superpowers/
    specs/2026-07-11-ci-live-findings-design.md ruling 1: an ``error`` outcome, OR a
    non-empty ``stale_decisions``, OR a non-empty ``slug_conflicts``, OR a non-empty
    ``domain_failures``. ``orphaned``/``ambiguous`` outcomes and ``empty_domains``/
    ``overbroad_domains`` are deliberately excluded -- informational, never fail the check
    on their own. The orphaned/ambiguous exclusion is the live-experiment refinement: a
    legitimate rename+heal leaves the renamed-away entity's leaf orphaned for good (no
    retirement path in an append-only store), and that residue must not red-flag a healing
    PR -- or the default branch after it merges -- forever; when an orphaned/ambiguous
    anchor actually costs reachability (a decision's only live anchor), the decision goes
    stale and ``stale_decisions`` already fires, so the failure signal is covered there
    instead. Drives ``sidegraph-sync --check``'s exit code (2 on True, 0 on False); a
    skipped-pass dict (``synced: False``, everything else empty) always reads False, so a
    version-skip is exit 0, same as a clean report.
    """
    if any(o["status"] in _FINDING_OUTCOME_STATUSES for o in d["outcomes"]):
        return True
    # domain_failures joins the finding set while orphaned/ambiguous stay out, and the
    # 2026-07-11 exclusion is the reason why: those leave PERMANENT residue in an
    # append-only store (a renamed-away leaf has no retirement path), so gating on them
    # keeps CI red forever on a healthy repo. A domain_failure is the opposite shape — it
    # exists only while a refresh actually raises. But unlike an ordinary per-entity retry,
    # it does NOT reliably disappear once the seed/domain/graph is fixed: a completed pass
    # (this one) clears VOLATILE_STALE_KEY and stamps last_synced regardless of whether this
    # domain's own refresh succeeded, so the NEXT pass is gated on graph_version again and
    # never re-attempts an unfixed domain until the graph version moves or a caller passes
    # `--force`/`force=True`. Same class as an `error` outcome for `--check` purposes; fix
    # the cause, then re-run with `--force` to confirm it cleared.
    return bool(d["stale_decisions"] or d["slug_conflicts"] or d["domain_failures"])


def maybe_sync(store: Store, reader: GraphifyReader | None) -> SyncReport | None:
    """Lazy read-path trigger: no reader -> None; else sync (self-skips on version match).

    Callers wrap this best-effort — a sync failure must never break retrieval or startup.
    """
    if reader is None:
        return None
    return sync(store, reader)


def refresh_code_drift_cache(
    store: Store, *, repo_root: Path | None = None, deadline: float = 10.0
) -> int | None:
    """Rescan code drift and merge the result into the :data:`~sidegraph.retrieval.
    DRIFT_CACHE_KEY` meta cache — the write half of the drift→supersede affordance (D2).

    Placed in this module because hooks already import it and it writes derived meta
    today (the TOC cache) — by-convention placement; ``sync()`` deliberately does NOT
    call this (the SessionStart hook runs ``maybe_sync`` first, and a second scan there
    would double the git cost for nothing).

    ALWAYS rescans — no head-stamp no-op: "same HEAD ⇒ no new drift" is false under
    ``add_anchors`` (bindings-only write), sync rebinds flipping ``orphaned → live``,
    and backward HEAD movement. Cost is bounded by ``deadline`` (a TOTAL scan budget,
    see ``scan_code_drift``).

    Merge semantics, per capture commit (spec round-2 N1 — a partial git failure must
    neither erase real markers nor freeze the cache forever):

    - inactive scan (nothing commit-stamped and anchored) → return ``0``, write nothing;
    - unscanned (repo root resolution failed — no batch ran) → leave any existing cache
      byte-untouched, return ``None``. A failed HEAD resolution alone does NOT discard a
      scan whose batches ran (review M-2: ``head`` is provenance only and gates nothing
      — the cache is written with ``"head": null``);
    - otherwise: a successful batch REPLACES its commit's entry (an empty list is a
      normal write — that is how markers clear once a repair supersede plus its commit
      land); a failed batch RETAINS the previous cache's entry for its commit, if any;
      commits absent from the scan's batch set drop out entirely (their records left
      the live+stamped+anchored join).

    Returns the live-filtered flattened count (``drifted_record_ids`` — the one filter
    definition, shared with the render side). Never raises: any unexpected failure
    returns ``None`` with the cache untouched (hook-caller contract).
    # see design/superpowers/specs/2026-07-30-drift-supersede-affordance-design.md (D2)
    """
    try:
        scan = scan_code_drift(store.path, repo_root, deadline=deadline)
        if not scan.active:
            return 0
        if scan.repo_root_failed:
            return None

        prev_by_commit: dict[str, list[str]] = {}
        raw = store.get_meta(DRIFT_CACHE_KEY)
        if raw is not None:
            try:
                prev = json.loads(raw)
                if isinstance(prev, dict) and isinstance(prev.get("by_commit"), dict):
                    prev_by_commit = {
                        c: [r for r in ids if isinstance(r, str)]
                        for c, ids in prev["by_commit"].items()
                        if isinstance(ids, list)
                    }
            except json.JSONDecodeError:
                pass

        by_commit: dict[str, list[str]] = {}
        for batch in scan.batches:
            if batch.failed:
                if batch.commit in prev_by_commit:
                    by_commit[batch.commit] = prev_by_commit[batch.commit]
            else:
                by_commit[batch.commit] = sorted({e.record_id for e in batch.entries})
        store.set_meta(
            DRIFT_CACHE_KEY,
            json.dumps(
                {
                    "head": scan.head,
                    "computed_at": datetime.now(UTC).isoformat(),
                    "by_commit": by_commit,
                }
            ),
        )
        return len(drifted_record_ids(store))
    except Exception:
        return None
