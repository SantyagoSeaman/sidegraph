import json
from pathlib import Path

import pytest

from sidegraph.doc_import import (
    ParsedDoc,
    _glob_to_regex,
    _parse_decision_doc_with_reason,
    extract_mention_tokens,
    import_docs,
    parse_decision_doc,
)
from sidegraph.engine.reader import GraphifyReader
from sidegraph.schema import DecisionKind, DecisionStatus
from sidegraph.store import Store

_SPECS_DIR = Path(__file__).resolve().parent.parent / "design" / "superpowers" / "specs"

# ---------------------------------------------------------------------------------------
# Parser fixtures
# ---------------------------------------------------------------------------------------

ADR_FIXTURE = """# Use SQLite for the store

## Context

We need a repo-committable store with no server process. `Store` lives in `store.py`.

## Decision

Use SQLite via the stdlib `sqlite3` module, wrapped by `submit_order` callers.

## Status

Accepted

## Consequences

No server to run; single-writer contention is acceptable at this scale.

## Rejected

Considered Postgres but it needs a server to operate.
"""

SPEC_FIXTURE = """# Retry policy hardening

**Date:** 2026-07-08
**Trigger:** Orders were double-submitted under network partition.

## Design — idempotency key

Use an idempotency key stored in `orders.py` keyed by `OrderId`.

## Scope notes / residuals

Out of scope: distributed locks.

## Rejected

Distributed locks — too heavy for this problem.
"""

FREEFORM_FIXTURE = """# Random notes

Just some prose. No decision structure here at all, just notes to self.
"""

TASK_PLAN_FIXTURE = """# Implement retry logic

## Tasks

- [ ] task 1
- [ ] task 2
"""

SUPERSEDED_FRONTMATTER_FIXTURE = """---
status: Superseded by ADR-9999
---
# Old storage decision

## Context

We used to need this.

## Decision

Old choice, no longer current.
"""

# Mirrors a real spec's shape (verified during pre-release calibration): a **Trigger:**
# value wrapping 3 physical lines, no Context/Trigger/Root-cause heading, and no
# Decision/Design heading either — so BOTH context (fed by the bold Trigger) and choice
# (falls through past an empty first-paragraph to the next section) depend on the
# multi-line capture working.
MULTI_LINE_TRIGGER_FIXTURE = """# Public release plan

**Date:** 2026-07-06
**Status:** Proposed (research done, awaiting go)
**Trigger:** First public commit was drafted as a bare code snapshot; comparative research
across 9 similar OSS projects shows a converged bar the repo
should meet: full README, user-facing docs, one-command install, plugin distribution.

## Research conclusions

1. Install one-liner.
2. Comparison table.
"""

# Mirrors a real spec's shape (verified during pre-release calibration): bold lead lines
# that are NOT Trigger/Status/Date (**Related:**), no Context/Trigger/Root-cause heading,
# no Decision/Design heading — only a bold **Status:** line qualifies the doc at all, and
# the first real section is "## Purpose".
RELATED_LEAK_FIXTURE = """# Some design note

**Date:** 2026-07-05
**Status:** Approved for planning
**Related:** [`docs/development-plan.md`](../../development-plan.md) and `CLAUDE.md`

## Purpose

This note explains the actual purpose in real prose, not the Related line above.

## Scope

Some scope text.
"""

# MADR (Markdown ADR) template shape: "Decision Drivers" and "Decision Outcome" both start
# with the bare "decision" prefix — without a more-specific keyword tried first, whichever
# comes first in DOCUMENT order wins, which for MADR is the wrong one (Decision Drivers is
# context, not the choice). "Considered Options" also uses different word order than the
# pre-existing "options considered" keyword and didn't match at all.
MADR_FIXTURE = """# Use event sourcing for order state

## Context and Problem Statement

Order state changes need a full audit trail for compliance.

## Decision Drivers

* Need replayability
* Need an audit trail

## Considered Options

* Event sourcing
* Plain CRUD with an audit log table

## Decision Outcome

Chosen option: "Event sourcing", because it gives replayability and an audit trail for free.

## Pros and Cons of the Options

### Event sourcing

Good, because replayable.
"""


def test_parse_adr_style_maps_fields():
    p = parse_decision_doc(ADR_FIXTURE, "docs/adr1.md")
    assert isinstance(p, ParsedDoc)
    assert p.title == "Use SQLite for the store"
    assert "repo-committable store" in p.context
    assert p.context.endswith("imported from docs/adr1.md")
    assert "sqlite3" in p.choice
    assert p.rejected is not None
    assert "Postgres" in p.rejected
    assert p.frontmatter_status is None


def test_parse_spec_style_maps_fields():
    p = parse_decision_doc(SPEC_FIXTURE, "design/specs/retry.md")
    assert p is not None
    assert p.title == "Retry policy hardening"
    assert "double-submitted" in p.context
    assert p.context.endswith("imported from design/specs/retry.md")
    assert "idempotency key" in p.choice
    assert p.rejected is not None
    assert "Distributed locks" in p.rejected


def test_parse_freeform_doc_rejected():
    assert parse_decision_doc(FREEFORM_FIXTURE, "notes.md") is None


def test_parse_task_plan_shape_rejected():
    assert parse_decision_doc(TASK_PLAN_FIXTURE, "plan.md") is None


# Blank ADR templates carry every qualifying section heading by construction, so the
# section gate alone can't reject them (found live: an ADR corpus's `_ADR-template.md`
# imported as "ADR-NNN: <Decision title>"). The angle-bracket placeholder left in the H1
# is the deterministic tell — but only OUTSIDE backtick spans, so a real title quoting a
# generic type (`List<T>`) still qualifies.
ADR_TEMPLATE_FIXTURE = """# ADR-NNN: <Decision title>

## Context

<What forces are at play; what problem this decision addresses.>

## Decision

<The change we're proposing / have agreed to.>

## Consequences

<What becomes easier or harder.>
"""


def test_parse_template_placeholder_h1_rejected():
    assert parse_decision_doc(ADR_TEMPLATE_FIXTURE, "_ADR-template.md") is None


def test_parse_backticked_angle_brackets_in_h1_still_qualify():
    # A real title QUOTING a generic type in backticks (`Dict<str, Node>`) must not trip the
    # H1 angle-bracket placeholder guard — the backtick span is stripped before that check.
    # (The body is real prose, NOT a blank template, so BUG G's placeholder-dominance tell
    # does not apply either — see test_parse_template_by_placeholder_dominated_body_rejected
    # for the all-placeholder-body case.)
    doc = (
        "# Resolve `Dict<str, Node>` anchors eagerly\n\n"
        "## Context\n\nAnchors were resolved lazily and races appeared under load.\n\n"
        "## Decision\n\nResolve every anchor eagerly at capture time instead.\n"
    )
    p = parse_decision_doc(doc, "docs/adr/dict-anchors.md")
    assert p is not None
    assert p.title.startswith("Resolve")


# ---------------------------------------------------------------------------------------
# BUG G — document TEMPLATE detection. A template (a skeleton to copy and fill in) must
# never import as a decision, and a template's own **Status: APPROVED** must never drive
# acceptance. Calibrated against a real architecture corpus where blank ADR/SAD templates
# were importing as false-positive decisions (one landing ACCEPTED), while its real ADRs
# must still import. Signals: a "template" filename, a `type: template` frontmatter field,
# or a Context/Decision body still dominated by placeholder stubs.
# ---------------------------------------------------------------------------------------

# Mirrors a real-world ADR template's shape: a plain real-word H1 (NO angle-bracket
# placeholder in it, so the H1 guard alone can't catch it), a bold **Status:** APPROVED lead
# line, and real ADR section headings whose bodies are prose INSTRUCTIONS (not <...> stubs) —
# so ONLY the filename/`type` tells mark it as a template. Pre-fix this imported as ACCEPTED.
ADR_TEMPLATE_APPROVED_FIXTURE = """---
id: ADR-template
title: "ADR — Architecture Decision Record (template)"
type: template
status: approved
---
# ADR - Architecture Decision Record

**Status:** APPROVED
**Date:** 25 Nov 2024

## Context *(OPTIONAL)*

Describe the general business and technical context related to this document, and state the
problem that this ADR resolves.

## Decision Description *(OPTIONAL)*

A detailed technical description of all possible options is provided here.

## Outcome and Rationale

The chosen option must be stated with all additions and constraints, along with the rationale.
"""


def test_parse_template_by_filename_rejected_as_template():
    parsed, reason = _parse_decision_doc_with_reason(
        ADR_TEMPLATE_APPROVED_FIXTURE, "architecture/ADR-template.md"
    )
    assert parsed is None
    assert reason == "template"


def test_parse_real_adr_with_template_in_name_not_dropped():
    # Review finding N1: the filename tell is anchored to the stem's end, so a REAL ADR that
    # merely mentions "template" mid-name is NOT dropped as a template. Uses a real-content
    # ADR fixture (not the placeholder template) so only the filename could flag it.
    doc = (
        "# ADR-012: Email template engine\n\n"
        "## Context\n\nOutbound email needs a rendering layer; today each caller hand-builds "
        "HTML, which drifts and breaks on locale changes.\n\n"
        "## Decision\n\nAdopt a single Jinja-based template engine behind a `render_email` "
        "facade; callers pass a template name and a context dict.\n"
    )
    parsed, reason = _parse_decision_doc_with_reason(
        doc, "architecture/ADR-012-email-template-engine.md"
    )
    assert parsed is not None, f"real ADR wrongly rejected (reason={reason!r})"
    assert parsed.title == "ADR-012: Email template engine"


def test_parse_template_by_frontmatter_type_rejected_as_template():
    # Same content; the filename does NOT say "template" — the `type: template` frontmatter
    # is the tell here.
    parsed, reason = _parse_decision_doc_with_reason(
        ADR_TEMPLATE_APPROVED_FIXTURE, "architecture/adr-skeleton.md"
    )
    assert parsed is None
    assert reason == "template"


def test_parse_template_by_placeholder_dominated_body_rejected():
    # Neither "template" in the filename nor `type: template` — a blank-template copy whose
    # Context/Decision bodies are still <...> placeholder stubs.
    doc = (
        "# Payments retry policy\n\n"
        "## Context\n\n<What forces are at play; the problem this decision addresses.>\n\n"
        "## Decision\n\n<The change we are proposing or have agreed to.>\n"
    )
    parsed, reason = _parse_decision_doc_with_reason(doc, "docs/adr/payments.md")
    assert parsed is None
    assert reason == "template"


def test_parse_real_adr_with_type_adr_not_flagged_template():
    # No-regression: a real, filled-in ADR (real prose bodies, `type: adr`, non-template
    # filename) must still parse — no false-positive template rejection.
    doc = (
        "---\nid: ADR-001\ntype: adr\nstatus: draft\n---\n"
        "# ADR-001: Use SQLite for the store\n\n"
        "## Context\n\nWe need a repo-committable store with no server process running.\n\n"
        "## Decision\n\nUse SQLite via the stdlib `sqlite3` module for the derived index.\n"
    )
    parsed, reason = _parse_decision_doc_with_reason(doc, "docs/adr/ADR-001-sqlite.md")
    assert parsed is not None
    assert reason is None
    assert parsed.title.startswith("ADR-001")


def test_import_docs_template_with_status_approved_skipped_not_accepted(tmp_path):
    # Headline BUG G repro: a template carrying **Status:** APPROVED must be counted as a
    # skipped template and NEVER written as an accepted decision.
    reader = _reader(tmp_path, IDENTIFIER_GRAPH)
    store = Store(tmp_path / "s.db")
    _write_md(tmp_path, "ADR-template.md", ADR_TEMPLATE_APPROVED_FIXTURE)

    report = import_docs(store, reader, [str(tmp_path / "ADR-template.md")], any_doc=True)
    assert report.imported == 0
    assert report.skipped_template == 1
    assert report.skipped_not_decision == 0
    assert list(store.iter_decisions()) == []


def test_import_docs_real_adr_alongside_template(tmp_path):
    # A mixed directory: the real ADR imports; the template is skipped as a template.
    reader = _reader(tmp_path, IDENTIFIER_GRAPH)
    store = Store(tmp_path / "s.db")
    _write_md(tmp_path, "docs/ADR-template.md", ADR_TEMPLATE_APPROVED_FIXTURE)
    _write_md(tmp_path, "docs/ADR-001.md", _adr("Use SQLite", decision="Wire `submit_order` in."))

    report = import_docs(store, reader, [str(tmp_path / "docs")], any_doc=True)
    assert report.imported == 1
    assert report.skipped_template == 1
    d = next(store.iter_decisions())
    assert d.title == "Use SQLite"


def test_parse_superseded_frontmatter_signal():
    p = parse_decision_doc(SUPERSEDED_FRONTMATTER_FIXTURE, "docs/old.md")
    assert p is not None  # still decision-shaped: H1 + Context/Decision
    assert p.frontmatter_status is not None
    assert "superseded" in p.frontmatter_status.lower()


def test_parse_bold_status_only_qualifies_with_degraded_fallbacks():
    # Coordinator fixture (a): H1 + bold **Status:** only, unknown headings only ->
    # qualifies; choice falls back to the first prose paragraph, context falls back to the
    # bare "imported from" line (no Context/Trigger/Root-cause source at all).
    text = (
        "# Some engineering note\n\n"
        "**Date:** 2026-07-08\n"
        "**Status:** Approved\n\n"
        "This is the first paragraph of prose right after the H1, explaining the note.\n\n"
        "## Purpose\n\nSome purpose text.\n\n"
        "## Architecture\n\nSome architecture text.\n"
    )
    p = parse_decision_doc(text, "design/x.md")
    assert p is not None
    assert p.context == "imported from design/x.md"
    assert p.choice == (
        "This is the first paragraph of prose right after the H1, explaining the note."
    )
    assert p.frontmatter_status == "Approved"


def test_parse_bold_date_only_does_not_qualify():
    # Coordinator fixture (b): bold **Date:** alone is never a qualifying signal.
    text = "# Some other note\n\n**Date:** 2026-07-08\n\n## Purpose\n\ntext\n"
    assert parse_decision_doc(text, "design/y.md") is None


def test_parse_only_decisions_heading_qualifies_and_supplies_choice():
    text = "# Some decision title\n\n## Decisions (from brainstorming)\n\nWe decided X.\n"
    p = parse_decision_doc(text, "x.md")
    assert p is not None
    assert p.choice == "We decided X."


def test_parse_only_h1_and_bold_trigger_qualifies():
    text = (
        "# Some title\n\n"
        "**Trigger:** Something happened that prompted this.\n\n"
        "Just a plain closing paragraph, no headings at all.\n"
    )
    p = parse_decision_doc(text, "y.md")
    assert p is not None
    assert "Something happened" in p.context


def test_parse_multiline_trigger_captured_and_not_leaked_into_choice():
    # Real shape (design refinement (a)): a wrapped **Trigger:** value must be captured in
    # full (not just its first physical line) for `context`, AND the wrapped continuation
    # lines must never leak into `choice` via the first-paragraph fallback (the pre-fix bug:
    # `choice` came back as "across 9 similar OSS projects..." — a lowercase mid-sentence
    # fragment of the Trigger, not a real decision statement).
    p = parse_decision_doc(MULTI_LINE_TRIGGER_FIXTURE, "design/plan.md")
    assert p is not None
    assert "comparative research" in p.context
    assert "should meet: full README" in p.context
    assert p.context.endswith("imported from design/plan.md")
    assert not p.choice.startswith("across 9 similar")
    assert "Install one-liner" in p.choice  # falls through to the next real section instead


def test_parse_non_trigger_bold_lead_lines_do_not_leak_into_choice():
    # Real shape (design refinement (b)): a doc with no Trigger, no Decision/Design section,
    # and non-qualifying bold lead lines (**Related:**) must fall through past the (now
    # correctly empty) first paragraph to the next real section body, not the raw
    # "**Related:** [...]" markup.
    p = parse_decision_doc(RELATED_LEAK_FIXTURE, "design/note.md")
    assert p is not None
    assert not p.choice.startswith("**Related:**")
    assert not p.choice.startswith("**")
    assert "actual purpose in real prose" in p.choice


def test_parse_madr_style_prioritizes_decision_outcome_over_decision_drivers():
    p = parse_decision_doc(MADR_FIXTURE, "docs/madr.md")
    assert p is not None
    assert p.choice.startswith('Chosen option: "Event sourcing"')
    assert "replayability" in p.context or "audit trail" in p.context
    assert p.rejected is not None
    assert "Plain CRUD with an audit log table" in p.rejected
    assert "Event sourcing" in p.rejected


def test_a_numbered_heading_still_matches_its_keyword():
    """Red against the pre-fix matcher: '1. Context' is not prefixed by 'context', so an
    arc42-style numbered document parsed to nothing at all (see profiles.py, genkovich sad.md).
    Covers both enumerator shapes real templates use: a single level carries its dot ("1.
    Context"), a multi-level one does not (e.g. "6.1 Security / privacy", "2.1 Jobs To Be
    Done") — "4.1 Root cause" below exercises that second shape via the same
    ``_heading_has_keyword`` primitive (B4's kind heuristic)."""
    text = (
        "---\nstatus: accepted\n---\n"
        "# Bound the payment queue\n\n"
        "## 1. Context\n\nThe worker drains an unbounded queue and is OOM-killed.\n\n"
        "## 4. Decision\n\nBound the queue at 10k with producer backpressure.\n\n"
        "## 4.1 Root cause\n\nThe retry policy had no backoff ceiling.\n"
    )
    parsed = parse_decision_doc(text, "docs/adr/0001-x.md")
    assert parsed is not None
    assert "OOM-killed" in parsed.context
    assert "producer backpressure" in parsed.choice
    assert parsed.suggested_kind == DecisionKind.LESSON  # multi-level "4.1 Root cause" matched


def test_a_heading_that_merely_starts_with_a_number_keeps_it():
    """Red against the first enumerator pattern (``\\d+(?:\\.\\d+)*\\.?``), whose trailing dot
    was merely optional: "2024 Context notes" un-enumerated to "Context notes", which
    prefix-matches the "context" keyword — a count/year is part of the heading, not an
    enumerator. "2024 in review" (the brief's original example) does NOT discriminate the two
    patterns: "in review" matches no dialect keyword either way, so both the old and new
    pattern leave ``context`` as the "imported from" fallback — this heading is chosen instead
    because the stripped remainder actually collides with a real keyword under the old
    pattern.
    """
    text = (
        "---\nstatus: accepted\n---\n"
        "# Retrospective\n\n"
        "## 2024 Context notes\n\nThe queue was bounded in March.\n\n"
        "## Decision\n\nKeep the 10k bound.\n"
    )
    parsed = parse_decision_doc(text, "docs/adr/0002-x.md")
    assert parsed is not None
    assert "queue was bounded" not in parsed.context


def test_default_profile_now_parses_numbered_headings_by_design():
    """Owner ruling, 2026-07-27 (re-review N2): the enumerator strip (this module's
    _ENUMERATOR_RE, round-2 fix) widens generic-adr's import surface too, not just
    genkovich-sdd's — an arc42-style numbered ADR dropped in docs/adr/ used to fail the
    decision-shaped gate outright and return None; it now parses like any other ADR. That is
    the intended effect of the fix, not an accidental regression, and this test exists so the
    next reader sees it was chosen rather than re-deriving it from a diff."""
    text = (
        "---\nstatus: accepted\n---\n"
        "# Bound the payment queue\n\n"
        "## 1. Context\n\nThe worker drains an unbounded queue and is OOM-killed.\n\n"
        "## 2. Decision\n\nBound the queue at 10k with producer backpressure.\n"
    )
    parsed = parse_decision_doc(text, "docs/adr/0003-x.md")  # default dialect: generic-adr
    assert parsed is not None
    assert "OOM-killed" in parsed.context
    assert "producer backpressure" in parsed.choice


def test_parse_nested_h3_under_h2_rolls_up_into_parent_body():
    # Regression: "## Design" immediately followed by numbered H3 subsections (this
    # project's own spec shape) must not resolve to an empty body that falls through to
    # the wrong fallback.
    text = (
        "# Domain abstraction\n\n"
        "## User decisions (this brainstorm)\n\n"
        "Some brainstorm notes that must NOT become choice.\n\n"
        "## Design\n\n"
        "### 1. The owned abstraction\n\n"
        "A Domain groups related decisions under a durable name.\n\n"
        "### 2. Something else\n\n"
        "More design detail.\n\n"
        "## Testing\n\nsome test notes\n"
    )
    p = parse_decision_doc(text, "design/domain.md")
    assert p is not None
    assert "owned abstraction" in p.choice
    assert "brainstorm notes" not in p.choice


def test_parse_title_truncated_plain_char_slice():
    long_h1 = "A very long title " + ("x" * 200)
    text = f"# {long_h1}\n\n## Context\n\nshort context\n\n## Decision\n\nchoose it\n"
    p = parse_decision_doc(text, "docs/long.md")
    assert p is not None
    assert len(p.title) == 120  # plain char slice, no word-boundary/marker (B3)
    assert p.title == long_h1[:120]


def test_parse_section_under_default_limit_not_truncated():
    # B3: the default section cap is now 2000 (was 600) — a 900-char section must survive
    # whole, unlike the old 600-char cap that used to cut it.
    long_context = "word " * 180  # 900 chars, well past the old 600 cap
    text = f"# Title\n\n## Context\n\n{long_context.strip()}\n\n## Decision\n\nchoose it\n"
    p = parse_decision_doc(text, "docs/long.md")
    assert p is not None
    section_part = p.context.split("\n\nimported from")[0]
    assert section_part == long_context.strip()
    assert "[truncated]" not in section_part


def test_parse_section_over_default_limit_truncated_at_word_boundary_with_marker():
    long_context = "word " * 500  # 2500 chars, past the 2000-char default cap
    text = f"# Title\n\n## Context\n\n{long_context.strip()}\n\n## Decision\n\nchoose it\n"
    p = parse_decision_doc(text, "docs/long.md")
    assert p is not None
    section_part = p.context.split("\n\nimported from")[0]
    assert section_part.endswith(" …[truncated]")
    body = section_part[: -len(" …[truncated]")]
    assert len(body) <= 2000
    assert long_context.strip().startswith(body)  # cut at a real word boundary
    # the char immediately after the kept body, in the ORIGINAL text, is whitespace —
    # never a hard mid-word cut.
    assert long_context.strip()[len(body)] == " "


def test_parse_section_limit_override_applies_and_respects_word_boundary():
    long_context = "word " * 100  # 500 chars
    text = f"# Title\n\n## Context\n\n{long_context.strip()}\n\n## Decision\n\nchoose it\n"
    p = parse_decision_doc(text, "docs/long.md", section_limit=200)
    assert p is not None
    section_part = p.context.split("\n\nimported from")[0]
    assert section_part.endswith(" …[truncated]")
    body = section_part[: -len(" …[truncated]")]
    assert len(body) <= 200
    assert long_context.strip()[len(body)] == " "


def test_parse_section_truncation_cuts_at_newline_when_no_spaces():
    # Review follow-up (Minor 3): word-boundary search only looked for literal ASCII
    # spaces — text whose sole whitespace near the cut point is a newline (no space
    # anywhere) must still cut cleanly at that boundary, not mid-word.
    word_run_1 = "слово" * 60  # 300 chars, no spaces
    word_run_2 = "слово" * 60  # 300 chars, no spaces
    long_context = word_run_1 + "\n" + word_run_2
    text = f"# Title\n\n## Context\n\n{long_context}\n\n## Decision\n\nchoose it\n"
    p = parse_decision_doc(text, "docs/x.md", section_limit=350)
    assert p is not None
    section_part = p.context.split("\n\nimported from")[0]
    assert section_part.endswith(" …[truncated]")
    body = section_part[: -len(" …[truncated]")]
    assert body == word_run_1  # cut exactly at the newline, not mid-word


_SECRET = "AKIAABCDEFGHIJKLMNOP"


def test_parse_redacts_planted_secret_in_section():
    text = (
        f"# Vendor integration\n\n"
        f"## Context\n\nUses api_key={_SECRET} for the vendor call.\n\n"
        f"## Decision\n\nRotate the key regularly.\n"
    )
    p = parse_decision_doc(text, "docs/vendor.md")
    assert p is not None
    assert _SECRET not in p.context
    assert "[REDACTED]" in p.context


# ---------------------------------------------------------------------------------------
# B2 — consequences extraction + rejected/consequences bold pseudo-heading fallback.
#
# Calibrated against a real ADR corpus (fix-wave B): every source ADR there carries a real
# "## Consequences" H2 (Positive/Negative/Risks subsections) but NO "## Rejected"/
# "## Alternatives" heading at all — rejection reasoning instead sits as bold prose lead-ins,
# e.g. a bare paragraph ("**Rejected — emitter-side auto-create.** A create-if-missing
# behaviour...") and a bulleted list item right after a "- **Chosen:** ..." item with no
# blank line between them ("- **Rejected (antipattern) — a centralised...** A single
# service..."). The fixtures below mirror both shapes.
# ---------------------------------------------------------------------------------------

CONSEQUENCES_HEADING_FIXTURE = """# Use a shared execution library

## Context

Checks must execute uniformly across many teams.

## Decision

Ship a shared library, run under the caller's own credentials.

## Consequences

### Positive
- Uniformity enforced once, not fifty times.

### Negative
- A bad release affects every team at once.
"""


def test_parse_consequences_real_heading_extracted():
    p = parse_decision_doc(CONSEQUENCES_HEADING_FIXTURE, "docs/adr.md")
    assert p is not None
    assert p.consequences is not None
    assert "Uniformity enforced once" in p.consequences
    assert "bad release affects every team" in p.consequences


REJECTED_BOLD_PARAGRAPH_FIXTURE = """# DQ runner architecture

## Context

Checks need one uniform result shape across teams.

## Decision

Adopt a shared execution library run under the caller's own identity.

## Evidence lifecycle

**Decision.** Test-entity provisioning is owned by the registration flow.

**Rejected — emitter-side auto-create.** A create-if-missing behaviour in the push path
would be operationally convenient but would let an unreviewed rule mint governance
entities under a pipeline role, inverting the review model.

## Consequences

Uniform results, one shared codebase.
"""


def test_parse_rejected_bold_pseudo_heading_bare_paragraph():
    # No real "## Rejected"/"## Alternatives" heading anywhere in this doc — the ONLY
    # source for `rejected` is the bold pseudo-heading paragraph.
    p = parse_decision_doc(REJECTED_BOLD_PARAGRAPH_FIXTURE, "docs/adr005.md")
    assert p is not None
    assert p.rejected is not None
    assert "emitter-side auto-create" in p.rejected
    assert "unreviewed rule mint governance entities" in p.rejected
    assert p.consequences is not None
    assert "Uniform results" in p.consequences


REJECTED_BOLD_BULLET_ADJACENT_FIXTURE = """# DQ runner mechanism

## Context

Execution must stay least-privilege.

## Decision

Adopt a shared execution library.

## Outcome and Rationale

- **Chosen — a shared library in the caller's context.** The execution logic ships as one
  versioned codebase, inheriting the caller's own least-privilege grants.
- **Rejected (antipattern) — a centralised DQ-runner service with broad standing access.**
  A single service holding all-data rights is rejected on security and reliability grounds.

## Consequences

Uniform DQResult, no central point of failure.
"""


def test_parse_rejected_bold_pseudo_heading_bulleted_adjacent_to_another_bold_line():
    # "- **Chosen:** ..." and "- **Rejected (antipattern):** ..." sit on CONSECUTIVE lines
    # with no blank line between them — the fallback must stop capturing "Chosen" at the
    # right line (via _BOLD_PSEUDO_ANY_RE) rather than swallowing "Rejected" into it, and
    # must never pick up "Chosen" content as rejected.
    p = parse_decision_doc(REJECTED_BOLD_BULLET_ADJACENT_FIXTURE, "docs/adr.md")
    assert p is not None
    assert p.rejected is not None
    assert "Rejected (antipattern)" in p.rejected
    assert "centralised DQ-runner service" in p.rejected
    assert "Chosen — a shared library" not in p.rejected


REJECTED_MULTIPLE_BLOCKS_FIXTURE = """# Two rejected alternatives

## Context

c

## Decision

d

## Outcome

**Rejected — option A.** Too slow for the target scale.

**Rejected — option B.** Requires a second system to operate.
"""


def test_parse_rejected_multiple_bold_pseudo_headings_joined():
    p = parse_decision_doc(REJECTED_MULTIPLE_BLOCKS_FIXTURE, "docs/two.md")
    assert p is not None
    assert p.rejected is not None
    assert "option A" in p.rejected
    assert "Too slow" in p.rejected
    assert "option B" in p.rejected
    assert "second system to operate" in p.rejected


REAL_REJECTED_HEADING_WINS_FIXTURE = """# Real heading wins

## Context

c

## Decision

d

## Rejected

The real, proper Rejected section content.

## Outcome

**Rejected — this must not appear.** A bold pseudo-heading that should be ignored because a
real Rejected section already exists.
"""


def test_parse_real_rejected_heading_wins_over_bold_pseudo_heading():
    p = parse_decision_doc(REAL_REJECTED_HEADING_WINS_FIXTURE, "docs/real.md")
    assert p is not None
    assert p.rejected is not None
    assert "real, proper Rejected section" in p.rejected
    assert "this must not appear" not in p.rejected


FENCED_BOLD_PSEUDO_HEADING_EXAMPLE_FIXTURE = """# Style guide for our ADRs

## Context

c

## Decision

Write ADRs using the following template as a worked example for future authors.

```text
**Rejected — example.** This shows how to phrase a rejected block; it is not a real
rejection, just documentation for future ADR authors.
```

## Consequences

None beyond a consistent template.
"""


def test_parse_rejected_bold_pseudo_heading_inside_fence_ignored():
    # Review follow-up (Minor 2): a doc QUOTING the bold-pseudo-heading convention inside a
    # fenced code block (e.g. a style guide showing authors how to write one) must not have
    # that fenced EXAMPLE text captured as real rejected content.
    p = parse_decision_doc(FENCED_BOLD_PSEUDO_HEADING_EXAMPLE_FIXTURE, "docs/style.md")
    assert p is not None
    assert p.rejected is None


CONSEQUENCES_BOLD_PSEUDO_HEADING_FIXTURE = """# No real consequences heading

## Context

c

## Decision

d

## Outcome

**Consequences — operational.** Running this in production requires a new on-call rotation.
"""


def test_parse_consequences_bold_pseudo_heading_fallback():
    p = parse_decision_doc(CONSEQUENCES_BOLD_PSEUDO_HEADING_FIXTURE, "docs/note.md")
    assert p is not None
    assert p.consequences is not None
    assert "new on-call rotation" in p.consequences


def test_parse_no_rejected_or_consequences_signal_both_none():
    p = parse_decision_doc(ADR_FIXTURE.replace("## Rejected", "## Something else"), "docs/x.md")
    assert p is not None
    assert p.rejected is None


# A synthetic ADR mirroring the REAL corpus's exact shape (Decision Description w/ Option
# subsections, Outcome and Rationale w/ an adjacent bulleted Chosen/Rejected pair, a
# standalone bold Rejected paragraph deeper in an unrelated subsection, and a real
# Consequences heading) — the recalibration check fix-wave B's report references: after B2,
# this shape must yield non-null rejected AND consequences (pre-B2 both were null).
CALIBRATION_ADR_FIXTURE = """---
status: draft
status_note: "proposed, pending EACL review"
---
# ADR-900: Calibration mirror of the real corpus shape

## Context

Some real-world context prose goes here, several sentences long.

## Decision Description

### Option A — rejected approach
Has problems X and Y.

### Option B — chosen approach (chosen)
Solves the problem cleanly.

## Decisions Summary and Comparison

| Axis | A | B |
|---|---|---|
| **Resume** | rejected | chosen |

## Outcome and Rationale

- **Chosen — option B.** Ships as one versioned codebase.
- **Rejected (antipattern) — option A.** Rejected on security and reliability grounds.

## Evidence lifecycle

**Rejected — emitter-side auto-create.** Would let an unreviewed rule mint entities.

## Consequences

### Positive
- Uniformity enforced once.

### Negative
- A bad release affects every team.
"""


def test_parse_calibration_fixture_yields_non_null_rejected_and_consequences():
    p = parse_decision_doc(CALIBRATION_ADR_FIXTURE, "docs/adr900.md")
    assert p is not None
    assert p.rejected is not None
    assert p.consequences is not None
    assert p.frontmatter_status is not None
    assert "draft" in p.frontmatter_status.lower()


# ---------------------------------------------------------------------------------------
# B4 — kind heuristic: a qualifying "Root cause" section suggests kind=lesson.
# ---------------------------------------------------------------------------------------

ROOT_CAUSE_FIXTURE = """# Retries doubled orders under partition

## Root cause

A network partition caused the client to retry a non-idempotent submit call.

## Decision

Add an idempotency key to every submit call.
"""


def test_parse_root_cause_section_suggests_lesson_kind():
    p = parse_decision_doc(ROOT_CAUSE_FIXTURE, "docs/postmortem.md")
    assert p is not None
    assert p.suggested_kind == DecisionKind.LESSON


def test_parse_no_root_cause_suggests_no_kind():
    p = parse_decision_doc(ADR_FIXTURE, "docs/adr1.md")
    assert p is not None
    assert p.suggested_kind is None


# ---------------------------------------------------------------------------------------
# Corpus fidelity — run the real parser over this project's own internal decision-doc
# corpus the fix was calibrated against (not synthetic fixtures). Verified pre-fix (via
# the shipped 4dca13e code) that these 4 files produced literal garbage choices; see the
# fixture-level regression tests above for the synthetic mirrors of the same two bug shapes.
#
# This internal design corpus is not part of the public snapshot (tools/release-public.sh
# ships src/tests/docs only) — a public clone has no design/ directory at all, so these 3
# tests skip there rather than fail with FileNotFoundError. They keep running (and keep
# calibrating against the real corpus) in every internal checkout.
# ---------------------------------------------------------------------------------------

_CORPUS_SKIP_REASON = "internal design corpus not present in public checkouts"


@pytest.mark.skipif(not _SPECS_DIR.is_dir(), reason=_CORPUS_SKIP_REASON)
def test_corpus_fidelity_every_parsed_choice_non_empty_and_not_bold_markup():
    files = sorted(_SPECS_DIR.glob("*.md"))
    assert files, f"corpus not found at {_SPECS_DIR}"
    for f in files:
        rel = f"design/superpowers/specs/{f.name}"
        p = parse_decision_doc(f.read_text(), rel)
        if p is None:
            continue  # not decision-shaped / unparseable — not this test's concern
        assert p.choice, f"{f.name}: choice must never be empty"
        assert not p.choice.startswith("**"), (
            f"{f.name}: choice starts with raw bold-lead markup: {p.choice[:60]!r}"
        )


@pytest.mark.skipif(not _SPECS_DIR.is_dir(), reason=_CORPUS_SKIP_REASON)
def test_corpus_fidelity_wrapped_trigger_does_not_leak_into_choice():
    # The real release-plan spec: Trigger wraps 3 lines, no Decision/Design section.
    # Pre-fix, `choice` was the wrapped continuation itself
    # ("across 9 similar OSS projects...", verified against the shipped 4dca13e code).
    name = "2026-07-06-public-release-plan.md"
    f = _SPECS_DIR / name
    p = parse_decision_doc(f.read_text(), f"design/superpowers/specs/{name}")
    assert p is not None
    assert not p.choice.startswith("across 9 similar")
    assert "comparative research" in p.context
    assert "plugin distribution" in p.context
    assert p.context.endswith(f"imported from design/superpowers/specs/{name}")


@pytest.mark.skipif(not _SPECS_DIR.is_dir(), reason=_CORPUS_SKIP_REASON)
def test_corpus_fidelity_non_trigger_bold_lead_lines_do_not_leak_into_choice():
    # These 3 real specs have no Trigger and no Decision/Design section — only Related/
    # Covers/Informed-by bold lead lines. Pre-fix (verified against the shipped 4dca13e
    # code), each one's `choice` was the raw bold-lead markup itself.
    old_bad_prefixes = {
        "2026-07-05-phase-0-de-risking-gate-design.md": "**Related:**",
        "2026-07-05-graphify-reader-anchor-resolution-design.md": "**Covers:**",
        "2026-07-06-sync-rebinding-design.md": "**Covers:**",
    }
    for name, old_bad_prefix in old_bad_prefixes.items():
        f = _SPECS_DIR / name
        p = parse_decision_doc(f.read_text(), f"design/superpowers/specs/{name}")
        assert p is not None, name
        assert not p.choice.startswith(old_bad_prefix), f"{name}: {p.choice[:60]!r}"
        assert not p.choice.startswith("**"), name


# ---------------------------------------------------------------------------------------
# extract_mention_tokens
# ---------------------------------------------------------------------------------------


def test_extract_mention_tokens_classifies_path_and_identifier_like():
    text = "See `exec.py` and `submit_order` and `AnchorBinding`, but not `ok` or `Store`."
    tokens = extract_mention_tokens(text)
    assert set(tokens) == {"exec.py", "submit_order", "AnchorBinding"}


def test_extract_mention_tokens_path_like_requires_no_whitespace():
    # A backticked prose phrase can contain a "/" without being a real path — a token with
    # ANY whitespace must never classify as path-like (and here it isn't identifier-like
    # either, so it's dropped entirely; only the genuine path survives).
    text = "See the `docs/ folder overview` for details, not `exec.py`."
    assert extract_mention_tokens(text) == ["exec.py"]


def test_extract_mention_tokens_frequency_ranked_and_deduped():
    text = "`fn_two` `fn_one` `fn_two` `fn_one` `fn_two` `fn_three`"
    tokens = extract_mention_tokens(text)
    # fn_two: 3, fn_one: 2, fn_three: 1 -> strictly frequency-ranked, no duplicates.
    assert tokens == ["fn_two", "fn_one", "fn_three"]


def test_extract_mention_tokens_ties_keep_first_appearance_order():
    text = "`fn_alpha` `fn_beta` `fn_gamma`"
    assert extract_mention_tokens(text) == ["fn_alpha", "fn_beta", "fn_gamma"]


def test_extract_mention_tokens_drops_short_and_bare_words():
    text = "`ok` `abc` `just a sentence` `Store` `handler`"
    assert extract_mention_tokens(text) == []


def test_extract_mention_tokens_path_like_wins_over_identifier_shape():
    # "anchor_binding.py" reads as snake_case too, but the extension makes it path-like —
    # not that the caller can tell from the token alone, this just documents it's kept.
    tokens = extract_mention_tokens("`anchor_binding.py`")
    assert tokens == ["anchor_binding.py"]


# ---------------------------------------------------------------------------------------
# import_docs: anchors (via GraphifyReader fixtures, mirrors test_importer.py's style)
# ---------------------------------------------------------------------------------------


def _write_graph(tmp_path, name, data):
    p = tmp_path / name
    p.write_text(json.dumps(data))
    return p


def _reader(tmp_path, graph, name="g.json"):
    return GraphifyReader(_write_graph(tmp_path, name, graph))


def _write_md(tmp_path, rel, text):
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def _adr(title, *, context="Some context text.", decision="Some decision text.", rejected=None):
    body = f"# {title}\n\n## Context\n\n{context}\n\n## Decision\n\n{decision}\n"
    if rejected:
        body += f"\n## Rejected\n\n{rejected}\n"
    return body


PATH_MENTION_GRAPH = {
    "built_at_commit": "v1",
    "nodes": [
        {
            "id": "file_exec",
            "label": "exec.py",
            "norm_label": "exec.py",
            "file_type": "code",
            "source_file": "exec.py",
            "community": 1,
        },
    ],
    "links": [],
}


def test_import_docs_path_mention_resolves_to_file_node(tmp_path):
    reader = _reader(tmp_path, PATH_MENTION_GRAPH)
    store = Store(tmp_path / "s.db")
    doc = _adr("Uses exec.py", decision="Wire it through `exec.py` directly.")
    _write_md(tmp_path, "docs/a.md", doc)

    report = import_docs(store, reader, [str(tmp_path / "docs" / "a.md")], any_doc=True)
    assert report.imported == 1

    d = next(store.iter_decisions())
    leaf = [b for b in store.bindings_for_record(d.id) if b.tier == 2]
    assert len(leaf) == 1
    entity = store.get_entity(leaf[0].entity_id)
    assert entity.canonical_name == "exec.py"


IDENTIFIER_GRAPH = {
    "nodes": [
        {
            "id": "fn_submit",
            "label": "submit_order",
            "norm_label": "submit_order",
            "file_type": "code",
            "source_file": "exec.py",
            "community": 1,
        },
    ],
    "links": [],
}


def test_import_docs_unique_identifier_mention_resolves(tmp_path):
    reader = _reader(tmp_path, IDENTIFIER_GRAPH)
    store = Store(tmp_path / "s.db")
    doc = _adr("Submit path", decision="Calls `submit_order` on success.")
    _write_md(tmp_path, "docs/a.md", doc)

    report = import_docs(store, reader, [str(tmp_path / "docs" / "a.md")], any_doc=True)
    assert report.imported == 1
    d = next(store.iter_decisions())
    leaf_names = {
        store.get_entity(b.entity_id).canonical_name
        for b in store.bindings_for_record(d.id)
        if b.tier == 2
    }
    assert "submit_order" in leaf_names


AMBIGUOUS_AND_RESOLVABLE_GRAPH = {
    "nodes": [
        # "helper_fn" (snake_case, >=4 chars) so it qualifies as identifier-like at all —
        # a bare word like "helper" is correctly filtered out by extract_mention_tokens
        # itself (not a "bare common word", per design §2) before it ever reaches resolve.
        {
            "id": "dup1",
            "label": "helper_fn",
            "norm_label": "helper_fn",
            "file_type": "code",
            "source_file": "a.py",
            "community": 1,
        },
        {
            "id": "dup2",
            "label": "helper_fn",
            "norm_label": "helper_fn",
            "file_type": "code",
            "source_file": "b.py",
            "community": 2,
        },
        {
            "id": "fn_submit",
            "label": "submit_order",
            "norm_label": "submit_order",
            "file_type": "code",
            "source_file": "exec.py",
            "community": 1,
        },
    ],
    "links": [],
}


def test_import_docs_ambiguous_identifier_skipped_never_guessed(tmp_path):
    reader = _reader(tmp_path, AMBIGUOUS_AND_RESOLVABLE_GRAPH)
    store = Store(tmp_path / "s.db")
    doc = _adr("Ambiguous mention", decision="Calls `helper_fn` then `submit_order` in sequence.")
    _write_md(tmp_path, "docs/a.md", doc)

    report = import_docs(
        store, reader, [str(tmp_path / "docs" / "a.md")], dry_run=True, any_doc=True
    )
    assert report.imported == 1
    item = report.dry_run[0]
    reasons = {s["name"]: s["reason"] for s in item["anchors_skipped"]}
    assert reasons.get("helper_fn") == "ambiguous"

    # Non-dry-run: "helper_fn" is never bound to anything.
    report2 = import_docs(store, reader, [str(tmp_path / "docs" / "a.md")], any_doc=True)
    assert report2.imported == 1
    assert store.find_entity("helper_fn", None) is None
    assert store.find_entity("helper_fn", "a.py") is None


def test_import_docs_unresolved_identifier_skipped(tmp_path):
    reader = _reader(tmp_path, IDENTIFIER_GRAPH)
    store = Store(tmp_path / "s.db")
    doc = _adr(
        "Unresolved mention",
        decision="Mentions `submit_order` and also `not_a_real_symbol`.",
    )
    _write_md(tmp_path, "docs/a.md", doc)

    report = import_docs(
        store, reader, [str(tmp_path / "docs" / "a.md")], dry_run=True, any_doc=True
    )
    reasons = {s["name"]: s["reason"] for s in report.dry_run[0]["anchors_skipped"]}
    assert reasons.get("not_a_real_symbol") == "unresolved"


FOUR_MENTIONS_PLUS_DOC_NODE_GRAPH = {
    "nodes": [
        {
            "id": "file_doc",
            "label": "a.md",
            "norm_label": "a.md",
            "file_type": "document",
            "source_file": "docs/a.md",
            "community": 1,
        },
        {
            "id": "fn1",
            "label": "fn_one",
            "norm_label": "fn_one",
            "file_type": "code",
            "source_file": "exec.py",
            "community": 1,
        },
        {
            "id": "fn2",
            "label": "fn_two",
            "norm_label": "fn_two",
            "file_type": "code",
            "source_file": "exec.py",
            "community": 1,
        },
        {
            "id": "fn3",
            "label": "fn_three",
            "norm_label": "fn_three",
            "file_type": "code",
            "source_file": "exec.py",
            "community": 1,
        },
        {
            "id": "fn4",
            "label": "fn_four",
            "norm_label": "fn_four",
            "file_type": "code",
            "source_file": "exec.py",
            "community": 1,
        },
    ],
    "links": [],
}


def test_import_docs_caps_mention_anchors_at_three_plus_doc_node(tmp_path, monkeypatch):
    # The doc's OWN file-node anchor is looked up by matching provenance.ref (the path
    # passed to import_docs) against the graph's source_file — chdir + a relative path so
    # that lines up with the fixture's "docs/a.md" without hard-coding tmp_path into it.
    monkeypatch.chdir(tmp_path)
    reader = _reader(tmp_path, FOUR_MENTIONS_PLUS_DOC_NODE_GRAPH)
    store = Store(tmp_path / "s.db")
    doc = _adr(
        "Central retry policy",
        decision="Touches `fn_one`, `fn_two`, `fn_three`, and `fn_four` in order.",
    )
    _write_md(tmp_path, "docs/a.md", doc)

    report = import_docs(store, reader, ["docs/a.md"], any_doc=True)
    assert report.imported == 1
    d = next(store.iter_decisions())
    leaf_names = {
        store.get_entity(b.entity_id).canonical_name
        for b in store.bindings_for_record(d.id)
        if b.tier == 2
    }
    # 3 mention anchors (first-appearance order, tied frequency) + 1 doc-node anchor.
    assert leaf_names == {"fn_one", "fn_two", "fn_three", "a.md"}
    assert store.find_entity("fn_four", "exec.py") is None


DOC_NODE_ONLY_GRAPH = {
    "nodes": [
        {
            "id": "file_doc",
            "label": "a.md",
            "norm_label": "a.md",
            "file_type": "document",
            "source_file": "docs/a.md",
            "community": 1,
        },
    ],
    "links": [],
}


def test_import_docs_absolute_path_normalized_for_doc_node_anchor_lookup(tmp_path, monkeypatch):
    # A caller (e.g. the CLI's `--docs`) may pass an ABSOLUTE path; the doc's OWN file-node
    # anchor only resolves against the graph's REPO-RELATIVE `source_file`, so the lookup
    # must normalize an absolute path against the cwd first. Here the doc has NO backticked
    # mentions at all, so the doc-node is the ONLY possible anchor — without normalization
    # this would be `skipped_unanchorable`.
    monkeypatch.chdir(tmp_path)
    reader = _reader(tmp_path, DOC_NODE_ONLY_GRAPH)
    store = Store(tmp_path / "s.db")
    doc = _adr("Doc with no mentions", decision="No backticked mentions in this doc at all.")
    md_path = _write_md(tmp_path, "docs/a.md", doc)

    report = import_docs(store, reader, [str(md_path)], any_doc=True)  # ABSOLUTE path in
    assert report.imported == 1
    assert report.skipped_unanchorable == 0
    d = next(store.iter_decisions())
    leaf_names = {
        store.get_entity(b.entity_id).canonical_name
        for b in store.bindings_for_record(d.id)
        if b.tier == 2
    }
    assert leaf_names == {"a.md"}


def test_import_docs_document_not_in_graph_mention_anchors_still_work(tmp_path):
    reader = _reader(tmp_path, IDENTIFIER_GRAPH)  # no document-type node for docs/a.md at all
    store = Store(tmp_path / "s.db")
    doc = _adr("Submit path", decision="Calls `submit_order` on success.")
    _write_md(tmp_path, "docs/a.md", doc)

    report = import_docs(store, reader, [str(tmp_path / "docs" / "a.md")], any_doc=True)
    assert report.imported == 1
    d = next(store.iter_decisions())
    leaf_names = {
        store.get_entity(b.entity_id).canonical_name
        for b in store.bindings_for_record(d.id)
        if b.tier == 2
    }
    assert leaf_names == {"submit_order"}


EMPTY_GRAPH = {"nodes": [], "links": []}


def test_import_docs_no_anchor_at_all_skipped_unanchorable(tmp_path):
    reader = _reader(tmp_path, EMPTY_GRAPH)
    store = Store(tmp_path / "s.db")
    doc = _adr("Nothing anchorable here", decision="No mentions, no matching doc node.")
    _write_md(tmp_path, "docs/a.md", doc)

    report = import_docs(store, reader, [str(tmp_path / "docs" / "a.md")], any_doc=True)
    assert report.imported == 0
    assert report.skipped_unanchorable == 1
    assert list(store.iter_decisions()) == []


def test_import_docs_not_decision_shaped_counted_and_skipped(tmp_path):
    reader = _reader(tmp_path, EMPTY_GRAPH)
    store = Store(tmp_path / "s.db")
    _write_md(tmp_path, "docs/notes.md", FREEFORM_FIXTURE)

    report = import_docs(store, reader, [str(tmp_path / "docs" / "notes.md")], any_doc=True)
    assert report.skipped_not_decision == 1
    assert report.imported == 0


def test_import_docs_superseded_frontmatter_counted_and_skipped(tmp_path):
    reader = _reader(tmp_path, EMPTY_GRAPH)
    store = Store(tmp_path / "s.db")
    _write_md(tmp_path, "docs/old.md", SUPERSEDED_FRONTMATTER_FIXTURE)

    report = import_docs(store, reader, [str(tmp_path / "docs" / "old.md")], any_doc=True)
    assert report.skipped_superseded_frontmatter == 1
    assert report.imported == 0
    assert list(store.iter_decisions()) == []


# ---------------------------------------------------------------------------------------
# import_docs: B1 — status-derived `proposed` override (calibrated against a real ADR
# corpus: every source ADR there carries `status: draft` in YAML frontmatter alongside a
# `status_note` like "proposed, pending EACL review", but the recommended --docs path was
# landing all of them `accepted` regardless).
# ---------------------------------------------------------------------------------------


def _adr_with_frontmatter_status(title, status, decision="Calls `submit_order` on success."):
    return (
        f"---\nstatus: {status}\n---\n"
        f"# {title}\n\n## Context\n\nSome context.\n\n## Decision\n\n{decision}\n"
    )


def _adr_with_bold_status(title, status, decision="Calls `submit_order` on success."):
    return (
        f"# {title}\n\n**Status:** {status}\n\n"
        f"## Context\n\nSome context.\n\n## Decision\n\n{decision}\n"
    )


def test_import_docs_frontmatter_draft_status_lands_proposed_without_propose_flag(tmp_path):
    reader = _reader(tmp_path, IDENTIFIER_GRAPH)
    store = Store(tmp_path / "s.db")
    doc = _adr_with_frontmatter_status("Submit path", "draft")
    path = str(_write_md(tmp_path, "docs/a.md", doc))

    report = import_docs(store, reader, [path], any_doc=True)  # no --propose
    assert report.imported == 1
    assert report.status_derived_proposed == 1
    d = next(store.iter_decisions())
    assert d.status == DecisionStatus.PROPOSED


def test_import_docs_frontmatter_draft_status_lands_proposed_even_with_propose_flag(tmp_path):
    # "regardless of --propose" — same outcome whether or not the flag was also passed.
    reader = _reader(tmp_path, IDENTIFIER_GRAPH)
    store = Store(tmp_path / "s.db")
    doc = _adr_with_frontmatter_status("Submit path", "draft")
    path = str(_write_md(tmp_path, "docs/a.md", doc))

    report = import_docs(store, reader, [path], propose=True, any_doc=True)
    assert report.imported == 1
    assert report.status_derived_proposed == 1
    d = next(store.iter_decisions())
    assert d.status == DecisionStatus.PROPOSED


def test_import_docs_status_note_pending_eacl_review_lands_proposed(tmp_path):
    # Mirrors the real corpus's exact phrasing shape: "proposed, pending EACL review".
    reader = _reader(tmp_path, IDENTIFIER_GRAPH)
    store = Store(tmp_path / "s.db")
    doc = _adr_with_frontmatter_status("Submit path", "proposed, pending EACL review")
    path = str(_write_md(tmp_path, "docs/a.md", doc))

    report = import_docs(store, reader, [path], any_doc=True)
    assert report.status_derived_proposed == 1
    d = next(store.iter_decisions())
    assert d.status == DecisionStatus.PROPOSED


def test_import_docs_bold_status_under_review_lands_proposed(tmp_path):
    reader = _reader(tmp_path, IDENTIFIER_GRAPH)
    store = Store(tmp_path / "s.db")
    doc = _adr_with_bold_status("Submit path", "Under review by architecture board")
    path = str(_write_md(tmp_path, "docs/a.md", doc))

    report = import_docs(store, reader, [path], any_doc=True)
    assert report.status_derived_proposed == 1
    d = next(store.iter_decisions())
    assert d.status == DecisionStatus.PROPOSED


def test_import_docs_explicit_accepted_status_lands_accepted_default_path(tmp_path):
    reader = _reader(tmp_path, IDENTIFIER_GRAPH)
    store = Store(tmp_path / "s.db")
    doc = _adr_with_frontmatter_status("Submit path", "Accepted")
    path = str(_write_md(tmp_path, "docs/a.md", doc))

    report = import_docs(store, reader, [path], any_doc=True)
    assert report.status_derived_proposed == 0
    d = next(store.iter_decisions())
    assert d.status == DecisionStatus.ACCEPTED


def test_import_docs_no_status_signal_takes_current_default_path(tmp_path):
    # Absent status: unaffected by B1 — plain --propose-controlled default, as before.
    reader = _reader(tmp_path, IDENTIFIER_GRAPH)
    store = Store(tmp_path / "s.db")
    doc = _adr("Submit path", decision="Calls `submit_order` on success.")
    path = str(_write_md(tmp_path, "docs/a.md", doc))

    report = import_docs(store, reader, [path], any_doc=True)
    assert report.status_derived_proposed == 0
    assert next(store.iter_decisions()).status == DecisionStatus.ACCEPTED


def test_import_docs_no_status_signal_respects_propose_flag(tmp_path):
    reader = _reader(tmp_path, IDENTIFIER_GRAPH)
    store = Store(tmp_path / "s.db")
    doc = _adr("Submit path", decision="Calls `submit_order` on success.")
    path = str(_write_md(tmp_path, "docs/a.md", doc))

    report = import_docs(store, reader, [path], propose=True, any_doc=True)
    assert report.status_derived_proposed == 0
    assert next(store.iter_decisions()).status == DecisionStatus.PROPOSED


def test_import_docs_status_derived_proposed_counted_in_dry_run(tmp_path):
    reader = _reader(tmp_path, IDENTIFIER_GRAPH)
    store = Store(tmp_path / "s.db")
    doc = _adr_with_frontmatter_status("Submit path", "draft")
    path = str(_write_md(tmp_path, "docs/a.md", doc))

    report = import_docs(store, reader, [path], dry_run=True, any_doc=True)
    assert report.imported == 1
    assert report.status_derived_proposed == 1
    assert list(store.iter_decisions()) == []  # dry-run writes nothing


def test_import_docs_status_derived_override_defers_ancestor_close(tmp_path):
    # Mirrors the --propose "predecessor stays open" rule (design §3 option (b)): when the
    # STATUS itself (not --propose) is what lands the successor proposed, an already-open
    # accepted ancestor must still be left untouched until ratification — never silently
    # closed by an unreviewed draft.
    reader = _reader(tmp_path, IDENTIFIER_GRAPH)
    store = Store(tmp_path / "s.db")
    md_path = tmp_path / "docs" / "a.md"
    _write_md(
        tmp_path, "docs/a.md", _adr("Submit path", decision="Calls `submit_order` on success.")
    )
    import_docs(store, reader, [str(md_path)], any_doc=True)  # accepted ancestor, no status signal
    old = next(store.iter_decisions())
    assert old.status == DecisionStatus.ACCEPTED

    md_path.write_text(
        _adr_with_frontmatter_status(
            "Submit path", "draft", decision="Now retries `submit_order` up to 3 times."
        )
    )
    report = import_docs(store, reader, [str(md_path)], any_doc=True)  # no --propose
    assert report.superseded == 1
    assert report.status_derived_proposed == 1

    old_after = store.get_decision(old.id)
    assert old_after.status == DecisionStatus.ACCEPTED  # NOT closed yet
    assert old_after.valid_to is None
    new = next(d for d in store.iter_decisions() if d.id != old.id)
    assert new.status == DecisionStatus.PROPOSED
    assert new.supersedes == old.id


# ---------------------------------------------------------------------------------------
# import_docs: B4 — kind heuristic (a qualifying "Root cause" section -> lesson).
# ---------------------------------------------------------------------------------------


def _lesson_doc(title, decision="Add an idempotency key to `submit_order`."):
    return (
        f"# {title}\n\n## Root cause\n\nA network partition caused a duplicate submit.\n\n"
        f"## Decision\n\n{decision}\n"
    )


def test_import_docs_root_cause_doc_lands_kind_lesson_by_default(tmp_path):
    reader = _reader(tmp_path, IDENTIFIER_GRAPH)
    store = Store(tmp_path / "s.db")
    path = str(_write_md(tmp_path, "docs/a.md", _lesson_doc("Duplicate submit postmortem")))

    report = import_docs(store, reader, [path], any_doc=True)  # no --kind
    assert report.imported == 1
    assert next(store.iter_decisions()).kind == DecisionKind.LESSON


def test_import_docs_explicit_kind_overrides_root_cause_heuristic(tmp_path):
    reader = _reader(tmp_path, IDENTIFIER_GRAPH)
    store = Store(tmp_path / "s.db")
    path = str(_write_md(tmp_path, "docs/a.md", _lesson_doc("Duplicate submit postmortem")))

    report = import_docs(
        store, reader, [path], kind="adr", any_doc=True
    )  # explicit --kind always wins
    assert report.imported == 1
    assert next(store.iter_decisions()).kind == DecisionKind.ADR


def test_import_docs_no_kind_and_no_root_cause_defaults_to_adr(tmp_path):
    reader = _reader(tmp_path, IDENTIFIER_GRAPH)
    store = Store(tmp_path / "s.db")
    doc = _adr("Submit path", decision="Calls `submit_order` on success.")
    path = str(_write_md(tmp_path, "docs/a.md", doc))

    report = import_docs(store, reader, [path], any_doc=True)
    assert report.imported == 1
    assert next(store.iter_decisions()).kind == DecisionKind.ADR


# ---------------------------------------------------------------------------------------
# import_docs: idempotency / supersession (design §3)
# ---------------------------------------------------------------------------------------


def test_import_docs_idempotent_rerun_skips(tmp_path):
    reader = _reader(tmp_path, IDENTIFIER_GRAPH)
    store = Store(tmp_path / "s.db")
    doc = _adr("Submit path", decision="Calls `submit_order` on success.")
    path = str(_write_md(tmp_path, "docs/a.md", doc))

    first = import_docs(store, reader, [path], any_doc=True)
    assert first.imported == 1

    second = import_docs(store, reader, [path], any_doc=True)
    assert second.imported == 0
    assert second.skipped_existing == 1
    assert len(list(store.iter_decisions())) == 1


def test_import_docs_edited_doc_supersedes_history_retrievable(tmp_path):
    reader = _reader(tmp_path, IDENTIFIER_GRAPH)
    store = Store(tmp_path / "s.db")
    original = _adr("Submit path", decision="Calls `submit_order` on success.")
    md_path = tmp_path / "docs" / "a.md"
    _write_md(tmp_path, "docs/a.md", original)

    first = import_docs(store, reader, [str(md_path)], any_doc=True)
    assert first.imported == 1
    old = next(store.iter_decisions())

    edited = _adr("Submit path", decision="Now retries `submit_order` up to 3 times.")
    md_path.write_text(edited)

    second = import_docs(store, reader, [str(md_path)], any_doc=True)
    assert second.imported == 0
    assert second.superseded == 1

    decisions = list(store.iter_decisions())
    assert len(decisions) == 2
    old_after = store.get_decision(old.id)
    assert old_after.status == DecisionStatus.SUPERSEDED
    new = next(d for d in decisions if d.id != old.id)
    assert new.supersedes == old.id
    assert new.status == DecisionStatus.ACCEPTED
    assert "retries" in new.choice

    # History is retrievable, never deleted.
    assert store.get_decision(old.id) is not None
    assert old.choice in store.get_decision(old.id).choice


def test_import_docs_propose_lands_proposed(tmp_path):
    reader = _reader(tmp_path, IDENTIFIER_GRAPH)
    store = Store(tmp_path / "s.db")
    doc = _adr("Submit path", decision="Calls `submit_order` on success.")
    path = str(_write_md(tmp_path, "docs/a.md", doc))

    report = import_docs(store, reader, [path], propose=True, any_doc=True)
    assert report.imported == 1
    d = next(store.iter_decisions())
    assert d.status == DecisionStatus.PROPOSED


def test_import_docs_propose_edited_doc_predecessor_stays_open(tmp_path):
    # Design §3 option (b) — the current rule: --propose on an edited doc must NOT close
    # an already-accepted predecessor immediately. The successor lands `proposed` with
    # `supersedes` set; the predecessor stays exactly as it was until a human ratifies.
    reader = _reader(tmp_path, IDENTIFIER_GRAPH)
    store = Store(tmp_path / "s.db")
    md_path = tmp_path / "docs" / "a.md"
    _write_md(
        tmp_path, "docs/a.md", _adr("Submit path", decision="Calls `submit_order` on success.")
    )
    import_docs(store, reader, [str(md_path)], any_doc=True)  # default: lands accepted
    old = next(store.iter_decisions())
    assert old.status == DecisionStatus.ACCEPTED

    md_path.write_text(_adr("Submit path", decision="Now retries `submit_order` up to 3 times."))
    report = import_docs(store, reader, [str(md_path)], propose=True, any_doc=True)
    assert report.superseded == 1

    old_after = store.get_decision(old.id)
    assert old_after.status == DecisionStatus.ACCEPTED  # NOT closed yet
    assert old_after.valid_to is None
    new = next(d for d in store.iter_decisions() if d.id != old.id)
    assert new.status == DecisionStatus.PROPOSED
    assert new.supersedes == old.id


def test_import_docs_propose_ratify_accept_closes_predecessor(tmp_path):
    # Ratifying the successor is what actually performs the deferred supersession: the
    # predecessor closes (superseded + valid_to), the successor accepts, and both stay
    # permanently retrievable (append-only history intact).
    reader = _reader(tmp_path, IDENTIFIER_GRAPH)
    store = Store(tmp_path / "s.db")
    md_path = tmp_path / "docs" / "a.md"
    _write_md(
        tmp_path, "docs/a.md", _adr("Submit path", decision="Calls `submit_order` on success.")
    )
    import_docs(store, reader, [str(md_path)], any_doc=True)
    old = next(store.iter_decisions())

    md_path.write_text(_adr("Submit path", decision="Now retries `submit_order` up to 3 times."))
    import_docs(store, reader, [str(md_path)], propose=True, any_doc=True)
    new = next(d for d in store.iter_decisions() if d.id != old.id)
    assert new.status == DecisionStatus.PROPOSED

    store.ratify(new.id)

    old_after = store.get_decision(old.id)
    assert old_after.status == DecisionStatus.SUPERSEDED
    assert old_after.valid_to is not None
    assert "submit_order` on success" in old_after.choice  # history intact, unedited
    new_after = store.get_decision(new.id)
    assert new_after.status == DecisionStatus.ACCEPTED
    assert new_after.supersedes == old.id


def test_import_docs_propose_ratify_drop_leaves_predecessor_untouched(tmp_path):
    reader = _reader(tmp_path, IDENTIFIER_GRAPH)
    store = Store(tmp_path / "s.db")
    md_path = tmp_path / "docs" / "a.md"
    _write_md(
        tmp_path, "docs/a.md", _adr("Submit path", decision="Calls `submit_order` on success.")
    )
    import_docs(store, reader, [str(md_path)], any_doc=True)
    old = next(store.iter_decisions())

    md_path.write_text(_adr("Submit path", decision="Now retries `submit_order` up to 3 times."))
    import_docs(store, reader, [str(md_path)], propose=True, any_doc=True)
    new = next(d for d in store.iter_decisions() if d.id != old.id)

    store.drop(new.id)

    old_after = store.get_decision(old.id)
    assert old_after.status == DecisionStatus.ACCEPTED
    assert old_after.valid_to is None
    assert store.get_decision(new.id).status == DecisionStatus.REJECTED


# ---------------------------------------------------------------------------------------
# import_docs: pending-proposal dedup (D1 fix — see Store.find_decisions_by_ref's
# docstring). Root cause was the old singular find_decision_by_ref returning only the
# FIRST non-superseded match in scan order (typically the older accepted ancestor), so a
# re-run's idempotency check never saw a still-pending proposal whose content actually
# matched, and every re-run proposed a fresh duplicate.
# ---------------------------------------------------------------------------------------


def _open_decisions_at_ref(store, ref):
    return store.find_decisions_by_ref("doc-import", ref)


def test_import_docs_rerun_while_pending_unchanged_skips(tmp_path):
    # (a) Re-running --propose with the SAME (unchanged) edited doc while the proposal
    # from the previous run is still pending must skip — not duplicate the proposal.
    reader = _reader(tmp_path, IDENTIFIER_GRAPH)
    store = Store(tmp_path / "s.db")
    md_path = tmp_path / "docs" / "a.md"
    _write_md(
        tmp_path, "docs/a.md", _adr("Submit path", decision="Calls `submit_order` on success.")
    )
    import_docs(store, reader, [str(md_path)], any_doc=True)  # accepted ancestor
    old = next(store.iter_decisions())

    md_path.write_text(_adr("Submit path", decision="Now retries `submit_order` up to 3 times."))
    first_propose = import_docs(store, reader, [str(md_path)], propose=True, any_doc=True)
    assert first_propose.superseded == 1
    assert len(list(store.iter_decisions())) == 2

    # Re-run with the identical (unedited-since-last-run) doc content, proposal untouched.
    rerun = import_docs(store, reader, [str(md_path)], propose=True, any_doc=True)
    assert rerun.imported == 0
    assert rerun.superseded == 0
    assert rerun.skipped_existing == 1
    decisions = list(store.iter_decisions())
    assert len(decisions) == 2  # zero new rows

    old_after = store.get_decision(old.id)
    assert old_after.status == DecisionStatus.ACCEPTED  # still untouched
    proposed = [d for d in decisions if d.status == DecisionStatus.PROPOSED]
    assert len(proposed) == 1  # not duplicated


def test_import_docs_edit_again_while_pending(tmp_path):
    # (b) Editing the doc AGAIN while a proposal from a previous --propose run is still
    # pending must reject the stale proposal and create exactly one new proposal whose
    # `supersedes` points at the ACCEPTED ancestor — never at the stale proposal.
    reader = _reader(tmp_path, IDENTIFIER_GRAPH)
    store = Store(tmp_path / "s.db")
    md_path = tmp_path / "docs" / "a.md"
    _write_md(
        tmp_path, "docs/a.md", _adr("Submit path", decision="Calls `submit_order` on success.")
    )
    import_docs(store, reader, [str(md_path)], any_doc=True)
    old = next(store.iter_decisions())

    md_path.write_text(_adr("Submit path", decision="Now retries `submit_order` up to 3 times."))
    import_docs(store, reader, [str(md_path)], propose=True, any_doc=True)
    first_proposal = next(d for d in store.iter_decisions() if d.id != old.id)
    assert first_proposal.status == DecisionStatus.PROPOSED

    # Edit AGAIN before the first proposal is ratified.
    md_path.write_text(
        _adr("Submit path", decision="Now retries `submit_order` up to 5 times with backoff.")
    )
    report = import_docs(store, reader, [str(md_path)], propose=True, any_doc=True)
    assert report.imported == 0
    assert report.superseded == 1

    # The stale first proposal is rejected, not silently orphaned.
    first_after = store.get_decision(first_proposal.id)
    assert first_after.status == DecisionStatus.REJECTED

    decisions = list(store.iter_decisions())
    proposed = [d for d in decisions if d.status == DecisionStatus.PROPOSED]
    assert len(proposed) == 1  # exactly one new proposal
    second_proposal = proposed[0]
    assert second_proposal.id != first_proposal.id
    assert second_proposal.supersedes == old.id  # accepted ancestor, NOT the stale proposal
    assert "backoff" in second_proposal.choice

    # Accepted ancestor is still open, untouched (--propose defers the close to ratify).
    old_after = store.get_decision(old.id)
    assert old_after.status == DecisionStatus.ACCEPTED
    assert old_after.valid_to is None


def test_import_docs_ratify_after_multiple_propose_cycles(tmp_path):
    # (c) Several edit/--propose cycles at the same ref, none ratified in between, then a
    # single ratify at the end: exactly one live accepted record must result, the original
    # ancestor superseded, and the whole rejected/superseded history retrievable.
    reader = _reader(tmp_path, IDENTIFIER_GRAPH)
    store = Store(tmp_path / "s.db")
    md_path = tmp_path / "docs" / "a.md"
    ref = str(md_path)  # provenance.ref is keyed on whatever path string the caller passed
    _write_md(
        tmp_path, "docs/a.md", _adr("Submit path", decision="Calls `submit_order` on success.")
    )
    import_docs(store, reader, [str(md_path)], any_doc=True)
    ancestor = next(store.iter_decisions())

    for i in range(3):
        md_path.write_text(
            _adr("Submit path", decision=f"Retries `submit_order` up to {i + 3} times.")
        )
        import_docs(store, reader, [str(md_path)], propose=True, any_doc=True)
        # Invariant holds at every step along the way, not just at the end.
        open_accepted = [
            d for d in _open_decisions_at_ref(store, ref) if d.status == DecisionStatus.ACCEPTED
        ]
        assert len(open_accepted) == 1

    final_proposal = next(d for d in store.iter_decisions() if d.status == DecisionStatus.PROPOSED)
    assert "5 times" in final_proposal.choice

    store.ratify(final_proposal.id)

    decisions = list(store.iter_decisions())
    accepted = [d for d in decisions if d.status == DecisionStatus.ACCEPTED]
    assert len(accepted) == 1
    assert accepted[0].id == final_proposal.id

    ancestor_after = store.get_decision(ancestor.id)
    assert ancestor_after.status == DecisionStatus.SUPERSEDED
    assert ancestor_after.valid_to is not None

    rejected = [d for d in decisions if d.status == DecisionStatus.REJECTED]
    assert len(rejected) == 2  # the two abandoned intermediate proposals

    # History intact: every intermediate draft is still retrievable, none deleted.
    assert len(decisions) == 4  # ancestor + 2 rejected drafts + final accepted
    for d in decisions:
        assert store.get_decision(d.id) is not None


def test_import_docs_invariant_no_two_open_accepted_across_mixed_propose_runs(tmp_path):
    # Invariant (3): no sequence of import/ratify operations may yield two non-superseded
    # ACCEPTED records at one ref — including a run that mixes --propose and non-propose
    # imports at the same ref while a proposal is pending.
    reader = _reader(tmp_path, IDENTIFIER_GRAPH)
    store = Store(tmp_path / "s.db")
    md_path = tmp_path / "docs" / "a.md"
    ref = str(md_path)  # provenance.ref is keyed on whatever path string the caller passed
    _write_md(
        tmp_path, "docs/a.md", _adr("Submit path", decision="Calls `submit_order` on success.")
    )
    import_docs(store, reader, [str(md_path)], any_doc=True)  # accepted ancestor

    md_path.write_text(_adr("Submit path", decision="Now retries `submit_order` up to 3 times."))
    import_docs(
        store, reader, [str(md_path)], propose=True, any_doc=True
    )  # pending proposal, id P1
    p1 = next(d for d in store.iter_decisions() if d.status == DecisionStatus.PROPOSED)

    # A plain (non --propose) run with yet another edit, while P1 is still pending.
    md_path.write_text(
        _adr("Submit path", decision="Now retries `submit_order` up to 9 times, no backoff.")
    )
    report = import_docs(
        store, reader, [str(md_path)], any_doc=True
    )  # immediate accept, no --propose
    assert report.superseded == 1

    def open_accepted():
        return [
            d for d in _open_decisions_at_ref(store, ref) if d.status == DecisionStatus.ACCEPTED
        ]

    assert len(open_accepted()) == 1

    # If someone still tries to ratify the now-stale P1, the invariant must hold: P1 was
    # already closed (rejected) by the plain import above, so ratify must refuse it rather
    # than minting a second accepted record.
    p1_after = store.get_decision(p1.id)
    assert p1_after.status == DecisionStatus.REJECTED
    try:
        store.ratify(p1.id)
        raised = False
    except ValueError:
        raised = True
    assert raised

    assert len(open_accepted()) == 1


def test_import_docs_rerun_without_propose_does_not_ratify_pending_proposal(tmp_path, monkeypatch):
    """Red against the unfixed branch: an unattended rerun ratified the proposal, bypassing
    the human queue while the report called it a skip."""
    monkeypatch.chdir(tmp_path)
    reader = _reader(tmp_path, IDENTIFIER_GRAPH)
    store = Store(tmp_path / "s.db")
    _write_md(
        tmp_path, "docs/a.md", _adr("Submit path", decision="Calls `submit_order` on success.")
    )

    first = import_docs(store, reader, ["docs/a.md"], any_doc=True, propose=True)
    assert first.imported == 1
    [proposed] = store.find_decisions_by_ref("doc-import", "docs/a.md")
    assert proposed.status == DecisionStatus.PROPOSED

    second = import_docs(store, reader, ["docs/a.md"], any_doc=True)

    assert store.get_decision(proposed.id).status == DecisionStatus.PROPOSED
    assert second.skipped_existing == 1
    assert second.imported == 0


def test_import_docs_dry_run_matches_real_run_over_a_pending_proposal(tmp_path, monkeypatch):
    """Red against the unfixed branch: dry-run predicted 'skipped existing' while the real
    run changed the record's status — and the import-adrs skill gates a human go/no-go on
    exactly that output."""
    monkeypatch.chdir(tmp_path)
    reader = _reader(tmp_path, IDENTIFIER_GRAPH)
    store = Store(tmp_path / "s.db")
    _write_md(
        tmp_path, "docs/a.md", _adr("Submit path", decision="Calls `submit_order` on success.")
    )

    first = import_docs(store, reader, ["docs/a.md"], any_doc=True, propose=True)
    assert first.imported == 1
    before = {d.id: d.status for d in store.find_decisions_by_ref("doc-import", "docs/a.md")}

    dry = import_docs(store, reader, ["docs/a.md"], any_doc=True, dry_run=True)
    after_dry = {d.id: d.status for d in store.find_decisions_by_ref("doc-import", "docs/a.md")}
    real = import_docs(store, reader, ["docs/a.md"], any_doc=True)
    after_real = {d.id: d.status for d in store.find_decisions_by_ref("doc-import", "docs/a.md")}

    assert after_dry == before
    assert after_real == before
    assert (dry.imported, dry.skipped_existing) == (real.imported, real.skipped_existing)


# ---------------------------------------------------------------------------------------
# import_docs: B2 — consequences written + part of the idempotency comparison.
# ---------------------------------------------------------------------------------------


def _adr_with_consequences(title, consequences, decision="Calls `submit_order` on success."):
    return (
        f"# {title}\n\n## Context\n\nc\n\n## Decision\n\n{decision}\n\n"
        f"## Consequences\n\n{consequences}\n"
    )


def test_import_docs_consequences_written_to_decision(tmp_path):
    reader = _reader(tmp_path, IDENTIFIER_GRAPH)
    store = Store(tmp_path / "s.db")
    doc = _adr_with_consequences("Submit path", "Retries add latency under failure.")
    path = str(_write_md(tmp_path, "docs/a.md", doc))

    report = import_docs(store, reader, [path], any_doc=True)
    assert report.imported == 1
    d = next(store.iter_decisions())
    assert d.consequences is not None
    assert "Retries add latency" in d.consequences


def test_import_docs_consequences_only_change_triggers_supersession(tmp_path):
    # Before B2, `consequences` was excluded from BOTH extraction and the idempotency
    # comparison; now that it's a real captured field, a doc whose ONLY change is its
    # Consequences section must still count as edited, not silently skipped_existing.
    reader = _reader(tmp_path, IDENTIFIER_GRAPH)
    store = Store(tmp_path / "s.db")
    md_path = tmp_path / "docs" / "a.md"
    _write_md(tmp_path, "docs/a.md", _adr_with_consequences("Submit path", "Original consequence."))
    import_docs(store, reader, [str(md_path)], any_doc=True)
    old = next(store.iter_decisions())

    md_path.write_text(_adr_with_consequences("Submit path", "Updated consequence text."))
    report = import_docs(store, reader, [str(md_path)], any_doc=True)
    assert report.superseded == 1
    assert report.skipped_existing == 0

    new = next(d for d in store.iter_decisions() if d.id != old.id)
    assert new.consequences is not None
    assert "Updated consequence" in new.consequences


# ---------------------------------------------------------------------------------------
# import_docs: B3 — raising --section-limit / the default cap re-parses fuller content and
# supersedes the old, tighter-capped record (evolution via the existing supersession path).
# ---------------------------------------------------------------------------------------


def test_import_docs_raised_section_limit_supersedes_tighter_capped_record(tmp_path):
    reader = _reader(tmp_path, IDENTIFIER_GRAPH)
    store = Store(tmp_path / "s.db")
    long_choice = "Calls `submit_order` on success. " + ("word " * 200)  # >1000 chars
    doc = _adr("Submit path", decision=long_choice)
    path = str(_write_md(tmp_path, "docs/a.md", doc))

    first = import_docs(store, reader, [path], section_limit=200, any_doc=True)
    assert first.imported == 1
    old = next(store.iter_decisions())
    assert old.choice.endswith(" …[truncated]")
    assert len(old.choice) < len(long_choice)

    # Same file, unchanged on disk — re-parsed with a HIGHER cap yields more/different
    # content, so the existing edited-doc supersession path (design §3) picks it up.
    second = import_docs(store, reader, [path], section_limit=2000, any_doc=True)
    assert second.imported == 0
    assert second.superseded == 1

    decisions = list(store.iter_decisions())
    assert len(decisions) == 2
    old_after = store.get_decision(old.id)
    assert old_after.status == DecisionStatus.SUPERSEDED
    new = next(d for d in decisions if d.id != old.id)
    assert new.supersedes == old.id
    assert new.status == DecisionStatus.ACCEPTED
    assert not new.choice.endswith(" …[truncated]")
    assert long_choice.strip() in new.choice


def test_import_docs_raised_section_limit_context_only_change_supersedes(tmp_path):
    # Review follow-up (Important 1): the idempotency tuple excluded `context` — a doc
    # whose ONLY over-cap field is context (title/choice/rejected/consequences all short
    # and unaffected) was silently `skipped_existing` after a --section-limit raise,
    # permanently keeping the tighter-capped context instead of superseding it.
    reader = _reader(tmp_path, IDENTIFIER_GRAPH)
    store = Store(tmp_path / "s.db")
    long_context = "Some context text. " + ("word " * 200)  # >1000 chars
    doc = _adr("Submit path", context=long_context, decision="Calls `submit_order` on success.")
    path = str(_write_md(tmp_path, "docs/a.md", doc))

    first = import_docs(store, reader, [path], section_limit=200, any_doc=True)
    assert first.imported == 1
    old = next(store.iter_decisions())
    assert old.context.split("\n\nimported from")[0].endswith(" …[truncated]")

    second = import_docs(store, reader, [path], section_limit=2000, any_doc=True)
    assert second.skipped_existing == 0
    assert second.superseded == 1

    decisions = list(store.iter_decisions())
    assert len(decisions) == 2
    new = next(d for d in decisions if d.id != old.id)
    assert new.supersedes == old.id
    context_body = new.context.split("\n\nimported from")[0]
    assert not context_body.endswith(" …[truncated]")
    assert long_context.strip() in context_body


def test_import_docs_tags_bind_durable_tag_entities(tmp_path):
    reader = _reader(tmp_path, IDENTIFIER_GRAPH)
    store = Store(tmp_path / "s.db")
    doc = _adr("Submit path", decision="Calls `submit_order` on success.")
    path = str(_write_md(tmp_path, "docs/a.md", doc))

    report = import_docs(store, reader, [path], tags=["Payments", "Retry Policy"], any_doc=True)
    assert report.imported == 1
    d = next(store.iter_decisions())
    tag_names = {
        store.get_entity(b.entity_id).canonical_name
        for b in store.bindings_for_record(d.id)
        if b.tier == 0
    }
    assert tag_names == {"tag:payments", "tag:retry-policy"}


def test_import_docs_dry_run_writes_nothing(tmp_path):
    reader = _reader(tmp_path, IDENTIFIER_GRAPH)
    store = Store(tmp_path / "s.db")
    doc = _adr("Submit path", decision="Calls `submit_order` on success.")
    path = str(_write_md(tmp_path, "docs/a.md", doc))

    report = import_docs(store, reader, [path], dry_run=True, any_doc=True)
    assert report.imported == 1
    assert list(store.iter_decisions()) == []
    assert list(store.iter_concrete_entities()) == []


def test_import_docs_directory_recursion(tmp_path):
    reader = _reader(tmp_path, IDENTIFIER_GRAPH)
    store = Store(tmp_path / "s.db")
    _write_md(
        tmp_path, "docs/a.md", _adr("Top level doc", decision="Calls `submit_order` directly.")
    )
    _write_md(
        tmp_path, "docs/sub/b.md", _adr("Nested doc", decision="Also calls `submit_order` here.")
    )
    _write_md(tmp_path, "docs/sub/notes.md", FREEFORM_FIXTURE)

    report = import_docs(store, reader, [str(tmp_path / "docs")], any_doc=True)
    assert report.imported == 2
    assert report.skipped_not_decision == 1


def test_import_docs_limit_caps_processed_files(tmp_path):
    reader = _reader(tmp_path, IDENTIFIER_GRAPH)
    store = Store(tmp_path / "s.db")
    _write_md(tmp_path, "docs/a.md", _adr("Doc A", decision="Calls `submit_order` here."))
    _write_md(tmp_path, "docs/b.md", _adr("Doc B", decision="Also calls `submit_order` here."))

    report = import_docs(store, reader, [str(tmp_path / "docs")], limit=1, any_doc=True)
    assert report.imported == 1


# ---------------------------------------------------------------------------------------
# import_docs: D7.1 — import glob enforcement (design/superpowers/specs/
# 2026-07-30-staleness-machinery-design.md). The enforcement layer lives inside
# import_docs itself (at per-file enumeration), so it is exercised directly here rather
# than only through the CLI — see test_cli_import.py for the CLI-level golden red test
# (the E8 shape: a graph report under graphify-out/).
# ---------------------------------------------------------------------------------------


def test_import_docs_default_skips_file_outside_profile_globs(tmp_path, monkeypatch):
    """A decision-shaped doc living OUTSIDE the active profile's declared scope (here,
    generic-adr's docs/adr/*.md + docs/decisions/*.md) is never even opened -- skipped and
    counted skipped_outside_profile, never parsed or anchor-resolved."""
    monkeypatch.chdir(tmp_path)
    reader = _reader(tmp_path, IDENTIFIER_GRAPH)
    store = Store(tmp_path / "s.db")
    _write_md(tmp_path, "graphify-out/report.md", _adr("Report", decision="Calls `submit_order`."))

    report = import_docs(store, reader, ["graphify-out/report.md"])
    assert report.imported == 0
    assert report.skipped_outside_profile == 1
    assert list(store.iter_decisions()) == []


def test_import_docs_any_doc_restores_no_filter_behavior(tmp_path, monkeypatch):
    """`any_doc=True` restores the pre-D7.1 no-filter behavior -- the same doc that D7.1's
    default skips (above) imports normally."""
    monkeypatch.chdir(tmp_path)
    reader = _reader(tmp_path, IDENTIFIER_GRAPH)
    store = Store(tmp_path / "s.db")
    _write_md(tmp_path, "graphify-out/report.md", _adr("Report", decision="Calls `submit_order`."))

    report = import_docs(store, reader, ["graphify-out/report.md"], any_doc=True)
    assert report.imported == 1
    assert report.skipped_outside_profile == 0


def test_import_docs_file_inside_profile_globs_still_imports_by_default(tmp_path, monkeypatch):
    """A doc that DOES match the active profile's ingest_globs is unaffected by D7.1's
    default enforcement -- the gate only ever excludes, never additionally restricts an
    already-in-scope file."""
    monkeypatch.chdir(tmp_path)
    reader = _reader(tmp_path, IDENTIFIER_GRAPH)
    store = Store(tmp_path / "s.db")
    _write_md(tmp_path, "docs/adr/0001.md", _adr("Use SQLite", decision="Calls `submit_order`."))

    report = import_docs(store, reader, ["docs/adr/0001.md"])
    assert report.imported == 1
    assert report.skipped_outside_profile == 0


def test_import_docs_directory_recursion_filters_out_of_profile_files(tmp_path, monkeypatch):
    """The CLI's explicit-path branch bypasses profile scoping entirely and may pass a
    DIRECTORY (design D7.1's own rationale for placing enforcement inside import_docs,
    at per-file enumeration, rather than at the CLI layer) -- a directory containing both
    an in-profile and an out-of-profile doc imports only the former."""
    monkeypatch.chdir(tmp_path)
    reader = _reader(tmp_path, IDENTIFIER_GRAPH)
    store = Store(tmp_path / "s.db")
    _write_md(tmp_path, "docs/adr/0001.md", _adr("Use SQLite", decision="Calls `submit_order`."))
    _write_md(tmp_path, "graphify-out/report.md", _adr("Report", decision="Calls `submit_order`."))

    report = import_docs(store, reader, ["."])
    assert report.imported == 1
    assert report.skipped_outside_profile == 1
    assert next(store.iter_decisions()).title == "Use SQLite"


def test_import_docs_report_field_default_zero(tmp_path):
    """DocImportReport.skipped_outside_profile defaults to 0 -- additive field, no existing
    caller inspecting the report shape breaks."""
    from sidegraph.doc_import import DocImportReport

    assert DocImportReport().skipped_outside_profile == 0


# ---------------------------------------------------------------------------------------
# import_docs: D7.1 BLOCKING-1 (code review) -- the glob gate must normalize against the
# REPO ROOT, never the process cwd. ingest_globs are repo-root-relative patterns; a caller
# running import_docs from a subdirectory, or passing a path from anywhere but the repo
# root (including through a symlinked prefix), must still resolve correctly.
# ---------------------------------------------------------------------------------------


def _git_repo(tmp_path) -> Path:
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    return repo


def test_import_docs_in_profile_doc_matches_from_a_non_root_cwd(tmp_path, monkeypatch):
    """A relative --docs path, given while running from a SUBDIRECTORY of the repo (not
    the root), must still be recognized as in-profile -- the glob check normalizes
    against the repo root, never the process cwd (BLOCKING-1)."""
    repo = _git_repo(tmp_path)
    reader = _reader(repo, IDENTIFIER_GRAPH)
    store = Store(tmp_path / "s.db")
    _write_md(repo, "docs/adr/0001.md", _adr("Use SQLite", decision="Calls `submit_order`."))

    monkeypatch.chdir(repo / "docs")  # NOT the repo root
    report = import_docs(store, reader, ["adr/0001.md"])  # relative to cwd, not repo root
    assert report.imported == 1
    assert report.skipped_outside_profile == 0


def test_import_docs_absolute_path_matches_from_a_non_root_cwd(tmp_path, monkeypatch):
    """Same shape as the relative-path case above, but the --docs path is absolute --
    the OLD (BLOCKING-1) code relativized an absolute path against os.getcwd(), which is
    wrong from any cwd but the repo root."""
    repo = _git_repo(tmp_path)
    reader = _reader(repo, IDENTIFIER_GRAPH)
    store = Store(tmp_path / "s.db")
    doc = _write_md(repo, "docs/adr/0001.md", _adr("Use SQLite", decision="Calls `submit_order`."))

    monkeypatch.chdir(repo / "docs")
    report = import_docs(store, reader, [str(doc)])
    assert report.imported == 1
    assert report.skipped_outside_profile == 0


def test_import_docs_symlinked_prefix_path_still_matches(tmp_path, monkeypatch):
    """A --docs path reached through a symlinked prefix (the macOS /tmp -> /private/tmp
    shape) must still resolve to the same repo-relative form -- both the file and the
    repo root are .resolve()-d before the comparison."""
    repo = _git_repo(tmp_path)
    reader = _reader(repo, IDENTIFIER_GRAPH)
    store = Store(tmp_path / "s.db")
    _write_md(repo, "docs/adr/0001.md", _adr("Use SQLite", decision="Calls `submit_order`."))
    alias = tmp_path / "alias"
    alias.symlink_to(repo)

    monkeypatch.chdir(repo)
    report = import_docs(store, reader, [str(alias / "docs" / "adr" / "0001.md")])
    assert report.imported == 1
    assert report.skipped_outside_profile == 0


# -- D7.1 CORRECTION-5 (code review): _glob_to_regex must not silently mistranslate an
# unsupported glob feature --------------------------------------------------------------


def test_glob_to_regex_rejects_recursive_descent():
    with pytest.raises(ValueError, match=r"\*\*"):
        _glob_to_regex("docs/**/*.md")


def test_glob_to_regex_rejects_character_class():
    with pytest.raises(ValueError, match=r"\[seq\]"):
        _glob_to_regex("docs/adr/[abc]*.md")


def test_no_registered_profile_uses_unsupported_glob_features():
    """A future profile edit that adds `**` or `[seq]` to ingest_globs must fail LOUDLY
    at test time, not as an imports-0 mystery in production."""
    from sidegraph.profiles import PROFILES

    for name, profile in PROFILES.items():
        for pattern in profile.ingest_globs:
            assert "**" not in pattern, f"{name}: {pattern!r} uses unsupported '**'"
            assert "[" not in pattern and "]" not in pattern, (
                f"{name}: {pattern!r} uses unsupported '[seq]'"
            )


# ---------------------------------------------------------------------------------------
# import_docs: a file that is not valid UTF-8 is a named skip, not an aborted run
# (design/superpowers/specs/2026-09-23-doc-import-encoding-design.md D1-D3).
# ---------------------------------------------------------------------------------------

# Real cp1251 bytes -- not valid UTF-8 (or utf-8-sig), so `.decode("utf-8-sig")` raises
# `UnicodeDecodeError` on it. Named to sort BEFORE the good ADR below, since
# `_collect_markdown_files` sorts a directory's files (doc_import.py:1381) -- "the run
# continues" is only actually exercised if the bad file is processed first.
CP1251_FIXTURE = "# Решение\n\nТекст не в UTF-8.\n".encode("cp1251")


def test_import_docs_encoding_undecodable_file_skipped_and_continues(tmp_path):
    reader = _reader(tmp_path, IDENTIFIER_GRAPH)
    store = Store(tmp_path / "s.db")
    bad = tmp_path / "docs" / "0001-legacy.md"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_bytes(CP1251_FIXTURE)
    _write_md(tmp_path, "docs/0002-good.md", _adr("Use SQLite", decision="Wire `submit_order` in."))

    report = import_docs(store, reader, [str(tmp_path / "docs")], any_doc=True)

    assert report.imported == 1
    assert report.skipped_undecodable == 1
    assert report.undecodable_files == [str(bad)]
    # The only kill for M5 (fall through with `decoded = ""` instead of `continue`): an
    # empty string is not decision-shaped either, so a fall-through would ALSO count this
    # file not-decision-shaped, on top of skipped_undecodable.
    assert report.skipped_not_decision == 0
    d = next(store.iter_decisions())
    assert d.title == "Use SQLite"


def test_import_docs_encoding_bom_stripped_and_imports(tmp_path):
    reader = _reader(tmp_path, IDENTIFIER_GRAPH)
    store = Store(tmp_path / "s.db")
    doc = _adr("Use SQLite", decision="Wire `submit_order` in.")
    path = tmp_path / "docs" / "a.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(doc, encoding="utf-8-sig")  # leading BOM

    report = import_docs(store, reader, [str(path)], any_doc=True)

    assert report.imported == 1
    assert report.skipped_not_decision == 0
    d = next(store.iter_decisions())
    assert d.title == "Use SQLite"
    assert "﻿" not in d.title


def test_import_docs_encoding_outside_profile_skipped_before_decode(tmp_path, monkeypatch):
    # D3: the profile-glob check runs BEFORE the decode, so an undecodable file outside the
    # active profile's scope is never even opened -- counted skipped_outside_profile, never
    # skipped_undecodable.
    monkeypatch.chdir(tmp_path)
    reader = _reader(tmp_path, IDENTIFIER_GRAPH)
    store = Store(tmp_path / "s.db")
    bad = tmp_path / "notes" / "bad.md"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_bytes(CP1251_FIXTURE)

    report = import_docs(store, reader, ["notes/bad.md"])

    assert report.skipped_outside_profile == 1
    assert report.skipped_undecodable == 0


# -- rejected-status predicate (v0.2-scope item 7) -------------------------------------------


def test_is_rejected_status_matches_turned_down_values():
    from sidegraph.doc_import import _is_rejected_status

    for value in (
        "rejected",
        "Rejected",
        "Rejected in favour of ADR-9999",
        "proposed, then rejected",
        "superseded, rejected",
        "  REJECTED  ",
    ):
        assert _is_rejected_status(value), value


def test_is_rejected_status_ignores_lookalikes():
    """Measured false positives on the bare-substring version (review finding 3). A false
    `rejected` lands CLOSED and invisible to retrieval, so unlike a false `proposed` there
    is no human queue in which anyone would ever notice it."""
    from sidegraph.doc_import import _is_rejected_status

    for value in (
        None,
        "",
        "accepted",
        "Accepted (rejected alternative: gRPC)",
        "not rejected",
        "un-rejected",
        "rejection criteria defined",
    ):
        assert not _is_rejected_status(value), value


def test_rejected_beats_draft_like_on_a_value_that_reads_as_both():
    from sidegraph.doc_import import _is_draft_like_status, _is_rejected_status

    both = "proposed, then rejected"
    assert _is_draft_like_status(both) and _is_rejected_status(both)


# Two heading shapes measured on real foreign Nygard-style ADR corpora (2026-08-03,
# rancher/turtles 18 ADRs + alphagov/govuk-infrastructure 23 ADRs): option comparisons sit
# under "## Proposed alternatives" (turtles 0009) or "## Options" with "### Option N"
# children (turtles 0016, govuk 0019). Neither matched any existing `rejected` keyword —
# prefix matching means "alternatives" cannot match "Proposed alternatives", and bare
# "Options" is not "options considered" — so the shipped dialect yielded rejected on 0 of
# 41 documents; these two keywords rescue exactly the 3 that carry structured alternatives.
PROPOSED_ALTERNATIVES_FIXTURE = """# 9. Helm chart repository

## Context

Which repository should serve the chart.

## Proposed alternatives

### Option 1 - `rancher/charts`.

Pros: discoverable. Cons: slower release cadence.

### Option 2 - specific rancher-turtles repository.

Pros: full control. Cons: users must add the repository first.

## Decision

Use `rancher/charts`.

## Consequences

Chart releases ride the shared pipeline.
"""

OPTIONS_HEADING_FIXTURE = """# 16. CAPI version pinning strategy

## Context

Prime and community builds need different pins.

## Options

1) Keep existing `config.yaml`, switch by build tag
- Pros: smallest change. Cons: hard to document.

2) No provider pinning, separate providers Helm chart
- Pros: clean split. Cons: more moving parts.

## Decision

Ship the separate providers chart.
"""


def test_parse_proposed_alternatives_heading_fills_rejected():
    p = parse_decision_doc(PROPOSED_ALTERNATIVES_FIXTURE, "docs/adr/0009.md")
    assert p is not None
    assert p.rejected is not None
    assert "rancher-turtles repository" in p.rejected
    assert "Use `rancher/charts`" in p.choice


def test_parse_options_heading_fills_rejected():
    p = parse_decision_doc(OPTIONS_HEADING_FIXTURE, "docs/adr/0016.md")
    assert p is not None
    assert p.rejected is not None
    assert "build tag" in p.rejected
    assert "providers chart" in p.choice


def test_parse_optional_heading_is_not_rejected():
    # Prefix matching must not let the bare "options" keyword swallow "Optional features".
    doc = OPTIONS_HEADING_FIXTURE.replace("## Options", "## Optional features")
    p = parse_decision_doc(doc, "docs/adr/0016.md")
    assert p is not None
    assert p.rejected is None
