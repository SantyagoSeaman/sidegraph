import json

from sidegraph.engine.reader import GraphifyReader

# Code-shape fixture: LLM-free AST pass emits rationale nodes for code (docstring/comment
# reasoning), each linked to the code entity it explains via `rationale_for`. Empirically
# verified on a real corpus (492 rationale_for edges): source=rationale, target=code.
CODE_SHAPE_GRAPH = {
    "nodes": [
        {
            "id": "rat_code",
            "label": "Retries are idempotent to survive at-least-once delivery",
            "norm_label": "retries are idempotent to survive at-least-once delivery",
            "file_type": "rationale",
            "source_file": "exec.py",
            "community": 1,
        },
        {
            "id": "fn_submit",
            "label": "submit_order",
            "norm_label": "submit_order",
            "file_type": "code",
            "source_file": "exec.py",
            "community": 1,
        },
    ],
    "links": [
        {"relation": "rationale_for", "source": "rat_code", "target": "fn_submit"},
    ],
}

# LLM-doc-shape fixture: semantic extraction over prose emits rationale nodes with no
# rationale_for edges; the recorded reasoning links to what it explains via `references`.
LLM_SHAPE_GRAPH = {
    "nodes": [
        {
            "id": "rat_llm",
            "label": "LN-01: Column and report level lineage is required",
            "norm_label": "ln-01: column and report level lineage is required",
            "file_type": "rationale",
            "source_file": "requirements.md",
            "community": 2,
        },
        {
            "id": "doc_adr",
            "label": "ADR-002-meta.md",
            "norm_label": "adr-002-meta.md",
            "file_type": "document",
            "source_file": "ADR-002-meta.md",
            "community": 2,
        },
        {
            "id": "concept_lineage",
            "label": "Lineage",
            "norm_label": "lineage",
            "file_type": "concept",
            "source_file": "ADR-002-meta.md",
            "community": 2,
        },
    ],
    "links": [
        {"relation": "references", "source": "rat_llm", "target": "doc_adr"},
        {"relation": "references", "source": "rat_llm", "target": "concept_lineage"},
    ],
}

# A rationale that references both an anchorable doc and a non-anchorable image: the image
# must not leak into targets.
IMAGE_TARGET_GRAPH = {
    "nodes": [
        {
            "id": "rat_img",
            "label": "Chose the flow shown in the diagram",
            "norm_label": "chose the flow shown in the diagram",
            "file_type": "rationale",
            "source_file": "ADR-003.md",
            "community": 4,
        },
        {
            "id": "doc3",
            "label": "ADR-003.md",
            "norm_label": "adr-003.md",
            "file_type": "document",
            "source_file": "ADR-003.md",
            "community": 4,
        },
        {
            "id": "img3",
            "label": "flow-diagram",
            "norm_label": "flow-diagram",
            "file_type": "image",
            "source_file": "ADR-003.md",
            "community": 4,
        },
    ],
    "links": [
        {"relation": "references", "source": "rat_img", "target": "doc3"},
        {"relation": "references", "source": "rat_img", "target": "img3"},
    ],
}

# A rationale node with BOTH a rationale_for edge (code shape) and a references edge (LLM
# shape) present at once — targets must come from rationale_for ONLY (precedence, folded in
# from the S1 review).
MIXED_EDGES_GRAPH = {
    "nodes": [
        {
            "id": "rat_mixed",
            "label": "Retries are idempotent to survive at-least-once delivery",
            "norm_label": "retries are idempotent to survive at-least-once delivery",
            "file_type": "rationale",
            "source_file": "exec.py",
            "community": 1,
        },
        {
            "id": "fn_submit",
            "label": "submit_order",
            "norm_label": "submit_order",
            "file_type": "code",
            "source_file": "exec.py",
            "community": 1,
        },
        {
            "id": "doc_unrelated",
            "label": "unrelated.md",
            "norm_label": "unrelated.md",
            "file_type": "document",
            "source_file": "unrelated.md",
            "community": 9,
        },
    ],
    "links": [
        {"relation": "rationale_for", "source": "rat_mixed", "target": "fn_submit"},
        {"relation": "references", "source": "rat_mixed", "target": "doc_unrelated"},
    ],
}

# A rationale node with no outgoing edges at all — observed on the real LLM ADR graph
# (some rationale nodes carry no references/rationale_for edges).
NO_EDGE_GRAPH = {
    "nodes": [
        {
            "id": "rat_lonely",
            "label": "PS-02: No external dependency on legacy pipeline",
            "norm_label": "ps-02: no external dependency on legacy pipeline",
            "file_type": "rationale",
            "source_file": "requirements.md",
            "community": 7,
        },
    ],
    "links": [],
}


def reader_for(tmp_path, graph, name="g.json"):
    p = tmp_path / name
    p.write_text(json.dumps(graph))
    return GraphifyReader(p)


def test_rationale_nodes_code_shape_uses_rationale_for(tmp_path):
    r = reader_for(tmp_path, CODE_SHAPE_GRAPH)
    nodes = r.rationale_nodes()
    assert len(nodes) == 1
    rn = nodes[0]
    assert rn.node_id == "rat_code"
    assert rn.text == "Retries are idempotent to survive at-least-once delivery"
    assert rn.file_path == "exec.py"
    assert rn.community == "1"
    assert [t.node_id for t in rn.targets] == ["fn_submit"]


def test_rationale_nodes_llm_shape_falls_back_to_references(tmp_path):
    r = reader_for(tmp_path, LLM_SHAPE_GRAPH)
    nodes = r.rationale_nodes()
    assert len(nodes) == 1
    rn = nodes[0]
    assert rn.node_id == "rat_llm"
    target_ids = {t.node_id for t in rn.targets}
    assert target_ids == {"doc_adr", "concept_lineage"}


def test_rationale_nodes_excludes_image_targets(tmp_path):
    r = reader_for(tmp_path, IMAGE_TARGET_GRAPH)
    nodes = r.rationale_nodes()
    assert len(nodes) == 1
    target_ids = {t.node_id for t in nodes[0].targets}
    assert target_ids == {"doc3"}
    assert "img3" not in target_ids


def test_rationale_nodes_prefers_rationale_for_over_references_when_both_present(tmp_path):
    r = reader_for(tmp_path, MIXED_EDGES_GRAPH)
    nodes = r.rationale_nodes()
    assert len(nodes) == 1
    target_ids = {t.node_id for t in nodes[0].targets}
    assert target_ids == {"fn_submit"}
    assert "doc_unrelated" not in target_ids


def test_rationale_nodes_with_no_edges_still_returned_with_empty_targets(tmp_path):
    r = reader_for(tmp_path, NO_EDGE_GRAPH)
    nodes = r.rationale_nodes()
    assert len(nodes) == 1
    assert nodes[0].node_id == "rat_lonely"
    assert nodes[0].targets == []
