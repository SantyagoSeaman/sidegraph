from pathlib import Path

from sidegraph.engine.reader import GraphifyReader, NodeRef

FIXTURE = Path(__file__).parent / "fixtures" / "mini_graph.json"


def reader():
    return GraphifyReader(FIXTURE)


def test_graph_version_reads_built_at_commit():
    v = reader().graph_version()
    assert v.startswith("abc123:")
    assert len(v) == len("abc123:") + 12


def test_list_nodes_maps_real_fields():
    nodes = {n.node_id: n for n in reader().list_nodes()}
    assert len(nodes) == 3
    n = nodes["m_cls"]
    assert isinstance(n, NodeRef)
    assert n.name == "Trader"
    assert n.norm_name == "trader"
    assert n.file_type == "code"
    assert n.file_path == "trader/exec.py"
    assert n.line == "L10"
    assert n.community == "1"


def test_get_node_returns_none_for_unknown():
    assert reader().get_node("nope") is None
    assert reader().get_node("m_fn").name == "place_order()"
