"""ADR projection keeps the complete source reasoning and writes no store state."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from sidegraph.schema import AnchorBinding, Decision, Descriptor, Entity, Fact, Provenance
from sidegraph.store import Store

NOW = datetime(2026, 10, 18, tzinfo=UTC)


def seed(root: Path) -> Path:
    """Tiny synthetic canonical store with independent trust and kind controls."""
    db = root / ".sidegraph"
    with Store(db) as store:
        entity = store.upsert_entity(
            Entity(
                canonical_name="Cache",
                descriptor=Descriptor(name="Cache", file_path="src/cache.py"),
            )
        )
        for rid, kind, status in [
            ("ADR", "adr", "accepted"),
            ("RULE", "constraint", "accepted"),
            ("DRAFT", "adr", "proposed"),
            ("DECLINED", "adr", "rejected"),
            ("TRAP", "gotcha", "accepted"),
        ]:
            decision = store.add_decision(
                Decision(
                    id=rid,
                    title=f"Synthetic {rid}",
                    kind=kind,
                    status=status,
                    context=f"Context for {rid}.",
                    choice=f"Choice for {rid}.",
                    rejected="A full rewrite loses the original evidence.",
                    consequences="Keep the scope explicit.",
                    valid_from=NOW,
                    provenance=Provenance(source="manual"),
                )
            )
            store.add_binding(
                AnchorBinding(record_id=decision.id, entity_id=entity.entity_id, tier=2)
            )
        for rid, status, closed in [
            ("FACT", "accepted", False),
            ("PENDING", "proposed", False),
            ("CLOSED", "accepted", True),
        ]:
            store.add_fact(
                Fact(
                    id=rid,
                    statement=f"{rid}: only12 observations on Python3.13; no general rate.",
                    source="Synthetic two-worker run; local only.",
                    supports=["ADR"],
                    status=status,
                    valid_from=NOW,
                    valid_to=NOW + timedelta(days=1) if closed else None,
                    provenance=Provenance(source="manual"),
                )
            )
    return db


def bytes_under(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_complete_projection_is_read_only_and_excludes_unratified_evidence(tmp_path):
    from sidegraph.adr import read_snapshot, render_record
    from sidegraph.profiles import get_profile

    db = seed(tmp_path)
    before = bytes_under(db)
    config = get_profile("generic-adr").adr_export
    with patch.object(Store, "__init__", side_effect=AssertionError("export constructed Store")):
        snapshot = read_snapshot(db)
        assert set(snapshot.exported) == {"ADR", "RULE"}
        text = render_record(
            snapshot, "ADR", {"ADR": "ADR.md", "RULE": "RULE.md"}, config, tmp_path / "adr"
        )
    assert "Context for ADR." in text and "Choice for ADR." in text
    assert "A full rewrite loses the original evidence." in text
    assert "only12 observations on Python3.13; no general rate." in text
    assert "Synthetic two-worker run; local only." in text
    assert "PENDING:" not in text and "CLOSED:" not in text
    assert "src/cache.py" in text and "decisions/ADR.json" in text
    assert bytes_under(db) == before


def test_minimal_custom_prose_cannot_remove_evidence_status_or_source(tmp_path):
    from sidegraph.adr import read_snapshot, render_record
    from sidegraph.profiles import AdrExport

    db = seed(tmp_path)
    config = AdrExport(
        directory="adr", template="$title\n$context\n$choice\n$rejected\n$consequences\n"
    )
    text = render_record(read_snapshot(db), "ADR", {"ADR": "ADR.md"}, config, tmp_path / "adr")
    assert "accepted" in text and "decisions/ADR.json" in text
    assert "facts/FACT.json" in text and "only12 observations" in text
    assert "src/cache.py" in text


@pytest.mark.parametrize(
    "field", ["$unknown", "$title $context $choice $rejected", "${title.__class__}"]
)
def test_invalid_template_cannot_project_partially(field):
    from sidegraph.profiles import AdrExport

    with pytest.raises(ValueError):
        AdrExport(directory="adr", template=field)


def test_archived_predecessor_keeps_a_pointer_to_cross_kind_successor(tmp_path):
    from sidegraph.adr import read_snapshot, render_record
    from sidegraph.profiles import get_profile

    db = seed(tmp_path)
    with Store(db) as store:
        store.add_decision(
            Decision(
                id="LATER",
                title="Later lesson",
                kind="lesson",
                status="accepted",
                context="A new observation changed the earlier decision.",
                choice="Use the new approach.",
                valid_from=NOW + timedelta(hours=1),
                supersedes="ADR",
                provenance=Provenance(source="manual"),
            )
        )
        store.compact()
    before = bytes_under(db)
    snapshot = read_snapshot(db)
    assert snapshot.exported["ADR"].status == "superseded"
    text = render_record(
        snapshot,
        "ADR",
        {"ADR": "ADR.md", "RULE": "RULE.md"},
        get_profile("generic-adr").adr_export,
        tmp_path / "adr",
    )
    assert "archive/" in text and "LATER.json" in text
    assert "LATER.md" not in text
    assert "src/cache.py" in text  # Compaction keeps canonical bindings hot.
    assert bytes_under(db) == before


def test_authored_closing_marker_and_unclosed_fence_do_not_claim_the_region(tmp_path):
    from sidegraph.adr import read_snapshot, render_record
    from sidegraph.profiles import get_profile

    db = seed(tmp_path)
    with Store(db) as store:
        store.add_decision(
            Decision(
                id="MARKER",
                title="Document the protocol",
                kind="adr",
                status="accepted",
                context="A marker must stay readable.\n```\n<!-- /sidegraph-adr -->",
                choice="Preserve the original source text.",
                valid_from=NOW,
                provenance=Provenance(source="manual"),
            )
        )
    text = render_record(
        read_snapshot(db),
        "MARKER",
        {"MARKER": "MARKER.md"},
        get_profile("generic-adr").adr_export,
        tmp_path / "adr",
    )
    assert "\n<!-- /sidegraph-adr -->" not in text
    assert "&lt;!-- /sidegraph-adr -->" in text


@pytest.mark.parametrize(
    "name,directory",
    [
        ("generic-adr", "docs/adr"),
        ("madr", "docs/adr"),
        ("superpowers", "docs/superpowers/adr"),
        ("genkovich-sdd", "docs/features/sidegraph/adr"),
        ("spec-kit", "specs/sidegraph/adr"),
        ("bmad", "_bmad-output/sidegraph-adr"),
        ("openspec", "openspec/adr"),
    ],
)
def test_profile_export_is_explicit_data_without_rewiring_its_reader(name, directory):
    from sidegraph.profiles import GENERIC_ADR_DIALECT, MADR_ADR_TEMPLATE, get_profile

    profile = get_profile(name)
    assert profile.adr_export.directory == directory
    if name == "madr":
        assert profile.adr_export.template == MADR_ADR_TEMPLATE
        assert profile.dialect is GENERIC_ADR_DIALECT and profile.marker is None


def test_archive_duplicate_admission_respects_existing_verifier_rules(tmp_path):
    from sidegraph.adr import read_snapshot

    db = seed(tmp_path)
    with Store(db) as store:
        store.add_decision(
            Decision(
                id="NEXT",
                title="Successor",
                kind="adr",
                status="accepted",
                context="New context.",
                choice="Next choice.",
                supersedes="ADR",
                valid_from=NOW,
                provenance=Provenance(source="manual"),
            )
        )
        hot = (db / "decisions/ADR.json").read_bytes()
        store.compact()
    segment = next((db / "archive").glob("*.jsonl"))
    (db / "archive/duplicate.jsonl").write_bytes(segment.read_bytes())
    assert read_snapshot(db).decisions["ADR"].status == "superseded"
    (db / "decisions/ADR.json").write_bytes(hot)
    with pytest.raises(ValueError, match="duplicate-ulid"):
        read_snapshot(db)


def test_doc_import_source_warning_is_retained_with_a_minimal_template(tmp_path):
    from sidegraph.adr import read_snapshot, render_record
    from sidegraph.profiles import AdrExport

    db = seed(tmp_path)
    with Store(db) as store:
        store.add_decision(
            Decision(
                id="DOC",
                title="An imported decision",
                kind="adr",
                status="accepted",
                context="Imported orientation.",
                choice="Consult the source.",
                valid_from=NOW,
                provenance=Provenance(source="doc-import", ref="docs/source.md"),
            )
        )
    text = render_record(
        read_snapshot(db),
        "DOC",
        {"DOC": "DOC.md"},
        AdrExport(template="$title\n$context\n$choice\n$rejected\n$consequences\n"),
        tmp_path / "adr",
    )
    assert "Document-derived orientation" in text and "docs/source.md" in text


def test_domain_index_groups_explicit_intent_without_inferring_from_paths(tmp_path):
    from sidegraph.adr import read_snapshot, render_index
    from sidegraph.schema import Domain

    db = seed(tmp_path)
    with Store(db) as store:
        store.add_domain(
            Domain(
                domain_id="AREA",
                slug="cache",
                title="Cache reasoning",
                summary="Durable cache tradeoffs.",
                status="accepted",
                path_prefixes=["src"],
                provenance=Provenance(source="manual"),
            )
        )
        entity = store.get_or_create_abstract_entity("domain:cache")
        store.add_binding(AnchorBinding(record_id="ADR", entity_id=entity.entity_id, tier=1))
    text = render_index(read_snapshot(db), {"ADR": "ADR.md", "RULE": "RULE.md"})
    explicit, unassigned = text.split("## Unassigned")
    assert "## Cache reasoning" in explicit and "](ADR.md)" in explicit
    assert "](RULE.md)" not in explicit and "](RULE.md)" in unassigned


def test_symlinked_store_uses_the_project_holding_the_link(tmp_path):
    from sidegraph.adr import read_snapshot, render_record
    from sidegraph.adr_files import plan_export
    from sidegraph.profiles import get_profile

    backing = seed(tmp_path / "backing")
    project = tmp_path / "project"
    project.mkdir()
    linked = project / ".sidegraph"
    linked.symlink_to(backing, target_is_directory=True)
    snapshot = read_snapshot(linked)
    assert snapshot.root == linked.absolute()
    config = get_profile("generic-adr").adr_export
    text = render_record(snapshot, "ADR", {"ADR": "ADR.md"}, config, project / "docs/adr")
    assert "](../../src/cache.py)" in text
    assert "](../../.sidegraph/decisions/ADR.json)" in text
    for out in (linked / "adr", backing / "adr"):
        with pytest.raises(ValueError):
            plan_export(snapshot, out, config)


def test_unlinkable_anchor_is_preserved_as_unverified_intent(tmp_path):
    from sidegraph.adr import read_snapshot, render_record
    from sidegraph.profiles import get_profile

    db = seed(tmp_path)
    with Store(db) as store:
        entity = store.upsert_entity(
            Entity(
                canonical_name="ExternalThing",
                descriptor=Descriptor(name="ExternalThing", file_path="../external.py"),
            )
        )
        store.add_binding(
            AnchorBinding(record_id="ADR", entity_id=entity.entity_id, tier=2, status="orphaned")
        )
    text = render_record(
        read_snapshot(db),
        "ADR",
        {"ADR": "ADR.md"},
        get_profile("generic-adr").adr_export,
        tmp_path / "adr",
    )
    assert "ExternalThing" in text and "../external.py" in text
    assert "](../external.py)" not in text


def test_default_export_adds_generic_detection_without_a_madr_marker(tmp_path):
    from sidegraph.adr import read_snapshot
    from sidegraph.adr_files import apply_plan, plan_export
    from sidegraph.profiles import detect_profile, get_profile

    db = seed(tmp_path)
    assert detect_profile(tmp_path).selected is None
    config = get_profile("generic-adr").adr_export
    apply_plan(plan_export(read_snapshot(db), tmp_path / config.directory, config))
    assert detect_profile(tmp_path).selected == "generic-adr"
    assert "madr" not in detect_profile(tmp_path).matches
