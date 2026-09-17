from sidegraph.viz.model import VizEdge, VizGraph, VizNode, VizStats
from sidegraph.viz.render import to_json


def _graph():
    nodes = [
        VizNode(id="d1", type="decision", label="adr", kind="adr", status="accepted", detail={}),
        VizNode(
            id="d2",
            type="decision",
            label="oops",
            kind="gotcha",
            status="accepted",
            detail={},
            problem=True,
        ),
        VizNode(id="d3", type="decision", label="old", kind="adr", status="superseded", detail={}),
        VizNode(id="f1", type="fact", label="fact", status="accepted", detail={}),
        VizNode(id="e1", type="entity", label="alpha", tier=2, detail={}),
    ]
    edges = [
        VizEdge(source="d1", target="e1", kind="anchor", status="live", relation="affects"),
        VizEdge(source="d2", target="e1", kind="anchor", status="orphaned", relation="affects"),
        VizEdge(source="d1", target="e1", kind="anchor", status="degraded", relation="creates"),
        VizEdge(source="f1", target="d1", kind="supports"),
    ]
    return VizGraph(nodes=nodes, edges=edges, stats=VizStats(decisions=3, facts=1, entities=1))


def test_to_json_shape():
    doc = to_json(_graph())
    assert set(doc) == {"nodes", "edges", "stats"}
    assert doc["stats"]["decisions"] == 3


def test_node_colors_and_shapes():
    by_id = {n["id"]: n for n in to_json(_graph())["nodes"]}
    assert by_id["d1"]["color"]["background"] == "#4f8ef7"  # adr blue
    assert by_id["d1"]["shape"] == "box"
    assert by_id["d2"]["color"]["background"] == "#e0533d"  # gotcha warm
    assert by_id["d2"]["color"]["border"] == "#e0533d"  # problem -> red border
    assert by_id["d3"]["opacity"] == 0.4  # superseded dimmed
    assert by_id["f1"]["shape"] == "diamond"
    assert by_id["e1"]["shape"] == "dot"


def test_edge_status_colors():
    edges = to_json(_graph())["edges"]
    colors = {(e["kind"], e.get("status")): e["color"]["color"] for e in edges}
    assert colors[("anchor", "live")] == "#8a94a6"
    assert colors[("anchor", "orphaned")] == "#e0533d"
    assert colors[("anchor", "degraded")] == "#e0913d"
    supports = next(e for e in edges if e["kind"] == "supports")
    assert supports["color"]["color"] == "#3dae9c"
    assert supports["arrows"] == "to"


def test_anchor_relation_label_hidden_for_default():
    edges = to_json(_graph())["edges"]
    live = next(e for e in edges if e["kind"] == "anchor" and e["status"] == "live")
    degraded = next(e for e in edges if e["kind"] == "anchor" and e["status"] == "degraded")
    assert live["label"] == ""  # relation "affects" is the default -> no label
    assert degraded["label"] == "creates"  # non-default relation shown


def test_structural_edge_styles():
    nodes = [
        VizNode(id="d1", type="decision", label="new", kind="adr", status="accepted", detail={}),
        VizNode(id="d2", type="decision", label="old", kind="adr", status="superseded", detail={}),
        VizNode(id="e1", type="entity", label="child", tier=1, detail={}),
        VizNode(id="e2", type="entity", label="parent", tier=1, detail={}),
    ]
    edges = [
        VizEdge(source="d1", target="d2", kind="supersedes"),
        VizEdge(source="e1", target="e2", kind="domain-parent"),
    ]
    graph = VizGraph(nodes=nodes, edges=edges, stats=VizStats(decisions=2, entities=2))
    doc = to_json(graph)
    by_kind = {e["kind"]: e for e in doc["edges"]}

    supersedes = by_kind["supersedes"]
    assert supersedes["color"]["color"] == "#b0b0b0"
    assert supersedes["dashes"] is True
    assert supersedes["label"] == "supersedes"

    domain_parent = by_kind["domain-parent"]
    assert domain_parent["color"]["color"] == "#6a5acd"
    assert domain_parent["dashes"] == [2, 4]
    assert domain_parent["label"] == "parent"
