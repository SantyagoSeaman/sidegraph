"""`Store.record_types`: the type of many record ids from one read of ``index.db``.

see design/superpowers/specs/2026-10-04-find-entity-unknown-record-type-design.md
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

from sidegraph import store as store_module
from sidegraph.schema import Decision, DecisionKind, Fact, Provenance
from sidegraph.store import Store


@pytest.fixture
def store(tmp_path) -> Iterator[Store]:
    with Store(tmp_path / "t.db") as s:
        yield s


def _decision(store: Store, title: str = "t") -> Decision:
    return store.add_decision(
        Decision(
            title=title,
            kind=DecisionKind.ADR,
            context="c",
            choice="ch",
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual"),
        )
    )


def _fact(store: Store, statement: str = "s") -> Fact:
    return store.add_fact(
        Fact(
            statement=statement,
            source="docs",
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="test"),
        )
    )


def test_record_types_of_nothing_is_empty(store):
    assert store.record_types([]) == {}


def test_record_types_names_decisions_and_facts_and_omits_an_absent_id(store):
    decision = _decision(store)
    fact = _fact(store)

    out = store.record_types([decision.id, fact.id, "01NOSUCHRECORD"])

    assert out == {decision.id: "decision", fact.id: "fact"}


def test_record_types_asks_a_repeated_id_once(store):
    decision = _decision(store)

    assert store.record_types([decision.id, decision.id]) == {decision.id: "decision"}


def test_record_types_covers_every_id_across_chunks(store, monkeypatch):
    """Red against nothing: the method is new. A boundary guard, shown by dropping the last id
    of each slice (spec mutation M3): seven ids over a chunk of three lose two of them."""
    monkeypatch.setattr(store_module, "_RECORD_TYPES_CHUNK", 3)
    expected: dict[str, str] = {}
    for n in range(4):
        expected[_decision(store, f"d{n}").id] = "decision"
    for n in range(3):
        expected[_fact(store, f"f{n}").id] = "fact"

    assert store.record_types(list(expected)) == expected
