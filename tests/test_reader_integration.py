from pathlib import Path

from sidegraph.anchoring import resolve_and_bind
from sidegraph.engine.reader import GraphifyReader
from sidegraph.schema import Descriptor
from sidegraph.store import Store

SLICE = Path(__file__).parent / "fixtures" / "bitfinex_slice.json"


def test_resolve_real_class_by_name_and_file():
    r = GraphifyReader(SLICE)
    res = r.resolve(Descriptor(name="BitfinexAdapter", file_path="adapters/bitfinex.py"))
    assert res.status == "resolved"
    assert res.node_id == "adapters_bitfinex_bitfinexadapter"
    assert res.community == "18"


def test_end_to_end_anchor_real_entity(tmp_path):
    from datetime import UTC, datetime

    from sidegraph.schema import Decision, DecisionKind, Provenance

    r = GraphifyReader(SLICE)
    s = Store(tmp_path / "e.db")
    d = s.add_decision(
        Decision(
            title="BitfinexAdapter owns rate-limit retries",
            kind=DecisionKind.LESSON,
            context="429s during bursts",
            choice="retry with backoff in the adapter",
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual"),
        )
    )
    bindings = resolve_and_bind(
        d.id, Descriptor(name="BitfinexAdapter", file_path="adapters/bitfinex.py"), r, s
    )
    tiers = {b.tier: b for b in bindings}
    assert set(tiers) == {2, 1}
    assert tiers[2].status == "live"
    # Tier-1 points at the real community 18
    assert s.get_entity(tiers[1].entity_id).canonical_name == "community:18"


def test_ambiguous_dunder_across_methods():
    # ".__init__()" canonicalizes to "__init__"; only one here, so still resolvable by file
    r = GraphifyReader(SLICE)
    res = r.resolve(Descriptor(name="__init__", file_path="adapters/bitfinex.py"))
    assert res.status == "resolved"
    assert res.node_id == "adapters_bitfinex_bitfinexadapter_init"
