"""SIDEGRAPH_AUTO_ACCEPT: agent captures land accepted; domains stay gated.
see design/superpowers/specs/2026-07-10-ratification-ux-and-mcp-gaps-design.md"""

import pytest

from sidegraph.capture import propose, propose_facts
from sidegraph.schema import DecisionStatus
from sidegraph.store import Store


@pytest.fixture
def store(tmp_path) -> Store:
    with Store(tmp_path / "test.db") as s:
        yield s


@pytest.fixture(autouse=True)
def _no_ambient_initiative(monkeypatch):
    """Tests must not depend on the ambient git branch (mirrors test_capture_facts.py's
    identical fixture): without this, `_derive_initiative()` picks up whatever branch the
    repo happens to be checked out on and silently adds a Tier-0 binding."""
    monkeypatch.setattr("sidegraph.capture._derive_initiative", lambda: None)


def _draft(**kw):
    base = dict(
        title="use httpx",
        kind="adr",
        context="why",
        choice="httpx",
        anchors=[{"name": "client.py", "file_path": "src/client.py"}],
    )
    base.update(kw)
    return base


def test_propose_auto_accept_lands_accepted_with_agent_provenance(store):
    results = propose([_draft()], store, None, auto_accept=True)
    d = store.get_decision(results[0].decision_id)
    assert d.status == DecisionStatus.ACCEPTED
    assert d.provenance.source == "agent"  # history never lies


def test_propose_auto_accept_covers_attached_and_standalone_facts(store):
    draft = _draft()
    draft["facts"] = [{"statement": "s1", "source": "src1"}]
    results = propose([draft], store, None, auto_accept=True)
    d_id = results[0].decision_id
    assert all(f.status == DecisionStatus.ACCEPTED for f in store.facts_for_decision(d_id))
    standalone = propose_facts(
        [{"statement": "s2", "source": "src2", "supports": [d_id]}],
        store,
        None,
        auto_accept=True,
    )
    assert store.get_fact(standalone[0].fact_id).status == DecisionStatus.ACCEPTED


def test_propose_default_still_lands_proposed(store):
    results = propose([_draft()], store, None)
    assert store.get_decision(results[0].decision_id).status == DecisionStatus.PROPOSED
