import json

import pytest

from sidegraph.engine.reader import GraphifyReader

MISSING = object()  # a graph.json without a built_at_commit key

NODE = {
    "id": "d",
    "label": "Context",
    "norm_label": "context",
    "file_type": "document",
    "source_file": "ADR-001.md",
    "community": 3,
}


def _write(tmp_path, name, data):
    p = tmp_path / name
    p.write_text(json.dumps(data))
    return p


def test_combines_commit_and_content_hash(tmp_path):
    p = _write(tmp_path, "g.json", {"built_at_commit": "abc123", "nodes": [], "links": []})
    v = GraphifyReader(p).graph_version()
    assert v.startswith("abc123:")
    assert len(v) == len("abc123:") + 12


@pytest.mark.parametrize("commit", [MISSING, None, ""])
def test_fallback_when_no_commit_is_recorded(tmp_path, commit):
    data = {"nodes": [NODE], "links": []}
    if commit is not MISSING:
        data["built_at_commit"] = commit
    p = _write(tmp_path, "g.json", data)
    v = GraphifyReader(p).graph_version()
    assert v.startswith("content:")
    assert len(v) == len("content:") + 12


@pytest.mark.parametrize("commit", [MISSING, "abc123"])
def test_a_version_is_stable_across_loads_of_one_file(tmp_path, commit):
    data = {"nodes": [NODE], "links": []}
    if commit is not MISSING:
        data["built_at_commit"] = commit
    p = _write(tmp_path, "g.json", data)
    assert GraphifyReader(p).graph_version() == GraphifyReader(p).graph_version()


def test_fallback_changes_when_content_changes(tmp_path):
    p = _write(tmp_path, "g.json", {"nodes": [NODE], "links": []})
    v1 = GraphifyReader(p).graph_version()
    p.write_text(json.dumps({"nodes": [dict(NODE, community=9)], "links": []}))
    assert GraphifyReader(p).graph_version() != v1


def test_same_commit_changed_content_changes_version(tmp_path):
    """The dirty-tree case: Graphify rewrites graph.json without bumping built_at_commit
    (e.g. an uncommitted rename triggers a rebuild). A bare commit would report "unchanged"
    and leave sync silently serving stale anchors — folding in the content hash catches it.
    """
    p = _write(tmp_path, "g.json", {"built_at_commit": "abc123", "nodes": [NODE], "links": []})
    v1 = GraphifyReader(p).graph_version()
    p.write_text(
        json.dumps({"built_at_commit": "abc123", "nodes": [dict(NODE, community=9)], "links": []})
    )
    v2 = GraphifyReader(p).graph_version()
    assert v2 != v1
