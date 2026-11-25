"""Open threads. See design/superpowers/specs/2026-11-20-sessionstart-briefing-design.md (B6)."""

import json
from datetime import datetime

from ..context import BriefingContext
from ..lines import size, title_line


def render(ctx: BriefingContext, budget: int) -> list[str]:
    """Place the drifted, recorded and queue parts in order as room permits."""
    lines = ["## Open threads"]
    drifted: list[tuple[str, dict]] = []
    seen: set[str] = set()
    for path in ctx.working_set:
        for record in ctx.by_file[path]:
            if record.record_type != "decision":
                continue
            row = record.decision
            record_id = row["id"]
            if record_id in ctx.drifted and record_id not in seen:
                seen.add(record_id)
                drifted.append((path, row))

    if drifted:
        part = [
            "Drifted records in the files you are working on:",
            *(title_line(row, ctx.drifted) for _, row in drifted[:5]),
        ]
        omitted = drifted[5:]
        if omitted:
            files = list(dict.fromkeys(path for path, _ in omitted))[:3]
            paths = json.dumps(files, ensure_ascii=False)
            part.append(f"{len(omitted)} more: get_task_context(files={paths}) tags them.")
        if size([*lines, *part]) <= budget:
            lines.extend(part)

    since = ctx.since
    if since is not None:
        ids = [
            row["id"] for row in ctx.accepted if datetime.fromisoformat(row["valid_from"]) >= since
        ]
        opening = (
            "Recorded in the 14 days before the newest record"
            if ctx.since_is_fallback
            else "Recorded since the last session"
        )
        for count in range(min(5, len(ids)), 0, -1):
            newest = ", ".join(ids[:count])
            recorded = f"{opening}: {len(ids)} accepted records; the newest: {newest}."
            if size([*lines, recorded]) <= budget:
                lines.append(recorded)
                break

    if ctx.queue_count:
        queue = (
            f"Awaiting ratification: {ctx.queue_count} proposed records; list_proposed lists them."
        )
        if size([*lines, queue]) <= budget:
            lines.append(queue)

    return lines if len(lines) > 1 else []
