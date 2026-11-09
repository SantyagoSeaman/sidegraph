"""The cited-back figure's pure rules: where a delivered id sits in Sidegraph's own output
(placements) and when a later quotation counts (the echo rule).
# see design/superpowers/specs/2026-11-07-stats-cited-back-and-asked-design.md (D6, D7)
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import fastmcp

from sidegraph import server
from sidegraph.engine.reader import GraphifyReader
from sidegraph.host import hooks
from sidegraph.hot_index import Record
from sidegraph.retrieval import _fmt_decision, _fmt_fact
from sidegraph.schema import (
    AnchorBinding,
    Decision,
    DecisionKind,
    DecisionStatus,
    Descriptor,
    Entity,
    Fact,
    Provenance,
)
from sidegraph.stats import echo
from sidegraph.stats.echo import Authored, Delivery
from sidegraph.store import Store

FIXTURE = Path(__file__).parent / "fixtures" / "mini_graph.json"
A, B = "01M3T0VEAEAAAAAAAAAAAAAAAA", "01M3T0VEAFBBBBBBBBBBBBBBBB"
KNOWN = frozenset({A, B})


def _delivery(text: str, channel: str = "mcp", position: int = 5, **kw) -> Delivery:
    return Delivery(channel=channel, position=position, text=text, **kw)


def _decision(title: str, **fields) -> Decision:
    fields.setdefault("kind", DecisionKind.GOTCHA)
    return Decision(
        title=title,
        context="c",
        choice=fields.pop("choice", "x"),
        status=DecisionStatus.ACCEPTED,
        valid_from=datetime.now(UTC),
        provenance=Provenance(source="manual"),
        **fields,
    )


def _call(name: str, arguments: dict):
    async def run():
        async with fastmcp.Client(server.mcp) as client:
            return await client.call_tool(name, arguments, raise_on_error=False)

    return asyncio.run(run())


def _anchored_store(tmp_path, monkeypatch, *decisions: Decision) -> Store:
    store = Store(tmp_path / "s")
    monkeypatch.setattr(server, "_store", store)
    monkeypatch.setenv("SIDEGRAPH_GRAPH", str(tmp_path / "no-such-graph.json"))
    monkeypatch.setattr(server, "_session_key", lambda _s: "sess-1")
    entity = store.upsert_entity(
        Entity(
            canonical_name="Trader",
            descriptor=Descriptor(name="Trader", file_path="trader/exec.py"),
        )
    )
    for d in decisions:
        store.add_decision(d)
        store.add_binding(
            AnchorBinding(record_id=d.id, entity_id=entity.entity_id, tier=2, status="live")
        )
    return store


# -- placements: real renders ---------------------------------------------------------------


def test_placements_from_a_task_context_render(tmp_path, monkeypatch):
    first, second = _decision("deadlock"), _decision("stale cache", kind=DecisionKind.ADR)
    store = _anchored_store(tmp_path, monkeypatch, first, second)
    text = server._get_task_context_impl(
        store, GraphifyReader(FIXTURE), ["trader/exec.py"], None, 4000, 6000
    )
    found = echo.placements(_delivery(text), frozenset({first.id, second.id}))
    assert sorted(found) == sorted([first.id, second.id])
    assert len(found) == len(set(found))
    store.close()


def test_a_record_followed_by_evidence_lines_is_still_placed(tmp_path, monkeypatch):
    """A real render puts a supporting fact's `  evidence:` line after the decision's id line."""
    decision = _decision("deadlock")
    store = _anchored_store(tmp_path, monkeypatch, decision)
    store.add_fact(
        Fact(
            statement="two writers took the lock in opposite order",
            source="trace",
            supports=[decision.id],
            status=DecisionStatus.ACCEPTED,
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual"),
        )
    )
    text = server._get_task_context_impl(
        store, GraphifyReader(FIXTURE), ["trader/exec.py"], None, 4000, 6000
    )
    assert "  evidence: two writers" in text
    assert echo.placements(_delivery(text), frozenset({decision.id})) == [decision.id]
    store.close()


def test_placements_from_a_pre_tool_use_block():
    records = [
        Record({"id": A, "kind": "gotcha", "title": "deadlock", "choice": "order locks"}, False),
        Record({"id": B, "kind": "adr", "title": "cache", "choice": "keep it"}, False),
    ]
    text = hooks._records_block("a.py", records)
    assert "(id " in text
    assert echo.placements(_delivery(text, "pre_tool_use"), KNOWN) == [A, B]


def test_placements_from_listing_json(tmp_path, monkeypatch):
    first, second = _decision("alpha lock"), _decision("beta other")
    store = _anchored_store(tmp_path, monkeypatch, first, second)
    known = frozenset({first.id, second.id})
    narrowed = _call("retrieve_decisions", {"query": "alpha"}).content[0].text
    assert echo.placements(_delivery(narrowed), known) == [first.id]
    overview = _call("retrieve_decisions", {}).content[0].text
    assert json.loads(overview)["overview"] is True
    assert echo.placements(_delivery(overview), known) == []
    found = _call("find_entity", {"name": "Trader", "file_path": "trader/exec.py"})
    assert echo.placements(_delivery(found.content[0].text), known) == []
    store.close()


def test_placements_from_a_real_drill_down_with_rendered_string_items(tmp_path):
    store = Store(tmp_path / "s")
    dom = server._add_domain_impl(store, None, slug="payments", title="Payments", summary="s.")
    server._ratify_impl(store, accept=[dom["domain_id"]])
    decision = _decision("use postgres", kind=DecisionKind.ADR)
    store._write_decision(decision)
    store._conn.commit()
    entity = store.find_abstract_entity("domain:payments")
    store.add_binding(
        AnchorBinding(record_id=decision.id, entity_id=entity.entity_id, tier=1, status="live")
    )
    text = server._dump(server._drill_down_impl(store, None, "payments"))
    assert isinstance(json.loads(text)["decisions"][0], str)
    assert echo.placements(_delivery(text), frozenset({decision.id})) == [decision.id]
    store.close()


def test_prose_references_are_not_placements():
    """A's prose mentions B's id; only A's own terminal suffix is a placement."""
    for choice in (f"see (id: {B})", f"see (id: {B})\ncontinued"):
        a = Decision(
            id=A,
            title="Alpha",
            kind=DecisionKind.ADR,
            context="c",
            choice=choice,
            status=DecisionStatus.ACCEPTED,
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual"),
        )
        line = _fmt_decision(a, detailed=True)
        assert f"(id: {B})" in line
        assert echo.placements(_delivery(line), KNOWN) == [A]


def test_every_record_of_a_two_file_hook_answer_is_placed():
    """A hook prints one line per record and a header per file between them: a record's id must
    survive the header, the overflow line and the closing marker that follow it."""
    first = Record({"id": A, "kind": "gotcha", "title": "deadlock", "choice": "order"}, False)
    second = Record({"id": B, "kind": "adr", "title": "cache", "choice": "keep it"}, False)
    seen: list[Record] = []
    blocks = [
        hooks._records_block("a.py", [first], seen=seen),
        hooks._records_block("b.py", [second], seen=seen),
    ]
    omitted = hooks._PRETOOL_OMITTED.format(count=2)
    read = "\n".join([hooks.MEMORY_GUARD_LINE, *blocks, omitted])
    assert echo.placements(_delivery(read, "pre_tool_use"), KNOWN) == [A, B]
    brief = hooks._render_brief([(None, "a.py", [first]), ("plan.md", "b.py", [second])])
    assert "a.py:\n" in brief and "b.py (named in plan.md):\n" in brief
    assert echo.placements(_delivery(brief, "subagent_brief"), KNOWN) == [A, B]


def test_unknown_ulids_are_not_placements():
    stranger = "01M3T0VEAGCCCCCCCCCCCCCCCC"
    assert echo.placements(_delivery(f"- [adr] x (id: {stranger})"), KNOWN) == []
    payload = json.dumps({"decisions": [{"id": stranger}, {"id": A}]})
    assert echo.placements(_delivery(payload), KNOWN) == [A]


def test_sent_earlier_rows_are_not_placements():
    payload = json.dumps({"decisions": [{"id": A}, {"id": B, "sent_earlier": True}]})
    assert echo.placements(_delivery(payload), KNOWN) == [A]
    pointer = f"- [adr] [sent earlier] Alpha: see before (id: {A})"
    assert echo.placements(_delivery(pointer), KNOWN) == []
    drifted = f"- [adr] [drifted] [sent earlier] Alpha: see before (id: {A})"
    assert echo.placements(_delivery(drifted), KNOWN) == []
    legend = "[sent earlier] = this record was delivered earlier in this session"
    assert echo.placements(_delivery(legend), KNOWN) == []
    string_item = json.dumps({"decisions": [f"- [adr] [sent earlier] x (id: {A})"]})
    assert echo.placements(_delivery(string_item), KNOWN) == []


def test_a_multi_paragraph_record_and_the_overflow_line_do_not_hide_the_id():
    """A record's prose may hold a blank line; the overflow line and a hint follow it."""
    text = (
        f"- [adr] first: see (id: {B})\n\nsecond paragraph of the same record. (id: {A})\n"
        "- (+7 more related not shown: drill_down a domain or retrieve_decisions(query=...))\n\n"
        "If this session's work invalidates a record above, call supersede_decision with its id."
    )
    assert echo.placements(_delivery(text), KNOWN) == [A]


def test_an_id_quoted_at_a_line_end_of_another_records_prose_is_not_a_placement():
    """Only the complete record's own terminal suffix counts, not any line-ending id marker."""
    related = f"- [adr] T3: replaces the old path (id: {B})\nsee notes"
    text = f"- [adr] T1: alpha (id: {A})\n{related}"
    assert echo.placements(_delivery(text), KNOWN) == [A]
    fact = Fact(
        statement=f"see (id: {B})\ncontinuation",
        source="trace",
        status=DecisionStatus.ACCEPTED,
        valid_from=datetime.now(UTC),
        provenance=Provenance(source="manual"),
    )
    assert f"(id: {B})\ncontinuation" in _fmt_fact(fact)
    assert echo.placements(_delivery(_fmt_fact(fact)), KNOWN) == []


def test_a_markdown_heading_inside_a_direct_tier_record_does_not_cut_it():
    a = Decision(
        id=A,
        title="Use X",
        kind=DecisionKind.ADR,
        context="c",
        choice="use X",
        consequences="## Positive\n- fast\n## Negative\n- slower cold start",
        status=DecisionStatus.ACCEPTED,
        valid_from=datetime.now(UTC),
        provenance=Provenance(source="manual"),
    )
    line = _fmt_decision(a, detailed=True)
    assert "\n## Negative" in line
    assert echo.placements(_delivery(line), KNOWN) == [A]
    text = f"## Decisions\n{line}\n\n## Known facts\n- [adr] other (id: {B})"
    assert echo.placements(_delivery(text), KNOWN) == [A, B]


def _adr(**fields) -> Decision:
    return Decision(
        id=A,
        title="Alpha",
        kind=DecisionKind.ADR,
        context="c",
        status=DecisionStatus.ACCEPTED,
        valid_from=datetime.now(UTC),
        provenance=Provenance(source="manual"),
        **fields,
    )


def test_prose_that_looks_like_an_evidence_line_is_not_stripped():
    """The renderer indents its `  evidence:` line; a continuation of the prose that merely
    starts with the word is the record's own text, and the id it ends with is the record's."""
    fact = Fact(
        statement=f"see (id: {B})\nevidence: continuation",
        source="trace",
        status=DecisionStatus.ACCEPTED,
        valid_from=datetime.now(UTC),
        provenance=Provenance(source="manual"),
    )
    rendered = _fmt_fact(fact)
    assert "\nevidence: continuation" in rendered
    assert echo.placements(_delivery(rendered), KNOWN) == []
    line = _fmt_decision(_adr(choice=f"see (id: {B})\nevidence: continuation"), detailed=True)
    assert echo.placements(_delivery(line), KNOWN) == [A]


def test_a_heading_inside_the_prose_after_a_quoted_id_does_not_cut_the_record():
    """The renderer puts a blank line before every section heading of its own; a heading in the
    prose, straight after a line that quotes another record's id, is the record's."""
    line = _fmt_decision(_adr(choice=f"see (id: {B})\n## continuation\ncontinued"), detailed=True)
    assert f"(id: {B})\n## continuation\ncontinued" in line
    assert echo.placements(_delivery(line), KNOWN) == [A]
    text = f"## Decisions\n{line}\n\n## Known facts\n- [adr] other (id: {B})"
    assert echo.placements(_delivery(text), KNOWN) == [A, B]


def test_a_legend_or_footer_after_the_last_record_does_not_hide_it():
    text = (
        f"- [adr] first (id: {A})\n"
        "[drifted] = changed code; verify before relying on it\n"
        'More: get_task_context(files=["a.py"]).'
    )
    assert echo.placements(_delivery(text), KNOWN) == [A]
    footer = f'- [adr] first (id {A})\nMore: get_task_context(files=["a.py"]).'
    assert echo.placements(_delivery(footer), KNOWN) == [A]


# -- needles and the echo rule ----------------------------------------------------------------


def test_needles_use_ten_characters_and_full_ids_for_prefix_twins():
    twin = A[:10] + "ZZZZZZZZZZZZZZZZ"
    needles = echo.needles(frozenset({A, B, twin}))
    assert needles[B] == B[:10]
    assert needles[A] == A
    assert needles[twin] == twin


def test_cited_counts_only_later_authored_text():
    needle_of = echo.needles(KNOWN)
    delivery = _delivery("x", position=10)
    authored = [
        Authored(5, f"before {A}"),
        Authored(10, f"same line {A}"),
        Authored(11, f"later short form | {B[:10]} | gotcha"),
        Authored(12, f"lowercase {A[:10].lower()}"),
    ]
    assert echo.cited(delivery, [A, B], authored, needle_of) == [B]
    assert echo.cited(delivery, [A, B], authored + [Authored(13, f"full {A}")], needle_of) == [
        A,
        B,
    ]
    assert echo.cited(delivery, [], authored, needle_of) == []


def test_asked_by_id_finds_ids_in_the_call_input():
    needle_of = echo.needles(KNOWN)
    delivery = _delivery("x", call_input=json.dumps({"ids": [A]}))
    assert echo.asked_by_id(delivery, [A, B], needle_of) == [A]
    assert echo.asked_by_id(_delivery("x"), [A, B], needle_of) == []


def test_last_positions_agree_with_cited():
    needle_of = echo.needles(KNOWN)
    authored = [Authored(3, f"a {A[:10]} b"), Authored(8, f"{B[:10]}"), Authored(9, "none")]
    last = echo.last_positions(authored, set(needle_of.values()))
    assert last == {A[:10]: 3, B[:10]: 8}
