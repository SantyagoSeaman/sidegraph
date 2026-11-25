"""Rank files and symbols by their anchored decision counts.

See design/superpowers/specs/2026-11-20-sessionstart-briefing-design.md (B7).
"""

import json

from ..context import BriefingContext
from ..lines import size

# Keep each list on one line while distinguishing literal backslashes from escapes.
_NAME_ESCAPES = str.maketrans(
    {char: json.dumps(char)[1:-1] for char in "\\\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029"}
)


def render(ctx: BriefingContext, budget: int) -> list[str]:
    """Render the highest-ranked files and symbols that fit."""
    file_counts = {
        path: len({record.decision["id"] for record in records if record.record_type == "decision"})
        for path, records in ctx.by_file.items()
    }
    lines = ["## Project vocabulary"]
    for label, counts, limit in (
        ("Files", file_counts, 8),
        ("Symbols", ctx.symbols, 10),
    ):
        ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        items = [f"{name.translate(_NAME_ESCAPES)} ({count})" for name, count in ranked[:limit]]
        while items:
            line = f"{label} with the most recorded memory: {', '.join(items)}."
            if size([*lines, line]) <= budget:
                lines.append(line)
                break
            items.pop()
    return lines if len(lines) > 1 else []
