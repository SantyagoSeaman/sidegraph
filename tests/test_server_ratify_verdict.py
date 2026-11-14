"""The ratify tool accepts a record, or drops one that is not proposed, only with the owner's
verdict, and stamps what it accepted as relayed by an agent. A proposed draft can be dropped
freely.
see design/superpowers/specs/2026-11-12-ratification-needs-a-human-verdict-design.md (D6-D9)"""

from __future__ import annotations

import asyncio

import fastmcp
import pytest

from sidegraph import server
from sidegraph.cli import ratify_main
from sidegraph.schema import DecisionStatus, DomainStatus
from sidegraph.store import Store


def call(name, arguments):
    async def run():
        async with fastmcp.Client(server.mcp) as client:
            return await client.call_tool(name, arguments, raise_on_error=False)

    return asyncio.run(run())


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "sidegraph.store._ratifier_identity", lambda actor=None: actor or "Test Person"
    )
    with Store(tmp_path / "store") as current:
        monkeypatch.setattr(server, "_store", current)
        monkeypatch.setattr(server, "_load_reader", lambda: None)
        yield current


def _proposal(store, title):
    draft = {"title": title, "kind": "adr", "context": "c", "choice": "ch", "anchors": []}
    return server._propose_decisions_impl(store, None, [draft])[0]["decision_id"]


def _canonical(store):
    return {str(p.relative_to(store.path)): p.read_bytes() for p in store.path.rglob("*.json")}


@pytest.mark.parametrize("arguments", [{}, {"verdict": "  "}, {"verdict": None}])
def test_mcp_ratify_accept_without_a_verdict_is_refused_and_writes_nothing(store, arguments):
    """Red against unfixed code: with no ``verdict`` argument the accept went through. The
    blank and null cases are red there only because the parameter did not exist."""
    first, second = _proposal(store, "First rule"), _proposal(store, "Second rule")
    before = _canonical(store)

    result = call("ratify", {"accept": [first], "drop": [second], **arguments})

    assert result.is_error
    assert "verdict" in str(result.content)
    assert _canonical(store) == before
    assert store.get_decision(first).status == DecisionStatus.PROPOSED
    assert store.get_decision(second).status == DecisionStatus.PROPOSED


def test_mcp_ratify_accept_with_a_verdict_stamps_agent_relayed(store):
    """Red against unfixed code: the stamp was the bare git name."""
    decision = _proposal(store, "First rule")
    fact = server._propose_decisions_impl(
        store,
        None,
        [],
        facts=[
            {
                "statement": "A retry takes 40 ms",
                "source": "benchmark",
                "anchors": [{"name": "Trader", "file_path": "trader/exec.py"}],
            }
        ],
    )[0]["fact_id"]
    domain = server._add_domain_impl(store, None, slug="payments", title="Payments", summary="s")[
        "domain_id"
    ]

    result = call("ratify", {"accept": [decision, fact, domain], "verdict": "accept all three"})

    assert not result.is_error
    assert all(v.startswith("accepted") for v in result.structured_content.values())
    assert store.get_decision(decision).ratified_by == "agent-relayed:Test Person"
    assert store.get_fact(fact).ratified_by == "agent-relayed:Test Person"
    assert store.get_domain(domain).ratified_by == "agent-relayed:Test Person"


def _accepted_domain(store):
    domain = server._add_domain_impl(store, None, slug="payments", title="Payments", summary="s")[
        "domain_id"
    ]
    store.ratify_domains(accept=[domain])
    return domain


def test_mcp_ratify_drops_a_proposed_draft_without_a_verdict(store):
    """Red against asking for a verdict on every call: an agent must be able to withdraw a
    draft, its own included. Passes on unfixed code."""
    pending = _proposal(store, "First rule")

    result = call("ratify", {"drop": [pending]})

    assert result.structured_content == {pending: "dropped"}
    assert store.get_decision(pending).ratified_by is None


def test_mcp_ratify_repeated_drop_of_a_settled_draft_needs_no_verdict(store):
    """Guard, passes on unfixed code: a drop that changes nothing is answered, not refused."""
    pending = _proposal(store, "First rule")
    call("ratify", {"drop": [pending]})

    result = call("ratify", {"drop": [pending]})

    assert result.structured_content == {pending: "already rejected"}


def test_mcp_ratify_cannot_drop_an_accepted_domain_without_a_verdict(store):
    """Red against unfixed code, and against asking for a verdict on accepts only: a domain
    drop also works on an accepted domain, so a free drop would retire what the owner
    accepted."""
    domain = _accepted_domain(store)
    before = _canonical(store)

    result = call("ratify", {"drop": [domain]})

    assert result.is_error
    assert "verdict" in str(result.content)
    assert _canonical(store) == before
    assert store.get_domain(domain).status == DomainStatus.ACCEPTED


def test_mcp_ratify_refuses_the_whole_drop_list_when_one_record_is_not_proposed(store):
    """Red against unfixed code, and against dropping the proposed part of a refused call:
    one call either passes the check or writes nothing."""
    domain = _accepted_domain(store)
    pending = _proposal(store, "First rule")
    before = _canonical(store)

    result = call("ratify", {"drop": [pending, domain]})

    assert result.is_error
    assert _canonical(store) == before
    assert store.get_decision(pending).status == DecisionStatus.PROPOSED
    assert store.get_domain(domain).status == DomainStatus.ACCEPTED


def test_mcp_ratify_drops_an_accepted_domain_with_a_verdict(store):
    """Over-reach guard: with the owner's words the drop works as before. Red against unfixed
    code only because the tool had no ``verdict`` parameter."""
    domain = _accepted_domain(store)

    result = call("ratify", {"drop": [domain], "verdict": "drop the payments domain"})

    assert result.structured_content == {domain: "dropped"}
    assert store.get_domain(domain).status == DomainStatus.DROPPED


def test_mcp_ratify_with_no_ids_needs_no_verdict(store):
    """Guard, passes on unfixed code: a call that names no record writes nothing and is not
    refused."""
    assert call("ratify", {}).structured_content == {}


def test_relayed_ratify_never_restamps_an_accepted_record(store):
    """Guard: an accepted record keeps the stamp it has. Red against unfixed code only because
    the tool had no ``verdict`` parameter."""
    settled = _proposal(store, "First rule")
    store.ratify(settled)

    result = call("ratify", {"accept": [settled], "verdict": "accept it"})

    assert result.structured_content == {settled: "already accepted"}
    assert store.get_decision(settled).ratified_by == "Test Person"


def test_cli_ratify_stamp_is_the_bare_name(store):
    """Red against stamping the command-line path as relayed; passes on unfixed code."""
    pending = _proposal(store, "First rule")

    assert ratify_main(["--db", str(store.path), "--accept", pending]) == 0

    with Store(store.path) as reopened:
        assert reopened.get_decision(pending).ratified_by == "Test Person"


def test_mcp_ratify_drop_of_an_already_dropped_domain_needs_no_verdict(store):
    """Guard for the rule that a drop which changes nothing is answered, not refused: the
    domain is already ``dropped``, so nothing is retired. Red against a check that treats
    every non-proposed domain as settled."""
    domain = _accepted_domain(store)
    assert not call("ratify", {"drop": [domain], "verdict": "drop it"}).is_error

    result = call("ratify", {"drop": [domain]})

    assert not result.is_error
    assert result.structured_content == {domain: "already dropped"}


def test_mcp_ratify_an_explicit_empty_accept_list_needs_no_verdict(store):
    """The shape a model often sends: ``accept=[]`` beside a draft to drop. Red against a
    check that asks for a verdict whenever ``accept`` is not ``None``."""
    pending = _proposal(store, "First rule")

    result = call("ratify", {"accept": [], "drop": [pending]})

    assert not result.is_error
    assert result.structured_content == {pending: "dropped"}


@pytest.mark.parametrize("bad", [0, ["a"], {"k": "v"}])
def test_mcp_ratify_a_wrong_type_for_verdict_names_the_field(store, bad):
    """Red against an allowlist of reportable field names that lacks ``verdict``: the error
    then said ``field: string_type``."""
    pending = _proposal(store, "First rule")

    result = call("ratify", {"accept": [pending], "verdict": bad})

    assert result.is_error
    assert "verdict: " in str(result.content)
    assert store.get_decision(pending).status == DecisionStatus.PROPOSED


def test_a_drop_with_no_verdict_does_not_overtake_a_second_writer_that_accepts_the_domain(
    store, monkeypatch
):
    """Red against unfixed code: the gate read the domain as proposed, another process accepted
    it, and the drop (decided on that earlier read) retired the accepted domain."""
    domain = server._add_domain_impl(store, None, slug="payments", title="Payments", summary="s")[
        "domain_id"
    ]
    other = Store(store.path)
    real_get_domain = store.get_domain
    calls: list[str] = []

    def get_domain_then_accept_elsewhere(domain_id):
        found = real_get_domain(domain_id)
        if not calls:  # right after the gate's own read
            calls.append(domain_id)
            other.refresh_if_stale()
            assert other.ratify_domains(accept=[domain_id], actor="Human")[domain_id] == "accepted"
        return found

    monkeypatch.setattr(store, "get_domain", get_domain_then_accept_elsewhere)
    try:
        out = server._ratify_with_verdict(store, None, [domain], None)
    finally:
        other.close()

    assert calls == [domain]
    assert out[domain].startswith("error") and "verdict" in out[domain]
    assert "accepted in the meantime" in out[domain]
    monkeypatch.undo()
    store.refresh_if_stale()
    assert store.get_domain(domain).status == DomainStatus.ACCEPTED


def test_a_verdictless_drop_of_a_proposed_domain_still_works(store):
    domain = server._add_domain_impl(store, None, slug="payments", title="Payments", summary="s")[
        "domain_id"
    ]

    out = server._ratify_with_verdict(store, None, [domain], None)

    assert out[domain] == "dropped"
    assert store.get_domain(domain).status == DomainStatus.DROPPED
