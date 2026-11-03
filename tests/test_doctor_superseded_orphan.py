"""``superseded-no-live-successor``: a superseded record whose successors are all rejected.

The broken shape is built with raw canonical writes: the propose-then-drop sequence that
produced it no longer closes the predecessor (a pending successor closes nothing), so only a
store written before that fix keeps it. Doctor reads raw files, so the tests pin exactly the
fields it uses.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from ulid import ULID

from sidegraph.doctor import SUPERSEDED_WITHOUT_LIVE_SUCCESSOR, curate
from tests.store_format2_support import (
    decision_body,
    fact_body,
    make_store,
    put_archive,
    put_file,
    put_shard_line,
)

NOW = datetime(2026, 7, 23, 12, 0, 0, tzinfo=UTC)
PAST = "2026-01-01T00:00:00+00:00"
_NEXT = [0]


def _rid() -> str:
    _NEXT[0] += 1
    return str(ULID.from_datetime(datetime(2026, 7, 1, 0, 0, _NEXT[0], tzinfo=UTC)))


def _payload(rid: str, status: str, supersedes: str | None, kind: str = "decision") -> dict:
    """A full, model-valid body: the check withholds itself while verify reports an unreadable
    record, so a stub would only test that."""
    closed = status in ("superseded", "rejected")
    body = decision_body if kind == "decision" else fact_body
    return body(rid, status=status, supersedes=supersedes, valid_to=PAST if closed else None)


def _put(root: Path, subdir: str, status: str, supersedes: str | None = None) -> str:
    rid = _rid()
    path = root / subdir / f"{rid}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _payload(rid, status, supersedes, subdir.removesuffix("s"))
    path.write_text(json.dumps(payload), encoding="utf-8")
    return rid


def _findings(root: Path):
    return [
        f for f in curate(root, now=NOW).findings if f.code == SUPERSEDED_WITHOUT_LIVE_SUCCESSOR
    ]


@pytest.mark.parametrize(
    ("subdir", "noun"), [("decisions", "decision"), ("facts", "fact")], ids=["decision", "fact"]
)
def test_only_rejected_successor_is_reported(tmp_path, subdir, noun):
    old = _put(tmp_path, subdir, "superseded")
    _put(tmp_path, subdir, "rejected", supersedes=old)
    [finding] = _findings(tmp_path)
    assert finding.record_id == old
    assert finding.record_type == noun
    assert finding.path == str(tmp_path / subdir / f"{old}.json")
    assert "every successor is rejected" in finding.detail
    assert f"add_{noun}" in finding.detail
    assert f"supersede_{noun}" in finding.detail  # says why the obvious heal is refused


@pytest.mark.parametrize("successor_status", ["accepted", "proposed", "superseded"])
@pytest.mark.parametrize("subdir", ["decisions", "facts"])
def test_chain_with_a_continuing_successor_is_healthy(tmp_path, subdir, successor_status):
    old = _put(tmp_path, subdir, "superseded")
    _put(tmp_path, subdir, successor_status, supersedes=old)
    assert _findings(tmp_path) == []


def test_one_live_successor_among_rejected_is_healthy(tmp_path):
    old = _put(tmp_path, "decisions", "superseded")
    _put(tmp_path, "decisions", "rejected", supersedes=old)
    _put(tmp_path, "decisions", "accepted", supersedes=old)
    assert _findings(tmp_path) == []


def test_rejected_successor_of_a_live_record_is_not_reported(tmp_path):
    old = _put(tmp_path, "decisions", "accepted")
    _put(tmp_path, "decisions", "rejected", supersedes=old)
    assert _findings(tmp_path) == []


def test_superseded_without_any_successor_is_left_to_verify(tmp_path):
    _put(tmp_path, "decisions", "superseded")
    assert _findings(tmp_path) == []


def test_archived_rejected_successor_counts(tmp_path):
    old = _put(tmp_path, "decisions", "superseded")
    line = {"record_type": "decision", **_payload(_rid(), "rejected", old)}
    (tmp_path / "archive").mkdir()
    (tmp_path / "archive" / "2026-01.jsonl").write_text(json.dumps(line) + "\n", encoding="utf-8")
    assert [f.record_id for f in _findings(tmp_path)] == [old]


def test_archived_live_successor_keeps_the_chain_healthy(tmp_path):
    old = _put(tmp_path, "decisions", "superseded")
    line = {"record_type": "decision", **_payload(_rid(), "superseded", old)}
    (tmp_path / "archive").mkdir()
    (tmp_path / "archive" / "2026-01.jsonl").write_text(json.dumps(line) + "\n", encoding="utf-8")
    assert _findings(tmp_path) == []


# -- successors whose status cannot be read ------------------------------------------------


@pytest.mark.parametrize("fmt", [1, 2])
@pytest.mark.parametrize("kind", ["decision", "fact"])
def test_successor_with_an_unreadable_status_suppresses_the_finding(tmp_path, fmt, kind):
    body = decision_body if kind == "decision" else fact_body
    store = make_store(tmp_path, fmt=fmt)
    old, rejected, unknown = (str(ULID()) for _ in range(3))
    put_file(store, kind, body(old, status="superseded", valid_to=PAST))
    put_file(store, kind, body(rejected, status="rejected", supersedes=old, valid_to=PAST))
    put_file(store, kind, body(unknown, status={}, supersedes=old))  # model-invalid
    assert _findings(store) == []


@pytest.mark.parametrize("fmt", [1, 2])
def test_any_model_invalid_record_withholds_the_check(tmp_path, fmt):
    store = _seed_pair(tmp_path, fmt)
    put_file(store, "decision", decision_body(str(ULID()), status={}))  # supersedes nothing
    assert _findings(store) == []


def _seed_pair(tmp_path, fmt):
    """A store holding the broken shape: ``OLD`` superseded, its only successor rejected."""
    store = make_store(tmp_path, fmt=fmt)
    put_file(store, "decision", decision_body("OLD", status="superseded", valid_to=PAST))
    put_file(
        store, "decision", decision_body("R", status="rejected", supersedes="OLD", valid_to=PAST)
    )
    assert [f.record_id for f in _findings(store)] == ["OLD"]
    return store


@pytest.mark.parametrize("fmt", [1, 2])
def test_archived_successor_with_unreadable_status_withholds_the_check(tmp_path, fmt):
    store = _seed_pair(tmp_path, fmt)
    line = {"record_type": "decision", **decision_body("U", status={}, supersedes="OLD")}
    put_archive(store, "2026-01-01-1-deadbeef0000", [line])
    assert _findings(store) == []


@pytest.mark.parametrize("fmt", [1, 2])
def test_corrupt_archive_segment_withholds_the_check(tmp_path, fmt):
    store = _seed_pair(tmp_path, fmt)
    put_archive(store, "2026-01-01-1-deadbeef0000", ['{"record_type":"decision","id":"U"'])
    assert _findings(store) == []


@pytest.mark.parametrize("defect", ["filename", "window"])
@pytest.mark.parametrize("kind", ["decision", "fact"])
def test_successor_the_reader_omits_withholds_the_check(tmp_path, monkeypatch, kind, defect):
    """An accepted successor that fails the store's own checks (its file is named for another
    id, or its validity window is backwards) is left out of the effective records on a format-2
    store, although repairing it would heal the chain."""
    body = decision_body if kind == "decision" else fact_body
    store = make_store(tmp_path, fmt=2)
    put_file(store, kind, body("OLD", status="superseded", valid_to=PAST))
    put_file(store, kind, body("R", status="rejected", supersedes="OLD", valid_to=PAST))
    if defect == "filename":
        put_file(store, kind, body("S", status="accepted", supersedes="OLD"), record_id="WRONG")
    else:
        broken = body("S", status="accepted", supersedes="OLD", valid_to="2000-01-01T00:00:00Z")
        put_file(store, kind, broken)
    # The pair does reach the advice when the guard is out of the way ...
    monkeypatch.setattr("sidegraph.doctor._record_sources_unreadable", lambda _root: False)
    assert [f.record_id for f in _findings(store)] == ["OLD"]
    monkeypatch.undo()
    # ... and is withheld while verify reports the defective successor.
    assert _findings(store) == []


def test_verify_that_cannot_run_withholds_the_check(tmp_path, monkeypatch):
    store = _seed_pair(tmp_path, 1)

    def refuse(_root):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr("sidegraph.doctor.verify_snapshot", refuse)
    assert _findings(store) == []


def test_undecodable_shard_line_withholds_the_check(tmp_path):
    store = _seed_pair(tmp_path, 2)
    (store / "shards").mkdir()
    (store / "shards" / "00.shard").write_text("{", "utf-8")
    assert _findings(store) == []


@pytest.mark.parametrize(
    ("fmt", "where"), [(1, "archive"), (2, "archive"), (2, "shard")], ids=lambda v: str(v)
)
def test_permission_denied_source_withholds_the_check(tmp_path, monkeypatch, fmt, where):
    store = _seed_pair(tmp_path, fmt)
    body = decision_body("U", status="deprecated", supersedes="OLD", valid_to=PAST)
    if where == "archive":
        blocked = put_archive(store, "seg", [{"record_type": "decision", **body}])
    else:
        blocked = put_shard_line(store, "decision", "U", body)
    real_bytes, real_text = Path.read_bytes, Path.read_text

    def deny(real):
        def read(self, *args, **kwargs):
            if self == blocked:
                raise PermissionError(13, "Permission denied", str(self))
            return real(self, *args, **kwargs)

        return read

    monkeypatch.setattr(Path, "read_bytes", deny(real_bytes))
    monkeypatch.setattr(Path, "read_text", deny(real_text))
    assert _findings(store) == []


@pytest.mark.parametrize("fmt", [1, 2])
def test_undecodable_successor_file_suppresses_the_finding(tmp_path, fmt):
    store = make_store(tmp_path, fmt=fmt)
    old, rejected = (str(ULID()) for _ in range(2))
    put_file(store, "decision", decision_body(old, status="superseded", valid_to=PAST))
    put_file(
        store, "decision", decision_body(rejected, status="rejected", supersedes=old, valid_to=PAST)
    )
    (store / "decisions" / f"{ULID()}.json").write_text('{"id": "x", "status": "acc', "utf-8")
    assert _findings(store) == []


@pytest.mark.parametrize("subdir", ["decisions", "facts"])
def test_deprecated_successor_is_a_deliberate_retirement(tmp_path, subdir):
    old = _put(tmp_path, subdir, "superseded")
    _put(tmp_path, subdir, "deprecated", supersedes=old)
    assert _findings(tmp_path) == []


# -- one finding per record, at its most specific file -------------------------------------


def test_record_in_both_hot_and_archive_is_reported_once_at_the_hot_file(tmp_path):
    store = make_store(tmp_path, fmt=1)
    old, rejected = (str(ULID()) for _ in range(2))
    hot = put_file(store, "decision", decision_body(old, status="superseded", valid_to=PAST))
    put_file(
        store, "decision", decision_body(rejected, status="rejected", supersedes=old, valid_to=PAST)
    )
    twin = {"record_type": "decision", **decision_body(old, status="superseded", valid_to=PAST)}
    put_archive(store, "2026-01-01-1-deadbeef0000", [twin])
    [finding] = _findings(store)
    assert finding.path == str(hot)


def test_archive_only_record_points_at_its_segment(tmp_path):
    store = make_store(tmp_path, fmt=1)
    old, rejected = (str(ULID()) for _ in range(2))
    lines = [
        {"record_type": "decision", **decision_body(old, status="superseded", valid_to=PAST)},
        {
            "record_type": "decision",
            **decision_body(rejected, status="rejected", supersedes=old, valid_to=PAST),
        },
    ]
    segment = put_archive(store, "2026-01-01-1-deadbeef0000", lines)
    [finding] = _findings(store)
    assert finding.path == str(segment)


def test_malformed_payload_values_do_not_crash(tmp_path):
    old = _put(tmp_path, "decisions", "superseded")
    bad = {"id": [1], "status": {}, "supersedes": [old]}
    (tmp_path / "decisions" / f"{_rid()}.json").write_text(json.dumps(bad), encoding="utf-8")
    other = {"id": _rid(), "status": ["superseded"], "supersedes": old}
    (tmp_path / "decisions" / f"{other['id']}.json").write_text(json.dumps(other), encoding="utf-8")
    assert _findings(tmp_path) == []
