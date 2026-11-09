"""Per-agent MCP delivery state follows eligible bodies that survive the hard cap."""

from contextlib import contextmanager

import pytest

from sidegraph import config, server
from sidegraph.config import SessionHint
from tests.test_retrieval_sent_earlier import memory as memory


@contextmanager
def caller(session="s1", agent=None, exact=True):
    token = config.CURRENT_SESSION.set(
        SessionHint(session, "codex", agent=agent, agent_exact=exact)
    )
    try:
        yield
    finally:
        config.CURRENT_SESSION.reset(token)


def read(store, tool="context", budget=10000):
    entities = [{"name": "Seed", "file_path": "seed.py"}]
    if tool == "query":
        return server._query_decisions_impl(store, None, None, entities, budget)
    return server._get_task_context_impl(store, None, None, entities, 0, budget)


@pytest.mark.parametrize("tool", ["context", "query"])
def test_second_read_uses_backrefs_and_keeps_hint(memory, tool):
    store, _, records = memory
    with caller():
        first = read(store, tool)
        second = read(store, tool)
    assert "[sent earlier]" not in first
    for d in records:
        assert f"[sent earlier] {d.title} (id: {d.id})" in second
    assert second.count("[sent earlier] =") == 1
    assert "supersede_decision" in second
    assert set(store.meta_with_prefix("mcp_sent:s1:-:")) == {
        f"mcp_sent:s1:-:{d.id}" for d in records
    }


def test_second_read_uses_freed_budget(memory):
    store, _, records = memory
    with caller():
        first = read(store, budget=1100)
        second = read(store, budget=1100)
    assert any(d.id in second and d.id not in first for d in records)


@pytest.mark.parametrize(
    "hint",
    [
        None,
        SessionHint("s1", "claude"),
        SessionHint(None, "codex", agent_exact=True),
        SessionHint("", "codex", agent_exact=True),
    ],
)
def test_inexact_or_missing_session_preserves_answers(memory, hint):
    store, _, _ = memory
    token = config.CURRENT_SESSION.set(None)
    try:
        original = read(store)
        config.CURRENT_SESSION.set(hint)
        assert read(store) == original
        assert read(store) == original
        assert not store.meta_with_prefix("mcp_sent:")
    finally:
        config.CURRENT_SESSION.reset(token)


def test_agents_in_one_session_receive_their_own_bodies(memory):
    store, _, _ = memory
    with caller(agent="a1"):
        first = read(store)
        assert "[sent earlier]" in read(store)
    with caller(agent="a2"):
        assert read(store) == first


def test_telemetry_off_still_claims_and_backreferences(memory, monkeypatch):
    store, _, _ = memory
    monkeypatch.setenv("SIDEGRAPH_TELEMETRY", "off")
    with caller():
        read(store)
        assert "[sent earlier]" in read(store)
    assert store.meta_with_prefix("mcp_sent:")


def test_hard_cap_only_claims_surviving_bodies(memory, monkeypatch):
    store, _, records = memory
    hard_cap = server._hard_cap

    def cap(*args, **kwargs):
        return hard_cap(*args, **kwargs, cap=1500)

    with caller():
        with monkeypatch.context() as patch:
            patch.setattr(server, "_hard_cap", cap)
            first = read(store)
        sent = store.meta_with_prefix("mcp_sent:s1:-:")
        assert sent and len(sent) < len(records)
        assert set(sent) == {f"mcp_sent:s1:-:{d.id}" for d in records if d.id in first}
        second = read(store)
    for d in records:
        if d.id not in first:
            assert f"{d.title}: Choose this" in second


@pytest.mark.parametrize("failure", ["read", "claim"])
@pytest.mark.parametrize("tool", ["context", "query"])
def test_sent_state_failure_renders_original_and_records_once(memory, monkeypatch, failure, tool):
    store, _, records = memory
    original = read(store, tool)
    key = f"mcp_sent:s1:-:{records[0].id}"
    store.set_meta(key, "prior")
    recorded = []

    def fail(*args, **kwargs):
        raise RuntimeError("injected sent-state failure")

    monkeypatch.setattr(server, "_record", lambda *args, **kwargs: recorded.append(kwargs["ctx"]))
    with caller(), monkeypatch.context() as patch:
        patch.setattr(store, "meta_with_prefix" if failure == "read" else "claim_meta_many", fail)
        assert read(store, tool) == original
    assert store.meta_with_prefix("mcp_sent:") == {key: "prior"}
    assert len(recorded) == 1
    assert not recorded[0].backref_ids


def test_nearest_only_backrefs_and_cap_keep_one_legend(tmp_path, monkeypatch):
    from tests.test_server_nearest_anchored import _area

    repo = _area(tmp_path)
    with caller():
        first = repo.call(files=["core/a.py"], structure_budget=120)
        second = repo.call(files=["core/a.py"], structure_budget=120)
        assert "## Nearest anchored records" in first
        assert "[sent earlier]" in second
        assert second.count("[sent earlier] =") == 1
        hard_cap = server._hard_cap

        def cap(*args, **kwargs):
            return hard_cap(*args, **kwargs, cap=len(second) - 100)

        with monkeypatch.context() as patch:
            patch.setattr(server, "_hard_cap", cap)
            cut = repo.call(files=["core/a.py"], structure_budget=120)
    assert "[output cut" in cut
    assert " [sent earlier] " in cut
    assert cut.count("[sent earlier] =") == 1


def test_capped_non_drifted_backrefs_keep_exactly_one_legend(memory, monkeypatch):
    store, _, _ = memory
    with caller():
        read(store)
        second = read(store)
        assert second.count("[sent earlier] =") == 1
        hard_cap = server._hard_cap

        def cap(*args, **kwargs):
            return hard_cap(*args, **kwargs, cap=len(second) - 80)

        monkeypatch.setattr(server, "_hard_cap", cap)
        cut = read(store)
    assert "[output cut" in cut
    assert " [sent earlier] " in cut
    assert cut.count("[sent earlier] =") == 1


def listing(monkeypatch, store, **kwargs):
    import json

    monkeypatch.setattr(server, "_get_store", lambda: store)
    return json.loads(server.retrieve_decisions(**kwargs).content[0].text)


def test_listing_rows_and_explicit_ids_escape(memory, monkeypatch):
    store, _, records = memory
    with caller():
        read(store)
        answer = listing(monkeypatch, store, status="accepted")
        assert all(row.get("sent_earlier") for row in answer["decisions"])
        assert all(
            set(row) == set(server._ROW_KEYS) | {"sent_earlier"} for row in answer["decisions"]
        )
        assert "5 records were sent to you earlier" in answer["hint"]
        full = listing(monkeypatch, store, ids=[records[0].id])["decisions"][0]
    assert full["choice"] == records[0].choice and "sent_earlier" not in full


def test_listing_full_records_are_backreferenced_by_context(memory, monkeypatch):
    store, _, records = memory
    with caller():
        full = listing(monkeypatch, store, query="Record", limit=1)
        assert len(full["decisions"]) == 1
        ctx = read(store)
    row = full["decisions"][0]
    assert f"[sent earlier] {row['title']} (id: {row['id']})" in ctx
    for d in records:
        if d.id != row["id"]:
            assert f"{d.title}: Choose this" in ctx


@pytest.mark.parametrize("limit,budget", [(1, 24000), (100, 4000)])
def test_listing_hint_counts_only_returned_marked_rows(memory, monkeypatch, limit, budget):
    from tests.test_server_decision_listing import _add

    store, _, _ = memory
    sent = [_add(store, f"Marked {i}") for i in range(100)]
    for d in sent:
        store.set_meta(f"mcp_sent:s1:-:{d.id}", "prior")
    with caller():
        answer = listing(monkeypatch, store, query="Marked", limit=limit, budget_chars=budget)
    count = len(answer["decisions"])
    assert 0 < count < 100
    assert all(row.get("sent_earlier") for row in answer["decisions"])
    assert f"{count} records were sent to you earlier" in answer["hint"]
    assert len(server._dump(answer)) <= budget


def test_proposals_are_never_claimed_or_backreferenced(memory, monkeypatch):
    from sidegraph.schema import AnchorBinding
    from tests.test_server_decision_listing import _add

    store, entity, _ = memory
    d = _add(store, "Proposed pointer guard", status="proposed", choice="proposed full choice")
    store.add_binding(
        AnchorBinding(record_id=d.id, entity_id=entity.entity_id, tier=2, status="live")
    )
    with caller():
        for kwargs in ({"status": "proposed"}, {"ids": [d.id]}):
            row = listing(monkeypatch, store, **kwargs)["decisions"][0]
            assert row["choice"] == d.choice and "sent_earlier" not in row
        read(store)
        text = read(store)
    assert d.choice in text
    assert store.get_meta(f"mcp_sent:s1:-:{d.id}") is None


def test_real_backrefs_and_listing_rows_are_not_echo_placements(memory, monkeypatch):
    from sidegraph.stats.echo import Delivery, placements

    store, _, records = memory
    known = frozenset(d.id for d in records)
    with caller():
        read(store)
        text = read(store)
        rows = listing(monkeypatch, store, status="accepted")
    assert " [sent earlier] " in text
    assert all(row.get("sent_earlier") for row in rows["decisions"])
    assert placements(Delivery("mcp", 0, text, tool="get_task_context"), known) == []
    assert (
        placements(Delivery("mcp", 0, server._dump(rows), tool="retrieve_decisions"), known) == []
    )


def test_degraded_related_and_fact_placements_do_not_claim_bodies(memory):
    from sidegraph.schema import AnchorBinding, Fact

    store, entity, records = memory
    fact = store.add_fact(
        Fact(
            statement="Standalone fact",
            source="measurement",
            status="accepted",
            provenance=records[0].provenance,
            valid_from=records[0].valid_from,
        )
    )
    store.add_binding(
        AnchorBinding(record_id=fact.id, entity_id=entity.entity_id, tier=2, status="live")
    )
    # A second entity is peripheral only in the core test; the global scope is the server route.
    from sidegraph.schema import Decision

    related = store.add_decision(
        Decision(
            title="Global related body",
            kind="adr",
            scope="global",
            context="Context",
            choice="Related full choice",
            status="accepted",
            provenance=records[0].provenance,
            valid_from=records[0].valid_from,
        )
    )
    with caller():
        small = read(store, budget=330)
        assert records[0].id in small
        assert not store.meta_with_prefix("mcp_sent:")
        read(store)
        sent = store.meta_with_prefix("mcp_sent:s1:-:")
        assert set(sent) == {f"mcp_sent:s1:-:{d.id}" for d in records}
        again = read(store)
    assert "fact: Standalone fact" in again
    assert f"{related.title}: Related full choice" in again
    assert f"[sent earlier] {related.title}" not in again


@pytest.mark.parametrize("tool", ["context", "query", "listing"])
def test_empty_claim_keeps_backrefs_while_another_writer_is_held(memory, monkeypatch, tool):
    from sidegraph.store import Store

    store, _, _ = memory
    monkeypatch.setenv("SIDEGRAPH_TELEMETRY", "off")
    with caller():
        read(store)
        store._conn.execute("PRAGMA busy_timeout=1")
        with Store(store.path) as writer:
            writer._conn.execute("BEGIN IMMEDIATE")
            try:
                if tool == "listing":
                    answer = listing(monkeypatch, store, status="accepted")
                else:
                    answer = read(store, tool)
            finally:
                writer._conn.rollback()
    if tool == "listing":
        assert all(row.get("sent_earlier") for row in answer["decisions"])
    else:
        assert " [sent earlier] " in answer
        assert "Choose this" not in answer
