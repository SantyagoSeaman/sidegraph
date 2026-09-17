import json

import pytest

from sidegraph.engine.reader import GraphifyReader
from sidegraph.retrieval import Seed, get_task_context
from sidegraph.server import _propose_decisions_impl, _ratify_decisions_impl
from sidegraph.store import Store
from sidegraph.sync import sync

# Non-git doc corpus: NO built_at_commit — sync must gate on the content-hash fallback.
DOC_GRAPH_A = {
    "nodes": [
        {
            "id": "adr1_vocab",
            "label": "Vocabulary and Entity Relations",
            "norm_label": "vocabulary and entity relations",
            "file_type": "document",
            "source_file": "ADR-001-dq.md",
            "community": 3,
        },
        {
            "id": "adr5_runner",
            "label": "DQ Runner Contract",
            "norm_label": "dq runner contract",
            "file_type": "document",
            "source_file": "ADR-005-runner.md",
            "community": 3,
        },
    ],
    "links": [],
}
# Rebuild: identical headings, Leiden renumbered 3 -> 11.
DOC_GRAPH_B = {"nodes": [dict(n, community=11) for n in DOC_GRAPH_A["nodes"]], "links": []}


@pytest.fixture(autouse=True)
def _no_ambient_initiative(monkeypatch):
    monkeypatch.setattr("sidegraph.capture._derive_initiative", lambda: None)


def _write(tmp_path, name, data):
    p = tmp_path / name
    p.write_text(json.dumps(data))
    return p


def _capture_gotcha(store, reader):
    results = _propose_decisions_impl(
        store,
        reader,
        [
            {
                "title": "DQ vocabulary is normative",
                "kind": "gotcha",
                "context": "c",
                "choice": "keep",
                "anchors": [
                    {"name": "Vocabulary and Entity Relations", "file_path": "ADR-001-dq.md"}
                ],
            }
        ],
        session_id="s-doc",
    )
    _ratify_decisions_impl(store, accept=[results[0]["decision_id"]])
    return results[0]["decision_id"]


def test_doc_loop_capture_retrieve_sync_repoint(tmp_path):
    store = Store(tmp_path / "d.db")
    reader_a = GraphifyReader(_write(tmp_path, "a.json", DOC_GRAPH_A))
    decision_id = _capture_gotcha(store, reader_a)

    # Provenance carries the content-hash version (no git anywhere).
    d = store.get_decision(decision_id)
    assert d.provenance.graph_version.startswith("content:")

    # Seeding by the anchored doc itself -> mistakes-first.
    ctx = get_task_context([Seed(file_path="ADR-001-dq.md")], store, reader_a)
    assert any("DQ vocabulary is normative" in m for m in ctx.mistakes)

    # Seeding by the OTHER doc in the cluster -> related, via community:3.
    ctx = get_task_context([Seed(file_path="ADR-005-runner.md")], store, reader_a)
    assert any("DQ vocabulary is normative" in r for r in ctx.related)

    # Stamp the version gate on graph A so the renumbered rebuild must RUN, not skip.
    first = sync(store, reader_a)
    assert not first.skipped

    # Rebuild renumbers 3 -> 11; the content hash differs, so sync runs and re-points.
    reader_b = GraphifyReader(_write(tmp_path, "b.json", DOC_GRAPH_B))
    report = sync(store, reader_b)
    assert not report.skipped
    assert sum(o.repointed for o in report.outcomes) >= 1

    # Fallback works under the NEW numbering.
    ctx = get_task_context([Seed(file_path="ADR-005-runner.md")], store, reader_b)
    assert any("DQ vocabulary is normative" in r for r in ctx.related)


def test_doc_sync_skips_on_unchanged_content(tmp_path):
    store = Store(tmp_path / "d.db")
    reader_a = GraphifyReader(_write(tmp_path, "a.json", DOC_GRAPH_A))
    _capture_gotcha(store, reader_a)

    first = sync(store, reader_a)
    assert not first.skipped
    # Same file text -> same content version -> the gate skips.
    again = sync(store, GraphifyReader(tmp_path / "a.json"))
    assert again.skipped
