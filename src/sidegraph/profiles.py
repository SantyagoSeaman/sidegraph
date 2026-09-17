"""Flow-profiles: per-flow reader configuration as data, not code (portable core).

A FlowProfile declares how to read one external spec/requirements flow's markdown
artifacts into decisions: the heading vocabulary (ReaderDialect) fed to doc_import's gate
and field extraction, the repo-relative ingest globs, and a marker path used for
auto-detection. Imports nothing from engine/ or host/ — this is portable core. See
design/superpowers/specs/2026-07-23-flow-profile-reader-design.md.
"""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel, ConfigDict, model_validator


class ProfileDetection(BaseModel):
    """What `detect_profile` concluded: the profile to use, if exactly one wins, plus every
    profile whose marker or globs matched. Frozen because Bootstrap treats it as plan input."""

    model_config = ConfigDict(frozen=True)

    selected: str | None
    matches: tuple[str, ...] = ()


class ReaderDialect(BaseModel):
    """Per-flow heading vocabulary. Each tuple is matched case-insensitively as a PREFIX of
    a heading (doc_import._heading_has_keyword); order within a tuple is priority order
    (most-specific first), exactly as the current module constants rely on."""

    context: tuple[str, ...]
    choice: tuple[str, ...]
    rejected: tuple[str, ...]
    consequences: tuple[str, ...]
    gate_only: tuple[str, ...]
    # Oneshot+granularity spec D2: heading keywords naming a section whose H3 children
    # become one Decision each (per-section multi-decision import). UNLIKE the other
    # tuples, selection is document-order-first — a split target is a location, not a
    # ranked field source. Default () = single-record parsing, byte-identical to before
    # the field existed.
    split_choice: tuple[str, ...] = ()

    def all_headings(self) -> tuple[str, ...]:
        """Every known heading — the decision-shaped gate passes when a doc has at least one.
        Replaces doc_import._KNOWN_SECTION_HEADINGS. ``split_choice`` is deliberately NOT
        included (spec review F12): a split target must already be choice material to gate
        a doc in; widening the decision-shaped gate is not the split feature's business."""
        return self.context + self.choice + self.rejected + self.consequences + self.gate_only


class FlowProfile(BaseModel):
    """One external flow's reader configuration and auto-detection marker."""

    name: str
    dialect: ReaderDialect
    ingest_globs: tuple[str, ...]
    marker: str | None = None
    # E1 (openspec profile design, design/superpowers/specs/2026-08-06-openspec-profile-
    # design.md §5): a regex with named groups `name`/`doc`, matched against a doc's
    # repo-relative path when it has no H1 at all. A match synthesizes the title
    # `"{name} ({doc})"` in place of the missing H1; no match (or `None`, the default)
    # rejects the doc exactly as every profile behaves today — this field is purely
    # additive and changes nothing for a profile that never sets it.
    title_pattern: str | None = None
    # E2 (design note §6): `(pattern, replacement)` applied once by `normalized_rel_path`
    # to collapse a flow's own archive-move layout onto its live-layout ref, so an
    # unedited doc re-imported after the move dedupes instead of duplicating. `None` (the
    # default) is the identity transform — every profile without this field is unaffected.
    ref_normalize: tuple[str, str] | None = None
    # I2 (R1 improvement wave §2): optional text appended to `context` for every record
    # parsed from this profile's LIVE tree (see doc_import._is_live_tree_path for the
    # trigger predicate both write paths share) — signals a retrieval-time reader that the
    # record describes an in-flight change, not settled implementation reality. `None`
    # (the default) is a no-op for every profile that never sets it.
    in_flight_note: str | None = None

    @model_validator(mode="after")
    def _in_flight_note_requires_ref_normalize(self) -> FlowProfile:
        """Setting `in_flight_note` without `ref_normalize` is a config error at
        profile-construction time (review Minor 6 — silent no-op forbidden): the
        live-vs-archived distinction the note depends on is DEFINED by `ref_normalize` — a
        profile with a note but no way to ever tell live from archived would either never
        stamp (silent no-op) or always stamp (defeats the "in flight" claim)."""
        if self.in_flight_note is not None and self.ref_normalize is None:
            raise ValueError(
                "in_flight_note requires ref_normalize to be set -- otherwise there is no "
                "way to distinguish a live-tree doc from an archived one"
            )
        return self

    def normalized_rel_path(self, path: str) -> str:
        """`path` with `ref_normalize`'s (pattern, replacement) applied once, or `path`
        unchanged when `ref_normalize` is unset (design note §6, E2). Byte-identical
        passthrough for every profile that never sets `ref_normalize`."""
        if self.ref_normalize is None:
            return path
        pattern, replacement = self.ref_normalize
        return re.sub(pattern, replacement, path)


# generic-adr = today's doc_import module constants, verbatim. The DEFAULT profile — kept
# conservative so it never over-matches (the prior-art false-positive discipline).
GENERIC_ADR_DIALECT = ReaderDialect(
    context=("context", "trigger", "root cause"),
    choice=("decision outcome", "decisions", "decision", "design"),
    rejected=(
        "rejected",
        "alternatives",
        "considered alternatives",
        "considered options",
        "options considered",
        # Measured on two real Nygard-style corpora (2026-08-03, rancher/turtles +
        # alphagov/govuk-infrastructure, 41 ADRs): structured option comparisons sit under
        # "## Proposed alternatives" or a bare "## Options" — prefix matching means none of
        # the keywords above can reach either. Together these two rescue every structured-
        # alternatives document in both corpora (3/41); the rest of the empty-`rejected`
        # mass is prose-only or simply never written down (see
        # design/testing/2026-08-02-bootstrap-foreign-corpus-evidence.md §7).
        "proposed alternatives",
        "options",
    ),
    consequences=("consequences",),
    gate_only=("status", "residuals", "scope notes", "user decisions"),
)

# superpowers = a LIBERAL dialect, safe ONLY because it is opt-in AND scope-limited to the
# specs dir (a random README can never reach it). Calibrated against the golden fixtures.
# "testing" is deliberately NOT mapped to consequences (spec §13, resolved): a test plan is
# not a consequence; a real "Risks" section is.
SUPERPOWERS_DIALECT = ReaderDialect(
    context=("goal", "context", "problem", "motivation"),
    # "approach" deliberately excluded: it is a PREFIX of the `rejected` keyword "approaches
    # considered" below, so a spec whose only choice-ish heading is "## Approaches
    # considered" would otherwise have that section assigned to BOTH `choice` and `rejected`
    # (duplicated text).
    choice=("architecture", "design", "components", "solution"),
    rejected=(
        "trade-off",
        "trade-offs",
        "alternatives",
        "alternatives considered",
        "approaches considered",
        "rejected",
    ),
    consequences=("consequences", "risks"),
    gate_only=("status",),
)

# genkovich-sdd — derived from upstream templates at github.com/genkovich/sdd (HEAD, checked
# 2026-07-27), not from resemblance to generic-adr. Three artifacts per feature, but only
# adr/*.md and sad.md are ingested (spec.md is a requirements doc, not a decision — see the
# registry entry below):
#   docs/features/<slug>/spec.md   requirements, NOT ingested: 1. Context / 2. Goals /
#                                  3. Non-goals / ... — no heading here can justify a `choice`
#   docs/features/<slug>/adr/*.md  Context / Decision drivers / Considered options /
#                                  Decision outcome / Consequences / Links
#   docs/features/<slug>/sad.md    arc42-shaped, numbered: 4. Solution strategy,
#                                  9. Architecture decisions, 11. Risks and technical debt
# `choice` deliberately carries NO bare "decision": the ADR template has BOTH "Decision drivers"
# (context) and "Decision outcome" (the choice), and a bare prefix would take the drivers — the
# same trap GENERIC_ADR_DIALECT documents above. `context` deliberately carries NO "goals"
# (dropped 2026-07-27): its only reachable heading was spec.md's "2. Goals", and spec.md is not
# ingested — a keyword no ingested document can ever match is unsourced surface, the same
# defect this whole wave exists to remove. sad.md's "1. Introduction and goals" is unaffected:
# it already matches via "introduction", which comes first in this tuple.
#
# The same rule, applied the rest of the way (2026-07-27 doc reconciliation): `rejected` carried
# three more keywords with the identical unsourced property — "alternatives considered",
# "alternatives", "rejected" — and `gate_only` carried "status". None of the four is a heading in
# either upstream template (both put status in frontmatter, not prose); "considered options" is
# the only `rejected` keyword either template can ever match, so it is the only one that stays,
# and `gate_only` ships empty rather than carry a keyword nothing can match. None of the four was
# load-bearing for any golden fixture, so dropping them moves no test (verified: the full suite is
# unchanged). `spec-kit`, calibrated the same way two tasks later, shipped with zero unsourced
# keywords from the start; this profile now matches that standard instead of contradicting it.
GENKOVICH_SDD_DIALECT = ReaderDialect(
    context=("context", "decision drivers", "introduction"),
    choice=("decision outcome", "solution strategy", "architecture decisions"),
    rejected=("considered options",),
    consequences=("consequences", "risks and technical debt", "risks"),
    gate_only=(),
)

# spec-kit — derived from github/spec-kit's actual templates (`templates/plan-template.md`,
# `templates/spec-template.md`; verified 2026-07-27), not from plausible vocabulary. Only
# plan.md is ingested (spec.md is a requirements doc — see the registry entry below); its
# real headings, in order, are exactly:
#   Summary / Technical Context / Constitution Check / Project Structure / Complexity
#   Tracking  (**Branch**, **Date**, **Spec**, **Input**, **Note** are bold metadata lines
#   right after the H1, not headings).
#
# `context` carries ONLY "technical context". spec-template.md's "User Scenarios & Testing"
# is a real heading too, but spec.md is not ingested (R3) — a keyword no ingested document
# can ever match is unsourced surface, the same reason genkovich-sdd's `context` dropped
# "goals". Bare "context"/"problem" (the brief's original guesses) match no heading in
# EITHER template at all — not even as a prefix match, since "Technical Context" does not
# start with "context" — so they are dropped outright, not just deprioritized.
#
# `choice` = "summary": plan-template's `## Summary` is filled with "the primary requirement
# plus the technical approach" upstream — the nearest real candidate to a chosen option. None
# of the brief's original guesses ("implementation", "architecture", "solution", "design")
# match any real heading in either template.
#
# `rejected` = "complexity tracking": upstream marks this section "Fill ONLY if Constitution
# Check has violations", so it is often absent — that is the flow's honest shape, not a
# calibration failure.
#
# `consequences=()`: neither template has a consequences section at all — the brief's
# "review"/"risks"/"consequences" guesses matched nothing (`## Review` does not exist).
# Verified-empty, same stance as bmad's verified-empty `rejected`: an honest empty tuple
# beats three keywords that silently match nothing.
#
# `gate_only=("constitution check",)`: `## Constitution Check` is a real plan.md heading,
# gate-only because it is a gating trigger note, not itself context/choice/rejected/
# consequences. "status" is deliberately NOT here (unlike GENERIC_ADR_DIALECT's ADR corpus,
# which has a literal "## Status" heading): spec-kit's status lives in spec.md's bold
# `**Status**` metadata line (spec.md not ingested), and plan.md has no "Status" heading of
# any kind — a keyword with nothing to match in the one document this profile reads is
# unsourced, the same standard applied to `context`/`choice` above.
SPEC_KIT_DIALECT = ReaderDialect(
    context=("technical context",),
    choice=("summary",),
    rejected=("complexity tracking",),
    consequences=(),
    gate_only=("constitution check",),
)

# bmad — derived from BMAD-METHOD's actual spine template
# (`src/bmm-skills/3-solutioning/bmad-architecture/assets/spine-template.md`, measured
# 2026-07-30 at bmad-code-org/BMAD-METHOD 9b672e1e), not from plausible vocabulary. Only
# ARCHITECTURE-SPINE.md is ingested; its real headings, in order, are exactly:
#   Design Paradigm / Inherited Invariants / Invariants & Rules (### AD-n — {decision}
#   blocks) / Consistency Conventions / Stack / Structural Seed /
#   Capability → Architecture Map / Deferred.
#
# `choice` = "invariants & rules" first: the AD blocks are "the durable heart" in the
# template's own words, and _parse_structure folds the ### AD-n children into the H2's
# body, so the whole run of decisions comes back as one section. Keyword priority (not doc
# order) is what keeps this safe: `## Design Paradigm` appears EARLIER in the document, and
# the generic dialect's bare "design" would have taken it — the same drivers-vs-outcome
# trap GENERIC_ADR_DIALECT documents. "consistency conventions" is second priority: real
# heading, decision content ("defaults that bind where independent builders would drift"),
# reachable only when a spine carries no Invariants & Rules section at all.
#
# `context` = "design paradigm": the frame the ADs bind within — the only context-shaped
# heading the template has.
#
# `rejected=()` and `consequences=()`: verified-empty (same stance as spec-kit's
# `consequences=()`). BMAD writes rationale and rejected alternatives ONLY into the run's
# flat, headingless `.memlog.md` ("Decisions, not rationale (rationale lives in the
# memlog)" — the spine template's own guide comment). Owner ruling 2026-07-30 (variant A):
# the memlog is NOT ingested — a heading-dialect reader can only degrade it into a
# degenerate record — and whether the live session channel covers what the memlog holds is
# measured in E10 rather than guessed here. A line-based memlog reader is a possible
# future wave, gated on that measurement.
#
# `gate_only=()`: an inherited-epic spine that is "mostly Inherited Invariants + a thin
# Deferred" records no NEW decisions (parent AD ids are read-only references), so failing
# the known-section gate and skipping it is the correct reading, not a calibration gap.
#
# PRD deliberately NOT ingested (R3): BMAD's PRD template is a requirements document
# (Vision / Target User / Features / Non-Goals / MVP Scope / Success Metrics / Open
# Questions) — no heading any `choice` keyword can match, same exclusion as genkovich's
# and spec-kit's spec.md.
# Oneshot+granularity spec D3 (E10 measured): `consequences=("deferred",)` — the spine's
# Deferred section (deferred decisions WITH reasons, verified negatives, open questions)
# was 5 of E10's 14 memlog-coverage losses and maps consequences-shaped; `split_choice` —
# one Decision per ### AD-n block (one-record-per-document with the 2000-char cap kept
# 3 of 9 invariants in E10's real spine). `rejected=()` stays: BMAD writes rejected
# alternatives only to the memlog (owner ruling variant A).
BMAD_DIALECT = ReaderDialect(
    context=("design paradigm",),
    choice=("invariants & rules", "consistency conventions"),
    rejected=(),
    consequences=("deferred",),
    gate_only=(),
    split_choice=("invariants & rules",),
)

# openspec — calibrated against the upstream templates shipped in the npm package
# (@fission-ai/openspec 1.8.0, schemas/spec-driven/templates/{proposal,design}.md, repo
# commit d5788966, checked 2026-08-06; the schemas/ dir ships in the package `files`, so
# the calibration source is re-checkable from node_modules without a clone), PLUS
# corpus-justified keywords under ruling R1 — each carries its citation below (D5
# deviation stated in the design note §3). Two doc kinds share one dialect: proposal.md
# (Why / What Changes / Impact) and design.md (Context / Decisions / Risks / Trade-offs).
# Delta specs and main capability specs are requirements documents and are NOT ingested
# (ruling R3); tasks.md is a checklist.
OPENSPEC_DIALECT = ReaderDialect(
    context=("context", "why"),
    # Most-specific first (prefix matching). Template: "decisions" (design),
    # "what changes" (proposal). Corpus-justified variants (design note §3 C7):
    # "architecture decisions" (4/38 archived, pre-2025-12 heading),
    # "architecture decision" (add-verify-skill design :3),
    # "key design decisions" (add-init-command design :17),
    # "key design decision" (unify-change-state-model design :10),
    # "design decisions" (project-config + project-local-schemas proposals :18),
    # "decision" singular (make-apply-instructions proposal :85 "Decision: Add …";
    #   no "Decision drivers"-style heading exists in the corpus to collide with).
    # "design decision" SINGULAR is deliberately ABSENT (review N6): its sole corpus
    #   citation ("## Design Decision: When is a change implementable?",
    #   make-apply-instructions proposal :18) holds the OPTIONS COMPARISON, and the
    #   file's real decision sits at :85 under "## Decision: Add `apply` block …" —
    #   with the longer keyword outranking bare "decision", the record stored the
    #   rejected options and dropped the decision. Without it, bare "decision"
    #   captures the real one.
    # "Trade-offs and Decisions" (1 file) is deliberately NOT here: "trade-offs" must
    # live in consequences, and a longer choice keyword sharing that prefix would
    # duplicate the same section into both fields (the superpowers "approach" lesson).
    # That one file's decisions land in consequences — measured miss, accepted.
    choice=(
        "architecture decisions",
        "architecture decision",
        "key design decisions",
        "key design decision",
        "design decisions",
        "decisions",
        "decision",
        "what changes",
    ),
    # NOT verified-empty: three real headings across the dogfood corpus (design note §3
    # R1 check). "alternatives considered" before "alternatives" — most specific first.
    rejected=("rejected", "alternatives considered", "alternatives"),
    # "risks / trade-offs" is the design.md template heading; "risks" rescues spacing
    # variants via the prefix rule; "trade-offs" rescues the bare form
    # (update-agent-instructions design :109); "impact" is the proposal template heading.
    consequences=("risks / trade-offs", "risks", "trade-offs", "impact"),
    # Template headings that qualify a doc as decision-shaped but map to no field.
    gate_only=("goals / non-goals", "capabilities", "migration plan", "open questions"),
    # PLURAL FORMS ONLY (review N2/N3): a plural heading names a CONTAINER of decisions
    # (its ### children are one record each — 24/25 archived and 11/11 live "## Decisions"
    # sections, design note §3 C2); a SINGULAR heading ("Architecture Decision: …",
    # "Decision: Add …") names ONE decision whose ### children are its own sections
    # (Context / Rationale / Alternatives Considered / …) — splitting there shredded one
    # real decision into 7 section-records (review N3, add-verify-skill). Singular
    # keywords stay in `choice` above; they must never be split targets. Split selection
    # is document-order-first; a matching heading with no H3 children is a verified
    # byte-identical no-op (review, "A3 died"); targets are level ≤ 2 only (E4).
    split_choice=(
        "architecture decisions",
        "key design decisions",
        "design decisions",
        "decisions",
    ),
)

PROFILES: dict[str, FlowProfile] = {
    "generic-adr": FlowProfile(
        name="generic-adr",
        dialect=GENERIC_ADR_DIALECT,
        ingest_globs=("docs/adr/*.md", "docs/decisions/*.md"),
        marker="docs/adr",
    ),
    "superpowers": FlowProfile(
        name="superpowers",
        dialect=SUPERPOWERS_DIALECT,
        ingest_globs=("docs/superpowers/specs/*.md",),
        marker="docs/superpowers",
    ),
    "genkovich-sdd": FlowProfile(
        name="genkovich-sdd",
        dialect=GENKOVICH_SDD_DIALECT,
        # spec.md deliberately excluded (owner ruling, 2026-07-27: do not ingest requirements
        # documents — genkovich itself separates requirements from decisions, and this
        # profile ingests decisions). Upstream keeps requirements in spec.md and the actual
        # decisions in adr/ and sad.md; spec.md has no heading any `choice` keyword can match,
        # so ingesting it produced a record whose `choice` restated its own `context`
        # (test_genkovich_globs_exclude_spec_by_design pins the exclusion).
        ingest_globs=(
            "docs/features/*/adr/*.md",
            "docs/features/*/sad.md",
        ),
        marker="docs/features",
    ),
    "spec-kit": FlowProfile(
        name="spec-kit",
        dialect=SPEC_KIT_DIALECT,
        # spec.md deliberately excluded (R3, owner ruling 2026-07-27: do not ingest
        # requirements documents). Upstream's spec.md states user scenarios / requirements /
        # success criteria / assumptions — no heading there can justify a `choice`; the only
        # heading in the whole flow that can is plan.md's own `## Summary`.
        ingest_globs=("specs/*/plan.md",),
        marker=".specify",
    ),
    "bmad": FlowProfile(
        name="bmad",
        dialect=BMAD_DIALECT,
        # Measured layout (upstream defaults): output_folder = "_bmad-output",
        # planning_artifacts = "{output_folder}/planning-artifacts", spine_output_path =
        # "{planning_artifacts}/architecture", run_folder_pattern =
        # "architecture-{project_name}-{date}". prd.md excluded (R3) and the run folder's
        # sibling .memlog.md excluded (owner ruling 2026-07-30, variant A) — see the
        # BMAD_DIALECT comment above for both rationales.
        ingest_globs=("_bmad-output/planning-artifacts/architecture/*/ARCHITECTURE-SPINE.md",),
        # The installer's scaffold dir ({project-root}/_bmad — memlog.py lives under
        # _bmad/scripts/), same role as spec-kit's ".specify".
        marker="_bmad",
    ),
    # openspec — D1: proposal.md IS ingested (not a requirements doc; R3 does not apply).
    # D2: marker is `openspec/changes`, not `openspec/config.yaml` (2026-02+ only) or the
    # legacy `openspec/project.md` — `openspec init` creates `changes/`+`changes/archive/`
    # unconditionally. D3/D4: known accepted-noise/plural-only-split judgment calls — see
    # design/superpowers/specs/2026-08-06-openspec-profile-design.md §4 for the full
    # rationale each decision carries.
    "openspec": FlowProfile(
        name="openspec",
        dialect=OPENSPEC_DIALECT,
        ingest_globs=(
            "openspec/changes/*/proposal.md",
            "openspec/changes/*/design.md",
            "openspec/changes/archive/*/proposal.md",
            "openspec/changes/archive/*/design.md",
        ),
        marker="openspec/changes",
        title_pattern=(
            r"openspec/changes/(?:archive/\d{4}-\d{2}-\d{2}-)?"
            r"(?P<name>[^/]+)/(?P<doc>proposal|design)\.md$"
        ),
        ref_normalize=(r"openspec/changes/archive/\d{4}-\d{2}-\d{2}-", "openspec/changes/"),
        # I2 (R1 improvement wave §2, R1 finding P5): battery q11 + s5/s6 measured live-tree
        # records read as settled implementation reality when they're actually still in
        # flight (an "unimplemented" record whose change had already merged; an "open" bug
        # already fixed). Stamped onto every record parsed from the LIVE tree only.
        in_flight_note=(
            "Change in flight (not yet archived) at import time — verify implementation "
            "state against the code before trusting task/spec claims."
        ),
    ),
}


def get_profile(name: str) -> FlowProfile:
    """The FlowProfile registered under `name`, or a ValueError listing the valid names."""
    try:
        return PROFILES[name]
    except KeyError:
        valid = ", ".join(sorted(PROFILES))
        raise ValueError(f"unknown flow profile {name!r} (valid: {valid})") from None


def detect_profile(root: Path, explicit: str | None = None) -> ProfileDetection:
    if explicit is not None:
        get_profile(explicit)
        return ProfileDetection(selected=explicit, matches=(explicit,))
    specific = sorted(
        name
        for name, profile in PROFILES.items()
        if name != "generic-adr" and profile.marker and (root / profile.marker).exists()
    )
    if len(specific) == 1:
        return ProfileDetection(selected=specific[0], matches=tuple(specific))
    if specific:
        return ProfileDetection(selected=None, matches=tuple(specific))
    generic = PROFILES["generic-adr"]
    if any(any(root.glob(pattern)) for pattern in generic.ingest_globs):
        return ProfileDetection(selected="generic-adr", matches=("generic-adr",))
    return ProfileDetection(selected=None, matches=())
