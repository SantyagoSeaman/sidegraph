"""dangling-record's fact branch (design D4/D5/D6, external review Defect B): an anchor is
not the only reachability shape a fact can have. The write path (capture.py:293-300) also
accepts a standalone fact with a bare ``supports`` id, and retrieval renders such a fact
inline for any decision that reaches a bucket (i.e. every LIVE one — see
retrieval.rank_decisions and its ``evidence=False`` calls for the terminal exception). The
OLD anchor-only rule for facts contradicted that and flagged every one of them: measured on
this repo's own store, all 9 ``dangling-record`` findings were exactly this false positive
(an accepted fact, one ``supports`` id, pointing at an accepted, anchored decision).

Hand-built minimal JSON files, same convention as ``test_doctor.py`` (curate reads raw
canonical files with no schema validation — these tests pin exactly the fields each branch
consumes).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from ulid import ULID

from sidegraph.doctor import DANGLING_RECORD, curate

NOW = datetime(2026, 7, 23, 12, 0, 0, tzinfo=UTC)


def _ulid_at(dt: datetime) -> str:
    return str(ULID.from_datetime(dt))


def _write(root: Path, rel: str, payload: object) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload), encoding="utf-8")


def _decision(root: Path, rid: str, status: str = "accepted") -> None:
    _write(root, f"decisions/{rid}.json", {"id": rid, "status": status, "valid_to": None})


def _fact(root: Path, rid: str, supports: list[str] | None = None) -> None:
    _write(root, f"facts/{rid}.json", {"id": rid, "valid_to": None, "supports": supports or []})


def _bind(root: Path, rid: str, entity_id: str = "E1") -> None:
    _write(root, f"bindings/{rid}.json", [{"entity_id": entity_id, "tier": 2, "weight": 1.0}])


def _write_archive_segment(root: Path, name: str, records: list[dict]) -> None:
    lines = [json.dumps(r) for r in records]
    p = root / "archive" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _codes(report) -> list[str]:
    return [f.code for f in report.findings]


def _only_detail(report) -> str:
    assert len(report.findings) == 1, report.findings
    return report.findings[0].detail


# -- silent: reachable via a live supports id, or via an anchor -----------------------------


def test_anchorless_fact_with_live_supports_is_silent(tmp_path):
    did = _ulid_at(NOW)
    _decision(tmp_path, did, status="accepted")
    _bind(tmp_path, did)  # keep the decision itself off the dangling list too (D7 branch)
    fid = _ulid_at(NOW)
    _fact(tmp_path, fid, supports=[did])
    assert DANGLING_RECORD not in _codes(curate(tmp_path, now=NOW))


def test_anchored_fact_with_dead_supports_is_silent(tmp_path):
    """D4/D7 parity for facts: an anchor alone is reachability, same as for a decision -- a
    fact need not ALSO have a live supports id once it has its own anchor."""
    fid = _ulid_at(NOW)
    _fact(tmp_path, fid, supports=["01DOESNOTEXISTATALLXXXXXX"])
    _bind(tmp_path, fid)
    assert DANGLING_RECORD not in _codes(curate(tmp_path, now=NOW))


# -- reported: unreachable, with distinct wording per D5/D6 ----------------------------------


def test_anchorless_fact_with_no_supports_is_reported(tmp_path):
    fid = _ulid_at(NOW)
    _fact(tmp_path, fid, supports=[])
    report = curate(tmp_path, now=NOW)
    assert _codes(report) == [DANGLING_RECORD]
    assert "no supporting decision" in _only_detail(report)


def test_anchorless_fact_supports_resolving_to_nothing_is_reported_with_d5_wording(tmp_path):
    fid = _ulid_at(NOW)
    _fact(tmp_path, fid, supports=["01NOSUCHDECISIONEXISTSXX"])
    report = curate(tmp_path, now=NOW)
    assert _codes(report) == [DANGLING_RECORD]
    assert "does not exist" in _only_detail(report)


def test_anchorless_fact_supports_only_terminal_is_reported_with_d6_wording(tmp_path):
    did = _ulid_at(NOW)
    _decision(tmp_path, did, status="superseded")
    fid = _ulid_at(NOW)
    _fact(tmp_path, fid, supports=[did])
    report = curate(tmp_path, now=NOW)
    assert _codes(report) == [DANGLING_RECORD]
    detail = _only_detail(report)
    assert "list_facts" in detail
    assert "does not exist" not in detail


def test_mixed_missing_and_terminal_supports_lands_in_the_missing_row(tmp_path):
    """Pinned so a change to branch precedence shows up here rather than silently: a
    genuinely missing id takes precedence over a merely-terminal one in the wording."""
    did = _ulid_at(NOW)
    _decision(tmp_path, did, status="rejected")
    fid = _ulid_at(NOW)
    _fact(tmp_path, fid, supports=["01MISSINGDECISIONNOPE00", did])
    report = curate(tmp_path, now=NOW)
    assert _codes(report) == [DANGLING_RECORD]
    assert "does not exist" in _only_detail(report)


def test_decision_with_no_anchors_is_still_reported(tmp_path):
    """D7: unchanged for decisions -- a decision has no ``supports`` field, so the
    anchor-only test that already existed stays correct for that branch."""
    did = _ulid_at(NOW)
    _decision(tmp_path, did, status="accepted")
    report = curate(tmp_path, now=NOW)
    assert _codes(report) == [DANGLING_RECORD]


# -- archived decisions: resolution must include them, not only hot ones --------------------


def test_fact_supporting_an_archived_terminal_decision_is_reported_with_d6_not_d5(tmp_path):
    """Doctor reads decisions/*.json directly; compact() moves terminal decisions into
    archive/*.jsonl. Without resolving the archive too, this fact would be reported as
    supporting "a decision that does not exist" -- which sends the reader hunting for
    corruption that is not there. The verdict doesn't change (still reported), but the
    WORDING must, which is exactly the failure D5's distinct wording exists to prevent."""
    did = _ulid_at(NOW)
    _write_archive_segment(
        tmp_path, "seg1.jsonl", [{"record_type": "decision", "id": did, "status": "superseded"}]
    )
    fid = _ulid_at(NOW)
    _fact(tmp_path, fid, supports=[did])
    report = curate(tmp_path, now=NOW)
    assert _codes(report) == [DANGLING_RECORD]
    detail = _only_detail(report)
    assert "does not exist" not in detail
    assert "list_facts" in detail
