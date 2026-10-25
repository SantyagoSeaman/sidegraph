"""Pure store rules: key survival, archive relations, re-exports.

# see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§5.3)
"""

from __future__ import annotations

from pathlib import Path

from sidegraph.store_rules import (
    ABSENT,
    classify_transition,
    compatible,
    deletable,
    is_derived_entity_body,
    keeps_keys,
    kind_of,
    superseding,
)

SRC = str(Path(__file__).resolve().parent.parent / "src")


def test_keeps_keys_objects_any_depth():
    assert keeps_keys({"d": {"language": None}}, {"d": {"language": "py"}})
    assert not keeps_keys({"d": {"language": None}}, {"d": {}})
    assert not keeps_keys({"valid_to": None}, {})


def test_keeps_keys_lists_by_position():
    assert not keeps_keys({"supports": ["a", "b"]}, {"supports": ["a"]})
    assert keeps_keys({"supports": ["a", "b"]}, {"supports": ["c", "b"]})
    assert not keeps_keys({"seed_anchors": [{"n": None}]}, {"seed_anchors": [{}]})


def test_keeps_keys_bindings_by_entity_id_last_entry():
    x = [{"entity_id": "E1", "tier": 1, "w": 1}, {"entity_id": "E2", "tier": 2}]
    y = [
        {"entity_id": "E2", "tier": 2},
        {"entity_id": "E1", "tier": 1},
        {"entity_id": "E1", "tier": 1, "w": 2},
    ]
    assert keeps_keys(x, y, bindings=True)
    assert not keeps_keys(x, [{"entity_id": "E2", "tier": 2}], bindings=True)
    assert keeps_keys([{"entity_id": "C"}], ABSENT, bindings=True, is_derived=lambda e: e == "C")
    assert not keeps_keys({"id": "x"}, ABSENT)


def test_keeps_keys_bindings_use_the_last_entry_only():
    x = [{"entity_id": "E1", "w": 1}]
    assert not keeps_keys(x, [{"entity_id": "E1", "w": 1}, {"entity_id": "E1"}], bindings=True)


def test_is_derived_entity_body():
    assert is_derived_entity_body({"kind": "abstract", "canonical_name": "community:3"})
    assert not is_derived_entity_body({"kind": "code", "canonical_name": "community:3"})
    assert not is_derived_entity_body(None)


def test_kind_of():
    assert kind_of("bindings") == "binding"
    assert kind_of("decision") == "decision"


def test_relations():
    a = {"id": "D", "status": "superseded", "valid_to": "2026-10-01T00:00:00Z", "note": None}
    s = {"id": "D", "status": "accepted", "valid_to": None}
    assert superseding("decision", a, s) and not compatible("decision", a, s)
    assert compatible("decision", a, {**a, "extra": 1})
    assert compatible("decision", a, {k: v for k, v in a.items() if k != "note"})
    assert not deletable("decision", a, {k: v for k, v in a.items() if k != "note"})
    assert deletable("decision", a, {**a, "extra": 1})


def test_verify_reexports_are_the_same_objects():
    import sidegraph.store_rules as r
    import sidegraph.verify as v

    assert v.classify_transition is r.classify_transition and v.Violation is r.Violation
    assert classify_transition is r.classify_transition


def test_keeps_keys_bindings_absent():
    assert not keeps_keys([{"entity_id": "E"}], ABSENT, bindings=True)
    assert keeps_keys([], ABSENT, bindings=True)
    mixed = [{"entity_id": "C"}, {"entity_id": "E"}]
    assert not keeps_keys(mixed, ABSENT, bindings=True, is_derived=lambda e: e == "C")


def test_keeps_keys_same_shape_rule():
    assert not keeps_keys({"a": {}}, {"a": None})
    assert not keeps_keys({"a": []}, {"a": None})
    assert keeps_keys({"a": 1}, {"a": {"x": 1}})


def test_keeps_keys_bindings_non_str_entity_ids():
    assert not keeps_keys([{"entity_id": ["E"]}], [{"entity_id": "E"}], bindings=True)
    assert not keeps_keys([{"entity_id": "E"}], [{"entity_id": ["E"]}], bindings=True)
    assert keeps_keys(
        [{"entity_id": "E"}], [{"entity_id": ["E"]}, {"entity_id": "E"}], bindings=True
    )
    assert not keeps_keys([{"tier": 1}], [{"entity_id": None, "tier": 1}], bindings=True)
    assert not keeps_keys([{"tier": 1}], [{"tier": 1}], bindings=True)


def test_compatible_requires_non_empty_keys_of_a():
    # B lacks `status`, which A holds non-empty: classify_transition stays silent on a missing
    # new status, so only the "present in B" clause refuses it.
    a = {"id": "D", "status": "accepted"}
    assert not compatible("decision", a, {"id": "D"})
    assert compatible("decision", {"id": "D", "status": None}, {"id": "D"})


def test_compatible_projects_b_onto_a_before_classifying():
    a = {"id": "D", "status": "proposed", "title": "t"}
    b = {"id": "D", "status": "accepted", "title": "t", "extra": 1}
    assert compatible("decision", a, b)


def test_compatible_dropped_domain_successor():
    a = {"domain_id": "M", "status": "dropped", "slug": "s"}
    b = {"domain_id": "M", "status": "superseded", "slug": "s"}
    assert compatible("domain", a, b)


def test_superseding_negative_cases():
    a = {"id": "D", "status": "superseded", "valid_to": "2026-10-01T00:00:00Z"}
    assert not superseding("decision", a, dict(a))
    older = {"id": "D", "status": "accepted", "valid_to": None}
    assert not superseding(
        "decision", older, a
    )  # an older archive does not supersede its successor


def test_deletable_refused_when_not_compatible():
    a = {"id": "D", "status": "accepted", "title": "t"}
    s = {"id": "D", "status": "accepted", "title": "other"}
    assert keeps_keys(a, s) and not compatible("decision", a, s)
    assert not deletable("decision", a, s)


def test_deletable_refused_when_a_null_key_would_be_lost():
    a = {"id": "D", "status": "accepted", "note": None}
    s = {"id": "D", "status": "accepted"}
    assert compatible("decision", a, s) and not keeps_keys(a, s)
    assert not deletable("decision", a, s)
