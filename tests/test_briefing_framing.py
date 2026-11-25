"""Framing examples, counts and budget boundaries."""

from dataclasses import replace

from sidegraph.host.briefing.blocks.framing import render
from sidegraph.host.briefing.lines import size
from tests.briefing_fixtures import context, decision, record, rich_context, rid

BUDGET = 1200
HEADING = "## Sidegraph: this project's memory"
PARAGRAPH = (
    "This project's decisions, rejected approaches and paid-for mistakes are recorded in "
    "Sidegraph, not kept in anyone's head: 7 accepted records, 5 of them recorded mistakes, "
    "anchored to code in 3 files. Treat it as the colleague who has been on the project "
    "longest: what you would ask that colleague, ask memory first."
)
LOOKUP = (
    "Records it holds, for example (look up the first with "
    'retrieve_decisions(ids=["01TEST00000000000000000001"])):'
)
GOTCHA = f"- [gotcha] First gotcha [drifted] (id {rid(1)})"
CONSTRAINT = f"- [constraint] Anchored constraint (id {rid(3)})"
ADR = f"- [adr] An ADR (id {rid(4)})"


def test_the_whole_block_on_the_rich_context() -> None:
    assert render(rich_context(), BUDGET) == [HEADING, PARAGRAPH, LOOKUP, GOTCHA, CONSTRAINT, ADR]


def test_a_record_with_no_live_anchor_is_not_an_example() -> None:
    ctx = rich_context()
    newer = decision(9, "An unanchored newer constraint", "constraint", days_ago=0)
    ctx = replace(ctx, accepted=(newer, *ctx.accepted))

    assert render(ctx, BUDGET) == [
        HEADING,
        "This project's decisions, rejected approaches and paid-for mistakes are recorded in "
        "Sidegraph, not kept in anyone's head: 8 accepted records, 6 of them recorded mistakes, "
        "anchored to code in 3 files. Treat it as the colleague who has been on the project "
        "longest: what you would ask that colleague, ask memory first.",
        LOOKUP,
        GOTCHA,
        CONSTRAINT,
        ADR,
    ]


def test_a_kind_with_no_anchored_record_is_skipped() -> None:
    ctx = rich_context()
    by_file = {"src/a.py": ctx.by_file["src/a.py"], "src/b.py": ctx.by_file["src/b.py"][:1]}
    ctx = replace(ctx, by_file=by_file)

    assert render(ctx, BUDGET)[2:] == [LOOKUP, GOTCHA, CONSTRAINT]

    by_file = {**ctx.by_file, "src/a.py": ctx.by_file["src/a.py"][2:]}
    assert render(replace(ctx, by_file=by_file), BUDGET)[2:] == [
        "Records it holds, for example (look up the first with "
        'retrieve_decisions(ids=["01TEST00000000000000000003"])):',
        CONSTRAINT,
    ]


def test_no_example_leaves_the_paragraph_alone() -> None:
    ctx = replace(rich_context(), by_file={})

    assert render(ctx, BUDGET) == [
        HEADING,
        "This project's decisions, rejected approaches and paid-for mistakes are recorded in "
        "Sidegraph, not kept in anyone's head: 7 accepted records, 5 of them recorded mistakes, "
        "anchored to code in 0 files. Treat it as the colleague who has been on the project "
        "longest: what you would ask that colleague, ask memory first.",
    ]


def test_the_lookup_names_the_first_example_and_says_look_up() -> None:
    ctx = rich_context()
    newer = decision(9, "Newest anchored constraint", "constraint", days_ago=0)
    ctx = replace(
        ctx,
        accepted=(newer, *ctx.accepted),
        by_file={**ctx.by_file, "src/b.py": (record(newer), *ctx.by_file["src/b.py"])},
    )
    lines = render(ctx, BUDGET)

    assert lines[2:5] == [
        LOOKUP,
        GOTCHA,
        f"- [constraint] Newest anchored constraint (id {rid(9)})",
    ]
    assert all("query=" not in line for line in lines)


def test_the_counts_come_from_the_context() -> None:
    assert render(rich_context(), BUDGET)[1:2] == [PARAGRAPH]


def test_the_sentence_and_the_first_example_are_one_unit() -> None:
    budget = size([HEADING, PARAGRAPH, LOOKUP, GOTCHA]) - 1

    assert size([HEADING, PARAGRAPH, LOOKUP]) <= budget
    assert render(rich_context(), budget) == [HEADING, PARAGRAPH]


def test_whole_units_fit_at_their_exact_sizes_and_keep_example_order() -> None:
    ctx = rich_context()
    minimum = [HEADING, PARAGRAPH]
    with_first = [*minimum, LOOKUP, GOTCHA]
    with_second = [*with_first, CONSTRAINT]

    assert render(ctx, size(minimum) - 1) == []
    assert render(ctx, size(minimum)) == minimum
    assert render(ctx, size(with_first)) == with_first
    assert render(ctx, size([*with_first, ADR])) == with_first
    assert render(ctx, size(with_second)) == with_second


def test_an_empty_store_gives_nothing() -> None:
    assert render(context(), BUDGET) == []


def test_a_drifted_example_carries_the_tag() -> None:
    assert render(rich_context(), BUDGET)[3:5] == [GOTCHA, CONSTRAINT]
