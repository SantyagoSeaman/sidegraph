"""Generated ADRs are documentation projections, never fresh capture evidence."""

from pathlib import Path

import pytest

from sidegraph.bootstrap.model import ScanResult
from sidegraph.bootstrap.planner import plan_sources
from sidegraph.doc_import import parse_decision_docs
from sidegraph.profiles import get_profile

BODY = (
    "# Cache decision\n\n## Context\n\nLocal storage is sufficient.\n\n## Decision\n\n"
    "Keep it local. The disk-backed cache remains scoped to this process. "
    "Retain the original source references when producing documentation, keep human "
    "changes visible, and review changes before writing them to the working tree. "
    "No remote service is needed for this decision.\n"
)

HEADER = "<!-- sidegraph-adr:v1 id=foreign-record number=1 sha256=" + "0" * 64 + " -->\n"


@pytest.mark.parametrize(
    "prefix", [HEADER, "\ufeff" + HEADER, "<!-- sidegraph-adr:v999 malformed -->\n"]
)
def test_projection_is_not_a_new_import_candidate(prefix):
    docs, reason = parse_decision_docs(prefix + BODY, "docs/adr/new.md")
    assert docs == []
    assert reason == "generated-adr"


@pytest.mark.parametrize(
    "example",
    [
        "`" + HEADER.rstrip() + "`",
        "    " + HEADER.rstrip(),
        "```html\n" + HEADER + "```",
        "> " + HEADER.rstrip(),
    ],
)
def test_quoted_marker_is_an_ordinary_human_document(example):
    docs, _ = parse_decision_docs(BODY + "\n" + example, "docs/adr/human.md")
    assert len(docs) == 1


def test_bootstrap_has_no_candidate_and_an_informational_issue(tmp_path: Path):
    rel = "docs/adr/new.md"
    doc = tmp_path / rel
    doc.parent.mkdir(parents=True)
    doc.write_text(HEADER + BODY, encoding="utf-8")
    plan = plan_sources(
        tmp_path, ScanResult(root=str(tmp_path), files=(rel,)), get_profile("generic-adr")
    )
    assert plan.candidates == ()
    assert len(plan.issues) == 1 and plan.issues[0].warning.value == "generated-projection"
    assert not (tmp_path / ".sidegraph").exists()


def test_real_import_skips_a_foreign_projection_before_anchor_writes(tmp_path, monkeypatch):
    import json

    from sidegraph.doc_import import import_docs
    from sidegraph.engine.reader import GraphifyReader
    from sidegraph.store import Store

    monkeypatch.chdir(tmp_path)
    rel = "docs/adr/generated.md"
    doc = tmp_path / rel
    doc.parent.mkdir(parents=True)
    doc.write_text(HEADER + BODY, encoding="utf-8")
    graph = tmp_path / "graph.json"
    graph.write_text(
        json.dumps(
            {
                "nodes": [
                    {
                        "id": "doc",
                        "label": "generated.md",
                        "norm_label": "generated.md",
                        "source_file": rel,
                        "file_type": "document",
                        "community": 0,
                    }
                ],
                "links": [],
            }
        ),
        encoding="utf-8",
    )
    reader = GraphifyReader(str(graph))
    with Store(tmp_path / ".sidegraph") as store:
        before = {str(p): p.read_bytes() for p in store.path.rglob("*") if p.is_file()}
        report = import_docs(store, reader, [rel], any_doc=True)
        assert report.imported == 0 and report.superseded == 0
        assert report.skipped_generated_adr == 1
        assert {str(p): p.read_bytes() for p in store.path.rglob("*") if p.is_file()} == before
