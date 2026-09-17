from datetime import UTC, datetime

from sidegraph.anchoring import resolve_and_bind
from sidegraph.engine.reader import ResolveResult
from sidegraph.schema import (
    Decision,
    DecisionKind,
    Descriptor,
    Domain,
    Provenance,
)
from sidegraph.store import Store


class FakeReader:
    def __init__(self, result):
        self._result = result

    def resolve(self, desc):
        return self._result

    def graph_version(self):
        return "testv1"


def _decision(store):
    return store.add_decision(
        Decision(
            title="t",
            kind=DecisionKind.ADR,
            context="c",
            choice="ch",
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual"),
        )
    )


def test_resolved_creates_leaf_and_community_bindings(tmp_path):
    s = Store(tmp_path / "t.db")
    d = _decision(s)
    reader = FakeReader(ResolveResult(status="resolved", node_id="n1", community="18"))
    bindings = resolve_and_bind(
        d.id, Descriptor(name="BitfinexAdapter", file_path="adapters/bitfinex.py"), reader, s
    )
    tiers = {b.tier: b for b in bindings}
    assert set(tiers) == {2, 1}
    assert tiers[2].status == "live"
    assert tiers[1].status == "live"
    # community entity is abstract and named community:18
    comm = s.get_entity(tiers[1].entity_id)
    assert comm.canonical_name == "community:18"
    # leaf entity's engine mapping is refreshed on resolve
    leaf = s.get_entity(tiers[2].entity_id)
    assert leaf.last_seen_node_id == "n1"
    assert leaf.last_seen_graph_version == "testv1"


def test_dedup_reuses_entity_across_decisions(tmp_path):
    s = Store(tmp_path / "t.db")
    reader = FakeReader(ResolveResult(status="resolved", node_id="n1", community="18"))
    ref = Descriptor(name="BitfinexAdapter", file_path="adapters/bitfinex.py")
    b1 = resolve_and_bind(_decision(s).id, ref, reader, s)
    b2 = resolve_and_bind(_decision(s).id, ref, reader, s)
    leaf1 = next(b for b in b1 if b.tier == 2)
    leaf2 = next(b for b in b2 if b.tier == 2)
    assert leaf1.entity_id == leaf2.entity_id  # same durable entity reused


def test_ambiguous_degrades_to_community_only(tmp_path):
    s = Store(tmp_path / "t.db")
    d = _decision(s)
    reader = FakeReader(ResolveResult(status="ambiguous", candidates=["a", "b"], community="5"))
    bindings = resolve_and_bind(d.id, Descriptor(name="run"), reader, s)
    tiers = {b.tier: b for b in bindings}
    assert set(tiers) == {1}  # no leaf binding
    assert tiers[1].status == "degraded"


def test_unresolved_creates_orphaned_leaf(tmp_path):
    s = Store(tmp_path / "t.db")
    d = _decision(s)
    reader = FakeReader(ResolveResult(status="unresolved"))
    bindings = resolve_and_bind(d.id, Descriptor(name="ghost"), reader, s)
    assert len(bindings) == 1
    assert bindings[0].tier == 2 and bindings[0].status == "orphaned"


def test_ambiguous_real_reader_degrades_to_community(tmp_path):
    import json

    from sidegraph.engine.reader import GraphifyReader

    data = {
        "built_at_commit": "x",
        "nodes": [
            {
                "id": "a",
                "label": "run()",
                "norm_label": "run()",
                "file_type": "code",
                "source_file": "m.py",
                "community": 7,
            },
            {
                "id": "b",
                "label": "run()",
                "norm_label": "run()",
                "file_type": "code",
                "source_file": "m.py",
                "community": 7,
            },
        ],
        "links": [],
    }
    p = tmp_path / "g.json"
    p.write_text(json.dumps(data))
    s = Store(tmp_path / "t.db")
    d = _decision(s)
    bindings = resolve_and_bind(d.id, Descriptor(name="run"), GraphifyReader(p), s)
    tiers = {b.tier: b for b in bindings}
    assert set(tiers) == {1}
    assert tiers[1].status == "degraded"
    assert s.get_entity(tiers[1].entity_id).canonical_name == "community:7"


def test_initiative_creates_tier0(tmp_path):
    s = Store(tmp_path / "t.db")
    d = _decision(s)
    reader = FakeReader(ResolveResult(status="resolved", node_id="n1", community="18"))
    bindings = resolve_and_bind(
        d.id, Descriptor(name="X", file_path="x.py"), reader, s, initiative="Metadata Platform"
    )
    tiers = {b.tier for b in bindings}
    assert tiers == {2, 1, 0}


# -- domain-aware Tier-1 binding (mind-model layer, M2) ----------------------------------


def _accept_domain(store, slug="payments", communities=("18",)):
    d = store.add_domain(
        Domain(
            slug=slug,
            title=slug.title(),
            summary="Order settlement and refunds.",
            communities=list(communities),
            provenance=Provenance(source="manual"),
        )
    )
    store.ratify_domains(accept=[d.domain_id])
    return d


def test_accepted_domain_covering_community_wins_over_bare_community(tmp_path):
    s = Store(tmp_path / "t.db")
    d = _decision(s)
    _accept_domain(s, slug="payments", communities=["18"])
    reader = FakeReader(ResolveResult(status="resolved", node_id="n1", community="18"))
    bindings = resolve_and_bind(
        d.id, Descriptor(name="BitfinexAdapter", file_path="adapters/bitfinex.py"), reader, s
    )
    tier1 = next(b for b in bindings if b.tier == 1)
    assert tier1.status == "live"
    assert s.get_entity(tier1.entity_id).canonical_name == "domain:payments"


def test_accepted_domain_wins_even_when_leaf_ambiguous(tmp_path):
    """Domain-covered Tier-1 bindings are always 'live': once a domain has claimed the
    community, Tier-1 confidence comes from the domain's curation, not from whether this
    particular leaf resolved cleanly (see anchoring.resolve_and_bind's docstring)."""
    s = Store(tmp_path / "t.db")
    d = _decision(s)
    _accept_domain(s, slug="payments", communities=["5"])
    reader = FakeReader(ResolveResult(status="ambiguous", candidates=["a", "b"], community="5"))
    bindings = resolve_and_bind(d.id, Descriptor(name="run"), reader, s)
    tiers = {b.tier: b for b in bindings}
    assert set(tiers) == {1}  # still no leaf binding on ambiguous
    assert tiers[1].status == "live"
    assert s.get_entity(tiers[1].entity_id).canonical_name == "domain:payments"


def test_proposed_domain_does_not_count_falls_back_to_community(tmp_path):
    s = Store(tmp_path / "t.db")
    d = _decision(s)
    s.add_domain(
        Domain(
            slug="payments",
            title="Payments",
            summary="Order settlement.",
            communities=["18"],
            provenance=Provenance(source="manual"),
        )
    )  # never ratified -> stays "proposed"
    reader = FakeReader(ResolveResult(status="resolved", node_id="n1", community="18"))
    bindings = resolve_and_bind(d.id, Descriptor(name="X", file_path="x.py"), reader, s)
    tier1 = next(b for b in bindings if b.tier == 1)
    assert s.get_entity(tier1.entity_id).canonical_name == "community:18"


def test_no_matching_domain_still_falls_back_to_community(tmp_path):
    s = Store(tmp_path / "t.db")
    d = _decision(s)
    _accept_domain(s, slug="shipping", communities=["99"])  # covers a different community
    reader = FakeReader(ResolveResult(status="resolved", node_id="n1", community="18"))
    bindings = resolve_and_bind(d.id, Descriptor(name="X", file_path="x.py"), reader, s)
    tier1 = next(b for b in bindings if b.tier == 1)
    assert s.get_entity(tier1.entity_id).canonical_name == "community:18"


def test_relation_override_applies_to_leaf_and_tier1_not_initiative(tmp_path):
    s = Store(tmp_path / "t.db")
    d = _decision(s)
    reader = FakeReader(ResolveResult(status="resolved", node_id="n1", community="18"))
    bindings = resolve_and_bind(
        d.id,
        Descriptor(name="X", file_path="x.py"),
        reader,
        s,
        initiative="proj",
        relation="deprecates",
    )
    by_tier = {b.tier: b for b in bindings}
    assert by_tier[2].relation == "deprecates"
    assert by_tier[1].relation == "deprecates"
    assert by_tier[0].relation == "affects"  # initiative unaffected by per-anchor override


def test_relation_override_applies_to_domain_binding_too(tmp_path):
    s = Store(tmp_path / "t.db")
    d = _decision(s)
    _accept_domain(s, slug="payments", communities=["18"])
    reader = FakeReader(ResolveResult(status="resolved", node_id="n1", community="18"))
    bindings = resolve_and_bind(
        d.id,
        Descriptor(name="X", file_path="x.py"),
        reader,
        s,
        relation="creates",
    )
    tier1 = next(b for b in bindings if b.tier == 1)
    assert tier1.relation == "creates"


# -- AnchorResolution: resolve_and_bind's return carries the resolve outcome (Gate-5 finding
# S3) without breaking any caller that treats it as a plain list[AnchorBinding] -------------


def test_resolve_and_bind_return_carries_resolve_status_and_candidates(tmp_path):
    s = Store(tmp_path / "t.db")
    d = _decision(s)
    reader = FakeReader(ResolveResult(status="ambiguous", candidates=["a", "b"], community="5"))
    result = resolve_and_bind(d.id, Descriptor(name="run"), reader, s)
    assert result.status == "ambiguous"
    assert result.candidates == ["a", "b"]
    # still list-shaped for every existing caller (iteration, indexing, len())
    assert isinstance(result, list)
    assert len(result) == 1
    assert result[0].tier == 1


def test_resolve_and_bind_return_status_resolved_has_no_candidates(tmp_path):
    s = Store(tmp_path / "t.db")
    d = _decision(s)
    reader = FakeReader(ResolveResult(status="resolved", node_id="n1", community="18"))
    result = resolve_and_bind(
        d.id,
        Descriptor(name="X", file_path="x.py"),
        reader,
        s,
    )
    assert result.status == "resolved"
    assert result.candidates == []


def test_no_relation_override_defaults_to_affects(tmp_path):
    s = Store(tmp_path / "t.db")
    d = _decision(s)
    reader = FakeReader(ResolveResult(status="resolved", node_id="n1", community="18"))
    bindings = resolve_and_bind(d.id, Descriptor(name="X", file_path="x.py"), reader, s)
    assert all(b.relation == "affects" for b in bindings)
