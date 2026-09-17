"""Compaction (see docs/reference/store-format.md#archive-segments-sidegraph-compact):
terminal-status decisions/domains pack into an immutable ``archive/<date>-<seq>.jsonl``
segment and their hot canonical files are removed in the same operation. CLAUDE.md
invariant #2 (append-only) still holds throughout: records MOVE, they are never lost or
mutated -- every test here proves that some way (round-trip fidelity, retrievability,
crash-window safety, or a hand-tampered hot file that must never be silently destroyed)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import UTC, datetime, timedelta

import pytest

from sidegraph.schema import (
    AnchorBinding,
    Decision,
    DecisionKind,
    DecisionStatus,
    Domain,
    DomainStatus,
    Entity,
    Provenance,
)
from sidegraph.store import Store


@pytest.fixture
def store(tmp_path) -> Store:
    with Store(tmp_path / "s") as s:
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
    reopened = Store(path)
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
    reopened = Store(path)
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
    store = Store(tmp_path / "s")
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

    reopened = Store(tmp_path / "s")
    try:
        got = reopened.get_decision(teammates_decision.id)
        assert got is not None
        assert got.title == "teammate's superseded decision"
        assert reopened.get_meta("volatile_stale") == "1"
    finally:
        reopened.close()


# -- hot file always wins over a conflicting archive entry (corruption-shaped, never
# destroy data) -----------------------------------------------------------------------------


def test_reload_prefers_hot_file_over_conflicting_archive_entry_and_warns(tmp_path, capsys) -> None:
    store = Store(tmp_path / "s")
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

    reopened = Store(tmp_path / "s")
    try:
        got = reopened.get_decision(d.id)
        assert got.title == "hot version"  # hot always wins
    finally:
        reopened.close()
    err = capsys.readouterr().err
    assert "WARNING" in err
    assert d.id in err


def test_compact_leaves_mismatched_leftover_hot_file_untouched_and_warns(
    store: Store, capsys
) -> None:
    old = store.add_decision(_decision(title="original"))
    store.add_decision(_decision(title="successor", supersedes=old.id))
    report1 = store.compact()
    assert report1.decisions_compacted == 1
    hot_path = _hot_decision_path(store, old.id)
    assert not hot_path.exists()

    # Recreate the hot file with DIFFERENT content than the archived copy -- corruption,
    # not crash debris (a real crash-window duplicate is byte-identical).
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
    reopened = Store(path)
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

    store = Store(repo / ".sidegraph")
    old = store.add_decision(_decision(title="old"))
    store.add_decision(_decision(title="new", supersedes=old.id))
    store.close()

    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "seed store", cwd=repo)

    reopened = Store(repo / ".sidegraph")
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
