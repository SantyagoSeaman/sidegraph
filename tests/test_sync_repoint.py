import json

from sidegraph.engine.reader import GraphifyReader
from sidegraph.schema import AnchorBinding, Descriptor, Entity
from sidegraph.store import Store
from sidegraph.sync import rebind_entity

GRAPH_RENUMBERED = {
    "built_at_commit": "vC",
    "nodes": [
        # same node id/file as capture time, but Leiden renumbered 1 -> 7 (the dogfood case)
        {
            "id": "s1",
            "label": "f_stable()",
            "norm_label": "f_stable()",
            "file_type": "code",
            "source_file": "a.py",
            "community": 7,
        },
    ],
    "links": [],
}


def _reader(tmp_path, data):
    p = tmp_path / "g.json"
    p.write_text(json.dumps(data))
    return GraphifyReader(p)


def _setup(tmp_path, baseline="1"):
    """Entity anchored at capture time in community <baseline> with a live Tier-1 binding."""
    from datetime import UTC, datetime

    from sidegraph.schema import Decision, DecisionKind, Provenance

    s = Store(tmp_path / "t.db")
    e = s.upsert_entity(
        Entity(
            canonical_name="f_stable",
            descriptor=Descriptor(name="f_stable", file_path="a.py"),
            last_seen_node_id="s1",
            last_seen_graph_version="vA",
            last_seen_community=baseline,
        )
    )
    d = s.add_decision(
        Decision(
            title="stable rule",
            kind=DecisionKind.GOTCHA,
            context="c",
            choice="ch",
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual"),
        )
    )
    s.add_binding(AnchorBinding(record_id=d.id, entity_id=e.entity_id, tier=2, status="live"))
    if baseline is not None:
        comm = s.get_or_create_abstract_entity(f"community:{baseline}")
        s.add_binding(
            AnchorBinding(record_id=d.id, entity_id=comm.entity_id, tier=1, status="live")
        )
    return s, e, d


def test_renumbered_community_repoints_binding(tmp_path):
    s, e, d = _setup(tmp_path, baseline="1")
    out = rebind_entity(e, s, _reader(tmp_path, GRAPH_RENUMBERED))
    assert out.status == "unchanged"  # same node id — still re-points
    assert out.repointed == 1
    by_entity = {
        s.get_entity(b.entity_id).canonical_name: b
        for b in s.bindings_for_record(d.id)
        if b.tier == 1
    }
    assert by_entity["community:7"].status == "live"
    assert by_entity["community:1"].status == "orphaned"
    assert s.get_entity(e.entity_id).last_seen_community == "7"


def test_backfill_none_baseline_adds_without_flipping(tmp_path):
    s, e, d = _setup(tmp_path, baseline=None)
    e.last_seen_community = None
    s.upsert_entity(e)
    out = rebind_entity(e, s, _reader(tmp_path, GRAPH_RENUMBERED))
    assert out.repointed == 1
    tier1 = [b for b in s.bindings_for_record(d.id) if b.tier == 1]
    assert len(tier1) == 1  # only the new one; nothing to flip
    assert s.get_entity(tier1[0].entity_id).canonical_name == "community:7"
    assert tier1[0].status == "live"


def test_same_community_is_noop(tmp_path):
    s, e, d = _setup(tmp_path, baseline="7")  # baseline already matches the graph
    out = rebind_entity(e, s, _reader(tmp_path, GRAPH_RENUMBERED))
    assert out.repointed == 0
    tier1 = [b for b in s.bindings_for_record(d.id) if b.tier == 1]
    assert len(tier1) == 1 and tier1[0].status == "live"


def test_orphaned_entity_not_repointed(tmp_path):
    s, e, d = _setup(tmp_path, baseline="1")
    gone = {"built_at_commit": "vC", "nodes": [], "links": []}
    out = rebind_entity(e, s, _reader(tmp_path, gone))
    assert out.status == "orphaned" and out.repointed == 0
    by_entity = {
        s.get_entity(b.entity_id).canonical_name: b
        for b in s.bindings_for_record(d.id)
        if b.tier == 1
    }
    assert by_entity["community:1"].status == "live"  # untouched — never guess


GRAPH_RENAMED_AND_RENUMBERED = {
    "built_at_commit": "vD",
    "nodes": [
        # f_stable was RENAMED AWAY, but its file survives; the whole cluster was
        # renumbered 1 -> 9 in the same rebuild (the dogfood-verified case).
        {
            "id": "s2",
            "label": "f_renamed()",
            "norm_label": "f_renamed()",
            "file_type": "code",
            "source_file": "a.py",
            "community": 9,
        },
        {
            "id": "h1",
            "label": "helper()",
            "norm_label": "helper()",
            "file_type": "code",
            "source_file": "a.py",
            "community": 9,
        },
    ],
    "links": [],
}


def test_orphan_with_surviving_file_repoints_via_file_community(tmp_path):
    s, e, d = _setup(tmp_path, baseline="1")
    out = rebind_entity(e, s, _reader(tmp_path, GRAPH_RENAMED_AND_RENUMBERED))
    assert out.status == "orphaned"
    assert out.repointed == 1
    by_entity = {
        s.get_entity(b.entity_id).canonical_name: b
        for b in s.bindings_for_record(d.id)
        if b.tier == 1
    }
    assert by_entity["community:9"].status == "live"
    assert by_entity["community:1"].status == "orphaned"
    kept = s.get_entity(e.entity_id)
    assert kept.last_seen_community == "9"
    assert kept.last_seen_node_id == "s1"  # mapping untouched — never guess


def test_orphan_with_file_gone_does_not_repoint(tmp_path):
    s, e, d = _setup(tmp_path, baseline="1")
    gone = {"built_at_commit": "vD", "nodes": [], "links": []}
    out = rebind_entity(e, s, _reader(tmp_path, gone))
    assert out.status == "orphaned" and out.repointed == 0
    by_entity = {
        s.get_entity(b.entity_id).canonical_name: b
        for b in s.bindings_for_record(d.id)
        if b.tier == 1
    }
    assert by_entity["community:1"].status == "live"  # true residual — untouched


def test_ambiguous_with_shared_community_repoints(tmp_path):
    s, e, d = _setup(tmp_path, baseline="1")
    dup = {
        "built_at_commit": "vD",
        "nodes": [
            {
                "id": "x1",
                "label": "f_stable()",
                "norm_label": "f_stable()",
                "file_type": "code",
                "source_file": "m.py",
                "community": 9,
            },
            {
                "id": "x2",
                "label": "f_stable()",
                "norm_label": "f_stable()",
                "file_type": "code",
                "source_file": "n.py",
                "community": 9,
            },
        ],
        "links": [],
    }
    e.descriptor.file_path = None  # force ambiguity (two name hits)
    s.upsert_entity(e)
    out = rebind_entity(e, s, _reader(tmp_path, dup))
    assert out.status == "ambiguous" and out.repointed == 1
    by_entity = {
        s.get_entity(b.entity_id).canonical_name: b
        for b in s.bindings_for_record(d.id)
        if b.tier == 1
    }
    assert by_entity["community:9"].status == "live"
    assert by_entity["community:1"].status == "orphaned"
