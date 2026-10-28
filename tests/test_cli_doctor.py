"""CLI contract tests for sidegraph-doctor (see
design/superpowers/specs/2026-07-23-sidegraph-doctor-design.md): exit matrix, --json
shape, --check escalation, --stale-days validation, and the executable never-writes
guarantee. Stores are seeded through Store (tests may write; doctor may not)."""

import json
import subprocess
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
from sidegraph.store import VOLATILE_STALE_KEY, Store


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


def test_an_archive_segment_of_invalid_utf8_is_a_violation_not_an_unreadable_store(
    tmp_path, capsys
):
    """Unfixed, the segment's whole-file read raised and the command exited 1 with "store not
    readable" and no file name."""
    db = _seed_healthy(tmp_path)
    segment = db / "archive" / "2020-01-01-1-deadbeef0000.jsonl"
    segment.parent.mkdir()
    segment.write_bytes(b'{"record_type": "decision", "title": "\xff\xfe"}\n')

    assert doctor_main(["--db", str(db)]) == 2

    out = capsys.readouterr().out
    assert f"bad-archive-segment  {segment}" in out
    assert "not readable" not in out


# -- --json --------------------------------------------------------------------------------


def test_json_output_shape_and_purity(tmp_path, capsys):
    db = _seed_with_finding(tmp_path)
    assert doctor_main(["--db", str(db), "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)  # pure JSON: parse the WHOLE stdout
    assert set(doc) == {"clean", "violations", "findings", "skipped"}
    assert doc["clean"] is False
    assert doc["violations"] == []
    assert doc["findings"][0]["code"] == "dangling-record"
    assert set(doc["findings"][0]) == {
        "code",
        "path",
        "detail",
        "record_id",
        "record_type",
        "first_seen",
    }
    # An index that no sync has computed the statuses of, and no graph in this test's directory.
    assert doc["skipped"] == ["binding-status", "orphaned-records", "graph"]


def test_json_clean_true_on_healthy_store(tmp_path, capsys):
    db = _seed_healthy(tmp_path)
    assert doctor_main(["--db", str(db), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["clean"] is True


def test_skipped_check_does_not_fail_even_with_check(tmp_path, capsys):
    db = _seed_healthy(tmp_path)
    (db / "index.db").unlink()  # fresh-clone shape: canonical files only
    assert doctor_main(["--db", str(db), "--check", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["skipped"] == ["binding-status", "never-surfaced", "orphaned-records", "graph"]
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
    s.set_meta(VOLATILE_STALE_KEY, "0")  # a finished sync; a new store's index is cold
    s.close()
    assert doctor_main(["--db", str(db), "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert "degraded-binding" in [f["code"] for f in doc["findings"]]


def test_reloaded_index_withholds_binding_statuses_end_to_end(tmp_path, capsys):
    """Reload probe: an orphaned binding after a sync, then a touch and a Store open (a reload
    resets every status to live and sets the flag). Doctor lists the check as skipped."""
    db = _seed_healthy(tmp_path)
    s = Store(db)
    d = next(iter(db.glob("decisions/*.json")))
    e = s.upsert_entity(
        Entity(canonical_name="beta", descriptor=Descriptor(name="beta", file_path="b.py"))
    )
    s.add_binding(AnchorBinding(record_id=d.stem, entity_id=e.entity_id, tier=2, status="orphaned"))
    s.set_meta(VOLATILE_STALE_KEY, "0")
    s.close()
    assert doctor_main(["--db", str(db), "--json"]) == 0
    assert "orphaned-binding" in [
        f["code"] for f in json.loads(capsys.readouterr().out)["findings"]
    ]

    time.sleep(0.05)
    d.touch()
    Store(db).close()  # the reload

    assert doctor_main(["--db", str(db), "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert "binding-status" in doc["skipped"] and "orphaned-records" in doc["skipped"]
    assert not {"orphaned-binding", "orphaned-record"} & {f["code"] for f in doc["findings"]}

    assert doctor_main(["--db", str(db)]) == 0
    out = capsys.readouterr().out
    assert (
        "binding-status check skipped (binding statuses are not computed for the current "
        "graph — run sidegraph-sync)"
    ) in out
    assert "no usable index.db" not in out


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


def test_doctor_cli_surfaces_graph_root_mismatch_via_graph_flag(tmp_path, capsys):
    """End-to-end wiring for defect 2: --graph is a real CLI flag, doctor_main builds a
    GraphifyReader from it, and a subdir-relative graph surfaces the advisory finding a
    person actually sees, not just something curate() can do when called directly."""
    import subprocess

    db = _seed_healthy(tmp_path)
    repo_root = tmp_path
    subprocess.run(["git", "init", "-q"], cwd=repo_root, check=True)
    paths = [f"pkg/mod_{i}.py" for i in range(10)]
    for p in paths:
        real = repo_root / "src" / p
        real.parent.mkdir(parents=True, exist_ok=True)
        real.write_text("pass\n")
    graph = repo_root / "graph.json"
    graph.write_text(
        json.dumps(
            {
                "built_at_commit": "v1",
                "nodes": [
                    {
                        "id": f"n{i}",
                        "label": f"fn_{i}()",
                        "norm_label": f"fn_{i}()",
                        "file_type": "code",
                        "source_file": p,
                        "community": i % 3,
                    }
                    for i, p in enumerate(paths)
                ],
                "links": [],
            }
        )
    )
    capsys.readouterr()
    assert doctor_main(["--db", str(db), "--graph", str(graph)]) == 0
    out = capsys.readouterr().out
    assert "graph-root-mismatch" in out
    assert "src" in out


def test_spelling_twins_are_informational_and_never_fail_check(tmp_path, capsys):
    # Red against unfixed code: no "spelling twins" line. Red against a spelling-twin finding
    # (a discarded design, declared): --check would exit 2 on a twin nobody can remove.
    db = _seed_healthy(tmp_path)
    s = Store(db)
    twin = s.upsert_entity(
        Entity(
            entity_id="00000000000000000000000000",
            canonical_name="src/w.py",
            descriptor=Descriptor(name="src/w.py", file_path="src/w.py"),
        )
    )
    plain = s.upsert_entity(
        Entity(canonical_name="w.py", descriptor=Descriptor(name="w.py", file_path="src/w.py"))
    )
    d = s.add_decision(
        Decision(
            title="twins",
            kind=DecisionKind.ADR,
            status=DecisionStatus.ACCEPTED,
            context="c",
            choice="ch",
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual"),
        )
    )
    for e in (twin, plain):
        s.add_binding(AnchorBinding(record_id=d.id, entity_id=e.entity_id, tier=2))
    s.close()
    assert doctor_main(["--db", str(db), "--check"]) == 0
    out = capsys.readouterr().out
    assert out.count("spelling twins: 1 entity is named by a path (full or partial)") == 1
    assert (
        f"  {twin.entity_id} 'src/w.py' (1 binding(s)) → {plain.entity_id} 'w.py' (1 binding(s))"
        in out
    )
    assert out.strip().splitlines()[-1] == "clean"
    assert doctor_main(["--db", str(db), "--json"]) == 0
    assert "spelling" not in capsys.readouterr().out


# -- first_seen on code-drift findings ----------------------------------------------------


def _stamped_drift_store(tmp_path):
    from datetime import UTC, datetime

    from tests.test_sync_drift_stamps import World

    repo = tmp_path / "drift-repo"
    repo.mkdir()
    for args in (
        ["init", "-q"],
        ["config", "user.email", "test@example.com"],
        ["config", "user.name", "Test"],
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    world = World(repo)
    world.drift("b.py")
    world.refresh(datetime(2026, 10, 1, 12, tzinfo=UTC))
    world.drift("a.py")
    world.refresh(datetime(2026, 10, 2, 12, tzinfo=UTC))
    world.store.close()
    return world, repo / ".sidegraph"


def test_json_code_drift_finding_carries_first_seen(tmp_path, capsys):
    world, db = _stamped_drift_store(tmp_path)
    doctor_main(["--db", str(db), "--json"])
    findings = json.loads(capsys.readouterr().out)["findings"]
    drift = {f["record_id"]: f for f in findings if f["code"] == "code-drift"}
    assert drift[world.x.id]["first_seen"] == "2026-10-02T12:00:00+00:00"
    assert drift[world.y.id]["first_seen"] is None  # present at seeding: unknown age
    assert all(f["first_seen"] is None for f in findings if f["code"] != "code-drift")


def test_json_does_not_touch_the_index(tmp_path, capsys):
    _world, db = _stamped_drift_store(tmp_path)
    before = (db / "index.db").read_bytes()
    doctor_main(["--db", str(db), "--json"])
    capsys.readouterr()
    assert (db / "index.db").read_bytes() == before
