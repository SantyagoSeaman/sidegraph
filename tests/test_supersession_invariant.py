"""The write-time supersession invariant (decisions, facts, domains).

A. only the live head (accepted, open) can be superseded; B. at most one pending successor per
record; D. ratify refuses a successor whose predecessor is no longer the live head (the
cross-branch merge case); E. a dropped domain stays supersedable; F. an accepted domain plus
its single pending same-slug revision is no slug conflict.
# see docs/concepts/data-model.md
"""

import json
from datetime import UTC, datetime

import pytest

from sidegraph.capture import RatifyPolicy, _auto_ratify, propose
from sidegraph.cli import sync_main
from sidegraph.schema import (
    Decision,
    DecisionKind,
    DecisionStatus,
    Domain,
    DomainStatus,
    Fact,
    Provenance,
)
from sidegraph.server import _supersede_decision_impl, _supersede_fact_impl
from sidegraph.store import Store, SupersessionConflictError


def _prov():
    return Provenance(source="manual")


def _decision(title, status=DecisionStatus.ACCEPTED, supersedes=None):
    return Decision(
        title=title,
        kind=DecisionKind.ADR,
        status=status,
        context="c",
        choice=title,
        supersedes=supersedes,
        valid_from=datetime.now(UTC),
        provenance=_prov(),
    )


def _fact(statement, status=DecisionStatus.ACCEPTED, supersedes=None):
    return Fact(
        statement=statement,
        source="trial",
        status=status,
        supersedes=supersedes,
        valid_from=datetime.now(UTC),
        provenance=_prov(),
    )


def _domain(slug, status=DomainStatus.ACCEPTED, supersedes=None, title=None):
    return Domain(
        slug=slug,
        title=title or slug,
        summary="s",
        status=status,
        supersedes=supersedes,
        provenance=_prov(),
    )


# -- decisions ---------------------------------------------------------------------------


def test_superseding_a_proposed_decision_is_refused_and_writes_nothing(tmp_path):
    s = Store(tmp_path / "s")
    a = s.add_decision(_decision("A"))
    b = s.add_decision(_decision("B", DecisionStatus.PROPOSED, a.id))
    before = {d.id for d in s.iter_decisions()}
    with pytest.raises(SupersessionConflictError, match=f"{b.id} is a pending proposal"):
        s.add_decision(_decision("C", DecisionStatus.PROPOSED, b.id))
    with pytest.raises(SupersessionConflictError, match="pending proposal"):
        s.add_decision(_decision("C2", DecisionStatus.ACCEPTED, b.id))
    assert {d.id for d in s.iter_decisions()} == before


def test_second_pending_successor_is_refused(tmp_path):
    s = Store(tmp_path / "s")
    a = s.add_decision(_decision("A"))
    p1 = s.add_decision(_decision("P1", DecisionStatus.PROPOSED, a.id))
    with pytest.raises(SupersessionConflictError, match=f"already has a pending successor {p1.id}"):
        s.add_decision(_decision("P2", DecisionStatus.PROPOSED, a.id))
    s.drop(p1.id)  # a dropped proposal frees the slot
    s.add_decision(_decision("P2", DecisionStatus.PROPOSED, a.id))


def test_superseding_a_closed_decision_names_its_live_head(tmp_path):
    s = Store(tmp_path / "s")
    a = s.add_decision(_decision("A"))
    b = s.add_decision(_decision("B", DecisionStatus.ACCEPTED, a.id))
    with pytest.raises(SupersessionConflictError, match=f"supersede its live successor {b.id}"):
        s.add_decision(_decision("C", DecisionStatus.PROPOSED, a.id))


def test_mcp_supersede_decision_of_a_pending_proposal_is_refused(tmp_path):
    s = Store(tmp_path / "s")
    a = s.add_decision(_decision("A"))
    b = s.add_decision(_decision("B", DecisionStatus.PROPOSED, a.id))
    with pytest.raises(SupersessionConflictError, match="pending proposal"):
        _supersede_decision_impl(s, None, b.id, "C", "adr", "c", "x")
    assert s.get_decision(a.id).status == DecisionStatus.ACCEPTED
    assert s.get_decision(b.id).status == DecisionStatus.PROPOSED


def test_propose_second_superseding_draft_is_rejected_with_the_reason(tmp_path):
    s = Store(tmp_path / "s")
    a = s.add_decision(_decision("A"))
    draft = dict(kind="adr", context="c", choice="y", supersedes=a.id, anchors=[])
    [first] = propose([dict(draft, title="one")], s, None)
    [second] = propose([dict(draft, title="two")], s, None)
    assert first.status == "written"
    assert second.status == "rejected"
    assert f"already has a pending successor {first.decision_id}" in second.reason


def test_ratify_refuses_the_second_of_two_pending_successors_from_a_merge(tmp_path):
    # Two branches each added one pending successor; the merged store holds both files.
    s = Store(tmp_path / "s")
    a = s.add_decision(_decision("A"))
    p1, p2 = (_decision(t, DecisionStatus.PROPOSED, a.id) for t in ("P1", "P2"))
    with s._mutation():
        s._write_decision(p1)
    with s._mutation():
        s._write_decision(p2)
    s.ratify(p1.id)
    with pytest.raises(SupersessionConflictError, match=f"{a.id} is already superseded by {p1.id}"):
        s.ratify(p2.id)
    assert s.get_decision(p2.id).status == DecisionStatus.PROPOSED


def test_auto_ratify_surfaces_the_refusal_text(tmp_path):
    s = Store(tmp_path / "s")
    a = s.add_decision(_decision("A"))
    p1, p2 = (_decision(t, DecisionStatus.PROPOSED, a.id) for t in ("P1", "P2"))
    with s._mutation():
        s._write_decision(p1)
    with s._mutation():
        s._write_decision(p2)
    s.ratify(p1.id)
    outcome = _auto_ratify(s, p2.id, "adr", RatifyPolicy.AUTO_ALL)
    assert outcome.ratified_by is None
    assert f"{a.id} is already superseded by {p1.id}" in (outcome.error or "")


# -- facts -------------------------------------------------------------------------------


def test_fact_chain_second_pending_and_ratify_close(tmp_path):
    s = Store(tmp_path / "s")
    support = s.add_decision(_decision("D"))
    a = s.add_fact(_fact("A"))
    b = s.add_fact(_fact("B", DecisionStatus.PROPOSED, a.id))
    assert s.get_fact(a.id).status == DecisionStatus.ACCEPTED  # a proposal closes nothing
    with pytest.raises(SupersessionConflictError, match=f"{b.id} is a pending proposal"):
        s.add_fact(_fact("C", DecisionStatus.PROPOSED, b.id))
    with pytest.raises(SupersessionConflictError, match="already has a pending successor"):
        s.add_fact(_fact("B2", DecisionStatus.PROPOSED, a.id))
    with pytest.raises(SupersessionConflictError, match="pending proposal"):
        _supersede_fact_impl(s, None, b.id, "C", "trial", supports=[support.id])
    s.ratify_fact(b.id)
    closed = s.get_fact(a.id)
    assert closed.status == DecisionStatus.SUPERSEDED and closed.valid_to is not None


def test_dropped_fact_proposal_leaves_predecessor_open(tmp_path):
    s = Store(tmp_path / "s")
    a = s.add_fact(_fact("A"))
    b = s.add_fact(_fact("B", DecisionStatus.PROPOSED, a.id))
    s.drop_fact(b.id)
    assert s.get_fact(a.id).status == DecisionStatus.ACCEPTED
    assert s.get_fact(a.id).valid_to is None


# -- domains -----------------------------------------------------------------------------


def test_superseding_a_proposed_domain_is_refused(tmp_path):
    s = Store(tmp_path / "s")
    a = s.add_domain(_domain("pay"))
    b = s.supersede_domain(a.domain_id, _domain("pay", DomainStatus.PROPOSED, a.domain_id))
    with pytest.raises(SupersessionConflictError, match="pending proposal"):
        s.supersede_domain(b.domain_id, _domain("pay", DomainStatus.PROPOSED, b.domain_id))
    assert s.get_domain(b.domain_id).status == DomainStatus.PROPOSED


def test_second_pending_domain_successor_is_refused(tmp_path):
    s = Store(tmp_path / "s")
    a = s.add_domain(_domain("pay"))
    p1 = s.supersede_domain(a.domain_id, _domain("pay", DomainStatus.PROPOSED, a.domain_id))
    with pytest.raises(SupersessionConflictError, match=f"pending successor {p1.domain_id}"):
        s.supersede_domain(a.domain_id, _domain("pay2", DomainStatus.PROPOSED, a.domain_id))


def test_dropped_domain_can_still_be_superseded(tmp_path):
    s = Store(tmp_path / "s")
    a = s.add_domain(_domain("pay"))
    s.ratify_domains(drop=[a.domain_id])
    s.supersede_domain(a.domain_id, _domain("freed", DomainStatus.PROPOSED, a.domain_id))
    assert s.get_domain(a.domain_id).status == DomainStatus.SUPERSEDED


def test_pending_same_slug_revision_is_no_slug_conflict(tmp_path, monkeypatch, capsys):
    store_dir = tmp_path / ".sidegraph"
    s = Store(store_dir)
    a = s.add_domain(_domain("pay"))
    s.supersede_domain(a.domain_id, _domain("pay", DomainStatus.PROPOSED, a.domain_id, "Pay 2"))
    assert s.domain_slug_conflicts() == []
    graph = tmp_path / "graph.json"
    graph.write_text(json.dumps({"built_at_commit": "v", "nodes": [], "links": []}))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SIDEGRAPH_DIR", str(store_dir))
    monkeypatch.setenv("SIDEGRAPH_GRAPH", str(graph))
    s.close()
    assert sync_main(["--check", "--force"]) == 0


def test_unrelated_and_competing_slug_holders_still_conflict(tmp_path):
    s = Store(tmp_path / "s")
    a = s.add_domain(_domain("pay"))
    s._write_domain(_domain("pay", DomainStatus.PROPOSED))  # unrelated proposal, same slug
    conflicts = s.domain_slug_conflicts()
    assert [c["slug"] for c in conflicts] == ["pay"] and a.domain_id in conflicts[0]["domain_ids"]
    s2 = Store(tmp_path / "s2")
    b = s2.add_domain(_domain("pay"))
    s2._write_domain(_domain("pay", DomainStatus.PROPOSED, b.domain_id))
    s2._write_domain(_domain("pay", DomainStatus.PROPOSED, b.domain_id))  # two competing revisions
    # Only a SINGLE pending revision is exempt: two revisions of one holder are rivals.
    [rivals] = s2.domain_slug_conflicts()
    assert rivals["slug"] == "pay" and len(rivals["domain_ids"]) >= 2


# -- round-3 review fixes ---------------------------------------------------------------


def _raw_decision(s, decision):
    """A record that arrived through a git merge: written without the write-time guard."""
    with s._mutation():
        s._write_decision(decision)
    return decision


def _raw_fact(s, fact):
    with s._mutation():
        s._write_fact(fact)
    return fact


def _head_chain(s):
    """A -> B -> C, all but C superseded; returns (a, b, c)."""
    a = s.add_decision(_decision("A"))
    b = s.add_decision(_decision("B", DecisionStatus.ACCEPTED, a.id))
    c = s.add_decision(_decision("C", DecisionStatus.ACCEPTED, b.id))
    return a, b, c


def test_ratify_follows_the_chain_to_the_live_head(tmp_path):
    s = Store(tmp_path / "s")
    a, _b, c = _head_chain(s)
    stale = _raw_decision(s, _decision("stale P", DecisionStatus.PROPOSED, a.id))
    with pytest.raises(SupersessionConflictError, match=f"superseded by {c.id}"):
        s.ratify(stale.id)
    assert s.get_decision(stale.id).status == DecisionStatus.PROPOSED


def test_no_longer_current_refusal_names_the_head_of_a_long_chain(tmp_path):
    s = Store(tmp_path / "s")
    a, _b, c = _head_chain(s)
    with pytest.raises(SupersessionConflictError, match=f"live successor {c.id}"):
        s.add_decision(_decision("D", DecisionStatus.PROPOSED, a.id))


def test_stale_pending_fact_cannot_be_ratified_after_another_replacement(tmp_path):
    s = Store(tmp_path / "s")
    a = s.add_fact(_fact("A"))
    b = s.add_fact(_fact("B", DecisionStatus.ACCEPTED, a.id))
    stale = _raw_fact(s, _fact("stale P", DecisionStatus.PROPOSED, a.id))
    with pytest.raises(SupersessionConflictError, match=f"superseded by {b.id}"):
        s.ratify_fact(stale.id)
    assert s.get_fact(stale.id).status == DecisionStatus.PROPOSED


def test_decision_ratify_preflights_cascaded_stale_facts(tmp_path):
    s = Store(tmp_path / "s")
    support = s.add_decision(_decision("support", DecisionStatus.PROPOSED))
    a = s.add_fact(_fact("A"))
    s.add_fact(_fact("B", DecisionStatus.ACCEPTED, a.id))
    stale = _fact("stale P", DecisionStatus.PROPOSED, a.id)
    stale.supports = [support.id]
    _raw_fact(s, stale)
    with pytest.raises(SupersessionConflictError, match="superseded by"):
        s.ratify(support.id)
    assert s.get_decision(support.id).status == DecisionStatus.PROPOSED  # nothing written
    assert s.get_fact(stale.id).status == DecisionStatus.PROPOSED


def test_decision_ratify_cascade_closes_the_fact_predecessor(tmp_path):
    s = Store(tmp_path / "s")
    support = s.add_decision(_decision("support", DecisionStatus.PROPOSED))
    a = s.add_fact(_fact("A"))
    pending = _fact("P", DecisionStatus.PROPOSED, a.id)
    pending.supports = [support.id]
    _raw_fact(s, pending)
    s.ratify(support.id)
    closed = s.get_fact(a.id)
    assert closed.status == DecisionStatus.SUPERSEDED and closed.valid_to is not None


def test_add_domain_refuses_supersedes(tmp_path):
    s = Store(tmp_path / "s")
    a = s.add_domain(_domain("a"))
    with pytest.raises(ValueError, match="supersede_domain"):
        s.add_domain(_domain("b", DomainStatus.PROPOSED, a.domain_id))
    assert [d.slug for d in s.iter_domains()] == ["a"]


def test_competing_same_slug_revisions_cannot_both_be_accepted(tmp_path):
    s = Store(tmp_path / "s")
    a = s.add_domain(_domain("pay"))
    r1 = s.supersede_domain(a.domain_id, _domain("pay", DomainStatus.PROPOSED, a.domain_id))
    r2 = _domain("pay", DomainStatus.PROPOSED, a.domain_id)
    with s._mutation():
        s._write_domain(r2)
    assert s.domain_slug_conflicts()
    out = s.ratify_domains(accept=[r1.domain_id, r2.domain_id])
    assert out[r1.domain_id] == "accepted"
    assert out[r2.domain_id].startswith("error:")
    assert [d.slug for d in s.iter_domains(status=DomainStatus.ACCEPTED)] == ["pay"]


def test_dropped_domain_with_a_pending_heir_refuses_a_second_heir(tmp_path):
    s = Store(tmp_path / "s")
    a = s.add_domain(_domain("pay"))
    s.supersede_domain(a.domain_id, _domain("pay-a", DomainStatus.PROPOSED, a.domain_id))
    s.ratify_domains(drop=[a.domain_id])
    with pytest.raises(SupersessionConflictError, match="already has a pending successor"):
        s.supersede_domain(a.domain_id, _domain("pay-b", DomainStatus.PROPOSED, a.domain_id))
    assert [d.slug for d in s.iter_domains(status=DomainStatus.PROPOSED)] == ["pay-a"]


def test_direct_supersede_is_refused_while_a_pending_successor_exists(tmp_path):
    s = Store(tmp_path / "s")
    a = s.add_decision(_decision("A"))
    pending = s.add_decision(_decision("B", DecisionStatus.PROPOSED, a.id))
    with pytest.raises(SupersessionConflictError, match=f"pending successor {pending.id}"):
        s.add_decision(_decision("C", DecisionStatus.ACCEPTED, a.id))
    with pytest.raises(SupersessionConflictError, match=f"pending successor {pending.id}"):
        _supersede_decision_impl(s, None, a.id, "C", "adr", "c", "x")
    assert s.get_decision(a.id).status == DecisionStatus.ACCEPTED


def test_accepted_record_with_a_valid_to_cannot_be_superseded(tmp_path):
    s = Store(tmp_path / "s")
    closed = _decision("A")
    closed.valid_to = datetime.now(UTC)
    _raw_decision(s, closed)
    with pytest.raises(SupersessionConflictError, match="no longer current"):
        s.add_decision(_decision("B", DecisionStatus.ACCEPTED, closed.id))
