from pathlib import Path

from sidegraph.engine.reader import GraphifyReader

FIXTURE = Path(__file__).parent / "fixtures" / "mini_graph.json"


def test_nodes_in_file_returns_code_nodes():
    r = GraphifyReader(FIXTURE)
    ids = {n.node_id for n in r.nodes_in_file("trader/exec.py")}
    assert ids == {"m_cls", "m_fn"}
    assert r.nodes_in_file("nope.py") == []
