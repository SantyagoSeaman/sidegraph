"""Marker majors, index stamps and the shards-under-major-1 refusal of the packed store.

# see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§5.5, §7.1, §7.2; T-F3)
"""

from __future__ import annotations

import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sidegraph import doctor, store_layout
from sidegraph.cli import blame_main, doctor_main
from sidegraph.doctor import open_curation_index
from sidegraph.gitio import open_index_ro
from sidegraph.hot_index import HotIndex
from sidegraph.schema import (
    AnchorBinding,
    Decision,
    DecisionKind,
    DecisionStatus,
    Descriptor,
    Entity,
    Provenance,
)
from sidegraph.stats.model import build_report
from sidegraph.store import Store
from sidegraph.store_format import FORMAT2_INDEX_STAMP, marker_text
from sidegraph.store_layout import (
    INDEX_STAMPS,
    StoreRequirementError,
    accepted_index_stamps,
    check_store_requirement,
    index_stamp,
    parse_format_marker,
    requirement_marker_text,
    store_marker,
    symlinked_internals,
)
from sidegraph.verify import UNKNOWN_SCHEMA_VERSION, verify_snapshot
from tests.store_format2_support import (
    _open_and_close_store,
    codes,
    entity_body,
    format1_store,
    git,
    git_repo,  # noqa: F401  (fixture)
    index_stamp_of,
    make_store,
    put_file,
)


def _decision(title: str, **over: object) -> Decision:
    return Decision(
        title=title,
        kind=DecisionKind.ADR,
        status=DecisionStatus.ACCEPTED,
        context="c",
        choice="ch",
        valid_from=datetime.now(UTC),
        provenance=Provenance(source="manual"),
        **over,
    )


F2 = "sidegraph-store 2.0.0\nrequires-sidegraph >= 0.17.0\nshards 64\n"


def _plant_shard(store: Path, name: str = "0a.shard", data: bytes = b"") -> Path:
    (store / "shards").mkdir(exist_ok=True)
    path = store / "shards" / name
    path.write_bytes(data)
    return path


def _restamp(store: Path, stamp: str) -> None:
    conn = sqlite3.connect(store / "index.db")
    try:
        conn.execute("UPDATE meta SET value = ? WHERE key = 'schema_version'", (stamp,))
        conn.commit()
    finally:
        conn.close()


# -- marker grammar --------------------------------------------------------------------------


def test_marker_major2_parses_exactly_two_lines_after_the_first(tmp_path):
    store = make_store(tmp_path)
    found = store_marker(store)
    assert (found.major, found.requirement, found.shards) == (2, "0.17.0", 64)
    assert parse_format_marker(F2, store / "format") == ("2.0.0", "0.17.0")
    for n in (16, 64, 256):
        text = marker_text(2, "0.17.0", n)
        (store / "format").write_text(text, encoding="utf-8")
        assert store_marker(store).shards == n


@pytest.mark.parametrize(
    "text",
    [
        # the major-2 grammar's own malformed texts are cases of test_store_format.py
        "sidegraph-store 2.1.0\nrequires-sidegraph >= 0.17.0\nshards 64\n",
        "sidegraph-store 1.0.0\nrequires-sidegraph >= 0.17.0\nshards 64\n",
    ],
)
def test_marker_malformed_variants_are_refused_as_malformed(tmp_path, text):
    store = make_store(tmp_path)
    (store / "format").write_text(text, encoding="utf-8")
    with pytest.raises(StoreRequirementError) as raised:
        store_marker(store)
    assert raised.value.kind == "malformed"


def test_an_unknown_major_keeps_its_refusal_kind_and_verifys_code(tmp_path):
    """Handoff: the major-2 grammar lives in ``store_format``, but the first-line and
    unknown-major judgements stay in ``store_layout`` so ``kind`` and verify's mapping hold."""
    store = make_store(tmp_path)
    (store / "format").write_text("sidegraph-store 99.0\n", encoding="utf-8")
    with pytest.raises(StoreRequirementError) as raised:
        store_marker(store)
    assert raised.value.kind == "major"
    (store / "format").write_text("version 2\n", encoding="utf-8")
    with pytest.raises(StoreRequirementError) as raised:
        store_marker(store)
    assert raised.value.kind == "unrecognized"
    (store / "format").write_text("sidegraph-store 99.0\n", encoding="utf-8")
    assert codes(verify_snapshot(store)) == {UNKNOWN_SCHEMA_VERSION}


def test_requirement_marker_text_keeps_major2_and_shards():
    keep = store_layout.parse_store_marker(F2, Path("format"))
    assert requirement_marker_text("0.18.0", keep=keep) == (
        "sidegraph-store 2.0.0\nrequires-sidegraph >= 0.18.0\nshards 64\n"
    )
    other = store_layout.parse_store_marker(marker_text(2, "0.17.0", 256), Path("format"))
    assert requirement_marker_text("0.18.0", keep=other).endswith("shards 256\n")
    assert requirement_marker_text("0.18.0") == (
        "sidegraph-store 1.0.0\nrequires-sidegraph >= 0.18.0\n"
    )
    major0 = store_layout.parse_store_marker("sidegraph-store 0.6.0\n", Path("format"))
    assert requirement_marker_text("0.18.0", keep=major0).startswith("sidegraph-store 1.0.0")


def test_index_stamp_follows_the_major():
    assert index_stamp("0.17.0", 2) == FORMAT2_INDEX_STAMP == "0.7.0+format2"
    assert index_stamp(None) == "0.6.0"
    assert index_stamp("0.16.0") == "0.6.0+requires"
    assert index_stamp("0.16.0", 1) == "0.6.0+requires"


def test_accepted_index_stamps_by_marker_major(tmp_path):
    assert accepted_index_stamps(make_store(tmp_path / "a")) == {FORMAT2_INDEX_STAMP}
    assert accepted_index_stamps(make_store(tmp_path / "b", fmt=1)) == INDEX_STAMPS
    assert accepted_index_stamps(tmp_path / "nowhere") == INDEX_STAMPS


# -- shards under a marker below major 2 ------------------------------------------------------


def test_lone_case_variant_shard_under_major1_refused_cold_and_held_open(tmp_path):  # T-F3
    store = make_store(tmp_path, fmt=1)
    s = Store(store)
    (store / "shards").mkdir()
    (store / "shards" / "0A.SHARD").write_bytes(b"")
    with pytest.raises(StoreRequirementError, match="sidegraph-migrate"):
        s.refresh_if_stale()  # held open: red for "marker signature without shard presence"
    with pytest.raises(StoreRequirementError, match="sidegraph-migrate"):
        Store(store)  # cold
    s.close()


@pytest.mark.parametrize("opener", ["store", "hot_index", "stats", "blame", "doctor"])
def test_shards_under_major1_refused_everywhere(tmp_path, opener, capsys):
    store = make_store(tmp_path, fmt=1)
    Store(store).close()  # a real index, stamped for format 1, so only the shard check refuses
    _plant_shard(store)
    if opener == "store":
        with pytest.raises(StoreRequirementError, match="sidegraph-migrate"):
            Store(store)
    elif opener == "hot_index":
        assert HotIndex.open(store) is None
    elif opener == "stats":
        with pytest.raises(StoreRequirementError, match="sidegraph-migrate"):
            build_report(store, None, window_days=7)
    elif opener == "blame":
        assert open_index_ro(store) is None
    else:
        assert doctor_main(["--db", str(store)]) == 2
        out = capsys.readouterr().out
        assert "shards-without-format2" in out and "remaining checks skipped" in out


def test_shards_with_no_marker_are_refused_too(tmp_path):
    store = tmp_path / ".sidegraph"
    store.mkdir()
    _plant_shard(store)
    with pytest.raises(StoreRequirementError, match="sidegraph-migrate"):
        check_store_requirement(store)
    assert check_store_requirement(store, allow_unmigrated_shards=True) is None


def test_major2_with_shard_files_is_not_refused(tmp_path):
    """Shards are served when the marker's major is 2, and only then."""
    store = make_store(tmp_path)
    _plant_shard(store)
    assert check_store_requirement(store) == "0.17.0"
    Store(store).close()


def test_the_migrate_caller_can_skip_the_shards_refusal(tmp_path):
    store = make_store(tmp_path, fmt=1)
    _plant_shard(store)
    with pytest.raises(StoreRequirementError):
        check_store_requirement(store)
    assert check_store_requirement(store, allow_unmigrated_shards=True) == "0.16.0"


def test_empty_shards_dir_or_tmp_only_is_not_packed(tmp_path):
    store = make_store(tmp_path, fmt=1)
    (store / "shards").mkdir()
    assert check_store_requirement(store) == "0.16.0"
    (store / "shards" / "0a.shard.123.abc.tmp").write_bytes(b"x")
    (store / "shards" / "notes.txt").write_bytes(b"x")
    assert check_store_requirement(store) == "0.16.0"
    Store(store).close()
    assert codes(verify_snapshot(store)) == set()


def test_an_unlistable_shards_dir_is_an_error_not_empty(tmp_path):
    """Handoff: a PermissionError is not "no shards"; the refusal must not swallow it."""
    if not hasattr(os, "geteuid") or os.geteuid() == 0:
        pytest.skip("needs POSIX permissions; root bypasses them")
    store = make_store(tmp_path, fmt=1)
    (store / "shards").mkdir()
    (store / "shards").chmod(0)
    try:
        with pytest.raises(PermissionError):
            check_store_requirement(store)
    finally:
        (store / "shards").chmod(0o755)


def test_a_symlinked_shard_file_is_a_symlinked_internal_even_though_listing_skips_it(tmp_path):
    store = make_store(tmp_path)
    target = tmp_path / "elsewhere.shard"
    target.write_bytes(b"")
    (store / "shards").mkdir()
    (store / "shards" / "0a.shard").symlink_to(target)
    assert symlinked_internals(store) == ["shards/0a.shard"]
    assert "symlinked-store-entry" in codes(verify_snapshot(store))
    with pytest.raises(ValueError, match="symlink"):
        Store(store)


def test_symlinked_shards_dir_refused(tmp_path):  # Review Focus 3
    store = make_store(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (store / "shards").symlink_to(elsewhere, target_is_directory=True)
    assert symlinked_internals(store) == ["shards"]
    assert "symlinked-store-entry" in codes(verify_snapshot(store))
    with pytest.raises(ValueError, match="symlink"):
        Store(store)


def test_a_gitattributes_symlink_is_a_symlinked_internal(tmp_path):
    store = make_store(tmp_path)
    (tmp_path / "attrs").write_text("*.shard -text\n", encoding="utf-8")
    (store / ".gitattributes").symlink_to(tmp_path / "attrs")
    assert symlinked_internals(store) == [".gitattributes"]


def test_verify_reports_shards_without_format2(tmp_path):
    store = make_store(tmp_path, fmt=1)
    Store(store).close()
    _plant_shard(store, "0A.SHARD")
    assert codes(verify_snapshot(store)) == {"shards-without-format2"}


def test_verify_accepts_a_format2_marker_and_a_fresh_store(tmp_path):
    store = make_store(tmp_path)
    assert codes(verify_snapshot(store)) == set()
    Store(tmp_path / "fresh").close()
    assert codes(verify_snapshot(tmp_path / "fresh")) == set()


# -- writers: fresh store, backfill, raise_requirement ------------------------------------------


def test_fresh_store_is_format2_without_shards_dir(tmp_path):
    Store(tmp_path / "s").close()
    assert (tmp_path / "s" / "format").read_text() == F2
    assert not (tmp_path / "s" / "shards").exists()
    assert index_stamp_of(tmp_path / "s") == FORMAT2_INDEX_STAMP
    Store(tmp_path / "s").close()  # reopens: the stamp is the one it expects


def test_missing_marker_on_an_empty_layout_is_format2_again(tmp_path):
    """Format-2 twin of the format-1 backfill test: no record, so no legacy layout to protect."""
    Store(tmp_path / "s").close()
    (tmp_path / "s" / "format").unlink()
    Store(tmp_path / "s").close()
    assert (tmp_path / "s" / "format").read_text() == F2


def test_format2_marker_first_write_is_atomic_and_exclusive(tmp_path, monkeypatch):
    """Format-2 twin: a failure publishing the whole temporary file leaves no partial marker."""
    import sidegraph.store as store_module

    attempts = []
    real_link = store_module.os.link

    def fail_publication(src, dst):
        if Path(dst).name == "format":
            attempts.append(Path(src))
            assert Path(src).read_text(encoding="utf-8") == F2
            raise OSError("simulated crash between tmp write and exclusive publication")
        return real_link(src, dst)

    with monkeypatch.context() as patch:
        patch.setattr(store_module.os, "link", fail_publication)
        with pytest.raises(OSError, match="simulated crash"):
            Store(tmp_path / "s")
    assert len(attempts) == 1
    assert not (tmp_path / "s" / "format").exists()
    with Store(tmp_path / "s"):
        assert (tmp_path / "s" / "format").read_text(encoding="utf-8") == F2
    assert list((tmp_path / "s").glob("format.*.tmp")) == []


def test_format2_marker_write_tolerates_lost_concurrent_first_open_race(tmp_path, monkeypatch):
    """Format-2 twin: another opener publishes while this opener's temporary file is swept."""
    import sidegraph.store as store_module

    real_link = store_module.os.link
    raced = False

    def race_once(src, dst):
        nonlocal raced
        if Path(dst).name == "format" and not raced:
            raced = True
            Path(dst).write_text(Path(src).read_text(encoding="utf-8"), encoding="utf-8")
            Path(src).unlink()
            raise FileNotFoundError(f"simulated: {src} swept by concurrent opener")
        return real_link(src, dst)

    monkeypatch.setattr(store_module.os, "link", race_once)
    with Store(tmp_path / "s") as store:
        assert raced
        assert (store.path / "format").read_text(encoding="utf-8") == F2


def test_backfill_writes_major0_but_refuses_with_shards(tmp_path):
    store = tmp_path / ".sidegraph"
    put_file(store, "entity", entity_body("E0001"))
    Store(store).close()
    assert (store / "format").read_text() == "sidegraph-store 0.6.0\n"
    assert store_marker(store).major == 0

    packed = tmp_path / "packed" / ".sidegraph"
    put_file(packed, "entity", entity_body("E0001"))
    _plant_shard(packed)
    with pytest.raises(StoreRequirementError, match="sidegraph-migrate"):
        Store(packed)
    assert not (packed / "format").exists()


def test_a_shard_only_store_without_a_marker_is_not_fresh(tmp_path):
    store = tmp_path / ".sidegraph"
    store.mkdir()
    _plant_shard(store)
    with pytest.raises(StoreRequirementError, match="sidegraph-migrate"):
        Store(store)
    assert not (store / "format").exists()


def test_raise_requirement_keeps_major2_shards_and_stamp(tmp_path):  # red: probe A3
    store = make_store(tmp_path, n=16)
    s = Store(store)
    change = s.raise_requirement("0.18.0", running="0.18.0")
    assert (change.previous, change.required) == ("0.17.0", "0.18.0")
    assert (store / "format").read_text() == (
        "sidegraph-store 2.0.0\nrequires-sidegraph >= 0.18.0\nshards 16\n"
    )
    assert index_stamp_of(store) == FORMAT2_INDEX_STAMP
    s.refresh_if_stale()  # the cached major survived the write
    s.close()


def test_raise_requirement_on_format1_still_writes_major1(tmp_path):
    store = make_store(tmp_path, fmt=1)
    s = Store(store)
    s.raise_requirement("0.17.0", running="0.17.0")
    assert (store / "format").read_text() == "sidegraph-store 1.0.0\nrequires-sidegraph >= 0.17.0\n"
    assert index_stamp_of(store) == "0.6.0+requires"
    s.close()


def test_store_compact_refuses_on_format2(tmp_path):
    Store(tmp_path / "s").close()
    s = Store(tmp_path / "s")
    with pytest.raises(StoreRequirementError, match="--settled-days"):
        s.compact(older_than_days=0)
    s.close()
    assert not (tmp_path / "s" / "archive").exists() or not list(
        (tmp_path / "s" / "archive").glob("*.jsonl")
    )


# -- index stamps ---------------------------------------------------------------------------------


def test_format1_store_stamped_060_opens(tmp_path):
    store = tmp_path / ".sidegraph"
    store.mkdir()
    (store / "format").write_text("sidegraph-store 0.6.0\n", encoding="utf-8")
    Store(store).close()
    assert index_stamp_of(store) == "0.6.0"
    Store(store).close()
    assert HotIndex.open(store) is not None


def test_a_held_open_store_reloads_instead_of_raising_on_the_format2_stamp(tmp_path):
    """The reload gate also accepts ``0.7.0+format2``: a 0.17 server held open across a
    migration pull rebuilds and re-stamps."""
    store = make_store(tmp_path, fmt=1)
    s = Store(store)
    _restamp(store, FORMAT2_INDEX_STAMP)
    s.refresh_if_stale()
    assert index_stamp_of(store) == "0.6.0+requires"
    s.close()


# -- index stamps, both directions, every read-only opener ----------------------------------------

# (layout builder, the stamp that does not belong to it)
_WRONG_STAMPS = [("format2", "0.6.0"), ("format1", FORMAT2_INDEX_STAMP)]


def _store_for(tmp_path: Path, layout: str) -> Path:
    if layout == "format2":
        Store(tmp_path / "s").close()
        return tmp_path / "s"
    Store(format1_store(tmp_path / "s")).close()
    return tmp_path / "s"


@pytest.mark.parametrize(("layout", "stamp"), _WRONG_STAMPS)
@pytest.mark.parametrize("reader", ["hot_index", "open_index_ro", "stats", "doctor_index"])
def test_a_stamp_of_the_other_layout_is_no_index_in_both_directions(
    tmp_path, layout, stamp, reader
):
    store = _store_for(tmp_path, layout)
    good = index_stamp_of(store)
    _restamp(store, stamp)
    if reader == "hot_index":
        assert HotIndex.open(store) is None
    elif reader == "open_index_ro":
        assert open_index_ro(store) is None
    elif reader == "stats":
        with pytest.raises(sqlite3.DatabaseError, match="stamped"):
            build_report(store, None, window_days=7)
    else:
        assert open_curation_index(store) is None
    _restamp(store, good)
    if reader == "hot_index":
        opened = HotIndex.open(store)
        assert opened is not None
        opened.close()
    elif reader == "open_index_ro":
        conn = open_index_ro(store)
        assert conn is not None
        conn.close()
    elif reader == "stats":
        build_report(store, None, window_days=7)
    else:
        conn = open_curation_index(store)
        assert conn is not None
        conn.close()


def _doctor_evidence(tmp_path: Path, layout: str) -> Path:
    """A store whose index holds an orphaned binding and a never-surfaced decision, with the
    binding statuses certified computed: what doctor's index checks report when they run."""
    store = tmp_path / "s"
    if layout == "format1":
        format1_store(store)
    with Store(store) as s:
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
        for _ in range(12):
            s.record_retrieval([], ["a.py"])
    conn = sqlite3.connect(store / "index.db")
    try:
        conn.execute("UPDATE anchor_bindings SET data = json_set(data, '$.status', 'orphaned')")
        conn.execute("DELETE FROM meta WHERE key = 'volatile_stale'")
        conn.commit()
    finally:
        conn.close()
    return store


@pytest.mark.parametrize(("layout", "stamp"), _WRONG_STAMPS)
def test_doctor_withholds_each_index_check_on_a_stamp_of_the_other_layout(tmp_path, layout, stamp):
    """Each guard is observed through its own result on an index that holds evidence: with the
    right stamp the check reports it, with the wrong one it is withheld."""
    store = _doctor_evidence(tmp_path, layout)

    findings, skipped = doctor._check_binding_statuses(store)
    assert [f.code for f in findings] == ["orphaned-binding"] and skipped == []
    never, never_skipped = doctor._check_never_surfaced(store)
    assert [f.code for f in never] == ["never-surfaced"] and never_skipped == []
    snapshot = doctor._binding_status_snapshot(store)
    assert "orphaned" in snapshot.statuses.values() and snapshot.uncomputed is False
    curation = open_curation_index(store)
    assert curation is not None
    curation.close()

    _restamp(store, stamp)
    assert doctor._check_binding_statuses(store) == ([], ["binding-status"])
    assert doctor._check_never_surfaced(store) == ([], ["never-surfaced"])
    assert doctor._binding_status_snapshot(store) == doctor._BindingStatusSnapshot({}, True)
    assert open_curation_index(store) is None


def test_doctor_says_why_it_withheld_the_index_checks(tmp_path, capsys):
    store = _doctor_evidence(tmp_path, "format2")
    _restamp(store, "0.6.0")
    doctor_main(["--db", str(store)])
    out = capsys.readouterr().out
    assert "index checks withheld: the index is stamped '0.6.0'" in out
    assert "orphaned-binding" not in out and "never-surfaced  " not in out
    _restamp(store, FORMAT2_INDEX_STAMP)
    doctor_main(["--db", str(store)])
    out = capsys.readouterr().out
    assert "withheld" not in out and "orphaned-binding" in out


def test_blame_says_why_it_joined_nothing_on_a_wrong_stamp(
    tmp_path,
    git_repo,  # noqa: F811
    monkeypatch,
    capsys,
):
    store = git_repo / ".sidegraph"
    Store(store).close()
    (git_repo / "f.txt").write_text("line\n", encoding="utf-8")
    git(git_repo, "add", "f.txt")
    git(git_repo, "commit", "-q", "-m", "add f")
    monkeypatch.chdir(git_repo)
    _restamp(store, "0.6.0")
    assert blame_main(["f.txt", "--db", str(store)]) == 0
    err = capsys.readouterr().err
    assert "sidegraph-blame:" in err and "'0.6.0'" in err and "No records are joined." in err
    _restamp(store, FORMAT2_INDEX_STAMP)
    assert blame_main(["f.txt", "--db", str(store)]) == 0
    assert "No records are joined" not in capsys.readouterr().err


# -- held-open handles ------------------------------------------------------------------------


def test_a_held_open_store_without_a_marker_notices_shards_arriving(tmp_path):
    store = make_store(tmp_path, fmt=1)
    s = Store(store)
    (store / "format").unlink()
    s.refresh_if_stale()  # marker missing, no shards: nothing to refuse
    _plant_shard(store, "0A.SHARD")
    with pytest.raises(StoreRequirementError, match="sidegraph-migrate"):
        s.refresh_if_stale()
    s.close()


def test_a_held_open_format1_store_cannot_compact_after_the_marker_became_format2(tmp_path):
    store = make_store(tmp_path, fmt=1)
    s = Store(store)
    first = s.add_decision(_decision("first"))
    s.add_decision(_decision("second", supersedes=first.id))
    (store / "format").write_text(F2, encoding="utf-8")  # a pull swapped the marker
    before = sorted(p.name for p in (store / "decisions").glob("*.json"))
    with pytest.raises(StoreRequirementError, match="--settled-days") as raised:
        s.compact()
    assert raised.value.kind == "layout"
    assert sorted(p.name for p in (store / "decisions").glob("*.json")) == before
    assert not list((store / "archive").glob("*.jsonl")) if (store / "archive").exists() else True
    s.close()


def test_concurrent_first_open_of_an_empty_directory_agrees_on_format2(tmp_path):
    """Format-2 twin of the first-open race: workers wait on a barrier, then all race into an
    empty directory; the exclusive create keeps the first marker whichever wins."""
    import multiprocessing as mp

    ctx = mp.get_context("fork" if "fork" in mp.get_all_start_methods() else "spawn")
    count = 4
    path = tmp_path / "round"
    barrier = ctx.Barrier(count)
    queue = ctx.Queue()
    procs = [
        ctx.Process(target=_open_and_close_store, args=(str(path), barrier, queue))
        for _ in range(count)
    ]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(timeout=30)
    outcomes = [queue.get(timeout=5) for _ in range(count)]
    assert [o for o in outcomes if o[0] == "error"] == []
    assert [proc.exitcode for proc in procs] == [0] * count
    assert (path / "format").read_text(encoding="utf-8") == F2
    assert not (path / "shards").exists()
