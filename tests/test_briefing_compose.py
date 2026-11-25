"""Core composition with injected briefing renderers (D7, K10, K11)."""

from collections.abc import Callable
from unittest.mock import Mock

from sidegraph.host.briefing.blocks import BLOCKS, Block
from sidegraph.host.briefing.context import BriefingContext
from sidegraph.host.briefing.core import DRIFT_LEGEND, compose
from sidegraph.host.briefing.lines import units
from sidegraph.store_layout import MEMORY_GUARD_LINE
from tests.briefing_fixtures import context, rich_context


def _block(
    name: str,
    lines: list[str] | Callable[[BriefingContext, int], list[str]],
    budget: int = 1000,
) -> Block:
    def render(ctx: BriefingContext, available: int) -> list[str]:
        return lines(ctx, available) if callable(lines) else lines

    return Block(name, name.upper(), budget, frozenset(), render)


def test_blocks_appear_in_registry_order_with_a_blank_line_between() -> None:
    blocks = [_block("a", ["A"]), _block("b", ["B"]), _block("c", ["C"])]
    assert compose(context(), frozenset({"c", "a"}), blocks=blocks) == (
        f"{MEMORY_GUARD_LINE}\n\nA\n\nC"
    )


def test_each_block_gets_the_smaller_of_its_budget_and_the_room() -> None:
    first = Mock(return_value=["123"])
    second = Mock(return_value=["456"])
    blocks = [_block("protocol", first, 10), _block("framing", second)]
    ctx = context(drifted=frozenset({"a"}))
    limit = units(MEMORY_GUARD_LINE) + units(DRIFT_LEGEND) + 24
    compose(ctx, frozenset({"protocol", "framing"}), blocks=blocks, max_units=limit)
    first.assert_called_once_with(ctx, 10)
    second.assert_called_once_with(ctx, 15)


def test_an_over_budget_block_is_discarded_whole_and_the_rest_render() -> None:
    report = Mock()
    blocks = [_block("protocol", ["defect"], 1), _block("framing", ["F"])]
    result = compose(context(), frozenset({"protocol", "framing"}), blocks=blocks, report=report)
    assert result == f"{MEMORY_GUARD_LINE}\n\nF"
    [(stage, error)] = [call.args for call in report.call_args_list]
    assert stage == "protocol" and isinstance(error, ValueError)
    assert all(part in str(error) for part in ("protocol", "6", "1"))


def test_a_raising_block_is_skipped_and_the_rest_render() -> None:
    report = Mock()
    error = RuntimeError("renderer fault")
    blocks = [_block("protocol", ["P"]), _block("framing", Mock(side_effect=error))]
    assert (
        compose(context(), frozenset({"protocol", "framing"}), blocks=blocks, report=report) == "P"
    )
    report.assert_called_once_with("framing", error)


def test_eight_full_blocks_stay_within_the_limit() -> None:
    def fill(ctx: BriefingContext, budget: int) -> list[str]:
        return ["x" * budget]

    blocks = [_block(block.name, fill, block.budget) for block in BLOCKS]
    result = compose(rich_context(), frozenset(b.name for b in blocks), blocks=blocks)
    assert units(result) <= 9500
    assert units(result) > 8100


def test_a_last_block_that_fits_exactly_is_kept_and_one_unit_more_is_not() -> None:
    blocks = [_block("protocol", ["P"]), _block("framing", ["F"])]
    expected = f"P\n\n{MEMORY_GUARD_LINE}\n\nF"
    enabled = frozenset({"protocol", "framing"})
    assert compose(context(), enabled, blocks=blocks, max_units=units(expected)) == expected
    assert compose(context(), enabled, blocks=blocks, max_units=units(expected) - 1) == "P"


def test_an_astral_character_counts_two_units() -> None:
    blocks = [_block("protocol", ["😀"])]
    assert compose(context(), frozenset({"protocol"}), blocks=blocks, max_units=2) == "😀"
    assert compose(context(), frozenset({"protocol"}), blocks=blocks, max_units=1) == ""


def test_every_output_line_came_from_a_block_or_is_the_guard_or_the_legend() -> None:
    a = ["## One", "entry"]
    b = ["## Two", "Title [drifted]"]
    blocks = [_block("protocol", a), _block("framing", b)]
    result = compose(rich_context(), frozenset({"protocol", "framing"}), blocks=blocks)
    assert result.splitlines() == [*a, "", MEMORY_GUARD_LINE, "", *b, "", DRIFT_LEGEND]


def test_the_guard_line_precedes_the_first_block_that_is_not_the_protocol() -> None:
    blocks = [_block("protocol", ["P"]), _block("framing", []), _block("changes", ["F"])]
    names = frozenset({"protocol", "framing", "changes"})
    assert compose(context(), names, blocks=blocks) == f"P\n\n{MEMORY_GUARD_LINE}\n\nF"
    assert compose(context(), frozenset({"protocol"}), blocks=blocks) == "P"
    assert compose(context(), frozenset({"protocol", "framing"}), blocks=blocks) == "P"


def test_the_legend_closes_a_briefing_that_shows_a_drifted_record() -> None:
    blocks = [_block("framing", ["Title [drifted]"])]
    assert compose(rich_context(), frozenset({"framing"}), blocks=blocks).endswith(
        f"Title [drifted]\n\n{DRIFT_LEGEND}"
    )


def test_a_drift_set_without_a_tagged_line_brings_no_legend() -> None:
    blocks = [_block("framing", ["F"])]
    assert compose(rich_context(), frozenset({"framing"}), blocks=blocks) == (
        f"{MEMORY_GUARD_LINE}\n\nF"
    )


def test_a_literal_drift_tag_in_a_title_brings_no_legend_and_no_overflow() -> None:
    expected = f"{MEMORY_GUARD_LINE}\n\nTitle [drifted]"
    blocks = [_block("framing", ["Title [drifted]"])]
    assert (
        compose(context(), frozenset({"framing"}), blocks=blocks, max_units=units(expected))
        == expected
    )


def test_nothing_rendered_gives_the_empty_string() -> None:
    blocks = [_block("framing", [])]
    assert compose(rich_context(), frozenset({"framing"}), blocks=blocks) == ""
