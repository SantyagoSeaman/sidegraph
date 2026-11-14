"""A refusal that an agent reads while a replacement waits for the owner says the same thing
everywhere: tell the owner, and do not drop what you did not write.
see design/superpowers/specs/2026-11-12-ratification-needs-a-human-verdict-design.md (D11)"""

from __future__ import annotations

import pytest

from sidegraph.server import _add_decision_impl, _supersede_decision_impl
from sidegraph.store import RetireRefusedError, Store, SupersessionConflictError


@pytest.fixture
def waiting(tmp_path):
    """An accepted decision and its successor, which waits for the owner."""
    with Store(tmp_path / "store") as store:
        old = _add_decision_impl(store, None, title="Old", kind="adr", context="c", choice="ch")
        new = _supersede_decision_impl(
            store, None, old["id"], title="New", kind="adr", context="c2", choice="ch2"
        )
        yield store, old["id"], new["id"]


def test_retire_refusal_for_a_waiting_successor_tells_the_agent_to_tell_the_owner(waiting):
    """Red against the old tail "ratify or drop it first", which an agent follows by
    dropping the waiting replacement and then retiring the accepted record."""
    store, old_id, new_id = waiting

    with pytest.raises(RetireRefusedError, match=f"pending successor {new_id}") as refused:
        store.retire(old_id, "gone")

    text = str(refused.value)
    assert "waits for the owner's verdict" in text
    assert "Tell the owner" in text and "Do not drop it yourself" in text
    assert "ratify or drop it first" not in text


def test_superseding_the_waiting_successor_itself_says_to_tell_the_owner(waiting):
    """Red against the old text, which advised dropping the proposal or ratifying it."""
    store, _old_id, new_id = waiting

    with pytest.raises(SupersessionConflictError, match="is a pending proposal") as refused:
        _supersede_decision_impl(
            store, None, new_id, title="Newer", kind="adr", context="c3", choice="ch3"
        )

    assert "if you did not write it in this session, tell the owner" in str(refused.value)
