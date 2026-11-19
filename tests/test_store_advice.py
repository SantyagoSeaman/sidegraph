"""Portal advice journal and canonical hashes (Portal design D9)."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import sidegraph.store as store_module
from sidegraph.store import Store
from sidegraph.store_format import encode_body
from tests.store_format2_support import (
    decision_body,
    domain_body,
    format1_store,
    make_store,
    put_archive,
    put_file,
    put_shard_line,
)
from tests.test_host_session_key import _run_session_start


def test_advice_survives_reload(tmp_path: Path) -> None:
    path = make_store(tmp_path)
    put_file(path, "decision", decision_body("D1"))
    store = Store(path)
    assert store.write_advice([("D1", "accept", "Supported by evidence.")], "agent") == {
        "D1": "recorded"
    }
    rows = store.advice_rows()
    assert len(rows) == 1
    assert rows[0]["record_hash"] == store.record_body_hash("D1")
    assert rows[0]["session_id"] == "agent"

    put_file(path, "decision", decision_body("D2"))
    store.refresh_if_stale()

    assert store.get_decision("D2") is not None
    assert store.advice_rows() == rows


def test_prune_removes_old_rows_in_all_five_journals(tmp_path, monkeypatch, capsys) -> None:
    path = make_store(tmp_path)
    put_file(path, "decision", decision_body("D1"))
    store = Store(path)
    store.record_touch("agent", "file.py", "Read")
    store.record_render_event(
        "agent",
        intent=None,
        selected=1,
        emitted=1,
        degraded=0,
        dropped_for_budget=0,
        chars_used=12,
        had_rejected=False,
        had_superseded=False,
    )
    store.record_session_transcript("agent", "transcript.jsonl", "claude")
    store.write_advice([("D1", "reject", "Unsupported.")], None)
    store.put_operation("key", "digest", "D1", None, ["new"])
    assert set(Store._JOURNAL_TABLES) == {
        "retrieval_events",
        "render_events",
        "session_transcripts",
        "ratify_advice",
        "portal_operations",
    }
    old = (datetime.now(UTC) - timedelta(days=31)).isoformat()
    with store._mutation():
        for table in Store._JOURNAL_TABLES:
            assert store._conn.execute(f"UPDATE {table} SET at = ?", (old,)).rowcount == 1
    store.close()

    _run_session_start(monkeypatch, capsys, {"session_id": "prune-on-start"}, path)

    store = Store(path)
    for table in Store._JOURNAL_TABLES:
        assert store._conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


def test_body_hash_stable_across_pack_and_mtime(tmp_path: Path) -> None:
    # A minimal legacy body exercises omitted defaults and exact canonical encoding.
    path = make_store(tmp_path)
    body = decision_body("D1", title="Почему 🧭")
    hot = put_file(path, "decision", body)
    store = Store(path)
    expected = hashlib.sha256(encode_body(body).encode()).hexdigest()
    assert store.record_body_hash("D1") == expected

    hot.touch()
    store.refresh_if_stale()
    assert store.record_body_hash("D1") == expected

    put_shard_line(path, "decision", "D1", body)
    hot.unlink()
    store.refresh_if_stale()
    assert store.get_decision("D1") is not None
    assert store.record_body_hash("D1") == expected


def test_body_hash_changes_with_body(tmp_path: Path) -> None:
    path = make_store(tmp_path)
    body = decision_body("D1")
    put_shard_line(path, "decision", "D1", body)
    store = Store(path)
    original = store.record_body_hash("D1")
    changed = decision_body("D1", choice="Updated choice.")
    put_file(path, "decision", changed)
    store.refresh_if_stale()

    assert store.record_body_hash("D1") != original
    assert store.record_body_hash("D1") == hashlib.sha256(encode_body(changed).encode()).hexdigest()
    assert store.record_body_hash("missing") is None


def test_domain_advice_hash_excludes_communities(tmp_path: Path) -> None:
    path = make_store(tmp_path)
    body = domain_body("DM1")
    put_file(path, "domain", {**body, "communities": ["one"]})
    with Store(path) as store:
        expected = hashlib.sha256(encode_body(body).encode()).hexdigest()
        assert store.write_advice([("DM1", "accept", "Reviewed.")], None) == {"DM1": "recorded"}
        assert store.advice_rows()[0]["record_hash"] == expected
        assert store.record_body_hash("DM1") == expected

        put_file(path, "domain", {**body, "communities": ["two"]})
        assert store.record_body_hash("DM1") == expected


def test_body_hash_follows_format1_archive_fallback(tmp_path: Path) -> None:
    path = format1_store(tmp_path / "store")
    archived = decision_body("D1", status="rejected", valid_to="2026-01-02T00:00:00Z")
    put_archive(path, "old", [{"record_type": "decision", **archived}])
    put_file(path, "decision", {"id": "D1"})

    with Store(path) as store:
        assert store.get_decision("D1") is not None
        assert (
            store.record_body_hash("D1")
            == hashlib.sha256(encode_body(archived).encode()).hexdigest()
        )


@pytest.mark.parametrize(("before", "after"), [(1, 2), (2, 1)])
def test_body_hash_uses_the_inventory_format(tmp_path: Path, before: int, after: int) -> None:
    path = make_store(tmp_path, fmt=before)
    archived = decision_body("D1", status="rejected", valid_to="2026-01-02T00:00:00Z")
    put_archive(path, "old", [{"record_type": "decision", **archived}])
    put_file(path, "decision", {"id": "D1"})

    with Store(path) as store:
        make_store(tmp_path, fmt=after)
        expected = (
            hashlib.sha256(encode_body(archived).encode()).hexdigest() if after == 1 else None
        )
        assert store.record_body_hash("D1") == expected


def test_advice_refuses_only_the_unencodable_body(tmp_path: Path) -> None:
    path = make_store(tmp_path)
    put_file(path, "decision", decision_body("D1"))
    malformed = put_file(path, "decision", decision_body("D2"))
    malformed.write_text(
        json.dumps(decision_body("D2", extra=chr(0xD800))) + "\n", encoding="utf-8"
    )
    with Store(path) as store:
        assert store.write_advice(
            [("D1", "accept", "Reviewed."), ("D2", "edit", "Review.")], None
        ) == {
            "D1": "recorded",
            "D2": "error: record body is unavailable",
        }
        assert [row["record_id"] for row in store.advice_rows()] == ["D1"]
        assert store.record_body_hash("D2") is None


def test_write_advice_checks_effective_status_after_external_change(tmp_path: Path) -> None:
    path = make_store(tmp_path)
    put_file(path, "decision", decision_body("D1"))
    with Store(path) as store:
        put_file(path, "decision", decision_body("D1", status="accepted"))

        assert store.write_advice([("D1", "accept", "Reviewed.")], None) == {
            "D1": "error: record is not proposed"
        }
        assert store.advice_rows() == []


def test_advice_batch_reads_the_inventory_once(tmp_path, monkeypatch) -> None:
    path = make_store(tmp_path)
    for rid in ("D1", "D2"):
        put_file(path, "decision", decision_body(rid))
    with Store(path) as store:
        original = store_module.inventory_from_disk
        reads = 0

        def counted(*args, **kwargs):
            nonlocal reads
            reads += 1
            return original(*args, **kwargs)

        monkeypatch.setattr(store_module, "inventory_from_disk", counted)
        assert store.write_advice(
            [("D1", "accept", "First."), ("D2", "edit", "Second.")], None
        ) == {
            "D1": "recorded",
            "D2": "recorded",
        }
        assert reads == 1


def test_advice_fingerprint_changes_on_older_at_and_on_prune(tmp_path, monkeypatch) -> None:
    path = make_store(tmp_path)
    for rid in ("D1", "D2"):
        put_file(path, "decision", decision_body(rid))
    store = Store(path)
    store.write_advice([("D1", "accept", "Supported.")], None)
    before = store.advice_fingerprint()
    earlier = datetime.now(UTC) - timedelta(days=31)

    class EarlierClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return earlier

    with monkeypatch.context() as clock:
        clock.setattr("sidegraph.store.datetime", EarlierClock)
        store.write_advice([("D2", "edit", "Check the wording.")], None)
    after_write = store.advice_fingerprint()
    assert after_write != before
    assert store.advice_rows()[1]["at"] == earlier.isoformat()

    assert store.prune_telemetry_events() == 1
    assert store.advice_fingerprint() != after_write
    assert store.advice_fingerprint() == before
