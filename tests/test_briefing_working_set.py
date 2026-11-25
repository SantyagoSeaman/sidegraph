"""Working-set memory groups and their continuation calls."""

import json

from sidegraph.host.briefing.blocks.working_set import render
from sidegraph.host.briefing.lines import decision_line, size
from sidegraph.host.hooks import _record_line
from tests.briefing_fixtures import context, decision, fact, record, rich_context, rid

HEADING = "## Memory for the files you are working on"
BUDGET = 4000


def test_each_group_is_the_hook_slice_row_by_row() -> None:
    ctx = rich_context()
    first, second, constraint, _ = ctx.by_file["src/a.py"]
    lesson, adr = ctx.by_file["src/b.py"]
    assert render(ctx, BUDGET) == [
        HEADING,
        "Recorded for src/a.py (3 of 4, mistakes first):",
        decision_line(first.decision, ctx.drifted, rejected=120),
        decision_line(second.decision, ctx.drifted, rejected=120),
        decision_line(constraint.decision, ctx.drifted, rejected=120),
        'More: get_task_context(files=["src/a.py"]).',
        "Recorded for src/b.py (2 of 2, mistakes first):",
        decision_line(lesson.decision, ctx.drifted, rejected=120),
        decision_line(adr.decision, ctx.drifted, rejected=120),
    ]


def test_a_decision_line_carries_choice_and_rejected() -> None:
    path = "src/a.py"
    ctx = context(by_file={path: (record(decision(1, "First gotcha")),)}, working_set=(path,))
    assert render(ctx, BUDGET) == [
        HEADING,
        "Recorded for src/a.py (1 of 1, mistakes first):",
        "- [gotcha] First gotcha — Do the safe thing. Rejected: The unsafe thing: it broke."
        f" (id {rid(1)})",
    ]


def test_a_fact_in_the_slice_is_its_whole_fact_line() -> None:
    path = "src/a.py"
    row = decision(1, "First gotcha")
    measured = record(fact(8, "A measured fact.", "a specific source"), "fact")
    ctx = context(by_file={path: (record(row), measured)}, working_set=(path,))
    assert render(ctx, BUDGET) == [
        HEADING,
        "Recorded for src/a.py (2 of 2, mistakes first):",
        decision_line(row, ctx.drifted, rejected=120),
        _record_line(measured),
    ]


def test_a_cut_names_the_files_left_out() -> None:
    paths = ("src/a.py", "src/b.py", "src/c.py", "src/d.py")
    row = decision(1, "First gotcha")
    ctx = context(by_file={path: (record(row),) for path in paths}, working_set=paths)
    line = decision_line(row, ctx.drifted, rejected=120)
    expected = [
        HEADING,
        "Recorded for src/a.py (1 of 1, mistakes first):",
        line,
        "Recorded for src/b.py (1 of 1, mistakes first):",
        line,
        "2 more changed files have recorded memory; next: "
        'get_task_context(files=["src/c.py", "src/d.py"]).',
    ]
    assert render(ctx, size(expected)) == expected

    extra = "src/e.py"
    ctx = context(by_file={**ctx.by_file, extra: (record(row),)}, working_set=(*paths, extra))
    capped = [
        HEADING,
        "Recorded for src/a.py (1 of 1, mistakes first):",
        line,
        "4 more changed files have recorded memory; next: "
        'get_task_context(files=["src/b.py", "src/c.py", "src/d.py"]).',
    ]
    assert render(ctx, size(capped)) == capped


def test_a_group_is_whole_or_absent() -> None:
    ctx = rich_context()
    first, second, constraint, _ = ctx.by_file["src/a.py"]
    lesson, adr = ctx.by_file["src/b.py"]
    first_group = [
        HEADING,
        "Recorded for src/a.py (3 of 4, mistakes first):",
        decision_line(first.decision, ctx.drifted, rejected=120),
        decision_line(second.decision, ctx.drifted, rejected=120),
        decision_line(constraint.decision, ctx.drifted, rejected=120),
        'More: get_task_context(files=["src/a.py"]).',
    ]
    cut = [
        *first_group,
        '1 more changed files have recorded memory; next: get_task_context(files=["src/b.py"]).',
    ]
    full = [
        *first_group,
        "Recorded for src/b.py (2 of 2, mistakes first):",
        decision_line(lesson.decision, ctx.drifted, rejected=120),
        decision_line(adr.decision, ctx.drifted, rejected=120),
    ]
    for budget in range(size(first_group), size(full)):
        assert render(ctx, budget) == (cut if budget >= size(cut) else [])
    assert render(ctx, size(full)) == full


def test_an_empty_working_set_gives_nothing() -> None:
    assert render(context(by_file=rich_context().by_file), BUDGET) == []


def test_a_path_with_a_quote_stays_a_valid_call() -> None:
    path = 'src/we"ird é\\name.py'
    row = decision(1, "First gotcha", kind="adr")
    ctx = context(by_file={path: (record(row), record(row), record(row))}, working_set=(path,))
    line = decision_line(row, ctx.drifted, rejected=120)
    lines = render(ctx, BUDGET)
    assert lines == [
        HEADING,
        r'Recorded for src/we"ird é\\name.py (2 of 3, mistakes first):',
        line,
        line,
        r'More: get_task_context(files=["src/we\"ird é\\name.py"]).',
    ]
    files = lines[-1].removeprefix("More: get_task_context(files=").removesuffix(").")
    assert json.loads(files) == [path]


def test_a_drifted_record_carries_the_tag() -> None:
    assert render(rich_context(), BUDGET)[2:4] == [
        "- [gotcha] First gotcha [drifted] — Do the safe thing."
        f" Rejected: The unsafe thing: it broke. (id {rid(1)})",
        "- [gotcha] Second gotcha — Do the safe thing."
        f" Rejected: The unsafe thing: it broke. (id {rid(2)})",
    ]


def test_a_filename_line_break_stays_in_one_header_line() -> None:
    path = "src/first\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029second.py"
    rows = [decision(1, "First", "adr"), decision(2, "Second", "adr")]
    ctx = context(by_file={path: tuple(record(row) for row in rows)}, working_set=(path,))
    assert render(ctx, BUDGET) == [
        HEADING,
        r"Recorded for src/first\n\r\u000b\f\u001c\u001d\u001e\u0085\u2028\u2029second.py"
        " (2 of 2, mistakes first):",
        decision_line(rows[0], ctx.drifted, rejected=120),
        decision_line(rows[1], ctx.drifted, rejected=120),
    ]


def test_continuation_calls_escape_unicode_line_breaks() -> None:
    first, second = "src/a.py", "src/first\x85\u2028\u2029second.py"
    rows = [decision(n, f"Decision {n}", "adr") for n in range(1, 5)]
    ctx = context(
        by_file={first: (record(rows[0]),), second: tuple(record(row) for row in rows[1:])},
        working_set=(first, second),
    )
    cut = [
        HEADING,
        "Recorded for src/a.py (1 of 1, mistakes first):",
        decision_line(rows[0], ctx.drifted, rejected=120),
        "1 more changed files have recorded memory; next: "
        r'get_task_context(files=["src/first\u0085\u2028\u2029second.py"]).',
    ]
    more = r'More: get_task_context(files=["src/first\u0085\u2028\u2029second.py"]).'
    assert [render(ctx, BUDGET)[-1:], render(ctx, size(cut))] == [[more], cut]


def test_a_literal_backslash_stays_distinct_from_a_line_break() -> None:
    literal = r"src/a\u2028b.py"
    separator = "src/a\u2028b.py"
    first, second = decision(1, "First"), decision(2, "Second")
    ctx = context(
        by_file={literal: (record(first),), separator: (record(second),)},
        working_set=(literal, separator),
    )
    assert render(ctx, BUDGET) == [
        HEADING,
        r"Recorded for src/a\\u2028b.py (1 of 1, mistakes first):",
        decision_line(first, ctx.drifted, rejected=120),
        r"Recorded for src/a\u2028b.py (1 of 1, mistakes first):",
        decision_line(second, ctx.drifted, rejected=120),
    ]
