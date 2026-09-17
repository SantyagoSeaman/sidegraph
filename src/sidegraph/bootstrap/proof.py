"""Read-only proof that a ratified bootstrap record reaches production retrieval."""

from __future__ import annotations

from collections.abc import Collection
from datetime import UTC, datetime

from sidegraph.bootstrap.model import ProofResult, ProofSelection
from sidegraph.engine.reader import GraphifyReader
from sidegraph.retrieval import Seed, get_task_context
from sidegraph.schema import DecisionKind, DecisionStatus, EntityKind
from sidegraph.store import Store

_SELECTION_RULE = (
    "accepted -> valid -> live tier-2 -> gotcha/lesson-or-rejected "
    "-> newest valid_from -> stable id -> lexicographically first non-empty concrete "
    "entity file_path"
)


def _proof_sort_key(item: ProofSelection) -> tuple[int, float, str]:
    decision = item.decision
    strong = decision.kind in (DecisionKind.GOTCHA, DecisionKind.LESSON) or bool(
        (decision.rejected or "").strip()
    )
    return (0 if strong else 1, -decision.valid_from.timestamp(), decision.id)


def _live_leaf_paths(store: Store, record_id: str) -> list[str]:
    """Concrete file paths from live tier-2 bindings, sorted for a stable seed."""
    paths: set[str] = set()
    for binding in store.bindings_for_record(record_id):
        if binding.tier != 2 or binding.status != "live":
            continue
        entity = store.get_entity(binding.entity_id)
        if entity is None or entity.kind != EntityKind.CONCRETE or entity.descriptor is None:
            continue
        file_path = entity.descriptor.file_path
        if file_path:
            paths.add(file_path)
    return sorted(paths)


def select_default_proof(
    store: Store, *, accepted_record_ids: Collection[str]
) -> ProofSelection | None:
    """Choose one current-run accepted record and its deterministic concrete anchor path."""
    now = datetime.now(UTC)
    eligible_ids = frozenset(accepted_record_ids)
    candidates: list[ProofSelection] = []
    for decision in store.iter_decisions():
        if decision.id not in eligible_ids:
            continue
        if decision.status != DecisionStatus.ACCEPTED:
            continue
        if decision.valid_from > now:
            continue
        if decision.valid_to is not None and decision.valid_to <= now:
            continue
        paths = _live_leaf_paths(store, decision.id)
        if paths:
            candidates.append(
                ProofSelection(decision=decision, file_path=paths[0], rule=_SELECTION_RULE)
            )
    return min(candidates, key=_proof_sort_key) if candidates else None


def prove_task_context(
    store: Store,
    reader: GraphifyReader,
    *,
    accepted_record_ids: Collection[str],
    file_path: str | None = None,
) -> ProofResult:
    """Prove that the deterministic record appears through production task retrieval."""
    selection = select_default_proof(store, accepted_record_ids=accepted_record_ids)
    if selection is None:
        return ProofResult(complete=False, reason="no eligible accepted decision")

    anchor_path = file_path or selection.file_path
    context = get_task_context([Seed(file_path=anchor_path)], store, reader)
    if selection.decision.id not in context.shown_ids:
        return ProofResult(complete=False, reason="selected decision did not surface")

    rendered = context.render(include_structure=False)
    primary_line = next(
        (line for line in rendered.splitlines() if selection.decision.id in line), None
    )
    if primary_line is None:
        return ProofResult(complete=False, reason="selected decision did not surface")

    return ProofResult(
        complete=True,
        primary_line=primary_line,
        source=selection.decision.provenance.ref,
        file_path=anchor_path,
        selection_rule=selection.rule,
        full_context=rendered,
        copyable_prompt=(
            f"Call get_task_context for {anchor_path} and explain why record "
            f"{selection.decision.id} applies before editing."
        ),
    )
