"""``sidegraph-verify --against <git-ref>`` transition layer (design/superpowers/specs/
2026-07-11-ci-integrity-design.md ruling 2, "Transition layer"): append-only as a checkable
contract. Two test groups:

- **Pure-core** (no git at all): every legal transition in the mutable-field tables passes
  ``classify_transition`` cleanly; every illegal case yields its exact code. The mutable-
  field tables themselves are PINNED here too — they were derived from ``store.py``'s write
  methods (see ``verify.py``'s transition-layer module comment for exactly which lines), not
  invented, and a silent drift between the derived tables and the write paths they describe
  would defeat the whole point.
- **Git-backed integration** (a real ``tmp_path`` git repo): ``verify_against`` end to end —
  one legitimate supersede-close commit is clean, one hand-edited commit is caught.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sidegraph.schema import (
    AnchorBinding,
    Decision,
    DecisionKind,
    Descriptor,
    Entity,
    Initiative,
    Provenance,
)
from sidegraph.store import Store
from sidegraph.verify import (
    _DECISION_STATUS_TRANSITIONS,
    _DOMAIN_STATUS_TRANSITIONS,
    _FACT_STATUS_TRANSITIONS,
    DECISION_MUTABLE_FIELDS,
    DOMAIN_MUTABLE_FIELDS,
    ENTITY_MUTABLE_FIELDS,
    FACT_MUTABLE_FIELDS,
    ILLEGAL_DELETION,
    ILLEGAL_FIELD_CHANGE,
    ILLEGAL_STATUS_JUMP,
    VALID_TO_CHANGED,
    VALID_TO_UNSET,
    Violation,
    classify_transition,
    verify_against,
)


def _codes(violations: list[Violation]) -> list[str]:
    return [v.code for v in violations]


# -- mutable-field tables, pinned -----------------------------------------------------------
#
# Derived from store.py's write methods (see verify.py's transition-layer comment for the
# exact line ranges read). A change here must be justified by a corresponding change to a
# store.py write path, never the other way around.


def test_decision_mutable_fields_pinned():
    assert {"status", "valid_to"} == DECISION_MUTABLE_FIELDS


def test_fact_mutable_fields_pinned():
    assert {"status", "valid_to"} == FACT_MUTABLE_FIELDS


def test_domain_mutable_fields_pinned():
    # NOT {"status", "valid_to", "communities"} as design ruling 2's plan literally states --
    # Domain has no valid_to field at all (see schema.py). Code wins.
    assert {"status", "communities"} == DOMAIN_MUTABLE_FIELDS


def test_entity_mutable_fields_pinned():
    assert {
        "descriptor",
        "last_seen_node_id",
        "last_seen_graph_version",
        "last_seen_community",
    } == ENTITY_MUTABLE_FIELDS


def test_decision_status_transitions_pinned():
    assert {
        ("proposed", "accepted"),
        ("proposed", "rejected"),
        ("proposed", "superseded"),
        ("accepted", "superseded"),
        ("accepted", "deprecated"),  # forward-compat only -- no live write path sets this
    } == _DECISION_STATUS_TRANSITIONS


def test_fact_status_transitions_pinned():
    assert {
        ("proposed", "accepted"),
        ("proposed", "rejected"),
        ("proposed", "superseded"),
        ("accepted", "superseded"),
    } == _FACT_STATUS_TRANSITIONS


def test_domain_status_transitions_pinned():
    assert {
        ("proposed", "accepted"),
        ("proposed", "dropped"),
        ("accepted", "dropped"),
        ("proposed", "superseded"),
        ("accepted", "superseded"),
        # supersede_domain (store.py) flips old.status -> SUPERSEDED with no gate on old's
        # current status; find_domain_by_slug keeps a dropped domain resolvable by slug for
        # exactly this reason -- a dropped domain can still be superseded/revived later.
        ("dropped", "superseded"),
    } == _DOMAIN_STATUS_TRANSITIONS


# -- fixtures ---------------------------------------------------------------------------------

_PROVENANCE = {
    "source": "manual",
    "ref": None,
    "author": None,
    "session_id": None,
    "graph_version": None,
}


def _decision_payload(**overrides) -> dict:
    base = dict(
        id="01DECISIONID000000000000A",
        title="Use file-per-record JSON",
        kind="adr",
        status="proposed",
        context="A single committed SQLite file can't be merged by git.",
        choice="One JSON file per record, plus a derived, gitignored local index.",
        rejected=None,
        consequences=None,
        valid_from="2026-01-10T00:00:00+00:00",
        valid_to=None,
        supersedes=None,
        scope="repo",
        layer=None,
        provenance=dict(_PROVENANCE),
    )
    base.update(overrides)
    return base


def _fact_payload(**overrides) -> dict:
    base = dict(
        id="01FACTID0000000000000000A",
        statement="httpx retries idempotent requests by default.",
        source="httpx docs",
        supports=[],
        status="proposed",
        valid_from="2026-01-10T00:00:00+00:00",
        valid_to=None,
        supersedes=None,
        provenance=dict(_PROVENANCE),
    )
    base.update(overrides)
    return base


def _domain_payload(**overrides) -> dict:
    base = dict(
        domain_id="01DOMAINID0000000000000A",
        slug="payments",
        title="Payments",
        summary="Order settlement.",
        parent_id=None,
        path_prefixes=[],
        seed_anchors=[],
        status="proposed",
        supersedes=None,
        provenance=dict(_PROVENANCE),
    )
    base.update(overrides)
    return base


def _entity_payload(**overrides) -> dict:
    base = dict(
        entity_id="01ENTITYID0000000000000A",
        canonical_name="f_widget",
        kind="concrete",
        descriptor={"name": "f_widget", "file_path": "a.py"},
    )
    base.update(overrides)
    return base


# -- added / deleted, generic across kinds -----------------------------------------------------


@pytest.mark.parametrize("kind", ["decision", "fact", "domain", "entity", "binding"])
def test_added_is_always_legal(kind):
    payload = {
        "decision": _decision_payload,
        "fact": _fact_payload,
        "domain": _domain_payload,
        "entity": _entity_payload,
        "binding": lambda: [{"entity_id": "x", "tier": 2}],
    }[kind]()
    assert classify_transition(kind, None, payload) == []


def test_binding_deletion_is_always_legal():
    assert classify_transition("binding", [{"entity_id": "x", "tier": 2}], None) == []


def test_binding_modification_is_never_flagged():
    old = [{"entity_id": "x", "tier": 2, "weight": 1.0, "relation": "affects", "status": "live"}]
    new = [
        {"entity_id": "y", "tier": 0, "weight": 0.2, "relation": "creates", "status": "orphaned"}
    ]
    assert classify_transition("binding", old, new) == []


def test_decision_deletion_absent_from_archive_is_illegal_at_pure_layer():
    old = _decision_payload()
    violations = classify_transition("decision", old, None)
    assert _codes(violations) == [ILLEGAL_DELETION]


def test_domain_deletion_absent_from_archive_is_illegal_at_pure_layer():
    old = _domain_payload()
    violations = classify_transition("domain", old, None)
    assert _codes(violations) == [ILLEGAL_DELETION]


def test_entity_deletion_is_illegal():
    old = _entity_payload()
    assert _codes(classify_transition("entity", old, None)) == [ILLEGAL_DELETION]


def test_unknown_kind_raises():
    with pytest.raises(ValueError):
        classify_transition("initiative", {}, {})


# -- decisions: legal transitions ---------------------------------------------------------------


@pytest.mark.parametrize("old_status,new_status", sorted(_DECISION_STATUS_TRANSITIONS))
def test_decision_legal_status_transition(old_status, new_status):
    old = _decision_payload(status=old_status)
    new = _decision_payload(status=new_status)
    assert classify_transition("decision", old, new) == []


def test_decision_legal_valid_to_close():
    old = _decision_payload(status="accepted", valid_to=None)
    new = _decision_payload(status="superseded", valid_to="2026-02-01T00:00:00+00:00")
    assert classify_transition("decision", old, new) == []


def test_decision_no_change_is_legal():
    payload = _decision_payload()
    assert classify_transition("decision", payload, dict(payload)) == []


# -- decisions: illegal transitions ---------------------------------------------------------------


def test_decision_edited_choice_is_illegal_field_change():
    old = _decision_payload()
    new = _decision_payload(choice="A completely different choice.")
    violations = classify_transition("decision", old, new)
    assert _codes(violations) == [ILLEGAL_FIELD_CHANGE]
    assert "choice" in violations[0].detail


def test_decision_valid_from_change_is_illegal_field_change():
    old = _decision_payload()
    new = _decision_payload(valid_from="2026-03-01T00:00:00+00:00")
    violations = classify_transition("decision", old, new)
    assert _codes(violations) == [ILLEGAL_FIELD_CHANGE]
    assert "valid_from" in violations[0].detail


def test_decision_rejected_to_accepted_is_illegal_status_jump():
    old = _decision_payload(status="rejected", valid_to="2026-01-15T00:00:00+00:00")
    new = _decision_payload(status="accepted", valid_to="2026-01-15T00:00:00+00:00")
    violations = classify_transition("decision", old, new)
    assert _codes(violations) == [ILLEGAL_STATUS_JUMP]


def test_decision_valid_to_unset_is_illegal():
    old = _decision_payload(status="superseded", valid_to="2026-02-01T00:00:00+00:00")
    new = _decision_payload(status="superseded", valid_to=None)
    violations = classify_transition("decision", old, new)
    assert _codes(violations) == [VALID_TO_UNSET]


def test_decision_valid_to_changed_is_illegal():
    old = _decision_payload(status="superseded", valid_to="2026-02-01T00:00:00+00:00")
    new = _decision_payload(status="superseded", valid_to="2026-03-01T00:00:00+00:00")
    violations = classify_transition("decision", old, new)
    assert _codes(violations) == [VALID_TO_CHANGED]


def test_decision_id_change_is_illegal_field_change():
    old = _decision_payload()
    new = _decision_payload(id="01SOMEOTHERID000000000000")
    violations = classify_transition("decision", old, new)
    assert ILLEGAL_FIELD_CHANGE in _codes(violations)


def test_decision_multiple_illegal_fields_yield_one_violation_each():
    old = _decision_payload()
    new = _decision_payload(choice="different", context="different too")
    violations = classify_transition("decision", old, new)
    assert sorted(_codes(violations)) == sorted([ILLEGAL_FIELD_CHANGE, ILLEGAL_FIELD_CHANGE])
    fields = sorted(v.detail for v in violations)
    assert "'choice'" in fields[0] or "'choice'" in fields[1]
    assert "'context'" in fields[0] or "'context'" in fields[1]


# -- facts: legal + illegal ------------------------------------------------------------------------


@pytest.mark.parametrize("old_status,new_status", sorted(_FACT_STATUS_TRANSITIONS))
def test_fact_legal_status_transition(old_status, new_status):
    old = _fact_payload(status=old_status)
    new = _fact_payload(status=new_status)
    assert classify_transition("fact", old, new) == []


def test_fact_deprecated_is_not_a_legal_target():
    old = _fact_payload(status="accepted")
    new = _fact_payload(status="deprecated")
    violations = classify_transition("fact", old, new)
    assert _codes(violations) == [ILLEGAL_STATUS_JUMP]


def test_fact_edited_statement_is_illegal_field_change():
    old = _fact_payload()
    new = _fact_payload(statement="A different statement entirely.")
    violations = classify_transition("fact", old, new)
    assert _codes(violations) == [ILLEGAL_FIELD_CHANGE]


# -- domains: legal + illegal ------------------------------------------------------------------


@pytest.mark.parametrize("old_status,new_status", sorted(_DOMAIN_STATUS_TRANSITIONS))
def test_domain_legal_status_transition(old_status, new_status):
    old = _domain_payload(status=old_status)
    new = _domain_payload(status=new_status)
    assert classify_transition("domain", old, new) == []


def test_domain_slug_change_is_illegal_field_change():
    old = _domain_payload()
    new = _domain_payload(slug="checkout")
    violations = classify_transition("domain", old, new)
    assert _codes(violations) == [ILLEGAL_FIELD_CHANGE]
    assert "slug" in violations[0].detail


def test_domain_parent_id_change_is_illegal_field_change():
    old = _domain_payload()
    new = _domain_payload(parent_id="01SOMEPARENTID0000000000")
    violations = classify_transition("domain", old, new)
    assert _codes(violations) == [ILLEGAL_FIELD_CHANGE]
    assert "parent_id" in violations[0].detail


def test_domain_communities_change_is_legal():
    old = _domain_payload(communities=["19"])
    new = _domain_payload(communities=["256"])
    assert classify_transition("domain", old, new) == []


# -- entities: legal + illegal -----------------------------------------------------------------


def test_entity_descriptor_move_is_legal():
    old = _entity_payload(descriptor={"name": "f_widget", "file_path": "a.py"})
    new = _entity_payload(descriptor={"name": "f_widget", "file_path": "b.py"})
    assert classify_transition("entity", old, new) == []


def test_entity_last_seen_refresh_is_legal():
    old = _entity_payload()
    new = _entity_payload(
        last_seen_node_id="n1", last_seen_graph_version="v1", last_seen_community="5"
    )
    assert classify_transition("entity", old, new) == []


def test_entity_canonical_name_change_is_illegal_field_change():
    old = _entity_payload()
    new = _entity_payload(canonical_name="g_widget")
    violations = classify_transition("entity", old, new)
    assert _codes(violations) == [ILLEGAL_FIELD_CHANGE]
    assert "canonical_name" in violations[0].detail


def test_entity_kind_change_is_illegal_field_change():
    old = _entity_payload()
    new = _entity_payload(kind="abstract")
    violations = classify_transition("entity", old, new)
    assert _codes(violations) == [ILLEGAL_FIELD_CHANGE]


# -- git-backed integration ---------------------------------------------------------------------


def _git(args: list[str], cwd: Path) -> None:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"


@pytest.fixture
def git_repo(tmp_path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], cwd=repo)
    _git(["config", "user.email", "test@example.com"], cwd=repo)
    _git(["config", "user.name", "Test"], cwd=repo)
    return repo


def _decision() -> Decision:
    return Decision(
        title="Use file-per-record JSON",
        kind=DecisionKind.ADR,
        context="A single committed SQLite file can't be merged by git.",
        choice="One JSON file per record, plus a derived, gitignored local index.",
        valid_from=datetime(2026, 1, 10, tzinfo=UTC),
        provenance=Provenance(source="manual"),
    )


def test_verify_against_clean_on_legal_supersede_commit(git_repo):
    store_dir = git_repo / ".sidegraph"
    with Store(store_dir) as s:
        old = s.add_decision(_decision())
        s.ratify(old.id)
    _git(["add", "-A"], cwd=git_repo)
    _git(["commit", "-q", "-m", "initial"], cwd=git_repo)

    with Store(store_dir) as s:
        s.add_decision(
            Decision(
                title="v2",
                kind=DecisionKind.ADR,
                context="Context.",
                choice="A revised choice.",
                valid_from=datetime(2026, 2, 1, tzinfo=UTC),
                supersedes=old.id,
                provenance=Provenance(source="manual"),
            )
        )
    _git(["add", "-A"], cwd=git_repo)
    _git(["commit", "-q", "-m", "supersede"], cwd=git_repo)

    assert verify_against(store_dir, "HEAD~1") == []


def test_verify_against_catches_hand_edit(git_repo):
    store_dir = git_repo / ".sidegraph"
    with Store(store_dir) as s:
        d = s.add_decision(_decision())
        path = store_dir / "decisions" / f"{d.id}.json"
    _git(["add", "-A"], cwd=git_repo)
    _git(["commit", "-q", "-m", "initial"], cwd=git_repo)

    data = json.loads(path.read_text(encoding="utf-8"))
    data["choice"] = "A hand-edited choice, bypassing the store API."
    path.write_text(json.dumps(data), encoding="utf-8")
    _git(["add", "-A"], cwd=git_repo)
    _git(["commit", "-q", "-m", "hand edit"], cwd=git_repo)

    violations = verify_against(store_dir, "HEAD~1")
    assert _codes(violations) == [ILLEGAL_FIELD_CHANGE]
    assert violations[0].path == ".sidegraph/decisions/" + d.id + ".json"


def test_verify_against_operational_error_outside_git_repo(tmp_path):
    store_dir = tmp_path / ".sidegraph"
    with Store(store_dir):
        pass
    with pytest.raises(ValueError):
        verify_against(store_dir, "HEAD")


def test_verify_against_operational_error_on_unknown_ref(git_repo):
    store_dir = git_repo / ".sidegraph"
    with Store(store_dir):
        pass
    _git(["add", "-A"], cwd=git_repo)
    _git(["commit", "-q", "-m", "initial"], cwd=git_repo)

    with pytest.raises(ValueError):
        verify_against(store_dir, "not-a-real-ref")


def test_verify_against_legal_compact_deletion(git_repo):
    store_dir = git_repo / ".sidegraph"
    with Store(store_dir) as s:
        d = s.add_decision(_decision())
        s.ratify(d.id)
        s.add_decision(
            Decision(
                title="v2",
                kind=DecisionKind.ADR,
                context="Context.",
                choice="A revised choice.",
                valid_from=datetime(2026, 2, 1, tzinfo=UTC),
                supersedes=d.id,
                provenance=Provenance(source="manual"),
            )
        )
    _git(["add", "-A"], cwd=git_repo)
    _git(["commit", "-q", "-m", "initial"], cwd=git_repo)

    with Store(store_dir) as s:
        s.compact()
    _git(["add", "-A"], cwd=git_repo)
    _git(["commit", "-q", "-m", "compact"], cwd=git_repo)

    assert verify_against(store_dir, "HEAD~1") == []


def _compacted_store_and_segment(store_dir: Path) -> Path:
    """Build a store with one already-compacted (superseded, then archived) decision;
    return the resulting archive segment's path. Shared by the two write-once tests below."""
    with Store(store_dir) as s:
        d = s.add_decision(_decision())
        s.ratify(d.id)
        s.add_decision(
            Decision(
                title="v2",
                kind=DecisionKind.ADR,
                context="Context.",
                choice="A revised choice.",
                valid_from=datetime(2026, 2, 1, tzinfo=UTC),
                supersedes=d.id,
                provenance=Provenance(source="manual"),
            )
        )
        s.compact()
    return next((store_dir / "archive").glob("*.jsonl"))


def test_verify_against_catches_tampered_archive_segment(git_repo):
    """Fix-round review (Important-3): archive segments are write-once
    (store._write_archive_segment: "Segments are write-once ... and this always creates a
    brand-new file"; docs/reference/store-format.md: "immutable segments") -- editing an
    already-published segment's content must be caught, not silently accepted."""
    store_dir = git_repo / ".sidegraph"
    segment_path = _compacted_store_and_segment(store_dir)
    _git(["add", "-A"], cwd=git_repo)
    _git(["commit", "-q", "-m", "initial+compact"], cwd=git_repo)

    lines = segment_path.read_text(encoding="utf-8").splitlines()
    tampered = []
    for line in lines:
        obj = json.loads(line)
        obj["choice"] = "TAMPERED, bypassing write-once archive segments"
        tampered.append(json.dumps(obj))
    segment_path.write_text("\n".join(tampered) + "\n", encoding="utf-8")
    _git(["add", "-A"], cwd=git_repo)
    _git(["commit", "-q", "-m", "tamper archive segment"], cwd=git_repo)

    violations = verify_against(store_dir, "HEAD~1")
    assert _codes(violations) == [ILLEGAL_FIELD_CHANGE]
    assert violations[0].path.startswith(".sidegraph/archive/")


def test_verify_against_catches_deleted_archive_segment(git_repo):
    """Companion to the tamper case above: no pruning path exists for a published segment
    at all, so its deletion is always illegal too."""
    store_dir = git_repo / ".sidegraph"
    segment_path = _compacted_store_and_segment(store_dir)
    _git(["add", "-A"], cwd=git_repo)
    _git(["commit", "-q", "-m", "initial+compact"], cwd=git_repo)

    segment_path.unlink()
    _git(["add", "-A"], cwd=git_repo)
    _git(["commit", "-q", "-m", "delete archive segment"], cwd=git_repo)

    violations = verify_against(store_dir, "HEAD~1")
    assert _codes(violations) == [ILLEGAL_DELETION]
    assert violations[0].path.startswith(".sidegraph/archive/")


def test_verify_against_illegal_deletion_without_archive(git_repo):
    store_dir = git_repo / ".sidegraph"
    with Store(store_dir) as s:
        d = s.add_decision(_decision())
        path = store_dir / "decisions" / f"{d.id}.json"
    _git(["add", "-A"], cwd=git_repo)
    _git(["commit", "-q", "-m", "initial"], cwd=git_repo)

    path.unlink()
    _git(["add", "-A"], cwd=git_repo)
    _git(["commit", "-q", "-m", "hand delete"], cwd=git_repo)

    violations = verify_against(store_dir, "HEAD~1")
    assert _codes(violations) == [ILLEGAL_DELETION]


def test_verify_against_ignores_bindings_and_initiatives_deletion_is_illegal(git_repo):
    store_dir = git_repo / ".sidegraph"
    with Store(store_dir) as s:
        d = s.add_decision(_decision())
        entity = s.upsert_entity(
            Entity(
                canonical_name="f_widget", descriptor=Descriptor(name="f_widget", file_path="a.py")
            )
        )
        s.add_binding(AnchorBinding(record_id=d.id, entity_id=entity.entity_id, tier=2))
        s.upsert_initiative(Initiative(name="Q3 payments push"))
        initiative_id = next(s.iter_initiatives()).id
    _git(["add", "-A"], cwd=git_repo)
    _git(["commit", "-q", "-m", "initial"], cwd=git_repo)

    (store_dir / "bindings" / f"{d.id}.json").unlink()
    (store_dir / "initiatives" / f"{initiative_id}.json").unlink()
    _git(["add", "-A"], cwd=git_repo)
    _git(["commit", "-q", "-m", "delete binding + initiative"], cwd=git_repo)

    violations = verify_against(store_dir, "HEAD~1")
    assert _codes(violations) == [ILLEGAL_DELETION]
    assert "initiative" in violations[0].detail


def test_verify_against_from_repo_subdirectory_with_relative_store_path(git_repo, monkeypatch):
    """Regression, fix-round review (Important-2): the git subprocess always runs with
    cwd=repo_root, not the caller's process cwd -- passing a store_dir that is RELATIVE TO
    THE CALLER'S CWD (here: a repo subdirectory, not repo_root) used to get re-resolved
    against repo_root instead inside the git diff pathspec, silently scoping the diff to a
    path that doesn't exist and returning an EMPTY diff -- a false "clean" verdict over real
    tampering. Store lives under a subdirectory of the repo (not at repo_root) and is passed
    as a path relative to that subdirectory, with the process cwd actually set there."""
    sub = git_repo / "sub"
    sub.mkdir()
    store_dir = sub / ".sidegraph"
    with Store(store_dir) as s:
        d = s.add_decision(_decision())
        path = store_dir / "decisions" / f"{d.id}.json"
    _git(["add", "-A"], cwd=git_repo)
    _git(["commit", "-q", "-m", "initial"], cwd=git_repo)

    data = json.loads(path.read_text(encoding="utf-8"))
    data["choice"] = "A hand-edited choice, bypassing the store API."
    path.write_text(json.dumps(data), encoding="utf-8")
    _git(["add", "-A"], cwd=git_repo)
    _git(["commit", "-q", "-m", "hand edit"], cwd=git_repo)

    monkeypatch.chdir(sub)
    violations = verify_against(Path(".sidegraph"), "HEAD~1")
    assert _codes(violations) == [ILLEGAL_FIELD_CHANGE]
