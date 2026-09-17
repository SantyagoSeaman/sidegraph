"""Deterministic capture pipeline (Stage 5) — no LLM key.

The generative work (distilling a session into What/Why/Where/Learned drafts, judging
significance) happens in the agent's session turn; this module is the pure write pipeline:
redact -> validate -> package -> anchor -> dedup -> write as ``status=proposed``. The human
ratifies via the store's ``ratify``/``drop`` (see docs/guides/capturing-decisions.md).
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

from .anchoring import entity_summaries, orphan_reason, resolve_and_bind
from .config import TELEMETRY_SESSION_KEY
from .engine.reader import GraphifyReader
from .retrieval import TOC_CACHE_KEY, build_toc
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
    canonicalize,
    matches_path_prefix,
    slugify,
)
from .store import _TERMINAL_DECISION_STATUSES, Store
from .sync import activate_accepted_domain

# v1 secret patterns. Redaction runs FIRST: its output is the only text that proceeds to
# validation/storage — mandatory for a repo-committed store.
_SECRET_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"(?i)bearer\s+[a-z0-9._~+/=-]{20,}"),
    re.compile(
        r"(?i)\b(?:[a-z0-9]+[_-])*(?:api[_-]?key|token|secret|password)"
        r"(?:[_-][a-z0-9]+)*\s*[=:]\s*\S+"
    ),
    # 2026-08-04 seeded-leak eval additions (design/testing/2026-08-04-redaction-seeded-leak.md):
    # the five adjacent classes the eval showed leaking that admit low-false-positive
    # patterns. Emails and bare hex tokens remain DOCUMENTED misses — both are too
    # collision-prone for pattern redaction (an email is not necessarily a secret; 40+ hex
    # collides with commit SHAs/digests) and are covered by the defense-in-depth guidance
    # (run the org's secret scanner over the store path in CI).
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),  # JWT
    re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://[^/\s:@]+):([^@\s]+)@"),  # URL credential
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),  # Google API key
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),  # sk- style API key (OpenAI et al.)
]

# Candidate PAN spans: 16 digits, optionally space/dash-grouped. Regex alone would eat
# ULIDs' neighbors and invoice numbers — a match must ALSO pass Luhn before redaction
# (check-what-you-redact, not pattern-and-pray). Applied by `redact` after the pattern
# passes above.
_PAN_CANDIDATE = re.compile(r"\b(?:\d[ -]?){15}\d\b")


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def redact(text: str) -> tuple[str, int]:
    """Scrub secrets from text. Returns (clean_text, replacement_count)."""
    total = 0
    for pattern in _SECRET_PATTERNS:
        text, n = pattern.subn("[REDACTED]", text)
        total += n

    def _pan_sub(m: re.Match[str]) -> str:
        nonlocal total
        digits = re.sub(r"[ -]", "", m.group(0))
        if len(digits) == 16 and _luhn_ok(digits):
            total += 1
            return "[REDACTED]"
        return m.group(0)

    text = _PAN_CANDIDATE.sub(_pan_sub, text)
    return text, total


class RatifyPolicy(StrEnum):
    """Auto-ratification policy for a propose/import/bootstrap batch (design D1).

    ``MANUAL`` (default): introduces NO new transition and leaves every existing write
    path's semantics exactly as they are — it is not a claim that every write lands
    ``proposed``. Some paths already land ``accepted`` under their own, pre-existing
    flags (e.g. ``importer.py``'s ``propose=False``, or the unrelated legacy
    ``SIDEGRAPH_AUTO_ACCEPT=on`` knob); that behavior is untouched and out of this
    feature's scope. ``AUTO_LOW_RISK``/``AUTO_ALL`` gate which shapes may self-ratify at
    write time. Resolved ONCE per CLI/MCP invocation by the outer shell from
    ``SIDEGRAPH_RATIFY_POLICY`` (via
    ``parse_ratify_policy``) and threaded down as a keyword, the same way ``auto_accept``
    already is — this module never reads the environment itself (see the module
    docstring).
    # see design/superpowers/specs/2026-09-11-auto-ratification-policy-design.md D1
    """

    MANUAL = "manual"
    AUTO_LOW_RISK = "auto-low-risk"
    AUTO_ALL = "auto-all"


def parse_ratify_policy(raw: str | None) -> RatifyPolicy:
    """Pure parse of ``SIDEGRAPH_RATIFY_POLICY`` into a ``RatifyPolicy`` (design D1).

    Unknown, empty, whitespace-only, or ``None`` input fails safe to
    ``RatifyPolicy.MANUAL`` — the ``_proposal_window_days`` precedent (``retrieval.py``):
    a typo in a regulated deployment must not silently open the auto-ratify gate.
    Surrounding whitespace is stripped before matching, same as that precedent
    (``retrieval.py:555``'s ``raw.strip()``) — a trailing space off a ``.env`` line must
    not silently downgrade an autonomous deployment's policy to ``manual``. Pure by
    construction: this function does not read the environment itself — the outer CLI/MCP
    shell reads the ``SIDEGRAPH_RATIFY_POLICY`` variable once and passes the raw value in
    as ``raw``.
    # see design/superpowers/specs/2026-09-11-auto-ratification-policy-design.md D1
    """
    if raw is None:
        return RatifyPolicy.MANUAL
    try:
        return RatifyPolicy(raw.strip())
    except ValueError:
        return RatifyPolicy.MANUAL


# Kinds `auto-low-risk` admits by shape (D3 gate 1): `gotcha`/`lesson` decisions and
# standalone facts. `adr`/`constraint` and domains are `auto-all`-only.
_LOW_RISK_KINDS: frozenset[str] = frozenset({"gotcha", "lesson", "fact"})
# The decision kinds `auto-all` additionally admits over `_LOW_RISK_KINDS` (still
# subject to the remaining gates). Domains are handled separately below — they have
# their own anchor gate (`domain_anchored`, never `live_tier12`) and are never eligible
# under `auto-low-risk` at all, so they do not belong in either kind set.
_AUTO_ALL_EXTRA_KINDS: frozenset[str] = frozenset({"adr", "constraint"})


@dataclass(frozen=True)
class AutoEligibility:
    """The ONE input shape every auto-ratify caller adapts its own write result to
    (design D3). ``ProposeResult``/``ProposeFactResult``/``ProposeDomainResult`` (and
    the importer/doc-import equivalents) each compute one of these per write and hand
    it to ``auto_ratify_eligible`` — the adaptation happens once per call site, never
    inside this predicate, and never the reverse (this shape never grows a
    caller-specific field).

    ``live_tier12`` is ``store.bindings_for_record(id)`` filtered to
    ``status == "live"`` and ``tier in (1, 2)``, computed AFTER the anchor step —
    never arithmetic over ``anchors_skipped``/``anchors_orphaned`` (record
    ``01KYD1WCNMGPH9964EQ98ZZFHW``: an empty ``anchors_skipped`` means "nothing was
    ambiguous", not "everything resolved"). It is always ``0`` for domains, which carry
    no ``AnchorBinding`` at propose time — ``domain_anchored`` is their anchor signal
    instead (reader present + lint clean + seed/prefix present).
    # see design/superpowers/specs/2026-09-11-auto-ratification-policy-design.md D3
    """

    kind: str
    live_tier12: int
    ambiguous_or_orphan_only: bool
    pipeline_clean: bool
    has_provenance: bool
    domain_anchored: bool
    has_supersedes: bool


def auto_ratify_eligible(signal: AutoEligibility, policy: RatifyPolicy) -> bool:
    """Conjunctive, deterministic auto-ratify gate — no LLM in the write path (design D3).

    Four gates, ALL of which must hold, checked cheapest first:

    1. Policy allows the draft's shape. ``auto-low-risk`` admits only ``lesson``/
       ``gotcha`` decisions and standalone facts, and additionally requires
       ``has_supersedes`` to be ``False`` for those kinds — a supersession's blast
       radius is the PREDECESSOR's kind, so a low-risk draft must never close a
       human-ratified record with nobody looking. ``auto-all`` additionally admits
       ``adr``/``constraint`` and domains, and admits a superseding draft by shape (D2
       still defers the predecessor's close to a successful ``Store.ratify``). Domains
       are eligible under ``auto-all`` only, never ``auto-low-risk``, regardless of
       anchor state.
    2. Anchors. Decisions/facts need ``live_tier12 >= 1`` AND
       ``not ambiguous_or_orphan_only``. Both conditions restate the SAME fact — "no
       live Tier-1/2 binding to stand on" — rather than gating on two independent
       signals: a record with ANY live Tier-1/2 binding is anchored, whatever happened
       to its OTHER anchors (an ambiguous anchor is a MISSING binding, not a wrong one,
       so it never disqualifies a binding that did resolve). The
       ``not ambiguous_or_orphan_only`` half of the conjunction is defence in depth
       against an adapter that computes ``live_tier12`` incorrectly, not an independent
       product rule — ``live_tier12 >= 1`` together with ``ambiguous_or_orphan_only``
       true is a CONTRADICTORY input (the flag promises zero live bindings; a positive
       count says otherwise), and the gate rejects that combination defensively rather
       than trusting either field alone. Domains carry no ``AnchorBinding`` at propose
       time, so they use ``domain_anchored`` instead and are never gated on
       ``live_tier12``/``ambiguous_or_orphan_only``.
    3. Pipeline verdict. The write path's own result reports a clean, non-dry-run write
       (``pipeline_clean``).
    4. Provenance. Already a write invariant — restated here so this gate never
       weakens it.

    Total over the declared field types: for any ``signal`` whose fields match
    ``AutoEligibility``'s own annotations, every branch is an equality/membership check
    or an ``int`` comparison, so this never raises — an unrecognized ``kind``, a
    negative ``live_tier12``, or a contradictory flag combination all resolve to
    ``False`` rather than an exception. This is not a defensive guarantee against a
    wrongly-typed field (``live_tier12`` is populated by a ``len(...)`` at every real
    call site, never user input): a non-``int`` ``live_tier12`` can raise on the
    ``< 1`` comparison, and this function does not guard against that.
    # see design/superpowers/specs/2026-09-11-auto-ratification-policy-design.md D3
    """
    if policy not in (RatifyPolicy.AUTO_LOW_RISK, RatifyPolicy.AUTO_ALL):
        return False

    if signal.kind == "domain":
        if policy != RatifyPolicy.AUTO_ALL:
            return False
        if not signal.domain_anchored:
            return False
        if not signal.pipeline_clean:
            return False
        return bool(signal.has_provenance)

    if policy == RatifyPolicy.AUTO_LOW_RISK:
        if signal.kind not in _LOW_RISK_KINDS:
            return False
        if signal.has_supersedes:
            return False
    else:  # RatifyPolicy.AUTO_ALL
        if signal.kind not in _LOW_RISK_KINDS and signal.kind not in _AUTO_ALL_EXTRA_KINDS:
            return False

    if signal.live_tier12 < 1:
        return False
    if signal.ambiguous_or_orphan_only:
        return False

    if not signal.pipeline_clean:
        return False

    return bool(signal.has_provenance)


@dataclass(frozen=True)
class AutoRatifyOutcome:
    """Normalized result of one :func:`_auto_ratify` transition attempt (design D2/D6).

    ``ratified_by`` is the ``"auto:<policy>"`` stamp when the transition fired and
    succeeded, ``None`` otherwise. ``error`` is ``None`` on success; the ``ValueError`` text
    for a failed ``store.ratify``/``ratify_fact`` call, or the ``"error: ..."`` outcome
    ``store.ratify_domains`` RETURNS (never raises) for a failed domain transition.
    ``cascaded_fact_ids`` carries the ids of every fact ``store.ratify``'s own cascade just
    accepted alongside the decision — empty for standalone facts and domains, which never
    cascade. The caller uses these ids to stamp the matching nested ``ProposeFactResult``
    objects, so the public result and the canonical store agree (T11/T14).
    """

    ratified_by: str | None
    error: str | None
    cascaded_fact_ids: tuple[str, ...] = ()


def _auto_ratify(
    store: Store,
    record_id: str,
    kind: str,
    policy: RatifyPolicy,
) -> AutoRatifyOutcome:
    """Call exactly one of the three C-2 transitions with the ``"auto:<policy>"`` stamp —
    the SOLE place in this codebase that builds that stamp string (design D2). Routes on
    ``kind`` (an :class:`AutoEligibility`.kind value, already computed by the
    caller for the eligibility check): ``"fact"`` -> :meth:`Store.ratify_fact`, ``"domain"``
    -> :meth:`Store.ratify_domains`, anything else (a decision kind — ``gotcha``/``lesson``/
    ``adr``/``constraint``) -> :meth:`Store.ratify`.

    Decision route / cascade guard (design D2 checkpoint-2 fix, Ruling Q, tightened by
    Ruling T after checkpoint-2's own fix round 1): this function builds the
    ``cascade_guard`` it hands to ``Store.ratify`` ITSELF, from ``store`` and ``policy`` —
    it is not an accepted parameter here. Every caller on the decision route (today only
    ``_propose_one``; a future importer/doc-import decision auto-block per plan Task 5)
    therefore gets the guard automatically and CANNOT omit it or forward the wrong one —
    closing exactly the gap an opt-in parameter would leave open (Task 5's own dispatch says
    nothing about a guard). The guard re-runs :func:`_fact_cascade_eligible` on the cascade
    set it is handed, reading each fact's bindings fresh via ``store`` at call time.

    Catches ``Exception`` — never ``BaseException``, so ``KeyboardInterrupt``/``SystemExit``
    still propagate out of an autonomous batch — normalizing both the anticipated
    ``ValueError`` race from ``ratify``/``ratify_fact`` (the proposal vanished or was already
    ratified between the eligibility check and this call, or the cascade guard refused) and
    any other unexpected failure from a returned flow into ``AutoRatifyOutcome.error``.
    ``ratify_domains`` never raises for a bad id; it RETURNS an ``"error: ..."`` outcome
    string instead, which this function detects and normalizes the same way, so every caller
    checks exactly one field regardless of which transition it called.
    # see design/superpowers/specs/2026-09-11-auto-ratification-policy-design.md D2/D6
    """
    stamp = f"auto:{policy.value}"
    try:
        if kind == "fact":
            store.ratify_fact(record_id, actor=stamp)
            return AutoRatifyOutcome(ratified_by=stamp, error=None)
        if kind == "domain":
            outcome = store.ratify_domains(accept=[record_id], actor=stamp)[record_id]
            if outcome.startswith("error"):
                return AutoRatifyOutcome(ratified_by=None, error=outcome)
            return AutoRatifyOutcome(ratified_by=stamp, error=None)

        def _cascade_guard(facts: Sequence[Fact]) -> bool:
            return all(_fact_cascade_eligible(store, fact, policy) for fact in facts)

        _decision, cascaded = store.ratify(record_id, actor=stamp, cascade_guard=_cascade_guard)
        return AutoRatifyOutcome(
            ratified_by=stamp, error=None, cascaded_fact_ids=tuple(f.id for f in cascaded)
        )
    except Exception as e:
        return AutoRatifyOutcome(ratified_by=None, error=str(e))


def _anchor_signal(store: Store, record_id: str) -> tuple[int, bool]:
    """``(live_tier12, ambiguous_or_orphan_only)`` for ``record_id`` — design D3 gate 2,
    computed from the SAME source for both fields (never ``anchors_skipped``/
    ``anchors_orphaned`` arithmetic, record ``01KYD1WCNMGPH9964EQ98ZZFHW``):
    ``store.bindings_for_record(record_id)`` filtered to ``status == "live"`` and
    ``tier in (1, 2)``, evaluated after the anchor step. The two fields describe the SAME
    fact from two angles by construction — ``ambiguous_or_orphan_only`` can never disagree
    with ``live_tier12`` because it is derived directly from it, never from a separate walk
    over skipped/orphaned buckets.
    """
    live_tier12 = sum(
        1 for b in store.bindings_for_record(record_id) if b.status == "live" and b.tier in (1, 2)
    )
    return live_tier12, live_tier12 == 0


def _cascade_set(store: Store, decision_id: str) -> list[Fact]:
    """The exact fact set :meth:`Store.ratify`'s own cascade will flip for ``decision_id``
    (mirrors the ``for fact in self.iter_proposed_facts(): if decision_id in fact.supports``
    cascade loop at the end of :meth:`Store.ratify`) — re-queried from the store rather than
    trusted from this call's own ``fact_results``, so any fact that would ride the same
    cascade is checked, not just this call's own successful writes. In practice this IS
    exactly the decision's step-7 attached facts (design D2): ``Store.add_fact`` rejects a
    ``supports`` id that does not exist, and ``decision_id`` is minted in this same call,
    so no OTHER fact can already be in this set within a single process (spec rev 8, D2).
    """
    return [f for f in store.iter_proposed_facts() if decision_id in f.supports]


def _fact_cascade_eligible(store: Store, fact: Fact, policy: RatifyPolicy) -> bool:
    """Per-fact half of D2's cascade rule: does ``fact`` pass the fact half of gates 2-4
    plus ``fact.supersedes is None`` under ``auto-low-risk`` — kind/policy admission
    inherited from the owning decision by reusing :func:`auto_ratify_eligible` itself with
    ``kind="fact"`` and the SAME ``policy`` the decision was gated on, rather than
    re-implementing the gate ladder here.

    Shared by two callers that read anchors through the same :func:`_anchor_signal`, just at
    different times (design D2 checkpoint-2 fix, Ruling Q/T): :func:`_cascade_eligible` (the
    cheap pre-check, run BEFORE the store lock is taken) and the ``cascade_guard`` closure
    :func:`_auto_ratify` builds ITSELF and hands to ``Store.ratify`` (the authoritative
    re-check, re-run on a freshly re-queried cascade set AFTER the lock is held — never on
    values computed before it, which is exactly the race the guard exists to close).
    """
    live_tier12, ambiguous_or_orphan_only = _anchor_signal(store, fact.id)
    signal = AutoEligibility(
        kind="fact",
        live_tier12=live_tier12,
        ambiguous_or_orphan_only=ambiguous_or_orphan_only,
        pipeline_clean=True,
        has_provenance=True,
        domain_anchored=False,
        has_supersedes=fact.supersedes is not None,
    )
    return auto_ratify_eligible(signal, policy)


def _cascade_eligible(store: Store, decision_id: str, policy: RatifyPolicy) -> bool:
    """D2's cascade rule: the decision's own auto-block is skipped unless EVERY fact in the
    cascade set (:func:`_cascade_set`) is :func:`_fact_cascade_eligible`. An empty cascade
    set (no attached facts, or none that landed ``written``) is vacuously eligible — there
    is nothing to block on.

    This is the cheap PRE-check only (design D2 checkpoint-2 fix, Ruling Q): it runs before
    ``Store.ratify`` takes its write lock, so a fact landing in the cascade set between this
    call and the transition would not be seen here. :func:`_auto_ratify` itself builds the
    authoritative ``cascade_guard`` closure it hands to ``Store.ratify`` (Ruling T — the
    guard is built inside :func:`_auto_ratify` itself and is never a parameter, so no
    caller can supply or omit it), which re-runs :func:`_fact_cascade_eligible` on a
    freshly re-queried set INSIDE the lock — that guard, not this function, decides.
    """
    return all(
        _fact_cascade_eligible(store, fact, policy) for fact in _cascade_set(store, decision_id)
    )


def _domain_anchored(
    reader: GraphifyReader | None,
    lint_warnings: list[str],
    seed_anchors: list[Descriptor],
    path_prefixes: list[str],
) -> bool:
    """D3's domain anchor gate: the reader must be present (domains carry no
    ``AnchorBinding`` at propose time, so this is the ONLY anchor signal they have — the
    ``if reader is not None:`` guard mirrors ``_lint_domain_path_prefixes``'s own dead-prefix
    check, so a reader-absent domain is never eligible) AND the pipeline's own lint is clean
    (``lint_warnings == []``) AND (at least one ``seed_anchor`` resolves to a live node OR
    ``path_prefixes`` is non-empty). "Non-empty prefixes" alone is NOT eligibility — that is
    exactly what ``lint_warnings`` screens for (record ``01KYSFQ5D45SBQZ238NP8Z4YWH``).
    """
    if reader is None or lint_warnings:
        return False
    if path_prefixes:
        return True
    return any(reader.resolve(anchor).status == "resolved" for anchor in seed_anchors)


class AnchorDraft(Descriptor):
    """A Where anchor with an optional per-anchor relation override (see
    ``AnchorBinding.relation``; omitted/None means the store default "affects")."""

    relation: Relation | None = None


class DraftFact(BaseModel):
    """A compact non-derivable-knowledge draft — attached to a ``DraftDecision`` (its
    ``facts`` list) or proposed standalone via ``propose_facts``.
    # see design/superpowers/specs/2026-07-10-facts-layer-design.md"""

    statement: str
    source: str
    anchors: list[AnchorDraft] = Field(default_factory=list)
    supports: list[str] = Field(default_factory=list)


class DraftDecision(BaseModel):
    """The What/Why/Where/Learned distillation form, mapped onto the Decision schema."""

    title: str  # What (short)
    kind: DecisionKind  # adr | lesson | constraint | gotcha
    context: str  # Why — incl. constraints that emerged
    choice: str  # what was decided
    rejected: str | None = None  # what was tried and abandoned
    consequences: str | None = None  # Learned / trade-offs accepted
    anchors: list[AnchorDraft] = Field(default_factory=list)  # Where
    initiative: str | None = None  # else derived from the git branch
    supersedes: str | None = None
    tags: list[str] = Field(default_factory=list)  # free text; slugified below
    facts: list[DraftFact] = Field(default_factory=list)  # attached, non-derivable knowledge

    @field_validator("tags", mode="before")
    @classmethod
    def _coerce_string_tags(cls, v: object) -> object:
        """Liberal-input: agents pass tags as a bare comma-separated string on the first
        try — split it instead of failing the draft (mirrors server._coerce_tags)."""
        if isinstance(v, str):
            return [part.strip() for part in v.split(",") if part.strip()]
        return v

    layer: Literal["business", "technical"] | None = None


class ProposeFactResult(BaseModel):
    status: str  # "written" | "deduped" | "rejected"
    fact_id: str | None = None
    reason: str | None = None
    redactions: int = 0
    # Same shape/rationale as ProposeResult.anchors_skipped (Gate-5 finding S3).
    anchors_skipped: list[dict] = Field(default_factory=list)
    # Same shape/rationale as ProposeResult.anchors_orphaned.
    anchors_orphaned: list[dict] = Field(default_factory=list)
    # Auto-ratification policy (design D2/D6) — additive/defaulted, same precedent as
    # `anchors_skipped`/`anchors_orphaned`: `None`/`None` under `manual` or when this fact
    # was ineligible. `ratified_by` carries the `"auto:<policy>"` stamp when the transition
    # fired and succeeded — set directly for a standalone fact's own auto-block, or by the
    # OWNING decision's cascade when this is a nested attached-fact result (the public
    # result must not say `ratified_by=None` for a fact whose canonical row the cascade just
    # accepted, T11/T14). `auto_ratify_error` carries the normalized failure reason when an
    # attempt failed; `None` when nothing was attempted or the attempt succeeded.
    ratified_by: str | None = None
    auto_ratify_error: str | None = None


class ProposeResult(BaseModel):
    status: str  # "written" | "deduped" | "rejected"
    decision_id: str | None = None
    reason: str | None = None
    redactions: int = 0
    # Gate-5 finding S3: anchors resolve_and_bind couldn't pin to one leaf (name matched
    # more than one graph node) -- [{"name", "reason": "ambiguous", "candidates"}, ...],
    # candidates capped at 5. Empty when every anchor resolved cleanly, there were no
    # anchors, or no graph reader was present (nothing to be ambiguous against).
    anchors_skipped: list[dict] = Field(default_factory=list)
    # Entity summaries [{"entity_id", "canonical_name", "tier": 2}, ...] for anchors that
    # resolved to NOTHING. The leaf is still written -- orphaned, deliberately, never
    # dropped -- but it is dead on arrival: retrieval, drill_down and the PreToolUse nudge
    # all skip orphaned bindings, and no Tier-1 community fallback is created either, so the
    # record has no delivery path through that anchor at all. Reported because the agent
    # writing the draft is the only one who can still fix the name, and it used to get back
    # a result indistinguishable from success. Same bucket vocabulary as add_anchors'.
    anchors_orphaned: list[dict] = Field(default_factory=list)
    # Attached facts (draft.facts) run through the same pipeline right after this decision
    # writes — see _propose_one's facts loop. Defaulted to [] so every existing caller that
    # builds/compares a bare ProposeResult (no facts) is unaffected.
    facts: list[ProposeFactResult] = Field(default_factory=list)
    # Live decisions already reachable via the draft's own anchors (design D2) — up to 3,
    # deduped, newest first (see _live_neighbors); each {"id", "kind", "title", "status"}.
    # A `deduped` result carries the existing duplicate record itself as its one neighbor
    # (the general walk never runs on that early-return path); additive/defaulted so every
    # existing caller comparing a bare ProposeResult is unaffected.
    neighbors: list[dict] = Field(default_factory=list)
    # Auto-ratification policy (design D2/D6) — same additive/defaulted contract as
    # ProposeFactResult's own pair; see that class's docstring. Set by _propose_one's
    # post-write auto block, AFTER the attached-facts loop above.
    ratified_by: str | None = None
    auto_ratify_error: str | None = None


_TEXT_FIELDS = ("title", "context", "choice", "rejected", "consequences")


class DraftDomain(BaseModel):
    """An agent-recognized Domain draft (§4.2, agent in-session path) — mirrors
    DraftDecision's role for the decision side of capture."""

    slug: str
    title: str
    summary: str
    parent_slug: str | None = None
    path_prefixes: list[str] = Field(default_factory=list)
    # Durable, committed authoring intent (§2a amendment — replaces an earlier raw
    # `communities` id seed, which review proved does NOT survive a fresh clone or a
    # `graphify update` rebuild: community ids are volatile, Leiden renumbers them every
    # build). Lets an agent-curated merge (several communities, no clean shared path) name
    # its membership durably, by anchoring to entities instead of ids — exactly how a
    # decision's own anchors resolve. May be given alongside path_prefixes, in place of it,
    # or omitted (inert until a human adds a rule later). `propose_domains`/`ratify` resolve
    # this to `communities` (see sync._recompute_domain_communities /
    # sync.refresh_domain_communities_now) — never populated directly from the draft.
    seed_anchors: list[Descriptor] = Field(default_factory=list)


class ProposeDomainResult(BaseModel):
    status: str  # "proposed" | "skipped" | "rejected"
    domain_id: str | None = None
    reason: str | None = None
    redactions: int = 0
    # design D7.4 (staleness-machinery wave, E8 gate checklist): deterministic lint
    # warnings on this domain's path_prefixes -- see _lint_domain_path_prefixes. Advisory
    # only, never blocks the write; under `auto-all` any warning keeps the draft proposed;
    # empty when path_prefixes is empty or every prefix passes both checks.
    # Additive/defaulted so every existing caller comparing a bare
    # ProposeDomainResult is unaffected.
    warnings: list[str] = Field(default_factory=list)
    # Auto-ratification policy (design D2/D6) — same additive/defaulted contract as
    # ProposeResult's own pair; see that class's docstring. Domains are eligible only under
    # `auto-all` (never `auto-low-risk`). `auto_ratify_error` is prefixed `"activation: "`
    # when the domain's own transition succeeded but `sync.activate_accepted_domain`
    # couldn't resolve its membership — accepted-but-unhealed stays visible, never silent.
    ratified_by: str | None = None
    auto_ratify_error: str | None = None


def _derive_initiative() -> str | None:
    """feature/aaa branch -> 'feature-aaa'. Best-effort; None on main/master or any error."""
    try:
        out = subprocess.run(["git", "branch", "--show-current"], capture_output=True, text=True)
        branch = out.stdout.strip()
        if out.returncode != 0 or not branch or branch in ("main", "master"):
            return None
        return branch.replace("/", "-")
    except (OSError, subprocess.SubprocessError):
        return None


def _capture_commit(store: Store) -> str | None:
    """``git rev-parse HEAD`` -- best-effort capture-time HEAD stamp (design D1).

    Runs with ``cwd=store.path`` — the STORE's own directory, never the ambient process
    cwd (CORRECTION-2, code review) — so the commit always names the repo the store
    actually lives in, regardless of where the calling process happens to be running
    from. This matters concretely for D5's doctor ``code-drift`` check: it diffs a
    stamped commit against ``HEAD`` in the repo it resolves from the STORE's directory
    (``verify._find_repo_root``), so a commit stamped against the wrong repo would
    silently degrade every batch to the git-unavailable note. ``None`` on any failure (no
    repo containing the store, git missing, non-zero exit), never raising. Also used by
    ``server._supersede_decision_impl`` (D6) so both write paths that ever construct a
    fresh ``Provenance`` stamp ``commit`` identically."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=store.path, capture_output=True, text=True
        )
        commit = out.stdout.strip()
        if out.returncode != 0 or not commit:
            return None
        return commit
    except (OSError, subprocess.SubprocessError):
        return None


# Freshness window for the D7.3 session_id fallback (below) — a marker older than this is
# worse than no attribution at all (a long-abandoned session's id leaking onto an
# unrelated later capture).
_SESSION_ID_FALLBACK_MAX_AGE_SECONDS = 24 * 60 * 60


def _session_id_fallback(store: Store) -> str | None:
    """Best-effort ``provenance.session_id`` source when the caller passed none (design
    D7.3 — E8 measured ``author=None session=None`` on Stop-channel captures). Reads
    ``TELEMETRY_SESSION_KEY`` (``config.py``), the same ``<session_id>|<iso-timestamp>``
    marker ``host.hooks.session_start`` now stamps UNCONDITIONALLY (D7.3 generalized that
    write off its old ``telemetry_enabled()`` gate specifically so this fallback always has
    something to read) — used only when fresh (< :data:`_SESSION_ID_FALLBACK_MAX_AGE_SECONDS`
    old); a stale marker from a long-abandoned session is worse than no attribution at all.
    Never raises: an absent key, an unparseable value, a naive/malformed timestamp, or a
    ``store.get_meta`` failure itself (NIT-5, code review — the read wasn't actually
    guarded before, despite this docstring's own claim) all return ``None``, same as
    passing no session_id at all — this is provenance, not security, and capture must
    never fail the caller over a best-effort attribution guess.
    """
    try:
        raw = store.get_meta(TELEMETRY_SESSION_KEY)
    except Exception:
        return None
    if raw is None:
        return None
    session_id, _, stamp = raw.partition("|")
    if not session_id:
        return None
    try:
        written = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if written.tzinfo is None:
        return None  # schema requires aware timestamps; a naive one can't be compared safely
    age = (datetime.now(UTC) - written).total_seconds()
    if age >= _SESSION_ID_FALLBACK_MAX_AGE_SECONDS:
        return None
    return session_id


def _bind_orphaned(
    record_id: str,
    anchor: Descriptor,
    store: Store,
    relation: Relation | None = None,
) -> None:
    """No engine available: record the anchor as an orphaned leaf (never silently dropped)."""
    # Atomic get-or-create (design D3): the old find_entity + upsert_entity longhand upserted
    # unconditionally even on a hit, which is a no-op (identity unchanged -> an index-only
    # rewrite, see Store.upsert_entity's docstring) -- unlike anchoring.py's Tier-2 leaf, this
    # call site has no engine mapping to refresh, so the collapse loses nothing.
    entity = store.get_or_create_entity(anchor)
    # Explicit kwarg rather than a splatted dict (see anchoring.resolve_and_bind for the
    # same pattern/rationale) — AnchorBinding's own default is "affects".
    store.add_binding(
        AnchorBinding(
            record_id=record_id,
            entity_id=entity.entity_id,
            tier=2,
            status="orphaned",
            relation=relation if relation is not None else "affects",
        )
    )


# Neighbors cap (design D2, "Thresholds" — chosen, not measured): how many live decisions
# `_live_neighbors` ever returns, after dedup-by-id and ULID-descending ordering.
_NEIGHBORS_CAP = 3


def _live_neighbors(draft: DraftDecision, store: Store) -> list[Decision]:
    """Live decisions already sharing one of ``draft``'s own ``anchors`` entities (design
    D2) — the walk ``_is_duplicate`` always performed, factored out so a new reporting step
    (``ProposeResult.neighbors``, below) can reuse it instead of re-walking the store.

    Walks ``draft.anchors`` specifically — never the record's eventual post-pipeline
    entity set (initiative/tag bindings, attached only after the write) — for two
    load-bearing reasons, which is also why every call site (this function's own callers)
    must run BEFORE ``store.add_decision``: (1) self-inclusion only becomes real AFTER
    ANCHORING (step 5, below) has bound the record to its own anchor entity — not merely
    after the write itself (correction, code review: the record carries no binding at
    all yet at that earlier boundary, so ``valid_decisions_for_entity`` can't yet return
    it) — but once anchoring HAS run, that same call (excludes only SUPERSEDED/REJECTED)
    would return the record being written as its own neighbor; (2) initiative/tag entities
    are shared by half the store and would flood the result with noise instead of the
    on-topic Tier-2 leaf(s) the draft actually names.

    Deduped by ``d.id`` BEFORE the cap — concatenating each anchor's own walk would
    otherwise return one decision once PER shared anchor, and capping first could deliver
    several copies of the same record and zero breadth (the E9 probe this closes: a real
    pair shared 5 entities, of which the signal was one Tier-2 leaf). Ordered by id
    descending (ULIDs sort by creation time, newest first; ``valid_decisions_for_entity``
    itself is an unsorted binding scan) and capped at :data:`_NEIGHBORS_CAP`.

    Used by both ``_is_duplicate`` (the exact kind+title dedup check) and ``_propose_one``'s
    reporting step, so the two can never disagree about which live records the draft's own
    anchors currently reach.
    """
    by_id: dict[str, Decision] = {}
    for anchor in draft.anchors:
        entity = store.resolve_descriptor(anchor.name, anchor.file_path)
        if entity is None:
            continue
        for d in store.valid_decisions_for_entity(entity.entity_id):
            by_id[d.id] = d
    return sorted(by_id.values(), key=lambda d: d.id, reverse=True)[:_NEIGHBORS_CAP]


def _neighbor_dict(d: Decision) -> dict:
    """Render one live neighbor for ``ProposeResult.neighbors`` (design D2): just enough for
    an agent to decide whether to ``supersede_decision`` it — the full record is a
    ``get_task_context``/``drill_down`` call away."""
    return {"id": d.id, "kind": d.kind.value, "title": d.title, "status": d.status.value}


def _is_duplicate(draft: DraftDecision, store: Store) -> str | None:
    """Deterministic minimal dedup: same kind + canonical title among the draft's live
    neighbors (:func:`_live_neighbors` — shared-anchor walk, design D2)."""
    target = canonicalize(draft.title)
    for d in _live_neighbors(draft, store):
        if d.kind == draft.kind and canonicalize(d.title) == target:
            return d.id
    return None


def _is_duplicate_fact(
    statement: str, anchors: list[AnchorDraft], supports: list[str], store: Store
) -> str | None:
    """Deterministic minimal dedup, mirrors ``_is_duplicate``: same canonicalized statement
    on a shared SUPPORTED decision, or on a shared anchor entity -> deduped. Conservative:
    when unsure, write (the human drops at ratify).

    The anchor-entity check deliberately walks ``bindings_for_entity`` + ``get_fact``
    (matching ``facts_for_decision``'s own ACCEPTED/PROPOSED status filter) rather than
    ``valid_facts_for_entity`` — that helper skips ``orphaned`` bindings (correct for
    *retrieval*, which cares whether a binding currently resolves in the graph), but every
    anchor bound without a reader (``_bind_orphaned``, the common capture-time path) is
    ``orphaned`` by construction. Entity identity (same canonical_name + file_path, per
    ``find_entity``) is independent of the binding's graph-resolution health, so dedup must
    not miss an orphaned duplicate.
    """
    canon = canonicalize(statement)
    for sid in supports:
        for f in store.facts_for_decision(sid):
            if canonicalize(f.statement) == canon:
                return f.id
    for anchor in anchors:
        entity = store.resolve_descriptor(anchor.name, anchor.file_path)
        if entity is None:
            continue
        for b in store.bindings_for_entity(entity.entity_id):
            existing = store.get_fact(b.record_id)
            if existing is None or existing.status not in (
                DecisionStatus.ACCEPTED,
                DecisionStatus.PROPOSED,
            ):
                continue
            if canonicalize(existing.statement) == canon:
                return existing.id
    return None


def _resolves_to_live_decision(store: Store, supports: Sequence[str]) -> bool:
    """True iff at least one id in ``supports`` names a decision that is still LIVE
    (``accepted``/``proposed``) — design D8's write-side half of the same "live" reachability
    rule doctor's tightened ``dangling-record`` check applies on read (D4). A fact this gate
    accepts is never something that check would go on to flag as unreachable.

    Shared by this module's own anchorless-fact gate (below) and ``server.py``'s
    ``_add_fact_impl``/``supersede_fact`` (the same rule, at the two human-asked entry
    points) — three write paths, one definition of "live", so they can never drift apart.
    """
    return any(
        (d := store.get_decision(sid)) is not None
        # Derived from the terminal set, never spelled out as (ACCEPTED, PROPOSED): doctor's
        # half of this rule computes "live" the same way (``doctor._TERMINAL_DECISION_STATUS_
        # VALUES``), and D4 requires the two complements to match EXACTLY. A hard-coded pair
        # here would silently drift the moment a sixth DecisionStatus is added — the write
        # gate would keep accepting what the read check had started flagging, which is the
        # contradiction this whole wave exists to remove (branch review, Low-1).
        and d.status not in _TERMINAL_DECISION_STATUSES
        for sid in supports
    )


def _supports_a_still_proposed_decision(store: Store, supports: Sequence[str]) -> bool:
    """True iff at least one id in ``supports`` names a decision that is still
    ``proposed`` — the store's own nested-evidence definition
    (``Store.pending_ratification_counts``): such a fact is covered by that decision's
    own verdict (its accept cascade, at ``_propose_one``'s post-write block after the
    facts loop, or its drop cascade, at ``Store.drop``) and must never certify itself
    ahead of it, even when the fact arrives later, through its own ``propose_facts``
    call (design D2 rev 12 erratum). A missing id cannot occur here — ``Store.add_fact``
    rejects unknown ``supports`` ids at write time.
    """
    return any(
        (d := store.get_decision(sid)) is not None and d.status == DecisionStatus.PROPOSED
        for sid in supports
    )


def _propose_fact_one(
    raw: object,
    store: Store,
    reader: GraphifyReader | None,
    session_id: str | None,
    author: str | None,
    graph_version: str | None,
    *,
    attached_to: str | None = None,
    inherited_anchors: list[AnchorDraft] | None = None,
    auto_accept: bool = False,
    ratify_policy: RatifyPolicy = RatifyPolicy.MANUAL,
) -> ProposeFactResult:
    """One Fact draft through the deterministic pipeline — mirrors ``_propose_one``'s
    stages exactly (see that function; each stage below cites its counterpart there).

    ``attached_to``/``inherited_anchors`` are set only when called from a
    ``DraftDecision.facts`` entry (see ``_propose_one``'s facts loop, below): ``supports``
    always includes the decision being written (plus the draft's own ``supports``), and a
    fact with no anchors of its own inherits the DECISION's anchors — but bindings are
    always minted on the FACT's own id, so it survives the decision independently. A
    standalone ``propose_facts`` draft passes neither, and must supply its own anchor or
    supports id (see the reachability check below).

    ``auto_accept`` (default ``False``): when true (``SIDEGRAPH_AUTO_ACCEPT=on``, threaded
    down from ``propose_facts``/``propose``'s own ``auto_accept`` — see
    design/superpowers/specs/2026-07-10-ratification-ux-and-mcp-gaps-design.md), the fact
    lands ``status=ACCEPTED`` instead of ``PROPOSED``, skipping the ratification queue.
    Provenance still stamps ``source="agent"`` regardless — history never lies about who
    authored a record, only whether a human reviewed it.

    ``ratify_policy`` (default ``RatifyPolicy.MANUAL``): the post-write auto-ratify block
    below only ever runs when ``attached_to is None`` — design D2/D3's standalone-only
    restriction. An ATTACHED fact is never independently eligible (gate 1's shape test is
    the OWNING DECISION's, not this fact's); it rides that decision's own cascade instead
    (see ``_propose_one``'s post-write block), which is why this parameter is accepted here
    unconditionally but only ever consulted inside the ``attached_to is None`` branch.
    # see design/superpowers/specs/2026-09-11-auto-ratification-policy-design.md D2/D3
    """
    # I1 (R1 improvement wave §1): the D7.3 session_id fallback (see
    # _session_id_fallback's docstring), applied here exactly as _propose_one applies it
    # to its own decision. _propose_one already resolves session_id BEFORE calling this
    # function for an ATTACHED fact, so this is a no-op there (never overwrites a real
    # id); a STANDALONE propose_facts draft never goes through _propose_one at all, so
    # without this line here it landed session_id=None even with a fresh marker present
    # (measured defect: both R1 facts came through this exact path).
    if session_id is None:
        session_id = _session_id_fallback(store)

    # 1. Validate.
    try:
        draft = DraftFact.model_validate(raw)
    except ValidationError as e:
        return ProposeFactResult(status="rejected", reason=f"invalid draft: {e}")

    # 2. Redact first — same gate as _propose_one's text fields; the scrubbed text is the
    # only text that proceeds to reachability/dedup/storage.
    statement, n1 = redact(draft.statement)
    source, n2 = redact(draft.source)
    redactions = n1 + n2

    # 3. Reachability: own anchors override inherited ones entirely (own present -> only
    # own, matching DraftDecision anchors being the sole anchor list, never additive with
    # anything). A standalone fact with no anchor is rejected unless its `supports` names at
    # least one LIVE decision (design D8) — existence alone used to be enough, but a fact
    # whose only supports id has since gone terminal is born flagged the moment it lands
    # (doctor's tightened dangling-record check, D4/D6). "Anchorless" is defined by the
    # REQUEST here (this function's own `anchors`, not a resolved outcome) — D8's residual.
    anchors = draft.anchors or (inherited_anchors or [])
    supports = ([attached_to] if attached_to else []) + list(draft.supports)
    if not anchors and not _resolves_to_live_decision(store, supports):
        reason = (
            "standalone fact needs at least one anchor or a supports id"
            if not supports
            else (
                "standalone fact's supports resolve only to a superseded/rejected/"
                "deprecated decision — add an anchor, or re-point supports at the successor"
            )
        )
        return ProposeFactResult(status="rejected", reason=reason)

    # 4. Dedup (conservative: when unsure, write; the human drops at ratify).
    dup_id = _is_duplicate_fact(statement, anchors, supports, store)
    if dup_id is not None:
        return ProposeFactResult(
            status="deduped", fact_id=dup_id, reason="duplicate", redactions=redactions
        )

    # 5. Package + validate + write (append-only; supersede closes the predecessor).
    try:
        fact = Fact(
            statement=statement,
            source=source,
            supports=supports,
            status=DecisionStatus.ACCEPTED if auto_accept else DecisionStatus.PROPOSED,
            valid_from=datetime.now(UTC),
            provenance=Provenance(
                source="agent",
                author=author,
                session_id=session_id,
                graph_version=graph_version,
                # П0 (git-bindings design, Blocker 1): the same best-effort HEAD stamp
                # _propose_one already applies to its own decision -- mechanically the I1
                # twin (commit 4b1c927), closing the fact/decision mirror so П1 rule (a)
                # and П2's provenance join can ever match a fact.
                commit=_capture_commit(store),
            ),
        )
        store.add_fact(fact)
    except (ValidationError, ValueError) as e:
        return ProposeFactResult(status="rejected", reason=str(e), redactions=redactions)

    # 6. Anchor — identical ladder to _propose_one's (see that function's step 5): anchors
    # carry their own optional relation override; strip it before handing the bare
    # Descriptor to resolve_and_bind/_bind_orphaned (which take the override as a separate
    # argument). resolve_and_bind's return already carries the reader.resolve() outcome, so
    # an ambiguous anchor is reported back (Gate-5 finding S3) without a second resolve()
    # call. Bindings are minted on fact.id (never on attached_to) — this is what lets an
    # attached fact survive its decision independently.
    anchors_skipped: list[dict] = []
    anchors_orphaned: list[dict] = []
    if reader is not None:
        for anchor in anchors:
            result = resolve_and_bind(
                fact.id,
                Descriptor(name=anchor.name, file_path=anchor.file_path),
                reader,
                store,
                relation=anchor.relation,
            )
            if result.status == "ambiguous":
                anchors_skipped.append(
                    {
                        "name": anchor.name,
                        "reason": "ambiguous",
                        "candidates": result.candidates[:5],
                    }
                )
            elif result.status == "unresolved":
                reason = orphan_reason(
                    Descriptor(name=anchor.name, file_path=anchor.file_path), reader
                )
                anchors_orphaned.extend(
                    {**s, "reason": reason}
                    for s in entity_summaries(store, [b for b in result if b.tier == 2])
                )
    else:
        for anchor in anchors:
            before = {b.entity_id for b in store.bindings_for_record(fact.id)}
            _bind_orphaned(
                fact.id,
                Descriptor(name=anchor.name, file_path=anchor.file_path),
                store,
                relation=anchor.relation,
            )
            anchors_orphaned.extend(
                {**s, "reason": "no-graph"}
                for s in entity_summaries(
                    store,
                    [
                        b
                        for b in store.bindings_for_record(fact.id)
                        if b.tier == 2 and b.entity_id not in before
                    ],
                )
            )

    # 7. Auto-ratify (design D2/D3) — standalone facts ONLY: a fact is a standalone
    # candidate iff `attached_to is None` (not created inside a `DraftDecision.facts`
    # entry — an attached fact is never independently eligible, it rides its decision's
    # cascade instead, see _propose_one's own post-write block, after its facts loop) AND
    # none of its `supports` ids resolves to a still-proposed decision (rev 12 erratum:
    # such a fact rides THAT decision's verdict too, even when it arrives later through
    # its own propose_facts call — see _supports_a_still_proposed_decision). Skipped
    # outright when `auto_accept` is true — the fact already landed ACCEPTED above, and the
    # hook runs ONLY on writes that landed proposed (D2).
    ratified_by: str | None = None
    auto_ratify_error: str | None = None
    if (
        attached_to is None
        and not auto_accept
        and ratify_policy != RatifyPolicy.MANUAL
        and not _supports_a_still_proposed_decision(store, fact.supports)
    ):
        live_tier12, ambiguous_or_orphan_only = _anchor_signal(store, fact.id)
        signal = AutoEligibility(
            kind="fact",
            live_tier12=live_tier12,
            ambiguous_or_orphan_only=ambiguous_or_orphan_only,
            pipeline_clean=True,
            has_provenance=True,
            domain_anchored=False,
            has_supersedes=fact.supersedes is not None,
        )
        if auto_ratify_eligible(signal, ratify_policy):
            outcome = _auto_ratify(store, fact.id, "fact", ratify_policy)
            ratified_by = outcome.ratified_by
            auto_ratify_error = outcome.error

    return ProposeFactResult(
        status="written",
        fact_id=fact.id,
        redactions=redactions,
        anchors_skipped=anchors_skipped,
        anchors_orphaned=anchors_orphaned,
        ratified_by=ratified_by,
        auto_ratify_error=auto_ratify_error,
    )


def _propose_one(
    raw: object,
    store: Store,
    reader: GraphifyReader | None,
    session_id: str | None,
    author: str | None,
    ref: str | None,
    graph_version: str | None,
    *,
    auto_accept: bool = False,
    ratify_policy: RatifyPolicy = RatifyPolicy.MANUAL,
) -> ProposeResult:
    """One DraftDecision through the deterministic pipeline (see module docstring for the
    overall redact -> validate -> package -> anchor -> dedup -> write stages).

    ``auto_accept`` (default ``False``): when true (``SIDEGRAPH_AUTO_ACCEPT=on`` — see
    design/superpowers/specs/2026-07-10-ratification-ux-and-mcp-gaps-design.md), the
    decision AND every attached fact (draft.facts, via the facts loop below) land
    ``status=ACCEPTED`` instead of ``PROPOSED``, bypassing the ratification queue.
    Provenance still stamps ``source="agent"`` regardless.

    ``ratify_policy`` (default ``RatifyPolicy.MANUAL``): when it allows and D3's gates pass
    (including the cascade rule over this call's own attached facts — see the post-write
    block after step 7, below), the decision is auto-ratified through the same
    ``Store.ratify`` a human tap calls, stamped ``"auto:<policy>"``. Ignored entirely when
    ``auto_accept`` is true — that write already landed ACCEPTED, and the hook runs ONLY on
    writes that landed ``proposed`` (design D2).
    """
    try:
        draft = DraftDecision.model_validate(raw)
    except ValidationError as e:
        return ProposeResult(status="rejected", reason=f"invalid draft: {e}")

    # D7.3: best-effort session_id fallback when the caller passed none (E8 measured
    # author=None session=None on Stop-channel captures) — see _session_id_fallback's
    # docstring. Resolved once, up front, so this decision's own Provenance AND any
    # attached facts (draft.facts, via the facts loop below, which already threads
    # `session_id` straight through) land the same attribution, rather than the decision
    # getting one value and its own evidence another.
    if session_id is None:
        session_id = _session_id_fallback(store)

    # 1. Redact first — the scrubbed text is the only text that proceeds. Tags are free
    # text until slugified, so they go through the same gate.
    redactions = 0
    clean: dict[str, str | None] = {}
    for name in _TEXT_FIELDS:
        value = getattr(draft, name)
        if value is None:
            clean[name] = None
        else:
            scrubbed, n = redact(value)
            clean[name] = scrubbed
            redactions += n

    tag_slugs: list[str] = []
    for tag in draft.tags:
        scrubbed, n = redact(tag)
        redactions += n
        slug = slugify(scrubbed)
        # A tag whose entire text WAS the secret redacts down to "[REDACTED]" -> slugifies
        # to exactly "redacted" -- skip it (never mint a nameless `tag:redacted` entity
        # that leaks nothing but also means nothing; see M2 review fold-in). A tag that
        # merely CONTAINS "redacted" alongside real words (e.g. "redacted-config") still
        # slugifies to something else and is kept.
        if slug and slug != "redacted":
            tag_slugs.append(slug)

    # 2. Dedup (conservative: when unsure, write; the human drops at ratify).
    dup_id = _is_duplicate(draft, store)
    if dup_id is not None:
        dup = store.get_decision(dup_id)
        return ProposeResult(
            status="deduped",
            reason=f"duplicate of {dup_id}",
            redactions=redactions,
            # The dedup early-return means the general neighbors walk below never runs on
            # this path (design D2) — yet "agent re-proposed the same title" is exactly
            # where supersession advice matters most, so the dup itself rides as the one
            # neighbor. `dup` is always found here in practice (it was just looked up by
            # this same store), but the None guard keeps this path never-fail regardless.
            neighbors=[_neighbor_dict(dup)] if dup is not None else [],
        )

    # 2b. Neighbors (design D2): live decisions the draft's own anchors already reach,
    # reported back so the agent can consider superseding one instead of leaving a fresh,
    # possibly-contradicting record alongside it. MUST run here — pre-write, immediately
    # after the dedup check, before store.add_decision below — see _live_neighbors's
    # docstring for why a post-write walk would be wrong twice over.
    neighbors = [_neighbor_dict(d) for d in _live_neighbors(draft, store)]

    # 3-4. Package + validate + write (append-only; supersede closes the predecessor).
    initiative = draft.initiative or _derive_initiative()
    # title/context/choice are required (non-Optional) on DraftDecision, and the loop above
    # only maps None -> None — they can only be None here if they went in None, which the
    # schema forbids. Only rejected/consequences are genuinely optional.
    assert clean["title"] is not None
    assert clean["context"] is not None
    assert clean["choice"] is not None
    try:
        decision = Decision(
            title=clean["title"],
            kind=draft.kind,
            status=DecisionStatus.ACCEPTED if auto_accept else DecisionStatus.PROPOSED,
            context=clean["context"],
            choice=clean["choice"],
            rejected=clean["rejected"],
            consequences=clean["consequences"],
            layer=draft.layer,
            valid_from=datetime.now(UTC),
            supersedes=draft.supersedes,
            provenance=Provenance(
                source="agent",
                ref=ref,
                author=author,
                session_id=session_id,
                graph_version=graph_version,
                commit=_capture_commit(store),
            ),
        )
        # Deferred supersession for an auto-policy proposal (design D2/T12): closing the
        # predecessor eagerly (the default) would flip it to superseded BEFORE the
        # post-write eligibility block below can reject this successor -- leaving a
        # rejected/ineligible draft with an already-closed predecessor and no accepted
        # successor to show for it. `manual` and legacy `auto_accept=True` keep today's
        # eager close (a manual write is reviewed by a human either way; auto_accept lands
        # this decision ACCEPTED directly, so there is no gap to defer across). Only when
        # `ratify_policy` allows auto AND this write is landing `proposed` AND the draft
        # actually names a predecessor is the close deferred to a successful `Store.ratify`
        # (its own existing deferred-supersession branch performs it, `auto-all` and
        # `auto-low-risk` alike — `auto-all` admits the shape and can still fail a later
        # gate or lose the transition race, `auto-low-risk` almost always fails gate 1 for
        # a superseding draft, see D3 — either way the close must wait for that verdict).
        defer_close = (
            not auto_accept
            and ratify_policy != RatifyPolicy.MANUAL
            and draft.supersedes is not None
        )
        store.add_decision(decision, close_predecessor=not defer_close)
    except (ValidationError, ValueError) as e:
        return ProposeResult(status="rejected", reason=str(e), redactions=redactions)

    # 5. Anchor (Stage-3 path with the engine; orphaned leaves without it). Anchors carry
    # their own optional relation override; strip it before handing the bare Descriptor to
    # resolve_and_bind/_bind_orphaned (which take the override as a separate argument).
    # resolve_and_bind's return already carries the reader.resolve() outcome (see
    # anchoring.AnchorResolution), so an ambiguous anchor is reported back (Gate-5 finding
    # S3) without a second resolve() call.
    anchors_skipped: list[dict] = []
    anchors_orphaned: list[dict] = []
    if reader is not None:
        for anchor in draft.anchors:
            result = resolve_and_bind(
                decision.id,
                Descriptor(name=anchor.name, file_path=anchor.file_path),
                reader,
                store,
                relation=anchor.relation,
            )
            if result.status == "ambiguous":
                anchors_skipped.append(
                    {
                        "name": anchor.name,
                        "reason": "ambiguous",
                        "candidates": result.candidates[:5],
                    }
                )
            elif result.status == "unresolved":
                reason = orphan_reason(
                    Descriptor(name=anchor.name, file_path=anchor.file_path), reader
                )
                anchors_orphaned.extend(
                    {**s, "reason": reason}
                    for s in entity_summaries(store, [b for b in result if b.tier == 2])
                )
    else:
        for anchor in draft.anchors:
            before = {b.entity_id for b in store.bindings_for_record(decision.id)}
            _bind_orphaned(
                decision.id,
                Descriptor(name=anchor.name, file_path=anchor.file_path),
                store,
                relation=anchor.relation,
            )
            anchors_orphaned.extend(
                {**s, "reason": "no-graph"}
                for s in entity_summaries(
                    store,
                    [
                        b
                        for b in store.bindings_for_record(decision.id)
                        if b.tier == 2 and b.entity_id not in before
                    ],
                )
            )
    if initiative:
        init = store.get_or_create_abstract_entity(f"initiative:{initiative}")
        store.add_binding(
            AnchorBinding(
                record_id=decision.id,
                entity_id=init.entity_id,
                tier=0,
                status="live",
            )
        )
    # 6. Tags — durable, cross-cutting `tag:<slug>` entities (tier-0, no lifecycle; see
    # spec §2). Bound at propose time, same as initiative, so they carry through ratify.
    for slug in tag_slugs:
        tag_entity = store.get_or_create_abstract_entity(f"tag:{slug}")
        store.add_binding(
            AnchorBinding(
                record_id=decision.id,
                entity_id=tag_entity.entity_id,
                tier=0,
            )
        )

    # 7. Attached facts (draft.facts) -- each runs through the same deterministic pipeline,
    # supporting THIS decision and, absent their own anchors, inheriting its anchors (see
    # _propose_fact_one's docstring). A bad fact never aborts the decision or its siblings:
    # _propose_fact_one already catches every anticipated failure internally (mirrors this
    # function's own per-stage try/except), same isolation guarantee `propose` gives
    # per-draft.
    fact_results = [
        _propose_fact_one(
            raw_fact,
            store,
            reader,
            session_id,
            author,
            graph_version,
            attached_to=decision.id,
            inherited_anchors=draft.anchors,
            auto_accept=auto_accept,
            ratify_policy=ratify_policy,
        )
        for raw_fact in draft.facts
    ]

    # 8. Auto-ratify (design D2/D3) — runs AFTER step 7 so this call's own attached facts
    # already exist and can be checked as the cascade set. Skipped outright when
    # `auto_accept` is true (this decision already landed ACCEPTED, and the hook runs ONLY
    # on writes that landed proposed). The decision's own gates run first (cheaper); the
    # cascade rule (`_cascade_eligible`) runs only when the decision itself is already
    # eligible, and blocks the WHOLE auto-block when any fact in the cascade set is not —
    # decision AND facts stay proposed together, to leave the queue by one human verdict.
    ratified_by: str | None = None
    auto_ratify_error: str | None = None
    if not auto_accept and ratify_policy != RatifyPolicy.MANUAL:
        live_tier12, ambiguous_or_orphan_only = _anchor_signal(store, decision.id)
        decision_signal = AutoEligibility(
            kind=draft.kind.value,
            live_tier12=live_tier12,
            ambiguous_or_orphan_only=ambiguous_or_orphan_only,
            pipeline_clean=True,
            has_provenance=True,
            domain_anchored=False,
            has_supersedes=decision.supersedes is not None,
        )
        if auto_ratify_eligible(decision_signal, ratify_policy) and _cascade_eligible(
            store, decision.id, ratify_policy
        ):
            # _cascade_eligible above is the cheap pre-check, run before Store.ratify takes
            # its write lock (design D2 checkpoint-2 fix, Ruling Q). _auto_ratify's own
            # decision route builds the authoritative re-check guard itself (Ruling T) and
            # hands it to Store.ratify, which re-runs the same per-fact test on a cascade set
            # re-queried fresh under that lock — closing the race where a fact could land
            # supporting this decision between the pre-check and the transition (external
            # review finding A1, scratchpad/probe_race.py).
            outcome = _auto_ratify(store, decision.id, decision_signal.kind, ratify_policy)
            ratified_by = outcome.ratified_by
            auto_ratify_error = outcome.error
            if outcome.cascaded_fact_ids:
                # Result truthfulness (design D6/T11/T14): the canonical cascade just
                # accepted these facts through Store.ratify -- stamp the matching NESTED
                # ProposeFactResult objects too, so the public result cannot say
                # `ratified_by=None` for a fact whose canonical row was just accepted.
                cascaded_ids = set(outcome.cascaded_fact_ids)
                fact_results = [
                    fr.model_copy(update={"ratified_by": ratified_by})
                    if fr.fact_id in cascaded_ids
                    else fr
                    for fr in fact_results
                ]

    return ProposeResult(
        status="written",
        decision_id=decision.id,
        redactions=redactions,
        anchors_skipped=anchors_skipped,
        anchors_orphaned=anchors_orphaned,
        facts=fact_results,
        neighbors=neighbors,
        ratified_by=ratified_by,
        auto_ratify_error=auto_ratify_error,
    )


def propose(
    drafts: Sequence[object],
    store: Store,
    reader: GraphifyReader | None,
    session_id: str | None = None,
    author: str | None = None,
    ref: str | None = None,
    *,
    auto_accept: bool = False,
    ratify_policy: RatifyPolicy = RatifyPolicy.MANUAL,
) -> list[ProposeResult]:
    """Run the deterministic write pipeline per draft. Per-draft failure never aborts the batch.

    ``auto_accept`` (default ``False``, keyword-only): the ``SIDEGRAPH_AUTO_ACCEPT=on``
    opt-in (see design/superpowers/specs/2026-07-10-ratification-ux-and-mcp-gaps-design.md)
    — when true, every drafted decision AND its attached facts land ``status=ACCEPTED``
    instead of ``PROPOSED``, bypassing the human ratification queue. Capture itself stays
    pure: the env var is read once by the caller (``server._auto_accept()``) and passed in
    here as a plain bool — this module never reads the environment. Provenance still
    stamps ``source="agent"`` either way; only the ratification status changes.

    ``ratify_policy`` (default ``RatifyPolicy.MANUAL``, keyword-only): the resolved
    ``SIDEGRAPH_RATIFY_POLICY`` value (design D1/D2), threaded the same way ``auto_accept``
    is — this module never reads the environment. When it allows and D3's gates pass, each
    written decision (and its eligible attached-fact cascade) is auto-ratified — see
    ``_propose_one``'s own post-write block.
    # see design/superpowers/specs/2026-09-11-auto-ratification-policy-design.md D1/D2
    """
    graph_version = reader.graph_version() if reader is not None else None
    return [
        _propose_one(
            raw,
            store,
            reader,
            session_id,
            author,
            ref,
            graph_version,
            auto_accept=auto_accept,
            ratify_policy=ratify_policy,
        )
        for raw in drafts
    ]


def propose_facts(
    drafts: Sequence[object],
    store: Store,
    reader: GraphifyReader | None,
    session_id: str | None = None,
    author: str | None = None,
    *,
    auto_accept: bool = False,
    ratify_policy: RatifyPolicy = RatifyPolicy.MANUAL,
) -> list[ProposeFactResult]:
    """Run the deterministic Fact write pipeline per STANDALONE draft (mirrors ``propose``
    for decisions). Neither ``attached_to`` nor ``inherited_anchors`` is set — a standalone
    draft must supply its own anchor or ``supports`` id (see ``_propose_fact_one``'s
    reachability check) — unlike a ``DraftDecision.facts`` entry, which always inherits
    ``attached_to``/the decision's anchors via ``_propose_one``'s facts loop. Per-draft
    failure never aborts the batch.

    ``auto_accept`` (default ``False``, keyword-only): same ``SIDEGRAPH_AUTO_ACCEPT=on``
    opt-in ``propose`` documents (see design/superpowers/specs/
    2026-07-10-ratification-ux-and-mcp-gaps-design.md) — standalone facts land
    ``status=ACCEPTED`` instead of ``PROPOSED`` when true. This module never reads the
    environment itself; the bool is passed in by the caller.

    ``ratify_policy`` (default ``RatifyPolicy.MANUAL``, keyword-only): same keyword
    ``propose`` documents (design D1/D2) — sampled once by the caller and passed down
    unchanged; the same object a combined MCP request passes to ``propose`` reaches this
    function too (see ``server._propose_decisions_impl``). When it allows and D3's gates
    pass, each written standalone fact is auto-ratified — see ``_propose_fact_one``'s own
    post-write block (``attached_to is None`` here always, for every draft this function
    writes).
    # see design/superpowers/specs/2026-09-11-auto-ratification-policy-design.md D1/D2
    """
    graph_version = reader.graph_version() if reader is not None else None
    return [
        _propose_fact_one(
            raw,
            store,
            reader,
            session_id,
            author,
            graph_version,
            auto_accept=auto_accept,
            ratify_policy=ratify_policy,
        )
        for raw in drafts
    ]


def format_proposal(d: Decision) -> str:
    """Human-readable render of a pending proposal (shared by the ratify CLI and MCP)."""
    lines = [f"{d.id}  [{d.kind.value}] {d.title}"]
    lines.append(f"  what:    {d.choice}")
    lines.append(f"  why:     {d.context}")
    if d.rejected:
        lines.append(f"  rejected: {d.rejected}")
    if d.consequences:
        lines.append(f"  learned: {d.consequences}")
    prov = d.provenance
    lines.append(f"  from:    session={prov.session_id or '-'} author={prov.author or '-'}")
    return "\n".join(lines)


def format_fact_proposal(f: Fact) -> str:
    """Human-readable render of a pending fact proposal — mirrors ``format_proposal``'s
    exact visual style (shared by the ratify CLI and MCP)."""
    lines = [f"{f.id}  [fact] {f.statement}"]
    lines.append(f"  source:   {f.source}")
    lines.append(f"  supports: {', '.join(f.supports) if f.supports else '-'}")
    prov = f.provenance
    lines.append(f"  from:     session={prov.session_id or '-'} author={prov.author or '-'}")
    return "\n".join(lines)


def _lint_domain_path_prefixes(
    path_prefixes: list[str],
    reader: GraphifyReader | None,
    store: Store,
) -> list[str]:
    """Deterministic domain-proposal lint (design D7.4, E8 gate checklist) — two
    mechanical, warning-only checks over ``path_prefixes``; never blocks the write --
    under ``auto-all`` any warning keeps the draft proposed. Shared by
    ``_propose_domain_one`` (the agent MCP path) and
    ``domains.bootstrap_domains`` (the CLI path), so both authoring routes catch the same
    two mistakes the same way.

    (a) **Dead prefix**: a ``path_prefix`` matching zero ``file_path``s among ALL current
    graph nodes (NIT-2, code review: not filtered to anchorable ones — the broader check
    is the safe direction, since it can only ever find MORE covering files than an
    anchorable-only scan would, so it never over-warns relative to that narrower
    reading) — a rule that can never resolve anything, most likely a typo or a path that
    moved. Best-effort: a no-op without a ``reader`` (nothing to check against);
    structurally can never fire for ``bootstrap_domains``'s own derived prefixes (they are
    computed from a real majority-share calc over this exact graph — see
    ``domains._derive_path_prefixes``), but an agent-typed ``propose_domains`` prefix has
    no such guarantee.

    (b) **Subsumes sibling anchor**: a ``path_prefix`` that would swallow another
    ACCEPTED domain's own ``seed_anchors`` file — the same "one rule silently expands to
    cover another domain's territory" shape the sync-time breadth guards
    (``domains._SHARED_DIR_NAMES``/``_PREFIX_BREADTH_CAP``) exist to catch for the
    bootstrap path; this is the propose-time counterpart for seed-anchor-based domains,
    which those guards don't cover. Store-only, no reader needed. "Live" = ACCEPTED —
    the same addressable-domain notion ``retrieval.py``'s TOC/drill_down use. Deduped one
    warning per ``(domain, prefix)`` pair (NIT-3, code review) — a domain with several
    seed anchors all falling under the SAME prefix names only the first match, rather
    than repeating the same complaint once per anchor.
    """
    warnings: list[str] = []
    if reader is not None:
        for p in path_prefixes:
            covers_something = any(
                n.file_path and matches_path_prefix(n.file_path, p) for n in reader.list_nodes()
            )
            if not covers_something:
                warnings.append(
                    f"path_prefix {p!r} matches no file in the current graph (dead prefix)"
                )

    if path_prefixes:
        for domain in store.iter_domains(status=DomainStatus.ACCEPTED):
            for p in path_prefixes:
                match = next(
                    (
                        anchor.file_path
                        for anchor in domain.seed_anchors
                        if anchor.file_path and matches_path_prefix(anchor.file_path, p)
                    ),
                    None,
                )
                if match is not None:
                    warnings.append(
                        f"path_prefix {p!r} subsumes domain {domain.slug!r}'s seed anchor {match!r}"
                    )

    return warnings


def _propose_domain_one(
    raw: object,
    store: Store,
    reader: GraphifyReader | None,
    session_id: str | None,
    author: str | None,
    graph_version: str | None,
    *,
    ratify_policy: RatifyPolicy = RatifyPolicy.MANUAL,
) -> ProposeDomainResult:
    """One Domain draft through the deterministic pipeline: redact -> dedup (by slug,
    ANY non-superseded status counts) -> resolve parent_slug -> write as `proposed` (§4.2,
    one gate, no exceptions).

    ``ratify_policy`` (default ``RatifyPolicy.MANUAL``, keyword-only): domains are eligible
    under ``auto-all`` only, never ``auto-low-risk`` (design D3) — the auto-block below is
    skipped outright unless ``ratify_policy is RatifyPolicy.AUTO_ALL``. On success, also
    runs ``sync.activate_accepted_domain`` (the same shared per-domain activation step the
    human MCP/CLI ratify paths use) — but never rebuilds the TOC cache itself; the
    once-per-batch rebuild is ``propose_domains``'s job (design D2), so a batch of N domains
    never pays N ``build_toc`` calls.
    # see design/superpowers/specs/2026-09-11-auto-ratification-policy-design.md D2/D3
    """
    try:
        draft = DraftDomain.model_validate(raw)
    except ValidationError as e:
        return ProposeDomainResult(status="rejected", reason=f"invalid draft: {e}")

    redactions = 0
    title, n = redact(draft.title)
    redactions += n
    summary, n = redact(draft.summary)
    redactions += n

    # Dedup: a non-superseded domain (proposed, accepted, OR dropped) at this slug already
    # exists -> skip rather than write a colliding/duplicate draft (find_domain_by_slug
    # already excludes only SUPERSEDED, matching this rule exactly).
    existing = store.find_domain_by_slug(draft.slug)
    if existing is not None:
        return ProposeDomainResult(
            status="skipped",
            domain_id=existing.domain_id,
            reason=f"slug {draft.slug!r} already used by domain {existing.domain_id}",
            redactions=redactions,
        )

    parent_id = None
    if draft.parent_slug is not None:
        parent = store.find_domain_by_slug(draft.parent_slug)
        if parent is None:
            return ProposeDomainResult(
                status="rejected",
                reason=f"parent_slug {draft.parent_slug!r} does not resolve to any domain",
                redactions=redactions,
            )
        parent_id = parent.domain_id

    try:
        domain = Domain(
            slug=draft.slug,
            title=title,
            summary=summary,
            parent_id=parent_id,
            seed_anchors=draft.seed_anchors,
            path_prefixes=draft.path_prefixes,
            provenance=Provenance(
                source="agent", author=author, session_id=session_id, graph_version=graph_version
            ),
        )
        store.add_domain(domain)
    except (ValidationError, ValueError) as e:
        return ProposeDomainResult(status="rejected", reason=str(e), redactions=redactions)

    warnings = _lint_domain_path_prefixes(draft.path_prefixes, reader, store)

    # Auto-ratify (design D2/D3) — auto-all only; never under auto-low-risk or manual.
    ratified_by: str | None = None
    auto_ratify_error: str | None = None
    if ratify_policy == RatifyPolicy.AUTO_ALL:
        domain_anchored = _domain_anchored(
            reader, warnings, draft.seed_anchors, draft.path_prefixes
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
            ratified_by = outcome.ratified_by
            auto_ratify_error = outcome.error
            if outcome.ratified_by is not None:
                # domain_anchored required `reader is not None` for eligibility, so this
                # transition's own reader is guaranteed present here.
                #
                # Ruling R (design D2/D6 checkpoint-2 fix): the domain transition already
                # committed by this point, so an activation failure must report and continue,
                # never abort the batch (external review finding A2,
                # scratchpad/probe_activation.py — an unprotected write inside the helper's
                # own refresh-failure handler could raise past this call). `Exception`, not
                # `BaseException`, consistent with `_auto_ratify`'s own catch, so
                # `KeyboardInterrupt`/`SystemExit` still propagate. `sync.activate_accepted_domain`
                # itself is deliberately NOT changed: the human MCP/CLI wrappers carried the
                # identical unprotected stale-marker write before the Task 4 extraction, and
                # changing the helper would change those byte-identical-proven paths too.
                try:
                    activation = activate_accepted_domain(domain, store, reader)
                except Exception as e:
                    auto_ratify_error = f"activation: {e}"
                else:
                    # `resolved` and `overbroad` are independent fields on
                    # `sync.DomainActivation` (checked separately, not elif'd, so neither
                    # depends on the other ever staying mutually exclusive) — rev 12
                    # erratum: a claim-cap rejection used to report a clean success here,
                    # while the human MCP/CLI wrappers (server.py, cli.py) rendered their
                    # own "path rule too broad" sentence for the identical outcome. This
                    # is that same sentence, the literal `sidegraph:heal-anchors` trigger
                    # phrase, so an unattended auto-all caller gets it too (D2, D6).
                    if not activation.resolved:
                        auto_ratify_error = f"activation: {activation.error}"
                    if activation.overbroad is not None:
                        prefixes = ", ".join(repr(p) for p in domain.path_prefixes)
                        auto_ratify_error = (
                            f"activation: path rule too broad: {prefixes} match "
                            f"{activation.overbroad['matched']}/{activation.overbroad['total']} "
                            "communities — not applied; seed_anchors, if any, still applied"
                        )

    return ProposeDomainResult(
        status="proposed",
        domain_id=domain.domain_id,
        redactions=redactions,
        warnings=warnings,
        ratified_by=ratified_by,
        auto_ratify_error=auto_ratify_error,
    )


def propose_domains(
    drafts: Sequence[object],
    store: Store,
    reader: GraphifyReader | None = None,
    session_id: str | None = None,
    author: str | None = None,
    *,
    ratify_policy: RatifyPolicy = RatifyPolicy.MANUAL,
) -> list[ProposeDomainResult]:
    """Run the deterministic Domain write pipeline per draft (§4.2, agent in-session path;
    mirrors ``propose`` for decisions). Per-draft failure never aborts the batch.

    ``reader`` (design D7.4) also feeds ``_lint_domain_path_prefixes``'s dead-prefix half —
    each result's ``warnings`` list is empty (never rejected/blocked) when a prefix has no
    reader to check against.

    ``ratify_policy`` (default ``RatifyPolicy.MANUAL``, keyword-only): the resolved
    ``SIDEGRAPH_RATIFY_POLICY`` value (design D1/D2), sampled once by the caller and passed
    down unchanged (see ``server._propose_domains_impl``). Domains are only ever eligible
    under ``AUTO_ALL`` (never ``AUTO_LOW_RISK``) — see ``_propose_domain_one``'s own
    auto-block. This function rebuilds ``TOC_CACHE_KEY`` ONCE, after the whole batch, when
    at least one domain was actually auto-ratified — never per domain (design D2: a domain
    bootstrap of N domains must not pay N ``build_toc`` calls; ``_propose_domain_one``'s own
    activation step never rebuilds it).
    # see design/superpowers/specs/2026-09-11-auto-ratification-policy-design.md D1/D2
    """
    graph_version = reader.graph_version() if reader is not None else None
    results = [
        _propose_domain_one(
            raw,
            store,
            reader,
            session_id,
            author,
            graph_version,
            ratify_policy=ratify_policy,
        )
        for raw in drafts
    ]
    if any(r.ratified_by is not None for r in results):
        store.set_meta(TOC_CACHE_KEY, json.dumps(build_toc(store)))
    return results


# How many community ids surface before a domain proposal's `communities:` line
# truncates (Gate-5 finding: an over-broad path rule can resolve to hundreds of
# communities — unreadable, and unnecessary, to dump every id in a ratify listing meant to
# catch scale at a glance, not enumerate membership).
_DOMAIN_PROPOSAL_COMMUNITY_SAMPLE = 8


def format_path_prefixes(prefixes: list[str]) -> str:
    """Render a Domain's ``path_prefixes`` membership rule for a ratify listing.

    ``"(none)"`` when empty, rather than omitting the line — Gate-5 finding: the ratify
    listing didn't show ``path_prefixes`` at all, so an over-broad auto-derived rule
    (``path_prefixes=["tests"]``, ≥80% of a community's members happened to be test files)
    was invisible at the one human gate meant to catch it before sync's REPLACE refresh
    silently expanded the domain to swallow unrelated communities. Shared by
    ``format_domain_proposal`` (CLI) and ``server._format_domain_proposal_line`` (MCP) so
    both surfaces show the same rule the same way.
    """
    if not prefixes:
        return "(none)"
    return ", ".join(f"{p.rstrip('/')}/" for p in prefixes)


def format_communities_sample(
    communities: list[str], limit: int = _DOMAIN_PROPOSAL_COMMUNITY_SAMPLE
) -> str:
    """Render a Domain's seed ``communities`` list for a ratify listing, truncated past
    ``limit`` ids (see ``_DOMAIN_PROPOSAL_COMMUNITY_SAMPLE``). ``"(none)"`` when empty —
    same rationale as ``format_path_prefixes``. Shared by ``format_domain_proposal`` (CLI)
    and ``server._format_domain_proposal_line`` (MCP)."""
    if not communities:
        return "(none)"
    if len(communities) <= limit:
        return ", ".join(communities)
    shown = ", ".join(communities[:limit])
    return f"{shown}, … (+{len(communities) - limit} more)"


# How many seed-anchor descriptors surface before a domain proposal's `anchors:` line
# truncates (Gate-6 finding: seed_anchors is the name-domains skill's PRIMARY membership
# shape — an agent-curated merge with no shared path prefix can carry a dozen+ anchors,
# unreadable to dump raw in a ratify listing meant to catch the rule at a glance).
_DOMAIN_PROPOSAL_ANCHOR_SAMPLE = 3


def _format_descriptor(d: Descriptor) -> str:
    """Render one seed-anchor ``Descriptor`` as ``name@file_path`` (bare ``name`` when
    ``file_path`` is absent) for a ratify listing."""
    return f"{d.name}@{d.file_path}" if d.file_path else d.name


def format_seed_anchors_sample(
    seed_anchors: list[Descriptor], limit: int = _DOMAIN_PROPOSAL_ANCHOR_SAMPLE
) -> str:
    """Render a Domain's ``seed_anchors`` membership rule for a ratify listing: the count
    plus a truncated ``name@file_path`` sample past ``limit`` (see
    ``_DOMAIN_PROPOSAL_ANCHOR_SAMPLE``).

    Gate-6 finding: ``seed_anchors`` — the name-domains skill's new PRIMARY membership
    shape — rendered as invisible as ``path_prefixes``/``communities`` did before Gate-5's
    fix: a domain authored with ONLY ``seed_anchors`` showed "paths: (none)  communities:
    (none)" at the human ratify gate, with no sign of the rule actually being approved.

    Unlike ``format_path_prefixes``/``format_communities_sample``, returns ``""`` (not
    ``"(none)"``) when empty — callers omit the whole ``anchors:`` line rather than adding
    a third always-present "(none)" row; ``path_prefixes``/``communities`` already show
    "no rule at all" between them. Shared by ``format_domain_proposal`` (CLI) and
    ``server._format_domain_proposal_line`` (MCP)."""
    if not seed_anchors:
        return ""
    descriptors = [_format_descriptor(d) for d in seed_anchors]
    if len(descriptors) <= limit:
        shown = ", ".join(descriptors)
    else:
        shown = ", ".join(descriptors[:limit]) + f", … (+{len(descriptors) - limit} more)"
    return f"{len(seed_anchors)} ({shown})"


def format_domain_proposal(d: Domain) -> str:
    """Human-readable render of a pending domain proposal (shared by the ratify CLI).

    Always renders the membership rule (``path_prefixes``, seed ``communities``, and seed
    ``seed_anchors``) — even when empty — so the human ratification gate can catch an
    over-broad rule, or see what it's actually approving, instead of only ever seeing
    prose (see ``format_path_prefixes``/``format_communities_sample``/
    ``format_seed_anchors_sample`` docstrings for the Gate-5/Gate-6 findings this fixes).
    The ``anchors:`` line is the one exception: it's omitted entirely when
    ``seed_anchors`` is empty, rather than printing a third "(none)" row."""
    lines = [f"{d.domain_id}  [domain] {d.slug} — {d.title}"]
    lines.append(f"  summary: {d.summary}")
    lines.append(f"  paths:   {format_path_prefixes(d.path_prefixes)}")
    lines.append(f"  communities: {format_communities_sample(d.communities)}")
    anchors = format_seed_anchors_sample(d.seed_anchors)
    if anchors:
        lines.append(f"  anchors: {anchors}")
    if d.parent_id:
        lines.append(f"  parent:  {d.parent_id}")
    prov = d.provenance
    lines.append(f"  from:    source={prov.source} author={prov.author or '-'}")
    return "\n".join(lines)
