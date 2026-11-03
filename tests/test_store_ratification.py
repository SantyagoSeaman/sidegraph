from datetime import UTC, datetime

import pytest

from sidegraph.schema import Decision, DecisionKind, DecisionStatus, Provenance
from sidegraph.store import Store, SupersessionConflictError


def _proposed(store, title="draft"):
    d = Decision(
        title=title,
        kind=DecisionKind.LESSON,
        context="c",
        choice="ch",
        valid_from=datetime.now(UTC),
        provenance=Provenance(source="agent"),
    )
    return store.add_decision(d)  # default status is PROPOSED


def test_iter_proposed_lists_only_proposed(tmp_path):
    s = Store(tmp_path / "t.db")
    d1 = _proposed(s, "one")
    d2 = _proposed(s, "two")
    s.ratify(d2.id)
    assert {d.id for d in s.iter_proposed()} == {d1.id}


def test_ratify_flips_to_accepted(tmp_path):
    s = Store(tmp_path / "t.db")
    d = _proposed(s)
    out, cascaded = s.ratify(d.id)
    assert out.status == DecisionStatus.ACCEPTED
    assert cascaded == []
    assert s.get_decision(d.id).status == DecisionStatus.ACCEPTED


def test_ratify_non_proposed_raises(tmp_path):
    s = Store(tmp_path / "t.db")
    d = _proposed(s)
    s.ratify(d.id)
    with pytest.raises(ValueError, match="not proposed"):
        s.ratify(d.id)
    with pytest.raises(ValueError, match="not proposed"):
        s.ratify("missing-id")


def test_drop_sets_rejected_and_valid_to_keeps_history(tmp_path):
    s = Store(tmp_path / "t.db")
    d = _proposed(s)
    out, cascaded = s.drop(d.id)
    assert out.status == DecisionStatus.REJECTED
    assert out.valid_to is not None
    assert cascaded == []
    kept = s.get_decision(d.id)  # append-only: still retrievable
    assert kept is not None and kept.status == DecisionStatus.REJECTED


def test_drop_non_proposed_decision_raises(tmp_path):
    """Pin: store.drop() stays proposal-only for DECISIONS -- deliberately UNCHANGED by
    the review-round-3 fix that extended domains' ratify_domains(drop=...) to also accept
    an already-accepted domain (design §6's accepted-vs-accepted slug conflict). A decision
    is a memory record with a narrow lifecycle; only a domain is the owned abstraction
    layer that's safe to retire after acceptance. An accepted decision must still never be
    droppable."""
    s = Store(tmp_path / "t.db")
    d = _proposed(s)
    s.ratify(d.id)
    assert s.get_decision(d.id).status == DecisionStatus.ACCEPTED
    with pytest.raises(ValueError, match="not proposed"):
        s.drop(d.id)
    assert s.get_decision(d.id).status == DecisionStatus.ACCEPTED  # unchanged


def test_drop_future_valid_from_stays_readable(tmp_path):
    from datetime import timedelta

    s = Store(tmp_path / "t.db")
    future = datetime.now(UTC) + timedelta(days=7)
    d = s.add_decision(
        Decision(
            title="effective next sprint",
            kind=DecisionKind.CONSTRAINT,
            context="c",
            choice="ch",
            valid_from=future,
            provenance=Provenance(source="manual"),
        )
    )
    s.drop(d.id)
    kept = s.get_decision(d.id)  # must NOT raise on re-read
    assert kept.status == DecisionStatus.REJECTED
    assert kept.valid_to >= kept.valid_from  # invariant preserved
    assert list(s.iter_decisions())  # whole-table iteration unpoisoned


def test_supersede_future_dated_predecessor_stays_readable(tmp_path):
    from datetime import timedelta

    s = Store(tmp_path / "t.db")
    future = datetime.now(UTC) + timedelta(days=7)
    old = s.add_decision(
        Decision(
            title="future rule",
            kind=DecisionKind.CONSTRAINT,
            status=DecisionStatus.ACCEPTED,
            context="c",
            choice="x",
            valid_from=future,
            provenance=Provenance(source="manual"),
        )
    )
    s.add_decision(
        Decision(
            title="replaces future rule",
            kind=DecisionKind.ADR,
            status=DecisionStatus.ACCEPTED,
            context="c",
            choice="y",
            valid_from=datetime.now(UTC),
            supersedes=old.id,
            provenance=Provenance(source="manual"),
        )
    )
    kept = s.get_decision(old.id)  # must NOT raise
    assert kept.status == DecisionStatus.SUPERSEDED
    assert kept.valid_to >= kept.valid_from
    assert list(s.iter_decisions())  # iteration unpoisoned


def test_dropped_future_dated_record_not_in_live_reads(tmp_path):
    from datetime import timedelta

    from sidegraph.schema import AnchorBinding, Entity, Scope

    s = Store(tmp_path / "t.db")
    e = s.upsert_entity(Entity(canonical_name="X"))
    future = datetime.now(UTC) + timedelta(days=7)
    d = s.add_decision(
        Decision(
            title="future draft",
            kind=DecisionKind.LESSON,
            context="c",
            choice="ch",
            scope=Scope.GLOBAL,
            valid_from=future,
            provenance=Provenance(source="agent"),
        )
    )
    s.add_binding(AnchorBinding(record_id=d.id, entity_id=e.entity_id, tier=2, status="live"))
    s.drop(d.id)  # valid_to clamps to the FUTURE valid_from
    assert d.id not in {x.id for x in s.valid_decisions_for_entity(e.entity_id)}
    assert d.id not in {x.id for x in s.decisions_by_scope(Scope.GLOBAL)}


# -- deferred supersession: a PROPOSED successor leaves its predecessor open until ratify
# (design §3 option (b), doc-import's --propose rule; see doc_import.py) ---------------------------


def _accepted(store, title="old way"):
    return store.add_decision(
        Decision(
            title=title,
            kind=DecisionKind.ADR,
            status=DecisionStatus.ACCEPTED,
            context="c",
            choice="x",
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual"),
        )
    )


def test_add_decision_proposed_successor_leaves_predecessor_untouched(tmp_path):
    s = Store(tmp_path / "t.db")
    old = _accepted(s)
    s.add_decision(
        Decision(
            title="new way",
            kind=DecisionKind.ADR,
            context="c",
            choice="y",
            supersedes=old.id,
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual"),
        )
    )
    reloaded = s.get_decision(old.id)
    assert reloaded.status == DecisionStatus.ACCEPTED
    assert reloaded.valid_to is None


def test_add_decision_proposed_successor_still_validates_supersedes_exists(tmp_path):
    s = Store(tmp_path / "t.db")
    with pytest.raises(ValueError, match="unknown decision"):
        s.add_decision(
            Decision(
                title="new way",
                kind=DecisionKind.ADR,
                context="c",
                choice="y",
                supersedes="01JUNKULIDDOESNOTEXIST00",
                valid_from=datetime.now(UTC),
                provenance=Provenance(source="manual"),
            )
        )


def test_ratify_closes_still_open_predecessor_when_supersedes_set(tmp_path):
    s = Store(tmp_path / "t.db")
    old = _accepted(s)
    new = s.add_decision(
        Decision(
            title="new way",
            kind=DecisionKind.ADR,
            status=DecisionStatus.PROPOSED,
            context="c",
            choice="y",
            supersedes=old.id,
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual"),
        )
    )
    assert s.get_decision(old.id).status == DecisionStatus.ACCEPTED  # untouched at write time

    s.ratify(new.id)

    reloaded_old = s.get_decision(old.id)
    assert reloaded_old.status == DecisionStatus.SUPERSEDED
    assert reloaded_old.valid_to is not None
    assert s.get_decision(new.id).status == DecisionStatus.ACCEPTED


def test_ratify_does_not_reclose_already_superseded_predecessor(tmp_path):
    # The legacy shape (a store written before the close was deferred): the predecessor was
    # closed at write time while its successor stayed proposed. Ratifying that successor
    # must be a no-op on the predecessor, not a crash or a second (re-stamped) close.
    s = Store(tmp_path / "t.db")
    old = _accepted(s)
    new = s.add_decision(
        Decision(
            title="new way",
            kind=DecisionKind.ADR,
            status=DecisionStatus.PROPOSED,
            context="c",
            choice="y",
            supersedes=old.id,
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual"),
        )
    )
    legacy = s.get_decision(old.id)
    legacy.status = DecisionStatus.SUPERSEDED
    legacy.valid_to = datetime.now(UTC)
    with s._mutation():
        s._write_decision(legacy)
    valid_to_before = s.get_decision(old.id).valid_to

    s.ratify(new.id)

    assert s.get_decision(old.id).valid_to == valid_to_before  # untouched, not re-stamped
    assert s.get_decision(new.id).status == DecisionStatus.ACCEPTED


def test_ratify_drop_never_touches_supersedes_target(tmp_path):
    s = Store(tmp_path / "t.db")
    old = _accepted(s)
    new = s.add_decision(
        Decision(
            title="new way",
            kind=DecisionKind.ADR,
            status=DecisionStatus.PROPOSED,
            context="c",
            choice="y",
            supersedes=old.id,
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual"),
        )
    )

    s.drop(new.id)

    reloaded_old = s.get_decision(old.id)
    assert reloaded_old.status == DecisionStatus.ACCEPTED
    assert reloaded_old.valid_to is None
    assert s.get_decision(new.id).status == DecisionStatus.REJECTED


def test_capture_ledger_idempotent(tmp_path):
    s = Store(tmp_path / "t.db")
    assert s.was_captured("sess-1") is False
    s.mark_captured("sess-1")
    assert s.was_captured("sess-1") is True
    s.mark_captured("sess-1")  # idempotent, no error
    assert s.was_captured("sess-1") is True


def _successor(store, old_id, title="new way", status=DecisionStatus.PROPOSED):
    return store.add_decision(
        Decision(
            title=title,
            kind=DecisionKind.ADR,
            status=status,
            context="c",
            choice="y",
            supersedes=old_id,
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual"),
        )
    )


def test_rejected_successor_still_closes_predecessor_at_write(tmp_path):
    # Every successor except a proposed one closes at write (doc-import `status: rejected`).
    s = Store(tmp_path / "t.db")
    old = _accepted(s)
    _successor(s, old.id, status=DecisionStatus.REJECTED)
    assert s.get_decision(old.id).status == DecisionStatus.SUPERSEDED


def test_proposed_successor_then_drop_leaves_predecessor_accepted_and_open(tmp_path):
    s = Store(tmp_path / "t.db")
    old = _accepted(s)
    new = _successor(s, old.id)
    s.drop(new.id)
    kept = s.get_decision(old.id)
    assert kept.status == DecisionStatus.ACCEPTED and kept.valid_to is None


def _merged_in(store, old_id, title="merged"):
    """A pending successor written the way a merge brings one in: past the write-time check."""
    d = Decision(
        title=title,
        kind=DecisionKind.ADR,
        status=DecisionStatus.PROPOSED,
        context="c",
        choice="y",
        supersedes=old_id,
        valid_from=datetime.now(UTC),
        provenance=Provenance(source="manual"),
    )
    with store._mutation():
        store._write_decision(d)
    return d


def test_chain_of_proposals_is_refused_at_write(tmp_path):
    # A (accepted) <- B (proposed): a record that is itself a pending proposal cannot be
    # superseded, so the chain A <- B <- C never exists.
    s = Store(tmp_path / "t.db")
    a = _accepted(s)
    b = _successor(s, a.id, "B")
    with pytest.raises(SupersessionConflictError, match=f"{b.id} is a pending proposal"):
        _successor(s, b.id, "C")


def test_ratify_refuses_successor_whose_predecessor_is_still_proposed(tmp_path):
    s = Store(tmp_path / "t.db")
    a = _accepted(s)
    b = _successor(s, a.id, "B")
    c = _merged_in(s, b.id, "C")
    with pytest.raises(ValueError, match=f"predecessor {b.id} is still proposed; ratify or drop"):
        s.ratify(c.id)
    assert s.get_decision(c.id).status == DecisionStatus.PROPOSED
    s.ratify(b.id)
    s.ratify(c.id)
    assert s.get_decision(a.id).status == DecisionStatus.SUPERSEDED
    assert s.get_decision(b.id).status == DecisionStatus.SUPERSEDED


def test_ratify_allows_successor_of_a_dropped_predecessor(tmp_path):
    s = Store(tmp_path / "t.db")
    a = _accepted(s)
    b = _successor(s, a.id, "B")
    c = _merged_in(s, b.id, "C")
    s.drop(b.id)
    s.ratify(c.id)
    assert s.get_decision(c.id).status == DecisionStatus.ACCEPTED
    assert s.get_decision(a.id).status == DecisionStatus.ACCEPTED  # C closes nothing


def test_ratify_refuses_second_sibling_successor(tmp_path):
    s = Store(tmp_path / "t.db")
    a = _accepted(s)
    p1 = _successor(s, a.id, "P1")
    p2 = _merged_in(s, a.id, "P2")
    s.ratify(p1.id)
    with pytest.raises(ValueError, match=f"{a.id} is already superseded by {p1.id}"):
        s.ratify(p2.id)
    assert s.get_decision(p2.id).status == DecisionStatus.PROPOSED
