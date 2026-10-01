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


# -- shared Tier-1 community rows: a record with two leaves in one community ------------------
#
# Tier-1 ``community:*`` rows are keyed by (record_id, entity_id), so two leaves of one
# decision in the same community share ONE row. A leaf leaving must not orphan a row a
# sibling leaf still holds.


def _node(node_id: str, label: str, source_file: str, community: int | None) -> dict:
    node = {
        "id": node_id,
        "label": f"{label}()",
        "norm_label": f"{label}()",
        "file_type": "code",
        "source_file": source_file,
    }
    if community is not None:
        node["community"] = community
    return node


def _graph(*nodes: dict) -> dict:
    return {"built_at_commit": "vC", "nodes": list(nodes), "links": []}


def _setup_two_leaves(tmp_path, relation="affects"):
    """One decision, two leaves f1 (a.py) and f2 (b.py), both baselined in community 5
    and sharing the one live ``community:5`` Tier-1 row. f1 is created before f2."""
    from datetime import UTC, datetime

    from sidegraph.schema import Decision, DecisionKind, Provenance

    s = Store(tmp_path / "t.db")
    leaves = []
    for name, path, node in (("f1", "a.py", "s1"), ("f2", "b.py", "s2")):
        leaves.append(
            s.upsert_entity(
                Entity(
                    canonical_name=name,
                    descriptor=Descriptor(name=name, file_path=path),
                    last_seen_node_id=node,
                    last_seen_graph_version="vA",
                    last_seen_community="5",
                )
            )
        )
    d = s.add_decision(
        Decision(
            title="shared rule",
            kind=DecisionKind.GOTCHA,
            context="c",
            choice="ch",
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual"),
        )
    )
    for e in leaves:
        s.add_binding(
            AnchorBinding(
                record_id=d.id, entity_id=e.entity_id, tier=2, status="live", relation=relation
            )
        )
    comm = s.get_or_create_abstract_entity("community:5")
    s.add_binding(
        AnchorBinding(
            record_id=d.id, entity_id=comm.entity_id, tier=1, status="live", relation=relation
        )
    )
    return s, leaves[0], leaves[1], d


def _tier1(s, d):
    return {
        s.get_entity(b.entity_id).canonical_name: b
        for b in s.bindings_for_record(d.id)
        if b.tier == 1
    }


def _reachable(s, name):
    ent = s.find_abstract_entity(name)
    return ent is not None and bool(s.valid_decisions_for_entity(ent.entity_id))


def test_shared_community_stays_live_when_one_leaf_leaves_via_sync(tmp_path):
    from sidegraph.sync import sync

    s, e1, e2, d = _setup_two_leaves(tmp_path)
    g = _graph(_node("s1", "f1", "a.py", 7), _node("s2", "f2", "b.py", 5))
    sync(s, _reader(tmp_path, g))
    t1 = _tier1(s, d)
    assert t1["community:5"].status == "live"
    assert t1["community:7"].status == "live"
    assert _reachable(s, "community:5")


def test_shared_community_stays_live_when_one_leaf_leaves_via_rebind(tmp_path):
    s, e1, e2, d = _setup_two_leaves(tmp_path)
    r = _reader(tmp_path, _graph(_node("s1", "f1", "a.py", 7), _node("s2", "f2", "b.py", 5)))
    rebind_entity(e1, s, r)
    rebind_entity(e2, s, r)
    t1 = _tier1(s, d)
    assert t1["community:5"].status == "live"
    assert t1["community:7"].status == "live"
    assert _reachable(s, "community:5")


def test_community_orphaned_when_every_leaf_leaves(tmp_path):
    s, e1, e2, d = _setup_two_leaves(tmp_path)
    r = _reader(tmp_path, _graph(_node("s1", "f1", "a.py", 7), _node("s2", "f2", "b.py", 7)))
    rebind_entity(e1, s, r)
    rebind_entity(e2, s, r)
    t1 = _tier1(s, d)
    assert t1["community:5"].status == "orphaned"
    assert t1["community:7"].status == "live"


def test_community_orphaned_when_the_staying_leaf_is_orphaned_in_the_same_pass(tmp_path):
    """f1 leaves 5 for 7; f2's symbol vanishes (its file has no nodes, so no observable
    community). Only a reconcile AFTER the whole ladder sees f2 orphaned: reconciling per
    entity would see f2 still live in 5 when f1 leaves, and keep the row."""
    from sidegraph.sync import sync

    s, e1, e2, d = _setup_two_leaves(tmp_path)
    assert [e.canonical_name for e in s.iter_concrete_entities()] == ["f1", "f2"]
    sync(s, _reader(tmp_path, _graph(_node("s1", "f1", "a.py", 7))))
    t1 = _tier1(s, d)
    assert t1["community:5"].status == "orphaned"
    assert t1["community:7"].status == "live"


def test_new_community_row_carries_the_leaf_relation(tmp_path):
    s, e1, e2, d = _setup_two_leaves(tmp_path, relation="modifies")
    r = _reader(tmp_path, _graph(_node("s1", "f1", "a.py", 7), _node("s2", "f2", "b.py", 5)))
    rebind_entity(e1, s, r)
    assert _tier1(s, d)["community:7"].relation == "modifies"


def test_orphaned_community_row_restored_when_a_live_leaf_holds_it(tmp_path):
    s, e1, e2, d = _setup_two_leaves(tmp_path)
    row = _tier1(s, d)["community:5"]
    row.status = "orphaned"
    s.add_binding(row)
    r = _reader(tmp_path, _graph(_node("s1", "f1", "a.py", 7), _node("s2", "f2", "b.py", 5)))
    rebind_entity(e1, s, r)
    assert _tier1(s, d)["community:5"].status == "live"


def test_vacated_community_kept_when_a_live_sibling_has_no_baseline(tmp_path):
    """A live leaf with no recorded community could be anywhere, so the record's
    communities cannot be told apart and nothing is orphaned."""
    s, e1, e2, d = _setup_two_leaves(tmp_path)
    e2.last_seen_community = None
    s.upsert_entity(e2)
    r = _reader(tmp_path, _graph(_node("s1", "f1", "a.py", 7), _node("s2", "f2", "b.py", 5)))
    rebind_entity(e1, s, r)
    assert _tier1(s, d)["community:5"].status == "live"


def test_ambiguous_anchor_tier1_row_survives_a_sibling_renumber(tmp_path):
    """An ambiguous anchor leaves a Tier-1-only ``degraded`` row (anchoring.py) with no
    leaf behind it. It was not vacated by anything in this pass, so it is never touched."""
    s, e1, e2, d = _setup_two_leaves(tmp_path)
    amb = s.get_or_create_abstract_entity("community:9")
    s.add_binding(AnchorBinding(record_id=d.id, entity_id=amb.entity_id, tier=1, status="degraded"))
    r = _reader(tmp_path, _graph(_node("s1", "f1", "a.py", 7), _node("s2", "f2", "b.py", 5)))
    rebind_entity(e1, s, r)
    rebind_entity(e2, s, r)
    assert _tier1(s, d)["community:9"].status == "degraded"
