"""``Store`` on a format-2 store: the reload reads the occurrence inventory (shard lines, per-record
files, legacy archive lines), the digest moves with every shard file, a quarantined identity is
skipped per ``(type, id)``, and a major-1 store reloads exactly as before.
# see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§5.3, §7.2, §7.3; T-L1)
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import pytest
from ulid import ULID

import sidegraph.store as store_module
from sidegraph import store_layout
from sidegraph.host.hooks import _store_holds_records
from sidegraph.schema import validate_record_body
from sidegraph.store import Store
from sidegraph.store_format import inventory_from_disk
from tests.store_format2_support import (
    bindings_body,
    decision_body,
    domain_body,
    entity_body,
    fact_body,
    make_store,
    put_archive,
    put_file,
    put_shard_line,
)


def _id() -> str:
    return str(ULID())


def _stat_rows(store_path: Path) -> dict[tuple[str, str], tuple[int, int]]:
    conn = sqlite3.connect(store_path / "index.db")
    try:
        rows = conn.execute("SELECT subdir, stem, size, mtime_ns FROM canonical_stat").fetchall()
    finally:
        conn.close()
    return {(r[0], r[1]): (r[2], r[3]) for r in rows}


# -- T-L1: every type is served from shards alone ---------------------------------------------


def test_store_serves_packed_records_by_type(tmp_path):
    store = make_store(tmp_path)
    ent, dec, fct, dom = _id(), _id(), _id(), _id()
    put_shard_line(store, "entity", ent, entity_body(ent, descriptor={"name": "f"}))
    put_shard_line(store, "decision", dec, decision_body(dec))
    put_shard_line(store, "fact", fct, fact_body(fct, supports=[dec]))
    put_shard_line(store, "domain", dom, domain_body(dom))
    put_shard_line(store, "bindings", dec, bindings_body(ent))
    with Store(store) as s:
        found = s.get_entity(ent)
        assert found is not None and found.descriptor is not None
        assert found.descriptor.name == "f"
        decision = s.get_decision(dec)
        assert decision is not None and decision.title == f"decision {dec}"
        fact = s.get_fact(fct)
        assert fact is not None and fact.supports == [dec]
        domain = s.get_domain(dom)
        assert domain is not None and domain.slug == f"domain-{dom.lower()}"
        assert [b.entity_id for b in s.bindings_for_record(dec)] == [ent]
        assert s._read_skip_list() == []


_VALID_TO = "2026-02-01T00:00:00Z"


def _archive_line(record_type: str, body: dict) -> dict:
    return {"record_type": record_type, **body}


def _served_twice(store: Path, check) -> None:
    """Run ``check(Store)`` on a cold open, then again after ``index.db`` is deleted (a cold
    rebuild), so the answer cannot come from index state a previous open left."""
    with Store(store) as s:
        check(s)
    for stale in store.glob("index.db*"):
        stale.unlink()
    with Store(store) as s:
        check(s)


def test_effective_record_matrix_through_a_store(tmp_path):
    """T-L1, rule 4 and rule 1 end to end: file > shard > archive; a superseding archive line
    beats a shard line, never a file; a compatible one does not; a bindings file replaces the
    shard line (no union); an unreadable file leaves no record; two differing shard lines
    quarantine, equal ones collapse; a decision and a fact may share an id."""
    store = make_store(tmp_path)
    f_s_a, s_a_compat, s_a_super, f_a_super = _id(), _id(), _id(), _id()
    bad_file, dup_diff, dup_same, shared = _id(), _id(), _id(), _id()
    owner, e1, e2, archive_only = _id(), _id(), _id(), _id()
    # file beats shard beats archive
    put_shard_line(store, "decision", f_s_a, decision_body(f_s_a, title="shard"))
    put_file(store, "decision", decision_body(f_s_a, title="file"))
    put_archive(
        store,
        "2020-01-01-1-aaaaaaaaaaaa",
        [
            _archive_line(
                "decision",
                decision_body(f_s_a, title="archive", status="rejected", valid_to=_VALID_TO),
            ),
            _archive_line(
                "decision",
                decision_body(s_a_compat, title="same", status="rejected", valid_to=_VALID_TO),
            ),
            _archive_line(
                "decision",
                decision_body(s_a_super, title="x", status="superseded", valid_to=_VALID_TO),
            ),
            _archive_line(
                "decision",
                decision_body(f_a_super, title="x", status="superseded", valid_to=_VALID_TO),
            ),
            _archive_line(
                "decision", decision_body(bad_file, status="rejected", valid_to=_VALID_TO)
            ),
            _archive_line(
                "decision",
                decision_body(
                    archive_only, title="archived", status="rejected", valid_to=_VALID_TO
                ),
            ),
        ],
    )
    # a compatible archive line does not beat the shard line (same state)
    put_shard_line(
        store,
        "decision",
        s_a_compat,
        decision_body(
            s_a_compat, title="same", status="rejected", valid_to=_VALID_TO, consequences="kept"
        ),
    )
    # a superseding archive line beats an older shard state, but never a file
    put_shard_line(
        store, "decision", s_a_super, decision_body(s_a_super, title="x", status="accepted")
    )
    put_shard_line(
        store, "decision", f_a_super, decision_body(f_a_super, title="x", status="accepted")
    )
    put_file(store, "decision", decision_body(f_a_super, title="x", status="accepted"))
    # an unreadable file is a gap: no fall-through to the shard line or the archive line
    put_shard_line(store, "decision", bad_file, decision_body(bad_file, title="shard"))
    put_file(store, "decision", decision_body(bad_file)).write_text("{ not json", encoding="utf-8")
    # two differing shard lines quarantine; two equal ones are one occurrence
    put_shard_line(store, "decision", dup_diff, decision_body(dup_diff, title="one"))
    put_shard_line(store, "decision", dup_diff, decision_body(dup_diff, title="two"))
    put_shard_line(store, "decision", dup_same, decision_body(dup_same))
    put_shard_line(store, "decision", dup_same, decision_body(dup_same))
    # a decision and a fact with one id are two identities
    put_shard_line(store, "decision", shared, decision_body(shared, title="the decision"))
    put_shard_line(store, "fact", shared, fact_body(shared))
    # a bindings file replaces the shard line's list, it does not add to it
    put_shard_line(store, "decision", owner, decision_body(owner))
    put_shard_line(store, "bindings", owner, bindings_body(e1))
    put_file(store, "bindings", bindings_body(e2), record_id=owner)

    def check(s: Store) -> None:
        assert s.get_decision(f_s_a).title == "file"
        assert s.get_decision(s_a_compat).title == "same"
        assert s.get_decision(s_a_compat).status.value == "rejected"
        assert s.get_decision(s_a_compat).consequences == "kept"  # the shard's, not the archive's
        assert s.get_decision(archive_only).title == "archived"
        assert s.get_decision(s_a_super).status.value == "superseded"
        assert s.get_decision(f_a_super).status.value == "accepted"
        assert s.get_decision(bad_file) is None and s.is_skipped("decisions", bad_file)
        assert s.get_decision(dup_diff) is None and s.is_skipped("decisions", dup_diff)
        assert s.get_decision(dup_same) is not None and not s.is_skipped("decisions", dup_same)
        assert s.get_decision(shared).title == "the decision"
        assert s.get_fact(shared) is not None
        assert [b.entity_id for b in s.bindings_for_record(owner)] == [e2]

    _served_twice(store, check)


def test_an_unreadable_archive_line_beside_a_good_shard_line_costs_nothing(tmp_path):
    store = make_store(tmp_path)
    dec = _id()
    put_shard_line(store, "decision", dec, decision_body(dec))
    put_archive(
        store,
        "2020-01-01-1-aaaaaaaaaaaa",
        [_archive_line("decision", decision_body(dec, status="bogus"))],
    )
    with Store(store) as s:
        assert not s.is_skipped("decisions", dec)
        assert s.get_decision(dec) is not None
        assert s.ratify(dec)[0].status.value == "accepted"
        # the segment itself is still reported, by its path and with no identity
        assert [e["reason"] for e in s._read_skip_list() if "type" not in e] == [
            "bad-archive-segment"
        ]


# -- §7.3: the digest sees every shard file ----------------------------------------------------


def test_digest_moves_on_stray_or_case_variant_change(tmp_path):
    store = make_store(tmp_path)
    dec = _id()
    put_shard_line(store, "decision", dec, decision_body(dec))
    with Store(store) as s:
        seen = {s._compute_canonical_digest()[0]}
        # a stray shard file (not a bucket name), then a case variant of a bucket, then edits
        stray = store / "shards" / "stray.shard"
        stray.write_bytes(b"")
        seen.add(s._compute_canonical_digest()[0])
        variant = store / "shards" / "0A.SHARD"
        variant.write_bytes(b"")
        seen.add(s._compute_canonical_digest()[0])
        stray.write_bytes(b"x")
        seen.add(s._compute_canonical_digest()[0])
        variant.write_bytes(b"xy")
        seen.add(s._compute_canonical_digest()[0])
        variant.unlink()
        seen.add(s._compute_canonical_digest()[0])
        assert len(seen) == 6


def test_canonical_stat_keys_a_shard_file_by_its_lower_cased_name(tmp_path):
    store = make_store(tmp_path)
    dec = _id()
    path = put_shard_line(store, "decision", dec, decision_body(dec))
    (store / "shards" / "0A.SHARD").write_bytes(b"")
    with Store(store) as s:
        _, stats = s._compute_canonical_digest()
        assert ("shards", path.name.lower()) in stats
        assert ("shards", "0a.shard") in stats
        assert not s._canonical_stat_mismatch(stats)
    assert ("shards", path.name.lower()) in _stat_rows(store)


def test_canonical_stat_rows_come_from_the_stat_taken_before_the_read(tmp_path, monkeypatch):
    """A writer replacing a shard after the inventory read it must leave the row older than the
    file, so the next digest walk reloads. A stat taken after the read would certify new bytes."""
    store = make_store(tmp_path)
    dec = _id()
    shard = put_shard_line(store, "decision", dec, decision_body(dec))
    real = store_module.inventory_from_disk

    def read_then_change(*args, **kwargs):
        inv = real(*args, **kwargs)
        shard.write_bytes(shard.read_bytes() + b"\n")  # a concurrent writer, after the read
        return inv

    monkeypatch.setattr(store_module, "inventory_from_disk", read_then_change)
    with Store(store) as s:
        _, stats = s._compute_canonical_digest()
        assert s._canonical_stat_mismatch(stats)


# -- store_has_any_records and the hook probe --------------------------------------------------


def test_shard_only_store_has_records(tmp_path):
    store = make_store(tmp_path)
    dec = _id()
    put_shard_line(store, "decision", dec, decision_body(dec))
    assert store_layout.store_has_any_records(store)
    assert _store_holds_records(store)
    for sub in store_layout.CANONICAL_SUBDIRS:
        (store / sub).mkdir(exist_ok=True)
    with Store(store) as s:
        assert s.get_decision(dec) is not None
    assert not (store / "stamping_live_since").exists()


def test_an_empty_format2_store_is_still_new(tmp_path):
    store = make_store(tmp_path)
    assert not store_layout.store_has_any_records(store)
    assert not _store_holds_records(store)


def test_a_shard_removed_between_listing_and_stat_is_skipped_not_fatal(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    shards = store / "shards"
    shards.mkdir()
    (shards / "00.shard").write_bytes(b"")
    (shards / "01.shard").write_bytes(b"")
    real = os.lstat

    def vanishing(p, *args, **kwargs):
        if str(p).endswith("00.shard"):
            raise FileNotFoundError(p)
        return real(p, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", vanishing)
    assert _store_holds_records(store)


def test_a_directory_named_like_a_shard_is_not_a_record(tmp_path):
    store = make_store(tmp_path)
    (store / "shards" / "x.shard").mkdir(parents=True)
    assert not store_layout.store_has_any_records(store)
    assert not _store_holds_records(store)


def test_an_empty_shards_directory_does_not_count_as_a_record(tmp_path):
    store = make_store(tmp_path)
    (store / "shards").mkdir()
    (store / "shards" / "README").write_text("x", encoding="utf-8")
    assert not store_layout.store_has_any_records(store)
    assert not _store_holds_records(store)


# -- the reload's seam -------------------------------------------------------------------------


def _meta_digest(s: Store) -> str | None:
    row = s._conn.execute("SELECT value FROM meta WHERE key = 'canonical_digest'").fetchone()
    return None if row is None else row[0]


def test_rebuild_runs_inside_an_open_transaction(tmp_path):
    """The helper neither begins nor commits: the caller's transaction stays open after it, and
    an exception in that caller rolls records, stat rows and meta back together."""
    store = make_store(tmp_path)
    dec = _id()
    put_shard_line(store, "decision", dec, decision_body(dec))
    with Store(store) as s:
        other = _id()
        put_shard_line(store, "decision", other, decision_body(other))
        inv = inventory_from_disk(s.path, validate=validate_record_body)
        digest = "digest-of-the-test"
        rows_before = _stat_rows_of(s)
        meta_before = _meta_digest(s)
        with pytest.raises(RuntimeError, match="boom"), s._mutation():  # BEGIN IMMEDIATE
            s._rebuild_index_in_transaction(inv, digest)
            assert s._conn.in_transaction  # not committed by the helper
            assert s.get_decision(other) is not None  # visible inside the transaction
            raise RuntimeError("boom")
        assert s.get_decision(other) is None
        assert _stat_rows_of(s) == rows_before
        assert _meta_digest(s) == meta_before
        with s._mutation():
            s._rebuild_index_in_transaction(inv, digest)
        assert s.get_decision(other) is not None
        assert _meta_digest(s) == digest


def _stat_rows_of(s: Store) -> dict[tuple[str, str], tuple[int, int]]:
    rows = s._conn.execute("SELECT subdir, stem, size, mtime_ns FROM canonical_stat").fetchall()
    return {(r[0], r[1]): (r[2], r[3]) for r in rows}


# -- quarantine: skip entries per identity -----------------------------------------------------


def test_a_quarantined_shard_identity_is_skipped_by_type_and_id(tmp_path):
    store = make_store(tmp_path)
    bad, good = _id(), _id()
    put_shard_line(store, "decision", bad, decision_body(bad, status="bogus"))
    put_shard_line(store, "decision", good, decision_body(good))
    with Store(store) as s:
        assert s.is_skipped("decisions", bad)
        assert not s.is_skipped("decisions", good)
        assert s.get_decision(bad) is None
        assert s.get_decision(good) is not None
        [entry] = s._read_skip_list()
        assert entry["type"] == "decision" and entry["id"] == bad
        assert entry["path"].startswith("shards/") and entry["path"].endswith(".shard")
        assert isinstance(entry["size"], int) and isinstance(entry["mtime_ns"], int)


def test_an_identity_with_no_occurrence_is_never_skipped(tmp_path):
    store = make_store(tmp_path)
    bad = _id()
    put_shard_line(store, "decision", bad, decision_body(bad, status="bogus"))
    with Store(store) as s:
        assert not s.is_skipped("decisions", _id())
        assert not s.is_skipped("bindings", bad)  # a different type of the same id


def test_an_unreadable_file_is_skipped_once_by_its_identity(tmp_path):
    store = make_store(tmp_path)
    bad = _id()
    path = put_file(store, "decision", decision_body(bad))
    path.write_text("<<<<<<< HEAD\n{}\n=======\n{}\n>>>>>>> b\n", encoding="utf-8")
    with Store(store) as s:
        assert s.is_skipped("decisions", bad)
        entries = s._read_skip_list()
        assert [(e["path"], e["type"], e["id"]) for e in entries] == [
            (f"decisions/{bad}.json", "decision", bad)
        ]


def test_an_unsafe_filename_has_no_identity_and_keeps_a_path_entry(tmp_path):
    store = make_store(tmp_path)
    (store / "decisions").mkdir()
    (store / "decisions" / "has space.json").write_text("{}", encoding="utf-8")
    with Store(store) as s:
        [entry] = s._read_skip_list()
        assert entry["path"] == "decisions/has space.json"
        assert "type" not in entry


# -- format 1 is untouched ---------------------------------------------------------------------


def test_format1_reload_keeps_todays_fallback(tmp_path):
    """On major 1 a hot file failing validation does not hide its archived copy (no inventory
    rule 1 there)."""
    store = make_store(tmp_path, fmt=1)
    dec = _id()
    put_file(store, "decision", decision_body(dec, status="bogus"))
    put_archive(
        store,
        "2020-01-01-1-aaaaaaaaaaaa",
        [
            {
                "record_type": "decision",
                **decision_body(dec, status="rejected", valid_to="2026-02-01T00:00:00Z"),
            }
        ],
    )
    with Store(store) as s:
        served = s.get_decision(dec)
        assert served is not None and served.status.value == "rejected"
        [entry] = s._read_skip_list()
        assert entry["path"] == f"decisions/{dec}.json" and "type" not in entry


def test_sweep_removes_stale_shard_tmp_files_only(tmp_path):
    store = make_store(tmp_path)
    (store / "shards").mkdir()
    old = store / "shards" / "00.shard.123.tmp"
    new = store / "shards" / "01.shard.456.tmp"
    old_shard = store / "shards" / "02.shard"
    old_readme = store / "shards" / "README"
    for p in (old, new, old_shard, old_readme):
        p.write_text("", encoding="utf-8")
    long_ago = 1_000_000_000
    for p in (old, old_shard, old_readme):
        os.utime(p, (long_ago, long_ago))
    with Store(store):
        pass
    assert not old.exists()
    assert new.exists() and old_shard.exists() and old_readme.exists()


@pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="modes do not stop root or Windows",
)
def test_an_unlistable_shards_directory_is_an_operational_error_not_an_empty_store(tmp_path):
    """The glob over a directory it cannot list returns nothing: ``shards/`` is probed with the
    other record directories, so the inventory refuses instead of reading "no records"."""
    store = make_store(tmp_path)
    shards = store / "shards"
    shards.mkdir()
    shards.chmod(0)
    try:
        with pytest.raises(PermissionError):
            store_layout.require_listable_record_dirs(store)
    finally:
        shards.chmod(0o755)


# -- what a person is told about a quarantined packed identity ----------------------------------


def test_the_refusal_for_a_packed_identity_names_it_and_its_shard_not_a_missing_file(tmp_path):
    store = make_store(tmp_path)
    dec = _id()
    put_shard_line(store, "decision", dec, decision_body(dec, status="bogus"))
    with Store(store) as s:
        text = s.skipped_refusal("decisions", dec)
        with pytest.raises(ValueError) as raised:
            s.add_decision(_decision_with_id(dec))
    assert str(raised.value) == text
    assert f"decision {dec}" in text and "shards/" in text and ".shard" in text
    assert f"decisions/{dec}.json" not in text


def test_the_refusal_for_a_broken_per_record_file_still_names_the_file(tmp_path):
    store = make_store(tmp_path)
    dec = _id()
    put_file(store, "decision", decision_body(dec)).write_text("{ nope", encoding="utf-8")
    with Store(store) as s:
        assert f"decisions/{dec}.json" in s.skipped_refusal("decisions", dec)


def test_each_quarantined_packed_identity_gets_its_own_warning_line(tmp_path, capsys):
    store = make_store(tmp_path)
    ids = [_id(), _id(), _id()]
    for i in ids:
        put_shard_line(store, "decision", i, decision_body(i, status="bogus"), bucket="00.shard")
    with Store(store):
        pass
    lines = [ln for ln in capsys.readouterr().err.splitlines() if "left out of the index" in ln]
    assert len(lines) == 3
    for i in ids:
        assert sum(f"decision {i}" in ln and "shards/00.shard" in ln for ln in lines) == 1


def _decision_with_id(dec: str):
    from datetime import UTC, datetime  # noqa: PLC0415

    from sidegraph.schema import Decision, DecisionKind, Provenance  # noqa: PLC0415

    return Decision(
        id=dec,
        title="t",
        kind=DecisionKind.ADR,
        context="c",
        choice="ch",
        valid_from=datetime.now(UTC),
        provenance=Provenance(source="manual"),
    )


# -- one validation, two readers ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("mutate", "valid"),
    [
        (lambda b: b, True),
        (lambda b: {**b, "status": "bogus"}, False),
        (lambda b: {**b, "title": "lone \ud800 surrogate"}, False),
    ],
    ids=["valid", "bad-status", "unserialisable"],
)
def test_a_file_and_a_shard_line_are_judged_alike(tmp_path, mutate, valid):
    """``Store._parse_record_file`` (format 1) and ``validate_record_body`` (the inventory) share
    one validate-and-serialise step: a body one refuses the other refuses."""
    from sidegraph.schema import Decision  # noqa: PLC0415
    from sidegraph.store import _parse_record_file  # noqa: PLC0415

    dec = _id()
    body = mutate(decision_body(dec))
    path = tmp_path / f"{dec}.json"
    import json  # noqa: PLC0415

    path.write_text(json.dumps(body), encoding="utf-8")  # ensure_ascii escapes the surrogate
    record, problem = _parse_record_file(path, "id", Decision)
    assert (problem is None) == (validate_record_body("decision", body) is None)
    assert (record is not None) == (problem is None) == valid
