"""Is the code graph current? The answer as data, and the one phrase every surface uses.

Portable core: this module knows nothing about Graphify or git. ``GraphifyReader.freshness()``
(the engine seam, ``engine/reader.py``) does the comparison and builds a ``GraphFreshness``;
SessionStart, ``get_task_context``, ``sidegraph-doctor`` and ``sidegraph-stats`` only render it.
# see design/superpowers/specs/2026-10-01-stale-graph-visible-design.md (D3)
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

# Characters of a commit id shown to a person. Matches git's default abbreviation.
_SHORT = 7


class GraphFreshness(BaseModel):
    """What the graph's build commit says about the graph, set against HEAD.

    ``stale`` means a file the graph should reflect changed since the build and the graph
    does not reflect it; ``commits_behind`` is reported and never decides that. ``unknown``
    carries a ``reason`` and nothing else a surface should print.
    """

    state: Literal["fresh", "stale", "unknown"]
    built_at: str | None = None  # full sha from graph.json
    head: str | None = None  # full sha
    in_history: bool | None = None  # built_at is an ancestor of HEAD
    commits_behind: int | None = None  # set only when in_history
    changed: int = 0  # counting paths (spec D2 step 7)
    sample: list[str] = []  # first 5 of them (spec D2 step 8)
    reason: str | None = None  # set only when state == "unknown"


def _plural(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def staleness_phrase(f: GraphFreshness) -> str:
    """The one wording of "how stale", for every surface.

    ``built at 314f1ac, 314 commits behind HEAD, 258 files changed since`` when the build
    commit is in HEAD's history; ``built at 8cb7279, a commit outside HEAD's history; 1 file
    differs from HEAD`` otherwise (a checkout of an older commit, a rewritten history).
    Singular and plural follow the counts.
    """
    built = f.built_at[:_SHORT] if f.built_at else "an unknown commit"
    if f.in_history:
        return (
            f"built at {built}, {_plural(f.commits_behind or 0, 'commit')} behind HEAD, "
            f"{_plural(f.changed, 'file')} changed since"
        )
    verb = "differs" if f.changed == 1 else "differ"
    return (
        f"built at {built}, a commit outside HEAD's history; "
        f"{_plural(f.changed, 'file')} {verb} from HEAD"
    )
