"""Memory for files in the working set.

See design/superpowers/specs/2026-11-20-sessionstart-briefing-design.md (B2).
"""

import json

from ...hooks import _record_line, _shown_slice
from ..context import BriefingContext
from ..lines import decision_line, size, units

# Git permits line separators in paths. Keep headers and JSON calls on one line.
_LINE_BREAK_ESCAPES = str.maketrans(
    {char: json.dumps(char)[1:-1] for char in "\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029"}
)


def render(ctx: BriefingContext, budget: int) -> list[str]:
    """Render the longest prefix of whole file groups that fits with its pointer."""
    heading = "## Memory for the files you are working on"
    lines = [heading]
    used = units(heading)
    best: list[str] = []
    for count, path in enumerate(ctx.working_set, start=1):
        records = ctx.by_file[path]
        shown = _shown_slice(records)
        display_path = path.replace("\\", "\\\\").translate(_LINE_BREAK_ESCAPES)
        group = [f"Recorded for {display_path} ({len(shown)} of {len(records)}, mistakes first):"]
        group.extend(
            decision_line(record.decision, ctx.drifted, rejected=120)
            if record.record_type == "decision"
            else _record_line(record)
            for record in shown
        )
        if len(shown) < len(records):
            files = json.dumps([path], ensure_ascii=False).translate(_LINE_BREAK_ESCAPES)
            group.append(f"More: get_task_context(files={files}).")

        used += 1 + size(group)
        if used > budget:
            break
        lines.extend(group)
        omitted = len(ctx.working_set) - count
        if not omitted:
            return lines

        omitted_paths = ctx.working_set[count : count + 3]
        files = json.dumps(omitted_paths, ensure_ascii=False).translate(_LINE_BREAK_ESCAPES)
        pointer = (
            f"{omitted} more changed files have recorded memory; next: "
            f"get_task_context(files={files})."
        )
        if used + 1 + units(pointer) <= budget:
            best = [*lines, pointer]
    return best
