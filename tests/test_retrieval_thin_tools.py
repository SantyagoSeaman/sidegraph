"""``query_structure`` / ``query_decisions`` — the thin tools (M5, spec §5 FR8.2): the two
halves of ``get_task_context`` exposed separately for cheap follow-ups."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from sidegraph.engine.reader import GraphifyReader
from sidegraph.retrieval import Seed, query_decisions, query_structure
from sidegraph.schema import (
    AnchorBinding,
    Decision,
    DecisionKind,
    DecisionStatus,
    Descriptor,
    Entity,
    Provenance,
)
from sidegraph.store import Store

FIXTURE = Path(__file__).parent / "fixtures" / "mini_graph.json"


# -- query_structure ------------------------------------------------------------------------


def test_query_structure_reader_none_returns_note(tmp_path):
    s = Store(tmp_path / "t.db")
    out = query_structure([Seed(file_path="trader/exec.py")], s, None)
    assert "No graph reader available" in out


def test_query_structure_renders_structural_map(tmp_path):
    r = GraphifyReader(FIXTURE)
    s = Store(tmp_path / "t.db")
    out = query_structure([Seed(file_path="trader/exec.py")], s, r)
    assert "## Structural map" in out
    assert "Trader" in out


def test_query_structure_no_structure_found(tmp_path):
    r = GraphifyReader(FIXTURE)
    s = Store(tmp_path / "t.db")
    out = query_structure([Seed(file_path="does/not/exist.py")], s, r)
    assert out == "No structural context found."


def test_query_structure_respects_budget_chars(tmp_path):
    r = GraphifyReader(FIXTURE)
    s = Store(tmp_path / "t.db")
    out = query_structure([Seed(file_path="trader/exec.py")], s, r, budget_chars=10)
    # too tight to fit any leaf line, no domains registered -> falls back to the "no
    # structural context" message rather than an empty "## Structural map" header
    assert out == "No structural context found."


# -- query_decisions ------------------------------------------------------------------------


def test_query_decisions_no_crash_without_reader(tmp_path):
    s = Store(tmp_path / "t.db")
    out = query_decisions([Seed(file_path="trader/exec.py")], s, None)
    assert isinstance(out, str)


def test_query_decisions_surfaces_seed_mistake(tmp_path):
    r = GraphifyReader(FIXTURE)
    s = Store(tmp_path / "t.db")
    e = s.upsert_entity(
        Entity(
            canonical_name="Trader",
            descriptor=Descriptor(name="Trader", file_path="trader/exec.py"),
        )
    )
    d = Decision(
        title="race",
        kind=DecisionKind.GOTCHA,
        context="c",
        choice="lock it",
        valid_from=datetime.now(UTC),
        provenance=Provenance(source="manual"),
    )
    s._write_decision(d)
    s._conn.commit()
    s.add_binding(AnchorBinding(record_id=d.id, entity_id=e.entity_id, tier=2, status="live"))

    out = query_decisions([Seed(name="Trader", file_path="trader/exec.py")], s, r)
    assert "race" in out
    assert "## Structural map" not in out  # decisions-only, no structure block


def test_query_decisions_no_decisions_found(tmp_path):
    s = Store(tmp_path / "t.db")
    out = query_decisions([Seed(file_path="trader/exec.py")], s, None)
    assert out == "No context found."


def test_query_decisions_respects_budget_chars(tmp_path):
    r = GraphifyReader(FIXTURE)
    s = Store(tmp_path / "t.db")
    e = s.upsert_entity(
        Entity(
            canonical_name="Trader",
            descriptor=Descriptor(name="Trader", file_path="trader/exec.py"),
        )
    )
    for i in range(5):
        d = Decision(
            title=f"gotcha{i} with a fairly long rationale line",
            kind=DecisionKind.GOTCHA,
            context="c",
            choice=f"do thing number {i} very carefully",
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual"),
        )
        s._write_decision(d)
        s._conn.commit()
        s.add_binding(AnchorBinding(record_id=d.id, entity_id=e.entity_id, tier=2, status="live"))

    unbounded = query_decisions(
        [Seed(name="Trader", file_path="trader/exec.py")],
        s,
        r,
        budget_chars=2000,
    )
    tight = query_decisions(
        [Seed(name="Trader", file_path="trader/exec.py")],
        s,
        r,
        budget_chars=40,
    )
    assert len(tight) < len(unbounded)


def test_query_decisions_renders_proposals_last_outside_mistakes(tmp_path):
    reader = GraphifyReader(FIXTURE)
    store = Store(tmp_path / "t.db")
    entity = store.upsert_entity(
        Entity(
            canonical_name="Trader",
            descriptor=Descriptor(name="Trader", file_path="trader/exec.py"),
        )
    )
    records = [
        Decision(
            title="Accepted ADR",
            kind=DecisionKind.ADR,
            status=DecisionStatus.ACCEPTED,
            context="c",
            choice="ch",
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual"),
        ),
        Decision(
            title="Proposed gotcha",
            kind=DecisionKind.GOTCHA,
            status=DecisionStatus.PROPOSED,
            context="c",
            choice="ch",
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual"),
        ),
    ]
    for decision in records:
        store.add_decision(decision)
        store.add_binding(
            AnchorBinding(record_id=decision.id, entity_id=entity.entity_id, tier=2, status="live")
        )

    out = query_decisions([Seed(file_path="trader/exec.py")], store, reader)

    assert "## ⚠ Known mistakes & gotchas" not in out
    assert out.index("Accepted ADR") < out.index("## Unratified proposals")
    assert "[unratified] Proposed gotcha" in out
