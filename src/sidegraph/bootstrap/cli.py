"""Guided, preview-first orchestration for repository bootstrap."""

from __future__ import annotations

import argparse
import os
import shlex
import sys
import traceback
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import NoReturn

from sidegraph.bootstrap.apply import apply_review, render_markdown_report
from sidegraph.bootstrap.catalog import load_canonical_catalog
from sidegraph.bootstrap.integrations import verify_integration
from sidegraph.bootstrap.model import (
    BootstrapCandidate,
    BootstrapPlan,
    BootstrapReport,
    HostKind,
    IntegrationResult,
    ProfileDetection,
    ProofResult,
    ReviewAction,
    ReviewResult,
    RunStatus,
)
from sidegraph.bootstrap.planner import plan_sources
from sidegraph.bootstrap.proof import prove_task_context
from sidegraph.bootstrap.review import render_candidate, review_plan
from sidegraph.bootstrap.scan import scan_sources
from sidegraph.capture import redact
from sidegraph.config import resolve_store_path
from sidegraph.domains import collect_domain_candidates
from sidegraph.engine.reader import GraphifyReader
from sidegraph.profiles import FlowProfile, detect_profile, get_profile
from sidegraph.store import Store


class BootstrapArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        self.exit(1, f"{self.prog}: error: {message}\n")


@dataclass(frozen=True)
class DomainSuggestions:
    count: int = 0
    titles: tuple[str, ...] = ()


def build_parser() -> BootstrapArgumentParser:
    parser = BootstrapArgumentParser(
        prog="sidegraph-bootstrap",
        description="Preview, review, and explicitly confirm Sidegraph onboarding.",
    )
    parser.add_argument("--root", default=".", help="repository root (default: current directory)")
    parser.add_argument("--db", help="Sidegraph store directory")
    parser.add_argument("--graph", help="Graphify graph.json path")
    parser.add_argument("--profile", help="flow profile name")
    parser.add_argument("--docs", action="append", help="additional document or directory")
    parser.add_argument("--include", action="append", help="explicitly include a source file")
    parser.add_argument(
        "--host",
        choices=tuple(host.value for host in HostKind),
        default=HostKind.CLAUDE_CODE.value,
    )
    parser.add_argument("--codex-config", help="Codex config.toml path")
    parser.add_argument("--candidate", help="review only one candidate key")
    parser.add_argument("--task", help="repository file path used for retrieval proof")
    parser.add_argument("--report", help="write an aggregate Markdown report")
    parser.add_argument(
        "--resume",
        action="store_true",
        help=(
            "marker for a resumed run; the flow is idempotent, so a rerun behaves identically "
            "with or without it"
        ),
    )
    return parser


def _resolve_graph_path(value: str | None, root: Path) -> Path:
    raw = value or os.environ.get("SIDEGRAPH_GRAPH") or "graphify-out/graph.json"
    path = Path(raw)
    return path if path.is_absolute() else root / path


def _resolve_optional_path(value: str | None, root: Path) -> Path | None:
    if value is None:
        return None
    path = Path(value)
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def _selected_host_config_inputs(
    args: argparse.Namespace, root: Path
) -> tuple[tuple[str, Path], ...]:
    if args.host == HostKind.CLAUDE_CODE.value:
        return (
            ("Claude Code MCP config", (root / ".mcp.json").resolve()),
            ("Claude Code hooks config", (root / ".claude" / "settings.json").resolve()),
        )
    codex_config = _resolve_optional_path(args.codex_config, root)
    return (
        ("Codex config", codex_config or (root / ".codex" / "config.toml").resolve()),
        ("Codex hooks config", (root / ".codex" / "hooks" / "hooks.json").resolve()),
    )


def _validate_report_destination(
    args: argparse.Namespace,
    root: Path,
    store_dir: Path,
    graph_path: Path,
    source_paths: tuple[str, ...] = (),
) -> Path | None:
    report_path = _resolve_optional_path(args.report, root)
    if report_path is None:
        return None
    protected_files = (
        ("graph input", graph_path.resolve()),
        *_selected_host_config_inputs(args, root),
        *(
            (f"scanned source {source_path}", (root / source_path).resolve())
            for source_path in source_paths
        ),
    )
    for label, protected_path in protected_files:
        if report_path == protected_path:
            raise ValueError(
                f"--report destination conflicts with protected {label}: {protected_path}"
            )
    try:
        report_path.relative_to(store_dir.resolve())
    except ValueError:
        pass
    else:
        raise ValueError(
            f"--report destination conflicts with protected Sidegraph store: {store_dir}"
        )
    return report_path


def require_selected_profile(detection: ProfileDetection) -> FlowProfile:
    if detection.selected is not None:
        return get_profile(detection.selected)
    if detection.matches:
        matches = ", ".join(detection.matches)
        raise ValueError(f"multiple flow profiles match ({matches}); choose --profile explicitly")
    raise ValueError("no flow profile detected; choose --profile explicitly")


def load_optional_reader(path: Path) -> GraphifyReader | None:
    if not path.exists():
        return None
    return GraphifyReader(path)


def select_candidate(plan: BootstrapPlan, key: str | None) -> BootstrapPlan:
    if key is None:
        return plan
    matches = tuple(candidate for candidate in plan.candidates if candidate.key == key)
    if not matches:
        raise ValueError(f"unknown candidate key {key!r}")
    return plan.model_copy(update={"candidates": matches})


def select_refreshed_candidate(
    plan: BootstrapPlan, selected: BootstrapCandidate | None
) -> BootstrapPlan:
    """Track a content-keyed selection by its stable source ref and fragment."""
    if selected is None:
        return plan
    matches = tuple(
        candidate
        for candidate in plan.candidates
        if (candidate.ref, candidate.fragment) == (selected.ref, selected.fragment)
    )
    if len(matches) > 1:
        raise ValueError(f"multiple refreshed candidates match source {selected.ref!r}")
    return plan.model_copy(update={"candidates": matches})


def _candidate_warning_text(plan: BootstrapPlan) -> str:
    count = len(plan.candidates)
    return f"Found {count} candidate" + ("" if count == 1 else "s")


def render_preview(plan: BootstrapPlan) -> None:
    print(_candidate_warning_text(plan))
    for number, candidate in enumerate(plan.candidates, start=1):
        print(f"  {number}. [{candidate.key}]")
        for line in render_candidate(candidate).splitlines():
            print(f"     {line}")


def render_diagnostic(plan: BootstrapPlan, review: ReviewResult | None = None) -> None:
    skipped = (
        0 if review is None else sum(item.action == ReviewAction.SKIP for item in review.items)
    )
    print(
        f"DIAGNOSTIC   no activation ({len(plan.files_read)} documents, "
        f"{len(plan.candidates)} candidates, {skipped} skipped); no writes"
    )


def _resume_argv(
    args: argparse.Namespace,
    root: Path,
    store_dir: Path,
    graph_path: Path,
    profile_name: str,
    candidate_key: str | None,
) -> list[str]:
    argv = [
        "sidegraph-bootstrap",
        "--resume",
        "--root",
        str(root),
        "--db",
        str(store_dir),
        "--graph",
        str(graph_path),
        "--profile",
        profile_name,
        "--host",
        args.host,
    ]
    for value in args.docs or ():
        argv.extend(("--docs", value))
    for value in args.include or ():
        argv.extend(("--include", value))
    optional = (
        ("--codex-config", args.codex_config),
        ("--candidate", candidate_key),
        ("--task", args.task),
        (
            "--report",
            str(_resolve_optional_path(args.report, root)) if args.report is not None else None,
        ),
    )
    for flag, value in optional:
        if value is not None:
            argv.extend((flag, value))
    return argv


def _resume_command(
    args: argparse.Namespace,
    root: Path,
    store_dir: Path,
    graph_path: Path,
    profile_name: str,
    candidate_key: str | None,
) -> str:
    return shlex.join(_resume_argv(args, root, store_dir, graph_path, profile_name, candidate_key))


def render_missing_graph(
    graph_path: Path,
    *,
    root: Path,
    resume_command: str,
) -> None:
    print(f"ACTIONABLE   graph not readable: {graph_path}")
    if graph_path == root / "graphify-out" / "graph.json":
        print(f"NEXT         cd {shlex.quote(str(root))} && graphify update .")
    print(f"RESUME       {resume_command}")


def render_changed_plan_diff(previous: BootstrapPlan, refreshed: BootstrapPlan) -> None:
    print("CHANGED      warning-producing inputs changed; stale actions discarded")
    old = {candidate.key: candidate.title for candidate in previous.candidates}
    new = {candidate.key: candidate.title for candidate in refreshed.candidates}
    for key in sorted(old.keys() - new.keys()):
        print(f"  - {old[key]} [{key}]")
    for key in sorted(new.keys() - old.keys()):
        print(f"  + {new[key]} [{key}]")
    render_preview(refreshed)


def _render_action_summary(review: ReviewResult) -> None:
    print("\nReviewed redacted action summary")
    for item in review.items:
        candidate = item.candidate
        print(f"- {item.action.value}: {candidate.title} [{candidate.key}]")
        print(f"  source: {candidate.ref}")
        print(f"  context: {candidate.context}")
        print(f"  choice: {candidate.choice}")
        print(f"  rejected: {candidate.rejected or '-'}")
        print(f"  consequences: {candidate.consequences or '-'}")
        for anchor in candidate.anchors:
            print(
                f"  anchor: {anchor.descriptor.name} ({anchor.status}, tier {anchor.tier or '-'})"
            )


def confirm_review(review: ReviewResult) -> bool:
    _render_action_summary(review)
    return input("Type literal 'confirm' to write reviewed actions: ").strip() == "confirm"


def render_cancelled_without_writes() -> None:
    print("CANCELLED    confirmation was not literal 'confirm'; no writes")


def render_input_ended_without_writes(phase: str, resume_command: str) -> None:
    print(f"CANCELLED    input ended during {phase}; no writes")
    print(f"RESUME       {resume_command}")


def with_exact_resume_command(
    report: BootstrapReport,
    args: argparse.Namespace,
    root: Path,
    store_dir: Path,
    graph_path: Path,
    profile_name: str,
    candidate_key: str | None,
) -> BootstrapReport:
    command = _resume_command(args, root, store_dir, graph_path, profile_name, candidate_key)
    return report.model_copy(update={"next_command": command})


def collect_post_apply_domain_candidates(
    report: BootstrapReport,
    store_dir: Path,
    reader: GraphifyReader,
) -> DomainSuggestions:
    if report.status != RunStatus.COMPLETE:
        return DomainSuggestions()
    store: Store | None = None
    try:
        store = Store(store_dir)
        candidates, _stats = collect_domain_candidates(store, reader, limit=5)
    except Exception:
        return DomainSuggestions()
    finally:
        if store is not None:
            with suppress(Exception):
                store.close()
    return DomainSuggestions(
        count=len(candidates),
        titles=tuple(candidate.suggested_title for candidate in candidates),
    )


def prove_after_apply(
    report: BootstrapReport,
    store_dir: Path,
    reader: GraphifyReader,
    *,
    file_path: str | None,
) -> ProofResult:
    if report.status != RunStatus.COMPLETE:
        return ProofResult(complete=False, reason=f"activation is {report.status.value}")
    store: Store | None = None
    try:
        store = Store(store_dir)
        return prove_task_context(
            store,
            reader,
            accepted_record_ids=tuple(item.record_id for item in report.durable_accepted_records),
            file_path=file_path,
        )
    except Exception as error:
        return ProofResult(complete=False, reason=str(error) or type(error).__name__)
    finally:
        if store is not None:
            with suppress(Exception):
                store.close()


def write_report_only_when_requested(
    path_value: str | None,
    root: Path,
    report: BootstrapReport,
    integration: IntegrationResult,
    proof: ProofResult,
    task_proof: ProofResult | None,
    elapsed_seconds: float,
) -> None:
    path = _resolve_optional_path(path_value, root)
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        render_markdown_report(
            report,
            integration=integration,
            proof=proof,
            task_proof=task_proof,
            elapsed_seconds=elapsed_seconds,
        ),
        encoding="utf-8",
    )


def integration_ready_for_selected_host(result: IntegrationResult) -> bool:
    if result.host == HostKind.CLAUDE_CODE:
        return result.fully_supported
    return (
        result.host == HostKind.CODEX
        and result.mcp == "verified"
        and result.session_start == "verified"
        and result.stop == "verified"
        and result.pretool_read_grep == "unsupported"
    )


def _accepted_anchors_complete(review: ReviewResult) -> bool:
    accepted = [item.candidate for item in review.items if item.action == ReviewAction.ACCEPT]
    return bool(accepted) and all(
        any(anchor.status == "resolved" and anchor.tier == 2 for anchor in candidate.anchors)
        and all(anchor.status == "resolved" for anchor in candidate.anchors)
        for candidate in accepted
    )


def completion_exit_code(
    report: BootstrapReport,
    review: ReviewResult,
    integration: IntegrationResult,
    proof: ProofResult,
) -> int:
    complete_for_selected_host = (
        report.status == RunStatus.COMPLETE
        and _accepted_anchors_complete(review)
        and integration_ready_for_selected_host(integration)
        and proof.complete
    )
    return 0 if complete_for_selected_host else 2


def _terminal_text(value: str) -> str:
    redacted, _ = redact(value)
    return "".join(
        character
        if ord(character) >= 32 and ord(character) != 127
        else character.encode("unicode_escape").decode("ascii")
        for character in redacted
    )


def _render_structured_values(label: str, values: tuple[str, ...]) -> None:
    if not values:
        print(f"{label:<13}<none>")
        return
    for value in values:
        print(f"{label:<13}{_terminal_text(value)}")


def _safe_input_error(error: str | None) -> str | None:
    if error is None:
        return None
    allowed_prefixes = (
        "source unavailable:",
        "source changed after preview:",
        "canonical catalog unavailable:",
        "canonical catalog changed after preview",
        "graph unavailable:",
        "graph changed after preview",
    )
    details = tuple(part.strip() for part in error.split(";"))
    return error if details and all(part.startswith(allowed_prefixes) for part in details) else None


def render_completion(
    report: BootstrapReport,
    review: ReviewResult,
    integration: IntegrationResult,
    proof: ProofResult,
    domain_candidates: DomainSuggestions,
    *,
    task: str | None,
    task_proof: ProofResult | None = None,
) -> None:
    if report.status == RunStatus.COMPLETE:
        print("STORE        ready")
    else:
        print(f"STORE        {report.status.value}")
        detail = _safe_input_error(report.error)
        if detail is not None:
            print(f"DETAIL       {_terminal_text(detail)}")

    print(
        f"{'FAILED REF':<13}"
        f"{_terminal_text(report.failed_ref) if report.failed_ref is not None else '<none>'}"
    )
    _render_structured_values("DURABLE", report.durable_candidate_keys)
    _render_structured_values("PENDING", report.pending_candidate_keys)
    _render_structured_values("VERIFY", report.verification_failures)
    _render_structured_values("CHANGED", report.canonical_files)

    if _accepted_anchors_complete(review):
        print("ANCHORS      ready")
    elif any(item.action == ReviewAction.ACCEPT for item in review.items):
        print("ANCHORS      incomplete: accepted candidate has unresolved or ambiguous anchors")
    else:
        print(
            "ANCHORS      incomplete: activation needs at least one accepted candidate "
            "(proposals neither block nor satisfy this check)"
        )

    if integration.host == HostKind.CLAUDE_CODE and integration.fully_supported:
        print("INTEGRATION  Claude Code MCP + hooks verified")
    elif integration.host == HostKind.CODEX and integration_ready_for_selected_host(integration):
        print("INTEGRATION  Codex best-effort")
        print("             Read/Grep PreToolUse unsupported")
    else:
        print(f"INTEGRATION  {integration.host.value} incomplete")
        if integration.host == HostKind.CODEX:
            print("             Read/Grep PreToolUse unsupported")
        if integration.next_action:
            print(f"ACTION       {integration.next_action}")

    if proof.complete:
        print("PROOF        production retrieval returned")
        if proof.primary_line is not None:
            print(f"PROOF LINE   {_terminal_text(proof.primary_line)}")
        if proof.file_path is not None:
            print(f"PROOF ANCHOR {_terminal_text(proof.file_path)}")
        if proof.source is not None:
            print(f"PROOF SOURCE {_terminal_text(proof.source)}")
        if proof.selection_rule is not None:
            print(f"PROOF RULE   {_terminal_text(proof.selection_rule)}")
        if proof.copyable_prompt is not None:
            print(f"HOST PROMPT  {_terminal_text(proof.copyable_prompt)}")
    else:
        reason = proof.reason or "retrieval proof unavailable"
        print(f"PROOF        incomplete: {_terminal_text(reason)}")
    if task is not None:
        print(f"TASK         {_terminal_text(task)}")
        if task_proof is None:
            print("TASK PROOF   incomplete: task proof was not run")
        elif task_proof.complete:
            print("TASK PROOF   production retrieval returned")
            if task_proof.primary_line is not None:
                print(f"TASK LINE    {_terminal_text(task_proof.primary_line)}")
            if task_proof.file_path is not None:
                print(f"TASK ANCHOR  {_terminal_text(task_proof.file_path)}")
            if task_proof.source is not None:
                print(f"TASK SOURCE  {_terminal_text(task_proof.source)}")
            if task_proof.selection_rule is not None:
                print(f"TASK RULE    {_terminal_text(task_proof.selection_rule)}")
        else:
            reason = task_proof.reason or "retrieval proof unavailable"
            print(f"TASK PROOF   incomplete: {_terminal_text(reason)}")

    if domain_candidates.count:
        titles = ", ".join(domain_candidates.titles)
        print(f"DOMAINS      {domain_candidates.count} read-only suggestion(s): {titles}")
        print("Optional orientation follow-up: sidegraph-domains bootstrap --dry-run")

    if report.next_command is not None:
        print(f"RESUME       {report.next_command}")


def _main(args: argparse.Namespace) -> int:
    started_at = monotonic()
    root = Path(args.root).resolve()
    store_dir = Path(resolve_store_path(args.db, root=str(root), warn_on_create=False)).resolve()
    graph_path = _resolve_graph_path(args.graph, root).resolve()
    _validate_report_destination(args, root, store_dir, graph_path)
    detection = detect_profile(root, args.profile)
    profile = require_selected_profile(detection)
    docs = tuple(Path(path) for path in (args.docs or ()))
    includes = tuple(Path(path) for path in (args.include or ()))
    scan = scan_sources(root, profile, docs, includes)
    protected_sources = scan.files
    _validate_report_destination(
        args,
        root,
        store_dir,
        graph_path,
        protected_sources,
    )
    catalog = load_canonical_catalog(store_dir)
    reader = load_optional_reader(graph_path)
    plan = select_candidate(
        plan_sources(root, scan, profile, catalog=catalog, reader=reader),
        args.candidate,
    )
    selected_source = plan.candidates[0] if args.candidate is not None else None
    selected_candidate_key = args.candidate
    render_preview(plan)
    if not plan.candidates:
        render_diagnostic(plan)
        return 0

    resume_command = _resume_command(
        args,
        root,
        store_dir,
        graph_path,
        profile.name,
        selected_candidate_key,
    )
    if reader is None:
        render_missing_graph(
            graph_path,
            root=root,
            resume_command=resume_command,
        )
        return 2

    try:
        review = review_plan(plan, reader=reader, catalog=catalog, read_line=input)
    except EOFError:
        render_input_ended_without_writes("candidate review", resume_command)
        return 2
    if not review.has_writes:
        render_diagnostic(plan, review)
        return 0

    try:
        refreshed_scan = scan_sources(root, profile, docs, includes)
        protected_sources = tuple(dict.fromkeys((*scan.files, *refreshed_scan.files)))
        _validate_report_destination(
            args,
            root,
            store_dir,
            graph_path,
            protected_sources,
        )
        refreshed_catalog = load_canonical_catalog(store_dir)
        refreshed_reader = load_optional_reader(graph_path)
        refreshed = select_refreshed_candidate(
            plan_sources(
                root,
                refreshed_scan,
                profile,
                catalog=refreshed_catalog,
                reader=refreshed_reader,
            ),
            selected_source,
        )
    except (OSError, UnicodeError, ValueError) as error:
        print(f"ACTIONABLE   refresh failed: {error}")
        print(f"RESUME       {resume_command}")
        return 2

    selected_candidate_key = (
        refreshed.candidates[0].key
        if selected_source is not None and refreshed.candidates
        else None
    )
    resume_command = _resume_command(
        args,
        root,
        store_dir,
        graph_path,
        profile.name,
        selected_candidate_key,
    )

    if refreshed_reader is None:
        render_missing_graph(
            graph_path,
            root=root,
            resume_command=resume_command,
        )
        return 2
    if refreshed.fingerprint != plan.fingerprint:
        render_changed_plan_diff(plan, refreshed)
        try:
            review = review_plan(
                refreshed,
                reader=refreshed_reader,
                catalog=refreshed_catalog,
                read_line=input,
            )
        except EOFError:
            render_input_ended_without_writes("candidate review", resume_command)
            return 2
        if not review.has_writes:
            render_diagnostic(refreshed, review)
            return 0

    try:
        confirmed = confirm_review(review)
    except EOFError:
        render_input_ended_without_writes("final confirmation", resume_command)
        return 2
    if not confirmed:
        render_cancelled_without_writes()
        print(f"RESUME       {resume_command}")
        return 2

    report = apply_review(refreshed, review, store_dir=store_dir, reader=refreshed_reader)
    # Everything below this point runs AFTER apply_review has durably written canonical
    # state (report reflects it). A failure here is never a "usage or operational error
    # before review" — that meaning is reserved for exit 1 (see
    # docs/getting-started/bootstrap.md#completion-recovery-and-hosts). It is at worst
    # partial-recoverable, exit 2, with a resume command — never main()'s catch-all
    # `except (OSError, UnicodeError, ValueError) -> return 1`, which would misreport a
    # run that already has memory on disk as having failed before writing anything. The
    # two handlers below therefore guard by BaseException, not by a named type list:
    # matching that list against every exception this tail can actually raise (e.g. the
    # unbounded recursion over a user-supplied .mcp.json in
    # integrations._command_strings, reached from verify_integration below) is a losing
    # game, and apply.py's own post-write handlers (_finalize, apply_review) already
    # settled on the same BaseException convention for exactly this reason. That means a
    # KeyboardInterrupt landing here (Ctrl-C after the write) is now reported and
    # resolved as partial-recoverable — exit 2 with a resume command — exactly like any
    # other post-write failure, never a bare interrupt traceback that drops the resume
    # command for memory already on disk. This mirrors apply_review's own write loop,
    # which already swallows a BaseException mid-write the same way. A Ctrl-C raised
    # earlier, before apply_review runs (e.g. during a review prompt), is untouched by
    # this and still propagates and aborts the process as before — nothing has been
    # durably written yet for a resume to reconcile.
    try:
        domain_candidates = collect_post_apply_domain_candidates(
            report,
            store_dir,
            refreshed_reader,
        )
        codex_config = _resolve_optional_path(args.codex_config, root)
        integration = verify_integration(root, HostKind(args.host), codex_config=codex_config)
        proof = prove_after_apply(
            report,
            store_dir,
            refreshed_reader,
            file_path=None,
        )
        elapsed_seconds = max(0.0, monotonic() - started_at)
        task_proof = (
            prove_after_apply(
                report,
                store_dir,
                refreshed_reader,
                file_path=args.task,
            )
            if args.task is not None
            else None
        )
        exit_code = completion_exit_code(report, review, integration, proof)
        if exit_code == 2:
            report = with_exact_resume_command(
                report,
                args,
                root,
                store_dir,
                graph_path,
                profile.name,
                selected_candidate_key,
            )
        render_completion(
            report,
            review,
            integration,
            proof,
            domain_candidates,
            task=args.task,
            task_proof=task_proof,
        )
    except BaseException as error:
        # Any exception here is post-durable-write and must resolve to
        # partial-recoverable, never escape to main()'s narrower catch-all and get
        # misreported as exit 1 (or, for KeyboardInterrupt/RecursionError, crash
        # uncaught with memory already on disk). A further print here can itself fail on
        # the same dead channel (or re-raise the same RecursionError); suppress that too
        # rather than let it re-raise past this handler and get misreported as exit 1.
        with suppress(BaseException):
            # {error} alone renders str() only -- empty for KeyboardInterrupt() and
            # thin for plenty of others, which made a real Ctrl-C after the write and a
            # genuine internal bug indistinguishable on the ACTIONABLE line: both an
            # empty reason. The type name identifies WHICH exception fired even when
            # its message doesn't; the traceback goes to stderr (never stdout, which
            # downstream tooling parses for ACTIONABLE/RESUME) so a genuine bug is
            # still post-mortem debuggable.
            print(
                f"ACTIONABLE   completion failed after durable write: "
                f"{type(error).__name__}: {error}"
            )
            print(f"RESUME       {resume_command}")
            traceback.print_exception(error, file=sys.stderr)
        return 2
    try:
        _validate_report_destination(
            args,
            root,
            store_dir,
            graph_path,
            protected_sources,
        )
        write_report_only_when_requested(
            args.report,
            root,
            report,
            integration,
            proof,
            task_proof,
            elapsed_seconds,
        )
    except BaseException as error:
        # Same reasoning as the completion handler above: this runs after apply_review's
        # durable write, so any exception here — not just OSError/UnicodeError/ValueError
        # — is partial-recoverable, never exit 1 and never an uncaught crash. And, same
        # as that handler, the diagnostic prints below must themselves be wrapped: this
        # handler exists precisely for a dead output channel, so an unguarded print here
        # would raise OSError, escape this except block and _main entirely, and land in
        # main()'s own `except (OSError, ...) -> return 1` — misreporting a run that
        # already durably wrote a canonical decision as exit 1. Suppressing here is what
        # keeps this handler's own fix from being defeated by its own failure mode.
        with suppress(BaseException):
            # Same identity loss as the completion handler above: {error} alone is
            # str()-only, so the type name goes on the ACTIONABLE line and the full
            # traceback on stderr — see that handler's comment for why.
            print(f"ACTIONABLE   report write failed: {type(error).__name__}: {error}")
            print(f"RESUME       {resume_command}")
            traceback.print_exception(error, file=sys.stderr)
        return 2
    return exit_code


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return _main(args)
    except (OSError, UnicodeError, ValueError) as error:
        print(f"ERROR        {error}", file=sys.stderr)
        return 1
