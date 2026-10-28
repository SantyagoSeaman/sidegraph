"""Packed doctor findings identify logical subjects independently of shared shard paths."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from ulid import ULID

from sidegraph.cli import doctor_main
from sidegraph.doctor import curate, open_curation_index
from sidegraph.migrate import migrate
from sidegraph.schema import AnchorBinding, Decision, Descriptor, Entity, Provenance
from sidegraph.store import VOLATILE_STALE_KEY, Store
from sidegraph.store_format import bucket
from tests.store_format2_support import commit_all, format1_store, git_repo  # noqa: F401

# ruff: noqa: F811


@pytest.mark.parametrize("packed", [False, True])
def test_shared_source_and_commit_findings_keep_distinct_ids(git_repo, packed, capsys):
    source = git_repo / "code.py"
    source.write_text("value = 1\n", encoding="utf-8")
    capture = commit_all(git_repo, "source", date="2026-01-01T00:00:00Z")
    root = format1_store(git_repo / ".sidegraph")
    first = str(ULID())
    second = str(ULID())
    while second == first or bucket(first, 64) != bucket(second, 64):
        second = str(ULID())
    ids = {first, second}
    with Store(root) as store:
        entity = store.upsert_entity(
            Entity(canonical_name="code", descriptor=Descriptor(name="code", file_path="code.py"))
        )
        for rid in ids:
            store.add_decision(
                Decision(
                    id=rid,
                    title="Keep value",
                    kind="gotcha",
                    status="accepted",
                    context="Synthetic shared scope",
                    choice="Keep one",
                    valid_from=datetime.now(UTC),
                    provenance=Provenance(source="manual", commit=capture),
                )
            )
            store.add_binding(AnchorBinding(record_id=rid, entity_id=entity.entity_id, tier=2))
    source.write_text("value = 2\n", encoding="utf-8")
    commit_all(git_repo, "source changed", date="2026-01-02T00:00:00Z")
    if packed:
        migrate(root, shards=64, dry_run=False, any_branch=True)
    with Store(root) as store:
        store.record_retrieval([], ["code.py"])
        store.set_meta(VOLATILE_STALE_KEY, "0")
    assert doctor_main(["--db", str(root), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    for code in ("code-drift", "never-surfaced"):
        rows = [r for r in report["findings"] if r["code"] == code]
        assert len(rows) == 2
        assert {r["record_id"] for r in rows} == ids
        assert {r["record_type"] for r in rows} == {"decision"}
        if packed:
            assert len({r["path"] for r in rows}) == 1
            assert "/shards/" in rows[0]["path"]
    assert doctor_main(["--db", str(root), "--flat"]) == 0
    lines = [
        line for line in capsys.readouterr().out.splitlines() if line.startswith("code-drift  ")
    ]
    assert len(lines) == 2 and lines[0] != lines[1]
    assert all(any(f"decision:{rid}" in line for line in lines) for rid in ids)
    # The default form groups the same records by file under one header.
    assert doctor_main(["--db", str(root)]) == 0
    grouped = capsys.readouterr().out
    assert "code-drift: 2 record(s) in 1 file group(s)" in grouped
    assert all(rid in grouped for rid in ids)
    # Binding and aggregate orphaned-record checks must retain the record too.
    with Store(root) as store:
        store._conn.execute(
            "UPDATE anchor_bindings SET data=json_set(data, '$.status', 'orphaned')"
        )
        store._conn.commit()
    conn = open_curation_index(root)
    assert conn is not None
    try:
        findings = curate(root, index=conn).findings
    finally:
        conn.close()
    for code in ("orphaned-binding", "orphaned-record"):
        rows = [r for r in findings if r.code == code]
        assert {r.record_id for r in rows} == ids
        assert all(
            r.record_type == ("bindings" if code == "orphaned-binding" else "decision")
            for r in rows
        )


@pytest.mark.parametrize("field", ["record_id", "entity_id"])
def test_blob_identity_in_derived_binding_row_does_not_crash_json(tmp_path, capsys, field):
    import sqlite3

    from tests.test_cli_doctor import _seed_healthy

    root = _seed_healthy(tmp_path)
    with Store(root) as store:
        store.set_meta(VOLATILE_STALE_KEY, "0")
        # Field names above are literals; only the corrupt value is bound.
        store._conn.execute(
            f"UPDATE anchor_bindings SET {field}=?, data=json_set(data, '$.status', 'orphaned')",
            (sqlite3.Binary(b"malformed-index-identity"),),
        )
        store._conn.commit()
    assert doctor_main(["--db", str(root), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert not any(row["code"] == "orphaned-binding" for row in report["findings"])


@pytest.mark.parametrize("check", ["expired", "unratified"])
def test_unreadable_record_id_never_produces_a_partial_subject(tmp_path, check):
    from sidegraph import doctor

    record = {
        "id": [],
        "status": "accepted",
        "valid_from": "2026-01-01T00:00:00Z",
        "valid_to": "2026-01-02T00:00:00Z",
        "provenance": {"source": "agent"},
    }
    if check == "expired":
        rows = doctor._check_expired_open([(tmp_path / "bad.json", record)], datetime.now(UTC))
    else:
        stamp = {"ratified_at": "2025-01-01T00:00:00Z"}
        rows = doctor._check_unratified_accepts(
            [(tmp_path / "stamp.json", stamp), (tmp_path / "bad.json", record)],
            [],
            tmp_path,
            {},
            [],
        )
    assert len(rows) == 1
    assert rows[0].record_id is None and rows[0].record_type is None
