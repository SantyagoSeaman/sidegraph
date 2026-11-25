"""The open threads SessionStart briefing block."""

from dataclasses import replace
from datetime import timedelta

from sidegraph.host.briefing.blocks.threads import render
from sidegraph.host.briefing.lines import size
from tests.briefing_fixtures import NOW, context, decision, fact, record, rich_context, rid

HEADING = "## Open threads"
DRIFT_HEADING = "Drifted records in the files you are working on:"
FIRST_DRIFTED = f"- [gotcha] First gotcha [drifted] (id {rid(1)})"
RECENT_IDS = ", ".join(rid(n) for n in (1, 2, 3, 4, 6))
RECORDED = f"Recorded since the last session: 5 accepted records; the newest: {RECENT_IDS}."
QUEUE = "Awaiting ratification: 4 proposed records; list_proposed lists them."


def test_the_three_parts_in_order() -> None:
    assert render(rich_context(), 1800) == [HEADING, DRIFT_HEADING, FIRST_DRIFTED, RECORDED, QUEUE]


def test_drifted_lists_only_working_set_records() -> None:
    ctx = replace(rich_context(), working_set=("src/b.py", "src/c.py"))
    assert render(ctx, 1800) == [
        HEADING,
        DRIFT_HEADING,
        f"- [adr] The new rule [drifted] (id {rid(6)})",
        RECORDED,
        QUEUE,
    ]


def test_at_most_five_drifted_with_a_pointer_naming_their_files() -> None:
    start, middle, end = "src/z.py", 'src/λ"\\b.py', "src/a.py"
    rows = [record(decision(n, f"Title {n}")) for n in range(1, 8)]
    ctx = context(
        working_set=(start, middle, end),
        by_file={
            end: (rows[6],),
            middle: tuple(rows[2:6]),
            start: (*rows[:3], record(fact(8, "A measured fact."), "fact")),
        },
        drifted=frozenset(rid(n) for n in range(1, 9)),
    )
    assert render(ctx, 1800) == [
        HEADING,
        DRIFT_HEADING,
        *(f"- [gotcha] Title {n} [drifted] (id {rid(n)})" for n in range(1, 6)),
        r'2 more: get_task_context(files=["src/λ\"\\b.py", "src/a.py"]) tags them.',
    ]


def test_the_pointer_lists_at_most_three_distinct_omitted_files() -> None:
    rows = tuple(record(decision(n, f"Title {n}")) for n in range(1, 11))
    ctx = context(
        working_set=("src/start.py", "src/z.py", "src/b.py", "src/a.py", "src/c.py"),
        by_file={
            "src/start.py": rows[:5],
            "src/z.py": rows[5:7],
            "src/b.py": (rows[7],),
            "src/a.py": (rows[8],),
            "src/c.py": (rows[9],),
        },
        drifted=frozenset(rid(n) for n in range(1, 11)),
    )
    assert render(ctx, 1800) == [
        HEADING,
        DRIFT_HEADING,
        *(f"- [gotcha] Title {n} [drifted] (id {rid(n)})" for n in range(1, 6)),
        '5 more: get_task_context(files=["src/z.py", "src/b.py", "src/a.py"]) tags them.',
    ]


def test_the_recorded_line_counts_accepted_records_in_the_window() -> None:
    ctx = context(
        accepted=tuple(decision(n, f"Title {n}", days_ago=n) for n in range(1, 8)),
        since=NOW - timedelta(days=6),
    )
    newest = ", ".join(rid(n) for n in range(1, 6))
    assert render(ctx, 1800) == [
        HEADING,
        f"Recorded since the last session: 6 accepted records; the newest: {newest}.",
    ]
    short = [
        HEADING,
        f"Recorded since the last session: 6 accepted records; the newest: {rid(1)}.",
    ]
    assert render(ctx, size(short)) == short


def test_the_recorded_line_changes_its_opening_under_the_fallback() -> None:
    ctx = replace(rich_context(), since_is_fallback=True)
    assert render(ctx, 1800) == [
        HEADING,
        DRIFT_HEADING,
        FIRST_DRIFTED,
        f"Recorded in the 14 days before the newest record: 5 accepted records; "
        f"the newest: {RECENT_IDS}.",
        QUEUE,
    ]


def test_a_changed_queue_count_changes_one_line() -> None:
    ctx = rich_context()
    assert render(ctx, 1800) == [HEADING, DRIFT_HEADING, FIRST_DRIFTED, RECORDED, QUEUE]
    assert render(replace(ctx, queue_count=9), 1800) == [
        HEADING,
        DRIFT_HEADING,
        FIRST_DRIFTED,
        RECORDED,
        "Awaiting ratification: 9 proposed records; list_proposed lists them.",
    ]


def test_a_zero_queue_leaves_the_line_out() -> None:
    ctx = replace(rich_context(), queue_count=0)
    assert render(ctx, 1800) == [HEADING, DRIFT_HEADING, FIRST_DRIFTED, RECORDED]


def test_an_empty_working_set_keeps_the_other_two_parts() -> None:
    ctx = replace(rich_context(), working_set=())
    assert render(ctx, 1800) == [HEADING, RECORDED, QUEUE]


def test_a_budget_for_the_queue_line_only() -> None:
    expected = [HEADING, QUEUE]
    assert render(rich_context(), size(expected)) == expected


def test_it_never_repeats_the_status_lines() -> None:
    assert render(context(queue_count=4), 1800) == [HEADING, QUEUE]


def test_nothing_open_gives_nothing() -> None:
    assert render(context(), 1800) == []
