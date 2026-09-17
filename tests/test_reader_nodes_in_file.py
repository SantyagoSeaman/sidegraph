from pathlib import Path

from sidegraph.engine.reader import GraphifyReader

FIXTURE = Path(__file__).parent / "fixtures" / "mini_graph.json"


def test_nodes_in_file_returns_code_nodes():
    r = GraphifyReader(FIXTURE)
    ids = {n.node_id for n in r.nodes_in_file("trader/exec.py")}
    assert ids == {"m_cls", "m_fn"}
    assert r.nodes_in_file("nope.py") == []


def test_nodes_in_file_excludes_non_anchorable_types(tmp_path):
    import json

    data = {
        "built_at_commit": "x",
        "nodes": [
            {
                "id": "code1",
                "label": "f()",
                "norm_label": "f()",
                "file_type": "code",
                "source_file": "a.py",
                "community": 1,
            },
            {
                "id": "doc1",
                "label": "design note",
                "norm_label": "design note",
                "file_type": "document",
                "source_file": "a.py",
                "community": 1,
            },
            {
                "id": "img1",
                "label": "diagram",
                "norm_label": "diagram",
                "file_type": "image",
                "source_file": "a.py",
                "community": 1,
            },
        ],
        "links": [],
    }
    p = tmp_path / "g.json"
    p.write_text(json.dumps(data))
    ids = {n.node_id for n in GraphifyReader(p).nodes_in_file("a.py")}
    # code and document nodes are anchorable; image nodes in the same file are excluded
    assert ids == {"code1", "doc1"}
