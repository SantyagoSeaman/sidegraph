"""Exact output for the tried-and-abandoned briefing block."""

from sidegraph.host.briefing.blocks.abandoned import render
from sidegraph.host.briefing.lines import size
from tests.briefing_fixtures import context, decision, fact, record, rich_context, rid

WORKING_SET_HEADING = "## Tried and abandoned in the files you are working on"
FALLBACK_HEADING = "## Tried and abandoned in the most-recorded files"
BUDGET = 2000


def test_with_a_working_set_the_lines_come_from_its_files_only() -> None:
    assert render(rich_context(), BUDGET) == [
        WORKING_SET_HEADING,
        f"- Abandoned: The unsafe thing: it broke. — [gotcha] First gotcha [drifted] (id {rid(1)})",
        f"- Abandoned: The unsafe thing: it broke. — [gotcha] Second gotcha (id {rid(2)})",
        "- Abandoned: The unsafe thing: it broke."
        f" — [constraint] Anchored constraint (id {rid(3)})",
        f"- Abandoned: The unsafe thing: it broke. — [lesson] A lesson (id {rid(5)})",
    ]


def test_without_one_the_three_most_recorded_files_are_used() -> None:
    ctx = context(
        by_file={
            "src/c.py": (record(decision(1, "Tie excluded")),),
            "src/b.py": (record(decision(2, "Tie included")),),
            "src/a.py": tuple(record(fact(n, "A measured fact."), "fact") for n in range(10, 14)),
            "src/d.py": (
                record(decision(3, "Two decisions")),
                record(decision(4, "No rejected approach", rejected=None)),
            ),
            "src/e.py": (
                record(decision(5, "Three decisions")),
                record(decision(6, "Second decision")),
                record(decision(7, "Third decision")),
            ),
        },
    )
    assert render(ctx, BUDGET) == [
        FALLBACK_HEADING,
        f"- Abandoned: The unsafe thing: it broke. — [gotcha] Three decisions (id {rid(5)})",
        f"- Abandoned: The unsafe thing: it broke. — [gotcha] Second decision (id {rid(6)})",
        f"- Abandoned: The unsafe thing: it broke. — [gotcha] Third decision (id {rid(7)})",
        f"- Abandoned: The unsafe thing: it broke. — [gotcha] Two decisions (id {rid(3)})",
        f"- Abandoned: The unsafe thing: it broke. — [gotcha] Tie included (id {rid(2)})",
    ]


def test_rejected_starts_at_its_first_character() -> None:
    row = decision(1, "Replacement", rejected="The predecessor's rule: gone.")
    ctx = context(by_file={"a.py": (record(row),)}, working_set=("a.py",))
    assert render(ctx, BUDGET) == [
        WORKING_SET_HEADING,
        f"- Abandoned: The predecessor's rule: gone. — [gotcha] Replacement (id {rid(1)})",
    ]


def test_rejected_and_title_are_clipped_to_their_respective_limits() -> None:
    row = decision(1, "T" * 90, rejected="R" * 250)
    ctx = context(by_file={"a.py": (record(row),)}, working_set=("a.py",))
    assert render(ctx, BUDGET) == [
        WORKING_SET_HEADING,
        f"- Abandoned: {'R' * 220}… — [gotcha] {'T' * 70}… (id {rid(1)})",
    ]


def test_a_blank_rejected_is_skipped() -> None:
    ctx = context(by_file=rich_context().by_file, working_set=("src/b.py",))
    assert render(ctx, BUDGET) == [
        WORKING_SET_HEADING,
        f"- Abandoned: The unsafe thing: it broke. — [lesson] A lesson (id {rid(5)})",
    ]


def test_a_record_on_two_files_is_printed_once() -> None:
    shared = record(decision(1, "Shared decision"))
    ctx = context(
        by_file={"a.py": (record(decision(2, "Other decision")), shared), "b.py": (shared,)},
        working_set=("b.py", "a.py"),
    )
    assert render(ctx, BUDGET) == [
        WORKING_SET_HEADING,
        f"- Abandoned: The unsafe thing: it broke. — [gotcha] Shared decision (id {rid(1)})",
        f"- Abandoned: The unsafe thing: it broke. — [gotcha] Other decision (id {rid(2)})",
    ]


def test_facts_are_not_entries() -> None:
    ctx = context(
        by_file={
            "a.py": (
                record(decision(1, "One decision")),
                record(fact(8, "A measured fact."), "fact"),
            )
        },
        working_set=("a.py",),
    )
    assert render(ctx, BUDGET) == [
        WORKING_SET_HEADING,
        f"- Abandoned: The unsafe thing: it broke. — [gotcha] One decision (id {rid(1)})",
    ]


def test_the_pointer_names_the_files_of_what_was_left_out() -> None:
    files = ("a.py", "b.py", "c.py", "d.py")
    ctx = context(
        by_file={
            path: (
                record(decision(2 * i + 1, "First entry")),
                record(decision(2 * i + 2, "Second entry")),
            )
            for i, path in enumerate(files)
        },
        working_set=files,
    )
    expected = [
        WORKING_SET_HEADING,
        f"- Abandoned: The unsafe thing: it broke. — [gotcha] First entry (id {rid(1)})",
        f"- Abandoned: The unsafe thing: it broke. — [gotcha] Second entry (id {rid(2)})",
        f"- Abandoned: The unsafe thing: it broke. — [gotcha] First entry (id {rid(3)})",
        '5 more: get_task_context(files=["b.py", "c.py", "d.py"]).',
    ]
    assert render(ctx, size(expected)) == expected

    extended = context(
        by_file={**ctx.by_file, "e.py": (record(decision(9, "Extra entry")),)},
        working_set=(*files, "e.py"),
    )
    assert render(extended, size(expected)) == [
        *expected[:-1],
        '6 more: get_task_context(files=["b.py", "c.py", "d.py"]).',
    ]


def test_a_path_with_a_quote_stays_a_valid_call() -> None:
    path = 'src/é"\\file.py'
    ctx = context(
        by_file={
            "a.py": (record(decision(1, "First entry")),),
            path: (record(decision(2, "Long entry", rejected="x" * 160)),),
        },
        working_set=("a.py", path),
    )
    expected = [
        WORKING_SET_HEADING,
        f"- Abandoned: The unsafe thing: it broke. — [gotcha] First entry (id {rid(1)})",
        r'1 more: get_task_context(files=["src/é\"\\file.py"]).',
    ]
    assert render(ctx, size(expected)) == expected


def test_a_path_with_a_lone_surrogate_stays_a_valid_call() -> None:
    ctx = context(
        by_file={
            "a.py": (record(decision(1, "First entry")),),
            "zé\udcff.py": (record(decision(2, "Long entry", rejected="x" * 160)),),
        },
    )
    first = f"- Abandoned: The unsafe thing: it broke. — [gotcha] First entry (id {rid(1)})"
    assert render(ctx, BUDGET) == [
        FALLBACK_HEADING,
        first,
        f"- Abandoned: {'x' * 160} — [gotcha] Long entry (id {rid(2)})",
    ]
    expected = [
        FALLBACK_HEADING,
        first,
        r'1 more: get_task_context(files=["zé\udcff.py"]).',
    ]
    assert render(ctx, size(expected)) == expected


def test_fallback_keeps_healthy_files_when_an_index_path_is_not_a_string() -> None:
    ctx = context(
        by_file={
            "a.py": (record(decision(1, "Healthy anchor")),),
            42: (record(decision(2, "Malformed anchor")),),
        },
    )
    assert render(ctx, BUDGET) == [
        FALLBACK_HEADING,
        f"- Abandoned: The unsafe thing: it broke. — [gotcha] Healthy anchor (id {rid(1)})",
    ]


def test_nothing_abandoned_gives_nothing() -> None:
    ctx = context(by_file={"a.py": (record(decision(1, "Blank", rejected=" \t\n")),)})
    assert render(ctx, BUDGET) == []


def test_a_drifted_record_carries_the_tag() -> None:
    ctx = context(
        by_file={"a.py": (record(decision(1, "Drifted decision")),)},
        working_set=("a.py",),
        drifted=frozenset({rid(1)}),
    )
    assert render(ctx, BUDGET) == [
        WORKING_SET_HEADING,
        "- Abandoned: The unsafe thing: it broke."
        f" — [gotcha] Drifted decision [drifted] (id {rid(1)})",
    ]
