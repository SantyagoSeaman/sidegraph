"""The server's render-event write (design 2026-09-18-usage-stats-design.md, D3/D4)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from sidegraph import server
from sidegraph.engine.reader import GraphifyReader
from sidegraph.retrieval import TaskContext
from sidegraph.schema import (
    AnchorBinding,
    Decision,
    DecisionKind,
    DecisionStatus,
    Descriptor,
    Entity,
    Provenance,
)
from sidegraph.store import Store

FIXTURE = Path(__file__).parent / "fixtures" / "mini_graph.json"


def _ctx():
    ctx = TaskContext()
    ctx.shown_ids = ["rec-1", "rec-2"]
    ctx.selected, ctx.emitted, ctx.degraded = 4, 2, 1
    ctx.dropped_for_budget, ctx.chars_used = 2, 3300
    return ctx


def test_record_writes_one_render_event(tmp_path, monkeypatch):
    store = Store(tmp_path / "s")
    monkeypatch.setattr(server, "_session_key", lambda _s: "sess-1")
    server._record(store, ["rec-1"], [], ctx=_ctx(), intent="check-plan")
    e = store.render_events()[0]
    assert (e["selected"], e["emitted"], e["degraded"], e["dropped_for_budget"]) == (4, 2, 1, 2)
    assert e["chars_used"] == 3300
    assert e["intent"] == "check-plan"
    store.close()


def test_emitted_is_read_from_the_context_not_from_shown_ids(tmp_path, monkeypatch):
    """`shown_ids` also carries fact ids, so its length can exceed the decision count."""
    store = Store(tmp_path / "s")
    monkeypatch.setattr(server, "_session_key", lambda _s: "sess-1")
    ctx = TaskContext()
    ctx.shown_ids = ["dec-1", "fact-1", "fact-2"]
    ctx.selected = ctx.emitted = 1
    server._record(store, ctx.shown_ids, [], ctx=ctx)
    assert store.render_events()[0]["emitted"] == 1
    store.close()


def test_no_ctx_writes_no_render_event(tmp_path, monkeypatch):
    """A caller that passes no `TaskContext` gets no render row. `drill_down` builds its own
    (see the drill-down tests below); this is the contract of `_record` itself."""
    store = Store(tmp_path / "s")
    monkeypatch.setattr(server, "_session_key", lambda _s: "sess-1")
    server._record(store, ["rec-1"], ["domain:payments"])
    assert store.render_events() == []
    store.close()


def test_telemetry_off_writes_no_render_event(tmp_path, monkeypatch):
    monkeypatch.setenv("SIDEGRAPH_TELEMETRY", "off")
    store = Store(tmp_path / "s")
    monkeypatch.setattr(server, "_session_key", lambda _s: "sess-1")
    server._record(store, ["rec-1"], [], ctx=_ctx())
    assert store.render_events() == []
    store.close()


def test_a_raising_writer_never_propagates(tmp_path, monkeypatch):
    store = Store(tmp_path / "s")
    monkeypatch.setattr(server, "_session_key", lambda _s: "sess-1")
    monkeypatch.setattr(store, "record_render_event", lambda *a, **k: 1 / 0)
    server._record(store, ["rec-1"], [], ctx=_ctx())  # must not raise
    store.close()


def test_a_raising_writer_does_not_cost_the_other_journals(tmp_path, monkeypatch):
    """The render write has its own suppress, placed after the two existing writes."""
    store = Store(tmp_path / "s")
    monkeypatch.setattr(server, "_session_key", lambda _s: "sess-1")
    monkeypatch.setattr(store, "record_render_event", lambda *a, **k: 1 / 0)
    server._record(store, ["rec-1"], [], ctx=_ctx())
    assert store.retrieval_shows() == {"rec-1": 1}
    store.close()


def test_a_missing_session_key_writes_no_render_event(tmp_path, monkeypatch):
    """No session means no event, never a misattributed one."""
    store = Store(tmp_path / "s")
    monkeypatch.setattr(server, "_session_key", lambda _s: None)
    server._record(store, ["rec-1"], [], ctx=_ctx(), intent="check-plan")
    assert store.render_events() == []
    store.close()


def _decision(title: str, **fields) -> Decision:
    return Decision(
        title=title,
        kind=DecisionKind.LESSON,
        context="c",
        choice="x",
        valid_from=datetime.now(UTC),
        provenance=Provenance(source="manual"),
        **fields,
    )


def test_flags_read_what_the_shown_records_carry(tmp_path, monkeypatch):
    """had_rejected / had_superseded come from the records that reached the render; a
    fact id in `shown_ids` has no decision behind it and must simply be skipped."""
    store = Store(tmp_path / "s")
    monkeypatch.setattr(server, "_session_key", lambda _s: "sess-1")
    plain = _decision("plain")
    rejected = _decision("with-rejected", rejected="tried X, abandoned")
    old = _decision("old", status=DecisionStatus.SUPERSEDED)
    for d in (plain, rejected, old):
        store._write_decision(d)
    store._conn.commit()

    ctx = TaskContext()
    ctx.shown_ids = [plain.id, "a-fact-id"]
    server._record(store, ctx.shown_ids, [], ctx=ctx)
    ctx.shown_ids = [plain.id, rejected.id]
    server._record(store, ctx.shown_ids, [], ctx=ctx)
    ctx.shown_ids = [old.id]
    server._record(store, ctx.shown_ids, [], ctx=ctx)

    got = [(e["had_rejected"], e["had_superseded"]) for e in store.render_events()]
    assert got == [(0, 0), (1, 0), (0, 1)]
    store.close()


def _store_with_seed_mistake(tmp_path):
    """A gotcha bound to the mini_graph fixture's `trader/exec.py`, so both impls render it."""
    reader = GraphifyReader(FIXTURE)
    store = Store(tmp_path / "t.db")
    entity = store.upsert_entity(
        Entity(
            canonical_name="Trader",
            descriptor=Descriptor(name="Trader", file_path="trader/exec.py"),
        )
    )
    decision = Decision(
        title="deadlock",
        kind=DecisionKind.GOTCHA,
        context="c",
        choice="order locks",
        valid_from=datetime.now(UTC),
        provenance=Provenance(source="manual"),
    )
    store._write_decision(decision)
    store._conn.commit()
    store.add_binding(
        AnchorBinding(record_id=decision.id, entity_id=entity.entity_id, tier=2, status="live")
    )
    return store, reader


def test_get_task_context_impl_threads_intent_and_counters(tmp_path, monkeypatch):
    store, reader = _store_with_seed_mistake(tmp_path)
    monkeypatch.setattr(server, "_session_key", lambda _s: "sess-1")
    server._get_task_context_impl(
        store, reader, ["trader/exec.py"], None, 4000, 6000, intent="check-plan"
    )
    [e] = store.render_events()
    assert e["intent"] == "check-plan"
    assert (e["selected"], e["emitted"]) == (1, 1)
    store.close()


def test_query_decisions_impl_threads_intent_and_counters(tmp_path, monkeypatch):
    store, reader = _store_with_seed_mistake(tmp_path)
    monkeypatch.setattr(server, "_session_key", lambda _s: "sess-1")
    server._query_decisions_impl(
        store, reader, ["trader/exec.py"], None, 2000, intent="explain-why"
    )
    [e] = store.render_events()
    assert e["intent"] == "explain-why"
    assert (e["selected"], e["emitted"]) == (1, 1)
    store.close()


def test_the_tools_forward_intent_to_their_impls(tmp_path, monkeypatch):
    """`get_task_context` and `query_decisions` are the only public surfaces; a dropped
    `intent` between the tool and its impl would leave every journal row unlabelled."""
    store, reader = _store_with_seed_mistake(tmp_path)
    monkeypatch.setattr(server, "_session_key", lambda _s: "sess-1")
    monkeypatch.setattr(server, "_get_store", lambda: store)
    monkeypatch.setattr(server, "_synced_reader", lambda: reader)
    server.get_task_context(files=["trader/exec.py"], intent="check-plan")
    server.query_decisions(files=["trader/exec.py"], intent="explain-why")
    assert [e["intent"] for e in store.render_events()] == ["check-plan", "explain-why"]
    store.close()


# --- drill_down delivers records, so it writes a render event (usage-stats fix wave 3) -------
#
# It applies no budget, so its row carries the records returned as `emitted` and zeros for every
# budget field, under the reserved intent the report reads to keep those zeros out of the budget
# figures. Before this it wrote none, and a session that also had an ordinary render row lost
# the drill-down's records from `showings`.


def _domain_store(tmp_path, *, rejected=""):
    """A domain with one accepted decision Tier-1 bound to its `domain:<slug>` entity: the
    construction `tests/test_server_telemetry.py` uses for its own drill_down test."""
    store = Store(tmp_path / "s")
    dom = server._add_domain_impl(store, None, slug="payments", title="Payments", summary="s.")
    server._ratify_impl(store, accept=[dom["domain_id"]])
    d = Decision(
        title="use postgres",
        kind=DecisionKind.ADR,
        context="c",
        choice="ch",
        rejected=rejected,
        valid_from=datetime.now(UTC),
        provenance=Provenance(source="manual"),
    )
    store._write_decision(d)
    store._conn.commit()
    entity = store.find_abstract_entity("domain:payments")
    store.add_binding(
        AnchorBinding(record_id=d.id, entity_id=entity.entity_id, tier=1, status="live")
    )
    return store, d


def test_drill_down_writes_a_render_event_of_the_records_it_returned(tmp_path, monkeypatch):
    store, _d = _domain_store(tmp_path)
    monkeypatch.setattr(server, "_session_key", lambda _s: "sess-1")
    out = server._drill_down_impl(store, None, "payments")
    assert any("use postgres" in line for line in out["decisions"])
    (e,) = store.render_events()
    assert e["session_id"] == "sess-1"
    assert e["intent"] == "drill_down" == server.DRILL_DOWN_INTENT
    assert e["emitted"] == 1 and e["selected"] == 1
    assert (e["degraded"], e["dropped_for_budget"], e["chars_used"]) == (0, 0, 0)
    assert (e["had_rejected"], e["had_superseded"]) == (0, 0)
    store.close()


def test_drill_down_row_says_when_a_returned_decision_held_a_rejected_alternative(
    tmp_path, monkeypatch
):
    """`had_rejected` applies to a drill-down: it returned a record with a non-empty `rejected`."""
    store, _d = _domain_store(tmp_path, rejected="mysql: no")
    monkeypatch.setattr(server, "_session_key", lambda _s: "sess-1")
    server._drill_down_impl(store, None, "payments")
    assert store.render_events()[0]["had_rejected"] == 1
    store.close()


def test_a_drill_down_of_an_unknown_slug_writes_no_render_event(tmp_path, monkeypatch):
    store, _d = _domain_store(tmp_path)
    monkeypatch.setattr(server, "_session_key", lambda _s: "sess-1")
    assert server._drill_down_impl(store, None, "no-such-domain")["found"] is False
    assert store.render_events() == []
    store.close()


def test_drill_down_with_telemetry_off_writes_no_render_event(tmp_path, monkeypatch):
    monkeypatch.setenv("SIDEGRAPH_TELEMETRY", "off")
    store, _d = _domain_store(tmp_path)
    monkeypatch.setattr(server, "_session_key", lambda _s: "sess-1")
    server._drill_down_impl(store, None, "payments")
    assert store.render_events() == []
    store.close()


def test_a_caller_cannot_pass_the_reserved_intent_to_a_budgeted_lookup(tmp_path, monkeypatch):
    """The label is how the report tells a drill-down's zeros from a budget's. A budgeted
    lookup that passed it would have its real counts read as no budget at all, so it is not
    recorded for a caller, and its other labels are untouched."""
    store = Store(tmp_path / "s")
    reader = GraphifyReader(FIXTURE)
    monkeypatch.setattr(server, "_session_key", lambda _s: "sess-1")
    server._get_task_context_impl(
        store, reader, ["trader/exec.py"], None, 4000, 6000, intent="drill_down"
    )
    server._get_task_context_impl(
        store, reader, ["trader/exec.py"], None, 4000, 6000, intent="check-plan"
    )
    server._query_decisions_impl(
        store, reader, ["trader/exec.py"], None, 4000, intent=" drill_down "
    )
    assert [e["intent"] for e in store.render_events()] == [None, "check-plan", None]
    store.close()


# --- one `_record` call is one session (usage-stats fix wave 3) ------------------------------
#
# `_record` used to read the session key once per journal write. The key lives in a shared
# `meta` row that a `SessionStart` overwrites, so a start landing between the two reads stamped
# the show rows and the render row of ONE call with two different sessions, and the report
# counted two sessions and two showings. The key is sampled once per call.


def _key_that_moves(monkeypatch, *values):
    """Patch `_session_key` to return `values` in order (the last one repeats), which is the
    interleaving without real concurrency: a new session starting between two reads."""
    calls: list[int] = []

    def moving(_store):
        calls.append(1)
        return values[min(len(calls), len(values)) - 1]

    monkeypatch.setattr(server, "_session_key", moving)
    return calls


def _path_anchored_domain_store(tmp_path):
    """`_domain_store`'s decision, also bound to a file, so the drill-down leaves a show row."""
    store, d = _domain_store(tmp_path)
    file_entity = store.get_or_create_entity(Descriptor(name="Trader", file_path="trader/exec.py"))
    store.add_binding(AnchorBinding(record_id=d.id, entity_id=file_entity.entity_id, tier=2))
    return store, d


def test_one_record_call_stamps_its_show_and_render_rows_with_one_session(tmp_path, monkeypatch):
    store, _d = _path_anchored_domain_store(tmp_path)
    calls = _key_that_moves(monkeypatch, "session-before", "session-after")
    server._drill_down_impl(store, None, "payments")
    (show,) = [e for e in store.retrieval_events() if e["kind"] == "show_anchor"]
    (render,) = store.render_events()
    assert show["session_id"] == render["session_id"] == "session-before"
    assert len(calls) == 1, "the key is read once per call, not once per journal write"
    store.close()


def test_a_failure_in_one_journal_write_still_costs_the_other_nothing(tmp_path, monkeypatch):
    """Sampling the key once must not couple the two writes: each keeps its own `suppress`."""
    from sidegraph.store import Store as _Store

    def boom(*_a, **_kw):
        raise RuntimeError("disk on fire")

    store, _d = _path_anchored_domain_store(tmp_path)
    monkeypatch.setattr(server, "_session_key", lambda _s: "sess-1")
    with monkeypatch.context() as m:
        m.setattr(_Store, "record_retrieval_events", boom)
        server._drill_down_impl(store, None, "payments")
    assert [e["session_id"] for e in store.render_events()] == ["sess-1"]
    assert [e for e in store.retrieval_events() if e["kind"] == "show_anchor"] == []

    with monkeypatch.context() as m:
        m.setattr(_Store, "record_render_event", boom)
        server._drill_down_impl(store, None, "payments")
    assert len(store.render_events()) == 1, "the second call wrote no render row"
    assert len([e for e in store.retrieval_events() if e["kind"] == "show_anchor"]) == 1
    store.close()
