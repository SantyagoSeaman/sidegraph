"""Show abandoned approaches for files with anchored decisions.

See design/superpowers/specs/2026-11-20-sessionstart-briefing-design.md (B5).
"""

import json

from ..context import BriefingContext
from ..lines import clip, fit_lines, title_line


def render(ctx: BriefingContext, budget: int) -> list[str]:
    """Render one line per decision with a rejected approach."""
    if ctx.working_set:
        files = ctx.working_set
        heading = "## Tried and abandoned in the files you are working on"
    else:
        # The hot index can pass through a malformed descriptor's non-string path.
        ranked = sorted(
            (-sum(record.record_type == "decision" for record in records), path)
            for path, records in ctx.by_file.items()
            if isinstance(path, str)
        )
        files = tuple(path for _, path in ranked[:3])
        heading = "## Tried and abandoned in the most-recorded files"

    entries: list[str] = []
    entry_paths: list[str] = []
    seen: set[str] = set()
    for path in files:
        for record in ctx.by_file[path]:
            if record.record_type != "decision":
                continue
            row = record.decision
            if row["id"] in seen:
                continue
            rejected = clip(row.get("rejected"), 220)
            if not rejected:
                continue
            seen.add(row["id"])
            title = title_line(row, ctx.drifted, limit=70).removeprefix("- ")
            entries.append(f"- Abandoned: {rejected} — {title}")
            entry_paths.append(path)

    def more(omitted: int) -> str:
        paths: list[str] = []
        for index in range(len(entries) - omitted, len(entries)):
            path = entry_paths[index]
            if path not in paths:
                paths.append(path)
                if len(paths) == 3:
                    break
        # Escape malformed filename bytes without losing ordinary non-ASCII characters.
        files_json = json.dumps(paths, ensure_ascii=False).encode("utf-8", "backslashreplace")
        return f"{omitted} more: get_task_context(files={files_json.decode('utf-8')})."

    return fit_lines(heading, entries, budget, more)
