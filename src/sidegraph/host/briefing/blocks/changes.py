"""The changes briefing block.

See design/superpowers/specs/2026-11-20-sessionstart-briefing-design.md (B4).
"""

from datetime import UTC, datetime

from ..context import BriefingContext
from ..lines import clip, decision_line, fit_lines, title_line

MAX_PREDECESSOR_ID_CHARS = 128


def render(ctx: BriefingContext, budget: int) -> list[str]:
    """Show accepted replacements and additions in the supplied session window."""
    since = ctx.since
    if since is None:
        return []

    if ctx.since_is_fallback:
        heading = f"## Changed in the 14 days before the newest record (since {since:%Y-%m-%d})"
    else:
        heading = f"## Changed since the last session ({since.astimezone(UTC):%Y-%m-%d %H:%M} UTC)"

    replacements: list[str] = []
    additions: list[str] = []
    # The context already orders accepted rows newest first.
    for row in ctx.accepted:
        if datetime.fromisoformat(row["valid_from"]) < since:
            continue
        predecessor_id = row.get("supersedes")
        if predecessor_id is None or predecessor_id == "":
            additions.append(decision_line(row, ctx.drifted))
            continue

        predecessor = "an earlier record"
        if (
            isinstance(predecessor_id, str)
            and len(predecessor_id) <= MAX_PREDECESSOR_ID_CHARS
            and predecessor_id.splitlines() == [predecessor_id]
        ):
            predecessor_title = clip(ctx.replaced_titles.get(predecessor_id), 60)
            if predecessor_title:
                predecessor = f'"{predecessor_title}"'
            predecessor += f" (id {predecessor_id})"
        replacements.append(f"{title_line(row, ctx.drifted)} replaces {predecessor}")

    return fit_lines(
        heading,
        replacements + additions,
        budget,
        lambda count: f"{count} more: retrieve_decisions() lists the newest records.",
    )
