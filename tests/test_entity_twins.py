"""Identical entities collapsed when read: ``Store.bindings_for_entity_group``.

Twins are entities with equal ``kind`` and equal ``descriptor`` (or equal ``canonical_name``
when they have no descriptor). The store keeps every entity as written; the group is a read-time
view, a per-``Store`` map checked against ``PRAGMA data_version`` inside one read snapshot.
# see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§6.4)
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest

from sidegraph import sync
from sidegraph.schema import (
    AnchorBinding,
    Decision,
    DecisionKind,
    DecisionStatus,
    Descriptor,
    Entity,
    EntityKind,
    Fact,
    Provenance,
)
from sidegraph.server import _find_entity_impl, _get_entity_history_impl
from sidegraph.store import Store
from tests.graph_fixtures import node, reader_of
from tests.store_format2_support import (
    bindings_body,
    canonical_bytes,
    decision_body,
    entity_body,
    make_store,
    put_file,
    put_shard_line,
)

FILE = "src/pkg/mod.py"
LOW, MID, HIGH = (
    "01AAAAAAAAAAAAAAAAAAAAAAAA",
    "01BBBBBBBBBBBBBBBBBBBBBBBB",
    "01CCCCCCCCCCCCCCCCCCCCCCCC",
)
REC1, REC2 = "01" + "R" * 22 + "01", "01" + "R" * 22 + "02"


def _entity(s: Store, eid: str, name: str = "mod.py", file: str | None = FILE, **kw) -> Entity:
    desc = Descriptor(name=name, file_path=file) if file is not None else None
    return s.upsert_entity(Entity(entity_id=eid, canonical_name=name, descriptor=desc, **kw))


def _decision(s: Store, title: str = "a decision") -> Decision:
    return s.add_decision(
        Decision(
            title=title,
            kind=DecisionKind.GOTCHA,
            status=DecisionStatus.ACCEPTED,
            context="c",
            choice="ch",
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual"),
        )
    )


def _bind(s: Store, record, entity, status: str = "live", tier: int = 2) -> AnchorBinding:
    return s.add_binding(
        AnchorBinding(record_id=record.id, entity_id=entity.entity_id, tier=tier, status=status)
    )


def _ids(bindings) -> list[tuple[str, str]]:
    return sorted((b.record_id, b.entity_id) for b in bindings)


# -- what the group is --------------------------------------------------------------------------


def test_group_returns_twins_bindings_once_live_first(tmp_path):
    # red against unfixed code: the twin's bindings are missing (no such method)
    s = Store(tmp_path / ".sidegraph")
    e1, e2 = _entity(s, LOW), _entity(s, MID)
    shared, only_twin, only_self, both_live = (_decision(s, n) for n in "abcd")
    _bind(s, shared, e1, "orphaned")  # the preferred twin's orphaned binding ...
    _bind(s, shared, e2, "live")  # ... never hides the other twin's live one
    _bind(s, only_twin, e2, "degraded")
    _bind(s, only_self, e1, "live")
    _bind(s, both_live, e1, "live")
    _bind(s, both_live, e2, "live")  # equal status: the lookup-preferred twin (lowest id) wins
    expected = sorted(
        [
            (shared.id, e2.entity_id),
            (only_twin.id, e2.entity_id),
            (only_self.id, e1.entity_id),
            (both_live.id, e1.entity_id),
        ]
    )
    for start in (e1, e2):  # asked from either twin: the same answer, one binding per record
        got = s.bindings_for_entity_group(start.entity_id)
        assert _ids(got) == expected
        assert len({b.record_id for b in got}) == len(got)
        assert {b.record_id: b.status for b in got}[shared.id] == "live"


def test_equal_descriptor_different_name_are_twins(tmp_path):
    # red against a twin key that also requires an equal canonical_name (rev 6)
    s = Store(tmp_path / ".sidegraph")
    e1 = _entity(s, LOW, name="mod.py")
    e2 = s.upsert_entity(
        Entity(
            entity_id=MID,
            canonical_name="some other spelling",
            descriptor=Descriptor(name="mod.py", file_path=FILE),
        )
    )
    r1, r2 = _decision(s, "one"), _decision(s, "two")
    _bind(s, r1, e1)
    _bind(s, r2, e2)
    assert _ids(s.bindings_for_entity_group(e1.entity_id)) == _ids([*_both(r1, e1), *_both(r2, e2)])


def _both(record, entity):
    return [AnchorBinding(record_id=record.id, entity_id=entity.entity_id, tier=2)]


@pytest.mark.parametrize("kind", [EntityKind.ABSTRACT, EntityKind.CONCRETE])
def test_descriptorless_twins_need_equal_name(tmp_path, kind):
    # red against a group that leaves descriptor-less entities of either kind alone (the
    # matching pair is not collapsed) and against a descriptor-less key that drops the name
    # (the differently named entity would join); another kind never joins
    s = Store(tmp_path / ".sidegraph")
    other = EntityKind.CONCRETE if kind is EntityKind.ABSTRACT else EntityKind.ABSTRACT
    twin_a, twin_b, different, other_kind = (
        s.upsert_entity(Entity(entity_id=eid, canonical_name=name, kind=k))
        for eid, name, k in (
            (LOW, "tag:alpha", kind),
            (MID, "tag:alpha", kind),
            (HIGH, "tag:beta", kind),
            ("01DDDDDDDDDDDDDDDDDDDDDDDD", "tag:alpha", other),
        )
    )
    for e, n in ((twin_a, "1"), (twin_b, "2"), (different, "3"), (other_kind, "4")):
        _bind(s, _decision(s, n), e)
    assert len(s.bindings_for_entity_group(twin_a.entity_id)) == 2
    assert len(s.bindings_for_entity_group(twin_b.entity_id)) == 2
    assert len(s.bindings_for_entity_group(different.entity_id)) == 1
    assert len(s.bindings_for_entity_group(other_kind.entity_id)) == 1


def test_different_descriptor_or_kind_is_not_a_twin(tmp_path):
    s = Store(tmp_path / ".sidegraph")
    e1 = _entity(s, LOW)
    other_file = _entity(s, MID, file="src/other/mod.py")
    abstract = s.upsert_entity(
        Entity(
            entity_id=HIGH,
            canonical_name="mod.py",
            kind=EntityKind.ABSTRACT,
            descriptor=Descriptor(name="mod.py", file_path=FILE),
        )
    )
    for e, n in ((e1, "1"), (other_file, "2"), (abstract, "3")):
        _bind(s, _decision(s, n), e)
    for e in (e1, other_file, abstract):
        assert len(s.bindings_for_entity_group(e.entity_id)) == 1


def test_an_id_with_no_entity_row_is_its_own_group(tmp_path):
    s = Store(tmp_path / ".sidegraph")
    e1 = _entity(s, LOW)
    r = _decision(s)
    _bind(s, r, e1)
    assert s.bindings_for_entity_group("01ZZZZZZZZZZZZZZZZZZZZZZZZ") == []


# -- what stays exact ---------------------------------------------------------------------------


def _twin_pair(tmp_path):
    s = Store(tmp_path / ".sidegraph")
    e1, e2 = _entity(s, LOW), _entity(s, MID)
    r1, r2 = _decision(s, "on the first twin"), _decision(s, "on the second twin")
    _bind(s, r1, e1)
    _bind(s, r2, e2)
    return s, e1, e2, r1, r2


def test_bindings_for_entity_stays_exact(tmp_path):
    s, e1, e2, r1, r2 = _twin_pair(tmp_path)
    assert [b.record_id for b in s.bindings_for_entity(e1.entity_id)] == [r1.id]
    assert [b.record_id for b in s.bindings_for_entity(e2.entity_id)] == [r2.id]


def test_full_sync_updates_both_twins_own_bindings(tmp_path):
    # red against a group read inside bindings_for_entity: a status change reaches only one
    # binding of a record both twins hold, and the other twin goes stale
    s = Store(tmp_path / ".sidegraph")
    e1, e2 = _entity(s, LOW), _entity(s, MID)
    r = _decision(s)
    _bind(s, r, e1)
    _bind(s, r, e2)
    sync._set_leaf_status(e2.entity_id, s, "orphaned")
    assert {b.entity_id: b.status for b in s.bindings_for_record(r.id)} == {
        e1.entity_id: "live",
        e2.entity_id: "orphaned",
    }
    sync._set_leaf_status(e1.entity_id, s, "orphaned")
    assert {b.status for b in s.bindings_for_record(r.id)} == {"orphaned"}


def _sync_twins(tmp_path, nodes, status):
    s = Store(tmp_path / ".sidegraph")
    desc = Descriptor(name="foo", file_path="a.py")
    e1 = s.upsert_entity(Entity(entity_id=LOW, canonical_name="foo", descriptor=desc))
    e2 = s.upsert_entity(Entity(entity_id=MID, canonical_name="foo again", descriptor=desc))
    r = _decision(s)
    _bind(s, r, e1, status)
    _bind(s, r, e2, status)
    sync.sync(s, reader_of(tmp_path, *nodes), force=True)
    own = {e1.entity_id, e2.entity_id}  # sync also binds the record to a community entity
    return {b.entity_id: b.status for b in s.bindings_for_record(r.id) if b.entity_id in own}, (
        e1,
        e2,
    )


def test_a_real_sync_updates_both_twins_own_bindings(tmp_path):
    # red against a group read inside bindings_for_entity (one binding per record): the first
    # twin's pass flips the only binding it sees and the other twin stays stale; and against a
    # sync that does nothing
    got, (e1, e2) = _sync_twins(tmp_path, [node("n1", "foo()", "a.py")], "orphaned")
    assert got == {e1.entity_id: "live", e2.entity_id: "live"}  # the symbol is found again
    (tmp_path / "gone").mkdir()
    got, (e1, e2) = _sync_twins(tmp_path / "gone", [node("x", "other()", "b.py")], "live")
    assert got == {e1.entity_id: "orphaned", e2.entity_id: "orphaned"}  # the symbol vanished


def test_file_history_includes_spelling_twin(tmp_path):
    # red against a history moved to the exact group: the full-path spelling twin is lost
    s = Store(tmp_path / ".sidegraph")
    spelling = _entity(s, LOW, name=FILE)  # an older full-path spelling of the same file
    base = _entity(s, MID, name="mod.py")
    for title, e in (
        ("Rebuild the index atomically", spelling),
        ("Keep the store append-only", base),
    ):
        _bind(s, _decision(s, title), e)
    out = _find_entity_impl(s, "mod.py", FILE)
    titles = sorted(h["title"] for h in _get_entity_history_impl(s, out["entity_id"]))
    assert titles == ["Keep the store append-only", "Rebuild the index atomically"]


def test_find_entity_report_stays_exact(tmp_path):
    # red against find_entity's report (server.py) moved to the group
    s, e1, e2, r1, r2 = _twin_pair(tmp_path)
    out = _find_entity_impl(s, "mod.py", FILE)
    assert out["entity_id"] == e1.entity_id  # the _lookup_preference pick
    assert [b["record_id"] for b in out["bindings"]] == [r1.id]


def test_status_filtered_before_twin_choice(tmp_path):
    # red against choosing the twin before filtering status: the preferred twin's orphaned
    # binding would hide the other twin's live one, and the record would vanish
    s = Store(tmp_path / ".sidegraph")
    e1, e2 = _entity(s, LOW), _entity(s, MID)
    d, f = (
        _decision(s),
        s.add_fact(
            Fact(
                statement="s",
                source="observed",
                valid_from=datetime.now(UTC),
                provenance=Provenance(source="manual"),
            )
        ),
    )
    for record in (d, f):
        _bind(s, record, e1, "orphaned")
        _bind(s, record, e2, "live")
    assert [x.id for x in s.valid_decisions_for_entity(e1.entity_id)] == [d.id]
    assert [x.id for x in s.valid_facts_for_entity(e1.entity_id)] == [f.id]


def test_degraded_beats_orphaned_across_twins(tmp_path):
    # red against a reversed status priority (orphaned over degraded): the preferred twin's
    # orphaned binding would win over the other twin's degraded one
    s = Store(tmp_path / ".sidegraph")
    e1, e2 = _entity(s, LOW), _entity(s, MID)
    r = _decision(s)
    _bind(s, r, e1, "orphaned")
    _bind(s, r, e2, "degraded")
    for start in (e1, e2):
        [b] = s.bindings_for_entity_group(start.entity_id)
        assert (b.entity_id, b.status) == (e2.entity_id, "degraded")


def test_superseded_history_reaches_the_other_twin(tmp_path):
    # red against superseded_for_entity on the exact accessor: a decision superseded while
    # anchored through the other twin is missing from this twin's "tried before" history
    s = Store(tmp_path / ".sidegraph")
    e1, e2 = _entity(s, LOW), _entity(s, MID)
    old = _decision(s, "tried and abandoned")
    s.add_decision(
        Decision(
            title="the replacement",
            kind=DecisionKind.GOTCHA,
            status=DecisionStatus.ACCEPTED,
            context="c",
            choice="ch",
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual"),
            supersedes=old.id,
        )
    )
    _bind(s, old, e2)  # only the OTHER twin
    for start in (e1, e2):
        assert [d.id for d in s.superseded_for_entity(start.entity_id)] == [old.id]
    _bind(s, old, e1)  # both twins bind it: still once
    for start in (e1, e2):
        assert [d.id for d in s.superseded_for_entity(start.entity_id)] == [old.id]


# -- the map and its invalidation ---------------------------------------------------------------


def test_second_store_moves_entity_then_mints_twin(tmp_path):
    # red against a map dropped only on reload and own writes: the first Store never reloads
    path = tmp_path / ".sidegraph"
    a, b = Store(path), Store(path)
    e1 = _entity(a, LOW)
    e2 = _entity(a, MID, file="src/other/mod.py")  # not a twin yet
    r1, r2 = _decision(a, "one"), _decision(a, "two")
    _bind(a, r1, e1)
    _bind(a, r2, e2)
    assert _ids(a.bindings_for_entity_group(e1.entity_id)) == [(r1.id, e1.entity_id)]  # warm
    moved = b.get_entity(e2.entity_id)
    assert moved is not None
    moved.descriptor = Descriptor(name="mod.py", file_path=FILE)  # the second Store moves it
    b.upsert_entity(moved)
    assert _ids(a.bindings_for_entity_group(e1.entity_id)) == _ids(_both(r1, e1) + _both(r2, e2))
    e3 = _entity(b, HIGH)  # then mints a twin
    r3 = _decision(b, "three")
    _bind(b, r3, e3)
    assert {r for r, _ in _ids(a.bindings_for_entity_group(e1.entity_id))} == {r1.id, r2.id, r3.id}


def _rival(s: Store):
    """A second connection that tries to commit with no wait; it autocommits each statement."""
    return sqlite3.connect(str(s.path / "index.db"), timeout=0, isolation_level=None)


def _move_e2_out_and_bind_new(s: Store, e2: Entity, record_id: str) -> None:
    """Commit on a second connection: E2 leaves E1's group and gains a binding to ``record_id``."""
    moved = e2.model_copy(
        update={"descriptor": Descriptor(name="mod.py", file_path="src/z/mod.py")}
    )
    conn = _rival(s)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE entities SET data = ? WHERE entity_id = ?",
            (moved.model_dump_json(), e2.entity_id),
        )
        binding = AnchorBinding(record_id=record_id, entity_id=e2.entity_id, tier=2)
        conn.execute(
            "INSERT INTO anchor_bindings (record_id, entity_id, data) VALUES (?, ?, ?)",
            (record_id, e2.entity_id, binding.model_dump_json()),
        )
        conn.execute("COMMIT")
    finally:
        conn.close()


def test_commit_between_begin_and_first_read_on_cache_hit(tmp_path):
    # red against a version read before the snapshot read: a warm map is trusted although a
    # commit landed after BEGIN and before the first table read (spec T-C2 fixture 2)
    s, e1, e2, r1, r2 = _twin_pair(tmp_path)
    assert len(s.bindings_for_entity_group(e1.entity_id)) == 2  # warm the map
    new = _decision(s, "new")
    fired: list[str] = []

    def hook(point: str) -> None:
        fired.append(point)
        if point == "after_begin":
            _move_e2_out_and_bind_new(s, e2, new.id)

    s._twin_group_hook = hook
    got = s.bindings_for_entity_group(e1.entity_id)
    s._twin_group_hook = None
    assert "after_begin" in fired
    assert _ids(got) == [(r1.id, e1.entity_id)]  # E2 out of the group, Rnew absent
    assert _ids(s.bindings_for_entity_group(e1.entity_id)) == [(r1.id, e1.entity_id)]


def _expect_locked_then_commit(s: Store, e2: Entity, record_id: str, locked: list[str], at: str):
    state = {"done": False}

    def hook(point: str) -> None:
        if point != at or state["done"]:
            return
        state["done"] = True
        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            _move_e2_out_and_bind_new(s, e2, record_id)
        locked.append(point)

    return hook


@pytest.mark.parametrize("point", ["after_scan", "after_pragma", "after_first_read"])
def test_commit_between_scan_and_version_read(tmp_path, point):
    # red against a version read after its scan (and a snapshot that is not held): the rival
    # commit is refused while the snapshot is open, lands after the call, and the NEXT call
    # sees the moved version, rebuilds and reflects it. The after_pragma point is also the
    # read-without-a-transaction case: the rival commit would land between the pragma and the
    # binding SELECT, mixing two states.
    s, e1, e2, r1, r2 = _twin_pair(tmp_path)
    new = _decision(s, "new")
    locked: list[str] = []
    s._twin_group_hook = _expect_locked_then_commit(s, e2, new.id, locked, point)
    before = s.bindings_for_entity_group(e1.entity_id)  # cold: the scan runs inside the call
    s._twin_group_hook = None
    assert locked == [point]
    assert _ids(before) == _ids(_both(r1, e1) + _both(r2, e2))  # the snapshot, not the rival
    assert not s._conn.in_transaction  # the group's own snapshot is closed again
    _move_e2_out_and_bind_new(s, e2, new.id)  # the commit now goes through
    assert _ids(s.bindings_for_entity_group(e1.entity_id)) == [(r1.id, e1.entity_id)]


def test_no_nested_transaction_inside_mutation(tmp_path):
    # red against a group that begins its own transaction inside _mutation
    s, e1, e2, r1, r2 = _twin_pair(tmp_path)
    statements: list[str] = []
    with s._mutation():
        s._conn.set_trace_callback(statements.append)
        try:
            got = s.bindings_for_entity_group(e1.entity_id)
            e3 = _entity(s, HIGH)  # an own write inside the transaction
            r3 = _decision(s, "three")
            _bind(s, r3, e3)
            seen = s.bindings_for_entity_group(e1.entity_id)
        finally:
            s._conn.set_trace_callback(None)
        assert s._conn.in_transaction  # the caller's transaction is still open
    assert not any(q.lstrip().upper().startswith(("BEGIN", "COMMIT")) for q in statements)
    assert len(got) == 2
    assert {b.record_id for b in seen} == {r1.id, r2.id, r3.id}


def _move_out(s: Store, e: Entity) -> None:
    """An own write (inside the caller's transaction) that takes ``e`` out of its twin group."""
    s.upsert_entity(
        e.model_copy(update={"descriptor": Descriptor(name="mod.py", file_path="src/z/mod.py")})
    )


def test_map_dropped_on_rollback_to(tmp_path):
    # red against a map kept across ROLLBACK TO (and across a rolled-back _mutation): a map
    # built after an entity moved out of the group would keep it out once the move is undone
    s, e1, e2, r1, r2 = _twin_pair(tmp_path)
    with s._mutation():
        assert len(s.bindings_for_entity_group(e1.entity_id)) == 2  # warm
        with pytest.raises(RuntimeError, match="boom"), s._savepoint():
            _move_out(s, e2)
            assert len(s.bindings_for_entity_group(e1.entity_id)) == 1  # the map has E2 out
            raise RuntimeError("boom")
        assert len(s.bindings_for_entity_group(e1.entity_id)) == 2  # E2 is back
    with pytest.raises(RuntimeError, match="boom"), s._mutation():
        _move_out(s, e2)
        assert len(s.bindings_for_entity_group(e1.entity_id)) == 1
        raise RuntimeError("boom")
    assert len(s.bindings_for_entity_group(e1.entity_id)) == 2


def test_quarantined_twin_not_in_group(tmp_path):
    # an entity occurrence the inventory quarantines has no index row: it is in no group, and
    # asking for its own id returns just what is bound to that id
    path = make_store(tmp_path)
    desc = {"name": "mod.py", "file_path": FILE}
    put_file(path, "entity", entity_body(LOW, canonical_name="mod.py", descriptor=desc))
    put_shard_line(
        path,
        "entity",
        MID,
        entity_body(MID, canonical_name="mod.py", descriptor=desc, kind="bogus"),
    )
    for rid, eid in ((REC1, LOW), (REC2, MID)):
        put_file(path, "decision", decision_body(rid))
        put_file(path, "bindings", bindings_body(eid), record_id=rid)
    with Store(path) as s:
        assert s.is_skipped("entities", MID)
        assert s.get_entity(MID) is None
        assert [b.record_id for b in s.bindings_for_entity_group(LOW)] == [REC1]
        assert [b.record_id for b in s.bindings_for_entity_group(MID)] == [REC2]


def test_no_store_byte_changes(tmp_path):
    path = make_store(tmp_path)
    desc = {"name": "mod.py", "file_path": FILE}
    for eid in (LOW, MID):
        put_file(path, "entity", entity_body(eid, canonical_name="mod.py", descriptor=desc))
    put_file(path, "decision", decision_body(REC1))
    put_file(path, "bindings", bindings_body(MID), record_id=REC1)
    with Store(path) as s:
        before = canonical_bytes(path)
        assert [b.record_id for b in s.bindings_for_entity_group(LOW)] == [REC1]
        s.valid_decisions_for_entity(LOW)
        s.superseded_for_entity(MID)
    assert canonical_bytes(path) == before


def test_a_rollback_mid_snapshot_does_not_hide_the_original_error(tmp_path):
    # red against an unconditional COMMIT in the snapshot's finally: SQLite already ended the
    # transaction, and "cannot commit - no transaction is active" replaces the real error
    s, e1, *_ = _twin_pair(tmp_path)

    def hook(point: str) -> None:
        if point == "after_pragma":
            s._conn.execute("ROLLBACK")
            raise RuntimeError("the original error")

    s._twin_group_hook = hook
    with pytest.raises(RuntimeError, match="the original error"):
        s.bindings_for_entity_group(e1.entity_id)
    s._twin_group_hook = None
    assert len(s.bindings_for_entity_group(e1.entity_id)) == 2  # and the store still works
