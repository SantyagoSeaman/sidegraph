"""Supported Python sequence shapes and the joint node budget stay admitted at capture."""

import pytest

from sidegraph import capture, input_limits, server
from sidegraph.store import Store


def decision(**updates):
    raw = dict(title="bounded", kind="lesson", context="context", choice="choice")
    raw.update(updates)
    return raw


@pytest.mark.parametrize("nodes", [11, 12])
def test_joint_node_budget_matches_core_before_any_call(tmp_path, monkeypatch, nodes):
    monkeypatch.setattr(input_limits, "MAX_REQUEST_NODES", nodes)
    calls = []
    original = capture.preflight_drafts

    def tracked(*args, **kwargs):
        calls.append(kwargs.get("metadata"))
        return original(*args, **kwargs)

    monkeypatch.setattr(capture, "preflight_drafts", tracked)
    monkeypatch.setattr(server, "preflight_drafts", tracked)
    with Store(tmp_path / "store") as store:
        if nodes == 11:
            with pytest.raises(input_limits.InputLimitError):
                server._propose_decisions_impl(store, None, [decision()])
            assert list(store.iter_decisions()) == []
            assert len(calls) == 1
        else:
            result = server._propose_decisions_impl(store, None, [decision()])
            assert len(list(store.iter_decisions())) == 1
            assert result[0]["status"] == "written"


@pytest.mark.parametrize("batch", [list, tuple])
def test_supported_sequence_and_strenum_control(tmp_path, batch):
    raw = decision(kind=capture.DecisionKind.LESSON, tags=("safe",))
    raw["facts"] = (dict(statement="evidence", source="source"),)
    with Store(tmp_path / "store") as store:
        results = capture.propose(batch([raw]), store, None)
        assert len(list(store.iter_decisions())) == 1
        assert len(list(store.iter_facts())) == 1
    assert results[0].status == "written"
