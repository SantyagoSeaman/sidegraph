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


def test_catalog_applies_identity_rule(tmp_path, capsys):
    """A file without its id, or whose id is not its filename, is not a record: minting a
    fresh ULID for it made the fingerprint differ between two loads (design/superpowers/
    specs/2026-09-29-record-identity-design.md D12)."""
    store_dir = tmp_path / ".sidegraph"
    write_model(store_dir / "decisions" / "good.json", decision("good", "Good"))

    def without(model: Decision | Fact | Domain, field: str) -> dict:
        data = json.loads(model.model_dump_json())
        del data[field]
        return data

    fact = Fact(
        id="fact-1",
        statement="s",
        source="src",
        valid_from=NOW,
        provenance=Provenance(source="manual"),
    )
    domain = Domain(
        domain_id="dom-1", slug="d", title="D", summary="sum", provenance=Provenance(source="m")
    )
    for subdir, name, data in (
        ("decisions", "noid", without(decision("x", "No id"), "id")),
        ("facts", "noid", without(fact, "id")),
        ("domains", "noid", without(domain, "domain_id")),
        ("decisions", "other", json.loads(decision("different", "Mismatch").model_dump_json())),
    ):
        (store_dir / subdir).mkdir(parents=True, exist_ok=True)
        (store_dir / subdir / f"{name}.json").write_text(json.dumps(data), encoding="utf-8")

    first = load_canonical_catalog(store_dir)
    second = load_canonical_catalog(store_dir)
    assert fingerprint_catalog(first) == fingerprint_catalog(second)
    assert [d.id for d in first.decisions] == ["good"]
    assert first.facts == ()
    assert first.domains == ()
    assert capsys.readouterr().err.count("WARNING") >= 4


def test_catalog_applies_identity_rule_to_archive_lines(tmp_path, capsys):
    """An archive line without an id, or with an unsafe one, is skipped as the store skips
    it: keying it by a freshly minted ULID made the fingerprint differ between loads (D12)."""
    from sidegraph.store import Store

    store_dir = tmp_path / ".sidegraph"
    good = decision("good", "Good")
    idless = json.loads(decision("x", "No id").model_dump_json())
    del idless["id"]
    lines = [
        {"record_type": "decision", **good.model_dump(mode="json")},
        {"record_type": "decision", **idless},
        {"record_type": "decision", **{**good.model_dump(mode="json"), "id": "../x"}},
    ]
    seg = store_dir / "archive" / "2026-08-01.jsonl"
    seg.parent.mkdir(parents=True)
    seg.write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")

    first = load_canonical_catalog(store_dir)
    second = load_canonical_catalog(store_dir)
    assert fingerprint_catalog(first) == fingerprint_catalog(second)
    assert [d.id for d in first.decisions] == ["good"]
    assert "archive" in capsys.readouterr().err
    store = Store(store_dir)
    try:
        count = store._conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
    finally:
        store.close()
    assert count == len(first.decisions)


def test_catalog_skips_a_non_object_archive_line(tmp_path, capsys):
    """A line like ``[]`` is skipped with a warning, as the store skips it, rather than
    failing a catalog load of a store that opens fine (design D8)."""
    store_dir = tmp_path / ".sidegraph"
    good = decision("good", "Good")
    seg = store_dir / "archive" / "2026-08-01.jsonl"
    seg.parent.mkdir(parents=True)
    seg.write_text(
        json.dumps({"record_type": "decision", **good.model_dump(mode="json")}) + "\n[]\n",
        encoding="utf-8",
    )

    first = load_canonical_catalog(store_dir)
    second = load_canonical_catalog(store_dir)

    assert [d.id for d in first.decisions] == ["good"]
    assert fingerprint_catalog(first) == fingerprint_catalog(second)
    assert "not a JSON object" in capsys.readouterr().err
