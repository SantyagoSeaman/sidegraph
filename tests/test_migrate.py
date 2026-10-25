"""``sidegraph-migrate``: the marker first, then maintenance (T-M3, T-M4).

Every test runs the real command (or ``migrate``) over a store on disk, mostly inside a git
repository whose store is committed on ``main``.
# see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§6.1, §9 T-M3, T-M4)
"""

# ruff: noqa: F811  (the imported ``git_repo`` fixture is redefined as a test argument)

from __future__ import annotations

import errno
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

from sidegraph import maintenance, maintenance_clock
from sidegraph import migrate as migrate_module
from sidegraph.cli import compact_main, migrate_main
from sidegraph.migrate import MigrationRefused, migrate, recover_shard_count
from sidegraph.schema import Decision
from sidegraph.store import Store, StoreBusyError
from sidegraph.store_format import bucket as bucket_of
from sidegraph.store_format import bucket_name, inventory_from_disk
from sidegraph.store_layout import store_marker
from sidegraph.store_rules import effective_all
from sidegraph.verify import verify_snapshot
from tests.store_format2_support import (
    GITIGNORE,
    OLD,
    STOPS,
    Killed,
    commit_all,
    decision_body,
    domain_body,
    first_shard,
    format1_committed,
    git,
    git_repo,  # noqa: F401  (pytest fixture)
    index_stamp_of,
    make_store,
    put_archive,
    put_file,
    put_shard_line,
    record_publishes,
    shard_bytes,
    tree_bytes,
    validator,
)


def run(store: Path, *args: str) -> int:
    return migrate_main(["--db", str(store), *args])


def markerless_committed(repo: Path) -> Path:
    """The committed format-1 store with its ``format`` file removed (a store older than the
    marker): the first ``Store`` open would backfill ``sidegraph-store 0.6.0``."""
    store = format1_committed(repo)
    (store / "format").unlink()
    commit_all(repo, "no marker", date=OLD)
    return store


def tree_state(root: Path) -> dict[str, tuple[bool, bytes | None, int]]:
    """Every file and directory under ``root``: name -> (is_file, bytes, mtime_ns)."""
    out: dict[str, tuple[bool, bytes | None, int]] = {}
    for path in sorted(root.rglob("*")):
        is_file = path.is_file()
        try:
            data = path.read_bytes() if is_file else None
        except PermissionError:
            data = b"<unreadable>"
        out[path.relative_to(root).as_posix()] = (is_file, data, path.stat().st_mtime_ns)
    return out


def snapshot_as_format2(store: Path) -> dict:
    """The effective records of the store read as format 2 whatever its marker says."""
    inv = inventory_from_disk(store, validate=validator, assume_major=2)
    return {k: e.raw for k, e in effective_all(inv).items()}


def survives(store: Path, before: dict) -> bool:
    now = snapshot_as_format2(store)
    return all(k in now and now[k] == v for k, v in before.items())


def gitattributes_has_line(store: Path) -> bool:
    path = store / ".gitattributes"
    return path.is_file() and "*.shard -text" in path.read_text(encoding="utf-8").splitlines()


def marker_of(store: Path):
    return store_marker(store)


# -- recover_shard_count ---------------------------------------------------------------------


def names(count: int, *, skip: tuple[int, ...] = ()) -> list[str]:
    return [bucket_name(b) for b in range(count) if b not in skip]


def test_recover_shard_count_is_the_largest_complete_set() -> None:
    assert recover_shard_count(names(16)) == 16
    assert recover_shard_count(names(64)) == 64
    assert recover_shard_count(names(256)) == 256
    assert recover_shard_count(names(64) + ["stray.shard", "ZZ.shard"]) == 64
    # 64 files present but 3f missing: only the 16-set is complete
    assert recover_shard_count(names(64, skip=(63,))) == 16
    assert recover_shard_count(names(15)) is None  # no complete set at all
    assert recover_shard_count(names(15) + ["stray.shard"]) is None  # 16 files, not a set
    assert recover_shard_count([]) is None


# -- T-M3: the order of the first durable steps ------------------------------------------------


def test_marker_fsynced_before_any_shard(git_repo, monkeypatch):  # T-M3, red: marker last
    store = format1_committed(git_repo)
    calls = record_publishes(monkeypatch, store)
    migrate(store, dry_run=False, any_branch=True)
    assert [c for c in calls if c[0] == "replace"][0] == ("replace", "format")
    first_replace = calls.index(("replace", "format"))
    assert ("fsync_tmp", "format") in calls[:first_replace]  # the tmp is flushed before replace
    assert ("fsync_dir", "store") in calls[first_replace:]
    assert calls.index(("fsync_dir", "store"), first_replace) < first_shard(calls)
    marker = marker_of(store)
    assert marker is not None and marker.major == 2 and marker.shards == 64
    assert marker.requirement == "0.17.0"


def test_marker_requirement_is_raised_to_the_format_floor(git_repo):
    store = format1_committed(git_repo)
    (store / "format").write_text(
        "sidegraph-store 1.0.0\nrequires-sidegraph >= 0.16.0\n", encoding="utf-8"
    )
    migrate(store, dry_run=False, any_branch=True)
    marker = marker_of(store)
    assert marker is not None and marker.requirement == "0.17.0"  # max(existing, 0.17.0)


def test_migrated_store_opens_stamped_as_format_2(git_repo):
    store = format1_committed(git_repo)
    before = snapshot_as_format2(store)
    migrate(store, dry_run=False, any_branch=True)
    assert index_stamp_of(store) == "0.7.0+format2"  # the handle was reloaded as major 2
    Store(store).close()  # opens as any format-2 store does
    assert verify_snapshot(store) == []
    assert survives(store, before)


def interrupted_migration_completes(git_repo, monkeypatch, stop_after, committed_marker, shape):
    # T-M3 + the interval between `format` and `.gitattributes` (rev 14). That the 0.16 wheel
    # refuses the interrupted store is asserted in the older-wheel test (Task 16).
    store = format1_committed(git_repo) if shape == "format1" else markerless_committed(git_repo)
    before = snapshot_as_format2(store)
    stop = STOPS[stop_after]

    def hook(label: str) -> None:
        if stop(label):
            raise Killed(label)

    with monkeypatch.context() as mp:
        mp.setattr(maintenance, "checkpoint", hook)
        with pytest.raises(Killed):
            migrate(store, dry_run=False, any_branch=True)
    assert marker_of(store).major == 2  # the marker was durable before every later step
    if committed_marker:
        commit_all(git_repo, "interrupted migration", date=OLD)
        finish = compact_main(["--db", str(store), "--settled-days", "0"])
    else:
        finish = run(store)  # HEAD's marker is below 2: migrate resumes
    assert finish == 0
    assert gitattributes_has_line(store)
    assert survives(store, before)
    assert not (store / "decisions" / "D1.json").exists()  # maintenance ran to the end


@pytest.mark.parametrize("committed_marker", [False, True])
@pytest.mark.parametrize("stop_after", ["format", "phase_c"])
def test_interrupted_migration_completes(git_repo, monkeypatch, stop_after, committed_marker):
    """The first and the last stop; the release run crosses every stop with every shape."""
    interrupted_migration_completes(git_repo, monkeypatch, stop_after, committed_marker, "format1")


# -- T-M4: entry conditions refuse and write nothing ---------------------------------------------


def break_store(repo: Path, store: Path, cond: str) -> None:
    if cond == "not_default_branch":
        git(repo, "switch", "-q", "-c", "topic")
    elif cond == "uncommitted_store":
        put_file(store, "decision", decision_body("D9"))
        return
    elif cond == "parse-error":
        (store / "decisions" / "BAD.json").write_text("{not json", encoding="utf-8")
    elif cond == "parse-error-unpacked":
        # initiatives are never packed, so only the snapshot layer sees this one
        (store / "initiatives").mkdir()
        (store / "initiatives" / "BAD.json").write_text("{not json", encoding="utf-8")
    elif cond == "unsafe-record-id":
        put_file(store, "decision", decision_body("X/Y"), record_id="UNSAFE")
    elif cond == "filename-id-mismatch":
        put_file(store, "decision", decision_body("D3"), record_id="D4")
    elif cond == "symlinked-store-entry":
        elsewhere = store.parent / "elsewhere"
        elsewhere.mkdir()
        (store / "facts" / "F1.json").replace(elsewhere / "F1.json")
        (store / "facts").rmdir()
        (store / "facts").symlink_to(elsewhere, target_is_directory=True)
    elif cond == "store_does_not_open":
        (store / "format").write_text(
            "sidegraph-store 1.0.0\nrequires-sidegraph >= 99.0.0\n", encoding="utf-8"
        )
    else:
        raise AssertionError(cond)
    commit_all(repo, cond, date=OLD)


@pytest.mark.parametrize(
    "cond",
    [
        "not_default_branch",
        "uncommitted_store",
        "parse-error",
        "parse-error-unpacked",
        "unsafe-record-id",
        "filename-id-mismatch",
        "symlinked-store-entry",
        "store_does_not_open",
    ],
)
def test_entry_conditions_refuse_write_nothing(git_repo, capsys, cond):  # T-M4
    store = format1_committed(git_repo)
    break_store(git_repo, store, cond)
    before = tree_bytes(store)
    status = git(git_repo, "status", "--porcelain")
    assert run(store) == 2
    out = capsys.readouterr().out
    assert out.startswith("refused:")
    assert tree_bytes(store) == before and git(git_repo, "status", "--porcelain") == status
    assert not (store / "shards").exists() and not (store / ".gitattributes").exists()
    assert not (store / "index.db").exists()  # the store was never opened


def test_any_branch_lifts_only_the_branch_condition(git_repo, capsys):
    store = format1_committed(git_repo)
    git(git_repo, "switch", "-q", "-c", "topic")
    assert run(store) == 2
    assert "--any-branch" in capsys.readouterr().out
    assert run(store, "--any-branch") == 0
    assert marker_of(store).major == 2


def test_detached_head_refused(git_repo, capsys):
    store = format1_committed(git_repo)
    git(git_repo, "checkout", "-q", "--detach")
    assert run(store) == 2
    assert "detached" in capsys.readouterr().out


def test_a_path_holding_no_store_is_refused_never_created(tmp_path, capsys):
    target = tmp_path / "nothing"
    assert run(target) == 2
    assert not target.exists()
    assert "no store" in capsys.readouterr().out


# -- the pairs rule ------------------------------------------------------------------------------


def pair_store(repo: Path, pair: str) -> tuple[Path, str]:
    """A format-1 store with one per-record file and one archive line of the same identity."""
    store = make_store(repo, fmt=1)
    (store / ".gitignore").write_text(GITIGNORE, encoding="utf-8")
    if pair == "revived_domain":
        rid = "DMR"
        put_file(store, "domain", domain_body(rid, status="superseded"))
        put_archive(
            store,
            "seg",
            [{"record_type": "domain", **domain_body(rid, status="dropped")}],
        )
    else:
        rid = "DK"
        archived = decision_body(rid, status="rejected", valid_to="2026-02-01T00:00:00Z")
        file_body = dict(archived)
        if pair == "kept_hot_extra_top":
            file_body["x_note"] = "kept hot with a key the dump lacks"
        elif pair == "kept_hot_extra_nested":
            file_body["provenance"] = {**archived["provenance"], "x_nested": 1}
        elif pair == "raw_file_lacking_dump_nulls":
            # the archive line is the model's dump, with every optional key present as null/[]
            dump = Decision.model_validate(archived).model_dump(mode="json")
            archived = {
                k: v for k, v in dump.items() if k in file_body or v is None or v in ([], {})
            }
            assert set(archived) > set(file_body)  # keys the raw file never had, all empty
        else:
            raise AssertionError(pair)
        put_file(store, "decision", file_body)
        put_archive(store, "seg", [{"record_type": "decision", **archived}])
    commit_all(repo, pair, date=OLD)
    return store, rid


@pytest.mark.parametrize(
    "pair",
    [
        "revived_domain",
        "kept_hot_extra_top",
        "kept_hot_extra_nested",
        "raw_file_lacking_dump_nulls",
    ],
)
def test_compatible_pairs_pass(git_repo, capsys, pair):  # T-M4, red: check_relocation as compat
    store, rid = pair_store(git_repo, pair)
    before = snapshot_as_format2(store)
    assert run(store) == 0
    out = capsys.readouterr().out
    record_type = "domain" if pair == "revived_domain" else "decision"
    assert "skipped" not in out  # not a duplicate-ulid
    assert not (
        store / ("domains" if record_type == "domain" else "decisions") / f"{rid}.json"
    ).exists()
    shard_text = b"".join(shard_bytes(store).values())
    assert rid.encode() in shard_text  # the file was packed, with its extra keys
    if pair == "kept_hot_extra_top":
        assert b"x_note" in shard_text
    if pair == "kept_hot_extra_nested":
        assert b"x_nested" in shard_text
    segment = store / "archive" / "seg.jsonl"
    if pair == "raw_file_lacking_dump_nulls":
        # red: `compatible` as the deletion test. The shard line lacks the dump's null keys, so
        # the segment is the only holder of them: it stays and is reported.
        assert segment.exists() and "archive/seg.jsonl" in out
    else:
        assert not segment.exists()
    assert survives(store, before)


def mixed_segment_store(repo: Path, archive_is_successor: bool) -> Path:
    """A segment of two lines: ``XD``, which also has a file whose values differ, and ``YQ``,
    whose file is gone."""
    store = make_store(repo, fmt=1)
    (store / ".gitignore").write_text(GITIGNORE, encoding="utf-8")
    archived = decision_body("XD", status="rejected", valid_to="2026-02-01T00:00:00Z")
    # The file is the archive line's predecessor (the archive line is its lifecycle successor),
    # or a state no legal transition leads to the archive line from.
    file_status = "proposed" if archive_is_successor else "accepted"
    put_file(store, "decision", decision_body("XD", status=file_status))
    other = decision_body("YQ", status="rejected", valid_to="2026-02-01T00:00:00Z")
    put_archive(
        store,
        "seg",
        [{"record_type": "decision", **archived}, {"record_type": "decision", **other}],
    )
    commit_all(repo, "mixed", date=OLD)
    return store


@pytest.mark.parametrize("archive_is_successor", [False, True])
def test_different_values_pair_skipped_in_mixed_segment(git_repo, capsys, archive_is_successor):
    store = mixed_segment_store(git_repo, archive_is_successor)  # T-M4
    file_bytes = (store / "decisions" / "XD.json").read_bytes()
    archive_xd = json.loads((store / "archive" / "seg.jsonl").read_text().splitlines()[0])
    before = snapshot_as_format2(store)
    assert run(store) == 0
    out = capsys.readouterr().out
    assert "decision XD: duplicate-ulid" in out  # skipped and reported
    assert marker_of(store).major == 2  # a duplicate identity does not refuse the migration
    assert any(v.code == "duplicate-ulid" for v in verify_snapshot(store))  # verify keeps saying so
    assert (store / "decisions" / "XD.json").read_bytes() == file_bytes  # its file untouched
    shards = b"".join(shard_bytes(store).values())
    assert b'"id":"YQ"' in shards and b'"id":"XD"' not in shards  # the other record packed
    assert (store / "archive" / "seg.jsonl").exists()  # the segment stays for XD's line
    # the person decides which copy is the record. Deleting the segment keeps the file's copy
    segment = store / "archive" / "seg.jsonl"
    saved = segment.read_bytes()
    segment.unlink()
    assert survives(store, before)
    # deleting the file instead keeps the archive's copy: every record is still there
    segment.write_bytes(saved)
    (store / "decisions" / "XD.json").unlink()
    after = snapshot_as_format2(store)
    archived = {k: v for k, v in archive_xd.items() if k != "record_type"}
    assert after[("decision", "XD")] == archived  # the archive's copy is now the record
    assert {k: v for k, v in after.items() if k != ("decision", "XD")} == {
        k: v for k, v in before.items() if k != ("decision", "XD")
    }


# -- a major-1 (or marker-less) store that already holds shards ----------------------------------


def unmigrated_shards(repo: Path, shape: str, n: int = 16) -> Path:
    store = make_store(repo, fmt=2, n=n)
    for b in range(n):
        (store / "shards").mkdir(exist_ok=True)
        (store / "shards" / bucket_name(b)).write_bytes(b"")
    wrong = bucket_name((bucket_of("S1", n) + 1) % n)
    for rid in ["S1", "S2", "S3"]:
        if shape == "misplaced_sole" and rid == "S1":
            # the record's only shard line sits in the wrong bucket
            put_shard_line(store, "decision", rid, decision_body(rid), bucket=wrong)
        else:
            put_shard_line(store, "decision", rid, decision_body(rid))
    put_file(store, "decision", decision_body("P1"))  # a plain file with no shard line
    if shape == "numeric_strays":
        # numeric names beyond the recovered 16: strays, not buckets of a bigger store
        for b in range(16, 40):
            (store / "shards" / bucket_name(b)).write_bytes(b"")
        put_shard_line(store, "decision", "S4", decision_body("S4"), bucket=bucket_name(16))
    if shape == "partial_larger":
        for b in range(16, 63):  # a larger set that is missing its last file
            (store / "shards" / bucket_name(b)).write_bytes(b"")
        put_shard_line(store, "decision", "S4", decision_body("S4"), bucket=bucket_name(42))
    if shape == "lone_upper":
        (store / "shards" / bucket_name(10)).rename(store / "shards" / "0A.shard")
    if shape == "misplaced_and_strays":
        wrong = bucket_name((bucket_of("S1", n) + 1) % n)
        put_shard_line(store, "decision", "S1", decision_body("S1"), bucket=wrong)
        put_shard_line(store, "decision", "S4", decision_body("S4"), bucket="extra.shard")
    (store / ".gitignore").write_text(GITIGNORE, encoding="utf-8")
    marker = store / "format"
    if shape == "no_format_all_n":
        marker.unlink()
    else:
        marker.write_text("sidegraph-store 1.0.0\nrequires-sidegraph >= 0.16.0\n", "utf-8")
    commit_all(repo, shape, date=OLD)
    return store


@pytest.mark.parametrize(
    "shape",
    [
        "major1_all_n",
        "no_format_all_n",
        "misplaced_and_strays",
        "misplaced_sole",
        "numeric_strays",
        "partial_larger",
    ],
)
def test_unmigrated_shards_completed(git_repo, capsys, shape):  # Muse rev 8 finding 1
    store = unmigrated_shards(git_repo, shape)
    before = snapshot_as_format2(store)
    assert run(store) == 0, capsys.readouterr().out
    marker = marker_of(store)
    assert marker is not None and marker.major == 2 and marker.shards == 16  # N from the files
    assert survives(store, before)
    assert not (store / "decisions" / "P1.json").exists()  # the plain file was folded
    assert not (store / "shards" / "extra.shard").exists()  # the stray healed
    assert sorted(p.name for p in (store / "shards").glob("*.shard")) == names(16)
    inv = inventory_from_disk(store, validate=validator)
    assert all(
        o.kind != "shard" or bucket_name(bucket_of(o.record_id, 16)) == Path(o.path).name
        for o in inv.occurrences
    )  # every line sits in its own bucket
    assert any(o.kind == "shard" and o.record_id == "S1" for o in inv.occurrences)
    if shape in ("numeric_strays", "partial_larger"):
        assert any(o.kind == "shard" and o.record_id == "S4" for o in inv.occurrences)


def test_lone_upper_case_bucket_names_the_git_mv_remedy(git_repo, capsys):
    store = unmigrated_shards(git_repo, "lone_upper")
    before = tree_bytes(store)
    assert run(store) == 2
    out = capsys.readouterr().out
    assert "git mv" in out and "0A.shard" in out
    assert "restore the missing bucket files" not in out  # N=16 is recovered from lower case
    assert tree_bytes(store) == before and marker_of(store).major == 1


@pytest.mark.parametrize("shape", ["numeric_strays", "partial_larger"])
def test_dry_run_classifies_numeric_names_beyond_n_as_strays(git_repo, capsys, shape):
    store = unmigrated_shards(git_repo, shape)
    assert run(store, "--dry-run") == 0
    assert "bucket-out-of-range" not in capsys.readouterr().out


def test_incomplete_name_set_refuses(git_repo, capsys):  # red: infer N from the file count
    store = unmigrated_shards(git_repo, "major1_all_n")
    (store / "shards" / bucket_name(15)).unlink()
    (store / "shards" / "extra.shard").write_bytes(b"")  # 16 files, not a set
    commit_all(git_repo, "gap", date=OLD)
    before = tree_bytes(store)
    assert run(store) == 2
    assert "refused:" in capsys.readouterr().out
    assert tree_bytes(store) == before
    assert marker_of(store).major == 1


def test_case_collision_refused_before_marker_switch(git_repo, monkeypatch, capsys):
    store = format1_committed(git_repo)  # red: switch the marker before the case check
    rel = f"{store.name}/shards"
    monkeypatch.setattr(
        maintenance_clock,
        "git_ls_files_shards",
        lambda wt: [f"{rel}/0a.shard", f"{rel}/0A.shard"],
    )
    before_marker = (store / "format").read_text(encoding="utf-8")
    assert run(store) == 2
    out = capsys.readouterr().out
    assert (store / "format").read_text(encoding="utf-8") == before_marker
    assert "git mv" in out and "0A.shard" in out
    assert not (store / "shards").exists()


# -- outside git, resuming, dry run ----------------------------------------------------------------


def test_outside_git_branch_condition_does_not_apply(tmp_path, capsys):
    store = make_store(tmp_path, fmt=1)
    put_file(store, "decision", decision_body("D1"))
    (store / ".gitignore").write_text(GITIGNORE, encoding="utf-8")
    before = snapshot_as_format2(store)
    assert run(store) == 0, capsys.readouterr().out
    assert marker_of(store).major == 2 and not (store / "decisions" / "D1.json").exists()
    assert survives(store, before)
    assert run(store) == 2  # outside git, format 2 is refused: nothing to resume
    assert "sidegraph-compact --settled-days 0" in capsys.readouterr().out


def test_resume_only_when_head_marker_below_2(git_repo, capsys):
    store = format1_committed(git_repo)
    migrate(store, dry_run=False, any_branch=True)  # work tree: major 2; HEAD: below 2
    assert run(store) == 0  # an uncommitted migration resumes
    capsys.readouterr()
    commit_all(git_repo, "migrated", date=OLD)  # HEAD's marker is now major 2
    before = tree_bytes(store)
    assert run(store) == 2
    assert "sidegraph-compact --settled-days 0" in capsys.readouterr().out
    assert tree_bytes(store) == before


def test_dry_run_writes_nothing_preparation_included(git_repo, capsys):
    store = format1_committed(git_repo)
    before = tree_bytes(store)
    status = git(git_repo, "status", "--porcelain")
    assert run(store, "--dry-run") == 0
    out = capsys.readouterr().out
    assert "Dry run: nothing was written." in out
    assert "decisions/D1.json" in out and ".gitattributes" in out and "empty shard files" in out
    assert tree_bytes(store) == before and git(git_repo, "status", "--porcelain") == status
    assert not (store / "shards").exists() and not (store / ".gitattributes").exists()
    assert marker_of(store).major == 1  # the marker included


def test_dry_run_on_major1_with_shards_lists_folds(git_repo, capsys):
    store = unmigrated_shards(git_repo, "misplaced_and_strays")  # reads the shards via assume_major
    before = tree_bytes(store)
    assert run(store, "--dry-run") == 0
    out = capsys.readouterr().out
    assert "decisions/P1.json" in out  # the plain file would fold
    assert "shard lines would be moved" in out
    assert tree_bytes(store) == before and marker_of(store).major == 1


def no_gitignore_committed(repo: Path) -> Path:
    store = format1_committed(repo)
    (store / ".gitignore").unlink()
    commit_all(repo, "no gitignore", date=OLD)
    return store


@pytest.mark.parametrize("fixture", ["no_gitignore", "markerless", "markerless_shards"])
def test_dry_run_leaves_the_whole_tree_untouched(git_repo, capsys, fixture):
    store = {
        "no_gitignore": no_gitignore_committed,
        "markerless": markerless_committed,
        "markerless_shards": lambda repo: unmigrated_shards(repo, "no_format_all_n"),
    }[fixture](git_repo)
    before = tree_state(store)
    assert run(store, "--dry-run") == 0
    assert "Dry run: nothing was written." in capsys.readouterr().out
    assert tree_state(store) == before  # names, bytes and mtimes: no .gitignore, format, index.db
    assert not (store / "index.db").exists()


@pytest.mark.parametrize("count", [16, 64, 256])
def test_shards_flag_values(git_repo, count):
    store = format1_committed(git_repo)
    assert run(store, "--shards", str(count)) == 0
    marker = marker_of(store)
    assert marker is not None and marker.shards == count
    assert len(list((store / "shards").glob("*.shard"))) == count


def test_shards_flag_refuses_other_counts(git_repo, capsys):
    store = format1_committed(git_repo)
    with pytest.raises(SystemExit) as exc:
        run(store, "--shards", "32")
    assert exc.value.code == 2
    assert marker_of(store).major == 1


def test_default_is_64_shards(git_repo):
    store = format1_committed(git_repo)
    assert run(store) == 0
    marker = marker_of(store)
    assert marker is not None and marker.shards == 64


def test_busy_store_exits_1(git_repo, monkeypatch, capsys):
    store = format1_committed(git_repo)

    def busy(*args: object, **kwargs: object) -> Store:
        raise StoreBusyError("store writer reservation is busy")

    monkeypatch.setattr(migrate_module, "Store", busy)
    assert run(store) == 1
    assert "busy, try again" in capsys.readouterr().out
    assert marker_of(store).major == 1


def test_sqlite_locked_exits_1(git_repo, monkeypatch, capsys):
    store = format1_committed(git_repo)

    def locked(*args: object, **kwargs: object) -> Store:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(migrate_module, "Store", locked)
    assert run(store) == 1
    assert "busy, try again" in capsys.readouterr().out


def test_migration_refused_carries_reasons(git_repo):
    store = format1_committed(git_repo)
    git(git_repo, "switch", "-q", "-c", "topic")
    with pytest.raises(MigrationRefused) as exc:
        migrate(store, dry_run=False, any_branch=False)
    assert any("default branch" in r for r in exc.value.reasons)


def test_shards_other_than_16_64_256_raise(git_repo):
    store = format1_committed(git_repo)
    with pytest.raises(ValueError, match="shards must be one of"):
        migrate(store, shards=32, dry_run=True, any_branch=True)


def test_symlink_made_after_the_marker_stops_the_run_before_shard_publication(
    git_repo, monkeypatch
):
    store = format1_committed(git_repo)
    elsewhere = store.parent / "elsewhere"
    elsewhere.mkdir()
    real_published = migrate_module._publish_marker

    def link_then_publish(store_obj: Store, text: str, expect: bytes | None) -> None:
        (elsewhere / "x").write_text("x", encoding="utf-8")
        os.symlink(elsewhere, store / "archive")  # a link made after the entry checks
        real_published(store_obj, text, expect)

    monkeypatch.setattr(migrate_module, "_publish_marker", link_then_publish)
    report = migrate(store, dry_run=False, any_branch=True)
    assert report.stopped and "symlinked" in report.stopped
    assert marker_of(store).major == 2  # the marker was switched (and is resumable) ...
    assert not (store / "shards").exists()  # ... but no shard was published through the link


def test_unmigrated_opener_is_private(tmp_path):
    # the public opener has no such parameter by name: a marker-less store with shards refuses
    store = make_store(tmp_path, fmt=2, n=16)
    (store / "shards").mkdir()
    (store / "shards" / "00.shard").write_bytes(b"")
    (store / "format").unlink()
    with pytest.raises(Exception, match="sidegraph-migrate"):
        Store(store)
    assert not (store / "format").exists()  # refused, not backfilled
    Store(store, _allow_unmigrated_shards=True).close()  # the private opener accepts the shape
    assert not (store / "format").exists()  # and neither refuses nor backfills


# -- a committed store with no `format` file --------------------------------------------------


@pytest.mark.parametrize("fixture", ["no_format_no_shards", "no_format_with_shards"])
def test_markerless_store_migrates_and_is_never_backfilled(git_repo, capsys, fixture):
    store = (
        markerless_committed(git_repo)
        if fixture == "no_format_no_shards"
        else unmigrated_shards(git_repo, "no_format_all_n")
    )
    before = snapshot_as_format2(store)
    assert run(store) == 0, capsys.readouterr().out
    marker = marker_of(store)
    assert marker is not None and marker.major == 2
    assert marker.shards == (64 if fixture == "no_format_no_shards" else 16)
    assert survives(store, before)
    assert (store / "format").read_text(encoding="utf-8").startswith("sidegraph-store 2.0.0\n")
    # a second run resumes (HEAD has no marker) instead of refusing on uncommitted changes
    assert run(store) == 0


def test_opener_never_backfills_a_marker(tmp_path):
    store = make_store(tmp_path, fmt=1)
    put_file(store, "decision", decision_body("D1"))
    (store / "format").unlink()
    Store(store, _allow_unmigrated_shards=True).close()
    assert not (store / "format").exists()  # no records-only backfill either
    Store(store).close()
    assert (store / "format").exists()  # the public opener still backfills


def test_marker_changed_after_the_checks_stops_the_run(git_repo, monkeypatch, capsys):
    store = format1_committed(git_repo)
    newer = "sidegraph-store 1.0.0\nrequires-sidegraph >= 0.16.1\n"

    def stage(label: str) -> None:
        if label == "migrate:before_marker":
            (store / "format").write_text(newer, encoding="utf-8")

    monkeypatch.setattr(maintenance, "stage", stage)
    assert run(store, "--any-branch") == 2
    assert "changed under the run" in capsys.readouterr().out
    assert (store / "format").read_text(encoding="utf-8") == newer  # the other writer's marker
    assert not (store / "shards").exists()


# -- git flags that hide edits ---------------------------------------------------------------


@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
def test_tracked_edit_hidden_from_git_status_still_refuses(git_repo, capsys, flag):
    store = format1_committed(git_repo)
    rel = ".sidegraph/decisions/D1.json"
    git(git_repo, "update-index", flag, rel)
    put_file(store, "decision", decision_body("D1", title="edited behind git's back"))
    assert git(git_repo, "status", "--porcelain").strip() == ""  # git status sees nothing
    before = tree_bytes(store)
    assert run(store) == 2
    assert "decisions/D1.json" in capsys.readouterr().out
    assert tree_bytes(store) == before and marker_of(store).major == 1


def test_a_valid_ignored_index_is_not_an_uncommitted_change(git_repo, capsys):
    store = format1_committed(git_repo)
    Store(store).close()  # leaves the derived, gitignored index.db behind
    assert (store / "index.db").exists()
    assert run(store) == 0, capsys.readouterr().out


# -- a store that does not open --------------------------------------------------------------


def corrupt_index(store: Path, scratch: Path) -> None:
    (store / "index.db").write_bytes(b"this is not a database" * 50)


def real_index(store: Path, scratch: Path) -> None:
    """Give ``store`` an ``index.db`` built in a copy, so the store itself is opened by nothing."""
    copy = scratch / "index_copy"
    shutil.copytree(store, copy)
    Store(copy).close()
    shutil.copy(copy / "index.db", store / "index.db")


def bad_meta_index(store: Path, scratch: Path) -> None:
    """A ``meta`` table without its ``value`` column: ``CREATE TABLE IF NOT EXISTS`` passes,
    the freshness reads of the open do not."""
    real_index(store, scratch)
    conn = sqlite3.connect(store / "index.db")
    conn.execute("DROP TABLE meta")
    conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY)")
    conn.commit()
    conn.close()


def bad_schema_index(store: Path, scratch: Path) -> None:
    """A database whose tables the current schema cannot be laid over: the stamp is fine."""
    real_index(store, scratch)
    conn = sqlite3.connect(store / "index.db")
    conn.execute("DROP TABLE anchor_bindings")
    conn.execute("CREATE TABLE anchor_bindings(id TEXT PRIMARY KEY)")
    conn.commit()
    conn.close()


def unreadable_index(store: Path, scratch: Path) -> None:
    real_index(store, scratch)
    (store / "index.db").chmod(0)


@pytest.mark.parametrize("kind", ["corrupt", "bad_schema", "bad_meta", "unreadable"])
@pytest.mark.parametrize("dry", [False, True])
def test_unusable_index_is_a_refusal_that_writes_nothing(git_repo, tmp_path, capsys, kind, dry):
    if kind == "unreadable" and sys.platform == "win32":
        pytest.skip("Windows has no mode 000: chmod only toggles read-only")
    if kind == "unreadable" and os.geteuid() == 0:
        pytest.skip("root reads a mode 000 file")
    store = format1_committed(git_repo)
    (store / ".gitignore").unlink()  # opening would create it
    (store / ".git-ignored-index").write_text("", encoding="utf-8")
    exclude = git_repo / ".git" / "info" / "exclude"
    exclude.write_text("index.db*\n.git-ignored-index\n", encoding="utf-8")
    commit_all(git_repo, "no gitignore", date=OLD)
    {
        "corrupt": corrupt_index,
        "bad_schema": bad_schema_index,
        "bad_meta": bad_meta_index,
        "unreadable": unreadable_index,
    }[kind](store, tmp_path)
    before = tree_state(store)
    try:
        assert run(store, *(["--dry-run"] if dry else [])) == 2
        after = tree_state(store)
    finally:
        (store / "index.db").chmod(0o644)
    out = capsys.readouterr().out
    assert out.startswith("refused:") and "index.db" in out
    assert after == before  # no .gitignore, no format, nothing
    assert not (store / ".gitignore").exists()


# -- findings that do not stop the migration ---------------------------------------------------


def test_nonblocking_findings_are_listed_after_the_run(git_repo, capsys):
    store = format1_committed(git_repo)
    put_file(store, "decision", decision_body("D5", supersedes="NOSUCH"))
    commit_all(git_repo, "dangling", date=OLD)
    assert any(v.code == "dangling-supersedes" for v in verify_snapshot(store))
    assert run(store, "--dry-run") == 0
    dry = capsys.readouterr().out
    assert "carried over, not blocking" in dry and "dangling-supersedes" in dry
    assert run(store) == 0
    out = capsys.readouterr().out
    assert "carried over, not blocking" in out and "dangling-supersedes" in out


# -- the probe never opens the original index ----------------------------------------------------


def wal_index(store: Path, scratch: Path, *, leftover: bool) -> None:
    """A WAL-mode ``index.db``: closed cleanly (no ``-wal``/``-shm``), or with both files left
    behind by a process that did not close it."""
    real_index(store, scratch)
    conn = sqlite3.connect(store / "index.db")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute("CREATE TABLE IF NOT EXISTS probe(x)")
    conn.execute("INSERT INTO probe VALUES (1)")
    conn.commit()
    if leftover:
        for suffix in ("-wal", "-shm"):
            shutil.copy2(store / f"index.db{suffix}", scratch / f"kept{suffix}")
    conn.close()
    if leftover:
        for suffix in ("-wal", "-shm"):
            shutil.copy2(scratch / f"kept{suffix}", store / f"index.db{suffix}")
        assert (store / "index.db-wal").stat().st_size > 0
    else:
        for suffix in ("-wal", "-shm"):
            (store / f"index.db{suffix}").unlink(missing_ok=True)


def hot_journal_index(store: Path, scratch: Path) -> None:
    """An ``index.db`` with a hot rollback journal beside it."""
    real_index(store, scratch)
    conn = sqlite3.connect(store / "index.db", isolation_level=None)
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("PRAGMA cache_size=1")
    conn.execute("CREATE TABLE bulk(x BLOB)")
    conn.execute("BEGIN")
    for _ in range(300):
        conn.execute("INSERT INTO bulk VALUES (zeroblob(2000))")
    for suffix in ("", "-journal"):
        shutil.copy2(store / f"index.db{suffix}", scratch / f"hot{suffix}")
    conn.rollback()
    conn.close()
    shutil.copy2(scratch / "hot", store / "index.db")
    shutil.copy2(scratch / "hot-journal", store / "index.db-journal")
    assert (store / "index.db-journal").stat().st_size > 0


@pytest.mark.parametrize(
    ("kind", "entry"),
    [
        ("wal_closed", "library"),
        ("wal_leftover", "library"),
        ("hot_journal", "library"),
        ("wal_closed", "command"),
    ],
)
def test_dry_run_never_opens_the_original_index(
    git_repo, tmp_path, capsys, monkeypatch, kind, entry
):
    scratch_root = tmp_path / "scratch_root"
    scratch_root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch_root))
    store = format1_committed(git_repo)
    builders = {
        "wal_closed": lambda: wal_index(store, tmp_path, leftover=False),
        "wal_leftover": lambda: wal_index(store, tmp_path, leftover=True),
        "hot_journal": lambda: hot_journal_index(store, tmp_path),
    }
    builders[kind]()
    before = tree_state(store)
    if entry == "command":
        assert run(store, "--dry-run") == 0
        capsys.readouterr()
    else:
        assert not migrate(store, dry_run=True, any_branch=False).stopped
    assert tree_state(store) == before  # names, bytes and mtimes: no -wal, -shm or journal change
    assert list(scratch_root.iterdir()) == []  # the probe's temporary directory is gone


# -- an unknown index stamp is Store's business ----------------------------------------------------


def stamp_index(store: Path, *, stale: bool) -> None:
    conn = sqlite3.connect(store / "index.db")
    conn.execute("UPDATE meta SET value = '9.9.9' WHERE key = 'schema_version'")
    if stale:
        conn.execute("UPDATE meta SET value = 'stale' WHERE key = 'canonical_digest'")
    conn.commit()
    conn.close()


def gitignore_less_with_stamped_index(repo: Path, *, stale: bool) -> Path:
    """A committed store without ``.gitignore`` whose gitignored index carries the unknown stamp
    ``9.9.9``, with the digest of the store as it is (``stale=False``) or a stale one."""
    store = format1_committed(repo)
    Store(store).close()  # builds the index while the store still has its .gitignore
    stamp_index(store, stale=stale)
    (store / ".gitignore").unlink()
    (repo / ".git" / "info" / "exclude").write_text("index.db*\n", encoding="utf-8")
    commit_all(repo, "no gitignore", date=OLD)
    return store


@pytest.mark.parametrize("dry", [False, True])
def test_unknown_stamp_with_a_matching_digest_is_refused_before_anything_is_written(
    git_repo, capsys, dry
):
    store = gitignore_less_with_stamped_index(git_repo, stale=False)
    before = tree_state(store)
    assert run(store, *(["--dry-run"] if dry else [])) == 2
    out = capsys.readouterr().out
    assert out.startswith("refused:") and "'9.9.9'" in out and "index.db" in out
    assert tree_state(store) == before  # no .gitignore, no initiatives/, no journal, no marker
    assert marker_of(store).major == 1


@pytest.mark.parametrize("dry", [False, True])
def test_unknown_stamp_with_a_stale_digest_is_rebuilt_not_refused(git_repo, capsys, dry):
    store = gitignore_less_with_stamped_index(git_repo, stale=True)
    before = tree_state(store)
    assert run(store, *(["--dry-run"] if dry else [])) == 0, capsys.readouterr().out
    if dry:
        assert tree_state(store) == before
    else:
        assert index_stamp_of(store) == "0.7.0+format2"  # the open rebuilt it


def test_the_open_and_the_probe_share_one_freshness_decision(git_repo):
    from sidegraph import store as store_module

    store = gitignore_less_with_stamped_index(git_repo, stale=False)
    stored, current = None, None
    conn = sqlite3.connect(store / "index.db")
    stored = conn.execute("SELECT value FROM meta WHERE key = 'canonical_digest'").fetchone()[0]
    conn.close()
    current, _ = store_module.canonical_digest(store)
    assert stored == current  # the digest of a store needs no Store, and equals Store's own
    assert store_module.index_freshness(stored, current, "9.9.9", "0.6.0+requires") == "refuse"
    assert store_module.index_freshness(stored, "other", "9.9.9", "0.6.0+requires") == "reload"
    assert store_module.index_freshness(stored, current, "0.6.0", "0.6.0+requires") == "reload"
    assert store_module.index_freshness(stored, current, "0.6.0+requires", "0.6.0+requires") == (
        "fresh"
    )
    # and the open itself takes the same verdict from it
    with pytest.raises(ValueError, match="9.9.9"):
        Store(store)


@pytest.mark.parametrize("dry", [False, True])
def test_a_resume_probes_the_index_too(git_repo, capsys, dry):
    store = format1_committed(git_repo)
    migrate(store, dry_run=False, any_branch=True)  # marker switched, uncommitted
    stamp_index(store, stale=False)
    before = tree_state(store)
    assert run(store, *(["--dry-run"] if dry else [])) == 2
    assert "'9.9.9'" in capsys.readouterr().out
    assert tree_state(store) == before


# -- the requirement holds on every path ----------------------------------------------------------


@pytest.mark.parametrize("dry", [False, True])
def test_unmet_requirement_refuses_a_resume(git_repo, capsys, dry):
    from sidegraph.store_format import marker_text

    store = format1_committed(git_repo)
    (store / "format").write_text(marker_text(2, "99.0.0", 64), encoding="utf-8")
    before = tree_state(store)  # work tree: major 2 requiring 99.0.0; HEAD: format 1
    assert run(store, *(["--dry-run"] if dry else [])) == 2
    out = capsys.readouterr().out
    assert out.startswith("refused:") and "99.0.0" in out
    assert tree_state(store) == before


# -- the dry run lists what the run writes ------------------------------


@pytest.mark.parametrize(
    "fixture", ["plain", "no_gitignore", "markerless", "markerless_shards", "mixed"]
)
def test_dry_run_lists_exactly_the_files_the_real_run_writes(git_repo, fixture):
    store = {
        "plain": format1_committed,
        "no_gitignore": no_gitignore_committed,
        "markerless": markerless_committed,
        "markerless_shards": lambda repo: unmigrated_shards(repo, "no_format_all_n"),
        "mixed": lambda repo: mixed_segment_store(repo, False),
    }[fixture](git_repo)
    dry = migrate(store, dry_run=True, any_branch=False)
    real = migrate(store, dry_run=False, any_branch=False)
    assert not dry.stopped and not real.stopped
    assert set(dry.wrote) == set(real.wrote)
    assert dry.wrote[0] == "format" == real.wrote[0]


def test_a_stopped_dry_run_does_not_claim_the_marker(git_repo, monkeypatch, capsys):
    store = format1_committed(git_repo)
    elsewhere = store.parent / "elsewhere"
    elsewhere.mkdir()

    def stage(label: str) -> None:
        if label == "after_step2":
            os.symlink(elsewhere, store / "archive")

    monkeypatch.setattr(maintenance, "stage", stage)
    report = migrate(store, dry_run=True, any_branch=False)
    assert report.stopped and "symlinked" in report.stopped
    assert "format" not in report.wrote
    assert marker_of(store).major == 1


# -- I/O errors are exit 1, not refusals ----------------------------


@pytest.mark.parametrize("path", ["fresh", "resume"])
def test_an_io_error_at_open_is_exit_1_with_the_resume_hint_when_switched(
    git_repo, monkeypatch, capsys, path
):
    store = format1_committed(git_repo)
    if path == "resume":
        migrate(store, dry_run=False, any_branch=True)  # the marker is switched, uncommitted

    def full_disk(*args: object, **kwargs: object) -> Store:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(migrate_module, "Store", full_disk)
    assert run(store) == 1
    out = capsys.readouterr().out
    assert "cannot migrate" in out and "No space left" in out
    assert ("already switched" in out) == (path == "resume")


# -- a full scratch space is the machine's, not the index's ----------------


@pytest.mark.parametrize("dry", [False, True])
def test_a_full_disk_while_copying_the_index_is_exit_1_not_a_refusal(
    git_repo, tmp_path, monkeypatch, capsys, dry
):
    store = format1_committed(git_repo)
    real_index(store, tmp_path)

    def full(out: object, chunk: bytes) -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(migrate_module, "_put_scratch", full)
    before = tree_state(store)
    assert run(store, *(["--dry-run"] if dry else [])) == 1
    out = capsys.readouterr().out
    assert "cannot migrate" in out and "No space left" in out and "delete it" not in out
    assert tree_state(store) == before


def test_a_full_disk_inside_the_probe_database_is_exit_1_not_a_refusal(
    git_repo, tmp_path, monkeypatch, capsys
):
    store = format1_committed(git_repo)
    real_index(store, tmp_path)
    error = sqlite3.OperationalError("database or disk is full")
    error.sqlite_errorcode = sqlite3.SQLITE_FULL  # type: ignore[attr-defined]

    class FullConnection:
        def execute(self, *args: object) -> None:
            return None

        def executescript(self, *args: object) -> None:
            raise error

        def close(self) -> None:
            return None

    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: FullConnection())
    assert run(store, "--dry-run") == 1
    out = capsys.readouterr().out
    assert "disk is full" in out and "delete it" not in out


def test_an_unreadable_source_is_still_a_refusal(git_repo, tmp_path, monkeypatch, capsys):
    store = format1_committed(git_repo)
    real_index(store, tmp_path)
    real_open = Path.open

    def deny(self: Path, *args: object, **kwargs: object):
        if self.name == "index.db":
            raise PermissionError(errno.EACCES, "Permission denied", str(self))
        return real_open(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "open", deny)
    assert run(store, "--dry-run") == 2
    assert "cannot be read" in capsys.readouterr().out
