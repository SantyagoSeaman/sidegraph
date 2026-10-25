"""Compaction (see docs/reference/store-format.md#archive-segments-sidegraph-compact):
terminal-status decisions/domains pack into an immutable ``archive/<date>-<seq>.jsonl``
segment and their hot canonical files are removed in the same operation. CLAUDE.md
invariant #2 (append-only) still holds throughout: records MOVE, they are never lost or
mutated -- every test here proves that some way (round-trip fidelity, retrievability,
crash-window safety, or a hand-tampered hot file that must never be silently destroyed)."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sidegraph.cli import compact_main
from sidegraph.schema import (
    AnchorBinding,
    Decision,
    DecisionKind,
    DecisionStatus,
    Descriptor,
    Domain,
    DomainStatus,
    Entity,
    Provenance,
)
from sidegraph.store import Store, _archive_record_line, _domain_canonical_payload
from sidegraph.store_layout import hot_archive_difference, hot_matches_archive
from tests.store_format2_support import format1_store


@pytest.fixture
def store(tmp_path) -> Store:
    with Store(format1_store(tmp_path / "s")) as s:
        yield s


def _decision(**overrides) -> Decision:
    base = dict(
        title="Use file-per-record JSON",
        kind=DecisionKind.ADR,
        context="A single committed SQLite file can't be merged by git.",
        choice="One JSON file per record, plus a derived, gitignored local index.",
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


def _write_decision_directly(store: Store, decision: Decision) -> Decision:
    """Bypass add_decision's high-level invariants (e.g. "a superseded decision must
    reference a real successor") to seed a specific terminal-status/timestamp shape
    directly -- mirrors test_store_domains.py's use of ``store._write_domain`` for the
    same purpose."""
    with store._lock:
        store._write_decision(decision)
        store._commit()
    return decision


def _hot_decision_path(store: Store, decision_id: str):
    return store.path / "decisions" / f"{decision_id}.json"


def _hot_domain_path(store: Store, domain_id: str):
    return store.path / "domains" / f"{domain_id}.json"


def _archive_segments(store: Store) -> list:
    d = store.path / "archive"
    return sorted(d.glob("*.jsonl")) if d.is_dir() else []


# -- terminal-status selection -------------------------------------------------------------


def test_only_terminal_status_decisions_and_domains_are_selected(store: Store) -> None:
    store.add_decision(_decision(title="proposed"))
    accepted_seed = store.add_decision(_decision(title="accepted seed"))
    store.ratify(accepted_seed.id)

    superseded_old = store.add_decision(_decision(title="superseded predecessor"))
    store.add_decision(_decision(title="successor", supersedes=superseded_old.id))

    rejected_seed = store.add_decision(_decision(title="to reject"))
    store.drop(rejected_seed.id)

    deprecated_seed = store.add_decision(_decision(title="to deprecate"))
    _write_decision_directly(
        store, deprecated_seed.model_copy(update={"status": DecisionStatus.DEPRECATED})
    )

    store.add_domain(_domain(slug="d-proposed"))
    domain_accepted = store.add_domain(_domain(slug="d-accepted"))
    store.ratify_domains(accept=[domain_accepted.domain_id])
    domain_superseded_old = store.add_domain(_domain(slug="d-superseded"))
    store.supersede_domain(
        domain_superseded_old.domain_id,
        _domain(slug="d-superseded", supersedes=domain_superseded_old.domain_id),
    )
    domain_dropped = store.add_domain(_domain(slug="d-dropped"))
    store.ratify_domains(drop=[domain_dropped.domain_id])

    report = store.compact()

    assert report.decisions_compacted == 3  # superseded, rejected, deprecated
    assert report.domains_compacted == 2  # superseded, dropped
    compacted_ulids = {item.ulid for item in report.items}
    assert compacted_ulids == {
        superseded_old.id,
        rejected_seed.id,
        deprecated_seed.id,
        domain_superseded_old.domain_id,
        domain_dropped.domain_id,
    }

    # terminal records' hot files are gone
    assert not _hot_decision_path(store, superseded_old.id).exists()
    assert not _hot_decision_path(store, rejected_seed.id).exists()
    assert not _hot_decision_path(store, deprecated_seed.id).exists()
    assert not _hot_domain_path(store, domain_superseded_old.domain_id).exists()
    assert not _hot_domain_path(store, domain_dropped.domain_id).exists()


def test_proposed_and_accepted_records_never_compact(store: Store) -> None:
    proposed = store.add_decision(_decision(title="proposed"))
    accepted_seed = store.add_decision(_decision(title="accepted seed"))
    accepted, _cascaded = store.ratify(accepted_seed.id)
    domain_proposed = store.add_domain(_domain(slug="d-proposed"))
    domain_accepted = store.add_domain(_domain(slug="d-accepted"))
    store.ratify_domains(accept=[domain_accepted.domain_id])

    report = store.compact()

    assert report.total_compacted == 0
    assert _hot_decision_path(store, proposed.id).is_file()
    assert _hot_decision_path(store, accepted.id).is_file()
    assert _hot_domain_path(store, domain_proposed.domain_id).is_file()
    assert _hot_domain_path(store, domain_accepted.domain_id).is_file()
    # still fully readable through the normal API -- nothing about them changed at all
    assert store.get_decision(proposed.id).status == DecisionStatus.PROPOSED
    assert store.get_domain(domain_accepted.domain_id).status == DomainStatus.ACCEPTED


# -- segment immutability / ULID sort ------------------------------------------------------


def test_second_compact_writes_a_new_segment_first_left_byte_identical(store: Store) -> None:
    old1 = store.add_decision(_decision(title="d1"))
    store.add_decision(_decision(title="d1-succ", supersedes=old1.id))
    report1 = store.compact()
    assert report1.segment_path is not None
    seg1 = store.path / report1.segment_path
    seg1_bytes_before = seg1.read_bytes()

    old2 = store.add_decision(_decision(title="d2"))
    store.add_decision(_decision(title="d2-succ", supersedes=old2.id))
    report2 = store.compact()

    assert report2.segment_path is not None
    assert report2.segment_path != report1.segment_path
    assert seg1.read_bytes() == seg1_bytes_before  # first segment untouched
    seg2 = store.path / report2.segment_path
    assert seg2.is_file()
    assert len(_archive_segments(store)) == 2


def test_records_sorted_by_ulid_within_segment(store: Store) -> None:
    olds = []
    for i in range(4):
        o = store.add_decision(_decision(title=f"d{i}"))
        store.add_decision(_decision(title=f"d{i}-succ", supersedes=o.id))
        olds.append(o)
    dom_old = store.add_domain(_domain(slug="d-super"))
    store.supersede_domain(dom_old.domain_id, _domain(slug="d-super", supersedes=dom_old.domain_id))

    report = store.compact()
    seg = store.path / report.segment_path
    lines = seg.read_text(encoding="utf-8").splitlines()
    assert lines  # sanity
    ids = []
    for line in lines:
        raw = json.loads(line)
        ids.append(raw["id"] if raw["record_type"] == "decision" else raw["domain_id"])
    assert ids == sorted(ids)
    # also matches the report's own item ordering
    assert [item.ulid for item in report.items] == sorted(item.ulid for item in report.items)


# -- crash-window safety --------------------------------------------------------------------


def test_crash_window_leftover_hot_file_cleaned_up_without_new_segment(store: Store) -> None:
    old = store.add_decision(_decision(title="d1"))
    store.add_decision(_decision(title="d1-succ", supersedes=old.id))
    hot_path = _hot_decision_path(store, old.id)
    original_bytes = hot_path.read_bytes()

    report1 = store.compact()
    assert report1.decisions_compacted == 1
    assert not hot_path.exists()
    assert len(_archive_segments(store)) == 1

    # Simulate the crash window: the segment was durably written, but the process died
    # before the hot file's removal landed -- recreate it, byte-identical.
    hot_path.parent.mkdir(parents=True, exist_ok=True)
    hot_path.write_bytes(original_bytes)

    report2 = store.compact()

    assert report2.decisions_compacted == 0
    assert report2.domains_compacted == 0
    assert report2.segment_path is None  # no NEW segment written
    assert report2.cleaned_up_hot_files == 1
    assert not hot_path.exists()  # completed the removal
    assert len(_archive_segments(store)) == 1  # still just the one segment -- no duplicate


# -- --older-than filtering -----------------------------------------------------------------


def test_older_than_filters_decisions_by_valid_to_and_always_excludes_domains(
    store: Store,
) -> None:
    now = datetime.now(UTC)
    recent = _write_decision_directly(
        store,
        _decision(
            title="recent",
            status=DecisionStatus.SUPERSEDED,
            valid_from=now - timedelta(days=2),
            valid_to=now - timedelta(days=1),
        ),
    )
    ancient = _write_decision_directly(
        store,
        _decision(
            title="ancient",
            status=DecisionStatus.SUPERSEDED,
            valid_from=now - timedelta(days=101),
            valid_to=now - timedelta(days=100),
        ),
    )
    no_timestamp = _write_decision_directly(
        store, _decision(title="no-ts", status=DecisionStatus.REJECTED, valid_to=None)
    )
    dropped_domain = store.add_domain(_domain(slug="d-dropped"))
    store.ratify_domains(drop=[dropped_domain.domain_id])

    report = store.compact(older_than_days=30, dry_run=True)

    compacted_ulids = {item.ulid for item in report.items}
    assert ancient.id in compacted_ulids
    assert recent.id not in compacted_ulids
    assert no_timestamp.id not in compacted_ulids
    assert dropped_domain.domain_id not in compacted_ulids
    # recent (too young) + no_timestamp (no reliable terminal timestamp) -- both decisions,
    # both conservatively excluded, none destroyed. The domain (which never carries a
    # terminal timestamp at all) is reported separately (review Minor-7).
    assert report.skipped_age_filtered == 2
    assert report.domains_excluded_age_unknown == 1
    assert report.decisions_compacted == 1
    assert report.domains_compacted == 0

    # dry_run=True above wrote nothing -- prove it, then run for real without the filter
    # disabled, confirming the excluded records are still there, untouched, and hot.
    assert _hot_decision_path(store, recent.id).is_file()
    assert _hot_decision_path(store, ancient.id).is_file()
    assert _hot_domain_path(store, dropped_domain.domain_id).is_file()


# -- --dry-run writes nothing -----------------------------------------------------------------


def _snapshot(store: Store) -> dict:
    out = {}
    for sub in ("decisions", "domains", "entities", "bindings", "initiatives"):
        d = store.path / sub
        if not d.is_dir():
            continue
        for f in d.iterdir():
            if f.suffix == ".json":
                out[f"{sub}/{f.name}"] = f.read_bytes()
    archive_dir = store.path / "archive"
    if archive_dir.is_dir():
        for f in archive_dir.iterdir():
            out[f"archive/{f.name}"] = f.read_bytes()
    return out


def test_dry_run_writes_nothing_digest_unchanged(store: Store) -> None:
    old = store.add_decision(_decision(title="d1"))
    store.add_decision(_decision(title="d1-succ", supersedes=old.id))
    digest_before = store.get_meta("canonical_digest")
    files_before = _snapshot(store)

    report = store.compact(dry_run=True)

    assert report.dry_run is True
    assert report.decisions_compacted == 1
    assert store.get_meta("canonical_digest") == digest_before
    assert _snapshot(store) == files_before
    assert not (store.path / "archive").exists()
    assert _hot_decision_path(store, old.id).is_file()


# -- loader round-trip fidelity --------------------------------------------------------------


def test_archived_decision_round_trips_every_field_including_cyrillic(store: Store) -> None:
    text = "решение — с грабли́ми"
    old = store.add_decision(
        _decision(
            title=text,
            context=f"context: {text}",
            choice=f"choice: {text}",
            rejected="tried X, abandoned because Y",
            consequences="downstream effect",
            kind=DecisionKind.LESSON,
            layer="technical",
        )
    )
    store.add_decision(_decision(title="successor", supersedes=old.id))
    report = store.compact()
    assert report.decisions_compacted == 1

    path = store.path
    store.close()
    reopened = Store(format1_store(path))
    try:
        got = reopened.get_decision(old.id)
        assert got is not None
        assert got.title == text
        assert got.context == f"context: {text}"
        assert got.choice == f"choice: {text}"
        assert got.rejected == "tried X, abandoned because Y"
        assert got.consequences == "downstream effect"
        assert got.kind == DecisionKind.LESSON
        assert got.layer == "technical"
        assert got.status == DecisionStatus.SUPERSEDED
        assert got.provenance.source == "manual"

        seg = _archive_segments(reopened)[0]
        raw_bytes = seg.read_bytes()
        assert text.encode("utf-8") in raw_bytes
        assert b"\\u" not in raw_bytes  # ensure_ascii=False preserved in the archive too
    finally:
        reopened.close()


def test_archived_domain_round_trips_canonical_fields(store: Store) -> None:
    old = store.add_domain(_domain(slug="payments", title="Payments", summary="v1 summary"))
    store.supersede_domain(
        old.domain_id,
        _domain(
            slug="payments",
            title="Payments v2",
            summary="v2 summary",
            supersedes=old.domain_id,
        ),
    )
    report = store.compact()
    assert report.domains_compacted == 1

    path = store.path
    store.close()
    reopened = Store(format1_store(path))
    try:
        got = reopened.get_domain(old.domain_id)
        assert got is not None
        assert got.title == "Payments"
        assert got.summary == "v1 summary"
        assert got.status == DomainStatus.SUPERSEDED
        assert got.communities == []  # volatile; cold default after a full reload
    finally:
        reopened.close()


# -- digest covers archive/ (absorbing an externally-added segment, e.g. a git pull) --------


def test_external_archive_segment_absorbed_on_reopen(tmp_path) -> None:
    store = Store(format1_store(tmp_path / "s"))
    store.close()

    now = datetime.now(UTC)
    teammates_decision = _decision(
        title="teammate's superseded decision",
        status=DecisionStatus.SUPERSEDED,
        valid_from=now - timedelta(days=1),
        valid_to=now,
    )
    archive_dir = tmp_path / "s" / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    line = json.dumps(
        {"record_type": "decision", **teammates_decision.model_dump(mode="json")},
        sort_keys=True,
        ensure_ascii=False,
    )
    (archive_dir / "2020-01-01-1.jsonl").write_text(line + "\n", encoding="utf-8")

    reopened = Store(format1_store(tmp_path / "s"))
    try:
        got = reopened.get_decision(teammates_decision.id)
        assert got is not None
        assert got.title == "teammate's superseded decision"
        assert reopened.get_meta("volatile_stale") == "1"
    finally:
        reopened.close()


# -- hot file always wins over a conflicting archive entry; both copies stay available -----


def test_reload_prefers_hot_file_over_conflicting_archive_entry_and_warns(tmp_path, capsys) -> None:
    store = Store(format1_store(tmp_path / "s"))
    d = store.add_decision(_decision(title="hot version"))
    store.close()

    conflicting = d.model_copy(update={"title": "stale archived version"})
    archive_dir = tmp_path / "s" / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    line = json.dumps(
        {"record_type": "decision", **conflicting.model_dump(mode="json")},
        sort_keys=True,
        ensure_ascii=False,
    )
    (archive_dir / "2020-01-01-1.jsonl").write_text(line + "\n", encoding="utf-8")

    reopened = Store(format1_store(tmp_path / "s"))
    try:
        got = reopened.get_decision(d.id)
        assert got.title == "hot version"  # hot always wins
    finally:
        reopened.close()
    err = capsys.readouterr().err
    assert "WARNING" in err
    assert d.id in err
    assert "shared values or structures differ; compare both copies" in err
    assert "stale archive" not in err


def test_compact_leaves_mismatched_leftover_hot_file_untouched_and_warns(
    store: Store, capsys
) -> None:
    old = store.add_decision(_decision(title="original"))
    store.add_decision(_decision(title="successor", supersedes=old.id))
    report1 = store.compact()
    assert report1.decisions_compacted == 1
    hot_path = _hot_decision_path(store, old.id)
    assert not hot_path.exists()

    # Recreate the hot file with a changed shared value, not crash debris (a real
    # crash-window duplicate is byte-identical).
    tampered = old.model_copy(update={"title": "tampered after archiving"})
    hot_path.write_text(
        json.dumps(tampered.model_dump(mode="json"), sort_keys=True, indent=2, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )

    report2 = store.compact()

    assert report2.cleaned_up_hot_files == 0  # mismatch -> NOT "safe to remove"
    assert hot_path.is_file()  # never destroyed
    assert json.loads(hot_path.read_text())["title"] == "tampered after archiving"
    err = capsys.readouterr().err
    assert "WARNING" in err
    assert old.id in err


@pytest.mark.parametrize(
    ("hot", "archived", "expected"),
    [
        ({"known": 1}, {"known": 1, "old_default": None}, None),
        ({"known": 1}, {"known": 1, "old_default": []}, None),
        ({"known": 1}, {"known": 1, "old_default": {}}, None),
        ({"known": 1, "extra": None}, {"known": 1}, "extra-fields"),
        ({"nested": {"extra": []}}, {"nested": {}}, "extra-fields"),
        ([{"extra": False}], [{}], "extra-fields"),
        ([{"extra": 0}], [{}], "extra-fields"),
        ({"known": 2}, {"known": 1}, "different-values"),
        ({"extra": None, "known": 2}, {"known": 1}, "different-values"),
        ({"a": {"x": None}}, {"a": None}, "different-values"),
        ([1, 2], [1], "different-values"),
        ({"a": {"x": 1}}, {"a": [1]}, "different-values"),
    ],
)
def test_hot_archive_difference_preserves_boolean_contract(hot, archived, expected):
    assert hot_archive_difference(hot, archived) == expected
    assert (hot_archive_difference(hot, archived) is None) is hot_matches_archive(hot, archived)


def test_compact_kept_leftover_count_ignores_fresh_age_and_status_filter(store: Store):
    old = store.add_decision(_decision(title="archived leftover"))
    store.add_decision(_decision(title="successor", supersedes=old.id))
    assert store.compact().decisions_compacted == 1

    hot_path = _hot_decision_path(store, old.id)
    payload = old.model_dump(mode="json")
    payload["status"] = DecisionStatus.ACCEPTED.value
    payload["valid_from"] = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    payload["valid_to"] = None
    hot_path.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")

    report = store.compact(older_than_days=365, dry_run=True)

    assert report.kept_hot_files == 1
    assert report.cleaned_up_hot_files == 0
    assert report.skipped_age_filtered == 0


def test_compact_skipped_hot_leftover_is_not_counted_as_kept(tmp_path):
    path = tmp_path / "s"
    store = Store(format1_store(path))
    old = store.add_decision(_decision(title="archived old"))
    store.add_decision(_decision(title="successor", supersedes=old.id))
    assert store.compact().decisions_compacted == 1
    hot_path = _hot_decision_path(store, old.id)
    hot_path.write_text(json.dumps({"id": "wrong-id"}) + "\n", encoding="utf-8")
    store.close()

    with Store(format1_store(path)) as reopened:
        report = reopened.compact(dry_run=True)
        assert report.kept_hot_files == 0
        assert report.cleaned_up_hot_files == 0
        assert hot_path.is_file()


def test_compact_reports_kept_domain_hot_file(store: Store, capsys):
    domain = store.add_domain(_domain(slug="kept-domain"))
    store.ratify_domains(drop=[domain.domain_id])
    hot_path = store.path / "domains" / f"{domain.domain_id}.json"
    payload = json.loads(hot_path.read_text(encoding="utf-8"))
    payload["future_field"] = False
    hot_path.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")

    dry_report = store.compact(dry_run=True)
    dry_warning = capsys.readouterr().err
    real_report = store.compact()

    assert dry_report.kept_hot_files == 1
    assert "prospective archive payload" in dry_warning
    assert "no archive segment has been published" in dry_warning
    assert "exists in BOTH" not in dry_warning
    assert real_report.kept_hot_files == 1
    assert hot_path.is_file()


# -- everything stays retrievable after compaction (the invariant guard) --------------------


def test_superseded_decision_and_bindings_fully_retrievable_after_compact_and_rebuild(
    store: Store,
) -> None:
    entity = store.upsert_entity(Entity(canonical_name="widget"))
    old = store.add_decision(
        _decision(title="old approach", context="ctx", choice="choice A", rejected="reason A")
    )
    store.add_binding(
        AnchorBinding(record_id=old.id, entity_id=entity.entity_id, tier=2, status="live")
    )
    store.add_decision(_decision(title="new approach", supersedes=old.id))

    report = store.compact()
    assert report.decisions_compacted == 1

    path = store.path
    store.close()
    reopened = Store(format1_store(path))
    try:
        got = reopened.get_decision(old.id)
        assert got is not None
        assert got.status == DecisionStatus.SUPERSEDED
        assert got.title == "old approach"
        assert got.choice == "choice A"
        assert got.rejected == "reason A"

        bindings = reopened.bindings_for_record(old.id)
        assert len(bindings) == 1
        assert bindings[0].entity_id == entity.entity_id

        tried_before = reopened.superseded_for_entity(entity.entity_id)
        assert any(dd.id == old.id for dd in tried_before)
    finally:
        reopened.close()


# -- sync/retrieval/ratify never trigger compaction implicitly ------------------------------


def test_compact_is_never_called_by_ratify_or_drop(store: Store, monkeypatch) -> None:
    calls = {"n": 0}
    real_compact = Store.compact

    def _spy(self, *a, **kw):
        calls["n"] += 1
        return real_compact(self, *a, **kw)

    monkeypatch.setattr(Store, "compact", _spy)

    proposed = store.add_decision(_decision(title="p"))
    store.ratify(proposed.id)
    proposed2 = store.add_decision(_decision(title="p2"))
    store.drop(proposed2.id)
    domain = store.add_domain(_domain(slug="x"))
    store.ratify_domains(accept=[domain.domain_id])

    assert calls["n"] == 0


# -- git-level: compact produces exactly the expected diff ----------------------------------


def _git(*args, cwd) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_compact_git_status_shows_only_removed_hot_files_and_new_segment(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", cwd=repo)

    store = Store(format1_store(repo / ".sidegraph"))
    old = store.add_decision(_decision(title="old"))
    store.add_decision(_decision(title="new", supersedes=old.id))
    store.close()

    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "seed store", cwd=repo)

    reopened = Store(format1_store(repo / ".sidegraph"))
    report = reopened.compact()
    reopened.close()
    assert report.segment_path is not None

    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    changed_paths = {line[3:].strip() for line in lines}
    # git's default (non--uall) porcelain output collapses a wholly-untracked new directory
    # to its own path rather than listing the file inside it -- ".sidegraph/archive/" here
    # is exactly (and only) the new segment ``report.segment_path`` points at.
    assert changed_paths == {
        f".sidegraph/decisions/{old.id}.json",
        ".sidegraph/archive/",
    }


# -- a hot file older than the schema's newest fields is not a mismatch -----------------------


def _strip_newer_keys(path) -> None:
    """Rewrite a hot file the way a version before the ratifier stamp and ``provenance.commit``
    wrote it: those keys are simply absent."""
    data = json.loads(path.read_text(encoding="utf-8"))
    data.pop("ratified_at", None)
    data.pop("ratified_by", None)
    data["provenance"].pop("commit", None)
    path.write_text(
        json.dumps(data, sort_keys=True, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def _old_decision(store: Store) -> Decision:
    old = store.add_decision(_decision(title="legacy"))
    store.add_decision(_decision(title="successor", supersedes=old.id))
    _strip_newer_keys(_hot_decision_path(store, old.id))
    return old


def test_compact_archives_a_hot_decision_that_lacks_newer_keys_without_a_warning(
    store: Store, capsys
) -> None:
    old = _old_decision(store)
    report = store.compact()
    assert report.decisions_compacted == 1
    assert not _hot_decision_path(store, old.id).exists()
    assert "DIFFERENT" not in capsys.readouterr().err
    path = store.path
    store.close()
    for f in path.glob("index.db*"):
        f.unlink()
    with Store(format1_store(path)) as fresh:
        assert fresh.get_decision(old.id).title == "legacy"
    assert "DIFFERENT" not in capsys.readouterr().err


def test_compact_archives_a_hot_domain_that_lacks_newer_keys_without_a_warning(
    store: Store, capsys
) -> None:
    dom = store.add_domain(_domain(slug="legacy-dom"))
    store.ratify_domains(drop=[dom.domain_id])
    _strip_newer_keys(_hot_domain_path(store, dom.domain_id))
    report = store.compact()
    assert report.domains_compacted == 1
    assert not _hot_domain_path(store, dom.domain_id).exists()
    assert "DIFFERENT" not in capsys.readouterr().err


def test_compact_keeps_and_warns_on_a_real_difference_in_a_legacy_shaped_hot_file(
    store: Store, capsys
) -> None:
    old = _old_decision(store)
    path = _hot_decision_path(store, old.id)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["choice"] = "edited after the fact"
    path.write_text(json.dumps(data, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    store.compact()
    assert path.is_file()
    err = capsys.readouterr().err
    assert "DIFFERENT" in err and old.id in err


def test_compact_keeps_and_warns_on_an_unknown_extra_key_holding_a_value(
    store: Store, capsys
) -> None:
    old = _old_decision(store)
    path = _hot_decision_path(store, old.id)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["from_a_newer_version"] = "keep me"
    path.write_text(json.dumps(data, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    store.compact()
    assert path.is_file()
    assert "from_a_newer_version" in path.read_text(encoding="utf-8")
    assert "DIFFERENT" in capsys.readouterr().err


def test_second_compact_cleans_a_leftover_that_differs_only_by_absent_keys(
    store: Store, capsys
) -> None:
    old = store.add_decision(_decision(title="legacy"))
    store.add_decision(_decision(title="successor", supersedes=old.id))
    hot = _hot_decision_path(store, old.id)
    original = hot.read_text(encoding="utf-8")
    store.compact()
    assert not hot.exists()
    hot.write_text(original, encoding="utf-8")
    _strip_newer_keys(hot)
    report = store.compact()
    assert report.cleaned_up_hot_files == 1
    assert not hot.exists()
    assert "DIFFERENT" not in capsys.readouterr().err


def test_second_compact_keeps_a_leftover_that_differs_for_real(store: Store, capsys) -> None:
    old = store.add_decision(_decision(title="legacy"))
    store.add_decision(_decision(title="successor", supersedes=old.id))
    hot = _hot_decision_path(store, old.id)
    original = hot.read_text(encoding="utf-8")
    store.compact()
    hot.write_text(original, encoding="utf-8")
    _strip_newer_keys(hot)
    data = json.loads(hot.read_text(encoding="utf-8"))
    data["title"] = "tampered"
    hot.write_text(json.dumps(data, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    report = store.compact()
    assert report.cleaned_up_hot_files == 0
    assert hot.is_file()
    assert "DIFFERENT" in capsys.readouterr().err


def test_reload_does_not_warn_for_a_hot_file_that_differs_only_by_absent_keys(
    store: Store, capsys
) -> None:
    old = store.add_decision(_decision(title="legacy"))
    store.add_decision(_decision(title="successor", supersedes=old.id))
    hot = _hot_decision_path(store, old.id)
    original = hot.read_text(encoding="utf-8")
    store.compact()
    path = store.path
    store.close()
    hot.write_text(original, encoding="utf-8")
    _strip_newer_keys(hot)
    for f in path.glob("index.db*"):
        f.unlink()
    capsys.readouterr()
    with Store(format1_store(path)) as fresh:
        assert fresh.get_decision(old.id).title == "legacy"
    assert "DIFFERENT" not in capsys.readouterr().err


# -- a key only the hot file holds always blocks the unlink ------------------------------------
# The archive line is a model dump, which drops keys the running version does not know, so a
# hot file from a newer version may hold a key the archive cannot. Keeping the file is the only
# way not to lose it, whatever the value is. Two values here (null, false); the value shapes
# are the cases of ``test_hot_archive_difference_preserves_boolean_contract``.


def _mutate_hot(path, fn) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    fn(data)
    path.write_text(json.dumps(data, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def _set(key, value):
    return lambda d: d.__setitem__(key, value)


_HOT_ONLY_KEYS = [
    pytest.param(_set("zz_extra", None), id="extra-null"),
    pytest.param(_set("zz_extra", False), id="extra-false"),
]


@pytest.mark.parametrize("mutate", _HOT_ONLY_KEYS)
def test_compact_keeps_a_hot_file_holding_a_key_the_archive_lacks(
    store: Store, capsys, mutate
) -> None:
    old = store.add_decision(_decision(title="newer-version"))
    store.add_decision(_decision(title="successor", supersedes=old.id))
    hot = _hot_decision_path(store, old.id)
    _mutate_hot(hot, mutate)
    store.compact()
    assert hot.is_file()
    assert old.id in capsys.readouterr().err


@pytest.mark.parametrize("mutate", _HOT_ONLY_KEYS)
def test_leftover_hot_file_holding_a_key_the_archive_lacks_is_kept(
    store: Store, capsys, mutate
) -> None:
    old = store.add_decision(_decision(title="newer-version"))
    store.add_decision(_decision(title="successor", supersedes=old.id))
    hot = _hot_decision_path(store, old.id)
    original = hot.read_text(encoding="utf-8")
    store.compact()
    hot.write_text(original, encoding="utf-8")
    _mutate_hot(hot, mutate)
    capsys.readouterr()
    report = store.compact()
    assert report.cleaned_up_hot_files == 0
    assert hot.is_file()
    assert old.id in capsys.readouterr().err


def test_compact_keeps_a_domain_whose_seed_anchor_holds_a_key_the_archive_lacks(
    store: Store, capsys
) -> None:
    dom = store.add_domain(_domain(slug="seeded", seed_anchors=[Descriptor(name="widget")]))
    store.ratify_domains(drop=[dom.domain_id])
    hot = _hot_domain_path(store, dom.domain_id)
    _mutate_hot(hot, lambda d: d["seed_anchors"][0].__setitem__("zz_extra", None))
    store.compact()
    assert hot.is_file()
    assert dom.domain_id in capsys.readouterr().err


def test_leftover_hot_domain_with_a_hot_only_key_is_kept(store: Store, capsys) -> None:
    dom = store.add_domain(_domain(slug="seeded2", seed_anchors=[Descriptor(name="widget")]))
    store.ratify_domains(drop=[dom.domain_id])
    hot = _hot_domain_path(store, dom.domain_id)
    original = hot.read_text(encoding="utf-8")
    store.compact()
    hot.write_text(original, encoding="utf-8")
    _mutate_hot(hot, lambda d: d["seed_anchors"][0].__setitem__("zz_extra", None))
    capsys.readouterr()
    report = store.compact()
    assert report.cleaned_up_hot_files == 0
    assert hot.is_file()
    assert dom.domain_id in capsys.readouterr().err


def test_leftover_with_an_empty_string_against_an_archived_null_is_kept(
    store: Store, capsys
) -> None:
    # `""` is a value, not an empty shape: a mutant treating every falsy value as empty
    # would clean this file.
    old = store.add_decision(_decision(title="legacy"))
    store.add_decision(_decision(title="successor", supersedes=old.id))
    hot = _hot_decision_path(store, old.id)
    original = hot.read_text(encoding="utf-8")
    store.compact()
    hot.write_text(original, encoding="utf-8")
    _mutate_hot(hot, lambda d: d.__setitem__("ratified_by", ""))
    report = store.compact()
    assert report.cleaned_up_hot_files == 0
    assert hot.is_file()
    assert old.id in capsys.readouterr().err


def test_reload_does_not_warn_for_a_hot_domain_that_differs_only_by_absent_keys(
    store: Store, capsys
) -> None:
    dom = store.add_domain(_domain(slug="legacy-dom2"))
    store.ratify_domains(drop=[dom.domain_id])
    hot = _hot_domain_path(store, dom.domain_id)
    original = hot.read_text(encoding="utf-8")
    store.compact()
    path = store.path
    store.close()
    hot.write_text(original, encoding="utf-8")
    _strip_newer_keys(hot)
    for f in path.glob("index.db*"):
        f.unlink()
    capsys.readouterr()
    with Store(format1_store(path)) as fresh:
        assert fresh.get_domain(dom.domain_id).slug == "legacy-dom2"
    assert "DIFFERENT" not in capsys.readouterr().err


# == the N5 compaction code-review fix wave (team-lead review of commit 061e913): 2 Important +
# 5 Minor findings; each test below is cross-referenced to its finding in a comment ==========


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
    reopened = Store(format1_store(path))
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
    store_a = Store(format1_store(tmp_path / "a"))
    old_a = store_a.add_decision(_decision(title="branch A decision"))
    store_a.add_decision(_decision(title="branch A succ", supersedes=old_a.id))
    report_a = store_a.compact()
    store_a.close()

    store_b = Store(format1_store(tmp_path / "b"))
    old_b = store_b.add_decision(_decision(title="branch B decision"))
    store_b.add_decision(_decision(title="branch B succ", supersedes=old_b.id))
    report_b = store_b.compact()
    store_b.close()

    name_a = Path(report_a.segment_path).name
    name_b = Path(report_b.segment_path).name
    assert name_a != name_b  # different content -> different filenames -> no git conflict


def test_identical_content_same_day_produces_identical_name_and_bytes(tmp_path) -> None:
    fixed_time = datetime(2026, 1, 1, tzinfo=UTC)

    def _seed_and_compact(store_path):
        s = Store(format1_store(store_path))
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
    store = Store(format1_store(tmp_path / "s"))
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

    reopened = Store(format1_store(tmp_path / "s"))
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
    store = Store(format1_store(tmp_path / "s"))
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

    # The reload tolerates the line and the store opens; the compaction, a mutation, does not
    # run over a segment it cannot fully read (design/superpowers/specs/
    # 2026-10-03-store-survives-a-bad-file-design.md D4, T11).
    with Store(format1_store(tmp_path / "s")) as reopened, pytest.raises(ValueError) as exc_info:
        reopened.compact()
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


def test_compact_dry_run_after_hot_becomes_unparseable_is_read_only(store: Store) -> None:
    old = store.add_decision(_decision(title="invalid-dry-run-old"))
    store.add_decision(_decision(title="invalid-dry-run-successor", supersedes=old.id))
    hot_path = _hot_decision_path(store, old.id)
    hot_path.write_text("not json\n", encoding="utf-8")
    invalid_bytes = hot_path.read_bytes()
    digest_before = store.get_meta("canonical_digest")

    with pytest.raises(ValueError):
        store.compact(dry_run=True)

    assert hot_path.read_bytes() == invalid_bytes
    assert not (store.path / "archive").exists()
    assert store.get_meta("canonical_digest") == digest_before


# == Minor-7: domains excluded under --older-than get their own report field + CLI wording ==


def test_report_separates_domain_age_exclusion_from_decision_skips(store: Store) -> None:
    dropped_domain = store.add_domain(_domain(slug="d-dropped"))
    store.ratify_domains(drop=[dropped_domain.domain_id])

    report = store.compact(older_than_days=30, dry_run=True)
    assert report.domains_excluded_age_unknown == 1
    assert report.skipped_age_filtered == 0


def test_cli_compact_older_than_reports_domain_exclusion_separately(tmp_path, capsys) -> None:
    db = tmp_path / "t.db"
    s = Store(format1_store(db))
    dom = s.add_domain(_domain(slug="d-dropped"))
    s.ratify_domains(drop=[dom.domain_id])
    s.close()

    assert compact_main(["--db", str(db), "--older-than", "30"]) == 0
    out = capsys.readouterr().out
    assert "1 domain(s) excluded: terminal age unknown" in out
