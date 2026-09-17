from pathlib import Path

from sidegraph.engine.reader import GraphifyReader
from sidegraph.retrieval import Seed, TaskContext, resolve_seeds
from sidegraph.schema import Descriptor, Entity
from sidegraph.store import Store

FIXTURE = Path(__file__).parent / "fixtures" / "mini_graph.json"


def test_render_orders_sections_mistakes_first():
    ctx = TaskContext(
        mistakes=["- gotcha A"], decisions=["- adr B"], structure=["- node C"], related=["- rel D"]
    )
    out = ctx.render()
    assert out.index("gotcha A") < out.index("adr B") < out.index("node C") < out.index("rel D")


def test_render_empty_is_note():
    assert TaskContext().render() == "No context found."


def test_resolve_file_seed_to_nodes(tmp_path):
    r = GraphifyReader(FIXTURE)
    s = Store(tmp_path / "t.db")
    res = resolve_seeds([Seed(file_path="trader/exec.py")], r, s)
    assert set(res.seed_node_ids) == {"m_cls", "m_fn"}
    assert res.seed_communities == ["1"]
    assert res.seed_entities == []  # no stored entities yet


def test_resolve_entity_ref_seed(tmp_path):
    r = GraphifyReader(FIXTURE)
    s = Store(tmp_path / "t.db")
    e = s.upsert_entity(
        Entity(
            canonical_name="Trader",
            descriptor=Descriptor(name="Trader", file_path="trader/exec.py"),
        )
    )
    res = resolve_seeds([Seed(name="Trader", file_path="trader/exec.py")], r, s)
    assert res.seed_node_ids == ["m_cls"]
    assert [x.entity_id for x in res.seed_entities] == [e.entity_id]


def test_resolve_no_reader_is_empty(tmp_path):
    s = Store(tmp_path / "t.db")
    res = resolve_seeds([Seed(file_path="trader/exec.py")], None, s)
    assert res.seed_node_ids == [] and res.seed_entities == []
