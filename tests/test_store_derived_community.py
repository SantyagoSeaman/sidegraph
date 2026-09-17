"""Derived community bindings: index-only, never canonical.
see design/superpowers/specs/2026-07-10-derived-community-bindings-design.md"""

import json
from datetime import UTC, datetime

import pytest

from sidegraph.schema import (
    AnchorBinding,
    Decision,
    DecisionKind,
    Entity,
    EntityKind,
    Provenance,
)
from sidegraph.store import Store

NOW = datetime.now(UTC)


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "s") as s:
        yield s


def _decision(**kw):
    base = dict(
        title="use httpx",
        kind=DecisionKind.ADR,
        context="c",
        choice="ch",
        valid_from=NOW,
        provenance=Provenance(source="test"),
    )
    base.update(kw)
    return Decision(**base)


def _canonical_snapshot(root):
    out = {}
    for p in sorted(root.rglob("*.json")):
        if "index.db" in str(p):
            continue
        out[str(p)] = (p.read_bytes(), p.stat().st_mtime_ns)
    return out


def test_community_entity_never_writes_canonical(store, tmp_path):
    e = store.get_or_create_abstract_entity("community:72")
    assert e.entity_id  # index row exists
    assert store.find_abstract_entity("community:72") is not None
    assert not (tmp_path / "s" / "entities" / f"{e.entity_id}.json").exists()


def test_upsert_entity_direct_call_is_index_only_for_community(store, tmp_path):
    """The guard lives in `upsert_entity` itself now (not just in the
    `get_or_create_abstract_entity` caller), so a direct call with a brand-new
    `community:*` abstract Entity is index-only too -- invariant airtight, not
    caller-discipline."""
    e = store.upsert_entity(Entity(canonical_name="community:5", kind=EntityKind.ABSTRACT))
    assert store.find_abstract_entity("community:5") is not None  # index row exists
    assert not (tmp_path / "s" / "entities" / f"{e.entity_id}.json").exists()


def test_noncommunity_abstract_entity_still_canonical(store, tmp_path):
    e = store.get_or_create_abstract_entity("domain:risk")
    assert (tmp_path / "s" / "entities" / f"{e.entity_id}.json").exists()


def test_community_binding_is_index_only(store, tmp_path):
    d = store.add_decision(_decision())
    leaf = store.upsert_entity(Entity(canonical_name="fee_gate.py"))
    store.add_binding(AnchorBinding(record_id=d.id, entity_id=leaf.entity_id, tier=2))
    before = _canonical_snapshot(tmp_path / "s")
    comm = store.get_or_create_abstract_entity("community:7")
    store.add_binding(AnchorBinding(record_id=d.id, entity_id=comm.entity_id, tier=1))
    assert _canonical_snapshot(tmp_path / "s") == before  # content AND mtime
    assert {b.entity_id for b in store.bindings_for_record(d.id)} == {
        leaf.entity_id,
        comm.entity_id,
    }  # index sees both


def test_canonical_payload_filters_community_entries(store, tmp_path):
    d = store.add_decision(_decision())
    comm = store.get_or_create_abstract_entity("community:7")
    store.add_binding(AnchorBinding(record_id=d.id, entity_id=comm.entity_id, tier=1))
    leaf = store.upsert_entity(Entity(canonical_name="fee_gate.py"))
    store.add_binding(AnchorBinding(record_id=d.id, entity_id=leaf.entity_id, tier=2))
    payload = json.loads((tmp_path / "s" / "bindings" / f"{d.id}.json").read_text())
    assert [p["entity_id"] for p in payload] == [leaf.entity_id]


def test_reload_tolerates_and_decays_legacy_community_entries(store, tmp_path):
    # Simulate an OLD-format store: hand-author canonical community entity + binding entry.
    d = store.add_decision(_decision())
    leaf = store.upsert_entity(Entity(canonical_name="fee_gate.py"))
    store.add_binding(AnchorBinding(record_id=d.id, entity_id=leaf.entity_id, tier=2))
    legacy_eid = "01LEGACYCOMMUNITYENTITYXXX"
    (tmp_path / "s" / "entities" / f"{legacy_eid}.json").write_text(
        json.dumps(
            {
                "entity_id": legacy_eid,
                "canonical_name": "community:9",
                "kind": "abstract",
                "descriptor": None,
            }
        )
        + "\n"
    )
    bpath = tmp_path / "s" / "bindings" / f"{d.id}.json"
    payload = json.loads(bpath.read_text())
    payload.append({"entity_id": legacy_eid, "tier": 1, "relation": "affects", "weight": 1.0})
    bpath.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n")
    (tmp_path / "s" / "index.db").unlink()
    with Store(tmp_path / "s") as reopened:
        assert {b.entity_id for b in reopened.bindings_for_record(d.id)} >= {legacy_eid}
        leaf2 = reopened.upsert_entity(Entity(canonical_name="other.py"))
        reopened.add_binding(AnchorBinding(record_id=d.id, entity_id=leaf2.entity_id, tier=2))
        fresh = json.loads(bpath.read_text())
        assert all(p["entity_id"] != legacy_eid for p in fresh)  # decayed on rewrite
