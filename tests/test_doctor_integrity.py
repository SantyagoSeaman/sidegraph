"""``sidegraph-doctor`` gets the registry's findings (``orphaned-record``, ``graph-stale``) and
says when it could not look at the graph.

Doctor is a pure read: it never constructs a ``Store``. It hands the registry a ``mode=ro``
connection on ``index.db`` (or ``None`` when there is no usable one) and renders the problems'
``findings``. A missing graph is a skipped check, never a finding: ``--check`` escalates every
finding, so a finding would make every CI job that runs doctor without a graph exit 2.
see design/superpowers/specs/2026-10-02-integrity-self-check-design.md (D6; T11, T12, T20)
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from sidegraph.cli import doctor_main
from sidegraph.doctor import ORPHANED_RECORD, curate
from sidegraph.store import VOLATILE_STALE_KEY, Store
from tests.test_cli_doctor import _seed_healthy
from tests.test_integrity import add_record, settled


def _ro(store_dir: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{store_dir / 'index.db'}?mode=ro", uri=True)


def _settled_healthy(tmp_path: Path) -> Path:
    """``_seed_healthy``, with the statuses marked as computed (a finished sync): a new store's
    index is cold, which doctor reports as a skipped orphan check."""
    db = _seed_healthy(tmp_path)
    store = Store(db)
    store.set_meta(VOLATILE_STALE_KEY, "0")
    store.close()
    return db


def _orphaned_store(tmp_path: Path) -> tuple[Path, str, str]:
    store_dir = tmp_path / "store"
    store = settled(Store(store_dir))
    decision = add_record(store, age_days=40, leaves=("orphaned", "orphaned"))
    fact = add_record(store, fact=True, age_days=50)
    add_record(store, age_days=1, leaves=("live",))  # not flagged
    store.close()
    return store_dir, decision, fact


def test_t11_each_record_with_every_anchor_orphaned_is_one_finding(tmp_path):
    """Red against unfixed code: no ``orphaned-record`` code exists (ImportError)."""
    store_dir, decision, fact = _orphaned_store(tmp_path)
    conn = _ro(store_dir)
    try:
        report = curate(store_dir, index=conn)
    finally:
        conn.close()

    findings = [f for f in report.findings if f.code == ORPHANED_RECORD]
    assert ORPHANED_RECORD == "orphaned-record"
    expected = sorted(
        [
            (str(store_dir / "decisions" / f"{decision}.json"), 2),
            (str(store_dir / "facts" / f"{fact}.json"), 1),
        ],
        key=lambda pair: Path(pair[0]).stem,
    )
    assert [(f.path, f.detail) for f in findings] == [
        (
            path,
            f"every code anchor of this record is orphaned ({leaves} leaf anchor(s)); retrieval "
            "reaches it only through its file or domain — re-anchor it with add_anchors or the "
            "heal-anchors skill, or supersede it if the code is gone",
        )
        for path, leaves in expected
    ]


def test_curate_without_an_index_runs_no_index_check(tmp_path):
    """Direct ``curate(store_dir)`` callers pass nothing and get no index checks."""
    store_dir, _decision, _fact = _orphaned_store(tmp_path)

    report = curate(store_dir)

    assert ORPHANED_RECORD not in [f.code for f in report.findings]
    assert report.skipped == []  # curate adds no skip of its own: doctor_main does


def test_t11_doctor_prints_the_finding_through_the_cli(tmp_path, capsys):
    store_dir, decision, _fact = _orphaned_store(tmp_path)

    assert doctor_main(["--db", str(store_dir)]) == 0

    out = capsys.readouterr().out
    assert (
        f"orphaned-record  {store_dir / 'decisions' / f'{decision}.json'}  every code anchor" in out
    )


def test_an_orphaned_record_adds_no_new_failure_under_check(tmp_path, capsys):
    """``orphaned-binding`` already reports the same records' bindings, so ``--check``'s verdict
    does not depend on the new code: with it, the store was already a finding."""
    store_dir, _decision, _fact = _orphaned_store(tmp_path)

    assert doctor_main(["--db", str(store_dir), "--check"]) == 2
    out = capsys.readouterr().out
    assert "orphaned-binding" in out
    assert "orphaned-record" in out


# -- T12: no graph ----------------------------------------------------------------------------


def test_t12_doctor_without_a_graph_prints_one_skipped_line_before_the_summary(
    tmp_path, capsys, monkeypatch
):
    """Red against unfixed code: no such line. The summary stays the last line."""
    db = _settled_healthy(tmp_path)
    monkeypatch.chdir(tmp_path)

    assert doctor_main(["--db", str(db)]) == 0

    lines = capsys.readouterr().out.strip().splitlines()
    graph = tmp_path / "graphify-out" / "graph.json"
    assert lines[-1] == "clean"
    assert lines[-2] == (
        f"graph checks skipped (no code graph at {graph} — build it from the repository root "
        "with `graphify update .`)"
    )


def test_t12_the_json_has_graph_in_skipped_and_prints_no_line(tmp_path, capsys, monkeypatch):
    db = _settled_healthy(tmp_path)
    monkeypatch.chdir(tmp_path)

    assert doctor_main(["--db", str(db), "--json"]) == 0

    doc = json.loads(capsys.readouterr().out)  # pure JSON: the whole of stdout
    assert doc["skipped"] == ["graph"]
    assert doc["clean"] is True


def test_t12_a_missing_graph_never_fails_check(tmp_path, capsys, monkeypatch):
    """A missing graph is not a finding. The clause is red against the discarded design where it
    was (R4): every CI job that runs doctor without a graph would start exiting 2."""
    db = _settled_healthy(tmp_path)
    monkeypatch.chdir(tmp_path)

    assert doctor_main(["--db", str(db), "--check"]) == 0
    assert doctor_main(["--db", str(db), "--check", "--json"]) == 0


def test_t12_a_present_graph_prints_no_skipped_line(tmp_path, capsys, monkeypatch):
    from tests.test_graph_freshness import make_repo

    fx = make_repo(tmp_path)
    db = _settled_healthy(tmp_path)
    monkeypatch.chdir(fx.repo)

    doctor_main(["--db", str(db), "--graph", str(fx.graph), "--json"])

    assert json.loads(capsys.readouterr().out)["skipped"] == []


def test_without_a_usable_index_the_orphan_check_is_skipped_with_the_existing_text(
    tmp_path, capsys, monkeypatch
):
    db = _seed_healthy(tmp_path)
    (db / "index.db").unlink()
    monkeypatch.chdir(tmp_path)

    assert doctor_main(["--db", str(db)]) == 0

    out = capsys.readouterr().out
    assert "orphaned-records check skipped (no usable index.db — run sidegraph-sync)" in out
    assert "graph check skipped" not in out  # the graph has its own line, not this text


# -- an index that has been reloaded and not synced -----------------------------------------


def test_a_reloaded_index_skips_the_orphan_check_and_says_so(tmp_path, capsys, monkeypatch):
    """Statuses read live until a sync recomputes them (``volatile_stale``), so the check cannot
    say anything: it is skipped, with the text that already says to run the sync."""
    store_dir = tmp_path / "store"
    store = Store(store_dir)  # a new store's index is cold: volatile_stale is "1"
    for _ in range(3):
        add_record(store, age_days=1)
    store.close()
    monkeypatch.chdir(tmp_path)

    assert doctor_main(["--db", str(store_dir), "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["skipped"] == ["orphaned-records", "graph"]
    assert ORPHANED_RECORD not in [f["code"] for f in doc["findings"]]

    assert doctor_main(["--db", str(store_dir)]) == 0
    out = capsys.readouterr().out
    assert "orphaned-records check skipped (no usable index.db — run sidegraph-sync)" in out


def test_a_synced_index_does_not_skip_the_orphan_check(tmp_path, capsys, monkeypatch):
    store_dir, _decision, _fact = _orphaned_store(tmp_path)  # settled
    monkeypatch.chdir(tmp_path)

    doctor_main(["--db", str(store_dir), "--json"])

    assert "orphaned-records" not in json.loads(capsys.readouterr().out)["skipped"]
