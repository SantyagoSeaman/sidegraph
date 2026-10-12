"""Fixes from the field-stats batch panel review: the hard cap's trailer, the telemetry of what
the cap cut, drill_down's whole envelope, normalised worktree seeds in telemetry, and doctor on
an index without a ``meta`` table.
see design/superpowers/specs/2026-10-12-batch-panel-fixes.md"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import sidegraph.server as server
from sidegraph.cli import doctor_main
from sidegraph.retrieval import _fmt_decision
from sidegraph.schema import Decision, DecisionKind, DecisionStatus, Provenance
from sidegraph.store import Store

_HARD = 40000
_WORKTREE_SENTENCE = (
    "1 of 1 seed path exists only in another checkout of this repository, not in the one this "
    "server reads (/repo): pkg/new.py (worktree .worktrees/feat, branch feat). The code graph "
    "this server reads (built from /repo) does not hold it."
)
_NOT_IN_GRAPH = "## Not in the code graph\n" + _WORKTREE_SENTENCE


# -- G1: the trailer survives the hard cap -----------------------------------------------


def _trailer() -> list[str]:
    return [_NOT_IN_GRAPH, server._STANDING_SUPERSEDE_HINT]


def _assert_advice_kept(out: str) -> None:
    assert len(out) <= _HARD
    assert out.endswith(server._STANDING_SUPERSEDE_HINT)
    assert "only in another checkout" in out
    assert "[output cut at 40000 characters" in out


def test_g1_an_h2_inside_a_record_does_not_hide_the_section_from_the_cut():
    decision = Decision(
        title="a record",
        kind=DecisionKind.ADR,
        status=DecisionStatus.ACCEPTED,
        context="see below\n## Notes\nthe cost is real",
        choice="ch",
        valid_from=datetime(2026, 10, 1, tzinfo=UTC),
        provenance=Provenance(source="manual"),
    )
    line = _fmt_decision(decision, detailed=True)
    assert "\n## Notes\n" in line  # precondition: the record really emits an H2 line
    body = (
        "guard\n\n## Decisions\n"
        + line
        + "\n"
        + "\n".join(f"- [adr] r{i} " + "x" * 90 for i in range(450))
    )
    out = server._hard_cap(body, _trailer(), "how")
    _assert_advice_kept(out)
    # the record lines after the H2 are still the Decisions section: the only one cut
    assert out.split("from: ")[1].split(". ")[0] == "decisions"


def test_g1_a_trailer_line_longer_than_the_cap_is_cut_from_its_front_and_says_so():
    huge = "## Not in the code graph\n1 of 1 seed path is not a file: " + "p" * 50000
    out = server._hard_cap("guard\n\n## Decisions\n- short", [huge, *_trailer()], "how")
    assert len(out) <= _HARD
    assert out.endswith(server._STANDING_SUPERSEDE_HINT)
    assert "only in another checkout" in out
    assert "[output cut at 40000 characters" in out
    assert "start of the advice" in out.split("[output cut")[1]


def test_g1_an_absurd_seed_path_is_clipped_in_the_not_in_graph_block():
    shown = server._listed(["a/" + "p" * 50000 + "/file.py", "short.py"])
    assert len(shown) < 500 and shown.endswith("short.py")
    assert "…" in shown and "/file.py" in shown


def test_g1_an_absurd_seed_is_clipped_in_the_read_block():
    from sidegraph import seed_ladder

    note = seed_ladder.SeedNote(
        given="./" + "q" * 50000 + "/x.py", outcome="normalised", read_as=("x.py",)
    )
    block = seed_ladder.read_block([note])
    assert len(block) < 600 and "/x.py" in block


# -- G2: doctor on an index without a meta table -----------------------------------------


def test_g2_doctor_survives_an_index_with_bindings_and_no_meta_table(tmp_path, capsys):
    db = tmp_path / "store"
    Store(db).close()
    conn = sqlite3.connect(db / "index.db")
    conn.execute("DROP TABLE meta")
    conn.commit()
    conn.close()
    assert doctor_main(["--db", str(db), "--json"]) in (0, 2)  # a report, not a traceback
    report = json.loads(capsys.readouterr().out)
    assert "findings" in report


def test_g2_a_missing_meta_table_counts_as_computed_and_a_sqlite_error_is_a_skip(
    tmp_path, capsys, monkeypatch
):
    from sidegraph import cli, integrity

    db = tmp_path / "store"
    Store(db).close()
    conn = sqlite3.connect(db / "index.db")
    conn.execute("DROP TABLE meta")
    assert integrity.binding_statuses_computed(conn) is True
    conn.close()

    def boom(_index):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(cli, "binding_statuses_computed", boom)
    assert doctor_main(["--db", str(db), "--json"]) in (0, 2)
    assert "orphaned-records" in json.loads(capsys.readouterr().out)["skipped"]


# -- C1: drill_down bounds its whole envelope --------------------------------------------


def _store(tmp_path: Path, monkeypatch) -> Store:
    store = Store(tmp_path / "t.db")
    monkeypatch.setattr(server, "_store", store)
    return store


def _domain(store: Store, slug: str, summary: str = "why.", parent: str | None = None):
    from sidegraph.schema import Domain, DomainStatus

    domain = Domain(
        slug=slug,
        title=slug.title(),
        summary=summary,
        parent_id=parent,
        status=DomainStatus.ACCEPTED,
        provenance=Provenance(source="manual"),
    )
    store.add_domain(domain)
    return domain


@pytest.mark.parametrize("budget", [4000, 24000])
def test_c1_a_huge_domain_summary_is_clipped_to_the_budget(tmp_path, monkeypatch, budget):
    store = _store(tmp_path, monkeypatch)
    _domain(store, "capture", summary="s" * 60000)
    out = server._drill_down_impl(store, None, "capture", limit=25, budget_chars=budget)
    assert len(server._dump(out)) <= budget
    assert out["domain"]["summary"].startswith("sss") and "clipped" in out["domain"]["summary"]
    assert "summary" in out["hint"] and "clipped" in out["hint"]
    store.close()


def test_c1_many_subdomains_are_capped_with_a_count_of_the_omitted(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    parent = _domain(store, "capture")
    for i in range(350):
        _domain(store, f"sub-{i:03d}", summary="b" * 150, parent=parent.domain_id)
    out = server._drill_down_impl(store, None, "capture", limit=25, budget_chars=24000)
    assert len(server._dump(out)) <= 24000
    kept = len(out["subdomains"])
    assert 0 < kept < 350 and out["subdomains_omitted"] == 350 - kept
    assert f"{350 - kept} of 350 subdomains" in out["hint"]
    # a domain well under the bound is untouched
    small = server._drill_down_impl(store, None, "sub-000", limit=25, budget_chars=24000)
    assert "hint" not in small and "subdomains_omitted" not in small
    store.close()


def test_c1_the_bounded_fixed_part_still_leaves_room_for_the_decisions_cut(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    _domain(store, "capture", summary="s" * 60000)
    out = server._drill_down_impl(store, None, "capture", limit=25, budget_chars=24000)
    assert out["found"] is True and out["decisions"] == []
    assert "decisions_omitted" not in out  # none exist; only the fixed part was cut
    store.close()


def test_c1_long_member_lines_and_titles_are_clipped_and_decisions_still_fit():
    result = {
        "found": True,
        "domain": {"slug": "d", "title": "T" * 9000, "summary": "why.", "status": "accepted"},
        "subdomains": [],
        "members": [f"- node{i} " + "m" * 5000 for i in range(20)],
        "decisions": [f"- [adr] decision {i}" for i in range(5)],
    }
    ids = [f"id{i}" for i in range(5)]
    kept = server._cut_drill_down(result, ids, 25, 4000)
    assert len(server._dump(result)) <= 4000
    assert kept == ids and len(result["decisions"]) == 5  # all five fit beside the bounded part
    assert result["members_omitted"] > 0 and "members are not listed" in result["hint"]
    assert "clipped" in result["domain"]["title"]


# -- C2: telemetry counts what the hard cap left in the text ------------------------------

_FIXTURE = Path(__file__).parent / "fixtures" / "mini_graph.json"
_ID_IN_TEXT = re.compile(r"\(id: ([0-9A-Z]{26})\)")


def _mistakes(tmp_path: Path, monkeypatch, count: int = 100, size: int = 340):
    from sidegraph.engine.reader import GraphifyReader
    from sidegraph.schema import AnchorBinding, Descriptor, Entity

    reader = GraphifyReader(_FIXTURE)
    store = Store(tmp_path / "t.db")
    monkeypatch.setattr(server, "_session_key", lambda _s: "sess-1")
    entity = store.upsert_entity(
        Entity(
            canonical_name="Trader",
            descriptor=Descriptor(name="Trader", file_path="trader/exec.py"),
        )
    )
    ids = []
    for i in range(count):
        decision = Decision(
            title=f"gotcha {i:03d}",
            kind=DecisionKind.GOTCHA,
            status=DecisionStatus.ACCEPTED,
            context="c " + "y" * (size + i % 7),
            choice="ch " + "z" * size,
            valid_from=datetime(2026, 10, 1, tzinfo=UTC) - timedelta(minutes=i),
            provenance=Provenance(source="manual"),
        )
        store._write_decision(decision)
        store._conn.commit()
        store.add_binding(
            AnchorBinding(record_id=decision.id, entity_id=entity.entity_id, tier=2, status="live")
        )
        ids.append(decision.id)
    return store, reader, ids


def _assert_telemetry_matches(store: Store, text: str, ids: list[str]) -> None:
    in_text = set(_ID_IN_TEXT.findall(text))
    assert "[output cut at 40000 characters" in text
    assert in_text, "precondition: records reached the text"
    assert set(store.retrieval_shows()) == in_text
    shown = {r["detail"] for r in store.retrieval_events() if r["kind"] == "show_anchor"}
    assert shown == in_text
    [event] = store.render_events()
    assert event["emitted"] == len(in_text)
    assert event["selected"] == event["emitted"] + event["dropped_for_budget"]
    # a degraded record is a tight line that kept its id; the ones the cap cut are not counted
    tight = [ln for ln in text.splitlines() if "(id: " in ln and "(context:" not in ln]
    assert event["degraded"] == len(tight)
    assert event["chars_used"] <= len(text)  # the figure describes the delivered text


def test_c2_query_decisions_counts_only_the_records_in_the_wire_text(tmp_path, monkeypatch):
    store, reader, ids = _mistakes(tmp_path, monkeypatch)
    text = server._query_decisions_impl(store, reader, ["trader/exec.py"], None, 40000)
    _assert_telemetry_matches(store, text, ids)
    store.close()


def test_c2_get_task_context_counts_only_the_records_in_the_wire_text(tmp_path, monkeypatch):
    store, reader, ids = _mistakes(tmp_path, monkeypatch)
    text = server._get_task_context_impl(store, reader, ["trader/exec.py"], None, 4000, 40000)
    _assert_telemetry_matches(store, text, ids)
    store.close()


def test_c2_facts_the_cap_cut_are_not_counted_as_shown(tmp_path, monkeypatch):
    from sidegraph.schema import AnchorBinding, Fact

    store, reader, ids = _mistakes(tmp_path, monkeypatch, count=59, size=300)
    entity = store.find_abstract_entity("Trader") or next(store.iter_concrete_entities())
    fact_ids = []
    for i in range(22):
        fact = Fact(
            statement=f"fact {i:02d} " + "q" * 480,
            source=f"bench {i}",
            status=DecisionStatus.ACCEPTED,
            valid_from=datetime(2026, 10, 1, tzinfo=UTC) - timedelta(minutes=i),
            provenance=Provenance(source="manual"),
        )
        store.add_fact(fact)
        store.add_binding(
            AnchorBinding(record_id=fact.id, entity_id=entity.entity_id, tier=2, status="live")
        )
        fact_ids.append(fact.id)
    text = server._query_decisions_impl(store, reader, ["trader/exec.py"], None, 40000)
    assert "[output cut at 40000 characters" in text
    assert "known facts" in text.split("from: ")[1].split(". ")[0]  # precondition
    in_text = {f"fact {i:02d}" for i in range(22) if f"fact {i:02d} " in text}
    shown = {store.get_fact(i).statement[:7] for i in fact_ids if i in store.retrieval_shows()}
    assert shown == in_text and len(in_text) < 22
    # the ranker placed facts that the cap then cut (they are in the render before the cut)
    placed = server._retrieve(
        [server.Seed(file_path="trader/exec.py")],
        store,
        reader,
        server.RetrievalBudget(memory_chars=40000),
    )
    assert set(fact_ids) & set(placed.shown_ids)


# -- round 2 -----------------------------------------------------------------------------


def _multiline_mistakes(tmp_path, monkeypatch, count: int = 40):
    """Gotchas whose detailed line has the id on a second physical line."""
    from sidegraph.engine.reader import GraphifyReader
    from sidegraph.schema import AnchorBinding, Descriptor, Entity

    reader = GraphifyReader(_FIXTURE)
    store = Store(tmp_path / "t.db")
    monkeypatch.setattr(server, "_session_key", lambda _s: "sess-1")
    entity = store.upsert_entity(
        Entity(
            canonical_name="Trader",
            descriptor=Descriptor(name="Trader", file_path="trader/exec.py"),
        )
    )
    for i in range(count):
        decision = Decision(
            title=f"gotcha {i:03d}",
            kind=DecisionKind.GOTCHA,
            status=DecisionStatus.ACCEPTED,
            context="see below\n" + "y" * 1170,
            choice="ch",
            valid_from=datetime(2026, 10, 1, tzinfo=UTC) - timedelta(minutes=i),
            provenance=Provenance(source="manual"),
        )
        store._write_decision(decision)
        store._conn.commit()
        store.add_binding(
            AnchorBinding(record_id=decision.id, entity_id=entity.entity_id, tier=2, status="live")
        )
    return store, reader


@pytest.mark.parametrize("tool", ["query_decisions", "get_task_context"])
def test_r2_1_a_record_is_cut_whole_never_without_its_id_line(tmp_path, monkeypatch, tool):
    store, reader = _multiline_mistakes(tmp_path, monkeypatch)
    if tool == "query_decisions":
        text = server._query_decisions_impl(store, reader, ["trader/exec.py"], None, 40000)
    else:
        text = server._get_task_context_impl(store, reader, ["trader/exec.py"], None, 4000, 40000)
    assert "[output cut at 40000 characters" in text
    in_text = set(_ID_IN_TEXT.findall(text))
    # a record's first line is never left without its id line
    assert text.count("(context: see below") == len(in_text)
    assert set(store.retrieval_shows()) == in_text
    [event] = store.render_events()
    assert event["emitted"] == len(in_text)
    store.close()


def test_r2_1_the_cap_keeps_a_multi_line_record_atomic():
    first = "- [gotcha] a: ch (context: see below"
    second = "yyyy) (id: " + "A" * 26 + ")"
    record = first + "\n" + second
    body = "guard\n\n## ⚠ Known mistakes & gotchas\n" + "\n".join([record] * 30)
    report = server._CapReport()
    cap = len(body) - len(second) - 5  # only the last id line would have to go
    out = server._hard_cap(body, ["advice"], "how", cap=cap, atoms=[record], report=report)
    assert out.count(first) == out.count(second)
    assert report.cut and report.kept.count(record) == out.count(first)


@pytest.mark.parametrize("budget", [4000, 24000])
@pytest.mark.parametrize(
    "summary",
    ["\x01" * 60000, '"\\' * 30000, "é\n\t" * 20000],
    ids=["control", "quotes-backslashes", "newlines"],
)
def test_r2_3_the_serialized_answer_fits_whatever_the_summary_escapes_to(
    tmp_path, monkeypatch, budget, summary
):
    store = _store(tmp_path, monkeypatch)
    _domain(store, "capture", summary=summary)
    out = server._drill_down_impl(store, None, "capture", limit=25, budget_chars=budget)
    assert len(server._dump(out)) <= budget
    assert "clipped" in out["domain"]["summary"]
    store.close()


@pytest.mark.parametrize("budget", [4000, 24000])
def test_r2_3_a_huge_slug_is_clipped_with_a_marker_and_the_hint_says_so(
    tmp_path, monkeypatch, budget
):
    store = _store(tmp_path, monkeypatch)
    slug = "s" * 60000
    _domain(store, slug)
    out = server._drill_down_impl(store, None, slug, limit=25, budget_chars=budget)
    assert len(server._dump(out)) <= budget
    assert "clipped" in out["domain"]["slug"] and "domain was shown truncated" in out["hint"]
    store.close()


def test_r2_3_the_final_check_drops_the_summary_when_nothing_else_helps():
    result = {
        "found": True,
        "domain": {"slug": "d", "title": "t", "summary": "x" * 3900, "status": "accepted"},
        "subdomains": [],
        "members": [],
        "decisions": [],
    }
    # a budget the clipped summary cannot meet beside the rest of the answer
    server._ensure_fits(result, 600)
    assert len(server._dump(result)) <= 600
    assert result["domain"]["summary"] == "" and "summary was omitted" in result["hint"]


def test_r2_4_trimming_many_subdomains_is_linear_and_keeps_the_front(tmp_path, monkeypatch):
    n = 6000
    result = {
        "found": True,
        "domain": {"slug": "capture", "title": "T", "summary": "why.", "status": "accepted"},
        "subdomains": [
            {"slug": f"sub-{i:05d}", "title": "Title", "summary": "s" * 150} for i in range(n)
        ],
        "members": [],
        "decisions": [],
    }
    calls = 0
    volume = 0
    real = server._dump
    full = len(real(result["subdomains"]))

    def counting(value):
        nonlocal calls, volume
        calls += 1
        out = real(value)
        volume += len(out)
        return out

    monkeypatch.setattr(server, "_dump", counting)
    server._cut_drill_down(result, [], 25, 24000)
    # a few measures per item and none per removal (that would be about n * n)
    assert calls < 10 * n
    # and each item is serialized a bounded number of times in total: re-dumping the
    # remaining list after every removal stays under the call bound but not this one
    assert volume < 5 * full
    kept = [s["slug"] for s in result["subdomains"]]
    assert kept == [f"sub-{i:05d}" for i in range(len(kept))] and 0 < len(kept) < n
    assert result["subdomains_omitted"] == n - len(kept)
    assert "list_domains" in result["hint"]
    assert len(real(result)) <= 24000


def test_r2_7_the_clip_marker_counts_what_was_left_out(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    _domain(store, "capture", summary="s" * 60000)
    out = server._drill_down_impl(store, None, "capture", limit=25, budget_chars=24000)
    summary = out["domain"]["summary"]
    kept, tail = summary.split("…", 1)
    assert tail == f" [clipped: {60000 - len(kept)} more characters]"
    # the summary gets the room there is: more than a third of half the budget
    assert len(kept) > 24000 // 2 // 3
    store.close()


def test_r2_7_text_under_no_known_heading_is_cut_last_and_named():
    body = "guard\n\n## Notes\n" + "\n".join(f"line {i} " + "x" * 90 for i in range(450))
    out = server._hard_cap(body, ["advice"], "how")
    assert len(out) <= _HARD and out.endswith("advice")
    assert out.split("from: ")[1].split(". ")[0] == "text under no known heading"


# -- round 3 -----------------------------------------------------------------------------


def test_r3_2_the_summary_hint_counts_from_the_kept_length_not_the_first_ellipsis(
    tmp_path, monkeypatch
):
    store = _store(tmp_path, monkeypatch)
    summary = "a… " * 5 + "s" * 60000
    _domain(store, "capture", summary=summary)
    out = server._drill_down_impl(store, None, "capture", limit=25, budget_chars=24000)
    text = out["domain"]["summary"]
    left_out = int(re.search(r"\[clipped: (\d+) more", text).group(1))
    assert f"{left_out} of {len(summary)} characters not shown" in out["hint"]
    store.close()


def test_r3_4_blank_runs_inside_a_record_collapse_as_before_when_nothing_is_cut():
    record = "- [adr] a: ch (context: one\n\n\n\ntwo) (id: " + "A" * 26 + ")"
    body = "guard\n\n## Decisions\n" + record
    plain = server._hard_cap(body, ["advice"], "how")
    atomic = server._hard_cap(body, ["advice"], "how", atoms=[record])
    assert plain == atomic and "one\n\ntwo" in atomic


def test_r3_3_half_the_budget_stays_for_the_decisions(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    domain = _domain(store, "capture", summary="s" * 60000)
    from sidegraph.schema import AnchorBinding

    entity = store.get_or_create_abstract_entity("domain:capture")
    for i in range(30):
        d = Decision(
            title=f"d{i}",
            kind=DecisionKind.GOTCHA,
            status=DecisionStatus.ACCEPTED,
            context="c",
            choice="x" * 150,
            valid_from=datetime(2026, 10, 1, tzinfo=UTC) - timedelta(minutes=i),
            provenance=Provenance(source="manual"),
        )
        store.add_decision(d)
        store.add_binding(
            AnchorBinding(record_id=d.id, entity_id=entity.entity_id, tier=1, status="live")
        )
    for budget, at_least in ((4000, 2), (24000, 20)):
        out = server._drill_down_impl(store, None, "capture", limit=25, budget_chars=budget)
        assert len(server._dump(out)) <= budget
        assert len(out["decisions"]) >= at_least, (budget, len(out["decisions"]))
    assert domain.slug == "capture"
    store.close()


def test_r3_3_fit_prefix_is_exact():
    assert server._fit_prefix([3, 3, 3], 9) == 3
    assert server._fit_prefix([3, 3, 3], 8) == 2
    assert server._fit_prefix([3, 3, 3], 2) == 0
    assert server._fit_prefix([], 5) == 0


def test_r3_3_the_cut_note_counts_the_physical_lines_of_a_multi_line_record():
    record = "- [gotcha] a: ch (context: see below\n" + "y" * 50 + ") (id: " + "A" * 26 + ")"
    body = "guard\n\n## ⚠ Known mistakes & gotchas\n" + "\n".join([record] * 40)
    out = server._hard_cap(body, ["advice"], "how", cap=len(body) // 2, atoms=[record])
    lost = int(re.search(r"\((\d+) lines\)", out).group(1))
    assert lost == 2 * (40 - out.count(record)) > 0  # whole two-line records, counted by line


def test_r3_3_a_record_next_to_other_text_on_its_line_is_not_an_atom():
    record = "x\ny"
    units = server._units("a x\ny b\nx\ny\nend", [record])
    assert units == ["a x", "y b", "x\ny", "end"]


def test_r3_3_the_final_check_keeps_a_margin():
    result = {
        "found": True,
        "domain": {"slug": "d", "title": "t", "summary": "x" * 100, "status": "a"},
        "subdomains": [],
        "members": ["m" * 50],
        "decisions": [],
    }
    size = len(server._dump(result))
    server._ensure_fits(result, size)  # fits exactly: untouched
    assert result["domain"]["summary"] == "x" * 100 and "hint" not in result
    # one over: the summary goes, and the 200-character margin takes the members with it
    server._ensure_fits(result, size - 1)
    assert result["domain"]["summary"] == "" and result["members"] == []
    assert "summary was omitted" in result["hint"]


def test_r3_3_the_bounded_fixed_part_leaves_room_for_the_counts_and_the_hint():
    result = {
        "found": True,
        "domain": {"slug": "d", "title": "T", "summary": "why.", "status": "accepted"},
        "subdomains": [
            {"slug": f"sub-{i:04d}", "title": "Title", "summary": "s" * 150} for i in range(200)
        ],
        "members": [],
        "decisions": [],
    }
    server._cut_drill_down(result, [], 25, 4000)
    assert "hint" in result and result["subdomains_omitted"] > 0
    assert len(server._dump(result)) <= 4000 // 2  # counts and hint included


def test_r3_3_combine_merges_the_degraded_ids_of_the_nearest_records():
    from sidegraph import nearest_anchored
    from sidegraph.retrieval import TaskContext

    first = TaskContext(shown_ids=["a"], degraded=1, degraded_ids=["a"], emitted=1)
    second = TaskContext(shown_ids=["b"], degraded=1, degraded_ids=["b"], emitted=1)
    merged = nearest_anchored.combine(first, second)
    assert merged.degraded_ids == ["a", "b"] and merged.degraded == 2


def test_r4_2_chars_used_describes_the_text_when_blank_runs_collapse(tmp_path, monkeypatch):
    from sidegraph.engine.reader import GraphifyReader
    from sidegraph.schema import AnchorBinding, Descriptor, Entity

    reader = GraphifyReader(_FIXTURE)
    store = Store(tmp_path / "t.db")
    monkeypatch.setattr(server, "_session_key", lambda _s: "sess-1")
    entity = store.upsert_entity(
        Entity(
            canonical_name="Trader",
            descriptor=Descriptor(name="Trader", file_path="trader/exec.py"),
        )
    )
    for i in range(40):
        decision = Decision(
            title=f"gotcha {i:03d}",
            kind=DecisionKind.GOTCHA,
            status=DecisionStatus.ACCEPTED,
            context="see below\n" + "\n" * 1100 + "end",
            choice="ch",
            valid_from=datetime(2026, 10, 1, tzinfo=UTC) - timedelta(minutes=i),
            provenance=Provenance(source="manual"),
        )
        store._write_decision(decision)
        store._conn.commit()
        store.add_binding(
            AnchorBinding(record_id=decision.id, entity_id=entity.entity_id, tier=2, status="live")
        )
    text = server._query_decisions_impl(store, reader, ["trader/exec.py"], None, 40000)
    assert "[output cut" not in text and len(text) < 20000  # precondition: collapsed, no cut
    [event] = store.render_events()
    assert event["chars_used"] <= len(text)
    store.close()
