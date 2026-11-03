"""One rule: a predecessor closes when its successor becomes accepted.

A proposed successor leaves its still-open predecessor alone under EVERY ratify policy; the
close happens at ratification, and dropping the proposal changes nothing about the predecessor.
# see docs/concepts/data-model.md
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sidegraph.capture import RatifyPolicy, format_proposal, propose
from sidegraph.engine.reader import GraphifyReader
from sidegraph.retrieval import Seed, get_task_context
from sidegraph.schema import (
    AnchorBinding,
    Decision,
    DecisionKind,
    DecisionStatus,
    Descriptor,
    Domain,
    DomainStatus,
    Entity,
    Provenance,
)
from sidegraph.server import _ratify_impl
from sidegraph.store import Store, SupersessionConflictError
from sidegraph.verify import verify_snapshot

FIXTURE = Path(__file__).parent / "fixtures" / "mini_graph.json"


def _accepted_predecessor(tmp_path):
    store = Store(tmp_path / "store")
    reader = GraphifyReader(FIXTURE)
    entity = store.upsert_entity(
        Entity(
            canonical_name="Trader",
            descriptor=Descriptor(name="Trader", file_path="trader/exec.py"),
        )
    )
    old = store.add_decision(
        Decision(
            title="old way",
            kind=DecisionKind.ADR,
            status=DecisionStatus.ACCEPTED,
            context="c",
            choice="x",
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual"),
        )
    )
    store.add_binding(
        AnchorBinding(record_id=old.id, entity_id=entity.entity_id, tier=2, status="live")
    )
    return store, reader, old


def _propose_successor(store, reader, old):
    [res] = propose(
        [
            {
                "title": "new way",
                "kind": "adr",
                "context": "c2",
                "choice": "y",
                "supersedes": old.id,
                "anchors": [{"name": "Trader", "file_path": "trader/exec.py"}],
            }
        ],
        store,
        reader,
        ratify_policy=RatifyPolicy.MANUAL,
    )
    assert res.status == "written"
    return res.decision_id


def test_manual_proposal_leaves_predecessor_accepted_and_open(tmp_path):
    store, reader, old = _accepted_predecessor(tmp_path)
    succ = _propose_successor(store, reader, old)
    pred = store.get_decision(old.id)
    assert pred.status == DecisionStatus.ACCEPTED and pred.valid_to is None
    assert store.get_decision(succ).status == DecisionStatus.PROPOSED
    assert verify_snapshot(tmp_path / "store") == []


def test_dropping_the_proposal_leaves_predecessor_live_and_retrievable(tmp_path):
    store, reader, old = _accepted_predecessor(tmp_path)
    succ = _propose_successor(store, reader, old)
    store.drop(succ)
    pred = store.get_decision(old.id)
    assert pred.status == DecisionStatus.ACCEPTED and pred.valid_to is None
    ctx = get_task_context([Seed(file_path="trader/exec.py")], store, reader)
    assert old.id in ctx.shown_ids


def test_pending_proposal_shows_predecessor_as_current(tmp_path):
    store, reader, old = _accepted_predecessor(tmp_path)
    _propose_successor(store, reader, old)
    ctx = get_task_context([Seed(file_path="trader/exec.py")], store, reader)
    assert any(old.id in line for line in ctx.decisions)
    assert not any("tried, reverted" in line for line in ctx.mistakes + ctx.decisions + ctx.related)


def test_ratifying_the_proposal_closes_predecessor_at_ratification_time(tmp_path):
    store, reader, old = _accepted_predecessor(tmp_path)
    succ = _propose_successor(store, reader, old)
    before = datetime.now(UTC)
    store.ratify(succ)
    pred = store.get_decision(old.id)
    assert pred.status == DecisionStatus.SUPERSEDED
    assert pred.valid_to is not None and pred.valid_to >= before
    assert pred.valid_to >= pred.valid_from
    assert verify_snapshot(tmp_path / "store") == []


def test_direct_accepted_write_still_closes_at_successor_valid_from(tmp_path):
    store, _, old = _accepted_predecessor(tmp_path)
    when = old.valid_from + timedelta(days=3)
    new = store.add_decision(
        Decision(
            title="direct",
            kind=DecisionKind.ADR,
            status=DecisionStatus.ACCEPTED,
            context="c",
            choice="z",
            valid_from=when,
            supersedes=old.id,
            provenance=Provenance(source="manual"),
        )
    )
    pred = store.get_decision(old.id)
    assert pred.status == DecisionStatus.SUPERSEDED
    assert pred.valid_to == new.valid_from


def _proposal(store, old_id, title="next"):
    return store.add_decision(
        Decision(
            title=title,
            kind=DecisionKind.ADR,
            status=DecisionStatus.PROPOSED,
            context="c",
            choice="y",
            supersedes=old_id,
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual"),
        )
    )


def test_chain_proposal_is_refused_at_write(tmp_path):
    store, _, a = _accepted_predecessor(tmp_path)
    b = _proposal(store, a.id, "B")
    with pytest.raises(SupersessionConflictError, match=f"{b.id} is a pending proposal"):
        _proposal(store, b.id, "C")
    assert store.get_decision(a.id).status == DecisionStatus.ACCEPTED


def test_sibling_ratify_via_mcp_surfaces_the_refusal_message(tmp_path):
    # A merge can bring in a second pending successor; write it the way a merge would.
    store, _, a = _accepted_predecessor(tmp_path)
    p1 = _proposal(store, a.id, "P1")
    p2 = Decision(
        title="P2",
        kind=DecisionKind.ADR,
        status=DecisionStatus.PROPOSED,
        context="c",
        choice="y",
        supersedes=a.id,
        valid_from=datetime.now(UTC),
        provenance=Provenance(source="manual"),
    )
    with store._mutation():
        store._write_decision(p2)
    assert _ratify_impl(store, accept=[p1.id])[p1.id] == "accepted"
    out = _ratify_impl(store, accept=[p2.id])[p2.id]
    assert f"{a.id} is already superseded by {p1.id}; supersede that one instead" in out


def test_format_proposal_names_what_ratifying_closes(tmp_path):
    store, _, a = _accepted_predecessor(tmp_path)
    p = _proposal(store, a.id)
    assert f"  supersedes: {a.id} old way" in format_proposal(p, a)
    assert f"  supersedes: {a.id}\n" in format_proposal(p) + "\n"
    assert "supersedes" not in format_proposal(a)


def _domain_pair(store):
    old = store.add_domain(
        Domain(
            slug="pay",
            title="Pay",
            summary="s",
            status=DomainStatus.ACCEPTED,
            provenance=Provenance(source="manual"),
        )
    )
    new = store.supersede_domain(
        old.domain_id,
        Domain(
            slug="pay",
            title="Payments",
            summary="s2",
            supersedes=old.domain_id,
            provenance=Provenance(source="manual"),
        ),
    )
    return old, new


def test_domain_supersede_proposal_leaves_old_live_when_dropped(tmp_path):
    store = Store(tmp_path / "store")
    old, new = _domain_pair(store)
    assert store.get_domain(old.domain_id).status == DomainStatus.ACCEPTED
    store.ratify_domains(drop=[new.domain_id])
    assert store.get_domain(old.domain_id).status == DomainStatus.ACCEPTED


def test_domain_supersede_proposal_closes_old_when_accepted(tmp_path):
    store = Store(tmp_path / "store")
    old, new = _domain_pair(store)
    assert store.ratify_domains(accept=[new.domain_id]) == {new.domain_id: "accepted"}
    assert store.get_domain(old.domain_id).status == DomainStatus.SUPERSEDED
    assert store.get_domain(new.domain_id).status == DomainStatus.ACCEPTED
