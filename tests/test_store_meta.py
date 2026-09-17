from datetime import UTC, datetime

import pytest

from sidegraph.schema import (
    SCHEMA_VERSION,
    AnchorBinding,
    Decision,
    DecisionKind,
    Descriptor,
    Entity,
    EntityKind,
    Provenance,
)
from sidegraph.store import Store


@pytest.fixture
def make_decision():
    """Decision-factory fixture — same idiom as ``tests/test_store.py``'s ``_decision``
    helper, exposed as a fixture so tests in this file can request it directly."""

    def _make(**overrides) -> Decision:
        base = dict(
            title="test decision",
            kind=DecisionKind.ADR,
            context="c",
            choice="ch",
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="test"),
        )
        base.update(overrides)
        return Decision(**base)

    return _make


def test_store_stamped_0_4_0_reloads_index_instead_of_raising(tmp_path, make_decision):
    # A pre-facts store must survive the 0.5.0 upgrade: the derived index is rebuilt
    # from canonical files and re-stamped, never hard-failed.
    # see design/superpowers/specs/2026-07-10-facts-layer-design.md
    with Store(tmp_path / "s") as store:
        d = store.add_decision(make_decision(title="pre-upgrade decision"))
    import sqlite3

    conn = sqlite3.connect(tmp_path / "s" / "index.db")
    conn.execute("UPDATE meta SET value = '0.4.0' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()
    with Store(tmp_path / "s") as reopened:
        assert reopened.schema_version == SCHEMA_VERSION
        assert reopened.get_decision(d.id) is not None


def test_store_stamped_0_5_0_reloads_index_instead_of_raising(tmp_path, make_decision):
    # A pre-derived-community-bindings store must survive the 0.6.0 upgrade: the derived
    # index is rebuilt from canonical files and re-stamped, never hard-failed.
    # see design/superpowers/specs/2026-07-10-derived-community-bindings-design.md
    with Store(tmp_path / "s") as store:
        d = store.add_decision(make_decision(title="pre-upgrade decision"))
    import sqlite3

    conn = sqlite3.connect(tmp_path / "s" / "index.db")
    conn.execute("UPDATE meta SET value = '0.5.0' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()
    with Store(tmp_path / "s") as reopened:
        assert reopened.schema_version == SCHEMA_VERSION
        assert reopened.get_decision(d.id) is not None


def test_store_stamped_0_4_0_with_real_old_ddl_reloads_instead_of_crashing(tmp_path, make_decision):
    # Regression test for a real (not forged) 0.4.0 store: this branch renamed the
    # anchor_bindings index column decision_id -> record_id in _SCHEMA_SQL, but
    # `CREATE TABLE IF NOT EXISTS` is a no-op against an EXISTING table with the OLD
    # column name. The prior 0.4.0 reload test only forged the version STAMP on a store
    # whose index.db was built by NEW code (so its anchor_bindings table already had
    # `record_id`) — it never exercised the real old DDL, so it missed this crash:
    #   sqlite3.OperationalError: table anchor_bindings has no column named record_id
    # see design/superpowers/specs/2026-07-10-facts-layer-design.md
    session_id = "session-real-0.4.0"
    with Store(tmp_path / "s") as store:
        entity = store.upsert_entity(Entity(canonical_name="Widget", kind=EntityKind.CONCRETE))
        decision = store.add_decision(make_decision(title="pre-upgrade decision"))
        store.add_binding(AnchorBinding(record_id=decision.id, entity_id=entity.entity_id, tier=1))
        store.mark_captured(session_id)

    import sqlite3

    conn = sqlite3.connect(tmp_path / "s" / "index.db")
    conn.execute("DROP TABLE anchor_bindings")
    conn.execute(
        """
        CREATE TABLE anchor_bindings (
            decision_id TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            data TEXT NOT NULL,
            PRIMARY KEY (decision_id, entity_id)
        )
        """
    )
    conn.execute("UPDATE meta SET value = '0.4.0' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()

    # RED (pre-fix): this raises sqlite3.OperationalError: table anchor_bindings has no
    # column named record_id. GREEN (post-fix): it must not raise at all.
    with Store(tmp_path / "s") as reopened:
        assert reopened.schema_version == SCHEMA_VERSION
        assert reopened.get_decision(decision.id) is not None
        bindings = reopened.bindings_for_record(decision.id)
        assert len(bindings) == 1
        assert bindings[0].entity_id == entity.entity_id
        # pins the don't-drop-capture_sessions rule
        assert reopened.was_captured(session_id) is True


def test_meta_roundtrip_and_upsert(tmp_path):
    s = Store(tmp_path / "t.db")
    assert s.get_meta("last_synced_graph_version") is None
    s.set_meta("last_synced_graph_version", "vA")
    assert s.get_meta("last_synced_graph_version") == "vA"
    s.set_meta("last_synced_graph_version", "vB")  # upsert
    assert s.get_meta("last_synced_graph_version") == "vB"
    assert s.get_meta("schema_version") is not None  # existing key readable


def test_set_meta_rejects_schema_version_overwrite(tmp_path):
    s = Store(tmp_path / "t.db")
    original = s.get_meta("schema_version")
    with pytest.raises(ValueError):
        s.set_meta("schema_version", "9.9.9")
    assert s.get_meta("schema_version") == original


def test_iter_concrete_entities_filters(tmp_path):
    s = Store(tmp_path / "t.db")
    tracked = s.upsert_entity(
        Entity(canonical_name="Trader", descriptor=Descriptor(name="Trader", file_path="a.py"))
    )
    s.upsert_entity(Entity(canonical_name="community:1", kind=EntityKind.ABSTRACT))
    s.upsert_entity(Entity(canonical_name="descriptorless"))
    ids = {e.entity_id for e in s.iter_concrete_entities()}
    assert ids == {tracked.entity_id}
