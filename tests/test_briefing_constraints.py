"""Exact lines for the constraints briefing block."""

from sidegraph.host.briefing.blocks.constraints import render
from tests.briefing_fixtures import context, decision, rich_context, rid


def test_every_constraint_once_in_the_order_of_the_context() -> None:
    assert render(rich_context(), 3000) == [
        "## Constraints in force (2)",
        f"- Anchored constraint (id {rid(3)})",
        f"- Unanchored constraint (id {rid(7)})",
    ]


def test_equal_dates_keep_the_context_order() -> None:
    ctx = context(
        accepted=(
            decision(7, "Zulu", "constraint"),
            decision(6, "Alpha", "constraint"),
            decision(3, "Mike", "constraint"),
        ),
    )
    assert render(ctx, 3000) == [
        "## Constraints in force (3)",
        f"- Zulu (id {rid(7)})",
        f"- Alpha (id {rid(6)})",
        f"- Mike (id {rid(3)})",
    ]


def test_other_kinds_are_absent() -> None:
    ctx = context(
        accepted=(
            decision(1, "A gotcha", days_ago=0.5),
            decision(2, "An ADR", "adr"),
            decision(3, "A constraint", "constraint", days_ago=2),
            decision(4, "A lesson", "lesson", days_ago=3),
        ),
    )
    assert render(ctx, 3000) == [
        "## Constraints in force (1)",
        f"- A constraint (id {rid(3)})",
    ]


def test_a_long_title_is_clipped_at_110() -> None:
    ctx = context(accepted=(decision(3, "x" * 111, "constraint"),))
    assert render(ctx, 3000) == [
        "## Constraints in force (1)",
        f"- {'x' * 110}… (id {rid(3)})",
    ]


def test_a_drifted_constraint_carries_the_tag_after_its_title() -> None:
    ctx = context(
        accepted=(decision(3, "A drifted constraint", "constraint"),),
        drifted=frozenset({rid(3)}),
    )
    assert render(ctx, 3000) == [
        "## Constraints in force (1)",
        f"- A drifted constraint [drifted] (id {rid(3)})",
    ]


def test_a_cut_list_keeps_the_full_count_and_its_pointer() -> None:
    ctx = context(
        accepted=tuple(
            decision(n, f"Constraint {n:02}", "constraint", days_ago=n) for n in range(1, 31)
        ),
    )
    expected = [
        "## Constraints in force (30)",
        *(f"- Constraint {n:02} (id {rid(n)})" for n in range(1, 11)),
        '20 more: retrieve_decisions(kind="constraint").',
    ]
    assert render(ctx, len("\n".join(expected))) == expected


def test_no_constraint_gives_nothing() -> None:
    ctx = context(accepted=(decision(1, "A gotcha"),))
    assert render(ctx, 3000) == []
