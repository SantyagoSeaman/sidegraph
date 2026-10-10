"""Pure, bounded preflight for untrusted capture inputs, before redaction or validation.

Mapping keys, stored model fields, and extras all count as input. No serializer or
validator runs here. The caller must complete request preflight before any write.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum

from pydantic import BaseModel

MAX_FIELD_BYTES = 256 * 1024
MAX_REQUEST_BYTES = 4 * 1024 * 1024
MAX_DRAFTS = 100
MAX_DEPTH = 32
MAX_ITEM_NODES = 4096
MAX_REQUEST_NODES = 65_536
MAX_IMPORT_BYTES = 8 * 1024 * 1024


class InputLimitError(ValueError):
    """Safe policy rejection: never include input values, keys, or encoding errors."""

    def __init__(self) -> None:
        super().__init__("Input exceeds resource limits.")


@dataclass(frozen=True)
class InputMeasurement:
    """UTF-8 string bytes and visited nodes for a completely admitted item."""

    string_bytes: int = 0
    nodes: int = 0


def _string_bytes(text: str, limit: int) -> int:
    # Every Unicode code point requires at least one byte. Avoid encoding huge rejected
    # inputs; even a rejected multibyte input can allocate at most four times this cap.
    if len(text) > limit:
        raise InputLimitError()
    try:
        size = len(text.encode("utf-8"))
    except UnicodeEncodeError:
        raise InputLimitError() from None
    if size > limit:
        raise InputLimitError()
    return size


def check_document_text(text: str, *, limit: int = MAX_IMPORT_BYTES) -> None:
    """Reject invalid UTF-8 or a document exceeding its byte cap, without field limits."""
    _string_bytes(text, limit)


def measure_item(value: object) -> InputMeasurement:
    """Bound one draft or direct argument and measure it without serializing models.

    Root depth is zero; a leaf at depth 32 is admitted. Each scalar/container visit,
    including a mapping key, counts as a node. Model storage keys count too, including
    unknown fields inserted by ``model_copy``. Shared subtrees count per occurrence;
    only cycles on the current traversal path are rejected. Other opaque Python types
    are rejected without invoking iterators or coercions. Private model attributes
    are bookkeeping, not draft input, and are not traversed.
    """
    nodes = 0
    string_bytes = 0
    active: set[int] = set()

    def walk(item: object, depth: int) -> None:
        nonlocal nodes, string_bytes
        if depth > MAX_DEPTH or nodes >= MAX_ITEM_NODES:
            raise InputLimitError()
        nodes += 1
        if isinstance(item, str):
            string_bytes += _string_bytes(item, MAX_FIELD_BYTES)
            return
        if isinstance(item, Enum):
            # Numeric enums become strings in Pydantic string fields, unlike plain
            # numbers. Reject non-string enums before any value access/conversion.
            raise InputLimitError()
        if isinstance(item, (bytes, bytearray)):
            # Pydantic accepts these as string inputs: count their encoded bytes before
            # it decodes them, and reject invalid UTF-8 without reflecting the value.
            if len(item) > MAX_FIELD_BYTES:
                raise InputLimitError()
            try:
                item.decode("utf-8")
            except UnicodeDecodeError:
                raise InputLimitError() from None
            string_bytes += len(item)
            return
        if isinstance(item, BaseModel):
            # Stored values avoid computed fields, property access, and serializers.
            fields = item.__dict__
            extras = item.__pydantic_extra__
            size = len(fields) + (len(extras) if extras is not None else 0)
            children = size * 2
        elif isinstance(item, Mapping):
            children = len(item) * 2
        elif isinstance(item, (list, tuple)):
            children = len(item)
        elif item is None or isinstance(item, (int, float)):
            return
        else:
            # Lax Pydantic coercion accepts other containers and Enum values. They
            # cannot be admitted as empty scalars: reject without consuming them.
            raise InputLimitError()
        # Check width and depth before obtaining an iterator or descending.
        if children > MAX_ITEM_NODES - nodes or (children and depth >= MAX_DEPTH):
            raise InputLimitError()
        identity = id(item)
        if identity in active:
            raise InputLimitError()
        active.add(identity)
        try:
            if isinstance(item, BaseModel):
                for key, child in fields.items():
                    walk(key, depth + 1)
                    walk(child, depth + 1)
                if extras is not None:
                    for key, child in extras.items():
                        walk(key, depth + 1)
                        walk(child, depth + 1)
            elif isinstance(item, Mapping):
                for key, child in item.items():
                    walk(key, depth + 1)
                    walk(child, depth + 1)
            else:
                for child in item:
                    walk(child, depth + 1)
        finally:
            active.remove(identity)

    walk(value, 0)
    return InputMeasurement(string_bytes, nodes)


def _admit(total: InputMeasurement, item: InputMeasurement) -> InputMeasurement:
    combined = InputMeasurement(total.string_bytes + item.string_bytes, total.nodes + item.nodes)
    if combined.string_bytes > MAX_REQUEST_BYTES or combined.nodes > MAX_REQUEST_NODES:
        raise InputLimitError()
    return combined


def _attached_fact_count(draft: object) -> int:
    if isinstance(draft, BaseModel):
        facts = draft.__dict__.get("facts")
    elif isinstance(draft, Mapping):
        facts = draft.get("facts")
    else:
        return 0
    # Malformed scalar/mapping facts fields remain item validation errors. A sequence's
    # length is sufficient for counting, even if every child is malformed or rejected.
    return len(facts) if isinstance(facts, (list, tuple)) else 0


def preflight_drafts(
    drafts: Sequence[object],
    *,
    decision_indices: Collection[int] = (),
    metadata: Sequence[object] = (),
) -> tuple[InputLimitError | None, ...]:
    """Preflight all drafts, isolating item errors and sharing admitted request budgets.

    Designate decision positions explicitly: ``range(len(decisions))`` for decisions
    alone or for a flattened ``[*decisions, *facts]`` MCP request. Only designated
    decisions' attached facts count toward the 100-draft cap. Rejected parents and
    malformed children still spend this draft count, but rejected top-level items
    spend neither admitted strings nor nodes. Request metadata (author/session/ref,
    for example) shares the same budgets, and metadata errors reject the request.
    Request-wide limits raise, so the caller
    must run this function to completion before writing any admitted sibling.
    """
    if not isinstance(drafts, (list, tuple)) or not isinstance(metadata, (list, tuple)):
        raise InputLimitError()
    draft_count = len(drafts)
    if draft_count > MAX_DRAFTS:
        raise InputLimitError()
    # Count all attached drafts first, including parents rejected by the later walk.
    for index, draft in enumerate(drafts):
        if index in decision_indices:
            draft_count += _attached_fact_count(draft)
            if draft_count > MAX_DRAFTS:
                raise InputLimitError()
    if len(metadata) > MAX_REQUEST_NODES:
        raise InputLimitError()
    total = InputMeasurement()
    for value in metadata:
        total = _admit(total, measure_item(value))
    errors: list[InputLimitError | None] = []
    for draft in drafts:
        try:
            measured = measure_item(draft)
        except InputLimitError as error:
            errors.append(error)
            continue
        total = _admit(total, measured)
        errors.append(None)
    return tuple(errors)


def preflight_direct(*values: object) -> InputMeasurement:
    """Check each direct-write argument plus shared byte/node totals; raise on any error.

    Pass every supplied value, including metadata, tags, descriptors, and provenance,
    before redaction, anchor validation, or mutation. Each argument has the same item
    ceilings as a draft; direct writes have no draft-count policy.
    """
    total = InputMeasurement()
    for value in values:
        total = _admit(total, measure_item(value))
    return total
