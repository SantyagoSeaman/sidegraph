"""An agent's supersede_decision waits for a verdict: the successor lands proposed and the
predecessor stays accepted until that successor is ratified.
see design/superpowers/specs/2026-11-12-ratification-needs-a-human-verdict-design.md (D1-D3, D5)"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from sidegraph.engine.reader import GraphifyReader
from sidegraph.retrieval import Seed, get_task_context
from sidegraph.schema import DecisionStatus
from sidegraph.server import (
    _add_decision_impl,
    _list_proposed_impl,
    _ratify_impl,
    _supersede_decision_impl,
)
from sidegraph.store import Store, SupersessionConflictError
from sidegraph.verify import verify_snapshot

FIXTURE = Path(__file__).parent / "fixtures" / "mini_graph.json"
ANCHORS = [{"name": "Trader", "file_path": "trader/exec.py"}]


@pytest.fixture
def world(tmp_path):
    """An accepted, anchored predecessor in a fresh store, with the fixture graph."""
    reader = GraphifyReader(FIXTURE)
    with Store(tmp_path / "store") as store:
        old = _add_decision_impl(
            store,
            reader,
            title="Batch limit is 100",
            kind="constraint",
            context="c",
            choice="ch",
            anchors=ANCHORS,
        )
        yield store, reader, old


def _succeed(store, reader, old_id, **kwargs):
    return _supersede_decision_impl(
        store,
        reader,
        old_id,
        title="Batch limit is 250",
        kind="constraint",
        context="c2",
        choice="ch2",
        **kwargs,
    )


def test_agent_supersede_lands_proposed_and_leaves_the_predecessor_live(world, tmp_path):
    """Red against unfixed code: the successor landed accepted and closed the predecessor."""
    store, reader, old = world
    out = _succeed(store, reader, old["id"], source="agent")

    assert out["status"] == "proposed"
    assert store.get_decision(out["id"]).status == DecisionStatus.PROPOSED
    predecessor = store.get_decision(old["id"])
    assert predecessor.status == DecisionStatus.ACCEPTED and predecessor.valid_to is None
    queue = _list_proposed_impl(store)
    assert out["id"] in queue and f"supersedes: {old['id']}" in queue
    assert out["bindings"] == old["bindings"]
    assert verify_snapshot(tmp_path / "store") == []


def test_supersede_with_source_omitted_is_gated(world):
    """Red against unfixed code, and against keeping "human" as the default: an omitted
    parameter must never accept."""
    store, reader, old = world
    out = _succeed(store, reader, old["id"])

    successor = store.get_decision(out["id"])
    assert successor.status == DecisionStatus.PROPOSED
    assert successor.provenance.source == "agent"
    assert store.get_decision(old["id"]).status == DecisionStatus.ACCEPTED


@pytest.mark.parametrize("source", ["Human", "owner", "manual"])
def test_a_source_other_than_the_exact_value_human_is_gated(world, source):
    """Red against unfixed code: any source landed accepted."""
    store, reader, old = world
    out = _succeed(store, reader, old["id"], source=source)

    assert out["status"] == "proposed"
    assert store.get_decision(old["id"]).status == DecisionStatus.ACCEPTED


def test_source_human_still_lands_accepted_and_closes_the_predecessor(world):
    """Over-reach guard: the owner-asked path keeps its stored behaviour. Red against unfixed
    code only through the new result key ``status``."""
    store, reader, old = world
    out = _succeed(store, reader, old["id"], source="human")

    assert out["status"] == "accepted"
    assert store.get_decision(out["id"]).status == DecisionStatus.ACCEPTED
    predecessor = store.get_decision(old["id"])
    assert predecessor.status == DecisionStatus.SUPERSEDED and predecessor.valid_to is not None


def test_ratifying_an_agent_successor_closes_the_predecessor(world):
    """Red against unfixed code: the successor was already accepted, so ratify answered
    "already accepted"."""
    store, reader, old = world
    out = _succeed(store, reader, old["id"], source="agent")
    before = datetime.now(UTC)

    assert _ratify_impl(store, accept=[out["id"]]) == {out["id"]: "accepted"}

    assert store.get_decision(out["id"]).status == DecisionStatus.ACCEPTED
    predecessor = store.get_decision(old["id"])
    assert predecessor.status == DecisionStatus.SUPERSEDED
    assert predecessor.valid_to is not None and predecessor.valid_to >= before


def test_dropping_an_agent_successor_leaves_the_predecessor_live(world):
    """Red against unfixed code: the drop of an accepted successor was refused."""
    store, reader, old = world
    out = _succeed(store, reader, old["id"], source="agent")

    assert _ratify_impl(store, drop=[out["id"]]) == {out["id"]: "dropped"}

    assert store.get_decision(out["id"]).status == DecisionStatus.REJECTED
    predecessor = store.get_decision(old["id"])
    assert predecessor.status == DecisionStatus.ACCEPTED and predecessor.valid_to is None
    context = get_task_context([Seed(file_path="trader/exec.py")], store, reader)
    assert old["id"] in context.shown_ids


def test_second_agent_supersede_is_refused_while_one_is_pending(world):
    """Red against unfixed code: there the first call closed the predecessor, and the second
    was refused as "no longer current"."""
    store, reader, old = world
    _succeed(store, reader, old["id"], source="agent")

    with pytest.raises(SupersessionConflictError, match="already has a pending successor") as e:
        _succeed(store, reader, old["id"], source="agent")
    # The refusal says what to do: the successor waits for the owner, it is not ours to drop.
    assert "Tell the owner" in str(e.value) and "Do not drop it yourself" in str(e.value)


def test_a_successor_that_keeps_its_predecessors_title_is_written(world):
    """Red against routing the tool through the draft pipeline, which dedups a same-title
    successor against the record it supersedes and writes nothing (and red against unfixed
    code through the landing status)."""
    store, reader, old = world
    out = _supersede_decision_impl(
        store,
        reader,
        old["id"],
        title="Batch limit is 100",
        kind="constraint",
        context="c2",
        choice="the limit is now 250",
    )

    assert out["status"] == "proposed"
    assert store.get_decision(out["id"]).supersedes == old["id"]


def test_a_dropped_successor_can_be_replaced_on_the_same_predecessor(world):
    """The "fix first" route for a queued successor: drop it, then supersede the same
    predecessor again with the corrected text. Red against unfixed code: there the first
    successor was accepted, so it could not be dropped."""
    store, reader, old = world
    first = _succeed(store, reader, old["id"])
    assert _ratify_impl(store, drop=[first["id"]]) == {first["id"]: "dropped"}

    second = _supersede_decision_impl(
        store,
        reader,
        old["id"],
        title="Batch limit is 250",
        kind="constraint",
        context="c2",
        choice="corrected: 250 per tenant",
    )

    assert second["status"] == "proposed"
    assert store.get_decision(old["id"]).status == DecisionStatus.ACCEPTED
    assert _ratify_impl(store, accept=[second["id"]]) == {second["id"]: "accepted"}
    assert store.get_decision(old["id"]).status == DecisionStatus.SUPERSEDED
    assert store.get_decision(second["id"]).supersedes == old["id"]
