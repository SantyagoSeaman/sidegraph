"""``duplicate-entity`` — a curation finding for a legal-but-ambiguous duplicate logical
identity (design D5, entity-identity-uniqueness spec).

A duplicate is LEGAL (D2: two branches each minting the same name produce two ULIDs -> two
files -> a clean git merge, and a UNIQUE index would brick the store on exactly that path
rather than guard it — see ``test_store_opens_with_merged_duplicate.py``). It is still worth
surfacing: lookups now deterministically resolve to the lowest ``entity_id`` (D4), so every
OTHER id in the group is reliably unreachable by name, and a human needs to see the group (and
each id's binding count) to decide which one should absorb the others (D7 — no automatic
merge).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from sidegraph.doctor import DUPLICATE_ENTITY, curate
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

# Sorts below any real ULID minted "now" -- stands in for an earlier-timestamped branch's
# entity, merged in later (same convention as test_entity_duplicate_resolution.py).
_HAND_MADE_LOWER_ID = "00000000000000000000000000"
_HAND_MADE_THIRD_ID = "00000000000000000000000AAA"


def _decision(store: Store, title: str = "an adr") -> Decision:
    return store.add_decision(
        Decision(
            title=title,
            kind=DecisionKind.ADR,
            status=DecisionStatus.ACCEPTED,
            context="c",
            choice="ch",
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual"),
        )
    )


def test_reports_a_concrete_duplicate_group_with_binding_counts(tmp_path: Path):
    db = tmp_path / "store"
    s = Store(db)
    descriptor = Descriptor(name="widget.Frobnicator", file_path="src/widget.py")

    higher = s.get_or_create_entity(descriptor)  # a real, "now" ULID
    lower = Entity(
        entity_id=_HAND_MADE_LOWER_ID,
        canonical_name=descriptor.name,
        kind=EntityKind.CONCRETE,
        descriptor=descriptor,
    )
    s.upsert_entity(lower)

    # Give the two ids different binding counts so the detail's counts are meaningfully
    # distinct, not just a coincidence of both being zero.
    s.add_binding(
        AnchorBinding(record_id=_decision(s, "d1").id, entity_id=higher.entity_id, tier=2)
    )
    d2 = _decision(s, "d2")
    s.add_binding(AnchorBinding(record_id=d2.id, entity_id=lower.entity_id, tier=2))
    s.add_binding(AnchorBinding(record_id=_decision(s, "d3").id, entity_id=lower.entity_id, tier=2))
    s.close()

    report = curate(db)
    findings = [f for f in report.findings if f.code == DUPLICATE_ENTITY]
    assert len(findings) == 1
    finding = findings[0]
    # Anchored at the WINNER's file (lowest id -- the one lookups actually resolve to).
    # The FULL path, store root included -- every other file-anchored finding reports
    # str(path), and an endswith() assertion alone would not have caught this one
    # hand-building "entities/<id>.json" instead (branch review, Low-2).
    assert finding.path == str(db / "entities" / f"{_HAND_MADE_LOWER_ID}.json")
    assert _HAND_MADE_LOWER_ID in finding.detail
    assert higher.entity_id in finding.detail
    assert "2 bindings" in finding.detail  # the lower (winner) id's count
    assert "1 bindings" in finding.detail  # the higher (loser) id's count


def test_reports_an_abstract_duplicate_group(tmp_path: Path):
    db = tmp_path / "store"
    s = Store(db)
    higher = s.get_or_create_abstract_entity("tag:duplicate-test")
    lower = Entity(
        entity_id=_HAND_MADE_LOWER_ID, canonical_name="tag:duplicate-test", kind=EntityKind.ABSTRACT
    )
    s.upsert_entity(lower)
    s.close()

    report = curate(db)
    findings = [f for f in report.findings if f.code == DUPLICATE_ENTITY]
    assert len(findings) == 1
    assert findings[0].path == str(db / "entities" / f"{_HAND_MADE_LOWER_ID}.json")
    assert higher.entity_id in findings[0].detail
    assert _HAND_MADE_LOWER_ID in findings[0].detail


def test_three_way_duplicate_is_one_finding_naming_every_id(tmp_path: Path):
    db = tmp_path / "store"
    s = Store(db)
    a = s.get_or_create_abstract_entity("tag:triple")
    b = Entity(entity_id=_HAND_MADE_LOWER_ID, canonical_name="tag:triple", kind=EntityKind.ABSTRACT)
    s.upsert_entity(b)
    c = Entity(entity_id=_HAND_MADE_THIRD_ID, canonical_name="tag:triple", kind=EntityKind.ABSTRACT)
    s.upsert_entity(c)
    s.close()

    report = curate(db)
    findings = [f for f in report.findings if f.code == DUPLICATE_ENTITY]
    assert len(findings) == 1
    assert "3 entities" in findings[0].detail
    for eid in (a.entity_id, b.entity_id, c.entity_id):
        assert eid in findings[0].detail


def test_stays_silent_without_a_duplicate(tmp_path: Path):
    db = tmp_path / "store"
    s = Store(db)
    s.get_or_create_abstract_entity("tag:unique")
    s.get_or_create_entity(Descriptor(name="Solo", file_path="solo.py"))
    s.close()

    report = curate(db)
    assert not [f for f in report.findings if f.code == DUPLICATE_ENTITY]


def test_kind_partitioned_an_abstract_and_concrete_sharing_a_name_is_not_a_duplicate(
    tmp_path: Path,
):
    """find_entity itself is kind-blind (a stray abstract row can satisfy a
    file_path=None concrete lookup at runtime), but doctor's grouping is deliberately
    kind-partitioned (design's own accepted edge case) -- this pair must NOT be reported."""
    db = tmp_path / "store"
    s = Store(db)
    s.upsert_entity(Entity(canonical_name="shared-name", kind=EntityKind.ABSTRACT))
    s.upsert_entity(
        Entity(
            canonical_name="shared-name",
            kind=EntityKind.CONCRETE,
            descriptor=Descriptor(name="shared-name", file_path=None),
        )
    )
    s.close()

    report = curate(db)
    assert not [f for f in report.findings if f.code == DUPLICATE_ENTITY]


def test_a_malformed_entity_file_does_not_abort_the_whole_doctor_pass(tmp_path):
    """One corrupt file must never abort the pass -- the module's stated policy, which
    `_check_unreferenced_entities` enforces one function above with an isinstance guard
    (doctor.py:260). `duplicate-entity`'s identity key called `canonicalize(name)` with no
    such guard, so an entity whose `canonical_name` is null raised AttributeError out of
    `curate()` and took every other check down with it (branch review on PR, Low-1)."""
    db = tmp_path / "store"
    s = Store(db)
    s.close()

    (db / "entities" / "01MALFORMED000000000000000.json").write_text(
        json.dumps(
            {
                "entity_id": "01MALFORMED000000000000000",
                "canonical_name": None,
                "kind": "concrete",
                "descriptor": None,
            }
        ),
        encoding="utf-8",
    )

    report = curate(db)  # must not raise
    assert not [f for f in report.findings if f.code == DUPLICATE_ENTITY]
