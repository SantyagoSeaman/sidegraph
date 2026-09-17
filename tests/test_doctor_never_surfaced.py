"""The one actionable telemetry finding (spec D5).

Two silences matter as much as the finding: an untouched area is not a defect, and a
store nobody has read yet has no evidence either way."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from sidegraph.doctor import NEVER_SURFACED, NEVER_SURFACED_CHECK, curate
from sidegraph.schema import (
    AnchorBinding,
    Decision,
    DecisionKind,
    DecisionStatus,
    Descriptor,
    Entity,
    EntityKind,
    Provenance,
)
from sidegraph.store import Store


def _seed_healthy(tmp_path: Path) -> tuple[Path, str]:
    """Same shape `tests/test_cli_doctor.py::_seed_healthy` builds: a store with one
    accepted decision anchored (tier 2) to a.py — no other findings. Returns the store dir
    and that decision's id, so callers can assert against the real record."""
    db = tmp_path / "store"
    s = Store(db)
    e = s.upsert_entity(
        Entity(canonical_name="alpha", descriptor=Descriptor(name="alpha", file_path="a.py"))
    )
    d = s.add_decision(
        Decision(
            title="an adr",
            kind=DecisionKind.ADR,
            status=DecisionStatus.ACCEPTED,
            context="c",
            choice="ch",
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual"),
        )
    )
    s.add_binding(AnchorBinding(record_id=d.id, entity_id=e.entity_id, tier=2))
    s.close()
    return db, d.id


def test_reports_a_decision_whose_area_is_queried_but_never_surfaces(tmp_path):
    """0 shows against N queries: people work there and this never comes up. The cause is
    either its anchors or the ranking — the finding names both and asserts neither."""
    db, decision_id = _seed_healthy(tmp_path)
    s = Store(db)
    for _ in range(12):
        s.record_retrieval([], ["a.py"])
    s.close()

    report = curate(db)
    findings = [f for f in report.findings if f.code == NEVER_SURFACED]
    assert len(findings) == 1
    assert "a.py" in findings[0].detail
    assert "12" in findings[0].detail
    assert findings[0].path.endswith(f"{decision_id}.json")


def test_stays_silent_when_the_area_was_never_queried(tmp_path):
    """0 shows / 0 queries is an absence of occasion, not dead memory. Reporting it would
    flag every decision in a store nobody happened to work near."""
    db, _decision_id = _seed_healthy(tmp_path)
    report = curate(db)
    assert not [f for f in report.findings if f.code == NEVER_SURFACED]


def test_stays_silent_for_a_decision_that_has_surfaced(tmp_path):
    db, decision_id = _seed_healthy(tmp_path)
    s = Store(db)
    s.record_retrieval([decision_id], ["a.py"])
    s.close()

    report = curate(db)
    assert not [f for f in report.findings if f.code == NEVER_SURFACED]


def test_a_decision_with_no_anchors_is_never_reported(tmp_path):
    """It has no area, so no query count could ever exonerate or convict it."""
    db = tmp_path / "store"
    s = Store(db)
    s.add_decision(
        Decision(
            title="unanchored",
            kind=DecisionKind.ADR,
            status=DecisionStatus.ACCEPTED,
            context="c",
            choice="ch",
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual"),
        )
    )
    s.record_retrieval([], ["a.py"])
    s.close()

    report = curate(db)
    assert not [f for f in report.findings if f.code == NEVER_SURFACED]


def test_a_decision_anchored_only_to_an_abstract_entity_is_never_reported(tmp_path):
    """The check's denominator is file paths, so a decision anchored only to Tier-0/Tier-1
    abstract entities (`domain:*`, `initiative:*`, `tag:*`) has no path to have been queried
    and is out of its reach — even though `drill_down` records `domain:<slug>` seeds.

    This is a real limit, not an oversight: 19 of this repo's own 83 accepted decisions are
    abstract-anchored. Pinned here so the boundary is a tested contract rather than
    incidental behaviour (PR #20 review)."""
    db = tmp_path / "store"
    s = Store(db)
    e = s.upsert_entity(Entity(canonical_name="domain:payments", kind=EntityKind.ABSTRACT))
    d = s.add_decision(
        Decision(
            title="an adr",
            kind=DecisionKind.ADR,
            status=DecisionStatus.ACCEPTED,
            context="c",
            choice="ch",
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual"),
        )
    )
    s.add_binding(AnchorBinding(record_id=d.id, entity_id=e.entity_id, tier=1))
    for _ in range(12):
        s.record_retrieval([], ["domain:payments"])
    s.close()

    report = curate(db)
    assert not [f for f in report.findings if f.code == NEVER_SURFACED]


def test_is_skipped_not_failed_without_an_index(tmp_path):
    """Same contract as binding-status: an absent or unusable index.db is a SKIPPED check,
    never an operational error."""
    db, _decision_id = _seed_healthy(tmp_path)
    (db / "index.db").unlink()

    report = curate(db)
    assert NEVER_SURFACED_CHECK in report.skipped
    assert not [f for f in report.findings if f.code == NEVER_SURFACED]
