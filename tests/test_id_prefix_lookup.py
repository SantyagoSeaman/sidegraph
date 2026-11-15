"""A shortened id says what became of it instead of matching nothing.

``retrieve_decisions(ids=[...])`` and ``list_facts(ids=[...])`` read a record by its full id. A
shortened one (an id copied from a table, or lower-cased) used to match nothing and say nothing.
It now resolves to the one record whose id it starts, is refused with the candidates when
several share it, and is reported when it is too short or starts no id.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from sidegraph.schema import Decision, DecisionKind, DecisionStatus, Fact, Provenance
from sidegraph.server import _decision_listing, _fact_listing
from sidegraph.store import Store

NOW = datetime.now(UTC)


def _decision_id(prefix: str, n: int) -> str:
    return f"{prefix}{n:0{26 - len(prefix)}d}"


def _add_decision(store: Store, prefix: str, n: int, *, context: str = "c") -> Decision:
    decision = Decision(
        id=_decision_id(prefix, n),
        title=f"decision {prefix}{n}",
        kind=DecisionKind.GOTCHA,
        status=DecisionStatus.ACCEPTED,
        context=context,
        choice="ch",
        valid_from=NOW - timedelta(hours=n + 1),
        provenance=Provenance(source="manual"),
    )
    store.add_decision(decision)
    return decision


def _add_fact(store: Store, prefix: str, n: int) -> Fact:
    fact = Fact(
        id=_decision_id(prefix, n),
        statement=f"fact {prefix}{n}",
        source="benchmark",
        status=DecisionStatus.ACCEPTED,
        valid_from=NOW - timedelta(hours=n + 1),
        provenance=Provenance(source="manual"),
    )
    store.add_fact(fact)
    return fact


def _ask(store: Store, **kwargs) -> dict:
    return json.loads(_decision_listing(store, None, **kwargs))


def _ask_facts(store: Store, **kwargs) -> dict:
    return json.loads(_fact_listing(store, None, **kwargs))


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    yield s
    s.close()


# -- retrieve_decisions -------------------------------------------------------------------------


def test_a_unique_prefix_resolves_to_its_record_and_says_so(store):
    wanted = _add_decision(store, "01JBCDEF", 1)
    _add_decision(store, "01JBXXXX", 2)
    given = wanted.id[:12].lower()

    answer = _ask(store, ids=[given])

    assert [d["id"] for d in answer["decisions"]] == [wanted.id]
    assert answer["matched"] == 1
    assert answer["id_lookup"] == [{"given": given, "status": "resolved", "id": wanted.id}]
    assert f'"{given}" → {wanted.id}' in answer["hint"]
    assert "full id" in answer["hint"]


def test_a_full_id_behaves_as_it_always_did(store):
    wanted = _add_decision(store, "01JBCDEF", 1)

    answer = _ask(store, ids=[wanted.id])

    assert [d["id"] for d in answer["decisions"]] == [wanted.id]
    assert "id_lookup" not in answer
    assert answer["hint"] is None


def test_an_unknown_full_length_id_keeps_its_plain_miss(store):
    _add_decision(store, "01JBCDEF", 1)

    answer = _ask(store, ids=["01JBCDEF" + "9" * 18])

    assert answer["matched"] == 0
    assert "id_lookup" not in answer
    assert answer["hint"].startswith("Nothing matched ids=[")


def test_a_prefix_shared_by_several_records_returns_none_and_lists_them(store):
    made = [_add_decision(store, "01JBCDEF", n) for n in (1, 2, 3)]
    _add_decision(store, "01JBXXXX", 4)

    answer = _ask(store, ids=["01jbcdef"])

    assert answer["matched"] == 0 and answer["decisions"] == []
    (note,) = answer["id_lookup"]
    assert note["given"] == "01jbcdef" and note["status"] == "ambiguous"
    assert note["candidates"] == sorted(d.id for d in made)
    assert "candidates_omitted" not in note
    assert "matches 3 records" in answer["hint"] and "full id" in answer["hint"]


def test_the_candidates_of_an_ambiguous_prefix_are_capped(store):
    made = [_add_decision(store, "01JBCDEF", n) for n in range(12)]

    answer = _ask(store, ids=["01JBCDEF"])

    (note,) = answer["id_lookup"]
    assert note["candidates"] == sorted(d.id for d in made)[:10]
    assert note["candidates_omitted"] == 2


def test_a_prefix_shorter_than_eight_characters_is_too_short_to_resolve(store):
    _add_decision(store, "01JBCDEF", 1)

    answer = _ask(store, ids=["01JBCDE"])

    assert answer["matched"] == 0
    assert answer["id_lookup"] == [{"given": "01JBCDE", "status": "too_short", "min_length": 8}]
    assert "too short" in answer["hint"] and "8" in answer["hint"]


def test_a_prefix_that_starts_no_id_says_so(store):
    _add_decision(store, "01JBCDEF", 1)

    answer = _ask(store, ids=["ZZZZZZZZZZ"])

    assert answer["id_lookup"] == [{"given": "ZZZZZZZZZZ", "status": "unknown"}]
    assert "No record id starts with" in answer["hint"]


def test_a_full_id_and_a_prefix_in_one_call_both_answer(store):
    first = _add_decision(store, "01JBCDEF", 1)
    second = _add_decision(store, "01JBXXXX", 2)

    answer = _ask(store, ids=[first.id, second.id[:14]])

    assert {d["id"] for d in answer["decisions"]} == {first.id, second.id}
    assert [n["given"] for n in answer["id_lookup"]] == [second.id[:14]]


def test_a_prefix_never_reads_a_record_the_other_filters_exclude(store):
    wanted = _add_decision(store, "01JBCDEF", 1)

    answer = _ask(store, ids=[wanted.id[:12]], kind="adr")

    assert answer["matched"] == 0
    assert answer["id_lookup"][0]["status"] == "resolved"


def test_the_notes_never_break_the_character_budget(store):
    for n in range(40):
        _add_decision(store, "01JBCDEF", n)

    text = _decision_listing(
        store, None, ids=["01JBCDEF", "x" * 300, "ZZZZZZZZ"], budget_chars=4000
    )

    assert len(text) <= 4000
    assert json.loads(text)["id_lookup"]


def test_a_decision_too_long_for_the_budget_says_so_in_the_hint(store):
    big = _add_decision(store, "01JBCDEF", 1, context="x" * 6000)

    answer = _ask(store, ids=[big.id], budget_chars=4000)

    assert answer["matched"] == 1 and answer["returned"] == 0
    assert "does not fit in budget_chars=4000" in answer["hint"]
    assert "Raise budget_chars" in answer["hint"]


# -- list_facts ---------------------------------------------------------------------------------


def test_a_fact_prefix_resolves_too(store):
    wanted = _add_fact(store, "01JFCDEF", 1)
    _add_fact(store, "01JFXXXX", 2)
    given = wanted.id[:10]

    answer = _ask_facts(store, ids=[given])

    assert [f["id"] for f in answer["facts"]] == [wanted.id]
    assert answer["id_lookup"] == [{"given": given, "status": "resolved", "id": wanted.id}]
    assert f'"{given}" → {wanted.id}' in answer["hint"]


def test_a_fact_prefix_shared_by_several_facts_lists_them(store):
    made = [_add_fact(store, "01JFCDEF", n) for n in (1, 2)]

    answer = _ask_facts(store, ids=["01JFCDEF"])

    assert answer["matched"] == 0
    (note,) = answer["id_lookup"]
    assert note["status"] == "ambiguous" and note["candidates"] == sorted(f.id for f in made)


def test_a_decision_prefix_is_not_resolved_against_facts(store):
    decision = _add_decision(store, "01JBCDEF", 1)
    _add_fact(store, "01JFCDEF", 1)

    answer = _ask_facts(store, ids=[decision.id[:12]])

    assert answer["id_lookup"] == [{"given": decision.id[:12], "status": "unknown"}]


def test_a_full_fact_id_behaves_as_it_always_did(store):
    wanted = _add_fact(store, "01JFCDEF", 1)

    answer = _ask_facts(store, ids=[wanted.id])

    assert [f["id"] for f in answer["facts"]] == [wanted.id]
    assert "id_lookup" not in answer


def test_a_fact_prefix_resolves_in_a_query_answer_of_retrieve_decisions(store):
    wanted = _add_fact(store, "01JFCDEF", 1)

    answer = _ask(store, query="fact", ids=[wanted.id[:12]])

    assert [f["id"] for f in answer["facts"]] == [wanted.id]
    assert answer["id_lookup"][0] == {
        "given": wanted.id[:12],
        "status": "resolved",
        "id": wanted.id,
    }


# -- list_domains -------------------------------------------------------------------------------


def test_a_domain_prefix_resolves_and_an_ambiguous_one_lists_candidates(store):
    from sidegraph import server
    from sidegraph.schema import Domain, DomainStatus

    domains = []
    for n, (prefix, slug) in enumerate(
        (("01JDCDEF", "alpha-area"), ("01JDCDEF", "beta-area"), ("01JDXXXX", "gamma-area"))
    ):
        domain = Domain(
            domain_id=_decision_id(prefix, n),
            slug=slug,
            title=slug.title(),
            summary="s",
            status=DomainStatus.ACCEPTED,
            provenance=Provenance(source="manual"),
        )
        store.add_domain(domain)
        domains.append(domain)

    one = json.loads(server._domain_listing(store, ids=[domains[2].domain_id[:12]]))
    both = json.loads(server._domain_listing(store, ids=["01jdcdef"]))

    assert [d["id"] for d in one["domains"]] == [domains[2].domain_id]
    assert one["id_lookup"][0]["status"] == "resolved"
    assert both["matched"] == 0 and both["id_lookup"][0]["status"] == "ambiguous"
    assert both["id_lookup"][0]["candidates"] == sorted(d.domain_id for d in domains[:2])


# -- review round 1 -----------------------------------------------------------------------------

PREFIXES = [f"01JBCD{i:02d}" for i in range(12)]


def _crowded_store(store: Store) -> list[str]:
    """Twelve ambiguous 8-character prefixes, twelve decisions, facts and domains under each."""
    from sidegraph.schema import Domain, DomainStatus

    for prefix in PREFIXES:
        for n in range(12):
            _add_decision(store, prefix, n)
            _add_fact(store, prefix, n)
            store.add_domain(
                Domain(
                    domain_id=_decision_id(prefix, n),
                    slug=f"area-{prefix.lower()}-{n}",
                    title="title",
                    summary="s",
                    status=DomainStatus.ACCEPTED,
                    provenance=Provenance(source="manual"),
                )
            )
    return PREFIXES


@pytest.mark.parametrize("tool", ["decisions", "facts", "domains"])
def test_twelve_ambiguous_prefixes_stay_inside_the_budget(store, tool):
    from sidegraph import server

    ids = _crowded_store(store)
    listing = {
        "decisions": lambda **kw: _decision_listing(store, None, **kw),
        "facts": lambda **kw: _fact_listing(store, None, **kw),
        "domains": lambda **kw: server._domain_listing(store, **kw),
    }[tool]

    for count in (1, 8, 10, 12):
        text = listing(ids=ids[:count], budget_chars=4000)
        assert len(text) <= 4000, (tool, count, len(text))
        answer = json.loads(text)
        assert answer["matched"] == 0
        notes = answer["id_lookup"]
        assert len(notes) + answer.get("id_lookup_omitted", 0) == count
        assert [n["given"] for n in notes] == ids[: len(notes)]
        # The report leaves a quarter of the budget for the records, and counts what it cut.
        assert len(json.dumps(notes, separators=(",", ":"))) + len(answer["hint"]) <= 1000
        for note in notes:
            assert len(note["candidates"]) + note.get("candidates_omitted", 0) == 12


def test_the_candidates_across_all_notes_are_capped(store):
    for prefix in PREFIXES[:4]:
        for n in range(12):
            _add_decision(store, prefix, n)

    answer = _ask(store, ids=PREFIXES[:4], budget_chars=40000)

    shown = sum(len(n["candidates"]) for n in answer["id_lookup"])
    assert shown == 20
    for note in answer["id_lookup"]:
        assert len(note["candidates"]) + note.get("candidates_omitted", 0) == 12
    assert "matches 12 records" in answer["hint"]
    last = answer["id_lookup"][-1]
    assert last["candidates"] == [] and last["candidates_omitted"] == 12
    assert (
        f'Id prefix "{last["given"]}" matches 12 records and none was read: pass a full id.'
        in answer["hint"]
    )


def test_more_than_ten_notes_are_cut_and_counted(store):
    _add_decision(store, "01JBCDEF", 1)

    answer = _ask(store, ids=[f"ZZZZZZZ{i:02d}" for i in range(15)])

    assert len(answer["id_lookup"]) == 10
    assert answer["id_lookup_omitted"] == 5
    assert "5 more ids are not listed" in answer["hint"]


def test_candidates_are_listed_in_id_order_whatever_the_age_of_the_records(store):
    made = []
    for n in (3, 1, 2):  # the lowest id is the newest record
        decision = _add_decision(store, "01JBCDEF", n)
        made.append(decision)

    answer = _ask(store, ids=["01JBCDEF"])

    assert answer["id_lookup"][0]["candidates"] == sorted(d.id for d in made)


def test_a_domain_hidden_by_status_is_not_reported_unknown(store):
    from sidegraph import server
    from sidegraph.schema import Domain, DomainStatus

    store.add_domain(
        Domain(
            domain_id=_decision_id("01JDCDEF", 1),
            slug="old-area",
            title="Old",
            summary="s",
            status=DomainStatus.PROPOSED,
            provenance=Provenance(source="manual"),
        )
    )

    answer = json.loads(server._domain_listing(store, ids=["01JDCDEF"], status="accepted"))

    assert answer["matched"] == 0
    assert answer["id_lookup"] == [
        {"given": "01JDCDEF", "status": "resolved", "id": _decision_id("01JDCDEF", 1)}
    ]


@pytest.mark.parametrize("tool", ["decisions", "facts", "domains"])
def test_an_empty_store_still_says_a_shortened_id_starts_nothing(store, tool):
    from sidegraph import server

    text = {
        "decisions": lambda: _decision_listing(store, None, ids=["ZZZZZZZZ"]),
        "facts": lambda: _fact_listing(store, None, ids=["ZZZZZZZZ"]),
        "domains": lambda: server._domain_listing(store, ids=["ZZZZZZZZ"]),
    }[tool]()

    assert json.loads(text)["id_lookup"] == [{"given": "ZZZZZZZZ", "status": "unknown"}]


def test_a_full_length_unknown_id_in_an_empty_store_keeps_its_plain_miss(store):
    answer = _ask(store, ids=["Z" * 26])

    assert "id_lookup" not in answer


def test_the_decision_listing_calls_its_candidates_records(store):
    _add_decision(store, "01JBCDEF", 1)
    _add_fact(store, "01JBCDEF", 2)

    answer = _ask(store, query="decision", ids=["01JBCDEF"])
    answer_no_query = _ask(store, ids=["ZZZZZZZZ"])

    assert "matches 2 records" in answer["hint"]
    assert "No record id starts with" in answer_no_query["hint"]
