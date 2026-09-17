from datetime import UTC, datetime

from sidegraph.anchoring import resolve_and_bind
from sidegraph.engine.reader import ResolveResult
from sidegraph.schema import Decision, DecisionKind, Descriptor, Entity, Provenance
from sidegraph.store import Store


class FakeReader:
    def __init__(self, result):
        self._result = result

    def resolve(self, desc):
        return self._result

    def graph_version(self):
        return "v1"


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


def test_entity_field_defaults_none():
    e = Entity(canonical_name="X")
    assert e.last_seen_community is None


def test_resolved_capture_records_community_baseline(tmp_path):
    s = Store(tmp_path / "t.db")
    d = _decision(s)
    reader = FakeReader(ResolveResult(status="resolved", node_id="n1", community="18"))
    bindings = resolve_and_bind(d.id, Descriptor(name="X", file_path="x.py"), reader, s)
    leaf = next(b for b in bindings if b.tier == 2)
    assert s.get_entity(leaf.entity_id).last_seen_community == "18"


def test_unresolved_capture_leaves_baseline_none(tmp_path):
    s = Store(tmp_path / "t.db")
    d = _decision(s)
    reader = FakeReader(ResolveResult(status="unresolved"))
    bindings = resolve_and_bind(d.id, Descriptor(name="ghost"), reader, s)
    leaf = next(b for b in bindings if b.tier == 2)
    assert s.get_entity(leaf.entity_id).last_seen_community is None
