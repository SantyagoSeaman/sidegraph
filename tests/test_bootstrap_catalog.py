from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sidegraph.bootstrap.catalog import (
    CanonicalCatalog,
    fingerprint_catalog,
    load_canonical_catalog,
)
from sidegraph.schema import (
    Decision,
    DecisionKind,
    DecisionStatus,
    Domain,
    Fact,
    Provenance,
)

NOW = datetime(2026, 8, 1, tzinfo=UTC)


def decision(
    record_id: str,
    title: str,
    *,
    status: DecisionStatus = DecisionStatus.ACCEPTED,
    ref: str = "docs/adr/001.md",
) -> Decision:
    return Decision(
        id=record_id,
        title=title,
        kind=DecisionKind.ADR,
        status=status,
        context="A bounded catalog test.",
        choice="Use the pure canonical reader.",
        valid_from=NOW,
        provenance=Provenance(source="doc-import", ref=ref),
    )


def write_model(path: Path, model: Decision | Fact | Domain) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(model.model_dump_json(), encoding="utf-8")


def write_archive(path: Path, *records: Decision | Domain) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for record in records:
        record_type = "decision" if isinstance(record, Decision) else "domain"
        lines.append(
            json.dumps(
                {"record_type": record_type, **record.model_dump(mode="json")},
                sort_keys=True,
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_catalog_reads_hot_and_archive_without_creating_index(tmp_path, monkeypatch):
    store_dir = tmp_path / ".sidegraph"
    write_model(store_dir / "decisions" / "hot.json", decision("hot", "Hot"))
    write_archive(store_dir / "archive" / "2026-08-01.jsonl", decision("old", "Archived"))

    def forbidden(*args, **kwargs):
        raise AssertionError("Store must not be opened to load the canonical catalog")

    monkeypatch.setattr("sidegraph.store.Store", forbidden)
    catalog = load_canonical_catalog(store_dir)

    assert {item.title for item in catalog.decisions} == {"Hot", "Archived"}
    assert not (store_dir / "index.db").exists()


def test_missing_store_is_an_empty_catalog(tmp_path):
    catalog = load_canonical_catalog(tmp_path / "missing")

    assert catalog == CanonicalCatalog()
    assert not (tmp_path / "missing").exists()


def test_catalog_includes_proposed_facts_and_domains_for_review_debt(tmp_path):
    store_dir = tmp_path / ".sidegraph"
    proposed = decision("proposal", "Proposal", status=DecisionStatus.PROPOSED)
    fact = Fact(
        id="fact",
        statement="The pure loader is read-only.",
        source="catalog fixture",
        status=DecisionStatus.PROPOSED,
        valid_from=NOW,
        provenance=Provenance(source="manual"),
    )
    domain = Domain(
        domain_id="domain",
        slug="bootstrap",
        title="Bootstrap",
        summary="Owns the repository bootstrap workflow.",
        provenance=Provenance(source="manual"),
    )
    write_model(store_dir / "decisions" / "proposal.json", proposed)
    write_model(store_dir / "facts" / "fact.json", fact)
    write_model(store_dir / "domains" / "domain.json", domain)

    catalog = load_canonical_catalog(store_dir)

    assert catalog.decisions == (proposed,)
    assert catalog.facts == (fact,)
    assert catalog.domains == (domain,)


def test_hot_payload_wins_over_same_archive_id(tmp_path):
    store_dir = tmp_path / ".sidegraph"
    write_archive(
        store_dir / "archive" / "2026-08-01.jsonl",
        decision("same", "Archived payload"),
    )
    write_model(
        store_dir / "decisions" / "same.json",
        decision("same", "Hot payload"),
    )

    catalog = load_canonical_catalog(store_dir)

    assert catalog.decisions[0].title == "Hot payload"


@pytest.mark.parametrize(
    ("relative_path", "content"),
    [("decisions/bad.json", "{"), ("archive/bad.jsonl", "{\n")],
)
def test_malformed_canonical_data_names_the_exact_path(tmp_path, relative_path, content):
    store_dir = tmp_path / ".sidegraph"
    path = store_dir / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")

    with pytest.raises(ValueError) as exc_info:
        load_canonical_catalog(store_dir)

    assert str(path) in str(exc_info.value)


def test_catalog_fingerprint_is_stable_for_the_same_sorted_snapshot():
    first = decision("a", "A")
    second = decision("b", "B")

    assert fingerprint_catalog(CanonicalCatalog(decisions=(first, second))) == (
        fingerprint_catalog(CanonicalCatalog(decisions=(first, second)))
    )
    assert fingerprint_catalog(CanonicalCatalog(decisions=(first,))) != (
        fingerprint_catalog(CanonicalCatalog(decisions=(first, second)))
    )
