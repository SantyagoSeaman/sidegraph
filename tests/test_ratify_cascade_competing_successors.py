"""A decision's cascade never accepts two successors of one fact.

Two branches each propose a successor of the same accepted fact, both attached as evidence to
one proposed decision; a conflict-free git merge brings both files together. Ratifying the
decision must refuse, writing nothing, exactly as accepting the two facts one by one refuses
the second (``Store._refuse_stale_predecessor``). Every ratify surface routes through
``Store.ratify``, so each one that can reach the merged state is checked here.
see design/superpowers/specs/2026-07-10-facts-layer-design.md
"""

import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sidegraph.capture import RatifyPolicy, _auto_ratify
from sidegraph.cli import ratify_main
from sidegraph.schema import Decision, DecisionKind, DecisionStatus, Fact, Provenance
from sidegraph.server import _ratify_impl
from sidegraph.store import Store, SupersessionConflictError

NOW = datetime.now(UTC)


def _decision(**kw):
    base = dict(
        title="size the worker pool",
        kind=DecisionKind.ADR,
        context="c",
        choice="ch",
        valid_from=NOW,
        provenance=Provenance(source="test"),
    )
    base.update(kw)
    return Decision(**base)


def _fact(**kw):
    base = dict(
        statement="the queue holds 100 jobs",
        source="load test",
        valid_from=NOW,
        provenance=Provenance(source="test"),
    )
    base.update(kw)
    return Fact(**base)


def _merged_store(tmp_path: Path) -> tuple[Path, str, str, str, str]:
    """``(store_path, decision_id, predecessor_id, left_id, right_id)``: the store after
    merging two branches that each proposed a successor of the same accepted fact."""
    base = tmp_path / "base"
    with Store(base) as s:
        p = s.add_fact(_fact(status=DecisionStatus.ACCEPTED))
        d = s.add_decision(_decision())
    ids = {}
    for branch, capacity in (("left", 200), ("right", 50)):
        shutil.copytree(base, tmp_path / branch)
        with Store(tmp_path / branch) as s:
            ids[branch] = s.add_fact(
                _fact(
                    statement=f"the queue holds {capacity} jobs", supersedes=p.id, supports=[d.id]
                )
            ).id
    # A conflict-free git merge: the right branch's new fact file lands beside the left's.
    name = f"{ids['right']}.json"
    shutil.copy2(tmp_path / "right" / "facts" / name, tmp_path / "left" / "facts" / name)
    return tmp_path / "left", d.id, p.id, ids["left"], ids["right"]


def _assert_names_the_remedy(message: str, d_id: str, left: str, right: str) -> None:
    assert f"decision {d_id!r} cannot be ratified" in message
    assert left in message and right in message
    assert "drop one of them (in a separate call)" in message
    assert f"then ratify {d_id} again" in message


def _assert_untouched(store_path: Path, d_id: str, p_id: str, left: str, right: str) -> None:
    with Store(store_path) as s:
        assert s.get_decision(d_id).status == DecisionStatus.PROPOSED
        assert s.get_fact(p_id).status == DecisionStatus.ACCEPTED
        assert s.get_fact(p_id).valid_to is None
        assert s.get_fact(left).status == DecisionStatus.PROPOSED
        assert s.get_fact(right).status == DecisionStatus.PROPOSED


def test_merged_state_holds_two_proposed_successors(tmp_path):
    store_path, d_id, p_id, left, right = _merged_store(tmp_path)
    with Store(store_path) as s:
        successors = [f for f in s.iter_proposed_facts() if f.supersedes == p_id]
        assert {f.id for f in successors} == {left, right}
        assert all(d_id in f.supports for f in successors)


def test_store_ratify_refuses_a_cascade_with_two_successors_of_one_fact(tmp_path):
    store_path, d_id, p_id, left, right = _merged_store(tmp_path)
    with Store(store_path) as s, pytest.raises(SupersessionConflictError, match=p_id) as err:
        s.ratify(d_id)
    _assert_names_the_remedy(str(err.value), d_id, left, right)
    _assert_untouched(store_path, d_id, p_id, left, right)


def test_direct_fact_ratification_already_refuses_the_second_successor(tmp_path):
    """The rule the cascade reuses: once one successor is accepted, the other is refused."""
    store_path, _d_id, p_id, left, right = _merged_store(tmp_path)
    with Store(store_path) as s:
        s.ratify_fact(left)
        with pytest.raises(SupersessionConflictError, match=f"superseded by {left}"):
            s.ratify_fact(right)


def test_mcp_ratify_reports_the_conflict_and_writes_nothing(tmp_path):
    store_path, d_id, p_id, left, right = _merged_store(tmp_path)
    with Store(store_path) as s:
        out = _ratify_impl(s, accept=[d_id])
    assert out[d_id].startswith("error:")
    assert p_id in out[d_id]
    _assert_names_the_remedy(out[d_id], d_id, left, right)
    assert left not in out and right not in out  # no fact reported as accepted evidence
    _assert_untouched(store_path, d_id, p_id, left, right)


def test_cli_ratify_reports_the_conflict_and_writes_nothing(tmp_path, capsys):
    store_path, d_id, p_id, left, right = _merged_store(tmp_path)
    assert ratify_main(["--db", str(store_path), "--accept", d_id]) != 0
    captured = capsys.readouterr()
    assert f"error {d_id}:" in captured.err and p_id in captured.err
    _assert_names_the_remedy(captured.err, d_id, left, right)
    assert "evidence of" not in captured.out
    _assert_untouched(store_path, d_id, p_id, left, right)


def test_a_permissive_cascade_guard_does_not_bypass_the_refusal(tmp_path):
    """Auto-ratification's call shape: the guard approves every fact, the refusal still holds."""
    store_path, d_id, p_id, left, right = _merged_store(tmp_path)
    with Store(store_path) as s, pytest.raises(SupersessionConflictError, match=p_id):
        s.ratify(d_id, actor="auto:auto-all", cascade_guard=lambda _facts: True)
    _assert_untouched(store_path, d_id, p_id, left, right)


def test_auto_ratify_reports_the_conflict_and_writes_nothing(tmp_path):
    store_path, d_id, p_id, left, right = _merged_store(tmp_path)
    with Store(store_path) as s:
        outcome = _auto_ratify(s, d_id, "adr", RatifyPolicy.AUTO_ALL)
    assert outcome.ratified_by is None
    assert outcome.error is not None and p_id in outcome.error
    _assert_untouched(store_path, d_id, p_id, left, right)


def test_dropping_one_successor_lets_the_decision_ratify(tmp_path):
    """The recovery the refusal points to: drop one competing fact, ratify again."""
    store_path, d_id, p_id, left, right = _merged_store(tmp_path)
    with Store(store_path) as s:
        s.drop_fact(right)
        _decision, cascaded = s.ratify(d_id)
        assert [f.id for f in cascaded] == [left]
        assert s.get_fact(p_id).status == DecisionStatus.SUPERSEDED
        assert s.get_fact(right).status == DecisionStatus.REJECTED


def test_one_mcp_call_with_accept_and_drop_leaves_the_decision_proposed(tmp_path):
    """Why the remedy says "in a separate call": ``accept`` runs before ``drop``."""
    store_path, d_id, _p_id, left, right = _merged_store(tmp_path)
    with Store(store_path) as s:
        out = _ratify_impl(s, accept=[d_id], drop=[right])
        assert out[d_id].startswith("error:")
        assert out[right] == "dropped"
        assert s.get_decision(d_id).status == DecisionStatus.PROPOSED
        out = _ratify_impl(s, accept=[d_id])
        assert out[d_id] == "accepted"
        assert s.get_fact(left).status == DecisionStatus.ACCEPTED
