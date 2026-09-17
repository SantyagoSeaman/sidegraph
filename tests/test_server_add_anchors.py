"""``add_anchors`` MCP tool — append bindings to an EXISTING decision or fact, the missing
in-place re-anchoring half of triage (design/superpowers/specs/
2026-07-11-ci-integrity-design.md ruling 3). Generalizes ``_bind_fact_anchors``'s
resolve-or-orphan ladder (see ``tests/test_server_facts.py``) to either record kind, tested
here through the testable core ``_add_anchors_impl``."""

from __future__ import annotations

from datetime import UTC, datetime

from sidegraph.engine.reader import ResolveResult
from sidegraph.schema import Decision, DecisionKind, Fact, Provenance
from sidegraph.server import _add_anchors_impl
from sidegraph.store import Store


class FakeReader:
    """Mirrors ``tests/test_anchoring.py``'s ``FakeReader`` — a fixed ``ResolveResult`` for
    every ``resolve()`` call, so each test controls the resolved/ambiguous/unresolved
    outcome directly instead of depending on a real graph fixture."""

    def __init__(self, result: ResolveResult) -> None:
        self._result = result

    def resolve(self, desc):
        return self._result

    def graph_version(self):
        return "testv1"


def _decision(store) -> Decision:
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


def _fact(store) -> Fact:
    return store.add_fact(
        Fact(
            statement="s",
            source="src",
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual"),
        )
    )


def test_add_anchors_binds_to_decision(tmp_path):
    store = Store(tmp_path / "srv.db")
    d = _decision(store)
    reader = FakeReader(ResolveResult(status="resolved", node_id="n1", community="18"))
    out = _add_anchors_impl(
        store, reader, d.id, anchors=[{"name": "Trader", "file_path": "trader/exec.py"}]
    )
    assert out["record_id"] == d.id
    assert out["orphaned"] == []
    assert out["ambiguous"] == []
    assert len(out["bound"]) == 1
    entry = out["bound"][0]
    assert set(entry) == {"entity_id", "canonical_name", "tier"}
    assert entry["canonical_name"] == "Trader"
    assert entry["tier"] == 2
    binds = store.bindings_for_record(d.id)
    assert {b.tier for b in binds} == {2, 1}  # leaf + community


def test_add_anchors_binds_to_fact(tmp_path):
    store = Store(tmp_path / "srv.db")
    f = _fact(store)
    reader = FakeReader(ResolveResult(status="resolved", node_id="n1", community="18"))
    out = _add_anchors_impl(
        store, reader, f.id, anchors=[{"name": "Trader", "file_path": "trader/exec.py"}]
    )
    assert out["record_id"] == f.id
    assert len(out["bound"]) == 1
    assert out["orphaned"] == []
    assert out["ambiguous"] == []
    binds = store.bindings_for_record(f.id)
    assert {b.tier for b in binds} == {2, 1}


def test_add_anchors_orphaned_fallback_with_no_reader(tmp_path):
    store = Store(tmp_path / "srv.db")
    d = _decision(store)
    out = _add_anchors_impl(
        store, None, d.id, anchors=[{"name": "sync.py", "file_path": "src/sync.py"}]
    )
    assert out["bound"] == []
    assert out["ambiguous"] == []
    assert len(out["orphaned"]) == 1
    assert out["orphaned"][0]["canonical_name"] == "sync.py"
    binds = store.bindings_for_record(d.id)
    assert len(binds) == 1 and binds[0].status == "orphaned"


def test_add_anchors_unresolved_with_reader_is_orphaned(tmp_path):
    store = Store(tmp_path / "srv.db")
    d = _decision(store)
    reader = FakeReader(ResolveResult(status="unresolved"))
    out = _add_anchors_impl(
        store, reader, d.id, anchors=[{"name": "ghost.py", "file_path": "src/ghost.py"}]
    )
    assert out["bound"] == []
    assert len(out["orphaned"]) == 1
    binds = store.bindings_for_record(d.id)
    assert len(binds) == 1 and binds[0].status == "orphaned"


def test_add_anchors_ambiguous_reports_candidates_and_writes_nothing(tmp_path):
    store = Store(tmp_path / "srv.db")
    d = _decision(store)
    reader = FakeReader(ResolveResult(status="ambiguous", candidates=["a", "b"]))
    out = _add_anchors_impl(store, reader, d.id, anchors=[{"name": "run"}])
    assert out["bound"] == []
    assert out["orphaned"] == []
    assert out["ambiguous"] == [{"name": "run", "reason": "ambiguous", "candidates": ["a", "b"]}]
    assert store.bindings_for_record(d.id) == []  # no community: nothing at all gets written


def test_add_anchors_unknown_record_returns_error_dict(tmp_path):
    store = Store(tmp_path / "srv.db")
    out = _add_anchors_impl(store, None, "01NOPE", anchors=[{"name": "x"}])
    assert out == {"error": "unknown record '01NOPE'"}


def test_add_anchors_invalid_relation_writes_nothing(tmp_path):
    store = Store(tmp_path / "srv.db")
    d = _decision(store)
    try:
        _add_anchors_impl(store, None, d.id, anchors=[{"name": "x", "relation": "bogus"}])
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
    assert store.bindings_for_record(d.id) == []


def test_add_anchors_duplicate_anchor_does_not_double_bind(tmp_path):
    # Pins store.add_binding's actual duplicate behavior (same record + entity is an
    # upsert on the (record_id, entity_id) primary key -- see store._index_write_binding)
    # rather than inventing a semantics for it: calling add_anchors twice with the exact
    # same anchor leaves exactly one binding, not two.
    store = Store(tmp_path / "srv.db")
    d = _decision(store)
    anchor = [{"name": "sync.py", "file_path": "src/sync.py"}]
    _add_anchors_impl(store, None, d.id, anchors=anchor)
    _add_anchors_impl(store, None, d.id, anchors=anchor)
    assert len(store.bindings_for_record(d.id)) == 1


def test_add_anchors_duplicate_anchor_with_reader_does_not_double_bind(tmp_path):
    store = Store(tmp_path / "srv.db")
    d = _decision(store)
    reader = FakeReader(ResolveResult(status="resolved", node_id="n1", community="18"))
    anchor = [{"name": "Trader", "file_path": "trader/exec.py"}]
    _add_anchors_impl(store, reader, d.id, anchors=anchor)
    _add_anchors_impl(store, reader, d.id, anchors=anchor)
    assert len(store.bindings_for_record(d.id)) == 2  # leaf + community, still just one each


def test_add_anchors_leaves_record_file_byte_identical(tmp_path):
    store = Store(tmp_path / "srv.db")
    d = _decision(store)
    path = store.path / "decisions" / f"{d.id}.json"
    before_bytes = path.read_bytes()
    before_mtime_ns = path.stat().st_mtime_ns

    reader = FakeReader(ResolveResult(status="resolved", node_id="n1", community="18"))
    out = _add_anchors_impl(
        store, reader, d.id, anchors=[{"name": "Trader", "file_path": "trader/exec.py"}]
    )
    assert out["bound"]  # sanity: the call actually did something

    after_bytes = path.read_bytes()
    after_mtime_ns = path.stat().st_mtime_ns
    assert after_bytes == before_bytes
    assert after_mtime_ns == before_mtime_ns
