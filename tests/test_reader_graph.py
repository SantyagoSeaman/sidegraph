from pathlib import Path

from sidegraph.engine.reader import GraphifyReader

FIXTURE = Path(__file__).parent / "fixtures" / "mini_graph.json"


def reader():
    return GraphifyReader(FIXTURE)


def test_neighbors_all_and_filtered():
    r = reader()
    names = {n.node_id for n in r.neighbors("m_fn")}
    assert names == {"m_cls", "o_fn"}  # undirected adjacency
    calls = {n.node_id for n in r.neighbors("m_fn", relations=["calls"])}
    assert calls == {"o_fn"}


def test_neighbors_preserves_link_order_dedup_and_self_loop(tmp_path):
    """Regression guard for the adjacency-index refactor (engine seam perf fix): the
    index must reproduce exactly what the old per-call full scan over ``links`` produced —
    same order, same first-occurrence dedup when a pair repeats under a different
    relation, and the same (slightly odd but pre-existing) self-loop behavior where a
    source==target edge makes a node its own neighbor.
    """
    import json

    from sidegraph.engine.reader import GraphifyReader

    data = {
        "built_at_commit": "x",
        "nodes": [
            {"id": "A", "label": "A", "norm_label": "a", "file_type": "code", "community": "1"},
            {"id": "B", "label": "B", "norm_label": "b", "file_type": "code", "community": "1"},
            {"id": "C", "label": "C", "norm_label": "c", "file_type": "code", "community": "1"},
            {"id": "D", "label": "D", "norm_label": "d", "file_type": "code", "community": "1"},
            {"id": "X", "label": "X", "norm_label": "x", "file_type": "code", "community": "1"},
        ],
        "links": [
            {"source": "A", "target": "B", "relation": "r1"},
            {"source": "C", "target": "A", "relation": "r2"},
            {"source": "A", "target": "B", "relation": "r3"},  # dup pair, different relation
            {"source": "A", "target": "D", "relation": "r4"},  # 3rd distinct neighbor
            {"source": "X", "target": "X", "relation": "self"},  # self-loop
        ],
    }
    p = tmp_path / "g.json"
    p.write_text(json.dumps(data))
    r = GraphifyReader(p)

    # Order: B (link 1), C (link 2), then D (link 4); link 3 is a dup of B and dropped.
    # Three DISTINCT neighbors in a non-palindromic order — a scan-reversal regression
    # would give ["D", "C", "B"] and fail here (the old ["B","C"] fixture was symmetric).
    assert [n.node_id for n in r.neighbors("A")] == ["B", "C", "D"]
    # Relation filter narrows to just the matching link (r2 is the C->A edge, so it's the
    # only one touching A under that relation; r1/r3's relation is excluded).
    assert [n.node_id for n in r.neighbors("A", relations=["r2"])] == ["C"]
    assert [n.node_id for n in r.neighbors("A", relations=["does-not-exist"])] == []
    assert [n.node_id for n in r.neighbors("C", relations=["r2"])] == ["A"]
    # Self-loop: X is returned as its own neighbor, matching the pre-refactor behavior.
    assert [n.node_id for n in r.neighbors("X")] == ["X"]


def test_containing_returns_community():
    assert reader().containing("m_cls") == "1"
    assert reader().containing("nope") is None


def test_communities_group_members_with_god_node():
    comms = {c.community_id: c for c in reader().communities()}
    assert set(comms) == {"1", "2"}
    assert set(comms["1"].members) == {"m_cls", "m_fn"}
    # m_fn has degree 2 (contains + calls), m_cls degree 1 -> god node is m_fn
    assert comms["1"].god_node == "m_fn"


def test_subgraph_respects_budget():
    sg = reader().subgraph(["m_cls"], budget=2)
    assert len(sg.nodes) == 2  # m_cls + m_fn, stops at budget
    ids = {n.node_id for n in sg.nodes}
    assert ids == {"m_cls", "m_fn"}
