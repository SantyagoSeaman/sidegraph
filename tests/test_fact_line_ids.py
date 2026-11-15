"""A fact line of the task context ends with the fact's id, in the form a decision line ends
with, and ``sidegraph-stats`` counts a delivered fact as a placement.
see design/superpowers/specs/2026-11-14-task-context-files-only-design.md (part B)
"""

from datetime import UTC, datetime

import pytest

from sidegraph import server
from sidegraph.engine.reader import GraphifyReader
from sidegraph.retrieval import (
    RetrievalBudget,
    _fmt_fact,
    get_task_context,
    rank_decisions,
    top_tier_sections,
)
from sidegraph.schema import (
    AnchorBinding,
    Decision,
    DecisionKind,
    DecisionStatus,
    Descriptor,
    Fact,
    Provenance,
)
from sidegraph.stats import echo
from sidegraph.store import Store
from tests.test_retrieval_named_first import (
    BASE,
    FILE,
    FILES_ONLY,
    _evidence,
    _record,
    _world,
)
from tests.test_stats_echo import FIXTURE, _anchored_store, _decision, _delivery

OTHER_ID = "01M3T0VEAEAAAAAAAAAAAAAAAA"


def _bound_fact(
    store, entity, statement="a standalone fact", status=DecisionStatus.ACCEPTED, source="s"
):
    """A fact bound to ``entity``. A proposed one is dated now: an old proposal is withheld."""
    fact = store.add_fact(
        Fact(
            statement=statement,
            source=source,
            status=status,
            valid_from=BASE if status == DecisionStatus.ACCEPTED else datetime.now(UTC),
            provenance=Provenance(source="manual"),
        )
    )
    store.add_binding(
        AnchorBinding(record_id=fact.id, entity_id=entity.entity_id, tier=2, status="live")
    )
    return fact


def test_a_known_fact_line_ends_with_its_id(tmp_path):
    store, reader, e = _world(tmp_path)
    fact = _bound_fact(store, e["foo"])
    ctx = get_task_context(FILES_ONLY, store, reader)
    assert ctx.facts == [f"- fact: a standalone fact [s] (id: {fact.id})"]


def test_an_evidence_line_ends_with_its_id(tmp_path):
    store, reader, e = _world(tmp_path)
    mistake = _record(store, [e["foo"]], "foo-gotcha", DecisionKind.GOTCHA)
    fact = _evidence(store, mistake, "why it bites")
    ctx = get_task_context(FILES_ONLY, store, reader)
    assert ctx.mistakes[1] == f"  evidence: why it bites [s] (id: {fact.id})"


def test_an_unratified_fact_line_ends_with_its_id(tmp_path):
    store, reader, e = _world(tmp_path)
    fact = _bound_fact(store, e["foo"], status=DecisionStatus.PROPOSED)
    ctx = get_task_context(FILES_ONLY, store, reader)
    assert ctx.unratified == [f"- fact [unratified]: a standalone fact [s] (id: {fact.id})"]


def test_a_clipped_fact_keeps_its_note_and_ends_with_its_id(tmp_path):
    store, reader, e = _world(tmp_path)
    fact = _bound_fact(store, e["foo"], statement="word " * 400)
    (line,) = get_task_context(FILES_ONLY, store, reader).facts
    assert line.endswith(f' (clipped; full: list_facts(ids=["{fact.id}"])) (id: {fact.id})')
    assert line.count("clipped; full:") == 1


@pytest.mark.parametrize("field", ["statement", "source"])
def test_a_standalone_fact_with_a_line_break_is_one_line_and_is_placed(tmp_path, field):
    store, reader, e = _world(tmp_path)
    # The second line of the fact's own text looks like an evidence line.
    fact = _bound_fact(store, e["foo"], **{field: "first\n  evidence: last"})
    ctx = get_task_context(FILES_ONLY, store, reader)
    assert ctx.shown_ids == [fact.id]
    # In a real answer a section can follow the fact: the parser reads neither it nor the
    # fact's second line as something else.
    text = ctx.render() + "\n\n## Structural map\n- mod.py (code) [src/mod.py]"
    assert echo.placements(_delivery(text), frozenset({fact.id})) == [fact.id]
    (line,) = ctx.facts
    assert line.startswith("- fact: ") and "\n" not in line
    assert "first   evidence: last" in line


def test_the_session_start_map_fact_lines_are_unchanged(tmp_path):
    store, _reader, e = _world(tmp_path)
    fact = _bound_fact(store, e["foo"])
    sections = {s.heading: s for s in top_tier_sections(store, None)}
    (entry,) = sections["## Known facts"].entries
    assert entry.text == "- fact: a standalone fact [s]"
    assert _fmt_fact(fact) == "- fact: a standalone fact [s]"


def test_a_delivered_fact_is_a_placement(tmp_path, monkeypatch):
    decision = _decision("deadlock")
    store = _anchored_store(tmp_path, monkeypatch, decision)
    entity = store.find_entity("Trader", "trader/exec.py")
    fact = _bound_fact(store, entity, statement="the queue holds 64 items")
    text = server._get_task_context_impl(
        store, GraphifyReader(FIXTURE), ["trader/exec.py"], None, 4000, 6000
    )
    assert f"- fact: the queue holds 64 items [s] (id: {fact.id})" in text
    found = echo.placements(_delivery(text), frozenset({decision.id, fact.id}))
    assert found == [decision.id, fact.id]
    store.close()


def test_an_evidence_fact_is_a_placement_after_its_record(tmp_path, monkeypatch):
    decision = _decision("deadlock")
    store = _anchored_store(tmp_path, monkeypatch, decision)
    fact = store.add_fact(
        Fact(
            statement="two writers took the lock in opposite order",
            source="trace",
            supports=[decision.id],
            status=DecisionStatus.ACCEPTED,
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual"),
        )
    )
    text = server._get_task_context_impl(
        store, GraphifyReader(FIXTURE), ["trader/exec.py"], None, 4000, 6000
    )
    found = echo.placements(_delivery(text), frozenset({decision.id, fact.id}))
    assert found == [decision.id, fact.id]
    store.close()


def _supporting(store, decision, statement="one line", source="s"):
    return store.add_fact(
        Fact(
            statement=statement,
            source=source,
            status=DecisionStatus.ACCEPTED,
            supports=[decision.id],
            valid_from=BASE,
            provenance=Provenance(source="manual"),
        )
    )


@pytest.mark.parametrize("field", ["statement", "source"])
def test_an_evidence_fact_with_a_line_break_is_one_line_and_both_records_are_placed(
    tmp_path, field
):
    store, reader, e = _world(tmp_path)
    mistake = _record(store, [e["foo"]], "foo-gotcha", DecisionKind.GOTCHA)
    fact = _supporting(store, mistake, **{field: "first\nsecond"})
    ctx = get_task_context(FILES_ONLY, store, reader)
    assert ctx.mistakes[1].startswith("  evidence: ") and "\n" not in ctx.mistakes[1]
    assert "first second" in ctx.mistakes[1]
    found = echo.placements(_delivery(ctx.render()), frozenset({mistake.id, fact.id}))
    assert found == [mistake.id, fact.id]


def test_an_evidence_like_line_inside_an_evidence_fact_is_not_a_placement(tmp_path):
    store, reader, e = _world(tmp_path)
    mistake = _record(store, [e["foo"]], "foo-gotcha", DecisionKind.GOTCHA)
    # A record of another file: stored, known to the stats, and not delivered by this call.
    decoy = _record(store, [e["run"]], "not delivered", DecisionKind.ADR)
    quoting = f"first\n  evidence: quoted (id: {decoy.id})\n  evidence: last"
    fact = _supporting(store, mistake, statement=quoting)
    text = get_task_context(FILES_ONLY, store, reader).render()
    assert decoy.id in text
    found = echo.placements(_delivery(text), frozenset({mistake.id, fact.id, decoy.id}))
    assert found == [mistake.id, fact.id]


def test_an_evidence_like_line_inside_a_records_prose_is_not_a_placement():
    record = "01M3T0VEAFBBBBBBBBBBBBBBBB"
    text = (
        "## Decisions\n"
        "- [adr] a title: a choice (context: first line\n"
        f"  evidence: quoted from another record (id: {OTHER_ID})\n"
        f"last line) (id: {record})\n"
    )
    assert echo.placements(_delivery(text), frozenset({record, OTHER_ID})) == [record]


# -- prose that looks like an evidence line ----------------------------------------------------
#
# The stats parser reads a line that starts with two spaces and `evidence: ` as a trailer of the
# record above it. The renderer makes that true: a physical line of a record's own prose that
# would match is rendered without its leading spaces, so only a real evidence line carries them.

QUOTING = "first\n  evidence: quoted (id: {other})\n  evidence: last"


def _task_context_text(store, reader) -> str:
    return server._get_task_context_impl(store, reader, [FILE], None, 4000, 6000)


def _bound_decision(store, entity, **fields):
    decision = store.add_decision(
        Decision(
            title=fields.pop("title", "quoting"),
            kind=fields.pop("kind", DecisionKind.GOTCHA),
            status=fields.pop("status", DecisionStatus.ACCEPTED),
            context=fields.pop("context", "c"),
            choice=fields.pop("choice", "use the lock"),
            valid_from=fields.pop("valid_from", BASE),
            provenance=Provenance(source="manual"),
            **fields,
        )
    )
    store.add_binding(
        AnchorBinding(record_id=decision.id, entity_id=entity.entity_id, tier=2, status="live")
    )
    return decision


def test_prose_ending_in_evidence_like_lines_does_not_place_the_quoted_record(tmp_path):
    store, reader, e = _world(tmp_path)
    decoy = _record(store, [e["run"]], "not delivered", DecisionKind.ADR)
    decision = _bound_decision(store, e["foo"], context=QUOTING.format(other=decoy.id))
    fact = _supporting(store, decision, statement="actual supporting fact")
    text = _task_context_text(store, reader)
    known = frozenset({decision.id, decoy.id, fact.id})
    assert echo.placements(_delivery(text), known) == [decision.id, fact.id]
    assert f"\n  evidence: actual supporting fact [s] (id: {fact.id})" in text
    assert "\nevidence: quoted" in text  # the prose line lost its two leading spaces


def test_a_context_ending_in_an_evidence_like_line_still_places_the_record(tmp_path):
    store, reader, e = _world(tmp_path)
    decoy = _record(store, [e["run"]], "not delivered", DecisionKind.ADR)
    mistake = _record(store, [e["foo"]], "foo-gotcha", DecisionKind.GOTCHA)
    ending = _bound_decision(
        store,
        e["foo"],
        title="ending",
        kind=DecisionKind.ADR,
        context=f"first\n  evidence: quoted (id: {decoy.id})",
    )
    text = get_task_context(FILES_ONLY, store, reader).render()
    assert "## Structural map" in text  # a section follows the last record
    known = frozenset({mistake.id, ending.id, decoy.id})
    assert echo.placements(_delivery(text), known) == [mistake.id, ending.id]


def test_the_title_of_a_record_cannot_pass_for_an_evidence_line_either(tmp_path):
    store, reader, e = _world(tmp_path)
    decoy = _record(store, [e["run"]], "not delivered", DecisionKind.ADR)
    decision = _bound_decision(
        store, e["foo"], title=f"head\n  evidence: quoted (id: {decoy.id})\n  evidence: tail"
    )
    text = _task_context_text(store, reader)
    known = frozenset({decision.id, decoy.id})
    assert echo.placements(_delivery(text), known) == [decision.id]


def test_a_decision_without_evidence_like_prose_renders_as_before(tmp_path):
    store, reader, e = _world(tmp_path)
    decision = _bound_decision(store, e["foo"], context="two\nlines  evidence: inline")
    text = _task_context_text(store, reader)
    assert "(context: two\nlines  evidence: inline)" in text
    assert echo.placements(_delivery(text), frozenset({decision.id})) == [decision.id]


# -- every place the task context renders a fact carries the id -----------------------------
#
# `render_fact` is one helper, but seven call sites use it: a site that went back to the old
# line would ship a fact with no id and nothing else would go red.


def _site_entity(store, name):
    return store.get_or_create_entity(Descriptor(name=name, file_path=f"{name}.py"))


def _site_fact(store, entity=None, *, status=DecisionStatus.ACCEPTED, supports=()):
    fact = store.add_fact(
        Fact(
            statement="a site fact",
            source="s",
            status=status,
            supports=list(supports),
            valid_from=BASE if status == DecisionStatus.ACCEPTED else datetime.now(UTC),
            provenance=Provenance(source="manual"),
        )
    )
    if entity is not None:
        store.add_binding(
            AnchorBinding(record_id=fact.id, entity_id=entity.entity_id, tier=2, status="live")
        )
    return fact


def _site_proposal(store, entity):
    return _bound_decision(
        store,
        entity,
        status=DecisionStatus.PROPOSED,
        valid_from=datetime.now(UTC),
        kind=DecisionKind.ADR,
    )


def test_a_fact_of_a_peripheral_entity_ends_with_its_id(tmp_path):
    store = Store(tmp_path / "s")
    seed, peripheral = _site_entity(store, "seed"), _site_entity(store, "peer")
    fact = _site_fact(store, peripheral)
    ctx = rank_decisions([seed], [peripheral], [], store, RetrievalBudget())
    assert ctx.facts == [f"- fact: a site fact [s] (id: {fact.id})"]
    store.close()


def test_a_fact_of_a_guessed_entity_ends_with_its_id(tmp_path):
    store = Store(tmp_path / "s")
    guessed = _site_entity(store, "guess")
    fact = _site_fact(store, guessed)
    ctx = rank_decisions([], [], [], store, RetrievalBudget(), trailing_entities=[guessed])
    assert ctx.facts == [f"- fact: a site fact [s] (id: {fact.id})"]
    store.close()


def test_an_accepted_fact_of_a_guessed_proposal_ends_with_its_id(tmp_path):
    store = Store(tmp_path / "s")
    guessed = _site_entity(store, "guess")
    proposal = _site_proposal(store, guessed)
    fact = _site_fact(store, supports=[proposal.id])
    ctx = rank_decisions([], [], [], store, RetrievalBudget(), trailing_entities=[guessed])
    assert ctx.facts == [f"- fact: a site fact [s] (id: {fact.id})"]
    store.close()


def test_an_unratified_fact_of_a_guessed_entity_ends_with_its_id(tmp_path):
    store = Store(tmp_path / "s")
    guessed = _site_entity(store, "guess")
    fact = _site_fact(store, guessed, status=DecisionStatus.PROPOSED)
    ctx = rank_decisions([], [], [], store, RetrievalBudget(), trailing_entities=[guessed])
    assert ctx.unratified == [f"- fact [unratified]: a site fact [s] (id: {fact.id})"]
    store.close()
