import json

from sidegraph.engine.reader import ANCHORABLE_FILE_TYPES, GraphifyReader
from sidegraph.schema import Descriptor

# Shape observed on the real ADR/SAD corpus: heading nodes with label + source_file +
# community, file_type "document", no built_at_commit (non-git corpus). Concept + rationale
# nodes added for the semantic-docs-layer wave (2026-07-07 design): LLM extraction emits
# `concept` (thematic entities) and `rationale` (recorded reasoning) nodes alongside them.
DOC_GRAPH = {
    "nodes": [
        {
            "id": "adr1",
            "label": "ADR-001-dq.md",
            "norm_label": "adr-001-dq.md",
            "file_type": "document",
            "source_file": "ADR-001-dq.md",
            "community": 12,
        },
        {
            "id": "adr1_title",
            "label": "ADR-001: DQ Execution & Triggering Model",
            "norm_label": "adr-001: dq execution & triggering model",
            "file_type": "document",
            "source_file": "ADR-001-dq.md",
            "community": 3,
        },
        {
            "id": "adr1_ctx",
            "label": "Context",
            "norm_label": "context",
            "file_type": "document",
            "source_file": "ADR-001-dq.md",
            "community": 3,
        },
        {
            "id": "adr2_ctx",
            "label": "Context",
            "norm_label": "context",
            "file_type": "document",
            "source_file": "ADR-002-meta.md",
            "community": 5,
        },
        {
            "id": "concept1",
            "label": "Idempotency",
            "norm_label": "idempotency",
            "file_type": "concept",
            "source_file": "ADR-001-dq.md",
            "community": 3,
        },
        {
            "id": "rationale1",
            "label": "Chose idempotent retries to survive at-least-once delivery",
            "norm_label": "chose idempotent retries to survive at-least-once delivery",
            "file_type": "rationale",
            "source_file": "ADR-001-dq.md",
            "community": 3,
        },
        # Non-anchorable types must stay invisible to resolve/nodes_in_file.
        {
            "id": "img1",
            "label": "Context",
            "norm_label": "context",
            "file_type": "image",
            "source_file": "ADR-001-dq.md",
            "community": 3,
        },
        {
            "id": "img2",
            "label": "flow-diagram",
            "norm_label": "flow-diagram",
            "file_type": "image",
            "source_file": "ADR-001-dq.md",
            "community": 3,
        },
    ],
    "links": [
        {"relation": "contains", "source": "adr1", "target": "adr1_title"},
        {"relation": "contains", "source": "adr1", "target": "adr1_ctx"},
    ],
}


def reader(tmp_path):
    p = tmp_path / "g.json"
    p.write_text(json.dumps(DOC_GRAPH))
    return GraphifyReader(p)


def test_allowlist_is_code_document_concept_rationale():
    assert frozenset({"code", "document", "concept", "rationale"}) == ANCHORABLE_FILE_TYPES


def test_resolve_heading_by_name_and_file(tmp_path):
    r = reader(tmp_path).resolve(Descriptor(name="Context", file_path="ADR-001-dq.md"))
    assert r.status == "resolved"
    assert r.node_id == "adr1_ctx"
    assert r.community == "3"


def test_resolve_heading_with_punctuation(tmp_path):
    r = reader(tmp_path).resolve(Descriptor(name="ADR-001: DQ Execution & Triggering Model"))
    assert r.status == "resolved"
    assert r.node_id == "adr1_title"


def test_repeated_heading_without_file_is_ambiguous(tmp_path):
    r = reader(tmp_path).resolve(Descriptor(name="Context"))
    assert r.status == "ambiguous"
    # The image node with the same label is NOT a candidate.
    assert sorted(r.candidates) == ["adr1_ctx", "adr2_ctx"]


def test_image_only_name_stays_unresolved(tmp_path):
    r = reader(tmp_path).resolve(Descriptor(name="flow-diagram"))
    assert r.status == "unresolved"


def test_resolve_concept_by_name_and_file(tmp_path):
    r = reader(tmp_path).resolve(Descriptor(name="Idempotency", file_path="ADR-001-dq.md"))
    assert r.status == "resolved"
    assert r.node_id == "concept1"


def test_resolve_rationale_by_name(tmp_path):
    r = reader(tmp_path).resolve(
        Descriptor(name="Chose idempotent retries to survive at-least-once delivery")
    )
    assert r.status == "resolved"
    assert r.node_id == "rationale1"


def test_nodes_in_file_returns_doc_nodes_not_images(tmp_path):
    ids = {n.node_id for n in reader(tmp_path).nodes_in_file("ADR-001-dq.md")}
    assert ids == {"adr1", "adr1_title", "adr1_ctx", "concept1", "rationale1"}
