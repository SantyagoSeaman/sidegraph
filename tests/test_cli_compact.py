from __future__ import annotations

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
