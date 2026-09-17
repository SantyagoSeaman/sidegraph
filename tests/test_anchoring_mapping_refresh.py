"""``anchoring.py:74``'s engine-mapping refresh-on-hit (design D3, entity-identity-uniqueness
spec) — the real risk in Task 3.

``resolve_and_bind``'s Tier-2 leaf handling is NOT a pure get-or-create: on a resolved node it
refreshes ``last_seen_node_id``/``last_seen_graph_version``/``last_seen_community`` and
upserts EVEN WHEN the entity already existed. Converting the longhand ``find_entity`` +
``upsert_entity`` there to ``Store.get_or_create_entity`` (which only finds-or-mints, never
refreshes) must leave that refresh AT THE CALL SITE — dropping it would silently stop sync's
engine mapping from updating on every subsequent resolve of an already-known entity.

This is a **characterization test** (spec §4 item 5, plan Task 3 step 1): green before AND
after the conversion, by design — it exists to pin the behavior BEFORE the call site is
touched, so the conversion cannot silently drop the refresh. It is NOT red-first evidence.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sidegraph.anchoring import resolve_and_bind
from sidegraph.engine.reader import ResolveResult
from sidegraph.schema import Decision, DecisionKind, Descriptor, Provenance
from sidegraph.store import Store


class _FakeReader:
    def __init__(self, result: ResolveResult, graph_version: str) -> None:
        self._result = result
        self._graph_version = graph_version

    def resolve(self, desc):
        return self._result

    def graph_version(self):
        return self._graph_version


def _decision(store: Store) -> Decision:
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


def test_a_resolved_node_refreshes_engine_mapping_on_an_entity_that_already_existed(tmp_path):
    s = Store(tmp_path / "t.db")
    ref = Descriptor(name="BitfinexAdapter", file_path="adapters/bitfinex.py")

    first_reader = _FakeReader(
        ResolveResult(status="resolved", node_id="n1", community="18"), graph_version="v1"
    )
    first_bindings = resolve_and_bind(_decision(s).id, ref, first_reader, s)
    leaf_id = next(b for b in first_bindings if b.tier == 2).entity_id
    first_entity = s.get_entity(leaf_id)
    assert first_entity.last_seen_node_id == "n1"
    assert first_entity.last_seen_graph_version == "v1"
    assert first_entity.last_seen_community == "18"

    # A second resolve of the SAME descriptor -- the entity already exists this time -- with
    # a DIFFERENT node id / graph version / community (a rebuild renumbered nodes and
    # communities, the ordinary case sync exists to handle). The refresh must still land on
    # the EXISTING entity, not merely on a freshly-minted one.
    second_reader = _FakeReader(
        ResolveResult(status="resolved", node_id="n2", community="42"), graph_version="v2"
    )
    second_bindings = resolve_and_bind(_decision(s).id, ref, second_reader, s)
    second_leaf_id = next(b for b in second_bindings if b.tier == 2).entity_id

    assert second_leaf_id == leaf_id, "a second resolve must reuse the same durable entity"
    refreshed = s.get_entity(leaf_id)
    assert refreshed.last_seen_node_id == "n2"
    assert refreshed.last_seen_graph_version == "v2"
    assert refreshed.last_seen_community == "42"


def test_an_unresolved_anchor_does_not_touch_an_existing_entitys_engine_mapping(tmp_path):
    """The complement: once resolved-then-refreshed, a LATER unresolved hit on the same
    descriptor (e.g. the file moved and the reader lost track of it) must not clobber the
    previously-recorded engine mapping -- ``resolve_and_bind`` only refreshes on
    ``status == "resolved"``."""
    s = Store(tmp_path / "t.db")
    ref = Descriptor(name="BitfinexAdapter", file_path="adapters/bitfinex.py")

    resolved_reader = _FakeReader(
        ResolveResult(status="resolved", node_id="n1", community="18"), graph_version="v1"
    )
    first_bindings = resolve_and_bind(_decision(s).id, ref, resolved_reader, s)
    leaf_id = next(b for b in first_bindings if b.tier == 2).entity_id

    unresolved_reader = _FakeReader(ResolveResult(status="unresolved"), graph_version="v2")
    second_bindings = resolve_and_bind(_decision(s).id, ref, unresolved_reader, s)
    second_leaf_id = next(b for b in second_bindings if b.tier == 2).entity_id

    assert second_leaf_id == leaf_id
    unchanged = s.get_entity(leaf_id)
    assert unchanged.last_seen_node_id == "n1"
    assert unchanged.last_seen_graph_version == "v1"
    assert unchanged.last_seen_community == "18"
