from datetime import UTC, datetime

from sidegraph.schema import (
    AnchorBinding,
    Decision,
    DecisionKind,
    DecisionStatus,
    Descriptor,
    Entity,
    Fact,
    Provenance,
)
from sidegraph.store import Store
from sidegraph.viz.model import build_graph


def _dec(store, title, kind=DecisionKind.ADR, status=DecisionStatus.ACCEPTED, supersedes=None):
    d = Decision(
        title=title,
        kind=kind,
        status=status,
        context="ctx",
        choice="ch",
        valid_from=datetime.now(UTC),
        supersedes=supersedes,
        provenance=Provenance(source="manual"),
    )
    return store.add_decision(d)


def _fact(store, statement, supports):
    f = Fact(
        statement=statement,
        source="src",
        supports=supports,
        valid_from=datetime.now(UTC),
        provenance=Provenance(source="manual"),
    )
    return store.add_fact(f)


def _store_with_graph(tmp_path):
    s = Store(tmp_path / "t.db")
    e_live = s.upsert_entity(
        Entity(canonical_name="alpha", descriptor=Descriptor(name="alpha", file_path="a.py"))
    )
    e_bad = s.upsert_entity(
        Entity(canonical_name="beta", descriptor=Descriptor(name="beta", file_path="b.py"))
    )
    d = _dec(s, "an adr", kind=DecisionKind.ADR)
    g = _dec(s, "a gotcha", kind=DecisionKind.GOTCHA)
    f = _fact(s, "a fact", supports=[d.id])
    # d: one live anchor; g: one degraded anchor; f: one live anchor
    s.add_binding(AnchorBinding(record_id=d.id, entity_id=e_live.entity_id, tier=2, status="live"))
    s.add_binding(
        AnchorBinding(record_id=g.id, entity_id=e_bad.entity_id, tier=2, status="degraded")
    )
    s.add_binding(AnchorBinding(record_id=f.id, entity_id=e_live.entity_id, tier=2, status="live"))
    return s, {"d": d, "g": g, "f": f, "e_live": e_live, "e_bad": e_bad}


def test_build_graph_assembles_nodes_edges_and_stats(tmp_path):
    s, r = _store_with_graph(tmp_path)
    graph = build_graph(s)
    ids = {n.id for n in graph.nodes}
    assert r["d"].id in ids and r["g"].id in ids and r["f"].id in ids
    assert r["e_live"].entity_id in ids and r["e_bad"].entity_id in ids
    kinds = {n.id: n.type for n in graph.nodes}
    assert kinds[r["d"].id] == "decision"
    assert kinds[r["f"].id] == "fact"
    assert kinds[r["e_live"].entity_id] == "entity"
    # a supports edge fact -> decision
    assert any(
        e.kind == "supports" and e.source == r["f"].id and e.target == r["d"].id
        for e in graph.edges
    )
    # an anchor edge with degraded status
    assert any(e.kind == "anchor" and e.status == "degraded" for e in graph.edges)
    assert graph.stats.decisions == 2
    assert graph.stats.facts == 1
    assert graph.stats.entities == 2
    assert graph.stats.degraded_bindings == 1


def test_dangling_record_flagged_when_no_binding(tmp_path):
    s, r = _store_with_graph(tmp_path)
    # add a decision with NO binding at all -> dangling
    lonely = _dec(s, "unanchored")
    graph = build_graph(s)
    node = next(n for n in graph.nodes if n.id == lonely.id)
    assert node.dangling is True
    assert node.problem is True
    # the degraded-anchored gotcha is also a problem, but not dangling (it has a binding)
    gnode = next(n for n in graph.nodes if n.id == r["g"].id)
    assert gnode.dangling is False
    assert gnode.problem is True
    assert graph.stats.dangling_records >= 1


def test_only_problems_drops_healthy_subgraph(tmp_path):
    s, r = _store_with_graph(tmp_path)
    graph = build_graph(s, only_problems=True)
    ids = {n.id for n in graph.nodes}
    # the degraded gotcha and its entity survive; the fully-live adr's entity does not
    assert r["g"].id in ids and r["e_bad"].entity_id in ids
    assert r["d"].id not in ids


def test_supersedes_edge_and_hide_superseded(tmp_path):
    s = Store(tmp_path / "t.db")
    old = _dec(s, "old choice")
    new = _dec(s, "new choice", supersedes=old.id)  # add_decision closes `old` -> superseded
    graph = build_graph(s)
    assert any(
        e.kind == "supersedes" and e.source == new.id and e.target == old.id for e in graph.edges
    )
    hidden = build_graph(s, include_superseded=False)
    assert old.id not in {n.id for n in hidden.nodes}


def test_max_nodes_truncates_and_records(tmp_path):
    s = Store(tmp_path / "t.db")
    for i in range(6):
        _dec(s, f"adr {i}")
    graph = build_graph(s, max_nodes=3)
    assert len(graph.nodes) == 3
    assert graph.stats.truncated == 3
