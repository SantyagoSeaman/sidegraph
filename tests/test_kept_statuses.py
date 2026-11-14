"""A rebuild keeps the leaf statuses a sync computed, and the hook index reads them while the
index is cold. The fixtures run a real sync, a real canonical edit and a real rebuild."""

from __future__ import annotations

import json
import shutil
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sidegraph.host import hooks
from sidegraph.hot_index import HotIndex
from sidegraph.schema import (
    AnchorBinding,
    Decision,
    DecisionKind,
    DecisionStatus,
    Fact,
    Provenance,
)
from sidegraph.store import VOLATILE_STALE_KEY, Store
from sidegraph.store_layout import KEPT_STATUS_COLUMN
from tests.test_hot_index import NOW, _reload_hot_pair, _Seeder, _synced_hot_pair


def _kept_by_name(root: Path) -> dict[str, list[str | None]]:
    """``{entity canonical name: [kept status of each of its binding rows]}``."""
    conn = sqlite3.connect(root / "index.db")
    try:
        rows = conn.execute(
            f"SELECT e.canonical_name, b.{KEPT_STATUS_COLUMN} FROM anchor_bindings b "
            "JOIN entities e ON e.entity_id = b.entity_id ORDER BY e.canonical_name"
        ).fetchall()
    finally:
        conn.close()
    found: dict[str, list[str | None]] = {}
    for name, kept in rows:
        found.setdefault(name, []).append(kept)
    return found


def _titles(root: Path, entities) -> tuple[list[str], bool]:
    """What the hook index hands over through ``entities``, and whether the cold index keeps
    back a record it would otherwise hand over."""
    index = HotIndex.open(root)
    assert index is not None
    try:
        ids = [e.entity_id for e in entities]
        return [r.decision["title"] for r in index.records_for(ids)], index.cold_withholds(ids)
    finally:
        index.close()


def _counts(root: Path) -> tuple[int, int, int]:
    index = HotIndex.open(root)
    assert index is not None
    try:
        return index.memory_counts()
    finally:
        index.close()


def _read(tmp_path: Path, monkeypatch, capsys, name: str, session: str) -> dict:
    """The real PreToolUse entry point on a Read of ``name``."""
    monkeypatch.setenv("SIDEGRAPH_DIR", str(tmp_path / ".sidegraph"))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    payload = {
        "hook_event_name": "PreToolUse",
        "session_id": session,
        "cwd": str(tmp_path),
        "tool_name": "Read",
        "tool_input": {"file_path": str(tmp_path / name)},
    }
    monkeypatch.setattr(hooks, "_read_payload", lambda: payload)
    hooks.pre_tool_use()
    return json.loads(capsys.readouterr().out)


def _set_flag(root: Path, value: str) -> None:
    conn = sqlite3.connect(root / "index.db")
    try:
        conn.execute("UPDATE meta SET value = ? WHERE key = ?", (value, VOLATILE_STALE_KEY))
        conn.commit()
    finally:
        conn.close()


def test_a_rebuild_keeps_the_computed_status_of_an_unchanged_entity(tmp_path):
    """Red against the unfixed rebuild (no column). The row's own status is still the
    placeholder every other reader sees, and the flag is still set for the next sync."""
    root, _reader, _entities, record = _synced_hot_pair(tmp_path)
    _reload_hot_pair(root, record)
    assert _kept_by_name(root) == {"gone": ["orphaned"], "kept": ["live"]}
    store = Store(root)
    try:
        assert store.get_meta(VOLATILE_STALE_KEY) == "1"
        statuses = {
            b.status
            for e in store.iter_concrete_entities()
            for b in store.bindings_for_entity(e.entity_id)
        }
        assert statuses == {"live"}
    finally:
        store.close()


def test_the_hook_index_delivers_kept_records_on_a_cold_index(tmp_path):
    """Red against the unfixed gate (nothing while cold). The orphan stays silent."""
    root, _reader, entities, record = _synced_hot_pair(tmp_path)
    _reload_hot_pair(root, record)
    assert _titles(root, entities) == (["Legitimate live memory"], False)
    assert _counts(root) == (1, 1, 1)


def test_kept_statuses_survive_a_second_rebuild(tmp_path):
    """Red against a rebuild that keeps only what a warm index holds."""
    root, _reader, entities, record = _synced_hot_pair(tmp_path)
    _reload_hot_pair(root, record)
    data = json.loads(record.read_text(encoding="utf-8"))
    data["context"] = "A second external edit before any sync"
    record.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    Store(root).close()
    assert _kept_by_name(root) == {"gone": ["orphaned"], "kept": ["live"]}
    assert _titles(root, entities) == (["Legitimate live memory"], False)


def test_a_changed_descriptor_is_not_kept(tmp_path):
    """Red against a rebuild that keeps a status by entity id alone: the status was computed
    for the old descriptor."""
    root, _reader, entities, _record = _synced_hot_pair(tmp_path)
    kept = entities[0]
    path = root / "entities" / f"{kept.entity_id}.json"
    body = json.loads(path.read_text(encoding="utf-8"))
    body["descriptor"]["file_path"] = "moved.py"
    path.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
    Store(root).close()
    assert _kept_by_name(root) == {"gone": ["orphaned"], "kept": [None]}
    assert _titles(root, entities) == ([], True)


def test_a_later_sync_verdict_beats_a_kept_status(tmp_path):
    """Red against a gate that trusts the kept status alone while the index is cold. A sync
    after the rebuild found the entity gone, then the flag was set again without a rebuild (a
    domain ratification does that): the row's own status is the newer verdict."""
    from sidegraph.sync import _set_leaf_status

    root, _reader, entities, record = _synced_hot_pair(tmp_path)
    _reload_hot_pair(root, record)
    store = Store(root)
    try:
        store.set_meta(VOLATILE_STALE_KEY, "0")
        _set_leaf_status(entities[0].entity_id, store, "orphaned")
        store.set_meta(VOLATILE_STALE_KEY, "1")
    finally:
        store.close()
    assert _kept_by_name(root)["kept"] == ["live"]  # the column still holds the older verdict
    assert _titles(root, entities) == ([], False)
    assert _counts(root) == (0, 0, 0)  # the SubagentStart brief reads the same rule


def test_a_kept_orphan_does_not_outlive_the_sync_that_healed_the_anchor(tmp_path):
    """Red against a rebuild that reads a kept status on a warm index too: the first rebuild
    kept ``orphaned``; a sync then healed the anchor and cleared the flag; the row's own status
    is the newer verdict, and the next rebuild must keep ``live``, not the old column."""
    from sidegraph.sync import _set_leaf_status

    root, _reader, entities, record = _synced_hot_pair(tmp_path)
    _reload_hot_pair(root, record)
    assert _kept_by_name(root)["gone"] == ["orphaned"]
    store = Store(root)
    try:
        _set_leaf_status(entities[1].entity_id, store, "live")  # a sync rebinds the anchor
        store.set_meta(VOLATILE_STALE_KEY, "0")  # and completes
    finally:
        store.close()
    _reload_hot_pair(root, record)  # a second rebuild, from a warm index
    assert _kept_by_name(root)["gone"] == ["live"]
    assert sorted(_titles(root, entities)[0]) == [
        "Legitimate live memory",
        "Orphan must stay silent",
    ]


def test_an_index_without_the_column_withholds_while_cold(tmp_path):
    """Red against a gate that reads a missing column as kept. An older version's rebuild
    recreates the table without the column and resets every status."""
    root, _reader, entities, record = _synced_hot_pair(tmp_path)
    _reload_hot_pair(root, record)
    conn = sqlite3.connect(root / "index.db")
    try:
        conn.execute(f"ALTER TABLE anchor_bindings DROP COLUMN {KEPT_STATUS_COLUMN}")
        conn.commit()
    finally:
        conn.close()
    assert _titles(root, entities) == ([], True)
    _set_flag(root, "0")  # computed statuses in an index without the column: read as ever
    assert sorted(_titles(root, entities)[0]) == [
        "Legitimate live memory",
        "Orphan must stay silent",
    ]


def test_a_new_record_on_an_unchanged_entity_is_delivered_before_a_sync(tmp_path):
    """Red against a rebuild that keeps statuses per binding row: a record that arrives by a
    pull is new to this index, and its entity's status is already computed."""
    root, _reader, entities, _record = _synced_hot_pair(tmp_path)
    elsewhere = tmp_path / "elsewhere" / ".sidegraph"
    shutil.copytree(root, elsewhere)
    other = Store(elsewhere)
    try:
        pulled = other.add_decision(
            Decision(
                title="Arrived by a pull",
                kind=DecisionKind.GOTCHA,
                status=DecisionStatus.ACCEPTED,
                context="Written in another checkout",
                choice="Deliver it through the entity already checked",
                valid_from=NOW,
                provenance=Provenance(source="manual"),
            )
        )
        other.add_binding(
            AnchorBinding(record_id=pulled.id, entity_id=entities[0].entity_id, tier=2)
        )
    finally:
        other.close()
    for part in ("decisions", "bindings"):
        for path in (elsewhere / part).glob(f"{pulled.id}*"):
            shutil.copy(path, root / part / path.name)
    Store(root).close()
    titles, withheld = _titles(root, entities)
    assert sorted(titles) == ["Arrived by a pull", "Legitimate live memory"]
    assert not withheld


def test_a_format_1_rebuild_keeps_statuses_too(tmp_path):
    """Red against a change that reaches only one of the two rebuild functions: a per-record
    (format 1) store rebuilds through the other one."""
    from tests.store_format2_support import format1_store

    format1_store(tmp_path / ".sidegraph")
    root, _reader, entities, record = _synced_hot_pair(tmp_path)
    store = Store(root)
    try:
        assert store._major < 2
    finally:
        store.close()
    _reload_hot_pair(root, record)
    assert _kept_by_name(root) == {"gone": ["orphaned"], "kept": ["live"]}
    assert _titles(root, entities) == (["Legitimate live memory"], False)


def test_compaction_keeps_the_statuses(tmp_path):
    """Red against the unfixed rebuild: maintenance rebuilds the index, and the hook index
    handed nothing over until the next sync."""
    root, _reader, entities, _record = _synced_hot_pair(tmp_path)
    store = Store(root)
    try:
        report = store.maintain(settled_days=0, dry_run=False, any_branch=True)
        assert report.stopped is None, report.stopped
        assert store.get_meta(VOLATILE_STALE_KEY) == "1"  # the index was rebuilt
    finally:
        store.close()
    assert _kept_by_name(root) == {"gone": ["orphaned"], "kept": ["live"]}
    assert _titles(root, entities) == (["Legitimate live memory"], False)


def test_an_entity_keeps_the_worst_of_its_leaf_statuses(tmp_path):
    """Red against a rebuild that keeps the best verdict of an entity's rows: the row a sync
    found orphaned reads ``live`` again after the rebuild, and a kept ``live`` would deliver
    through it."""
    root, _reader, entities, record = _synced_hot_pair(tmp_path)
    store = Store(root)
    try:
        second = store.add_decision(
            Decision(
                title="Second record on the same entity",
                kind=DecisionKind.GOTCHA,
                status=DecisionStatus.ACCEPTED,
                context="Two leaf rows on one entity",
                choice="One of them was found orphaned",
                valid_from=NOW,
                provenance=Provenance(source="manual"),
            )
        )
        store.add_binding(
            AnchorBinding(
                record_id=second.id, entity_id=entities[0].entity_id, tier=2, status="orphaned"
            )
        )
        assert store.get_meta(VOLATILE_STALE_KEY) == "0"
    finally:
        store.close()
    _reload_hot_pair(root, record)
    assert _kept_by_name(root)["kept"] == ["orphaned", "orphaned"]
    assert _titles(root, entities) == ([], False)
    assert _counts(root) == (0, 0, 0)


def test_a_descriptor_changed_in_place_is_not_kept(tmp_path, monkeypatch, capsys):
    """Red against a rebuild that compares only the two descriptors it can see: the entity's
    descriptor was changed through the store before the rebuild, so the old index already
    held the new one, and the status in its rows was computed for the old one."""
    root, _reader, entities, record = _synced_hot_pair(tmp_path)
    store = Store(root)
    try:
        entity = store.get_entity(entities[0].entity_id)
        entity.descriptor.file_path = "elsewhere.py"
        store.upsert_entity(entity)
        assert store.get_meta(VOLATILE_STALE_KEY) == "0"
    finally:
        store.close()
    (tmp_path / "elsewhere.py").write_text("def kept():\n    return 1\n", encoding="utf-8")
    _reload_hot_pair(root, record)
    assert _kept_by_name(root)["kept"] == [None]
    assert _titles(root, entities) == ([], True)
    assert _read(tmp_path, monkeypatch, capsys, "elsewhere.py", "changed-descriptor") == {}
    conn = sqlite3.connect(root / "index.db")
    try:
        marks = conn.execute("SELECT COUNT(*) FROM meta WHERE key LIKE 'descriptor_changed:%'")
        assert marks.fetchone()[0] == 0  # the rebuild used the mark and deleted it
    finally:
        conn.close()


def test_a_descriptor_changed_in_place_on_a_cold_index_stops_its_kept_status(tmp_path):
    """Red against a hook index that reads the kept status without the mark: the rebuild kept
    ``live`` for the old descriptor, and the entity now names another file."""
    root, _reader, entities, record = _synced_hot_pair(tmp_path)
    _reload_hot_pair(root, record)
    assert _titles(root, entities) == (["Legitimate live memory"], False)
    store = Store(root)
    try:
        entity = store.get_entity(entities[0].entity_id)
        entity.descriptor.file_path = "elsewhere.py"
        store.upsert_entity(entity)
        assert store.get_meta(VOLATILE_STALE_KEY) == "1"
    finally:
        store.close()
    assert _kept_by_name(root)["kept"] == ["live"]  # the column is as the rebuild left it
    assert _titles(root, entities) == ([], True)
    assert _counts(root) == (0, 0, 0)


def test_a_kept_status_is_the_last_syncs_verdict_until_the_next_sync(tmp_path, monkeypatch, capsys):
    """This test documents a behaviour; it is not a defect to fix. A kept status is what the
    last sync computed, against the graph that sync saw. When the code changes after that sync
    (here the symbol leaves the graph) and a rebuild follows, the hook still delivers through
    the anchor, exactly as an index that is not cold does, until the next sync finds it gone.
    Red against a rebuild that checks the graph, which this design does not do."""
    from sidegraph.engine.reader import GraphifyReader
    from sidegraph.sync import sync

    root, _reader, _entities, record = _synced_hot_pair(tmp_path)
    graph = tmp_path / "graphify-out" / "graph.json"
    body = json.loads(graph.read_text(encoding="utf-8"))
    body["nodes"] = []
    graph.write_text(json.dumps(body) + "\n", encoding="utf-8")
    _reload_hot_pair(root, record)
    before = _read(tmp_path, monkeypatch, capsys, "live.py", "before-the-sync")
    assert "Legitimate live memory" in before["hookSpecificOutput"]["additionalContext"]
    store = Store(root)
    try:
        sync(store, GraphifyReader(graph), force=True)
        statuses = {
            binding.status
            for entity in store.iter_concrete_entities()
            for binding in store.bindings_for_entity(entity.entity_id)
        }
        assert statuses == {"orphaned"}
    finally:
        store.close()
    assert _read(tmp_path, monkeypatch, capsys, "live.py", "after-the-sync") == {}


def test_the_cause_check_follows_the_delivery_rules(tmp_path, monkeypatch):
    """One entity per record kind. On a cold index with nothing kept, the cause check says
    "withheld" exactly for the entities whose record an index that is not cold hands over.
    Red against a cause check with rules of its own."""
    root = tmp_path / ".sidegraph"
    store = Store(root)
    seed = _Seeder(store)
    past = datetime.now(UTC) - timedelta(days=1)
    superseded = seed.decision("Superseded", DecisionKind.ADR)
    cases = {
        "accepted": seed.decision("Accepted", DecisionKind.GOTCHA),
        "proposed": seed.decision("Proposed", DecisionKind.ADR, DecisionStatus.PROPOSED),
        "proposed-old": seed.decision(
            "Proposed long ago", DecisionKind.ADR, DecisionStatus.PROPOSED, age_days=400
        ),
        "expired": seed.decision("Expired", DecisionKind.GOTCHA, age_days=3, valid_to=past),
        "superseded": superseded,
        "rejected": seed.decision("Rejected", DecisionKind.ADR, DecisionStatus.REJECTED),
        "fact": store.add_fact(
            Fact(
                statement="Sample n=20 completed in 8 ms.",
                source="Synthetic fixture.",
                status="accepted",
                valid_from=datetime.now(UTC),
                provenance=Provenance(source="agent"),
            )
        ),
    }
    seed.decision("Successor", DecisionKind.ADR, supersedes=superseded.id)
    entity_of = {
        name: seed.anchor(record, f"{name}.py").entity_id for name, record in cases.items()
    }
    store.close()

    def answers(flag: str, unratified: str | None) -> dict[str, tuple[bool, bool]]:
        conn = sqlite3.connect(root / "index.db")
        conn.execute("UPDATE meta SET value = ? WHERE key = ?", (flag, VOLATILE_STALE_KEY))
        conn.commit()
        conn.close()
        if unratified is None:
            monkeypatch.delenv("SIDEGRAPH_UNRATIFIED", raising=False)
        else:
            monkeypatch.setenv("SIDEGRAPH_UNRATIFIED", unratified)
        index = HotIndex.open(root)
        assert index is not None
        try:
            return {
                name: (bool(index.records_for([entity])), index.cold_withholds([entity]))
                for name, entity in entity_of.items()
            }
        finally:
            index.close()

    for unratified in (None, "off"):
        warm = answers("0", unratified)
        cold = answers("1", unratified)
        assert all(not withheld for _, withheld in warm.values())  # nothing is cold
        assert all(not delivered for delivered, _ in cold.values())  # nothing was kept
        assert {name: withheld for name, (_, withheld) in cold.items()} == {
            name: delivered for name, (delivered, _) in warm.items()
        }
    assert warm["accepted"][0] and warm["fact"][0] and not warm["proposed"][0]


def test_the_cause_check_stops_at_the_first_record_that_qualifies(tmp_path, monkeypatch):
    """Sixty records behind one cold entity. The read for delivery fetches no row and looks no
    record up; the cause check looks one record up and stops. Red against a cause found by
    loading every withheld record."""
    root = tmp_path / ".sidegraph"
    store = Store(root)
    seed = _Seeder(store)
    entity = seed.anchor(seed.decision("Record 0", DecisionKind.GOTCHA), "a.py")
    for number in range(1, 60):
        seed.anchor(seed.decision(f"Record {number}", DecisionKind.GOTCHA), "a.py", entity=entity)
    assert store.get_meta(VOLATILE_STALE_KEY) == "1"
    store.close()
    looked_up: list[str] = []
    original = HotIndex._decisions_for_bindings

    def counting(self, record_ids, now):
        looked_up.extend(record_ids)
        return original(self, record_ids, now)

    monkeypatch.setattr(HotIndex, "_decisions_for_bindings", counting)
    index = HotIndex.open(root)
    assert index is not None
    try:
        assert index.records_for([entity.entity_id]) == []
        assert looked_up == []
        assert index.cold_withholds([entity.entity_id])
        assert len(looked_up) == 1
    finally:
        index.close()


def test_the_cause_reads_never_wait_for_a_lock(tmp_path):
    """Red against a cause read made under the handle's busy timeout: another process takes
    the lock after the delivery read, and the run must not be kept waiting to learn why it
    was empty. The timeout here is three seconds, so a read that waits fails the bound."""
    root = tmp_path / ".sidegraph"
    store = Store(root)
    seed = _Seeder(store)
    entity = seed.anchor(seed.decision("Uncomputed memory", DecisionKind.GOTCHA), "a.py")
    assert store.get_meta(VOLATILE_STALE_KEY) == "1"
    store.close()
    index = HotIndex.open(root, busy_timeout_seconds=3.0)
    assert index is not None
    holder = sqlite3.connect(root / "index.db")
    try:
        holder.execute("BEGIN EXCLUSIVE")
        started = time.monotonic()
        with pytest.raises(sqlite3.OperationalError):
            index.cold_withholds([entity.entity_id])
        with pytest.raises(sqlite3.OperationalError):
            index.has_meta_now("pretool_file:s:-:a.py")
        assert time.monotonic() - started < 1.5
        assert not index.lock_lost  # nothing the run delivers depended on these reads
        holder.rollback()
        assert index.cold_withholds([entity.entity_id])
        assert not index.has_meta_now("pretool_file:s:-:a.py")
    finally:
        holder.close()
        index.close()


def test_the_cause_scan_of_the_counts_never_waits_for_a_lock(tmp_path):
    """Red against a cause scan made under the handle's busy timeout: the counts are read,
    another process takes the lock, and the scan that would say why they are zero is given
    up at once. ``cold_withheld`` is then ``None``: not read."""
    root = tmp_path / ".sidegraph"
    store = Store(root)
    seed = _Seeder(store)
    seed.anchor(seed.decision("Uncomputed memory", DecisionKind.GOTCHA), "a.py")
    store.close()
    index = HotIndex.open(root, busy_timeout_seconds=3.0)
    assert index is not None
    conn = index._conn
    holder = sqlite3.connect(root / "index.db")

    class LockBeforeTheCauseScan:
        binding_reads = 0

        def execute(self, sql, parameters=()):
            if "FROM anchor_bindings" in sql:
                self.binding_reads += 1
                if self.binding_reads == 2:
                    holder.execute("BEGIN EXCLUSIVE")
            return conn.execute(sql, parameters)

        def close(self):
            conn.close()

    index._conn = LockBeforeTheCauseScan()
    try:
        started = time.monotonic()
        assert index.memory_counts() == (0, 0, 0)
        assert time.monotonic() - started < 1.5
        assert index.cold_withheld is None
        assert not index.lock_lost
    finally:
        holder.rollback()
        holder.close()
        index.close()
