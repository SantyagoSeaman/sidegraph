from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sidegraph.bootstrap.catalog import CanonicalCatalog
from sidegraph.bootstrap.model import EditableCandidate, ScanResult, WarningCode
from sidegraph.bootstrap.planner import plan_sources, replan_edited_candidate
from sidegraph.engine.reader import GraphifyReader
from sidegraph.profiles import get_profile
from sidegraph.schema import Decision, DecisionKind, DecisionStatus, Provenance

FIXTURES = Path(__file__).parent / "fixtures" / "bootstrap" / "flows"
GRAPH_FIXTURE = Path(__file__).parent / "fixtures" / "bootstrap" / "graph.json"


def write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def write_adr(
    root: Path,
    choice: str = "Use SQLite.",
    rejected: str | None = "Do not add a hosted database.",
) -> Path:
    rejected_section = f"\n\n## Rejected\n\n{rejected}" if rejected is not None else ""
    return write(
        root / "docs/adr/001.md",
        (
            "# Use SQLite\n\n"
            "## Context\n\nThe project needs local state.\n\n"
            f"## Decision\n\n{choice}{rejected_section}\n"
        ),
    )


def graphless_plan(root: Path, doc: Path):
    scan = ScanResult(root=str(root), files=(doc.relative_to(root).as_posix(),))
    return plan_sources(root, scan, get_profile("generic-adr"))


def catalog_decision(
    ref: str,
    *,
    status: DecisionStatus = DecisionStatus.ACCEPTED,
) -> Decision:
    return Decision(
        id=f"existing-{status}",
        title="Existing",
        kind=DecisionKind.ADR,
        status=status,
        context="Existing context.",
        choice="Existing choice.",
        valid_from=datetime(2026, 8, 1, tzinfo=UTC),
        provenance=Provenance(source="doc-import", ref=ref),
    )


def plan_with_inputs(
    root: Path,
    doc: Path,
    *,
    catalog: CanonicalCatalog | None = None,
    reader: GraphifyReader | None = None,
):
    scan = ScanResult(root=str(root), files=(doc.relative_to(root).as_posix(),))
    return plan_sources(
        root,
        scan,
        get_profile("generic-adr"),
        catalog=catalog,
        reader=reader,
    )


def test_graphless_plan_never_constructs_store(tmp_path, monkeypatch):
    doc = write_adr(tmp_path, rejected="Do not use Redis; it adds an outage domain.")
    scan = ScanResult(root=str(tmp_path), files=(doc.relative_to(tmp_path).as_posix(),))

    def forbidden(*args, **kwargs):
        raise AssertionError("Store must not be opened during planning")

    monkeypatch.setattr("sidegraph.store.Store", forbidden)
    plan = plan_sources(tmp_path, scan, get_profile("generic-adr"))
    assert len(plan.candidates) == 1
    assert plan.candidates[0].anchors == ()
    assert plan.candidates[0].source_hash
    assert not (tmp_path / ".sidegraph").exists()


@pytest.mark.parametrize(
    "status",
    [DecisionStatus.ACCEPTED, DecisionStatus.PROPOSED, DecisionStatus.REJECTED],
)
def test_catalog_duplicate_is_a_warning_and_not_removed(tmp_path, status):
    doc = write_adr(tmp_path)
    catalog = CanonicalCatalog(decisions=(catalog_decision("docs/adr/001.md", status=status),))

    plan = plan_with_inputs(tmp_path, doc, catalog=catalog)

    assert len(plan.candidates) == 1
    assert WarningCode.DUPLICATE_CANONICAL in plan.candidates[0].warnings


def test_superseded_catalog_record_does_not_warn_as_a_duplicate(tmp_path):
    doc = write_adr(tmp_path)
    catalog = CanonicalCatalog(
        decisions=(catalog_decision("docs/adr/001.md", status=DecisionStatus.SUPERSEDED),)
    )

    plan = plan_with_inputs(tmp_path, doc, catalog=catalog)

    assert WarningCode.DUPLICATE_CANONICAL not in plan.candidates[0].warnings


def test_reader_enrichment_reports_resolved_ambiguous_and_unresolved(tmp_path):
    docs = (
        write_adr(tmp_path, choice="Use `RetryClient`."),
        write(
            tmp_path / "docs/adr/002.md",
            "# Cache\n\n## Decision\n\nUse `Cache`.\n\n## Rejected\n\nNo cache.\n",
        ),
        write(
            tmp_path / "docs/adr/003.md",
            "# Missing\n\n## Decision\n\nUse `MissingThing`.\n\n## Rejected\n\nNo missing thing.\n",
        ),
    )
    scan = ScanResult(
        root=str(tmp_path),
        files=tuple(doc.relative_to(tmp_path).as_posix() for doc in docs),
    )

    plan = plan_sources(
        tmp_path,
        scan,
        get_profile("generic-adr"),
        reader=GraphifyReader(GRAPH_FIXTURE),
    )

    mention_anchors = [candidate.anchors[0] for candidate in plan.candidates]
    assert [anchor.status for anchor in mention_anchors] == [
        "resolved",
        "ambiguous",
        "unresolved",
    ]
    assert [anchor.tier for anchor in mention_anchors] == [2, 1, 2]
    assert mention_anchors[1].candidates == ("code-2", "code-3")
    assert WarningCode.AMBIGUOUS_ANCHOR in plan.candidates[1].warnings
    assert WarningCode.UNRESOLVED_ANCHOR in plan.candidates[2].warnings


def test_document_anchor_is_separate_from_mention_intents_and_deterministic(tmp_path):
    graph_path = tmp_path / "graph.json"
    graph = json.loads(GRAPH_FIXTURE.read_text(encoding="utf-8"))
    graph["nodes"].insert(
        0,
        {
            "id": "doc-0",
            "label": "Earlier node",
            "norm_label": "earlier node",
            "file_type": "document",
            "source_file": "docs/adr/001-retry.md",
            "community": 1,
        },
    )
    graph_path.write_text(json.dumps(graph), encoding="utf-8")
    graph_before = graph_path.read_bytes()
    doc = write(
        tmp_path / "docs/adr/001-retry.md",
        "# Retry\n\n## Decision\n\nUse `RetryClient`.\n\n## Rejected\n\nNo retry.\n",
    )

    candidate = plan_with_inputs(
        tmp_path,
        doc,
        reader=GraphifyReader(graph_path),
    ).candidates[0]

    assert tuple(anchor.name for anchor in candidate.anchor_intents) == ("RetryClient",)
    assert candidate.file_anchor_intent is not None
    assert candidate.file_anchor_intent.name == "Earlier node"
    assert tuple(anchor.descriptor.name for anchor in candidate.anchors) == (
        "RetryClient",
        "Earlier node",
    )
    assert graph_path.read_bytes() == graph_before


def test_catalog_or_graph_change_invalidates_the_plan(tmp_path):
    doc = write_adr(tmp_path)
    graph_v1 = tmp_path / "v1.json"
    graph_v2 = tmp_path / "v2.json"
    graph = json.loads(GRAPH_FIXTURE.read_text(encoding="utf-8"))
    graph_v1.write_text(json.dumps(graph), encoding="utf-8")
    graph["nodes"][0]["label"] = "Changed retry ADR"
    graph_v2.write_text(json.dumps(graph), encoding="utf-8")

    original = plan_with_inputs(
        tmp_path,
        doc,
        catalog=CanonicalCatalog(),
        reader=GraphifyReader(graph_v1),
    )
    catalog_changed = plan_with_inputs(
        tmp_path,
        doc,
        catalog=CanonicalCatalog(decisions=(catalog_decision("docs/adr/001.md"),)),
        reader=GraphifyReader(graph_v1),
    )
    graph_changed = plan_with_inputs(
        tmp_path,
        doc,
        catalog=CanonicalCatalog(),
        reader=GraphifyReader(graph_v2),
    )

    assert original.fingerprint != catalog_changed.fingerprint
    assert original.fingerprint != graph_changed.fingerprint
    assert original.catalog_fingerprint
    assert original.graph_version == GraphifyReader(graph_v1).graph_version()


def test_secret_is_redacted_before_candidate_and_plan_serialization(tmp_path):
    doc = write_adr(tmp_path, choice="Use token=abcdefghijklmnopqrstuvwxyz123456.")
    plan = graphless_plan(tmp_path, doc)
    payload = plan.model_dump_json()
    assert "abcdefghijklmnopqrstuvwxyz123456" not in payload
    assert "[REDACTED]" in payload


@pytest.mark.parametrize(
    ("choice", "rejected", "expected"),
    [
        ("Use SQLite.", None, WarningCode.MISSING_REJECTED),
        ("Currently the system uses SQLite.", None, WarningCode.CURRENT_STATE),
    ],
)
def test_warning_rules_are_deterministic(tmp_path, choice, rejected, expected):
    candidate = graphless_plan(tmp_path, write_adr(tmp_path, choice, rejected)).candidates[0]
    assert expected in candidate.warnings


def test_current_state_warning_order_is_stable(tmp_path):
    candidate = graphless_plan(
        tmp_path,
        write_adr(tmp_path, "Currently the system uses SQLite.", None),
    ).candidates[0]
    assert candidate.warnings == (
        WarningCode.MISSING_REJECTED,
        WarningCode.CURRENT_STATE,
    )


def test_unparseable_choice_is_a_plan_issue_not_a_stored_candidate(tmp_path):
    doc = write(tmp_path / "docs/adr/empty.md", "# Empty\n\n## Context\n\nOnly context.\n")
    plan = graphless_plan(tmp_path, doc)
    assert plan.candidates == ()
    assert plan.issues[0].warning == WarningCode.MISSING_CHOICE


@pytest.mark.parametrize(
    ("profile_name", "rel_path", "expected_candidates"),
    [
        (
            "generic-adr",
            "generic-adr/docs/adr/001-retry.md",
            (
                (
                    "Use bounded retries",
                    "Use three bounded retries in `src/client.py`.",
                    "docs/adr/001-retry.md",
                ),
            ),
        ),
        (
            "superpowers",
            "superpowers/docs/superpowers/specs/2026-07-01-cache-design.md",
            (
                (
                    "Cache design",
                    "Use one process-local immutable cache in `src/cache.py`.",
                    "docs/superpowers/specs/2026-07-01-cache-design.md",
                ),
            ),
        ),
        (
            "genkovich-sdd",
            "genkovich-sdd/docs/features/cache/adr/001.md",
            (
                (
                    "Cache ADR",
                    "Use an in-process cache in `src/cache.py`.",
                    "docs/features/cache/adr/001.md",
                ),
            ),
        ),
        (
            "spec-kit",
            "spec-kit/specs/cache/plan.md",
            (
                (
                    "Implementation Plan: Cache",
                    "Implement a bounded in-process cache in `src/cache.py`.",
                    "specs/cache/plan.md",
                ),
            ),
        ),
        (
            "bmad",
            "bmad/_bmad-output/planning-artifacts/architecture/cache/ARCHITECTURE-SPINE.md",
            (
                (
                    "AD-1 — Cache graph reads",
                    "Use one bounded in-process cache in `src/cache.py`.",
                    (
                        "_bmad-output/planning-artifacts/architecture/cache/"
                        "ARCHITECTURE-SPINE.md#ad-1-cache-graph-reads"
                    ),
                ),
            ),
        ),
    ],
)
def test_profile_fixture_produces_stable_candidates(profile_name, rel_path, expected_candidates):
    root = FIXTURES / profile_name
    scan = ScanResult(root=str(root), files=(Path(rel_path).relative_to(profile_name).as_posix(),))

    plan = plan_sources(root, scan, get_profile(profile_name))

    assert tuple((item.title, item.choice, item.ref) for item in plan.candidates) == (
        expected_candidates
    )
    assert plan.profile == profile_name


def test_split_heading_secret_is_redacted_before_fragment_and_plan_serialization(tmp_path):
    secret = "abcdefghijklmnopqrstuvwxyz123456"
    rel = "_bmad-output/planning-artifacts/architecture/cache/ARCHITECTURE-SPINE.md"
    doc = write(
        tmp_path / rel,
        (
            "# Architecture Spine\n\n"
            "## Design Paradigm\n\nLocal-first tooling.\n\n"
            "## Invariants & Rules\n\n"
            f"### AD-1 token={secret}\n\nUse a bounded local cache.\n\n"
            "## Deferred\n\nRemote caching is deferred.\n"
        ),
    )
    scan = ScanResult(root=str(tmp_path), files=(doc.relative_to(tmp_path).as_posix(),))

    plan = plan_sources(tmp_path, scan, get_profile("bmad"))

    assert len(plan.candidates) == 1
    candidate = plan.candidates[0]
    assert candidate.fragment == "ad-1-redacted"
    assert candidate.ref == f"{rel}#ad-1-redacted"
    assert candidate.title == "AD-1 [REDACTED]"
    key_fields = "\0".join(
        (
            candidate.ref,
            candidate.title,
            candidate.context,
            candidate.choice,
            candidate.rejected or "",
            candidate.consequences or "",
        )
    )
    assert secret not in key_fields
    assert secret not in plan.model_dump_json()


def test_duplicate_candidate_is_reported_and_first_candidate_wins(tmp_path):
    doc = write_adr(tmp_path)
    rel = doc.relative_to(tmp_path).as_posix()
    scan = ScanResult(root=str(tmp_path), files=(rel, rel))

    plan = plan_sources(tmp_path, scan, get_profile("generic-adr"))

    assert len(plan.candidates) == 1
    assert plan.issues[-1].warning == WarningCode.DUPLICATE_PLAN
    assert plan.issues[-1].ref == rel


def test_same_redacted_content_at_different_refs_is_reported_as_duplicate(tmp_path):
    content = (
        "# Use SQLite\n\n"
        "## Context\n\nThe project needs local state.\n\n"
        "## Decision\n\nUse SQLite.\n\n"
        "## Rejected\n\nDo not add a hosted database.\n"
    )
    first = write(tmp_path / "docs/adr/a.md", content)
    second = write(tmp_path / "docs/adr/b.md", content)
    scan = ScanResult(
        root=str(tmp_path),
        files=(first.relative_to(tmp_path).as_posix(), second.relative_to(tmp_path).as_posix()),
    )

    plan = plan_sources(tmp_path, scan, get_profile("generic-adr"))

    assert tuple(candidate.ref for candidate in plan.candidates) == ("docs/adr/a.md",)
    assert plan.issues[-1].warning == WarningCode.DUPLICATE_PLAN
    assert plan.issues[-1].ref == "docs/adr/b.md"


def test_same_context_free_content_at_different_refs_is_reported_as_duplicate(tmp_path):
    content = (
        "# Use SQLite\n\n"
        "## Decision\n\nUse SQLite.\n\n"
        "## Rejected\n\nDo not add a hosted database.\n"
    )
    first = write(tmp_path / "docs/adr/a.md", content)
    second = write(tmp_path / "docs/adr/b.md", content)
    scan = ScanResult(
        root=str(tmp_path),
        files=(first.relative_to(tmp_path).as_posix(), second.relative_to(tmp_path).as_posix()),
    )

    plan = plan_sources(tmp_path, scan, get_profile("generic-adr"))

    assert tuple(candidate.ref for candidate in plan.candidates) == ("docs/adr/a.md",)
    assert plan.issues[-1].warning == WarningCode.DUPLICATE_PLAN
    assert plan.issues[-1].ref == "docs/adr/b.md"


def test_fingerprint_includes_inputs_that_only_produce_issues(tmp_path):
    doc = write(tmp_path / "docs/adr/empty.md", "# Empty\n\n## Context\n\nFirst context.\n")
    first = graphless_plan(tmp_path, doc)
    write(doc, "# Empty\n\n## Context\n\nChanged context.\n")
    second = graphless_plan(tmp_path, doc)

    assert first.candidates == second.candidates == ()
    assert first.fingerprint != second.fingerprint
    assert first.source_fingerprints[0].sha256 != second.source_fingerprints[0].sha256


def test_replan_edited_candidate_redacts_and_recomputes_quality_warnings(tmp_path):
    candidate = graphless_plan(tmp_path, write_adr(tmp_path)).candidates[0]
    edited = EditableCandidate(
        title="Cache token=abcdefghijklmnopqrstuvwxyz123456",
        context="Currently local.",
        choice="Currently the system uses SQLite.",
        rejected=None,
        consequences=None,
        kind=DecisionKind.ADR,
    )

    replanned = replan_edited_candidate(candidate, edited, reader=None, catalog=None)

    assert "abcdefghijklmnopqrstuvwxyz123456" not in replanned.model_dump_json()
    assert "[REDACTED]" in replanned.title
    assert replanned.key != candidate.key
    assert replanned.anchors == ()
    assert replanned.warnings == (
        WarningCode.MISSING_REJECTED,
        WarningCode.CURRENT_STATE,
    )


# -- openspec profile: E1/E2 in the bootstrap seam (design/superpowers/specs/2026-08-06-
# openspec-profile-design.md §5/§6) ----------------------------------------------------


def test_openspec_bootstrap_candidates_get_synthesized_titles(tmp_path):
    """Spec T11 (review Blocker 2): the planner must carry E1 the same way `import_docs`
    does — without it, `parse_decision_docs` rejects every H1-less openspec doc outright
    and `plan_sources` yields zero candidates for the whole corpus (design note §5,
    review probe5 vs probe8: 68 records / 113 zero-record files under bootstrap vs
    339 / 12 under the importer). Red against unfixed bootstrap (0 candidates)."""
    doc = write(
        tmp_path / "openspec/changes/add-thing/proposal.md",
        (
            "## Why\n\nWe need a faster release cadence.\n\n"
            "## What Changes\n\nShip the thin slice first.\n\n"
            "## Impact\n\n- Affected specs: importer\n"
        ),
    )
    scan = ScanResult(root=str(tmp_path), files=(doc.relative_to(tmp_path).as_posix(),))

    plan = plan_sources(tmp_path, scan, get_profile("openspec"))

    assert len(plan.candidates) == 1
    assert plan.candidates[0].title == "add-thing (proposal)"
    assert "faster release cadence" in plan.candidates[0].context


def test_openspec_bootstrap_echo_refusal_fires_post_e2_normalization(tmp_path):
    """Spec T15: bootstrap's pre-existing `_choice_is_context_fallback` refusal
    (`planner.py:88-91`) must still fire for an archived-path echo-shaped doc after E2
    normalizes the stamp — a bootstrap normalization that left the stamp ON-DISK would
    make `_context_body`'s `removesuffix` no-op against it, so the predicate would never
    fire (silently disabling the refusal). Red against exactly that: a ref-only bootstrap
    normalization (round-3 M3) — `plan.issues[0].ref` would carry the on-disk archived
    path instead of the normalized one, and (under the bug) `plan.candidates` would carry
    the echoed record instead of being empty."""
    archived_rel = "openspec/changes/archive/2026-01-01-add-thing/design.md"
    doc = write(tmp_path / archived_rel, "## Context\n\nWe need a faster release cadence.\n")
    scan = ScanResult(root=str(tmp_path), files=(doc.relative_to(tmp_path).as_posix(),))

    plan = plan_sources(tmp_path, scan, get_profile("openspec"))

    assert plan.candidates == ()
    assert len(plan.issues) == 1
    assert plan.issues[0].warning == WarningCode.MISSING_CHOICE
    assert plan.issues[0].ref == "openspec/changes/add-thing/design.md"
