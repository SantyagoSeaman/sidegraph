"""Contract tests for doctor.curate — advisory curation lint (see
design/superpowers/specs/2026-07-23-sidegraph-doctor-design.md).

Stores are hand-built minimal JSON files, NOT Store-seeded: curate reads raw canonical
files without schema validation, so tests pin exactly the fields each check consumes.
"""

import inspect
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ulid import ULID

from sidegraph import doctor
from sidegraph.doctor import (
    BINDING_STATUS_CHECK,
    DANGLING_RECORD,
    DEGRADED_BINDING,
    EXPIRED_OPEN_VALIDITY,
    NEVER_SURFACED_CHECK,
    ORPHANED_BINDING,
    STALE_PROPOSAL,
    UNREFERENCED_ENTITY,
    curate,
)

NOW = datetime(2026, 7, 23, 12, 0, 0, tzinfo=UTC)


def _ulid_at(dt: datetime) -> str:
    return str(ULID.from_datetime(dt))


def _write(root: Path, rel: str, payload: object) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload), encoding="utf-8")


def _decision(root: Path, rid: str, status: str = "accepted", valid_to: str | None = None) -> None:
    _write(root, f"decisions/{rid}.json", {"id": rid, "status": status, "valid_to": valid_to})


def _fact(root: Path, rid: str, valid_to: str | None = None) -> None:
    _write(root, f"facts/{rid}.json", {"id": rid, "valid_to": valid_to})


def _bind(root: Path, rid: str, entity_id: str = "E1") -> None:
    _write(root, f"bindings/{rid}.json", [{"entity_id": entity_id, "tier": 2, "weight": 1.0}])


def _codes(report) -> list[str]:
    return [f.code for f in report.findings]


# -- empty / trivially healthy -------------------------------------------------------------


def test_empty_store_dir_yields_no_findings(tmp_path):
    report = curate(tmp_path, now=NOW)
    assert report.findings == []
    # no index.db in a bare dir -- both index-only checks skip
    assert report.skipped == [BINDING_STATUS_CHECK, NEVER_SURFACED_CHECK]


# -- dangling-record -----------------------------------------------------------------------


def test_open_decision_without_bindings_file_is_dangling(tmp_path):
    rid = _ulid_at(NOW)
    _decision(tmp_path, rid, status="accepted")
    report = curate(tmp_path, now=NOW)
    assert _codes(report) == [DANGLING_RECORD]
    assert rid in report.findings[0].detail


def test_open_decision_with_empty_bindings_list_is_dangling(tmp_path):
    rid = _ulid_at(NOW)
    _decision(tmp_path, rid, status="proposed")
    _write(tmp_path, f"bindings/{rid}.json", [])
    assert _codes(curate(tmp_path, now=NOW)) == [DANGLING_RECORD]


def test_bound_decision_is_not_dangling(tmp_path):
    rid = _ulid_at(NOW)
    _decision(tmp_path, rid)
    _bind(tmp_path, rid)
    assert DANGLING_RECORD not in _codes(curate(tmp_path, now=NOW))


def test_terminal_decisions_are_not_dangling(tmp_path):
    # superseded/rejected/deprecated records awaiting compaction are not curation targets
    for status in ("superseded", "rejected", "deprecated"):
        rid = _ulid_at(NOW)
        _decision(tmp_path, rid, status=status)
    assert DANGLING_RECORD not in _codes(curate(tmp_path, now=NOW))


def test_open_fact_without_bindings_is_dangling(tmp_path):
    rid = _ulid_at(NOW)
    _fact(tmp_path, rid, valid_to=None)
    assert _codes(curate(tmp_path, now=NOW)) == [DANGLING_RECORD]


def test_closed_fact_is_not_dangling(tmp_path):
    rid = _ulid_at(NOW)
    _fact(tmp_path, rid, valid_to="2026-01-01T00:00:00+00:00")
    assert DANGLING_RECORD not in _codes(curate(tmp_path, now=NOW))


def test_dangling_findings_are_path_sorted(tmp_path):
    r1 = _ulid_at(NOW - timedelta(seconds=2))
    r2 = _ulid_at(NOW - timedelta(seconds=1))
    _decision(tmp_path, r2)
    _decision(tmp_path, r1)
    report = curate(tmp_path, now=NOW)
    assert [f.path for f in report.findings] == sorted(f.path for f in report.findings)


# -- stale-proposal ------------------------------------------------------------------------


def test_old_proposed_decision_is_stale(tmp_path):
    rid = _ulid_at(NOW - timedelta(days=31))
    _decision(tmp_path, rid, status="proposed")
    _bind(tmp_path, rid)
    report = curate(tmp_path, now=NOW)
    assert _codes(report) == [STALE_PROPOSAL]
    assert "31 days" in report.findings[0].detail


def test_proposal_exactly_at_threshold_is_not_stale(tmp_path):
    rid = _ulid_at(NOW - timedelta(days=30))
    _decision(tmp_path, rid, status="proposed")
    _bind(tmp_path, rid)
    assert STALE_PROPOSAL not in _codes(curate(tmp_path, now=NOW))


def test_stale_days_parameter_is_honored(tmp_path):
    rid = _ulid_at(NOW - timedelta(days=10))
    _decision(tmp_path, rid, status="proposed")
    _bind(tmp_path, rid)
    assert STALE_PROPOSAL in _codes(curate(tmp_path, stale_days=5, now=NOW))
    assert STALE_PROPOSAL not in _codes(curate(tmp_path, stale_days=30, now=NOW))


def test_old_accepted_decision_is_not_stale(tmp_path):
    rid = _ulid_at(NOW - timedelta(days=100))
    _decision(tmp_path, rid, status="accepted")
    _bind(tmp_path, rid)
    assert STALE_PROPOSAL not in _codes(curate(tmp_path, now=NOW))


def test_old_proposed_domain_is_stale(tmp_path):
    did = _ulid_at(NOW - timedelta(days=40))
    _write(tmp_path, f"domains/{did}.json", {"domain_id": did, "status": "proposed"})
    report = curate(tmp_path, now=NOW)
    assert _codes(report) == [STALE_PROPOSAL]
    assert "domain" in report.findings[0].detail


def test_non_ulid_id_never_flags_and_never_crashes(tmp_path):
    _write(tmp_path, "decisions/not-a-ulid.json", {"id": "not-a-ulid", "status": "proposed"})
    _bind(tmp_path, "not-a-ulid")
    assert STALE_PROPOSAL not in _codes(curate(tmp_path, now=NOW))


# -- unreferenced-entity -------------------------------------------------------------------


def test_entity_referenced_by_no_anchor_set_is_flagged(tmp_path):
    _write(tmp_path, "entities/E1.json", {"entity_id": "E1", "canonical_name": "alpha"})
    report = curate(tmp_path, now=NOW)
    assert _codes(report) == [UNREFERENCED_ENTITY]
    assert "alpha" in report.findings[0].detail


def test_referenced_entity_is_not_flagged(tmp_path):
    rid = _ulid_at(NOW)
    _write(tmp_path, "entities/E1.json", {"entity_id": "E1", "canonical_name": "alpha"})
    _decision(tmp_path, rid)
    _bind(tmp_path, rid, entity_id="E1")
    assert UNREFERENCED_ENTITY not in _codes(curate(tmp_path, now=NOW))


def test_structural_entities_are_exempt(tmp_path):
    # domain:* pairs a Domain row; community:* is legacy derived leftover — neither flags
    _write(tmp_path, "entities/E1.json", {"entity_id": "E1", "canonical_name": "domain:auth"})
    _write(tmp_path, "entities/E2.json", {"entity_id": "E2", "canonical_name": "community:42"})
    assert UNREFERENCED_ENTITY not in _codes(curate(tmp_path, now=NOW))


def test_non_list_bindings_payload_is_ignored_not_fatal(tmp_path):
    # a hand-broken bindings file (JSON object, not list) contributes no references
    _write(tmp_path, "entities/E1.json", {"entity_id": "E1", "canonical_name": "alpha"})
    _write(tmp_path, "bindings/whatever.json", {"entity_id": "E1"})
    assert _codes(curate(tmp_path, now=NOW)) == [UNREFERENCED_ENTITY]


# -- expired-open-validity -----------------------------------------------------------------


def test_accepted_decision_with_past_valid_to_is_expired_open(tmp_path):
    rid = _ulid_at(NOW)
    _decision(tmp_path, rid, status="accepted", valid_to="2026-01-01T00:00:00+00:00")
    _bind(tmp_path, rid)
    report = curate(tmp_path, now=NOW)
    assert _codes(report) == [EXPIRED_OPEN_VALIDITY]
    assert "accepted" in report.findings[0].detail


def test_valid_to_exactly_now_is_not_expired(tmp_path):
    rid = _ulid_at(NOW)
    _decision(tmp_path, rid, status="accepted", valid_to=NOW.isoformat())
    _bind(tmp_path, rid)
    assert EXPIRED_OPEN_VALIDITY not in _codes(curate(tmp_path, now=NOW))


def test_future_valid_to_is_not_expired(tmp_path):
    rid = _ulid_at(NOW)
    _decision(tmp_path, rid, status="accepted", valid_to="2030-01-01T00:00:00+00:00")
    _bind(tmp_path, rid)
    assert EXPIRED_OPEN_VALIDITY not in _codes(curate(tmp_path, now=NOW))


def test_superseded_decision_with_past_valid_to_is_not_expired_open(tmp_path):
    rid = _ulid_at(NOW)
    _decision(tmp_path, rid, status="superseded", valid_to="2026-01-01T00:00:00+00:00")
    assert EXPIRED_OPEN_VALIDITY not in _codes(curate(tmp_path, now=NOW))


def test_fact_with_past_valid_to_is_not_expired_open(tmp_path):
    # decisions only: Fact.status is deprecated-unused, valid_to set = closed by convention
    rid = _ulid_at(NOW)
    _fact(tmp_path, rid, valid_to="2026-01-01T00:00:00+00:00")
    assert EXPIRED_OPEN_VALIDITY not in _codes(curate(tmp_path, now=NOW))


def test_garbage_and_naive_valid_to_never_flag_or_crash(tmp_path):
    r1 = _ulid_at(NOW - timedelta(seconds=1))
    r2 = _ulid_at(NOW)
    _decision(tmp_path, r1, status="accepted", valid_to="not-a-timestamp")
    _decision(tmp_path, r2, status="accepted", valid_to="2026-01-01T00:00:00")  # naive
    _bind(tmp_path, r1)
    _bind(tmp_path, r2)
    assert EXPIRED_OPEN_VALIDITY not in _codes(curate(tmp_path, now=NOW))


# -- resilience ----------------------------------------------------------------------------


def test_corrupt_json_files_are_silently_skipped(tmp_path):
    # surfacing corruption is verify_snapshot's parse-error job; doctor must not crash
    (tmp_path / "decisions").mkdir(parents=True)
    (tmp_path / "decisions" / "bad.json").write_text("{not json", encoding="utf-8")
    (tmp_path / "bindings").mkdir()
    (tmp_path / "bindings" / "bad.json").write_text("[broken", encoding="utf-8")
    assert curate(tmp_path, now=NOW).findings == []


def test_now_defaults_to_current_time(tmp_path):
    rid = _ulid_at(datetime.now(UTC) - timedelta(days=365))
    _decision(tmp_path, rid, status="proposed")
    _bind(tmp_path, rid)
    assert STALE_PROPOSAL in _codes(curate(tmp_path))


# -- degraded-binding / orphaned-binding (index.db, read-only) -----------------------------


def _index_with_bindings(root: Path, rows: list[tuple[str, str, str]]) -> None:
    """Minimal index.db exposing the anchor_bindings shape doctor queries:
    (record_id, entity_id, data-JSON-with-status)."""
    conn = sqlite3.connect(root / "index.db")
    conn.execute("CREATE TABLE anchor_bindings (record_id TEXT, entity_id TEXT, data TEXT)")
    for record_id, entity_id, status in rows:
        conn.execute(
            "INSERT INTO anchor_bindings VALUES (?, ?, ?)",
            (record_id, entity_id, json.dumps({"status": status})),
        )
    conn.commit()
    conn.close()


def test_missing_index_db_skips_binding_status_check(tmp_path):
    report = curate(tmp_path, now=NOW)
    assert report.skipped == [BINDING_STATUS_CHECK, NEVER_SURFACED_CHECK]
    assert report.findings == []


def test_degraded_and_orphaned_bindings_are_reported(tmp_path):
    _index_with_bindings(
        tmp_path, [("R1", "E1", "degraded"), ("R2", "E2", "orphaned"), ("R3", "E3", "live")]
    )
    report = curate(tmp_path, now=NOW)
    # this hand-built index has anchor_bindings but no retrieval_shows/retrieval_seeds
    # (those are Store-created tables), so never-surfaced skips while binding-status runs.
    assert report.skipped == [NEVER_SURFACED_CHECK]
    assert sorted(_codes(report)) == [DEGRADED_BINDING, ORPHANED_BINDING]
    degraded = next(f for f in report.findings if f.code == DEGRADED_BINDING)
    assert "as of last sync" in degraded.detail
    assert "R1" in degraded.detail and "E1" in degraded.detail
    assert degraded.path == "bindings/R1.json"


def test_live_only_index_yields_no_binding_findings(tmp_path):
    _index_with_bindings(tmp_path, [("R1", "E1", "live")])
    report = curate(tmp_path, now=NOW)
    assert report.findings == []
    assert report.skipped == [NEVER_SURFACED_CHECK]  # no retrieval_shows/retrieval_seeds table


def test_corrupt_index_db_is_skipped_not_fatal(tmp_path):
    (tmp_path / "index.db").write_bytes(b"this is not a sqlite database at all")
    report = curate(tmp_path, now=NOW)
    assert report.skipped == [BINDING_STATUS_CHECK, NEVER_SURFACED_CHECK]
    assert report.findings == []


def test_index_without_anchor_bindings_table_is_skipped(tmp_path):
    conn = sqlite3.connect(tmp_path / "index.db")
    conn.execute("CREATE TABLE something_else (x TEXT)")
    conn.commit()
    conn.close()
    assert curate(tmp_path, now=NOW).skipped == [BINDING_STATUS_CHECK, NEVER_SURFACED_CHECK]


def test_non_json_binding_row_is_skipped_others_still_report(tmp_path):
    conn = sqlite3.connect(tmp_path / "index.db")
    conn.execute("CREATE TABLE anchor_bindings (record_id TEXT, entity_id TEXT, data TEXT)")
    conn.execute("INSERT INTO anchor_bindings VALUES ('R0', 'E0', 'not json')")
    conn.execute(
        "INSERT INTO anchor_bindings VALUES ('R1', 'E1', ?)",
        (json.dumps({"status": "orphaned"}),),
    )
    conn.commit()
    conn.close()
    assert _codes(curate(tmp_path, now=NOW)) == [ORPHANED_BINDING]


def test_index_read_leaves_file_bytes_untouched(tmp_path):
    _index_with_bindings(tmp_path, [("R1", "E1", "degraded")])
    before = (tmp_path / "index.db").read_bytes()
    curate(tmp_path, now=NOW)
    assert (tmp_path / "index.db").read_bytes() == before
    # store's DELETE journal + mode=ro creates no -wal/-shm sidecars either
    assert sorted(p.name for p in tmp_path.iterdir()) == ["index.db"]


def test_doctor_opens_the_index_without_immutable():
    """D8: the coverage journal makes index.db a write target on every tool call, and
    doctor runs mid-session. `immutable=1` disables locking and change detection, which is
    only sound on a quiescent file; the store is DELETE-journal, so plain `mode=ro` locks
    correctly and adds no sidecars. Asserted over BOTH connect sites — fixing one and
    leaving the other is the single-call-site trap this repo keeps falling into."""
    source = Path(inspect.getfile(doctor)).read_text()
    assert "immutable=1" not in source, "both connect strings and both docstrings must drop it"
    # The URI form specifically (not just "mode=ro" anywhere) -- two docstring mentions
    # alone would satisfy a looser substring count without proving either connect string
    # still opens read-only.
    assert source.count('?mode=ro"') >= 2, "both connect strings still open read-only"
