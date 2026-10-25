"""A directory (or FIFO) named ``*.json`` in a record directory costs only that entry.

Every record directory is read with ``glob("*.json")``, which matches directories too, and the
reload read each match: ``IsADirectoryError`` aborted the open of the whole store, and a FIFO
blocked it for ever. A non-regular entry is not a file at all, so the reload skips it BEFORE any
read, lists it in ``skipped_canonical_files`` and carries on; an ``OSError`` on the read of a
real file still propagates (that is operational, not a bad file).
see design/superpowers/specs/2026-10-03-store-survives-a-bad-file-design.md (D1, D2)
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

import sidegraph.store as store_module
from sidegraph import verify
from sidegraph.schema import Decision, DecisionKind
from sidegraph.store import Store
from tests.store_format2_support import format1_store
from tests.store_non_file_support import (
    NOT_A_FILE,
    SAFE,
    SEGMENT,
    UNSAFE_NAME,
    _prov,
    _seed,
    _Timeout,
    needs_fifo,
)

SKIP_KEY = "skipped_canonical_files"
# first, middle and last of the inventory's one record-dir loop, plus initiatives (its own loop)
RECORD_DIRS = ["entities", "facts", "bindings", "initiatives"]


def _skipped(s: Store) -> dict[str, str]:
    row = s._conn.execute("SELECT value FROM meta WHERE key = ?", (SKIP_KEY,)).fetchone()
    return {e["path"]: e["reason"] for e in json.loads(row["value"])} if row else {}


def _plant(path: Path, kind: str) -> None:
    path.parent.mkdir(exist_ok=True)
    if kind == "dir":
        path.mkdir()
    else:
        os.mkfifo(path)


# -- the reload skips a non-file entry in every record directory --------------------------


@pytest.mark.parametrize("name,reason", [(f"{SAFE}.json", NOT_A_FILE), (".x.json", UNSAFE_NAME)])
@pytest.mark.parametrize("subdir", RECORD_DIRS)
def test_a_directory_in_a_record_dir_is_skipped_not_raised(tmp_path, subdir, name, reason):
    db, decision = _seed(tmp_path)
    (db / subdir / name).mkdir()

    with Store(db) as s:
        assert _skipped(s) == {f"{subdir}/{name}": reason}
        assert s.get_decision(decision.id) is not None  # the other records load


@pytest.mark.parametrize("name", [f"{SAFE}.json", ".x.json"])
def test_a_directory_in_the_archive_dir_is_skipped_not_raised(tmp_path, name):
    db, decision = _seed(tmp_path)
    (db / "archive").mkdir(exist_ok=True)
    (db / "archive" / (name + "l")).mkdir()
    (db / "archive" / SEGMENT).mkdir()

    with Store(db) as s:
        skipped = _skipped(s)
        # a segment name is not an id: the reason is the same whatever the name
        assert skipped[f"archive/{name}l"] == NOT_A_FILE
        assert skipped[f"archive/{SEGMENT}"] == NOT_A_FILE
        assert s.get_decision(decision.id) is not None


@needs_fifo
@pytest.mark.parametrize("subdir", ["decisions", "bindings", "entities", "archive"])
def test_a_fifo_in_a_record_dir_does_not_block_the_open(tmp_path, subdir):
    db, decision = _seed(tmp_path)
    name = f"{SAFE}.jsonl" if subdir == "archive" else f"{SAFE}.json"
    _plant(db / subdir / name, "fifo")

    with _Timeout(), Store(db) as s:
        assert _skipped(s) == {f"{subdir}/{name}": NOT_A_FILE}
        assert s.get_decision(decision.id) is not None


def test_a_symlink_to_a_directory_is_skipped(tmp_path):
    db, _ = _seed(tmp_path)
    (db / "facts" / f"{SAFE}.json").symlink_to(tmp_path)  # stat follows the link: a directory

    with Store(db) as s:
        assert _skipped(s) == {f"facts/{SAFE}.json": NOT_A_FILE}


# -- format 1 (per-record reload loops) and a symlink cycle ------------------------------


@pytest.mark.parametrize("subdir", ["entities", "bindings"])
def test_a_format1_store_skips_a_directory_in_a_record_dir(tmp_path, subdir):
    """The format-1 reload walks its record dirs with its own loops, apart from the inventory
    the default layout uses; each must skip a non-file entry and still load a real record."""
    db = format1_store(tmp_path / ".sidegraph")
    with Store(db) as s:
        decision = s.add_decision(
            Decision(
                title="an adr",
                kind=DecisionKind.ADR,
                context="c",
                choice="ch",
                valid_from=datetime.now(UTC),
                provenance=_prov(),
            )
        )
    (db / subdir / f"{SAFE}.json").mkdir()

    with Store(db) as s:
        assert _skipped(s) == {f"{subdir}/{SAFE}.json": NOT_A_FILE}
        assert s.get_decision(decision.id) is not None


def test_a_symlink_cycle_in_a_record_dir_is_skipped_not_raised(tmp_path):
    """``stat`` of a link in a cycle raises ELOOP, not FileNotFoundError. A committed
    ``a -> b -> a`` is checked out verbatim by git, so it can reach a user."""
    db, decision = _seed(tmp_path)
    a = db / "decisions" / f"{SAFE}.json"
    b = db / "decisions" / "b-link"
    a.symlink_to(b)
    b.symlink_to(a)

    with Store(db) as s:
        assert _skipped(s) == {f"decisions/{SAFE}.json": NOT_A_FILE}
        assert s.get_decision(decision.id) is not None


# -- the skip is certified: a reopen takes the fast path ----------------------------------


@pytest.mark.parametrize("name", [f"{SAFE}.json", ".x.json"])
def test_a_skipped_directory_does_not_reload_on_every_open(tmp_path, monkeypatch, name):
    db, _ = _seed(tmp_path)
    (db / "decisions" / name).mkdir()
    with Store(db):
        pass
    reloads = []
    orig = Store._reload_index_from_canonical
    monkeypatch.setattr(
        Store,
        "_reload_index_from_canonical",
        lambda self, digest: (reloads.append(1), orig(self, digest))[1],
    )

    with Store(db) as s:
        assert _skipped(s) == {
            f"decisions/{name}": NOT_A_FILE if name != ".x.json" else UNSAFE_NAME
        }
    with Store(db):
        pass

    assert reloads == []


def test_the_skip_is_dropped_once_the_entry_is_removed(tmp_path):
    db, _ = _seed(tmp_path)
    entry = db / "decisions" / f"{SAFE}.json"
    entry.mkdir()
    with Store(db) as s:
        assert _skipped(s)
    entry.rmdir()
    with Store(db) as s:
        assert _skipped(s) == {}


# -- the skip reason names an unsafe filename ----------------------------------------------


def test_the_reason_for_an_unsafe_filename_names_the_filename():
    problem = store_module._record_identity_problem({"id": "A"}, "id", ".x")
    assert problem is not None and "filename" in problem and "does not match" not in problem


def test_an_unsafe_named_copy_of_a_record_is_skipped_for_its_filename(tmp_path):
    db, decision = _seed(tmp_path)
    source = db / "decisions" / f"{decision.id}.json"
    (db / "decisions" / ".copy.json").write_text(source.read_text(encoding="utf-8"), "utf-8")

    with Store(db) as s:
        assert _skipped(s) == {"decisions/.copy.json": UNSAFE_NAME}


# -- verify reports the entry once, and neither crashes nor hangs ---------------------------


def _verify(db: Path) -> list[tuple[str, str]]:
    return [(v.code, Path(v.path).name) for v in verify.verify_snapshot(db)]


@pytest.mark.parametrize("subdir", RECORD_DIRS)
def test_verify_reports_a_directory_with_an_unsafe_name_once_as_unsafe(tmp_path, subdir):
    db, _ = _seed(tmp_path)
    (db / subdir / ".x.json").mkdir()
    assert _verify(db) == [(verify.UNSAFE_RECORD_ID, ".x.json")]


@pytest.mark.parametrize("subdir", RECORD_DIRS)
def test_verify_reports_a_safe_named_directory_as_a_parse_error(tmp_path, subdir):
    db, _ = _seed(tmp_path)
    (db / subdir / f"{SAFE}.json").mkdir()
    found = [v for v in verify.verify_snapshot(db) if v.path.endswith(f"{SAFE}.json")]
    assert [(v.code, v.detail) for v in found] == [
        (verify.PARSE_ERROR, f"unreadable: {NOT_A_FILE}")
    ]


def test_verify_reports_a_directory_in_the_archive_as_a_bad_segment(tmp_path):
    db, _ = _seed(tmp_path)
    (db / "archive").mkdir(exist_ok=True)
    (db / "archive" / SEGMENT).mkdir()
    found = verify.verify_snapshot(db)
    assert [(v.code, v.detail) for v in found] == [
        (verify.BAD_ARCHIVE_SEGMENT, f"unreadable: {NOT_A_FILE}")
    ]


@needs_fifo
@pytest.mark.parametrize("subdir", ["decisions", "bindings", "archive"])
def test_verify_does_not_block_on_a_fifo(tmp_path, subdir):
    db, _ = _seed(tmp_path)
    name = f"{SAFE}.jsonl" if subdir == "archive" else f"{SAFE}.json"
    _plant(db / subdir / name, "fifo")
    with _Timeout():
        found = [v for v in verify.verify_snapshot(db) if v.path.endswith(name)]
    assert len(found) == 1


# -- the stale-tmp sweep globs ``*.tmp`` and must not unlink a directory ------------------------


def test_a_directory_named_like_tmp_debris_does_not_fail_the_open(tmp_path):
    db, decision = _seed(tmp_path)
    stale = db / "decisions" / "old.tmp"
    stale.mkdir()
    os.utime(stale, (1_000_000_000, 1_000_000_000))

    with Store(db) as s:
        assert s.get_decision(decision.id) is not None
    assert stale.is_dir()


# -- the skip's stat row is recorded: a write between two opens does not force a reload -----


def _count_reloads(monkeypatch) -> list[int]:
    reloads: list[int] = []
    orig = Store._reload_index_from_canonical
    monkeypatch.setattr(
        Store,
        "_reload_index_from_canonical",
        lambda self, digest: (reloads.append(1), orig(self, digest))[1],
    )
    return reloads


@pytest.mark.parametrize("subdir", ["decisions", "bindings"])
def test_a_write_after_a_skipped_directory_does_not_force_a_reload(tmp_path, monkeypatch, subdir):
    """The stat row of a skipped entry is what lets ``_touch_digest`` certify the next write:
    without it the digest is refused and every later open reloads."""
    db, _ = _seed(tmp_path)
    (db / subdir / f"{SAFE}.json").mkdir()
    with Store(db) as s:
        s.add_decision(
            Decision(
                title="later",
                kind=DecisionKind.ADR,
                context="c",
                choice="x",
                valid_from=datetime.now(UTC),
                provenance=_prov(),
            )
        )
    reloads = _count_reloads(monkeypatch)

    with Store(db):
        pass

    assert reloads == []


# -- a dangling symlink named ``*.json`` is a non-file entry too ---------------------------


@pytest.mark.parametrize("subdir", RECORD_DIRS)
@pytest.mark.parametrize("name,reason", [(f"{SAFE}.json", NOT_A_FILE), (".x.json", UNSAFE_NAME)])
def test_a_dangling_symlink_in_a_record_dir_is_skipped_not_raised(tmp_path, subdir, name, reason):
    db, decision = _seed(tmp_path)
    (db / subdir / name).symlink_to(tmp_path / "no-such-target")

    with Store(db) as s:
        assert _skipped(s) == {f"{subdir}/{name}": reason}
        assert s.get_decision(decision.id) is not None


@pytest.mark.parametrize("name", [f"{SAFE}.jsonl", ".x.jsonl"])
def test_a_dangling_symlink_in_the_archive_dir_is_skipped_not_raised(tmp_path, name):
    db, decision = _seed(tmp_path)
    (db / "archive").mkdir(exist_ok=True)
    (db / "archive" / name).symlink_to(tmp_path / "no-such-target")

    with Store(db) as s:
        assert _skipped(s) == {f"archive/{name}": NOT_A_FILE}
        assert s.get_decision(decision.id) is not None


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores modes")
def test_stat_entry_only_describes_a_dangling_or_looping_link_not_any_oserror(tmp_path):
    """A symlink into a directory the process cannot search is a permission problem, not a bad
    entry: ``stat_entry`` must raise it, not hide it behind ``lstat``."""
    from sidegraph.store_layout import stat_entry

    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "target").write_text("{}")
    link = tmp_path / f"{SAFE}.json"
    link.symlink_to(locked / "target")
    locked.chmod(0o000)
    try:
        try:
            link.stat()
        except PermissionError:
            pass
        else:
            pytest.skip("chmod has no effect here")
        with pytest.raises(PermissionError):
            stat_entry(link)
    finally:
        locked.chmod(0o755)


def test_a_dangling_symlink_does_not_reload_on_every_open(tmp_path, monkeypatch):
    db, _ = _seed(tmp_path)
    (db / "decisions" / f"{SAFE}.json").symlink_to(tmp_path / "no-such-target")
    with Store(db):
        pass
    reloads = _count_reloads(monkeypatch)

    with Store(db) as s:
        assert _skipped(s) == {f"decisions/{SAFE}.json": NOT_A_FILE}
    with Store(db):
        pass

    assert reloads == []


def test_an_unreadable_regular_file_still_stops_the_open(tmp_path):
    """Only a non-file entry is skipped: an ``OSError`` on a real file propagates."""
    db, decision = _seed(tmp_path)
    path = db / "decisions" / f"{decision.id}.json"
    (db / "index.db").unlink()
    path.chmod(0o000)
    try:
        try:
            path.read_bytes()
        except PermissionError:
            pass
        else:
            pytest.skip("chmod has no effect here")
        with pytest.raises(PermissionError):
            Store(db)
    finally:
        path.chmod(0o644)


def test_verify_reports_a_dangling_symlink_without_a_traceback(tmp_path):
    db, _ = _seed(tmp_path)
    (db / "decisions" / f"{SAFE}.json").symlink_to(tmp_path / "no-such-target")
    (db / "archive").mkdir(exist_ok=True)
    (db / "archive" / SEGMENT).symlink_to(tmp_path / "no-such-target")

    found = {(v.code, v.detail) for v in verify.verify_snapshot(db)}

    assert found == {
        (verify.PARSE_ERROR, f"unreadable: {NOT_A_FILE}"),
        (verify.BAD_ARCHIVE_SEGMENT, f"unreadable: {NOT_A_FILE}"),
    }


# -- ``sidegraph-verify --against`` reads the working-tree file of a tracked path ----------
