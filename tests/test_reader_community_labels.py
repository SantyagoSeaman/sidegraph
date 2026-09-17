"""``GraphifyReader.community_labels()`` — the engine-seam-only sidecar reader (see
docs/concepts/mind-model.md#domain-lifecycle)."""

import json

from sidegraph.engine.reader import GraphifyReader

_GRAPH = {
    "built_at_commit": "v1",
    "nodes": [
        {
            "id": "n1",
            "label": "Thing",
            "norm_label": "thing",
            "file_type": "code",
            "source_file": "a.py",
            "community": 1,
        },
    ],
    "links": [],
}


def _write(tmp_path, name, data):
    p = tmp_path / name
    p.write_text(data if isinstance(data, str) else json.dumps(data))
    return p


def test_community_labels_missing_sidecar_returns_empty_dict(tmp_path):
    graph = _write(tmp_path, "graph.json", _GRAPH)
    reader = GraphifyReader(graph)
    assert reader.community_labels() == {}


def test_community_labels_reads_sidecar_next_to_graph(tmp_path):
    graph = _write(tmp_path, "graph.json", _GRAPH)
    _write(tmp_path, ".graphify_labels.json", {"0": "Alpha Domain", "1": "Beta Domain"})
    reader = GraphifyReader(graph)
    assert reader.community_labels() == {"0": "Alpha Domain", "1": "Beta Domain"}


def test_community_labels_malformed_json_returns_empty_dict(tmp_path):
    graph = _write(tmp_path, "graph.json", _GRAPH)
    _write(tmp_path, ".graphify_labels.json", "{not valid json")
    reader = GraphifyReader(graph)
    assert reader.community_labels() == {}


def test_community_labels_non_dict_json_returns_empty_dict(tmp_path):
    graph = _write(tmp_path, "graph.json", _GRAPH)
    _write(tmp_path, ".graphify_labels.json", ["not", "a", "dict"])
    reader = GraphifyReader(graph)
    assert reader.community_labels() == {}


def test_community_labels_drops_non_string_values(tmp_path):
    graph = _write(tmp_path, "graph.json", _GRAPH)
    _write(tmp_path, ".graphify_labels.json", {"0": "Alpha", "1": 42, "2": None})
    reader = GraphifyReader(graph)
    assert reader.community_labels() == {"0": "Alpha"}


def test_community_labels_drops_blank_after_strip_values(tmp_path):
    """A whitespace-only or empty label is worthless as a title source and, left in,
    crashes ``bootstrap_domains`` downstream (``Domain.title`` rejects blank text) — drop
    it at the seam so every caller of ``community_labels()`` gets a real fallback instead."""
    graph = _write(tmp_path, "graph.json", _GRAPH)
    _write(tmp_path, ".graphify_labels.json", {"0": "Alpha", "1": "   ", "2": ""})
    reader = GraphifyReader(graph)
    assert reader.community_labels() == {"0": "Alpha"}


def test_community_labels_looked_up_relative_to_graph_directory(tmp_path):
    sub = tmp_path / "graphify-out"
    sub.mkdir()
    graph = _write(sub, "graph.json", _GRAPH)
    _write(sub, ".graphify_labels.json", {"0": "Nested"})
    # A reader opened via a relative-looking nested path still resolves the sidecar
    # from graph.json's own parent directory, not the cwd.
    reader = GraphifyReader(graph)
    assert reader.community_labels() == {"0": "Nested"}
