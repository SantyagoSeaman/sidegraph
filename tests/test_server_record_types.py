"""`find_entity` and `get_entity_history` resolve a binding's record type in one query, and
call a record that exists nowhere ``unknown`` instead of a decision.

see design/superpowers/specs/2026-10-04-find-entity-unknown-record-type-design.md
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

import pytest

from sidegraph.schema import (
    AnchorBinding,
    Decision,
    DecisionKind,
    Descriptor,
    Entity,
    Fact,
    Provenance,
)
from sidegraph.server import _find_entity_impl, _get_entity_history_impl
from sidegraph.store import Store

_RECORD_TABLE_READ = re.compile(r"\bFROM\s+(decisions|facts)\b", re.IGNORECASE)


@contextmanager
def _record_table_reads(store: Store) -> Iterator[dict[str, list[str]]]:
    """Collect every statement the store's connection runs that reads ``decisions`` or
    ``facts``. A statement counts once however many of the two tables it reads, so a
    ``UNION ALL`` over both is one read."""
    seen: dict[str, list[str]] = {"decisions": [], "facts": [], "all": []}

    def trace(statement: str) -> None:
        tables = {m.lower() for m in _RECORD_TABLE_READ.findall(statement)}
        if not tables:
            return
        seen["all"].append(statement)
        for table in tables:
            seen[table].append(statement)

    store._conn.set_trace_callback(trace)
    try:
        yield seen
    finally:
        store._conn.set_trace_callback(None)


def _entity(store: Store) -> Entity:
    return store.upsert_entity(
        Entity(
            canonical_name="Trader",
            descriptor=Descriptor(name="Trader", file_path="trader/exec.py"),
        )
    )


def _decision(store: Store, entity: Entity, title: str = "t") -> Decision:
    d = store.add_decision(
        Decision(
            title=title,
            kind=DecisionKind.ADR,
            context="c",
            choice="ch",
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual"),
        )
    )
    store.add_binding(AnchorBinding(record_id=d.id, entity_id=entity.entity_id, tier=2))
    return d


def _fact(store: Store, entity: Entity, statement: str = "s") -> Fact:
    f = store.add_fact(
        Fact(
            statement=statement,
            source="docs",
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="test"),
        )
    )
    store.add_binding(AnchorBinding(record_id=f.id, entity_id=entity.entity_id, tier=2))
    return f


def _dangle_decision(store: Store, entity: Entity, title: str = "gone") -> str:
    """A binding whose decision is no longer in the index: the state a record file removed by
    hand (or lost in a merge) leaves behind, since ``add_binding`` refuses to create it."""
    d = _decision(store, entity, title)
    with store._lock:
        store._conn.execute("DELETE FROM decisions WHERE id = ?", (d.id,))
        store._conn.commit()
    assert store.get_decision(d.id) is None
    return d.id


@pytest.fixture
def store(tmp_path) -> Iterator[Store]:
    with Store(tmp_path / "t.db") as s:
        yield s


def test_find_entity_reports_unknown_for_a_binding_whose_record_exists_nowhere(store):
    """T1. Red against unfixed code: the fallback labelled it ``"decision"``."""
    entity = _entity(store)
    dangling = _dangle_decision(store, entity)

    out = _find_entity_impl(store, "Trader", "trader/exec.py")

    assert out["bindings"] == [
        {"record_id": dangling, "record_type": "unknown", "tier": 2, "status": "live"}
    ]


def test_find_entity_labels_a_decision_a_fact_and_a_dangling_record(store):
    """T2. Red against unfixed code on the third label only: the first two are pinned by
    ``test_server_find_entity`` and pass before the fix too."""
    entity = _entity(store)
    decision = _decision(store, entity)
    fact = _fact(store, entity)
    dangling = _dangle_decision(store, entity)

    out = _find_entity_impl(store, "Trader", "trader/exec.py")

    types = {b["record_id"]: b["record_type"] for b in out["bindings"]}
    assert types == {decision.id: "decision", fact.id: "fact", dangling: "unknown"}


def test_find_entity_reads_the_record_tables_once_for_all_bindings(store):
    """T3. Red against unfixed code: one ``get_fact`` statement per binding (here six)."""
    entity = _entity(store)
    for n in range(2):
        _decision(store, entity, f"d{n}")
        _fact(store, entity, f"f{n}")
        _dangle_decision(store, entity, f"x{n}")

    with _record_table_reads(store) as seen:
        out = _find_entity_impl(store, "Trader", "trader/exec.py")

    assert len(out["bindings"]) == 6
    assert len(seen["all"]) == 1, seen["all"]


def test_get_entity_history_skips_a_dangling_binding_and_keeps_the_rest(store):
    """T5. Red against nothing: it passes before and after. A guard that moving the history
    onto ``record_types`` keeps "an unknown record is skipped" and the newest-first order."""
    entity = _entity(store)
    older = _decision(store, entity, "older")
    _dangle_decision(store, entity)
    newer = _fact(store, entity)

    history = _get_entity_history_impl(store, entity.entity_id)

    assert [(h["id"], h["record_type"]) for h in history] == [
        (newer.id, "fact"),
        (older.id, "decision"),
    ]


def test_get_entity_history_reads_the_decisions_table_at_most_once(store):
    """T6. Red against unfixed code: ``get_decision`` ran once per binding, so facts and
    dangling bindings each cost a miss on the decisions table first (here five)."""
    entity = _entity(store)
    for n in range(3):
        _fact(store, entity, f"f{n}")
    for n in range(2):
        _dangle_decision(store, entity, f"x{n}")

    with _record_table_reads(store) as seen:
        history = _get_entity_history_impl(store, entity.entity_id)

    assert len(history) == 3
    assert len(seen["decisions"]) <= 1, seen["decisions"]
