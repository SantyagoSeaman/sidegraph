"""Regression tests for the N5 compaction code-review fix wave (team-lead review of commit
061e913): 2 Important + 5 Minor findings, all fixed in the commit that added this file. See
that commit's message for the full finding-by-finding mapping; each test below is
cross-referenced to its finding in a comment."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sidegraph.cli import compact_main
from sidegraph.schema import (
    Decision,
    DecisionKind,
    DecisionStatus,
    Domain,
    Provenance,
)
from sidegraph.store import Store, _archive_record_line, _domain_canonical_payload


@pytest.fixture
def store(tmp_path) -> Store:
    with Store(tmp_path / "s") as s:
        yield s


def _decision(**overrides) -> Decision:
    base = dict(
        title="Use file-per-record JSON",
        kind=DecisionKind.ADR,
        context="ctx",
        choice="choice",
        valid_from=datetime.now(UTC),
        provenance=Provenance(source="manual"),
    )
    base.update(overrides)
    return Decision(**base)


def _domain(**overrides) -> Domain:
    base = dict(
        slug="payments",
        title="Payments",
        summary="Handles order settlement and refunds.",
        provenance=Provenance(source="manual"),
    )
    base.update(overrides)
    return Domain(**base)


def _hot_decision_path(store: Store, decision_id: str) -> Path:
    return store.path / "decisions" / f"{decision_id}.json"


def _expected_segment_content(decisions, domains) -> str:
    """Replicate Store._write_archive_segment's own content construction so a test can
    predict the exact filename (including its content-hash suffix) a real write would
    produce -- used to deliberately pre-occupy that name and force the EEXIST retry path
    (Important-1)."""
    records = [
        (d.id, _archive_record_line("decision", d.model_dump(mode="json"))) for d in decisions
    ] + [
        (dom.domain_id, _archive_record_line("domain", _domain_canonical_payload(dom)))
        for dom in domains
    ]
    records.sort(key=lambda item: item[0])
    return "\n".join(line for _, line in records) + "\n"


def _expected_segment_name(date_str: str, seq: int, content: str) -> str:
    h = hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]
    return f"{date_str}-{seq}-{h}.jsonl"


# == Important-1: segment publish is concurrency-safe (unique tmp name, exclusive-create
# publish, seq-collision retry) =============================================================


def test_publish_retries_under_next_seq_when_target_name_pre_exists(store: Store) -> None:
    old = store.add_decision(_decision(title="d1"))
    store.add_decision(_decision(title="d1-succ", supersedes=old.id))
    decision = store.get_decision(old.id)
    assert decision is not None

    content = _expected_segment_content([decision], [])
    date_str = datetime.now(UTC).date().isoformat()
    would_be_name = _expected_segment_name(date_str, 1, content)

    archive_dir = store.path / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    decoy_path = archive_dir / would_be_name
    decoy_path.write_bytes(b"decoy content that must never be touched")
    decoy_bytes_before = decoy_path.read_bytes()

    segment_relpath = store._write_archive_segment([decision], [])

    # landed under the NEXT seq, not the occupied one
    hash12 = would_be_name[: -len(".jsonl")].rsplit("-", 1)[1]
    assert segment_relpath == f"archive/{date_str}-2-{hash12}.jsonl"
    # the pre-existing file at seq 1 is completely untouched
    assert decoy_path.read_bytes() == decoy_bytes_before
    # and the real segment is readable, with the real content
    real_segment = store.path / segment_relpath
    assert real_segment.read_text(encoding="utf-8") == content
    # no leftover tmp debris
    assert not any(archive_dir.glob("*.tmp"))


def test_publish_failure_leaves_no_partial_segment_and_never_removes_hot_files(
    store: Store, monkeypatch
) -> None:
    old = store.add_decision(_decision(title="d1"))
    store.add_decision(_decision(title="d1-succ", supersedes=old.id))
    hot_path = _hot_decision_path(store, old.id)

    import sidegraph.store as store_module

    def _boom(a, b):
        raise OSError("simulated disk failure during segment publish")

    monkeypatch.setattr(store_module.os, "link", _boom)

    with pytest.raises(OSError, match="simulated disk failure"):
        store.compact()

    # the hot file must still be there -- compact() must never reach the removal step
    # once the segment write itself failed
    assert hot_path.is_file()

    archive_dir = store.path / "archive"
    if archive_dir.is_dir():
        assert list(archive_dir.glob("*.jsonl")) == []  # no partial/truncated segment
        assert list(archive_dir.glob("*.tmp")) == []  # tmp cleaned up even on failure


# == Important-2: add_decision never resurrects an already-terminal (possibly archived)
# predecessor =================================================================================


def test_supersede_never_resurrects_archived_rejected_predecessor(store: Store, capsys) -> None:
    old = store.add_decision(_decision(title="predecessor"))
    store.drop(old.id)  # -> REJECTED, valid_to stamped
    report = store.compact()
    assert report.decisions_compacted == 1
    hot_path = _hot_decision_path(store, old.id)
    assert not hot_path.is_file()

    successor = store.add_decision(_decision(title="successor", supersedes=old.id))

    # never resurrected as a fresh hot file
    assert not hot_path.is_file()
    reloaded_old = store.get_decision(old.id)
    assert reloaded_old is not None
    assert reloaded_old.status == DecisionStatus.REJECTED  # unchanged, still terminal
    # the relationship is still recorded on the successor (append-only semantics)
    assert store.get_decision(successor.id).supersedes == old.id

    # a cold reopen must not warn about a hot/archive mismatch -- there IS no hot file
    path = store.path
    store.close()
    reopened = Store(path)
    try:
        assert reopened.get_decision(old.id).status == DecisionStatus.REJECTED
    finally:
        reopened.close()
    assert "WARNING" not in capsys.readouterr().err


def test_supersede_still_closes_a_still_open_accepted_predecessor(store: Store) -> None:
    """The guard must not overreach: an OPEN (accepted/proposed) predecessor is closed
    exactly as before -- only an ALREADY-terminal one is protected."""
    old = store.add_decision(_decision(title="predecessor"))
    store.ratify(old.id)  # -> ACCEPTED (still open)
    successor = store.add_decision(_decision(title="successor", supersedes=old.id))

    assert store.get_decision(old.id).status == DecisionStatus.SUPERSEDED
    assert store.get_decision(successor.id).supersedes == old.id


def test_deferred_supersession_recovery_via_ratify_still_works(store: Store) -> None:
    """ratify()'s own deferred-supersession guard (predecessor closed only if still
    accepted/proposed) is untouched by this fix -- still covered end to end."""
    old = store.add_decision(_decision(title="predecessor"))
    store.ratify(old.id)
    successor = store.add_decision(
        _decision(title="successor (proposed)", supersedes=old.id), close_predecessor=False
    )
    # deferred: predecessor left open until the successor itself is ratified
    assert store.get_decision(old.id).status == DecisionStatus.ACCEPTED

    store.ratify(successor.id)
    assert store.get_decision(old.id).status == DecisionStatus.SUPERSEDED


# == Minor-3: fsync on the segment content + the archive directory entry ====================


def test_compact_fsyncs_segment_content_and_archive_directory(store: Store, monkeypatch) -> None:
    old = store.add_decision(_decision(title="d1"))
    store.add_decision(_decision(title="d1-succ", supersedes=old.id))

    import sidegraph.store as store_module

    real_fsync = store_module.os.fsync
    calls = []

    def _spy(fd):
        calls.append(fd)
        return real_fsync(fd)

    monkeypatch.setattr(store_module.os, "fsync", _spy)

    report = store.compact()
    assert report.segment_path is not None
    # at least one fsync for the tmp file's content, one for the archive directory entry
    assert len(calls) >= 2


# == Minor-4: segment filenames carry a content-hash suffix; same-day cross-branch
# compacts never collide; the loader still accepts the legacy bare name ====================


def test_segment_filename_has_date_seq_hash_shape(store: Store) -> None:
    old = store.add_decision(_decision(title="d1"))
    store.add_decision(_decision(title="d1-succ", supersedes=old.id))
    report = store.compact()
    name = Path(report.segment_path).name
    assert name.endswith(".jsonl")
    # "YYYY-MM-DD-seq-hash12.jsonl" -- 5 dash-separated components once the suffix is gone
    parts = name[: -len(".jsonl")].split("-")
    assert len(parts) == 5  # YYYY, MM, DD, seq, hash12
    year, month, day, seq_str, hash12 = parts
    assert len(year) == 4 and len(month) == 2 and len(day) == 2
    assert seq_str.isdigit()
    assert len(hash12) == 12
    assert all(c in "0123456789abcdef" for c in hash12)


def test_same_day_cross_branch_compacts_get_different_names_no_collision(tmp_path) -> None:
    store_a = Store(tmp_path / "a")
    old_a = store_a.add_decision(_decision(title="branch A decision"))
    store_a.add_decision(_decision(title="branch A succ", supersedes=old_a.id))
    report_a = store_a.compact()
    store_a.close()

    store_b = Store(tmp_path / "b")
    old_b = store_b.add_decision(_decision(title="branch B decision"))
    store_b.add_decision(_decision(title="branch B succ", supersedes=old_b.id))
    report_b = store_b.compact()
    store_b.close()

    name_a = Path(report_a.segment_path).name
    name_b = Path(report_b.segment_path).name
    assert name_a != name_b  # different content -> different filenames -> no git conflict

    # simulate the merge: both segments land in one directory side by side
    merged = tmp_path / "merged-archive"
    merged.mkdir()
    (merged / name_a).write_bytes((tmp_path / "a" / report_a.segment_path).read_bytes())
    (merged / name_b).write_bytes((tmp_path / "b" / report_b.segment_path).read_bytes())
    assert len(list(merged.glob("*.jsonl"))) == 2  # both survive, no overwrite


def test_identical_content_same_day_produces_identical_name_and_bytes(tmp_path) -> None:
    fixed_time = datetime(2026, 1, 1, tzinfo=UTC)

    def _seed_and_compact(store_path):
        s = Store(store_path)
        d = _decision(
            title="identical across branches",
            status=DecisionStatus.SUPERSEDED,
            id="01FIXEDULID00000000000AA",
            valid_from=fixed_time,
            valid_to=fixed_time,
        )
        with s._lock:
            s._write_decision(d)
            s._commit()
        report = s.compact()
        s.close()
        return report

    report_a = _seed_and_compact(tmp_path / "a")
    report_b = _seed_and_compact(tmp_path / "b")

    assert report_a.segment_path == report_b.segment_path  # same name
    bytes_a = (tmp_path / "a" / report_a.segment_path).read_bytes()
    bytes_b = (tmp_path / "b" / report_b.segment_path).read_bytes()
    assert bytes_a == bytes_b  # and same bytes -- trivially mergeable, no conflict


def test_loader_still_accepts_legacy_bare_segment_name(tmp_path) -> None:
    store = Store(tmp_path / "s")
    store.close()

    now = datetime.now(UTC)
    legacy_decision = _decision(
        title="legacy format decision",
        status=DecisionStatus.SUPERSEDED,
        valid_from=now,
        valid_to=now,
    )
    archive_dir = tmp_path / "s" / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    line = json.dumps(
        {"record_type": "decision", **legacy_decision.model_dump(mode="json")},
        sort_keys=True,
        ensure_ascii=False,
    )
    (archive_dir / "2020-01-01-1.jsonl").write_text(line + "\n", encoding="utf-8")  # bare name

    reopened = Store(tmp_path / "s")
    try:
        got = reopened.get_decision(legacy_decision.id)
        assert got is not None
        assert got.title == "legacy format decision"
    finally:
        reopened.close()


def test_seq_numbering_recognizes_legacy_bare_segment_names(store: Store) -> None:
    date_str = datetime.now(UTC).date().isoformat()
    archive_dir = store.path / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    (archive_dir / f"{date_str}-3.jsonl").write_text("", encoding="utf-8")  # legacy, bare

    old = store.add_decision(_decision(title="d1"))
    store.add_decision(_decision(title="d1-succ", supersedes=old.id))
    report = store.compact()

    name = Path(report.segment_path).name
    assert name.startswith(f"{date_str}-4-")  # picked up the legacy seq 3, didn't collide


# == Minor-5: a corrupt archive segment names itself (path + line number) in the error ======


def test_corrupt_archive_segment_error_names_path_and_line(tmp_path) -> None:
    store = Store(tmp_path / "s")
    store.close()

    archive_dir = tmp_path / "s" / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC)
    good_decision = _decision(
        title="ok", status=DecisionStatus.SUPERSEDED, valid_from=now, valid_to=now
    )
    good_line = json.dumps(
        {"record_type": "decision", **good_decision.model_dump(mode="json")},
        sort_keys=True,
        ensure_ascii=False,
    )
    bad_segment = archive_dir / "2020-01-01-1-deadbeef0000.jsonl"
    bad_segment.write_text(good_line + "\nnot json at all\n", encoding="utf-8")

    with pytest.raises(ValueError) as exc_info:
        Store(tmp_path / "s")
    msg = str(exc_info.value)
    assert str(bad_segment) in msg
    assert "line 2" in msg


# == Minor-6: compact() never deletes a fresh candidate's hot file if it no longer matches
# what got archived (compares on-disk content, not just the trusted index snapshot) ========


def test_fresh_candidate_hot_file_changed_externally_is_never_deleted(store: Store, capsys) -> None:
    old = store.add_decision(_decision(title="original"))
    store.add_decision(_decision(title="successor", supersedes=old.id))
    hot_path = _hot_decision_path(store, old.id)

    # Simulate the hot file changing out from under compact() between the index read and
    # the removal step (e.g. a hand edit, or state from a process not going through this
    # Store's lock).
    tampered = old.model_copy(update={"title": "changed out from under compact"})
    hot_path.write_text(
        json.dumps(tampered.model_dump(mode="json"), sort_keys=True, indent=2, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )

    report = store.compact()

    assert report.decisions_compacted == 1  # still archived, from the index's snapshot
    assert hot_path.is_file()  # never deleted -- newer on-disk state preserved
    assert json.loads(hot_path.read_text())["title"] == "changed out from under compact"
    err = capsys.readouterr().err
    assert "WARNING" in err
    assert old.id in err


# == Minor-7: domains excluded under --older-than get their own report field + CLI wording ==


def test_report_separates_domain_age_exclusion_from_decision_skips(store: Store) -> None:
    dropped_domain = store.add_domain(_domain(slug="d-dropped"))
    store.ratify_domains(drop=[dropped_domain.domain_id])

    report = store.compact(older_than_days=30, dry_run=True)
    assert report.domains_excluded_age_unknown == 1
    assert report.skipped_age_filtered == 0


def test_cli_compact_older_than_reports_domain_exclusion_separately(tmp_path, capsys) -> None:
    db = tmp_path / "t.db"
    s = Store(db)
    dom = s.add_domain(_domain(slug="d-dropped"))
    s.ratify_domains(drop=[dom.domain_id])
    s.close()

    assert compact_main(["--db", str(db), "--older-than", "30"]) == 0
    out = capsys.readouterr().out
    assert "1 domain(s) excluded: terminal age unknown" in out
