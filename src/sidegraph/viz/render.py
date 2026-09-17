"""Turn a :class:`~sidegraph.viz.model.VizGraph` into a vis-network-ready JSON document
(``to_json``) and a self-contained offline HTML file (``to_html``, added in Task 4).

# see design/superpowers/specs/2026-07-12-decision-graph-viz-design.md
"""

from __future__ import annotations

import importlib.resources as resources
import json
from dataclasses import asdict

from .model import VizEdge, VizGraph, VizNode

_KIND_COLOR = {
    "adr": "#4f8ef7",  # blue
    "gotcha": "#e0533d",  # warm red — mistakes
    "constraint": "#e0913d",  # amber
    "lesson": "#c76fe0",  # purple
}
_TIER_COLOR = {2: "#8a94a6", 1: "#6a5acd", 0: "#557a55"}
_FACT_COLOR = "#3dae9c"
_EDGE_STATUS_COLOR = {"live": "#8a94a6", "degraded": "#e0913d", "orphaned": "#e0533d"}
_TERMINAL_STATUSES = frozenset({"superseded", "rejected", "deprecated"})
_PROBLEM_BORDER = "#e0533d"


def _node_json(n: VizNode) -> dict[str, object]:
    if n.type == "decision":
        background = _KIND_COLOR.get(n.kind or "", "#4f8ef7")
        shape = "box"
    elif n.type == "fact":
        background = _FACT_COLOR
        shape = "diamond"
    else:
        background = _TIER_COLOR.get(n.tier if n.tier is not None else 2, "#8a94a6")
        shape = "dot"
    border = _PROBLEM_BORDER if n.problem else background
    title = n.type
    if n.status:
        title += f" · {n.status}"
    if n.tier is not None:
        title += f" · tier {n.tier}"
    return {
        "id": n.id,
        "type": n.type,
        "label": n.label,
        "title": title,
        "color": {
            "background": background,
            "border": border,
            "highlight": {"background": background, "border": "#ffffff"},
        },
        "shape": shape,
        "size": 10 + min(n.degree, 20) * 1.5,
        "opacity": 0.4 if (n.status in _TERMINAL_STATUSES) else 1.0,
        "problem": n.problem,
        "dangling": n.dangling,
        "detail": n.detail,
    }


def _edge_json(e: VizEdge) -> dict[str, object]:
    if e.kind == "anchor":
        color = _EDGE_STATUS_COLOR.get(e.status or "live", "#8a94a6")
        width = 1.0 + 3.0 * e.weight
        label = "" if e.relation in (None, "affects") else e.relation
        dashes: object = False
    elif e.kind == "supersedes":
        color, width, label, dashes = "#b0b0b0", 1.5, "supersedes", True
    elif e.kind == "supports":
        color, width, label, dashes = "#3dae9c", 1.5, "supports", False
    else:  # domain-parent
        color, width, label, dashes = "#6a5acd", 1.5, "parent", [2, 4]
    return {
        "from": e.source,
        "to": e.target,
        "kind": e.kind,
        "color": {"color": color, "opacity": 0.7},
        "width": width,
        "label": label,
        "dashes": dashes,
        "arrows": "to",
        "status": e.status,
        "relation": e.relation,
        "weight": e.weight,
    }


def to_json(graph: VizGraph) -> dict[str, object]:
    """vis-network-ready ``{nodes, edges, stats}`` — embedded in the HTML and emitted by
    ``sidegraph-viz --json``."""
    return {
        "nodes": [_node_json(n) for n in graph.nodes],
        "edges": [_edge_json(e) for e in graph.edges],
        "stats": asdict(graph.stats),
    }


def to_html(graph: VizGraph) -> str:
    """Render a self-contained offline HTML: the vendored vis-network library and the graph
    JSON are inlined into ``template.html``. No external resource is ever loaded."""
    pkg = resources.files("sidegraph.viz")
    template = pkg.joinpath("template.html").read_text(encoding="utf-8")
    library = pkg.joinpath("assets", "vis-network.min.js").read_text(encoding="utf-8")
    # `</` -> `<\/` so no string value inside the JSON can close the <script> element early.
    data = json.dumps(to_json(graph)).replace("</", "<\\/")
    html = template.replace("__SIDEGRAPH_VIS_JS__", library)
    html = html.replace("__SIDEGRAPH_GRAPH_JSON__", data)
    return html
