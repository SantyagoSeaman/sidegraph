"""Confirmed bootstrap writes, recovery reconciliation, and aggregate reporting."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from ulid import ULID

from sidegraph.bootstrap.catalog import (
    CanonicalCatalog,
    fingerprint_catalog,
    load_canonical_catalog,
)
from sidegraph.bootstrap.model import (
    AcceptedRecord,
    BootstrapPlan,
    BootstrapReport,
    IntegrationResult,
    ProofResult,
    Reconciliation,
    ReviewAction,
    ReviewedCandidate,
    ReviewResult,
    RunStatus,
)
from sidegraph.doc_import import DocWriteRequest, ParsedDoc, apply_doc_candidate
from sidegraph.engine.reader import GraphifyReader
from sidegraph.profiles import FlowProfile, get_profile
from sidegraph.schema import Decision, DecisionStatus, DomainStatus
from sidegraph.store import Store
from sidegraph.verify import verify_snapshot

_CANONICAL_DIRS = (
    "decisions",
    "facts",
    "domains",
    "entities",
    "bindings",
    "initiatives",
    "archive",
)
_CANONICAL_ROOT_FILES = ("format", ".gitignore")


def _error_text(error: BaseException) -> str:
    return str(error) or type(error).__name__


def canonical_manifest(store_dir: Path) -> dict[str, str]:
    """Hash committed store files, excluding derived and temporary state."""
    store_dir = Path(store_dir)
    if not store_dir.is_dir():
        return {}

    paths: list[Path] = []
    for name in _CANONICAL_ROOT_FILES:
        path = store_dir / name
        if path.is_file() and not path.is_symlink():
            paths.append(path)
    for name in _CANONICAL_DIRS:
        root = store_dir / name
        if not root.is_dir():
            continue
        paths.extend(
            path
            for path in root.rglob("*")
            if path.is_file() and not path.is_symlink() and not path.name.endswith(".tmp")
        )

    store_label = store_dir.name
    return {
        f"{store_label}/{path.relative_to(store_dir).as_posix()}": hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in sorted(paths)
    }


def validate_plan_inputs(
    plan: BootstrapPlan,
    store_dir: Path,
    reader: GraphifyReader,
) -> tuple[str, ...]:
    """Validate all preview inputs without constructing persistent state."""
    failures: list[str] = []
    root = Path(plan.root)

    for source in plan.source_fingerprints:
        path = root / source.path
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as error:
            failures.append(f"source unavailable: {source.path}: {_error_text(error)}")
            continue
        if digest != source.sha256:
            failures.append(f"source changed after preview: {source.path}")

    try:
        current_catalog = fingerprint_catalog(load_canonical_catalog(Path(store_dir)))
    except (OSError, ValueError) as error:
        failures.append(f"canonical catalog unavailable: {_error_text(error)}")
    else:
        if current_catalog != plan.catalog_fingerprint:
            failures.append("canonical catalog changed after preview")

    try:
        current_graph = GraphifyReader(reader.path).graph_version()
    except (OSError, UnicodeError, ValueError) as error:
        failures.append(f"graph unavailable: {reader.path}: {_error_text(error)}")
    else:
        if current_graph != plan.graph_version:
            failures.append("graph changed after preview")

    return tuple(failures)


def build_doc_request(
    item: ReviewedCandidate,
    graph_version: str | None,
    profile: FlowProfile,
) -> DocWriteRequest:
    """Convert only explicitly write-approved, already-redacted review state.

    ``rel_path`` (E2, design note §6) is derived from ``profile.normalized_rel_path`` of
    the candidate's ON-DISK ``file_path`` — never the on-disk path itself — so the
    resulting ``request.ref`` equals ``candidate.ref`` (also normalized, at plan time) by
    construction: both derive from the same transform applied to the same input.
    """
    if item.action == ReviewAction.SKIP:
        raise ValueError(f"skipped candidate cannot be converted: {item.candidate.ref}")

    candidate = item.candidate
    status = (
        DecisionStatus.ACCEPTED if item.action == ReviewAction.ACCEPT else DecisionStatus.PROPOSED
    )
    return DocWriteRequest(
        parsed=ParsedDoc(
            title=candidate.title,
            context=candidate.context,
            choice=candidate.choice,
            rejected=candidate.rejected,
            consequences=candidate.consequences,
            frontmatter_status=None,
            suggested_kind=candidate.kind,
            fragment=candidate.fragment,
        ),
        rel_path=profile.normalized_rel_path(candidate.file_path),
        source_hash=candidate.source_hash,
        status=status,
        anchors=candidate.anchor_intents,
        file_anchor=candidate.file_anchor_intent,
        graph_version=graph_version,
        # The human chose `accept` in the review loop; this is the only place in the codebase
        # that grants the ratification authority (spec §3.1).
        ratify_matching_proposal=item.action == ReviewAction.ACCEPT,
    )


def _matching_reviewed_decision(
    item: ReviewedCandidate, catalog: CanonicalCatalog
) -> Decision | None:
    candidate = item.candidate
    expected_status = (
        DecisionStatus.ACCEPTED if item.action == ReviewAction.ACCEPT else DecisionStatus.PROPOSED
    )
    expected_content = (
        candidate.title,
        candidate.context,
        candidate.choice,
        candidate.rejected,
        candidate.consequences,
    )
    return next(
        (
            decision
            for decision in catalog.decisions
            if decision.provenance.source == "doc-import"
            and decision.provenance.ref == candidate.ref
            and decision.status == expected_status
            and (
                decision.title,
                decision.context,
                decision.choice,
                decision.rejected,
                decision.consequences,
            )
            == expected_content
        ),
        None,
    )


def reconcile_plan(
    plan: BootstrapPlan,
    review: ReviewResult,
    store_dir: Path,
    before: Mapping[str, str],
) -> Reconciliation:
    """Compare reviewed writes with canonical truth after reopen/rebuild."""
    del plan  # Review items carry the possibly edited candidates that are authoritative here.
    catalog = load_canonical_catalog(Path(store_dir))
    durable: list[str] = []
    pending: list[str] = []
    accepted_records: list[AcceptedRecord] = []
    for item in review.items:
        if item.action == ReviewAction.SKIP:
            continue
        match = _matching_reviewed_decision(item, catalog)
        target = durable if match is not None else pending
        target.append(item.candidate.key)
        if match is not None and item.action == ReviewAction.ACCEPT:
            accepted_records.append(AcceptedRecord(record_id=match.id, ref=item.candidate.ref))

    after = canonical_manifest(Path(store_dir))
    changed = tuple(
        sorted(path for path in set(before) | set(after) if before.get(path) != after.get(path))
    )
    return Reconciliation(
        durable=tuple(durable),
        pending=tuple(pending),
        canonical_files=changed,
        durable_accepted_records=tuple(accepted_records),
    )


def proposal_debt(catalog: CanonicalCatalog) -> tuple[int, int | None]:
    """Return proposed-record count and the age of the oldest parseable ULID."""
    proposed_ids = [
        decision.id for decision in catalog.decisions if decision.status == DecisionStatus.PROPOSED
    ]
    proposed_ids.extend(fact.id for fact in catalog.facts if fact.status == DecisionStatus.PROPOSED)
    proposed_ids.extend(
        domain.domain_id for domain in catalog.domains if domain.status == DomainStatus.PROPOSED
    )

    created: list[datetime] = []
    for record_id in proposed_ids:
        try:
            created.append(ULID.from_str(record_id).datetime)
        except (TypeError, ValueError):
            continue
    if not created:
        return len(proposed_ids), None
    age = datetime.now(UTC) - min(created)
    return len(proposed_ids), max(0, age.days)


def _counts(plan: BootstrapPlan, review: ReviewResult) -> dict[str, int | float]:
    accepted_without_edit = sum(
        item.action == ReviewAction.ACCEPT and not item.edited for item in review.items
    )
    edited_then_accepted = sum(
        item.action == ReviewAction.ACCEPT and item.edited for item in review.items
    )
    kept_proposed_without_edit = sum(
        item.action == ReviewAction.KEEP_PROPOSED and not item.edited for item in review.items
    )
    edited_then_kept_proposed = sum(
        item.action == ReviewAction.KEEP_PROPOSED and item.edited for item in review.items
    )
    return {
        "documents_scanned": len(plan.files_read),
        "candidates": len(plan.candidates),
        "accepted": sum(item.action == ReviewAction.ACCEPT for item in review.items),
        "kept_proposed": sum(item.action == ReviewAction.KEEP_PROPOSED for item in review.items),
        "skipped": sum(item.action == ReviewAction.SKIP for item in review.items),
        "reviewed_candidates": len(review.items),
        "accepted_without_edit": accepted_without_edit,
        "edited_then_accepted": edited_then_accepted,
        "kept_proposed_without_edit": kept_proposed_without_edit,
        "edited_then_kept_proposed": edited_then_kept_proposed,
        "candidate_precision_numerator": sum(
            item.action == ReviewAction.ACCEPT or item.edited for item in review.items
        ),
        "review_elapsed_seconds": review.elapsed_seconds,
        "accept_action_seconds": sum(
            item.action_elapsed_seconds
            for item in review.items
            if item.action == ReviewAction.ACCEPT and not item.edited
        ),
        "edit_accept_action_seconds": sum(
            item.action_elapsed_seconds
            for item in review.items
            if item.action == ReviewAction.ACCEPT and item.edited
        ),
        "keep_proposed_action_seconds": sum(
            item.action_elapsed_seconds
            for item in review.items
            if item.action == ReviewAction.KEEP_PROPOSED and not item.edited
        ),
        "edit_keep_proposed_action_seconds": sum(
            item.action_elapsed_seconds
            for item in review.items
            if item.action == ReviewAction.KEEP_PROPOSED and item.edited
        ),
        "skip_action_seconds": sum(
            item.action_elapsed_seconds for item in review.items if item.action == ReviewAction.SKIP
        ),
        "live_anchors": sum(
            any(anchor.status == "resolved" and anchor.tier == 2 for anchor in candidate.anchors)
            for candidate in plan.candidates
        ),
    }


def _pending_keys(review: ReviewResult) -> tuple[str, ...]:
    return tuple(item.candidate.key for item in review.items if item.action != ReviewAction.SKIP)


def _incomplete_report(
    plan: BootstrapPlan,
    review: ReviewResult,
    store_dir: Path,
    *,
    error: str,
    before: Mapping[str, str] | None = None,
) -> BootstrapReport:
    try:
        debt_count, oldest_days = proposal_debt(load_canonical_catalog(store_dir))
    except (OSError, ValueError):
        debt_count, oldest_days = 0, None
    canonical_files: tuple[str, ...] = ()
    if before is not None:
        try:
            after = canonical_manifest(store_dir)
            canonical_files = tuple(
                sorted(
                    path for path in set(before) | set(after) if before.get(path) != after.get(path)
                )
            )
        except OSError:
            pass
    return BootstrapReport(
        status=RunStatus.INCOMPLETE,
        **_counts(plan, review),
        review_debt_count=debt_count,
        oldest_proposal_days=oldest_days,
        pending_candidate_keys=_pending_keys(review),
        canonical_files=canonical_files,
        error=error,
        next_command="sidegraph-bootstrap --resume",
    )


def _diagnostic_report(
    plan: BootstrapPlan,
    review: ReviewResult,
    store_dir: Path,
) -> BootstrapReport:
    try:
        debt_count, oldest_days = proposal_debt(load_canonical_catalog(store_dir))
    except (OSError, ValueError):
        debt_count, oldest_days = 0, None
    return BootstrapReport(
        status=RunStatus.DIAGNOSTIC,
        **_counts(plan, review),
        review_debt_count=debt_count,
        oldest_proposal_days=oldest_days,
    )


def _finalize(
    plan: BootstrapPlan,
    review: ReviewResult,
    store_dir: Path,
    before: Mapping[str, str],
    error: str | None,
    failed_ref: str | None,
) -> BootstrapReport:
    reopen_error: str | None = None
    try:
        healed = Store(store_dir)
        healed.close()
    except BaseException as exc:
        reopen_error = _error_text(exc)

    verification_errors: tuple[str, ...] = ()
    if reopen_error is None:
        try:
            violations = tuple(verify_snapshot(store_dir))
            verification_errors = tuple(
                f"{violation.code} {violation.path} {violation.detail}" for violation in violations
            )
        except BaseException as exc:
            verification_errors = (f"verification failed: {_error_text(exc)}",)

    reconciliation_error: str | None = None
    try:
        reconciliation = reconcile_plan(plan, review, store_dir, before)
    except (OSError, ValueError) as exc:
        reconciliation_error = _error_text(exc)
        try:
            after = canonical_manifest(store_dir)
            changed = tuple(
                sorted(
                    path for path in set(before) | set(after) if before.get(path) != after.get(path)
                )
            )
        except OSError:
            changed = ()
        reconciliation = Reconciliation(pending=_pending_keys(review), canonical_files=changed)

    complete = (
        not error
        and not reopen_error
        and not verification_errors
        and not reconciliation_error
        and not reconciliation.pending
    )
    status = (
        RunStatus.COMPLETE
        if complete
        else RunStatus.PARTIAL_RECOVERABLE
        if reconciliation.durable
        else RunStatus.INCOMPLETE
    )
    try:
        debt_count, oldest_days = proposal_debt(load_canonical_catalog(store_dir))
    except (OSError, ValueError):
        debt_count, oldest_days = 0, None
    report_error = error or reopen_error or reconciliation_error
    if report_error is None and verification_errors:
        report_error = verification_errors[0]
    return BootstrapReport(
        status=status,
        **_counts(plan, review),
        review_debt_count=debt_count,
        oldest_proposal_days=oldest_days,
        durable_candidate_keys=reconciliation.durable,
        pending_candidate_keys=reconciliation.pending,
        durable_accepted_records=reconciliation.durable_accepted_records,
        canonical_files=reconciliation.canonical_files,
        verification_failures=verification_errors,
        failed_ref=failed_ref,
        error=report_error,
        next_command=(
            None
            if complete
            else f"sidegraph-verify --db {store_dir}"
            if verification_errors
            else "sidegraph-bootstrap --resume"
        ),
    )


def apply_review(
    plan: BootstrapPlan,
    review: ReviewResult,
    *,
    store_dir: Path,
    reader: GraphifyReader,
) -> BootstrapReport:
    """Apply confirmed review items and reconcile any durable partial writes."""
    store_dir = Path(store_dir)
    failures = validate_plan_inputs(plan, store_dir, reader)
    if failures:
        return _incomplete_report(
            plan,
            review,
            store_dir,
            error="; ".join(failures),
        )
    if not review.has_writes:
        return _diagnostic_report(plan, review, store_dir)

    try:
        before = canonical_manifest(store_dir)
    except OSError as exc:
        return _incomplete_report(plan, review, store_dir, error=_error_text(exc))

    try:
        store = Store(store_dir)
    except BaseException as exc:
        return _incomplete_report(
            plan,
            review,
            store_dir,
            error=_error_text(exc),
            before=before,
        )

    error: str | None = None
    failed_ref: str | None = None
    profile = get_profile(plan.profile)
    try:
        for item in review.items:
            if item.action == ReviewAction.SKIP:
                continue
            request = build_doc_request(item, plan.graph_version, profile)
            if request.ref != item.candidate.ref:
                raise AssertionError(
                    f"reviewed candidate ref changed during conversion: {item.candidate.ref}"
                )
            try:
                apply_doc_candidate(store, reader, request)
            except BaseException as exc:
                error = _error_text(exc)
                failed_ref = item.candidate.ref
                break
    finally:
        try:
            store.close()
        except BaseException as exc:
            if error is None:
                error = _error_text(exc)

    return _finalize(plan, review, store_dir, before, error, failed_ref)


def render_markdown_report(
    report: BootstrapReport,
    *,
    integration: IntegrationResult | None = None,
    proof: ProofResult | None = None,
    task_proof: ProofResult | None = None,
    elapsed_seconds: float | None = None,
) -> str:
    """Render operational aggregates without candidate/source/error content."""
    reviewed = report.reviewed_candidates

    def rate(count: int) -> str:
        if reviewed == 0:
            return "not applicable (0 reviewed)"
        return f"{count}/{reviewed} ({count / reviewed:.1%})"

    lines = [
        "# Sidegraph bootstrap report",
        "",
        f"- status: {report.status.value}",
        f"- documents scanned: {report.documents_scanned}",
        f"- candidates: {report.candidates}",
        f"- accepted: {report.accepted}",
        f"- kept proposed: {report.kept_proposed}",
        f"- skipped: {report.skipped}",
        f"- reviewed candidates: {reviewed}",
        f"- candidate precision: {rate(report.candidate_precision_numerator)}",
        f"- accept: {rate(report.accepted_without_edit)}; "
        f"action seconds: {report.accept_action_seconds:.3f}",
        f"- edit then accept: {rate(report.edited_then_accepted)}; "
        f"action seconds: {report.edit_accept_action_seconds:.3f}",
        f"- keep proposed: {rate(report.kept_proposed_without_edit)}; "
        f"action seconds: {report.keep_proposed_action_seconds:.3f}",
        f"- edit then keep proposed: {rate(report.edited_then_kept_proposed)}; "
        f"action seconds: {report.edit_keep_proposed_action_seconds:.3f}",
        f"- skip: {rate(report.skipped)}; action seconds: {report.skip_action_seconds:.3f}",
        f"- review elapsed seconds: {report.review_elapsed_seconds:.3f}",
        f"- live anchors: {report.live_anchors}",
        f"- review debt (proposed): {report.review_debt_count}",
        f"- oldest proposal days: {report.oldest_proposal_days}",
        f"- durable candidates: {len(report.durable_candidate_keys)}",
        f"- pending candidates: {len(report.pending_candidate_keys)}",
        f"- verification failures: {len(report.verification_failures)}",
    ]
    if elapsed_seconds is not None:
        lines.append(f"- elapsed seconds: {elapsed_seconds:.3f}")
    if report.next_command is not None:
        lines.append(f"- next command: `{report.next_command}`")

    if report.canonical_files:
        lines.extend(("", "## Changed canonical files", ""))
        lines.extend(f"- `{path}`" for path in report.canonical_files)

    if integration is not None:
        lines.extend(
            (
                "",
                "## Integration",
                "",
                f"- host: {integration.host.value}",
                f"- fully supported: {str(integration.fully_supported).lower()}",
                f"- mcp: {integration.mcp}",
                f"- session start: {integration.session_start}",
                f"- stop: {integration.stop}",
                f"- pretool read/grep: {integration.pretool_read_grep}",
            )
        )
        if integration.next_action is not None:
            lines.append(f"- next action: {integration.next_action}")

    if proof is not None:
        lines.extend(
            (
                "",
                "## Proof",
                "",
                f"- complete: {str(proof.complete).lower()}",
            )
        )

    if task_proof is not None:
        lines.extend(
            (
                "",
                "## Optional task proof",
                "",
                f"- complete: {str(task_proof.complete).lower()}",
            )
        )

    return "\n".join(lines) + "\n"
