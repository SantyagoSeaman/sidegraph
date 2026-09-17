from datetime import UTC, datetime
from pathlib import Path

from sidegraph.engine.reader import GraphifyReader
from sidegraph.schema import (
    AnchorBinding,
    Decision,
    DecisionKind,
    DecisionStatus,
    Descriptor,
    Entity,
    Provenance,
)
from sidegraph.server import _get_task_context_impl
from sidegraph.store import Store

FIXTURE = Path(__file__).parent / "fixtures" / "mini_graph.json"


def test_impl_renders_seed_mistake(tmp_path):
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
        status=DecisionStatus.ACCEPTED,
        context="c",
        choice="order locks",
        valid_from=datetime.now(UTC),
        provenance=Provenance(source="manual"),
    )
    s._write_decision(d)
    s._conn.commit()
    s.add_binding(AnchorBinding(record_id=d.id, entity_id=e.entity_id, tier=2, status="live"))
    out = _get_task_context_impl(
        s, r, files=["trader/exec.py"], entities=None, structure_budget=4000, memory_budget=2000
    )
    assert "deadlock" in out
    assert out.index("deadlock") < out.index("Structural map")


def test_impl_no_reader_still_returns_str(tmp_path):
    s = Store(tmp_path / "t.db")
    out = _get_task_context_impl(
        s, None, files=["trader/exec.py"], entities=None, structure_budget=4000, memory_budget=2000
    )
    assert isinstance(out, str)
