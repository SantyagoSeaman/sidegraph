"""Shared contracts for all eight SessionStart briefing blocks (S1–S5)."""

import re
from dataclasses import replace

import pytest

from sidegraph.host.briefing import FLAG_NAMES
from sidegraph.host.briefing.blocks import BLOCKS, Block
from sidegraph.host.briefing.lines import size
from tests.briefing_fixtures import restrict, rich_context
from tests.test_docs_claims import _src_env_var_names

NEEDS = {
    "protocol": set(),
    "framing": {"decisions", "anchors"},
    "working_set": {"anchors", "git"},
    "abandoned": {"anchors", "git"},
    "constraints": {"decisions"},
    "changes": {"decisions", "window"},
    "threads": {"decisions", "window", "anchors", "git", "queue"},
    "vocabulary": {"anchors", "symbols"},
}


def test_the_registry_is_the_eight_blocks_in_order() -> None:
    assert [block.name for block in BLOCKS] == list(NEEDS)
    assert [block.budget for block in BLOCKS] == [900, 1200, 4000, 2000, 3000, 2400, 1800, 900]
    assert tuple("SIDEGRAPH_BRIEFING_" + block.name.upper() for block in BLOCKS) == FLAG_NAMES
    assert {block.name: block.needs for block in BLOCKS} == NEEDS


def test_the_docs_guard_sees_the_flags() -> None:
    assert set(FLAG_NAMES) <= _src_env_var_names()


@pytest.mark.parametrize("block", BLOCKS, ids=lambda b: b.name)
class TestSharedBlocks:
    def test_s1_every_budget_gives_nothing_or_a_whole_text(self, block: Block) -> None:
        ctx = rich_context()
        for budget in range(block.budget + 1):
            lines = block.render(ctx, budget)
            if not lines:
                continue
            assert size(lines) <= budget
            assert all("\n" not in line for line in lines)
            assert lines[0].startswith("## ") and len(lines) >= 2
            assert not lines[-1].endswith(":")

    def test_s2_every_bullet_carries_an_id(self, block: Block) -> None:
        for line in block.render(rich_context(), block.budget):
            if line.startswith(("- [", "- Abandoned")):
                assert re.search(r"\(id [0-9A-Z]{26}\)", line)

    def test_s3_no_proposal_is_rendered(self, block: Block) -> None:
        ctx = rich_context()
        lines = block.render(ctx, block.budget)
        assert all("[unratified]" not in line for line in lines)
        if block.name != "threads":
            assert block.render(replace(ctx, queue_count=9), block.budget) == lines

    def test_s4_the_output_does_not_depend_on_mapping_order(self, block: Block) -> None:
        ctx = rich_context()
        lines = block.render(ctx, block.budget)
        assert block.render(ctx, block.budget) == lines
        reversed_ctx = replace(
            ctx,
            by_file=dict(reversed(list(ctx.by_file.items()))),
            symbols=dict(reversed(list(ctx.symbols.items()))),
            replaced_titles=dict(reversed(list(ctx.replaced_titles.items()))),
        )
        assert block.render(reversed_ctx, block.budget) == lines

    def test_s5_a_block_reads_only_the_parts_it_declares(self, block: Block) -> None:
        ctx = rich_context()
        assert block.render(restrict(ctx, block.needs), block.budget) == block.render(
            ctx, block.budget
        )
