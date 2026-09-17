"""Bootstrap ``Domain`` proposals from graph communities (portable core, path 1 of the
three authoring paths — see docs/concepts/mind-model.md#domain-lifecycle).

Reads communities via the engine seam (``GraphifyReader.communities()`` +
``community_labels()``) and writes proposals through ``store.add_domain`` — same
"deterministic pipeline, status=proposed" shape as ``importer.import_rationales`` and
``capture.propose_domains``, but scanning the graph's own community structure instead of
rationale nodes or agent drafts. One ``Domain`` draft per *significant* community (member
count, counting only anchorable nodes, at or above ``min_members``): slug/title/summary
prefer the engine's community label when the corpus has one, falling back to a
deterministic god-node-derived name/digest otherwise. Everything lands ``status=proposed``
— bootstrap is not exempt from the single ratification gate (§4).
"""

from __future__ import annotations

import json

from pydantic import BaseModel, Field

from .capture import (
    AutoEligibility,
    RatifyPolicy,
    _auto_ratify,
    _domain_anchored,
    _lint_domain_path_prefixes,
    auto_ratify_eligible,
    redact,
)
from .engine.reader import ANCHORABLE_FILE_TYPES, Community, GraphifyReader
from .retrieval import TOC_CACHE_KEY, build_toc
from .schema import Descriptor, Domain, DomainStatus, Provenance, matches_path_prefix, slugify
from .store import Store
from .sync import activate_accepted_domain

# Below this fraction of anchorable members sharing a single top-level directory,
# path_prefixes is left empty rather than derived from a weak majority (§4.1: "≥80% of
# members share a top-level dir").
_PATH_PREFIX_THRESHOLD = 0.8

# How many member names surface in the deterministic digest fallback summary
# ("N entities incl. a, b, c — around <top file>").
_DIGEST_SAMPLE_SIZE = 3

# Well-known shared/infra top-level directory names: NEVER derived as a path_prefixes
# stabilizer, even when a community's members overwhelmingly share one of them. Gate-5
# blocker: a community that clusters with its own tests/ (>=80% of its anchorable members
# live there) is the NORMAL case for a god-node community with per-module tests, not
# evidence "tests/" is that community's home — deriving it as a stabilizer let sync's
# REPLACE refresh (see sync._recompute_domain_communities) legitimately, and silently,
# expand the domain to swallow every OTHER community that also ships tests/ (true of any
# community in the corpus). See docs/guides/naming-your-domains.md.
_SHARED_DIR_NAMES = frozenset(
    {
        "test",
        "tests",
        "src",
        "lib",
        "docs",
        "doc",
        "examples",
        "scripts",
        "tools",
        "utils",
    }
)

# A path-derived candidate is also skipped when the communities sharing its winning
# directory — the candidate's own community INCLUDED — already add up to more than this
# fraction of ALL communities in the graph. Both the numerator (the claimed set, computed
# by _prefix_community_breadth) and the denominator (the total community count) count the
# candidate's own community; this is deliberately the SAME ratio
# sync._recompute_domain_communities computes for its refresh-time _DOMAIN_CLAIM_CAP
# (Option A, owner-approved 2026-09-14: the two guards used to disagree — derive excluded
# the candidate's own community from its "other communities" count while refresh included
# it, so bootstrap could derive a stabilizer the very next refresh rejected as overbroad.
# Tightening derive to match refresh's numerator, over the SAME graph snapshot, is the
# goal — a rule this guard lets through should never trip the refresh cap AT THE MOMENT
# IT IS DERIVED. That depended on _majority_top_dir and _prefix_community_breadth
# decomposing file_path the SAME way, which round 1 alone did not achieve — see
# _prefix_community_breadth's docstring, NEW Major A, fix round 2. This says nothing
# about a LATER graph rebuild either way — a directory one community owns exclusively
# today can be shared by five tomorrow, and refresh capping the rule then is the design
# working as intended, not a regression of this guarantee). Generalizes the
# _SHARED_DIR_NAMES guard past its fixed name list to any cross-cutting directory pattern
# (vendored deps, generated code, ...).
_PREFIX_BREADTH_CAP = 0.2

# Scale-aware default `limit` (finding B, scale-robustness hardening wave): on a monorepo-
# scale corpus (Airflow: 5,517 communities, 2,578 significant) an unbounded
# `list_domain_candidates` call is a ~276K-token dump — ~140x a normal budget. 100 is the
# measured sweet spot (~10K tokens). This is a PUBLIC-SURFACE default applied by the
# `list_domain_candidates` MCP tool and the `sidegraph-domains bootstrap` CLI, one shared
# constant so the two stay consistent by construction; `collect_domain_candidates`'s own
# `limit` parameter default stays `None` (unlimited) -- callers that want everything
# (tests included) never have to fight a collector-level default.
DEFAULT_CANDIDATE_LIMIT = 100


class BootstrapReport(BaseModel):
    """Counts (+ dry-run-only listing) for one ``bootstrap_domains`` run.

    ``proposed``/``skipped_existing`` reflect what WOULD happen whether or not
    ``dry_run`` actually wrote anything (mirrors ``ImportReport``); ``below_threshold``
    and ``filtered`` explain why a community was never even considered a candidate.
    ``dry_run`` carries the listing and is populated only on a dry run.

    ``total_before_limit`` (finding B/C) mirrors ``CollectStats.total_before_limit`` — the
    full significant-community count BEFORE ``limit`` truncated it, computed either way
    (dry run or not). The CLI uses it to note when ``--limit`` (default or explicit) cut
    candidates the run never even considered, in BOTH the ``--dry-run`` listing and the
    real, pre-write threshold check (BUG C: that check runs off a separate, read-only
    ``collect_domain_candidates`` call made BEFORE ``bootstrap_domains`` writes anything —
    see ``cli._domains_bootstrap``).
    """

    proposed: int = 0
    skipped_existing: int = 0
    below_threshold: int = 0
    filtered: int = 0
    total_before_limit: int = 0
    # Auto-ratification policy (design D2/D6) -- additive/defaulted, same contract as
    # importer.ImportReport's/doc_import.DocImportReport's own pair: incremented/appended
    # by the post-write auto block below, always empty/zero under `manual`/`auto-low-risk`
    # (domains are `auto-all`-only, D3) or when a proposed domain was ineligible. A domain
    # accepted before its activation step fails increments this AND appends the
    # activation failure -- the counter reports canonical acceptance, not a clean run.
    auto_ratified: int = 0
    auto_ratify_failures: list[str] = Field(default_factory=list)  # ["<domain id>: <reason>"]
    dry_run: list[dict] = Field(default_factory=list)  # [{"community_id", "slug", ...}, ...]
    # design D7.4 (staleness-machinery wave, E8 gate checklist): deterministic lint
    # warnings, one entry per candidate that has >= 1 (see capture._lint_domain_path_
    # prefixes) -- {"slug", "warnings": [...]}. Populated on BOTH dry-run and real runs;
    # advisory only, never blocks the write -- under `auto-all` any warning keeps the
    # draft proposed.
    warnings: list[dict] = Field(default_factory=list)


class DomainCandidate(BaseModel):
    """One significant community's fully-resolved bootstrap proposal, as computed by
    ``collect_domain_candidates`` — the single source of truth ``bootstrap_domains``
    (which writes it) and the read-only ``list_domain_candidates`` MCP tool (which only
    displays it) both build on, so the tool always shows exactly what the CLI would
    propose (see domain-onboarding design §1)."""

    community_id: str
    suggested_slug: str
    suggested_title: str
    summary: str
    path_prefixes: list[str] = Field(default_factory=list)
    member_count: int
    top_members: list[str] = Field(default_factory=list)  # <= _DIGEST_SAMPLE_SIZE names
    top_file: str | None = None
    has_label: bool = False  # a validated engine label survived _label_names_other_community
    # The community's god-node resolved to a durable name+file_path Descriptor (§2a
    # amendment) -- what `list_domain_candidates` surfaces so the name-domains skill can
    # build a Domain.seed_anchors entry from a candidate, rather than the volatile
    # community_id alone (which does not survive a fresh clone/rebuild). None only when the
    # community has no god node, or the god node itself has no name (defensive).
    anchor: Descriptor | None = None


class CollectStats(BaseModel):
    """Selection counts for one ``collect_domain_candidates`` run — the shared basis for
    ``BootstrapReport``'s fields and ``list_domain_candidates``'s ``skipped``/
    ``already_claimed`` output. ``already_claimed`` folds together the two "this
    community isn't a NEW candidate" reasons the collector's Pass 1 can hit — the
    community itself already claimed by a non-superseded domain, and a computed slug
    colliding with one already in the store — matching ``BootstrapReport.skipped_existing``
    exactly (see ``collect_domain_candidates``'s own docstring, "Idempotent:" paragraph,
    for why both count as "existing").

    ``total_before_limit`` (finding B) is the significant (threshold/path-filter-surviving)
    community count BEFORE ``limit`` truncates it — cheap to compute (a bare ``len()`` right
    after the filter loop, no per-candidate label/summary/path_prefixes work, which stays
    bounded to at most ``limit`` items). This is the basis for ``list_domain_candidates``'s
    ``truncated``/``total_significant`` signaling and ``sidegraph-domains bootstrap``'s
    before-write over-threshold nag: comparing it against the requested ``limit`` answers
    "were there more candidates than what got shown/written?" independent of the SEPARATE
    already-claimed skip, which only ever narrows the (possibly already-limited) survivor
    set further and says nothing about how many candidates existed in total."""

    total: int = 0
    total_before_limit: int = 0
    already_claimed: int = 0
    below_threshold: int = 0
    filtered: int = 0


def _anchorable_member_ids(community_members: list[str], reader: GraphifyReader) -> list[str]:
    out = []
    for node_id in community_members:
        node = reader.get_node(node_id)
        if node is not None and node.file_type in ANCHORABLE_FILE_TYPES:
            out.append(node_id)
    return out


def _matches_any_prefix(node_ids: list[str], prefixes: list[str], reader: GraphifyReader) -> bool:
    for node_id in node_ids:
        node = reader.get_node(node_id)
        if (
            node is not None
            and node.file_path is not None
            and any(matches_path_prefix(node.file_path, p) for p in prefixes)
        ):
            return True
    return False


def _top_file(
    anchorable_ids: list[str],
    god_node_id: str | None,
    reader: GraphifyReader,
) -> str | None:
    """The god node's file, when it has one; else the most common file_path among
    members (a deterministic stand-in for "where this community lives" when no single
    node dominates by degree)."""
    if god_node_id is not None:
        god = reader.get_node(god_node_id)
        if god is not None and god.file_path:
            return god.file_path
    counts: dict[str, int] = {}
    for node_id in anchorable_ids:
        node = reader.get_node(node_id)
        if node is not None and node.file_path:
            counts[node.file_path] = counts.get(node.file_path, 0) + 1
    if not counts:
        return None
    return max(counts.items(), key=lambda kv: kv[1])[0]


def _god_node_descriptor(god_node_id: str | None, reader: GraphifyReader) -> Descriptor | None:
    """The community's god-node resolved to a durable ``Descriptor`` (name + file_path) —
    what a ``Domain.seed_anchors`` entry needs to durably anchor to this community across
    clones/rebuilds (§2a amendment). ``None`` when the community has no god node, or the
    god node has no name (defensive — mirrors the blank-name guard ``collect_domain_
    candidates`` already applies to the god-node title fallback)."""
    if god_node_id is None:
        return None
    node = reader.get_node(god_node_id)
    if node is None or not node.name.strip():
        return None
    return Descriptor(name=node.name, file_path=node.file_path)


def _top_member_names(
    anchorable_ids: list[str],
    reader: GraphifyReader,
    limit: int = _DIGEST_SAMPLE_SIZE,
) -> list[str]:
    """First ``limit`` named anchorable members, in member-list order — the same sample
    ``_member_digest``'s "incl. a, b, c" prose draws from, also surfaced verbatim as a
    ``DomainCandidate``'s ``top_members`` for the ``list_domain_candidates`` tool."""
    names: list[str] = []
    for node_id in anchorable_ids:
        node = reader.get_node(node_id)
        if node is not None and node.name:
            names.append(node.name)
        if len(names) >= limit:
            break
    return names


def _member_digest(
    anchorable_ids: list[str],
    god_node_id: str | None,
    reader: GraphifyReader,
) -> str:
    """Deterministic fallback summary when the engine has no label for this community:
    "N entities incl. a, b, c — around <top file>"."""
    names = _top_member_names(anchorable_ids, reader)
    incl = ", ".join(names) if names else "unnamed members"
    n = len(anchorable_ids)
    top_file = _top_file(anchorable_ids, god_node_id, reader)
    if top_file:
        return f"{n} entities incl. {incl} — around {top_file}"
    return f"{n} entities incl. {incl}"


def _prefix_community_breadth(reader: GraphifyReader) -> tuple[dict[str, set[str]], int]:
    """Precompute, once per bootstrap run: for every top-level directory that appears in
    an anchorable node's file_path ANYWHERE in the graph, the set of community ids that
    have >= 1 such node, plus the total number of distinct communities in the graph. This
    is the denominator/lookup ``_derive_path_prefixes``'s breadth guard needs (see
    ``_PREFIX_BREADTH_CAP``) — computed once rather than per-candidate-community, since
    it depends on the whole graph, not any one community's members.

    The index key is ``file_path.split("/", 1)[0]`` — the text up to (not including) the
    first ``"/"``, or the whole string when there is none (fix round 1, Major 1: a
    ``PurePosixPath`` + "skip anything under 2 parts" version used here before missed a
    ``file_path`` that IS the bare directory name, e.g. ``"trader"``, or that name plus a
    trailing slash, e.g. ``"trader/"`` — both match ``schema.matches_path_prefix`` at
    refresh time but were invisible to derive's breadth count, so a community sharing a
    directory only through such a node could trip the refresh cap without derive ever
    seeing it coming). No length filter: a bare ``"trader"`` node contributes to
    ``dir_to_communities["trader"]`` exactly as ``"trader/x.py"`` does, same as
    ``matches_path_prefix("trader", "trader")`` is ``True``.

    ``_majority_top_dir`` decomposes each candidate's OWN members' paths with
    ``str.partition("/")`` for the identical head value (fix round 2, NEW Major A: it was
    still on ``PurePosixPath`` when this function's key moved above, so the two disagreed
    on an absolute ``file_path`` — ``PurePosixPath("/a/x.py").parts[0]`` is ``"/"``, this
    function's key for the same path is ``""`` — and derive could pick a majority top
    directory this dict had no entry for at all, so the single-community floor waved it
    through unchecked). ``_majority_top_dir`` additionally REJECTS an empty head (an
    absolute path can never become a majority top directory — see its own docstring), so
    every ``top_dir`` this module can actually derive is now a key this dict's lookup
    treats the identical way ``matches_path_prefix`` would.
    """
    dir_to_communities: dict[str, set[str]] = {}
    all_communities: set[str] = set()
    for node in reader.list_nodes():
        if node.community is not None:
            all_communities.add(node.community)
        if node.file_type not in ANCHORABLE_FILE_TYPES or not node.file_path:
            continue
        if node.community is None:
            continue
        top_dir = node.file_path.split("/", 1)[0]
        dir_to_communities.setdefault(top_dir, set()).add(node.community)
    return dir_to_communities, len(all_communities)


def _majority_top_dir(anchorable_ids: list[str], reader: GraphifyReader) -> str | None:
    """The single top-level directory shared by >= ``_PATH_PREFIX_THRESHOLD`` of the
    members that HAVE a file_path (and a directory component); else ``None`` — a weak
    majority is worse than no signal at all. The bare majority calc, with neither of
    ``_derive_path_prefixes``'s two stabilizer-only vetoes applied — reused by
    ``community_group_path`` for ``list_domain_candidates``'s presentational grouping
    (a structure HINT, never a membership rule, so those vetoes don't apply there).

    Decomposes each ``file_path`` with ``str.partition("/")`` — ``head, sep, _rest`` —
    the SAME single decomposition ``_prefix_community_breadth`` uses, and equivalent to
    ``schema.matches_path_prefix`` for every head this function can return (fix round 2,
    NEW Major A: this function was left on ``PurePosixPath`` when round 1 moved breadth's
    own key to ``split("/", 1)[0]``, so the two disagreed on an absolute path —
    ``PurePosixPath("/a/x.py").parts[0]`` is ``"/"``, the partition head is ``""`` — and
    derive could pick a majority directory breadth had no record of at all). NOT the same
    decomposition ``matches_path_prefix`` itself performs — that function never
    partitions anything, it compares with ``==``/``startswith`` — merely one that always
    agrees with it for a single-segment prefix (measured exhaustively over 9,331
    synthetic paths and every real corpus in the workspace, fix round 2 review).

    A member "has a directory component" (counts toward the vote) only when there IS a
    separator AND the head is non-empty: no separator (``"x.py"``) is skipped exactly as
    before the round-2 fix; an EMPTY head (``"/a/x.py"``, ``"//a/x.py"``) is skipped too
    — an absolute path can never become a majority top directory, since after
    ``matches_path_prefix`` strips a trailing slash from an empty-string prefix, it would
    match every absolute path AND every empty ``file_path`` in the graph. This "no
    separator" skip is DELIBERATELY narrower than ``_prefix_community_breadth``'s own
    filter (fix round 3, NEW Minor C: a bare ``"trader"`` node still indexes under
    ``dir_breadth["trader"]`` there, unchanged — see that function's docstring) because
    the two functions answer different questions over the SAME decomposed head: this one
    asks which directory a community's OWN members mostly live in — a member with no
    separator has no directory to vote for, full stop — while breadth asks which
    candidate top directories a refresh with prefix D would go on to MATCH — and
    ``matches_path_prefix(fp, fp)`` is ``True`` for a bare name matching itself exactly,
    so a bare-name node must still be indexed under its own name for that question. A
    majority function that let bare names vote would propose file names like
    ``"pyproject.toml"`` as directory stabilizers on a real corpus, instead of a
    community's actual home directory (or no stabilizer, correctly, when there is
    none) — mutant F15 (fix round 3 review) is this exact veto removed, and
    ``test_bootstrap_majority_ignores_a_root_level_file_like_a_missing_vote`` below pins
    it."""
    dirs: dict[str, int] = {}
    total = 0
    for node_id in anchorable_ids:
        node = reader.get_node(node_id)
        if node is None or not node.file_path:
            continue
        head, sep, _rest = node.file_path.partition("/")
        if not sep or not head:
            continue  # no directory component to group by (bare name, or an absolute path)
        total += 1
        dirs[head] = dirs.get(head, 0) + 1
    if total == 0:
        return None
    top_dir, count = max(dirs.items(), key=lambda kv: kv[1])
    if count / total < _PATH_PREFIX_THRESHOLD:
        return None
    return top_dir


def _derive_path_prefixes(
    anchorable_ids: list[str],
    community_id: str,
    reader: GraphifyReader,
    dir_breadth: dict[str, set[str]],
    total_communities: int,
) -> list[str]:
    """``[_majority_top_dir(...)]`` when there is one; else ``[]``.

    Past the majority check, two more guards (Gate-5 finding, see the module-level
    ``_SHARED_DIR_NAMES``/``_PREFIX_BREADTH_CAP`` docstrings) can still veto the winning
    directory back to ``[]``: (a) it's a well-known shared/infra directory name, or (b) the
    communities sharing it — its own community INCLUDED, via ``dir_breadth`` from
    ``_prefix_community_breadth`` — already add up to more than 20% of ALL communities in
    the graph today (the same claimed-set-over-total ratio
    ``sync._recompute_domain_communities`` caps at refresh time, over the SAME graph
    snapshot; the single-community floor below is the identical floor that guard has, for
    the identical reason). This depends on ``top_dir`` itself and ``dir_breadth`` agreeing
    on what a directory name IS (fix round 2, NEW Major A: ``_majority_top_dir`` used to
    decompose ``file_path`` differently than ``_prefix_community_breadth`` did, so a
    ``top_dir`` this function picked could be a key ``dir_breadth`` had never populated at
    all — the single-community floor then waved an absolute-path directory like ``"/"``
    straight through; see both functions' own docstrings). Either veto is evidence the
    directory is a cross-cutting pattern, not this community's own home, and deriving it
    as a stabilizer would let sync's REPLACE refresh silently swallow every other
    community that happens to share it too — today. A later graph rebuild can still grow
    a directory's share past the cap; refresh capping the rule then is this guard's own
    defense-in-depth working as designed, not a hole in what this function promises.
    """
    top_dir = _majority_top_dir(anchorable_ids, reader)
    if top_dir is None:
        return []
    if top_dir in _SHARED_DIR_NAMES:
        return []
    if total_communities > 0:
        claimed = dir_breadth.get(top_dir, set()) | {community_id}
        if len(claimed) > 1 and len(claimed) / total_communities > _PREFIX_BREADTH_CAP:
            return []
    return [top_dir]


def community_group_path(
    community_id: str,
    reader: GraphifyReader,
    communities_by_id: dict[str, Community] | None = None,
) -> str | None:
    """The presentational top-level-directory grouping hint for ``community_id`` — the
    SAME majority-dir calc ``_derive_path_prefixes`` uses for its stabilizer rule, minus
    both of its vetoes (a structure HINT only, per domain-onboarding design §1's "hybrid"
    grouping — never a membership rule, so the vetoes that protect ``path_prefixes`` from
    becoming an over-broad sync stabilizer don't apply here). Used by
    ``list_domain_candidates`` to group a ``DomainCandidate`` that didn't itself derive a
    ``path_prefixes`` stabilizer. ``communities_by_id``, when given, skips re-walking
    ``reader.communities()`` per candidate — the caller builds it once."""
    by_id = communities_by_id
    if by_id is None:
        by_id = {c.community_id: c for c in reader.communities()}
    community = by_id.get(community_id)
    if community is None:
        return None
    anchorable = _anchorable_member_ids(community.members, reader)
    return _majority_top_dir(anchorable, reader)


def _dedupe_identical_prefix_sets(
    pending: list[dict],
    reader: GraphifyReader,
    dir_breadth: dict[str, set[str]],
    total_communities: int,
) -> dict[str, list[str]]:
    """Derive ``path_prefixes`` for every ``pending`` bootstrap candidate this run WILL
    propose, then dedupe: when >= 2 of them derive the IDENTICAL non-empty prefix set
    (live finding — two communities each independently >= 80% under the same top-level
    directory, e.g. both under 'trader/'), only the candidate with the MOST anchorable
    members keeps it; the rest fall back to ``[]`` (title/summary/slug untouched, still
    proposed). Ties on member count break on the LOWER community id
    (``_community_sort_key`` — same numeric-aware ordering ``bootstrap_domains`` already
    uses for candidate ordering, so the winner is deterministic across runs of the same
    graph).

    Without this, two domains racing for the same directory stabilizer within one run
    would both carry a ``path_prefixes`` rule that matches the SAME code — a later sync
    REPLACE refresh (``sync._recompute_domain_communities``) would then let both domains'
    ``communities`` legitimately point at (some of) the same nodes, the identical
    "silently-overlapping memory" shape the Gate-5 breadth guard exists to prevent, just
    triggered by two rules agreeing with each other instead of one rule alone reaching too
    broad.
    """
    derived: dict[str, list[str]] = {
        p["community_id"]: _derive_path_prefixes(
            p["anchorable"], p["community_id"], reader, dir_breadth, total_communities
        )
        for p in pending
    }
    member_counts = {p["community_id"]: len(p["anchorable"]) for p in pending}

    by_prefix_set: dict[tuple[str, ...], list[str]] = {}
    for community_id, prefixes in derived.items():
        if prefixes:
            by_prefix_set.setdefault(tuple(prefixes), []).append(community_id)

    for community_ids in by_prefix_set.values():
        if len(community_ids) < 2:
            continue
        winner = min(
            community_ids,
            key=lambda cid: (-member_counts[cid], _community_sort_key(cid)),
        )
        for cid in community_ids:
            if cid != winner:
                derived[cid] = []

    return derived


def _dedupe_slug(base: str, used: set[str]) -> str:
    """Disambiguate two communities that would otherwise mint the same slug within THIS
    run (e.g. two communities both god-node-named ``utils.py``) with -2/-3 suffixes.
    Collisions against slugs already living in the store are handled separately, as a
    skip (see the idempotency check in ``bootstrap_domains``) — never suffixed."""
    if base not in used:
        used.add(base)
        return base
    i = 2
    candidate = f"{base}-{i}"
    while candidate in used:
        i += 1
        candidate = f"{base}-{i}"
    used.add(candidate)
    return candidate


def _community_sort_key(community_id: str) -> tuple[int, object]:
    """Numeric communities sort numerically; anything else falls back to lexicographic —
    keeps bootstrap output (and --limit's meaning) deterministic across runs of the same
    graph.json regardless of dict/set iteration order."""
    try:
        return (0, int(community_id))
    except ValueError:
        return (1, community_id)


def _claimed_communities(store: Store) -> set[str]:
    """Community ids already claimed by a non-superseded domain (proposed, accepted, OR
    dropped — matches ``find_domain_by_slug``'s "non-superseded" rule exactly, see
    capture.py's ``_propose_domain_one``)."""
    claimed: set[str] = set()
    for domain in store.iter_domains():
        if domain.status == DomainStatus.SUPERSEDED:
            continue
        claimed.update(domain.communities)
    return claimed


def _label_names_other_community(label: str, community_id: str, reader: GraphifyReader) -> bool:
    """True when ``label`` is demonstrably the engine's name for an entity that
    ``reader``'s CURRENT graph places in a community other than ``community_id``.

    Guards against a real-corpus finding (independent evaluation, 2026-07-08): a
    community's label in ``.graphify_labels.json`` does not always correspond to the
    SAME community partition as the graph.json sitting next to it — verified even when
    both come from the exact same ``graph_version`` (so not stale/drifted engine data,
    just the label pass and the community field disagreeing on numbering). Trusting such
    a label verbatim would title/slug a domain after an entity that isn't actually in the
    community the domain stores, e.g. a domain named "OrderBook" whose stored community
    is all TA-strategy files because the label sidecar's community numbering slipped.

    Uses ``GraphifyReader.resolve`` — the same name -> node(s) -> community resolution
    ``capture``'s anchor binding already relies on — rather than re-deriving anything
    here:

    - ``resolved`` (one match): rejected when that node's community differs from
      ``community_id``.
    - ``ambiguous`` (several same-named nodes): each candidate's OWN community is looked
      up (``ResolveResult.candidates`` only carries node ids); rejected ONLY when NONE of
      them live in ``community_id`` — i.e. every candidate is provably elsewhere. A label
      with at least one candidate in ``community_id`` is plausibly correct and kept, even
      though other, same-named entities exist elsewhere too (measured on the real corpus:
      32/259 proposals had a label with zero candidates in its own community — all
      provably wrong; the remaining ambiguous labels all had a candidate that matched,
      and rejecting those too would have thrown away otherwise-correct titles).
    - ``unresolved`` (no node anywhere has this name — free-form descriptive prose, e.g.
      "Alpha Domain", a legitimate, tested label shape, see
      ``test_bootstrap_labeled_community_uses_label_for_title_and_summary``): no evidence
      against it, kept.
    """
    result = reader.resolve(Descriptor(name=label))
    if result.status == "unresolved":
        return False
    if result.status == "resolved":
        return result.community is not None and result.community != community_id
    candidate_communities = {
        node.community
        for node in (reader.get_node(node_id) for node_id in result.candidates)
        if node is not None and node.community is not None
    }
    return bool(candidate_communities) and community_id not in candidate_communities


def collect_domain_candidates(
    store: Store,
    reader: GraphifyReader,
    *,
    min_members: int = 5,
    paths: list[str] | None = None,
    limit: int | None = None,
) -> tuple[list[DomainCandidate], CollectStats]:
    """The deterministic candidate-selection body shared by ``bootstrap_domains`` (which
    then writes each survivor as a ``Domain``) and the read-only ``list_domain_candidates``
    MCP tool (which only displays them) — one source of truth, so the tool always shows
    exactly what the CLI would propose (domain-onboarding design §1).

    A community is *significant* when its anchorable member count (code/document/
    concept/rationale nodes only — never-guess extends to file types, same filter as
    ``GraphifyReader.ANCHORABLE_FILE_TYPES``) is ``>= min_members``. ``paths``, when
    given, further restricts candidates to communities with at least one anchorable
    member whose ``file_path`` starts with one of the given prefixes (mirrors
    ``import_rationales``'s per-node ``--path`` filter, applied existentially at the
    community level since a community has no single file_path of its own). ``limit``
    caps the number of *significant* communities considered, applied after the
    threshold/path filters in a deterministic (community-id-sorted) order — same
    ordering rule ``import_rationales`` uses for its own ``kept[:limit]`` slice.
    ``None`` (this function's own default) means unlimited; the public surfaces built on
    top of this collector (the ``list_domain_candidates`` MCP tool, ``sidegraph-domains
    bootstrap``) apply their own scale-aware default (``DEFAULT_CANDIDATE_LIMIT``, finding
    B) instead of inheriting this one, so a caller reaching for the collector directly (a
    test, a script) never has to fight an implicit cap. The returned ``CollectStats.
    total_before_limit`` names the full significant count regardless of what ``limit``
    trimmed it to — repeatedly bootstrapping under the SAME low limit only ever proposes
    the same low community-id-sorted window (already-claimed communities in that window
    get skipped on a rerun, but communities past the window are never reached); widen
    ``limit`` (or narrow ``min_members``/``paths``) to get past it.

    Per significant community: slug/title come from the engine's community label
    (``GraphifyReader.community_labels()``) when present, else the community's god-node
    name; summary is the label itself when present, else a deterministic member digest
    ("N entities incl. a, b, c — around <top file>"). A label that ``resolve()`` shows
    names an entity actually living in a DIFFERENT community is rejected first and never
    reaches title/summary — see ``_label_names_other_community`` — since the community
    field a label's key was written against isn't guaranteed to match this graph.json's
    (real-corpus finding: a domain's own namesake god node ended up outside the community
    the domain stored). ``path_prefixes`` is derived only
    when >= 80% of members share a single top-level directory AND that directory is
    neither a well-known shared/infra name (``tests/``, ``src/``, ...) nor already claimed
    (own community included) by communities adding up to > 20% of ALL communities in the
    graph (Gate-5 guard — see ``_derive_path_prefixes``); either trips leaves
    ``path_prefixes`` empty rather than a stabilizer that would let a later sync silently
    swallow unrelated communities.
    Redaction runs on
    title/summary/slug regardless of origin (§4.1: "bootstrap summaries derive from
    engine data, redact anyway" — the graph can embed doc/rationale text a human typed);
    the slug is derived from the already-redacted title, never the raw label/god-node
    name, so secret material can't leak into the repo-committed slug or its paired
    `domain:<slug>` entity name.

    Each candidate also carries ``anchor`` — the community's god-node resolved to a durable
    name+file_path ``Descriptor`` (§2a amendment, unredacted — a code/doc symbol name, not
    free-form prose). ``list_domain_candidates`` surfaces this so the ``name-domains``
    skill can build a ``Domain.seed_anchors`` entry directly from a candidate instead of
    the volatile ``community_id`` alone, which does not survive a fresh clone or rebuild.

    Idempotent: a community already claimed by a non-superseded domain (``communities``
    contains its id) is skipped outright, and a computed slug colliding with an existing
    non-superseded domain is also skipped (``store.find_domain_by_slug``) — never
    reproposed, never suffixed against the store (suffixing only disambiguates
    collisions WITHIN this run). Both skips fold into ``CollectStats.already_claimed``.

    Within-run prefix dedup (live finding, see ``_dedupe_identical_prefix_sets``): when two
    or more candidates surviving THIS run independently derive the IDENTICAL non-empty
    ``path_prefixes`` set, only the one with the most anchorable members keeps it — the
    rest still survive, just with ``path_prefixes=[]`` (title/summary/slug untouched). Two
    domains carrying a stabilizer that matches the same code would let a later sync
    REPLACE refresh point both at (some of) the same communities, the same
    silently-overlapping-memory shape the breadth guard above exists to prevent.
    """
    labels = reader.community_labels()
    claimed = _claimed_communities(store)
    dir_breadth, total_communities = _prefix_community_breadth(reader)

    stats = CollectStats()
    # (community_id, anchorable_ids, god_node)
    candidates: list[tuple[str, list[str], str | None]] = []
    for community in reader.communities():
        anchorable = _anchorable_member_ids(community.members, reader)
        if len(anchorable) < min_members:
            stats.below_threshold += 1
            continue
        if paths and not _matches_any_prefix(anchorable, paths, reader):
            stats.filtered += 1
            continue
        candidates.append((community.community_id, anchorable, community.god_node))

    candidates.sort(key=lambda c: _community_sort_key(c[0]))
    # Free byproduct of the loop above (no per-candidate label/summary/path_prefixes work
    # yet) -- the true significant-community count BEFORE `limit` truncates it (finding B).
    stats.total_before_limit = len(candidates)
    if limit is not None:
        candidates = candidates[:limit]

    # Pass 1: resolve which candidates actually survive (the "claimed" skip and within-run
    # slug dedup are inherently order-dependent — `used_slugs` and
    # `store.find_domain_by_slug` both need sequential state), building each survivor's
    # slug/title/summary WITHOUT yet deriving path_prefixes.
    used_slugs: set[str] = set()
    pending: list[dict] = []
    for community_id, anchorable, god_node_id in candidates:
        if community_id in claimed:
            stats.already_claimed += 1
            continue

        label = labels.get(community_id)
        if label is not None and _label_names_other_community(label, community_id, reader):
            label = None
        # Belt-and-braces: community_labels() already drops blank-after-strip values, but
        # a god node's name comes straight from graph.json with no such guard — a blank
        # name must fall through to the next fallback rather than reach Domain.title,
        # which raises (mid-run, after any earlier proposals in the batch already
        # committed) on blank text.
        god_name = None
        if god_node_id is not None:
            god_node = reader.get_node(god_node_id)
            if god_node is not None and god_node.name.strip():
                god_name = god_node.name

        # Redact FIRST: title/slug both derive from this one clean value, never the raw
        # label/god-node name — the engine can embed doc/rationale text a human typed, and
        # slugify() alone doesn't scrub secrets, only unsafe slug characters (probe:
        # "Auth (api_key=sk-live-...)" leaked the raw key into the repo-committed slug,
        # and thus the `domain:<slug>` entity name, when slugify ran on unredacted text).
        base_name = label or god_name or f"community-{community_id}"
        title, _ = redact(base_name)
        base_slug = slugify(title) or f"community-{community_id}"
        slug = _dedupe_slug(base_slug, used_slugs)

        existing = store.find_domain_by_slug(slug)
        if existing is not None:
            stats.already_claimed += 1
            continue

        raw_summary = label if label else _member_digest(anchorable, god_node_id, reader)
        summary, _ = redact(raw_summary)

        pending.append(
            {
                "community_id": community_id,
                "anchorable": anchorable,
                "god_node_id": god_node_id,
                "slug": slug,
                "title": title,
                "summary": summary,
                "has_label": label is not None,
            }
        )

    # Pass 2: derive path_prefixes for every survivor, then dedupe an identical non-empty
    # set derived by >= 2 of them down to the single largest-membership candidate (live
    # finding — see `_dedupe_identical_prefix_sets`).
    derived_prefixes = _dedupe_identical_prefix_sets(
        pending, reader, dir_breadth, total_communities
    )

    # Pass 3: assemble the final DomainCandidate per survivor, same order as `pending`.
    out: list[DomainCandidate] = []
    for p in pending:
        out.append(
            DomainCandidate(
                community_id=p["community_id"],
                suggested_slug=p["slug"],
                suggested_title=p["title"],
                summary=p["summary"],
                path_prefixes=derived_prefixes[p["community_id"]],
                member_count=len(p["anchorable"]),
                top_members=_top_member_names(p["anchorable"], reader),
                top_file=_top_file(p["anchorable"], p["god_node_id"], reader),
                has_label=p["has_label"],
                anchor=_god_node_descriptor(p["god_node_id"], reader),
            )
        )

    stats.total = len(out)
    return out, stats


def bootstrap_domains(
    store: Store,
    reader: GraphifyReader,
    *,
    min_members: int = 5,
    paths: list[str] | None = None,
    dry_run: bool = False,
    limit: int | None = None,
    ratify_policy: RatifyPolicy = RatifyPolicy.MANUAL,
) -> BootstrapReport:
    """Propose one ``Domain`` draft per significant community (§4.1, bootstrap path).

    Selection is entirely delegated to ``collect_domain_candidates`` (see its docstring
    for the full threshold/label/path_prefixes/redaction/idempotency rules); this
    function's own job is only to turn each survivor into a ``Domain`` write (or, on
    ``dry_run=True``, a listing entry) — the writing half of the bootstrap pipeline.

    ``ratify_policy`` (default ``RatifyPolicy.MANUAL``): the resolved
    ``SIDEGRAPH_RATIFY_POLICY`` value (design D1), sampled once by the CLI shell
    immediately before this call and passed down unchanged. Under ``AUTO_ALL`` only (D3:
    domains are never eligible under ``auto-low-risk``), each proposed domain's own
    post-write block stamps it ``auto:auto-all`` via the shared ``_auto_ratify`` helper
    when ``_domain_anchored`` and the rest of the D3 gate hold, then immediately resolves
    its membership through the same ``sync.activate_accepted_domain`` helper the human
    MCP/CLI ratify paths use — a bootstrap domain is not exempt from the one
    ``ratify_domains`` gate, it can just clear it without a human tap. The once-per-batch
    TOC rebuild happens after the whole loop, only when at least one domain was actually
    auto-ratified.
    # see design/superpowers/specs/2026-09-11-auto-ratification-policy-design.md D1/D2/D3
    """
    graph_version = reader.graph_version()
    candidates, stats = collect_domain_candidates(
        store, reader, min_members=min_members, paths=paths, limit=limit
    )

    report = BootstrapReport(
        below_threshold=stats.below_threshold,
        filtered=stats.filtered,
        skipped_existing=stats.already_claimed,
        total_before_limit=stats.total_before_limit,
    )

    for c in candidates:
        # D7.4: computed regardless of dry_run -- a real run's proposals get the same
        # lint a dry run's listing would have shown (the CLI prints report.warnings after
        # either path; see cli._domains_bootstrap).
        prefix_warnings = _lint_domain_path_prefixes(c.path_prefixes, reader, store)
        if prefix_warnings:
            report.warnings.append({"slug": c.suggested_slug, "warnings": prefix_warnings})

        if dry_run:
            report.proposed += 1
            report.dry_run.append(
                {
                    "community_id": c.community_id,
                    "slug": c.suggested_slug,
                    "title": c.suggested_title,
                    "summary": c.summary,
                    "path_prefixes": c.path_prefixes,
                    "warnings": prefix_warnings,
                }
            )
            continue

        domain = Domain(
            slug=c.suggested_slug,
            title=c.suggested_title,
            summary=c.summary,
            communities=[c.community_id],
            path_prefixes=c.path_prefixes,
            provenance=Provenance(
                source="bootstrap",
                author="sidegraph-domains",
                graph_version=graph_version,
            ),
        )
        store.add_domain(domain)
        report.proposed += 1

        # Auto-ratify (design D2/D3), auto-all only -- reuses `prefix_warnings`, already
        # computed above for the dry-run listing too, rather than a second lint call.
        if not dry_run and ratify_policy == RatifyPolicy.AUTO_ALL:
            domain_anchored = _domain_anchored(
                reader, prefix_warnings, domain.seed_anchors, domain.path_prefixes
            )
            signal = AutoEligibility(
                kind="domain",
                live_tier12=0,
                ambiguous_or_orphan_only=True,
                pipeline_clean=True,
                has_provenance=True,
                domain_anchored=domain_anchored,
                has_supersedes=False,
            )
            if auto_ratify_eligible(signal, ratify_policy):
                outcome = _auto_ratify(store, domain.domain_id, "domain", ratify_policy)
                auto_ratify_error = outcome.error
                if outcome.ratified_by is not None:
                    report.auto_ratified += 1
                    # Ruling R (design D2/D6 checkpoint-2 fix): the domain transition
                    # already committed by this point, so an activation failure must
                    # report and continue, never abort the batch -- mirrors
                    # capture.py's own domain auto-block; sync.activate_accepted_domain
                    # itself stays unchanged (its human MCP/CLI callers carry the
                    # identical unprotected stale-marker write).
                    try:
                        activation = activate_accepted_domain(domain, store, reader)
                    except Exception as e:
                        auto_ratify_error = f"activation: {e}"
                    else:
                        # `resolved` and `overbroad` are independent fields on
                        # `sync.DomainActivation` (checked separately, not elif'd) --
                        # rev 12 erratum: a claim-cap rejection used to report a clean
                        # success here, while the human MCP/CLI wrappers (server.py,
                        # cli.py) rendered their own "path rule too broad" sentence for
                        # the identical outcome -- mirrors capture.py's own domain
                        # auto-block (same sentence, same literal
                        # `sidegraph:heal-anchors` trigger phrase).
                        if not activation.resolved:
                            auto_ratify_error = f"activation: {activation.error}"
                        if activation.overbroad is not None:
                            prefixes = ", ".join(repr(p) for p in domain.path_prefixes)
                            auto_ratify_error = (
                                f"activation: path rule too broad: {prefixes} match "
                                f"{activation.overbroad['matched']}/"
                                f"{activation.overbroad['total']} communities — not "
                                "applied; seed_anchors, if any, still applied"
                            )
                if auto_ratify_error is not None:
                    report.auto_ratify_failures.append(f"{domain.domain_id}: {auto_ratify_error}")

    if report.auto_ratified > 0:
        store.set_meta(TOC_CACHE_KEY, json.dumps(build_toc(store)))

    return report
