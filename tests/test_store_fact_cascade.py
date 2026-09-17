"""Cascade ratification: facts ride their decision's verdict.
see design/superpowers/specs/2026-07-10-facts-layer-design.md"""

from datetime import UTC, datetime

import pytest

from sidegraph.schema import (
    Decision,
    DecisionKind,
    DecisionStatus,
    Fact,
    Provenance,
)
from sidegraph.store import Store

NOW = datetime.now(UTC)


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "s") as s:
        yield s


def _decision(**kw):
    base = dict(
        title="use httpx",
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
        statement="httpx has no built-in retry",
        source="httpx docs",
        valid_from=NOW,
        provenance=Provenance(source="test"),
    )
    base.update(kw)
    return Fact(**base)


def test_accept_decision_accepts_its_proposed_facts(store):
    d = store.add_decision(_decision())
    f = store.add_fact(_fact(supports=[d.id]))
    decision, cascaded = store.ratify(d.id)
    assert decision.status == DecisionStatus.ACCEPTED
    assert [x.id for x in cascaded] == [f.id]
    assert store.get_fact(f.id).status == DecisionStatus.ACCEPTED


def test_drop_decision_drops_sole_supporter_facts(store):
    d = store.add_decision(_decision())
    f = store.add_fact(_fact(supports=[d.id]))
    decision, cascaded = store.drop(d.id)
    assert decision.status == DecisionStatus.REJECTED
    assert [x.id for x in cascaded] == [f.id]
    dropped = store.get_fact(f.id)
    assert dropped.status == DecisionStatus.REJECTED and dropped.valid_to is not None


def test_multi_support_fact_survives_one_dropped_supporter(store):
    d1 = store.add_decision(_decision(title="a"))
    d2 = store.add_decision(_decision(title="b"))
    f = store.add_fact(_fact(supports=[d1.id, d2.id]))
    _, cascaded = store.drop(d1.id)
    assert cascaded == []
    assert store.get_fact(f.id).status == DecisionStatus.PROPOSED
    # ...and dropping the second (last live) supporter now cascades
    _, cascaded2 = store.drop(d2.id)
    assert [x.id for x in cascaded2] == [f.id]


def test_standalone_fact_never_cascades(store):
    d = store.add_decision(_decision())
    f = store.add_fact(_fact())  # supports=[]
    _, cascaded = store.drop(d.id)
    assert cascaded == []
    assert store.get_fact(f.id).status == DecisionStatus.PROPOSED


def test_already_accepted_fact_untouched_by_cascade(store):
    d = store.add_decision(_decision())
    store.add_fact(_fact(supports=[d.id], status=DecisionStatus.ACCEPTED))
    _, cascaded = store.ratify(d.id)
    assert cascaded == []


def test_ratify_fact_and_drop_fact_direct(store):
    f1 = store.add_fact(_fact())
    f2 = store.add_fact(_fact(statement="another"))
    assert store.ratify_fact(f1.id).status == DecisionStatus.ACCEPTED
    assert store.drop_fact(f2.id).status == DecisionStatus.REJECTED
    with pytest.raises(ValueError):
        store.ratify_fact(f1.id)  # not proposed anymore
