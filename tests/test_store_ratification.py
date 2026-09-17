from datetime import UTC, datetime

import pytest

from sidegraph.schema import Decision, DecisionKind, DecisionStatus, Provenance
from sidegraph.store import Store


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


# -- deferred supersession: add_decision(close_predecessor=False) + ratify (design §3
# option (b), doc-import's --propose rule; see doc_import.py) ---------------------------


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


def test_add_decision_close_predecessor_false_leaves_predecessor_untouched(tmp_path):
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
        ),
        close_predecessor=False,
    )
    reloaded = s.get_decision(old.id)
    assert reloaded.status == DecisionStatus.ACCEPTED
    assert reloaded.valid_to is None


def test_add_decision_close_predecessor_false_still_validates_supersedes_exists(tmp_path):
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
            ),
            close_predecessor=False,
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
        ),
        close_predecessor=False,
    )
    assert s.get_decision(old.id).status == DecisionStatus.ACCEPTED  # untouched at write time

    s.ratify(new.id)

    reloaded_old = s.get_decision(old.id)
    assert reloaded_old.status == DecisionStatus.SUPERSEDED
    assert reloaded_old.valid_to is not None
    assert s.get_decision(new.id).status == DecisionStatus.ACCEPTED


def test_ratify_does_not_reclose_already_superseded_predecessor(tmp_path):
    # The ordinary (close_predecessor=True, the default) path already closed the
    # predecessor at write time; ratifying a proposed successor that also supersedes it
    # must be a no-op there, not a crash or a second (re-stamped) close.
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
    assert s.get_decision(old.id).status == DecisionStatus.SUPERSEDED
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
        ),
        close_predecessor=False,
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
