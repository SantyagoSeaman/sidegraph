"""Line formats and UTF-16 budgets.

See design/superpowers/specs/2026-11-20-sessionstart-briefing-design.md (D7, D8).
"""

from collections.abc import Callable, Sequence

from ...store_layout import clip_line
from ..hooks import _clip_choice, _first_sentence

DRIFT_TAG = " [drifted]"


def units(text: str) -> int:
    """Count UTF-16 code units."""
    return len(text.encode("utf-16-le")) // 2


def size(lines: Sequence[str]) -> int:
    """Count the units of lines joined with line breaks."""
    return units("\n".join(lines))


def clip(text: str | None, limit: int) -> str:
    """Collapse whitespace and clip the resulting line."""
    return clip_line(" ".join(text.split()), limit) if isinstance(text, str) else ""


def decision_line(row: dict, drifted: frozenset[str], *, rejected: int = 0) -> str:
    """Render a decision with its first sentence and optional rejected text."""
    title = clip(row["title"], 90)
    drift = DRIFT_TAG if row["id"] in drifted else ""
    choice = _clip_choice(_first_sentence(row["choice"]), 120)
    gist = f" — {choice}" if choice else ""
    reason = clip(row.get("rejected"), rejected) if rejected > 0 else ""
    rejected_text = f" Rejected: {reason}" if reason else ""
    return f"- [{row['kind']}] {title}{drift}{gist}{rejected_text} (id {row['id']})"


def title_line(row: dict, drifted: frozenset[str], *, limit: int = 90, kind: bool = True) -> str:
    """Render a decision's title and id."""
    label = f"[{row['kind']}] " if kind else ""
    title = clip(row["title"], limit)
    drift = DRIFT_TAG if row["id"] in drifted else ""
    return f"- {label}{title}{drift} (id {row['id']})"


def fit_lines(
    heading: str, entries: Sequence[str], budget: int, more: Callable[[int], str]
) -> list[str]:
    """Keep the longest prefix that fits with its heading and any pointer."""
    used = units(heading)
    best_count = 0
    best_pointer: list[str] = []
    for count, entry in enumerate(entries, start=1):
        used += 1 + units(entry)
        if used > budget:
            break
        omitted = len(entries) - count
        if not omitted:
            return [heading, *entries]
        pointer = more(omitted)
        if used + 1 + units(pointer) <= budget:
            best_count = count
            best_pointer = [pointer]
    return [heading, *entries[:best_count], *best_pointer] if best_count else []
