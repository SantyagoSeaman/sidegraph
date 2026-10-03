"""Guessed seeds never crowd out the records of the seeds the agent got right.

``get_task_context(seeds, ..., guessed=...)``: the ``guessed`` seeds are what the seed ladder
rewrote. When the exact ``seeds`` resolved to a graph node, they are read exactly as without
``guessed`` (the same map, the same peripherals), and the guessed seeds' entities trail every
record the exact seeds and their neighbours fill: the related tier, the superseded one-liners
and the Known facts included. When no exact seed resolved to a node they act as the seeds,
which is the empty-answer case. A rewritten entity seed keeps its original as an exact seed,
so a descriptor stored under the original file still matches at the tier it always had.
# see design/superpowers/specs/2026-10-02-tolerant-seeds-design.md (D3, tests A11, A12)
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from sidegraph.engine.reader import GraphifyReader
from sidegraph.retrieval import RetrievalBudget, Seed, get_task_context
from sidegraph.schema import (
    AnchorBinding,
    Decision,
    DecisionKind,
    DecisionStatus,
    Descriptor,
    Entity,
    Fact,
    Provenance,
)
from sidegraph.store import Store


def _reader(tmp_path: Path) -> GraphifyReader:
    nodes = [
        ("exact", "ExactThing", "a/exact.py", 1),
        ("guess", "GuessThing", "b/guess.py", 2),
        ("cache", "Cache", "src/cache.py", 3),
    ]
    graph = {
        "built_at_commit": "x",
        "nodes": [
            {
                "id": i,
                "label": label,
                "norm_label": label.lower(),
                "file_type": "code",
                "source_file": path,
                "community": community,
            }
            for i, label, path, community in nodes
        ],
        "links": [],
    }
    p = tmp_path / "g.json"
    p.write_text(json.dumps(graph))
    return GraphifyReader(p)


def _entity(store: Store, name: str, path: str) -> Entity:
    return store.upsert_entity(
        Entity(canonical_name=name, descriptor=Descriptor(name=name, file_path=path))
    )


def _record(store: Store, entity: Entity, title: str, kind: DecisionKind) -> Decision:
    d = store.add_decision(
        Decision(
            title=title,
            kind=kind,
            status=DecisionStatus.ACCEPTED,
            context="c",
            choice="ch",
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual"),
        )
    )
    store.add_binding(
        AnchorBinding(record_id=d.id, entity_id=entity.entity_id, tier=2, status="live")
    )
    return d


EXACT = Seed(file_path="a/exact.py")
GUESS = Seed(file_path="b/guess.py")


def _store_with_adrs_and_gotchas(tmp_path: Path) -> tuple[Store, list[Decision], list[Decision]]:
    """Three ADRs on the exact seed's entity, six gotchas on the guessed one's. Gotchas are
    mistakes, so an equal-footing guessed seed would spend the budget on them first."""
    store = Store(tmp_path / "t.db")
    exact = _entity(store, "ExactThing", "a/exact.py")
    guess = _entity(store, "GuessThing", "b/guess.py")
    adrs = [_record(store, exact, f"Exact adr {i}", DecisionKind.ADR) for i in range(3)]
    gotchas = [_record(store, guess, f"Guess gotcha {i}", DecisionKind.GOTCHA) for i in range(6)]
    return store, adrs, gotchas


# One direct line is about 110 characters: the budget holds the three ADRs with room for a few
# related one-liners, and well under the six gotchas rendered in the direct tier.
BUDGET = RetrievalBudget(memory_chars=600)


def test_a11_guessed_seeds_land_in_related_and_every_direct_record_stays(tmp_path):
    store, adrs, gotchas = _store_with_adrs_and_gotchas(tmp_path)
    reader = _reader(tmp_path)

    alone = get_task_context([EXACT], store, reader, BUDGET)
    both = get_task_context([EXACT], store, reader, BUDGET, guessed=[GUESS])

    assert all(d.id in "".join(alone.decisions) for d in adrs)
    assert both.decisions == alone.decisions
    assert both.mistakes == alone.mistakes == []
    related = "".join(both.related)
    assert any(g.id not in related and g.title in related for g in gotchas)  # tight, no id
    assert all(g.title not in "".join(both.decisions) for g in gotchas)
    for g in gotchas:
        assert g.id not in "".join(both.mistakes)


def test_a11_the_map_is_the_exact_seeds_map_when_they_resolved(tmp_path):
    store, _adrs, _gotchas = _store_with_adrs_and_gotchas(tmp_path)
    reader = _reader(tmp_path)

    alone = get_task_context([EXACT], store, reader, BUDGET)
    both = get_task_context([EXACT], store, reader, BUDGET, guessed=[GUESS])

    assert both.structure == alone.structure
    assert "b/guess.py" not in "\n".join(both.structure)


def test_a11_with_no_exact_seed_the_map_is_the_guessed_seeds_map(tmp_path):
    store, _adrs, _gotchas = _store_with_adrs_and_gotchas(tmp_path)
    reader = _reader(tmp_path)

    out = get_task_context([], store, reader, BUDGET, guessed=[GUESS])

    assert "b/guess.py" in "\n".join(out.structure)


def _neighbour_graph(tmp_path: Path) -> GraphifyReader:
    """``ExactThing`` calls three neighbours; ``GuessThing`` calls one of its own."""
    spec = [
        ("exact", "ExactThing", "a/exact.py"),
        ("n1", "NeighbourOne", "n/n1.py"),
        ("n2", "NeighbourTwo", "n/n2.py"),
        ("n3", "NeighbourThree", "n/n3.py"),
        ("guess", "GuessThing", "b/guess.py"),
        ("g1", "GuessNeighbour", "g/g1.py"),
    ]
    graph = {
        "built_at_commit": "x",
        "nodes": [
            {
                "id": i,
                "label": label,
                "norm_label": label.lower(),
                "file_type": "code",
                "source_file": path,
                "community": n,
            }
            for n, (i, label, path) in enumerate(spec)
        ],
        "links": [
            {"source": "exact", "target": "n1", "relation": "calls"},
            {"source": "exact", "target": "n2", "relation": "calls"},
            {"source": "exact", "target": "n3", "relation": "calls"},
            {"source": "guess", "target": "g1", "relation": "calls"},
        ],
    }
    p = tmp_path / "neighbours.json"
    p.write_text(json.dumps(graph))
    return GraphifyReader(p)


def _neighbour_store(tmp_path: Path) -> Store:
    store = Store(tmp_path / "n.db")
    for name, path, kind, titles in (
        ("NeighbourOne", "n/n1.py", DecisionKind.ADR, ["Neighbour one rule"]),
        ("NeighbourTwo", "n/n2.py", DecisionKind.ADR, ["Neighbour two rule"]),
        ("NeighbourThree", "n/n3.py", DecisionKind.ADR, ["Neighbour three rule"]),
        ("GuessThing", "b/guess.py", DecisionKind.GOTCHA, ["Guess gotcha a", "Guess gotcha b"]),
        ("GuessNeighbour", "g/g1.py", DecisionKind.ADR, ["Guess neighbour rule"]),
    ):
        entity = _entity(store, name, path)
        for title in titles:
            _record(store, entity, title, kind)
    return store


def test_a11_a_guess_never_pushes_out_what_the_exact_seeds_peripherals_would_show(tmp_path):
    """The map's node cap is four: the exact seed and its three neighbours fill it. A guessed
    seed must neither take a slot from them (the third neighbour's record would vanish) nor
    stand ahead of them in the related tier (its records would spend a budget the neighbours'
    own just fits)."""
    store = _neighbour_store(tmp_path)
    reader = _neighbour_graph(tmp_path)
    roomy = RetrievalBudget(structure_chars=480, memory_chars=6000)
    fits = get_task_context([EXACT], store, reader, roomy)
    assert len(fits.related) == 3 and "Neighbour three rule" in "".join(fits.related)
    tight = RetrievalBudget(structure_chars=480, memory_chars=fits.chars_used)

    alone = get_task_context([EXACT], store, reader, tight)
    both = get_task_context([EXACT], store, reader, tight, guessed=[GUESS])

    assert alone.related == fits.related
    assert set(alone.shown_ids) <= set(both.shown_ids)
    assert both.related[: len(alone.related)] == alone.related
    assert both.structure == alone.structure
    assert "Guess gotcha a" not in "".join(both.related)  # it did not fit behind them


def test_a11_a_guess_is_appended_after_the_exact_peripherals_when_there_is_room(tmp_path):
    store = _neighbour_store(tmp_path)
    reader = _neighbour_graph(tmp_path)
    roomy = RetrievalBudget(structure_chars=480, memory_chars=6000)

    alone = get_task_context([EXACT], store, reader, roomy)
    both = get_task_context([EXACT], store, reader, roomy, guessed=[GUESS])

    assert both.related[: len(alone.related)] == alone.related
    assert "Guess gotcha a" in "".join(both.related[len(alone.related) :])
    assert both.structure == alone.structure


def test_a11_with_no_exact_seed_the_guessed_ones_act_as_seeds(tmp_path):
    store, _adrs, gotchas = _store_with_adrs_and_gotchas(tmp_path)
    reader = _reader(tmp_path)

    as_guessed = get_task_context([], store, reader, BUDGET, guessed=[GUESS])
    as_seed = get_task_context([GUESS], store, reader, BUDGET)

    assert as_guessed.render() == as_seed.render()
    assert as_guessed.mistakes and gotchas[0].title in "".join(as_guessed.mistakes)


def test_a11_exact_seeds_that_resolve_nothing_do_not_demote_the_guessed_ones(tmp_path):
    """A seed the graph and the store both lack gives the guessed ones nothing to yield to."""
    store, _adrs, gotchas = _store_with_adrs_and_gotchas(tmp_path)
    reader = _reader(tmp_path)

    out = get_task_context([Seed(name="Frobnicate")], store, reader, BUDGET, guessed=[GUESS])

    assert any(g.title in "".join(out.mistakes) for g in gotchas)


def test_a11_without_guessed_seeds_nothing_changes(tmp_path):
    store, _adrs, _gotchas = _store_with_adrs_and_gotchas(tmp_path)
    reader = _reader(tmp_path)

    plain = get_task_context([EXACT, GUESS], store, reader, BUDGET)
    keyword = get_task_context([EXACT, GUESS], store, reader, BUDGET, guessed=())

    assert plain.render() == keyword.render()
    assert plain.shown_ids == keyword.shown_ids


def test_a11_a_guessed_seed_already_exact_is_not_listed_twice(tmp_path):
    store, adrs, _gotchas = _store_with_adrs_and_gotchas(tmp_path)
    reader = _reader(tmp_path)

    out = get_task_context([EXACT], store, reader, BUDGET, guessed=[EXACT])

    assert out.render() == get_task_context([EXACT], store, reader, BUDGET).render()
    assert out.shown_ids.count(adrs[0].id) == 1


def test_a12_a_rewritten_entity_seed_still_matches_a_descriptor_stored_under_its_original(
    tmp_path,
):
    """The record was written while the file was ``app/old/cache.py``; the graph now holds
    ``Cache`` in ``src/cache.py``. The ladder keeps the original as an exact seed beside the
    guessed rewrite: the original is what reaches the stored descriptor."""
    store = Store(tmp_path / "t.db")
    reader = _reader(tmp_path)
    old = _entity(store, "Cache", "app/old/cache.py")
    record = _record(store, old, "Cache keeps three entries", DecisionKind.GOTCHA)
    rewritten = Seed(name="Cache", file_path="src/cache.py")
    original = Seed(name="Cache", file_path="app/old/cache.py")

    with_original = get_task_context([original], store, reader, BUDGET, guessed=[rewritten])
    rewrite_only = get_task_context([], store, reader, BUDGET, guessed=[rewritten])

    assert record.id in with_original.shown_ids
    assert record.id not in rewrite_only.shown_ids


def test_a12_a_rewrite_keeps_its_own_records_in_the_tier_they_had_when_only_a_store_match_is_exact(
    tmp_path,
):
    """The original matches a stored descriptor and no graph node. That is not a seed that
    "resolved" in the sense that demotes the rewrite: the rewrite's own gotcha stays a
    mistake."""
    store = Store(tmp_path / "t.db")
    reader = _reader(tmp_path)
    old = _entity(store, "Cache", "app/old/cache.py")
    new = _entity(store, "Cache", "src/cache.py")
    _record(store, old, "Old cache rule", DecisionKind.GOTCHA)
    fresh = _record(store, new, "New cache rule", DecisionKind.GOTCHA)
    rewritten = Seed(name="Cache", file_path="src/cache.py")
    original = Seed(name="Cache", file_path="app/old/cache.py")

    out = get_task_context([original], store, reader, BUDGET, guessed=[rewritten])

    assert any(fresh.id in line for line in out.mistakes)
    assert not any("New cache rule" in line for line in out.related)


def _exact_entity_with_history(tmp_path: Path) -> tuple[Store, list[Decision]]:
    """The exact seed's entity holds an ADR, the decision that ADR replaced, and a standalone
    fact; the guessed seed's holds eight ADRs."""
    store = Store(tmp_path / "h.db")
    exact = _entity(store, "ExactThing", "a/exact.py")
    guess = _entity(store, "GuessThing", "b/guess.py")
    old = _record(store, exact, "Exact predecessor", DecisionKind.ADR)
    current = store.add_decision(
        Decision(
            title="Exact current",
            kind=DecisionKind.ADR,
            status=DecisionStatus.ACCEPTED,
            supersedes=old.id,
            context="c",
            choice="ch",
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual"),
        )
    )
    store.add_binding(
        AnchorBinding(record_id=current.id, entity_id=exact.entity_id, tier=2, status="live")
    )
    fact = store.add_fact(
        Fact(
            statement="Exact fact about the thing",
            source="test",
            status=DecisionStatus.ACCEPTED,
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual"),
        )
    )
    store.add_binding(
        AnchorBinding(record_id=fact.id, entity_id=exact.entity_id, tier=2, status="live")
    )
    for i in range(8):
        _record(store, guess, f"Guess adr {i}", DecisionKind.ADR)
    return store, [old, current]


def test_a11_a_guess_never_pushes_out_the_exact_seeds_history_or_facts(tmp_path):
    """The exact seed has a superseded one-liner and a standalone fact; the guessed seed has
    eight ADRs that would fill the related tier first if they stood among the peripherals. They
    trail every bucket the exact seed's own records fill."""
    store, _ = _exact_entity_with_history(tmp_path)
    reader = _reader(tmp_path)
    roomy = RetrievalBudget(memory_chars=6000)
    fits = get_task_context([EXACT], store, reader, roomy)
    assert any("Exact predecessor" in line for line in fits.related) and fits.facts
    tight = RetrievalBudget(memory_chars=fits.chars_used)

    alone = get_task_context([EXACT], store, reader, tight)
    both = get_task_context([EXACT], store, reader, tight, guessed=[GUESS])

    assert set(alone.shown_ids) <= set(both.shown_ids)
    assert both.related == alone.related and both.facts == alone.facts
    assert both.decisions == alone.decisions
    assert not any("Guess adr" in line for line in both.related)  # no room behind them


def test_a11_a_guess_trails_the_history_and_facts_when_there_is_room(tmp_path):
    store, _ = _exact_entity_with_history(tmp_path)
    reader = _reader(tmp_path)
    roomy = RetrievalBudget(memory_chars=6000)

    alone = get_task_context([EXACT], store, reader, roomy)
    both = get_task_context([EXACT], store, reader, roomy, guessed=[GUESS])

    assert both.related[: len(alone.related)] == alone.related
    assert both.facts == alone.facts
    assert any("Guess adr" in line for line in both.related[len(alone.related) :])


def test_a11_a_guess_never_pushes_out_the_exact_seeds_unratified_records(tmp_path):
    """An exact seed with a proposed gotcha, and a guessed seed with accepted ADRs that would
    take the budget if they were placed before the proposals."""
    store = Store(tmp_path / "u.db")
    exact = _entity(store, "ExactThing", "a/exact.py")
    guess = _entity(store, "GuessThing", "b/guess.py")
    _record(store, exact, "Exact adr", DecisionKind.ADR)
    proposal = store.add_decision(
        Decision(
            title="Exact proposed gotcha",
            kind=DecisionKind.GOTCHA,
            status=DecisionStatus.PROPOSED,
            context="c",
            choice="ch",
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="agent"),
        )
    )
    store.add_binding(
        AnchorBinding(record_id=proposal.id, entity_id=exact.entity_id, tier=2, status="live")
    )
    for i in range(6):
        _record(store, guess, f"Guess adr {i}", DecisionKind.ADR)
    reader = _reader(tmp_path)
    fits = get_task_context([EXACT], store, reader, RetrievalBudget(memory_chars=6000))
    assert any(proposal.id in line for line in fits.unratified)
    tight = RetrievalBudget(memory_chars=fits.chars_used)

    alone = get_task_context([EXACT], store, reader, tight)
    both = get_task_context([EXACT], store, reader, tight, guessed=[GUESS])

    assert both.unratified == alone.unratified and proposal.id in both.shown_ids
    assert set(alone.shown_ids) <= set(both.shown_ids)
    assert not any("Guess adr" in line for line in both.related)


def test_a11_a_proposed_fact_on_a_guessed_seeds_decision_shows_as_it_does_for_an_exact_one(
    tmp_path,
):
    store = Store(tmp_path / "f.db")
    exact = _entity(store, "ExactThing", "a/exact.py")
    guess = _entity(store, "GuessThing", "b/guess.py")
    _record(store, exact, "Exact adr", DecisionKind.ADR)
    guessed_adr = _record(store, guess, "Guess adr", DecisionKind.ADR)
    store.add_fact(
        Fact(
            statement="Proposed evidence for the guess",
            source="test",
            supports=[guessed_adr.id],
            status=DecisionStatus.PROPOSED,
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="agent"),
        )
    )
    reader = _reader(tmp_path)
    budget = RetrievalBudget(memory_chars=6000)

    as_exact = get_task_context([GUESS], store, reader, budget)
    as_guess = get_task_context([EXACT], store, reader, budget, guessed=[GUESS])

    assert "Proposed evidence for the guess" in "".join(as_exact.unratified)
    assert "Proposed evidence for the guess" in "".join(as_guess.unratified)
