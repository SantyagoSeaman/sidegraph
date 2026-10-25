"""A dry run's ``wrote`` list equals the real run's, in order, and the real run's list is
exactly what git sees changed under the store: the preview simulates the publisher's phases,
and nothing else ties the two together on stores with misplaced lines, strays and segments.

Every case builds a committed format-2 repository and runs ``Store.maintain`` dry, then real.
# see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§6.2)
"""

# ruff: noqa: F811  (the imported ``git_repo`` fixture is redefined as a test argument)

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from sidegraph.store import Store
from tests.store_format2_support import (
    archived_decision,
    build,
    commit_all,
    decision_body,
    fact_body,
    git,
    git_repo,  # noqa: F401  (pytest fixture)
    ids_in_bucket,
    make_store,
    put_archive,
    put_file,
    put_shard_line,
)

OLD = "2026-01-01T00:00:00Z"


def b_fixture(name):
    return lambda p: build(name, p)


def b_reorder(p):
    s = make_store(p)
    a, b = ids_in_bucket(7, 2, prefix="RO")
    put_shard_line(s, "decision", a, decision_body(a))
    path = put_shard_line(s, "decision", b, decision_body(b))
    lines = path.read_bytes().splitlines(keepends=True)
    path.write_bytes(b"".join(reversed(lines)))
    return s


def b_segment_dup(p):
    s = make_store(p)
    rid = ids_in_bucket(9, 1, prefix="SG")[0]
    arch = archived_decision(rid)
    put_shard_line(s, "decision", rid, {k: v for k, v in arch.items() if k != "record_type"})
    put_archive(s, "seg1", [arch])
    return s


def b_segment_fold(p):
    s = make_store(p)
    put_archive(s, "seg2", [archived_decision("ARCHX"), archived_decision("ARCHY")])
    put_file(s, "decision", decision_body("D1"))
    return s


def b_unencodable(p):
    s = make_store(p)
    put_file(s, "fact", fact_body("01A", future_key="x"))
    path = s / "facts" / "01A.json"
    path.write_text(path.read_text("utf-8").replace('"x"', '"\\ud800"'), "utf-8")
    put_file(s, "fact", fact_body("01B", statement="ok"))
    return s


def b_unencodable_seg(p):
    s = make_store(p)
    line = json.dumps(archived_decision("01G", f="x"))
    put_archive(s, "seg", [line.replace('"x"', '"\\ud800"')])
    put_file(s, "decision", decision_body("D9"))
    return s


def b_stray_dup_equal(p):
    s = make_store(p)
    x = ids_in_bucket(3, 1, prefix="Q")[0]
    put_shard_line(s, "decision", x, decision_body(x))  # in its bucket
    put_shard_line(s, "decision", x, decision_body(x), bucket="extra.shard")  # equal, stray
    put_shard_line(
        s, "decision", x, decision_body(x), bucket="03.shard"
    )  # equal duplicate in bucket
    return s


def b_mixed(p):
    s = make_store(p)
    put_file(s, "decision", decision_body("M1"))
    put_file(s, "decision", decision_body("M2", status="accepted"))
    x, y = ids_in_bucket(3, 2, prefix="MX")
    put_shard_line(s, "decision", x, decision_body(x), bucket="11.shard")
    put_shard_line(s, "decision", y, decision_body(y), bucket="zz.shard")
    (s / "shards" / "yy.shard").write_bytes(b"")
    put_archive(s, "segm", [archived_decision("MARCH")])
    return s


def b_empty_stray_only(p):
    s = make_store(p)
    (s / "shards").mkdir(parents=True, exist_ok=True)
    for b in range(64):
        (s / "shards" / f"{b:02x}.shard").write_bytes(b"")
    (s / "shards" / "ff.shard").write_bytes(b"")
    return s


FIX = {
    n: b_fixture(n)
    for n in [
        "swap_two_shards",
        "three_shard_cycle",
        "misplaced_with_eligible_newer_file",
        "swap_with_phase_c_replacement",
        "stray_into_two_buckets",
        "stray_skipped_beside_eligible",
        "bucket_two_equal_lines",
        "equal_bindings_bodies_one_in_dest_other_misplaced_or_stray",
    ]
}
FIX.update(
    reorder=b_reorder,
    segment_dup=b_segment_dup,
    segment_fold=b_segment_fold,
    unencodable=b_unencodable,
    unencodable_seg=b_unencodable_seg,
    stray_dup_equal=b_stray_dup_equal,
    mixed=b_mixed,
    empty_stray_only=b_empty_stray_only,
)


def porcelain(repo: Path) -> list[str]:
    out = git(repo, "status", "--porcelain", "-uall", "--", ".sidegraph")
    return sorted(line[3:].removeprefix(".sidegraph/") for line in out.splitlines())


# one fixture per kind of write: moves, a cycle, canonicalising, segment dedupe and fold, strays
WRITE_KINDS = [
    "swap_two_shards",
    "three_shard_cycle",
    "reorder",
    "segment_dup",
    "segment_fold",
    "mixed",
]


@pytest.mark.parametrize("name", WRITE_KINDS)
def test_dry_run_wrote_equals_real_wrote_equals_git_changes(git_repo, tmp_path, name):
    staging = tmp_path / "staging"
    staging.mkdir()
    shutil.move(str(FIX[name](staging)), str(git_repo / ".sidegraph"))
    store = git_repo / ".sidegraph"
    Store(store).close()
    commit_all(git_repo, "init", date=OLD)
    with Store(store) as s:
        dry = s.maintain(settled_days=0, dry_run=True, any_branch=False)
    assert dry.stopped is None
    with Store(store) as s:
        real = s.maintain(settled_days=0, dry_run=False, any_branch=False)
    assert real.stopped is None
    assert dry.wrote == real.wrote
    assert sorted(real.wrote) == porcelain(git_repo)
