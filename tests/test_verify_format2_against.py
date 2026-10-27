"""``sidegraph-verify --against REF`` on two inventories (packed store, format 2): the
transition layer's rules 1-8, one parametrised fixture per case.

Each case is a REF commit plus a new work tree (see ``store_format2_support.Case``). CLEAN cases
expect no finding; FINDING cases expect the named code among the findings (one diff may break
two rules, and the named code is the one the case exists for).

# see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§7.1, §8, §9 T-V2)
"""

# ruff: noqa: F811  (the imported ``git_repo`` fixture is redefined as a test argument)

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sidegraph.store_format import bucket as bucket_of
from sidegraph.store_format import bucket_name, encode_line, marker_text
from sidegraph.verify import verify_against
from tests.store_format2_support import (
    Case,
    Config,
    DelFile,
    DelLine,
    DelSegment,
    File,
    Line,
    Marker,
    Raw,
    Segment,
    bindings_body,
    build_case,
    codes,
    decision_body,
    domain_body,
    entity_body,
    fact_body,
    git_repo,  # noqa: F401  (fixture)
)

T = "2026-02-01T00:00:00Z"
T2 = "2026-03-01T00:00:00Z"
M1 = marker_text(1, "0.16.0", None)
M2 = marker_text(2, "0.17.0", 64)


def other(rid: str) -> str:
    """A valid shard file name that is not ``rid``'s own bucket (64 shards)."""
    return bucket_name((bucket_of(rid, 64) + 1) % 64)


def own(rid: str) -> str:
    return bucket_name(bucket_of(rid, 64))


def arch(body: dict, record_type: str = "decision") -> dict:
    return {"record_type": record_type, **body}


def accept(body: dict, by: str = "a") -> dict:
    return {**body, "status": "accepted", "ratified_by": by, "ratified_at": T}


def close(body: dict, status: str = "rejected") -> dict:
    return {**body, "status": status, "valid_to": T}


def drop_key(body: dict, key: str) -> dict:
    return {k: v for k, v in body.items() if k != key}


def bad(body: dict) -> dict:
    """Parseable, fails the model (and changes nothing ``classify_transition`` compares)."""
    return {**body, "valid_to": "not a date"}


# decisions
D0 = decision_body("D")  # proposed
D1 = accept(D0)  # accepted
DR = close(D0)  # rejected: terminal
DS = close(D1, "superseded")  # superseded: terminal
D0N = {**D0, "valid_to": None}  # a newer client's explicit null key
# facts, entities, domains, bindings
F0 = fact_body("F", status="proposed")
F1 = accept(F0)
E0 = entity_body("E", descriptor={"name": "f", "file_path": "a.py"})
E1 = entity_body("E", descriptor={"name": "f", "file_path": "b.py"})
M0 = domain_body("M")
M1D = accept(M0)
MD = {**M0, "status": "dropped"}
B0 = bindings_body("E1", "E2")
CASES: dict[str, Case] = {}

# -- CLEAN ------------------------------------------------------------------------------------

_ALL_FILES = [
    File("decision", D1),
    File("decision", close(decision_body("D2"))),
    File("fact", F1),
    File("fact", close(fact_body("F2"), "rejected")),
    File("entity", E0),
    File("domain", M1D),
    File("domain", {**domain_body("M2"), "status": "dropped"}),
    File("bindings", B0, "D"),
]
_ALL_LINES = [
    Line(
        f.t,
        f.record_id
        or f.body[
            "id"
            if f.t in ("decision", "fact")
            # type: ignore[index]
            else ("entity_id" if f.t == "entity" else "domain_id")
        ],
        f.body,
    )
    for f in _ALL_FILES
]
CASES["verbatim_file_to_shard_live_and_terminal_all_types"] = Case(
    ref=list(_ALL_FILES),
    new=[
        DelFile(
            f.t,
            f.record_id
            or f.body[
                "id"
                if f.t in ("decision", "fact")
                # type: ignore[index]
                else ("entity_id" if f.t == "entity" else "domain_id")
            ],
        )
        for f in _ALL_FILES
    ]
    + _ALL_LINES,
)
CASES["verbatim_archive_to_shard"] = Case(
    ref=[
        Segment(
            "a.jsonl",
            [
                arch(D0),
                arch(DR | {"id": "D2"}),
                arch(M0, "domain"),
                arch(MD | {"domain_id": "M2"}, "domain"),
            ],
        )
    ],
    new=[
        DelSegment("a.jsonl"),
        Line("decision", "D", D0),
        Line("decision", "D2", DR | {"id": "D2"}),
        Line("domain", "M", M0),
        Line("domain", "M2", MD | {"domain_id": "M2"}),
    ],
)
CASES["fold_after_change_landed_on_target"] = Case(
    ref=[Line("decision", "D", D0), File("decision", D1)],
    new=[DelFile("decision", "D"), DelLine("decision", "D"), Line("decision", "D", D1)],
)
CASES["older_client_edit_drops_null_key"] = Case(
    ref=[Line("decision", "D", D0N)],
    new=[File("decision", drop_key(D1, "valid_to"))],
)
CASES["unchanged_stale_shard_under_newer_file"] = Case(
    ref=[Line("decision", "D", D0), File("decision", D1)],
    new=[File("decision", DS)],
)
CASES["sanctioned_shadow_removal_lifecycle_reversal"] = Case(
    ref=[Line("decision", "D", D1), File("decision", D0)],  # file older: a shadow-divergence
    new=[DelFile("decision", "D")],
)
CASES["sanctioned_shadow_removal_ratifier_stamp"] = Case(
    ref=[Line("decision", "D", D1), File("decision", accept(D0, "b"))],
    new=[DelFile("decision", "D")],
)
_DUP = [File("decision", {**D0, "title": "other"}), Segment("a.jsonl", [arch(DR)])]
CASES["duplicate_ulid_file_removed_result_one_copy"] = Case(
    ref=list(_DUP), new=[DelFile("decision", "D")]
)
CASES["duplicate_ulid_segment_removed_result_one_copy"] = Case(
    ref=list(_DUP), new=[DelSegment("a.jsonl")]
)
CASES["stale_misplaced_moved_under_newer_file"] = Case(
    ref=[Line("decision", "D", D0, other("D")), File("decision", D1)],
    new=[DelLine("decision", "D", other("D")), Line("decision", "D", D0)],
)
CASES["committed_crash_state_equal_copy_beside_unchanged"] = Case(
    ref=[Line("decision", "D", D0), File("decision", D1)],
    new=[Line("decision", "D", D0, other("D"))],
)
CASES["misplaced_healed_under_file_omitting_null"] = Case(
    ref=[
        Line("decision", "D", D0N, other("D")),
        File("decision", drop_key(D1, "valid_to")),
    ],
    new=[DelLine("decision", "D", other("D")), Line("decision", "D", D0N)],
)
CASES["kept_hot_format1_compact"] = Case(  # today's compact keeps a file with a non-empty extra key
    ref=[File("decision", {**DR, "note": "kept"})],
    new=[Segment("c.jsonl", [arch(DR)])],
)
CASES["bindings_file_deleted_no_other_copy"] = Case(
    ref=[File("bindings", B0, "D")], new=[DelFile("bindings", "D")]
)
_MA = {**MD, "parent_id": None}  # the archive line: dropped, with a null key
_MS = {**M0, "status": "superseded", "parent_id": None}  # its shard successor keeps the key
CASES["segment_deleted_keys_survive_in_deletable_successor"] = Case(
    ref=[
        Line("domain", "M", _MS),
        Segment("a.jsonl", [arch(_MA, "domain")]),
        File("domain", {**drop_key(_MS, "parent_id"), "communities": ["c"]}),  # a newer file
    ],
    new=[DelSegment("a.jsonl")],
)
_DA = close(D1, "superseded")  # an archive line superseding the proposed shard line
CASES["deleted_decision_file_archive_effective_ci_ref"] = Case(
    ref=[Line("decision", "D", D0), File("decision", D1)],
    new=[DelFile("decision", "D"), Segment("a.jsonl", [arch(_DA)])],
)
CASES["sole_unparseable_shard_line_repaired_into_file"] = Case(
    ref=[Raw(f"shards/{own('D')}", b'{"type":"decision","id":"D","body":{broken\n')],
    new=[Raw(f"shards/{own('D')}", b""), File("decision", D0)],
)
CASES["file_plus_two_lines_resolved_delete_file"] = Case(
    ref=[
        Line("decision", "D", D0),
        Line("decision", "D", {**D0, "title": "two"}),
        File("decision", {**D0, "title": "three"}),
    ],
    new=[DelFile("decision", "D"), DelLine("decision", "D", k=1)],
)
CASES["bindings_duplicate_packed_resolved_to_union_file"] = Case(
    ref=[
        Line("bindings", "D", bindings_body("E1")),
        Line("bindings", "D", bindings_body("E2")),
    ],
    new=[File("bindings", bindings_body("E1", "E2"), "D"), DelLine("bindings", "D", k=1)],
)
_COM = entity_body("C", kind="abstract", canonical_name="community:7")
CASES["derived_only_bindings_line_removed"] = Case(
    ref=[Line("entity", "C", _COM), Line("bindings", "D", bindings_body("C"))],
    new=[DelLine("bindings", "D")],
)
_STAMPS = [Line("decision", "D", accept(D0, "a")), Line("decision", "D", accept(D0, "b"))]
CASES["ratifier_stamp_dispute_either_side"] = Case(
    ref=list(_STAMPS), new=[DelLine("decision", "D", k=1)]
)
CASES["one_of_two_duplicate_ulid_segments_removed"] = Case(
    ref=[
        Segment("a.jsonl", [arch(D1)]),
        Segment("b.jsonl", [arch({**D1, "note": 1})]),
    ],
    new=[DelSegment("b.jsonl")],
)
CASES["segment_on_core_autocrlf"] = Case(
    ref=[Config("core.autocrlf", "true"), Segment("a.jsonl", [arch(DR)])],
    new=[Raw("archive/a.jsonl", (json.dumps(arch(DR), sort_keys=True) + "\r\n").encode())],
)
CASES["full_rollback_no_shard_left"] = Case(
    ref=[Line("decision", "D", D0)],
    new=[Marker(M1), File("decision", D0), Raw(f"shards/{own('D')}", None)],
)
CASES["unrecoverable_line_removed_alone"] = Case(
    ref=[Raw("shards/00.shard", b"not a record at all\n")],
    new=[Raw("shards/00.shard", b"")],
)
_CONFLICT = (
    b"<<<<<<< HEAD\n"
    + json.dumps(D0, sort_keys=True).encode()
    + b"\n=======\n"
    + json.dumps(D1, sort_keys=True).encode()
    + b"\n>>>>>>> branch\n"
)
CASES["conflicted_file_repaired_as_rule3_edit"] = Case(
    ref=[Line("decision", "D", D0), Raw("decisions/D.json", _CONFLICT)],
    new=[File("decision", D1)],
)
_ZR = close(decision_body("Z"))
CASES["mixed_segment_human_resolves"] = Case(
    ref=[
        File("decision", {**D0, "title": "other"}),
        Segment("s.jsonl", [arch(DR), arch(_ZR)]),
        Line("decision", "Z", _ZR),
    ],
    new=[DelSegment("s.jsonl")],
)

# extra clean cases (beyond the ledger's names)
CASES["ratifier_stamp_dispute_other_side"] = Case(
    ref=list(_STAMPS), new=[DelLine("decision", "D", k=0)]
)
CASES["mixed_segment_human_deletes_file"] = Case(
    ref=[
        File("decision", {**D0, "title": "other"}),
        Segment("s.jsonl", [arch(DR), arch(_ZR)]),
        Line("decision", "Z", _ZR),
    ],
    new=[DelFile("decision", "D")],
)
CASES["migration_format1_to_format2_fold"] = Case(
    ref=[Marker(M1), File("decision", D1), File("bindings", B0, "D")],
    new=[
        Marker(M2),
        DelFile("decision", "D"),
        DelFile("bindings", "D"),
        Line("decision", "D", D1),
        Line("bindings", "D", B0),
    ],
)
CASES["older_client_edit_of_a_file_drops_null_key"] = Case(
    ref=[File("decision", D0N)], new=[File("decision", drop_key(D1, "valid_to"))]
)
CASES["kept_hot_file_deleted_archive_survives_unchanged"] = Case(
    ref=[File("decision", DR), Segment("c.jsonl", [arch(DR)])],
    new=[DelFile("decision", "D")],
)
CASES["new_identity_as_file_and_archive_is_not_judged"] = Case(
    ref=[], new=[File("decision", D0), Segment("n.jsonl", [arch(DR | {"id": "N"})])]
)

CLEAN = [
    "verbatim_file_to_shard_live_and_terminal_all_types",
    "verbatim_archive_to_shard",
    "fold_after_change_landed_on_target",
    "older_client_edit_drops_null_key",
    "unchanged_stale_shard_under_newer_file",
    "sanctioned_shadow_removal_lifecycle_reversal",
    "sanctioned_shadow_removal_ratifier_stamp",
    "duplicate_ulid_file_removed_result_one_copy",
    "duplicate_ulid_segment_removed_result_one_copy",
    "stale_misplaced_moved_under_newer_file",
    "committed_crash_state_equal_copy_beside_unchanged",
    "misplaced_healed_under_file_omitting_null",
    "kept_hot_format1_compact",
    "bindings_file_deleted_no_other_copy",
    "segment_deleted_keys_survive_in_deletable_successor",
    "deleted_decision_file_archive_effective_ci_ref",
    "sole_unparseable_shard_line_repaired_into_file",
    "file_plus_two_lines_resolved_delete_file",
    "bindings_duplicate_packed_resolved_to_union_file",
    "derived_only_bindings_line_removed",
    "ratifier_stamp_dispute_either_side",
    "one_of_two_duplicate_ulid_segments_removed",
    "segment_on_core_autocrlf",
    "full_rollback_no_shard_left",
    "unrecoverable_line_removed_alone",  # §28 Opus q1
    "conflicted_file_repaired_as_rule3_edit",  # §28 Opus 3
    "mixed_segment_human_resolves",  # T-M4 tail, from Task 10
    # beyond the ledger's names
    "ratifier_stamp_dispute_other_side",
    "mixed_segment_human_deletes_file",
    "migration_format1_to_format2_fold",
    "new_identity_as_file_and_archive_is_not_judged",
    "older_client_edit_of_a_file_drops_null_key",
    "kept_hot_file_deleted_archive_survives_unchanged",
]

# -- FINDING ----------------------------------------------------------------------------------

LOST = FIELD = "illegal-field-change"
DEL = "illegal-deletion"
JUMP = "illegal-status-jump"
PROV = "shard-body-without-source"

_K = {"valid_to": None}  # a null key a stale copy may lack
CASES["status_change_and_fold_same_diff"] = Case(
    ref=[Line("decision", "D", D0)],
    new=[DelLine("decision", "D"), Line("decision", "D", D1)],
)
CASES["destroyed_null_key_top"] = Case(
    ref=[Line("decision", "D", D0), File("decision", D0N)], new=[DelFile("decision", "D")]
)
_PROV_NULL = {"source": "manual", "session": None}
CASES["destroyed_null_key_nested"] = Case(
    ref=[
        Line("decision", "D", D0),
        File("decision", {**D0, "provenance": _PROV_NULL}),
    ],
    new=[DelFile("decision", "D")],
)
_ES = entity_body("E", descriptor={"name": "f", "file_path": "a.py"})
CASES["descriptor_language_on_fold"] = Case(
    ref=[
        Line("entity", "E", _ES),
        File(
            "entity",
            entity_body("E", descriptor={"name": "f", "file_path": "a.py", "language": "py"}),
        ),
    ],
    new=[DelFile("entity", "E")],
)
_BE = {"entity_id": "E1", "tier": 2}
CASES["bindings_entry_key_lost"] = Case(
    ref=[Line("bindings", "D", [_BE]), File("bindings", [{**_BE, "weight": 0.5}], "D")],
    new=[DelFile("bindings", "D")],
)
CASES["lost_with_verbatim_sibling"] = Case(
    ref=[
        Line("decision", "D", D0),
        Line("decision", "D", D0, other("D")),
        File("decision", D0N),
    ],
    new=[DelFile("decision", "D")],
)
_SK = {**D0, "valid_to": None}
_FOLDED = drop_key(D1, "valid_to")
CASES["lost_routine_fold"] = Case(
    ref=[Line("decision", "D", _SK), File("decision", _FOLDED)],
    new=[DelFile("decision", "D"), DelLine("decision", "D"), Line("decision", "D", _FOLDED)],
)
CASES["lost_shard_modified_archive_deleted"] = Case(
    ref=[
        Line("decision", "D", _SK),
        Segment("a.jsonl", [arch(_SK)]),
        File("decision", _FOLDED),
    ],
    new=[
        DelSegment("a.jsonl"),
        DelFile("decision", "D"),
        DelLine("decision", "D"),
        Line("decision", "D", _FOLDED),
    ],
)
CASES["lost_shadowed_line_deleted_file_stays"] = Case(
    ref=[Line("decision", "D", _SK), File("decision", _FOLDED)],
    new=[DelLine("decision", "D")],
)
_SN = {**D0, "supersedes": None}  # a proposed line holding a null key the closed body lacks
CASES["superseding_replaces_null_key_line_segment_deleted"] = Case(
    ref=[Line("decision", "D", _SN), Segment("a.jsonl", [arch(DR)])],
    new=[DelSegment("a.jsonl"), DelLine("decision", "D"), Line("decision", "D", DR)],
)
CASES["segment_line_key_missing_in_shard"] = Case(
    ref=[Line("decision", "D", D0), Segment("a.jsonl", [arch(D0N)])],
    new=[DelSegment("a.jsonl")],
)
CASES["illegal_status_jump"] = Case(ref=[File("decision", D1)], new=[File("decision", D0)])
CASES["non_divergent_file_removed"] = Case(
    ref=[Line("decision", "D", D0), File("decision", D1)], new=[DelFile("decision", "D")]
)
CASES["bindings_line_showing_dropped_entry_removed_or_rewritten"] = Case(
    ref=[Line("bindings", "D", B0), File("bindings", bindings_body("E1"), "D")],
    new=[DelLine("bindings", "D")],
)
_SA = {**M0, "seed_anchors": [{"name": "x"}]}
_SAN = {**M0, "seed_anchors": [{"name": "x", "file_path": None}]}
CASES["seed_anchors_null_lost_by_fold"] = Case(
    ref=[Line("domain", "M", _SA), File("domain", _SAN)], new=[DelFile("domain", "M")]
)
CASES["seed_anchors_null_lost_by_segment_delete"] = Case(
    ref=[
        Line("domain", "M", {**MD, "seed_anchors": [{"name": "x"}]}),
        Segment(
            "a.jsonl", [arch({**MD, "seed_anchors": [{"name": "x", "file_path": None}]}, "domain")]
        ),
    ],
    new=[DelSegment("a.jsonl")],
)
_SEG = [arch(DR | {"id": "D1x"}), arch(DR | {"id": "D2x"})]


def _rewrite(lines: list[dict], *, reverse: bool) -> bytes:
    out = b""
    for line in lines:
        items = list(line.items())
        if reverse:
            items.reverse()
        out += json.dumps(dict(items)).encode() + b"\n"
    return out


CASES["segment_rewritten_keys_reordered"] = Case(
    ref=[Segment("a.jsonl", _SEG)], new=[Raw("archive/a.jsonl", _rewrite(_SEG, reverse=True))]
)
CASES["segment_one_line_removed"] = Case(
    ref=[Segment("a.jsonl", _SEG)], new=[Raw("archive/a.jsonl", _rewrite(_SEG[:1], reverse=False))]
)
CASES["bindings_shard_line_removed_no_copy"] = Case(
    ref=[Line("bindings", "D", B0)], new=[DelLine("bindings", "D")]
)
_EF = entity_body("E", descriptor={"name": "f", "file_path": "x.py"})
CASES["entity_file_descriptor_differs_deleted"] = Case(
    ref=[Line("entity", "E", E0), File("entity", _EF)], new=[DelFile("entity", "E")]
)
CASES["bindings_file_relation_differs_deleted"] = Case(
    ref=[
        Line("bindings", "D", [{**_BE, "relation": "affects"}]),
        File("bindings", [{**_BE, "relation": "creates"}], "D"),
    ],
    new=[DelFile("bindings", "D")],
)
_V1 = json.dumps({"id": "V", "a": 1, "b": 2}).encode()
_V2 = json.dumps({"b": 2, "a": 1, "id": "V"}).encode()
CASES["verification_rewritten_reordered"] = Case(
    ref=[Raw("verifications/V.json", _V1)], new=[Raw("verifications/V.json", _V2)]
)
CASES["verification_modified"] = Case(
    ref=[Raw("verifications/V.json", _V1)], new=[Raw("verifications/V.json", b'{"id":"V"}')]
)
CASES["initiative_deleted"] = Case(
    ref=[Raw("initiatives/I.json", b'{"id":"I","name":"n"}')],
    new=[Raw("initiatives/I.json", None)],
)
CASES["ratified_after_ref_and_folded"] = Case(
    ref=[Line("decision", "D", D0)], new=[Line("decision", "D", D1)]
)
CASES["entity_moved_and_folded"] = Case(
    ref=[Line("entity", "E", E0)], new=[DelLine("entity", "E"), Line("entity", "E", E1)]
)
CASES["anchor_added_and_folded"] = Case(
    ref=[Line("bindings", "D", bindings_body("E1"))],
    new=[DelLine("bindings", "D"), Line("bindings", "D", B0)],
)
CASES["created_and_folded"] = Case(ref=[], new=[Line("decision", "D", D0)])
CASES["created_and_folded_with_rule5_removal"] = Case(
    ref=[Line("decision", "D", D1), File("decision", D0)],  # a shadow-divergence
    new=[DelFile("decision", "D"), Line("decision", "N", decision_body("N"))],
)
_BAD1 = bad(D1)
CASES["model_invalid_repair_reverts_accepted"] = Case(
    ref=[Line("decision", "D", _BAD1)],
    new=[DelLine("decision", "D"), File("decision", D0)],
)
CASES["model_invalid_repair_changes_choice"] = Case(
    ref=[Line("decision", "D", _BAD1)],
    new=[DelLine("decision", "D"), File("decision", {**D1, "choice": "something else"})],
)
CASES["model_invalid_repair_clears_ratifier"] = Case(
    ref=[Line("decision", "D", _BAD1)],
    new=[
        DelLine("decision", "D"),
        File("decision", {**D1, "ratified_by": None, "ratified_at": None}),
    ],
)
CASES["bindings_duplicate_resolved_to_smaller_line"] = Case(
    ref=[
        Line("bindings", "D", bindings_body("E1")),
        Line("bindings", "D", bindings_body("E1", "E2")),
    ],
    new=[DelLine("bindings", "D", k=1)],
)
CASES["file_deleted_older_verbatim_line_added"] = Case(
    ref=[Line("decision", "D", D0), File("decision", D1)],
    new=[DelFile("decision", "D"), Line("decision", "D", D0, other("D"))],
)
_SH = [Line("decision", "D", accept(D0, "a")), File("decision", accept(D0, "b"))]
CASES["ref_shadow_line_deleted_illegal_stamp"] = Case(ref=list(_SH), new=[DelLine("decision", "D")])
CASES["ref_shadow_line_rewritten_to_file"] = Case(
    ref=list(_SH),
    new=[DelLine("decision", "D"), Line("decision", "D", accept(D0, "b"))],
)
CASES["dup_packed_keep_proposed_drop_accepted_same_file"] = Case(
    ref=[Line("decision", "D", D0), Line("decision", "D", D1)],
    new=[DelLine("decision", "D", k=1)],
)
CASES["dup_packed_keep_proposed_drop_accepted_across_files"] = Case(
    ref=[Line("decision", "D", D0), Line("decision", "D", D1, other("D"))],
    new=[DelLine("decision", "D", other("D"))],
)
CASES["parseable_invalid_file_beside_readable_line_resolved_older"] = Case(
    ref=[Line("decision", "D", D0), File("decision", _BAD1)],
    new=[File("decision", D0)],
)
CASES["tweaked_old_entity_body_file_deleted"] = Case(
    ref=[Line("entity", "E", E0), File("entity", E1)],
    new=[
        DelFile("entity", "E"),
        DelLine("entity", "E"),
        Line("entity", "E", entity_body("E", descriptor={"name": "f", "file_path": "c.py"})),
    ],
)
_B1 = [{"entity_id": "E1", "tier": 2, "relation": "affects", "weight": 1.0}]
_B2 = [{"entity_id": "E1", "tier": 2, "relation": "creates", "weight": 1.0}]
CASES["bindings_line_old_relation_tweaked_weight"] = Case(
    ref=[Line("bindings", "D", _B1), File("bindings", _B2, "D")],
    new=[
        DelFile("bindings", "D"),
        DelLine("bindings", "D"),
        Line("bindings", "D", [{**_B1[0], "weight": 0.5}]),
    ],
)
CASES["edit_and_fold_one_pr"] = Case(
    ref=[Line("fact", "F", F0)], new=[DelLine("fact", "F"), Line("fact", "F", F1)]
)
CASES["two_sides_of_duplicate_ulid_removed"] = Case(
    ref=list(_DUP), new=[DelFile("decision", "D"), DelSegment("a.jsonl")]
)
CASES["downgrade_leaves_shard"] = Case(ref=[Line("decision", "D", D0)], new=[Marker(M1)])
CASES["marker_downgrade_from_2"] = Case(
    ref=[Line("decision", "D", D0)],
    new=[Marker(M1), Raw(f"shards/{own('D')}", None)],  # the record is lost with its shard
)
CASES["hot_accepted_deleted_verbatim_into_new_segment"] = Case(
    ref=[File("decision", D1)],
    new=[DelFile("decision", "D"), Segment("a.jsonl", [arch(D1)])],
)
CASES["hot_accepted_deleted_onto_nonterminal_ref_archive"] = Case(
    ref=[File("decision", D1), Segment("a.jsonl", [arch(D1)])],
    new=[DelFile("decision", "D")],
)
_GARBAGE = b"not a record at all\n"
CASES["unrecoverable_line_removed_with_readable_line"] = Case(
    ref=[Raw(f"shards/{own('D')}", _GARBAGE + encode_line("decision", "D", D0) + b"\n")],
    new=[Raw(f"shards/{own('D')}", b"")],
)

# review round 1: unreadable REF occurrences
_BAD_ARCH = arch({**D0, "valid_to": "not a date"})  # a model-invalid archive line
CASES["unreadable_archive_history_deleted"] = Case(
    ref=[
        Line("decision", "D", D0),
        Raw("decisions/D.json", b"broken"),
        Segment("a.jsonl", [_BAD_ARCH]),
    ],
    new=[Raw("decisions/D.json", None), DelSegment("a.jsonl")],
)
CASES["unchanged_invalid_accepted_archive_beside_chosen_older"] = Case(
    ref=[
        Line("decision", "D", D0),
        Raw("decisions/D.json", b"broken"),
        Segment("a.jsonl", [arch(bad(D1))]),
    ],
    new=[Raw("decisions/D.json", None)],
)
CASES["sole_unreadable_file_deleted"] = Case(
    ref=[Raw("decisions/D.json", b"broken")], new=[Raw("decisions/D.json", None)]
)
CASES["unparseable_edit_over_valid_shard"] = Case(
    ref=[Line("decision", "D", D0)], new=[Raw("decisions/D.json", b"broken")]
)
CASES["bindings_shard_line_rewritten_to_smaller_list"] = Case(
    ref=[Line("bindings", "D", B0), File("bindings", bindings_body("E1"), "D")],
    new=[DelLine("bindings", "D"), Line("bindings", "D", bindings_body("E1"))],
)
CASES["stray_older_line_added_file_deleted"] = Case(
    ref=[Line("decision", "D", D0), File("decision", D1)],
    new=[DelFile("decision", "D"), Line("decision", "D", D0, "zz.shard")],
)

# review round 2
CASES["unparseable_file_deleted_archive_line_added"] = Case(
    ref=[Raw("decisions/D.json", b"broken")],
    new=[Raw("decisions/D.json", None), Segment("a.jsonl", [arch(DR)])],
)
CASES["unparseable_shard_line_replaced_by_archive_line"] = Case(
    ref=[Raw(f"shards/{own('D')}", b'{"type":"decision","id":"D","body":{broken\n')],
    new=[Raw(f"shards/{own('D')}", b""), Segment("a.jsonl", [arch(DR)])],
)
CASES["unparseable_shard_line_replaced_by_new_shard_line"] = Case(
    ref=[Raw(f"shards/{own('D')}", b'{"type":"decision","id":"D","body":{broken\n')],
    new=[Raw(f"shards/{own('D')}", encode_line("decision", "D", D0) + b"\n")],
)

FINDING = {
    "status_change_and_fold_same_diff": PROV,
    "destroyed_null_key_top": LOST,
    "destroyed_null_key_nested": LOST,
    "descriptor_language_on_fold": LOST,
    "bindings_entry_key_lost": LOST,
    "lost_with_verbatim_sibling": LOST,
    "lost_shard_modified_archive_deleted": LOST,
    "lost_routine_fold": LOST,
    "lost_shadowed_line_deleted_file_stays": LOST,
    "superseding_replaces_null_key_line_segment_deleted": LOST,
    "segment_line_key_missing_in_shard": DEL,
    "illegal_status_jump": JUMP,
    "non_divergent_file_removed": DEL,
    "bindings_line_showing_dropped_entry_removed_or_rewritten": LOST,
    "seed_anchors_null_lost_by_fold": LOST,
    "seed_anchors_null_lost_by_segment_delete": LOST,
    "segment_rewritten_keys_reordered": FIELD,
    "bindings_shard_line_removed_no_copy": DEL,
    "entity_file_descriptor_differs_deleted": DEL,
    "bindings_file_relation_differs_deleted": DEL,
    "verification_rewritten_reordered": FIELD,
    "ratified_after_ref_and_folded": PROV,
    "entity_moved_and_folded": PROV,
    "anchor_added_and_folded": PROV,
    "created_and_folded": PROV,
    "created_and_folded_with_rule5_removal": PROV,
    "model_invalid_repair_reverts_accepted": JUMP,
    "model_invalid_repair_changes_choice": FIELD,
    "model_invalid_repair_clears_ratifier": FIELD,
    "bindings_duplicate_resolved_to_smaller_line": LOST,
    "file_deleted_older_verbatim_line_added": DEL,
    "ref_shadow_line_deleted_illegal_stamp": FIELD,
    "ref_shadow_line_rewritten_to_file": FIELD,
    "dup_packed_keep_proposed_drop_accepted_same_file": JUMP,
    "dup_packed_keep_proposed_drop_accepted_across_files": JUMP,
    "parseable_invalid_file_beside_readable_line_resolved_older": JUMP,  # §28 Codex 2 multi-copy
    "segment_one_line_removed": FIELD,
    "tweaked_old_entity_body_file_deleted": PROV,
    "bindings_line_old_relation_tweaked_weight": PROV,
    "edit_and_fold_one_pr": PROV,
    "two_sides_of_duplicate_ulid_removed": DEL,
    "downgrade_leaves_shard": "format-downgrade",
    "marker_downgrade_from_2": "format-downgrade",
    "hot_accepted_deleted_verbatim_into_new_segment": DEL,
    "hot_accepted_deleted_onto_nonterminal_ref_archive": DEL,
    "unrecoverable_line_removed_with_readable_line": DEL,  # §28 Opus q1: cannot hide it
    "initiative_deleted": DEL,
    "verification_modified": FIELD,
    "unreadable_archive_history_deleted": DEL,
    "unchanged_invalid_accepted_archive_beside_chosen_older": JUMP,
    "sole_unreadable_file_deleted": DEL,
    "unparseable_edit_over_valid_shard": DEL,
    "bindings_shard_line_rewritten_to_smaller_list": LOST,
    "stray_older_line_added_file_deleted": DEL,
    "unparseable_file_deleted_archive_line_added": DEL,
    "unparseable_shard_line_replaced_by_archive_line": DEL,
    "unparseable_shard_line_replaced_by_new_shard_line": DEL,
}


def _run(git_repo: Path, name: str) -> list:
    store, ref = build_case(git_repo, CASES[name])
    return verify_against(store, ref)


# One representative row per rule stays here; the rest of the matrix is in
# tests/private/test_verify_format2_against.py (release-only).
PUBLIC_CLEAN = [
    "verbatim_file_to_shard_live_and_terminal_all_types",
    "sanctioned_shadow_removal_lifecycle_reversal",
    "migration_format1_to_format2_fold",
]
PUBLIC_FINDING = [
    "status_change_and_fold_same_diff",  # shard-body-without-source
    "destroyed_null_key_top",  # illegal-field-change (a lost key)
    "illegal_status_jump",
    "non_divergent_file_removed",  # illegal-deletion
    "segment_rewritten_keys_reordered",  # illegal-field-change (a rewritten segment)
]


@pytest.mark.slow
@pytest.mark.parametrize("case", PUBLIC_CLEAN)
def test_against_clean(git_repo: Path, case: str) -> None:
    # a compatibility guard for the packed layer: each case is a state the layer must accept;
    # the mutants listed in the module's report are what make each of them bite
    found = _run(git_repo, case)
    assert found == [], found


@pytest.mark.slow
@pytest.mark.parametrize("case", PUBLIC_FINDING)
def test_against_finding(git_repo: Path, case: str) -> None:
    # containment: one diff may break two rules (a deleted segment both loses a key and is an
    # illegal deletion); the named code is the one the case exists for
    found = _run(git_repo, case)
    assert FINDING[case] in codes(found), found


def test_every_case_is_listed() -> None:
    assert set(CASES) == set(CLEAN) | set(FINDING)


# -- the pieces under the dispatcher -----------------------------------------------------------


def _inventory(root: Path, ops: list):
    from sidegraph.store_format import inventory_from_disk
    from tests.store_format2_support import _apply, make_store, validator

    store = make_store(root)
    for op in ops:
        _apply(root, store, op)
    return inventory_from_disk(store, validate=validator)


def test_diff_occurrences_compares_a_multiset_of_bodies_per_path_type_id(tmp_path: Path) -> None:
    # red: keyed without k, two lines of one identity in one file collapse into one
    from sidegraph.store_rules import diff_occurrences

    ref = _inventory(tmp_path / "a", [Line("decision", "D", D0), Line("decision", "D", D1)])
    new = _inventory(tmp_path / "b", [Line("decision", "D", D1), Line("decision", "D", D1)])
    d = diff_occurrences(ref, new).identities[("decision", "D")]
    assert [o.raw for o in d.removed] == [D0]
    assert [o.raw for o in d.added] == [D1]
    assert len(d.unchanged) == 1
    assert d.destroyed == d.removed and d.changed


def test_diff_occurrences_files_are_keyed_by_path(tmp_path: Path) -> None:
    from sidegraph.store_rules import diff_occurrences

    ref = _inventory(tmp_path / "a", [File("decision", D0), File("fact", F0)])
    new = _inventory(tmp_path / "b", [File("decision", D1), File("fact", F0)])
    diff = diff_occurrences(ref, new).identities
    changed = diff[("decision", "D")]
    assert [(o.raw, n.raw) for o, n in changed.modified] == [(D0, D1)]
    assert [o.raw for o in changed.destroyed] == [D0] and [o.raw for o in changed.added] == [D1]
    assert not diff[("fact", "F")].changed


def test_diff_occurrences_ignores_key_order_and_whitespace_of_a_line(tmp_path: Path) -> None:
    from sidegraph.store_rules import diff_occurrences

    ref = _inventory(tmp_path / "a", [Line("decision", "D", D0)])
    path = tmp_path / "b"
    new = _inventory(
        path,
        [
            Raw(
                "shards/" + own("D"),
                json.dumps(
                    {"type": "decision", "id": "D", "body": dict(reversed(list(D0.items())))},
                    indent=1,
                )
                .replace("\n", " ")
                .encode()
                + b"\n",
            )
        ],
    )
    assert not diff_occurrences(ref, new).identities[("decision", "D")].changed


@pytest.mark.slow
def test_committed_symlink_in_ref_is_not_a_deletion(git_repo: Path) -> None:
    # red: the git-tree view reads a committed symlink as an unreadable occurrence, and a
    # transition layer that trusts it reports the record as deleted (or crashes)
    import os

    from tests.store_format2_support import commit_all, make_store

    store = make_store(git_repo)
    (store / "decisions").mkdir()
    os.symlink("elsewhere.json", store / "decisions" / "D.json")
    ref = commit_all(git_repo, "ref", date="2026-02-01T00:00:00Z")
    found = verify_against(store, ref)
    assert codes(found) == {"symlinked-store-entry"}
    (store / "decisions" / "D.json").unlink()
    (store / "decisions" / "D.json").write_text(json.dumps(D0), encoding="utf-8")
    assert verify_against(store, ref) == []


@pytest.mark.slow
def test_removed_occurrence_is_reported_without_ref_line_number(git_repo: Path) -> None:
    # red: a removed line was reported as the work-tree path with REF's line number
    found = _run(git_repo, "bindings_shard_line_removed_no_copy")
    assert found and all(not v.path.rsplit("/", 1)[-1].count(":") for v in found), found


@pytest.mark.slow
def test_against_json_with_a_malformed_marker_is_structured(git_repo: Path, capsys) -> None:
    # red: the marker error was re-raised by the inventory: plain text, exit 1, findings lost
    from sidegraph.cli import verify_main

    store, ref = build_case(git_repo, Case(ref=[Line("decision", "D", D0)]))
    (store / "format").write_text("garbage\n", encoding="utf-8")
    assert verify_main(["--db", str(store), "--against", ref, "--json"]) == 2
    out = json.loads(capsys.readouterr().out)
    assert any(v["code"] == "format-downgrade" for v in out["violations"]), out


def _notes(a: object, b: object) -> list:
    return [
        Segment("a.jsonl", [arch({**DR, "note": a})]),
        Segment("b.jsonl", [arch({**DR, "note": b})]),
    ]


@pytest.mark.slow
@pytest.mark.parametrize(
    "ref_ops,dup",
    [
        ([File("decision", {**D0, "title": "other"}), Segment("a.jsonl", [arch(DR)])], True),
        ([File("decision", {**DR, "note": "kept"}), Segment("a.jsonl", [arch(DR)])], False),
        ([Line("decision", "D", D0), Segment("a.jsonl", [arch(DS)])], False),  # superseding
        ([Line("decision", "D", D1), Segment("a.jsonl", [arch({**DR, "title": "x"})])], True),
        ([Segment("a.jsonl", [arch(DR)]), Segment("b.jsonl", [arch({**DR, "note": 1})])], True),
        ([Segment("a.jsonl", [arch(DR)]), Segment("b.jsonl", [arch(DR)])], False),
        (
            [
                Segment("a.jsonl", [arch({**DR, "note": 1})]),
                Segment("b.jsonl", [arch({**DR, "note": True})]),
            ],
            True,
        ),
    ],
)
def test_ref_duplicate_ulid_agrees_with_the_snapshot_layer(
    tmp_path: Path, ref_ops: list, dup: bool
) -> None:
    # `_ref_duplicate_ulid` re-states `_packed_archive_overlap` over occurrences; the two must
    # give one verdict on the same stores
    from sidegraph.schema import validate_record_body
    from sidegraph.store_format import inventory_from_disk
    from sidegraph.verify import DUPLICATE_ULID, _Against, verify_snapshot
    from tests.store_format2_support import _apply, make_store

    store = make_store(tmp_path)
    for op in ref_ops:
        _apply(tmp_path, store, op)
    inv = inventory_from_disk(store, validate=validate_record_body)
    d = _Against("", inv, inv).diff.identities[("decision", "D")]
    mine = _Against("", inv, inv)._ref_duplicate_ulid(d)
    theirs = DUPLICATE_ULID in codes(verify_snapshot(store))
    assert mine == theirs == dup


@pytest.mark.slow
def test_against_does_not_report_a_deleted_record_for_a_permission_denied_file(
    git_repo: Path,
) -> None:
    # red: an unreadable (mode 000) tracked file read as "no effective record": illegal-deletion
    import os

    if not hasattr(os, "geteuid") or os.geteuid() == 0:
        pytest.skip("needs POSIX permissions; root bypasses them")
    store, ref = build_case(git_repo, Case(ref=[File("decision", D0)]))
    path = store / "decisions" / "D.json"
    path.chmod(0)
    try:
        assert DEL not in codes(verify_against(store, ref))
    finally:
        path.chmod(0o644)


@pytest.mark.slow
def test_against_with_a_malformed_marker_still_runs_the_independent_checks(
    git_repo: Path,
) -> None:
    # red: the marker error returned early and skipped path rules and provenance
    case = Case(
        ref=[Line("decision", "D", D0), Raw("initiatives/I.json", b'{"id":"I","name":"n"}')],
        new=[
            Raw("initiatives/I.json", None),
            DelLine("decision", "D"),
            Line("decision", "D", D1),
            Marker("garbage\n"),
        ],
    )
    store, ref = build_case(git_repo, case)
    got = codes(verify_against(store, ref))
    assert {"format-downgrade", "illegal-deletion", "shard-body-without-source"} <= got


@pytest.mark.slow
def test_a_non_marker_value_error_is_not_a_format_downgrade(git_repo: Path, monkeypatch) -> None:
    # red: `except ValueError` relabelled any inventory error as a downgrade
    from sidegraph import verify

    store, ref = build_case(git_repo, Case(ref=[Line("decision", "D", D0)]))

    real = verify.inventory_from_disk
    calls = []

    def boom(*a, **k):
        if not calls:
            calls.append(1)
            raise ValueError("not a marker problem")
        return real(*a, **k)  # a retry after a mislabelled catch would succeed

    monkeypatch.setattr(verify, "inventory_from_disk", boom)
    with pytest.raises(ValueError, match="not a marker problem"):
        verify_against(store, ref)


@pytest.mark.slow
def test_against_json_with_a_non_utf8_marker_is_structured(git_repo: Path, capsys) -> None:
    # red: the narrowed catch let UnicodeDecodeError through: plain text, exit 1
    from sidegraph.cli import verify_main

    store, ref = build_case(git_repo, Case(ref=[Line("decision", "D", D0)]))
    (store / "format").write_bytes(b"\xff\xfe garbage\n")
    assert verify_main(["--db", str(store), "--against", ref, "--json"]) == 2
    got = {v["code"] for v in json.loads(capsys.readouterr().out)["violations"]}
    assert {"format-downgrade", "unknown-schema-version"} <= got


def test_duplicate_archive_payloads_differing_as_1_and_true(tmp_path: Path) -> None:
    # format 1 keeps Python's ``==`` (byte-identical to the base); a packed store compares
    # JSON types. red: the exemption in `_check_duplicate_ulids` or `_packed_archive_overlap`
    # comparing with the other equality
    from sidegraph.verify import DUPLICATE_ULID, verify_snapshot
    from tests.store_format2_support import format1_store, make_store, put_archive, put_shard_line

    def segments(store: Path, other: object) -> None:
        put_archive(store, "a", [arch({**DR, "note": 1})])
        put_archive(store, "b", [arch({**DR, "note": other})])

    for other in (True, 1.0):
        one = format1_store(tmp_path / f"one-{other}" / ".sidegraph")
        segments(one, other)
        assert DUPLICATE_ULID not in codes(verify_snapshot(one))
        two = make_store(tmp_path / f"two-{other}")
        put_shard_line(
            two, "decision", "D", {**DR, "note": 1}
        )  # a compatible shard line beside the segments
        segments(two, other)
        assert DUPLICATE_ULID in codes(verify_snapshot(two))


@pytest.mark.parametrize("change", ["deleted", "repaired"])
@pytest.mark.parametrize("bad_id", ["../x", ["x"], None])
def test_a_packed_store_file_with_an_unsafe_baseline_id_is_no_record(
    git_repo: Path, change: str, bad_id: object
) -> None:
    """As in format 1: the reload never loaded a file whose body id is unsafe, so deleting it,
    or repairing its id, is no transition finding."""
    from tests.store_format2_support import commit_all, make_store, put_file

    store = make_store(git_repo, fmt=2)
    body = decision_body("D1", id=bad_id)
    if bad_id is None:
        del body["id"]  # no id at all
    path = put_file(store, "decision", body, record_id="D1")
    commit_all(git_repo, "unsafe id", date=T)
    if change == "deleted":
        path.unlink()
    else:
        path.write_text(json.dumps(decision_body("D1")), encoding="utf-8")
    assert codes(verify_against(store, "HEAD")) == set()


@pytest.mark.parametrize("edit", ["unchanged", "edited"])
def test_an_unsafe_id_file_beside_a_valid_shard_line_shadows_nothing(
    git_repo: Path, edit: str
) -> None:
    """The file is no record on either side, so the shard line of the same identity is what
    both trees hold: nothing was deleted."""
    from tests.store_format2_support import commit_all, make_store, put_file, put_shard_line

    store = make_store(git_repo, fmt=2)
    put_shard_line(store, "decision", "D1", decision_body("D1", status="accepted"))
    path = put_file(store, "decision", decision_body("D1", id="../x"), record_id="D1")
    commit_all(git_repo, "shard line and a stray", date=T)
    if edit == "edited":
        path.write_text(
            json.dumps(decision_body("D1", id="../x", title="edited")), encoding="utf-8"
        )
    assert codes(verify_against(store, "HEAD")) == set()


def test_a_real_file_record_edited_to_an_unsafe_id_is_still_judged(git_repo: Path) -> None:
    """The baseline file held a loaded record D1 (beside its shard line): editing its id to an
    unsafe one is a change to a real record, not the cleanup of a stray."""
    from tests.store_format2_support import commit_all, make_store, put_file, put_shard_line

    store = make_store(git_repo, fmt=2)
    put_shard_line(store, "decision", "D1", decision_body("D1", status="accepted"))
    path = put_file(store, "decision", decision_body("D1", status="accepted"))
    commit_all(git_repo, "file and shard copy", date=T)
    path.write_text(json.dumps(decision_body("D1", id="../x", status="accepted")), encoding="utf-8")
    assert codes(verify_against(store, "HEAD")) != set()


@pytest.mark.parametrize("content", ["not json", "[1]"])
def test_deleting_an_undecodable_packed_store_file_is_illegal(git_repo: Path, content: str) -> None:
    """A packed store quarantines the identity of a file that does not decode: it stands for a
    damaged record, so deleting it is still a judged loss (pre-release panel Opus 2 rejected;
    the private matrix's sole_unreadable_file_deleted pins the same rule)."""
    from tests.store_format2_support import commit_all, make_store

    store = make_store(git_repo, fmt=2)
    path = store / "decisions" / "D1.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    commit_all(git_repo, "undecodable file", date=T)
    path.unlink()
    assert "illegal-deletion" in codes(verify_against(store, "HEAD"))


@pytest.mark.parametrize("fmt", [1, 2])
@pytest.mark.parametrize("body", [{"id": "../x", "name": "n"}, {"name": "n"}, "not json", None])
# "not json" is a damaged initiative, not a stray: its deletion stays illegal like ``None``'s
def test_deleting_a_never_loaded_initiative_file_is_no_finding(
    git_repo: Path, fmt: int, body: object
) -> None:
    """Pre-release panel, Muse r2: the never-loaded rule covers initiatives too. ``None`` is the
    control: a real initiative's deletion is still illegal."""
    from tests.store_format2_support import commit_all, make_store

    store = make_store(git_repo, fmt=fmt)
    path = store / "initiatives" / "I1.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    real = {"id": "I1", "name": "n"}
    text = body if isinstance(body, str) else json.dumps(real if body is None else body)
    path.write_text(text, encoding="utf-8")
    commit_all(git_repo, "initiative file", date=T)
    path.unlink()
    expected = {"illegal-deletion"} if body is None or body == "not json" else set()
    assert codes(verify_against(store, "HEAD")) == expected


def test_a_new_unsafe_id_file_beside_a_valid_shard_line_is_judged(git_repo: Path) -> None:
    """Rule 1 quarantines the identity once the file appears, so the shard line's record is no
    longer served: an added never-loaded file is judged, unlike one present on both sides."""
    from tests.store_format2_support import commit_all, make_store, put_shard_line

    store = make_store(git_repo, fmt=2)
    put_shard_line(store, "decision", "D1", decision_body("D1", status="accepted"))
    commit_all(git_repo, "a valid shard line", date=T)
    path = store / "decisions" / "D1.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(decision_body("D1", id="../x")), encoding="utf-8")
    assert codes(verify_against(store, "HEAD")) != set()
