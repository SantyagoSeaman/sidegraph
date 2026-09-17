"""Git-native store persistence guarantees (see docs/reference/store-format.md): atomic
canonical writes, and index freshness against the canonical files on disk (a git pull
bringing new/changed files, or a deleted/missing index.db)."""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sidegraph.schema import (
    SCHEMA_VERSION,
    AnchorBinding,
    Decision,
    DecisionKind,
    DecisionStatus,
    Entity,
    Provenance,
)
from sidegraph.store import Store


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


def _age(path: Path, seconds: float) -> None:
    """Back-date ``path``'s mtime by ``seconds`` -- past the sweep's
    ``_TMP_DEBRIS_MIN_AGE_SECONDS`` gate (design D4), so a fixture that only LOOKS like
    crash debris (a stale-sounding name) actually behaves like it to the age-gated sweep."""
    t = time.time() - seconds
    os.utime(path, (t, t))


# -- atomic canonical writes (tmp + os.replace) -------------------------------------------


def test_atomic_write_crash_between_tmp_and_replace_leaves_original_intact(tmp_path, monkeypatch):
    store = Store(tmp_path / "s")
    d = store.add_decision(_decision(title="original"))
    path = store.path / "decisions" / f"{d.id}.json"
    original_text = path.read_text()
    assert json.loads(original_text)["title"] == "original"

    # Simulate a crash strictly between the tmp write and the atomic replace: the tmp file
    # lands with the NEW content, but the swap into place never happens.
    import sidegraph.store as store_module

    real_replace = store_module.os.replace

    def _boom(src, dst):
        raise OSError("simulated crash between tmp write and replace")

    monkeypatch.setattr(store_module.os, "replace", _boom)
    with pytest.raises(OSError, match="simulated crash"):
        store.add_decision(_decision(title="should not land"))
    monkeypatch.setattr(store_module.os, "replace", real_replace)

    # The original (unrelated) decision file is untouched -- old content survives the
    # crash window, and the never-swapped-in write never becomes visible.
    assert path.read_text() == original_text
    assert store.get_decision(d.id).title == "original"
    titles = {x.title for x in store.iter_decisions()}
    assert "should not land" not in titles


def test_atomic_write_leaves_no_tmp_file_on_success(tmp_path):
    """The exact-name assertion this used to make (``<id>.json.tmp``) stopped being able to
    fail once _atomic_write_text started giving every write a unique tmp name (design D5) --
    that fixed name is never written again, so it would pass forever even against a leaking
    implementation. Assert on a glob over the record dir instead."""
    store = Store(tmp_path / "s")
    store.add_decision(_decision())
    assert list((store.path / "decisions").glob("*.tmp")) == []


# -- digest freshness: absorbing external changes (e.g. a git pull) ----------------------


def test_external_file_added_is_absorbed_on_open_and_marks_volatile_stale(tmp_path):
    store = Store(tmp_path / "s")
    d1 = store.add_decision(_decision(title="mine"))
    store.close()

    # Simulate a teammate's branch merging in a NEW decision file via plain git — never
    # touching this process's Store API at all.
    teammate = _decision(title="teammate's decision")
    (tmp_path / "s" / "decisions" / f"{teammate.id}.json").write_text(
        json.dumps(teammate.model_dump(mode="json"), sort_keys=True, indent=2) + "\n"
    )

    reopened = Store(tmp_path / "s")
    try:
        assert reopened.get_decision(d1.id) is not None
        got = reopened.get_decision(teammate.id)
        assert got is not None and got.title == "teammate's decision"
        assert reopened.get_meta("volatile_stale") == "1"
    finally:
        reopened.close()


def test_reopen_with_no_external_change_is_a_fast_noop(tmp_path):
    store = Store(tmp_path / "s")
    store.add_decision(_decision())
    store.set_meta("volatile_stale", "0")  # simulate a completed sync clearing the flag
    store.close()

    reopened = Store(tmp_path / "s")
    try:
        # Nothing external changed -> the digest still matches -> the fast path never
        # touches volatile_stale.
        assert reopened.get_meta("volatile_stale") == "0"
    finally:
        reopened.close()


def test_hand_corrupted_schema_version_on_a_fresh_digest_still_rejected(tmp_path):
    """A canonical-directory store's index can go stale in ways a digest match alone can't
    catch (e.g. someone hand-edits ONLY the meta table, never touching a canonical file) --
    the schema_version stamp is still cross-checked on the fast (digest-match) path."""
    store = Store(tmp_path / "s")
    store.close()

    import sqlite3

    conn = sqlite3.connect(str(tmp_path / "s" / "index.db"))
    conn.execute("UPDATE meta SET value = 'bogus' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()

    with pytest.raises(ValueError, match="schema_version"):
        Store(tmp_path / "s")


# -- index deleted -> full rebuild from canonical files -----------------------------------


def test_index_deleted_rebuilds_equal_canonical_state(tmp_path):
    store = Store(tmp_path / "s")
    e = store.upsert_entity(Entity(canonical_name="sidegraph.store"))
    d = store.add_decision(_decision())
    store.add_binding(AnchorBinding(record_id=d.id, entity_id=e.entity_id, tier=2))
    store.close()

    (tmp_path / "s" / "index.db").unlink()

    reopened = Store(tmp_path / "s")
    try:
        assert reopened.get_decision(d.id) is not None
        assert reopened.get_decision(d.id).title == d.title
        got_entity = reopened.get_entity(e.entity_id)
        assert got_entity is not None and got_entity.canonical_name == "sidegraph.store"
        bindings = reopened.bindings_for_record(d.id)
        assert len(bindings) == 1 and bindings[0].entity_id == e.entity_id
        assert reopened.get_meta("volatile_stale") == "1"
    finally:
        reopened.close()


def test_index_deleted_resets_volatile_fields_to_cold_defaults(tmp_path):
    """Entity engine-mapping and binding status are index-only (never in the canonical
    files), so a from-scratch index rebuild can only recover their COLD defaults -- the
    next sync re-derives them from the live graph (design §3)."""
    store = Store(tmp_path / "s")
    e = store.upsert_entity(Entity(canonical_name="x", last_seen_node_id="n1"))
    d = store.add_decision(_decision())
    store.add_binding(
        AnchorBinding(record_id=d.id, entity_id=e.entity_id, tier=2, status="orphaned")
    )
    assert store.get_entity(e.entity_id).last_seen_node_id == "n1"
    assert store.bindings_for_record(d.id)[0].status == "orphaned"
    store.close()

    (tmp_path / "s" / "index.db").unlink()

    reopened = Store(tmp_path / "s")
    try:
        assert reopened.get_entity(e.entity_id).last_seen_node_id is None
        assert reopened.bindings_for_record(d.id)[0].status == "live"
    finally:
        reopened.close()


# -- canonical write order: successor written BEFORE predecessor is flipped --------------


def test_add_decision_close_predecessor_writes_successor_before_flipping_predecessor(
    tmp_path, monkeypatch
):
    """CRITICAL-adjacent regression: add_decision's close_predecessor path must write the
    SUCCESSOR canonically before flipping the predecessor's status -- the old (buggy) order
    flipped+wrote the predecessor to SUPERSEDED first, so a successor-write failure left a
    durable "superseded with zero successors" record on disk (the next digest reload adopts
    it as-is; its slug/id becomes unresolvable). Mirrors ratify()'s write order: a failure
    must instead leave the tolerated deferred shape -- both records live, successor simply
    absent."""
    store = Store(tmp_path / "s")
    old = store.add_decision(_decision(title="predecessor"))

    new = _decision(title="successor", supersedes=old.id)

    import sidegraph.store as store_module

    real_write_decision_canonical = store_module.Store._write_decision_canonical

    def _boom(self, decision):
        if decision.id == new.id:
            raise OSError("simulated failure writing successor")
        return real_write_decision_canonical(self, decision)

    monkeypatch.setattr(store_module.Store, "_write_decision_canonical", _boom)
    with pytest.raises(OSError, match="simulated failure writing successor"):
        store.add_decision(new)
    monkeypatch.setattr(
        store_module.Store, "_write_decision_canonical", real_write_decision_canonical
    )

    path = store.path
    store.close()
    reopened = Store(path)
    try:
        reloaded_old = reopened.get_decision(old.id)
        assert reloaded_old.status == DecisionStatus.PROPOSED  # unchanged, never flipped
        assert reloaded_old.valid_to is None
        assert reopened.get_decision(new.id) is None  # successor never landed
    finally:
        reopened.close()


# -- readability: non-ASCII prose survives verbatim on disk ------------------------------


def test_cyrillic_and_em_dash_round_trip_and_appear_verbatim_on_disk(tmp_path):
    text = "решение — с грабли́ми"
    store = Store(tmp_path / "s")
    d = store.add_decision(
        _decision(title=text, context=f"context: {text}", choice=f"choice: {text}")
    )

    reloaded = store.get_decision(d.id)
    assert reloaded.title == text

    path = store.path / "decisions" / f"{d.id}.json"
    raw_bytes = path.read_bytes()
    assert text.encode("utf-8") in raw_bytes
    assert b"\\u" not in raw_bytes  # never \u-escaped


# -- committed format marker (.sidegraph/format) ------------------------------------------


def test_fresh_store_writes_format_marker(tmp_path):
    store = Store(tmp_path / "s")
    try:
        marker = store.path / "format"
        assert marker.is_file()
        assert marker.read_text() == f"sidegraph-store {SCHEMA_VERSION}\n"
    finally:
        store.close()


def test_missing_marker_on_existing_layout_is_backfilled(tmp_path):
    """N1-era stores predate the format marker -- opening one must write it rather than
    reject it."""
    store = Store(tmp_path / "s")
    store.close()

    marker = tmp_path / "s" / "format"
    marker.unlink()
    assert not marker.exists()

    reopened = Store(tmp_path / "s")
    try:
        assert marker.read_text() == f"sidegraph-store {SCHEMA_VERSION}\n"
    finally:
        reopened.close()


def test_unknown_major_format_marker_hard_rejected(tmp_path):
    store = Store(tmp_path / "s")
    store.close()

    marker = tmp_path / "s" / "format"
    marker.write_text("sidegraph-store 9.9.9\n")

    with pytest.raises(ValueError, match="format"):
        Store(tmp_path / "s")


def test_format_marker_write_goes_through_atomic_replace(tmp_path, monkeypatch):
    """Fold-in (review residual): the marker write must go through tmp + os.replace, not a
    bare write_text -- a crash strictly between the two must leave NO marker at all on a
    fresh store (never a truncated one that would then hard-reject every later open)."""
    import sidegraph.store as store_module

    real_replace = store_module.os.replace

    def _boom(src, dst):
        raise OSError("simulated crash between tmp write and replace")

    monkeypatch.setattr(store_module.os, "replace", _boom)
    with pytest.raises(OSError, match="simulated crash"):
        Store(tmp_path / "s")
    monkeypatch.setattr(store_module.os, "replace", real_replace)

    marker = tmp_path / "s" / "format"
    # The tmp name is per-attempt-unique (`format.<pid>.<random>.tmp`, never a fixed
    # `format.tmp` -- see _atomic_write_text_race_tolerant), so every attempt this crash
    # simulation triggered leaves its OWN leftover; glob for them rather than one fixed name.
    tmp_leftovers = list((tmp_path / "s").glob("format.*.tmp"))
    assert not marker.exists()  # never swapped into place -- proves it wasn't a bare write
    assert tmp_leftovers  # the tmp artifact IS there -- proves tmp+replace ran at all

    # This fixture never actually simulated staleness -- only a stale-looking NAME -- and
    # the sweep is now age-gated (design D4): a leftover only seconds old is indistinguishable
    # from a live in-flight buffer and must survive. Age it past the gate so the recovery
    # reopen's sweep is finally checking what this test's assertion claims.
    for leftover in tmp_leftovers:
        _age(leftover, 3600)

    # A retry succeeds: the leftover .tmp(s) are swept, and the marker writes cleanly.
    store = Store(tmp_path / "s")
    try:
        assert marker.is_file()
        assert marker.read_text() == f"sidegraph-store {SCHEMA_VERSION}\n"
        assert list((tmp_path / "s").glob("format.*.tmp")) == []
    finally:
        store.close()


# -- concurrent first-open race tolerance (review Important-2a) --------------------------
#
# _ensure_format_marker/_ensure_gitignore write to a FIXED tmp filename (there's exactly
# one correct answer, so no need for a unique-per-caller name) -- but that means two
# threads/processes racing a store's FIRST open can have the loser's os.replace raise
# FileNotFoundError once the winner's replace already consumed the shared tmp file. Since
# both writers always produce IDENTICAL content, losing that race must be treated as
# success, not a crash.


def test_atomic_write_text_race_tolerant_swallows_lost_race(tmp_path, monkeypatch):
    import sidegraph.store as store_module
    from sidegraph.store import _atomic_write_text_race_tolerant

    target = tmp_path / "shared-target"

    def _fake_replace(src, dst):
        # Simulate a concurrent winner: the target already exists with the SAME content
        # this call was about to write, and the shared tmp file it depended on is gone.
        Path(dst).write_text(Path(src).read_text())
        raise FileNotFoundError(f"simulated: {src} already consumed by concurrent opener")

    monkeypatch.setattr(store_module.os, "replace", _fake_replace)
    _atomic_write_text_race_tolerant(target, "content\n")  # must not raise
    assert target.read_text() == "content\n"


def test_atomic_write_text_race_tolerant_reraises_when_target_missing(tmp_path, monkeypatch):
    import sidegraph.store as store_module
    from sidegraph.store import _atomic_write_text_race_tolerant

    target = tmp_path / "shared-target"

    def _boom(src, dst):
        raise FileNotFoundError("simulated: genuinely missing, not a lost race")

    monkeypatch.setattr(store_module.os, "replace", _boom)
    with pytest.raises(FileNotFoundError, match="genuinely missing"):
        _atomic_write_text_race_tolerant(target, "content\n")


def test_format_marker_write_tolerates_lost_concurrent_first_open_race(tmp_path, monkeypatch):
    """End-to-end: Store(...)'s first-ever open must not crash when it loses this race --
    the format marker write is the first os.replace call in __init__."""
    import sidegraph.store as store_module

    real_replace = store_module.os.replace
    calls = {"n": 0}

    def _race_once(src, dst):
        calls["n"] += 1
        if calls["n"] == 1:
            Path(dst).write_text(Path(src).read_text())
            Path(src).unlink()
            raise FileNotFoundError(f"simulated: {src} already consumed by concurrent opener")
        return real_replace(src, dst)

    monkeypatch.setattr(store_module.os, "replace", _race_once)
    store = Store(tmp_path / "s")  # must NOT raise
    try:
        marker = store.path / "format"
        assert marker.is_file()
        assert marker.read_text() == f"sidegraph-store {SCHEMA_VERSION}\n"
    finally:
        store.close()


def test_gitignore_write_tolerates_lost_concurrent_first_open_race(tmp_path, monkeypatch):
    """Same race tolerance as the format marker, for .gitignore -- forced by pre-seeding a
    format marker so _ensure_gitignore's os.replace is the first one __init__ makes."""
    import sidegraph.store as store_module

    store_dir = tmp_path / "s"
    store_dir.mkdir()
    (store_dir / "format").write_text(f"sidegraph-store {SCHEMA_VERSION}\n")

    real_replace = store_module.os.replace
    calls = {"n": 0}

    def _race_once(src, dst):
        calls["n"] += 1
        if calls["n"] == 1:
            Path(dst).write_text(Path(src).read_text())
            Path(src).unlink()
            raise FileNotFoundError(f"simulated: {src} already consumed by concurrent opener")
        return real_replace(src, dst)

    monkeypatch.setattr(store_module.os, "replace", _race_once)
    store = Store(store_dir)  # must NOT raise
    try:
        gitignore = store.path / ".gitignore"
        assert gitignore.is_file()
        assert gitignore.read_text() == "index.db*\n*.tmp\n"
    finally:
        store.close()


# -- committed .gitignore (index.db*, *.tmp) ------------------------------------------


def test_fresh_store_writes_gitignore(tmp_path):
    store = Store(tmp_path / "s")
    try:
        gitignore = store.path / ".gitignore"
        assert gitignore.is_file()
        assert gitignore.read_text() == "index.db*\n*.tmp\n"
    finally:
        store.close()


def test_existing_gitignore_is_never_overwritten(tmp_path):
    store_dir = tmp_path / "s"
    store_dir.mkdir()
    gitignore = store_dir / ".gitignore"
    gitignore.write_text("# hand-edited by a teammate\ncustom-pattern\n")

    store = Store(store_dir)
    try:
        assert gitignore.read_text() == "# hand-edited by a teammate\ncustom-pattern\n"
    finally:
        store.close()


def test_gitignore_backfilled_on_existing_layout_missing_it(tmp_path):
    """N1-era stores predate the .gitignore convention -- opening one must write it, same
    as the format marker's own backfill story."""
    store = Store(tmp_path / "s")
    store.close()

    gitignore = tmp_path / "s" / ".gitignore"
    gitignore.unlink()
    assert not gitignore.exists()

    reopened = Store(tmp_path / "s")
    try:
        assert gitignore.read_text() == "index.db*\n*.tmp\n"
    finally:
        reopened.close()


def test_gitignore_write_leaves_no_tmp_file_on_success(tmp_path):
    store = Store(tmp_path / "s")
    try:
        assert not (store.path / ".gitignore.tmp").exists()
    finally:
        store.close()


# -- tmp hygiene: stale *.tmp files swept on open -----------------------------------------


def test_stale_tmp_file_swept_on_open(tmp_path):
    store = Store(tmp_path / "s")
    d = store.add_decision(_decision())
    store.close()

    # This fixture never actually simulated staleness -- only a stale-looking NAME -- and
    # the sweep is now age-gated (design D4), so a fresh mtime would survive the reopen
    # below. Ageing it is this test finally checking what its name claims.
    stale_tmp = tmp_path / "s" / "decisions" / "leftover-from-a-crash.json.tmp"
    stale_tmp.write_text("{}")
    _age(stale_tmp, 3600)
    assert stale_tmp.exists()

    reopened = Store(tmp_path / "s")
    try:
        assert not stale_tmp.exists()
        assert reopened.get_decision(d.id) is not None
    finally:
        reopened.close()


def test_stale_tmp_file_in_store_root_swept_on_open(tmp_path):
    """The sweep covers the store ROOT too, not just the canonical record dirs -- the
    format marker and .gitignore are atomic-written there (see
    test_format_marker_write_goes_through_atomic_replace). Uses a realistic per-attempt
    unique tmp name (``format.<pid>.<random>.tmp``, matching what
    ``_atomic_write_text_race_tolerant`` actually produces), not the old fixed
    ``format.tmp``."""
    store = Store(tmp_path / "s")
    store.close()

    # This fixture never actually simulated staleness -- only a stale-looking NAME -- and
    # the sweep is now age-gated (design D4), so a fresh mtime would survive the reopen
    # below. Ageing it is this test finally checking what its name claims.
    stale_tmp = tmp_path / "s" / "format.12345.deadbeef0000.tmp"
    stale_tmp.write_text("garbage")
    _age(stale_tmp, 3600)
    assert stale_tmp.exists()

    reopened = Store(tmp_path / "s")
    try:
        assert not stale_tmp.exists()
        assert (tmp_path / "s" / "format").is_file()
    finally:
        reopened.close()


def test_foreign_root_tmp_file_survives_open(tmp_path):
    """Review finding (Minor 3): the root-level sweep must only remove the store's OWN
    tmp artifacts (``format.*.tmp`` / ``.gitignore.*.tmp`` -- see
    ``_ROOT_TMP_GLOB_PATTERNS``) -- never a blanket ``*.tmp`` glob, which would delete an
    unrelated file a user happens to have sitting in the store root (observed in practice
    as the same bug behind Important-1: a bare SIDEGRAPH_DB default turning the repo root
    into the store clobbered a user's build-artifact.tmp). The glob sweep stays scoped to
    the 5 canonical record subdirs only."""
    store = Store(tmp_path / "s")
    store.close()

    foreign = tmp_path / "s" / "render.tmp"
    foreign.write_text("not ours")

    reopened = Store(tmp_path / "s")
    try:
        assert foreign.exists()
        assert foreign.read_text() == "not ours"
    finally:
        reopened.close()
