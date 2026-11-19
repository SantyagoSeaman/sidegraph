from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sidegraph.schema import Decision, DecisionStatus, Fact
from sidegraph.server import _verify_store_impl
from sidegraph.store import Store, canonical_digest
from tests.store_record_support import _decision, _fact


def _half_closed(
    store: Store, monkeypatch: pytest.MonkeyPatch, noun: str
) -> tuple[Decision | Fact, Decision | Fact]:
    if noun == "decision":
        predecessor = store.add_decision(_decision())
        successor = _decision(supersedes=predecessor.id, valid_from=predecessor.valid_from)
        add, write = store.add_decision, store._write_decision
    else:
        predecessor = store.add_fact(_fact())
        successor = _fact(
            supersedes=predecessor.id,
            valid_from=predecessor.valid_from - timedelta(seconds=1),
        )
        add, write = store.add_fact, store._write_fact

    def fail_on_predecessor(record: Decision | Fact) -> None:
        if record.id == predecessor.id:
            raise OSError("injected predecessor write failure")
        write(record)

    with monkeypatch.context() as patch:
        patch.setattr(store, f"_write_{noun}", fail_on_predecessor)
        with pytest.raises(OSError, match="injected predecessor write failure"):
            add(successor)
    store.refresh_if_stale()
    return predecessor, successor


@pytest.mark.parametrize("noun", ["decision", "fact"])
def test_closes_open_predecessor_of_accepted_successor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, noun: str
) -> None:
    with Store(tmp_path / "store") as store:
        predecessor, successor = _half_closed(store, monkeypatch, noun)
        get = store.get_decision if noun == "decision" else store.get_fact
        assert get(predecessor.id) == predecessor
        assert get(successor.id) == successor
        closed = store.close_superseded_predecessor(successor.id)
        assert closed.id == predecessor.id
        assert closed.status == DecisionStatus.SUPERSEDED
        assert closed.valid_to == max(successor.valid_from, predecessor.valid_from)
        assert get(predecessor.id) == closed
        assert get(successor.id) == successor


def _assert_refused(store: Store, successor_id: str) -> None:
    before = canonical_digest(store.path)
    stamp = store.canonical_digest()
    with pytest.raises(ValueError):
        store.close_superseded_predecessor(successor_id)
    assert canonical_digest(store.path) == before
    assert store.canonical_digest() == stamp


def test_refuses_when_successor_not_accepted(tmp_path: Path) -> None:
    with Store(tmp_path / "store") as store:
        predecessor = store.add_decision(_decision())
        successor = store.add_decision(
            _decision(supersedes=predecessor.id, status=DecisionStatus.PROPOSED)
        )
        _assert_refused(store, successor.id)


def test_refuses_when_successor_does_not_name_it(tmp_path: Path) -> None:
    with Store(tmp_path / "store") as store:
        successor = store.add_decision(_decision())
        _assert_refused(store, successor.id)


@pytest.mark.parametrize("already_superseded", [True, False])
def test_refuses_when_predecessor_already_closed(tmp_path: Path, already_superseded: bool) -> None:
    with Store(tmp_path / "store") as store:
        predecessor = store.add_decision(_decision())
        successor = _decision(supersedes=predecessor.id)
        if already_superseded:
            store.add_decision(successor)
        else:
            # An accepted record can already have a validity end without a status flip.
            predecessor.valid_to = datetime.now(UTC)
            with store._mutation():
                store._write_decision(predecessor)
                store._write_decision(successor)
        _assert_refused(store, successor.id)


def test_refuses_when_successor_missing(tmp_path: Path) -> None:
    with Store(tmp_path / "store") as store:
        _assert_refused(store, _decision().id)


@pytest.mark.parametrize("noun", ["decision", "fact"])
def test_verify_accepts_the_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, noun: str
) -> None:
    with Store(tmp_path / "store") as store:
        _, successor = _half_closed(store, monkeypatch, noun)
        store.close_superseded_predecessor(successor.id)
        assert _verify_store_impl(store) == {"clean": True, "violations": []}
