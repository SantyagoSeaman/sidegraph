"""Ratify surface: fact routing, cascade reporting, nested queue render (Task 7).
see design/superpowers/specs/2026-07-10-facts-layer-design.md"""

from datetime import UTC, datetime

import pytest

from sidegraph.schema import Decision, DecisionKind, DecisionStatus, Fact, Provenance
from sidegraph.server import _list_proposed_impl, _ratify_impl
from sidegraph.store import Store

NOW = datetime.now(UTC)


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


def test_ratify_routes_fact_ids(tmp_path):
    store = Store(tmp_path / "s")
    f = store.add_fact(_fact())
    out = _ratify_impl(store, accept=[f.id])
    assert out[f.id] == "accepted"
    assert store.get_fact(f.id).status == DecisionStatus.ACCEPTED


def test_ratify_decision_reports_cascaded_facts(tmp_path):
    store = Store(tmp_path / "s")
    d = store.add_decision(_decision())
    f = store.add_fact(_fact(supports=[d.id]))
    out = _ratify_impl(store, accept=[d.id])
    assert out[d.id] == "accepted"
    assert out[f.id] == f"accepted (evidence of {d.id})"


def test_drop_decision_reports_cascaded_facts(tmp_path):
    store = Store(tmp_path / "s")
    d = store.add_decision(_decision())
    f = store.add_fact(_fact(supports=[d.id]))
    out = _ratify_impl(store, drop=[d.id])
    assert out[f.id] == f"dropped (evidence of {d.id})"


def test_list_proposed_nests_attached_and_sections_standalone(tmp_path):
    store = Store(tmp_path / "s")
    d = store.add_decision(_decision())
    store.add_fact(_fact(supports=[d.id], statement="attached one"))
    store.add_fact(_fact(statement="standalone one"))
    text = _list_proposed_impl(store)
    assert "evidence: attached one" in text
    assert "Facts:" in text and "standalone one" in text
    # the attached fact must NOT be double-listed in the standalone section
    assert text.index("attached one") == text.rindex("attached one")


def test_unknown_id_error_mentions_fact(tmp_path):
    store = Store(tmp_path / "s")
    out = _ratify_impl(store, accept=["01NOPE"])
    assert "unknown" in out["01NOPE"].lower()
    assert "fact" in out["01NOPE"].lower()


# -- fix pass: accepting a decision AND its nested fact in the same call must not yield a
# spurious error, and must not depend on which order the caller listed the two ids in.


@pytest.mark.parametrize("order", ["decision_first", "fact_first"])
def test_ratify_accept_decision_and_nested_fact_together_is_order_independent(tmp_path, order):
    store = Store(tmp_path / "s")
    d = store.add_decision(_decision())
    f = store.add_fact(_fact(supports=[d.id]))
    ids = [d.id, f.id] if order == "decision_first" else [f.id, d.id]

    out = _ratify_impl(store, accept=ids)

    assert out[d.id] == "accepted"
    assert out[f.id] == f"accepted (evidence of {d.id})"
    assert not any(str(v).startswith("error") for v in out.values())
    assert store.get_fact(f.id).status == DecisionStatus.ACCEPTED
    assert store.get_decision(d.id).status == DecisionStatus.ACCEPTED
