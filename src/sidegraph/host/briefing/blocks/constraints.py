"""Constraints in force.

See design/superpowers/specs/2026-11-20-sessionstart-briefing-design.md (B3).
"""

from ..context import BriefingContext
from ..lines import fit_lines, title_line


def render(ctx: BriefingContext, budget: int) -> list[str]:
    """Show live accepted constraint titles in context order."""
    entries = [
        title_line(row, ctx.drifted, limit=110, kind=False)
        for row in ctx.accepted
        if row["kind"] == "constraint"
    ]
    return fit_lines(
        f"## Constraints in force ({len(entries)})",
        entries,
        budget,
        lambda n: f'{n} more: retrieve_decisions(kind="constraint").',
    )
