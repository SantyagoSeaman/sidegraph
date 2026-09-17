from pathlib import Path

import pytest

from sidegraph.engine.reader import GraphifyReader
from sidegraph.server import _add_decision_impl
from sidegraph.store import Store

FIXTURE = Path(__file__).parent / "fixtures" / "mini_graph.json"


def test_add_decision_with_anchors_creates_bindings(tmp_path):
    store = Store(tmp_path / "srv.db")
    reader = GraphifyReader(FIXTURE)
    out = _add_decision_impl(
        store,
        reader,
        title="Use Trader for exec",
        kind="adr",
        context="c",
        choice="ch",
        anchors=[{"name": "Trader", "file_path": "trader/exec.py"}],
    )
    assert out["bindings"] == 2  # leaf + community
    binds = store.bindings_for_record(out["id"])
    assert {b.tier for b in binds} == {2, 1}


def test_add_decision_result_includes_entities(tmp_path):
    store = Store(tmp_path / "srv.db")
    reader = GraphifyReader(FIXTURE)
    out = _add_decision_impl(
        store,
        reader,
        title="Use Trader for exec",
        kind="adr",
        context="c",
        choice="ch",
        anchors=[{"name": "Trader", "file_path": "trader/exec.py"}],
    )
    assert len(out["entities"]) == 2
    tiers = {e["tier"] for e in out["entities"]}
    assert tiers == {2, 1}
    for e in out["entities"]:
        assert e["entity_id"]
        assert e["canonical_name"]
    # the tier-2 entity_id matches what find_entity/get_entity_history would need
    leaf = next(e for e in out["entities"] if e["tier"] == 2)
    found = store.find_entity("Trader", "trader/exec.py")
    assert found is not None and leaf["entity_id"] == found.entity_id


def test_add_decision_without_anchors_entities_empty(tmp_path):
    store = Store(tmp_path / "srv.db")
    out = _add_decision_impl(store, None, title="t", kind="adr", context="c", choice="ch")
    assert out["entities"] == []


def test_add_decision_without_anchors_still_writes(tmp_path):
    store = Store(tmp_path / "srv.db")
    out = _add_decision_impl(store, None, title="t", kind="adr", context="c", choice="ch")
    assert out["status"] == "accepted"
    assert out["bindings"] == 0  # no reader, no anchors -> no bindings


def test_add_decision_skips_malformed_anchor(tmp_path):
    store = Store(tmp_path / "srv.db")
    reader = GraphifyReader(FIXTURE)
    out = _add_decision_impl(
        store,
        reader,
        title="Use Trader for exec",
        kind="adr",
        context="c",
        choice="ch",
        anchors=[
            {"file_path": "trader/exec.py"},  # malformed: no "name"
            {"name": "Trader", "file_path": "trader/exec.py"},  # good
        ],
    )
    assert out["id"]
    assert out["status"] == "accepted"
    assert out["bindings"] == 2  # only the good anchor binds
    assert store.get_decision(out["id"]) is not None


def test_add_decision_anchors_but_no_reader_still_writes(tmp_path):
    store = Store(tmp_path / "srv.db")
    out = _add_decision_impl(
        store,
        None,
        title="t",
        kind="adr",
        context="c",
        choice="ch",
        anchors=[{"name": "Trader", "file_path": "trader/exec.py"}],
    )
    assert out["status"] == "accepted"
    # reader absent -> anchoring skipped, still writes
    assert out["bindings"] == 0


def test_add_decision_records_graph_version_in_provenance(tmp_path):
    store = Store(tmp_path / "srv.db")
    reader = GraphifyReader(FIXTURE)
    out = _add_decision_impl(
        store,
        reader,
        title="t",
        kind="adr",
        context="c",
        choice="ch",
    )
    decision = Store(tmp_path / "srv.db").get_decision(out["id"])
    assert decision.provenance.graph_version == reader.graph_version()
    assert decision.provenance.graph_version.startswith("abc123:")


# -- tags + layer + per-anchor relation at capture (mind-model layer, M2) -----------------


def test_add_decision_tags_bind_tier0_entities(tmp_path):
    store = Store(tmp_path / "srv.db")
    out = _add_decision_impl(
        store,
        None,
        title="t",
        kind="adr",
        context="c",
        choice="ch",
        tags=["Security", "  Needs Review "],
    )
    security = store.find_abstract_entity("tag:security")
    needs_review = store.find_abstract_entity("tag:needs-review")
    assert security is not None and needs_review is not None
    tiers = {e["tier"] for e in out["entities"]}
    assert 0 in tiers
    bound = {d.id for d in store.valid_decisions_for_entity(security.entity_id)}
    assert out["id"] in bound


def test_add_decision_empty_slug_tag_skipped(tmp_path):
    store = Store(tmp_path / "srv.db")
    out = _add_decision_impl(
        store,
        None,
        title="t",
        kind="adr",
        context="c",
        choice="ch",
        tags=["!!!", ""],
    )
    assert out["bindings"] == 0  # neither tag slugified to anything bindable


def test_add_decision_layer_round_trips(tmp_path):
    store = Store(tmp_path / "srv.db")
    out = _add_decision_impl(
        store,
        None,
        title="t",
        kind="adr",
        context="c",
        choice="ch",
        layer="technical",
    )
    assert store.get_decision(out["id"]).layer == "technical"


def test_add_decision_bare_string_tags_coerced(tmp_path):
    # Live finding (manual test 2026-07-08): agents pass tags as a bare string on the
    # first try; the tool must accept it (comma-separated) instead of forcing a retry.
    store = Store(tmp_path / "srv.db")
    out = _add_decision_impl(
        store,
        None,
        title="t",
        kind="adr",
        context="c",
        choice="ch",
        tags="Security, needs review",
    )
    assert store.find_abstract_entity("tag:security") is not None
    assert store.find_abstract_entity("tag:needs-review") is not None
    assert 0 in {e["tier"] for e in out["entities"]}


def test_add_decision_no_tags_or_layer_defaults(tmp_path):
    store = Store(tmp_path / "srv.db")
    out = _add_decision_impl(store, None, title="t", kind="adr", context="c", choice="ch")
    assert store.get_decision(out["id"]).layer is None
    assert out["bindings"] == 0


def test_add_decision_per_anchor_relation_override(tmp_path):
    store = Store(tmp_path / "srv.db")
    reader = GraphifyReader(FIXTURE)
    out = _add_decision_impl(
        store,
        reader,
        title="t",
        kind="adr",
        context="c",
        choice="ch",
        anchors=[{"name": "Trader", "file_path": "trader/exec.py", "relation": "deprecates"}],
    )
    binds = store.bindings_for_record(out["id"])
    assert {b.relation for b in binds} == {"deprecates"}


def test_add_decision_anchor_without_relation_defaults_to_affects(tmp_path):
    store = Store(tmp_path / "srv.db")
    reader = GraphifyReader(FIXTURE)
    out = _add_decision_impl(
        store,
        reader,
        title="t",
        kind="adr",
        context="c",
        choice="ch",
        anchors=[{"name": "Trader", "file_path": "trader/exec.py"}],
    )
    binds = store.bindings_for_record(out["id"])
    assert {b.relation for b in binds} == {"affects"}


# -- per-anchor relation validated BEFORE any write (M5, M2 review fold-in) ---------------


def test_add_decision_invalid_anchor_relation_writes_nothing(tmp_path):
    store = Store(tmp_path / "srv.db")
    with pytest.raises(ValueError, match="relation"):
        _add_decision_impl(
            store,
            None,
            title="t",
            kind="adr",
            context="c",
            choice="ch",
            anchors=[{"name": "Trader", "relation": "not-a-real-relation"}],
        )
    assert list(store.iter_decisions()) == []


def test_add_decision_invalid_anchor_relation_no_half_anchored_row(tmp_path):
    """A valid anchor listed BEFORE the invalid one must not get its entity/binding
    written either -- validation runs for the whole batch before any store write."""
    store = Store(tmp_path / "srv.db")
    reader = GraphifyReader(FIXTURE)
    with pytest.raises(ValueError):
        _add_decision_impl(
            store,
            reader,
            title="t",
            kind="adr",
            context="c",
            choice="ch",
            anchors=[
                {"name": "Trader", "file_path": "trader/exec.py"},
                {"name": "helper()", "file_path": "util/misc.py", "relation": "bogus"},
            ],
        )
    assert list(store.iter_decisions()) == []
    assert store.find_entity("Trader", "trader/exec.py") is None


def test_add_decision_valid_relation_still_writes(tmp_path):
    store = Store(tmp_path / "srv.db")
    out = _add_decision_impl(
        store,
        None,
        title="t",
        kind="adr",
        context="c",
        choice="ch",
        anchors=[{"name": "Trader", "relation": "creates"}],
    )
    assert store.get_decision(out["id"]) is not None
