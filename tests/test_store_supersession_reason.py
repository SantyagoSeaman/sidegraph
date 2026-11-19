from pathlib import Path

import pytest

from sidegraph.schema import Decision, DecisionStatus, Fact
from sidegraph.store import Store, SupersessionConflictError
from tests.store_record_support import _decision, _fact


def _add(
    store: Store,
    noun: str,
    *,
    supersedes: str | None = None,
    status: DecisionStatus = DecisionStatus.ACCEPTED,
) -> Decision | Fact:
    if noun == "decision":
        return store.add_decision(_decision(supersedes=supersedes, status=status))
    return store.add_fact(_fact(supersedes=supersedes, status=status))


def _merged_proposal(store: Store, noun: str, predecessor: str) -> str:
    # A merge can bring in a proposal the write-time guards would refuse.
    with store._mutation():
        if noun == "decision":
            decision = _decision(status=DecisionStatus.PROPOSED, supersedes=predecessor)
            store._write_decision(decision)
            return decision.id
        fact = _fact(status=DecisionStatus.PROPOSED, supersedes=predecessor)
        store._write_fact(fact)
        return fact.id


@pytest.mark.parametrize("noun", ["decision", "fact"])
def test_not_current_reason_and_head(tmp_path: Path, noun: str) -> None:
    with Store(tmp_path / "store") as store:
        old = _add(store, noun)
        head = _add(store, noun, supersedes=old.id)
        with pytest.raises(SupersessionConflictError) as exc:
            _add(store, noun, supersedes=old.id)
    assert str(exc.value) == (
        f"{noun} {old.id} is no longer current; supersede its live successor {head.id} instead"
    )
    assert (exc.value.reason, exc.value.head, exc.value.pending) == ("not_current", head.id, None)


@pytest.mark.parametrize("noun", ["decision", "fact"])
def test_not_current_without_a_live_head(tmp_path: Path, noun: str) -> None:
    with Store(tmp_path / "store") as store:
        old = _add(store, noun, status=DecisionStatus.REJECTED)
        with pytest.raises(SupersessionConflictError) as exc:
            _add(store, noun, supersedes=old.id)
    assert str(exc.value) == f"{noun} {old.id} is no longer current and cannot be superseded"
    assert (exc.value.reason, exc.value.head, exc.value.pending) == ("not_current", None, None)


@pytest.mark.parametrize("noun", ["decision", "fact"])
def test_pending_successor_reason(tmp_path: Path, noun: str) -> None:
    with Store(tmp_path / "store") as store:
        old = _add(store, noun)
        pending = _add(store, noun, supersedes=old.id, status=DecisionStatus.PROPOSED)
        with pytest.raises(SupersessionConflictError) as exc:
            _add(store, noun, supersedes=old.id)
    assert str(exc.value) == (
        f"{noun} {old.id} already has a pending successor {pending.id}; it waits for "
        "the owner's verdict. Tell the owner: it must be ratified or dropped first. Do "
        "not drop it yourself"
    )
    assert (exc.value.reason, exc.value.head, exc.value.pending) == (
        "pending_successor",
        None,
        pending.id,
    )


@pytest.mark.parametrize("noun", ["decision", "fact"])
def test_pending_proposal_reason(tmp_path: Path, noun: str) -> None:
    with Store(tmp_path / "store") as store:
        pending = _add(store, noun, status=DecisionStatus.PROPOSED)
        with pytest.raises(SupersessionConflictError) as exc:
            _add(store, noun, supersedes=pending.id)
    assert str(exc.value) == (
        f"{noun} {pending.id} is a pending proposal; amend it by dropping it and "
        "proposing again, or ratify it first; if you did not write it in this "
        "session, tell the owner"
    )
    assert (exc.value.reason, exc.value.head, exc.value.pending) == (
        "pending_proposal",
        None,
        pending.id,
    )


@pytest.mark.parametrize("noun", ["decision", "fact"])
def test_ratify_refuses_still_proposed_predecessor(tmp_path: Path, noun: str) -> None:
    with Store(tmp_path / "store") as store:
        pending = _add(store, noun, status=DecisionStatus.PROPOSED)
        successor = _merged_proposal(store, noun, pending.id)
        ratify = store.ratify if noun == "decision" else store.ratify_fact
        with pytest.raises(SupersessionConflictError) as exc:
            ratify(successor)
    assert str(exc.value) == (
        f"{noun} {successor!r} cannot be ratified: its predecessor {pending.id} "
        "is still proposed; ratify or drop it first"
    )
    assert (exc.value.reason, exc.value.head, exc.value.pending) == (
        "pending_proposal",
        None,
        pending.id,
    )


@pytest.mark.parametrize("noun", ["decision", "fact"])
def test_ratify_refuses_already_superseded_predecessor(tmp_path: Path, noun: str) -> None:
    with Store(tmp_path / "store") as store:
        old = _add(store, noun)
        head = _add(store, noun, supersedes=old.id)
        successor = _merged_proposal(store, noun, old.id)
        ratify = store.ratify if noun == "decision" else store.ratify_fact
        with pytest.raises(SupersessionConflictError) as exc:
            ratify(successor)
    assert str(exc.value) == (
        f"{noun} {successor!r} cannot be ratified: {old.id} is already "
        f"superseded by {head.id}; supersede that one instead"
    )
    assert (exc.value.reason, exc.value.head, exc.value.pending) == ("not_current", head.id, None)


def test_evidence_conflict_reason(tmp_path: Path) -> None:
    with Store(tmp_path / "store") as store:
        draft = store.add_decision(_decision(status=DecisionStatus.PROPOSED))
        old = store.add_fact(_fact())
        first, second = (
            _fact(status=DecisionStatus.PROPOSED, supports=[draft.id], supersedes=old.id)
            for _ in range(2)
        )
        # A merge can attach two proposed successors to the same fact.
        with store._mutation():
            store._write_fact(first)
            store._write_fact(second)
        first_id, second_id = sorted((first.id, second.id))
        with pytest.raises(SupersessionConflictError) as exc:
            store.ratify(draft.id)
    assert str(exc.value) == (
        f"decision {draft.id!r} cannot be ratified: its evidence facts "
        f"{first_id} and {second_id} both supersede {old.id}; drop one "
        f"of them (in a separate call), then ratify {draft.id} again"
    )
    assert (exc.value.reason, exc.value.head, exc.value.pending) == (
        "evidence_conflict",
        None,
        None,
    )
