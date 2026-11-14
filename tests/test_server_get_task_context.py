from datetime import UTC, datetime
from pathlib import Path

from sidegraph import retrieval, server
from sidegraph.engine.reader import GraphifyReader
from sidegraph.retrieval import RetrievalBudget, Seed
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
    assert "## Structural map" not in out


def test_answer_keeps_the_walk(tmp_path, monkeypatch):
    from tests.test_ratify_policy import GOLDEN_GRAPH, _golden_store

    with _golden_store(tmp_path / "g", datetime.now(UTC)) as store:
        reader = GraphifyReader(GOLDEN_GRAPH)
        ctx = retrieval.get_task_context(
            [Seed(file_path="trader/exec.py")], store, reader, RetrievalBudget(4000, 6000)
        )
        shown = []
        monkeypatch.setattr(server, "_record", lambda _store, ids, *_a, **_kw: shown.extend(ids))

        out = _get_task_context_impl(store, reader, ["trader/exec.py"], None, 4000, 6000)

        expected = ctx.render_parts(include_structure=False)[0]
        assert out.startswith(expected)
        assert "helper() stays side-effect free" in out
        assert shown == ctx.shown_ids


def test_a_seed_without_memory_answers_no_context_found(tmp_path):
    with Store(tmp_path / "s") as store:
        out = _get_task_context_impl(
            store, GraphifyReader(FIXTURE), ["trader/exec.py"], None, 4000, 6000
        )

    assert out.startswith("No context found.")
    assert "## Structural map" not in out


def test_the_tool_description_points_to_query_structure():
    doc = " ".join(server.get_task_context.__doc__.split())
    assert "then a structural map" not in doc
    assert "query_structure" in doc


def test_the_tool_description_says_what_comes_first():
    doc = " ".join(server.get_task_context.__doc__.split())
    assert "mistakes ranked first" not in doc
    assert "Records of a symbol named in ``entities`` take the budget before" in doc
    assert "among each, mistakes come before decisions" in doc
