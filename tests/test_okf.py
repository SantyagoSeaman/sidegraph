"""Unit tests for okf.build_bundle — the OKF v0.1 mapping contract (see
design/superpowers/specs/2026-07-23-okf-export-design.md). Stores are seeded through
Store; assertions pin exact frontmatter lines, section shapes, filenames, and link
targets."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from ulid import ULID

from sidegraph.okf import build_bundle
from sidegraph.schema import (
    AnchorBinding,
    Decision,
    DecisionKind,
    DecisionStatus,
    Descriptor,
    Domain,
    DomainStatus,
    Entity,
    Fact,
    Provenance,
)
from sidegraph.store import Store

NOW = datetime(2026, 7, 23, 12, 0, 0, tzinfo=UTC)


def _store(tmp_path: Path) -> Store:
    return Store(tmp_path / "store")


def _decision(**overrides: object) -> Decision:
    base: dict = {
        "title": "Use a sqlite index",
        "kind": DecisionKind.ADR,
        "status": DecisionStatus.ACCEPTED,
        "context": "Queries were slow.",
        "choice": "Derive a sqlite index from canonical files.",
        "valid_from": NOW,
        "provenance": Provenance(source="manual"),
    }
    base.update(overrides)
    return Decision(**base)


def _fact(**overrides: object) -> Fact:
    base: dict = {
        "statement": "sqlite handles our volume fine.",
        "source": "benchmark run",
        "status": DecisionStatus.ACCEPTED,
        "valid_from": NOW,
        "provenance": Provenance(source="manual"),
    }
    base.update(overrides)
    return Fact(**base)


def _domain(slug: str = "auth", **overrides: object) -> Domain:
    base: dict = {
        "slug": slug,
        "title": "Authentication",
        "summary": "Everything about signing users in.",
        "status": DomainStatus.ACCEPTED,
        "provenance": Provenance(source="manual"),
    }
    base.update(overrides)
    return Domain(**base)


def _single(bundle: dict[str, str], section: str) -> tuple[str, str]:
    """The one concept file under ``section/`` (excluding its index)."""
    paths = [p for p in bundle if p.startswith(f"{section}/") and p != f"{section}/index.md"]
    assert len(paths) == 1, paths
    return paths[0], bundle[paths[0]]


class TestDecisionMapping:
    def test_full_decision_maps_all_fields(self, tmp_path: Path) -> None:
        s = _store(tmp_path)
        d = s.add_decision(
            _decision(
                rejected="Tried a flat file scan.",
                consequences="Index rebuilds on clone.",
                layer="technical",
                valid_to=NOW + timedelta(days=1),
                provenance=Provenance(source="agent", ref="abc123", author="alex"),
            )
        )
        path, text = _single(build_bundle(s), "decisions")
        assert path == f"decisions/use-a-sqlite-index-{d.id.lower()}.md"
        lines = text.splitlines()
        assert lines[0] == "---"
        assert "type: decision" in lines
        assert 'title: "Use a sqlite index"' in lines
        assert 'description: "Derive a sqlite index from canonical files."' in lines
        assert f"timestamp: {NOW.isoformat()}" in lines
        assert f"id: {d.id.lower()}" in lines
        assert "kind: adr" in lines
        assert "status: accepted" in lines
        assert "scope: repo" in lines
        assert "layer: technical" in lines
        assert f"valid_from: {NOW.isoformat()}" in lines
        assert f"valid_to: {(NOW + timedelta(days=1)).isoformat()}" in lines
        assert (
            text.index("# Context")
            < text.index("# Choice")
            < text.index("# Rejected")
            < text.index("# Consequences")
            < text.index("# Provenance")
        )
        assert "- source: agent" in lines
        assert "- ref: abc123" in lines
        assert "- author: alex" in lines
        assert text.endswith("\n") and not text.endswith("\n\n")

    def test_minimal_decision_omits_unset_keys_and_sections(self, tmp_path: Path) -> None:
        s = _store(tmp_path)
        s.add_decision(_decision())
        _, text = _single(build_bundle(s), "decisions")
        for absent in (
            "layer:",
            "valid_to:",
            "supersedes:",
            "# Rejected",
            "# Consequences",
            "# Anchors",
            "# History",
        ):
            assert absent not in text, absent

    def test_supersession_links_both_directions(self, tmp_path: Path) -> None:
        s = _store(tmp_path)
        old = s.add_decision(_decision(title="Old way"))
        new = s.add_decision(_decision(title="New way", supersedes=old.id))
        bundle = build_bundle(s)
        old_path = f"decisions/old-way-{old.id.lower()}.md"
        new_path = f"decisions/new-way-{new.id.lower()}.md"
        assert f"supersedes: {old.id.lower()}" in bundle[new_path].splitlines()
        assert f"- Supersedes [Old way](/{old_path})" in bundle[new_path]
        assert f"- Superseded by [New way](/{new_path})" in bundle[old_path]
        assert "status: superseded" in bundle[old_path].splitlines()  # auto-closed on add

    def test_multiple_successors_sorted_by_id(self, tmp_path: Path) -> None:
        s = _store(tmp_path)
        old = s.add_decision(_decision(title="Old way"))
        s1 = str(ULID.from_datetime(NOW + timedelta(seconds=1)))
        s2 = str(ULID.from_datetime(NOW + timedelta(seconds=2)))
        s.add_decision(_decision(title="Fork A", id=s1, supersedes=old.id))
        s.add_decision(_decision(title="Fork B", id=s2, supersedes=old.id))
        text = build_bundle(s)[f"decisions/old-way-{old.id.lower()}.md"]
        assert text.index("Fork A") < text.index("Fork B")

    def test_dangling_references_render_plain_id(self, tmp_path: Path) -> None:
        # Store guards reject dangling ids on write; simulate a DIRTY store by deleting
        # the predecessor's canonical file and the index (forces a full rebuild). Covers
        # both dangling shapes at once: a successor's `supersedes` and a fact's
        # `supports` pointing at the deleted record.
        db = tmp_path / "store"
        s = Store(db)
        old = s.add_decision(_decision(title="Old way"))
        new = s.add_decision(_decision(title="New way", supersedes=old.id))
        f = s.add_fact(_fact(supports=[old.id]))
        del s
        (db / "decisions" / f"{old.id}.json").unlink()
        (db / "index.db").unlink()
        bundle = build_bundle(Store(db))
        dec_text = bundle[f"decisions/new-way-{new.id.lower()}.md"]
        assert f"- Supersedes {old.id}" in dec_text
        assert "(/decisions/old-way" not in dec_text
        fact_text = bundle[f"facts/sqlite-handles-our-volume-fine-{f.id.lower()}.md"]
        assert f"- {old.id}" in fact_text
        assert "(/decisions/old-way" not in fact_text

    def test_cross_type_id_collision_renders_plain_ids(self, tmp_path: Path) -> None:
        # A Decision and a Fact sharing one ULID is store corruption (each add-path
        # enforces uniqueness only within its own type; reachable via explicit ids only).
        # A reference to the colliding id must render as a plain id, never guess a target.
        s = _store(tmp_path)
        shared = str(ULID.from_datetime(NOW))
        s.add_decision(_decision(title="Collided", id=shared))
        s.add_fact(_fact(id=shared))
        succ = s.add_decision(_decision(title="Successor", supersedes=shared))
        bundle = build_bundle(s)
        text = bundle[f"decisions/successor-{succ.id.lower()}.md"]
        assert f"- Supersedes {shared}" in text
        assert "- Supersedes [" not in text
        # both concept files still export at their own per-type paths
        assert f"decisions/collided-{shared.lower()}.md" in bundle
        assert f"facts/sqlite-handles-our-volume-fine-{shared.lower()}.md" in bundle


class TestFactMapping:
    def test_fact_maps_statement_source_supports(self, tmp_path: Path) -> None:
        s = _store(tmp_path)
        d = s.add_decision(_decision())
        f = s.add_fact(_fact(supports=[d.id]))
        bundle = build_bundle(s)
        path = f"facts/sqlite-handles-our-volume-fine-{f.id.lower()}.md"
        text = bundle[path]
        lines = text.splitlines()
        assert "type: fact" in lines
        assert 'title: "sqlite handles our volume fine."' in lines
        assert f"id: {f.id.lower()}" in lines
        assert "# Source" in text
        assert "benchmark run" in text
        assert f"- [Use a sqlite index](/decisions/use-a-sqlite-index-{d.id.lower()}.md)" in text

    def test_fact_title_truncated_to_80(self, tmp_path: Path) -> None:
        s = _store(tmp_path)
        s.add_fact(_fact(statement="x" * 100))
        _, text = _single(build_bundle(s), "facts")
        assert f'title: "{"x" * 79}…"' in text.splitlines()


class TestDomainMapping:
    def test_domain_collapse_latest_row_wins(self, tmp_path: Path) -> None:
        s = _store(tmp_path)
        old = s.add_domain(_domain(summary="Old summary."))
        new = s.supersede_domain(
            old.domain_id, _domain(summary="New summary.", supersedes=old.domain_id)
        )
        bundle = build_bundle(s)
        concept_paths = [p for p in bundle if p.startswith("domains/") and p != "domains/index.md"]
        assert concept_paths == ["domains/auth.md"]
        lines = bundle["domains/auth.md"].splitlines()
        assert "New summary." in bundle["domains/auth.md"]
        assert "Old summary." not in bundle["domains/auth.md"]
        assert f"id: {new.domain_id.lower()}" in lines
        assert "slug: auth" in lines
        assert "type: domain" in lines

    def test_domain_parent_hierarchy_link(self, tmp_path: Path) -> None:
        s = _store(tmp_path)
        parent = s.add_domain(_domain(slug="platform", title="Platform", summary="Base layer."))
        s.add_domain(_domain(slug="auth", parent_id=parent.domain_id))
        bundle = build_bundle(s)
        assert "- Parent: [Platform](/domains/platform.md)" in bundle["domains/auth.md"]

    def test_domain_binding_links_to_domain_page_not_entity(self, tmp_path: Path) -> None:
        s = _store(tmp_path)
        s.add_domain(_domain(slug="auth"))
        d = s.add_decision(_decision())
        ent = s.get_or_create_abstract_entity("domain:auth")
        s.add_binding(AnchorBinding(record_id=d.id, entity_id=ent.entity_id, tier=1))
        bundle = build_bundle(s)
        dec_text = bundle[f"decisions/use-a-sqlite-index-{d.id.lower()}.md"]
        assert "- [Authentication](/domains/auth.md) — affects, tier 1" in dec_text
        assert not any(p.startswith("entities/") for p in bundle)
        assert (
            f"- [Use a sqlite index](/decisions/use-a-sqlite-index-{d.id.lower()}.md)"
            in bundle["domains/auth.md"]
        )

    def test_domain_entity_without_row_exports_as_concept(self, tmp_path: Path) -> None:
        s = _store(tmp_path)
        d = s.add_decision(_decision())
        ent = s.get_or_create_abstract_entity("domain:ghost")
        s.add_binding(AnchorBinding(record_id=d.id, entity_id=ent.entity_id, tier=1))
        bundle = build_bundle(s)
        path = f"entities/domainghost-{ent.entity_id.lower()}.md"
        assert path in bundle
        assert "type: concept" in bundle[path].splitlines()

    def test_community_binding_skipped_entirely(self, tmp_path: Path) -> None:
        s = _store(tmp_path)
        d = s.add_decision(_decision())
        ent = s.get_or_create_abstract_entity("community:7")
        s.add_binding(AnchorBinding(record_id=d.id, entity_id=ent.entity_id, tier=1))
        bundle = build_bundle(s)
        assert "# Anchors" not in bundle[f"decisions/use-a-sqlite-index-{d.id.lower()}.md"]
        assert not any(p.startswith("entities/") for p in bundle)


class TestEntityMapping:
    def test_concrete_entity_page_with_backlinks(self, tmp_path: Path) -> None:
        s = _store(tmp_path)
        e = s.upsert_entity(
            Entity(
                canonical_name="auth.login",
                descriptor=Descriptor(name="login", file_path="src/auth.py"),
            )
        )
        d = s.add_decision(_decision())
        f = s.add_fact(_fact())
        s.add_binding(
            AnchorBinding(record_id=d.id, entity_id=e.entity_id, tier=2, relation="modifies")
        )
        s.add_binding(AnchorBinding(record_id=f.id, entity_id=e.entity_id, tier=2))
        bundle = build_bundle(s)
        path = f"entities/authlogin-{e.entity_id.lower()}.md"
        lines = bundle[path].splitlines()
        assert "type: code-entity" in lines
        assert 'title: "login"' in lines
        assert 'name: "auth.login"' in lines
        assert 'file_path: "src/auth.py"' in lines
        body = bundle[path].split("# Anchored records")[1]
        assert body.index("Use a sqlite index") < body.index("sqlite handles our volume fine.")
        assert "— modifies" in body
        dec_text = bundle[f"decisions/use-a-sqlite-index-{d.id.lower()}.md"]
        assert f"- [login](/{path}) — modifies, tier 2" in dec_text

    def test_unreferenced_entity_not_exported(self, tmp_path: Path) -> None:
        s = _store(tmp_path)
        s.upsert_entity(Entity(canonical_name="lonely"))
        s.add_decision(_decision())
        assert not any(p.startswith("entities/") for p in build_bundle(s))


class TestNamingAndEscaping:
    def test_slug_truncated_at_60(self, tmp_path: Path) -> None:
        s = _store(tmp_path)
        s.add_decision(_decision(title="w" + "o" * 100))
        path, _ = _single(build_bundle(s), "decisions")
        slug = path.removeprefix("decisions/").rsplit("-", 1)[0]
        assert slug == "w" + "o" * 59

    def test_non_ascii_title_falls_back_to_record(self, tmp_path: Path) -> None:
        s = _store(tmp_path)
        d = s.add_decision(_decision(title="Решение о хранилище"))
        path, _ = _single(build_bundle(s), "decisions")
        assert path == f"decisions/record-{d.id.lower()}.md"

    def test_duplicate_titles_disambiguated_by_ulid(self, tmp_path: Path) -> None:
        s = _store(tmp_path)
        s.add_decision(_decision())
        s.add_decision(_decision())
        bundle = build_bundle(s)
        assert (
            len([p for p in bundle if p.startswith("decisions/") and p != "decisions/index.md"])
            == 2
        )

    def test_frontmatter_quoting_and_label_escaping(self, tmp_path: Path) -> None:
        s = _store(tmp_path)
        old = s.add_decision(_decision(title='He said "quote": [ok]'))
        s.add_decision(_decision(title="Successor", supersedes=old.id))
        bundle = build_bundle(s)
        old_path = next(p for p in bundle if p.startswith("decisions/he-said"))
        assert 'title: "He said \\"quote\\": [ok]"' in bundle[old_path].splitlines()
        succ_path = next(p for p in bundle if p.startswith("decisions/successor"))
        assert '- Supersedes [He said "quote": \\[ok\\]](' in bundle[succ_path]


class TestBundleStructure:
    def test_empty_store_exports_root_index_only(self, tmp_path: Path) -> None:
        bundle = build_bundle(_store(tmp_path))
        assert set(bundle) == {"index.md"}
        lines = bundle["index.md"].splitlines()
        assert lines[0] == "---"
        assert 'okf_version: "0.1"' in lines[:4]
        assert "generator: sidegraph" in lines[:4]
        assert "## Contents" not in bundle["index.md"]

    def test_root_index_lists_only_present_sections(self, tmp_path: Path) -> None:
        s = _store(tmp_path)
        s.add_decision(_decision())
        text = build_bundle(s)["index.md"]
        assert "- [decisions/](/decisions/index.md) — 1 decision" in text
        assert "facts/" not in text
        assert "domains/" not in text
        assert "entities/" not in text

    def test_section_index_sorted_by_id_not_title(self, tmp_path: Path) -> None:
        s = _store(tmp_path)
        first = str(ULID.from_datetime(NOW - timedelta(seconds=10)))
        second = str(ULID.from_datetime(NOW))
        s.add_decision(_decision(title="Beta", id=first))
        s.add_decision(_decision(title="Alpha", id=second))
        idx = build_bundle(s)["decisions/index.md"]
        assert idx.index("Beta") < idx.index("Alpha")

    def test_log_groups_by_date_newest_first(self, tmp_path: Path) -> None:
        s = _store(tmp_path)
        s.add_decision(_decision(title="Older", valid_from=NOW - timedelta(days=2)))
        s.add_decision(_decision(title="Newer"))
        s.add_fact(_fact())
        log = build_bundle(s)["log.md"]
        assert log.splitlines()[0] == "# Log"
        assert log.index("## 2026-07-23") < log.index("## 2026-07-21")
        assert "- decision (adr): [Newer]" in log
        assert "- fact: [sqlite handles our volume fine.]" in log

    def test_build_bundle_is_deterministic(self, tmp_path: Path) -> None:
        s = _store(tmp_path)
        e = s.upsert_entity(Entity(canonical_name="auth.login"))
        s.add_domain(_domain())
        d = s.add_decision(_decision())
        f = s.add_fact(_fact(supports=[d.id]))
        s.add_binding(AnchorBinding(record_id=d.id, entity_id=e.entity_id, tier=2))
        s.add_binding(AnchorBinding(record_id=f.id, entity_id=e.entity_id, tier=2))
        assert build_bundle(s) == build_bundle(s)

    def test_every_file_ends_with_single_newline(self, tmp_path: Path) -> None:
        s = _store(tmp_path)
        e = s.upsert_entity(Entity(canonical_name="auth.login"))
        s.add_domain(_domain())
        d = s.add_decision(_decision())
        s.add_fact(_fact(supports=[d.id]))
        s.add_binding(AnchorBinding(record_id=d.id, entity_id=e.entity_id, tier=2))
        for path, content in build_bundle(s).items():
            assert content.endswith("\n") and not content.endswith("\n\n"), path


class TestProposedExcluded:
    """Proposed (unratified) drafts are never published — the store owner ratifies before
    a record enters the shared memory. Only ``proposed`` is filtered: superseded/rejected/
    deprecated history stays ("tried before, abandoned…" is the product). See spec §1."""

    def test_proposed_decision_absent_from_bundle(self, tmp_path: Path) -> None:
        s = _store(tmp_path)
        kept = s.add_decision(_decision(title="Kept way"))
        draft = s.add_decision(_decision(title="Draft way", status=DecisionStatus.PROPOSED))
        bundle = build_bundle(s)
        blob = "\n".join(bundle.values())
        assert draft.id.lower() not in blob
        assert "Draft way" not in blob
        assert f"decisions/kept-way-{kept.id.lower()}.md" in bundle
        assert "- [decisions/](/decisions/index.md) — 1 decision records" in bundle["index.md"]

    def test_proposed_fact_absent_from_bundle(self, tmp_path: Path) -> None:
        s = _store(tmp_path)
        s.add_decision(_decision())
        draft = s.add_fact(_fact(statement="draft-only fact", status=DecisionStatus.PROPOSED))
        bundle = build_bundle(s)
        blob = "\n".join(bundle.values())
        assert draft.id.lower() not in blob
        assert "draft-only fact" not in blob
        assert not any(p.startswith("facts/") for p in bundle)

    def test_proposed_domain_absent_from_bundle(self, tmp_path: Path) -> None:
        s = _store(tmp_path)
        s.add_domain(_domain(slug="kept", title="Kept domain"))
        s.add_domain(_domain(slug="draft", title="Draft domain", status=DomainStatus.PROPOSED))
        bundle = build_bundle(s)
        assert "domains/draft.md" not in bundle
        assert "Draft domain" not in "\n".join(bundle.values())
        assert "domains/kept.md" in bundle
        assert "- [domains/](/domains/index.md) — 1 domains" in bundle["index.md"]

    def test_superseded_decision_still_exported(self, tmp_path: Path) -> None:
        # Boundary guard: filtering is proposed-only, superseded history must survive.
        s = _store(tmp_path)
        old = s.add_decision(_decision(title="Old way"))
        s.add_decision(_decision(title="New way", supersedes=old.id))
        bundle = build_bundle(s)
        old_path = f"decisions/old-way-{old.id.lower()}.md"
        assert old_path in bundle
        assert "status: superseded" in bundle[old_path].splitlines()

    def test_proposed_successor_does_not_leak_onto_predecessor(self, tmp_path: Path) -> None:
        # A proposed draft superseding an accepted record is fully hidden: the predecessor
        # page carries no "Superseded by" link pointing at the unpublished draft.
        s = _store(tmp_path)
        kept = s.add_decision(_decision(title="Kept way"))
        draft = s.add_decision(
            _decision(title="Draft way", status=DecisionStatus.PROPOSED, supersedes=kept.id)
        )
        bundle = build_bundle(s)
        kept_path = f"decisions/kept-way-{kept.id.lower()}.md"
        assert draft.id.lower() not in "\n".join(bundle.values())
        assert "Superseded by" not in bundle[kept_path]

    def test_entity_referenced_only_by_proposed_not_exported(self, tmp_path: Path) -> None:
        s = _store(tmp_path)
        e = s.upsert_entity(
            Entity(
                canonical_name="auth.login",
                descriptor=Descriptor(name="login", file_path="src/auth.py"),
            )
        )
        draft = s.add_decision(_decision(status=DecisionStatus.PROPOSED))
        s.add_binding(AnchorBinding(record_id=draft.id, entity_id=e.entity_id, tier=2))
        assert not any(p.startswith("entities/") for p in build_bundle(s))
