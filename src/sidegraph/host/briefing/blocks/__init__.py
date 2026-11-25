"""Block order, flags, budgets and inputs.

See design/superpowers/specs/2026-11-20-sessionstart-briefing-design.md (D2).
"""

from collections.abc import Callable
from typing import NamedTuple

from ..context import BriefingContext
from . import abandoned, changes, constraints, framing, protocol, threads, vocabulary, working_set


class Block(NamedTuple):
    """One optional briefing renderer and its inputs."""

    name: str
    flag: str
    budget: int
    needs: frozenset[str]
    render: Callable[[BriefingContext, int], list[str]]


BLOCKS: tuple[Block, ...] = (
    Block("protocol", "SIDEGRAPH_BRIEFING_PROTOCOL", 900, frozenset(), protocol.render),
    Block(
        "framing",
        "SIDEGRAPH_BRIEFING_FRAMING",
        1200,
        frozenset({"decisions", "anchors"}),
        framing.render,
    ),
    Block(
        "working_set",
        "SIDEGRAPH_BRIEFING_WORKING_SET",
        4000,
        frozenset({"anchors", "git"}),
        working_set.render,
    ),
    Block(
        "abandoned",
        "SIDEGRAPH_BRIEFING_ABANDONED",
        2000,
        frozenset({"anchors", "git"}),
        abandoned.render,
    ),
    Block(
        "constraints",
        "SIDEGRAPH_BRIEFING_CONSTRAINTS",
        3000,
        frozenset({"decisions"}),
        constraints.render,
    ),
    Block(
        "changes",
        "SIDEGRAPH_BRIEFING_CHANGES",
        2400,
        frozenset({"decisions", "window"}),
        changes.render,
    ),
    Block(
        "threads",
        "SIDEGRAPH_BRIEFING_THREADS",
        1800,
        frozenset({"decisions", "window", "anchors", "git", "queue"}),
        threads.render,
    ),
    Block(
        "vocabulary",
        "SIDEGRAPH_BRIEFING_VOCABULARY",
        900,
        frozenset({"anchors", "symbols"}),
        vocabulary.render,
    ),
)
FLAG_NAMES: tuple[str, ...] = tuple(block.flag for block in BLOCKS)
