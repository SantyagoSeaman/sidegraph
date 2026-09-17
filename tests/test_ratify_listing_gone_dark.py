"""The ratify listing must mark records that have stopped surfacing.

Practitioner re-review round 2, staff engineer: "Nothing marks a gone-dark record where I
review it." The surfacing window is only humane if the queue tells you which items it has
already stopped delivering — otherwise the reviewer cannot tell an urgent backlog from an
inert one, and the window becomes a silent drop.

Red target: unfixed code renders every proposal identically.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sidegraph.schema import Decision, DecisionKind, DecisionStatus, Provenance
from sidegraph.server import _list_proposed_impl
from sidegraph.store import Store

MARK = "[not surfacing]"


def _proposal(store: Store, title: str, age_days: int) -> Decision:
    d = Decision(
        title=title,
        kind=DecisionKind.ADR,
        status=DecisionStatus.PROPOSED,
        context="c",
        choice="ch",
        valid_from=datetime.now(UTC) - timedelta(days=age_days),
        provenance=Provenance(source="agent"),
    )
    store._write_decision(d)
    store._conn.commit()
    return d


def test_aged_out_proposal_is_marked_in_the_listing(tmp_path):
    store = Store(tmp_path / "s")
    _proposal(store, "still delivering", age_days=1)
    _proposal(store, "gone dark", age_days=90)
    text = _list_proposed_impl(store)
    lines = [ln for ln in text.splitlines() if "gone dark" in ln or "still delivering" in ln]
    assert any(MARK in ln and "gone dark" in ln for ln in lines)
    assert not any(MARK in ln and "still delivering" in ln for ln in lines)


def test_regulated_mode_marks_every_proposal(tmp_path, monkeypatch):
    monkeypatch.setenv("SIDEGRAPH_UNRATIFIED", "off")
    store = Store(tmp_path / "s")
    _proposal(store, "fresh but withheld", age_days=0)
    assert MARK in _list_proposed_impl(store)


def test_listing_still_shows_the_record_itself(tmp_path):
    """Over-reach guard (declared): marking must never hide the item — the queue is where
    an aged-out proposal is still meant to be reviewable."""
    store = Store(tmp_path / "s")
    d = _proposal(store, "gone dark", age_days=90)
    text = _list_proposed_impl(store)
    assert "gone dark" in text
    assert d.id in text
