"""The ratifier stamp says which path accepted a record: a verdict relayed by an agent is
stamped ``agent-relayed:<name>``, never the bare name.
see design/superpowers/specs/2026-11-12-ratification-needs-a-human-verdict-design.md (D7)"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from sidegraph import server
from sidegraph.schema import DecisionStatus, DomainStatus
from sidegraph.store import Store


@dataclass
class Pending:
    decision: str
    attached_fact: str
    fact: str
    domain: str


def _patch_identity(monkeypatch, name):
    # Keeps an explicit actor's pass-through, as the real function does.
    monkeypatch.setattr("sidegraph.store._ratifier_identity", lambda actor=None: actor or name)


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A store holding one pending record of each kind, and a fact attached to the decision."""
    _patch_identity(monkeypatch, "Test Person")
    with Store(tmp_path / "store") as store:
        results = server._propose_decisions_impl(
            store,
            None,
            [
                {
                    "title": "Batch limit is 250",
                    "kind": "adr",
                    "context": "c",
                    "choice": "ch",
                    "anchors": [],
                    "facts": [{"statement": "The API caps a batch at 250", "source": "docs"}],
                }
            ],
            facts=[
                {
                    "statement": "A retry takes 40 ms",
                    "source": "benchmark",
                    "anchors": [{"name": "Trader", "file_path": "trader/exec.py"}],
                }
            ],
        )
        domain = server._add_domain_impl(
            store, None, slug="payments", title="Payments", summary="s"
        )
        yield (
            store,
            Pending(
                decision=results[0]["decision_id"],
                attached_fact=results[0]["facts"][0]["fact_id"],
                fact=results[1]["fact_id"],
                domain=domain["domain_id"],
            ),
        )


def test_relayed_ratify_stamps_agent_relayed_for_every_kind(world):
    """Red against unfixed code: the stamp was the bare git name."""
    store, ids = world

    _decision, cascaded = store.ratify(ids.decision, relayed=True)
    store.ratify_fact(ids.fact, relayed=True)
    outcome = store.ratify_domains(accept=[ids.domain], relayed=True)

    assert [f.id for f in cascaded] == [ids.attached_fact]
    assert outcome[ids.domain].startswith("accepted")
    assert store.get_decision(ids.decision).ratified_by == "agent-relayed:Test Person"
    assert store.get_fact(ids.attached_fact).ratified_by == "agent-relayed:Test Person"
    assert store.get_fact(ids.fact).ratified_by == "agent-relayed:Test Person"
    assert store.get_domain(ids.domain).ratified_by == "agent-relayed:Test Person"


def test_relayed_ratify_without_a_git_name_stamps_agent_relayed(world, monkeypatch):
    """Red against stamping None when git gives no name: that an agent relayed the verdict
    is known, so it is recorded."""
    store, ids = world
    _patch_identity(monkeypatch, None)

    store.ratify(ids.decision, relayed=True)

    assert store.get_decision(ids.decision).ratified_by == "agent-relayed"


def test_relayed_with_an_explicit_actor_is_refused_and_writes_nothing(world):
    """Red against composing both into one stamp: the two are exclusive."""
    store, ids = world

    with pytest.raises(ValueError, match="exclusive"):
        store.ratify(ids.decision, actor="auto:auto-all", relayed=True)
    with pytest.raises(ValueError, match="exclusive"):
        store.ratify_fact(ids.fact, actor="auto:auto-all", relayed=True)
    with pytest.raises(ValueError, match="exclusive"):
        store.ratify_domains(accept=[ids.domain], actor="auto:auto-all", relayed=True)

    assert store.get_decision(ids.decision).status == DecisionStatus.PROPOSED
    assert store.get_fact(ids.fact).status == DecisionStatus.PROPOSED
    assert store.get_domain(ids.domain).status == DomainStatus.PROPOSED


def test_unrelayed_ratify_keeps_the_bare_name(world):
    """Over-reach guard, red against nothing: the command-line path stamps the name alone."""
    store, ids = world

    store.ratify(ids.decision)
    store.ratify_fact(ids.fact)
    store.ratify_domains(accept=[ids.domain])

    assert store.get_decision(ids.decision).ratified_by == "Test Person"
    assert store.get_fact(ids.fact).ratified_by == "Test Person"
    assert store.get_domain(ids.domain).ratified_by == "Test Person"
