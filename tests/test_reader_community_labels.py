"""``GraphifyReader.community_labels()`` — the engine-seam-only sidecar reader (see
docs/concepts/mind-model.md#domain-lifecycle)."""

import json

import pytest

from sidegraph.domains import collect_domain_candidates
from sidegraph.engine.reader import GraphifyReader
from sidegraph.store import Store
from tests.graph_fixtures import node

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
    _write(sub, ".graphify_labels.json", {"0": "Alpha Domain", "1": "Beta Domain"})
    # A reader opened via a relative-looking nested path still resolves the sidecar
    # from graph.json's own parent directory, not the cwd.
    reader = GraphifyReader(graph)
    assert reader.community_labels() == {"0": "Alpha Domain", "1": "Beta Domain"}


def test_only_the_pending_sidecar_yields_its_labels(tmp_path):
    # red against unfixed code: {}
    graph = _write(tmp_path, "graph.json", _GRAPH)
    _write(tmp_path, ".graphify_labels.pending.json", {"1": "Alpha"})
    assert GraphifyReader(graph).community_labels() == {"1": "Alpha"}


def test_curated_labels_win_then_pending_then_node_names_and_placeholders_are_skipped(tmp_path):
    # red against unfixed code: {"1": "Curated", "2": "Community 2"}
    nodes = [
        {**_GRAPH["nodes"][0], "id": "n3", "community": 3, "community_name": "From node"},
        {**_GRAPH["nodes"][0], "id": "n1", "community": 1, "community_name": "Ignored"},
        {**_GRAPH["nodes"][0], "id": "n2", "community": 2, "community_name": "Behind pending"},
        {**_GRAPH["nodes"][0], "id": "n4", "community": 4, "community_name": "Community 4"},
    ]
    graph = _write(tmp_path, "graph.json", {**_GRAPH, "nodes": nodes})
    _write(tmp_path, ".graphify_labels.json", {"1": "Curated", "2": "Community 2"})
    _write(tmp_path, ".graphify_labels.pending.json", {"1": "Hub", "2": "Hub two"})
    assert GraphifyReader(graph).community_labels() == {
        "1": "Curated",
        "2": "Hub two",
        "3": "From node",
    }


def test_a_hub_name_in_path_suffix_form_comes_back_as_the_basename(tmp_path):
    # red against unfixed code: {}; red against a merge without the hub fold (declared)
    nodes = [
        node("boot", "bootstrap/cli.py", "src/sidegraph/bootstrap/cli.py", community=4),
        node("cli", "sidegraph/cli.py", "src/sidegraph/cli.py", community=5),
    ]
    graph = _write(tmp_path, "graph.json", {**_GRAPH, "nodes": nodes})
    _write(tmp_path, ".graphify_labels.pending.json", {"4": "bootstrap/cli.py", "5": "docs/notes"})
    assert GraphifyReader(graph).community_labels() == {"4": "cli.py", "5": "docs/notes"}


@pytest.mark.parametrize("source", ["curated", "node"])  # pending: the path-suffix test above
def test_hub_aliases_are_normalised_for_each_label_source(tmp_path, source):
    nodes = [
        node("boot", "bootstrap/cli.py", "src/sidegraph/bootstrap/cli.py", community=4),
        node("cli", "sidegraph/cli.py", "src/sidegraph/cli.py", community=5),
    ]
    if source == "node":
        nodes[0]["community_name"] = "bootstrap/cli.py"
    else:
        _write(tmp_path, ".graphify_labels.json", {"4": "bootstrap/cli.py"})
    graph = _write(tmp_path, "graph.json", {**_GRAPH, "nodes": nodes})
    assert GraphifyReader(graph).community_labels() == {"4": "cli.py"}


def test_a_hub_alias_from_another_community_does_not_bypass_the_domain_title_guard(tmp_path):
    graph = _write(
        tmp_path,
        "graph.json",
        {
            "nodes": [
                node("boot", "bootstrap/cli.py", "src/sidegraph/bootstrap/cli.py", community=4),
                node("cli", "sidegraph/cli.py", "src/sidegraph/cli.py", community=5),
                node("readme", "readme()", "src/readme.py", community=5),
            ],
            "links": [
                {"source": "readme", "target": "cli", "relation": "calls"},
                {"source": "readme", "target": "readme", "relation": "calls"},
            ],
        },
    )
    _write(
        tmp_path,
        ".graphify_labels.pending.json",
        {"4": "bootstrap/cli.py", "5": "bootstrap/cli.py"},
    )
    reader = GraphifyReader(graph)
    assert reader.community_labels() == {"4": "cli.py", "5": "bootstrap/cli.py"}
    with Store(tmp_path / "store") as store:
        candidates, _ = collect_domain_candidates(store, reader, min_members=1)
    assert next(c.suggested_title for c in candidates if c.community_id == "5") == "readme()"


@pytest.mark.parametrize("curated", ["{broken", ["not an object"], {"1": "Community 1"}])
def test_unusable_curated_labels_do_not_hide_valid_pending_labels(tmp_path, curated):
    graph = _write(tmp_path, "graph.json", _GRAPH)
    _write(tmp_path, ".graphify_labels.json", curated)
    _write(tmp_path, ".graphify_labels.pending.json", {"1": "Alpha"})
    assert GraphifyReader(graph).community_labels() == {"1": "Alpha"}
