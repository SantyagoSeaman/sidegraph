"""CLI contract tests for sidegraph-doctor (see
design/superpowers/specs/2026-07-23-sidegraph-doctor-design.md): exit matrix, --json
shape, --check escalation, --stale-days validation, and the executable never-writes
guarantee. Stores are seeded through Store (tests may write; doctor may not)."""

import json
import time
from datetime import UTC, datetime
from pathlib import Path

from sidegraph.cli import doctor_main
from sidegraph.schema import (
    AnchorBinding,
    Decision,
    DecisionKind,
    DecisionStatus,
    Descriptor,
    Entity,
    Provenance,
)
from sidegraph.store import Store


def _seed_healthy(tmp_path: Path) -> Path:
    """A store with one accepted, bound decision — no violations, no findings."""
    db = tmp_path / "store"
    s = Store(db)
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
    return db


def _seed_with_finding(tmp_path: Path) -> Path:
    """Healthy store plus one dangling accepted decision (advisory finding, no violation)."""
    db = _seed_healthy(tmp_path)
    s = Store(db)
    s.add_decision(
        Decision(
            title="unanchored",
            kind=DecisionKind.LESSON,
            status=DecisionStatus.ACCEPTED,
            context="c",
            choice="ch",
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual"),
        )
    )
    return db


def _break_strict(db: Path) -> None:
    """Introduce a strict violation verify_snapshot reports (parse-error)."""
    (db / "decisions" / "corrupt.json").write_text("{not json", encoding="utf-8")


# -- exit matrix ---------------------------------------------------------------------------


def test_healthy_store_exits_0_and_prints_clean(tmp_path, capsys):
    db = _seed_healthy(tmp_path)
    assert doctor_main(["--db", str(db)]) == 0
    assert capsys.readouterr().out.strip().splitlines()[-1] == "clean"


def test_findings_only_exit_0_by_default(tmp_path, capsys):
    db = _seed_with_finding(tmp_path)
    assert doctor_main(["--db", str(db)]) == 0
    out = capsys.readouterr().out
    assert "dangling-record" in out
    assert "0 violation(s), 1 finding(s)" in out


def test_findings_only_exit_2_with_check(tmp_path):
    db = _seed_with_finding(tmp_path)
    assert doctor_main(["--db", str(db), "--check"]) == 2


def test_violations_exit_2_with_and_without_check(tmp_path):
    db = _seed_healthy(tmp_path)
    _break_strict(db)
    assert doctor_main(["--db", str(db)]) == 2
    assert doctor_main(["--db", str(db), "--check"]) == 2


def test_missing_store_dir_is_operational_error(tmp_path, capsys):
    assert doctor_main(["--db", str(tmp_path / "nope")]) == 1
    assert "store not readable" in capsys.readouterr().out


def test_against_outside_git_repo_is_operational_error(tmp_path, capsys):
    db = _seed_healthy(tmp_path)  # tmp_path is not a git repository
    assert doctor_main(["--db", str(db), "--against", "HEAD"]) == 1
    assert "--against" in capsys.readouterr().out


def test_negative_stale_days_is_usage_error(tmp_path, capsys):
    db = _seed_healthy(tmp_path)
    assert doctor_main(["--db", str(db), "--stale-days", "-1"]) == 1
    assert "--stale-days" in capsys.readouterr().out


# -- --json --------------------------------------------------------------------------------


def test_json_output_shape_and_purity(tmp_path, capsys):
    db = _seed_with_finding(tmp_path)
    assert doctor_main(["--db", str(db), "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)  # pure JSON: parse the WHOLE stdout
    assert set(doc) == {"clean", "violations", "findings", "skipped"}
    assert doc["clean"] is False
    assert doc["violations"] == []
    assert doc["findings"][0]["code"] == "dangling-record"
    assert set(doc["findings"][0]) == {"code", "path", "detail"}
    assert doc["skipped"] == []  # Store-seeded stores carry an index.db


def test_json_clean_true_on_healthy_store(tmp_path, capsys):
    db = _seed_healthy(tmp_path)
    assert doctor_main(["--db", str(db), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["clean"] is True


def test_skipped_check_does_not_fail_even_with_check(tmp_path, capsys):
    db = _seed_healthy(tmp_path)
    (db / "index.db").unlink()  # fresh-clone shape: canonical files only
    assert doctor_main(["--db", str(db), "--check", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["skipped"] == ["binding-status", "never-surfaced"]
    assert doc["clean"] is True


def test_degraded_binding_surfaces_through_cli(tmp_path, capsys):
    db = _seed_healthy(tmp_path)
    s = Store(db)
    d = s.add_decision(
        Decision(
            title="decayed",
            kind=DecisionKind.GOTCHA,
            status=DecisionStatus.ACCEPTED,
            context="c",
            choice="ch",
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual"),
        )
    )
    e = s.upsert_entity(
        Entity(canonical_name="beta", descriptor=Descriptor(name="beta", file_path="b.py"))
    )
    s.add_binding(AnchorBinding(record_id=d.id, entity_id=e.entity_id, tier=2, status="degraded"))
    assert doctor_main(["--db", str(db), "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert "degraded-binding" in [f["code"] for f in doc["findings"]]


# -- --stale-days passthrough --------------------------------------------------------------


def test_stale_days_flag_reaches_curate(tmp_path, capsys):
    db = _seed_healthy(tmp_path)
    s = Store(db)
    d = s.add_decision(  # default status is PROPOSED
        Decision(
            title="pending",
            kind=DecisionKind.ADR,
            context="c",
            choice="ch",
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual"),
        )
    )
    e = s.upsert_entity(
        Entity(canonical_name="gamma", descriptor=Descriptor(name="gamma", file_path="g.py"))
    )
    s.add_binding(AnchorBinding(record_id=d.id, entity_id=e.entity_id, tier=2))  # not dangling
    time.sleep(0.01)  # ULID has ms precision; ensure "now" is strictly after creation

    # threshold 0: a proposal created strictly before "now" IS stale
    assert doctor_main(["--db", str(db), "--stale-days", "0", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert "stale-proposal" in [f["code"] for f in doc["findings"]]

    # default threshold 30: a seconds-old proposal is not stale
    assert doctor_main(["--db", str(db), "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert "stale-proposal" not in [f["code"] for f in doc["findings"]]


# -- never writes --------------------------------------------------------------------------


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()
    }


def test_doctor_never_writes(tmp_path):
    db = _seed_with_finding(tmp_path)  # includes index.db
    before = _snapshot(db)
    doctor_main(["--db", str(db)])
    doctor_main(["--db", str(db), "--check", "--json"])
    assert _snapshot(db) == before


def test_doctor_reports_ratification_latency(tmp_path, capsys, monkeypatch):
    """Red against unfixed code: doctor has no time-to-ratify line. The stat is derived
    from ratified_at − valid_from over stamped records (2026-08-04 lifecycle D4);
    unstamped (pre-change) records are excluded, never guessed."""
    db = tmp_path / "store"
    s = Store(db)
    monkeypatch.setattr("sidegraph.store._ratifier_identity", lambda actor=None: "tester")
    from datetime import timedelta

    d = s.add_decision(
        Decision(
            title="waited three days",
            kind=DecisionKind.ADR,
            status=DecisionStatus.PROPOSED,
            context="c",
            choice="ch",
            valid_from=datetime.now(UTC) - timedelta(days=3),
            provenance=Provenance(source="agent"),
        )
    )
    s.ratify(d.id)
    capsys.readouterr()
    assert doctor_main(["--db", str(db)]) == 0
    out = capsys.readouterr().out
    assert "time-to-ratify" in out
    assert "median 3 days" in out
    assert "1 stamped" in out


def test_doctor_latency_line_absent_without_stamps(tmp_path, capsys):
    """No stamped records -> no latency line (nothing to compute, nothing guessed)."""
    db = _seed_healthy(tmp_path)
    capsys.readouterr()
    assert doctor_main(["--db", str(db)]) == 0
    assert "time-to-ratify" not in capsys.readouterr().out
