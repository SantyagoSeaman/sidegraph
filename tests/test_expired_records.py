"""An expired record is history in every read, the way a superseded one is.

A Decision or Fact is expired when ``valid_to`` is set and has passed; the stored ``status`` does
not matter, and a ``valid_to`` still in the future is live. The decision listing and its
filtered-answer twin live in ``test_retrieve_decisions_query.py``; this file holds the other
read surfaces: the overview, ``list_facts``, ``get_entity_history``, ``list_proposed``, the
drift count, and the places that were stricter than the rule.
see design/superpowers/specs/2026-11-15-0310-batch-a-design.md (D3, surfaces #1-#9)
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import sidegraph.cli as cli_module
import sidegraph.server as server_module
from sidegraph.retrieval import _live_facts, live_drifted_ids, top_tier_map
from sidegraph.schema import (
    AnchorBinding,
    Decision,
    DecisionKind,
    DecisionStatus,
    Descriptor,
    Fact,
    Provenance,
)
from sidegraph.store import Store
from sidegraph.store_layout import is_expired

NOW = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)


@pytest.fixture
def store(tmp_path: Path):
    s = Store(tmp_path / "t.db")
    yield s
    s.close()


def _put(store, record):
    """Write a record as it is: the write path refuses some shapes a merged store can hold."""
    with store._mutation():
        (store._write_fact if isinstance(record, Fact) else store._write_decision)(record)
    return record


def _record(title, *, fact=False, status=DecisionStatus.ACCEPTED, ended=True, future=False):
    """An accepted record that ended two hours ago (``ended``), that ends tomorrow
    (``future``), or that never ends (neither)."""
    start = datetime.now(UTC) - timedelta(hours=5)
    end = None
    if ended:
        end = start + timedelta(hours=3)
    if future:
        end = datetime.now(UTC) + timedelta(days=1)
    fields = {
        "status": status,
        "valid_from": start,
        "valid_to": end,
        "provenance": Provenance(source="manual"),
    }
    if fact:
        return Fact(statement=title, source="bench", **fields)
    return Decision(title=title, kind=DecisionKind.ADR, context="c", choice="ch", **fields)


def _anchor(store, record, name="Thing", path="src/thing.py"):
    entity = store.get_or_create_entity(Descriptor(name=name, file_path=path))
    store.add_binding(
        AnchorBinding(record_id=record.id, entity_id=entity.entity_id, tier=2, status="live")
    )
    return entity


# -- the predicate ---------------------------------------------------------------------------


def test_is_expired_follows_the_rule():
    """Red target: new code, no unfixed form. One predicate: ``valid_to <= now``, a datetime or
    an ISO string, a naive value read as UTC, no end never expired."""
    assert not is_expired(None, NOW)
    assert is_expired(NOW - timedelta(seconds=1), NOW)
    assert is_expired(NOW, NOW)
    assert not is_expired(NOW + timedelta(seconds=1), NOW)
    assert is_expired((NOW - timedelta(days=1)).isoformat(), NOW)
    assert not is_expired((NOW + timedelta(days=1)).isoformat(), NOW)
    assert is_expired("2026-11-14T12:00:00Z", NOW)
    assert is_expired(datetime(2026, 11, 14, 12, 0), NOW)
    assert not is_expired(datetime(2026, 11, 16, 12, 0), NOW)


# -- #2: the overview ------------------------------------------------------------------------


def test_the_overview_leaves_an_expired_record_out_and_counts_it(store):
    """Red target: unfixed code lists the expired accepted decision among the newest rows and
    has no ``counts["expired"]``."""
    live = _put(store, _record("live one", ended=False))
    future = _put(store, _record("valid until tomorrow", ended=False, future=True))
    gone = _put(store, _record("gone one"))

    answer = json.loads(server_module._decision_listing(store, None))

    assert answer["overview"] is True and answer["total"] == 3
    assert {row["id"] for row in answer["newest"]} == {live.id, future.id}
    assert answer["counts"]["status"] == {"accepted": 3}
    assert answer["counts"]["expired"] == 1
    everything = json.loads(server_module._decision_listing(store, None, include_superseded=True))
    rows = {row["id"]: row for row in everything["newest"]}
    assert set(rows) == {live.id, future.id, gone.id}
    assert rows[gone.id]["expired"] is True
    assert "expired" not in rows[live.id] and "expired" not in rows[future.id]


def test_a_store_with_nothing_expired_has_no_expired_count(store):
    """Red target: none, an over-reach guard (declared): a live answer does not change."""
    _put(store, _record("live one", ended=False))
    _put(store, _record("valid until tomorrow", ended=False, future=True))

    answer = json.loads(server_module._decision_listing(store, None))

    assert "expired" not in answer["counts"]
    assert all("expired" not in row for row in answer["newest"])


def test_a_partial_match_row_of_an_expired_record_carries_the_marker(store):
    """Red target: unfixed code gives the compact partial rows no ``expired`` key. An explicit
    ``status`` shows the expired records, and a row that is only a pointer must still say so."""
    gone = _put(store, _record("redis cache sizing"))
    gone_fact = _put(store, _record("redis cache sizing result", fact=True))

    answer = json.loads(
        server_module._decision_listing(
            store, None, query="redis cache eviction", status="accepted"
        )
    )

    assert answer["match"] == "partial"
    assert [r["id"] for r in answer["decisions"]] == [gone.id]
    assert answer["decisions"][0]["expired"] is True
    assert answer["facts"][0]["id"] == gone_fact.id and answer["facts"][0]["expired"] is True


# -- #3, #4: list_facts ----------------------------------------------------------------------


def test_list_facts_hides_an_expired_fact_by_default(store):
    """Red target: unfixed code lists the expired accepted fact in the default answer."""
    live = _put(store, _record("alpha live", fact=True, ended=False))
    future = _put(store, _record("alpha until tomorrow", fact=True, ended=False, future=True))
    gone = _put(store, _record("alpha gone", fact=True))

    answer = json.loads(server_module._fact_listing(store, None))

    assert {f["id"] for f in answer["facts"]} == {live.id, future.id}
    assert answer["counts"]["status"] == {"accepted": 3}
    assert answer["counts"]["expired"] == 1
    assert "expired" not in answer["facts"][0]
    assert gone.id not in {f["id"] for f in answer["facts"]}


def test_list_facts_shows_an_expired_fact_when_history_is_asked_for_and_marks_it(store):
    """Red target: unfixed code returns the fact with no ``expired`` key (the visibility half
    is a guard: it already shows with ``include_superseded``, ``ids`` or ``status``)."""
    live = _put(store, _record("alpha live", fact=True, ended=False))
    gone = _put(store, _record("alpha gone", fact=True))

    for asked in (
        {"include_superseded": True},
        {"ids": [gone.id]},
        {"status": "accepted"},
        {"query": "gone", "include_superseded": True},
    ):
        answer = json.loads(server_module._fact_listing(store, None, **asked))
        rows = {f["id"]: f for f in answer["facts"]}
        assert rows[gone.id]["expired"] is True, asked
        assert "expired" not in rows.get(live.id, {}), asked


def test_list_facts_with_no_expired_fact_is_unchanged(store):
    """Red target: none, an over-reach guard (declared): no key, no count."""
    _put(store, _record("alpha until tomorrow", fact=True, ended=False, future=True))

    answer = json.loads(server_module._fact_listing(store, None))

    assert "expired" not in answer["counts"]
    assert "expired" not in answer["facts"][0]


# -- #5: the hint ----------------------------------------------------------------------------


def test_an_empty_answer_names_expired_records_among_the_hidden_history(store):
    """Red target: unfixed code's hint reads "Superseded, rejected and retired records" and
    leaves out expired ones."""
    for text in (
        json.loads(server_module._decision_listing(store, None, query="nothing here"))["hint"],
        json.loads(server_module._fact_listing(store, None, query="nothing here"))["hint"],
    ):
        assert "History records (superseded, rejected, retired or expired) need" in text


# -- #6: get_entity_history ------------------------------------------------------------------


def test_entity_history_keeps_every_record_and_marks_the_expired_ones(store):
    """Red target: unfixed code returns the expired decision and fact with no ``expired`` key
    (the set is unchanged: history shows everything)."""
    live = _put(store, _record("live ruling", ended=False))
    gone = _put(store, _record("gone ruling"))
    gone_fact = _put(store, _record("gone measurement", fact=True))
    future = _put(store, _record("ruling until tomorrow", ended=False, future=True))
    entity = None
    for record in (live, gone, gone_fact, future):
        entity = _anchor(store, record)

    records = server_module._get_entity_history_impl(store, entity.entity_id)

    by_id = {r["id"]: r for r in records}
    assert set(by_id) == {live.id, gone.id, gone_fact.id, future.id}
    assert by_id[gone.id]["expired"] is True and by_id[gone_fact.id]["expired"] is True
    assert "expired" not in by_id[live.id] and "expired" not in by_id[future.id]


def test_the_short_row_of_an_expired_record_in_entity_history_carries_the_marker(store):
    """Red target: unfixed code's ``_history_row`` drops the marker."""
    gone = _put(store, _record("gone ruling"))

    row = server_module._history_row(
        {**gone.model_dump(mode="json"), "record_type": "decision", "expired": True}
    )

    assert row["expired"] is True
    assert "expired" not in server_module._history_row(
        {**gone.model_dump(mode="json"), "record_type": "decision"}
    )


# -- #7: list_proposed -----------------------------------------------------------------------


def test_list_proposed_tags_an_expired_proposal_and_keeps_it_in_the_queue(store):
    """Red target: unfixed code renders an expired proposal like a pending one, with no tag."""
    fresh = _put(store, _record("fresh proposal", status=DecisionStatus.PROPOSED, ended=False))
    stale = _put(store, _record("stale proposal", status=DecisionStatus.PROPOSED))
    stale_fact = _put(
        store, _record("stale measurement", fact=True, status=DecisionStatus.PROPOSED)
    )
    until = _put(
        store,
        _record(
            "proposal until tomorrow", status=DecisionStatus.PROPOSED, ended=False, future=True
        ),
    )

    text = server_module._list_proposed_impl(store)

    lines = {line.split()[0]: line for line in text.splitlines() if line[:1].isalnum()}
    assert "[expired]" in lines[stale.id] and "[expired]" in lines[stale_fact.id]
    assert "[expired]" not in lines[fresh.id] and "[expired]" not in lines[until.id]
    assert store.pending_ratification_counts()[:2] == (3, 1)


def _proposal_with_evidence(store):
    """A proposed decision with three proposed supporting facts: one expired, one that ends
    tomorrow, one that never ends. Returns the decision and the facts in that order."""
    decision = _put(store, _record("proposal with evidence", status=DecisionStatus.PROPOSED))
    facts = []
    for label, kwargs in (
        ("lapsed evidence", {}),
        ("evidence until tomorrow", {"ended": False, "future": True}),
        ("open evidence", {"ended": False}),
    ):
        fact = _record(label, fact=True, status=DecisionStatus.PROPOSED, **kwargs)
        facts.append(_put(store, fact.model_copy(update={"supports": [decision.id]})))
    return decision, facts


def _evidence_lines(block):
    return {
        line.split("(")[-1].rstrip(")"): line for line in block.splitlines() if "evidence:" in line
    }


def test_nested_evidence_of_a_proposal_tags_only_the_expired_fact_in_list_proposed(store):
    """Red target: the nested ``evidence:`` line of an expired supporting fact had no
    ``[expired]`` tag, though the same fact listed on its own carries it."""
    decision, (lapsed, tomorrow, never) = _proposal_with_evidence(store)

    lines = _evidence_lines(server_module._format_proposed_decision_block(store, decision))

    assert "[expired]" in lines[lapsed.id]
    assert "[expired]" not in lines[tomorrow.id] and "[expired]" not in lines[never.id]
    listed = _evidence_lines(server_module._list_proposed_impl(store))
    assert "[expired]" in listed[lapsed.id] and "[expired]" not in listed[tomorrow.id]


def test_nested_evidence_of_a_proposal_tags_only_the_expired_fact_in_the_ratify_cli(store):
    """Red target: as above, for the bare ``sidegraph-ratify`` listing (``cli.py``), which
    keeps its own copy of the block renderer."""
    decision, (lapsed, tomorrow, never) = _proposal_with_evidence(store)

    lines = _evidence_lines(cli_module._format_proposed_decision_block(store, decision))

    assert "[expired]" in lines[lapsed.id]
    assert "[expired]" not in lines[tomorrow.id] and "[expired]" not in lines[never.id]


# -- #8: the Stop drift nudge ----------------------------------------------------------------


def test_the_drift_count_leaves_an_expired_record_out(store):
    """Red target: unfixed code counts the expired accepted decision as drifted."""
    live = _put(store, _record("live ruling", ended=False))
    future = _put(store, _record("ruling until tomorrow", ended=False, future=True))
    gone = _put(store, _record("gone ruling"))

    assert live_drifted_ids(store, {"c1": [live.id, future.id, gone.id]}) == frozenset(
        {live.id, future.id}
    )


# -- #9: the places that were stricter than the rule -----------------------------------------


def test_a_fact_that_ends_in_the_future_is_still_surfaced():
    """Red target: unfixed ``_live_facts`` drops any fact with a ``valid_to`` set."""
    live = _record("never ends", fact=True, ended=False)
    future = _record("ends tomorrow", fact=True, ended=False, future=True)
    gone = _record("ended", fact=True)

    assert _live_facts([live, future, gone]) == [live, future]


def test_the_session_map_lists_a_record_that_ends_in_the_future_and_not_an_ended_one(store):
    """Red target: unfixed code hides the accepted decision and the fact whose ``valid_to`` is
    still ahead, and the proposed decision likewise."""
    for title, kwargs in (
        ("decision until tomorrow", {"ended": False, "future": True}),
        ("decision that ended", {}),
        ("proposal until tomorrow", {"ended": False, "future": True, "proposed": True}),
        ("proposal that ended", {"proposed": True}),
    ):
        status = (
            DecisionStatus.PROPOSED if kwargs.pop("proposed", False) else DecisionStatus.ACCEPTED
        )
        _put(store, _record(title, status=status, **kwargs))
    for title, kwargs in (
        ("fact until tomorrow", {"ended": False, "future": True}),
        ("fact that ended", {}),
    ):
        _put(store, _record(title, fact=True, **kwargs))

    text = top_tier_map(store, None)

    assert "decision until tomorrow" in text and "proposal until tomorrow" in text
    assert "fact until tomorrow" in text
    assert "decision that ended" not in text and "proposal that ended" not in text
    assert "fact that ended" not in text
