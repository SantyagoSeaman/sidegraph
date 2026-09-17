"""MCP `query_structure` / `query_decisions` — thin server wrappers (M5, spec §5 FR8.2)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from sidegraph.engine.reader import GraphifyReader
from sidegraph.schema import AnchorBinding, Decision, DecisionKind, Descriptor, Entity, Provenance
from sidegraph.server import _query_decisions_impl, _query_structure_impl
from sidegraph.store import Store

FIXTURE = Path(__file__).parent / "fixtures" / "mini_graph.json"


def test_query_structure_impl_renders_map(tmp_path):
    r = GraphifyReader(FIXTURE)
    s = Store(tmp_path / "t.db")
    out = _query_structure_impl(s, r, files=["trader/exec.py"], entities=None, budget_chars=4000)
    assert "## Structural map" in out
    assert "Trader" in out


def test_query_structure_impl_no_reader_returns_note(tmp_path):
    s = Store(tmp_path / "t.db")
    out = _query_structure_impl(s, None, files=["trader/exec.py"], entities=None, budget_chars=4000)
    assert "No graph reader available" in out


def test_query_decisions_impl_renders_mistake(tmp_path):
    r = GraphifyReader(FIXTURE)
    s = Store(tmp_path / "t.db")
    e = s.upsert_entity(
        Entity(
            canonical_name="Trader",
            descriptor=Descriptor(name="Trader", file_path="trader/exec.py"),
        )
    )
    d = Decision(
        title="deadlock",
        kind=DecisionKind.GOTCHA,
        context="c",
        choice="order locks",
        valid_from=datetime.now(UTC),
        provenance=Provenance(source="manual"),
    )
    s._write_decision(d)
    s._conn.commit()
    s.add_binding(AnchorBinding(record_id=d.id, entity_id=e.entity_id, tier=2, status="live"))

    out = _query_decisions_impl(
        s,
        r,
        files=["trader/exec.py"],
        entities=None,
        budget_chars=2000,
    )
    assert "deadlock" in out
    assert "## Structural map" not in out


def test_query_decisions_impl_no_reader_still_returns_str(tmp_path):
    s = Store(tmp_path / "t.db")
    out = _query_decisions_impl(
        s,
        None,
        files=["trader/exec.py"],
        entities=None,
        budget_chars=2000,
    )
    assert isinstance(out, str)


def test_query_decisions_tool_docstring_notes_default_structure_budget():
    """M6 review fold-in: query_decisions has no structure_budget param, but peripheral
    gathering still walks the structural subgraph with RetrievalBudget's default -- the
    docstring should say so rather than leave a caller to assume it's free/skipped."""
    from sidegraph.server import query_decisions

    assert "default" in query_decisions.__doc__.lower()
    assert "structure" in query_decisions.__doc__.lower()
