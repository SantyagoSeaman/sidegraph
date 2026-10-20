from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime

from sidegraph.cli import compact_main
from sidegraph.schema import Decision, DecisionKind, DecisionStatus, Provenance
from sidegraph.store import Store


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


def test_cli_compact_nothing_to_compact(tmp_path, capsys):
    db = tmp_path / "t.db"
    Store(db)
    assert compact_main(["--db", str(db)]) == 0
    assert "nothing to compact" in capsys.readouterr().out


def test_cli_compact_reports_segment_and_counts(tmp_path, capsys):
    db = tmp_path / "t.db"
    s = Store(db)
    old = s.add_decision(_decision(title="old"))
    s.add_decision(_decision(title="new", supersedes=old.id))
    s.close()

    assert compact_main(["--db", str(db)]) == 0
    out = capsys.readouterr().out
    assert "compacted 1 record(s) into archive/" in out
    assert "1 decisions, 0 domains" in out
    assert "0 skipped" in out

    # a second run finds nothing new
    assert compact_main(["--db", str(db)]) == 0
    assert "nothing to compact" in capsys.readouterr().out


def test_cli_compact_dry_run_lists_and_writes_nothing(tmp_path, capsys):
    db = tmp_path / "t.db"
    s = Store(db)
    old = s.add_decision(_decision(title="old"))
    s.add_decision(_decision(title="new", supersedes=old.id))
    s.close()

    assert compact_main(["--db", str(db), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert old.id in out
    assert "would compact 1 record(s)" in out
    assert not (db / "archive").exists()
    assert (db / "decisions" / f"{old.id}.json").is_file()


def _compact_fixture_with_extra_hot_field(db):
    store = Store(db)
    old = store.add_decision(_decision(title="old"))
    store.add_decision(_decision(title="new", supersedes=old.id))
    hot_path = db / "decisions" / f"{old.id}.json"
    payload = json.loads(hot_path.read_text(encoding="utf-8"))
    payload["future_field"] = None
    hot_path.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    store.close()
    return old.id, hot_path


def test_cli_compact_reports_kept_fresh_hot_file(tmp_path, capsys):
    db = tmp_path / "t.db"
    record_id, hot_path = _compact_fixture_with_extra_hot_field(db)
    hot_bytes_before = hot_path.read_bytes()

    assert compact_main(["--db", str(db)]) == 0

    out = capsys.readouterr().out
    assert "compacted 1 record(s) into archive/" in out
    assert "kept 1 hot file(s): their content is not fully represented in the archive" in out
    hot_bytes = hot_path.read_bytes()
    assert hot_bytes == hot_bytes_before
    assert json.loads(hot_bytes)["future_field"] is None
    segments = list((db / "archive").glob("*.jsonl"))
    assert len(segments) == 1
    segment_bytes = segments[0].read_bytes()
    assert len(segment_bytes.splitlines()) == 1
    assert record_id in hot_path.name
    assert hashlib.sha256(hot_bytes).hexdigest() == hashlib.sha256(hot_bytes_before).hexdigest()


def test_cli_compact_reports_kept_only_repeat(tmp_path, capsys):
    db = tmp_path / "t.db"
    _record_id, hot_path = _compact_fixture_with_extra_hot_field(db)
    assert compact_main(["--db", str(db)]) == 0
    segment_path = next((db / "archive").glob("*.jsonl"))
    hot_hash = hashlib.sha256(hot_path.read_bytes()).hexdigest()
    segment_hash = hashlib.sha256(segment_path.read_bytes()).hexdigest()
    capsys.readouterr()

    assert compact_main(["--db", str(db)]) == 0

    out = capsys.readouterr().out
    assert "nothing to compact" not in out
    assert "compacted 0 record(s); 0 skipped (not terminal enough)" in out
    assert "kept 1 hot file(s): their content is not fully represented in the archive" in out
    assert hashlib.sha256(hot_path.read_bytes()).hexdigest() == hot_hash
    assert hashlib.sha256(segment_path.read_bytes()).hexdigest() == segment_hash

    assert compact_main(["--db", str(db), "--dry-run"]) == 0
    dry_out = capsys.readouterr().out
    assert "nothing to compact" not in dry_out
    assert "would compact 0 record(s) (0 decisions, 0 domains); 0 skipped" in dry_out
    assert (
        "would keep 1 hot file(s): their content is not fully represented in the archive" in dry_out
    )
    assert hashlib.sha256(hot_path.read_bytes()).hexdigest() == hot_hash
    assert hashlib.sha256(segment_path.read_bytes()).hexdigest() == segment_hash

    # Force a cold reload after the warm repeat and dry-run using only this temporary fixture's
    # derived index artifacts.
    for index_artifact in db.glob("index.db*"):
        index_artifact.unlink()
    with Store(db) as reopened:
        captured = capsys.readouterr()
        assert (
            "extra fields are not represented in the archive; a newer Sidegraph may have "
            "written them" in captured.err
        )
        assert reopened.get_decision(_record_id).title == "old"
    assert hashlib.sha256(hot_path.read_bytes()).hexdigest() == hot_hash
    assert hashlib.sha256(segment_path.read_bytes()).hexdigest() == segment_hash


def test_cli_compact_dry_run_reports_kept_fresh_hot_file(tmp_path, capsys):
    db = tmp_path / "t.db"
    _record_id, hot_path = _compact_fixture_with_extra_hot_field(db)
    hot_bytes = hot_path.read_bytes()
    # Warm open observes the externally-added field and refreshes the derived digest before
    # this dry-run preservation check begins.
    Store(db).close()
    conn = sqlite3.connect(str(db / "index.db"))
    digest_before = conn.execute(
        "SELECT value FROM meta WHERE key = 'canonical_digest'"
    ).fetchone()[0]
    conn.close()

    assert compact_main(["--db", str(db), "--dry-run"]) == 0

    captured = capsys.readouterr()
    out = captured.out
    err = captured.err
    assert "would compact 1 record(s) (1 decisions, 0 domains); 0 skipped" in out
    assert "would keep 1 hot file(s): their content is not fully represented in the archive" in out
    assert (
        "extra fields are not represented in the archive; a newer Sidegraph may have written them"
        in err
    )
    assert "prospective archive payload" in err
    assert "no archive segment has been published" in err
    assert "exists in BOTH" not in err
    assert "archive segment with DIFFERENT content" not in err
    assert hot_path.read_bytes() == hot_bytes
    assert not (db / "archive").exists()
    conn = sqlite3.connect(str(db / "index.db"))
    digest_after = conn.execute("SELECT value FROM meta WHERE key = 'canonical_digest'").fetchone()[
        0
    ]
    conn.close()
    assert digest_after == digest_before


def test_cli_compact_reports_leftover_cleanup_and_kept_counts_together(tmp_path, capsys):
    db = tmp_path / "t.db"
    store = Store(db)
    kept = store.add_decision(_decision(title="kept leftover"))
    store.add_decision(_decision(title="kept successor", supersedes=kept.id))
    cleaned = store.add_decision(_decision(title="clean leftover"))
    store.add_decision(_decision(title="clean successor", supersedes=cleaned.id))
    assert store.compact().decisions_compacted == 2

    kept_path = db / "decisions" / f"{kept.id}.json"
    kept_payload = store.get_decision(kept.id).model_dump(mode="json")
    kept_payload["future_field"] = None
    kept_path.write_text(
        json.dumps(kept_payload, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    cleaned_path = db / "decisions" / f"{cleaned.id}.json"
    cleaned_path.write_text(
        json.dumps(store.get_decision(cleaned.id).model_dump(mode="json"), sort_keys=True, indent=2)
        + "\n",
        encoding="utf-8",
    )
    store.close()

    assert compact_main(["--db", str(db)]) == 0

    out = capsys.readouterr().out
    assert "compacted 0 record(s); 0 skipped (not terminal enough)" in out
    assert "cleaned up 1 leftover hot file(s)" in out
    assert "kept 1 hot file(s): their content is not fully represented in the archive" in out
    assert not cleaned_path.exists()
    assert kept_path.is_file()


def test_compact_extra_field_warning_is_not_corruption(tmp_path, capsys):
    db = tmp_path / "t.db"
    _compact_fixture_with_extra_hot_field(db)

    assert compact_main(["--db", str(db)]) == 0

    err = capsys.readouterr().err
    assert (
        "extra fields are not represented in the archive; a newer Sidegraph may have written them"
        in err
    )
    assert "corruption-shaped" not in err
    assert "future_field" not in err


def test_cli_compact_older_than_negative_rejected(tmp_path, capsys):
    db = tmp_path / "t.db"
    assert compact_main(["--db", str(db), "--older-than", "-1"]) == 1
    assert "--older-than must be >= 0" in capsys.readouterr().out
    # rejected before any store I/O -- nothing was created
    assert not db.exists()


def test_cli_compact_store_open_failure_exits_nonzero(tmp_path, capsys):
    db = tmp_path / "bad.db"
    Store(db)
    conn = sqlite3.connect(str(db / "index.db"))
    conn.execute("UPDATE meta SET value = 'bogus' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()
    assert compact_main(["--db", str(db)]) == 1
    assert "not readable" in capsys.readouterr().out


def test_cli_compact_older_than_skips_too_recent_and_says_so(tmp_path, capsys):
    from datetime import timedelta

    db = tmp_path / "t.db"
    s = Store(db)
    now = datetime.now(UTC)
    d = _decision(
        title="recent",
        status=DecisionStatus.SUPERSEDED,
        valid_from=now - timedelta(hours=1),
        valid_to=now,
    )
    with s._lock:
        s._write_decision(d)
        s._commit()
    s.close()

    assert compact_main(["--db", str(db), "--older-than", "30"]) == 0
    out = capsys.readouterr().out
    assert "nothing to compact; 1 skipped (not terminal enough)" in out
    assert (db / "decisions" / f"{d.id}.json").is_file()  # untouched, kept hot


def test_cli_compact_over_a_corrupt_archive_segment_exits_nonzero_naming_it(tmp_path, capsys):
    """The store opens over a segment with an unreadable line (the reload skips it), so the
    refusal now comes from ``compact()``: the command reports it and exits 1, no traceback.
    see design/superpowers/specs/2026-10-03-store-survives-a-bad-file-design.md (D4)"""
    db = tmp_path / "t.db"
    Store(db).close()
    segment = db / "archive" / "2020-01-01-1-deadbeef0000.jsonl"
    segment.parent.mkdir()
    segment.write_text("not json at all\n", encoding="utf-8")

    assert compact_main(["--db", str(db)]) == 1

    out = capsys.readouterr().out
    assert str(segment) in out
    assert "line 1" in out


def test_cli_compact_dry_run_over_a_corrupt_archive_segment_exits_nonzero_too(tmp_path, capsys):
    """``docs/reference/cli.md`` says ``--dry-run`` is refused as well: it reads the same
    segments, and a listing built over a segment it cannot read would be a guess."""
    db = tmp_path / "t.db"
    Store(db).close()
    segment = db / "archive" / "2020-01-01-1-deadbeef0000.jsonl"
    segment.parent.mkdir()
    segment.write_text("not json at all\n", encoding="utf-8")

    assert compact_main(["--db", str(db), "--dry-run"]) == 1

    out = capsys.readouterr().out
    assert f"cannot compact ({db}): corrupt archive segment {segment} at line 1" in out
