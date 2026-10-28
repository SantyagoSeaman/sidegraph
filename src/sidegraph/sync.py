"""Sync / rebinding job (Stage 6): memory that stays anchored as code evolves.

After a Graphify rebuild, node ids shift. ``sync`` re-resolves every tracked concrete
entity with a deterministic ladder — exact (name+file) -> moved (unique name-only AND the
old path confirmed gone from disk AND that same move confirmed by COMMITTED git history —
see ``_committed_evidence_confirms_move``; unconfirmed evidence reports
``moved_uncommitted`` and leaves the binding untouched rather than guessing, remembering
the entity so a later sync re-verifies it once git HEAD moves, even when the graph did not
change — the dirty-tree guard, ``SIDEGRAPH_TRUST_DIRTY_TREE=on`` escapes it; the hit must also
be the same name, case kept, and git must show the new path arriving with the old one's
removal — see ``_move_adds_the_new_path``; a refusal falls through to orphaned) -> ambiguous
-> orphaned —
healing or degrading tier-2 leaf bindings (never deleting) and refreshing the
durable->engine mapping. Gated on
``graph_version`` vs the store's ``last_synced_graph_version`` meta stamp, so the lazy
read-path invocation is a cheap no-op in the common case. No LLM, no fuzzy matching
(deferred). See docs/guides/surviving-refactors.md for the rebinding mechanics.

Mind-model layer (see docs/concepts/mind-model.md): the same pass computes each accepted
``Domain``'s effective per-node membership from its frozen domain snapshot, when present,
or its ``path_prefixes`` and ``seed_anchors`` rules. It publishes ``domain_members`` and the
communities wholly contained in that membership together under a writer reservation. The
rules' community contribution keeps the 20% cap (``_DOMAIN_CLAIM_CAP``); every matching path
node is still a member. Failed refreshes retain prior membership and are reported until
retried. The pass also precomputes the SessionStart TOC cache.
``refresh_domain_communities_now`` refreshes a domain immediately at ratify time.

Index-only mode (``canonical_writes=False``, see ``sync``): a linked worktree reads the main
checkout's graph and syncs it into its own cold index. The pass refreshes every derived thing as
usual and abstains from the one thing that can touch a tracked file, the moved rung's adoption
(``_rebind_ladder``), in the full pass and in the narrow re-verification pass.
see design/superpowers/specs/2026-10-01-worktree-borrowed-graph-design.md (D2)

Git-native store wave (see docs/reference/store-format.md): the report also surfaces
``Store.domain_slug_conflicts()`` — cross-branch domain-slug races — as
``SyncReport.slug_conflicts``. Sync neither detects nor caches these itself, only calls
the store's method, which recomputes live on every call (a single indexed query).
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import NamedTuple

from pydantic import BaseModel

from . import domain_snapshot
from .doctor import SECTION_UNAVAILABLE, CodeDriftScan, scan_code_drift
from .engine.reader import ANCHORABLE_FILE_TYPES, GraphifyReader, NodeRef
from .retrieval import (
    DRIFT_CACHE_KEY,
    TOC_CACHE_KEY,
    build_toc,
    live_drifted_ids,
    recent_drift,
)
from .schema import (
    AnchorBinding,
    DecisionStatus,
    Descriptor,
    Domain,
    DomainStatus,
    Entity,
    matches_path_prefix,
    strip_decoration,
)
from .store import (
    MEMBERSHIP_FAILED_KEY,
    MEMBERSHIP_POPULATED_KEY,
    MEMBERSHIP_STAMP_KEY,
    VOLATILE_STALE_KEY,
    Store,
    StoreBusyError,
)
from .store_layout import snapshot_signature, stamp_cache
from .verify import _run_git

LAST_SYNCED_KEY = "last_synced_graph_version"
# Derived meta (design/superpowers/specs/2026-09-29-sync-pending-moves-and-shared-
# communities-design.md D1): the entities a pass left as ``moved_uncommitted`` plus the git
# HEAD it saw, as ``{"head": <sha or null>, "entity_ids": [...]}``. Committing the rename
# does not move graph.json's version, so without this the gate would skip forever.
PENDING_MOVES_KEY = "pending_uncommitted_moves"
# Derived meta: the ``(record_id, vacated communities)`` pairs whose Tier-1 reconcile
# raised or abstained, as ``{"<record_id>": ["<community>", ...]}``. The adopts had already
# committed and the pass stamps the graph version anyway, so without this the vacated set is
# lost and the stale ``community:*`` row stays live. Every ``sync()`` call retries these
# first; a pair is kept on an exception or an abstain and cleared when the reconcile
# settles.
PENDING_TIER1_META = "pending_tier1_reconcile"


class RebindOutcome(BaseModel):
    entity_id: str
    canonical_name: str
    status: str  # unchanged | rebound | moved | moved_uncommitted | ambiguous | orphaned
    # | error -- see _committed_evidence_confirms_move for what distinguishes "moved" from
    # "moved_uncommitted" (dirty-tree guard, defect fixed 2026-09-18).
    node_id: str | None = None
    detail: str | None = None
    repointed: int = 0


def _set_leaf_status(entity_id: str, store: Store, status: str) -> None:
    """Transition all tier-2 (leaf) bindings on an entity. Status change only, never delete."""
    for b in store.bindings_for_entity(entity_id):
        if b.tier == 2 and b.status != status:
            b.status = status
            store.add_binding(b)


def _repoint_communities(
    entity: Entity,
    new_community: str | None,
    store: Store,
    vacated: dict[str, set[str]] | None = None,
) -> int:
    """Re-point Tier-1 bindings when Leiden renumbered this entity's community.

    Community ids are snapshot labels, not identities — every rebuild may renumber them
    (dogfood: 149 -> 19). For each decision leaf-bound to this entity: ensure a live Tier-1
    binding to the current community (a new row takes the leaf's relation; an existing row
    keeps its own) and record ``(record_id, old_community)`` in ``vacated``. It does NOT
    orphan the old row: two leaves of one decision in one community share that one row, so
    only ``_reconcile_tier1_communities``, once every leaf has settled, may say whether
    anything still holds it (design/superpowers/specs/2026-09-29-sync-pending-moves-and-
    shared-communities-design.md D4). Returns the number of decisions re-pointed.
    """
    old = entity.last_seen_community
    if new_community is None or new_community == old:
        return 0
    new_entity = store.get_or_create_abstract_entity(f"community:{new_community}")

    repointed = 0
    for b in store.bindings_for_entity(entity.entity_id):
        if b.tier != 2:
            continue
        repointed += 1
        existing = next(
            (
                tb
                for tb in store.bindings_for_record(b.record_id)
                if tb.entity_id == new_entity.entity_id
            ),
            None,
        )
        if existing is None:
            store.add_binding(
                AnchorBinding(
                    record_id=b.record_id,
                    entity_id=new_entity.entity_id,
                    tier=1,
                    status="live",
                    relation=b.relation,
                )
            )
        elif existing.status != "live":
            store.add_binding(
                AnchorBinding(
                    record_id=b.record_id,
                    entity_id=new_entity.entity_id,
                    tier=1,
                    weight=existing.weight,
                    status="live",
                    relation=existing.relation,
                )
            )
        if vacated is not None and old is not None:
            vacated.setdefault(b.record_id, set()).add(old)
    return repointed


def _reconcile_tier1_communities(
    record_id: str, vacated: set[str], store: Store, graph_version: str
) -> bool:
    """Settle one record's Tier-1 ``community:*`` rows after every leaf has been rebound.

    Returns ``True`` when the record was settled (restored and orphaned as computed) and
    ``False`` when it abstained because a live/degraded leaf has no baseline. A raise means
    the reconcile failed.

    *held* is the ``last_seen_community`` of each live/degraded Tier-2 leaf. Restore: a row
    for a held community that was orphaned goes live again. Orphan: a row for a community a
    leaf *vacated* in this pass and no leaf holds any more. Skipped when a live/degraded
    leaf has no baseline (its community is unknown, so the record's communities cannot be
    told apart), except a live leaf on a path-less descriptor mapped on this graph version yet
    holding no community: identical external stubs in split communities (resolved, collapsed)
    hold none, so it neither joins *held* nor blocks the settle. A degraded leaf or an old mapping
    left by a failed rebind does not establish that the current answer holds none. Rows nothing
    vacated (an ambiguous anchor's Tier-1-only row, an orphan-rung repoint, inherited rows) and
    ``domain:*`` rows are
    never orphaned here. Status-only, append-only preserved. See design/superpowers/specs/
    2026-09-29-sync-pending-moves-and-shared-communities-design.md D4.
    """
    held: set[str] = set()
    unknown_baseline = False
    for b in store.bindings_for_record(record_id):
        if b.tier != 2 or b.status not in ("live", "degraded"):
            continue
        leaf = store.get_entity(b.entity_id)
        if leaf is None:
            continue
        if leaf.last_seen_community is None:
            if (
                b.status == "live"
                and leaf.descriptor is not None
                and leaf.descriptor.file_path is None
                and leaf.last_seen_node_id is not None
                and leaf.last_seen_graph_version == graph_version
            ):
                continue  # identical stubs in split communities: this leaf holds none
            unknown_baseline = True
        else:
            held.add(leaf.last_seen_community)

    def rows_for(community: str) -> list[AnchorBinding]:
        ent = store.find_abstract_entity(f"community:{community}")
        if ent is None:
            return []
        return [
            tb
            for tb in store.bindings_for_record(record_id)
            if tb.tier == 1 and tb.entity_id == ent.entity_id
        ]

    for community in held:
        for tb in rows_for(community):
            if tb.status == "orphaned":
                tb.status = "live"
                store.add_binding(tb)
    if unknown_baseline:
        return False
    for community in vacated - held:
        for tb in rows_for(community):
            if tb.status != "orphaned":
                tb.status = "orphaned"
                store.add_binding(tb)
    return True


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


def _repoint_off_path(
    entity: Entity,
    community: str | None,
    store: Store,
    vacated: dict[str, set[str]] | None = None,
) -> int:
    """Re-point on a non-adopted rung: bindings + baseline only; the node mapping is
    NEVER touched here (never guess the entity)."""
    if community is None:
        return 0
    repointed = _repoint_communities(entity, community, store, vacated)
    if entity.last_seen_community != community:
        entity.last_seen_community = community
        store.upsert_entity(entity)
    return repointed


def _adopt(
    entity: Entity,
    node_id: str,
    version: str,
    store: Store,
    community: str | None,
    vacated: dict[str, set[str]] | None = None,
) -> int:
    if (
        community is None
        and entity.last_seen_community is not None
        and entity.descriptor is not None
        and entity.descriptor.file_path is None
        and vacated is not None
    ):
        # A resolved path-less leaf now holds none. Record its known departure before the
        # old baseline is cleared; reconcile still respects siblings and unknown baselines.
        for binding in store.bindings_for_entity(entity.entity_id):
            if binding.tier == 2:
                vacated.setdefault(binding.record_id, set()).add(entity.last_seen_community)
    repointed = _repoint_communities(entity, community, store, vacated)
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
    ``git rev-parse --show-toplevel`` call ``verify._find_repo_root`` and
    ``verify.find_store_project_repo`` (``doctor.scan_code_drift``'s root) use to answer this
    same class of question ("is this repo-relative path real"), but never raises: a graph
    outside any git working tree, or git being unavailable, degrades to ``None`` (mirrors
    ``doctor.py``'s own ``repo_root_failed`` tolerance) -- see the fail-closed consequence of
    that below.

    Deliberately keyed off the READER's location, not the store's: entity ``file_path``
    descriptors are relative to the checkout the graph was built from, and the store (a
    directory of small JSON files) is routinely copied elsewhere for safe inspection --
    e.g. to sync a copy against the real, uncopied graph without touching the committed
    store (run from the checkout with an explicit ``--graph``: the default graph follows the
    store) -- which would make a store-rooted lookup report "not a git repo" and silently
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

    The repository is found from the graph's directory with git's repository-local
    variables (``GIT_DIR`` and friends) dropped, never from the ambient environment.
    """
    try:
        result = _run_git(["rev-parse", "--show-toplevel"], cwd=reader.path.parent, timeout=5.0)
    except ValueError:
        return None
    if result.returncode != 0:
        return None
    return Path(result.stdout.strip()).resolve()


def _committed_evidence_confirms_move(repo_root: Path, old_path: str, new_path: str) -> bool:
    """True only when a real, COMMITTED git history confirms the move the "moved" rung is
    about to adopt: ``old_path`` absent from HEAD's tree, AND ``new_path`` present in it.

    This is the guard the dirty-tree defect needed: the rung's own ``old_confirmed_gone``
    check (see its call site below) only proves the old path is missing from the WORKING
    TREE, which an uncommitted delete, rename, or ``git stash`` satisfies just as well as
    a real, shared move — the syncing person's local, unpushed (or even uncommitted)
    state would then be enough to make ``rebind_entity`` rewrite the entity's canonical,
    repo-committed ``descriptor`` for every clone that ever pulls this store. Requiring
    BOTH halves to be true AT HEAD closes that: a dirty tree can make the old path vanish
    from disk, but it cannot make git's HEAD lie about what was actually committed.

    Requires a resolvable HEAD first (``git rev-parse --verify -q HEAD``) — an empty
    repository (``git init`` with zero commits) has none, and this reports "unconfirmed"
    rather than treating "no HEAD to check" as "path confirmed absent", which would
    silently reopen the exact hole this function exists to close. Fails CLOSED — returns
    ``False`` — on any git failure (missing binary, timeout, an unreadable repo): "can't
    verify" must never collapse into "verified", same discipline
    ``_resolve_repo_root``'s own ``None`` return already applies one level up.
    """
    try:
        head = _run_git(["rev-parse", "--verify", "-q", "HEAD"], cwd=repo_root, timeout=5.0)
        if head.returncode != 0:
            return False  # no commits yet -- nothing is committed, so nothing is confirmed
        old_gone = (
            _run_git(["cat-file", "-e", f"HEAD:{old_path}"], cwd=repo_root, timeout=5.0).returncode
            != 0
        )
        new_present = (
            _run_git(["cat-file", "-e", f"HEAD:{new_path}"], cwd=repo_root, timeout=5.0).returncode
            == 0
        )
    except ValueError:
        return False
    return old_gone and new_present


def _tree_has(repo_root: Path, rev: str, path: str) -> bool | None:
    """Whether ``path`` is in ``rev``'s tree: ``True`` / ``False``, or ``None`` when git could
    not answer (a missing binary, a timeout, a non-zero exit). ``git ls-tree`` exits 0 with
    empty output for an absent path, so "absent" and "failed" never look alike here."""
    try:
        result = _run_git(["ls-tree", rev, "--", path], cwd=repo_root, timeout=5.0)
    except ValueError:
        return None
    if result.returncode != 0:
        return None
    return bool(result.stdout.strip())


def _move_adds_the_new_path(repo_root: Path, old_path: str, new_path: str) -> bool:
    """True only when git shows ``new_path`` was ADDED by the change that removed
    ``old_path`` -- the half of a move ``_committed_evidence_confirms_move`` cannot see: it
    only proves ``new_path`` is in HEAD now, which a file that was there all along satisfies
    just as well (a deleted type whose name matches an old member elsewhere).

    Fails CLOSED. Any git answer that is not a clear yes refuses: a non-zero exit, a git that
    cannot run, an empty answer where one is required, a shallow boundary. A refusal leaves
    the leaf orphaned, which is visible and repairable; a wrong adoption rewrites a shared
    descriptor silently.

    - Old path still in HEAD (only reachable with ``SIDEGRAPH_TRUST_DIRTY_TREE=on``, an
      uncommitted move): ``new_path`` must be absent from HEAD, i.e. new in the working tree.
    - Otherwise: find the mainline commit that deleted ``old_path`` (``--first-parent -m``,
      so a merge counts as one change: a branch that adds in one commit and deletes in the
      next, merged ``--no-ff``, is judged as a whole). Its first parent must exist (it does
      not at a shallow boundary) and lack ``new_path``, and the commit itself must hold it.

    # see design/superpowers/specs/2026-10-01-moved-rung-false-adoption-design.md (D2)
    """
    old_in_head = _tree_has(repo_root, "HEAD", old_path)
    if old_in_head is None:
        return False
    if old_in_head:
        return _tree_has(repo_root, "HEAD", new_path) is False
    try:
        log = _run_git(
            [
                "log",
                "--first-parent",
                "-m",
                "--no-show-signature",
                "-1",
                "--format=%H",
                "--diff-filter=D",
                "--",
                # a pathspec reads [ * ? as a glob; ls-tree does not, and takes no magic
                f":(literal){old_path}",
            ],
            cwd=repo_root,
            timeout=5.0,
        )
        change = log.stdout.strip()
        if log.returncode != 0 or not change or len(change.split()) != 1:
            return False
        parent = f"{change}^1"
        if _run_git(["rev-parse", "--verify", "-q", parent], cwd=repo_root, timeout=5.0).returncode:
            return False
    except ValueError:
        return False
    return (
        _tree_has(repo_root, parent, new_path) is False
        and _tree_has(repo_root, change, new_path) is True
    )


def _trust_dirty_tree() -> bool:
    """``SIDEGRAPH_TRUST_DIRTY_TREE=on`` — off-by-default escape hatch, point-of-use env
    read (same convention as ``SIDEGRAPH_AUTO_ACCEPT``/the host-hook ``*_NUDGE`` flags):
    for someone who has verified their own working tree matches what they want anchored
    and wants the "moved" rung to trust disk state without waiting for a commit, same as
    every sync before this fix did unconditionally. Overrides ONLY the committed-evidence
    check in ``_committed_evidence_confirms_move`` — the repo-root, same-suffix, and
    cross-type guards around it are never bypassed by this flag.
    """
    return os.environ.get("SIDEGRAPH_TRUST_DIRTY_TREE") == "on"


def rebind_entity(
    entity: Entity,
    store: Store,
    reader: GraphifyReader,
    repo_root: Path | None = None,
    vacated: dict[str, set[str]] | None = None,
    canonical_writes: bool = True,
) -> RebindOutcome:
    """One entity through the deterministic rebind ladder, then (with no ``vacated``
    collector) reconcile the Tier-1 community rows of the records it touched.

    ``canonical_writes=False`` keeps the pass index-only: the moved rung abstains (see
    ``_rebind_ladder``), so no tracked ``entities/<id>.json`` is ever rewritten.

    ``sync()`` passes one collector across the whole ladder and reconciles once after it,
    because the sibling leaves' final state decides whether a vacated community is still
    held (design D4). A direct caller (tests, scripts) passes none and gets its own records
    reconciled immediately. See ``_rebind_ladder`` for the ladder itself.
    """
    collector: dict[str, set[str]] = {} if vacated is None else vacated
    outcome = _rebind_ladder(entity, store, reader, repo_root, collector, canonical_writes)
    if vacated is None:
        for record_id, gone in collector.items():
            _reconcile_tier1_communities(record_id, gone, store, reader.graph_version())
    return outcome


def _rebind_ladder(
    entity: Entity,
    store: Store,
    reader: GraphifyReader,
    repo_root: Path | None,
    vacated: dict[str, set[str]],
    canonical_writes: bool = True,
) -> RebindOutcome:
    """One entity through the deterministic rebind ladder.

    ``canonical_writes=False`` (a borrowed graph, see ``sync``) makes the moved rung abstain
    entirely: its adoption is the only step that rewrites a tracked file, and the evidence it
    reads (the old path gone from disk, the move in HEAD's tree) is the graph's checkout's, not
    the branch the store belongs to. An exact miss then falls through to the same orphan rung as
    any move the rung cannot verify. The ambiguity branch beside it is index-only and stays.

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
        repointed = _adopt(entity, exact.node_id, version, store, exact.community, vacated)
        return RebindOutcome(
            **base,
            status="unchanged" if unchanged else "rebound",
            node_id=exact.node_id,
            repointed=repointed,
        )
    if exact.status == "ambiguous":
        _set_leaf_status(entity.entity_id, store, "degraded")
        repointed = _repoint_off_path(entity, exact.community, store, vacated)
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
        if loose.status == "resolved" and canonical_writes:
            # same resolve() invariant as the exact-match branch above.
            assert loose.node_id is not None
            node = reader.get_node(loose.node_id)
            new_file = node.file_path if node is not None else None
            # The lookup is case-insensitive and decoration-blind, so a unique hit can be a
            # different symbol altogether (a deleted type ``Priority`` against an old member
            # ``.priority``). A move keeps the name: same spelling once the decoration both
            # sides carry is stripped, case kept. Checked BEFORE everything below, so a loose
            # match never reaches ``moved_uncommitted`` (which is remembered and re-run) and a
            # refusal falls through to the orphan rung.
            same_name = node is not None and strip_decoration(node.name) == strip_decoration(
                desc.name
            )
            same_suffix = (
                new_file is not None
                and PurePosixPath(new_file).suffix == PurePosixPath(desc.file_path).suffix
            )
            old_confirmed_gone = repo_root is not None and not (repo_root / desc.file_path).exists()
            if same_name and same_suffix and old_confirmed_gone:
                # repo_root is not None here (old_confirmed_gone's own short-circuit proves
                # it), and new_file is not None (same_suffix's own check proves it) -- both
                # narrowed for the committed-evidence check below.
                assert repo_root is not None
                assert new_file is not None
                # Disk-only evidence stops here (defect fixed 2026-09-18, dirty-tree
                # corruption): the working tree can show the old path gone -- and the same
                # unique same-suffix hit -- from nothing more than one person's local,
                # uncommitted delete/rename/stash. Before rewriting a canonical, shared
                # descriptor, require the SAME move to be independently confirmed by git's
                # committed history (HEAD), not just by whatever this one working tree
                # currently looks like. SIDEGRAPH_TRUST_DIRTY_TREE=on is the documented,
                # off-by-default override for someone who has verified their own tree and
                # wants the old (disk-only) behavior back.
                if _trust_dirty_tree() or _committed_evidence_confirms_move(
                    repo_root, desc.file_path, new_file
                ):
                    # Committed evidence says the new path is in HEAD, not that it came WITH
                    # the move: a file that was there all along says the same. Adopt only when
                    # git shows the change that removed the old path also added the new one;
                    # any git failure refuses, and the refusal falls through to the orphan
                    # rung.
                    if _move_adds_the_new_path(repo_root, desc.file_path, new_file):
                        entity.descriptor = Descriptor(name=desc.name, file_path=new_file)
                        repointed = _adopt(
                            entity, loose.node_id, version, store, loose.community, vacated
                        )
                        return RebindOutcome(
                            **base,
                            status="moved",
                            node_id=loose.node_id,
                            detail=f"{desc.file_path} -> {new_file}",
                            repointed=repointed,
                        )
                elif _tree_has(repo_root, "HEAD", new_file) is False:
                    # Evidence looks like a move on disk but isn't (yet) committed -- never
                    # guess. Leave the binding exactly as it was (no adopt, no repoint, no
                    # status change) and surface it the same way orphaned/ambiguous outcomes
                    # are surfaced: informational, routed to a human, never a silent rewrite.
                    # Only while the move can still come true: a new path that is ALREADY in
                    # HEAD was there before the old one went, no later commit makes that a
                    # move (``_move_adds_the_new_path``), and git failing to say fails closed.
                    # Both fall through to the orphan rung instead of being remembered.
                    return RebindOutcome(
                        **base,
                        status="moved_uncommitted",
                        detail=(
                            f"{desc.file_path} -> {new_file} (uncommitted -- commit the move "
                            "so sync can verify it, or set SIDEGRAPH_TRUST_DIRTY_TREE=on to "
                            "trust the working tree)"
                        ),
                    )
            # fall through to orphan: a different symbol with the same loose name, a
            # cross-suffix hit (collision, not a move), the old path is still on disk
            # (out-of-scope file that never moved), repo_root is unknown and the old path's
            # fate can't be verified either way (fail closed), the new path is already in
            # HEAD, or git cannot show the new path arrived with the old one's removal
        if loose.status == "ambiguous":
            _set_leaf_status(entity.entity_id, store, "degraded")
            repointed = _repoint_off_path(entity, loose.community, store, vacated)
            return RebindOutcome(
                **base,
                status="ambiguous",
                detail=f"{len(loose.candidates)} candidates",
                repointed=repointed,
            )

    observed = _observed_community_for_orphan(desc, reader)
    repointed = _repoint_off_path(entity, observed, store, vacated)
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


@dataclass
class MembershipRefresh:
    """What one membership pass (``_refresh_domains``) did, or what ``ensure_domain_membership``
    found recorded: the fields a ``SyncReport`` carries about domains, and ``failed_ids``, the
    failed domains capture must not bind from. ``recomputed`` is ``False`` when the failures
    are a recorded pass's, reported again for the same identity, and for a rolled-back pass.
    ``rolled_back`` marks a pass of this call that could not take the writer reservation or
    failed as a whole: nothing it computed was published.
    # see design/superpowers/specs/2026-10-14-graphify-upgrade-design.md (D5.3)"""

    refreshed: int = 0
    empty: list[dict] = field(default_factory=list)
    overbroad: list[dict] = field(default_factory=list)
    failures: list[dict] = field(default_factory=list)
    splits: list[dict] = field(default_factory=list)
    failed_ids: list[str] = field(default_factory=list)
    recomputed: bool = True
    rolled_back: bool = False

    def fail(self, domain: Domain, exc: BaseException) -> None:
        self.failures.append(
            {"slug": domain.slug, "title": domain.title, "error": f"{type(exc).__name__}: {exc}"}
        )
        self.failed_ids.append(domain.domain_id)

    @classmethod
    def rolled_back_by(cls, exc: BaseException) -> MembershipRefresh:
        """A pass that could not take the reservation (a busy writer) or failed as a whole. It
        rolled back, so the previous rows and certification stand. One pass-level failure stands
        for every accepted domain: listing them would read the store again, and a writer holding
        an exclusive lock makes that read wait and fail too. ``busy`` says which it was."""
        return cls(
            failures=[
                {
                    "slug": "*",
                    "title": "every accepted domain",
                    "error": f"{type(exc).__name__}: {exc}",
                    "busy": _is_busy(exc),
                }
            ],
            recomputed=False,
            rolled_back=True,
        )

    @property
    def busy(self) -> bool:
        """The pass could not run because another writer held the store: nothing failed, and
        the next pass runs it."""
        return any(f.get("busy") for f in self.failures)

    def report_fields(self) -> dict:
        """The ``SyncReport`` fields this pass fills."""
        return {
            "domains_refreshed": self.refreshed,
            "empty_domains": self.empty,
            "overbroad_domains": self.overbroad,
            "domain_failures": self.failures,
            "snapshot_splits": self.splits,
        }


@dataclass(frozen=True)
class DomainMembership:
    """One accepted domain's effective membership over one graph: its member nodes, and
    ``communities``, whose every anchorable node is a member (for a domain governed by its
    rules, exactly the rule's claim).
    # see design/superpowers/specs/2026-10-14-graphify-upgrade-design.md (D5.3)"""

    communities: list[str]
    members: list[NodeRef]
    cap_hit: dict[str, int] | None = None

    def rows(self) -> list[tuple[str, str | None, str | None]]:
        return [(n.node_id, n.file_path or None, n.community) for n in self.members]


class _MembershipView:
    """What one pass needs to compute every accepted domain's membership over one graph,
    indexed once: the type-split communities and, when the store has a snapshot, each node's
    recorded domains and which accepted domain holds each covered slug."""

    def __init__(self, reader: GraphifyReader) -> None:
        self.reader = reader
        self.nodes = reader.list_nodes()
        self.current = {n.community for n in self.nodes if n.community is not None}
        self.anchorable = [n for n in self.nodes if n.file_type in ANCHORABLE_FILE_TYPES]
        self.by_community: dict[str, list[NodeRef]] = {}
        for n in self.anchorable:
            if n.community is not None:
                self.by_community.setdefault(n.community, []).append(n)
        self.recorded: domain_snapshot.Recorded | None = None
        self.heirs: dict[str, str] = {}
        self.splits: dict[str, list[str]] = {}
        self.rederive: frozenset[str] = frozenset()

    @classmethod
    def for_store(
        cls,
        store: Store,
        reader: GraphifyReader,
        snap: domain_snapshot.DomainSnapshot | None,
        accepted: list[Domain],
        *,
        rederive: frozenset[str] = frozenset(),
    ) -> _MembershipView:
        """The view over ``snap`` (or none) and ``accepted``, which the caller read once, inside
        the reservation it publishes from; lineage reads superseded records by id."""
        view = cls(reader)
        view.rederive = rederive
        if snap is not None:
            view.heirs, view.splits = domain_snapshot.snapshot_heirs(
                snap, accepted, store.get_domain
            )
            view.recorded = domain_snapshot.recorded_sets(view.anchorable, snap)
        return view

    def frozen_slug(self, domain: Domain) -> str | None:
        """The covered slug whose entries ``domain`` holds, or ``None``: no snapshot, a domain
        accepted after it, a part of a split, or one ``--refresh --domain`` re-derives."""
        if self.recorded is None or domain.slug in self.rederive:
            return None
        return self.heirs.get(domain.domain_id)

    def rule_members(self, domain: Domain, claimed: Iterable[str]) -> list[NodeRef]:
        """Every anchorable node of the claimed communities (path-less ones too: capture can
        resolve them), plus every anchorable node under one of ``domain.path_prefixes``,
        whether or not the cap let the prefix claim a community: the read rule before the index."""
        members = {n.node_id: n for c in claimed for n in self.by_community.get(c, [])}
        if domain.path_prefixes:
            for n in self.anchorable:
                if n.file_path and any(
                    matches_path_prefix(n.file_path, p) for p in domain.path_prefixes
                ):
                    members.setdefault(n.node_id, n)
        return sorted(members.values(), key=lambda n: n.node_id)

    def _wholly_inside(self, members: dict[str, NodeRef]) -> list[str]:
        touched = {n.community for n in members.values() if n.community is not None}
        return sorted(c for c in touched if all(n.node_id in members for n in self.by_community[c]))

    def membership(self, domain: Domain) -> DomainMembership:
        slug = self.frozen_slug(domain)
        if slug is None:
            communities, _matched, cap_hit = _recompute_domain_communities(
                domain, self.nodes, self.current, self.reader
            )
            ruled = self.rule_members(domain, communities)
            if self.recorded is None or domain.slug in self.rederive:
                return DomainMembership(communities, ruled, cap_hit)
            # Rule 4 (a domain accepted after the snapshot, or a part of a split): its rules
            # reach only nodes in files the snapshot does not list, never a listed node.
            listed = self.recorded.listed
            kept = {n.node_id: n for n in ruled if n.file_path and n.node_id not in listed}
            return DomainMembership(
                self._wholly_inside(kept), sorted(kept.values(), key=lambda n: n.node_id), cap_hit
            )
        assert self.recorded is not None
        sets, listed = self.recorded.sets, self.recorded.listed
        members = {n.node_id: n for n in self.anchorable if slug in sets[n.node_id]}
        # Rule 2: a node in a file the snapshot does not list also joins a domain whose prefix
        # covers that file, as before the snapshot; a listed node never does.
        if domain.path_prefixes:
            for n in self.anchorable:
                if (
                    n.node_id not in listed
                    and n.file_path
                    and any(matches_path_prefix(n.file_path, p) for p in domain.path_prefixes)
                ):
                    members.setdefault(n.node_id, n)
        return DomainMembership(
            self._wholly_inside(members), sorted(members.values(), key=lambda n: n.node_id)
        )


def rule_members(
    domain: Domain, communities: Iterable[str], reader: GraphifyReader
) -> list[NodeRef]:
    """``domain``'s members by its rules over ``reader``'s graph, given the communities its
    rule claims (see ``_MembershipView.rule_members``)."""
    return _MembershipView(reader).rule_members(domain, communities)


def _store_membership(
    store: Store, domain_id: str, m: DomainMembership, *, populated: bool
) -> bool:
    """Write ``m`` into the index, communities and rows in one transaction (the caller's
    reservation and savepoint), compared with the domain as stored now, never with a copy the
    caller read earlier. ``True`` when the membership readers see changed: the communities, or
    the rows once the index is ``populated`` (``MEMBERSHIP_POPULATED_KEY``). Rows written while
    the index is being populated (after the upgrade, or a reload) are not counted."""
    with store._mutation():
        stored = store.get_domain(domain_id)
        if stored is None:
            raise ValueError(f"domain {domain_id!r} vanished")
        changed = False
        if m.communities != sorted(stored.communities):
            store.refresh_domain_communities(domain_id, m.communities)
            changed = True
        if store.replace_domain_members(domain_id, m.rows()) and populated:
            changed = True
    return changed


def membership_stamp(store: Store, reader: GraphifyReader, signature: str | None = None) -> str:
    """The identity the membership index is certified for: the graph and the resolver
    (``reader.sync_stamp()``) and the domain snapshot's content (``signature``, the signature
    of the very bytes a pass parsed; read from disk when not given)."""
    if signature is None:
        signature = snapshot_signature(store.path)
    return f"{reader.sync_stamp()}|{signature}"


def _recorded(store: Store, identity: str) -> dict | None:
    """The failure a pass recorded for ``identity`` (``MEMBERSHIP_FAILED_KEY``), or ``None``."""
    try:
        data = json.loads(store.get_meta(MEMBERSHIP_FAILED_KEY) or "null")
    except ValueError:
        return None
    if (
        isinstance(data, dict)
        and data.get("identity") == identity
        and isinstance(data.get("failures"), list)
        and isinstance(data.get("failed_ids"), list)
    ):
        return data
    return None


def _settled(store: Store, identity: str) -> tuple[bool, MembershipRefresh | None]:
    """Whether a pass already settled ``identity``, and what it left: ``(True, None)`` when the
    index is certified for it, ``(True, <the recorded failure>)`` when a pass for it failed,
    ``(False, None)`` when none did. A recorded whole-pass failure outranks a certification for
    the same identity (a forced pass that failed as a whole leaves the earlier one in place)."""
    recorded = _recorded(store, identity)
    certified = store.get_meta(MEMBERSHIP_STAMP_KEY) == identity
    if recorded is None or (certified and not recorded.get("whole")):
        return certified, None
    return True, MembershipRefresh(
        failures=recorded["failures"], failed_ids=recorded["failed_ids"], recomputed=False
    )


def _settle(store: Store, identity: str, out: MembershipRefresh) -> None:
    """Inside the pass's reservation: certify ``identity``, or, when a domain failed, drop
    whatever certification the index held and record the failure for ``identity``."""
    if out.failures:
        store.delete_meta(MEMBERSHIP_STAMP_KEY)
        store.set_meta(
            MEMBERSHIP_FAILED_KEY,
            json.dumps(
                {"identity": identity, "failures": out.failures, "failed_ids": out.failed_ids}
            ),
        )
    else:
        store.set_meta(MEMBERSHIP_STAMP_KEY, identity)
        store.delete_meta(MEMBERSHIP_FAILED_KEY)


def _is_busy(exc: BaseException) -> bool:
    """Whether ``exc`` says another writer held the store: ``StoreBusyError``, or SQLite's BUSY
    or LOCKED."""
    if isinstance(exc, StoreBusyError):
        return True
    code = getattr(exc, "sqlite_errorcode", None)
    return (
        isinstance(exc, sqlite3.OperationalError)
        and isinstance(code, int)
        and code & 0xFF in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
    )


def _record_whole_failure(store: Store, identity: str, out: MembershipRefresh) -> None:
    """Record a pass that failed as a whole, for any reason but a busy writer, under the
    ``identity`` it read. Only inside the failed pass's own transaction, after its savepoint
    rolled the pass back, so no other writer can have changed what the pass read. When the
    error ended that transaction (SQLite rolls some errors back whole), ``_savepoint`` refuses
    and nothing is recorded: another writer may have changed the state meanwhile, and no
    comparison can prove it did not. The next call recomputes. ``ensure`` then reports a
    recorded failure without recomputing until the identity changes, and capture binds from
    no row for it, whatever the certification says. The certification is left alone.
    Best-effort: a record that cannot be written leaves the failure to be recomputed."""
    record = json.dumps(
        {"identity": identity, "failures": out.failures, "failed_ids": [], "whole": True}
    )
    with contextlib.suppress(Exception), store._savepoint():
        store.set_meta(MEMBERSHIP_FAILED_KEY, record)


def _apply(
    store: Store,
    view: _MembershipView,
    accepted: list[Domain],
    out: MembershipRefresh,
    *,
    populated: bool,
) -> None:
    """Compute and write each accepted domain's membership, each inside its own savepoint."""
    for domain in accepted:
        # The savepoint encloses the computation and the WRITE: refresh_domain_communities
        # raises ValueError on a domain that vanished, and a failure after the community update
        # must not leave that domain's communities ahead of its rows.
        try:
            with store._savepoint():
                m = view.membership(domain)
                if _store_membership(store, domain.domain_id, m, populated=populated):
                    out.refreshed += 1
        except Exception as exc:  # one bad domain must not cost the others their heal
            if not store._conn.in_transaction:
                raise  # the pass lost its reservation; never reacquire it to settle stale work
            out.fail(domain, exc)
            continue
        if m.cap_hit is not None:
            out.overbroad.append({"slug": domain.slug, "title": domain.title, **m.cap_hit})
        if not m.communities and not m.members:
            out.empty.append({"slug": domain.slug, "title": domain.title})


def ensure_domain_membership(store: Store, reader: GraphifyReader) -> MembershipRefresh | None:
    """Recompute every accepted domain's membership when the index is not certified for the
    current identity (graph, snapshot): after a reload, a changed snapshot, a domain status
    change, or on a graph the store has not synced. Domain membership only; the rebind ladder
    waits for the next sync. ``None`` when the index was current. When a pass already failed
    for this identity, the recorded failure comes back without a recompute
    (``recomputed=False``); a new identity, a status change, or ``sync(force=True)``
    recomputes. A skipped sync, capture's Tier-1 pick, ``drill_down`` and ratify call it.

    The certification and a recorded failure are checked here without the reservation, then
    again inside it (``only_if_stale``): a caller that waited for another's pass computes
    nothing.
    # see design/superpowers/specs/2026-10-14-graphify-upgrade-design.md (D5.3)"""
    settled, found = _settled(store, membership_stamp(store, reader))
    if settled:
        return found
    return _refresh_domains(store, reader, only_if_stale=True)


def membership_exclusions(
    store: Store, reader: GraphifyReader, identity: str | None = None
) -> frozenset[str] | None:
    """Which accepted domains' rows capture must not bind from for ``identity`` (the one the
    caller checked; the identity on disk now when not given): none (the index is certified),
    the failed domains (the last pass for this identity recorded failures; every other
    domain's rows were written by it), or ``None``: no row is certified for this identity
    (nothing is, or the pass for it failed as a whole, which outranks a certification). Only
    capture asks, because a binding is durable; the reading surfaces show the last published
    membership whatever this says. Read it inside the reservation that ran
    ``ensure_domain_membership``."""
    if identity is None:
        identity = membership_stamp(store, reader)
    recorded = _recorded(store, identity)
    if recorded is not None and recorded.get("whole"):
        return None
    if store.get_meta(MEMBERSHIP_STAMP_KEY) == identity:
        return frozenset()
    return None if recorded is None else frozenset(recorded["failed_ids"])


def _refresh_domains(
    store: Store, reader: GraphifyReader, *, only_if_stale: bool = False
) -> MembershipRefresh | None:
    """Refresh every ACCEPTED domain's membership: its ``communities`` and its member rows (the
    index retrieval and capture read). The whole pass runs inside ONE writer reservation: it
    reads the accepted domains and the identity, computes and writes each domain inside its
    own savepoint, and certifies that identity, or records the failures. A status change takes
    the same reservation, so none lands between the reads and the certification. The first
    pass over an empty index (after the upgrade, or a reload) marks it populated, when no
    domain failed. The pass asks for a large page cache (``_mutation(large_cache=True)``), kept
    until the owning transaction ends, so its writes do not spill and lock readers out.

    ``only_if_stale`` (``ensure_domain_membership``): inside the reservation, an identity a pass
    already settled returns ``None`` (certified) or the recorded failure, and nothing is
    computed. A pass that cannot take the reservation, or fails as a whole, rolls back and
    returns ``MembershipRefresh.rolled_back_by``; one that failed for any reason but a busy
    writer is recorded inside its own reservation (``_record_whole_failure``). ``None`` comes
    back only under ``only_if_stale``. A domain with no communities and no members is
    reported empty; cap hits are reported without changing domain status. When transaction
    loss also ends an owning caller's transaction, propagate through both handlers so the
    caller cannot continue and commit partial work.

    The snapshot is read inside the reservation, once: the identity
    certified carries the hash of the very bytes applied, so a pull landing meanwhile leaves
    the certification behind. An invalid snapshot fails every domain's refresh, keeps the
    previous membership and says why (a finding for ``--check``), and never falls back to the
    rules the snapshot replaced; the failure is recorded like any other, so it is reported
    without recomputing until the file, the graph or a domain's status changes. A whole-pass
    failure is recorded under the identity of the bytes read when it came after that read, and
    under the identity on disk when it came before."""
    out = MembershipRefresh()
    try:
        with store._mutation(large_cache=True):
            identity: str | None = None
            try:
                with store._savepoint():
                    identity = membership_stamp(store, reader)
                    if only_if_stale:
                        settled, found = _settled(store, identity)
                        if settled:
                            return found
                    accepted = list(store.iter_domains(status=DomainStatus.ACCEPTED))
                    try:
                        snap, signature = domain_snapshot.read_snapshot(store.path)
                    except domain_snapshot.SnapshotError as exc:
                        for domain in accepted:
                            out.fail(domain, exc)
                        _settle(store, membership_stamp(store, reader, exc.signature), out)
                        return out
                    identity = membership_stamp(store, reader, signature)
                    view = _MembershipView.for_store(store, reader, snap, accepted)
                    by_id = {d.domain_id: d for d in accepted}
                    out.splits = [
                        {"slug": slug, "domains": sorted(by_id[i].slug for i in ids if i in by_id)}
                        for slug, ids in sorted(view.splits.items())
                    ]
                    populated = store.get_meta(MEMBERSHIP_POPULATED_KEY) is not None
                    _apply(store, view, accepted, out, populated=populated)
                    if not populated and not out.failures:
                        store.set_meta(MEMBERSHIP_POPULATED_KEY, "1")
                    _settle(store, identity, out)
            except Exception as exc:  # failed as a whole; recorded inside this reservation (T3)
                if not store._conn.in_transaction and store._mutation_depth > 1:
                    raise  # preserve loss of the caller's transaction through both handlers
                failed = MembershipRefresh.rolled_back_by(exc)
                if identity is not None and not failed.busy:
                    _record_whole_failure(store, identity, failed)
                return failed
    except Exception as exc:  # no reservation (a busy writer), or its commit failed
        if not store._conn.in_transaction and store._mutation_depth > 0:
            raise  # the nested scope unwound, but its caller still owns the lost transaction
        return MembershipRefresh.rolled_back_by(exc)
    return out


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
    with store._mutation(large_cache=True), store._savepoint():
        current = store.get_domain(domain.domain_id)
        if current is None:
            raise ValueError(f"domain {domain.domain_id!r} vanished")
        snap, signature = domain_snapshot.read_snapshot(store.path)
        identity = membership_stamp(store, reader, signature)
        populated = store.get_meta(MEMBERSHIP_POPULATED_KEY) is not None
        m = _MembershipView.for_store(
            store, reader, snap, list(store.iter_domains(status=DomainStatus.ACCEPTED))
        ).membership(current)
        _store_membership(store, current.domain_id, m, populated=populated)
        # Rows for this identity now sit in the index. A certification for any other identity
        # no longer holds; one for this identity does, because a pass for it computed the same
        # rows from the same state (any status change since would have uncertified it).
        if store.get_meta(MEMBERSHIP_STAMP_KEY) != identity:
            store._uncertify_membership()
    overbroad = (
        {"slug": domain.slug, "title": domain.title, **m.cap_hit} if m.cap_hit is not None else None
    )
    return m.communities, overbroad


@dataclass(frozen=True)
class DomainActivation:
    """Result of :func:`activate_accepted_domain` (design D2's shared per-domain
    post-accept step). ``resolved`` is the caller's cue for the pre-existing
    never-fail-a-ratify heal flag (``VOLATILE_STALE_KEY``, already set by this function
    when it applies — a caller never needs to set it itself); ``overbroad`` is
    ``refresh_domain_communities_now``'s own cap-hit dict (or ``None``); ``error`` is the
    exception text when the refresh raised, else ``None``. ``note`` explains frozen or split
    snapshot membership when applicable. Deliberately renders NOTHING —
    the "path rule too broad" sentence is a literal trigger phrase for the
    ``sidegraph:heal-anchors`` skill and stays in the two human callers (``server.py``,
    ``cli.py``), which build their own message from ``overbroad``.
    """

    resolved: bool
    overbroad: dict | None
    error: str | None
    note: str | None = None


def _snapshot_note(domain: Domain, store: Store) -> str | None:
    """The sentence a ratify answer adds when the store has a domain snapshot: the domain's
    membership is frozen, or it is a part of a split. ``None`` otherwise."""
    snap = domain_snapshot.load_snapshot(store.path)
    if snap is None:
        return None
    accepted = list(store.iter_domains(status=DomainStatus.ACCEPTED))
    heirs, splits = domain_snapshot.snapshot_heirs(snap, accepted, store.get_domain)
    slug = heirs.get(domain.domain_id)
    if slug is not None:
        return (
            f"membership frozen by the domain snapshot (as {slug}); a rule change applies "
            f"only after `sidegraph-domains snapshot --refresh --domain {domain.slug}`"
        )
    for split_slug, ids in splits.items():
        if domain.domain_id in ids:
            parts = sorted(d.slug for d in accepted if d.domain_id in ids)
            flags = " ".join(f"--domain {p}" for p in parts)
            return (
                f"split from snapshot domain {split_slug}: its frozen membership applies to "
                f"no part until `sidegraph-domains snapshot --refresh {flags}`"
            )
    return None


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
    With a snapshot, success also schedules a full pass: a renamed or split heir can
    change another domain's membership.
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
    note: str | None = None
    if domain_snapshot.snapshot_path(store.path).is_file():
        store.set_meta(VOLATILE_STALE_KEY, "1")
        try:
            note = _snapshot_note(domain, store)
        except Exception:  # advice only: sync and verify report a broken snapshot
            note = None
    return DomainActivation(resolved=True, overbroad=overbroad, error=None, note=note)


# A snapshot command's refusal when the file changed under it: git takes no lock, so a pull
# can rewrite it at any moment.
SNAPSHOT_CHANGED = "the domain snapshot changed while this command ran; run it again"


def take_snapshot(
    store: Store,
    reader: GraphifyReader,
    *,
    refresh: bool,
    rederive: frozenset[str] = frozenset(),
    store_commit: str | None = None,
    based_on: str | None = None,
) -> domain_snapshot.DomainSnapshot:
    """What ``sidegraph-domains snapshot`` writes: covered accepted domains' membership over
    ``reader``'s graph (D5.4).

    - First snapshot (``refresh=False``): every accepted domain, by its rules.
    - ``refresh`` alone: every accepted domain's effective membership (frozen entries and
      votes; new-file rule members for the others), all frozen now.
    - ``refresh`` with ``rederive`` slugs: those domains by their full rules; every other
      domain that holds entries keeps its effective membership; the rest stay out.

    A graph that is not the one the store last synced (``--graph``, ``--build-with``) does not
    lend a domain its previous communities: their ids belong to another graph.

    Runs inside one writer reservation, the caller's when it publishes in the same one (the
    snapshot command does): the accepted domains and the snapshot being refreshed are read
    once there, so no status change or pull mixes two states into what it freezes. Raises
    ``ValueError``: for a ``rederive`` slug that names no accepted domain, as read there;
    an incomplete split refresh (every accepted part must be named by ``rederive``);
    ``SNAPSHOT_CHANGED`` when the snapshot it reads is not the one whose signature the caller
    read first (``based_on``); and ``domain_snapshot.SnapshotError`` for an invalid snapshot
    under ``refresh``.
    """
    with store._mutation():
        synced = store.get_meta(LAST_SYNCED_KEY)
        same_graph = synced in (reader.sync_stamp(), f"index-only:{reader.sync_stamp()}")
        accepted = list(store.iter_domains(status=DomainStatus.ACCEPTED))
        unknown = sorted(rederive - {d.slug for d in accepted})
        if unknown:
            raise ValueError(f"--domain names no accepted domain: {', '.join(unknown)}")
        try:
            current, signature = domain_snapshot.read_snapshot(store.path)
        except domain_snapshot.SnapshotError as exc:
            if refresh and (based_on is None or exc.signature == based_on):
                raise
            current, signature = None, exc.signature
        if based_on is not None and signature != based_on:
            raise ValueError(SNAPSHOT_CHANGED)
        if not refresh:
            current = None
        view = _MembershipView.for_store(store, reader, current, accepted, rederive=rederive)
        if refresh:
            for slug, part_ids in view.splits.items():
                parts = sorted(d.slug for d in accepted if d.domain_id in part_ids)
                if not set(parts) <= rederive:
                    flags = " ".join(f"--domain {part}" for part in parts)
                    raise ValueError(
                        f"snapshot domain {slug!r} is split; refresh must name every part: {flags}"
                    )
        members: dict[str, list[str]] = {}
        domain_ids: dict[str, str] = {}
        for d in accepted:
            if refresh and rederive and d.slug not in rederive and view.frozen_slug(d) is None:
                continue
            domain_ids[d.slug] = d.domain_id
            if not same_graph:
                d = d.model_copy(update={"communities": []})
            members[d.slug] = [n.node_id for n in view.membership(d).members]
    return domain_snapshot.build_snapshot(
        view.anchorable,
        members,
        domain_ids,
        taken_at=datetime.now(UTC),
        engine_version=reader.engine_version(),
        engine_generation=reader.engine_generation(),
        graph_version=reader.graph_version(),
        store_commit=store_commit,
    )


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

    snapshot_splits: list[dict] = []  # {"slug", "domains"}: a covered domain two accepted
    # domains descend from; neither holds its frozen entries until `snapshot --refresh` names
    # them. Informational, like overbroad_domains.

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for o in self.outcomes:
            out[o.status] = out.get(o.status, 0) + 1
        return out


def _current_head(path: Path) -> str | None:
    """The git HEAD sha of the repo containing ``path``, or ``None`` when it cannot be
    resolved (no repo, no commits yet, a git failure). Never raises. Git's repository-local
    variables (``GIT_DIR`` and friends) are dropped, so the answer is for ``path``'s own
    repository, never an ambient one."""
    try:
        result = _run_git(["rev-parse", "--verify", "-q", "HEAD"], cwd=path, timeout=5.0)
    except ValueError:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _record_pending_moves(
    store: Store,
    head: str | None,
    outcomes: list[RebindOutcome],
    watched: frozenset[str] = frozenset(),
) -> None:
    """Remember which entities ended this pass as ``moved_uncommitted``, plus any ``watched``
    id (an abandoned move, design D2), with ``head`` -- the HEAD read BEFORE the ladder ran,
    so a commit landing mid-pass differs from it and the next gated call re-checks. Drops
    the key when there is nothing to watch. It replaces the key, never merges into it, so a
    full pass keeps only what its own outcomes say (design D1)."""
    ids = [
        o.entity_id for o in outcomes if o.status == "moved_uncommitted" or o.entity_id in watched
    ]
    if not ids:
        store.delete_meta(PENDING_MOVES_KEY)
        return
    store.set_meta(PENDING_MOVES_KEY, json.dumps({"head": head, "entity_ids": ids}))


def _load_pending_moves(store: Store) -> tuple[str | None, list[str]] | None:
    """The remembered pending moves as ``(head, entity_ids)``, or ``None`` when there are
    none. A key that does not parse or has the wrong shape is removed and read as empty: it
    must never raise on every gated call."""
    raw = store.get_meta(PENDING_MOVES_KEY)
    if raw is None:
        return None
    try:
        data = json.loads(raw)
        head = data["head"]
        ids = data["entity_ids"]
        valid = (
            (head is None or isinstance(head, str))
            and isinstance(ids, list)
            and all(isinstance(i, str) for i in ids)
        )
    except (ValueError, KeyError, TypeError):
        valid = False
    if not valid or not ids:
        store.delete_meta(PENDING_MOVES_KEY)
        return None
    return head, ids


def _reconcile_vacated(
    vacated: dict[str, set[str]], store: Store, outcomes: list[RebindOutcome], graph_version: str
) -> None:
    """Reconcile every record's Tier-1 community rows, one record at a time. A failure is
    isolated and reported as an ``error`` outcome (never silent: both lazy-sync callers
    suppress exceptions), so the rest of the pass, including the pending-moves key, still
    lands. Callers record the key BEFORE calling this: it says what the adopts did."""
    failed: dict[str, set[str]] = {}
    for record_id, gone in vacated.items():
        try:
            _reconcile_tier1_communities(record_id, gone, store, graph_version)
        except Exception as e:  # the adopts have committed; do not lose the rest of the pass
            failed[record_id] = gone
            outcomes.append(_tier1_error(record_id, e))
    if failed:
        pending = _load_pending_tier1(store)
        for record_id, gone in failed.items():
            pending.setdefault(record_id, set()).update(gone)
        _save_pending_tier1(store, pending)


def _tier1_error(record_id: str, error: Exception) -> RebindOutcome:
    return RebindOutcome(
        entity_id=record_id,
        canonical_name=f"community rows of record {record_id}",
        status="error",
        detail=f"tier-1 community reconcile failed: {error}",
    )


def _load_pending_tier1(store: Store) -> dict[str, set[str]]:
    """The remembered failed reconciles, ``{}`` when none. A key that does not parse or has
    the wrong shape is removed and read as empty: it must never raise on every sync."""
    raw = store.get_meta(PENDING_TIER1_META)
    if raw is None:
        return {}
    try:
        data = json.loads(raw)
        pending = {str(k): {str(c) for c in v} for k, v in data.items() if v}
    except (ValueError, TypeError, AttributeError):
        store.delete_meta(PENDING_TIER1_META)
        return {}
    return pending


def _save_pending_tier1(store: Store, pending: dict[str, set[str]]) -> None:
    if not pending:
        store.delete_meta(PENDING_TIER1_META)
        return
    store.set_meta(PENDING_TIER1_META, json.dumps({k: sorted(v) for k, v in pending.items()}))


def _retry_pending_tier1(store: Store, graph_version: str) -> list[RebindOutcome]:
    """Re-run the Tier-1 reconcile for the pairs an earlier pass failed on. Runs at the
    start of every ``sync()``, gated or not. A pair that fails again stays in the key and is
    reported as an ``error`` outcome. A pair that now succeeds is cleared and reported as a
    ``moved`` outcome (noteworthy, not a finding), so a gated call that repaired a binding
    does not look like a skip. A pair whose reconcile abstained (a live/degraded Tier-2 leaf
    has no community baseline yet) stays in the key and reports nothing: it is a specified
    non-event, not a finding, and a later pass re-baselines the leaf, and the next sync then
    settles it."""
    pending = _load_pending_tier1(store)
    if not pending:
        return []
    outcomes: list[RebindOutcome] = []
    remaining: dict[str, set[str]] = {}
    for record_id, gone in pending.items():
        try:
            settled = _reconcile_tier1_communities(record_id, gone, store, graph_version)
        except Exception as e:
            remaining[record_id] = gone
            outcomes.append(_tier1_error(record_id, e))
        else:
            if not settled:
                remaining[record_id] = gone
                continue
            outcomes.append(
                RebindOutcome(
                    entity_id=record_id,
                    canonical_name=f"community rows of record {record_id}",
                    status="moved",
                    detail="tier-1 community reconcile retried",
                )
            )
    _save_pending_tier1(store, remaining)
    return outcomes


def _stale_decisions(store: Store) -> list[dict]:
    """Live decisions whose every Tier-2 leaf is orphaned."""
    stale: list[dict] = []
    for d in store.iter_decisions():
        if d.status in (DecisionStatus.SUPERSEDED, DecisionStatus.REJECTED):
            continue
        # Leaf-based: surviving Tier-1/Tier-0 bindings keep the decision retrievable
        # (the escalation), but do not vouch for the claim — the concrete code is gone.
        leaves = [b for b in store.bindings_for_record(d.id) if b.tier == 2]
        if leaves and all(b.status == "orphaned" for b in leaves):
            stale.append({"id": d.id, "title": d.title})
    return stale


def _reverify_pending_moves(
    store: Store, reader: GraphifyReader, entity_ids: list[str], version: str, head: str
) -> SyncReport:
    """The narrow pass (design D2): HEAD moved since a pass left entities
    ``moved_uncommitted`` and the graph did not change, so re-run the ladder over only
    those entities. ``head`` is the HEAD the gate read before this pass started, and is what
    gets recorded. Everything per entity, the store lookup included, sits inside one guard:
    an error is isolated, becomes an ``error`` outcome and drops that id from the key, as
    does an id the store no longer resolves. An entity whose old path is back on disk (the
    rename was reverted or stashed) is left untouched and reported ``unchanged``, but stays
    in the key with the current HEAD: the rename may come back, and it is re-checked only
    after the next HEAD change. Domain membership reads graph nodes and the graph did not
    change, so the domain fields of the report stay empty; slug conflicts and the stale scan
    are recomputed and the TOC cache is rebuilt the way a skipped pass does.
    """
    outcomes: list[RebindOutcome] = []
    vacated: dict[str, set[str]] = {}
    watched: set[str] = set()
    repo_root = _resolve_repo_root(reader)
    for entity_id in entity_ids:
        entity = None
        try:
            entity = store.get_entity(entity_id)
            if entity is None or entity.descriptor is None:
                continue
            old_path = entity.descriptor.file_path
            if reader.resolve(entity.descriptor).status == "resolved":
                # The graph already has this entity at its recorded path: it was adopted
                # (a pass that adopted it but did not finish leaves the key stale). The
                # file existing there is the adoption, not a reverted rename: run the
                # ladder's first rung and drop it from the key.
                outcomes.append(rebind_entity(entity, store, reader, repo_root, vacated))
                continue
            if repo_root is not None and old_path is not None and (repo_root / old_path).exists():
                # The descriptor still names the pre-move path (moved_uncommitted never
                # repoints), so the file being back means the rename was reverted or
                # stashed. The ladder would fall through to orphan on a graph built on the
                # dirty tree: leave the leaf untouched and keep watching the entity.
                outcomes.append(
                    RebindOutcome(
                        entity_id=entity.entity_id,
                        canonical_name=entity.canonical_name,
                        status="unchanged",
                        detail=f"pending move of {old_path} abandoned (file is back on disk)",
                    )
                )
                watched.add(entity_id)
                continue
            outcomes.append(rebind_entity(entity, store, reader, repo_root, vacated))
        except Exception as e:  # one bad entity must not abort the pass
            outcomes.append(
                RebindOutcome(
                    entity_id=entity_id,
                    canonical_name=entity.canonical_name if entity is not None else entity_id,
                    status="error",
                    detail=str(e),
                )
            )
    # The key first: it records what the adopts above did, so a reconcile failure cannot
    # leave an adopted entity remembered as pending.
    _record_pending_moves(store, head, outcomes, frozenset(watched))
    _reconcile_vacated(vacated, store, outcomes, version)
    if next(store.iter_domains(status=DomainStatus.ACCEPTED), None) is not None:
        _rebuild_toc_cache(store, reader)
    return SyncReport(
        from_version=version,
        to_version=version,
        outcomes=outcomes,
        stale_decisions=_stale_decisions(store),
        slug_conflicts=store.domain_slug_conflicts(),
    )


# Key inside the cached TOC dict: the store's canonical digest when it was built (see
# ``Store.canonical_digest``). A cache that carries none, because a path with no graph reader
# wrote it, is never trusted as current.
TOC_DIGEST_FIELD = "store_digest"


def _rebuild_toc_cache(store: Store, reader: GraphifyReader) -> None:
    """Build the TOC with ``reader`` and cache it, stamped with the canonical digest.

    The digest is read BEFORE the build: a canonical write that lands during it then leaves
    the stamp behind the store, and the next pass rebuilds, never the other way round.
    """
    digest = store.canonical_digest()
    toc = build_toc(store, reader)
    if digest is not None:
        toc[TOC_DIGEST_FIELD] = digest
    store.set_meta(TOC_CACHE_KEY, json.dumps(toc))


def _toc_cache_is_current(store: Store) -> bool:
    """True when the cached TOC was built, with a reader, over the store as it is now."""
    digest = store.canonical_digest()
    if digest is None:
        return False
    try:
        cache = json.loads(store.get_meta(TOC_CACHE_KEY) or "null")
    except ValueError:
        return False
    return isinstance(cache, dict) and cache.get(TOC_DIGEST_FIELD) == digest


def sync(
    store: Store, reader: GraphifyReader, force: bool = False, *, canonical_writes: bool = True
) -> SyncReport:
    """Full rebind pass, gated on graph_version AND the volatile-reload flag. Stamps
    last_synced, and clears the reload flag, only on completion.

    ``canonical_writes=False`` is the index-only mode for a graph that is not this store's own
    (a linked worktree reading the main checkout's): every derived refresh runs, and the moved
    rung abstains in the full pass while the narrow re-verification pass is skipped, because an
    adoption rewrites a tracked ``entities/<id>.json`` from evidence about another checkout's
    tree. The pending-moves key is left as it was. Its version stamp is its own value
    (``index-only:<graph version>``), compared and written in place of the plain one: a graph
    the store later reads as its own, byte for byte the same (a copy, a symlinked
    ``graphify-out``, a deterministic rebuild at the same commit), must not look "already
    synced" to a pass that owns the moved rung.

    Entities that error during rebind are not retried until the next graph-version change
    (or force=True) — the stamp records a completed visit, not universal success. The
    exceptions are an entity left ``moved_uncommitted`` and a remembered Tier-1 reconcile
    (``PENDING_TIER1_META``; see ``_retry_pending_tier1``: a retry that repairs the record
    or fails again reports an outcome and also prevents the skip, an abstain does not). A
    ``moved_uncommitted`` entity is remembered under
    ``PENDING_MOVES_KEY`` with the git HEAD read before the ladder ran, and when the gate
    would skip, a resolvable HEAD that differs from the recorded one runs a NARROW pass —
    the rebind ladder over only those entities, returning a non-skipped report whose
    ``outcomes`` cover just them, whose ``stale_decisions`` and ``slug_conflicts`` are
    recomputed, and whose domain fields stay empty (the graph did not change, so domain
    membership was not recomputed). The gate below skips the rebind ladder itself when the
    version already matches, the store's volatile state isn't cold (`VOLATILE_STALE_KEY`),
    and `force` wasn't passed — but the gate does NOT skip the TOC cache refresh: a skipped
    pass still rewrites `toc_cache` when at least one accepted domain exists and the store
    changed since the cache was built, since that cache can go stale from content-only changes
    the graph never sees (see the gate's own comment below).
    A skipped pass rebuilds uncertified membership. Refresh failures are reported again
    for the same identity until a graph/snapshot/status change, or a forced recompute. A
    whole-pass rollback returns the held outcomes and domain report without further store
    reads or stamp writes; a busy writer is reported but is not a --check finding.
    """
    to_version = reader.graph_version()
    # What the gate stamps and compares is the graph version PLUS the resolver revision
    # (reader.sync_stamp), so the first pass after an upgrade that taught resolve() something
    # new reruns once and heals what the older rules left orphaned. The report is a different
    # thing: it shows the plain graph version on both sides, so the revision is stripped from
    # the stored stamp before it becomes ``from_version``.
    stamp = reader.sync_stamp()
    if not canonical_writes:
        stamp = f"index-only:{stamp}"
    last_stamp = store.get_meta(LAST_SYNCED_KEY)
    from_version = reader.without_revision(last_stamp)
    # An earlier pass's failed Tier-1 reconcile is retried first, whatever the gate says.
    retried = _retry_pending_tier1(store, to_version)
    # A canonical reload (pull, merge, branch switch, hand edit — even a bare touch, since
    # the digest hashes mtime_ns) resets every derived table but deliberately PRESERVES the
    # meta table, so graph_version alone cannot see that volatile state went cold. This is
    # the flag git-native design §3 introduced for exactly that event ("the next
    # sidegraph-sync / lazy sync rebuilds volatile state") — set on every reload since, and
    # read here for the first time.
    volatile_stale = store.get_meta(VOLATILE_STALE_KEY) == "1"
    if stamp == last_stamp and not volatile_stale and not force:
        # A committed move does not change graph.json, so a pass that left entities
        # `moved_uncommitted` would otherwise never revisit them. Re-check them once HEAD has
        # resolvably moved (design D2); an unresolvable HEAD keeps the key and skips.
        pending = _load_pending_moves(store) if canonical_writes else None
        if pending is not None:
            recorded_head, pending_ids = pending
            head = _current_head(reader.path.parent)
            if head is not None and head != recorded_head:
                report = _reverify_pending_moves(store, reader, pending_ids, to_version, head)
                report.outcomes[:0] = retried
                return report
        # TOC cache is volatile state (§5/§6), and `sync` is the store's one volatile-refresh
        # verb (see docs/reference/store-format.md) — a content-only change (e.g. `add_decision`
        # bound to an already-accepted domain, via capture or import) never moves
        # graph_version, so the version gate above must not ALSO gate the cache refresh: doing
        # so left the cache stale until an unrelated domain accept/drop happened to rebuild it
        # (cli.py/server.py's own explicit ratify-time refresh), which real sessions hit (fix-
        # wave B, B6). But every `get_task_context` call lands here, and a TOC build counts
        # every domain's decisions (0.1-0.2 s on a real store), so the rebuild is gated on the
        # store having changed since the cache was built: the cache carries the canonical
        # digest it was built over, and an unchanged digest keeps it. Gated also on "at least
        # one accepted domain exists" to avoid a pointless write when there is nothing to
        # refresh.
        # see design/superpowers/specs/2026-10-01-session-start-text-design.md (D2b)
        # A reload, a snapshot or domain status change, or a graph the index was not computed
        # for can leave domain membership uncertified while the graph stamp still matches:
        # rebuild it, and the TOC rendered from it. A failure already recorded for this state
        # comes back without a recompute, and is reported again below.
        rebuilt = ensure_domain_membership(store, reader)
        if rebuilt is not None and rebuilt.rolled_back:
            # The pass could not take the reservation, or failed as a whole, and rolled back.
            # Report it from what is in hand: another writer may hold the store exclusively,
            # and any further read (stale decisions, slug conflicts, the TOC) would wait out
            # the busy timeout and raise in place of this report. The next pass reads them.
            return SyncReport(
                from_version=to_version,
                to_version=to_version,
                outcomes=retried,
                **rebuilt.report_fields(),
            )
        if next(store.iter_domains(status=DomainStatus.ACCEPTED), None) is not None and (
            (rebuilt is not None and rebuilt.recomputed) or not _toc_cache_is_current(store)
        ):
            _rebuild_toc_cache(store, reader)
        if retried or (rebuilt is not None and rebuilt.failures):
            # A retried Tier-1 reconcile, or a domain refresh that failed for this state: report
            # it (a narrow report), not a silent skip.
            return SyncReport(
                from_version=to_version,
                to_version=to_version,
                outcomes=retried,
                stale_decisions=_stale_decisions(store),
                slug_conflicts=store.domain_slug_conflicts(),
                **(rebuilt.report_fields() if rebuilt is not None else {}),
            )
        return SyncReport(from_version=from_version, to_version=to_version, skipped=True)

    # Read BEFORE the ladder: a commit landing after it must differ from what is recorded,
    # or the gate would skip that move indefinitely (design D2).
    head_before = _current_head(reader.path.parent) if canonical_writes else None
    outcomes: list[RebindOutcome] = list(retried)
    vacated: dict[str, set[str]] = {}
    # Resolved once for the whole pass (one subprocess call, not one per entity) and
    # threaded through every rebind_entity call — see _resolve_repo_root / the moved
    # rung's own comment for what this guards against and how it degrades when unknown.
    repo_root = _resolve_repo_root(reader) if canonical_writes else None
    for entity in store.iter_concrete_entities():
        try:
            outcomes.append(
                rebind_entity(
                    entity, store, reader, repo_root, vacated, canonical_writes=canonical_writes
                )
            )
        except Exception as e:  # one bad entity must not abort the pass
            outcomes.append(
                RebindOutcome(
                    entity_id=entity.entity_id,
                    canonical_name=entity.canonical_name,
                    status="error",
                    detail=str(e),
                )
            )

    # Reconciled after the whole ladder, not per entity: a sibling leaf may still be
    # unvisited (or orphaned later in the pass) when an earlier leaf leaves a community.
    # The pending key is recorded before the reconcile (see _reverify_pending_moves).
    if canonical_writes:
        _record_pending_moves(store, head_before, outcomes)
    _reconcile_vacated(vacated, store, outcomes, to_version)

    refresh = _refresh_domains(store, reader, only_if_stale=not force)
    if refresh is None:
        refresh = MembershipRefresh(recomputed=False)
    if refresh.rolled_back:
        return SyncReport(
            from_version=from_version,
            to_version=to_version,
            outcomes=outcomes,
            **refresh.report_fields(),
        )

    stale = _stale_decisions(store)

    store.set_meta(LAST_SYNCED_KEY, stamp)
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
    _rebuild_toc_cache(store, reader)
    return SyncReport(
        from_version=from_version,
        to_version=to_version,
        outcomes=outcomes,
        stale_decisions=stale,
        **refresh.report_fields(),
        slug_conflicts=store.domain_slug_conflicts(),
    )


# Outcome statuses worth a human's attention -- "unchanged" (nothing happened) and
# "rebound" (same name+file_path descriptor match as last sync, but the resolved node id
# changed -- the mapping just heals itself) are noise; everything else is a heads-up. One
# filter, two consumers: ``report_as_dict``'s "outcomes" field below (shared by both
# ``sidegraph-sync --json`` and the ``sync_anchors`` MCP tool) and ``sidegraph-sync``'s own
# prose printer (``cli.sync_main``).
_SYNC_OUTCOME_NOTEWORTHY = ("moved", "moved_uncommitted", "orphaned", "ambiguous", "error")

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
# matters and harmful (permanently red) where it doesn't. "moved_uncommitted" (dirty-tree
# guard) is informational on purpose: the leaf stays live and silent, and nothing is stale.
# The move is remembered (``PENDING_MOVES_KEY``), and the first sync after HEAD moves
# re-verifies it, lazily or explicitly, even when the graph was not rebuilt. A person just
# needs to commit their move or use the documented escape hatch, not a red CI run. Only "error"
# (the rebind ladder itself raised on an entity) stays a genuine failing outcome.
_FINDING_OUTCOME_STATUSES = ("error",)


def report_as_dict(report: SyncReport) -> dict:
    """The shared report shape both ``sidegraph-sync --json`` (``cli.sync_main``) and the
    ``sync_anchors`` MCP tool (``server._sync_anchors_impl``) return -- one dict shape, two
    callers (design/superpowers/specs/2026-07-11-ci-integrity-design.md ruling 1).

    ``synced`` is ``False`` when the pass was skipped outright (``graph_version``
    unchanged, no ``force``, no pending move to re-verify, and no remembered Tier-1
    reconcile whose retry repaired the record or failed again; a retry that abstains
    because a live or degraded Tier-2 anchor has no recorded community reports nothing)
    -- every OTHER field is then
    an EMPTY default (``outcomes:
    []``, ``counts: ""``, ``repointed: 0``, ``stale_decisions: []``, ``empty_domains:
    []``, ``overbroad_domains: []``, ``slug_conflicts: []``, ``domains_refreshed: 0``,
    ``domain_failures: []``, ``snapshot_splits: []``) from a fresh, un-run
    ``SyncReport(skipped=True)`` -- NOT a
    prior (possibly stale) report --
    so a caller must never read a skipped pass as "everything's clean" on its own;
    ``report_has_findings`` below happens to still read a skipped dict as clean (nothing to
    find), which is exactly the "version-skip is exit 0" contract ``--check`` wants.

    A third kind, the NARROW report (a pending ``moved_uncommitted`` move re-verified after
    HEAD moved, graph unchanged): ``synced: true`` with ``from_version == to_version``,
    ``outcomes`` limited to the re-verified entities, ``stale_decisions`` and
    ``slug_conflicts`` recomputed, and ``empty_domains``/``overbroad_domains``/
    ``domain_failures`` empty and ``domains_refreshed`` 0 because domain membership was not
    recomputed. ``--check`` implies force, so it never sees one.

    A MEMBERSHIP report comes from a skipped gate whose domain membership is uncertified
    and whose refresh failed, now or for the same graph/snapshot identity earlier. It has
    ``synced: true``, equal versions, retried Tier-1 outcomes and domain fields. A pass that
    rolled back during this sync, full or skipped, returns without further store reads:
    stale decisions and slug conflicts are empty because they were not read. Its one ``*``
    failure carries ``busy``; a busy writer is not a --check finding. A non-busy whole-pass
    failure is recorded under the failed identity while its reservation survives.

    ``snapshot_splits`` lists covered domains with multiple accepted heirs. Neither
    heir holds the frozen entries until refresh names the parts; this is informational.

    ``outcomes`` carries only entities worth a human's attention -- moved/moved_uncommitted/
    orphaned/ambiguous/error (``moved`` also reports a repaired Tier-1 community reconcile,
    ``tier-1 community reconcile retried``) -- filtered via ``_SYNC_OUTCOME_NOTEWORTHY``, never the
    "unchanged"/"rebound" majority, same filter ``sidegraph-sync``'s own printer applies.
    ``counts`` is ``report.counts()`` rendered as a string (e.g. ``"{'unchanged': 3}"``),
    ``""`` when nothing is tracked yet. ``repointed`` sums EVERY outcome's ``repointed``,
    not just the filtered ones.
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
        "snapshot_splits": report.snapshot_splits,
    }


def report_has_findings(d: dict) -> bool:
    """True iff a ``report_as_dict`` dict has an attention finding -- design/superpowers/
    specs/2026-07-11-ci-live-findings-design.md ruling 1: an ``error`` outcome, OR a
    non-empty ``stale_decisions``, OR a non-empty ``slug_conflicts``, OR a ``domain_failures``
    entry other than a busy ``*`` one. ``orphaned``/``ambiguous`` outcomes and ``empty_domains``/
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
    # keeps CI red forever on a healthy repo. A domain_failure is the opposite shape: it
    # exists only while a refresh actually raises. A pass in which a domain failed never
    # certifies domain membership and records the failure (MEMBERSHIP_FAILED_KEY), so every
    # later sync, lazy or explicit, reports it again; it recomputes once the graph, the
    # snapshot or a domain's status changes, or under --force. Same class as an `error`
    # outcome for `--check` purposes; fix the cause, then re-run with `--force` to confirm it
    # cleared. The one exception is the `*` entry with `busy: true`: another writer held the
    # store, nothing failed, and the next sync runs the pass, so it is no finding.
    return bool(
        d["stale_decisions"]
        or d["slug_conflicts"]
        or any(not f.get("busy") for f in d["domain_failures"])
    )


def maybe_sync(
    store: Store, reader: GraphifyReader | None, *, canonical_writes: bool = True
) -> SyncReport | None:
    """Lazy read-path trigger: no reader -> None; else sync (self-skips on version match).
    ``canonical_writes=False`` is ``sync``'s index-only mode, for a borrowed graph.

    Callers wrap this best-effort — a sync failure must never break retrieval or startup.
    """
    if reader is None:
        return None
    return sync(store, reader, canonical_writes=canonical_writes)


class DriftRefresh(NamedTuple):
    """What one refresh published: the live drifted count and the recency pair (or None)."""

    total: int
    recent: tuple[int, str] | None


def build_cache(raw: str | None, scan: CodeDriftScan, now: datetime) -> dict:
    """The next code-drift cache value from the previous raw value and a finished scan.

    Per capture commit a successful batch replaces its entry and a failed batch retains the
    previous one; the first-seen stamps and tombstones come from `stamp_cache`. Ids whose
    optional check comparison did not run (a section scope that could not be bounded
    included) are `retained`, and never stamped ``now``. Pure:
    no git, no store reads.
    # see design/superpowers/specs/2026-10-27-drift-notice-delta-design.md (4.2, 4.3)
    """
    prev: object = None
    prev_by_commit: dict[str, list[str]] = {}
    if raw is not None:
        try:
            prev = json.loads(raw)
        except json.JSONDecodeError:
            prev = None
        if isinstance(prev, dict) and isinstance(prev.get("by_commit"), dict):
            prev_by_commit = {
                c: [r for r in ids if isinstance(r, str)]
                for c, ids in prev["by_commit"].items()
                if isinstance(ids, list)
            }

    by_commit: dict[str, list[str]] = {}
    retained: set[str] = set()
    for batch in scan.batches:
        if batch.failed:
            if batch.commit in prev_by_commit:
                by_commit[batch.commit] = prev_by_commit[batch.commit]
        else:
            by_commit[batch.commit] = sorted({e.record_id for e in batch.entries})
            retained.update(
                e.record_id
                for e in batch.entries
                if e.check_reason is not None
                and (
                    "capture advisory retained" in e.check_reason
                    or e.check_reason == SECTION_UNAVAILABLE
                )
            )
    ids = {rid for members in by_commit.values() for rid in members}
    if scan.checks_unreadable:
        retained = set(ids)
    since, first_seen, absent = stamp_cache(prev, ids, retained, now)
    # No first_seen_checked here: stamp_cache consumed the marks (store_layout.checked_marks).
    return {
        "head": scan.head,
        "computed_at": now.isoformat(),
        "by_commit": by_commit,
        "first_seen_since": since,
        "first_seen": first_seen,
        "first_seen_absent": absent,
    }


def refresh_code_drift_snapshot(
    store: Store,
    *,
    now: datetime | None = None,
    repo_root: Path | None = None,
    deadline: float = 10.0,
    busy_timeout_seconds: float = 5.0,
) -> DriftRefresh | None:
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

    - inactive scan (nothing commit-stamped and anchored) → return ``DriftRefresh(0, None)``,
      write nothing;
    - unscanned (repo root resolution failed — no batch ran) → leave any existing cache
      byte-untouched, return ``None``. A failed HEAD resolution alone does NOT discard a
      scan whose batches ran (review M-2: ``head`` is provenance only and gates nothing
      — the cache is written with ``"head": null``);
    - otherwise: a successful batch REPLACES its commit's entry (an empty list is a
      normal write — that is how markers clear once a repair supersede plus its commit
      land); a failed batch RETAINS the previous cache's entry for its commit, if any;
      commits absent from the scan's batch set drop out entirely (their records left
      the live+stamped+anchored join).

    Returns a `DriftRefresh`: the live-filtered flattened count (`live_drifted_ids` — the one
    filter definition, shared with the render side) and the recency pair, both derived from
    the payload this call published. Never raises: any unexpected failure
    returns ``None`` with the cache untouched (hook-caller contract).
    # see design/superpowers/specs/2026-07-30-drift-supersede-affordance-design.md (D2)
    # and design/superpowers/specs/2026-10-27-drift-notice-delta-design.md (4.3)
    """
    try:
        scan = scan_code_drift(
            store.path,
            repo_root,
            deadline=deadline,
            busy_timeout_seconds=busy_timeout_seconds,
        )
        if not scan.active:
            return DriftRefresh(0, None)
        # A hook that cannot certify binding membership must not read an old drift cache or
        # publish a result filtered against incomplete statuses. With zero wait, this guard
        # wins even when the unreadable snapshot coincides with the ordinary uncomputed state.
        if busy_timeout_seconds == 0.0 and scan.binding_statuses_uncomputed:
            return None
        if scan.repo_root_failed:
            return None

        payload: dict = {}
        stamped_at: list[datetime] = []

        def decide(raw: str | None) -> str:
            # The clock is read under the write lock: a stamp is never older than the publish.
            instant = now if now is not None else datetime.now(UTC)
            payload.update(build_cache(raw, scan, instant))
            stamped_at.append(instant)
            return json.dumps(payload)

        store.update_meta_if(DRIFT_CACHE_KEY, decide)
        live = live_drifted_ids(store, payload["by_commit"])
        return DriftRefresh(len(live), recent_drift(live, payload, stamped_at[0]))
    except Exception:
        return None


def refresh_code_drift_cache(
    store: Store,
    *,
    repo_root: Path | None = None,
    deadline: float = 10.0,
    busy_timeout_seconds: float = 5.0,
) -> int | None:
    """`refresh_code_drift_snapshot`, reduced to the live drifted count (the Stop hook's view)."""
    snapshot = refresh_code_drift_snapshot(
        store,
        repo_root=repo_root,
        deadline=deadline,
        busy_timeout_seconds=busy_timeout_seconds,
    )
    return None if snapshot is None else snapshot.total
