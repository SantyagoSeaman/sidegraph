"""``sidegraph-verify`` snapshot layer on a packed (format 2) store: the findings of the shard
lines, the per-record-file-over-shard relation, the dropped bindings entry, the reference checks
on packed bodies, and the states maintenance heals, which verify never reports.

# see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§7.1, §9 T-V1)
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

import pytest

from sidegraph.cli import verify_main
from sidegraph.maintenance import self_healing_counts
from sidegraph.store_format import (
    ShardListing,
    bucket_name,
    inventory_from_disk,
)
from sidegraph.store_format import bucket as bucket_of
from sidegraph.verify import (
    BAD_ARCHIVE_SEGMENT,
    BAD_SHARD_LINE,
    BINDING_ENTRY_DROPPED,
    DANGLING_BINDING_ENTITY,
    DANGLING_FACT_SUPPORT,
    DANGLING_PARENT,
    DANGLING_SUPERSEDES,
    DUPLICATE_PACKED_RECORD,
    DUPLICATE_ULID,
    PARENT_CYCLE,
    PARSE_ERROR,
    SHADOW_DIVERGENCE,
    SHARD_CONFLICT,
    SHARD_NAME_CASE,
    SHARDS_WITHOUT_FORMAT2,
    SUPERSEDED_WITHOUT_SUCCESSOR,
    _check_shard_name_case,
    verify_snapshot,
)
from tests.store_format2_support import (
    archived_decision,
    bindings_body,
    codes,
    decision_body,
    domain_body,
    entity_body,
    fact_body,
    make_store,
    put_archive,
    put_file,
    put_shard_line,
    validator,
)

_SHARD_PATH = re.compile(r"shards/[0-9a-fA-F]{2}\.(?:shard|SHARD):\d+")


def _exit(store: Path) -> int:
    return verify_main(["--db", str(store)])


def _of(findings: list, code: str) -> list:
    return [f for f in findings if f.code == code]


def _raw_shard(store: Path, name: str, data: bytes) -> Path:
    path = store / "shards" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _case_sensitive(tmp_path: Path) -> bool:
    probe = tmp_path / "caseprobe"
    probe.mkdir()
    (probe / "a").write_text("1")
    (probe / "A").write_text("2")
    return len(list(probe.iterdir())) == 2


# -- a clean packed store ----------------------------------------------------------------------


def test_clean_packed_store_is_clean(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    put_shard_line(store, "decision", "D001", decision_body("D001"))
    put_shard_line(store, "entity", "E001", entity_body("E001"))
    put_shard_line(store, "bindings", "D001", bindings_body("E001"))
    put_file(store, "fact", fact_body("F001", supports=["D001"]))
    assert verify_snapshot(store) == []
    assert _exit(store) == 0


# -- T-V1: one fixture per finding --------------------------------------------------------------


def _bad_envelope(store: Path) -> None:
    _raw_shard(store, "00.shard", b'{"type":"decision","id":"D001"}\n')


def _bad_json(store: Path) -> None:
    _raw_shard(store, "00.shard", b"this is not json\n")


def _bad_model(store: Path) -> None:
    bad = decision_body("D001")
    del bad["title"]
    put_shard_line(store, "decision", "D001", bad)


def _bad_body_id(store: Path) -> None:
    put_shard_line(store, "decision", "D001", decision_body("OTHER"))


@pytest.mark.parametrize("build", [_bad_envelope, _bad_json, _bad_model, _bad_body_id])
def test_bad_shard_line(tmp_path: Path, build) -> None:  # T-V1
    # red: the shard layer is not walked (unfixed code reports nothing for a packed store)
    store = make_store(tmp_path)
    build(store)
    found = verify_snapshot(store)
    assert BAD_SHARD_LINE in codes(found)
    assert PARSE_ERROR not in codes(found)  # a shard line is not a record file
    (hit,) = _of(found, BAD_SHARD_LINE)
    assert _SHARD_PATH.fullmatch(hit.path)
    assert _exit(store) == 2


def test_bad_shard_line_reports_the_line_number(tmp_path: Path) -> None:  # T-V1
    # red: a path without the line, or one counted from 0
    store = make_store(tmp_path)
    good = decision_body("D001")
    put_shard_line(store, "decision", "D001", good, bucket="05.shard")
    path = store / "shards" / "05.shard"
    path.write_bytes(path.read_bytes() + b"garbage\n")
    (hit,) = _of(verify_snapshot(store), BAD_SHARD_LINE)
    assert hit.path == "shards/05.shard:2"


def test_shard_conflict(tmp_path: Path) -> None:  # T-V1
    # red: conflict markers reported as bad-shard-line only, or not at all
    store = make_store(tmp_path)
    put_shard_line(store, "decision", "D001", decision_body("D001"), bucket="05.shard")
    path = store / "shards" / "05.shard"
    ours = path.read_bytes()
    theirs = ours.replace(b"proposed", b"accepted")
    path.write_bytes(b"<<<<<<< HEAD\n" + ours + b"=======\n" + theirs + b">>>>>>> branch\n")
    found = verify_snapshot(store)
    assert SHARD_CONFLICT in codes(found)
    assert all(_SHARD_PATH.fullmatch(f.path) for f in _of(found, SHARD_CONFLICT))
    assert BAD_SHARD_LINE not in codes(found)  # the marker lines are the conflict, not bad lines
    assert _exit(store) == 2


def test_duplicate_packed_record(tmp_path: Path) -> None:  # T-V1
    # red: two lines of one identity with differing parsed bodies go unreported
    store = make_store(tmp_path)
    put_shard_line(store, "decision", "D001", decision_body("D001"))
    put_shard_line(store, "decision", "D001", decision_body("D001", status="accepted"))
    found = verify_snapshot(store)
    assert DUPLICATE_PACKED_RECORD in codes(found)
    assert _SHARD_PATH.fullmatch(_of(found, DUPLICATE_PACKED_RECORD)[0].path)
    assert _exit(store) == 2


def test_duplicate_packed_record_in_two_files_incl_stray(tmp_path: Path) -> None:  # T-V1
    # red: a stray file's differing copy is not compared with the bucket file's
    store = make_store(tmp_path)
    put_shard_line(store, "decision", "D001", decision_body("D001"))
    put_shard_line(store, "decision", "D001", decision_body("D001", status="accepted"),
                   bucket="extra.shard")  # fmt: skip
    assert DUPLICATE_PACKED_RECORD in codes(verify_snapshot(store))


def test_shadow_divergence(tmp_path: Path) -> None:  # T-V1
    # red: a file that is not a legal successor of its shard copy is accepted silently
    store = make_store(tmp_path)
    put_shard_line(store, "decision", "D001", decision_body("D001", status="accepted"))
    put_file(store, "decision", decision_body("D001", status="proposed"))
    found = verify_snapshot(store)
    assert SHADOW_DIVERGENCE in codes(found)
    assert DUPLICATE_ULID not in codes(found)  # file + shard is the normal pre-maintenance pair
    assert _exit(store) == 2


def test_legal_file_over_shard_is_clean(tmp_path: Path) -> None:  # T-V1
    # red: a mutant judging every differing pair as divergent (an edit of a packed record)
    store = make_store(tmp_path)
    put_shard_line(store, "decision", "D001", decision_body("D001", status="proposed"))
    put_file(store, "decision", decision_body("D001", status="accepted"))
    assert verify_snapshot(store) == []


def test_bindings_files_exempt_from_shadow_divergence(tmp_path: Path) -> None:  # T-V1
    # both bodies are readable (valid relations); the file differs from its shard line
    # red: divergence reported for readable bindings
    store = make_store(tmp_path)
    put_file(store, "entity", entity_body("E001"))
    put_file(store, "entity", entity_body("E002"))
    put_shard_line(
        store, "bindings", "D001", [{"entity_id": "E001", "tier": 2, "relation": "affects"}]
    )
    put_file(
        store,
        "bindings",
        [
            {"entity_id": "E001", "tier": 1, "relation": "modifies"},
            {"entity_id": "E002", "tier": 2},
        ],
        record_id="D001",
    )
    assert verify_snapshot(store) == []  # clean: nothing unreadable, nothing divergent


def test_binding_entry_dropped(tmp_path: Path) -> None:  # T-V1
    # merged tree: main's shard line holds E002, the branch's bindings file never had it
    # red: judged against a REF/merge base (no base here), or not judged at all
    store = make_store(tmp_path)
    put_file(store, "entity", entity_body("E001"))
    put_file(store, "entity", entity_body("E002"))
    put_shard_line(store, "bindings", "D001", bindings_body("E001", "E002"))
    put_file(store, "bindings", bindings_body("E001"), record_id="D001")
    found = verify_snapshot(store)
    (hit,) = _of(found, BINDING_ENTRY_DROPPED)
    assert "E002" in hit.detail and "E001" not in hit.detail
    assert _exit(store) == 2


def test_binding_entry_dropped_exclusions(tmp_path: Path) -> None:
    # abstract community:* excluded; a concrete entity named community:… is not; an unresolved
    # entity is not.
    # red: exclusion by entity_id prefix; exclusion by name without kind; unresolved treated
    # as derived
    store = make_store(tmp_path)
    put_file(store, "entity", entity_body("DERIV1", kind="abstract", canonical_name="community:7"))
    put_file(store, "entity", entity_body("community-1", canonical_name="community:looks"))
    put_file(store, "entity", entity_body("community-2", canonical_name="plain"))
    put_file(store, "entity", entity_body("KEEP"))
    put_shard_line(
        store, "bindings", "R1", bindings_body("DERIV1", "KEEP"), bucket=bucket_name(1)
    )  # dropped: DERIV1 only (excluded)
    put_file(store, "bindings", bindings_body("KEEP"), record_id="R1")
    put_shard_line(store, "bindings", "R2", bindings_body("community-1"), bucket=bucket_name(2))
    put_file(store, "bindings", [], record_id="R2")  # concrete, named community:…
    put_shard_line(store, "bindings", "R3", bindings_body("community-2"), bucket=bucket_name(3))
    put_file(store, "bindings", [], record_id="R3")  # concrete, id starts like the prefix
    put_shard_line(store, "bindings", "R4", bindings_body("GHOST"), bucket=bucket_name(4))
    put_file(store, "bindings", [], record_id="R4")  # unresolved
    dropped = {f.detail: f for f in _of(verify_snapshot(store), BINDING_ENTRY_DROPPED)}
    text = "\n".join(dropped)
    assert "DERIV1" not in text
    assert "community-1" in text
    assert "community-2" in text
    assert "GHOST" in text
    assert len(dropped) == 3


def test_shard_name_case_unit_two_equal_lower() -> None:  # T-V1
    # two files whose names are equal lower-cased (cannot coexist on APFS/NTFS, hence a unit)
    # red: collisions not turned into a finding
    listing = ShardListing({}, [Path("shards/AB.shard"), Path("shards/ab.shard")], ["AB.shard"])
    found = _check_shard_name_case(listing)
    assert [f.code for f in found] == [SHARD_NAME_CASE]
    assert found[0].path == "shards/AB.shard"


def test_shard_name_case_two_equal_lower_on_disk(tmp_path: Path) -> None:  # T-V1
    if not _case_sensitive(tmp_path):
        pytest.skip("case-insensitive filesystem cannot hold both files")
    store = make_store(tmp_path)
    put_shard_line(store, "decision", "D001", decision_body("D001"), bucket="0a.shard")
    _raw_shard(store, "0A.shard", b"")
    assert SHARD_NAME_CASE in codes(verify_snapshot(store))


def test_shard_name_case_upper_name(tmp_path: Path) -> None:  # T-V1
    # red: a *.SHARD file invisible (the bucket-name check is case-sensitive)
    store = make_store(tmp_path)
    put_shard_line(store, "decision", "D001", decision_body("D001"), bucket="0A.SHARD")
    found = verify_snapshot(store)
    assert SHARD_NAME_CASE in codes(found)
    assert _of(found, SHARD_NAME_CASE)[0].path == "shards/0A.SHARD"
    assert _exit(store) == 2


def test_other_stray_names_are_not_case_findings(tmp_path: Path) -> None:
    # red: every stray flagged as a case problem
    store = make_store(tmp_path)
    put_shard_line(store, "decision", "D001", decision_body("D001"), bucket="extra.shard")
    assert SHARD_NAME_CASE not in codes(verify_snapshot(store))


def test_bad_archive_segment_nonterminal_line(tmp_path: Path) -> None:  # T-V1
    # red: a readable line of a nonterminal status accepted by the packed-store path
    store = make_store(tmp_path)
    line = {"record_type": "decision", **decision_body("D001", status="accepted")}
    put_archive(store, "seg1", [line])
    found = verify_snapshot(store)
    assert BAD_ARCHIVE_SEGMENT in codes(found)
    assert _exit(store) == 2


def test_shards_without_format2(tmp_path: Path) -> None:  # T-V1
    # red: shard files under a major-1 marker pass
    store = make_store(tmp_path, fmt=1)
    _raw_shard(store, "00.shard", b"")
    assert SHARDS_WITHOUT_FORMAT2 in codes(verify_snapshot(store))
    assert _exit(store) == 2


# -- states maintenance heals are not reported --------------------------------------------------


def _misplaced(store: Path) -> None:
    other = bucket_name((bucket_of("D001", 64) + 1) % 64)
    put_shard_line(store, "decision", "D001", decision_body("D001"), bucket=other)


def _equal_duplicate(store: Path) -> None:
    put_shard_line(store, "decision", "D001", decision_body("D001"))
    put_shard_line(store, "decision", "D001", decision_body("D001"))


def _stray(store: Path) -> None:
    put_shard_line(store, "decision", "D001", decision_body("D001"), bucket="extra.shard")


@pytest.mark.parametrize(
    ("state", "build"),
    [("misplaced", _misplaced), ("equal_duplicate", _equal_duplicate), ("stray", _stray)],
)
def test_self_healing_states_not_reported(tmp_path: Path, state: str, build) -> None:  # T-V1
    # red: any of the three reported (a bucket-position check, an equal-body duplicate check,
    # a stray-name check)
    store = make_store(tmp_path)
    build(store)
    assert verify_snapshot(store) == []
    assert _exit(store) == 0
    inv = inventory_from_disk(store, validate=validator)
    assert self_healing_counts(inv, 64)[state] == 1


# -- duplicate-ulid on a packed store -----------------------------------------------------------


def test_format2_duplicate_ulid_rules(tmp_path: Path) -> None:
    # red: file+archive and shard+archive always duplicate (format-1 rule), or never
    arch = archived_decision("D001")
    arch_body = {k: v for k, v in arch.items() if k != "record_type"}

    def fresh(name: str) -> Path:
        return make_store(tmp_path / name)

    # file + shard: not a duplicate
    s = fresh("file_shard")
    put_shard_line(s, "decision", "D001", decision_body("D001"))
    put_file(s, "decision", decision_body("D001"))
    assert DUPLICATE_ULID not in codes(verify_snapshot(s))
    # file + archive, compatible: not a duplicate
    s = fresh("file_arch_ok")
    put_file(s, "decision", arch_body)
    put_archive(s, "seg1", [arch])
    assert DUPLICATE_ULID not in codes(verify_snapshot(s))
    # file + archive, incompatible: duplicate
    s = fresh("file_arch_bad")
    put_file(s, "decision", decision_body("D001"))
    put_archive(s, "seg1", [arch])
    assert DUPLICATE_ULID in codes(verify_snapshot(s))
    # shard + archive, the archive line supersedes the shard body: not a duplicate
    s = fresh("shard_arch_superseding")
    put_shard_line(s, "decision", "D001", decision_body("D001", status="accepted"))
    later = {  # accepted -> deprecated is a legal move, so this line is a successor of the shard
        "record_type": "decision",
        **decision_body("D001", status="deprecated", valid_to="2026-02-01T00:00:00Z"),
    }
    put_archive(s, "seg1", [later])
    assert DUPLICATE_ULID not in codes(verify_snapshot(s))
    # shard + archive, compatible (the shard already holds the terminal state): not a duplicate
    s = fresh("shard_arch_compatible")
    put_shard_line(s, "decision", "D001", arch_body)
    put_archive(s, "seg1", [arch])
    assert DUPLICATE_ULID not in codes(verify_snapshot(s))
    # shard + archive, unrelated bodies (an immutable field differs): duplicate
    s = fresh("shard_arch_bad")
    put_shard_line(s, "decision", "D001", decision_body("D001", title="another"))
    put_archive(s, "seg1", [arch])
    assert DUPLICATE_ULID in codes(verify_snapshot(s))
    # differing archive lines of one identity: duplicate, even with a file compatible with one
    s = fresh("arch_differ")
    other = archived_decision("D001", title="another")
    put_file(s, "decision", arch_body)  # compatible with the first archive line
    put_archive(s, "seg1", [arch])
    put_archive(s, "seg2", [other])
    assert DUPLICATE_ULID in codes(verify_snapshot(s))
    # byte-identical archive pair: exempt
    s = fresh("arch_identical")
    put_archive(s, "seg1", [arch])
    put_archive(s, "seg2", [arch])
    assert DUPLICATE_ULID not in codes(verify_snapshot(s))
    # two per-record files of one id stay a duplicate
    s = fresh("two_files")
    put_file(s, "decision", decision_body("D001"))
    shutil.copy(s / "decisions" / "D001.json", s / "decisions" / "D001-copy.json")
    assert DUPLICATE_ULID in codes(verify_snapshot(s))


def test_format1_file_and_archive_stay_duplicate(tmp_path: Path) -> None:
    # red: the format-2 relaxation applied to a format-1 store
    store = make_store(tmp_path, fmt=1)
    arch = archived_decision("D001")
    put_file(store, "decision", {k: v for k, v in arch.items() if k != "record_type"})
    put_archive(store, "seg1", [arch])
    assert DUPLICATE_ULID in codes(verify_snapshot(store))


# -- the reference checks see packed bodies -----------------------------------------------------


def test_reference_checks_see_shard_bodies(tmp_path: Path) -> None:
    # red: the shard bodies are not part of the pools (everything dangles, or nothing does)
    store = make_store(tmp_path)
    put_shard_line(store, "decision", "D001", decision_body("D001", status="accepted"))
    put_shard_line(store, "decision", "D002", decision_body("D002", supersedes="D001"))
    put_shard_line(store, "entity", "E001", entity_body("E001"))
    put_shard_line(store, "bindings", "D001", bindings_body("E001"))
    put_shard_line(store, "domain", "M001", domain_body("M001"))
    put_shard_line(store, "domain", "M002", domain_body("M002", parent_id="M001"))
    put_shard_line(store, "fact", "F001", fact_body("F001", supports=["D001"]))
    assert verify_snapshot(store) == []


def test_reference_findings_on_shard_bodies(tmp_path: Path) -> None:
    # red: any of the four checks skipped for packed bodies
    store = make_store(tmp_path)
    put_shard_line(store, "decision", "D001", decision_body("D001", supersedes="NOPE"))
    put_shard_line(store, "decision", "D002", decision_body("D002", status="superseded"))
    put_shard_line(store, "bindings", "D001", bindings_body("GHOST"))
    put_shard_line(store, "domain", "M001", domain_body("M001", parent_id="NOPE"))
    put_shard_line(store, "fact", "F001", fact_body("F001", supports=["NOPE"]))
    found = verify_snapshot(store)
    for code in (
        DANGLING_SUPERSEDES,
        SUPERSEDED_WITHOUT_SUCCESSOR,
        DANGLING_BINDING_ENTITY,
        DANGLING_PARENT,
        DANGLING_FACT_SUPPORT,
    ):
        hits = _of(found, code)
        assert hits, code
        assert all(_SHARD_PATH.fullmatch(h.path) for h in hits), (code, [h.path for h in hits])


def test_reference_checks_run_on_a_shadowed_shard_line(tmp_path: Path) -> None:
    # the per-record file is clean, the shard copy under it dangles: still a finding
    # red: only the effective body of an identity is checked
    store = make_store(tmp_path)
    put_shard_line(store, "decision", "D001", decision_body("D001", supersedes="NOPE"))
    put_file(store, "decision", decision_body("D001"))
    found = verify_snapshot(store)
    assert any(_SHARD_PATH.fullmatch(f.path) for f in _of(found, DANGLING_SUPERSEDES))


# -- review round 1 ----------------------------------------------------------------------------

_KINDS = [
    (
        "decision",
        lambda i: decision_body(i, status="accepted"),
        lambda i: decision_body(i, status="proposed"),
    ),
    (
        "domain",
        lambda i: domain_body(i, status="accepted"),
        lambda i: domain_body(i, status="proposed"),
    ),
    (
        "fact",
        lambda i: fact_body(i, status="accepted"),
        lambda i: fact_body(i, status="proposed"),
    ),
    (
        "entity",
        lambda i: entity_body(i, canonical_name="one"),
        lambda i: entity_body(i, canonical_name="two"),
    ),
]


@pytest.mark.parametrize(("kind", "shard_body", "file_body"), _KINDS, ids=[k[0] for k in _KINDS])
def test_shadow_divergence_every_kind(tmp_path: Path, kind: str, shard_body, file_body) -> None:
    # red: shadow checks skipped for one kind
    store = make_store(tmp_path)
    put_shard_line(store, kind, "R001", shard_body("R001"))
    put_file(store, kind, file_body("R001"))
    assert SHADOW_DIVERGENCE in codes(verify_snapshot(store))
    assert _exit(store) == 2


@pytest.mark.parametrize("big_in_extra", [False, True], ids=["AB", "BA"])
def test_binding_entry_dropped_unions_differing_shard_lines(
    tmp_path: Path, big_in_extra: bool
) -> None:
    # red: judged only when exactly one distinct shard line exists, or only against the first
    # line (the {E1,E2} line is the first in AB and the second in BA)
    store = make_store(tmp_path)
    put_file(store, "entity", entity_body("E001"))
    put_file(store, "entity", entity_body("E002"))
    big, small = bindings_body("E001", "E002"), bindings_body("E001")
    put_shard_line(store, "bindings", "D001", small if big_in_extra else big)
    put_shard_line(store, "bindings", "D001", big if big_in_extra else small, bucket="extra.shard")
    put_file(store, "bindings", bindings_body("E001"), record_id="D001")
    found = verify_snapshot(store)
    assert DUPLICATE_PACKED_RECORD in codes(found)
    (hit,) = _of(found, BINDING_ENTRY_DROPPED)
    assert "E002" in hit.detail


@pytest.mark.parametrize("bad", [[]], ids=["list"])
def test_unhashable_binding_entity_in_a_shard_line_is_a_finding(tmp_path: Path, bad) -> None:
    # red: the invalid body enters the pools and the membership test raises (exit 1, no report)
    store = make_store(tmp_path)
    put_shard_line(store, "bindings", "D001", [{"entity_id": bad, "tier": 2}])
    put_shard_line(store, "decision", "D002", decision_body("D002", status="superseded"))
    found = verify_snapshot(store)
    assert BAD_SHARD_LINE in codes(found)
    assert SUPERSEDED_WITHOUT_SUCCESSOR in codes(found)  # the rest of the report survives
    assert _exit(store) == 2


@pytest.mark.parametrize(
    ("kind", "body"),
    [
        ("decision", decision_body("R001", supersedes=[])),
        ("fact", fact_body("R001", supports=[[]])),
        ("fact", fact_body("R001", supersedes={})),
        ("domain", domain_body("R001", parent_id=[])),
    ],
    ids=["supersedes", "supports", "fact-supersedes", "parent"],
)
def test_non_string_reference_in_a_shard_line_is_a_finding(tmp_path: Path, kind, body) -> None:
    # regression guard (the old checks already test isinstance, so no mutant of the pooling
    # alone turns it red): the report survives and names the line
    store = make_store(tmp_path)
    put_shard_line(store, kind, "R001", body)
    put_shard_line(store, "decision", "D002", decision_body("D002", status="superseded"))
    found = verify_snapshot(store)
    assert BAD_SHARD_LINE in codes(found)
    assert SUPERSEDED_WITHOUT_SUCCESSOR in codes(found)
    assert _exit(store) == 2


def test_reference_to_an_unreadable_shard_line_does_not_dangle(tmp_path: Path) -> None:
    # red: the identity of an unreadable line is dropped from the pools
    store = make_store(tmp_path)
    bad = decision_body("D001")
    del bad["title"]
    put_shard_line(store, "decision", "D001", bad)
    put_shard_line(store, "decision", "D002", decision_body("D002", supersedes="D001"))
    found = verify_snapshot(store)
    assert BAD_SHARD_LINE in codes(found)
    assert DANGLING_SUPERSEDES not in codes(found)


def test_checks_run_on_every_shard_occurrence(tmp_path: Path) -> None:
    # a superseded shard line shadowed by a proposed file: its own finding, at its own line
    # red: the per-record checks look at one representative body per identity
    store = make_store(tmp_path)
    put_shard_line(store, "decision", "D001", decision_body("D001", status="superseded"))
    put_file(store, "decision", decision_body("D001", status="proposed"))
    found = verify_snapshot(store)
    assert SHADOW_DIVERGENCE in codes(found)
    (hit,) = _of(found, SUPERSEDED_WITHOUT_SUCCESSOR)
    assert _SHARD_PATH.fullmatch(hit.path)


def test_checks_run_on_a_shadowed_archive_line(tmp_path: Path) -> None:
    # an archive line shadowed by a shard line, and a second archive line for the id
    # red: only the archive line the pool kept is checked
    store = make_store(tmp_path)
    lone = archived_decision("D001", supersedes="NOPE")
    put_shard_line(store, "decision", "D001", decision_body("D001", supersedes="D002"))
    put_shard_line(store, "decision", "D002", decision_body("D002"))
    put_archive(store, "seg1", [lone])
    found = verify_snapshot(store)
    hits = _of(found, DANGLING_SUPERSEDES)
    assert any(h.path.endswith("seg1.jsonl") for h in hits)
    second = tmp_path / "second"
    store = make_store(second)
    put_archive(store, "seg1", [archived_decision("D003")])
    put_archive(store, "seg2", [archived_decision("D003", supersedes="NOPE")])
    hits = _of(verify_snapshot(store), DANGLING_SUPERSEDES)
    assert any(h.path.endswith("seg2.jsonl") for h in hits)


def test_shard_paths_use_forward_slashes(tmp_path: Path) -> None:
    # regression guard for Windows CI (a Path-built location only misbehaves there)
    store = make_store(tmp_path)
    _bad_json(store)
    (hit,) = _of(verify_snapshot(store), BAD_SHARD_LINE)
    assert isinstance(hit.path, str)
    assert "\\" not in hit.path
    assert hit.path == "shards/00.shard:1"


def _skip_if_chmod_is_moot(tmp_path: Path) -> None:
    probe = tmp_path / "probe"
    probe.write_text("x")
    probe.chmod(0)
    try:
        readable = os.access(probe, os.R_OK)
    finally:
        probe.chmod(0o600)
    if readable:
        pytest.skip("mode 000 does not make a file unreadable here (root, or Windows)")


def test_unreadable_shard_file_is_a_finding_not_an_abort(tmp_path: Path) -> None:
    # red: the PermissionError propagates and verify exits 1 with no report
    _skip_if_chmod_is_moot(tmp_path)
    store = make_store(tmp_path)
    shard = put_shard_line(store, "decision", "D001", decision_body("D001"))
    put_shard_line(store, "decision", "D002", decision_body("D002", status="superseded"))
    shard.chmod(0)
    try:
        found = verify_snapshot(store)
        code = _exit(store)
    finally:
        shard.chmod(0o600)
    unreadable = [f for f in _of(found, BAD_SHARD_LINE) if f.path == f"shards/{shard.name}"]
    assert unreadable
    assert code == 2


@pytest.mark.parametrize("fmt", [1, 2])
def test_unreadable_record_file_is_a_parse_error_on_both_formats(tmp_path: Path, fmt: int) -> None:
    # red: the abort of a packed store where format 1 reports and goes on
    _skip_if_chmod_is_moot(tmp_path)
    store = make_store(tmp_path, fmt=fmt)
    path = put_file(store, "decision", decision_body("D001"))
    put_file(store, "decision", decision_body("D002", status="superseded"))
    path.chmod(0)
    try:
        found = verify_snapshot(store)
        code = _exit(store)
    finally:
        path.chmod(0o600)
    assert PARSE_ERROR in codes(found)
    assert SUPERSEDED_WITHOUT_SUCCESSOR in codes(found)
    assert code == 2


# -- review round 2 ----------------------------------------------------------------------------


@pytest.mark.parametrize("invalid_first", [True, False], ids=["invalid-first", "valid-first"])
def test_archive_pool_carries_the_first_parsed_line(tmp_path: Path, invalid_first: bool) -> None:
    # a.jsonl holds a line that fails its model, b.jsonl a valid line of the same id whose
    # supersedes dangles; the walk pools the first PARSED line, the occurrence loop must not
    # assume it was the first READABLE one
    # red: the carried occurrence inferred from readability
    store = make_store(tmp_path)
    broken = archived_decision("D001")
    del broken["title"]
    good = archived_decision("D001", supersedes="NOPE")
    first, second = (broken, good) if invalid_first else (good, broken)
    put_archive(store, "a", [first])
    put_archive(store, "b", [second])
    assert DANGLING_SUPERSEDES in codes(verify_snapshot(store))


def test_parent_cycle_on_a_shadowed_shard_line(tmp_path: Path) -> None:
    # red: cycle detection only on the pooled bodies
    store = make_store(tmp_path)
    put_shard_line(store, "domain", "M001", domain_body("M001", parent_id="M001"))
    put_file(store, "domain", domain_body("M001"))
    found = verify_snapshot(store)
    (hit,) = _of(found, PARENT_CYCLE)
    assert _SHARD_PATH.fullmatch(hit.path)


def test_cycle_through_a_pooled_parent_on_a_shadowed_line(tmp_path: Path) -> None:
    # red: the walk stops at the occurrence's own id instead of closing there
    store = make_store(tmp_path)
    put_shard_line(store, "domain", "M001", domain_body("M001", parent_id="M002"))
    put_file(store, "domain", domain_body("M001"))
    put_file(store, "domain", domain_body("M002", parent_id="M001"))
    assert PARENT_CYCLE in codes(verify_snapshot(store))


def test_one_successor_set_for_pooled_and_shadowed_records(tmp_path: Path) -> None:
    # D1 is superseded by the shard line of D2, which a D2 file (without the link) shadows
    # red: the pooled check counts only the pooled bodies' links
    store = make_store(tmp_path)
    put_shard_line(store, "decision", "D001", decision_body("D001", status="superseded"))
    put_shard_line(store, "decision", "D002", decision_body("D002", supersedes="D001"))
    put_file(store, "decision", decision_body("D002"))
    assert SUPERSEDED_WITHOUT_SUCCESSOR not in codes(verify_snapshot(store))
