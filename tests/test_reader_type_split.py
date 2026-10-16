"""A community that holds code and documents is two (type-split, intended change 2d).

see design/superpowers/specs/2026-10-14-graphify-upgrade-design.md (D5.3)"""

from __future__ import annotations

import json

from sidegraph.engine.reader import DOC_PART_SUFFIX, RESOLVER_REVISION, GraphifyReader
from sidegraph.schema import Descriptor


def _node(nid, label, file_type, source, community):
    return {
        "id": nid,
        "label": label,
        "norm_label": label.lower(),
        "file_type": file_type,
        "source_file": source,
        "community": community,
    }


def _reader(tmp_path, nodes):
    path = tmp_path / "graph.json"
    path.write_text(json.dumps({"nodes": nodes, "links": []}), encoding="utf-8")
    return GraphifyReader(path)


MIXED = [
    _node("util", "util.py", "code", "src/util.py", 7),
    _node("helper", "helper()", "code", "src/util.py", 7),
    _node("why", "Helpers stay pure.", "rationale", "src/util.py", 7),
    _node("guide", "guide.md", "document", "docs/guide.md", 7),
    _node("guide_h", "Using helper", "document", "docs/guide.md", 7),
    _node("pic", "arch.png", "image", "docs/arch.png", 7),
    _node("other", "other.py", "code", "src/other.py", 8),
]
DOC = f"7{DOC_PART_SUFFIX}"


def test_a_community_of_code_and_documents_is_split_in_two(tmp_path):
    """Red against the unsplit reader: one community 7 holding six nodes."""
    reader = _reader(tmp_path, MIXED)
    assert {n.node_id: n.community for n in reader.list_nodes()} == {
        "util": "7",
        "helper": "7",
        "why": "7",
        "guide": DOC,
        "guide_h": DOC,
        "pic": DOC,
        "other": "8",
    }
    assert {c.community_id: sorted(c.members) for c in reader.communities()} == {
        "7": ["helper", "util", "why"],
        DOC: ["guide", "guide_h", "pic"],
        "8": ["other"],
    }


def test_resolve_reports_the_document_part(tmp_path):
    """Red against the unsplit reader ('7'). Sync stores this answer as last_seen_community."""
    result = _reader(tmp_path, MIXED).resolve(
        Descriptor(name="Using helper", file_path="docs/guide.md")
    )
    assert (result.status, result.community) == ("resolved", DOC)


def test_a_graph_without_a_mixed_community_keeps_every_id(tmp_path):
    """Guard (red against nothing): every 0.9.6 graph has 0 mixed communities
    (domain-measure.md), so the split is an identity there."""
    nodes = [n for n in MIXED if n["file_type"] not in ("document", "image")]
    nodes.append(_node("guide", "guide.md", "document", "docs/guide.md", 9))
    reader = _reader(tmp_path, nodes)
    assert {n.node_id: n.community for n in reader.list_nodes()} == {
        n["id"]: str(n["community"]) for n in nodes
    }


def test_documents_and_concepts_alone_are_not_split(tmp_path):
    """Guard: semantic-docs corpora cluster concept nodes with their documents on 0.9.6. Red
    against a split on any document/non-document mix (a discarded design, declared)."""
    nodes = [
        _node("budget", "Retry budget", "concept", "docs/a.md", 4),
        _node("a", "a.md", "document", "docs/a.md", 4),
        _node("a_h", "Budgets", "document", "docs/a.md", 4),
    ]
    assert {n.community for n in _reader(tmp_path, nodes).list_nodes()} == {"4"}


def test_the_split_reruns_the_first_sync_after_the_upgrade(tmp_path):
    """Red against RESOLVER_REVISION 3: a store synced under it would keep unsplit communities
    and an empty membership index until its graph changed."""
    assert RESOLVER_REVISION == 4
    assert _reader(tmp_path, MIXED).sync_stamp().endswith(":r4")
