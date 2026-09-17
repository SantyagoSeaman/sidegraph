"""Telemetry wiring on the retrieval surfaces (spec §3, D6, D7)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from sidegraph.config import TELEMETRY_SESSION_KEY
from sidegraph.engine.reader import GraphifyReader
from sidegraph.schema import AnchorBinding, Decision, DecisionKind, Descriptor, Entity, Provenance
from sidegraph.server import (
    _add_domain_impl,
    _drill_down_impl,
    _get_task_context_impl,
    _query_decisions_impl,
    _query_structure_impl,
    _ratify_impl,
)
from sidegraph.store import Store

FIXTURE = Path(__file__).parent / "fixtures" / "mini_graph.json"


def _store_with_seed_mistake(tmp_path):
    """Same shape `test_server_thin_tools.py::test_query_decisions_impl_renders_mistake`
    builds: a store with a mistake decision bound to a Trader entity resolvable via the
    mini_graph fixture's `trader/exec.py`. Returns (store, reader, decision) so callers can
    assert against the real id telemetry should record."""
    r = GraphifyReader(FIXTURE)
    s = Store(tmp_path / "t.db")
    e = s.upsert_entity(
        Entity(
            canonical_name="Trader",
            descriptor=Descriptor(name="Trader", file_path="trader/exec.py"),
        )
    )
    d = Decision(
        title="deadlock",
        kind=DecisionKind.GOTCHA,
        context="c",
        choice="order locks",
        valid_from=datetime.now(UTC),
        provenance=Provenance(source="manual"),
    )
    s._write_decision(d)
    s._conn.commit()
    s.add_binding(AnchorBinding(record_id=d.id, entity_id=e.entity_id, tier=2, status="live"))
    return s, r, d


def test_get_task_context_impl_records_real_shows_and_seeds(tmp_path):
    """The positive case: telemetry on by default records exactly what the render showed.
    Without this, `_record(store, [], ...)` would pass every existing test in this module
    while every decision drifted toward a false `never-surfaced` flag -- the noise this
    design exists to prevent."""
    s, r, d = _store_with_seed_mistake(tmp_path)

    out = _get_task_context_impl(s, r, ["trader/exec.py"], None, 4000, 6000)

    assert "deadlock" in out
    assert s.retrieval_shows() == {d.id: 1}
    assert s.retrieval_seed_queries() == {"trader/exec.py": 1}


def test_query_decisions_impl_records_real_shows_and_seeds(tmp_path):
    """Same positive-path requirement as get_task_context (review follow-up: mutation
    testing found gutting `ctx.shown_ids` to `[]` at this call site left the entire suite
    green -- nothing here proved the real id/seed landed)."""
    s, r, d = _store_with_seed_mistake(tmp_path)

    out = _query_decisions_impl(s, r, files=["trader/exec.py"], entities=None, budget_chars=2000)

    assert "deadlock" in out
    assert s.retrieval_shows() == {d.id: 1}
    assert s.retrieval_seed_queries() == {"trader/exec.py": 1}


def _store_with_domain_decision(tmp_path):
    """A domain with one decision reachable through it (Tier-1 bound directly to the
    domain's paired `domain:<slug>` abstract entity) -- same construction
    `tests/test_retrieval_drilldown.py::test_drill_down_decisions_mistakes_first` uses,
    via this module's own server-level `_add_domain_impl`/`_ratify_impl` helpers (matching
    `tests/test_server_drilldown.py`'s style, since this exercises the server wrapper, not
    `retrieval.drill_down` directly). Returns (store, decision)."""
    s = Store(tmp_path / "t.db")
    dom = _add_domain_impl(s, None, slug="payments", title="Payments", summary="s.")
    _ratify_impl(s, accept=[dom["domain_id"]])
    d = Decision(
        title="use postgres",
        kind=DecisionKind.ADR,
        context="c",
        choice="ch",
        valid_from=datetime.now(UTC),
        provenance=Provenance(source="manual"),
    )
    s._write_decision(d)
    s._conn.commit()
    entity = s.find_abstract_entity("domain:payments")
    s.add_binding(AnchorBinding(record_id=d.id, entity_id=entity.entity_id, tier=1, status="live"))
    return s, d


def test_drill_down_impl_records_real_shows_and_seeds(tmp_path):
    """Same positive-path requirement as get_task_context (review follow-up: mutation
    testing found gutting `decision_ids` to `[]` at this call site also left the entire
    suite green)."""
    s, d = _store_with_domain_decision(tmp_path)

    out = _drill_down_impl(s, None, "payments")

    assert out["found"] is True
    assert any("use postgres" in line for line in out["decisions"])
    assert s.retrieval_shows() == {d.id: 1}
    assert s.retrieval_seed_queries() == {"domain:payments": 1}


def test_opt_out_records_nothing_but_still_returns_the_render(tmp_path, monkeypatch):
    """D6. Off means off — and the retrieval must be entirely unaffected."""
    monkeypatch.setenv("SIDEGRAPH_TELEMETRY", "off")
    s, r, _d = _store_with_seed_mistake(tmp_path)

    out = _get_task_context_impl(s, r, ["trader/exec.py"], None, 4000, 6000)

    assert "deadlock" in out
    assert s.retrieval_shows() == {}
    assert s.retrieval_seed_queries() == {}


def test_a_recording_failure_never_fails_the_retrieval(tmp_path, monkeypatch):
    """D7. A tool that broke reading because it could not write a counter would be worse
    than having no counters at all."""
    s, r, _d = _store_with_seed_mistake(tmp_path)

    def _boom(self, record_ids, seeds):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(Store, "record_retrieval", _boom)

    out = _get_task_context_impl(s, r, ["trader/exec.py"], None, 4000, 6000)

    assert "deadlock" in out


def test_anchor_paths_counts_bindings_regardless_of_status(tmp_path):
    """`_anchor_paths`'s own docstring pins that it must count every binding regardless of
    status: doctor's canonical resolution has no status field to filter on, so an
    implementation that filtered to live-only bindings would silently disagree with it
    about what a record is anchored to. A decision with one live and one orphaned binding
    must still produce a `show_anchor` event for the orphaned entity's file."""
    r = GraphifyReader(FIXTURE)
    s = Store(tmp_path / "t.db")
    live_entity = s.upsert_entity(
        Entity(
            canonical_name="Trader",
            descriptor=Descriptor(name="Trader", file_path="trader/exec.py"),
        )
    )
    orphaned_entity = s.upsert_entity(
        Entity(
            canonical_name="Risk",
            descriptor=Descriptor(name="Risk", file_path="trader/risk.py"),
        )
    )
    d = Decision(
        title="deadlock",
        kind=DecisionKind.GOTCHA,
        context="c",
        choice="order locks",
        valid_from=datetime.now(UTC),
        provenance=Provenance(source="manual"),
    )
    s._write_decision(d)
    s._conn.commit()
    s.add_binding(
        AnchorBinding(record_id=d.id, entity_id=live_entity.entity_id, tier=2, status="live")
    )
    s.add_binding(
        AnchorBinding(
            record_id=d.id, entity_id=orphaned_entity.entity_id, tier=2, status="orphaned"
        )
    )
    s.set_meta(TELEMETRY_SESSION_KEY, f"sess-orphan|{datetime.now(UTC).isoformat()}")

    out = _get_task_context_impl(s, r, ["trader/exec.py"], None, 4000, 6000)

    assert "deadlock" in out
    shown = {e["key"] for e in s.retrieval_events("sess-orphan") if e["kind"] == "show_anchor"}
    assert "trader/risk.py" in shown, "an orphaned binding's file must still be counted"


def test_a_naive_session_key_timestamp_records_no_events(tmp_path):
    """`_session_key`'s TTL check treats a naive (no-tzinfo) timestamp as untrustworthy —
    unexercised until now. Paired with the aware case below: only the aware key results in
    a journal entry for the same retrieval."""
    s, r, _d = _store_with_seed_mistake(tmp_path)
    s.set_meta(TELEMETRY_SESSION_KEY, f"naive-sess|{datetime.now().isoformat()}")

    _get_task_context_impl(s, r, ["trader/exec.py"], None, 4000, 6000)

    assert s.retrieval_events("naive-sess") == []


def test_an_aware_session_key_timestamp_records_events(tmp_path):
    s, r, _d = _store_with_seed_mistake(tmp_path)
    s.set_meta(TELEMETRY_SESSION_KEY, f"aware-sess|{datetime.now(UTC).isoformat()}")

    _get_task_context_impl(s, r, ["trader/exec.py"], None, 4000, 6000)

    assert s.retrieval_events("aware-sess") != []


def test_query_structure_records_nothing_at_all(tmp_path):
    """Spec correction (fix-wave review): the never-surfaced denominator counts
    opportunities for a decision to surface, and query_structure returns no decision
    memory, so it never offers one. Recording its seeds would inflate that denominator
    with non-opportunities -- an area explored only structurally could then be flagged
    "never surfaced" when no decision could possibly have fired there. Originally this
    tool recorded seeds only; that was wrong and has been removed."""
    r = GraphifyReader(FIXTURE)
    s = Store(tmp_path / "t.db")

    out = _query_structure_impl(s, r, files=["trader/exec.py"], entities=None, budget_chars=4000)

    assert "## Structural map" in out
    assert s.retrieval_shows() == {}
    assert s.retrieval_seed_queries() == {}
