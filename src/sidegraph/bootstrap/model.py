from __future__ import annotations

import hashlib
from collections.abc import Mapping
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field

from sidegraph.profiles import ProfileDetection as ProfileDetection
from sidegraph.schema import Decision, DecisionKind, DecisionStatus, Descriptor


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True)


class WarningCode(StrEnum):
    MISSING_CHOICE = "missing-choice"
    MISSING_REJECTED = "missing-rejected-alternatives"
    UNRESOLVED_ANCHOR = "unresolved-anchor"
    AMBIGUOUS_ANCHOR = "ambiguous-anchor"
    CURRENT_STATE = "likely-current-state-summary"
    DUPLICATE_PLAN = "duplicate-within-plan"
    DUPLICATE_CANONICAL = "duplicate-canonical-memory"


# One consequence sentence per WarningCode (design §3.8, m3) — rendered beside the bare code
# in render_candidate so a reviewer sees what accepting will actually do, not just a label.
# A stable, documented public contract (predecessor spec §3.3): pinned by
# tests/test_bootstrap_review.py and mirrored in docs/getting-started/bootstrap.md.
#
# DUPLICATE_CANONICAL is the one load-bearing case: it fires on a REF match
# (planner.py's _enrich_candidate queries catalog.find_by_ref), never a content match, so
# its sentence must stay true across every content relationship that ref can carry —
# checked clause by clause, by construction, against all six write-path cells
# (existing accepted/proposed/rejected record x identical/changed accepted content; see
# design §3.8's table and the doc-import write seam in doc_import.py/planner.py):
#   accepted + identical  -> skipped-existing, record unchanged (bindings may still repair)
#   accepted + changed    -> superseding successor written, predecessor closed
#   proposed + identical  -> ratified in place (the accept grant from spec §3.1), any
#                            supporting facts cascade
#   proposed + changed    -> Store.drop marks the stale draft rejected, a fresh record is
#                            written with supersedes=None
#   rejected + identical  -> needs_status_change flips; a new accepted record revives it
#                            with supersedes=<rejected id>
#   rejected + changed    -> a fresh accepted record is written with supersedes=None (no
#                            open record exists to close, so the clause is vacuously true)
# Two earlier drafts of this sentence were false: one claimed accepting always "writes a
# successor that supersedes the stored record" (false for identical-vs-accepted); the next
# claimed identical content "is skipped without a write" (false for the ratify and revive
# cells, and over-claimed even for the one cell it fit, since the skipped-existing branch
# still runs _apply_doc_bindings and can write binding records).
WARNING_CONSEQUENCES: Mapping[WarningCode, str] = {
    WarningCode.MISSING_CHOICE: (
        "the document states no decision; the record would carry only context"
    ),
    WarningCode.MISSING_REJECTED: (
        "no rejected alternatives were found; the most valuable field stays empty"
    ),
    WarningCode.UNRESOLVED_ANCHOR: (
        "the anchor does not resolve; the record would not surface for that code"
    ),
    WarningCode.AMBIGUOUS_ANCHOR: (
        "several entities match; Sidegraph never guesses, so the anchor stays degraded"
    ),
    WarningCode.CURRENT_STATE: (
        "this reads as a current-state summary, not a decision with a fork"
    ),
    WarningCode.DUPLICATE_PLAN: ("another candidate in this same plan carries identical content"),
    WarningCode.DUPLICATE_CANONICAL: (
        "a record already exists for this source; accepting identical content leaves an "
        "accepted record unchanged, ratifies a pending proposal, or revives a rejected "
        "record — accepting changed content writes a replacement (closing any open record "
        "at this source), and edits made in an earlier review are not carried over"
    ),
}


class Exclusion(FrozenModel):
    path: str
    reason: str


class ScanResult(FrozenModel):
    root: str
    files: tuple[str, ...] = ()
    exclusions: tuple[Exclusion, ...] = ()
    max_bytes: int = 512_000


class SourceFingerprint(FrozenModel):
    path: str
    sha256: str


class AnchorPlan(FrozenModel):
    descriptor: Descriptor
    status: Literal["resolved", "ambiguous", "unresolved"]
    candidates: tuple[str, ...] = ()
    tier: int | None = None


class PlanIssue(FrozenModel):
    file_path: str
    ref: str | None = None
    warning: WarningCode
    detail: str


class EditableCandidate(FrozenModel):
    title: str
    context: str
    choice: str
    rejected: str | None = None
    consequences: str | None = None
    kind: DecisionKind


class BootstrapCandidate(FrozenModel):
    key: str
    file_path: str
    ref: str
    fragment: str | None = None
    source_hash: str
    title: str
    context: str
    choice: str
    rejected: str | None = None
    consequences: str | None = None
    kind: DecisionKind
    default_status: DecisionStatus
    redacted_anchor_text: str = Field(exclude=True)
    anchor_intents: tuple[Descriptor, ...] = ()
    file_anchor_intent: Descriptor | None = None
    anchors: tuple[AnchorPlan, ...] = ()
    warnings: tuple[WarningCode, ...] = ()

    @classmethod
    def from_fields(cls, **fields: Any) -> BootstrapCandidate:
        material = "\0".join(
            str(fields.get(name) or "")
            for name in (
                "ref",
                "title",
                "context",
                "choice",
                "rejected",
                "consequences",
                "kind",
            )
        )
        return cls(key=hashlib.sha256(material.encode()).hexdigest()[:16], **fields)


class BootstrapPlan(FrozenModel):
    root: str
    profile: str
    fingerprint: str
    source_fingerprints: tuple[SourceFingerprint, ...] = ()
    catalog_fingerprint: str
    graph_version: str | None = None
    candidates: tuple[BootstrapCandidate, ...] = ()
    issues: tuple[PlanIssue, ...] = ()
    files_read: tuple[str, ...] = ()
    exclusions: tuple[Exclusion, ...] = ()


class ReviewAction(StrEnum):
    ACCEPT = "accept"
    KEEP_PROPOSED = "keep-proposed"
    SKIP = "skip"


class ReviewedCandidate(FrozenModel):
    candidate: BootstrapCandidate
    action: ReviewAction
    edited: bool = False
    action_elapsed_seconds: float = Field(default=0.0, ge=0.0)


class ReviewResult(FrozenModel):
    items: tuple[ReviewedCandidate, ...]
    elapsed_seconds: float = Field(default=0.0, ge=0.0)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def has_writes(self) -> bool:
        return any(item.action != ReviewAction.SKIP for item in self.items)


class EditResult(FrozenModel):
    candidate: BootstrapCandidate
    diff: str


class RunStatus(StrEnum):
    COMPLETE = "complete"
    PARTIAL_RECOVERABLE = "partial-recoverable"
    INCOMPLETE = "incomplete"
    DIAGNOSTIC = "diagnostic"


class AcceptedRecord(FrozenModel):
    record_id: str
    ref: str


class Reconciliation(FrozenModel):
    durable: tuple[str, ...] = ()
    pending: tuple[str, ...] = ()
    canonical_files: tuple[str, ...] = ()
    durable_accepted_records: tuple[AcceptedRecord, ...] = ()


class BootstrapReport(FrozenModel):
    status: RunStatus
    documents_scanned: int
    candidates: int
    accepted: int
    kept_proposed: int
    skipped: int
    reviewed_candidates: int = 0
    accepted_without_edit: int = 0
    edited_then_accepted: int = 0
    kept_proposed_without_edit: int = 0
    edited_then_kept_proposed: int = 0
    candidate_precision_numerator: int = 0
    review_elapsed_seconds: float = 0.0
    accept_action_seconds: float = 0.0
    edit_accept_action_seconds: float = 0.0
    keep_proposed_action_seconds: float = 0.0
    edit_keep_proposed_action_seconds: float = 0.0
    skip_action_seconds: float = 0.0
    live_anchors: int
    review_debt_count: int
    oldest_proposal_days: int | None = None
    durable_candidate_keys: tuple[str, ...] = ()
    pending_candidate_keys: tuple[str, ...] = ()
    durable_accepted_records: tuple[AcceptedRecord, ...] = ()
    canonical_files: tuple[str, ...] = ()
    verification_failures: tuple[str, ...] = ()
    failed_ref: str | None = None
    error: str | None = None
    next_command: str | None = None


class HostKind(StrEnum):
    CLAUDE_CODE = "claude-code"
    CODEX = "codex"


class IntegrationResult(FrozenModel):
    host: HostKind
    mcp: Literal["verified", "missing", "invalid", "unsupported"]
    session_start: Literal["verified", "missing", "invalid", "unsupported"]
    stop: Literal["verified", "missing", "invalid", "unsupported"]
    pretool_read_grep: Literal["verified", "missing", "invalid", "unsupported"]
    fully_supported: bool
    next_action: str | None = None


class ProofSelection(FrozenModel):
    decision: Decision
    file_path: str
    rule: str


class ProofResult(FrozenModel):
    complete: bool
    primary_line: str | None = None
    source: str | None = None
    file_path: str | None = None
    selection_rule: str | None = None
    full_context: str | None = None
    copyable_prompt: str | None = None
    reason: str | None = None
