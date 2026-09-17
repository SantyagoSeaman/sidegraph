import re
from datetime import UTC, datetime

from sidegraph.retrieval import (
    RetrievalBudget,
    _clip_consequences,
    _clip_line,
    _clip_rejected,
    _fmt_decision,
    rank_decisions,
)
from sidegraph.schema import (
    AnchorBinding,
    Decision,
    DecisionKind,
    DecisionStatus,
    Domain,
    Entity,
    Provenance,
    Scope,
)
from sidegraph.store import Store


def _bound_decision(store, entity_id, title, kind, scope=Scope.REPO):
    d = Decision(
        title=title,
        kind=kind,
        context="c",
        choice=f"do {title}",
        scope=scope,
        # accepted: these tests exercise budget/ranking, not ratification
        status=DecisionStatus.ACCEPTED,
        valid_from=datetime.now(UTC),
        provenance=Provenance(source="manual"),
    )
    store._write_decision(d)
    store._conn.commit()
    store.add_binding(AnchorBinding(record_id=d.id, entity_id=entity_id, tier=2, status="live"))
    return d


def test_rank_mistakes_before_adrs_before_peripheral(tmp_path):
    s = Store(tmp_path / "t.db")
    seed = s.upsert_entity(Entity(canonical_name="Seed"))
    peri = s.upsert_entity(Entity(canonical_name="Peri"))
    _bound_decision(s, seed.entity_id, "gotcha1", DecisionKind.GOTCHA)
    _bound_decision(s, seed.entity_id, "adr1", DecisionKind.ADR)
    _bound_decision(s, peri.entity_id, "peri-adr", DecisionKind.ADR)
    ctx = rank_decisions([seed], [peri], [], s, RetrievalBudget())
    assert any("gotcha1" in m for m in ctx.mistakes)
    assert any("adr1" in d for d in ctx.decisions)
    assert any("peri-adr" in r for r in ctx.related)
    assert ctx.mistakes and ctx.decisions  # buckets populated distinctly


def test_rank_mistakes_bucket_carries_rejected_and_consequences(tmp_path):
    # Fix-wave C, C4: the mistakes-first block is a DIRECT-tier bucket too — a gotcha/
    # lesson/constraint bound to a seed must render its rejected AND consequences, not just
    # its choice snippet.
    s = Store(tmp_path / "t.db")
    seed = s.upsert_entity(Entity(canonical_name="Seed"))
    d = Decision(
        title="retry storm gotcha",
        kind=DecisionKind.GOTCHA,
        context="c",
        choice="Retries without backoff caused a thundering herd.",
        rejected="Considered a global rate limiter but it added a single point of failure.",
        consequences="Added jittered exponential backoff to every retry path.",
        status=DecisionStatus.ACCEPTED,
        valid_from=datetime.now(UTC),
        provenance=Provenance(source="manual"),
    )
    s._write_decision(d)
    s._conn.commit()
    s.add_binding(AnchorBinding(record_id=d.id, entity_id=seed.entity_id, tier=2, status="live"))

    ctx = rank_decisions([seed], [], [], s, RetrievalBudget())
    line = next(x for x in ctx.mistakes if "retry storm gotcha" in x)
    assert "single point of failure" in line
    assert "(rejected: " in line
    assert "jittered exponential backoff" in line
    assert "(consequences: " in line


def test_rank_memory_budget_keeps_mistakes_first(tmp_path):
    s = Store(tmp_path / "t.db")
    seed = s.upsert_entity(Entity(canonical_name="Seed"))
    _bound_decision(s, seed.entity_id, "gotchaX", DecisionKind.GOTCHA)
    _bound_decision(s, seed.entity_id, "adrY", DecisionKind.ADR)
    # Direct-tier entries now also carry a "(context: c)" suffix (fix-wave C, C1); the
    # detailed-tier id suffix (design D4) survives even the tight degrade-before-drop
    # render (~33 chars, "gotchaX"'s tight line is 30) -- so the one-line budget needs
    # room for exactly one degraded-plus-id mistake line, but not two.
    tiny = RetrievalBudget(memory_chars=70)  # room for ~one degraded+id line, not two
    ctx = rank_decisions([seed], [], [], s, tiny)
    assert any("gotchaX" in m for m in ctx.mistakes)
    total = sum(len(x) for x in ctx.mistakes + ctx.decisions + ctx.related)
    assert total <= 70


def test_rank_superseded_renders_without_double_prefix(tmp_path):
    s = Store(tmp_path / "t.db")
    seed = s.upsert_entity(Entity(canonical_name="Seed"))
    d = Decision(
        title="old approach",
        kind=DecisionKind.ADR,
        context="c",
        choice="do old",
        status=DecisionStatus.SUPERSEDED,
        valid_from=datetime.now(UTC),
        # no valid_to: this fixture exercises _reverted_prefix's D4 fallback branch (the
        # bare, undated prefix), not the dated one -- see test_rank_superseded_with_
        # valid_to_renders_dated_prefix below for the dated case.
        provenance=Provenance(source="manual"),
    )
    s._write_decision(d)
    s._conn.commit()
    s.add_binding(AnchorBinding(record_id=d.id, entity_id=seed.entity_id, tier=2, status="live"))
    ctx = rank_decisions([seed], [], [], s, RetrievalBudget())
    line = next(r for r in ctx.related if "old approach" in r)
    # Fixture carries no valid_to, so this takes D4's fallback branch -- unchanged by the
    # temporal-markers dated prefix. Do not "fix" this into the dated form.
    assert line.startswith("~ tried, reverted: [adr]")
    assert "~ tried, reverted: - [" not in line


def test_rank_superseded_dedupes_title_choice_under_reverted_prefix(tmp_path):
    s = Store(tmp_path / "t.db")
    seed = s.upsert_entity(Entity(canonical_name="Seed"))
    d = Decision(
        title="single-line rationale",
        kind=DecisionKind.ADR,
        context="c",
        choice="single-line rationale",
        status=DecisionStatus.SUPERSEDED,
        valid_from=datetime.now(UTC),
        # no valid_to: this fixture exercises D4's fallback branch too -- see the note in
        # test_rank_superseded_renders_without_double_prefix above.
        provenance=Provenance(source="import"),
    )
    s._write_decision(d)
    s._conn.commit()
    s.add_binding(AnchorBinding(record_id=d.id, entity_id=seed.entity_id, tier=2, status="live"))
    ctx = rank_decisions([seed], [], [], s, RetrievalBudget())
    line = next(r for r in ctx.related if "single-line rationale" in r)
    # Fixture carries no valid_to, so this takes D4's fallback branch -- unchanged by the
    # temporal-markers dated prefix. Do not "fix" this into the dated form.
    assert line == "~ tried, reverted: [adr] single-line rationale"
    assert ": single-line rationale" not in line


def test_rank_superseded_with_valid_to_renders_dated_prefix(tmp_path):
    # Temporal markers, test 1 (the spec's only genuinely unfixed target): a superseded
    # record that DOES carry valid_to renders a "YYYY-MM" marker in the prefix. No existing
    # fixture sets valid_to (both fixtures above are undated), so this is the one new
    # fixture the change needs.
    s = Store(tmp_path / "t.db")
    seed = s.upsert_entity(Entity(canonical_name="Seed"))
    d = Decision(
        title="dated old approach",
        kind=DecisionKind.ADR,
        context="c",
        choice="do dated old",
        status=DecisionStatus.SUPERSEDED,
        valid_from=datetime(2025, 6, 1, tzinfo=UTC),
        valid_to=datetime(2026, 1, 15, tzinfo=UTC),
        provenance=Provenance(source="manual"),
    )
    s._write_decision(d)
    s._conn.commit()
    s.add_binding(AnchorBinding(record_id=d.id, entity_id=seed.entity_id, tier=2, status="live"))
    ctx = rank_decisions([seed], [], [], s, RetrievalBudget())
    line = next(r for r in ctx.related if "dated old approach" in r)
    assert line.startswith("~ tried, reverted 2026-01: [adr]")


def test_rank_superseded_prefix_paired_bare_and_dated(tmp_path):
    # Temporal markers, test 2 (discarded -- red only against a formatter that prints None,
    # an empty slot, or raises). Paired in one test so the bare-prefix half is checked
    # alongside a dated sibling from the SAME render pass, not in isolation -- the absence
    # half alone already passes today via D4's fallback, which is why the spec pairs it.
    s = Store(tmp_path / "t.db")
    seed = s.upsert_entity(Entity(canonical_name="Seed"))
    bare = Decision(
        title="bare old approach",
        kind=DecisionKind.ADR,
        context="c",
        choice="do bare old",
        status=DecisionStatus.SUPERSEDED,
        valid_from=datetime(2025, 6, 1, tzinfo=UTC),
        provenance=Provenance(source="manual"),
    )
    dated = Decision(
        title="dated sibling approach",
        kind=DecisionKind.ADR,
        context="c",
        choice="do dated sibling",
        status=DecisionStatus.SUPERSEDED,
        valid_from=datetime(2025, 6, 1, tzinfo=UTC),
        valid_to=datetime(2026, 3, 1, tzinfo=UTC),
        provenance=Provenance(source="manual"),
    )
    for record in (bare, dated):
        s._write_decision(record)
    s._conn.commit()
    for record in (bare, dated):
        s.add_binding(
            AnchorBinding(record_id=record.id, entity_id=seed.entity_id, tier=2, status="live")
        )
    ctx = rank_decisions([seed], [], [], s, RetrievalBudget())
    bare_line = next(r for r in ctx.related if "bare old approach" in r)
    dated_line = next(r for r in ctx.related if "dated sibling approach" in r)
    assert bare_line.startswith("~ tried, reverted: [adr]")
    assert dated_line.startswith("~ tried, reverted 2026-03: [adr]")


def test_rank_superseded_dedupe_applies_under_dated_prefix(tmp_path):
    # Temporal markers, test 3: _fmt_decision's title/choice dedup must still collapse
    # under a COMPUTED dated prefix, not just the old literal one. Red before this change --
    # the exact-equality assertion below pins the dated "2026-02" prefix, which unfixed code
    # doesn't produce -- even though the dedup property it exercises (_fmt_decision itself)
    # is untouched by the change. Kept on as a guard going forward: a future refactor could
    # bypass the dedup once the prefix is computed rather than literal.
    s = Store(tmp_path / "t.db")
    seed = s.upsert_entity(Entity(canonical_name="Seed"))
    d = Decision(
        title="dated rationale",
        kind=DecisionKind.ADR,
        context="c",
        choice="dated rationale",
        status=DecisionStatus.SUPERSEDED,
        valid_from=datetime(2025, 6, 1, tzinfo=UTC),
        valid_to=datetime(2026, 2, 1, tzinfo=UTC),
        provenance=Provenance(source="import"),
    )
    s._write_decision(d)
    s._conn.commit()
    s.add_binding(AnchorBinding(record_id=d.id, entity_id=seed.entity_id, tier=2, status="live"))
    ctx = rank_decisions([seed], [], [], s, RetrievalBudget())
    line = next(r for r in ctx.related if "dated rationale" in r)
    assert line == "~ tried, reverted 2026-02: [adr] dated rationale"


def test_rank_live_decision_lines_carry_no_date(tmp_path):
    # Temporal markers, test 4 (discarded -- red only against an implementation that dates
    # every line, D1's obvious over-reach). The dated prefix is scoped to the superseded
    # one-liner only; live mistake/ADR lines keep their plain "- " prefix.
    s = Store(tmp_path / "t.db")
    seed = s.upsert_entity(Entity(canonical_name="Seed"))
    _bound_decision(s, seed.entity_id, "live gotcha", DecisionKind.GOTCHA)
    _bound_decision(s, seed.entity_id, "live adr", DecisionKind.ADR)
    ctx = rank_decisions([seed], [], [], s, RetrievalBudget())
    assert ctx.mistakes and ctx.decisions
    assert all(line.startswith("- [") for line in ctx.mistakes + ctx.decisions)
    # Prefix shape alone isn't the spec's requirement -- "no date appears" is. An
    # implementation that appended a date elsewhere in the line (not the prefix) would
    # still pass the startswith check above, so also assert no date-shaped substring
    # occurs anywhere in the rendered lines. Scoped to the lines THIS test builds, not
    # arbitrary fixture content: the only date-producing mechanism is _reverted_prefix,
    # used at exactly one call site, so the remaining false-positive risk is a decision's
    # own text containing a date -- and the fixture titles/choices above ("live gotcha",
    # "live adr", "do live gotcha", "do live adr") contain no date-shaped substring.
    assert not any(re.search(r"20\d\d-\d\d", line) for line in ctx.mistakes + ctx.decisions)


def test_rank_direct_bucket_long_choice_clipped_at_1200_word_boundary(tmp_path):
    # Fix-wave C, C1: a decision bound DIRECTLY to a seed (bucket B, `ctx.decisions`) gets
    # the generous ~1200-char allowance, not the tight 240-char related clip — cut at a word
    # boundary with an ellipsis marker, never a silent mid-word amputation like
    # "long-only" -> "Lo".
    s = Store(tmp_path / "t.db")
    seed = s.upsert_entity(Entity(canonical_name="Seed"))
    long_choice = "Long-only router. " + ("word " * 300)  # far past 1200 chars
    d = Decision(
        title="router design",
        kind=DecisionKind.ADR,
        context="c",
        choice=long_choice,
        rejected="Rejected alternative. " + ("also " * 150),
        consequences="Consequence detail. " + ("cons " * 150),
        status=DecisionStatus.ACCEPTED,
        valid_from=datetime.now(UTC),
        provenance=Provenance(source="manual"),
    )
    s._write_decision(d)
    s._conn.commit()
    s.add_binding(AnchorBinding(record_id=d.id, entity_id=seed.entity_id, tier=2, status="live"))

    ctx = rank_decisions([seed], [], [], s, RetrievalBudget(memory_chars=10_000))
    line = next(x for x in ctx.decisions if "router design" in x)
    assert long_choice[:240] in line  # far more than the old tight clip survives
    assert long_choice[:1200] not in line  # but still genuinely clipped, not unbounded
    assert line.count("Long-only router.") == 1
    assert "…" in line
    # word-boundary cut: the clipped prefix is itself a clean prefix of the source text,
    # immediately followed by a space in the ORIGINAL — never a fragment of a longer word.
    choice_part = line.split(": ", 1)[1].split(" (context:")[0]
    clipped_prefix = choice_part[: choice_part.index("…")].rstrip()
    assert long_choice.startswith(clipped_prefix)
    assert long_choice[len(clipped_prefix)] == " "
    assert len(clipped_prefix) <= 1200
    # rejected AND consequences both survive (clipped, but present) — the direct tier's
    # generous per-side allowance, in the existing "(rejected: ...)"/"(consequences: ...)"
    # suffix style.
    assert "(rejected: " in line
    assert "also" in line
    assert "(consequences: " in line
    assert "cons" in line


def test_rank_default_budget_fits_two_realistic_direct_entries(tmp_path):
    # Critical regression, caught by live verification against a real private ADR corpus
    # (fix-wave C, C5): the generous per-field clips (choice/context <=1200 each,
    # rejected/consequences <=400 each) can sum to ~2400-2900 chars for ONE real-world
    # ADR-scale decision (long context + a full comparison-table choice + populated
    # consequences) — comfortably MORE than the OLD default memory_chars=2000 budget. With
    # the default left at 2000, `add()`'s all-or-nothing check dropped the seed-anchored
    # decision WHOLE, the opposite of C1's intent (verified live: both ADR-002's `## Decisions`
    # entry for Q2 and ADR-006/ADR-007's for Q4 vanished entirely under the old default).
    # The default budget must be large enough that get_task_context's actual seeded use case
    # — two seed files landing two direct entries this size, in the SAME call — both survive.
    s = Store(tmp_path / "t.db")
    seed_a = s.upsert_entity(Entity(canonical_name="SeedA"))
    seed_b = s.upsert_entity(Entity(canonical_name="SeedB"))

    def _realistic_decision(title):
        return Decision(
            title=title,
            kind=DecisionKind.ADR,
            context="Background context. " + ("ctx " * 450),  # ~2000 chars, real-ADR scale
            choice="Chosen option summary. " + ("row " * 300),  # ~1200+ chars
            consequences="Consequence detail. " + ("cons " * 400),  # ~2000 chars
            status=DecisionStatus.ACCEPTED,
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual"),
        )

    d_a = _realistic_decision("adr a")
    d_b = _realistic_decision("adr b")
    s._write_decision(d_a)
    s._write_decision(d_b)
    s._conn.commit()
    s.add_binding(
        AnchorBinding(record_id=d_a.id, entity_id=seed_a.entity_id, tier=2, status="live")
    )
    s.add_binding(
        AnchorBinding(record_id=d_b.id, entity_id=seed_b.entity_id, tier=2, status="live")
    )

    ctx = rank_decisions([seed_a, seed_b], [], [], s, RetrievalBudget())  # DEFAULT budget
    assert any("adr a" in x for x in ctx.decisions)
    assert any("adr b" in x for x in ctx.decisions)


def _maxed_field_decision(title, kind=DecisionKind.ADR):
    """A decision with all four fields at (roughly) doc_import's own 2000-char section cap —
    renders to a ~3300+ char detailed line, comfortably more than an explicit small budget
    and enough that a few of them can exceed even the generous 6000-char default."""
    return Decision(
        title=title,
        kind=kind,
        context="Background. " + ("ctx " * 500),  # >1200 chars after the direct-tier clip
        choice="Chosen thing. " + ("row " * 400),  # >1200 chars after the direct-tier clip
        rejected="Rejected alt. " + ("alt " * 150),  # >400 chars after the direct-tier clip
        consequences="Consequence. " + ("cons " * 150),  # >400 chars after the direct-tier clip
        status=DecisionStatus.ACCEPTED,
        valid_from=datetime.now(UTC),
        provenance=Provenance(source="manual"),
    )


def test_rank_mistakes_degrades_to_tight_tier_instead_of_dropping_under_small_budget(tmp_path):
    # Review follow-up (Important 1): the all-or-nothing drop inverted mistakes-first under
    # an explicit small budget — a maxed-field gotcha's ~3300+ char detailed line didn't fit
    # a 2000-char budget and was dropped WHOLE, while a lower-priority Related one-liner
    # (needing far less room) still rendered. Degrading to the tight tier before dropping
    # preserves mistakes-first at any budget: the mistake must still appear (as its tight
    # one-liner), not vanish in favor of something less important.
    s = Store(tmp_path / "t.db")
    seed = s.upsert_entity(Entity(canonical_name="Seed"))
    maxed = _maxed_field_decision("max gotcha", kind=DecisionKind.GOTCHA)
    s._write_decision(maxed)
    s._conn.commit()
    s.add_binding(
        AnchorBinding(record_id=maxed.id, entity_id=seed.entity_id, tier=2, status="live")
    )

    comm_entity = s.get_or_create_abstract_entity("community:5")
    minor = Decision(
        title="minor related",
        kind=DecisionKind.ADR,
        context="c",
        choice="Some minor related content.",
        status=DecisionStatus.ACCEPTED,
        valid_from=datetime.now(UTC),
        provenance=Provenance(source="manual"),
    )
    s._write_decision(minor)
    s._conn.commit()
    s.add_binding(
        AnchorBinding(record_id=minor.id, entity_id=comm_entity.entity_id, tier=1, status="live")
    )

    ctx = rank_decisions([seed], [], ["5"], s, RetrievalBudget(memory_chars=2000))
    mistake_line = next((x for x in ctx.mistakes if "max gotcha" in x), None)
    assert mistake_line is not None  # degraded to tight tier, NOT dropped
    assert "(context: " not in mistake_line
    assert "(rejected: " not in mistake_line
    assert "(consequences: " not in mistake_line


def test_rank_default_budget_degrades_third_maxed_entry_instead_of_dropping(tmp_path):
    # Review follow-up (Important 1): even the generous 6000-char default can be exceeded
    # by several maxed-field direct entries at once (2 x ~3300 > 6000) — the third must
    # degrade to its tight one-liner rather than vanish entirely.
    s = Store(tmp_path / "t.db")
    seeds = [s.upsert_entity(Entity(canonical_name=f"Seed{i}")) for i in range(3)]
    decisions = [_maxed_field_decision(f"maxed {i}") for i in range(3)]
    for d in decisions:
        s._write_decision(d)
    s._conn.commit()
    for d, seed in zip(decisions, seeds, strict=True):
        s.add_binding(
            AnchorBinding(record_id=d.id, entity_id=seed.entity_id, tier=2, status="live")
        )

    ctx = rank_decisions(seeds, [], [], s, RetrievalBudget())  # default memory_chars=6000
    for i in range(3):
        assert any(f"maxed {i}" in x for x in ctx.decisions)
    total = sum(len(x) for x in ctx.mistakes + ctx.decisions + ctx.related)
    assert total <= 6000


def test_rank_related_bucket_choice_clipped_at_240_no_rejected_or_consequences(tmp_path):
    # Fix-wave C, C1: a decision reachable only via the Related bucket (community/domain
    # union, not a direct seed anchor) keeps the OLD tight one-liner — 240-char choice
    # snippet only, no context/rejected/consequences at all, regardless of whether those
    # fields are populated. This is also what fixes the "same rejected snippet repeats in
    # every unrelated question" regression: an off-topic related entry never carries one.
    s = Store(tmp_path / "t.db")
    comm_entity = s.get_or_create_abstract_entity("community:7")
    long_choice = "Long-only router. " + ("word " * 300)
    d = Decision(
        title="router design",
        kind=DecisionKind.ADR,
        context="c",
        choice=long_choice,
        rejected="Rejected alternative. " + ("also " * 150),
        consequences="Consequence detail. " + ("cons " * 150),
        status=DecisionStatus.ACCEPTED,
        valid_from=datetime.now(UTC),
        provenance=Provenance(source="manual"),
    )
    s._write_decision(d)
    s._conn.commit()
    s.add_binding(
        AnchorBinding(record_id=d.id, entity_id=comm_entity.entity_id, tier=1, status="live")
    )

    ctx = rank_decisions([], [], ["7"], s, RetrievalBudget(memory_chars=10_000))
    line = next(x for x in ctx.related if "router design" in x)
    assert long_choice[:240] not in line  # tight clip, well under the direct-tier survivor
    assert "…" in line
    assert "(context: " not in line
    assert "(rejected: " not in line
    assert "(consequences: " not in line
    assert "also" not in line
    assert "cons" not in line


def test_clip_line_cuts_at_newline_when_no_spaces():
    # Review follow-up (Minor 3): word-boundary search only looked for literal ASCII
    # spaces — text whose sole whitespace near the cut point is a newline (no space at
    # all) must still cut cleanly at that boundary, not mid-word.
    word_run_1 = "слово" * 60  # 300 chars, no spaces
    word_run_2 = "слово" * 60  # 300 chars, no spaces
    text = word_run_1 + "\n" + word_run_2
    clipped = _clip_line(text, limit=350)
    assert clipped == word_run_1 + "…"


# ---------------------------------------------------------------------------------------
# Fix-wave D: consequences/rejected clip is positivity-skewed under the plain top-down
# clip — calibrated against a real private ADR corpus, where every ADR's `consequences`
# field is shaped "### Positive\n...\n\n### Negative / trade-offs accepted\n...\n\n###
# Risks\n..." and a 400-char top-down clip never reaches past Positive; ADR-005's
# `rejected` field is two bold-pseudo-heading blocks ("**Rejected (antipattern) — ...**
# ...", "**Rejected — emitter-side auto-create.** ...") and the same top-down clip
# rendered only the first.
# ---------------------------------------------------------------------------------------

_REAL_CONSEQUENCES_SHAPE = (
    "### Positive\n"
    "- Benefit one, explained at some length so this section alone exceeds the clip. "
    + ("benefit-filler " * 40)
    + "\n\n### Negative / trade-offs accepted\n"
    "- A real accepted cost that a reader must see: DOWNSIDE_MARKER_TEXT. " + ("cost-filler " * 20)
)

_REAL_REJECTED_SHAPE = (
    "**Rejected (antipattern) — a centralised runner service.** "
    + ("security-ground-filler " * 30)
    + "\n\n**Rejected — emitter-side auto-create.** SECOND_BLOCK_MARKER_TEXT. "
    + ("auto-create-filler " * 10)
)


def test_clip_consequences_prioritizes_negative_over_positive_when_clipped():
    clipped = _clip_consequences(_REAL_CONSEQUENCES_SHAPE, 400)
    assert len(clipped) <= 400 + 2 * len("…")  # two independently-clipped, joined parts
    assert "DOWNSIDE_MARKER_TEXT" in clipped
    assert "### Negative" in clipped
    assert "### Positive" in clipped  # remainder still fills in with positive content


def test_clip_consequences_no_negative_marker_unchanged_top_down():
    # No recognizable Negative/Risks/Trade-offs heading at all -> plain top-down clip,
    # byte-identical to before fix-wave D.
    text = "Just a long block of plain prose consequences. " + ("word " * 200)
    assert _clip_consequences(text, 400) == _clip_line(text, 400)


def test_clip_rejected_multiblock_represents_both_blocks_when_clipped():
    clipped = _clip_rejected(_REAL_REJECTED_SHAPE, 400)
    assert len(clipped) <= 400 + 2 * len("…")
    assert "Rejected (antipattern)" in clipped
    assert "SECOND_BLOCK_MARKER_TEXT" in clipped  # the second block, previously invisible


def test_clip_rejected_single_block_unchanged_top_down():
    # A single bold-pseudo-heading block (no second paragraph) -> nothing to redistribute
    # across, so the plain top-down clip applies, byte-identical to before fix-wave D.
    text = "**Rejected — only one alternative considered.** " + ("word " * 200)
    assert _clip_rejected(text, 400) == _clip_line(text, 400)


def test_clip_rejected_freeform_prose_unchanged_top_down():
    # Plain prose with no bold-lead structure at all (most of the corpus's rejected
    # fields, when populated, are NOT multi-block) -> plain top-down clip, unchanged.
    text = "Considered Postgres but it needs a server to operate. " + ("word " * 200)
    assert _clip_rejected(text, 400) == _clip_line(text, 400)


def test_rank_direct_bucket_consequences_and_rejected_use_the_prioritized_clip(tmp_path):
    # Integration-level: rank_decisions' detailed render for a direct-tier decision goes
    # through the SAME prioritized clip, not a bypassed/duplicated implementation.
    s = Store(tmp_path / "t.db")
    seed = s.upsert_entity(Entity(canonical_name="Seed"))
    d = Decision(
        title="runner architecture",
        kind=DecisionKind.ADR,
        context="c",
        choice="ch",
        rejected=_REAL_REJECTED_SHAPE,
        consequences=_REAL_CONSEQUENCES_SHAPE,
        status=DecisionStatus.ACCEPTED,
        valid_from=datetime.now(UTC),
        provenance=Provenance(source="manual"),
    )
    s._write_decision(d)
    s._conn.commit()
    s.add_binding(AnchorBinding(record_id=d.id, entity_id=seed.entity_id, tier=2, status="live"))

    ctx = rank_decisions([seed], [], [], s, RetrievalBudget())
    line = next(x for x in ctx.decisions if "runner architecture" in x)
    assert "SECOND_BLOCK_MARKER_TEXT" in line
    assert "DOWNSIDE_MARKER_TEXT" in line


def test_rank_memory_budget_math_holds_with_per_line_clip(tmp_path):
    # A single decision's rendered line, even with a very long choice, must never alone
    # consume more than the per-line clip's worth of the memory budget.
    s = Store(tmp_path / "t.db")
    seed = s.upsert_entity(Entity(canonical_name="Seed"))
    _bound_decision(s, seed.entity_id, "shortone", DecisionKind.ADR)
    huge = Decision(
        title="huge one",
        kind=DecisionKind.ADR,
        context="c",
        choice="x " * 2000,  # ~4000 chars, unclipped would blow any reasonable budget
        status=DecisionStatus.ACCEPTED,
        valid_from=datetime.now(UTC),
        provenance=Provenance(source="manual"),
    )
    s._write_decision(huge)
    s._conn.commit()
    s.add_binding(AnchorBinding(record_id=huge.id, entity_id=seed.entity_id, tier=2, status="live"))

    ctx = rank_decisions([seed], [], [], s, RetrievalBudget(memory_chars=2000))
    total = sum(len(x) for x in ctx.mistakes + ctx.decisions + ctx.related)
    assert total <= 2000
    # both decisions fit now that the huge one's line is clipped, not dropped whole.
    assert any("shortone" in x for x in ctx.decisions)
    assert any("huge one" in x for x in ctx.decisions)


def test_rank_direct_entries_fill_budget_before_related_ones(tmp_path):
    # C2: under budget pressure, depth for direct entries wins over breadth of related
    # ones — rank_decisions gathers buckets A/B (direct) before C/D (related), and since
    # `add()` shares one cumulative budget counter across all buckets, a direct entry that
    # nearly fills the budget survives intact while a related entry that no longer fits is
    # simply dropped, never preferred over shrinking/evicting the direct one.
    s = Store(tmp_path / "t.db")
    seed = s.upsert_entity(Entity(canonical_name="Seed"))
    comm_entity = s.get_or_create_abstract_entity("community:9")
    long_choice = "word " * 300  # clips to the ~1200-char direct-tier allowance
    direct = Decision(
        title="direct decision",
        kind=DecisionKind.ADR,
        context="c",
        choice=long_choice,
        status=DecisionStatus.ACCEPTED,
        valid_from=datetime.now(UTC),
        provenance=Provenance(source="manual"),
    )
    related = Decision(
        title="related decision",
        kind=DecisionKind.ADR,
        context="c",
        choice="Some related content that would easily fit on its own.",
        status=DecisionStatus.ACCEPTED,
        valid_from=datetime.now(UTC),
        provenance=Provenance(source="manual"),
    )
    # Budget sized to admit the direct entry exactly, with nothing left over for the
    # related one (computed from the actual rendered length, not guessed).
    direct_line_len = len(_fmt_decision(direct, detailed=True))
    tight_budget = RetrievalBudget(memory_chars=direct_line_len + 2)

    s._write_decision(direct)
    s._write_decision(related)
    s._conn.commit()
    s.add_binding(
        AnchorBinding(record_id=direct.id, entity_id=seed.entity_id, tier=2, status="live")
    )
    s.add_binding(
        AnchorBinding(record_id=related.id, entity_id=comm_entity.entity_id, tier=1, status="live")
    )

    ctx = rank_decisions([seed], [], ["9"], s, tight_budget)
    assert any("direct decision" in x for x in ctx.decisions)
    assert not any("related decision" in x for x in ctx.related)


def test_rank_global_scope_in_related(tmp_path):
    s = Store(tmp_path / "t.db")
    g = Decision(
        title="glob",
        kind=DecisionKind.CONSTRAINT,
        status=DecisionStatus.ACCEPTED,
        context="c",
        choice="ch",
        scope=Scope.GLOBAL,
        valid_from=datetime.now(UTC),
        provenance=Provenance(source="manual"),
    )
    s._write_decision(g)
    s._conn.commit()
    ctx = rank_decisions([], [], [], s, RetrievalBudget())
    assert any("glob" in r for r in ctx.related)


# -- bucket C learns domains (M5, M2 review PINNED I1) -----------------------------------


def _accepted_domain(store, slug, communities, **overrides):
    base = dict(
        slug=slug,
        title=slug.title(),
        summary=f"{slug} summary",
        communities=communities,
        provenance=Provenance(source="manual"),
    )
    base.update(overrides)
    d = store.add_domain(Domain(**base))
    store.ratify_domains(accept=[d.domain_id])
    return store.get_domain(d.domain_id)


def test_rank_related_bucket_learns_domain_for_seed_community(tmp_path):
    s = Store(tmp_path / "t.db")
    _accepted_domain(s, "payments", ["7"])
    domain_entity = s.find_abstract_entity("domain:payments")
    _bound_decision(s, domain_entity.entity_id, "domain-note", DecisionKind.ADR)

    ctx = rank_decisions([], [], ["7"], s, RetrievalBudget())
    assert any("domain-note" in r for r in ctx.related)


def test_rank_related_bucket_still_surfaces_legacy_community_binding_alongside_domain(tmp_path):
    """A decision bound before the domain existed (still on the bare `community:<id>`
    entity) must keep surfacing even once an accepted domain claims that community."""
    s = Store(tmp_path / "t.db")
    comm_entity = s.get_or_create_abstract_entity("community:7")
    _bound_decision(s, comm_entity.entity_id, "legacy-note", DecisionKind.ADR)
    _accepted_domain(s, "payments", ["7"])
    domain_entity = s.find_abstract_entity("domain:payments")
    _bound_decision(s, domain_entity.entity_id, "domain-note", DecisionKind.ADR)

    ctx = rank_decisions([], [], ["7"], s, RetrievalBudget())
    assert any("legacy-note" in r for r in ctx.related)
    assert any("domain-note" in r for r in ctx.related)


def test_rank_related_bucket_dedupes_decision_bound_to_both_domain_and_community(tmp_path):
    s = Store(tmp_path / "t.db")
    _accepted_domain(s, "payments", ["7"])
    domain_entity = s.find_abstract_entity("domain:payments")
    comm_entity = s.get_or_create_abstract_entity("community:7")
    d = _bound_decision(s, domain_entity.entity_id, "dual-bound", DecisionKind.ADR)
    s.add_binding(
        AnchorBinding(record_id=d.id, entity_id=comm_entity.entity_id, tier=1, status="live")
    )

    ctx = rank_decisions([], [], ["7"], s, RetrievalBudget())
    matches = [r for r in ctx.related if "dual-bound" in r]
    assert len(matches) == 1


def test_rank_related_bucket_unions_all_accepted_domains_covering_seed_community(tmp_path):
    """Gate-5 finding 2 regression: an "orphan window" where two accepted domains cover the
    SAME community at once (not prevented at write time -- one domain claimed it, then a
    second one later also claimed it, e.g. before sync narrows things back down). A decision
    tier-1-bound to the OLDER covering domain must still surface via rank_decisions: with the
    old (singular, newest-only) `find_domain_by_community` lookup, the older domain's paired
    entity was never even looked at, and the decision wasn't bound to the bare
    `community:<id>` entity either, so it silently vanished from `related`."""
    s = Store(tmp_path / "t.db")
    older = _accepted_domain(s, "payments-old", ["7"])
    _accepted_domain(s, "payments-new", ["7"])
    older_entity = s.find_abstract_entity(f"domain:{older.slug}")
    _bound_decision(s, older_entity.entity_id, "older-domain-note", DecisionKind.ADR)

    ctx = rank_decisions([], [], ["7"], s, RetrievalBudget())
    assert any("older-domain-note" in r for r in ctx.related)


def test_rank_related_bucket_unchanged_without_domains(tmp_path):
    """No accepted domain covers the seed community -> byte-identical to before PINNED
    I1 existed: only the bare `community:<id>` entity's decisions surface."""
    s = Store(tmp_path / "t.db")
    comm_entity = s.get_or_create_abstract_entity("community:7")
    _bound_decision(s, comm_entity.entity_id, "legacy-only-note", DecisionKind.ADR)

    ctx = rank_decisions([], [], ["7"], s, RetrievalBudget())
    assert any("legacy-only-note" in r for r in ctx.related)


def test_shown_ids_carries_every_rendered_decision(tmp_path):
    """The ids must match what actually reached the render, or the telemetry counts
    records the agent never saw."""
    s = Store(tmp_path / "t.db")
    seed = s.upsert_entity(Entity(canonical_name="Seed"))
    peri = s.upsert_entity(Entity(canonical_name="Peri"))
    gotcha = _bound_decision(s, seed.entity_id, "gotcha1", DecisionKind.GOTCHA)
    adr = _bound_decision(s, seed.entity_id, "adr1", DecisionKind.ADR)
    peri_adr = _bound_decision(s, peri.entity_id, "peri-adr", DecisionKind.ADR)

    ctx = rank_decisions([seed], [peri], [], s, RetrievalBudget())
    rendered = ctx.render()
    expected = {d.id for d in (gotcha, adr, peri_adr) if d.title in rendered}
    assert set(ctx.shown_ids) == expected
