import pytest

from sidegraph.schema import SCHEMA_VERSION, Descriptor, canonicalize


def test_schema_version_is_0_6_0():
    assert SCHEMA_VERSION == "0.6.0"


def test_descriptor_fields_are_name_and_file():
    d = Descriptor(name="BitfinexAdapter", file_path="adapters/bitfinex.py")
    assert d.name == "BitfinexAdapter"
    assert d.file_path == "adapters/bitfinex.py"
    assert Descriptor(name="x").file_path is None


def test_canonicalize_lowercases_and_strips_decoration():
    assert canonicalize("BitfinexAdapter") == "bitfinexadapter"
    assert canonicalize("_t()") == "_t"
    assert canonicalize(".__init__()") == "__init__"
    assert canonicalize("  place_order( self ) ") == "place_order"


def test_store_rejects_mismatched_schema_version(tmp_path):
    """A legacy single-file store (see store._migrate_legacy) whose stamped
    schema_version isn't in store._MIGRATABLE_SCHEMA_VERSIONS is a hard rejection — no
    migration path beyond the one forward step from 0.2.x/0.3.x (see
    tests/test_store_migration.py for the versions that DO migrate)."""
    import sqlite3

    from sidegraph.store import Store

    db = tmp_path / "old.db"
    # simulate a store stamped with an older version
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute("INSERT INTO meta VALUES ('schema_version', '0.1.0')")
    conn.commit()
    conn.close()
    with pytest.raises(ValueError, match="schema_version"):
        Store(db)
    # nothing was migrated: the original file is untouched, no backup/canonical dirs appear
    assert db.exists()
    assert not (tmp_path / "old.db.migrated-backup").exists()
    assert not (tmp_path / "decisions").exists()


_PRE_0_3_SCHEMA_SQL = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE entities (
    entity_id TEXT PRIMARY KEY, canonical_name TEXT NOT NULL, data TEXT NOT NULL
);
CREATE TABLE decisions (
    id TEXT PRIMARY KEY, status TEXT NOT NULL, supersedes TEXT, data TEXT NOT NULL
);
CREATE TABLE anchor_bindings (
    decision_id TEXT NOT NULL, entity_id TEXT NOT NULL, data TEXT NOT NULL,
    PRIMARY KEY (decision_id, entity_id)
);
CREATE TABLE initiatives (id TEXT PRIMARY KEY, name TEXT NOT NULL, data TEXT NOT NULL);
CREATE TABLE capture_sessions (session_id TEXT PRIMARY KEY, captured_at TEXT NOT NULL);
"""


def test_store_still_rejects_0_1_0_even_though_0_2_0_and_0_3_0_migrate(tmp_path):
    """0.2.0/0.3.0 (the two immediately-prior releases) are the only forward-migratable
    versions — anything older/unknown stays a hard rejection (see
    store._MIGRATABLE_SCHEMA_VERSIONS; tests/test_store_migration.py covers the
    versions that DO migrate)."""
    import sqlite3

    from sidegraph.store import Store

    db = tmp_path / "ancient.db"
    conn = sqlite3.connect(db)
    conn.executescript(_PRE_0_3_SCHEMA_SQL)
    conn.execute("INSERT INTO meta VALUES ('schema_version', '0.1.0')")
    conn.commit()
    conn.close()
    with pytest.raises(ValueError, match="schema_version"):
        Store(db)


def test_decision_rejects_naive_datetime():
    from datetime import datetime

    from pydantic import ValidationError

    from sidegraph.schema import Decision, DecisionKind, Provenance

    with pytest.raises(ValidationError):
        Decision(
            title="t",
            kind=DecisionKind.ADR,
            context="c",
            choice="ch",
            valid_from=datetime.now(),  # naive -> must be rejected now
            provenance=Provenance(source="manual"),
        )
