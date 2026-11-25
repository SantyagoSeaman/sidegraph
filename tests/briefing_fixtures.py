"""Plain briefing rows and a context shared by the block tests."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Literal

from sidegraph.host.briefing.context import BriefingContext
from sidegraph.hot_index import Record

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
PART_FIELDS = {
    "decisions": ("accepted",),
    "anchors": ("by_file",),
    "git": ("working_set",),
    "window": ("since", "since_is_fallback", "replaced_titles"),
    "queue": ("queue_count",),
    "symbols": ("symbols",),
}


def rid(n: int) -> str:
    return f"01TEST{n:020d}"


def decision(
    n: int,
    title: str,
    kind: str = "gotcha",
    *,
    days_ago: float = 1.0,
    choice: str = "Do the safe thing. More.",
    rejected: str | None = "The unsafe thing: it broke.",
    supersedes: str | None = None,
) -> dict:
    return {
        "id": rid(n),
        "title": title,
        "kind": kind,
        "choice": choice,
        "rejected": rejected,
        "supersedes": supersedes,
        "valid_from": (NOW - timedelta(days=days_ago)).isoformat(),
        "valid_to": None,
        "status": "accepted",
    }


def fact(n: int, statement: str, source: str = "measured") -> dict:
    return {
        "id": rid(n),
        "statement": statement,
        "source": source,
        "status": "accepted",
        "valid_from": NOW.isoformat(),
        "valid_to": None,
    }


def record(row: dict, record_type: Literal["decision", "fact"] = "decision") -> Record:
    return Record(row, False, record_type)


def context(**fields) -> BriefingContext:
    return BriefingContext(now=NOW, **fields)


def restrict(ctx: BriefingContext, needs: frozenset[str]) -> BriefingContext:
    defaults = context()
    return replace(
        ctx,
        **{
            name: getattr(defaults, name)
            for part, names in PART_FIELDS.items()
            if part not in needs
            for name in names
        },
    )


def rich_context() -> BriefingContext:
    first = decision(1, "First gotcha", days_ago=0.5)
    second = decision(2, "Second gotcha")
    constraint = decision(3, "Anchored constraint", "constraint", days_ago=2)
    adr = decision(4, "An ADR", "adr", days_ago=2.5, rejected=None)
    lesson = decision(5, "A lesson", "lesson", days_ago=4)
    replacement = decision(6, "The new rule", "adr", days_ago=2.8, supersedes=rid(0))
    unanchored = decision(7, "Unanchored constraint", "constraint", days_ago=20)
    return context(
        accepted=(first, second, constraint, adr, replacement, lesson, unanchored),
        by_file={
            "src/a.py": (
                record(first),
                record(second),
                record(constraint),
                record(fact(8, "A measured fact."), "fact"),
            ),
            "src/b.py": (record(lesson), record(adr)),
            "src/c.py": (record(replacement),),
        },
        working_set=("src/a.py", "src/b.py"),
        drifted=frozenset({rid(1), rid(6)}),
        since=NOW - timedelta(days=3),
        since_is_fallback=False,
        replaced_titles={rid(0): "The old rule"},
        queue_count=4,
        symbols={"Store": 3, "open_store": 1, "Zeta": 1},
    )
