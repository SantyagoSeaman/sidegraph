from pathlib import Path

from sidegraph.engine.reader import GraphifyReader
from sidegraph.schema import Descriptor

FIXTURE = Path(__file__).parent / "fixtures" / "mini_graph.json"


def reader():
    return GraphifyReader(FIXTURE)


def test_resolve_single_match_by_name():
    r = reader().resolve(Descriptor(name="Trader"))
    assert r.status == "resolved"
    assert r.node_id == "m_cls"
    assert r.community == "1"


def test_resolve_canonicalizes_decorated_name():
    # query bare "place_order" matches node label "place_order()"
    r = reader().resolve(Descriptor(name="place_order"))
    assert r.status == "resolved"
    assert r.node_id == "m_fn"


def test_resolve_file_filter_narrows():
    r = reader().resolve(Descriptor(name="helper", file_path="util/misc.py"))
    assert r.status == "resolved"
    assert r.node_id == "o_fn"


def test_resolve_unresolved_when_absent():
    r = reader().resolve(Descriptor(name="does_not_exist"))
    assert r.status == "unresolved"
    assert r.node_id is None


def test_resolve_ambiguous_returns_candidates(tmp_path):
    # two code nodes share a canonical name in different files
    import json

    data = {
        "built_at_commit": "x",
        "nodes": [
            {
                "id": "a",
                "label": "run()",
                "norm_label": "run()",
                "file_type": "code",
                "source_file": "a.py",
                "community": 1,
            },
            {
                "id": "b",
                "label": "run()",
                "norm_label": "run()",
                "file_type": "code",
                "source_file": "b.py",
                "community": 2,
            },
        ],
        "links": [],
    }
    p = tmp_path / "g.json"
    p.write_text(json.dumps(data))
    r = GraphifyReader(p).resolve(Descriptor(name="run"))
    assert r.status == "ambiguous"
    assert sorted(r.candidates) == ["a", "b"]
    assert r.node_id is None


def test_resolve_ambiguous_carries_shared_community(tmp_path):
    import json

    data = {
        "built_at_commit": "x",
        "nodes": [
            {
                "id": "a",
                "label": "run()",
                "norm_label": "run()",
                "file_type": "code",
                "source_file": "m.py",
                "community": 7,
            },
            {
                "id": "b",
                "label": "run()",
                "norm_label": "run()",
                "file_type": "code",
                "source_file": "m.py",
                "community": 7,
            },
        ],
        "links": [],
    }
    p = tmp_path / "g.json"
    p.write_text(json.dumps(data))
    r = GraphifyReader(p).resolve(Descriptor(name="run"))
    assert r.status == "ambiguous"
    assert r.community == "7"  # shared community carried


def test_resolve_ambiguous_different_communities_yields_none(tmp_path):
    import json

    data = {
        "built_at_commit": "x",
        "nodes": [
            {
                "id": "a",
                "label": "run()",
                "norm_label": "run()",
                "file_type": "code",
                "source_file": "a.py",
                "community": 1,
            },
            {
                "id": "b",
                "label": "run()",
                "norm_label": "run()",
                "file_type": "code",
                "source_file": "b.py",
                "community": 2,
            },
        ],
        "links": [],
    }
    p = tmp_path / "g.json"
    p.write_text(json.dumps(data))
    r = GraphifyReader(p).resolve(Descriptor(name="run"))
    assert r.status == "ambiguous"
    assert r.community is None
