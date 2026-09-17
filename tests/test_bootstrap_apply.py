from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest
from ulid import ULID

from sidegraph.bootstrap.apply import (
    apply_review,
    build_doc_request,
    canonical_manifest,
    proposal_debt,
    reconcile_plan,
    render_markdown_report,
    validate_plan_inputs,
)
from sidegraph.bootstrap.catalog import (
    CanonicalCatalog,
    fingerprint_catalog,
    load_canonical_catalog,
)
from sidegraph.bootstrap.model import (
    BootstrapCandidate,
    BootstrapPlan,
    BootstrapReport,
    ReviewAction,
    ReviewedCandidate,
    ReviewResult,
    RunStatus,
    ScanResult,
    SourceFingerprint,
)
from sidegraph.bootstrap.planner import plan_sources
from sidegraph.doc_import import DocWriteRequest, ParsedDoc, apply_doc_candidate, import_docs
from sidegraph.engine.reader import GraphifyReader
from sidegraph.profiles import get_profile
from sidegraph.schema import (
    Decision,
    DecisionKind,
    DecisionStatus,
    Descriptor,
    Fact,
    Provenance,
)
from sidegraph.store import Store


def write_graph(tmp_path):
    graph = tmp_path / "graph.json"
    graph.write_text(
        json.dumps(
            {
                "built_at_commit": "v1",
                "nodes": [
                    {
                        "id": "fn_submit",
                        "label": "submit_order",
                        "norm_label": "submit_order",
                        "file_type": "code",
                        "source_file": "exec.py",
                        "community": 1,
                    }
                ],
                "links": [],
            }
        ),
        encoding="utf-8",
    )
    return graph


def request_for_status(status):
    return DocWriteRequest(
        parsed=ParsedDoc(
            title="Submit path",
            context="Retries need a durable policy.\n\nimported from docs/a.md",
            choice="Call `submit_order` with an idempotency key.",
        ),
        rel_path="docs/a.md",
        source_hash=hashlib.sha256(b"reviewed source\n").hexdigest(),
        status=status,
        anchors=(Descriptor(name="submit_order"),),
        graph_version="v1",
    )


@pytest.mark.parametrize("status", [DecisionStatus.ACCEPTED, DecisionStatus.PROPOSED])
def test_apply_doc_candidate_honors_reviewed_status(tmp_path, status):
    store = Store(tmp_path / ".sidegraph")
    reader = GraphifyReader(write_graph(tmp_path))
    request = request_for_status(status)
    result = apply_doc_candidate(store, reader, request)
    written = store.get_decision(result.decision_id)
    assert written is not None
    assert written.status == status
    assert written.provenance.source == "doc-import"
    assert not hasattr(written.provenance, "review_action")


def test_accepting_matching_proposal_ratifies_it_repairs_bindings_and_cascades_facts(
    tmp_path,
):
    """Skipping the status transition leaves review, bindings, and supporting facts pending."""
    store = Store(tmp_path / ".sidegraph")
    reader = GraphifyReader(write_graph(tmp_path))
    request = request_for_status(DecisionStatus.ACCEPTED).model_copy(
        update={"ratify_matching_proposal": True}
    )
    proposal = store.add_decision(
        Decision(
            title=request.parsed.title,
            kind=DecisionKind.ADR,
            status=DecisionStatus.PROPOSED,
            context=request.parsed.context,
            choice=request.parsed.choice,
            rejected=request.parsed.rejected,
            consequences=request.parsed.consequences,
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="doc-import", ref=request.ref),
        )
    )
    supporting = store.add_fact(
        Fact(
            statement="Retries need an idempotency key.",
            source="reviewed ADR",
            status=DecisionStatus.PROPOSED,
            supports=[proposal.id],
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="doc-import", ref=request.ref),
        )
    )

    result = apply_doc_candidate(store, reader, request)

    assert result.action == "skipped-existing"
    assert result.decision_id == proposal.id
    assert store.get_decision(proposal.id).status == DecisionStatus.ACCEPTED
    assert store.get_fact(supporting.id).status == DecisionStatus.ACCEPTED
    bindings = store.bindings_for_record(proposal.id)
    assert len(bindings) == 2
    assert {binding.tier for binding in bindings} == {1, 2}


def test_apply_doc_candidate_rejects_non_import_status(tmp_path):
    store = Store(tmp_path / ".sidegraph")
    reader = GraphifyReader(write_graph(tmp_path))
    request = request_for_status(DecisionStatus.SUPERSEDED)
    with pytest.raises(
        ValueError,
        match="document import can write only accepted, proposed, or rejected",
    ):
        apply_doc_candidate(store, reader, request)


def test_import_docs_dry_run_classification_does_not_mutate_store(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "docs/a.md"
    source.parent.mkdir(parents=True)
    source.write_text(
        "# Submit path\n\n## Context\n\nRetries need policy.\n\n"
        "## Decision\n\nCall `submit_order` once.\n",
        encoding="utf-8",
    )
    store_root = tmp_path / ".sidegraph"
    store = Store(store_root)
    reader = GraphifyReader(write_graph(tmp_path))
    import_docs(store, reader, ["docs/a.md"], any_doc=True)
    source.write_text(
        "# Submit path\n\n## Context\n\nRetries need policy.\n\n"
        "## Decision\n\nCall `submit_order` three times.\n",
        encoding="utf-8",
    )
    before = {
        path.relative_to(store_root).as_posix(): path.read_bytes()
        for path in store_root.rglob("*")
        if path.is_file()
    }

    report = import_docs(store, reader, ["docs/a.md"], any_doc=True, dry_run=True)

    after = {
        path.relative_to(store_root).as_posix(): path.read_bytes()
        for path in store_root.rglob("*")
        if path.is_file()
    }
    assert report.superseded == 1
    assert after == before


def accepted_plan(tmp_path, *, count=2, action=ReviewAction.ACCEPT):
    graph = write_graph(tmp_path)
    reader = GraphifyReader(graph)
    candidates = []
    source_fingerprints = []
    files_read = []
    for number in range(1, count + 1):
        rel_path = f"docs/adr/{number:03d}.md"
        source = tmp_path / rel_path
        source.parent.mkdir(parents=True, exist_ok=True)
        source_bytes = f"reviewed source {number}\n".encode()
        source.write_bytes(source_bytes)
        source_hash = hashlib.sha256(source_bytes).hexdigest()
        source_fingerprints.append(SourceFingerprint(path=rel_path, sha256=source_hash))
        files_read.append(rel_path)
        candidates.append(
            BootstrapCandidate.from_fields(
                file_path=rel_path,
                ref=rel_path,
                source_hash=source_hash,
                title=f"ADR {number}",
                context=f"Redacted context {number}",
                choice=f"Redacted choice {number}",
                kind=DecisionKind.ADR,
                default_status=DecisionStatus.PROPOSED,
                redacted_anchor_text="submit_order",
                anchor_intents=(Descriptor(name="submit_order"),),
            )
        )
    plan = BootstrapPlan(
        root=tmp_path.as_posix(),
        profile="generic-adr",
        fingerprint="reviewed-plan",
        source_fingerprints=tuple(source_fingerprints),
        catalog_fingerprint=fingerprint_catalog(CanonicalCatalog()),
        graph_version=reader.graph_version(),
        candidates=tuple(candidates),
        files_read=tuple(files_read),
    )
    review = ReviewResult(
        items=tuple(ReviewedCandidate(candidate=item, action=action) for item in candidates)
    )
    return plan, review, reader


def test_failure_after_canonical_write_is_partial_recoverable(tmp_path, monkeypatch):
    """Removing reopen reconciliation would lose the second durable candidate."""
    plan, review, reader = accepted_plan(tmp_path, count=2)
    original = Store._index_write_decision
    calls = {"n": 0}

    def fail_second(self, decision):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("index failure")
        return original(self, decision)

    monkeypatch.setattr(Store, "_index_write_decision", fail_second)
    report = apply_review(plan, review, store_dir=tmp_path / ".sidegraph", reader=reader)
    assert report.status == RunStatus.PARTIAL_RECOVERABLE
    assert report.durable_candidate_keys == tuple(item.candidate.key for item in review.items)
    assert report.pending_candidate_keys == ()
    assert report.failed_ref == "docs/adr/002.md"
    assert report.next_command == "sidegraph-bootstrap --resume"


def test_reopen_and_rerun_converges_without_duplicates(tmp_path, monkeypatch):
    """Minting a duplicate on resume instead of matching canonical content is a bug."""
    plan, review, reader = accepted_plan(tmp_path, count=2)
    original = Store._index_write_decision
    calls = {"n": 0}

    def fail_second(self, decision):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("index failure")
        return original(self, decision)

    monkeypatch.setattr(Store, "_index_write_decision", fail_second)
    first = apply_review(plan, review, store_dir=tmp_path / ".sidegraph", reader=reader)
    monkeypatch.setattr(Store, "_index_write_decision", original)
    resumed_plan = plan.model_copy(
        update={
            "catalog_fingerprint": fingerprint_catalog(
                load_canonical_catalog(tmp_path / ".sidegraph")
            )
        }
    )
    second = apply_review(
        resumed_plan,
        review,
        store_dir=tmp_path / ".sidegraph",
        reader=reader,
    )
    store = Store(tmp_path / ".sidegraph")
    try:
        decisions = list(store.iter_decisions())
        bindings = [binding for item in decisions for binding in store.bindings_for_record(item.id)]
    finally:
        store.close()
    assert first.status == RunStatus.PARTIAL_RECOVERABLE
    assert second.status == RunStatus.COMPLETE
    assert len(decisions) == 2
    assert len({item.id for item in decisions}) == 2
    assert len({(item.record_id, item.entity_id) for item in bindings}) == len(bindings)


def test_reconciliation_returns_current_run_durable_accepted_ids_and_refs(tmp_path):
    """Proof cannot be run-scoped if apply reconciliation discards canonical identity."""
    plan, review, reader = accepted_plan(tmp_path, count=1)

    report = apply_review(plan, review, store_dir=tmp_path / ".sidegraph", reader=reader)

    assert len(report.durable_accepted_records) == 1
    accepted = report.durable_accepted_records[0]
    assert accepted.ref == "docs/adr/001.md"
    store = Store(tmp_path / ".sidegraph")
    try:
        assert store.get_decision(accepted.record_id).status == DecisionStatus.ACCEPTED
    finally:
        store.close()

    proposed_root = tmp_path / "proposed"
    proposed_root.mkdir()
    proposed_plan, proposed_review, proposed_reader = accepted_plan(
        proposed_root,
        count=1,
        action=ReviewAction.KEEP_PROPOSED,
    )
    proposed = apply_review(
        proposed_plan,
        proposed_review,
        store_dir=proposed_root / ".sidegraph",
        reader=proposed_reader,
    )
    assert proposed.durable_accepted_records == ()


def test_report_never_contains_source_content_or_secret():
    """Rendering error/proof detail would disclose reviewed source or raw secrets."""
    report = BootstrapReport(
        status=RunStatus.INCOMPLETE,
        documents_scanned=1,
        candidates=1,
        accepted=1,
        kept_proposed=0,
        skipped=0,
        live_anchors=0,
        review_debt_count=0,
        failed_ref="docs/private/sk-live-secret.md#private-fragment",
        error="private architecture paragraph sk-live-secret",
        next_command="sidegraph-bootstrap --resume",
    )
    rendered = render_markdown_report(report)
    assert "private architecture paragraph" not in rendered
    assert "sk-live-secret" not in rendered
    assert "private-fragment" not in rendered
    assert "documents scanned:" in rendered
    assert "accepted:" in rendered


def test_report_lists_canonical_files_and_review_debt(tmp_path):
    plan, review, reader = accepted_plan(
        tmp_path,
        count=2,
        action=ReviewAction.KEEP_PROPOSED,
    )
    report = apply_review(plan, review, store_dir=tmp_path / ".sidegraph", reader=reader)
    assert report.review_debt_count == 2
    assert report.oldest_proposal_days is not None
    assert all(path.startswith(".sidegraph/") for path in report.canonical_files)
    rendered = render_markdown_report(report)
    assert "review debt (proposed): 2" in rendered


def test_report_aggregates_edit_action_rates_precision_and_elapsed_time(tmp_path):
    """Collapsing edits into final statuses makes activation evidence uncomputable."""
    plan, original_review, reader = accepted_plan(tmp_path, count=3)
    review = ReviewResult(
        items=(
            original_review.items[0].model_copy(update={"action_elapsed_seconds": 1.0}),
            original_review.items[1].model_copy(
                update={
                    "action": ReviewAction.KEEP_PROPOSED,
                    "edited": True,
                    "action_elapsed_seconds": 2.0,
                }
            ),
            original_review.items[2].model_copy(
                update={
                    "action": ReviewAction.SKIP,
                    "action_elapsed_seconds": 3.0,
                }
            ),
        ),
        elapsed_seconds=6.0,
    )

    report = apply_review(plan, review, store_dir=tmp_path / ".sidegraph", reader=reader)
    rendered = render_markdown_report(report, elapsed_seconds=42.5)

    assert report.reviewed_candidates == 3
    assert report.accepted_without_edit == 1
    assert report.edited_then_accepted == 0
    assert report.kept_proposed_without_edit == 0
    assert report.edited_then_kept_proposed == 1
    assert report.candidate_precision_numerator == 2
    assert report.review_elapsed_seconds == 6.0
    assert "- candidate precision: 2/3 (66.7%)" in rendered
    assert "- accept: 1/3 (33.3%); action seconds: 1.000" in rendered
    assert "- edit then keep proposed: 1/3 (33.3%); action seconds: 2.000" in rendered
    assert "- skip: 1/3 (33.3%); action seconds: 3.000" in rendered
    assert "- review elapsed seconds: 6.000" in rendered
    assert "- elapsed seconds: 42.500" in rendered


def test_reconciliation_ignores_skipped_candidates(tmp_path):
    plan, review, _reader = accepted_plan(tmp_path, count=2)
    mixed = ReviewResult(
        items=(
            review.items[0],
            review.items[1].model_copy(update={"action": ReviewAction.SKIP}),
        )
    )
    result = reconcile_plan(plan, mixed, tmp_path / ".sidegraph", before={})
    assert result.pending == (review.items[0].candidate.key,)


def test_stale_source_returns_incomplete_without_opening_store(tmp_path, monkeypatch):
    """Moving source validation after Store construction would create partial state."""
    plan, review, reader = accepted_plan(tmp_path, count=1)
    (tmp_path / plan.source_fingerprints[0].path).write_text("changed\n", encoding="utf-8")

    def forbidden_store(*args, **kwargs):
        raise AssertionError("Store must not be constructed for a stale preview")

    monkeypatch.setattr("sidegraph.bootstrap.apply.Store", forbidden_store)
    report = apply_review(plan, review, store_dir=tmp_path / ".sidegraph", reader=reader)
    assert report.status == RunStatus.INCOMPLETE
    assert report.next_command == "sidegraph-bootstrap --resume"
    assert not (tmp_path / ".sidegraph").exists()


def test_all_skipped_review_is_diagnostic_without_opening_store(tmp_path, monkeypatch):
    plan, review, reader = accepted_plan(tmp_path, count=2)
    skipped = ReviewResult(
        items=tuple(item.model_copy(update={"action": ReviewAction.SKIP}) for item in review.items)
    )

    def forbidden_store(*args, **kwargs):
        raise AssertionError("Store must not be constructed for an all-skipped review")

    monkeypatch.setattr("sidegraph.bootstrap.apply.Store", forbidden_store)
    report = apply_review(plan, skipped, store_dir=tmp_path / ".sidegraph", reader=reader)
    assert report.status == RunStatus.DIAGNOSTIC
    assert report.durable_candidate_keys == ()
    assert report.pending_candidate_keys == ()
    assert report.next_command is None
    assert not (tmp_path / ".sidegraph").exists()


def test_validate_plan_inputs_checks_every_source_catalog_and_fresh_graph(tmp_path):
    plan, _review, reader = accepted_plan(tmp_path, count=2)
    for item in plan.source_fingerprints:
        (tmp_path / item.path).write_text(f"changed {item.path}\n", encoding="utf-8")
    write_graph(tmp_path).write_text('{"built_at_commit":"v2","nodes":[],"links":[]}\n')
    store_dir = tmp_path / ".sidegraph"
    store = Store(store_dir)
    try:
        store.add_decision(
            Decision(
                title="Unrelated",
                kind=DecisionKind.ADR,
                status=DecisionStatus.ACCEPTED,
                context="c",
                choice="ch",
                valid_from=datetime.now(UTC),
                provenance=Provenance(source="manual"),
            )
        )
    finally:
        store.close()
    failures = validate_plan_inputs(plan, store_dir, reader)
    assert sum("source changed" in item for item in failures) == 2
    assert any("canonical catalog changed" in item for item in failures)
    assert any("graph changed" in item for item in failures)


def test_split_reviewed_candidate_maps_to_constructible_parsed_doc(tmp_path):
    plan, review, _reader = accepted_plan(tmp_path, count=1)
    candidate = review.items[0].candidate.model_copy(
        update={"ref": "docs/adr/001.md#ad-2-cache-reads", "fragment": "ad-2-cache-reads"}
    )
    item = ReviewedCandidate(candidate=candidate, action=ReviewAction.ACCEPT)
    request = build_doc_request(item, "commit:hash", get_profile("generic-adr"))
    assert request.parsed.fragment == "ad-2-cache-reads"
    assert request.parsed.frontmatter_status is None
    assert request.parsed.suggested_kind == item.candidate.kind
    assert request.ref == item.candidate.ref
    assert request.status == DecisionStatus.ACCEPTED


def test_build_doc_request_rejects_skipped_candidate(tmp_path):
    _plan, review, _reader = accepted_plan(tmp_path, count=1)
    skipped = review.items[0].model_copy(update={"action": ReviewAction.SKIP})
    with pytest.raises(ValueError, match="skipped candidate"):
        build_doc_request(skipped, "commit:hash", get_profile("generic-adr"))


def test_bootstrap_accept_grants_ratification_through_build_doc_request(tmp_path):
    """Guard only — passes before the fix; it pins that Bootstrap keeps the authority the
    importer loses. Kills Task 7's mutation 1."""
    store = Store(tmp_path / ".sidegraph")
    reader = GraphifyReader(write_graph(tmp_path))
    candidate = BootstrapCandidate.from_fields(
        file_path="docs/a.md",
        ref="docs/a.md",
        fragment=None,
        source_hash=hashlib.sha256(b"reviewed source\n").hexdigest(),
        title="Submit path",
        context="Retries need a durable policy.",
        choice="Call `submit_order` with an idempotency key.",
        rejected=None,
        consequences=None,
        kind=DecisionKind.ADR,
        default_status=DecisionStatus.PROPOSED,
        redacted_anchor_text="Call `submit_order`.",
        anchor_intents=(Descriptor(name="submit_order"),),
    )
    proposal = store.add_decision(
        Decision(
            title=candidate.title,
            kind=DecisionKind.ADR,
            status=DecisionStatus.PROPOSED,
            context=candidate.context,
            choice=candidate.choice,
            rejected=candidate.rejected,
            consequences=candidate.consequences,
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="doc-import", ref=candidate.ref),
        )
    )
    item = ReviewedCandidate(candidate=candidate, action=ReviewAction.ACCEPT)
    request = build_doc_request(item, "v1", get_profile("generic-adr"))
    apply_doc_candidate(store, reader, request)
    assert store.get_decision(proposal.id).status == DecisionStatus.ACCEPTED


def test_openspec_bootstrap_apply_ref_and_stamp_match_importer_after_normalization(
    tmp_path, monkeypatch
):
    """Spec T12 (round-3: the stamp clause is what makes this red against a ref-only
    normalization, where rev-3-as-first-written normalized only `ref`): `build_doc_
    request`'s derived ref equals the planner candidate's ref (no AssertionError at
    apply.py:485), both derived from the SAME archived-path doc — and the candidate's
    `context` stamp byte-matches `sidegraph-import`'s stamp for the identical doc content
    at the identical archived path."""
    text = (
        "## Why\n\nWe need a faster release cadence.\n\n"
        "## What Changes\n\nShip the thin slice first via `submit_order`.\n\n"
        "## Impact\n\n- Affected specs: importer\n"
    )
    archived_rel = "openspec/changes/archive/2026-01-01-add-thing/proposal.md"
    path = tmp_path / archived_rel
    path.parent.mkdir(parents=True)
    path.write_text(text, encoding="utf-8")
    profile = get_profile("openspec")
    scan = ScanResult(root=str(tmp_path), files=(archived_rel,))

    plan = plan_sources(tmp_path, scan, profile)
    assert len(plan.candidates) == 1
    candidate = plan.candidates[0]
    assert candidate.ref == "openspec/changes/add-thing/proposal.md"
    assert candidate.file_path == archived_rel  # stays on-disk (planner.py:209,218 lookups)

    item = ReviewedCandidate(candidate=candidate, action=ReviewAction.ACCEPT)
    request = build_doc_request(item, "v1", profile)
    assert request.ref == candidate.ref  # apply.py:485's assertion holds by construction

    # sidegraph-import's own stamp for the SAME doc at the SAME archived path — both
    # importers must see the identical repo-relative form, so run from tmp_path.
    monkeypatch.chdir(tmp_path)
    store = Store(tmp_path / ".sidegraph")
    reader = GraphifyReader(write_graph(tmp_path))
    report = import_docs(store, reader, [archived_rel], profile="openspec", any_doc=True)
    assert report.imported == 1
    imported = next(store.iter_decisions())
    assert request.parsed.context == imported.context


def test_openspec_bootstrap_apply_live_path_note_matches_importer(tmp_path, monkeypatch):
    """I2 (R1 improvement wave §2) cross-path pin, mirroring T12 above but for a LIVE
    (non-archived) doc: both write paths must apply the SAME in_flight_note the SAME way
    -- a one-sided implementation (only import_docs OR only plan_sources stamps) would
    break this equality even though it can't be caught by T12 itself (T12's archived doc
    never triggers the note in either path)."""
    text = (
        "## Why\n\nWe need a faster release cadence.\n\n"
        "## What Changes\n\nShip the thin slice first via `submit_order`.\n\n"
        "## Impact\n\n- Affected specs: importer\n"
    )
    live_rel = "openspec/changes/add-thing/proposal.md"
    path = tmp_path / live_rel
    path.parent.mkdir(parents=True)
    path.write_text(text, encoding="utf-8")
    profile = get_profile("openspec")
    assert profile.in_flight_note is not None  # otherwise this test proves nothing
    scan = ScanResult(root=str(tmp_path), files=(live_rel,))

    plan = plan_sources(tmp_path, scan, profile)
    assert len(plan.candidates) == 1
    candidate = plan.candidates[0]
    assert candidate.context.rstrip().endswith(profile.in_flight_note)

    item = ReviewedCandidate(candidate=candidate, action=ReviewAction.ACCEPT)
    request = build_doc_request(item, "v1", profile)

    monkeypatch.chdir(tmp_path)
    store = Store(tmp_path / ".sidegraph")
    reader = GraphifyReader(write_graph(tmp_path))
    report = import_docs(store, reader, [live_rel], profile="openspec")
    assert report.imported == 1
    imported = next(store.iter_decisions())
    assert imported.context.rstrip().endswith(profile.in_flight_note)
    assert request.parsed.context == imported.context


def _pending_proposal_for(store, request):
    return store.add_decision(
        Decision(
            title=request.parsed.title,
            kind=DecisionKind.ADR,
            status=DecisionStatus.PROPOSED,
            context=request.parsed.context,
            choice=request.parsed.choice,
            rejected=request.parsed.rejected,
            consequences=request.parsed.consequences,
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="doc-import", ref=request.ref),
        )
    )


def test_default_grant_never_ratifies_a_pending_proposal(tmp_path):
    """Red against the unfixed branch: the shared writer ratified ACCEPTED-over-PROPOSED
    with no grant. Also the test that makes Task 7's mutation 2 bite."""
    store = Store(tmp_path / ".sidegraph")
    reader = GraphifyReader(write_graph(tmp_path))
    request = request_for_status(DecisionStatus.ACCEPTED)
    proposal = _pending_proposal_for(store, request)

    result = apply_doc_candidate(store, reader, request)

    assert result.action == "skipped-existing"
    assert store.get_decision(proposal.id).status == DecisionStatus.PROPOSED


def test_doc_write_request_does_not_grant_ratification_by_default():
    """Red against a True default: a new caller must inherit the safe behavior by omission."""
    assert request_for_status(DecisionStatus.ACCEPTED).ratify_matching_proposal is False


@pytest.mark.parametrize(
    ("fault", "expected_status"),
    [
        ("canonical", RunStatus.INCOMPLETE),
        ("index", RunStatus.PARTIAL_RECOVERABLE),
        ("commit", RunStatus.PARTIAL_RECOVERABLE),
        ("keyboard", RunStatus.PARTIAL_RECOVERABLE),
        ("rebuild", RunStatus.PARTIAL_RECOVERABLE),
        ("verification", RunStatus.PARTIAL_RECOVERABLE),
    ],
)
def test_fault_boundaries_report_only_allowed_durable_states(
    tmp_path,
    monkeypatch,
    fault,
    expected_status,
):
    """Each persistence boundary must reconcile files, never promise rollback."""
    plan, review, reader = accepted_plan(tmp_path, count=1)
    store_dir = tmp_path / ".sidegraph"

    if fault == "rebuild":
        initial = Store(store_dir)
        initial.close()

    if fault == "canonical":
        monkeypatch.setattr(
            Store,
            "_write_decision_canonical",
            lambda self, decision: (_ for _ in ()).throw(RuntimeError("canonical failure")),
        )
    elif fault in {"index", "rebuild"}:
        monkeypatch.setattr(
            Store,
            "_index_write_decision",
            lambda self, decision: (_ for _ in ()).throw(RuntimeError("index failure")),
        )
        if fault == "rebuild":
            monkeypatch.setattr(
                Store,
                "_reload_index_from_canonical",
                lambda self, digest: (_ for _ in ()).throw(RuntimeError("rebuild failure")),
            )
    elif fault == "commit":
        monkeypatch.setattr(
            Store,
            "_commit",
            lambda self: (_ for _ in ()).throw(RuntimeError("commit failure")),
        )
    elif fault == "keyboard":
        monkeypatch.setattr(
            Store,
            "_index_write_decision",
            lambda self, decision: (_ for _ in ()).throw(KeyboardInterrupt()),
        )
    else:
        monkeypatch.setattr(
            "sidegraph.bootstrap.apply.verify_snapshot",
            lambda path: (_ for _ in ()).throw(RuntimeError("verification failure")),
        )

    report = apply_review(plan, review, store_dir=store_dir, reader=reader)
    assert report.status == expected_status
    if fault == "canonical":
        assert report.durable_candidate_keys == ()
        assert report.pending_candidate_keys == (review.items[0].candidate.key,)
    else:
        assert report.durable_candidate_keys == (review.items[0].candidate.key,)
        assert report.pending_candidate_keys == ()


@pytest.mark.parametrize("fault", ["binding-canonical", "binding-index", "binding-commit"])
def test_resume_repairs_the_complete_expected_binding_set(tmp_path, monkeypatch, fault):
    plan, review, reader = accepted_plan(tmp_path, count=1)
    store_dir = tmp_path / ".sidegraph"

    if fault == "binding-canonical":
        original = Store._write_bindings_file

        def fail_binding_canonical(self, record_id, items):
            raise RuntimeError("binding canonical failure")

        monkeypatch.setattr(Store, "_write_bindings_file", fail_binding_canonical)
    elif fault == "binding-index":
        original = Store._index_write_binding

        def fail_binding_index(self, binding):
            raise RuntimeError("binding index failure")

        monkeypatch.setattr(Store, "_index_write_binding", fail_binding_index)
    else:
        original = Store._commit
        calls = {"n": 0}

        def fail_binding_commit(self):
            calls["n"] += 1
            if calls["n"] == 4:
                raise RuntimeError("binding commit failure")
            return original(self)

        monkeypatch.setattr(Store, "_commit", fail_binding_commit)

    first = apply_review(plan, review, store_dir=store_dir, reader=reader)
    if fault == "binding-canonical":
        monkeypatch.setattr(Store, "_write_bindings_file", original)
    elif fault == "binding-index":
        monkeypatch.setattr(Store, "_index_write_binding", original)
    else:
        monkeypatch.setattr(Store, "_commit", original)

    resumed_plan = plan.model_copy(
        update={"catalog_fingerprint": fingerprint_catalog(load_canonical_catalog(store_dir))}
    )
    second = apply_review(resumed_plan, review, store_dir=store_dir, reader=reader)
    store = Store(store_dir)
    try:
        decision = next(iter(store.iter_decisions()))
        actual = {
            (binding.tier, store.get_entity(binding.entity_id).canonical_name)
            for binding in store.bindings_for_record(decision.id)
        }
    finally:
        store.close()

    assert first.status == RunStatus.PARTIAL_RECOVERABLE
    assert second.status == RunStatus.COMPLETE
    assert actual == {(2, "submit_order"), (1, "community:1")}


def test_manifest_tracks_only_committed_store_files(tmp_path):
    store_dir = tmp_path / ".sidegraph"
    store = Store(store_dir)
    store.close()
    (store_dir / "index.db-wal").write_bytes(b"derived")
    (store_dir / "decisions" / "ignored.tmp").write_bytes(b"temporary")
    (store_dir / "notes.txt").write_text("unrelated\n", encoding="utf-8")
    manifest = canonical_manifest(store_dir)
    assert set(manifest) == {".sidegraph/.gitignore", ".sidegraph/format"}


def test_manifest_labels_custom_store_paths_with_the_selected_directory(tmp_path):
    """Hard-coding .sidegraph makes recovery evidence misleading for --db overrides."""
    store_dir = tmp_path / "state"
    store = Store(store_dir)
    store.close()

    manifest = canonical_manifest(store_dir)

    assert set(manifest) == {"state/.gitignore", "state/format"}


def test_review_debt_uses_oldest_parseable_ulid_and_ignores_custom_ids():
    old = str(ULID.from_datetime(datetime.now(UTC) - timedelta(days=9)))
    catalog = CanonicalCatalog(
        decisions=(
            Decision(
                id=old,
                title="Old proposal",
                kind=DecisionKind.ADR,
                status=DecisionStatus.PROPOSED,
                context="c",
                choice="ch",
                valid_from=datetime.now(UTC) - timedelta(days=9),
                provenance=Provenance(source="manual"),
            ),
            Decision(
                id="hand-edited-id",
                title="Custom proposal",
                kind=DecisionKind.ADR,
                status=DecisionStatus.PROPOSED,
                context="c",
                choice="ch",
                valid_from=datetime.now(UTC),
                provenance=Provenance(source="manual"),
            ),
        )
    )
    count, oldest_days = proposal_debt(catalog)
    assert count == 2
    assert oldest_days == 9
