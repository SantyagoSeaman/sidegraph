"""Project memory counts and anchored examples.

See design/superpowers/specs/2026-11-20-sessionstart-briefing-design.md (B1).
"""

from ....store_layout import MISTAKE_KINDS
from ..context import BriefingContext
from ..lines import size, title_line


def render(ctx: BriefingContext, budget: int) -> list[str]:
    """Render the framing paragraph and as many whole examples as fit."""
    if not ctx.accepted:
        return []

    mistakes = sum(row["kind"] in MISTAKE_KINDS for row in ctx.accepted)
    lines = [
        "## Sidegraph: this project's memory",
        "This project's decisions, rejected approaches and paid-for mistakes are recorded in "
        f"Sidegraph, not kept in anyone's head: {len(ctx.accepted)} accepted records, "
        f"{mistakes} of them recorded mistakes, anchored to code in {len(ctx.by_file)} files. "
        "Treat it as the colleague who has been on the project longest: what you would ask "
        "that colleague, ask memory first.",
    ]
    if size(lines) > budget:
        return []

    anchored_ids = {
        record.decision["id"]
        for records in ctx.by_file.values()
        for record in records
        if record.record_type == "decision"
    }
    examples = []
    for kind in ("gotcha", "constraint", "adr"):
        # The context already orders accepted decisions newest first.
        row = next(
            (row for row in ctx.accepted if row["kind"] == kind and row["id"] in anchored_ids),
            None,
        )
        if row is not None:
            examples.append(row)
    if not examples:
        return lines

    first, *remaining = examples
    lookup = (
        "Records it holds, for example (look up the first with "
        f'retrieve_decisions(ids=["{first["id"]}"])):'
    )
    with_first = [*lines, lookup, title_line(first, ctx.drifted)]
    if size(with_first) > budget:
        return lines

    for row in remaining:
        line = title_line(row, ctx.drifted)
        if size([*with_first, line]) > budget:
            break
        with_first.append(line)
    return with_first
