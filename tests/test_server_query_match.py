"""Wire parity for the shared Portal query matcher."""

import json
from collections.abc import Iterator
from dataclasses import asdict
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path

import pytest

from sidegraph import server, store_layout
from sidegraph.schema import AnchorBinding, Decision, Descriptor, Entity, Fact, Provenance
from sidegraph.store import Store

_GOLDEN = Path(__file__).parent / "fixtures" / "portal_goldens" / "retrieve_decisions.json"
_LIMITED_CASE = "cache (limit=2)"
_RESULT_ORDER = "_result_order"
_QUERIES = (
    "cache",
    "CACHE retry absent CACHE",
    "cache rebuild_index",
    "old latch",
    "ПОВТОР",
    "missing cache",
    "discarded throttle",
)
_NOW = datetime(2026, 11, 17, 12, tzinfo=UTC)


@pytest.fixture
def query_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Store]:
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> datetime:
            if tz is None:
                return _NOW.astimezone().replace(tzinfo=None)
            return _NOW.astimezone(tz)

    monkeypatch.setattr(server, "datetime", FrozenDatetime)
    monkeypatch.setattr(store_layout, "datetime", FrozenDatetime)
    monkeypatch.delenv("SIDEGRAPH_UNRATIFIED", raising=False)
    monkeypatch.delenv("SIDEGRAPH_PROPOSAL_WINDOW_DAYS", raising=False)
    monkeypatch.setenv("SIDEGRAPH_TELEMETRY", "off")

    with Store(tmp_path / ".sidegraph") as store:
        monkeypatch.setattr(server, "_get_store", lambda: store)
        first = Decision(
            id="01M2B0F19RPYSVTTWG9D6XSHZW",
            title="Cache retry: Повтор после сбоя",
            kind="gotcha",
            status="accepted",
            context="A failed request may be retried.",
            choice="Back off before retrying.",
            valid_from=_NOW - timedelta(days=4),
            provenance=Provenance(source="manual"),
        )
        old = Decision(
            id="01M2B0F19RPYSVTTWG9D6XSHZX",
            title="Use the old latch",
            kind="adr",
            status="accepted",
            context="A rebuild needs exclusive access.",
            choice="Acquire the latch.",
            valid_from=_NOW - timedelta(days=5),
            provenance=Provenance(source="manual"),
        )
        successor = Decision(
            id="01M2B0F19RPYSVTTWG9D6XSHZY",
            title="Use a mutex",
            kind="adr",
            status="accepted",
            context=old.context,
            choice="Acquire the mutex.",
            supersedes=old.id,
            valid_from=_NOW - timedelta(days=1),
            provenance=Provenance(source="manual"),
        )
        proposal = Decision(
            id="01M2B0F19RPYSVTTWG9D6XSHZZ",
            title="Cache retry jitter",
            kind="lesson",
            status="proposed",
            context=first.context,
            choice="Add jitter to backoff.",
            valid_from=_NOW - timedelta(days=1),
            provenance=Provenance(source="agent"),
        )
        expired = Decision(
            id="01M2B0F19RPYSVTTWG9D6XSJ02",
            title="Discarded throttle",
            kind="adr",
            status="accepted",
            context="A trial temporarily limited throughput.",
            choice="Use the temporary throttle.",
            valid_from=_NOW - timedelta(days=5),
            valid_to=_NOW - timedelta(days=1),
            provenance=Provenance(source="manual"),
        )
        for decision in (first, old, successor, proposal, expired):
            store.add_decision(decision)
        fact = Fact(
            id="01M2B0F19RPYSVTTWG9D6XSJ00",
            statement="Cache rebuild measurements.",
            source="Cache retry benchmark",
            status="accepted",
            valid_from=_NOW - timedelta(days=2),
            provenance=Provenance(source="manual"),
        )
        store.add_fact(fact)
        store.add_fact(
            Fact(
                id="01M2B0F19RPYSVTTWG9D6XSJ03",
                statement="Cache capacity measurements.",
                source="Cache retry capacity run",
                status="accepted",
                valid_from=_NOW - timedelta(days=3),
                provenance=Provenance(source="manual"),
            )
        )
        entity = store.upsert_entity(
            Entity(
                entity_id="01M2B0F19RPYSVTTWG9D6XSJ01",
                canonical_name="rebuild_index",
                descriptor=Descriptor(name="rebuild_index", file_path="cache/index.py"),
            )
        )
        store.add_binding(AnchorBinding(record_id=successor.id, entity_id=entity.entity_id, tier=2))
        store.add_binding(AnchorBinding(record_id=first.id, entity_id=entity.entity_id, tier=2))
        yield store


def test_retrieve_decisions_golden(query_store: Store) -> None:
    """Compare the exact tool JSON, including object key order and separators."""
    expected = json.loads(_GOLDEN.read_text(encoding="utf-8"))
    del expected[_RESULT_ORDER]
    actual = {query: server.retrieve_decisions(query=query).content[0].text for query in _QUERIES}
    actual[_LIMITED_CASE] = server.retrieve_decisions(query="cache", limit=2).content[0].text
    assert list(actual) == list(expected)
    for case, text in actual.items():
        assert text == json.dumps(expected[case], ensure_ascii=False, separators=(",", ":"))


@pytest.mark.parametrize("query", _QUERIES)
def test_match_query_matches_tool_order(query_store: Store, query: str) -> None:
    decisions = server._retrieve_decisions_impl(query_store, include_superseded=True)
    facts = server._list_facts_impl(query_store, include_superseded=True)
    candidates = [row for row in decisions + facts if not server._in_history(row)]
    history = [row for row in decisions + facts if server._in_history(row)]

    match = server.match_query(query_store, candidates, history, query)
    answer = json.loads(server.retrieve_decisions(query=query).content[0].text)
    rows = answer["decisions"] + answer.get("facts", [])
    assert set(asdict(match)) == {"words", "full", "partial", "via_anchor", "history_replacements"}
    assert match.words == list(dict.fromkeys(query.lower().split()))
    assert match.via_anchor == {
        row["id"]: row["matched_anchors"] for row in rows if "matched_anchors" in row
    }
    assert match.history_replacements == {
        row["id"]: row["matched_history"] for row in rows if "matched_history" in row
    }

    if answer.get("match") == "partial":
        assert match.full == []
        assert dict(match.partial) == {row["id"]: row["matched_words"] for row in rows}
        ordered_ids = [record_id for record_id, _ in match.partial]
    else:
        assert match.partial == []
        ordered_ids = match.full

    assert len(ordered_ids) == len(rows)
    expected = json.loads(_GOLDEN.read_text(encoding="utf-8"))
    assert ordered_ids == expected[_RESULT_ORDER][query]
    # The wire splits facts into their own array. A limit at each position checks
    # their placement relative to decisions as well as order within each array.
    fact_ids = {row["id"] for row in facts}
    for limit in range(1, len(rows) + 1):
        limited = json.loads(server.retrieve_decisions(query=query, limit=limit).content[0].text)
        assert [row["id"] for row in limited["decisions"]] == [
            record_id for record_id in ordered_ids[:limit] if record_id not in fact_ids
        ]
        assert [row["id"] for row in limited.get("facts", [])] == [
            record_id for record_id in ordered_ids[:limit] if record_id in fact_ids
        ]


@pytest.mark.parametrize("pool_name", ["_retrieve_decisions_impl", "_list_facts_impl"])
def test_all_proposals_includes_non_surfacing(
    query_store: Store, monkeypatch: pytest.MonkeyPatch, pool_name: str
) -> None:
    proposal = next(row for row in query_store.iter_decisions() if row.status == "proposed")
    accepted = next(row for row in query_store.iter_decisions() if row.title == "Use a mutex")
    entity_id = query_store.bindings_for_record(accepted.id)[0].entity_id
    fact = query_store.add_fact(
        Fact(
            statement="Draft measurements.",
            source="Test run",
            valid_from=_NOW,
            provenance=Provenance(source="agent"),
        )
    )
    for record_id in (proposal.id, fact.id):
        query_store.add_binding(AnchorBinding(record_id=record_id, entity_id=entity_id, tier=2))

    before = server._get_entity_history_impl(query_store, entity_id)
    assert {proposal.id, fact.id} <= {row["id"] for row in before}
    monkeypatch.setenv("SIDEGRAPH_UNRATIFIED", "off")
    pool = getattr(server, pool_name)
    target = proposal.id if pool_name == "_retrieve_decisions_impl" else fact.id
    regular = pool(query_store)
    assert target not in {row["id"] for row in regular}
    assert server._get_entity_history_impl(query_store, entity_id) == [
        row for row in before if row["status"] != "proposed"
    ]

    expanded = pool(query_store, all_proposals=True)
    assert {row["id"] for row in expanded} == {row["id"] for row in regular} | {target}
