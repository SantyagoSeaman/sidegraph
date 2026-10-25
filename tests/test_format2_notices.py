"""The two notices of the packed store format: ``store-not-migrated`` (urgent) and
``maintenance-due`` (soft), on the SessionStart and doctor surfaces.

Every test builds a store on disk and runs the real detectors; the surface tests run the real
hook and the real ``sidegraph-doctor``.
# see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§6.3, §9 T-N1)
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from sidegraph import integrity
from sidegraph.cli import doctor_main
from sidegraph.integrity import Inputs, run
from sidegraph.store import Store
from tests.store_format2_support import (
    archived_decision,
    decision_body,
    ids_in_bucket,
    make_store,
    put_archive,
    put_file,
    put_shard_line,
    shard_name,
)
from tests.test_host_integrity import context, start

NOW = datetime.now(UTC)
GUIDE = "docs/guides/migrating-to-0.17.md"


def notices(store: Path, surface: str = "session") -> dict[str, integrity.Problem]:
    """The two notices' problems by check id, from the real registry."""
    result = run(Inputs(store_dir=store, now=NOW), surface)  # type: ignore[arg-type]
    return {p.check: p for p in result.problems if p.check in _IDS}


_IDS = {"store-not-migrated", "maintenance-due"}
LIMIT = 3  # the file threshold the tests run under: the real 200 would mean writing 201 files


def test_the_documented_file_threshold_is_200():
    assert integrity._MAINTENANCE_FILE_THRESHOLD == 200  # docs/reference/cli.md names it


@pytest.fixture
def low_threshold(monkeypatch: pytest.MonkeyPatch) -> int:
    monkeypatch.setattr(integrity, "_MAINTENANCE_FILE_THRESHOLD", LIMIT)
    return LIMIT


def ids_of(store: Path, surface: str = "session") -> set[str]:
    return set(notices(store, surface))


def with_one_file(tmp_path: Path, *, fmt: int) -> Path:
    store = make_store(tmp_path, fmt=fmt)
    put_file(store, "decision", decision_body("D1"))
    return store


# -- urgent -------------------------------------------------------------------------------------


@pytest.mark.parametrize("surface", ["session", "doctor"])
def test_urgent_notice_on_unmigrated_store_with_records(tmp_path, surface):
    store = with_one_file(tmp_path, fmt=1)
    problem = notices(store, surface)["store-not-migrated"]
    line = problem.line
    assert line is not None
    assert "sidegraph-migrate" in line and GUIDE in line and "sidegraph:compact" in line
    assert problem.notice == line
    assert problem.severity == "degraded"
    assert problem.findings and GUIDE in problem.findings[0][1]
    assert "maintenance-due" not in ids_of(store, surface)


def test_urgent_notice_on_a_store_with_only_an_archive_segment(tmp_path):
    store = make_store(tmp_path, fmt=1)
    put_archive(store, "seg1", [archived_decision("ARCH1")])
    assert "store-not-migrated" in ids_of(store)


def test_urgent_notice_on_a_major_zero_marker(tmp_path):
    store = tmp_path / ".sidegraph"
    store.mkdir()
    (store / "format").write_text("sidegraph-store 0.6.0\n", encoding="utf-8")
    put_file(store, "entity", {"entity_id": "E1", "canonical_name": "e", "kind": "concrete"})
    assert "store-not-migrated" in ids_of(store)


def test_a_store_without_a_marker_file_says_nothing_until_a_store_open_stamps_it(tmp_path):
    store = tmp_path / ".sidegraph"
    put_file(store, "entity", {"entity_id": "E1", "canonical_name": "e", "kind": "concrete"})
    assert not (store / "format").exists()
    assert ids_of(store) == set()
    Store(store).close()  # the first open backfills the major-0 marker
    assert (store / "format").exists()
    assert "store-not-migrated" in ids_of(store)


@pytest.mark.parametrize("fmt", [1, 2])
def test_no_notice_on_empty_store(tmp_path, fmt):
    store = make_store(tmp_path, fmt=fmt)
    assert ids_of(store) == set()
    assert ids_of(store, "doctor") == set()


def test_no_notice_on_a_store_without_a_marker_or_directory(tmp_path):
    assert ids_of(tmp_path / "nowhere") == set()


# -- soft ---------------------------------------------------------------------------------------


def soft_store(tmp_path: Path, trigger: str) -> Path:
    store = make_store(tmp_path, n=64)
    (a,) = ids_in_bucket(3, 1, prefix="XA")
    (b,) = ids_in_bucket(17, 1, prefix="YB")
    put_shard_line(store, "decision", a, decision_body(a))  # a healthy packed store
    if trigger == "over_threshold":
        for i in range(LIMIT + 1):
            put_file(store, "decision", decision_body(f"F{i:05d}"))
    elif trigger == "deletable_segment":
        archived = archived_decision(b)
        put_shard_line(
            store, "decision", b, {k: v for k, v in archived.items() if k != "record_type"}
        )
        put_archive(store, "seg1", [archived])
    elif trigger == "misplaced":
        put_shard_line(store, "decision", b, decision_body(b), bucket=shard_name(3))
    elif trigger == "equal_duplicate":
        put_shard_line(store, "decision", a, decision_body(a))  # the same line twice
    elif trigger == "stray":
        put_shard_line(store, "decision", b, decision_body(b), bucket="ff.shard")
    else:  # pragma: no cover
        raise AssertionError(trigger)
    return store


@pytest.mark.parametrize(
    "trigger", ["over_threshold", "deletable_segment", "misplaced", "equal_duplicate", "stray"]
)
@pytest.mark.parametrize("surface", ["session", "doctor"])
def test_soft_notice(tmp_path, low_threshold, trigger, surface):
    store = soft_store(tmp_path, trigger)
    problem = notices(store, surface)["maintenance-due"]
    assert problem.severity == "advisory"
    assert problem.line is not None and "sidegraph-compact" in problem.line
    assert problem.notice == problem.line
    assert "store-not-migrated" not in ids_of(store, surface)


def test_healthy_packed_store_has_no_notice(tmp_path):
    store = make_store(tmp_path, n=64)
    (a,) = ids_in_bucket(3, 1, prefix="XA")
    put_shard_line(store, "decision", a, decision_body(a))
    assert ids_of(store) == set()


def test_files_at_the_threshold_give_no_soft_notice(tmp_path, low_threshold):
    store = make_store(tmp_path, n=64)
    for i in range(low_threshold):
        put_file(store, "decision", decision_body(f"F{i:05d}"))
    assert "maintenance-due" not in ids_of(store)


def test_undeletable_segment_does_not_trigger(tmp_path):
    store = make_store(tmp_path, n=64)
    (b,) = ids_in_bucket(17, 1, prefix="YB")
    archived = archived_decision(b)
    packed = {k: v for k, v in archived.items() if k != "record_type"}
    put_shard_line(store, "decision", b, packed)
    put_archive(store, "seg1", [{**archived, "note": "a key the shard line lacks"}])
    assert ids_of(store) == set()


# -- the surfaces -------------------------------------------------------------------------------


def test_session_start_and_doctor_show_them(tmp_path, monkeypatch, capsys):
    unmigrated = with_one_file(tmp_path / "old", fmt=1)
    out = start(monkeypatch, capsys, unmigrated)
    assert "sidegraph-migrate" in context(out) and GUIDE in context(out)
    assert "sidegraph-migrate" in out["systemMessage"]
    doctor_main(["--db", str(unmigrated)])
    printed = capsys.readouterr().out
    assert any(
        line.startswith("store-not-migrated  ") and "sidegraph-migrate" in line
        for line in printed.splitlines()
    )

    packed = soft_store(tmp_path / "new", "misplaced")
    out = start(monkeypatch, capsys, packed)
    assert "sidegraph-compact" in context(out)
    assert "sidegraph-compact" in out["systemMessage"]
    doctor_main(["--db", str(packed)])
    printed = capsys.readouterr().out
    assert any(line.startswith("maintenance-due  ") for line in printed.splitlines())


def test_the_new_checks_run_last_in_the_registry():
    """Hosts splice their checks into the registry by index (``CHECKS[:5]``)."""
    assert [c.id for c in integrity.CHECKS][-2:] == ["store-not-migrated", "maintenance-due"]


# -- cost ---------------------------------------------------------------------------------------


def test_many_records_are_decided_without_reading_them_all(tmp_path, monkeypatch, low_threshold):
    """Over the file threshold the soft notice is known from the directory listing alone."""
    store = make_store(tmp_path, n=64)
    for i in range(low_threshold + 1):
        put_file(store, "decision", decision_body(f"F{i:05d}"))

    def boom(*_a, **_k):
        raise AssertionError("the shards were read")

    monkeypatch.setattr(integrity, "inventory_from_disk", boom)
    assert "maintenance-due" in ids_of(store)


def test_directories_named_json_are_not_records(tmp_path, low_threshold):
    store = make_store(tmp_path, n=64)
    for i in range(low_threshold + 1):
        (store / "decisions" / f"F{i:05d}.json").mkdir(parents=True)
    assert ids_of(store) == set()
    old = make_store(tmp_path / "old", fmt=1)
    (old / "decisions" / "D1.json").mkdir(parents=True)
    assert ids_of(old) == set()


def test_urgent_notice_counts_initiatives(tmp_path):
    store = make_store(tmp_path, fmt=1)
    (store / "initiatives").mkdir()
    (store / "initiatives" / "I1.json").write_text("{}", encoding="utf-8")
    assert "store-not-migrated" in ids_of(store)


def test_guide_is_the_public_url(tmp_path):
    line = notices(with_one_file(tmp_path, fmt=1))["store-not-migrated"].line
    assert (
        line is not None
        and f"https://github.com/SantyagoSeaman/sidegraph/blob/main/{GUIDE}" in line
    )


def test_a_line_that_fails_the_model_check_keeps_its_segment(tmp_path):
    """The planner's own check applies: a segment of an unreadable line is not removable."""
    store = make_store(tmp_path, n=64)
    (b,) = ids_in_bucket(17, 1, prefix="YB")
    thin = {"record_type": "decision", "id": b, "status": "rejected"}
    full = archived_decision(b)
    put_shard_line(store, "decision", b, {k: v for k, v in full.items() if k != "record_type"})
    put_archive(store, "seg1", [thin])  # every key of it is in the shard line: only the model
    # check keeps the segment
    (c,) = ids_in_bucket(40, 1, prefix="ZC")
    put_shard_line(store, "decision", c, decision_body(c), bucket="ff.shard")  # a stray: due
    # the detector itself, not the registry (which swallows an exception as "not run")
    problem = integrity._detect_maintenance_due(Inputs(store_dir=store, now=NOW))
    assert problem is not None and problem.line is not None
    assert "stray shard file" in problem.line and "archive segment" not in problem.line


def test_a_directory_named_like_a_segment_is_not_a_segment(tmp_path):
    store = make_store(tmp_path, fmt=1)
    (store / "archive" / "seg.jsonl").mkdir(parents=True)
    assert ids_of(store) == set()


def test_untracked_file_beside_a_terminal_archive_is_not_due(tmp_path):
    """No shard line covers the archived record, so maintenance would keep the segment unless
    the per-record file folds; without a clock that is not promised."""
    store = make_store(tmp_path, n=64)
    (b,) = ids_in_bucket(17, 1, prefix="YB")
    archived = archived_decision(b)
    put_file(store, "decision", {k: v for k, v in archived.items() if k != "record_type"})
    put_archive(store, "seg1", [archived])
    assert ids_of(store) == set()


@pytest.mark.parametrize("check", ["store-not-migrated", "maintenance-due"])
def test_surfaces_are_session_and_doctor(check):
    found = next(c for c in integrity.CHECKS if c.id == check)
    assert found.surfaces == frozenset({"session", "doctor"})
