"""The context-sized store behind the whole-text cap tests, built once per test module.

``ctx_env`` is a store of 8 files x 30 decisions over a 483-node graph, the shape measured on
field stores. Building it costs seconds, so a module builds one template and every test gets a
fresh copy of the store directory: tests that record shows, events or drift metadata cannot leak
into one another. The graph file and its reader are read-only and shared.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest

import sidegraph.server as server
from sidegraph.engine.reader import GraphifyReader
from sidegraph.store import Store

H2_HEADINGS = [
    "## ⚠ Known mistakes & gotchas",
    "## Decisions",
    "## Known facts",
    "## Related",
    "## Unratified proposals",
    "## Notes",
]


def build_context_graph(path: Path, files: int = 8) -> Path:
    nodes, links = [], []
    for f in range(files):
        for i in range(60):
            nodes.append(
                {
                    "id": f"f{f}n{i}",
                    "label": f"Worker{f}_{i}()",
                    "norm_label": f"worker{f}_{i}()",
                    "file_type": "code",
                    "source_file": f"src/pkg{f}/mod{f}.py",
                    "source_location": f"L{i}",
                    "community": f,
                }
            )
            if i:
                links.append(
                    {"source": f"f{f}n{i - 1}", "target": f"f{f}n{i}", "relation": "calls"}
                )
        if f:
            links.append({"source": f"f{f}n0", "target": f"f{f - 1}n5", "relation": "calls"})
    # a file with no record of its own, in pkg0's directory but not linked to it: the
    # nearest-anchored records answer for it
    for i in range(3):
        nodes.append(
            {
                "id": f"e{i}",
                "label": f"Empty{i}()",
                "norm_label": f"empty{i}()",
                "file_type": "code",
                "source_file": "src/pkg0/empty.py",
                "source_location": f"L{i}",
                "community": 99,
            }
        )
    path.write_text(json.dumps({"built_at_commit": "v1", "nodes": nodes, "links": links}))
    return path


def fill_context_store(store: Store, reader: GraphifyReader, files: int = 8) -> list[str]:
    filler = "lorem ipsum dolor sit amet " * 12
    ids = []
    for f in range(files):
        for i in range(0, 60, 2):
            out = server._add_decision_impl(
                store,
                reader,
                title=f"Decision {f}-{i} about worker",
                kind="gotcha" if i % 4 == 0 else "adr",
                context=filler,
                choice=filler,
                rejected=filler,
                anchors=[{"name": f"Worker{f}_{i}()", "file_path": f"src/pkg{f}/mod{f}.py"}],
            )
            ids.append(out["id"])
    return ids


@pytest.fixture(scope="module")
def ctx_template(tmp_path_factory):
    """(graph file, closed template store directory, file paths, decision ids)."""
    root = tmp_path_factory.mktemp("ctx-template")
    files = 8
    graph = build_context_graph(root / "ctx.json", files)
    reader = GraphifyReader(graph)
    store = Store(root / "t.db")
    try:
        ids = fill_context_store(store, reader, files)
    finally:
        store.close()
    paths = [f"src/pkg{f}/mod{f}.py" for f in range(files)]
    return graph, root / "t.db", paths, ids


@pytest.fixture
def ctx_env(ctx_template, tmp_path, monkeypatch):
    graph, template, paths, ids = ctx_template
    copy = tmp_path / "t.db"
    shutil.copytree(template, copy)
    store = Store(copy)
    reader = GraphifyReader(graph)
    monkeypatch.setattr(server, "_store", store)
    monkeypatch.setenv("SIDEGRAPH_GRAPH", str(tmp_path / "no-such-graph.json"))
    monkeypatch.setattr(server, "_synced_reader", lambda: (reader, None))
    yield store, reader, paths, ids
    store.close()


@pytest.fixture
def uncapped_related(monkeypatch):
    """The Related bucket without its line cap. The whole-text cap tests need a text over 40000
    characters with Related as a cuttable section; the field-shaped fixture reaches that only
    while every related record is rendered."""
    import sidegraph.retrieval as retrieval

    monkeypatch.setattr(retrieval, "_RELATED_LINE_CAP", 10**6)


def add_h2_records(store: Store, reader: GraphifyReader, heading: str, count: int = 60) -> None:
    for i in range(count):
        server._add_decision_impl(
            store,
            reader,
            title=f"H2 record {i:02d}",
            kind="gotcha",
            context=f"see below\n{heading}\n" + "y" * 1100,
            choice="ch",
            anchors=[{"name": "Worker0_1()", "file_path": "src/pkg0/mod0.py"}],
        )


def check_heading_line_records_are_cut_whole(
    ctx_env, monkeypatch, structure: int, heading: str
) -> None:
    """A line in a record's text that equals a section heading is record text: the nearest block
    is built with its own heading level, so no record body is ever rewritten."""
    store, reader, _, _ = ctx_env
    monkeypatch.setattr(server, "_session_key", lambda _s: "sess-1")
    add_h2_records(store, reader, heading)
    text = server._get_task_context_impl(
        store, reader, ["src/pkg0/empty.py"], None, structure, 40000
    )
    assert "[output cut at 40000 characters" in text
    assert "## Nearest anchored records" in text  # precondition
    pattern = r"\(context: see below\n" + re.escape(heading) + r"\n[^\n]*\) \(id: [0-9A-Z]{26}\)"
    whole = re.findall(pattern, text)
    titles = set(re.findall(r"H2 record \d\d", text))
    assert whole  # precondition: detailed records with the heading line reached the text
    assert text.count("(context: see below") == len(whole)  # none opens without its id line
    shown = {store.get_decision(i).title for i in store.retrieval_shows()}
    assert {t for t in shown if t.startswith("H2 record")} == titles
    journal = {
        store.get_decision(r["detail"]).title
        for r in store.retrieval_events()
        if r["kind"] == "show_anchor"
    }
    assert {t for t in journal if t.startswith("H2 record")} == titles
    [event] = store.render_events()
    assert event["emitted"] >= len(whole)
