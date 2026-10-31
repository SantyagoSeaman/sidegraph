"""Every listing tool is bounded: a character budget, a final cap, a remainder that says how
to narrow. The host replaces an answer over its limit with a notice, so an unbounded answer is
no answer.

Fixtures are built to the sizes measured on a real store (facts of about 900 characters, an
entity with 150 records, a domain with 80 decisions, a queue with one 4,900-character block, a
graph with 300 communities). Threshold fixtures sit ON their boundary.
# see design/superpowers/specs/2026-10-11-bounded-listing-tools-design.md (D1-D8, tests L1-L16)
"""
# ruff: noqa: F811  (the imported ``ctx_env`` fixture is redefined as a test argument)

from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import fastmcp
import mcp.types as mt
import pytest

import sidegraph.server as server
from sidegraph.engine.reader import GraphifyReader
from sidegraph.retrieval import _DRIFT_LEGEND, DRIFT_CACHE_KEY
from sidegraph.schema import (
    AnchorBinding,
    Decision,
    DecisionKind,
    DecisionStatus,
    Descriptor,
    Domain,
    DomainStatus,
    Fact,
    Provenance,
)
from sidegraph.store import Store
from tests.bounded_listings_support import (
    H2_HEADINGS,
    check_heading_line_records_are_cut_whole,
    ctx_env,  # noqa: F401  (fixture)
    ctx_template,  # noqa: F401  (fixture)
    uncapped_related,  # noqa: F401  (fixture)
)

NOW = datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC)


# -- builders ---------------------------------------------------------------------------


def _fact(
    store: Store,
    i: int,
    *,
    size: int = 560,
    status: DecisionStatus = DecisionStatus.ACCEPTED,
    word: str = "latency",
    supersedes: str | None = None,
) -> Fact:
    fact = Fact(
        statement=f"fact {i} {word} " + "x" * size,
        source=f"benchmark {i}",
        status=status,
        valid_from=NOW - timedelta(minutes=i),
        supersedes=supersedes,
        provenance=Provenance(source="manual"),
    )
    store.add_fact(fact)
    return fact


def _decision(
    store: Store,
    i: int,
    *,
    kind: DecisionKind = DecisionKind.GOTCHA,
    status: DecisionStatus = DecisionStatus.ACCEPTED,
    size: int = 300,
    supersedes: str | None = None,
) -> Decision:
    decision = Decision(
        title=f"decision {i}",
        kind=kind,
        status=status,
        context="c " + "y" * size,
        choice="ch " + "z" * size,
        valid_from=NOW - timedelta(minutes=i),
        supersedes=supersedes,
        provenance=Provenance(source="manual"),
    )
    store.add_decision(decision)
    return decision


def _bind(store: Store, record_id: str, entity_id: str, tier: int = 1) -> None:
    store.add_binding(
        AnchorBinding(record_id=record_id, entity_id=entity_id, tier=tier, status="live")
    )


def test_mcp_list_domains_bounds_oversized_domain_before_envelope_assertions(tmp_path, monkeypatch):
    """The real MCP response must stay inside its text budget for one oversized row."""
    store = Store(tmp_path / "mcp_oversized_domain.db")
    monkeypatch.setattr(server, "_store", store)
    domain = Domain(
        slug="large-domain",
        title="Large domain",
        summary="x" * 60_000,
        provenance=Provenance(source="manual"),
    )
    store.add_domain(domain)

    async def run():
        async with fastmcp.Client(server.mcp) as client:
            return await client.call_tool("list_domains", {})

    try:
        result = asyncio.run(run())
        text = result.content[0].text
        assert len(text) <= 24_000
        payload = json.loads(text)
        assert payload["matched"] == 1
        assert payload["returned"] == 0
        assert payload["omitted"] == 1
        assert payload["domains"] == []
        assert f'Store.get_domain("{domain.domain_id}")' in payload["hint"]
    finally:
        store.close()


def _add_listing_domain(
    store: Store,
    slug: str,
    *,
    title: str | None = None,
    summary: str = "summary",
    parent: str | None = None,
    status: DomainStatus = DomainStatus.PROPOSED,
):
    return store.add_domain(
        Domain(
            slug=slug,
            title=title or slug.title(),
            summary=summary,
            parent_id=parent,
            status=status,
            provenance=Provenance(source="manual"),
        )
    )


def test_domain_listing_keeps_raw_rows_and_filters_exact_ids_and_query(store):
    parent = _add_listing_domain(store, "group", title="Shared name", summary="Payments and orders")
    _add_listing_domain(
        store, "child", title="Child", summary="Payments refunds", parent=parent.domain_id
    )
    rows = server._list_domains_impl(store)

    plain = json.loads(server._domain_listing(store))
    assert plain == {
        "matched": len(rows),
        "returned": len(rows),
        "omitted": 0,
        "domains": rows,
        "hint": None,
    }

    child = next(row for row in rows if row["slug"] == "child")
    selected = json.loads(server._domain_listing(store, ids=[child["id"]]))
    assert [row["id"] for row in selected["domains"]] == [child["id"]]
    assert selected["domains"][0]["parent_slug"] == "group"
    query = json.loads(server._domain_listing(store, query="payments refunds"))
    assert [row["slug"] for row in query["domains"]] == ["child"]
    combined = json.loads(
        server._domain_listing(
            store,
            status="proposed",
            query="child payments refunds",
            ids=[child["id"]],
        )
    )
    assert [row["id"] for row in combined["domains"]] == [child["id"]]
    assert json.loads(server._domain_listing(store, query="payments missing"))["matched"] == 0
    with pytest.raises(ValueError, match="DomainStatus"):
        server._domain_listing(store, status="unknown")


def test_domain_listing_empty_hints_distinguish_store_and_filters(store):
    empty = json.loads(server._domain_listing(store))
    assert empty["hint"] == "No domains in the store."

    _add_listing_domain(store, "present")
    filtered = json.loads(server._domain_listing(store, query="secret caller text"))
    assert filtered["matched"] == filtered["returned"] == filtered["omitted"] == 0
    assert "query" in filtered["hint"]
    assert "secret caller text" not in filtered["hint"]


def test_domain_listing_blank_filters_match_the_unfiltered_call(store):
    _add_listing_domain(store, "present")
    assert server._domain_listing(store, query=" \n ", ids=[" ", ""]) == server._domain_listing(
        store
    )


def test_domain_listing_mid_list_oversize_keeps_prefix_and_local_fallback(store):
    first = _add_listing_domain(store, "a-small")
    blocked = _add_listing_domain(store, "b-large", summary="z" * 50_000)
    third = _add_listing_domain(store, "c-small")

    text = server._domain_listing(store, budget_chars=4000)
    answer = json.loads(text)
    assert len(text) <= 4000
    assert answer["matched"] == 3 and answer["returned"] == 1 and answer["omitted"] == 2
    assert [row["id"] for row in answer["domains"]] == [first.domain_id]
    assert f'Store.get_domain("{blocked.domain_id}")' in answer["hint"]
    assert "raise budget" not in answer["hint"]
    follow_up = json.loads(server._domain_listing(store, ids=[third.domain_id]))
    assert [row["id"] for row in follow_up["domains"]] == [third.domain_id]


def test_domain_listing_first_oversized_row_does_not_suggest_a_larger_maximum(store):
    domain = _add_listing_domain(store, "too-large", summary="z" * 60_000)
    text = server._domain_listing(store, budget_chars=40_000)
    answer = json.loads(text)
    assert len(text) <= 40_000
    assert answer["matched"] == 1 and answer["returned"] == 0 and answer["omitted"] == 1
    assert f'Store.get_domain("{domain.domain_id}")' in answer["hint"]
    assert "budget_chars=40000" in answer["hint"]
    assert "raise budget" not in answer["hint"]


def test_domain_listing_count_cut_suggests_a_real_limit_and_filters(store):
    for index in range(3):
        _add_listing_domain(store, f"row-{index}")
    answer = json.loads(server._domain_listing(store, limit=1))
    assert answer["returned"] == 1 and answer["omitted"] == 2
    assert "limit=1" in answer["hint"]
    assert "raise limit (max 100)" in answer["hint"]
    assert "status, query or ids" in answer["hint"]


def test_domain_listing_nonvacuous_limit_and_budget_clamps(store):
    for index in range(101):
        _add_listing_domain(store, f"many-{index:03d}")
    by_count = json.loads(server._domain_listing(store, limit=1000, budget_chars=40_000))
    assert by_count["matched"] == 101
    assert by_count["returned"] == 100 and by_count["omitted"] == 1
    assert "limit=100" in by_count["hint"]

    one_small = server._list_domains_impl(store)[0]
    only_one = json.loads(server._domain_listing(store, ids=[one_small["id"]], limit=0))
    assert only_one["returned"] == 1 and only_one["omitted"] == 0

    oversized_store = Store(store.path.parent / "oversized-clamp.db")
    try:
        oversized = _add_listing_domain(oversized_store, "large", summary="x" * 50_000)
        max_clamped = json.loads(
            server._domain_listing(oversized_store, ids=[oversized.domain_id], budget_chars=10**9)
        )
        assert max_clamped["returned"] == 0 and max_clamped["omitted"] == 1
        assert "budget_chars=40000" in max_clamped["hint"]
        assert "raise budget" not in max_clamped["hint"]
    finally:
        oversized_store.close()

    minimum_budget = json.loads(
        server._domain_listing(store, ids=[one_small["id"]], budget_chars=1)
    )
    assert minimum_budget["returned"] == 1 and minimum_budget["omitted"] == 0
    assert len(server._domain_listing(store, ids=[one_small["id"]], budget_chars=1)) <= 4000


def test_domain_listing_uses_maximal_prefix_at_hint_null_boundary(store):
    _add_listing_domain(store, "first", summary="a" * 3000)
    _add_listing_domain(store, "second", summary="b" * 3000)
    rows = server._list_domains_impl(store)
    full = {"matched": 2, "returned": 2, "omitted": 0, "domains": rows, "hint": None}
    boundary = len(server._dump(full))
    answer = json.loads(server._domain_listing(store, budget_chars=boundary))
    assert answer["returned"] == 2 and answer["hint"] is None

    under = json.loads(server._domain_listing(store, budget_chars=boundary - 1))
    assert under["returned"] == 1 and under["omitted"] == 1
    assert len(server._dump(under)) <= boundary - 1


def test_domain_listing_real_tool_is_text_only_and_read_only(store, monkeypatch):
    _add_listing_domain(store, "present")
    monkeypatch.setattr(server, "_synced_reader", lambda: pytest.fail("unexpected sync"))
    monkeypatch.setattr(server, "_load_reader", lambda: pytest.fail("unexpected reader load"))
    monkeypatch.setattr(store, "_mutation", lambda *a, **k: pytest.fail("unexpected mutation"))
    before = {
        path.relative_to(store.path): path.read_bytes()
        for path in store.path.rglob("*")
        if path.is_file()
    }

    text, answer = wire("list_domains", {})
    assert len(text) <= 24_000 and answer["returned"] == 1
    tools = {tool.name: tool for tool in asyncio.run(server.mcp.list_tools())}
    assert tools["list_domains"].output_schema is None
    assert tools["list_domains"].annotations.readOnlyHint is True
    after = {
        path.relative_to(store.path): path.read_bytes()
        for path in store.path.rglob("*")
        if path.is_file()
    }
    assert before == after
    relative = {str(path) for path in before}
    assert any(path.endswith("index.db") for path in relative)
    assert any("/domains/" in f"/{path}" and path.endswith(".json") for path in relative)


def test_domain_listing_sizes_escaped_fields_and_long_fallback_ids(store, monkeypatch):
    escaped = _add_listing_domain(store, "escaped", summary='quote " slash \\\\ ' + "\x01" * 500)
    text = server._domain_listing(store, ids=[escaped.domain_id], budget_chars=4000)
    answer = json.loads(text)
    assert len(text) <= 4000
    assert answer["returned"] == 1
    assert answer["domains"][0]["summary"] == 'quote " slash \\\\ ' + "\x01" * 500

    row = {
        "id": "long-id" * 30,
        "slug": "large",
        "title": "Large",
        "summary": "x" * 50_000,
        "status": "proposed",
        "member_count": 0,
        "path_prefixes": [],
        "seed_anchor_count": 0,
        "parent_slug": None,
        "child_slugs": [],
    }
    monkeypatch.setattr(server, "_list_domains_impl", lambda store, status=None: [row])
    long_id = json.loads(server._domain_listing(store, budget_chars=4000))
    assert long_id["returned"] == 0
    assert "Store.iter_domains() on the configured store" in long_id["hint"]
    assert row["id"] not in long_id["hint"]
    assert "raise budget" not in long_id["hint"]


@pytest.mark.parametrize(("budget", "count"), [(4000, 700)])
def test_domain_listing_omits_escaped_overflow_without_raise_budget_advice(
    store, monkeypatch, budget, count
):
    row = {
        "id": "escaped-overflow",
        "slug": "escaped",
        "title": "Escaped",
        "summary": "\x01" * count,
        "status": "proposed",
        "member_count": 0,
        "path_prefixes": [],
        "seed_anchor_count": 0,
        "parent_slug": None,
        "child_slugs": [],
    }
    assert len(row["summary"]) < budget < len(server._dump(row))
    monkeypatch.setattr(server, "_list_domains_impl", lambda store, status=None: [row])
    text = server._domain_listing(store, budget_chars=budget)
    answer = json.loads(text)
    assert len(text) <= budget
    assert answer["returned"] == 0 and answer["omitted"] == 1
    assert f"budget_chars={budget}" in answer["hint"]
    assert "raise budget" not in answer["hint"]


def test_domain_listing_rejects_a_long_non_summary_row_whole(store, monkeypatch):
    row = {
        "id": "long-fields",
        "slug": "area-" + "s" * 1200,
        "title": "Title " + "t" * 1500,
        "summary": "short",
        "status": "proposed",
        "member_count": 0,
        "path_prefixes": ["src/" + "p" * 1500],
        "seed_anchor_count": 0,
        "parent_slug": None,
        "child_slugs": ["child-" + str(index) for index in range(150)],
    }
    monkeypatch.setattr(server, "_list_domains_impl", lambda store, status=None: [row])
    text = server._domain_listing(store, budget_chars=4000)
    answer = json.loads(text)
    assert len(text) <= 4000
    assert answer["matched"] == 1 and answer["returned"] == 0 and answer["omitted"] == 1
    assert answer["domains"] == []


def test_domain_listing_fallback_never_uses_configured_store_path(store):
    _add_listing_domain(store, "too-large", summary="x" * 50_000)
    fake_root = store.path / "not-created-configured-store"
    fake_path = fake_root / "/".join(["p" * 200] * 24)
    assert len(server._dump(str(fake_path))) > 4000
    assert not fake_root.exists()
    proxy = SimpleNamespace(path=fake_path, iter_domains=store.iter_domains)
    text = server._domain_listing(proxy, budget_chars=1)
    answer = json.loads(text)
    assert len(text) <= 4000
    assert not fake_root.exists()
    assert str(fake_path) not in answer["hint"]
    assert "Store.get_domain(" in answer["hint"]
    assert "on the configured store" in answer["hint"]


def test_domain_listing_filtered_lineage_keeps_unselected_status_relatives(store):
    parent = _add_listing_domain(store, "parent")
    store.ratify_domains(accept=[parent.domain_id])
    selected_child = _add_listing_domain(store, "selected-child", parent=parent.domain_id)
    dropped_sibling = _add_listing_domain(store, "dropped-sibling", parent=parent.domain_id)
    store.ratify_domains(drop=[dropped_sibling.domain_id])

    child = json.loads(
        server._domain_listing(
            store, status="proposed", ids=[selected_child.domain_id], query="selected-child"
        )
    )["domains"][0]
    assert child["parent_slug"] == "parent"
    assert child["child_slugs"] == []

    parent_row = json.loads(
        server._domain_listing(store, status="accepted", ids=[parent.domain_id])
    )["domains"][0]
    assert parent_row["child_slugs"] == ["dropped-sibling", "selected-child"]


def test_domain_listing_distinguishes_same_slug_revisions_by_id(store):
    old = _add_listing_domain(store, "revision", summary="old")
    new = Domain(
        slug=old.slug,
        title="New revision",
        summary="new",
        supersedes=old.domain_id,
        provenance=Provenance(source="manual"),
    )
    store.supersede_domain(old.domain_id, new)
    rows = server._list_domains_impl(store)
    assert [row["id"] for row in rows] == [old.domain_id, new.domain_id]
    old_rows = json.loads(server._domain_listing(store, ids=[old.domain_id]))["domains"]
    new_rows = json.loads(server._domain_listing(store, ids=[new.domain_id]))["domains"]
    assert [row["id"] for row in old_rows] == [old.domain_id]
    assert [row["id"] for row in new_rows] == [new.domain_id]


@pytest.fixture
def store(tmp_path: Path, monkeypatch):
    s = Store(tmp_path / "t.db")
    monkeypatch.setattr(server, "_store", s)
    monkeypatch.setenv("SIDEGRAPH_GRAPH", str(tmp_path / "no-such-graph.json"))
    yield s
    s.close()


@pytest.fixture
def facts_store(store):
    """200 facts of about 900 characters; one superseded pair, one proposed, one rejected."""
    for i in range(200):
        _fact(store, i)
    old = _fact(store, 300, word="old")
    _fact(store, 0, word="new", supersedes=old.id)
    _fact(store, 301, status=DecisionStatus.PROPOSED, word="pending")
    entity = store.get_or_create_entity(Descriptor(name="Worker", file_path="x.py"))
    _bind(store, store.get_fact(old.id).id, entity.entity_id)
    return store


def call(name: str, arguments: dict | None = None):
    async def run():
        async with fastmcp.Client(server.mcp) as client:
            return await client.call_tool(name, arguments or {}, raise_on_error=False)

    return asyncio.run(run())


def wire(name: str, arguments: dict | None = None) -> tuple[str, dict | None]:
    """The text a tool sent, after checking it is one text block with no structured copy."""
    result = call(name, arguments)
    assert not result.is_error, result.content
    assert len(result.content) == 1 and isinstance(result.content[0], mt.TextContent)
    assert result.structured_content is None
    assert result.data is None
    text = result.content[0].text
    try:
        return text, json.loads(text)
    except json.JSONDecodeError:
        return text, None


# -- L1-L4, L14, L16: list_facts ----------------------------------------------------------


def test_l1_list_facts_default_is_the_newest_25_under_the_budget(facts_store):
    answer = json.loads(server._fact_listing(facts_store, None))
    text = server._fact_listing(facts_store, None)

    assert answer["returned"] == 25
    assert len(text) <= 24000
    assert answer["omitted"] == answer["matched"] - 25
    assert "limit=25" in answer["hint"] and "raise limit" in answer["hint"]
    stamps = [f["valid_from"] for f in answer["facts"]]
    assert stamps == sorted(stamps, reverse=True)
    # the 25 newest sit under the budget: the limit cuts first
    assert sum(len(server._dump(f)) for f in answer["facts"]) < 24000


def test_l1b_limit_100_is_cut_by_the_budget(facts_store):
    text = server._fact_listing(facts_store, None, limit=100)
    answer = json.loads(text)

    assert 25 < answer["returned"] < 100
    assert len(text) <= 24000
    assert answer["omitted"] == answer["matched"] - answer["returned"]
    assert "character budget" in answer["hint"] and "raise limit" not in answer["hint"]
    assert "query" in answer["hint"] and "files" in answer["hint"]


@pytest.mark.parametrize(
    ("limit", "budget"),
    [(0, 1), (1, 4000), (25, 24000), (100, 40000), (1000, 10**9), (100, 4000)],
)
def test_l2_the_length_never_exceeds_the_clamped_budget(facts_store, limit, budget):
    text = server._fact_listing(facts_store, None, limit=limit, budget_chars=budget)
    answer = json.loads(text)

    assert len(text) <= max(4000, min(40000, budget))
    assert answer["returned"] <= max(1, min(100, limit))
    assert answer["returned"] >= 1 or budget < 4000


def test_l2_a_budget_below_the_minimum_is_raised_to_4000(facts_store):
    text = server._fact_listing(facts_store, None, limit=100, budget_chars=1)
    assert 3500 < len(text) <= 4000


def test_l3_filters(facts_store):
    status = json.loads(server._fact_listing(facts_store, None, status="superseded"))
    assert status["matched"] == 1 and status["facts"][0]["statement"].startswith("fact 300 old")

    some = [f["id"] for f in json.loads(server._fact_listing(facts_store, None))["facts"][:2]]
    by_ids = json.loads(server._fact_listing(facts_store, None, ids=some + [" "]))
    assert [f["id"] for f in by_ids["facts"]] == some

    words = json.loads(server._fact_listing(facts_store, None, query="benchmark 7 LATENCY"))
    assert words["matched"] > 0
    assert all("fact" in f["statement"] for f in words["facts"])
    one = json.loads(server._fact_listing(facts_store, None, query="fact 150 latency"))
    assert one["matched"] == 1

    # `files`: the old superseded fact is anchored to x.py (default hides it, status shows it)
    hit = json.loads(server._fact_listing(facts_store, None, files=["x.py"], status="superseded"))
    assert hit["matched"] == 1
    miss = json.loads(server._fact_listing(facts_store, None, files=["x.py", "nope.py"]))
    assert miss["matched"] == 0
    assert {u["path"] for u in miss["unresolved_files"]} == {"nope.py"}
    assert miss["unresolved_files"][0]["reason"] == "no-graph"

    blank = json.loads(server._fact_listing(facts_store, None, files=[""], query="  ", ids=[]))
    plain = json.loads(server._fact_listing(facts_store, None))
    assert blank == plain


def test_l4_counts_cover_history_and_follow_the_proposal_policy(facts_store, monkeypatch):
    # The fixture dates records from a fixed NOW; with no proposal window the pending fact
    # surfaces whatever today's date is.
    monkeypatch.setenv("SIDEGRAPH_PROPOSAL_WINDOW_DAYS", "0")
    answer = json.loads(server._fact_listing(facts_store, None))
    assert answer["counts"]["status"] == {"accepted": 201, "proposed": 1, "superseded": 1}
    assert all(f["status"] != "superseded" for f in answer["facts"])

    monkeypatch.setenv("SIDEGRAPH_UNRATIFIED", "off")
    hidden = json.loads(server._fact_listing(facts_store, None, include_superseded=True))
    assert "proposed" not in hidden["counts"]["status"]
    assert all(f["status"] != "proposed" for f in hidden["facts"])


def test_l16_list_facts_stays_read_only_and_writes_nothing(facts_store, monkeypatch):
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    assert tools["list_facts"].annotations.readOnlyHint is True

    synced: list[str] = []
    monkeypatch.setattr(server, "_synced_reader", lambda: synced.append("sync") or (None, None))
    before = sorted(p.name for p in facts_store.path.rglob("*") if p.is_file())
    wire("list_facts", {"files": ["x.py"]})
    after = sorted(p.name for p in facts_store.path.rglob("*") if p.is_file())
    assert synced == [] and before == after


# -- L5: text only, bounded through the real transport ------------------------------------


# -- L6, L6b: get_entity_history ----------------------------------------------------------


@pytest.fixture
def history(store):
    """One entity holding 150 records: 100 decisions (15 of them superseded, each by a live
    successor that is also bound) and 50 facts."""
    entity = store.get_or_create_abstract_entity("domain:history")
    superseded: list[str] = []
    for i in range(15):
        old = _decision(store, 1000 + i, size=250)
        _bind(store, old.id, entity.entity_id)
        new = _decision(store, i, size=250, supersedes=old.id)
        _bind(store, new.id, entity.entity_id)
        superseded.append(old.id)
    for i in range(15, 85):
        _bind(store, _decision(store, i, size=250).id, entity.entity_id)
    for i in range(50):
        _bind(store, _fact(store, 2000 + i, size=250).id, entity.entity_id)
    assert len(server._get_entity_history_impl(store, entity.entity_id)) == 150
    return store, entity.entity_id, superseded


def _reachable(answer: dict) -> set[str]:
    return (
        {r["id"] for r in answer["records"]}
        | {r["id"] for r in answer.get("omitted_records", [])}
        | set(answer.get("omitted_ids", []))
    )


def test_l6_every_record_stays_reachable_and_the_fill_rule_holds(history):
    store, eid, superseded = history
    everything = server._get_entity_history_impl(store, eid)
    text = server._entity_history_listing(store, eid)
    answer = json.loads(text)

    assert len(text) <= 24000
    assert _reachable(answer) == {r["id"] for r in everything}
    assert set(superseded) <= _reachable(answer)
    assert all(r["record_type"] in ("decision", "fact") for r in answer["records"])
    assert {"id", "record_type", "kind", "status", "valid_from", "title"} == set(
        answer["omitted_records"][0]
    )
    # full records: newest first, up to half the budget
    used, expected = 0, 0
    for record in everything[:25]:
        size = len(server._dump(record)) + (1 if expected else 0)
        if expected and used + size > 12000:
            break
        used += size
        expected += 1
    assert answer["returned"] == expected == len(answer["records"])
    assert answer["omitted"] == 150 - expected
    assert "Store.get_decision" in answer["hint"] and "Store.get_fact" in answer["hint"]

    only = json.loads(
        server._entity_history_listing(store, eid, status=["superseded"], budget_chars=40000)
    )
    assert {r["id"] for r in only["records"]} == set(superseded)
    assert "omitted_records" not in only


def test_l6_the_half_budget_boundary(history):
    store, eid, _ = history
    everything = server._get_entity_history_impl(store, eid)
    sizes = [len(server._dump(r)) for r in everything]
    k = 6
    half = sum(sizes[:k]) + (k - 1)  # exactly k full records fit in half the budget
    on = json.loads(server._entity_history_listing(store, eid, budget_chars=2 * half))
    under = json.loads(server._entity_history_listing(store, eid, budget_chars=2 * half - 2))

    assert on["returned"] == k
    assert under["returned"] == k - 1


def test_l6_at_least_one_full_record(history):
    store, eid, _ = history
    answer = json.loads(server._entity_history_listing(store, eid, budget_chars=4000))
    assert answer["returned"] >= 1 or answer["omitted_ids"]


def test_l6b_overflow_falls_back_to_ids_and_counts_the_unlisted(history):
    store, eid, _ = history
    everything = server._get_entity_history_impl(store, eid)
    text = server._entity_history_listing(store, eid, budget_chars=4000)
    answer = json.loads(text)

    assert len(text) <= 4000
    assert "omitted_ids" in answer
    listed = _reachable(answer)
    assert len(listed) + answer.get("omitted_ids_unlisted", 0) == len(everything)
    assert "Store.get_decision" in answer["hint"] and "Store.get_fact" in answer["hint"]

    garbage = "9" * 5000
    clipped = server._entity_history_listing(store, garbage, budget_chars=4000)
    assert len(clipped) <= 4000
    assert json.loads(clipped)["entity_id"].endswith("…")


# -- L7, L8: drill_down -------------------------------------------------------------------


def _domain(store: Store, count: int, *, drifted: int = 0, size: int = 250, slug="capture"):
    dom = server._add_domain_impl(store, None, slug=slug, title=slug.title(), summary="why.")
    server._ratify_impl(store, accept=[dom["domain_id"]])
    entity = store.find_abstract_entity(f"domain:{slug}")
    ids = []
    for i in range(count):
        d = _decision(store, i, size=size, kind=DecisionKind.GOTCHA)
        _bind(store, d.id, entity.entity_id)
        ids.append(d.id)
    if drifted:
        payload = {"head": "h", "computed_at": NOW.isoformat(), "by_commit": {"c": ids[-drifted:]}}
        store.set_meta(DRIFT_CACHE_KEY, json.dumps(payload))
    return ids


def test_l7_drill_down_is_cut_with_its_telemetry_ids_and_legend(store, monkeypatch):
    monkeypatch.setattr(server, "_session_key", lambda _s: "sess-1")
    ids = _domain(store, 80, drifted=3)  # the drifted ones are the oldest: they get cut
    whole = server._drill_down(("capture"), store, None)
    assert len(whole["decisions"]) == 80 and "legend" in whole

    out = server._drill_down_impl(store, None, "capture", limit=25, budget_chars=24000)
    shown = len(out["decisions"])

    assert 0 < shown < 80
    assert len(server._dump(out)) <= 24000
    assert out["decisions_total"] == 80 and out["decisions_omitted"] == 80 - shown
    assert out["omitted_ids"] == whole["decision_ids"][shown:]
    assert "retrieve_decisions(ids=" in out["hint"]
    assert "legend" not in out  # no shown line is [drifted]
    (event,) = store.render_events()
    assert event["emitted"] == event["selected"] == shown
    assert (event["degraded"], event["dropped_for_budget"], event["chars_used"]) == (0, 0, 0)
    # the per-id counters count exactly what was shown
    assert set(store.retrieval_shows()) == set(whole["decision_ids"][:shown])
    assert set(ids) - set(store.retrieval_shows()) == set(whole["decision_ids"][shown:])

    # now the drifted records are the newest: shown, so the legend stays
    store.set_meta(
        DRIFT_CACHE_KEY,
        json.dumps({"head": "h", "computed_at": NOW.isoformat(), "by_commit": {"c": ids[:3]}}),
    )
    again = server._drill_down_impl(store, None, "capture", limit=25, budget_chars=24000)
    assert again["legend"] == _DRIFT_LEGEND
    assert any(" [drifted]" in line for line in again["decisions"])


def test_l7_limit_cuts_before_the_budget(store):
    _domain(store, 30, size=20)
    out = server._drill_down_impl(store, None, "capture", limit=7, budget_chars=40000)
    assert len(out["decisions"]) == 7 and out["decisions_omitted"] == 23


def test_l8_a_small_domain_is_byte_identical_to_the_unbounded_answer(store):
    _domain(store, 3)
    whole = server._drill_down_impl(store, None, "capture")
    cut = server._drill_down_impl(store, None, "capture", limit=25, budget_chars=24000)
    assert server._dump(cut) == server._dump(whole)
    assert "hint" not in cut and "omitted_ids" not in cut
    text, parsed = wire("drill_down", {"domain_slug": "capture"})
    assert parsed["found"] is True
    missing = call("drill_down", {"domain_slug": "no-such"})
    assert json.loads(missing.content[0].text) == {"found": False, "candidates": ["capture"]}


# -- L9: list_domain_candidates -----------------------------------------------------------


def _graph(path: Path, communities: int) -> Path:
    nodes = [
        {
            "id": f"c{c}n{i}",
            "label": f"Thing{c}_{i}",
            "norm_label": f"thing{c}_{i}",
            "file_type": "code",
            "source_file": f"area{c % 40}/f{c}_{i}.py",
            "community": c,
        }
        for c in range(communities)
        for i in range(5)
    ]
    path.write_text(json.dumps({"built_at_commit": "v1", "nodes": nodes, "links": []}))
    return path


def test_l9_candidates_are_cut_by_the_budget_and_the_totals_follow(store, tmp_path):
    reader = GraphifyReader(_graph(tmp_path / "g.json", 300))
    free = server._list_domain_candidates_impl(store, reader, limit=None)
    free_text = server._dump(free)
    assert len(free_text) > 40000 and free["truncated"] is False

    cut = server._list_domain_candidates_impl(store, reader, limit=None, budget_chars=24000)
    flat = [c for g in cut["groups"] for c in g["candidates"]] + cut["ungrouped"]
    assert len(server._dump(cut)) <= 24000
    assert cut["truncated"] is True
    assert cut["total_candidates"] == len(flat) < 300
    assert cut["omitted_by_budget"] == 300 - len(flat)
    assert all(g["candidates"] for g in cut["groups"])
    assert all(
        g["member_total"] == sum(c["members"] for c in g["candidates"]) for g in cut["groups"]
    )
    assert [g["path"] for g in cut["groups"]] == sorted(g["path"] for g in cut["groups"])
    assert cut["total_significant"] == 300
    assert "limit=0 lifts the count limit, not the budget" in cut["note"]
    assert "paths=" in cut["note"] and "max 40000" in cut["note"]

    both = server._list_domain_candidates_impl(store, reader, limit=100, budget_chars=24000)
    assert "limit=100 left" in both["note"] and "the character budget kept" in both["note"]

    narrowed = server._list_domain_candidates_impl(
        store, reader, limit=None, paths=["area3/"], budget_chars=24000
    )
    assert narrowed["truncated"] is False and narrowed["total_candidates"] > 0


def test_l9_limit_semantics_and_byte_identity_are_kept(store, tmp_path):
    reader = GraphifyReader(_graph(tmp_path / "g.json", 30))
    plain = server._list_domain_candidates_impl(store, reader, limit=None)
    assert (
        server._list_domain_candidates_impl(store, reader, limit=None, budget_chars=None) == plain
    )
    limited = server._list_domain_candidates_impl(store, reader, limit=10)
    assert limited["truncated"] is True and "limit=10 cut the list" in limited["note"]
    assert "omitted_by_budget" not in limited

    monkey = pytest.MonkeyPatch()
    monkey.setenv("SIDEGRAPH_GRAPH", str(tmp_path / "g.json"))
    try:
        everything, _ = wire("list_domain_candidates", {"limit": 0, "budget_chars": 40000})
        assert json.loads(everything)["total_candidates"] == 30
        wide, _ = wire("list_domain_candidates", {"limit": 200, "budget_chars": 40000})
        assert json.loads(wide)["total_candidates"] == 30
    finally:
        monkey.undo()


# -- L10, L11: list_proposed --------------------------------------------------------------


def _proposed(store: Store, decisions: int = 30, facts: int = 6, domains: int = 4) -> str:
    """A pending queue with the oversized decision (a block over 4,000 characters) first.
    Returns the id of that decision."""
    big = _decision(store, 900, status=DecisionStatus.PROPOSED, size=2500)
    for i in range(decisions):
        _decision(store, i, status=DecisionStatus.PROPOSED, size=250)
    for i in range(facts):
        _fact(store, 500 + i, status=DecisionStatus.PROPOSED, size=300)
    for i in range(domains):
        server._add_domain_impl(store, None, slug=f"dom-{i}", title=f"Dom {i}", summary="s.")
    return big.id


def test_l10_a_long_queue_is_cut_and_the_rest_is_listed_by_id(store):
    big_id = _proposed(store)
    assert len(server._format_proposed_decision_block(store, store.get_decision(big_id))) > 4000

    text = server._list_proposed_impl(store, 4000)

    assert len(text) <= 4000
    first = text.splitlines()[0]
    assert first.startswith("Pending: 31 decisions, 6 facts, 4 domains. Showing ")
    shown, tail = text.split("Not shown (")
    assert "raise budget_chars (max 40000)" in tail
    assert big_id in tail  # the oversized block is in the tail by id ...
    assert big_id not in shown  # ... and not rendered
    # no block is cut mid-way: every rendered decision has its "from:" line, the last of the block
    rendered = [b for b in shown.split("\n\n") if "[gotcha]" in b]
    assert rendered and all("what:" in b and "from:" in b for b in rendered)
    n_shown = shown.count("[gotcha]")
    assert f"Showing {n_shown} decisions" in first
    # the tail counts all three kinds
    assert tail.startswith(f"{31 - n_shown} decisions, ")


def test_l10_facts_and_domains_still_fill_after_a_decision_does_not_fit(store):
    big_id = _decision(store, 900, status=DecisionStatus.PROPOSED, size=2500).id
    small = _decision(store, 1, status=DecisionStatus.PROPOSED, size=100)
    fact = _fact(store, 500, status=DecisionStatus.PROPOSED, size=100)
    server._add_domain_impl(store, None, slug="dom", title="Dom", summary="s.")
    # the big decision is newer than the small one: it comes first and does not fit
    text = server._list_proposed_impl(store, 4000)
    shown, _, tail = text.partition("Not shown (")

    assert big_id in tail and big_id not in shown
    assert small.id in shown and fact.id in shown and "Domains:" in shown
    assert "Showing 1 decisions, 1 facts, 1 domains." in text


def test_l10_the_tail_lists_ids_in_the_order_of_the_blocks(store):
    _proposed(store, decisions=40, facts=30, domains=0)
    text = server._list_proposed_impl(store, 4000)
    tail = text.partition("Not shown (")[2]
    ids = [w for w in tail.replace(",", " ").split() if len(w) == 26 and w.isalnum()]
    assert ids
    position = {}
    for kind, items in (
        (0, [d.id for d in store.iter_proposed()]),
        (1, [f.id for f in store.iter_proposed_facts()]),
    ):
        for n, id_ in enumerate(items):
            position[id_] = (kind, n)
    ranks = [position[i] for i in ids if i in position]
    assert ranks == sorted(ranks)
    assert "more ids" in tail


def test_l10_the_budget_is_a_ceiling_at_every_value(store):
    _proposed(store)
    for budget in (4000, 6000, 12000, 24000, 40000):
        assert len(server._list_proposed_impl(store, budget)) <= budget


def test_l11_a_small_queue_renders_exactly_as_before(store):
    _proposed(store, decisions=2, facts=1, domains=1)
    plain = server._list_proposed_impl(store)
    assert server._list_proposed_impl(store, 24000) == plain
    assert "Pending:" not in plain and "Not shown" not in plain
    assert wire("list_proposed")[0] == plain


# -- L12, L13: the cap on the whole text --------------------------------------------------


def _drift(store: Store, ids: list[str]) -> None:
    payload = {"head": "h", "computed_at": NOW.isoformat(), "by_commit": {"c": ids}}
    store.set_meta(DRIFT_CACHE_KEY, json.dumps(payload))


def _gotcha_ids(ids: list[str]) -> list[str]:
    return [x for n, x in enumerate(ids) if (n % 30) % 2 == 0]  # i % 4 == 0 in the fixture


def _tagged(text: str) -> list[str]:
    """Lines that carry the `[drifted]` tag: the legend's own text cannot satisfy this."""
    return [line for line in text.splitlines() if " [drifted]" in line]


def _note_sections(text: str) -> list[str]:
    note = text[text.index("[output cut") :].split("]")[0]
    return note.split("from: ")[1].split(". ")[0].split(", ")


@pytest.mark.parametrize("count", [1, 8])
def test_l12_get_task_context_is_capped_and_keeps_its_advice(ctx_env, uncapped_related, count):
    store, reader, paths, ids = ctx_env
    # a guessed seed (leading ./) puts "How your seeds were read" in the trailer, a missing one
    # "Not in the code graph"
    seeds = [f"./{paths[0]}", "src/missing/nope.py", *paths[1:count]]
    _drift(store, ids[:40])
    text = server._get_task_context_impl(store, reader, seeds, None, 10**9, 10**9)

    assert len(text) <= 40000
    assert "How your seeds were read" in text
    assert "Not in the code graph" in text
    assert server._STANDING_SUPERSEDE_HINT in text
    assert "[output cut at 40000 characters" in text
    # the cut order: the structural map goes first, the records stay (partly)
    assert _note_sections(text)[0] == "structure"
    assert "## ⚠ Known mistakes" in text
    assert "structure" in _note_sections(text) and "## Structural map" not in text
    # the legend follows the tag on a kept line, never the legend's own text
    assert (_DRIFT_LEGEND in text) == bool(_tagged(text[: text.index("[output cut")]))
    # cut at a line boundary: the note starts its own paragraph
    assert "\n\n[output cut" in text


def test_l12_the_cut_order_removes_related_before_the_records(ctx_env, uncapped_related):
    store, reader, paths, _ = ctx_env
    text = server._get_task_context_impl(store, reader, paths[:1], None, 10**9, 10**9)
    sections = _note_sections(text)
    assert sections[:2] == ["structure", "related decisions"]
    assert "records" not in sections or sections.index("records") > 1


def test_l12_the_legend_survives_when_a_kept_line_is_drifted(ctx_env):
    store, reader, paths, ids = ctx_env
    _drift(store, _gotcha_ids(ids))
    text = server._get_task_context_impl(store, reader, paths, None, 10**9, 10**9)
    assert len(text) <= 40000 and "[output cut" in text
    assert _tagged(text) and _DRIFT_LEGEND in text


def test_l12_the_legend_goes_when_every_drifted_line_was_cut(ctx_env, monkeypatch):
    store, reader, paths, ids = ctx_env
    adrs = [x for x in ids if x not in set(_gotcha_ids(ids))]

    def ask(*, capped: bool) -> str:
        with monkeypatch.context() as patch:
            if not capped:
                patch.setattr(
                    server,
                    "_hard_cap",
                    lambda body, trailer, how, **kw: "\n\n".join(
                        p for p in (body, kw.get("legend"), *trailer) if p
                    ),
                )
            return server._get_task_context_impl(store, reader, paths[:2], None, 10**9, 10**9)

    # The last ADR line of the Decisions section is the first of it that the cap cuts.
    _drift(store, adrs)
    whole, cut = ask(capped=False), ask(capped=True)
    last = _tagged(whole)[-1]
    assert last not in cut and _tagged(cut)  # precondition: the cap reaches into Decisions
    cut_ids = re.findall(r"\(id: ([0-9A-Z]{26})\)", last)
    assert len(cut_ids) == 1

    # Now only that one is drifted: it renders in the whole answer, and none survives the cut.
    _drift(store, cut_ids)
    whole, cut = ask(capped=False), ask(capped=True)
    assert _tagged(whole) and _DRIFT_LEGEND in whole  # precondition: they do render
    assert "[output cut" in cut
    assert not _tagged(cut) and _DRIFT_LEGEND not in cut
    assert server._STANDING_SUPERSEDE_HINT in cut


def test_l12_nearest_records_the_cap_cut_are_not_counted_as_shown(
    ctx_env, uncapped_related, monkeypatch
):
    """Telemetry follows the delivered text, the nearest-anchored records included. Records are
    matched by title. A degraded line is a tight line with an id outside the Related section,
    whose capped one-liners carry ids too."""
    import re

    store, reader, paths, _ = ctx_env
    monkeypatch.setattr(server, "_session_key", lambda _s: "sess-1")
    text = server._get_task_context_impl(store, reader, ["src/pkg0/empty.py"], None, 40000, 40000)
    assert _note_sections(text)[0] == "nearest records"  # precondition: the cap cut them
    in_text = set(re.findall(r"Decision \d+-\d+ about worker", text))
    shown = {store.get_decision(i).title for i in store.retrieval_shows()}
    assert in_text and shown == in_text
    [event] = store.render_events()
    assert event["emitted"] == len(in_text)
    outside_related = re.sub(r"(?ms)^#+ Related\n.*?(?=^#|\Z)", "", text)
    tight = [ln for ln in outside_related.splitlines() if "(id: " in ln and "(context:" not in ln]
    assert event["degraded"] == len(tight)  # the nearest records' degraded ids are merged


@pytest.mark.parametrize("heading", H2_HEADINGS)
def test_r3_1_nearest_records_with_a_heading_line_in_their_text_are_cut_whole_and_counted_once(
    ctx_env, monkeypatch, heading
):
    check_heading_line_records_are_cut_whole(ctx_env, monkeypatch, 4000, heading)


def test_r4_1_every_bucket_heading_renders_at_the_nearest_blocks_level():
    from sidegraph import nearest_anchored
    from sidegraph.retrieval import TaskContext

    ctx = TaskContext(
        mistakes=["- m"], decisions=["- d"], facts=["- f"], related=["- r"], unratified=["- u"]
    )
    text, _ = nearest_anchored.records_block(ctx, guarded=True)
    heads = [ln for ln in text.splitlines() if ln.startswith("#")]
    assert heads[0].startswith("## Nearest anchored records")
    assert heads[1:] == [
        "### ⚠ Known mistakes & gotchas",
        "### Decisions",
        "### Known facts",
        "### Related",
        "### Unratified proposals",
    ]


def test_l12_query_decisions_is_capped_and_keeps_the_hint(ctx_env):
    store, reader, paths, ids = ctx_env
    text = server._query_decisions_impl(store, reader, paths, None, 10**9)
    assert len(text) <= 40000
    assert text.endswith(server._STANDING_SUPERSEDE_HINT)
    assert "[output cut at 40000 characters" in text
    # its advice names its own parameter, not get_task_context's
    assert "lower budget_chars" in text and "structure_budget" not in text
    structure = server._query_structure_impl(store, reader, paths, None, 10**9)
    assert "structure_budget" not in structure

    _drift(store, _gotcha_ids(ids))
    drifted = server._query_decisions_impl(store, reader, paths, None, 10**9)
    assert _tagged(drifted) and _DRIFT_LEGEND in drifted
    assert drifted.index(_DRIFT_LEGEND) < drifted.index(server._STANDING_SUPERSEDE_HINT)
    assert drifted.endswith(server._STANDING_SUPERSEDE_HINT)
    low = server._query_structure_impl(store, reader, paths[:1], None, 120)
    assert low and len(low) < 400  # no floor: a tiny budget still works


def test_l12_an_answer_under_the_cap_is_untouched(ctx_env):
    store, reader, paths, _ = ctx_env
    text = server._get_task_context_impl(store, reader, paths[:1], None, 4000, 6000)
    assert "[output cut" not in text and len(text) < 40000


def test_l13_retrieve_decisions_maximum_is_40000(store):
    for i in range(160):
        _decision(store, i, size=600)
    for budget in (10**9, 40000):
        text = server._decision_listing(
            store, None, status="accepted", limit=100, budget_chars=budget
        )
        assert len(text) <= 40000
    assert server._LISTING_BUDGET_MAX == 40000
    hint = json.loads(
        server._decision_listing(store, None, status="accepted", limit=100, budget_chars=4000)
    )["hint"]
    assert "max 40000" in hint


# -- L15: the docs guard ------------------------------------------------------------------

REPO = Path(__file__).resolve().parent.parent


def test_l15_listing_tools_document_their_budget_and_no_doc_says_everything():
    reference = (REPO / "docs/reference/mcp-tools.md").read_text()
    for tool in (
        "list_facts",
        "get_entity_history",
        "drill_down",
        "list_domain_candidates",
        "list_proposed",
        "retrieve_decisions",
    ):
        section = reference[reference.index(f"## `{tool}`") :]
        section = section.split("\n## ")[0]
        assert "budget_chars" in section, tool

    shipped = [
        *(REPO / "docs").rglob("*.md"),
        *(REPO / "plugin").rglob("SKILL.md"),
        REPO / "README.md",
    ]
    banned = (
        "limit=0` returns the full candidate list",
        "`limit=0` for everything",
        "pass a wider `limit`",
        "render of everything pending",
        "renders every pending",
        "still list every pending record",
    )
    for path in shipped:
        text = path.read_text()
        for phrase in banned:
            assert phrase not in text, f"{path.relative_to(REPO)}: {phrase!r}"


# -- review follow-ups: edges, clamps through the wire, hints -----------------------------


def test_l9_the_note_does_not_push_a_limit_cut_answer_over_the_budget(store, tmp_path):
    reader = GraphifyReader(_graph(tmp_path / "g.json", 300))
    for budget, limits in ((24000, range(60, 110)), (40000, range(130, 175)), (4000, range(5, 20))):
        for limit in limits:
            out = server._list_domain_candidates_impl(
                store, reader, limit=limit, budget_chars=budget
            )
            assert len(server._dump(out)) <= budget, (budget, limit)


def test_l9_member_totals_follow_a_cut_inside_a_group(store, tmp_path):
    reader = GraphifyReader(_graph(tmp_path / "g.json", 300))
    mid_group = 0
    full = server._list_domain_candidates_impl(store, reader, limit=None)
    by_path = {g["path"]: len(g["candidates"]) for g in full["groups"]}
    for budget in range(22000, 24001, 250):
        out = server._list_domain_candidates_impl(store, reader, limit=None, budget_chars=budget)
        for g in out["groups"]:
            assert g["member_total"] == sum(c["members"] for c in g["candidates"])
            assert len(g["candidates"]) in range(1, 41)
        mid_group += sum(1 for g in out["groups"] if len(g["candidates"]) < by_path[g["path"]])
    assert mid_group  # precondition: some budget cut a group in the middle


def test_l9_a_bound_at_its_maximum_is_not_offered(store, tmp_path):
    reader = GraphifyReader(_graph(tmp_path / "g.json", 300))
    out = server._list_domain_candidates_impl(store, reader, limit=None, budget_chars=40000)
    assert "raise budget_chars" not in out["note"] and "paths=" in out["note"]


def test_l7_omitted_ids_overflow_is_counted_and_the_hint_names_the_bound(store):
    ids = _domain(store, 200, size=10)
    out = server._drill_down_impl(store, None, "capture", limit=100, budget_chars=4000)
    assert len(server._dump(out)) <= 4000
    assert out["decisions"] == [] or len(out["decisions"]) < 200
    listed = out["omitted_ids"]
    assert len(listed) + out.get("omitted_ids_unlisted", 0) == out["decisions_omitted"]
    assert "did not fit in omitted_ids" in out["hint"]
    whole = server._drill_down("capture", store, None)["decision_ids"]
    assert listed == whole[len(out["decisions"]) :][: len(listed)]
    assert set(whole) == set(ids)

    by_limit = server._drill_down_impl(store, None, "capture", limit=3, budget_chars=40000)
    assert (
        "limit=3 cut the list" in by_limit["hint"] and "raise limit (max 100)" in by_limit["hint"]
    )
    assert "budget_chars" not in by_limit["hint"]
    _domain(store, 120, size=400, slug="wide")
    by_budget = server._drill_down_impl(store, None, "wide", limit=100, budget_chars=40000)
    assert "character budget" in by_budget["hint"]
    assert "raise budget_chars" not in by_budget["hint"]  # already at its maximum


def test_history_hint_names_only_the_keys_that_are_present(history):
    store, eid, _ = history
    small = store.get_or_create_abstract_entity("domain:small")
    for i in range(30):
        _bind(store, _decision(store, 3000 + i, size=250).id, small.entity_id)
    rows = json.loads(server._entity_history_listing(store, small.entity_id, limit=3))
    assert "omitted_ids" not in rows and "omitted_records" in rows
    assert "short rows in omitted_records" in rows["hint"] and "omitted_ids" not in rows["hint"]
    ids = json.loads(server._entity_history_listing(store, eid, budget_chars=4000))
    assert "omitted_records" not in ids and "bare ids in omitted_ids" in ids["hint"]
    assert "short rows" not in ids["hint"]
    if "omitted_ids_unlisted" in ids:
        assert "omitted_ids_unlisted" in ids["hint"]


def test_history_ids_stay_newest_first_and_the_first_record_is_never_dropped(store):
    entity = store.get_or_create_abstract_entity("domain:big")
    for i in range(3):
        _bind(store, _decision(store, i, size=1200).id, entity.entity_id)
    eid = entity.entity_id
    everything = server._get_entity_history_impl(store, eid)
    assert len(server._dump(everything[0])) > 2000  # over half of a 4,000 budget
    out = json.loads(server._entity_history_listing(store, eid, budget_chars=4000))
    assert out["returned"] == 1  # at least one full record, though it is over half the budget
    assert [r["id"] for r in out["records"]] == [everything[0]["id"]]
    assert [r["id"] for r in out["omitted_records"]] == [r["id"] for r in everything[1:]]


def test_history_omitted_ids_run_newest_first(history):
    store, eid, _ = history
    everything = [r["id"] for r in server._get_entity_history_impl(store, eid)]
    out = json.loads(server._entity_history_listing(store, eid, budget_chars=4000))
    listed = out["omitted_ids"]
    start = out["returned"] + len(out.get("omitted_records", []))
    assert listed == everything[start : start + len(listed)]


def test_the_shells_clamp_limit_and_budget(store, tmp_path, monkeypatch):
    _domain(store, 120, size=300)
    _domain(store, 30, size=300, slug="small")
    # limit 0 -> 1 and a budget of 1 -> 4000
    _, tiny = wire("drill_down", {"domain_slug": "small", "limit": 0, "budget_chars": 1})
    assert len(tiny["decisions"]) == 1
    # a huge budget and limit are held to 100 decisions and 40,000 characters
    text, huge = wire(
        "drill_down", {"domain_slug": "capture", "limit": 1000, "budget_chars": 10**9}
    )
    assert len(text) <= 40000 and 25 < len(huge["decisions"]) < 120

    graph = _graph(tmp_path / "g.json", 300)
    monkeypatch.setenv("SIDEGRAPH_GRAPH", str(graph))
    text, parsed = wire("list_domain_candidates", {"limit": 0, "budget_chars": 10**9})
    assert len(text) <= 40000 and parsed["truncated"] is True
    text, parsed = wire("list_domain_candidates", {"budget_chars": 1})
    assert len(text) <= 4000

    _proposed(store, decisions=150, facts=20, domains=5)
    text, _ = wire("list_proposed", {"budget_chars": 10**9})
    assert 24000 < len(text) <= 40000
    text, _ = wire("list_proposed", {"budget_chars": 1})
    assert len(text) <= 4000 and text.startswith("Pending:")
    assert "Store.get_decision(id)" in text


def test_list_proposed_does_not_offer_a_budget_at_its_maximum(store):
    _proposed(store, decisions=150, facts=20, domains=5)
    text = server._list_proposed_impl(store, 40000)
    assert "Not shown (" in text and "raise budget_chars" not in text
    assert "raise budget_chars (max 40000)" in server._list_proposed_impl(store, 24000)


def test_a_partial_cut_keeps_the_blank_line_before_the_next_heading():
    body = "guard\n\n## Structural map\n" + "\n".join(f"s{i}" * 20 for i in range(80))
    body += "\n\n## Related\n" + "\n".join(f"r{i}" * 20 for i in range(80))
    out = server._hard_cap(body, ["tail advice"], "how", cap=2500)
    assert len(out) <= 2500 and out.endswith("tail advice")
    assert "\n\n## Related" in out  # the map was cut in the middle, the heading is still apart
    assert "from: structure" in out


def test_hard_cap_keeps_the_legend_only_while_a_kept_line_is_drifted():
    def records(tag: str) -> str:
        return "## Decisions\n" + "\n".join(f"- kept {i}{tag if i == 0 else ''}" for i in range(40))

    tagged_map = "## Structural map\n" + "\n".join(f"- node {i} x [drifted]" for i in range(3))
    for tag, legend_kept in (("", False), (" [drifted]", True)):
        body = f"guard\n\n{records(tag)}\n\n{tagged_map}"
        whole = server._hard_cap(body, ["advice"], "how", legend="LEGEND", cap=10**6)
        cap = len(whole) - len(tagged_map) - 10  # the whole map has to go, and a little more
        out = server._hard_cap(body, ["advice"], "how", legend="LEGEND", cap=cap)
        assert len(out) <= cap and "## Structural map" not in out and "node 0" not in out
        assert ("LEGEND" in out) is legend_kept, tag
        assert out.endswith("advice")


def test_proposed_tail_does_not_promise_by_id_bypasses_visibility(store):
    decision = _decision(store, 1, status=DecisionStatus.PROPOSED, size=60_000)
    text = server._list_proposed_impl(store, budget_chars=4000)
    assert decision.id in text
    assert "Store.get_decision" in text
    assert "Store.get_fact" in text
    assert "may be hidden" in text
    assert "Read one with retrieve_decisions" not in text


def test_history_tail_explains_visibility_and_offers_local_record_reads(history):
    _store, eid, _ = history
    answer = json.loads(server._entity_history_listing(_store, eid, budget_chars=4000))
    assert answer["omitted"] > 0
    assert "surfacing policy" in answer["hint"]
    assert "Store.get_decision" in answer["hint"]
    assert "Store.get_fact" in answer["hint"]


# -- L6: the history listing obeys the proposal-surfacing policy ---------------------------


def _history_with_a_proposal(
    store: Store, age: timedelta = timedelta(0)
) -> tuple[str, str, str, str, str]:
    """An entity with a superseded pair, a rejected decision, one proposed decision and one
    proposed fact. Dated from the real clock, so only the policy under test can hide the
    proposals."""
    entity = store.get_or_create_abstract_entity("domain:proposals")
    now = datetime.now(UTC) - age
    old = Decision(
        title="old",
        kind=DecisionKind.GOTCHA,
        context="c",
        choice="ch",
        valid_from=now - timedelta(days=2),
        provenance=Provenance(source="manual"),
    )
    store.add_decision(old)
    new = Decision(
        title="new",
        kind=DecisionKind.GOTCHA,
        context="c",
        choice="ch",
        supersedes=old.id,
        valid_from=now - timedelta(days=1),
        provenance=Provenance(source="manual"),
    )
    store.add_decision(new)
    pending = Decision(
        title="pending",
        kind=DecisionKind.GOTCHA,
        context="c",
        choice="ch",
        status=DecisionStatus.PROPOSED,
        valid_from=now,
        provenance=Provenance(source="agent"),
    )
    store.add_decision(pending)
    fact = Fact(
        statement="pending fact",
        source="bench",
        status=DecisionStatus.PROPOSED,
        valid_from=now,
        provenance=Provenance(source="agent"),
    )
    store.add_fact(fact)
    dropped = Decision(
        title="dropped",
        kind=DecisionKind.GOTCHA,
        context="c",
        choice="ch",
        status=DecisionStatus.REJECTED,
        valid_from=now - timedelta(days=3),
        provenance=Provenance(source="agent"),
    )
    store.add_decision(dropped)
    for record_id in (old.id, new.id, pending.id, fact.id, dropped.id):
        _bind(store, record_id, entity.entity_id)
    return entity.entity_id, old.id, pending.id, fact.id, dropped.id


def test_l6_history_hides_proposals_in_regulated_mode(store, monkeypatch):
    eid, old_id, pending_id, fact_id, dropped_id = _history_with_a_proposal(store)
    shown = {r["id"] for r in json.loads(server._entity_history_listing(store, eid))["records"]}
    assert {pending_id, fact_id, old_id} <= shown

    monkeypatch.setenv("SIDEGRAPH_UNRATIFIED", "off")
    answer = json.loads(server._entity_history_listing(store, eid))
    assert pending_id not in _reachable(answer) and fact_id not in _reachable(answer)
    # Superseded and rejected history stays: it is the point of the tool.
    assert {old_id, dropped_id} <= _reachable(answer)


def test_l6_history_hides_proposals_outside_the_window(store, monkeypatch):
    eid, old_id, pending_id, fact_id, dropped_id = _history_with_a_proposal(
        store, age=timedelta(days=10)
    )
    monkeypatch.setenv("SIDEGRAPH_PROPOSAL_WINDOW_DAYS", "30")
    shown = _reachable(json.loads(server._entity_history_listing(store, eid)))
    assert {pending_id, fact_id} <= shown
    monkeypatch.setenv("SIDEGRAPH_PROPOSAL_WINDOW_DAYS", "5")
    answer = json.loads(server._entity_history_listing(store, eid))
    assert pending_id not in _reachable(answer) and fact_id not in _reachable(answer)
    assert {old_id, dropped_id} <= _reachable(answer)
