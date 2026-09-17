"""find_entity — the missing entity_id discovery tool (see CLAUDE.md gap notes)."""

from __future__ import annotations

from sidegraph.schema import Descriptor, Entity
from sidegraph.server import _find_entity_impl
from sidegraph.store import Store


def test_find_entity_by_name_and_file_path(tmp_path):
    store = Store(tmp_path / "t.db")
    e = store.upsert_entity(
        Entity(
            canonical_name="Trader",
            descriptor=Descriptor(name="Trader", file_path="trader/exec.py"),
        )
    )

    out = _find_entity_impl(store, "Trader", "trader/exec.py")

    assert out["found"] is True
    assert out["entity_id"] == e.entity_id
    assert out["canonical_name"] == "Trader"
    assert out["descriptor"] == {"name": "Trader", "file_path": "trader/exec.py"}
    assert out["bindings"] == []


def test_find_entity_name_only_unique_match(tmp_path):
    store = Store(tmp_path / "t.db")
    e = store.upsert_entity(
        Entity(
            canonical_name="Trader",
            descriptor=Descriptor(name="Trader", file_path="trader/exec.py"),
        )
    )

    # No file_path given, and the exact descriptor lookup (file_path=None) misses —
    # falls back to the name-only scan, which finds exactly one match.
    out = _find_entity_impl(store, "Trader")

    assert out["found"] is True
    assert out["entity_id"] == e.entity_id


def test_find_entity_name_only_ambiguous_returns_candidates_never_guesses(tmp_path):
    store = Store(tmp_path / "t.db")
    a = store.upsert_entity(
        Entity(
            canonical_name="Trader",
            descriptor=Descriptor(name="Trader", file_path="trader/exec.py"),
        )
    )
    b = store.upsert_entity(
        Entity(
            canonical_name="Trader",
            descriptor=Descriptor(name="Trader", file_path="other/trader.py"),
        )
    )

    out = _find_entity_impl(store, "Trader")

    assert out["found"] is False
    assert "candidates" in out
    ids = {c["entity_id"] for c in out["candidates"]}
    assert ids == {a.entity_id, b.entity_id}
    files = {c["file_path"] for c in out["candidates"]}
    assert files == {"trader/exec.py", "other/trader.py"}


def test_find_entity_not_found(tmp_path):
    store = Store(tmp_path / "t.db")

    out = _find_entity_impl(store, "NoSuchEntity")

    assert out == {"found": False}


def test_find_entity_includes_bindings_summary(tmp_path):
    from datetime import UTC, datetime

    from sidegraph.schema import AnchorBinding, Decision, DecisionKind, Provenance

    store = Store(tmp_path / "t.db")
    e = store.upsert_entity(
        Entity(
            canonical_name="Trader",
            descriptor=Descriptor(name="Trader", file_path="trader/exec.py"),
        )
    )
    d = store.add_decision(
        Decision(
            title="t",
            kind=DecisionKind.ADR,
            context="c",
            choice="ch",
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual"),
        )
    )
    store.add_binding(AnchorBinding(record_id=d.id, entity_id=e.entity_id, tier=2))

    out = _find_entity_impl(store, "Trader", "trader/exec.py")

    assert out["bindings"] == [
        {"record_id": d.id, "record_type": "decision", "tier": 2, "status": "live"}
    ]


def test_find_entity_bindings_report_record_type_for_facts(tmp_path):
    """A binding to a Fact (not a Decision) is reported with record_type "fact" —
    see CLAUDE.md facts layer / store.add_binding widening."""
    from datetime import UTC, datetime

    from sidegraph.schema import AnchorBinding, Fact, Provenance

    store = Store(tmp_path / "t.db")
    e = store.upsert_entity(
        Entity(
            canonical_name="Trader",
            descriptor=Descriptor(name="Trader", file_path="trader/exec.py"),
        )
    )

    f = store.add_fact(
        Fact(
            statement="httpx has no built-in retry",
            source="httpx docs",
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="test"),
        )
    )
    store.add_binding(AnchorBinding(record_id=f.id, entity_id=e.entity_id, tier=2))

    out = _find_entity_impl(store, "Trader", "trader/exec.py")

    assert out["bindings"] == [
        {"record_id": f.id, "record_type": "fact", "tier": 2, "status": "live"}
    ]
