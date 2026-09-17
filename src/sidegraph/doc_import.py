"""Decision-shaped markdown -> anchored decisions (portable core, no LLM, no key).

Importer #2 (mirrors ``importer.py``'s shape/report/CLI patterns exactly). A markdown file
qualifies as **decision-shaped** when it has an H1 title AND at least one known
decision-section heading (ADR/Nygard style: Context/Decision/Status/Consequences/
Rejected/Alternatives/... or this project's own spec style: Trigger/Design/Residuals/
Scope notes/User decisions). Anchors come from backtick-quoted mentions in the doc, never
guessed, plus the document's own file-level node when present. See
``docs/reference/cli.md#importing-decision-shaped-markdown---docs``.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, computed_field

from .anchoring import resolve_and_bind
from .capture import (
    AutoEligibility,
    RatifyPolicy,
    _anchor_signal,
    _auto_ratify,
    auto_ratify_eligible,
    redact,
)
from .engine.reader import GraphifyReader
from .profiles import GENERIC_ADR_DIALECT, FlowProfile, ReaderDialect, get_profile
from .schema import (
    AnchorBinding,
    Decision,
    DecisionKind,
    DecisionStatus,
    Descriptor,
    Provenance,
    slugify,
)
from .store import Store
from .verify import _find_repo_root

# Cap on frequency-ranked, resolved mention-anchors per doc (design §2) — separate from,
# and in addition to, the doc's own file-node anchor (always added when present).
_MAX_MENTION_ANCHORS = 3

_TITLE_LIMIT = 120
# Fix-wave B, B3: 600 chars amputated the "why" mid-word ("Secu", "over-en", "deadli" were all
# observed against a real ADR corpus) — raised to 2000 (default; overridable per run via
# `import_docs(section_limit=...)` / `sidegraph-import --docs --section-limit N`, min 200 —
# see `_truncate_section`). The title limit is untouched: a title is a lead-in, not the
# recorded reasoning, so it stays a plain 120-char slice, no word-boundary/marker logic.
_SECTION_LIMIT = 2000
_MIN_SECTION_LIMIT = 200
# Appended when a section body is cut at `section_limit` so a downstream reader (human or
# agent) KNOWS the text was truncated, instead of silently reading a sentence that just stops
# — the eval's own root-cause finding was that a silent mid-word cut gets misread as complete.
_TRUNCATION_MARKER = " …[truncated]"

# Known decision-section headings (design §1), matched case-insensitively as a PREFIX of
# the heading text (verified against a real-world ADR/spec corpus during pre-release
# calibration: the exact-heading rule qualified only 4/11 decision docs there; prefix
# matching — real headings look like "Design — track the observed community…", "Scope notes /
# residuals (documented, not solved here)", "User decisions (this brainstorm)" — plus the
# bold-metadata gate below (see :data:`_BOLD_META_RE`) takes that to 11/11, while still
# correctly excluding every task-plan file (headings like "## Tasks" don't start with
# anything known, and this project's plan template's bold metadata is "**Goal:**", not
# Trigger/Status). Prefix, not substring: substring matching let "Decision" false-positive-
# match inside "User decisions" ("decisions" contains "decision"), silently stealing
# `choice` from the wrong (gate-only) section — a heading has to actually START WITH the
# keyword to count.
#
# Most-specific-first ordering matters within `_CHOICE_HEADINGS`: MADR's template carries
# BOTH "Decision Drivers" and "Decision Outcome" — both start with bare "decision", so
# without a more specific keyword tried first, `_first_matching_section` (doc order, first
# KEYWORD match wins) would pick whichever section happens to come first in the document,
# which for MADR is "Decision Drivers" (context, not the choice). "decision outcome" is
# tried before the bare "decision" fallback so the outcome section always wins when both
# exist. Same story for `_REJECTED_HEADINGS`: MADR's "Considered Options" heading didn't
# match any existing keyword at all (word order differs from "options considered") until
# added explicitly.
_CONTEXT_HEADINGS = GENERIC_ADR_DIALECT.context
_CHOICE_HEADINGS = GENERIC_ADR_DIALECT.choice
_REJECTED_HEADINGS = GENERIC_ADR_DIALECT.rejected
# Fix-wave B, B2: real ADRs carry a genuine "## Consequences" H2 (Positive/Negative/Risks
# subsections) — split out of `_GATE_ONLY_HEADINGS` into its own extraction keyword tuple so
# `Decision.consequences` (a schema field the pre-B2 mapping never populated at all) actually
# gets filled from it, the same "first matching known heading" mechanism `choice`/`rejected`
# already use.
_CONSEQUENCES_HEADINGS = GENERIC_ADR_DIALECT.consequences
_GATE_ONLY_HEADINGS = GENERIC_ADR_DIALECT.gate_only
_KNOWN_SECTION_HEADINGS = GENERIC_ADR_DIALECT.all_headings()

_HEADING_RE = re.compile(r"^(#{1,3})\s+(.*)$")
_FENCE_RE = re.compile(r"^\s*```")
# Blank templates carry every qualifying section heading by construction, so the section
# gate can't reject them — the angle-bracket placeholder left in the H1 ("<Decision
# title>") is the deterministic tell (found live on an ADR corpus's `_ADR-template.md`).
# Backtick spans are stripped first so a real title quoting a generic (`Dict<str, Node>`)
# still qualifies; an unbackticked generic in an H1 is a miss we accept — a skipped doc
# beats an imported blank form (false-positive imports are worse than missed specs).
_BACKTICK_SPAN_RE = re.compile(r"`[^`]*`")
_PLACEHOLDER_RE = re.compile(r"<[^<>]+>")
_FRONTMATTER_RE = re.compile(r"^---[ \t]*\n(.*?\n)---[ \t]*\n?", re.DOTALL)
_FRONTMATTER_STATUS_RE = re.compile(r"^\s*status\s*:\s*(.+?)\s*$", re.IGNORECASE)

# BUG G — document-TEMPLATE tells (see :func:`_is_template_doc` / :func:`_placeholder_dominated`).
# A template is a skeleton meant to be COPIED and filled in, never a decision — yet blank ADR/
# SAD templates carry real-word H1s (`# ADR - Architecture Decision Record`) plus every
# qualifying section heading, so the decision-shaped gate and the H1-placeholder guard
# (:data:`_PLACEHOLDER_RE`, which only catches an angle-bracket IN the H1) both waved them
# through — one even landed ACCEPTED off a template's own `**Status:** APPROVED` line,
# polluting the store with a fake accepted decision. Calibrated against a real architecture
# corpus: all 3 of its ADR/SAD templates are `*template*.md` AND carry `type: template`
# frontmatter, while its 11 real ADRs are neither (their `type:` is adr/appendix/annex).
# Conservative by design — prefer MISSING a template (importing a real doc) over rejecting a
# real ADR: the filename/`type` tells are near-zero-false-positive, and the placeholder-body
# tell fires only when placeholder stubs DOMINATE the qualifying section text.
_FRONTMATTER_TYPE_RE = re.compile(r"^\s*type\s*:\s*(.+?)\s*$", re.IGNORECASE)
# Non-link square-bracket stub: `[...]` NOT followed by `(` (a markdown link `[text](url)`)
# or `[` (a reference link `[text][ref]`) — a bare `[decision here]`/`[added]` guidance stub,
# not real linked content.
_BRACKET_PLACEHOLDER_RE = re.compile(r"\[[^\[\]\n]+\](?![(\[])")
# Bare authoring markers / date-number stubs left unfilled in a blank template.
_PLACEHOLDER_MARKER_RE = re.compile(r"\b(?:TODO|TBD|FIXME|XXX|NNN|YYYY(?:-MM-DD)?)\b")
# A Context/Decision body counts as blank-template when placeholder text makes up at least
# this fraction of its (backtick-stripped) characters.
_PLACEHOLDER_DOMINANCE = 0.5

# Bold metadata lines right after the H1 (this project's own spec-style convention — real
# specs open with "**Date:** ... **Status:** ... **Trigger:** ..." right after the title).
# Distinct from YAML frontmatter (a real "---" fence):
# this is prose-adjacent metadata, not a machine block, but it is common and regular
# enough in this corpus to parse deliberately (mid-task spec refinement).
#
# Adjudicated gate rule (mid-task refinement, second pass, live-corpus verified 11/11 specs
# / 0/10 plans): a bold **Trigger:** OR **Status:** line is an ADDITIONAL qualifying signal
# alongside the known-heading-prefix gate (Nygard ADRs carry Status as a first-class field,
# so a bold Status line is a strong decision-doc signal on its own). **Date:** alone NEVER
# qualifies — every note, dated or not, has a date; using it as a gate would let arbitrary
# prose in. The known-section VOCABULARY is deliberately NOT widened with generic
# engineering-doc headings like "Purpose"/"Scope"/"Architecture"/"Components" to catch the
# remaining stragglers some other way: those headings are endemic to READMEs and ordinary
# module docs too, and a false-positive import (some unrelated doc misread as a decision)
# is worse than a missed one (which the interactive/agent capture path still covers). Do
# not pad this list without re-running the live-corpus check both ways (specs qualifying
# AND plans/READMEs staying excluded).
_BOLD_META_RE = re.compile(r"^\*\*(Trigger|Status|Date):\*\*\s*(.*)$", re.IGNORECASE)

# ANY bold "**Key:** value" lead line, not just Trigger/Status/Date — real specs carry
# non-qualifying bold keys right alongside the qualifying ones (e.g. "**Related:**",
# "**Covers:**", "**Informed by:**", "**Builds on:**", as seen in real specs alongside the
# qualifying keys). This is used ONLY to decide
# what counts as "prose" for the first-paragraph fallback (see `_strip_bold_lead_lines`) —
# it must NEVER widen the qualifying gate itself, which stays exactly `_BOLD_META_RE`
# (Trigger/Status only, per the adjudicated rule above).
_BOLD_ANY_RE = re.compile(r"^\*\*([A-Za-z][^*]*):\*\*\s*(.*)$")

# Fix-wave B, B2: bold-line pseudo-headings for `rejected`/`consequences` content that lives
# INSIDE a narrative section rather than under its own real H2/H3 heading — the calibrated
# finding against a real ADR corpus: none of its 8 imported ADRs had a literal "## Rejected"
# heading (rejection reasoning instead sits in prose, e.g. "**Rejected — emitter-side
# auto-create.** A create-if-missing behaviour..." and, as a list item,
# "- **Rejected (antipattern) — a centralised...** A single service..."). Unlike
# `_BOLD_META_RE`/`_BOLD_ANY_RE` (the H1 lead-block ``**Key:** value`` convention, colon
# required), these pseudo-headings have NO colon — the label runs straight into an em-dash or
# a closing `**`, and a leading `- ` list marker is common, so it's stripped rather than
# required or rejected.
#
# Deliberately narrow (Rejected/Alternative(s)/Considered for `rejected`; Consequence(s) for
# `consequences`) rather than a generic "any bold lead-in", mirroring the same
# false-positive-avoidance stance as `_BOLD_META_RE`'s Trigger/Status-only gate: a
# **Chosen:**-style bold lead-in must never be misread as a rejection.
_BOLD_PSEUDO_REJECTED_RE = re.compile(
    r"^-?\s*\*\*(Rejected|Alternatives?|Considered)\b", re.IGNORECASE
)
_BOLD_PSEUDO_CONSEQUENCES_RE = re.compile(r"^-?\s*\*\*(Consequences?)\b", re.IGNORECASE)
# Detects the START of ANY bold pseudo-heading line (not just the two keyword sets above) so
# a multi-item block with no blank line between entries (e.g. "- **Chosen:** ...\n-
# **Rejected:** ...", observed live) stops capturing the PRIOR block at the right line rather
# than swallowing the next one — the same role `_BOLD_ANY_RE` plays for `_bold_lead_blocks`.
_BOLD_PSEUDO_ANY_RE = re.compile(r"^-?\s*\*\*[A-Za-z]")

# "known source extension" allowlist for the path-like mention classifier (design §2).
_SOURCE_EXTENSIONS = (
    ".py",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".go",
    ".rs",
    ".java",
    ".rb",
    ".c",
    ".cc",
    ".cpp",
    ".h",
    ".hpp",
    ".cs",
    ".php",
    ".swift",
    ".kt",
    ".scala",
    ".md",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".sh",
    ".sql",
)
_SNAKE_RE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)+$")
_CAMEL_RE = re.compile(r"^[A-Za-z][a-z0-9]*(?:[A-Z][a-z0-9]*)+$")
_BACKTICK_RE = re.compile(r"`([^`\n]+)`")


class ParsedDoc(BaseModel):
    """One decision-shaped doc's field mapping (design §1). ``title``/``context``/
    ``choice``/``rejected``/``consequences`` are already redacted+truncated — ready to write
    onto a :class:`~sidegraph.schema.Decision` verbatim. ``frontmatter_status`` is the raw
    (unredacted, untruncated) value of either a YAML-frontmatter ``status:`` key OR a bold
    ``**Status:**`` metadata line right after the H1 (YAML frontmatter wins if somehow both
    are present) — it is never persisted; the caller uses it only to decide whether this
    doc is a superseded/deprecated history doc that should not be (re-)imported as live
    memory, AND (fix-wave B, B1) whether it reads as a draft/proposed/pending-review doc
    that must land ``proposed`` regardless of ``--propose``. ``suggested_kind`` (fix-wave B,
    B4) is likewise never persisted as-is: a per-document signal (currently just "the doc
    has a qualifying 'Root cause' section" -> :class:`~sidegraph.schema.DecisionKind.LESSON`)
    the caller applies UNLESS the run's own ``--kind`` was explicitly given, which always
    wins."""

    title: str
    context: str
    choice: str
    rejected: str | None = None
    consequences: str | None = None
    frontmatter_status: str | None = None
    suggested_kind: DecisionKind | None = None
    # Oneshot+granularity spec D2: set on split-produced CHILD records only — the
    # slugified H3 heading this record was cut from. The import loop keys the child's
    # provenance/idempotency on the effective ref `f"{rel_path}#{fragment}"`; None (the
    # default, and always the parent's value) keeps the bare-path ref unchanged.
    fragment: str | None = None


class DocWriteRequest(BaseModel):
    parsed: ParsedDoc
    rel_path: str
    source_hash: str
    status: DecisionStatus
    anchors: tuple[Descriptor, ...] = ()
    file_anchor: Descriptor | None = None
    anchors_skipped: tuple[dict, ...] = ()
    graph_version: str | None = None
    tags: tuple[str, ...] = ()
    # A write may ratify a pending proposal at the same ref ONLY when the caller states that a
    # human asked for it (Bootstrap's `accept` action). Default False so an unattended importer
    # rerun can never bypass the ratification queue — see
    # design/superpowers/specs/2026-08-02-bootstrap-release-readiness-design.md §3.1.
    ratify_matching_proposal: bool = False

    @computed_field  # type: ignore[prop-decorator]
    @property
    def ref(self) -> str:
        if self.parsed.fragment is None:
            return self.rel_path
        return f"{self.rel_path}#{self.parsed.fragment}"


class DocWriteResult(BaseModel):
    action: Literal["written", "superseded", "skipped-existing", "skipped-unanchorable"]
    decision_id: str | None = None


@dataclass(frozen=True)
class _DocDisposition:
    action: Literal["written", "superseded", "skipped-existing", "skipped-unanchorable"]
    accepted_ancestor: Decision | None
    pending_proposal: Decision | None
    supersedable: tuple[Decision, ...]
    inherit_bindings_from: str | None


class DocImportReport(BaseModel):
    """Counts (+ dry-run-only listing) for one ``import_docs`` run. Mirrors
    ``importer.ImportReport``'s shape, widened for doc-import's extra outcomes: a doc can
    be *superseded* (edited since its last import) as well as freshly *imported*, and can
    be skipped for four distinct reasons (existing/unanchorable/not-decision-shaped/
    unparseable) plus a fifth doc-import-only reason (superseded/deprecated frontmatter —
    a history doc, never (re-)imported as live) and a sixth (a document TEMPLATE — see
    ``skipped_template``)."""

    imported: int = 0
    superseded: int = 0
    skipped_existing: int = 0
    skipped_unanchorable: int = 0
    skipped_not_decision: int = 0
    skipped_superseded_frontmatter: int = 0
    # BUG G: a document TEMPLATE (skeleton to copy/fill) — detected by a "template" filename,
    # a `type: template` frontmatter field, or a placeholder-dominated Context/Decision body
    # (see `_is_template_doc`/`_placeholder_dominated`). Counted distinctly from a plain
    # not-decision-shaped skip: blank templates were importing as false-positive decisions
    # (one even landing ACCEPTED off the template's own `**Status:** APPROVED` line).
    skipped_template: int = 0
    # A doc that IS decision-shaped (has the H1 + qualifying section/bold-metadata signal)
    # but whose `choice` came back empty even after every fallback (design refinement (c) —
    # see `_parse_decision_doc_with_reason`/`_deepest_choice_fallback`) — never written as a
    # Decision with an empty choice, counted separately from a plain not-decision-shaped
    # skip so the CLI/dry-run listing can tell the two apart.
    skipped_unparseable: int = 0
    # Of the decisions counted above as imported/superseded (dry-run or real), how many
    # landed `proposed` because their source status read as draft/pending/under-review-like
    # (fix-wave B, B1) — regardless of whether `--propose` was also passed. Counted
    # separately from a plain `--propose` run so the report line can say WHY, e.g. "N landed
    # proposed (source status: draft)".
    status_derived_proposed: int = 0
    # Of the decisions counted above, how many landed `rejected` because their source
    # status read as turned down — regardless of `--propose`. Before this counter such a
    # doc landed `accepted`, i.e. the store asserted the team had adopted what it refused.
    status_derived_rejected: int = 0
    # design D7.1 (staleness-machinery wave, E8 hygiene): a file enumerated from `paths`
    # (directly, or via directory recursion) whose repo-relative path matches NONE of the
    # active profile's `ingest_globs` — never parsed, never anchor-resolved, just skipped
    # and counted. Distinct from every other skip reason above: those all fire AFTER a doc
    # was at least read; this one fires before the file is opened at all. Zero unless
    # `any_doc=True` was NOT passed and at least one enumerated file falls outside the
    # profile's globs (the CLI's `--any-doc` flag restores today's no-filter behavior).
    skipped_outside_profile: int = 0
    # E3 (design note §7, review Major 4/Blocker N1): a split-produced parent that is
    # never written because it is degenerate — either an echo of its own `context` (rule
    # 1) or empty even after every fallback (rule 2, which previously discarded the whole
    # file's children along with it). The children stand alone either way; this counts the
    # suppressed parent, once per file where either rule fired.
    skipped_degenerate_parent: int = 0
    # Auto-ratification policy (design D2/D6) -- additive/defaulted, same contract as
    # importer.ImportReport's own pair: incremented/appended by the post-write auto block
    # in _import_one_record, always empty/zero under `manual` or when a written record
    # was ineligible.
    auto_ratified: int = 0
    auto_ratify_failures: list[str] = Field(default_factory=list)  # ["<decision id>: <reason>"]
    # [{"file_path", "ref", "title", "action": "imported"|"superseded", "anchors_skipped"},
    # ...] — one item per RECORD (a split-capable dialect can emit several per file);
    # `ref` is the effective ref (`path` or `path#fragment`), `file_path` the real path.
    dry_run: list[dict] = Field(default_factory=list)

    def by_file(self) -> dict[str, int]:
        """Per-file breakdown of the dry-run listing, for the CLI's ``--dry-run`` printer."""
        out: dict[str, int] = {}
        for item in self.dry_run:
            fp = item["file_path"] or "<unknown>"
            out[fp] = out.get(fp, 0) + 1
        return out


# -- parser (design §1) ------------------------------------------------------------------


def _strip_frontmatter(text: str) -> tuple[str, str | None]:
    """Split a leading ``---``-delimited YAML-ish frontmatter block off ``text`` and pull
    its ``status:`` value, if any. Returns ``(remaining_text, status_or_None)``. Only a
    genuine frontmatter block counts (a real ``---`` fence at byte 0) — this project's own
    bold-metadata-line convention (``**Status:** ...`` right under the H1) is NOT
    frontmatter and is intentionally not parsed here (it is prose, not machine metadata;
    see design §1's literal "status frontmatter" wording)."""
    if not text.startswith("---"):
        return text, None
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return text, None
    status = None
    for line in m.group(1).splitlines():
        mm = _FRONTMATTER_STATUS_RE.match(line)
        if mm:
            status = mm.group(1).strip().strip("\"'")
    return text[m.end() :], status


def _frontmatter_type(text: str) -> str | None:
    """Raw ``type:`` value of a leading ``---``-delimited YAML-frontmatter block, or ``None``
    (BUG G — a ``type: template`` doc is a skeleton, never a decision). Mirrors
    :func:`_strip_frontmatter`'s parse; only a genuine ``---`` fence at byte 0 counts, and a
    key repeated more than once keeps the last occurrence."""
    if not text.startswith("---"):
        return None
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return None
    type_value: str | None = None
    for line in m.group(1).splitlines():
        mm = _FRONTMATTER_TYPE_RE.match(line)
        if mm:
            type_value = mm.group(1).strip().strip("\"'")
    return type_value


def _is_template_doc(rel_path: str, frontmatter_type: str | None) -> bool:
    """True when the doc is a TEMPLATE by its cheapest, near-zero-false-positive tells (BUG
    G): a filename whose STEM is ``template`` as a whole word (``ADR-template.md``,
    ``SAD-template.md``, ``_ADR-template.md``, ``template.md``) OR an explicit
    ``type: template`` frontmatter field. Both hold for every template in the calibration
    corpus and for none of its real ADRs. The filename tell is anchored to the stem's END (a
    ``-``/``_`` boundary then ``template``) rather than a bare substring so a REAL ADR that
    merely mentions templates in its name -- ``ADR-012-email-template-engine.md`` -- is NOT
    dropped (review finding N1)."""
    stem = rel_path.rsplit("/", 1)[-1].rsplit(".", 1)[0].lower()
    if stem == "template" or stem.endswith("-template") or stem.endswith("_template"):
        return True
    return frontmatter_type is not None and frontmatter_type.strip().lower() == "template"


def _placeholder_dominated(text: str) -> bool:
    """True when placeholder stubs (``<...>``, non-link ``[...]``, TODO/TBD/NNN/YYYY markers)
    make up at least :data:`_PLACEHOLDER_DOMINANCE` of ``text``'s non-backtick characters (BUG
    G — a blank-template Context/Decision body left unfilled). Backtick spans are stripped
    first so a real ADR quoting a generic (`` `Dict<str, Node>` ``) never counts; empty text
    is never dominated."""
    stripped = _BACKTICK_SPAN_RE.sub("", text).strip()
    total = len(stripped)
    if total == 0:
        return False
    placeholder_chars = 0

    def _blank(m: re.Match[str]) -> str:
        nonlocal placeholder_chars
        placeholder_chars += len(m.group(0))
        return " " * len(m.group(0))

    # Blank each <...>/[...] span out (same length in spaces) so a marker sitting INSIDE one
    # (e.g. `<TBD>`) is not counted twice, then tally the bare markers left in the remainder.
    remainder = _BRACKET_PLACEHOLDER_RE.sub(_blank, _PLACEHOLDER_RE.sub(_blank, stripped))
    for mk in _PLACEHOLDER_MARKER_RE.finditer(remainder):
        placeholder_chars += len(mk.group(0))
    return placeholder_chars / total >= _PLACEHOLDER_DOMINANCE


def _heading_positions(lines: list[str]) -> list[tuple[int, int, str]]:
    """``[(line_index, level, heading_text), ...]`` outside fenced code blocks — a line
    inside a ```` ``` ````-fenced block (e.g. a Python ``# comment``) must never be mistaken
    for a markdown heading."""
    out: list[tuple[int, int, str]] = []
    in_fence = False
    for i, line in enumerate(lines):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = _HEADING_RE.match(line)
        if m:
            out.append((i, len(m.group(1)), m.group(2).rstrip("#").strip()))
    return out


def _first_paragraph(lines: list[str]) -> str:
    """First contiguous block of non-blank lines, space-joined — the ``choice`` fallback
    when neither a Decision nor a Design section exists (design §1)."""
    started = False
    out: list[str] = []
    for line in lines:
        if line.strip() == "":
            if started:
                break
            continue
        started = True
        out.append(line.strip())
    return " ".join(out).strip()


def _bold_lead_blocks(lines: list[str], key_re: re.Pattern[str]) -> list[tuple[str, str, int, int]]:
    """Walk ``lines`` (expected to be a doc's "lead" block, between the H1 and the first
    heading) for ``**Key:** value`` blocks whose key matches ``key_re``. Returns
    ``[(key_lower, joined_value, start_idx, end_idx_exclusive), ...]``.

    A value's continuation runs across physical lines until a blank line, the next bold
    lead line (:data:`_BOLD_ANY_RE` — ANY key, not just ``key_re``'s, so one key's
    continuation never swallows the next key's line), or a heading — real ``**Trigger:**``
    values routinely wrap 3-4 physical lines (observed in a real multi-line trigger
    description): capturing only the first physical line left ``context``/``choice``
    truncated mid-sentence, and — worse
    — let the WRAPPED continuation (starting mid-sentence, lowercase) leak into the
    first-paragraph ``choice`` fallback when no Decision/Design section existed.
    """
    out: list[tuple[str, str, int, int]] = []
    i = 0
    n = len(lines)
    while i < n:
        m = key_re.match(lines[i].strip())
        if m is None:
            i += 1
            continue
        start = i
        key = m.group(1).lower()
        value_parts = [m.group(2).strip()]
        i += 1
        while i < n:
            nxt = lines[i]
            if nxt.strip() == "" or _BOLD_ANY_RE.match(nxt.strip()) or _HEADING_RE.match(nxt):
                break
            value_parts.append(nxt.strip())
            i += 1
        out.append((key, " ".join(p for p in value_parts if p).strip(), start, i))
    return out


def _bold_pseudo_heading_blocks(lines: list[str], key_re: re.Pattern[str]) -> list[str]:
    """Bold-line pseudo-headings ANYWHERE in ``lines`` (a full document body — unlike
    :func:`_bold_lead_blocks`, these appear inside narrative section bodies, not just the H1
    lead block) whose line start matches ``key_re``. Returns the joined block text (the
    matching line plus every following line up to a blank line, a markdown heading, or the
    next bold pseudo-heading of ANY kind — :data:`_BOLD_PSEUDO_ANY_RE`), one entry per match,
    in document order.

    A leading ``- `` list marker is stripped; the ``**...**`` markup itself is kept — unlike
    ``**Trigger:** value``, there is no colon separating a machine key from its value here
    (real examples: ``"**Rejected — emitter-side auto-create.** A create-if-missing
    behaviour..."``), so the bold span is part of the meaningful content, not a label to
    discard (fix-wave B, B2).

    Fence-aware (review follow-up, Minor 2): a line inside a ```` ``` ````-fenced block
    (e.g. a style guide QUOTING this very convention as a worked example for future ADR
    authors) is never scanned as a real pseudo-heading — the same ``_FENCE_RE`` toggle
    :func:`_heading_positions` uses, so a fenced example is skipped exactly like a fenced
    ``#`` comment is already skipped for real headings."""
    out: list[str] = []
    i = 0
    n = len(lines)
    in_fence = False
    while i < n:
        if _FENCE_RE.match(lines[i]):
            in_fence = not in_fence
            i += 1
            continue
        if in_fence:
            i += 1
            continue
        stripped = lines[i].strip()
        if key_re.match(stripped) is None:
            i += 1
            continue
        block = [re.sub(r"^-\s*", "", stripped)]
        i += 1
        while i < n:
            nxt = lines[i]
            if _FENCE_RE.match(nxt):
                # A fence boundary ends this block too — never let it span into (or
                # swallow the toggle of) a fenced region; re-processed by the outer loop.
                break
            if (
                nxt.strip() == ""
                or _HEADING_RE.match(nxt)
                or _BOLD_PSEUDO_ANY_RE.match(nxt.strip())
            ):
                break
            block.append(nxt.strip())
            i += 1
        out.append(" ".join(p for p in block if p).strip())
    return out


def _extract_bold_metadata(lines: list[str]) -> dict[str, str]:
    """``{"trigger"|"status"|"date": value, ...}`` from bold ``**Key:** value`` lines (see
    :data:`_BOLD_META_RE`) among ``lines`` — expected to be the "lead" block between the H1
    and the first heading, where this project's own specs put them. Values spanning several
    wrapped physical lines are joined (see :func:`_bold_lead_blocks`). A key repeated more
    than once keeps the LAST occurrence (simplest deterministic rule; not expected in
    practice — this is metadata, not a section)."""
    return {key: value for key, value, _, _ in _bold_lead_blocks(lines, _BOLD_META_RE)}


def _strip_bold_lead_lines(lines: list[str]) -> list[str]:
    """Remove every ``**Key:** value`` lead block (key line + wrapped continuation lines),
    for ANY bold key — not just the qualifying Trigger/Status/Date (:data:`_BOLD_META_RE`)
    — from ``lines`` before it is scanned for the first-paragraph ``choice`` fallback.

    Design refinement (b): real lead blocks routinely carry non-qualifying bold keys right
    alongside the qualifying ones — ``**Related:**``, ``**Covers:**``, ``**Informed by:**``
    (observed on real specs during pre-release calibration). Stripping only
    Trigger/Status/Date left those lines as the "first paragraph" in any doc with no real
    Decision/Design section — the raw ``**Related:** [...]`` markup became ``choice``
    verbatim. This only changes what counts as "prose" for the fallback scan; the
    QUALIFYING gate (:data:`_BOLD_META_RE`, Trigger/Status only) is untouched.
    """
    strip_indices: set[int] = set()
    for _, _, start, end in _bold_lead_blocks(lines, _BOLD_ANY_RE):
        strip_indices.update(range(start, end))
    return [ln for i, ln in enumerate(lines) if i not in strip_indices]


def _parse_structure(
    text: str,
) -> tuple[str | None, list[tuple[str, str]], str, dict[str, str]]:
    """``(h1_text, sections, first_paragraph_after_h1, bold_metadata)``. ``h1_text`` is
    ``None`` when no H1 exists at all (freeform doc by default — see E1 below for the one
    profile-gated exception). ``sections`` is ``[(heading_text, body_text), ...]`` for
    every H2/H3 (and any further H1) heading found AFTER the H1 — or, when there is no H1
    at all, every H1/H2/H3 heading in the WHOLE document (E1, design note §5: a
    ``title_pattern``-synthesized title stands in for a missing H1, so the sections it
    would have introduced must still be visible; :func:`_split_section_children` already
    reads the same "no H1 -> start of file" convention independently, via its own
    ``h1_idx = -1`` default). A section's body runs to the next heading at or above ITS
    OWN level, so a deeper nested heading (H3 under an H2) is folded INTO its parent's
    body rather than truncating it to empty — this project's own spec style routinely puts
    an umbrella "## Design" heading directly above a run of "### 1. ...", "### 2. ..."
    numbered subsections with no prose of its own between the H2 and its first H3 child;
    without this rule, ``choice`` extraction (which keys off a "## Design"/"##
    Decision(s)" match) would see an empty body for exactly that heading and silently fall
    through to the wrong fallback. Each H3 still appears as its OWN entry too (its body
    stops at the next heading of any level <=3, i.e. unchanged for H3s specifically) — so a
    keyword match against a nested H3 heading still works. ``bold_metadata`` is extracted
    from the same "lead" lines ``first_paragraph`` comes from (see
    :func:`_extract_bold_metadata`)."""
    lines = text.split("\n")
    headings = _heading_positions(lines)
    h1 = next(((i, t) for i, lvl, t in headings if lvl == 1), None)
    h1_idx, h1_text = h1 if h1 is not None else (-1, None)
    rest = [(i, lvl, t) for i, lvl, t in headings if i > h1_idx]

    lead_end = rest[0][0] if rest else len(lines)
    lead_lines = lines[h1_idx + 1 : lead_end]
    bold_metadata = _extract_bold_metadata(lead_lines)
    # The "first paragraph" fallback means actual prose, not the bold metadata block those
    # lines sit right above (design refinement (a): a doc with only bold **Status:**/
    # **Date:** lines and a real opening paragraph must fall back to THAT paragraph, not to
    # "**Date:** 2026-... **Status:** Approved" verbatim) — strip EVERY bold lead line (any
    # key, with its wrapped continuation), not just the qualifying Trigger/Status/Date ones
    # (design refinement (b) — see :func:`_strip_bold_lead_lines`), before scanning for the
    # first non-blank prose block.
    prose_lead_lines = _strip_bold_lead_lines(lead_lines)
    first_paragraph = _first_paragraph(prose_lead_lines)

    sections: list[tuple[str, str]] = []
    for idx, (pos, lvl, heading_text) in enumerate(rest):
        body_start = pos + 1
        body_end = len(lines)
        for later_pos, later_lvl, _ in rest[idx + 1 :]:
            if later_lvl <= lvl:
                body_end = later_pos
                break
        sections.append((heading_text, "\n".join(lines[body_start:body_end]).strip()))

    return h1_text, sections, first_paragraph, bold_metadata


# Numbered section headings ("## 1. Context", "## 6.1 Security / privacy",
# "## IV. Alternatives") are endemic to arc42-style templates — genkovich's sad.md numbers every
# section ("1. Introduction and goals" ... "11. Risks and technical debt"). spec.md numbers its
# sections the same way upstream, but spec.md is a requirements document that R3 excludes from
# ingestion (see profiles.py), so sad.md alone is what this strip exists to read today. The
# keyword match is a PREFIX match, so "context" does not match "1. Context" and every section of
# such a document is invisible. Strip the enumerator before matching only; the heading text
# callers see is unchanged.
# Decimal form: a single level must carry its dot ("1. Context"), so a heading that merely
# STARTS with a year or count ("2024 in review") is untouched; a multi-level enumerator needs
# no trailing dot, because real templates write "6.1 Security / privacy" and "2.1 Jobs To Be
# Done" without one. The roman form always requires the dot, so "Impact" can never be read as
# "I" + "mpact".
_ENUMERATOR_RE = re.compile(r"^\s*(?:\d+(?:\.\d+)+\.?|\d+\.|[IVXLCDM]+\.)\s+")


def _heading_has_keyword(heading: str, keyword: str) -> bool:
    """Case-insensitive PREFIX match: ``heading`` must START WITH ``keyword``, after
    stripping a leading numeric/roman enumerator (see :data:`_ENUMERATOR_RE`) — for this
    comparison only; the heading text stored and returned everywhere else is untouched (see
    the module-level comment above :data:`_CONTEXT_HEADINGS` for why prefix, not substring
    or exact)."""
    normalized = _ENUMERATOR_RE.sub("", heading.strip())
    return normalized.lower().startswith(keyword.lower())


def _matches_any(heading: str, keywords: tuple[str, ...]) -> bool:
    return any(_heading_has_keyword(heading, kw) for kw in keywords)


def _first_matching_section(
    sections: list[tuple[str, str]], keywords: tuple[str, ...]
) -> str | None:
    """The body of the first section (by KEYWORD priority, then document order) whose
    heading starts with one of ``keywords`` — implements "first of [A, B]" from design §1:
    every section is checked against the higher-priority keyword before any section is
    checked against the next one."""
    for kw in keywords:
        for heading, body in sections:
            if _heading_has_keyword(heading, kw):
                return body
    return None


_WHITESPACE_RE = re.compile(r"\s")


def _last_whitespace_index(s: str) -> int:
    """Index of the LAST whitespace character (any of ``\\s`` — space, tab, newline, ...)
    in ``s``, or ``-1`` when none exists. Review follow-up (Minor 3): a plain
    ``s.rfind(" ")`` only finds a literal ASCII space, so text whose sole whitespace near a
    cut point is a newline (no space at all — e.g. a section with no blank-line-separated
    prose, just wrapped lines) fell through to the hard-cut fallback and amputated mid-word
    anyway."""
    idx = -1
    for m in _WHITESPACE_RE.finditer(s):
        idx = m.start()
    return idx


def _truncate_section(text: str, limit: int) -> str:
    """Cap ``text`` (already redacted+stripped) at ``limit`` chars, cutting at a word
    boundary and appending :data:`_TRUNCATION_MARKER` when it's actually cut (fix-wave B,
    B3) — never a silent mid-word amputation ("Secu", "over-en", "deadli" were all observed
    against a real ADR corpus at the old 600-char cap). Falls back to a hard cut at
    ``limit`` only when there is no whitespace at all in the first ``limit`` chars (a single
    very long token, e.g. a URL)."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    last_ws = _last_whitespace_index(cut)
    if last_ws > 0:
        cut = cut[:last_ws]
    return cut.rstrip() + _TRUNCATION_MARKER


def _deepest_choice_fallback(sections: list[tuple[str, str]]) -> str:
    """Design refinement (c): when a doc's lead block is ALL bold metadata (no real prose
    paragraph survives :func:`_strip_bold_lead_lines`) AND no Decision/Design section
    exists either, ``choice`` would otherwise come back empty — the first-paragraph
    fallback and the keyword-matched-section fallback both missed. Fall through one level
    deeper: the first non-empty body of ANY section, in document order, regardless of
    whether its heading matches a known keyword (real examples: a "## Purpose" section, a
    "## Research conclusions..." section — neither matches a known keyword). Returns
    ``""`` (never ``None``) when every section body is empty too — the caller treats that as
    unparseable."""
    for _, body in sections:
        if body.strip():
            return body
    return ""


def _synthesize_title(rel_path: str, title_pattern: str | None) -> str | None:
    """E1 (design note §5): a fallback title for a doc with no H1, from ``title_pattern``'s
    named ``name``/``doc`` groups matched against ``rel_path`` — ``f"{name} ({doc})"``.
    ``None`` when no pattern was supplied (every profile's default) or it doesn't match
    ``rel_path`` — the caller then rejects the doc exactly as it did before E1 existed; the
    pattern doubles as this fallback's own scope guard, so a profile can never synthesize a
    title for a path outside what it actually ingests."""
    if title_pattern is None:
        return None
    match = re.search(title_pattern, rel_path)
    if match is None:
        return None
    return f"{match.group('name')} ({match.group('doc')})"


def _choice_is_context_echo(context: str, choice: str, rel_path: str) -> bool:
    """E3 rule 1 (design note §7, review Major 4): true when a split-produced parent's
    ``choice`` is nothing but its own ``context`` echoed back — the exact
    predicate ``bootstrap/planner.py``'s ``_choice_is_context_fallback`` already applies to
    every bootstrap candidate (``planner.py:88-91``), duplicated here rather than imported
    (core must never import the bootstrap seam — see ``test_flow_profile.py``'s
    portable-core contract tests) so both write paths agree on the same records. ``context``
    is the field's FINAL stamped value (already carrying the trailing ``"imported from
    <rel_path>"`` suffix); it is stripped back off before comparing."""
    suffix = f"\n\nimported from {rel_path}"
    stripped_context = context.removesuffix(suffix).strip()
    return bool(stripped_context) and choice.strip() == stripped_context


def _split_section_children(
    lines: list[str], dialect: ReaderDialect
) -> tuple[str, str, list[tuple[str, str]]] | None:
    """The split target and its H3 children (oneshot+granularity spec D2, review F1).

    Selection is DOC-ORDER-first (unlike `_first_matching_section`'s keyword-priority
    rule — a split target is a location, not a ranked field source): the first heading
    AFTER the H1, of any recognized level LEVEL <= 2 (E4, design note §7, review Major
    N2 — a level-3 prefix match is a leaf, never a container of its own children, so it
    is skipped and the scan continues to a possible later level-<=2 match; `_HEADING_RE`
    treats nothing deeper than H3 as a heading at all, so an `####` between the split
    heading and its first H3 folds into the intro), whose text prefix-matches a
    `split_choice` keyword. Children are headings of level 3 whose line index lies
    strictly between the split heading's line and the next heading of level <= 2 (or
    EOF) — an H3 under any OTHER H2 is never a child (the naive positional reading
    manufactured spurious children; T16 pins the negative). Each child's body runs to
    the next recognized heading, matching `_parse_structure`'s own H3 body rule. Returns
    ``(split_heading_text, intro_text_before_first_child, children)`` or ``None`` when
    `split_choice` is empty or nothing matches.
    """
    if not dialect.split_choice:
        return None
    headings = _heading_positions(lines)
    # Candidates start AFTER the first H1 (code review F14): `_parse_structure`'s
    # sections exclude the H1 and anything above it, and "the first section" must mean
    # the same population here — an H1 that happens to prefix-match a split keyword is
    # a title, not a split target.
    h1_idx = next((i for i, lvl, _ in headings if lvl == 1), -1)
    split = next(
        (
            (i, lvl, text)
            for i, lvl, text in headings
            # E4: level <= 2 only — a level-3 match is skipped, not a stop (skip-and-
            # continue, round-3 Info); the scan keeps looking for a later level-<=2 match.
            if i > h1_idx and lvl <= 2 and _matches_any(text, dialect.split_choice)
        ),
        None,
    )
    if split is None:
        return None
    split_idx, _split_lvl, split_text = split
    after = [(i, lvl, t) for i, lvl, t in headings if i > split_idx]
    section_end = next((i for i, lvl, _ in after if lvl <= 2), len(lines))
    children: list[tuple[str, str]] = []
    for pos, (i, lvl, text) in enumerate(after):
        # Containment is guarded TWICE, and each guard is independently sufficient (code
        # review F10): the `i >= section_end` skip here, and the `min(..., section_end)`
        # clamp below (which empties an out-of-section body so the empty-choice rule
        # drops the child). Do not delete one believing T16 covers it — T16 bites only
        # against the fully naive reading with neither guard.
        if i >= section_end or lvl != 3:
            continue
        body_end = next((j for j, _, _ in after[pos + 1 :] if j > i), len(lines))
        body_end = min(body_end, section_end)
        children.append((text, "\n".join(lines[i + 1 : body_end]).strip()))
    intro_end = min((i for i, _, _ in after), default=len(lines))
    intro = "\n".join(lines[split_idx + 1 : intro_end]).strip()
    return split_text, intro, children


def _parse_decision_doc_with_reason(
    path_text: str,
    rel_path: str,
    section_limit: int = _SECTION_LIMIT,
    *,
    dialect: ReaderDialect = GENERIC_ADR_DIALECT,
    title_pattern: str | None = None,
) -> tuple[ParsedDoc | None, str | None]:
    """Single-doc compatibility wrapper over :func:`_parse_decision_docs_with_reason` —
    returns the PARENT record only. With a split-capable dialect the parent's `choice` is
    the first matching NON-split section (spec F6: the exclusion lives in the parser, and
    `parse_decision_doc`'s contract changes for split profiles by design — the AD content
    moves to the child records the plural API returns)."""
    docs, reason = _parse_decision_docs_with_reason(
        path_text, rel_path, section_limit, dialect=dialect, title_pattern=title_pattern
    )
    return (docs[0] if docs else None), reason


def _parse_decision_docs_with_reason(
    path_text: str,
    rel_path: str,
    section_limit: int = _SECTION_LIMIT,
    *,
    dialect: ReaderDialect = GENERIC_ADR_DIALECT,
    title_pattern: str | None = None,
) -> tuple[list[ParsedDoc], str | None]:
    """Core of :func:`parse_decision_doc`, plus a skip ``reason`` for the caller
    (``import_docs``) to count separately: ``None`` when the doc simply isn't
    decision-shaped (no H1, or no qualifying section/bold-metadata signal at all);
    ``"template"`` (BUG G) when it is a document TEMPLATE (a "template" filename, a
    ``type: template`` frontmatter field, or a placeholder-dominated Context/Decision body —
    see :func:`_is_template_doc`/:func:`_placeholder_dominated`), short-circuited before any
    status handling so a template's own ``**Status:** APPROVED`` can never drive acceptance;
    ``"unparseable"`` when it IS decision-shaped but every ``choice`` source — the known
    Decision/Design section, the first prose paragraph, AND the deepest any-section
    fallback (:func:`_deepest_choice_fallback`) — came back empty. A Decision is never
    written with an empty ``choice`` (design refinement (c)).

    ``section_limit`` (fix-wave B, B3) is the per-field cap applied to ``context``/
    ``choice``/``rejected``/``consequences`` AFTER redaction, at a word boundary (see
    :func:`_truncate_section`) — the caller (``import_docs``/the CLI's ``--section-limit``)
    controls it; this function trusts whatever it's given.
    """
    body, fm_status = _strip_frontmatter(path_text)
    fm_type = _frontmatter_type(path_text)
    lines = body.split("\n")
    h1_text, sections, first_paragraph, bold_meta = _parse_structure(body)
    if h1_text is None:
        # E1 (design note §5): a `title_pattern` match synthesizes a title in place of the
        # missing H1 and parsing proceeds exactly as if that were the H1 — no match (or no
        # pattern at all, every profile's default) rejects exactly as before E1 existed.
        h1_text = _synthesize_title(rel_path, title_pattern)
        if h1_text is None:
            return [], None

    # Oneshot+granularity spec D2: computed BEFORE the placeholder probe so probe and
    # extraction see the same (excluded) choice pool (review F6). The exclusion is
    # conditional on >= 1 child (review F7): a split keyword whose section has no H3s is
    # ordinary choice material, byte-identical to a dialect without split_choice.
    split = _split_section_children(lines, dialect)
    split_children: list[tuple[str, str]] = []
    split_intro = ""
    if split is not None and split[2]:
        split_heading, split_intro, split_children = split
        # Exclusion is keyed on (heading, body) PAIRS, not heading text alone (code
        # review F3): a duplicate heading text elsewhere in the doc — a second section
        # named like the split target, or a child H3 named like a later real H2 — must
        # not knock an unrelated section out of the parent's choice pool. The split
        # section's own entry is identified by its heading plus the folded body
        # _parse_structure gives it; a same-named section with different content stays.
        split_body = next((b for h, b in sections if h == split_heading), None)
        excluded_pairs = {(split_heading, split_body)} | set(split_children)
        choice_sections = [(h, b) for h, b in sections if (h, b) not in excluded_pairs]
    else:
        choice_sections = sections
    # BUG G: a document TEMPLATE (skeleton to copy/fill) is never a decision — and a
    # template's own `**Status:** APPROVED` must never drive acceptance, so this
    # short-circuits BEFORE any status handling (and before the H1-placeholder guard, so a
    # `*template*.md` file is counted as "template", not "not-decision-shaped"). The
    # filename/`type: template` tells are near-zero-false-positive; the placeholder-body
    # tell below catches a renamed copy the first two miss.
    if _is_template_doc(rel_path, fm_type):
        return [], "template"
    if _PLACEHOLDER_RE.search(_BACKTICK_SPAN_RE.sub("", h1_text)):
        return [], None  # blank template, not a record — see _PLACEHOLDER_RE
    known_headings = dialect.all_headings()
    has_known_section = any(_matches_any(h, known_headings) for h, _ in sections)
    has_qualifying_bold_meta = "trigger" in bold_meta or "status" in bold_meta
    if not has_known_section and not has_qualifying_bold_meta:
        return [], None

    # BUG G: a decision-shaped doc whose Context AND Decision sections are still mostly
    # placeholder stubs is a blank-template copy the filename/frontmatter tells missed. Probe
    # the same section sources the extraction below uses, pre-redact (redaction can never
    # introduce placeholder markup, so raw is the honest signal).
    context_probe = _first_matching_section(sections, dialect.context) or bold_meta.get("trigger")
    choice_probe = (
        _first_matching_section(choice_sections, dialect.choice) or split_intro or first_paragraph
    )
    if _placeholder_dominated(f"{context_probe or ''}\n{choice_probe or ''}"):
        return [], "template"

    title_clean, _ = redact(h1_text)
    title = title_clean.strip()[:_TITLE_LIMIT]

    context_body = _first_matching_section(sections, dialect.context) or bold_meta.get("trigger")
    if context_body:
        clean, _ = redact(context_body)
        clean = _truncate_section(clean.strip(), section_limit)
        context = f"{clean}\n\nimported from {rel_path}" if clean else f"imported from {rel_path}"
    else:
        context = f"imported from {rel_path}"

    choice_body = (
        _first_matching_section(choice_sections, dialect.choice) or split_intro or first_paragraph
    )
    choice_clean, _ = redact(choice_body or "")
    choice = _truncate_section(choice_clean.strip(), section_limit)
    if not choice:
        choice_clean, _ = redact(_deepest_choice_fallback(choice_sections))
        choice = _truncate_section(choice_clean.strip(), section_limit)
    # E3 rule 2 (design note §7, review Blocker N1): this early return used to fire BEFORE
    # children were built, so a split with N children yielded ZERO records whenever the
    # parent's own choice fell through every fallback to empty — 22 real child decisions
    # silently discarded across 4 archived files on the calibration corpus. When a split
    # produced children, they stand alone instead (the parent is simply never built below —
    # `choice` stays falsy, so the `if choice:` guard around `parent = ParsedDoc(...)`
    # skips it, and the caller counts it via `skipped_degenerate_parent`); a doc with NO
    # split children keeps today's behavior exactly.
    if not choice and not split_children:
        return [], "unparseable"

    # `rejected`/`consequences` (fix-wave B, B2): a real heading match wins when present;
    # calibrated against a real ADR corpus where NONE of 8 imported ADRs had a literal
    # "## Rejected" heading, so falling straight through to "None, nothing captured" (the
    # pre-B2 behavior) left the schema's headline field null on every record. The bold
    # pseudo-heading fallback (see :data:`_BOLD_PSEUDO_REJECTED_RE`/
    # :func:`_bold_pseudo_heading_blocks`) recovers what that corpus's prose actually marks
    # up; multiple matches (a doc can carry more than one "**Rejected...**" block) are
    # joined, not just the first.
    rejected_body = _first_matching_section(sections, dialect.rejected)
    if not rejected_body or not rejected_body.strip():
        pseudo = _bold_pseudo_heading_blocks(lines, _BOLD_PSEUDO_REJECTED_RE)
        rejected_body = "\n\n".join(pseudo) if pseudo else None
    rejected: str | None = None
    if rejected_body and rejected_body.strip():
        rejected_clean, _ = redact(rejected_body)
        rejected = _truncate_section(rejected_clean.strip(), section_limit) or None

    consequences_body = _first_matching_section(sections, dialect.consequences)
    if not consequences_body or not consequences_body.strip():
        pseudo = _bold_pseudo_heading_blocks(lines, _BOLD_PSEUDO_CONSEQUENCES_RE)
        consequences_body = "\n\n".join(pseudo) if pseudo else None
    consequences: str | None = None
    if consequences_body and consequences_body.strip():
        consequences_clean, _ = redact(consequences_body)
        consequences = _truncate_section(consequences_clean.strip(), section_limit) or None

    frontmatter_status = fm_status if fm_status is not None else bold_meta.get("status")

    # B4: a single conservative kind signal — a doc whose qualifying sections include
    # "Root cause" reads as a lesson (a root-cause doc IS a lesson), never overriding an
    # explicit `--kind` (the caller's job — see ParsedDoc's docstring).
    suggested_kind = (
        DecisionKind.LESSON
        if any(_heading_has_keyword(h, "root cause") for h, _ in sections)
        else None
    )

    # E3 rule 2's degenerate-empty-shape case reaches here with `choice == ""` (the early
    # "unparseable" return above was bypassed because `split_children` is non-empty) — the
    # parent is simply never built; `import_docs` infers the omission (no record with
    # `fragment is None` in the returned list) and counts `skipped_degenerate_parent`.
    parent = (
        ParsedDoc(
            title=title,
            context=context,
            choice=choice,
            rejected=rejected,
            consequences=consequences,
            frontmatter_status=frontmatter_status,
            suggested_kind=suggested_kind,
        )
        if choice
        else None
    )

    # Children (spec D2): one ParsedDoc per H3 under the split section. Fields: title =
    # the H3 heading; choice = its body; context = the SAME matched context section the
    # parent uses, suffixed with the child's EFFECTIVE ref (review F13 — an agent reading
    # the record can find the exact section); rejected/consequences stay on the parent
    # (doc-level sections, not duplicated N times). Fragments: `schema.slugify` (the
    # existing slugifier — review F8), collisions get `-2`, `-3`, … in doc order, an
    # empty slug falls back to `section-<n>`. A child whose body redacts/truncates to
    # empty is not emitted (a Decision is never written with an empty choice — same rule
    # as refinement (c) above).
    children_docs: list[ParsedDoc] = []
    if split_children:
        raw_context_clean = ""
        if context_body:
            clean, _ = redact(context_body)
            raw_context_clean = _truncate_section(clean.strip(), section_limit)
        # Uniqueness is a POST-CONDITION over the fragments actually emitted, not an
        # inference from a per-base counter (code review F1, BLOCKING): a heading whose
        # slug is naturally `<base>-2` collides with the second occurrence of `<base>`
        # under counting — two children then share one effective ref and the import loop
        # "supersedes" one AD with its sibling on a fresh store, flip-flopping every run
        # (measured: total 4→6→8 across three imports of an unchanged file). The loop
        # below advances the ordinal until the fragment is genuinely free, which also
        # closes the same hole for the `section-<n>` fallback.
        used_fragments: set[str] = set()
        for n, (child_heading, child_body) in enumerate(split_children, start=1):
            child_choice_clean, _ = redact(child_body)
            child_choice = _truncate_section(child_choice_clean.strip(), section_limit)
            if not child_choice:
                continue
            child_title_clean, _ = redact(child_heading)
            base = slugify(child_title_clean) or f"section-{n}"
            fragment, k = base, 1
            while fragment in used_fragments:
                k += 1
                fragment = f"{base}-{k}"
            used_fragments.add(fragment)
            effective_ref = f"{rel_path}#{fragment}"
            child_context = (
                f"{raw_context_clean}\n\nimported from {effective_ref}"
                if raw_context_clean
                else f"imported from {effective_ref}"
            )
            children_docs.append(
                ParsedDoc(
                    title=child_title_clean.strip()[:_TITLE_LIMIT],
                    context=child_context,
                    choice=child_choice,
                    rejected=None,
                    consequences=None,
                    frontmatter_status=frontmatter_status,
                    suggested_kind=suggested_kind,
                    fragment=fragment,
                )
            )

    records = ([parent] if parent is not None else []) + children_docs
    if not records:
        # Every split child ALSO redacted/truncated to empty (an extreme edge case — the
        # parent's own choice already had to be empty to reach this branch) — nothing
        # usable came out of this doc at all, same "unparseable" reason a non-split doc
        # gets for the identical outcome.
        return [], "unparseable"
    return records, None


def parse_decision_docs(
    text: str,
    rel_path: str,
    section_limit: int = _SECTION_LIMIT,
    *,
    dialect: ReaderDialect = GENERIC_ADR_DIALECT,
    title_pattern: str | None = None,
) -> tuple[list[ParsedDoc], str | None]:
    return _parse_decision_docs_with_reason(
        text, rel_path, section_limit=section_limit, dialect=dialect, title_pattern=title_pattern
    )


def parse_decision_doc(
    path_text: str,
    rel_path: str,
    section_limit: int = _SECTION_LIMIT,
    *,
    dialect: ReaderDialect = GENERIC_ADR_DIALECT,
    title_pattern: str | None = None,
) -> ParsedDoc | None:
    """Parse one markdown file into a :class:`ParsedDoc`, or ``None`` when it is not
    decision-shaped (design §1, refined: needs an H1 title AND — at least one known
    decision-section heading (prefix match) OR a bold ``**Trigger:**``/``**Status:**``
    metadata line right after the H1 (see the adjudication note above
    :data:`_BOLD_META_RE` — a bold ``**Date:**`` line alone is NOT a qualifying signal);
    freeform docs, and task-plan/README/module-doc files whose only headings are things
    like "## Tasks" or "## Purpose"/"## Architecture", are never imported) OR its
    ``choice`` would come back empty even after every fallback (see
    :func:`_parse_decision_doc_with_reason` — ``import_docs`` distinguishes this
    ``"unparseable"`` case from a plain not-decision-shaped skip via its own ``reason``
    counter; this public wrapper collapses both to ``None`` for callers that only care
    whether a usable :class:`ParsedDoc` came out).

    ``path_text`` is the RAW file content (not pre-redacted — every field this function
    derives is redacted internally, redact-then-truncate order, mirroring
    ``importer._make_title``'s rationale: truncating a raw secret first can leave a
    partial, non-matching fragment that a later redact pass would miss). ``rel_path`` is
    the doc's path as it should be stamped into ``context`` and (by the caller)
    ``provenance.ref`` — never redacted (a path is not a secret). ``section_limit``
    (fix-wave B, B3; default :data:`_SECTION_LIMIT`, 2000) is the per-field char cap applied
    to ``context``/``choice``/``rejected``/``consequences`` AFTER redaction, at a word
    boundary with a trailing :data:`_TRUNCATION_MARKER` when actually cut — never the old
    silent mid-word cut.

    Field mapping (the PARENT record — with a ``split_choice`` dialect this function
    returns only the parent; the per-H3 child records come from
    :func:`_parse_decision_docs_with_reason`):
    - ``title`` = H1 text, redacted, truncated to :data:`_TITLE_LIMIT` chars (plain char
      slice — a title is a lead-in, not recorded reasoning, so it gets no word-boundary/
      marker treatment). E1: when the doc has no H1 at all, ``title_pattern`` — a regex
      with named ``name``/``doc`` groups, matched against ``rel_path`` — supplies a
      synthesized ``"{name} ({doc})"`` title instead; ``None`` (the default, byte-identical
      to before E1 existed) or no match rejects the doc exactly as a missing H1 always has.
    - ``context`` = first of [Context, Trigger, Root cause] section text (redacted,
      truncated to ``section_limit``) + ``"\\n\\nimported from <rel_path>"``; when
      none of those sections exist, falls back to a bold ``**Trigger:**`` metadata line
      (continuation lines included — see :func:`_bold_lead_blocks`); when that's absent
      too, ``context`` is just the "imported from" line (context is a required field —
      never empty).
    - ``choice`` = first of [Decision Outcome, Decisions, Decision, Design] section text;
      when none exist, the first prose paragraph right after the H1 (with ALL bold lead
      lines stripped, any key — see :func:`_strip_bold_lead_lines`); when that's empty too,
      the first non-empty body of ANY section (:func:`_deepest_choice_fallback`). Redacted,
      truncated. Never empty: a doc that still comes back with nothing usable is skipped
      entirely (see :func:`_parse_decision_doc_with_reason`).
    - ``rejected`` = first of [Rejected, Alternatives, "Considered alternatives",
      "Considered options", "Options considered"] section text; when no such REAL heading
      exists, falls back to bold pseudo-heading blocks (fix-wave B, B2 — e.g. a bare
      ``"**Rejected — ..."`` paragraph or ``"- **Rejected (antipattern) — ..."`` list item,
      see :data:`_BOLD_PSEUDO_REJECTED_RE`), joined when more than one exists. Redacted,
      truncated; ``None`` when nothing was found either way.
    - ``consequences`` = first of [Consequences] section text, same bold pseudo-heading
      fallback as ``rejected`` when no real heading exists (fix-wave B, B2 — this field was
      never populated by the mapping before B2). Redacted, truncated; ``None`` when absent.
    - ``frontmatter_status`` = the raw value of a leading YAML-frontmatter ``status:`` key,
      or (when that's absent) a bold ``**Status:**`` metadata line — the caller
      (``import_docs``) decides whether "superseded"/"deprecated" in it means "skip, this
      is a history doc" (design §1's kind/status bullet), and (fix-wave B, B1) whether a
      draft/proposed/pending/under-review-like value means the imported decision must land
      ``proposed`` regardless of ``--propose``.
    - ``suggested_kind`` — see :class:`ParsedDoc`'s docstring (fix-wave B, B4).

    ``kind`` is NOT part of this return value's APPLIED kind: ``suggested_kind`` is only a
    signal; the run's own ``--kind``, when explicitly given, always overrides it — applied
    by the caller when constructing the :class:`~sidegraph.schema.Decision`.
    """
    parsed, _ = _parse_decision_doc_with_reason(
        path_text,
        rel_path,
        section_limit=section_limit,
        dialect=dialect,
        title_pattern=title_pattern,
    )
    return parsed


def _is_historical_status(status: str | None) -> bool:
    """True when a frontmatter ``status:`` value marks the doc as history (design §1:
    "history docs are not re-imported as live"). Substring, case-insensitive — covers
    "Superseded", "superseded by ADR-9999", "Deprecated in favor of...", etc."""
    if not status:
        return False
    s = status.lower()
    return "superseded" in s or "deprecated" in s


# Fix-wave B, B1: substrings (case-insensitive) that mark a frontmatter/bold `status` value
# as "not yet ratified" — calibrated against a real ADR corpus where every source ADR
# carries `status: draft` (YAML frontmatter) alongside a `status_note` like "proposed,
# pending EACL review". The recommended `--docs` import path was landing all of these
# `accepted` regardless (a real distortion: an agent querying the store saw ratified
# decisions where the corpus says draft-pending-review).
_DRAFT_STATUS_MARKERS = ("draft", "propos", "pending", "under review")


# A doc the team TURNED DOWN. Deliberately NOT folded into _DRAFT_STATUS_MARKERS
# (v0.2-scope item 7 says "add the marker", and that is the trap): a rejected ADR is not
# "proposed, awaiting review" — labelling it so would push a settled no into the human
# ratification queue and invite someone to accept what was already refused. It is also not
# history to skip (_is_historical_status): a rejected proposal, with its reasons, is
# exactly the "tried before, abandoned because…" the store exists to keep. It lands
# `rejected` — closed, retrievable, truthfully labelled.
# Deliberately NOT a bare substring, unlike _DRAFT_STATUS_MARKERS. The two are not
# symmetric in consequence (review finding 3): a false `proposed` lands in the human
# ratification queue where someone sees and fixes it, while a false `rejected` lands
# CLOSED — invisible to retrieval, so nobody ever notices the record that quietly stopped
# being memory. Measured false positives on the bare substring: "Accepted (rejected
# alternative: gRPC)", "not rejected", "rejection criteria defined" all landed rejected.
_REJECTED_WORD_RE = re.compile(r"(?<!\w)(?<!not )(?<!un-)(?<!un)reject(?:ed)?(?!\w)")
_PARENTHETICAL_RE = re.compile(r"\([^)]*\)")


def _is_rejected_status(status: str | None) -> bool:
    """True when a frontmatter/bold ``status`` value reads as TURNED DOWN.

    Word-boundary match on ``reject``/``rejected``, after parenthesised asides are
    stripped and with ``not``/``un`` negations excluded. So:

    - ``rejected``, ``Rejected in favour of ADR-9999``, ``proposed, then rejected`` → True
    - ``Accepted (rejected alternative: gRPC)`` → False (the aside is about an option)
    - ``not rejected``, ``un-rejected`` → False
    - ``rejection criteria defined`` → False (``rejection`` is a different word)

    Checked BEFORE :func:`_is_draft_like_status`: a value carrying both readings is
    terminal, and the terminal one wins."""
    if not status:
        return False
    return bool(_REJECTED_WORD_RE.search(_PARENTHETICAL_RE.sub(" ", status.lower())))


def _is_draft_like_status(status: str | None) -> bool:
    """True when a frontmatter/bold ``status`` value reads as not-yet-ratified (see
    :data:`_DRAFT_STATUS_MARKERS`). The caller (``import_docs``) uses this to force the
    imported decision ``proposed`` REGARDLESS of ``--propose`` — an explicit
    accepted/approved value, or an absent/unrecognized one, takes the normal
    ``--propose``-controlled default path unchanged (design: B1's override exists only to
    stop a genuine distortion, not to second-guess every status string)."""
    if not status:
        return False
    s = status.lower()
    return any(marker in s for marker in _DRAFT_STATUS_MARKERS)


# -- mention anchors (design §2) ----------------------------------------------------------


def _is_path_like(token: str) -> bool:
    """A real path never contains whitespace — a backticked prose phrase can still contain
    a "/" (e.g. `` `see the docs/ folder for details` ``) or end in what looks like an
    extension by coincidence; rejecting any token with whitespace up front kills that false
    positive before the "/" / extension checks ever run."""
    if any(ch.isspace() for ch in token):
        return False
    if "/" in token:
        return True
    lowered = token.lower()
    return lowered.endswith(_SOURCE_EXTENSIONS)


def _is_identifier_like(token: str) -> bool:
    """CamelCase or snake_case, >=4 chars (design §2). A single ordinary word (no case
    transition, no underscore) never matches either pattern, which is exactly what "not a
    bare common word" requires — no separate stopword list needed."""
    if len(token) < 4:
        return False
    return bool(_SNAKE_RE.match(token) or _CAMEL_RE.match(token))


def _classify_token(token: str) -> str | None:
    """``"path" | "identifier" | None`` — path-like takes priority per design §2's
    ordering (a token like ``anchor_binding.py`` is a path, not an identifier)."""
    if _is_path_like(token):
        return "path"
    if _is_identifier_like(token):
        return "identifier"
    return None


def extract_mention_tokens(text: str) -> list[str]:
    """Backtick-quoted tokens from ``text``, filtered to path-like/identifier-like
    (design §2 — a token that is neither, e.g. plain prose emphasis, is dropped outright),
    deduped, and frequency-ranked (most-mentioned first; ties keep first-appearance order,
    since ``sorted`` is stable and tokens are collected in that order to begin with).

    Pure tokenizer — no graph dependency: it never decides whether a token actually
    resolves against the graph (that is ``import_docs``'s job, via ``reader``). Callers
    that care about the "redaction before any use" invariant (design §1) must pass
    already-redacted ``text``; this function does not redact.
    """
    counts: dict[str, int] = {}
    order: list[str] = []
    for m in _BACKTICK_RE.finditer(text):
        token = m.group(1).strip()
        if not token or _classify_token(token) is None:
            continue
        if token not in counts:
            order.append(token)
            counts[token] = 0
        counts[token] += 1
    return sorted(order, key=lambda t: -counts[t])


def _file_node_descriptor(path: str, reader: GraphifyReader) -> Descriptor | None:
    """The exact file-level node for ``path`` — never guesses a member: only a node whose
    label equals the path's basename counts, and it must independently confirm
    ``resolved`` via ``reader.resolve`` (not just "a node with this name exists somewhere
    in this file" — never bind an anchor whose resolution isn't itself confirmed clean).
    Shared by the path-like mention branch and the doc's own always-added file-node anchor
    (design §2's "Plus always" bullet) — both are literally the same lookup.
    """
    nodes = reader.nodes_in_file(path)
    if not nodes:
        return None
    basename = path.rsplit("/", 1)[-1]
    match = next((n for n in nodes if n.name == basename), None)
    if match is None:
        return None
    ref = Descriptor(name=match.name, file_path=path)
    if reader.resolve(ref).status != "resolved":
        return None
    return ref


def _select_mention_anchors(
    text: str, reader: GraphifyReader
) -> tuple[list[Descriptor], list[dict]]:
    """Frequency-ranked mention tokens -> up to :data:`_MAX_MENTION_ANCHORS` resolved
    anchors (design §2). Never guesses: a path-like token with no matching file node, or
    an identifier that resolves ambiguous/unresolved, is reported in the second return
    value (``[{"name", "reason", "candidates"}, ...]`` — reuses the
    ``anchoring.AnchorResolution`` shape for identifiers; ``"unresolved"``/``[]`` for a
    path-like miss) and simply doesn't count toward the cap — scanning continues to the
    next-ranked token rather than stopping at the first miss.
    """
    anchors: list[Descriptor] = []
    skipped: list[dict] = []
    for token in extract_mention_tokens(text):
        if len(anchors) >= _MAX_MENTION_ANCHORS:
            break
        if _classify_token(token) == "path":
            ref = _file_node_descriptor(token, reader)
            if ref is not None:
                anchors.append(ref)
            else:
                skipped.append({"name": token, "reason": "unresolved", "candidates": []})
        else:
            ref = Descriptor(name=token)
            result = reader.resolve(ref)
            if result.status == "resolved":
                anchors.append(ref)
            else:
                skipped.append(
                    {
                        "name": token,
                        "reason": result.status,
                        "candidates": result.candidates[:5],
                    }
                )
    return anchors, skipped


# -- import (design §3/§4) -----------------------------------------------------------------


def _collect_markdown_files(paths: Sequence[str | Path]) -> list[str]:
    """Expand ``paths`` (each a file or a directory to recurse) into a sorted, deterministic
    list of ``*.md`` file path strings. A directory contributes every ``*.md`` under it
    (recursive); a bare file is taken as-is regardless of extension (the parser itself
    rejects anything that isn't decision-shaped, so an explicit non-``.md`` file is simply
    very likely to come back unparseable — no special-cased rejection needed here). Paths
    that don't exist are silently omitted (the CLI validates existence up front so this
    only matters to direct callers of ``import_docs``)."""
    out: list[str] = []
    for p in paths:
        path = Path(p)
        if path.is_dir():
            out.extend(str(f) for f in sorted(path.rglob("*.md")))
        elif path.is_file():
            out.append(str(path))
    return out


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Translate one repo-relative ingest glob (single ``*``/``?`` wildcards, never
    crossing a ``/`` — the same semantics ``Path.cwd().glob()`` already uses for
    profile-only discovery, see ``cli._import_docs_mode``) into a fully-anchored regex
    (design D7.1).

    Neither ``fnmatch.translate`` nor ``PurePath.match`` fits: ``fnmatch``'s ``*`` crosses
    ``/`` (a doc several directories deeper than a glob intends would still match), and
    ``PurePath.match`` right-anchors instead of fully anchoring a RELATIVE pattern (a file
    under an unrelated leading directory, e.g. ``other/docs/adr/x.md``, would wrongly match
    ``docs/adr/*.md``) — this enforcement needs neither looseness.

    Raises ``ValueError`` naming the feature when ``pattern`` contains ``**`` (recursive
    descent) or a ``[seq]`` character class (code review CORRECTION-5): neither is
    supported by this translator, and silently mistranslating either — ``**`` would
    flatten to two independent single-segment ``*``s; ``[seq]`` would become a literal,
    escaped substring match via the ``else`` branch below — would diverge from real glob
    semantics with no signal beyond a mysteriously-empty import. Loud and immediate (the
    very first time the pattern is ever used) beats that.
    """
    if "**" in pattern:
        raise ValueError(f"unsupported glob feature '**' (recursive descent) in {pattern!r}")
    if "[" in pattern or "]" in pattern:
        raise ValueError(f"unsupported glob feature '[seq]' (character class) in {pattern!r}")
    parts = []
    for ch in pattern:
        if ch == "*":
            parts.append("[^/]*")
        elif ch == "?":
            parts.append("[^/]")
        else:
            parts.append(re.escape(ch))
    return re.compile("".join(parts) + r"\Z")


def _matches_ingest_globs(rel_path: str, globs: tuple[str, ...]) -> bool:
    """True iff ``rel_path`` (repo-relative) matches at least one of ``globs`` (design
    D7.1) — the per-file gate ``import_docs`` applies at enumeration, before a file
    outside the active profile's declared scope is ever opened."""
    return any(_glob_to_regex(g).fullmatch(rel_path) for g in globs)


def _is_live_tree_path(rel_path: str, profile: FlowProfile) -> bool:
    """I2 (R1 improvement wave §2) trigger predicate, shared by both write paths
    (``import_docs`` below and ``bootstrap/planner.py``'s ``plan_sources``): True iff the
    ON-DISK, repo-root-relative ``rel_path`` sits in ``profile``'s LIVE tree.

    Three clauses, all required (review Minor 5 — direct, not proxy):

    1. ``profile.ref_normalize`` is set — a profile with no archive-move convention has no
       live/archived distinction to make, so this is always ``False`` for it (every
       profile but openspec today).
    2. The on-disk ``rel_path`` does NOT already contain the archive segment
       ``ref_normalize``'s own pattern rewrites — implemented as "the pattern doesn't
       match the on-disk path" (not merely "doesn't end in a date-stamped dir"), so a
       non-dated archive dir still contains the literal archive segment and is excluded
       the same way.
    3. ``rel_path`` matches ``profile.ingest_globs`` — closes the ``--any-doc`` stray-doc
       hole: a doc reached via ``--any-doc`` from outside the profile's own tree is never
       stamped, even if it happens to dodge clause 2.
    """
    if profile.ref_normalize is None:
        return False
    pattern, _replacement = profile.ref_normalize
    if re.search(pattern, rel_path):
        return False
    return _matches_ingest_globs(rel_path, profile.ingest_globs)


def _glob_repo_root() -> Path:
    """The repo root the D7.1 import-glob gate normalizes against — resolved from the
    process's current directory the same way D5 resolves ``sidegraph-doctor``'s
    (``verify._find_repo_root``: ``git rev-parse --show-toplevel``, wrapped never-raise).
    ``ingest_globs`` are repo-root-relative patterns, never cwd-relative — a caller
    running ``import_docs`` from a subdirectory (or passing a path via a symlinked
    prefix) must still normalize against the SAME root a glob like ``docs/adr/*.md``
    means. Git missing, no repo, or any other failure all fall back to the bare (but
    still ``.resolve()``-d) cwd itself, so a caller already running from the repo root —
    the common case — behaves exactly as before this fix (BLOCKING-1, code review)."""
    try:
        return _find_repo_root(Path.cwd())
    except (ValueError, OSError):
        return Path.cwd().resolve()


def import_docs(
    store: Store,
    reader: GraphifyReader,
    paths: Sequence[str | Path],
    *,
    kind: str | None = None,
    propose: bool = False,
    dry_run: bool = False,
    limit: int | None = None,
    tags: list[str] | None = None,
    section_limit: int = _SECTION_LIMIT,
    profile: str = "generic-adr",
    any_doc: bool = False,
    ratify_policy: RatifyPolicy = RatifyPolicy.MANUAL,
) -> DocImportReport:
    """One :class:`~sidegraph.schema.Decision` per decision-shaped markdown file under
    ``paths`` (each a file or a directory recursed for ``*.md``; see design §4).

    ``kind`` (fix-wave B, B4): ``None`` (the default) means "auto" — per document,
    :attr:`ParsedDoc.suggested_kind` is used when the parser found a signal (currently: a
    qualifying "Root cause" section -> ``lesson``), else ``adr``. An explicit string (any
    valid :class:`~sidegraph.schema.DecisionKind` value, including ``"adr"``) always wins
    for every document this run, exactly like before B4 — the heuristic only fires when the
    caller didn't ask for a specific kind.

    ``section_limit`` (fix-wave B, B3, default :data:`_SECTION_LIMIT`): forwarded to
    :func:`parse_decision_doc` as the per-field word-boundary truncation cap.

    Pipeline per file: ``parse_decision_doc`` -> historical-frontmatter skip -> idempotency
    lookup (``provenance.source == "doc-import"``, ``provenance.ref == <rel_path>``, title-
    agnostic — see ``Store.find_decisions_by_ref``) -> mention-anchor resolution (only when
    about to write; a doc that's ``skipped_existing`` never touches the graph) -> write.

    **Idempotency & evolution (design §3, append-only, no schema change; pending-proposal
    dedup fix).** The idempotency check compares the fresh parse against EVERY currently
    open (``accepted`` OR ``proposed``) decision at the same ``provenance.ref`` — not just
    one of them (see ``Store.find_decisions_by_ref``'s docstring for the regression this
    fixes: comparing against only a single, scan-order-picked record let a re-run miss an
    already-pending proposal with matching content and duplicate it every run). A doc whose
    ``(title, choice, rejected)`` are byte-identical to ANY open record at that ref is
    ``skipped_existing`` — no anchor resolution even attempted for it, and nothing is
    written or touched, regardless of which of the (at most two) open records matched.

    A doc at the same ``ref`` whose parsed content differs from ALL open records (the doc
    was edited again) gets a fresh :class:`~sidegraph.schema.Decision`. Its ``supersedes``
    always points at the ACCEPTED ancestor, if one is open — never at a still-pending
    proposal — so that a chain of edit/``--propose`` cycles always resolves back to the one
    real predecessor a ratify-accept needs to close. If a proposal was still pending at
    this ref (``edit-again-while-pending``), that stale, never-ratified draft is closed
    immediately as ``rejected`` (via ``Store.drop``) as part of this same write — closing
    an unratified draft is NOT the ratify-guarded case ``Store.ratify`` exists for (nothing
    reviewed/accepted it yet), so there is nothing to defer; it simply never matched the
    new parse and is superseded-in-spirit by the new proposal/decision being written here.

    **Status-derived ``proposed`` override (fix-wave B, B1):** when the doc's own extracted
    status (YAML frontmatter ``status:`` or a bold ``**Status:**`` line) reads as
    draft/proposed/pending/under-review-like (:func:`_is_draft_like_status`), the decision
    lands ``proposed`` REGARDLESS of ``--propose`` — calibrated against a real ADR corpus
    where every source doc says ``status: draft`` / "proposed, pending EACL review" but the
    unconditional-``accepted`` default was silently promoting all of them past review. An
    explicit accepted/approved status, or an absent/unrecognized one, takes the normal
    ``--propose``-controlled default path, unchanged. ``DocImportReport.status_derived_proposed``
    counts how many decisions this run landed ``proposed`` for this reason specifically (the
    CLI prints it as its own report line).

    **``--propose`` rule (design §3, option (b) — ratify-time supersession):** ``propose``
    (or the status-derived override above — either one landing the successor ``proposed``
    has the same effect here) controls the successor's ``status`` (``proposed`` vs
    ``accepted``) same as a fresh import, AND gates WHEN the ACCEPTED ancestor (if any)
    closes. Without either (or on a doc with no accepted ancestor at all),
    ``store.add_decision`` closes the ancestor immediately in the same transaction as before
    (the same mechanism
    ``server._supersede_decision_impl`` uses) — nothing changed there. WHEN the successor
    lands ``proposed`` (``--propose`` or the status-derived override) on an edited doc, it
    carries ``supersedes=<ancestor_id>``
    but the ancestor is left exactly as it was (typically still ``accepted``) — an accepted
    decision must never be silently closed by a mere proposal that hasn't been reviewed
    yet. The ancestor only closes once a human actually ratifies the successor:
    ``Store.ratify`` closes any still-open (``accepted``/``proposed``) predecessor it
    supersedes in the same operation (valid_to + status=superseded) — this is shared,
    generic machinery, not doc-import-specific, so it applies identically whether
    ratification comes through the MCP ``ratify``/``ratify_decisions`` tools or
    ``sidegraph-ratify``. Dropping the proposal instead (``Store.drop``) never touches the
    ancestor — it stays untouched, exactly as if the edit had never been proposed. History
    is preserved either way; the only change from the old rule is WHEN an accepted record
    closes.

    **Anchors (design §2)** are always freshly resolved from the CURRENT doc content and
    CURRENT graph on every run (never inherited from a superseded predecessor, unlike
    ``supersede_decision``'s default) — the whole point of doc-import is that the doc's
    mentions are the source of truth. A doc with NO anchor at all (no resolved mention AND
    no doc-level file node) is skipped entirely and counted ``skipped_unanchorable`` —
    consistent with importer #1, never written with zero anchors.

    ``dry_run=True`` runs the full pipeline (parse -> frontmatter -> idempotency -> anchor
    resolution) so counts reflect what WOULD happen, but writes nothing;
    ``DocImportReport.dry_run`` carries ``{"file_path", "ref", "title", "action":
    "imported"|"superseded", "anchors_skipped"}`` per RECORD (a split-capable dialect can
    emit several per file). ``tags`` (free text, slugified, empty slugs
    dropped) bind durable ``tag:<slug>`` entities on every written decision, same mechanism
    as ``capture.py``'s tags.

    **Import glob enforcement (design D7.1, staleness-machinery wave, E8 hygiene):** every
    enumerated file (from an explicit ``--docs PATH`` — a file or, recursed, a directory —
    the CLI's explicit-path branch bypasses profile scoping entirely, so this is the only
    layer that ever sees individual files) is matched against the active ``profile``'s
    ``ingest_globs`` (:func:`_matches_ingest_globs`) BEFORE it is even opened. A file
    outside every glob is skipped and counted ``skipped_outside_profile`` — never parsed,
    never anchor-resolved. ``any_doc=True`` restores the pre-D7.1 no-filter behavior (the
    CLI's ``--any-doc`` flag). Profile-only discovery (no explicit ``--docs PATH``) is
    unaffected either way: those files already came FROM ``profile.ingest_globs``, so the
    check is a structural no-op there.

    Normalization is against the REPO ROOT (:func:`_glob_repo_root`), never the process's
    current directory (BLOCKING-1, code review): ``ingest_globs`` are repo-relative
    patterns, so a caller running from a subdirectory, or passing an absolute path (or one
    reached through a symlinked prefix) from anywhere but the repo root, must still
    resolve to the same repo-relative form a glob like ``docs/adr/*.md`` means. Both the
    file and the repo root are ``.resolve()``-d before the comparison (the macOS
    ``/tmp`` -> ``/private/tmp`` symlink case).

    **E1/E2/E3 (openspec profile design, design/superpowers/specs/2026-08-06-openspec-
    profile-design.md).** E1: ``active_profile.title_pattern`` is threaded into the parse
    call so a doc with no H1 still qualifies when its path matches (§5). E2:
    ``active_profile.normalized_rel_path(rel_path)`` — not the on-disk path — is what gets
    parsed and so stamped into ``context``/``provenance.ref``, so a doc re-imported after a
    flow's own archive-style move dedupes by ref instead of duplicating (§6); the on-disk
    path stays in play for file I/O, the anchor lookup, and the dry-run/``by_file()``
    listings. E3: a split-produced parent that is either an echo of its own ``context`` or
    empty even after every fallback is never written — ``DocImportReport.
    skipped_degenerate_parent`` counts it, once per file, and its children stand alone (§7).

    ``ratify_policy`` (default ``RatifyPolicy.MANUAL``): the resolved
    ``SIDEGRAPH_RATIFY_POLICY`` value (design D1), sampled once by the CLI shell
    immediately before this call and passed down unchanged. ``_import_one_record``'s own
    post-write block stamps a written record ``auto:<policy>`` via the shared
    ``_auto_ratify`` helper when it landed ``proposed`` (``lands_proposed``, not the raw
    ``propose`` flag — a ``status: rejected`` doc imported with ``--propose`` lands
    REJECTED with ``action == "written"`` and must never be handed to a ratify
    transition) and passes the same D3 gate every other write path uses. This seam (the
    per-record adapter) is deliberately the ONLY hook site — the shared
    ``apply_doc_candidate``/``_apply_doc_state`` write path stays untouched, since
    Bootstrap's ``bootstrap/apply.py`` calls it directly and must never auto-ratify.
    # see design/superpowers/specs/2026-09-11-auto-ratification-policy-design.md D1/D2/D3
    """
    # None = "auto per document" (B4); an explicit kind (including "adr") always wins.
    decision_kind_override = DecisionKind(kind) if kind is not None else None
    graph_version = reader.graph_version()
    active_profile = get_profile(profile)
    dialect = active_profile.dialect
    tag_slugs = [s for s in (slugify(t) for t in (tags or [])) if s and s != "redacted"]

    files = _collect_markdown_files(paths)
    process = files[:limit] if limit is not None else files

    report = DocImportReport()
    # Resolved ONCE per run, not per file (BLOCKING-1) — repo-root-relative, never
    # cwd-relative. Always computed now (I2, R1 improvement wave §2): the live-tree
    # trigger predicate below needs a repo-root-relative on-disk path REGARDLESS of
    # `any_doc` — its own glob-match clause is independent of the D7.1 enforcement gate
    # below, which stays `any_doc`-gated exactly as before.
    repo_root = _glob_repo_root()

    for rel_path in process:
        # D7.1: enforcement runs FIRST, before the file is even read — an out-of-profile
        # doc costs nothing beyond the glob check itself. Both sides are `.resolve()`-d
        # (follows symlinks, absolute-izes a relative path against the CURRENT cwd) before
        # computing the repo-root-relative form a glob like `docs/adr/*.md` actually means
        # — see `_glob_repo_root`'s docstring for why cwd alone isn't enough.
        glob_check_path = os.path.relpath(Path(rel_path).resolve(), repo_root)
        if not any_doc and not _matches_ingest_globs(glob_check_path, active_profile.ingest_globs):
            report.skipped_outside_profile += 1
            continue

        source_bytes = Path(rel_path).read_bytes()
        source_hash = hashlib.sha256(source_bytes).hexdigest()
        raw_text = source_bytes.decode().replace("\r\n", "\n").replace("\r", "\n")
        # E2 (design note §6): the NORMALIZED path — not the on-disk `rel_path` — is what
        # gets stamped into `context`/children's `effective_ref`/`provenance.ref` (via
        # `DocWriteRequest.rel_path` below), so an unedited doc re-imported after an
        # archive-style move dedupes by ref instead of duplicating. The on-disk `rel_path`
        # stays in play for exactly two things: file I/O (already done, above) and the
        # anchor lookup (`anchor_lookup_path`, below) — plus the dry-run/`by_file()`
        # listings, which deliberately show what the operator would open (review N5).
        normalized_rel_path = active_profile.normalized_rel_path(rel_path)
        docs, reason = _parse_decision_docs_with_reason(
            raw_text,
            normalized_rel_path,
            section_limit=section_limit,
            dialect=dialect,
            title_pattern=active_profile.title_pattern,
        )
        if not docs:
            if reason == "template":
                report.skipped_template += 1
            elif reason == "unparseable":
                report.skipped_unparseable += 1
            else:
                report.skipped_not_decision += 1
            continue
        # Historical status is a DOC-level property (frontmatter) — checked once per file;
        # children inherit the parent's frontmatter_status by construction.
        if _is_historical_status(docs[0].frontmatter_status):
            report.skipped_superseded_frontmatter += 1
            continue

        # E3 rule 1 (design note §7, review Major 4): doc_import-only — bootstrap already
        # refuses an echo-choice parent via its own `_choice_is_context_fallback` check, so
        # only this write path has the gap to close. Rule 2 (the empty-choice case)
        # already happened inside the parser when it fires: `docs` simply has no record
        # with `fragment is None` (no parent) while its children are present. Both cases
        # share one counter; rule 1 additionally drops the echoed parent from `docs` before
        # any record from this file reaches `_import_one_record`.
        has_parent = docs[0].fragment is None
        has_children = any(d.fragment is not None for d in docs)
        if not has_parent and has_children:
            report.skipped_degenerate_parent += 1
        elif (
            has_parent
            and has_children
            and _choice_is_context_echo(docs[0].context, docs[0].choice, normalized_rel_path)
        ):
            report.skipped_degenerate_parent += 1
            docs = docs[1:]

        # I2 (R1 improvement wave §2, Blocker 1 — decide-then-stamp): the in-flight note is
        # appended to every SURVIVING record's `context` — parent AND children alike (review
        # Major 2) — only AFTER the degenerate-parent decision above has already run against
        # the note-free context. Stamping any earlier would corrupt
        # `_choice_is_context_echo`'s own strip-and-compare (measured: flips the E3 rule 1
        # refusal from 11 hits to 0 on the live tree) — the ONLY correct order is
        # decide-then-stamp. `test_openspec_bootstrap_apply_live_path_note_matches_importer`
        # (test_bootstrap_apply.py) is the cross-path stamp-equality pin for a LIVE doc — the
        # profile wave's own T12 only exercises an archived doc, where neither write path
        # stamps at all, so it can't catch a one-sided or differently-placed note here; if
        # the live twin above goes red, the bug is in this sequencing.
        if active_profile.in_flight_note and _is_live_tree_path(glob_check_path, active_profile):
            note = active_profile.in_flight_note
            docs = [d.model_copy(update={"context": f"{d.context}\n\n{note}"}) for d in docs]

        # Doc-level anchor context, computed once per file: the redacted whole text feeds
        # the PARENT's mention anchors (unchanged); the file-node anchor is shared by every
        # record from this file. Children take their mention anchors from their OWN
        # redacted title+choice text instead — a per-AD record should bind to the symbols
        # its own block names, not to the whole document's top-3.
        clean_whole, _ = redact(raw_text)
        # Doc-node anchor lookup needs a REPO-RELATIVE path to match the graph's
        # `source_file` — a caller (the CLI's `--docs` in particular) may pass an absolute
        # path straight through; normalize it against the cwd here, just for this lookup
        # (provenance.ref/idempotency stay keyed on whatever path the caller actually
        # passed, unchanged — the CLI's absolute-path warning depends on this rationale).
        # Deliberately the ON-DISK path (E2): anchor lookups match the graph's actual
        # `source_file`, which never moves just because a flow archives a change folder.
        anchor_lookup_path = rel_path
        if os.path.isabs(anchor_lookup_path):
            anchor_lookup_path = os.path.relpath(anchor_lookup_path, os.getcwd())
        file_anchor = _file_node_descriptor(anchor_lookup_path, reader)

        for parsed in docs:
            _import_one_record(
                parsed,
                normalized_rel_path,
                file_path=rel_path,
                report=report,
                store=store,
                reader=reader,
                source_hash=source_hash,
                graph_version=graph_version,
                clean_whole=clean_whole,
                file_anchor=file_anchor,
                tag_slugs=tag_slugs,
                decision_kind_override=decision_kind_override,
                propose=propose,
                dry_run=dry_run,
                ratify_policy=ratify_policy,
            )

    return report


def _import_one_record(
    parsed: ParsedDoc,
    rel_path: str,
    *,
    file_path: str,
    report: DocImportReport,
    store: Store,
    reader: GraphifyReader,
    source_hash: str,
    graph_version: str | None,
    clean_whole: str,
    file_anchor: Descriptor | None,
    tag_slugs: list[str],
    decision_kind_override: DecisionKind | None,
    propose: bool,
    dry_run: bool,
    ratify_policy: RatifyPolicy,
) -> None:
    """Compatibility adapter from importer options/reporting to the shared write seam.

    ``rel_path`` (E2, design note §6) is the NORMALIZED path — it drives
    ``DocWriteRequest.rel_path``/``.ref`` and so ``context``'s "imported from" stamp and
    ``provenance.ref``. ``file_path`` is the real, on-disk path (review N5) — used ONLY for
    the dry-run listing's ``"file_path"`` entry, which shows the operator what they'd
    actually open; every other on-disk use (file I/O, the anchor lookup) already happened
    in the caller before this record-level adapter runs."""
    status_rejected = _is_rejected_status(parsed.frontmatter_status)
    status_derived = not status_rejected and _is_draft_like_status(parsed.frontmatter_status)
    lands_proposed = not status_rejected and (propose or status_derived)
    if status_rejected:
        status = DecisionStatus.REJECTED
    else:
        status = DecisionStatus.PROPOSED if lands_proposed else DecisionStatus.ACCEPTED
    parsed_for_write = parsed.model_copy(
        update={"suggested_kind": decision_kind_override or parsed.suggested_kind}
    )
    preflight = DocWriteRequest(
        parsed=parsed_for_write,
        rel_path=rel_path,
        source_hash=source_hash,
        status=status,
        file_anchor=file_anchor,
        graph_version=graph_version,
        tags=tuple(tag_slugs),
    )
    preflight_disposition = _classify_doc_candidate(store, preflight)
    if preflight_disposition.action == "skipped-existing":
        report.skipped_existing += 1
        return

    mention_source = clean_whole if parsed.fragment is None else f"{parsed.title}\n{parsed.choice}"
    anchors, anchors_skipped = _select_mention_anchors(mention_source, reader)
    request = preflight.model_copy(
        update={"anchors": tuple(anchors), "anchors_skipped": tuple(anchors_skipped)}
    )

    if dry_run:
        disposition = _classify_doc_candidate(store, request)
        result = DocWriteResult(action=disposition.action)
    else:
        result = apply_doc_candidate(store, reader, request)

    # Auto-ratify (design D2/D3, spec rev 9 Ruling V): gated on `lands_proposed`, NOT the
    # raw `propose` flag — a `status: rejected` doc lands REJECTED even with `--propose`
    # (`action == "written"`), and handing that to _auto_ratify would report a spurious
    # "not proposed" failure. `result.action == "written"` excludes a superseding write
    # (D2 defers the predecessor's close to a successful Store.ratify, never to an
    # unratified auto write); `result.decision_id is not None` narrows for mypy and is
    # never False here (a dry run never reaches this branch at all: `not dry_run` is a
    # declared equivalent mutant — the dry-run DocWriteResult above carries no
    # decision_id, so _anchor_signal(store, None) would be ineligible anyway).
    if (
        lands_proposed
        and not status_derived
        and not dry_run
        and result.action == "written"
        and result.decision_id is not None
    ):
        decision = store.get_decision(result.decision_id)
        assert decision is not None  # just written by apply_doc_candidate, above
        live_tier12, ambiguous_or_orphan_only = _anchor_signal(store, result.decision_id)
        signal = AutoEligibility(
            kind=decision.kind.value,
            live_tier12=live_tier12,
            ambiguous_or_orphan_only=ambiguous_or_orphan_only,
            pipeline_clean=True,
            has_provenance=True,
            domain_anchored=False,
            has_supersedes=decision.supersedes is not None,
        )
        if auto_ratify_eligible(signal, ratify_policy):
            outcome = _auto_ratify(store, result.decision_id, signal.kind, ratify_policy)
            if outcome.ratified_by is not None:
                report.auto_ratified += 1
            if outcome.error is not None:
                report.auto_ratify_failures.append(f"{result.decision_id}: {outcome.error}")

    if result.action == "skipped-existing":
        report.skipped_existing += 1
        return
    if result.action == "skipped-unanchorable":
        report.skipped_unanchorable += 1
        return
    if result.action == "superseded":
        report.superseded += 1
    else:
        report.imported += 1
    if status_derived:
        report.status_derived_proposed += 1
    if status_rejected:
        report.status_derived_rejected += 1
    if dry_run:
        report.dry_run.append(
            {
                "file_path": file_path,
                "ref": request.ref,
                "title": parsed.title,
                "action": "superseded" if result.action == "superseded" else "imported",
                "anchors_skipped": anchors_skipped,
            }
        )


_DOC_WRITE_STATUSES = (
    DecisionStatus.ACCEPTED,
    DecisionStatus.PROPOSED,
    DecisionStatus.REJECTED,
)


def _content_matches(request: DocWriteRequest, decisions: Sequence[Decision]) -> list[Decision]:
    parsed = request.parsed
    return [
        decision
        for decision in decisions
        if (
            decision.title,
            decision.context,
            decision.choice,
            decision.rejected,
            decision.consequences,
        )
        == (
            parsed.title,
            parsed.context,
            parsed.choice,
            parsed.rejected,
            parsed.consequences,
        )
    ]


def _preferred_content_match(
    request: DocWriteRequest, decisions: Sequence[Decision]
) -> Decision | None:
    matches = _content_matches(request, decisions)
    open_matches = [decision for decision in matches if decision.status != DecisionStatus.REJECTED]
    rejected_matches = [
        decision for decision in matches if decision.status == DecisionStatus.REJECTED
    ]
    return next(iter(open_matches), None) or next(iter(rejected_matches), None)


def _classify_doc_state(
    store: Store, request: DocWriteRequest, existing: Sequence[Decision]
) -> _DocDisposition:
    match = _preferred_content_match(request, existing)
    status_rejected = request.status == DecisionStatus.REJECTED
    needs_status_change = match is not None and status_rejected != (
        match.status == DecisionStatus.REJECTED
    )
    supersedable = tuple(
        decision for decision in existing if decision.status != DecisionStatus.REJECTED
    )
    accepted_ancestor = next(
        (decision for decision in existing if decision.status == DecisionStatus.ACCEPTED), None
    )
    pending_proposal = next(
        (decision for decision in existing if decision.status == DecisionStatus.PROPOSED), None
    )

    if match is not None and not needs_status_change:
        return _DocDisposition(
            action="skipped-existing",
            accepted_ancestor=accepted_ancestor,
            pending_proposal=pending_proposal,
            supersedable=supersedable,
            inherit_bindings_from=None,
        )

    inherit_bindings_from = None
    if not request.anchors and request.file_anchor is None:
        donor = match if needs_status_change else None
        if donor is None and status_rejected and supersedable:
            donor = supersedable[0]
        if donor is not None and store.bindings_for_record(donor.id):
            inherit_bindings_from = donor.id
        else:
            return _DocDisposition(
                action="skipped-unanchorable",
                accepted_ancestor=accepted_ancestor,
                pending_proposal=pending_proposal,
                supersedable=supersedable,
                inherit_bindings_from=None,
            )

    return _DocDisposition(
        action="superseded" if supersedable else "written",
        accepted_ancestor=accepted_ancestor,
        pending_proposal=pending_proposal,
        supersedable=supersedable,
        inherit_bindings_from=inherit_bindings_from,
    )


def _classify_doc_candidate(store: Store, request: DocWriteRequest) -> _DocDisposition:
    existing = store.find_decisions_by_ref("doc-import", request.ref, statuses=_DOC_WRITE_STATUSES)
    return _classify_doc_state(store, request, existing)


def _apply_doc_bindings(
    *,
    store: Store,
    reader: GraphifyReader,
    request: DocWriteRequest,
    decision_id: str,
    inherit_bindings_from: str | None,
) -> None:
    """Idempotently apply every binding requested for one document decision."""
    for anchor in request.anchors:
        resolve_and_bind(decision_id, anchor, reader, store)
    for inherited in (
        store.bindings_for_record(inherit_bindings_from) if inherit_bindings_from else []
    ):
        store.add_binding(
            AnchorBinding(
                record_id=decision_id,
                entity_id=inherited.entity_id,
                tier=inherited.tier,
                weight=inherited.weight,
                relation=inherited.relation,
                status=inherited.status,
            )
        )
    if request.file_anchor is not None:
        resolve_and_bind(decision_id, request.file_anchor, reader, store)
    for slug in request.tags:
        tag_entity = store.get_or_create_abstract_entity(f"tag:{slug}")
        store.add_binding(
            AnchorBinding(record_id=decision_id, entity_id=tag_entity.entity_id, tier=0)
        )


def _apply_doc_state(
    *,
    store: Store,
    reader: GraphifyReader,
    request: DocWriteRequest,
    existing: Sequence[Decision],
    decision_kind: DecisionKind,
) -> DocWriteResult:
    disposition = _classify_doc_state(store, request, existing)
    if disposition.action == "skipped-unanchorable":
        return DocWriteResult(action=disposition.action)
    if disposition.action == "skipped-existing":
        match = _preferred_content_match(request, existing)
        if match is None:  # Defensive: classification can skip only a content match.
            raise RuntimeError("document classification lost its existing content match")
        if (
            request.ratify_matching_proposal
            and request.status == DecisionStatus.ACCEPTED
            and match.status == DecisionStatus.PROPOSED
        ):
            match, _cascaded = store.ratify(match.id)
        _apply_doc_bindings(
            store=store,
            reader=reader,
            request=request,
            decision_id=match.id,
            inherit_bindings_from=None,
        )
        return DocWriteResult(action=disposition.action, decision_id=match.id)

    if disposition.pending_proposal is not None:
        store.drop(disposition.pending_proposal.id)

    match = _preferred_content_match(request, existing)
    needs_status_change = match is not None and (request.status == DecisionStatus.REJECTED) != (
        match.status == DecisionStatus.REJECTED
    )
    decision = Decision(
        title=request.parsed.title,
        kind=decision_kind,
        status=request.status,
        context=request.parsed.context,
        choice=request.parsed.choice,
        rejected=request.parsed.rejected,
        consequences=request.parsed.consequences,
        valid_from=datetime.now(UTC),
        valid_to=datetime.now(UTC) if request.status == DecisionStatus.REJECTED else None,
        supersedes=(
            disposition.accepted_ancestor.id
            if disposition.accepted_ancestor is not None
            else (match.id if needs_status_change and match is not None else None)
        ),
        provenance=Provenance(
            source="doc-import",
            author="sidegraph-import",
            ref=request.ref,
            graph_version=request.graph_version,
        ),
    )
    close_predecessor = not (
        request.status == DecisionStatus.PROPOSED and disposition.accepted_ancestor is not None
    )
    store.add_decision(decision, close_predecessor=close_predecessor)
    _apply_doc_bindings(
        store=store,
        reader=reader,
        request=request,
        decision_id=decision.id,
        inherit_bindings_from=disposition.inherit_bindings_from,
    )

    return DocWriteResult(
        action=disposition.action,
        decision_id=decision.id,
    )


def apply_doc_candidate(
    store: Store, reader: GraphifyReader, request: DocWriteRequest
) -> DocWriteResult:
    if request.status not in (
        DecisionStatus.ACCEPTED,
        DecisionStatus.PROPOSED,
        DecisionStatus.REJECTED,
    ):
        raise ValueError("document import can write only accepted, proposed, or rejected")
    decision_kind = request.parsed.suggested_kind or DecisionKind.ADR
    existing = store.find_decisions_by_ref("doc-import", request.ref, statuses=_DOC_WRITE_STATUSES)
    return _apply_doc_state(
        store=store,
        reader=reader,
        request=request,
        existing=existing,
        decision_kind=decision_kind,
    )
