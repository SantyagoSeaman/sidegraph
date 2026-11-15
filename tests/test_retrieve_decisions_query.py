"""``retrieve_decisions(query=...)``: facts, anchors, the partial match and the mistake order.

The listing's bounds and filters are tested in ``test_server_decision_listing.py``; this file
holds what a query returns beyond the decisions whose text holds every word.
# see design/superpowers/specs/2026-11-14-retrieve-decisions-main-read-tool-design.md
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import sidegraph.server as server_module
from sidegraph.schema import (
    AnchorBinding,
    Decision,
    DecisionKind,
    DecisionStatus,
    Descriptor,
    Fact,
    Provenance,
)
from sidegraph.server import _decision_listing, _decision_listing_result
from sidegraph.store import VOLATILE_STALE_KEY, Store
from sidegraph.store_layout import MISTAKE_KINDS


@pytest.fixture
def store(tmp_path: Path):
    s = Store(tmp_path / "t.db")
    yield s
    s.close()


def _add(
    store, title, *, kind="gotcha", status="accepted", choice="ch", hours_ago=1.0, supersedes=None
):
    decision = Decision(
        title=title,
        kind=DecisionKind(kind),
        status=DecisionStatus(status),
        context="c",
        choice=choice,
        valid_from=datetime.now(UTC) - timedelta(hours=hours_ago),
        supersedes=supersedes,
        provenance=Provenance(source="manual"),
    )
    store.add_decision(decision)
    return decision


def _add_fact(
    store, statement, *, source="bench", status="accepted", hours_ago=1.0, supersedes=None
):
    fact = Fact(
        statement=statement,
        source=source,
        status=DecisionStatus(status),
        valid_from=datetime.now(UTC) - timedelta(hours=hours_ago),
        supersedes=supersedes,
        provenance=Provenance(source="manual"),
    )
    store.add_fact(fact)
    return fact


def _anchor(store, record, name, file_path, *, status="live", tier=2):
    entity = store.get_or_create_entity(Descriptor(name=name, file_path=file_path))
    store.add_binding(
        AnchorBinding(record_id=record.id, entity_id=entity.entity_id, tier=tier, status=status)
    )
    return entity


def _ask(store, reader=None, **kwargs) -> dict:
    return json.loads(_decision_listing(store, reader, **kwargs))


def _ids(rows) -> list[str]:
    return [row["id"] for row in rows]


# -- facts --------------------------------------------------------------------------------------


def test_query_returns_matching_facts_under_their_own_key(store):
    decision = _add(store, "alpha lock order")
    fact = _add_fact(store, "alpha takes 40 ms")
    _add_fact(store, "beta takes 90 ms")

    answer = _ask(store, query="alpha")

    assert _ids(answer["decisions"]) == [decision.id]
    assert _ids(answer["facts"]) == [fact.id]
    assert answer["facts_matched"] == 1
    assert (
        "kind" not in answer["facts"][0] and answer["facts"][0]["statement"] == "alpha takes 40 ms"
    )


def test_decision_counts_do_not_count_facts(store):
    _add(store, "alpha one")
    _add(store, "alpha two")
    _add_fact(store, "alpha fact")

    answer = _ask(store, query="alpha")

    assert (answer["matched"], answer["returned"], answer["omitted"]) == (2, 2, 0)
    assert answer["returned"] == len(answer["decisions"])


def test_no_fact_match_leaves_the_answer_as_it_was(store):
    _add(store, "alpha one")
    _add_fact(store, "beta fact")

    answer = _ask(store, query="alpha")

    assert "facts" not in answer and "facts_matched" not in answer
    assert list(answer) == ["overview", "matched", "returned", "omitted", "decisions", "hint"]


def test_a_fact_never_displaces_a_decision(store):
    """Five decisions fit with 240 characters to spare. A matching fact does not fit, and the
    answer that would name it with the whole cut hint puts the fifth decision out. The five
    stay; the fact is counted and its id is listed, under a hint without its last sentence."""
    kept = [_add(store, f"alpha {i}", choice="x" * 900, hours_ago=i + 1) for i in range(5)]
    whole = _decision_listing(store, None, query="alpha", budget_chars=40000)
    budget = len(whole) + 240
    assert budget > 4000 and json.loads(whole)["returned"] == 5
    fact = _add_fact(store, "alpha " + "y" * 400)

    text = _decision_listing(store, None, query="alpha", budget_chars=budget)
    answer = json.loads(text)

    assert len(text) <= budget
    assert _ids(answer["decisions"]) == [d.id for d in kept] and answer["omitted"] == 0
    assert (answer["facts_matched"], answer["facts"]) == (1, [])
    assert answer["omitted_fact_ids"] == [fact.id]
    assert answer["hint"] == (
        "1 more fact matched; the character budget cut the list. "
        "Read them with list_facts(ids=[...]) (ids in omitted_fact_ids)."
    )


def test_facts_take_the_room_decisions_leave(store):
    _add(store, "alpha decision", choice="x" * 1500)
    kept = _add_fact(store, "alpha small", hours_ago=1)
    cut = _add_fact(store, "alpha " + "y" * 3000, hours_ago=2)

    answer = _ask(store, query="alpha", budget_chars=4000)

    assert answer["returned"] == 1 and answer["omitted"] == 0
    assert answer["facts_matched"] == 2 and _ids(answer["facts"]) == [kept.id]
    assert answer["omitted_fact_ids"] == [cut.id]
    assert "omitted_ids" not in answer
    assert answer["hint"].startswith("1 more fact matched; the character budget cut the list.")
    assert "list_facts(ids=[...])" in answer["hint"] and "omitted_fact_ids" in answer["hint"]


def test_a_kind_filter_keeps_facts_out(store):
    _add(store, "alpha gotcha")
    _add_fact(store, "alpha fact")

    assert "facts" not in _ask(store, query="alpha", kind="gotcha")


def test_fact_filters_follow_list_facts(store):
    live = _add_fact(store, "alpha live")
    old = _add_fact(store, "alpha old", status="superseded")

    assert _ids(_ask(store, query="alpha")["facts"]) == [live.id]
    assert set(_ids(_ask(store, query="alpha", include_superseded=True)["facts"])) == {
        live.id,
        old.id,
    }
    assert _ids(_ask(store, query="alpha", status="superseded")["facts"]) == [old.id]
    assert _ids(_ask(store, query="alpha", ids=[old.id])["facts"]) == [old.id]


def test_a_fact_is_searched_by_its_source_too(store):
    """The query words are looked for in a fact's ``statement`` and in its ``source``."""
    fact = _add_fact(store, "takes 40 ms", source="zebra benchmark")
    _add_fact(store, "takes 90 ms", source="other benchmark")

    answer = _ask(store, query="zebra")

    assert _ids(answer["facts"]) == [fact.id] and answer["facts_matched"] == 1


def test_the_files_filter_keeps_only_anchored_facts(store):
    """Two facts match the query; ``files`` keeps the one anchored to the file."""
    near = _add_fact(store, "alpha near")
    _add_fact(store, "alpha far")
    _anchor(store, near, "Lock", "src/a.py")

    both = _ask(store, query="alpha")
    narrowed = _ask(store, query="alpha", files=["src/a.py"])

    assert both["facts_matched"] == 2
    assert _ids(narrowed["facts"]) == [near.id] and narrowed["facts_matched"] == 1


def test_facts_omit_provenance_by_default(store):
    _add_fact(store, "alpha fact")

    assert "provenance" not in _ask(store, query="alpha")["facts"][0]
    assert "provenance" in _ask(store, query="alpha", include_provenance=True)["facts"][0]


def test_a_fact_is_never_a_sent_earlier_row_and_is_not_claimed(store):
    decision = _add(store, "alpha decision")
    fact = _add_fact(store, "alpha fact")

    text, full_ids = _decision_listing_result(
        store, None, query="alpha", sent=frozenset({decision.id, fact.id})
    )
    answer = json.loads(text)

    assert answer["decisions"][0]["sent_earlier"] is True
    assert answer["facts"][0]["statement"] == "alpha fact"
    assert full_ids == []
    text, full_ids = _decision_listing_result(store, None, query="alpha")
    assert full_ids == [decision.id]


def test_list_facts_query_is_unchanged(store):
    """``list_facts`` keeps the every-word rule on the text: no anchors, no partial match."""
    fact = _add_fact(store, "alpha fact")
    _anchor(store, fact, "Worker", "pkg/worker.py")

    assert json.loads(server_module._fact_listing(store, None, query="worker"))["matched"] == 0
    assert json.loads(server_module._fact_listing(store, None, query="alpha zebra"))["matched"] == 0


def test_a_proposed_fact_follows_the_proposal_policy(store, monkeypatch):
    """The fact pool comes from ``_list_facts_impl``, so regulated mode withholds a proposed
    fact here as it does in ``list_facts``."""
    draft = _add_fact(store, "alpha draft", status="proposed")

    assert _ids(_ask(store, query="alpha")["facts"]) == [draft.id]
    monkeypatch.setenv("SIDEGRAPH_UNRATIFIED", "off")
    assert "facts" not in _ask(store, query="alpha")


def test_a_fact_past_its_validity_window_is_history(store):
    live = _add_fact(store, "alpha now")
    start = datetime.now(UTC) - timedelta(hours=3)
    ended = Fact(
        statement="alpha then",
        source="bench",
        status=DecisionStatus.ACCEPTED,
        valid_from=start,
        valid_to=start + timedelta(hours=1),
        provenance=Provenance(source="manual"),
    )
    store.add_fact(ended)

    assert _ids(_ask(store, query="alpha")["facts"]) == [live.id]
    both = _ask(store, query="alpha", include_superseded=True)["facts"]
    assert set(_ids(both)) == {live.id, ended.id}
    assert _ids(_ask(store, query="alpha", ids=[ended.id])["facts"]) == [ended.id]


# -- accepted before proposed, and the cascade ---------------------------------------------------


def test_accepted_records_come_before_proposals_in_every_group(store):
    """Unratified memory never outranks accepted memory: the records of every group that are
    not proposals are placed first, then the proposals of every group. Each half keeps the
    order of a filtered listing, mistakes first, then newest."""
    draft_gotcha = _add(store, "alpha draft gotcha", status="proposed", hours_ago=1)
    draft_adr = _add(store, "alpha draft adr", kind="adr", status="proposed", hours_ago=2)
    kept_adr = _add(store, "alpha kept adr", kind="adr", hours_ago=3)
    kept_gotcha = _add(store, "alpha kept gotcha", hours_ago=9)
    kept_fact = _add_fact(store, "alpha kept fact", hours_ago=5)
    draft_fact = _add_fact(store, "alpha draft fact", status="proposed", hours_ago=1)

    answer = _ask(store, query="alpha")

    assert _ids(answer["decisions"]) == [kept_gotcha.id, kept_adr.id, draft_gotcha.id, draft_adr.id]
    assert _ids(answer["facts"]) == [kept_fact.id, draft_fact.id]
    # The accepted fact is placed before the proposed decisions, whose group comes first.
    three = _ask(store, query="alpha", limit=3)
    assert _ids(three["decisions"]) == [kept_gotcha.id, kept_adr.id]
    assert _ids(three["facts"]) == [kept_fact.id]
    assert three["omitted_ids"] == [draft_gotcha.id, draft_adr.id]
    assert three["omitted_fact_ids"] == [draft_fact.id]


def test_a_proposal_takes_only_the_room_accepted_records_leave(store):
    """A proposed decision and an accepted fact match. Under a limit of one, and under a
    budget that holds one of them, the answer returns the accepted fact."""
    draft = _add(store, "alpha draft", status="proposed", choice="x" * 4200)
    fact = _add_fact(store, "alpha accepted fact")

    for bound in ({"limit": 1}, {"budget_chars": 4000}):
        answer = _ask(store, query="alpha", **bound)

        assert answer["decisions"] == [] and _ids(answer["facts"]) == [fact.id]
        assert (answer["matched"], answer["returned"], answer["omitted"]) == (1, 0, 1)
        assert answer["omitted_ids"] == [draft.id]


def test_a_later_group_never_puts_out_a_record_of_an_earlier_one(store):
    """Two decisions and a fact fit with 200 characters to spare. A proposed decision matches
    too and does not fit. The fact, placed in an earlier group, stays, and the proposal is
    counted and named as omitted."""
    for i in range(2):
        _add(store, f"alpha {i}", choice="x" * 1900, hours_ago=i + 2)
    fact = _add_fact(store, "alpha " + "y" * 300)
    whole = _decision_listing(store, None, query="alpha", budget_chars=40000)
    budget = len(whole) + 200
    assert budget > 4000 and len(json.loads(whole)["facts"]) == 1
    draft = _add(store, "alpha draft", status="proposed", choice="z" * 900)

    text = _decision_listing(store, None, query="alpha", budget_chars=budget)
    answer = json.loads(text)

    assert len(text) <= budget
    assert answer["returned"] == 2 and _ids(answer["facts"]) == [fact.id]
    assert (answer["matched"], answer["omitted"], answer["omitted_ids"]) == (3, 1, [draft.id])
    assert "omitted_fact_ids" not in answer


def test_a_refused_group_is_counted_and_its_ids_are_listed(store):
    """An accepted decision fits with 200 characters to spare. A proposed decision holds the
    word too and has no room. The answer returns the accepted one and never reads as
    complete: the proposal is counted, its id is listed as omitted, and the hint says so."""
    kept = _add(store, "alpha kept", kind="adr", choice="x" * 3800, hours_ago=5)
    alone = _decision_listing(store, None, query="alpha", budget_chars=40000)
    budget = len(alone) + 200
    assert budget > 4000 and json.loads(alone)["hint"] is None
    draft = _add(store, "alpha draft", kind="adr", status="proposed", choice="y" * 900)

    text = _decision_listing(store, None, query="alpha", budget_chars=budget)
    answer = json.loads(text)

    assert len(text) <= budget
    assert _ids(answer["decisions"]) == [kept.id]
    assert (answer["matched"], answer["returned"], answer["omitted"]) == (2, 1, 1)
    assert answer["omitted_ids"] == [draft.id]
    assert answer["hint"] == (
        "1 more matched; the character budget cut the list. "
        "Read them with retrieve_decisions(ids=[...]) (ids in omitted_ids)."
    )


def test_the_account_of_a_refused_group_shrinks_before_a_placed_record_gives_way(store):
    """The same two records under every budget from the exact fit of the accepted one to 300
    characters more. No answer is over its budget, every answer counts both records and has
    a hint. As the room shrinks the hint loses its advice, then its reading instruction, the
    id gives way to the unlisted count, and only when the counts and one sentence do not fit
    does the accepted record give way; it is then the first id listed. The id is never
    dropped to keep the reading instruction."""
    kept = _add(store, "alpha kept", kind="adr", choice="x" * 3800, hours_ago=5)
    exact = len(_decision_listing(store, None, query="alpha", budget_chars=40000))
    assert exact > 4000
    draft = _add(store, "alpha draft", kind="adr", status="proposed", choice="y" * 900)
    cut = "1 more matched; the character budget cut the list."
    read = " Read them with retrieve_decisions(ids=[...]) (ids in omitted_ids)."
    advice = " Narrow with files=[...] or query=..., or raise budget_chars (max 40000)."
    forms: list[str] = []
    first_kept = None

    for spare in range(301):
        budget = exact + spare
        text = _decision_listing(store, None, query="alpha", budget_chars=budget)
        answer = json.loads(text)
        assert len(text) <= budget
        assert answer["matched"] == 2 and answer["hint"] is not None
        listed = answer["omitted_ids"] + ["unlisted"] * answer.get("omitted_ids_unlisted", 0)
        if answer["returned"]:
            first_kept = spare if first_kept is None else first_kept
            assert _ids(answer["decisions"]) == [kept.id] and listed in ([draft.id], ["unlisted"])
        else:
            assert first_kept is None and listed == [kept.id, draft.id]
        hint = answer["hint"].replace(str(budget), "N")
        assert hint != cut + read or listed == [draft.id]
        if hint not in forms:
            forms.append(hint)

    gave_way = "2 matched, but the first one does not fit in budget_chars=N. Raise budget_chars"
    assert forms[0].startswith(gave_way)
    assert forms[1:] == [cut, cut + read, cut + read + advice]
    # The account in its shortest form: two keys and one sentence.
    assert 0 < first_kept <= 100


def test_the_hint_counts_decisions_and_facts_apart(store):
    _add(store, "alpha one", choice="x" * 3000, hours_ago=1)
    _add(store, "alpha two", choice="x" * 3000, hours_ago=2)
    for i in range(2):
        _add_fact(store, f"alpha {i} " + "y" * 900)

    answer = _ask(store, query="alpha", budget_chars=4000)

    assert (answer["returned"], answer["omitted"], answer["facts"]) == (1, 1, [])
    assert answer["hint"].startswith(
        "1 more decision and 2 more facts matched; the character budget cut the list."
    )
    assert "retrieve_decisions(ids=[...]) (ids in omitted_ids) and list_facts" in answer["hint"]


def test_a_fact_too_large_for_the_budget_is_counted_as_a_fact(store):
    _add_fact(store, "alpha " + "y" * 4200)

    answer = _ask(store, query="alpha", budget_chars=4000)

    assert (answer["matched"], answer["facts_matched"], answer["facts"]) == (0, 1, [])
    assert answer["hint"].startswith("1 fact matched, but the first one does not fit")


def test_a_call_without_a_query_keeps_its_order(store):
    """Only a query moves proposals behind accepted records. A filtered call without a query
    is the listing as it was."""
    kept = _add(store, "kept adr", kind="adr", hours_ago=9)
    draft = _add(store, "draft gotcha", status="proposed", hours_ago=1)

    answer = _ask(store, status=["accepted", "proposed"])

    assert _ids(answer["decisions"]) == [draft.id, kept.id]


def test_omitted_decision_ids_are_listed_before_omitted_fact_ids(store):
    """An accepted decision fits; a matching fact and a proposed decision do not. Under every
    budget from the exact fit up, a fact id is listed only when every omitted decision id is,
    and it is the fact id that gives way first when the room runs short."""
    _add(store, "alpha kept", kind="adr", choice="x" * 3800, hours_ago=5)
    exact = len(_decision_listing(store, None, query="alpha", budget_chars=40000))
    assert exact > 4000
    fact = _add_fact(store, "alpha " + "y" * 900)
    draft = _add(store, "alpha draft", kind="adr", status="proposed", choice="z" * 900)
    listed_both = decision_only = False

    for spare in range(500):
        text = _decision_listing(store, None, query="alpha", budget_chars=exact + spare)
        answer = json.loads(text)
        assert len(text) <= exact + spare
        if answer["returned"] != 1:
            continue
        decisions_listed = answer.get("omitted_ids") == [draft.id]
        if answer.get("omitted_fact_ids"):
            assert decisions_listed and "omitted_ids_unlisted" not in answer
            assert answer["omitted_fact_ids"] == [fact.id]
            listed_both = True
        elif decisions_listed:
            assert answer["omitted_fact_ids_unlisted"] == 1
            decision_only = True

    assert listed_both and decision_only


def test_the_first_record_not_fitting_counts_decisions_and_facts_apart(store):
    _add(store, "alpha big", kind="adr", choice="x" * 4200)
    _add_fact(store, "alpha small")

    answer = _ask(store, query="alpha", budget_chars=4000)

    assert (answer["returned"], answer["facts"]) == (0, [])
    assert answer["hint"] == (
        "1 decision and 1 fact matched, but the first one does not fit in budget_chars=4000. "
        "Raise budget_chars (max 40000)."
    )


def test_a_refused_group_under_a_limit_keeps_the_limit_hint_whole_or_shortened(store):
    """The refusal's shortened hint is the one the limit hint is cut to as well: the count
    and the bound first, then the reading instruction, then the advice."""
    _add(store, "alpha kept", kind="adr", choice="x" * 3800, hours_ago=5)
    exact = len(_decision_listing(store, None, query="alpha", limit=1, budget_chars=40000))
    draft = _add(store, "alpha draft", kind="adr", status="proposed", choice="y" * 900)
    cut = "1 more matched; limit=1 cut the list."
    read = " Read them with retrieve_decisions(ids=[...]) (ids in omitted_ids)."
    advice = " Narrow with files=[...] or query=..., or raise limit (max 100)."
    forms: list[str] = []

    for spare in range(301):
        text = _decision_listing(store, None, query="alpha", limit=1, budget_chars=exact + spare)
        answer = json.loads(text)
        assert len(text) <= exact + spare and answer["matched"] == 2
        if answer["returned"]:
            assert _ids(answer["decisions"]) != [draft.id]
            if answer["hint"] not in forms:
                forms.append(answer["hint"])

    assert forms == [cut, cut + read, cut + read + advice]


# -- anchors ------------------------------------------------------------------------------------


def _cold(store, record) -> Store:
    """The same store after an external edit of ``record``'s file and a reopen: the index is
    rebuilt, every binding row reads the placeholder ``live``, and the statuses the last sync
    computed survive only in the kept-status column. The caller closes the result."""
    path = store.path / "decisions" / f"{record.id}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["context"] = "an external edit"
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    store.close()
    cold = Store(store.path)
    assert cold.get_meta(VOLATILE_STALE_KEY) == "1"
    return cold


def test_query_finds_a_record_by_the_name_of_its_anchor(store):
    decision = _add(store, "lock order")
    _anchor(store, decision, "parseTaskLines", "src/tasks.ts")

    assert _ids(_ask(store, query="parseTaskLines")["decisions"]) == [decision.id]
    assert _ids(_ask(store, query="lock tasks.ts")["decisions"]) == [decision.id]
    assert _ask(store, query="parseTaskLines zebra")["matched"] == 0


def test_an_anchor_match_needs_an_anchor_for_every_word_the_text_lacks(store):
    """Two words are missing from the text and only one is held by an anchor: no match."""
    decision = _add(store, "lock order")
    _anchor(store, decision, "Worker", "pkg/worker.py")

    assert _ids(_ask(store, query="lock worker")["decisions"]) == [decision.id]
    assert _ask(store, query="lock worker zebra")["matched"] == 0
    assert _ask(store, query="worker zebra")["matched"] == 0


def test_anchor_matches_are_newest_first_within_a_kind(store):
    old = _add(store, "an old gotcha", kind="gotcha", hours_ago=30)
    _anchor(store, old, "Worker", "pkg/worker.py")
    new = _add(store, "a new gotcha", kind="gotcha", hours_ago=2)
    _anchor(store, new, "Worker", "pkg/worker.py")

    assert _ids(_ask(store, query="worker")["decisions"]) == [new.id, old.id]
    assert _ids(_ask(store, query="worker", limit=1)["decisions"]) == [new.id]


def test_an_index_row_with_a_name_or_path_that_is_not_text_is_skipped(store):
    """A row edited outside Sidegraph can parse and still hold a number for a name or a path.
    That one value is left out; the query answers, and what the row still holds is searched."""
    decision = _add(store, "lock order")
    odd = _anchor(store, decision, "Worker", "pkg/worker.py")
    other = _add(store, "queue order")
    _anchor(store, other, "Runner", "pkg/runner.py")
    nameless = _add(store, "retry order")
    nameless_entity = _anchor(store, nameless, "Retrier", "pkg/retrier.py")
    for entity, field, value in (
        (odd, "canonical_name", 7),
        (nameless_entity, "file_path", 7),
    ):
        row = store._conn.execute(
            "SELECT data FROM entities WHERE entity_id = ?", (entity.entity_id,)
        ).fetchone()
        body = json.loads(row["data"])
        if field == "file_path":
            body["descriptor"]["file_path"] = value
        else:
            body[field] = value
        store._conn.execute(
            "UPDATE entities SET data = ? WHERE entity_id = ?",
            (json.dumps(body), entity.entity_id),
        )
    store._conn.commit()

    assert _ids(_ask(store, query="runner")["decisions"]) == [other.id]
    by_path = _ask(store, query="worker")["decisions"]
    assert _ids(by_path) == [decision.id] and by_path[0]["matched_anchors"] == ["pkg/worker.py"]
    by_name = _ask(store, query="retrier")["decisions"]
    assert _ids(by_name) == [nameless.id] and by_name[0]["matched_anchors"] == ["Retrier"]
    assert _ask(store, query="7")["matched"] == 0


def test_an_anchor_matched_record_says_which_anchor_matched(store):
    """``matched_anchors`` comes first in the record and holds the stored names or paths that
    hold a word the record's own text lacks. A text match carries no such key."""
    by_anchor = _add(store, "lock order")
    _anchor(store, by_anchor, "parseTaskLines", "src/tasks.ts")
    _anchor(store, by_anchor, "Unrelated", "src/other.ts")
    in_text = _add(store, "parseTaskLines splits on CRLF", kind="adr")
    _anchor(store, in_text, "parseTaskLines", "src/tasks.ts")

    by_name = _ask(store, query="parsetasklines")["decisions"]
    by_path = _ask(store, query="lock tasks.ts")["decisions"]

    assert _ids(by_name) == [in_text.id, by_anchor.id]
    assert "matched_anchors" not in by_name[0]
    assert next(iter(by_name[1])) == "matched_anchors"
    assert by_name[1]["matched_anchors"] == ["parseTaskLines"]
    assert by_path[0]["matched_anchors"] == ["src/tasks.ts"]


def test_anchor_matches_come_after_every_text_match(store):
    by_anchor = _add(store, "an unrelated gotcha", kind="gotcha", hours_ago=1)
    _anchor(store, by_anchor, "Worker", "pkg/worker.py")
    in_text = _add(store, "the worker pool size", kind="adr", hours_ago=50)

    answer = _ask(store, query="worker")

    assert _ids(answer["decisions"]) == [in_text.id, by_anchor.id]


def test_an_anchor_match_never_displaces_a_text_match(store):
    in_text = _add(store, "the worker pool size", kind="adr", choice="x" * 2500, hours_ago=50)
    by_anchor = _add(store, "an unrelated gotcha", kind="gotcha", choice="y" * 2500)
    _anchor(store, by_anchor, "Worker", "pkg/worker.py")

    answer = _ask(store, query="worker", budget_chars=4000)

    assert _ids(answer["decisions"]) == [in_text.id]


def test_an_anchor_match_never_puts_out_a_text_matched_fact(store):
    """Five decisions and a fact fit with 200 characters to spare. A decision that matches
    only through its anchor does not fit. The fact stays, and the anchor match is counted
    and named as omitted."""
    for i in range(5):
        _add(store, f"alpha {i}", choice="x" * 900, hours_ago=i + 1)
    fact = _add_fact(store, "alpha " + "y" * 400)
    whole = _decision_listing(store, None, query="alpha", budget_chars=40000)
    budget = len(whole) + 200
    assert budget > 4000 and _ids(json.loads(whole)["facts"]) == [fact.id]
    by_anchor = _add(store, "unrelated", choice="z" * 2000)
    _anchor(store, by_anchor, "alpha", "pkg/worker.py")

    text = _decision_listing(store, None, query="alpha", budget_chars=budget)
    answer = json.loads(text)

    assert len(text) <= budget
    assert answer["returned"] == 5 and _ids(answer["facts"]) == [fact.id]
    assert (answer["matched"], answer["omitted"], answer["omitted_ids"]) == (6, 1, [by_anchor.id])
    assert answer["hint"].startswith("1 more matched; the character budget cut the list.")


def test_an_anchor_match_with_no_room_for_its_account_puts_out_the_last_record_by_name(store):
    """Two decisions and a fact fill the budget to the character. An anchor-matched record
    is added to the store. There is no room to count it, so the fact, the last record
    placed, gives way: both decisions stay, nothing is left out without being named, and the
    answer keeps to its budget."""
    for i in range(2):
        _add(store, f"worker pool {i}", choice="x" * 1900, hours_ago=i + 2)
    fact = _add_fact(store, "worker pool takes 40 ms " + "y" * 300)
    exact = len(_decision_listing(store, None, query="worker pool", budget_chars=40000))
    assert exact > 4000
    by_anchor = _add(store, "an unrelated pool gotcha", choice="z" * 900)
    _anchor(store, by_anchor, "Worker", "pkg/worker.py")

    text = _decision_listing(store, None, query="worker pool", budget_chars=exact)
    answer = json.loads(text)

    assert len(text) <= exact
    assert (answer["matched"], answer["returned"], answer["omitted"]) == (3, 2, 1)
    assert (answer["facts_matched"], answer["facts"]) == (1, [])
    assert answer["omitted_ids"] == [by_anchor.id] and answer["omitted_fact_ids"] == [fact.id]
    assert answer["hint"].startswith("1 more decision and 1 more fact matched;")


def test_an_accepted_anchor_match_comes_before_a_proposed_text_match(store):
    draft = _add_fact(store, "alpha draft", status="proposed")
    kept = _add(store, "unrelated", kind="adr")
    _anchor(store, kept, "alpha", "pkg/worker.py")

    answer = _ask(store, query="alpha", limit=1)

    assert _ids(answer["decisions"]) == [kept.id] and answer["facts"] == []
    assert answer["omitted_fact_ids"] == [draft.id]


def test_an_orphaned_binding_is_not_searched(store):
    decision = _add(store, "lock order")
    _anchor(store, decision, "Worker", "pkg/worker.py", status="orphaned")

    assert _ask(store, query="worker")["matched"] == 0


def test_a_degraded_binding_is_searched(store):
    """A record is delivered through a degraded binding, so a query finds it there too."""
    decision = _add(store, "lock order")
    _anchor(store, decision, "Worker", "pkg/worker.py", status="degraded")

    assert _ids(_ask(store, query="worker")["decisions"]) == [decision.id]


def test_a_cold_index_leaves_out_only_the_bindings_known_to_be_orphaned(store):
    """After a rebuild every row reads ``live``. The row a sync judged orphaned stays out, the
    one it judged degraded is searched, and so is a row no sync has judged yet."""
    gone = _add(store, "lock order")
    _anchor(store, gone, "Worker", "pkg/worker.py", status="orphaned")
    weak = _add(store, "queue order")
    _anchor(store, weak, "Runner", "pkg/runner.py", status="degraded")
    store.set_meta(VOLATILE_STALE_KEY, "0")  # a sync completed: these statuses are computed
    cold = _cold(store, gone)
    try:
        fresh = _add(cold, "retry order")
        _anchor(cold, fresh, "Retrier", "pkg/retrier.py")

        assert _ask(cold, query="worker")["matched"] == 0
        assert _ids(_ask(cold, query="runner")["decisions"]) == [weak.id]
        assert _ids(_ask(cold, query="retrier")["decisions"]) == [fresh.id]
    finally:
        cold.close()


def test_a_cold_index_without_the_kept_status_column_is_searched(store):
    """An index last rebuilt before the column existed kept no status: while it is cold, every
    row counts as not judged."""
    decision = _add(store, "lock order")
    _anchor(store, decision, "Worker", "pkg/worker.py")
    store._conn.execute("ALTER TABLE anchor_bindings DROP COLUMN kept_status")
    store.set_meta(VOLATILE_STALE_KEY, "1")

    assert _ids(_ask(store, query="worker")["decisions"]) == [decision.id]


def test_a_kept_status_is_dropped_when_the_descriptor_changed_since(store):
    """The kept ``orphaned`` was computed for the old descriptor. Once the entity's descriptor
    is changed in place, the row counts as not judged and is searched."""
    gone = _add(store, "lock order")
    entity = _anchor(store, gone, "Worker", "pkg/worker.py", status="orphaned")
    store.set_meta(VOLATILE_STALE_KEY, "0")
    cold = _cold(store, gone)
    try:
        assert _ask(cold, query="worker")["matched"] == 0
        moved = entity.model_copy(
            update={"descriptor": Descriptor(name="Worker", file_path="pkg/moved.py")}
        )
        cold.upsert_entity(moved)

        assert _ids(_ask(cold, query="moved.py")["decisions"]) == [gone.id]
    finally:
        cold.close()


def test_a_derived_community_entity_is_not_searched(store):
    """A synced index holds ``community:<n>`` entities that a cold one lacks. They are not
    searched, so a query answers the same on both."""
    decision = _add(store, "lock order")
    community = store.get_or_create_abstract_entity("community:78")
    store.add_binding(AnchorBinding(record_id=decision.id, entity_id=community.entity_id, tier=1))
    store.set_meta(VOLATILE_STALE_KEY, "0")

    assert store.anchor_names() == {}
    assert _ask(store, query="community")["matched"] == 0
    assert _ask(store, query="78")["matched"] == 0


def test_a_label_is_searched_without_its_scheme(store):
    """``tag:``, ``initiative:`` and ``domain:`` say what kind of label an anchor is. The
    scheme is not part of the name: the word ``tag`` must not find every tagged record."""
    decision = _add(store, "lock order")
    tag = store.get_or_create_abstract_entity("tag:locking")
    store.add_binding(AnchorBinding(record_id=decision.id, entity_id=tag.entity_id, tier=0))

    assert _ask(store, query="tag")["matched"] == 0
    found = _ask(store, query="locking")["decisions"]
    assert _ids(found) == [decision.id] and found[0]["matched_anchors"] == ["locking"]


def test_anchor_names_lists_each_name_once_in_binding_order(store):
    one = _add(store, "lock order")
    _anchor(store, one, "Worker", "pkg/worker.py")
    _anchor(store, one, "Worker::run", "pkg/worker.py")
    _anchor(store, one, "Gone", "pkg/gone.py", status="orphaned")
    tag = store.get_or_create_abstract_entity("tag:locking")
    store.add_binding(AnchorBinding(record_id=one.id, entity_id=tag.entity_id, tier=0))
    fact = _add_fact(store, "40 ms at p95")
    _anchor(store, fact, "Worker", "pkg/worker.py")
    _add(store, "no anchor")

    assert store.anchor_names() == {
        one.id: ("Worker", "pkg/worker.py", "Worker::run", "locking"),
        fact.id: ("Worker", "pkg/worker.py"),
    }
    assert not store._conn.in_transaction


def test_a_query_reads_the_anchors_in_one_store_call(store, monkeypatch):
    """No read per record: one ``anchor_names`` call serves the whole query, and none is made
    when every candidate holds every word in its text."""
    for i in range(6):
        decision = _add(store, f"record {i}", hours_ago=i + 1)
        _anchor(store, decision, f"Sym{i}", f"pkg/m{i}.py")
    calls = {"anchor_names": 0, "bindings_for_record": 0, "get_entity": 0}
    for name in calls:
        real = getattr(Store, name)

        def spy(self, *args, _real=real, _name=name, **kwargs):
            calls[_name] += 1
            return _real(self, *args, **kwargs)

        monkeypatch.setattr(Store, name, spy)

    assert _ask(store, query="record")["matched"] == 6
    assert calls == {"anchor_names": 0, "bindings_for_record": 0, "get_entity": 0}
    assert _ask(store, query="record sym3")["matched"] == 1
    assert calls == {"anchor_names": 1, "bindings_for_record": 0, "get_entity": 0}


def test_anchor_search_reads_no_graph(store):
    class NoGraph:
        def __getattr__(self, name):
            raise AssertionError(f"the query path read the graph: {name}")

    decision = _add(store, "lock order")
    _anchor(store, decision, "Worker", "pkg/worker.py")

    assert _ids(_ask(store, NoGraph(), query="worker")["decisions"]) == [decision.id]


def test_a_fact_is_found_by_its_anchor_too(store):
    fact = _add_fact(store, "40 ms at p95")
    _anchor(store, fact, "Worker", "pkg/worker.py")

    found = _ask(store, query="worker")["facts"]

    assert _ids(found) == [fact.id] and found[0]["matched_anchors"] == ["Worker", "pkg/worker.py"]


# -- partial match ------------------------------------------------------------------------------

_PARTIAL_HINT = (
    "No record holds every word of the query. These hold at least 2 of its 4 words; "
    "matched_words on each says which. Read the ones that are about your question with "
    "retrieve_decisions(ids=[...]) or list_facts(ids=[...])."
)


def test_no_full_match_returns_rows_of_the_records_holding_two_words(store):
    two = _add(store, "cold index delivery", choice="x" * 900)
    one = _add(store, "cold start")
    _add(store, "unrelated")

    answer = _ask(store, query="cold index hook zebra")

    assert answer["match"] == "partial"
    assert _ids(answer["decisions"]) == [two.id]
    assert one.id not in _ids(answer["decisions"])
    row = answer["decisions"][0]
    assert list(row) == ["matched_words", "id", "kind", "status", "title", "valid_from"]
    assert row["matched_words"] == ["cold", "index"] and row["title"] == "cold index delivery"
    assert answer["hint"] == _PARTIAL_HINT


def test_a_partial_fact_is_a_row_with_its_statement_clipped(store):
    short = _add_fact(store, "cold index took 3 s", hours_ago=1)
    long = _add_fact(store, "cold index " + "y" * 900, hours_ago=2)

    answer = _ask(store, query="cold index zebra")

    assert _ids(answer["facts"]) == [short.id, long.id] and answer["facts_matched"] == 2
    assert all(
        list(row) == ["matched_words", "id", "status", "statement"] for row in answer["facts"]
    )
    assert answer["facts"][0]["statement"] == "cold index took 3 s"
    clipped = answer["facts"][1]["statement"]
    assert len(clipped) <= 240 and clipped.endswith("more characters]")


def test_the_rows_of_a_partial_answer_are_not_claimed_as_sent(store):
    """A row points at a record. It is not a record sent in full: nothing is claimed, and a
    record sent earlier is the same row as any other. ``ids`` returns the body."""
    decision = _add(store, "cold index delivery")

    text, full_ids = _decision_listing_result(
        store, None, query="cold index zebra", sent=frozenset({decision.id})
    )

    assert full_ids == []
    assert "sent_earlier" not in json.loads(text)["decisions"][0]
    assert _ask(store, ids=[decision.id])["decisions"][0]["choice"] == "ch"


def test_a_partial_answer_journals_no_record_as_delivered(store, monkeypatch):
    journaled = []
    monkeypatch.setattr(server_module, "_telemetry_enabled", lambda: True)
    monkeypatch.setattr(
        server_module, "_record_listing", lambda store, tool, ids: journaled.append(ids)
    )
    _add(store, "cold index delivery")

    text = _decision_listing(store, None, query="cold index zebra")
    server_module._journal_listing(store, "retrieve_decisions", text, "decisions")

    assert json.loads(text)["match"] == "partial" and journaled == [[]]


def test_a_partial_answer_is_cut_like_any_other(store):
    first = _add(store, "cold index delivery", hours_ago=1)
    second = _add(store, "cold index rebuild", hours_ago=2)
    fact = _add_fact(store, "cold index took 3 s")

    answer = _ask(store, query="cold index zebra", limit=1)

    assert _ids(answer["decisions"]) == [first.id] and answer["facts"] == []
    assert answer["omitted_ids"] == [second.id] and answer["omitted_fact_ids"] == [fact.id]
    assert answer["hint"].startswith("No record holds every word of the query.")
    assert "1 more decision and 1 more fact matched; limit=1 cut the list." in answer["hint"]


def test_a_full_match_never_gets_partial_company(store):
    full = _add(store, "cold index")
    _add(store, "cold start")

    answer = _ask(store, query="cold index")

    assert _ids(answer["decisions"]) == [full.id]
    assert "match" not in answer and "matched_words" not in answer["decisions"][0]


def test_partial_counts_the_words_in_the_text_only(store):
    """A word held only through an anchor does not count towards a partial match."""
    one_in_text = _add(store, "an unrelated pool gotcha")
    _anchor(store, one_in_text, "Worker", "pkg/worker.py")
    two_in_text = _add(store, "the worker pool")

    answer = _ask(store, query="worker pool zebra")

    assert _ids(answer["decisions"]) == [two_in_text.id]
    assert answer["decisions"][0]["matched_words"] == ["worker", "pool"]


def test_partial_order_is_words_held_then_mistakes_then_newest(store):
    """A record that holds three words comes before a gotcha that holds two; a fact comes
    after the decisions that hold as many words; a proposal comes after everything accepted."""
    adr_three = _add(store, "cold index rebuild", kind="adr", hours_ago=5)
    adr_two = _add(store, "cold index", kind="adr", hours_ago=1)
    gotcha_two = _add(store, "index rebuild", kind="gotcha", hours_ago=9)
    _add(store, "cold start", kind="gotcha", hours_ago=2)
    fact_three = _add_fact(store, "cold index rebuild took 3 s", hours_ago=4)
    fact_two = _add_fact(store, "cold index took 3 s", hours_ago=4)
    draft_three = _add(store, "cold index rebuild draft", status="proposed", hours_ago=1)

    answer = _ask(store, query="cold index rebuild zebra")

    assert _ids(answer["decisions"]) == [adr_three.id, gotcha_two.id, adr_two.id, draft_three.id]
    assert _ids(answer["facts"]) == [fact_three.id, fact_two.id]
    # The fact that holds three words is placed before the decisions that hold two.
    two = _ask(store, query="cold index rebuild zebra", limit=2)
    assert _ids(two["decisions"]) == [adr_three.id] and _ids(two["facts"]) == [fact_three.id]


def test_a_full_match_in_hidden_history_stops_the_partial_match(store):
    """The only record that holds every word is retired. The answer is empty, says that the
    match is history and how to read it, and offers no partial match."""
    old = _add(store, "redis cache eviction policy", kind="adr", hours_ago=10)
    store.retire(old.id, "subject gone")
    _add(store, "redis cache headers", kind="adr")

    answer = _ask(store, query="redis cache eviction")

    assert answer["matched"] == 0 and "match" not in answer
    assert "1 record that holds every word is history." in answer["hint"]
    assert "include_superseded=True" in answer["hint"]
    assert "No record holds" not in answer["hint"]
    assert _ids(
        _ask(store, query="redis cache eviction", include_superseded=True)["decisions"]
    ) == [old.id]


def test_an_expired_fact_that_holds_every_word_stops_the_partial_match(store):
    start = datetime.now(UTC) - timedelta(hours=3)
    ended = Fact(
        statement="redis cache eviction hit rate 92%",
        source="bench",
        status=DecisionStatus.ACCEPTED,
        valid_from=start,
        valid_to=start + timedelta(hours=1),
        provenance=Provenance(source="manual"),
    )
    store.add_fact(ended)
    _add(store, "redis cache headers", kind="adr")

    answer = _ask(store, query="redis cache eviction")

    assert answer["matched"] == 0 and "match" not in answer
    assert "1 record that holds every word is history." in answer["hint"]
    assert _ids(_ask(store, query="redis cache eviction", include_superseded=True)["facts"]) == [
        ended.id
    ]


def test_history_that_holds_only_some_words_does_not_stop_the_partial_match(store):
    old = _add(store, "redis cache sizing", kind="adr", hours_ago=10)
    store.retire(old.id, "subject gone")
    live = _add(store, "redis cache headers", kind="adr")

    answer = _ask(store, query="redis cache eviction")

    assert answer["match"] == "partial" and _ids(answer["decisions"]) == [live.id]


def test_history_ruled_out_by_kind_does_not_stop_the_partial_match(store):
    """The retired ADR holds every word but the call asks for gotchas: it is not history of
    this call, so it must not be named and the partial match stands."""
    old = _add(store, "redis cache eviction policy", kind="adr", hours_ago=10)
    store.retire(old.id, "subject gone")
    live = _add(store, "redis cache headers", kind="gotcha")

    answer = _ask(store, query="redis cache eviction", kind="gotcha")

    assert answer["match"] == "partial" and _ids(answer["decisions"]) == [live.id]
    assert "is history" not in answer["hint"]


def test_history_ruled_out_by_files_does_not_stop_the_partial_match(store):
    old = _add(store, "redis cache eviction policy", kind="adr", hours_ago=10)
    store.retire(old.id, "subject gone")
    _anchor(store, old, "Evictor", "src/other.py")
    live = _add(store, "redis cache headers", kind="gotcha")
    _anchor(store, live, "Headers", "src/a.py")

    answer = _ask(store, query="redis cache eviction", files=["src/a.py"])

    assert answer["match"] == "partial" and _ids(answer["decisions"]) == [live.id]
    assert "is history" not in answer["hint"]


def test_an_expired_fact_ruled_out_by_files_does_not_stop_the_partial_match(store):
    start = datetime.now(UTC) - timedelta(hours=3)
    ended = Fact(
        statement="redis cache eviction hit rate 92%",
        source="bench",
        status=DecisionStatus.ACCEPTED,
        valid_from=start,
        valid_to=start + timedelta(hours=1),
        provenance=Provenance(source="manual"),
    )
    store.add_fact(ended)
    _anchor(store, ended, "Evictor", "src/other.py")
    live = _add(store, "redis cache headers", kind="gotcha")
    _anchor(store, live, "Headers", "src/a.py")

    answer = _ask(store, query="redis cache eviction", files=["src/a.py"])

    assert answer["match"] == "partial" and _ids(answer["decisions"]) == [live.id]
    assert "is history" not in answer["hint"]


def test_history_that_holds_a_word_through_an_anchor_stops_the_partial_match(store):
    """The retired record holds two of the three words in its text and the third in the name of
    its anchor: it holds every word, as a live one would, so the answer names it as history."""
    old = _add(store, "redis cache policy", kind="adr", hours_ago=10)
    _anchor(store, old, "Eviction", "src/evict.py")
    store.retire(old.id, "subject gone")
    _add(store, "redis cache headers", kind="gotcha")

    answer = _ask(store, query="redis cache eviction")

    assert answer["matched"] == 0 and "match" not in answer
    assert "1 record that holds every word is history." in answer["hint"]
    assert _ids(
        _ask(store, query="redis cache eviction", include_superseded=True)["decisions"]
    ) == [old.id]


def test_two_records_of_hidden_history_are_named_in_the_plural(store):
    for title in ("redis cache eviction policy", "redis cache eviction sizing"):
        old = _add(store, title, kind="adr", hours_ago=10)
        store.retire(old.id, "subject gone")
    _add(store, "redis cache headers", kind="gotcha")

    answer = _ask(store, query="redis cache eviction")

    assert "2 records that hold every word are history." in answer["hint"]


# -- a full match in hidden history that a live record replaced ----------------------------------
# see design/superpowers/specs/2026-11-14-retrieve-decisions-main-read-tool-design.md (D8)


def test_a_history_full_match_with_a_live_successor_returns_the_successor(store):
    old = _add(store, "redis cache eviction policy", kind="adr", hours_ago=10)
    new = _add(store, "lru for every cache", kind="adr", hours_ago=2, supersedes=old.id)
    _add(store, "redis cache headers", kind="adr")

    answer = _ask(store, query="redis cache eviction")

    assert "match" not in answer and answer["matched"] == 1 and answer["returned"] == 1
    assert _ids(answer["decisions"]) == [new.id]
    row = answer["decisions"][0]
    assert row["matched_history"] == [old.id]
    assert list(row)[0] == "matched_history"  # first among the keys, as matched_anchors is
    assert row["choice"] == "ch"  # the record is returned in full
    assert answer["hint"].startswith("No live record holds every word of the query.")
    assert "matched_history" in answer["hint"] and "retrieve_decisions(ids=[...])" in answer["hint"]
    assert "include_superseded" not in answer["hint"]
    # Asked for history, the call answers as before: the old record, nothing replaced.
    asked = _ask(store, query="redis cache eviction", include_superseded=True)
    assert _ids(asked["decisions"]) == [old.id] and "matched_history" not in asked["decisions"][0]


def test_a_chain_of_two_supersessions_returns_the_live_end(store):
    first = _add(store, "redis cache eviction policy", kind="adr", hours_ago=10)
    middle = _add(store, "lru everywhere", kind="adr", hours_ago=6, supersedes=first.id)
    end = _add(store, "ttl only", kind="adr", hours_ago=2, supersedes=middle.id)

    answer = _ask(store, query="redis cache eviction")

    assert _ids(answer["decisions"]) == [end.id]
    assert answer["decisions"][0]["matched_history"] == [first.id]


def test_two_history_matches_with_one_live_end_return_it_once(store):
    first = _add(store, "redis cache eviction policy", kind="adr", hours_ago=10)
    middle = _add(
        store, "redis cache eviction sizing", kind="adr", hours_ago=6, supersedes=first.id
    )
    end = _add(store, "ttl only", kind="adr", hours_ago=2, supersedes=middle.id)

    answer = _ask(store, query="redis cache eviction")

    assert _ids(answer["decisions"]) == [end.id] and answer["matched"] == 1
    assert sorted(answer["decisions"][0]["matched_history"]) == sorted([first.id, middle.id])


def test_a_retired_history_match_stays_empty_with_the_history_hint(store):
    old = _add(store, "redis cache eviction policy", kind="adr", hours_ago=10)
    store.retire(old.id, "subject gone")

    answer = _ask(store, query="redis cache eviction")

    assert answer["matched"] == 0 and answer["decisions"] == []
    assert "1 record that holds every word is history." in answer["hint"]
    assert "include_superseded=True" in answer["hint"]


def test_a_successor_the_kind_filter_excludes_leaves_the_answer_empty(store):
    old = _add(store, "redis cache eviction policy", kind="gotcha", hours_ago=10)
    _add(store, "lru everywhere", kind="adr", hours_ago=2, supersedes=old.id)

    answer = _ask(store, query="redis cache eviction", kind="gotcha")

    # The gotcha is history of this call; its successor is an ADR the call did not ask for.
    assert answer["matched"] == 0 and answer["decisions"] == []
    assert "1 record that holds every word is history." in answer["hint"]
    assert _ids(_ask(store, query="redis cache eviction", kind="adr")["decisions"]) == []


def test_a_successor_the_files_filter_excludes_leaves_the_answer_empty(store):
    old = _add(store, "redis cache eviction policy", kind="adr", hours_ago=10)
    _anchor(store, old, "Evictor", "src/a.py")
    new = _add(store, "lru everywhere", kind="adr", hours_ago=2, supersedes=old.id)
    _anchor(store, new, "Lru", "src/b.py")

    answer = _ask(store, query="redis cache eviction", files=["src/a.py"])

    assert answer["matched"] == 0 and answer["decisions"] == []
    assert "1 record that holds every word is history." in answer["hint"]


def test_a_successor_anchored_to_the_files_is_returned(store):
    old = _add(store, "redis cache eviction policy", kind="adr", hours_ago=10)
    _anchor(store, old, "Evictor", "src/a.py")
    new = _add(store, "lru everywhere", kind="adr", hours_ago=2, supersedes=old.id)
    _anchor(store, new, "Lru", "src/a.py")

    answer = _ask(store, query="redis cache eviction", files=["src/a.py"])

    assert _ids(answer["decisions"]) == [new.id]
    assert answer["decisions"][0]["matched_history"] == [old.id]


def test_a_live_full_match_is_unchanged_beside_a_replaced_one(store):
    old = _add(store, "redis cache eviction policy", kind="adr", hours_ago=10)
    _add(store, "lru everywhere", kind="adr", hours_ago=2, supersedes=old.id)
    live = _add(store, "redis cache eviction notes", kind="adr", hours_ago=3)

    answer = _ask(store, query="redis cache eviction")

    assert _ids(answer["decisions"]) == [live.id]
    assert "matched_history" not in answer["decisions"][0]
    assert "No live record" not in (answer["hint"] or "")


def test_a_partial_answer_is_unchanged_when_the_history_holds_only_some_words(store):
    old = _add(store, "redis cache sizing", kind="adr", hours_ago=10)
    _add(store, "lru everywhere", kind="adr", hours_ago=2, supersedes=old.id)
    live = _add(store, "redis cache headers", kind="adr")

    answer = _ask(store, query="redis cache eviction")

    assert answer["match"] == "partial" and live.id in _ids(answer["decisions"])
    assert all("matched_history" not in row for row in answer["decisions"])


def test_the_replacements_follow_the_limit_and_the_budget(store):
    news = []
    for i in range(3):
        old = _add(store, f"redis cache eviction policy {i}", kind="adr", hours_ago=20 + i)
        news.append(
            _add(
                store, f"lru {i}", kind="adr", hours_ago=2 + i, choice="x" * 3000, supersedes=old.id
            )
        )

    two = _ask(store, query="redis cache eviction", limit=2)
    assert two["matched"] == 3 and two["returned"] == 2 and two["omitted"] == 1
    assert all("matched_history" in row for row in two["decisions"])
    assert two["hint"].startswith("No live record holds every word of the query.")
    assert "limit=2 cut the list" in two["hint"]

    text = _decision_listing(store, None, query="redis cache eviction", budget_chars=4000)
    cut = json.loads(text)
    assert cut["matched"] == 3 and cut["returned"] < 3 and cut["omitted"] > 0
    assert len(text) <= 4000


def test_a_replacement_the_first_has_no_room_for_is_still_counted(store):
    old = _add(store, "redis cache eviction policy", kind="adr", hours_ago=10)
    _add(store, "lru everywhere", kind="adr", hours_ago=2, choice="x" * 6000, supersedes=old.id)

    answer = _ask(store, query="redis cache eviction", budget_chars=4000)

    assert answer["matched"] == 1 and answer["returned"] == 0
    assert "does not fit in budget_chars=4000" in answer["hint"]


def test_a_fact_history_match_returns_the_live_fact_that_replaced_it(store):
    old = _add_fact(store, "redis cache eviction hit rate 92%", hours_ago=10)
    new = _add_fact(store, "lru hit rate 95%", hours_ago=2, supersedes=old.id)
    _add(store, "redis cache headers", kind="adr")

    answer = _ask(store, query="redis cache eviction")

    assert answer["matched"] == 0 and answer["decisions"] == []
    assert _ids(answer["facts"]) == [new.id] and answer["facts_matched"] == 1
    assert answer["facts"][0]["matched_history"] == [old.id]
    assert answer["hint"].startswith("No live record holds every word of the query.")
    assert "list_facts(ids=[...])" in answer["hint"]


def test_an_expired_fact_without_a_successor_stays_history(store):
    start = datetime.now(UTC) - timedelta(hours=3)
    ended = Fact(
        statement="redis cache eviction hit rate 92%",
        source="bench",
        status=DecisionStatus.ACCEPTED,
        valid_from=start,
        valid_to=start + timedelta(hours=1),
        provenance=Provenance(source="manual"),
    )
    store.add_fact(ended)

    answer = _ask(store, query="redis cache eviction")

    assert answer["matched"] == 0 and "facts" not in answer
    assert "1 record that holds every word is history." in answer["hint"]


def test_replacements_are_ranked_mistakes_first_then_newest(store):
    old_adr = _add(store, "redis cache eviction policy", kind="adr", hours_ago=30)
    adr = _add(store, "lru everywhere", kind="adr", hours_ago=1, supersedes=old_adr.id)
    old_gotcha = _add(store, "redis cache eviction trap", kind="gotcha", hours_ago=30)
    gotcha = _add(
        store, "never evict hot keys", kind="gotcha", hours_ago=5, supersedes=old_gotcha.id
    )
    old_lesson = _add(store, "redis cache eviction lesson", kind="lesson", hours_ago=30)
    lesson = _add(store, "evict by size", kind="lesson", hours_ago=3, supersedes=old_lesson.id)

    answer = _ask(store, query="redis cache eviction")

    assert _ids(answer["decisions"]) == [gotcha.id, lesson.id, adr.id]
    # Within a rank the newest first, whatever the order the history matches were met in.
    old_a = _add(store, "redis cache eviction sizing", kind="adr", hours_ago=40)
    older = _add(store, "ttl for sessions", kind="adr", hours_ago=9, supersedes=old_a.id)
    old_b = _add(store, "redis cache eviction limits", kind="adr", hours_ago=40)
    newer = _add(store, "ttl for tokens", kind="adr", hours_ago=0.5, supersedes=old_b.id)
    again = _ask(store, query="redis cache eviction")
    assert _ids(again["decisions"]) == [gotcha.id, lesson.id, newer.id, adr.id, older.id]


def _put(store, record):
    """Write a record as it is, with no supersession rule: the shapes the write path refuses
    (a fork, a closed successor) are the ones a store can still hold after a merge."""
    with store._mutation():
        (store._write_fact if isinstance(record, Fact) else store._write_decision)(record)
    return record


def _chain_link(title, parent, *, live=False, valid_to=None, fact=False, month=1):
    """A record of a hand-built chain: ``superseded`` and closed unless ``live``."""
    when = datetime(2026, month, 1, tzinfo=UTC)
    closed = None if live else when + timedelta(days=1)
    fields = {
        "status": DecisionStatus.ACCEPTED if live else DecisionStatus.SUPERSEDED,
        "valid_from": when,
        "valid_to": valid_to or closed,
        "supersedes": parent,
        "provenance": Provenance(source="manual"),
    }
    if fact:
        return Fact(statement=title, source="bench", **fields)
    return Decision(title=title, kind=DecisionKind.ADR, context="c", choice="ch", **fields)


def test_a_successor_valid_until_a_future_date_is_still_live(store):
    """The read side counts a record as live until its ``valid_to`` passes, the way a fact is
    (``_expired``): a successor that ends in thirty days is the answer today."""
    soon = datetime.now(UTC) + timedelta(days=30)
    old = _put(store, _chain_link("redis cache eviction policy", None))
    new = _put(store, _chain_link("lru everywhere", old.id, live=True, valid_to=soon))
    old_fact = _put(store, _chain_link("redis cache eviction rate", None, fact=True))
    new_fact = _put(
        store, _chain_link("lru rate", old_fact.id, live=True, valid_to=soon, fact=True)
    )

    answer = _ask(store, query="redis cache eviction")

    assert _ids(answer["decisions"]) == [new.id] and _ids(answer["facts"]) == [new_fact.id]
    assert answer["decisions"][0]["matched_history"] == [old.id]
    assert answer["facts"][0]["matched_history"] == [old_fact.id]


def test_a_successor_whose_validity_has_passed_is_not_returned(store):
    old = _put(store, _chain_link("redis cache eviction policy", None))
    ended = datetime.now(UTC) - timedelta(hours=1)
    _put(store, _chain_link("lru everywhere", old.id, live=True, valid_to=ended))
    old_fact = _put(store, _chain_link("redis cache eviction rate", None, fact=True))
    _put(store, _chain_link("lru rate", old_fact.id, live=True, valid_to=ended, fact=True))

    answer = _ask(store, query="redis cache eviction")

    assert answer["matched"] == 0 and answer["decisions"] == [] and "facts" not in answer
    assert "2 records that hold every word are history." in answer["hint"]


def test_a_chain_longer_than_sixty_four_links_resolves_to_its_live_end(store):
    parent = None
    for i in range(70):
        link = _put(store, _chain_link("redis cache eviction" if i == 0 else f"step {i}", parent))
        parent = link.id
    end = _put(store, _chain_link("lru today", parent, live=True))

    answer = _ask(store, query="redis cache eviction")

    assert _ids(answer["decisions"]) == [end.id]


def test_a_forked_chain_returns_one_live_end(store):
    """The walk that names a head is the write path's: of two live ends it names one (the
    last met), and the answer returns that one record."""
    old = _put(store, _chain_link("redis cache eviction policy", None))
    left = _put(store, _chain_link("left", old.id))
    right = _put(store, _chain_link("right", old.id))
    ends = [
        _put(store, _chain_link("lru left", left.id, live=True)).id,
        _put(store, _chain_link("lru right", right.id, live=True)).id,
    ]

    answer = _ask(store, query="redis cache eviction")

    assert answer["matched"] == 1 and _ids(answer["decisions"])[0] in ends
    assert answer["decisions"][0]["matched_history"] == [old.id]


def test_a_long_shared_chain_is_read_once_per_member(store):
    """Every record of the chain holds the words and is a history match, so each match asks
    for the head of a chain that shares its tail with the next. The reads stay linear in the
    chain, not quadratic."""
    parent, size = None, 200
    for _ in range(size):
        parent = _put(store, _chain_link("redis cache eviction", parent)).id
    _put(store, _chain_link("lru today", parent, live=True))
    selects: list[str] = []
    store._conn.set_trace_callback(
        lambda sql: selects.append(sql) if sql.lstrip().upper().startswith("SELECT") else None
    )

    answer = _ask(store, query="redis cache eviction")

    store._conn.set_trace_callback(None)
    assert answer["matched"] == 1 and len(answer["decisions"][0]["matched_history"]) == size
    assert len(selects) < 3 * size


def test_replacement_facts_are_in_the_answer_in_order_after_the_decisions(store):
    """Two facts placed by recency of the replacement, not of the history record they
    replaced; the decision comes before every fact (the order of a query answer, D1)."""
    # The history record of the OLDER replacement is the newer of the two history matches, so
    # the heads are met in the wrong order and only a sort puts the newer replacement first.
    pred_of_keep = _put(store, _chain_link("redis cache eviction rate a", None, fact=True, month=2))
    pred_of_top = _put(store, _chain_link("redis cache eviction rate b", None, fact=True, month=1))
    keep = _put(store, _chain_link("lru a", pred_of_keep.id, live=True, fact=True, month=3))
    top = _put(store, _chain_link("lru b", pred_of_top.id, live=True, fact=True, month=6))
    old = _put(store, _chain_link("redis cache eviction policy", None))
    head = _put(store, _chain_link("lru everywhere", old.id, live=True))

    answer = _ask(store, query="redis cache eviction")
    assert _ids(answer["decisions"]) == [head.id]
    assert _ids(answer["facts"]) == [top.id, keep.id]

    one = _ask(store, query="redis cache eviction", limit=1)
    assert _ids(one["decisions"]) == [head.id] and one["facts"] == []
    assert one["omitted_fact_ids"] == [top.id, keep.id]
    facts_only = _ask(store, query="redis cache eviction", limit=2)
    assert _ids(facts_only["decisions"]) == [head.id] and _ids(facts_only["facts"]) == [top.id]


def test_a_replacement_fact_is_not_claimed_as_sent(store):
    """Only decisions are claimed in the sent ledger: no surface back-references a fact
    (D2: a fact is never a sent-earlier row)."""
    old = _put(store, _chain_link("redis cache eviction rate", None, fact=True))
    new = _put(store, _chain_link("lru rate", old.id, live=True, fact=True))

    text, sent = _decision_listing_result(store, None, query="redis cache eviction")

    assert _ids(json.loads(text)["facts"]) == [new.id] and sent == []


def test_a_decision_and_a_fact_replacement_are_returned_together(store):
    old = _add(store, "redis cache eviction policy", kind="adr", hours_ago=10)
    new = _add(store, "lru everywhere", kind="adr", hours_ago=2, supersedes=old.id)
    old_fact = _add_fact(store, "redis cache eviction hit rate 92%", hours_ago=10)
    new_fact = _add_fact(store, "lru hit rate 95%", hours_ago=2, supersedes=old_fact.id)

    answer = _ask(store, query="redis cache eviction")

    assert _ids(answer["decisions"]) == [new.id] and _ids(answer["facts"]) == [new_fact.id]


def test_the_replacement_is_claimed_as_sent_in_full(store):
    old = _add(store, "redis cache eviction policy", kind="adr", hours_ago=10)
    new = _add(store, "lru everywhere", kind="adr", hours_ago=2, supersedes=old.id)

    _, sent = _decision_listing_result(store, None, query="redis cache eviction")
    again = _ask(store, query="redis cache eviction", sent=frozenset(sent))

    assert sent == [new.id]
    assert again["decisions"][0]["sent_earlier"] is True
    assert again["decisions"][0]["matched_history"] == [old.id]


def test_a_repeated_word_counts_once(store):
    """``cold cold zebra`` is two words, not three: the record that holds ``cold`` holds one of
    them, and a partial match needs two."""
    _add(store, "cold start")

    answer = _ask(store, query="cold cold zebra")

    assert answer["matched"] == 0 and "match" not in answer
    assert "No searched record holds both words." in answer["hint"]
    assert "In no searched record: zebra." in answer["hint"]


def test_a_word_held_through_an_anchor_is_not_named_absent(store):
    decision = _add(store, "lock order")
    _anchor(store, decision, "Worker", "pkg/worker.py")

    answer = _ask(store, query="worker zebra")

    assert answer["matched"] == 0
    assert "In no searched record: zebra." in answer["hint"]
    assert "None of its words" not in answer["hint"]


@pytest.mark.parametrize("narrow", ["ids", "files"])
def test_a_miss_under_ids_or_files_speaks_of_the_records_the_call_admits(store, narrow):
    decision = _add(store, "Index layout")
    _anchor(store, decision, "Lock", "src/a.py")
    kwargs = {"ids": [decision.id]} if narrow == "ids" else {"files": ["src/a.py"]}

    answer = _ask(store, query="index zebra", **kwargs)

    assert answer["matched"] == 0
    assert "No record these filters admit holds both words." in answer["hint"]


def test_partial_rows_of_one_kind_are_newest_first(store):
    """Two gotchas hold the same words; the older one is stored first."""
    older = _add(store, "cold index old", kind="gotcha", hours_ago=5)
    newer = _add(store, "cold index new", kind="gotcha", hours_ago=1)

    answer = _ask(store, query="cold index zebra")

    assert _ids(answer["decisions"]) == [newer.id, older.id]


def test_one_word_query_has_no_partial_form(store):
    _add(store, "cold index")

    answer = _ask(store, query="zebra")

    assert answer["matched"] == 0 and "match" not in answer
    assert "No searched record holds that word" in answer["hint"]


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("zebra quagga", "None of its words occurs in any searched record."),
        ("cold index zebra quagga okapi", "none holds 3 of the 5 in its text."),
        ("cold index zebra quagga okapi", "In no searched record: zebra, quagga, okapi."),
    ],
)
def test_a_miss_says_which_words_occur_nowhere(store, query, expected):
    _add(store, "cold index")

    answer = _ask(store, query=query)

    assert answer["matched"] == 0
    assert expected in answer["hint"]


def test_a_two_word_query_has_no_partial_form(store):
    """One word of two is an any-word match: the answer stays empty and says which word no
    record holds."""
    _add(store, "Cache layout")

    answer = _ask(store, query="cache zebra")

    assert answer["matched"] == 0 and "match" not in answer
    assert "No searched record holds both words." in answer["hint"]
    assert "In no searched record: zebra." in answer["hint"]


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("cache", "No record these filters admit holds that word"),
        ("cache", "Try another word, a symbol or a file name, or drop a filter."),
        ("cache zebra", "None of its words occurs in any record these filters admit."),
        ("index zebra", "No record these filters admit holds both words."),
        ("index zebra", "In no record these filters admit: zebra."),
        ("index zebra", "Try fewer or other words, or drop a filter."),
    ],
)
def test_a_miss_under_a_filter_speaks_of_the_records_the_filter_admits(store, query, expected):
    """The store holds ``cache`` in an ADR and the call asks for gotchas: the answer must not
    say that no record holds the word."""
    _add(store, "Cache layout", kind="adr")
    _add(store, "Index layout", kind="gotcha")

    answer = _ask(store, query=query, kind="gotcha")

    assert answer["matched"] == 0
    assert expected in answer["hint"]


def test_a_partial_answer_under_a_filter_says_so(store):
    _add(store, "cold index hook zebra", kind="adr")
    lead = _add(store, "cold index delivery", kind="gotcha")

    answer = _ask(store, query="cold index hook zebra", kind="gotcha")

    assert _ids(answer["decisions"]) == [lead.id]
    assert answer["hint"].startswith("No record these filters admit holds every word of the query.")


# -- one definition of a mistake ----------------------------------------------------------------


def test_the_listing_and_the_task_context_agree_on_what_a_mistake_is():
    assert set(server_module._MISTAKE_RANK) == set(MISTAKE_KINDS)


def test_constraints_rank_with_the_lessons(store):
    spec = [
        ("adr new", "adr", 1),
        ("gotcha old", "gotcha", 30),
        ("lesson new", "lesson", 2),
        ("constraint mid", "constraint", 15),
        ("lesson old", "lesson", 40),
    ]
    for title, kind, hours in spec:
        _add(store, title, kind=kind, hours_ago=hours)

    listed = _ask(store, status="accepted")

    assert [d["title"] for d in listed["decisions"]] == [
        "gotcha old",
        "lesson new",
        "constraint mid",
        "lesson old",
        "adr new",
    ]


def test_constraints_rank_with_the_mistakes_among_anchor_matches_and_partial_leads(store):
    """A constraint is older than the ADR that holds the same words. It comes first in the
    group of the anchor matches and among the leads of a partial answer."""
    for title, kind, hours in (("lock order", "adr", 1), ("lock rule", "constraint", 30)):
        _anchor(store, _add(store, title, kind=kind, hours_ago=hours), "Worker", "pkg/worker.py")
    for title, kind, hours in (("cold index new", "adr", 1), ("cold index old", "constraint", 30)):
        _add(store, title, kind=kind, hours_ago=hours)

    anchored = _ask(store, query="worker")
    partial = _ask(store, query="cold index zebra")

    assert [d["title"] for d in anchored["decisions"]] == ["lock rule", "lock order"]
    assert partial["match"] == "partial"
    assert [d["title"] for d in partial["decisions"]] == ["cold index old", "cold index new"]
