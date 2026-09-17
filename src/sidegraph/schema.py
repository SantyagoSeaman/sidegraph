"""Core record types for the owned decision store.

Six record types live in an owned, append-only store (see
``docs/concepts/data-model.md``) — the founding three:

- :class:`Entity`      — internal identity + engine mapping (created lazily).
- :class:`Decision`    — the ADR / lesson / gotcha, with temporal validity.
- :class:`AnchorBinding` — a tiered, status-tracked link between a record and an entity.

Plus :class:`Initiative`, a flat container that groups decisions (Tier-0 anchoring),
:class:`Domain`, an owned, append-only named abstraction with a paired abstract entity
(see ``docs/concepts/mind-model.md``), and :class:`Fact`, a compact, falsifiable piece of
non-derivable knowledge that informed one or more decisions (see
``design/superpowers/specs/2026-07-10-facts-layer-design.md``).

These are **never** written into the engine's regenerated ``graph.json``; the store is a
separate, repo-committed sidecar. The invariants documented on each model are enforced on
write by :mod:`sidegraph.store`.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, BaseModel, Field, field_validator, model_validator
from ulid import ULID

# The store format is a public contract from day one. Migration tooling is deferred beyond
# one forward step; this field is not (see CLAUDE.md invariant #3). Bumped 0.2.0 -> 0.3.0 for
# the mind-model layer (Domain record, Decision.layer, AnchorBinding.relation — see
# docs/concepts/mind-model.md). Bumped 0.3.0 -> 0.4.0 for the git-native store rewrite
# (file-per-record canonical layout + derived local index — see
# docs/reference/store-format.md): a repo-committed single SQLite file cannot be merged by
# git, and sync was rewriting the committed db on every graph rebuild. Legacy 0.2.x/0.3.x
# single-file stores are migrated forward on ``Store.__init__`` (eagerly, not deferred to
# first write — see store.py's ``_migrate_legacy``).
#
# NOT bumped for ``Domain.seed_anchors`` (durable domain membership, §2a amendment —
# design/superpowers/specs/2026-07-08-domain-onboarding-design.md): purely additive,
# defaulted (``[]``) field on an existing record. An existing ``domains/<id>.json`` with no
# ``seed_anchors`` key loads unchanged (Pydantic fills the default); a new file WITH the key
# is read fine by nothing-but-old code paths too, since nothing reads it except the new
# sync logic added alongside it. No migration semantics change either direction — bumping
# would only make ``Store._refresh_freshness``'s exact-match ``schema_version`` gate hard-
# reject every teammate's already-fresh local ``index.db`` on next open, for zero actual
# incompatibility.
#
# Bumped 0.5.0 -> 0.6.0 for derived community bindings (see
# design/superpowers/specs/2026-07-10-derived-community-bindings-design.md): community
# labels are snapshot labels, not identities, so ``community:*`` abstract entities and any
# Tier-1 binding pointing at one are now fully DERIVED — index-only, never written to a
# canonical file (neither at capture nor at sync/repointing time). A 0.5.0 store's
# canonical files may still contain community entities/bindings written by the old code;
# they load into the index unchanged (tolerant reload) and decay lazily off a record's
# committed file on that record's next legitimate (non-community) canonical rewrite.
SCHEMA_VERSION = "0.6.0"

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def _new_id() -> str:
    """Mint a fresh, sortable, immutable ULID string."""
    return str(ULID())


def canonicalize(name: str) -> str:
    """Normalize an entity name for matching: lowercase, strip a leading ``.`` and any
    call decoration. Graphify renders functions ``f()`` and methods ``.m()``; decisions
    reference bare symbols. Canonicalizing both sides makes name matching robust.
    """
    return name.strip().lstrip(".").split("(", 1)[0].strip().lower()


_SLUGIFY_STRIP_RE = re.compile(r"[^a-z0-9-]")
_SLUGIFY_COLLAPSE_RE = re.compile(r"-+")


def slugify(text: str) -> str:
    """Kebab-case a free-form label for a ``tag:<slug>`` (or manually-typed domain slug)
    abstract entity: lowercase, spaces -> ``-``, drop everything outside ``[a-z0-9-]``,
    collapse repeats, strip leading/trailing ``-``. May return ``""`` for input with no
    ``[a-z0-9]`` content — callers must skip an empty slug rather than mint a nameless tag
    (see ``docs/concepts/mind-model.md``).
    """
    s = text.strip().lower().replace(" ", "-")
    s = _SLUGIFY_STRIP_RE.sub("", s)
    return _SLUGIFY_COLLAPSE_RE.sub("-", s).strip("-")


def matches_path_prefix(file_path: str, prefix: str) -> bool:
    """Boundary-safe ``Domain.path_prefixes`` match: true iff ``file_path`` equals
    ``prefix`` exactly, or sits under it as a true subdirectory (``prefix`` + ``"/"``).

    A bare ``file_path.startswith(prefix)`` false-positives on sibling paths that merely
    share a text prefix — ``"payments"`` would loosely match ``"payments_v2/x.py"``, a
    different top-level directory entirely. ``prefix`` may itself already carry a trailing
    ``"/"`` (both authoring paths allow it); that's normalized away before comparing. The
    one place this rule is encoded — shared by ``domains._matches_any_prefix`` (bootstrap
    ``--paths`` filtering) and ``sync._recompute_domain_communities`` (post-rebuild
    refresh) so the two authoring/refresh paths can never drift apart on the boundary rule.
    """
    prefix = prefix.rstrip("/")
    return file_path == prefix or file_path.startswith(prefix + "/")


class EntityKind(StrEnum):
    CONCRETE = "concrete"  # a real code entity (module, class, function)
    ABSTRACT = "abstract"  # a semantic/domain concept (Tier-0)


class DecisionKind(StrEnum):
    ADR = "adr"
    LESSON = "lesson"
    CONSTRAINT = "constraint"
    GOTCHA = "gotcha"


class DecisionStatus(StrEnum):
    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    SUPERSEDED = "superseded"
    REJECTED = "rejected"
    DEPRECATED = "deprecated"


class Scope(StrEnum):
    REPO = "repo"
    MODULE = "module"
    GLOBAL = "global"


class DomainStatus(StrEnum):
    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    SUPERSEDED = "superseded"
    DROPPED = "dropped"


# Shared with capture.py's per-anchor override (AnchorDraft.relation) so the two stay in
# lockstep by construction rather than by two hand-synced Literal lists.
Relation = Literal["creates", "modifies", "affects", "deprecates", "considered"]


class Descriptor(BaseModel):
    """What :meth:`GraphifyReader.resolve` needs to find an entity's current node.

    Graphify 0.9.6 emits no signatures/qual-names/enclosing-module, so identity is
    ``name`` + optional ``file_path`` (see ``docs/integrations/graphify.md``).
    """

    name: str
    file_path: str | None = None

    @field_validator("file_path", mode="after")
    @classmethod
    def _blank_path_is_no_path(cls, v: str | None) -> str | None:
        """``""`` (and whitespace) means "no path", and is folded here so it can never be
        STORED as one.

        Two sources produce it: Graphify emits ``source_file: ""`` for a path-less node, and
        an agent filling an MCP anchor writes ``"file_path": ""`` for "I don't know" as
        readily as it omits the key. Without this fold the two spellings diverge exactly
        where it hurts — a ``""`` descriptor minted before the real carrier exists makes
        ``_adopt_path_carrying_entity`` see two "paths" (``""`` and the real one), read that
        as ambiguity, and go back to minting the path-less twin this schema change exists to
        prevent (external review, round 3, reproduced). Folding at the boundary keeps the
        one-descriptor-one-entity rule true by construction instead of by three agreeing
        predicates."""
        if v is None:
            return None
        return v.strip() or None


class Entity(BaseModel):
    """Internal identity + engine mapping. Minted once, ``entity_id`` never changes.

    Created **lazily** — only when a decision first references it — to stay bounded.
    ``last_seen_node_id`` maps our durable id onto Graphify's shifting, path-derived node
    id and is refreshed on each engine rebuild; ``last_seen_community`` records the observed
    Leiden community as of that same rebuild — communities are snapshot labels Leiden
    renumbers every rebuild, so this baseline is what sync uses to re-point Tier-1 bindings
    (see ``docs/concepts/data-model.md``).
    """

    entity_id: str = Field(default_factory=_new_id)
    canonical_name: str
    kind: EntityKind = EntityKind.CONCRETE
    descriptor: Descriptor | None = None
    last_seen_node_id: str | None = None
    last_seen_graph_version: str | None = None
    last_seen_community: str | None = None


class Provenance(BaseModel):
    """Where a decision came from. Always present on write."""

    source: str  # commit | pr | manual | agent
    ref: str | None = None
    author: str | None = None
    session_id: str | None = None
    graph_version: str | None = None
    # Best-effort `git rev-parse HEAD` at capture time (design/superpowers/specs/
    # 2026-07-30-staleness-machinery-design.md, D1) — additive, no SCHEMA_VERSION bump
    # (same `seed_anchors` precedent this module documents above): purely additive,
    # defaulted, old files load, new files tolerated. Stamped on the propose path
    # (capture._propose_one) and the supersede_decision path (D6); NEVER backfilled —
    # provenance is immutable after write (verify.DECISION_MUTABLE_FIELDS), so a pre-wave
    # record simply lacks this field and is skipped, not corrected, by doctor's
    # `code-drift` check (D5).
    commit: str | None = None


class Domain(BaseModel):
    """The owned abstraction: a named area of the system with WHY-IT-EXISTS prose.

    Paired with an abstract :class:`Entity` (``canonical_name=f"domain:{slug}"``) minted
    at acceptance, so :class:`AnchorBinding` machinery works unchanged (Tier-1 decisions
    bind to the domain entity). Append-only, like :class:`Decision`: editing a summary is
    a new ``Domain`` row with ``supersedes`` set; the slug (and its paired entity) is
    stable across that revision (see
    ``docs/concepts/mind-model.md#domain-lifecycle``).
    """

    domain_id: str = Field(default_factory=_new_id)
    slug: str
    title: str
    summary: str  # the WHY-IT-EXISTS prose — 1-3 sentences, REQUIRED
    parent_id: str | None = None  # optional self-reference; acyclic, enforced on write
    communities: list[str] = Field(default_factory=list)  # last-seen engine community ids
    path_prefixes: list[str] = Field(default_factory=list)  # stabilizer/bootstrap rule
    # COMMITTED, durable authoring intent (§2a amendment): "this domain includes the
    # communities these entities currently live in." Same Descriptor (name + optional
    # file_path) a decision's own anchor resolves via GraphifyReader.resolve — unlike
    # `communities` (a volatile, last-seen snapshot popped from the canonical file by
    # `_domain_canonical_payload`), `seed_anchors` survives a fresh clone/rebuild because it
    # anchors to durable entities, not renumberable Leiden ids. `sync._recompute_domain_
    # communities` resolves each anchor to its CURRENT community every pass (see sync.py).
    seed_anchors: list[Descriptor] = Field(default_factory=list)

    # Ratifier stamp (2026-08-04 proposal-lifecycle design D4): additive-defaulted — no
    # SCHEMA_VERSION bump (the `capture_commit`/`seed_anchors` precedent; old files load,
    # old readers tolerate). Set ONLY by the store's ratify transitions; None means a
    # pre-change record or unavailable identity — never guessed. Git history remains the
    # tamper-evidence layer; this puts the who/when in the record itself (practitioner
    # panel, security reviewer objection 7).
    ratified_by: str | None = None
    ratified_at: AwareDatetime | None = None
    status: DomainStatus = DomainStatus.PROPOSED
    supersedes: str | None = None  # id of the Domain this one replaces
    provenance: Provenance

    @field_validator("slug")
    @classmethod
    def _check_slug(cls, v: str) -> str:
        if not _SLUG_RE.match(v):
            raise ValueError(f"slug must be kebab-case (^[a-z0-9][a-z0-9-]*$): {v!r}")
        return v

    @field_validator("title")
    @classmethod
    def _check_title_non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("title must be non-empty")
        return v

    @field_validator("summary")
    @classmethod
    def _check_summary_non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("summary must be non-empty")
        return v


class Decision(BaseModel):
    """The memory: an ADR, lesson, constraint, or gotcha with temporal validity.

    Append-only; never hard-deleted. A reversal sets ``valid_to`` on the old record and
    creates a new one whose ``supersedes`` points at it. ``rejected`` captures what was tried
    — what was tried and abandoned — which is exactly what retrieval surfaces first.
    """

    id: str = Field(default_factory=_new_id)
    title: str
    kind: DecisionKind
    status: DecisionStatus = DecisionStatus.PROPOSED

    context: str
    choice: str
    rejected: str | None = None  # tried/considered and abandoned, and why
    consequences: str | None = None

    valid_from: AwareDatetime
    valid_to: AwareDatetime | None = None
    supersedes: str | None = None  # id of the Decision this one replaces

    scope: Scope = Scope.REPO
    layer: Literal["business", "technical"] | None = None  # filter axis; obvious at capture
    provenance: Provenance

    # Ratifier stamp (2026-08-04 proposal-lifecycle design D4): additive-defaulted — no
    # SCHEMA_VERSION bump (the `capture_commit`/`seed_anchors` precedent; old files load,
    # old readers tolerate). Set ONLY by the store's ratify transitions; None means a
    # pre-change record or unavailable identity — never guessed. Git history remains the
    # tamper-evidence layer; this puts the who/when in the record itself (practitioner
    # panel, security reviewer objection 7).
    ratified_by: str | None = None
    ratified_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def _check_validity_window(self) -> Decision:
        if self.valid_to is not None and self.valid_to < self.valid_from:
            raise ValueError("valid_to must be >= valid_from")
        return self


class Fact(BaseModel):
    """A compact, non-derivable piece of knowledge that informed decisions.

    Only facts the code graph cannot derive belong here: empirics (benchmarks, observed
    behavior), external constraints (API limits, library capabilities), trial-learned
    knowledge. "The code does X" is the engine's job — never store it. Falsifiable:
    reversal is supersession, never deletion.
    # see design/superpowers/specs/2026-07-10-facts-layer-design.md
    """

    id: str = Field(default_factory=_new_id)
    statement: str  # the fact itself, 1-2 sentences — hard-compact
    source: str  # epistemics: how we know ("benchmark run 2026-07-09", "httpx docs")
    supports: list[str] = Field(default_factory=list)  # decision ids this fact informed

    # Ratifier stamp (2026-08-04 proposal-lifecycle design D4): additive-defaulted — no
    # SCHEMA_VERSION bump (the `capture_commit`/`seed_anchors` precedent; old files load,
    # old readers tolerate). Set ONLY by the store's ratify transitions; None means a
    # pre-change record or unavailable identity — never guessed. Git history remains the
    # tamper-evidence layer; this puts the who/when in the record itself (practitioner
    # panel, security reviewer objection 7).
    ratified_by: str | None = None
    ratified_at: AwareDatetime | None = None
    status: DecisionStatus = DecisionStatus.PROPOSED  # DEPRECATED unused for facts

    valid_from: AwareDatetime
    valid_to: AwareDatetime | None = None
    supersedes: str | None = None  # id of the Fact this one replaces

    provenance: Provenance

    @model_validator(mode="after")
    def _check_validity_window(self) -> Fact:
        if self.valid_to is not None and self.valid_to < self.valid_from:
            raise ValueError("valid_to must be >= valid_from")
        return self

    @model_validator(mode="after")
    def _check_nonempty(self) -> Fact:
        if not self.statement.strip():
            raise ValueError("statement must be non-empty")
        if not self.source.strip():
            raise ValueError("source must be non-empty")
        return self


class AnchorBinding(BaseModel):
    """A tiered, status-tracked link from a record (decision or fact) to an entity.

    One decision may have several bindings (multi-anchor). Retrieval resolves the best
    ``live`` binding; degradation is recorded here (status flips), never by deleting.
    """

    record_id: str
    entity_id: str
    tier: int = Field(ge=0, le=2)  # 0 semantic/abstract | 1 group | 2 leaf
    weight: float = Field(default=1.0, ge=0.0, le=1.0)
    status: str = "live"  # live | degraded | orphaned
    # additive; defaulted to avoid capture friction — rendered only when non-default
    relation: Relation = "affects"


class Initiative(BaseModel):
    """A flat container grouping related decisions (the Tier-0 owning anchor)."""

    id: str = Field(default_factory=_new_id)
    name: str
    description: str | None = None
    tags: list[str] = Field(default_factory=list)
