import json

from sidegraph.engine.reader import GraphifyReader

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


def test_fallback_when_key_missing(tmp_path):
    p = _write(tmp_path, "g.json", {"nodes": [NODE], "links": []})
    v = GraphifyReader(p).graph_version()
    assert v.startswith("content:")
    assert len(v) == len("content:") + 12


def test_fallback_when_null(tmp_path):
    p = _write(tmp_path, "g.json", {"built_at_commit": None, "nodes": [], "links": []})
    assert GraphifyReader(p).graph_version().startswith("content:")


def test_fallback_when_empty_string(tmp_path):
    p = _write(tmp_path, "g.json", {"built_at_commit": "", "nodes": [], "links": []})
    assert GraphifyReader(p).graph_version().startswith("content:")


def test_fallback_stable_across_loads(tmp_path):
    p = _write(tmp_path, "g.json", {"nodes": [NODE], "links": []})
    assert GraphifyReader(p).graph_version() == GraphifyReader(p).graph_version()


def test_fallback_changes_when_content_changes(tmp_path):
    p = _write(tmp_path, "g.json", {"nodes": [NODE], "links": []})
    v1 = GraphifyReader(p).graph_version()
    p.write_text(json.dumps({"nodes": [dict(NODE, community=9)], "links": []}))
    assert GraphifyReader(p).graph_version() != v1


def test_same_commit_same_content_is_stable(tmp_path):
    data = {"built_at_commit": "abc123", "nodes": [NODE], "links": []}
    p = _write(tmp_path, "g.json", data)
    assert GraphifyReader(p).graph_version() == GraphifyReader(p).graph_version()


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
