"""Interactive, side-effect-free review of bootstrap candidates."""

from __future__ import annotations

import difflib
import json
import os
import shlex
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from time import monotonic as _monotonic
from typing import TYPE_CHECKING, Any

from sidegraph.bootstrap.model import (
    WARNING_CONSEQUENCES,
    BootstrapCandidate,
    BootstrapPlan,
    EditableCandidate,
    EditResult,
    ReviewAction,
    ReviewedCandidate,
    ReviewResult,
)
from sidegraph.bootstrap.planner import replan_edited_candidate
from sidegraph.capture import redact

if TYPE_CHECKING:
    from sidegraph.bootstrap.catalog import CanonicalCatalog
    from sidegraph.engine.reader import GraphifyReader


def _editable(candidate: BootstrapCandidate) -> EditableCandidate:
    """Return the only fields an editor may see, redacting again at this boundary."""
    title, _ = redact(candidate.title)
    context, _ = redact(candidate.context)
    choice, _ = redact(candidate.choice)
    rejected = None
    if candidate.rejected is not None:
        rejected, _ = redact(candidate.rejected)
    consequences = None
    if candidate.consequences is not None:
        consequences, _ = redact(candidate.consequences)
    return EditableCandidate(
        title=title,
        context=context,
        choice=choice,
        rejected=rejected,
        consequences=consequences,
        kind=candidate.kind,
    )


def render_candidate(candidate: BootstrapCandidate) -> str:
    """Render every reviewable field through the redaction boundary."""
    editable = _editable(candidate)
    source, _ = redact(candidate.ref)
    lines = [
        f"title: {editable.title}",
        f"source: {source}",
        f"context: {editable.context}",
        f"choice: {editable.choice}",
        f"rejected: {editable.rejected or '-'}",
        f"consequences: {editable.consequences or '-'}",
    ]
    if candidate.warnings:
        for warning in candidate.warnings:
            lines.append(f"warning: {warning.value} — {WARNING_CONSEQUENCES[warning]}")
    else:
        lines.append("warnings: none")
    if candidate.anchors:
        for anchor in candidate.anchors:
            name, _ = redact(anchor.descriptor.name)
            tier = anchor.tier if anchor.tier is not None else "-"
            lines.append(f"anchor: {name} ({anchor.status}, tier {tier})")
    else:
        lines.append("anchor: -")
    return "\n".join(lines)


def render_redacted_diff(before: BootstrapCandidate, after: BootstrapCandidate) -> str:
    """Render an editor-safe unified diff without source-only candidate fields."""
    old_json = json.dumps(_editable(before).model_dump(mode="json"), indent=2, sort_keys=True)
    new_json = json.dumps(_editable(after).model_dump(mode="json"), indent=2, sort_keys=True)
    old = old_json.splitlines(keepends=True)
    new = new_json.splitlines(keepends=True)
    return "".join(difflib.unified_diff(old, new, fromfile="before", tofile="after"))


def _redact_json_text(value: object) -> object:
    """Remove secrets from parsed editor JSON before validation can render an error."""
    if isinstance(value, str):
        return redact(value)[0]
    if isinstance(value, list):
        return [_redact_json_text(item) for item in value]
    if isinstance(value, dict):
        return {
            redact(key)[0] if isinstance(key, str) else key: _redact_json_text(item)
            for key, item in value.items()
        }
    return value


def _load_redacted_editable(path: Path) -> EditableCandidate:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError("edited JSON is invalid") from error
    return EditableCandidate.model_validate(_redact_json_text(parsed))


class EditRejected(ValueError):
    """Edited content failed to parse or validate. Carries the REDACTED buffer so the next
    edit of the same candidate can reopen the user's own text instead of a fresh render."""

    def __init__(self, message: str, buffer: str) -> None:
        super().__init__(message)
        self.buffer = buffer


def edit_candidate(
    candidate: BootstrapCandidate,
    *,
    editor: str,
    reader: GraphifyReader | None = None,
    catalog: CanonicalCatalog | None = None,
    run_editor: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    seed: str | None = None,
) -> EditResult:
    """Edit redacted fields and replan; this function never writes canonical state."""
    argv = shlex.split(editor)
    if not argv:
        raise ValueError("editor command is empty")

    fd, name = tempfile.mkstemp(prefix="sidegraph-bootstrap-", suffix=".json")
    path = Path(name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            if seed is not None:
                handle.write(seed)
            else:
                json.dump(_editable(candidate).model_dump(mode="json"), handle, indent=2)
                handle.write("\n")
        completed = run_editor([*argv, str(path)])
        if completed.returncode != 0:
            raise subprocess.CalledProcessError(completed.returncode, [*argv, str(path)])
        try:
            changed = _load_redacted_editable(path)
        except ValueError as error:
            raw = path.read_text(encoding="utf-8")
            # Redact here explicitly: on a JSONDecodeError nothing was parsed, so
            # _redact_json_text never ran over this text.
            raise EditRejected(str(error), redact(raw)[0]) from error
        replanned = replan_edited_candidate(candidate, changed, reader=reader, catalog=catalog)
        diff = render_redacted_diff(candidate, replanned)
        return EditResult(candidate=replanned, diff=diff)
    finally:
        path.unlink(missing_ok=True)


def _batch_numbers(command: str, candidate_count: int, *, first: int) -> set[int] | None:
    """Parse `batch N,M`. Returns None when the command is not an unambiguous selection of
    still-unreviewed candidates — the caller reprompts rather than assuming an intent."""
    prefix = "batch "
    if not command.startswith(prefix):
        return None
    values = [value.strip() for value in command.removeprefix(prefix).split(",")]
    # isdigit() alone accepts Unicode digits (e.g. '²') that int() cannot parse and raises
    # ValueError on; require plain ASCII digits so no input can escape as an exception. This
    # also rejects non-ASCII decimal digits (e.g. Arabic-Indic '٣') that int() could parse —
    # a deliberate choice, not a side effect: candidate numbers are indices this CLI prints in
    # ASCII, so accepting another digit script would be a silent-conversion surface, not a
    # feature.
    if not values or any(not (value.isascii() and value.isdigit()) for value in values):
        return None
    numbers = {int(value) for value in values}
    if (
        len(numbers) != len(values)
        or not numbers
        or any(n < first or n > candidate_count for n in numbers)
    ):
        return None
    return numbers


def _edit_failure(detail: str) -> str:
    """Redact and clip an edit-failure message; the candidate is untouched either way. The
    whole message is clipped to at most 200 characters, not just the detail before the
    suffix is appended."""
    redacted, _ = redact(detail)
    suffix = "; candidate unchanged"
    budget = 200 - len(suffix)
    if len(redacted) > budget:
        redacted = f"{redacted[: budget - 3]}..."
    return f"{redacted}{suffix}"


def _confirm_edited_candidate(read_line: Callable[[str], str]) -> ReviewAction:
    actions = {
        "accept": ReviewAction.ACCEPT,
        "proposed": ReviewAction.KEEP_PROPOSED,
        "cancel": ReviewAction.SKIP,
    }
    while True:
        confirmation = read_line("Edited candidate: accept, proposed, or cancel? ").strip().lower()
        if confirmation in actions:
            return actions[confirmation]


def review_plan(
    plan: BootstrapPlan,
    *,
    reader: GraphifyReader | None = None,
    catalog: CanonicalCatalog | None = None,
    read_line: Callable[[str], str] = input,
    write_line: Callable[[str], None] = print,
    environ: Mapping[str, str] = os.environ,
    run_editor: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    monotonic: Callable[[], float] = _monotonic,
) -> ReviewResult:
    """Review candidates in plan order using explicit, run-local decisions only."""
    items: list[ReviewedCandidate] = []
    candidates = plan.candidates
    for number, candidate in enumerate(candidates, start=1):
        action_started = monotonic()
        seed: str | None = None
        while True:
            write_line(f"\nCandidate {number}/{len(candidates)} [{candidate.key}]")
            write_line(render_candidate(candidate))
            command = (
                read_line(
                    f"Candidate {number}/{len(candidates)}: "
                    "accept (a), proposed (p), skip (s), edit (e), "
                    "or batch 1,3? "
                )
                .strip()
                .lower()
            )
            batch = _batch_numbers(command, len(candidates), first=number)
            if command.startswith("batch"):
                if batch is None:
                    example = (
                        f"batch {number}"
                        if number == len(candidates)
                        else f"batch {number},{len(candidates)}"
                    )
                    write_line(
                        f"batch needs distinct candidate numbers between {number} and "
                        f"{len(candidates)}, for example: {example}"
                    )
                    continue
                remaining = candidates[number - 1 :]
                elapsed = max(0.0, monotonic() - action_started)
                elapsed_per_candidate = elapsed / len(remaining)
                items.extend(
                    ReviewedCandidate(
                        candidate=remaining_candidate,
                        action=(
                            ReviewAction.ACCEPT if index in batch else ReviewAction.KEEP_PROPOSED
                        ),
                        action_elapsed_seconds=elapsed_per_candidate,
                    )
                    for index, remaining_candidate in enumerate(remaining, start=number)
                )
                return ReviewResult(
                    items=tuple(items),
                    elapsed_seconds=sum(item.action_elapsed_seconds for item in items),
                )

            edited_action = False
            if command == "a":
                action = ReviewAction.ACCEPT
            elif command == "p":
                action = ReviewAction.KEEP_PROPOSED
            elif command == "e":
                editor = environ.get("VISUAL") or environ.get("EDITOR")
                if not editor:
                    write_line(
                        "Set VISUAL or EDITOR, then rerun sidegraph-bootstrap --candidate "
                        f"{candidate.key}"
                    )
                    action = ReviewAction.KEEP_PROPOSED
                else:
                    try:
                        edited = edit_candidate(
                            candidate,
                            editor=editor,
                            reader=reader,
                            catalog=catalog,
                            run_editor=run_editor,
                            seed=seed,
                        )
                    except subprocess.CalledProcessError as error:
                        write_line(
                            _edit_failure(f"edit cancelled by the editor (exit {error.returncode})")
                        )
                        seed = None  # cancel means cancel: discard the buffer
                        continue
                    except OSError as error:
                        write_line(_edit_failure(f"editor step failed: {error.strerror or error}"))
                        seed = None  # transient failure: reopen a fresh render, not stale text
                        continue
                    except ValueError as error:
                        # EditRejected carries the redacted buffer; a plain ValueError does not.
                        seed = getattr(error, "buffer", None)
                        write_line(_edit_failure(f"edited candidate was not valid: {error}"))
                        continue
                    write_line(edited.diff)
                    action = _confirm_edited_candidate(read_line)
                    if action != ReviewAction.SKIP:
                        candidate = edited.candidate
                        edited_action = True
            else:
                action = ReviewAction.SKIP
            break
        seed = None
        elapsed = max(0.0, monotonic() - action_started)
        items.append(
            ReviewedCandidate(
                candidate=candidate,
                action=action,
                edited=edited_action,
                action_elapsed_seconds=elapsed,
            )
        )
    return ReviewResult(
        items=tuple(items),
        elapsed_seconds=sum(item.action_elapsed_seconds for item in items),
    )
