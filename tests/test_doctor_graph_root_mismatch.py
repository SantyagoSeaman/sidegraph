"""Defect 2 wiring: sidegraph-doctor surfaces the subdir-run graph diagnostic
(GraphifyReader.detect_subdir_mismatch, see tests/test_reader_subdir_mismatch.py) as an
ordinary curation Finding, so the person who ran `graphify update` from a subdirectory
sees ONE actionable line instead of every anchor in the store looking mysteriously
orphaned.

Red target: test_finding_when_graph_paths_are_subdir_relative is red against unfixed code
(doctor.curate has no `reader` parameter and no graph-root-mismatch check at all).
"""

from __future__ import annotations

import json
from pathlib import Path

from sidegraph.doctor import GRAPH_ROOT_MISMATCH, curate
from sidegraph.engine.reader import GraphifyReader

ANCHORABLE_PATHS = [f"pkg/mod_{i}.py" for i in range(10)]


def _node(i: int, path: str) -> dict:
    return {
        "id": f"n{i}",
        "label": f"fn_{i}()",
        "norm_label": f"fn_{i}()",
        "file_type": "code",
        "source_file": path,
        "community": i % 3,
    }


def _write_graph(path: Path, paths: list[str]) -> Path:
    data = {
        "built_at_commit": "v1",
        "nodes": [_node(i, p) for i, p in enumerate(paths)],
        "links": [],
    }
    path.write_text(json.dumps(data))
    return path


def test_finding_when_graph_paths_are_subdir_relative(tmp_path):
    repo_root = tmp_path
    store_dir = repo_root / ".sidegraph"
    store_dir.mkdir()
    for p in ANCHORABLE_PATHS:
        real = repo_root / "src" / p
        real.parent.mkdir(parents=True, exist_ok=True)
        real.write_text("pass\n")
    graph_path = _write_graph(repo_root / "graph.json", ANCHORABLE_PATHS)
    reader = GraphifyReader(graph_path)

    report = curate(store_dir, reader=reader, repo_root=repo_root)

    codes = [f.code for f in report.findings]
    assert GRAPH_ROOT_MISMATCH in codes
    finding = next(f for f in report.findings if f.code == GRAPH_ROOT_MISMATCH)
    assert "src" in finding.detail


def test_no_finding_when_graph_paths_resolve_at_repo_root(tmp_path):
    repo_root = tmp_path
    store_dir = repo_root / ".sidegraph"
    store_dir.mkdir()
    for p in ANCHORABLE_PATHS:
        real = repo_root / p
        real.parent.mkdir(parents=True, exist_ok=True)
        real.write_text("pass\n")
    graph_path = _write_graph(repo_root / "graph.json", ANCHORABLE_PATHS)
    reader = GraphifyReader(graph_path)

    report = curate(store_dir, reader=reader, repo_root=repo_root)

    assert GRAPH_ROOT_MISMATCH not in [f.code for f in report.findings]


def test_no_finding_when_reader_is_none(tmp_path):
    store_dir = tmp_path / ".sidegraph"
    store_dir.mkdir()

    report = curate(store_dir, reader=None)

    assert GRAPH_ROOT_MISMATCH not in [f.code for f in report.findings]
