"""Exact output for the briefing's changes block (B4)."""

import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from sidegraph.host.briefing.blocks.changes import render
from sidegraph.host.briefing.context import build, read_parts
from sidegraph.host.briefing.lines import size
from sidegraph.hot_index import HotIndex
from sidegraph.store import Store
from tests.briefing_fixtures import NOW, context, decision, rich_context, rid

BUDGET = 2400
HEADING = "## Changed since the last session (2026-11-17 12:00 UTC)"
REPLACEMENT = f'- [adr] The new rule [drifted] (id {rid(6)}) replaces "The old rule" (id {rid(0)})'
FIRST = f"- [gotcha] First gotcha [drifted] — Do the safe thing. (id {rid(1)})"
SECOND = f"- [gotcha] Second gotcha — Do the safe thing. (id {rid(2)})"
CONSTRAINT = f"- [constraint] Anchored constraint — Do the safe thing. (id {rid(3)})"
ADR = f"- [adr] An ADR — Do the safe thing. (id {rid(4)})"


def test_replacements_come_before_additions_each_newest_first() -> None:
    ctx = rich_context()
    assert render(ctx, BUDGET) == [HEADING, REPLACEMENT, FIRST, SECOND, CONSTRAINT, ADR]

    newer = decision(9, "The newer rule", "adr", supersedes=rid(8), days_ago=0.25)
    ctx = replace(ctx, accepted=(newer, *ctx.accepted))
    assert render(ctx, BUDGET) == [
        HEADING,
        f"- [adr] The newer rule (id {rid(9)}) replaces an earlier record (id {rid(8)})",
        REPLACEMENT,
        FIRST,
        SECOND,
        CONSTRAINT,
        ADR,
    ]


def test_a_record_at_exactly_since_is_in_and_one_before_it_is_out() -> None:
    since = NOW - timedelta(days=3)
    at = decision(1, "At the cutoff", days_ago=3)
    before = decision(2, "Just before the cutoff", days_ago=3)
    before["valid_from"] = (since - timedelta(microseconds=1)).isoformat()
    assert render(context(accepted=(at, before), since=since), BUDGET) == [
        HEADING,
        f"- [gotcha] At the cutoff — Do the safe thing. (id {rid(1)})",
    ]


def test_an_empty_predecessor_id_renders_as_an_addition() -> None:
    row = decision(2, "Second gotcha", supersedes="")
    ctx = context(accepted=(row,), since=NOW - timedelta(days=3))
    assert render(ctx, BUDGET) == [HEADING, SECOND]


def test_a_replacement_line_carries_both_ids_and_the_old_title() -> None:
    ctx = rich_context()
    assert render(ctx, BUDGET)[1:2] == [REPLACEMENT]


def test_a_missing_predecessor_title() -> None:
    ctx = replace(rich_context(), replaced_titles={})
    assert render(ctx, BUDGET)[1:2] == [
        f"- [adr] The new rule [drifted] (id {rid(6)}) replaces an earlier record (id {rid(0)})"
    ]


def test_an_unencodable_predecessor_title_is_treated_as_missing(tmp_path: Path) -> None:
    Store(tmp_path).close()
    predecessor = decision(0, "Damaged\ud800 title", "adr", days_ago=4)
    predecessor["status"] = "superseded"
    replacement = decision(6, "The new rule", "adr", supersedes=rid(0))
    healthy = decision(2, "Second gotcha")
    with closing(sqlite3.connect(tmp_path / "index.db")) as conn, conn:
        conn.executemany(
            "INSERT INTO decisions (id, status, supersedes, data) VALUES (?, ?, ?, ?)",
            (
                (row["id"], row["status"], row["supersedes"], json.dumps(row))
                for row in (predecessor, replacement, healthy)
            ),
        )
    index = HotIndex.open(tmp_path)
    assert index is not None
    with closing(index), index.snapshot():
        parts = read_parts(index, frozenset({"decisions", "window"}), NOW)
    ctx = build(parts, NOW, NOW - timedelta(days=3), ())
    assert render(ctx, BUDGET) == [
        HEADING,
        f"- [adr] The new rule (id {rid(6)}) replaces an earlier record (id {rid(0)})",
        SECOND,
    ]


def test_replacement_titles_are_collapsed_and_clipped_to_their_own_limits() -> None:
    new_word = "n" * 79
    old_word = "o" * 49
    replacement = decision(6, f"New\nrule\t  {new_word} next", "adr", supersedes=rid(0))
    ctx = context(
        accepted=(replacement,),
        since=NOW - timedelta(days=3),
        replaced_titles={rid(0): f"Old\nrule\t  {old_word} end"},
    )
    assert render(ctx, BUDGET) == [
        HEADING,
        f'- [adr] New rule {new_word}… (id {rid(6)}) replaces "Old rule {old_word}…" (id {rid(0)})',
    ]


@pytest.mark.parametrize("stored_id", ["old", " legacy/記録 ", "x" * 128])
def test_a_single_line_predecessor_id_is_cited_as_stored(stored_id: str) -> None:
    row = decision(1, "New rule", supersedes=stored_id)
    ctx = context(
        accepted=(row,), since=NOW - timedelta(days=3), replaced_titles={stored_id: "Old rule"}
    )
    assert render(ctx, BUDGET) == [
        HEADING,
        f'- [gotcha] New rule (id {rid(1)}) replaces "Old rule" (id {stored_id})',
    ]


@pytest.mark.parametrize(
    "bad_id",
    [[], "bad\nid", "bad\u2028id", "bad\r", "x" * 129, "x" * 3000],
    ids=["array", "newline", "line-separator", "trailing-break", "over-limit", "over-budget"],
)
def test_an_uncitable_predecessor_keeps_the_replacement_visible(bad_id: object) -> None:
    malformed = decision(1, "New rule", days_ago=1)
    malformed["supersedes"] = bad_id
    healthy = decision(2, "Second gotcha", days_ago=2)
    ctx = context(accepted=(malformed, healthy), since=NOW - timedelta(days=3))
    assert render(ctx, BUDGET) == [
        HEADING,
        f"- [gotcha] New rule (id {rid(1)}) replaces an earlier record",
        SECOND,
    ]


def test_rejected_is_never_shown() -> None:
    lines = render(rich_context(), BUDGET)
    assert lines == [HEADING, REPLACEMENT, FIRST, SECOND, CONSTRAINT, ADR]
    assert all("The unsafe thing" not in line for line in lines)


def test_the_heading_follows_the_fallback_flag() -> None:
    ctx = rich_context()
    assert render(ctx, BUDGET)[:1] == [HEADING]
    assert render(replace(ctx, since_is_fallback=True), BUDGET)[:1] == [
        "## Changed in the 14 days before the newest record (since 2026-11-17)"
    ]


def test_the_session_heading_formats_the_cutoff_in_utc() -> None:
    since = datetime.fromisoformat("2026-11-17T12:00:00+05:30")
    ctx = replace(rich_context(), since=since)
    assert render(ctx, BUDGET)[:1] == ["## Changed since the last session (2026-11-17 06:30 UTC)"]


def test_the_lines_do_not_depend_on_now() -> None:
    ctx = replace(rich_context(), since_is_fallback=True)
    lines = render(ctx, BUDGET)
    assert lines == [
        "## Changed in the 14 days before the newest record (since 2026-11-17)",
        REPLACEMENT,
        FIRST,
        SECOND,
        CONSTRAINT,
        ADR,
    ]
    assert render(replace(ctx, now=NOW + timedelta(days=365)), BUDGET) == lines


def test_a_cut_list_ends_with_its_pointer() -> None:
    pointer = "3 more: retrieve_decisions() lists the newest records."
    expected = [HEADING, REPLACEMENT, FIRST, pointer]
    assert render(rich_context(), size(expected)) == expected


def test_an_empty_window_gives_nothing() -> None:
    ctx = rich_context()
    assert render(replace(ctx, since=NOW), BUDGET) == []
    assert render(replace(ctx, since=None), BUDGET) == []
