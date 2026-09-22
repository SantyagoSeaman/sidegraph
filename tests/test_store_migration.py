"""Migration of legacy (0.2.x / 0.3.x) single-file SQLite stores into the git-native
file-per-record canonical layout (see
docs/reference/store-format.md#migration-to-schema-040-from-schema-02x-and-03x). Legacy fixture dbs
are hand-crafted with raw sqlite3 (same technique the pre-rewrite schema_version gate
tests used — see tests/test_schema_descriptor.py) so this suite never depends on an OLD
copy of Store to produce them."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

import pytest

from sidegraph.schema import (
    SCHEMA_VERSION,
    AnchorBinding,
    Decision,
    DecisionKind,
    Domain,
    DomainStatus,
    Entity,
    EntityKind,
    Initiative,
    Provenance,
)
from sidegraph.store import Store

_0_2_0_SCHEMA_SQL = """
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

# 0.3.0 added the `domains` table (mind-model layer) — otherwise identical to 0.2.0.
_0_3_0_SCHEMA_SQL = (
    _0_2_0_SCHEMA_SQL
    + """
CREATE TABLE domains (
    domain_id TEXT PRIMARY KEY, slug TEXT NOT NULL, status TEXT NOT NULL,
    supersedes TEXT, data TEXT NOT NULL
);
"""
)


def _decision(**overrides) -> Decision:
    base = dict(
        title="Use SQLite for the store",
        kind=DecisionKind.ADR,
        context="Need a repo-committable, serverless store.",
        choice="SQLite via stdlib sqlite3.",
        valid_from=datetime.now(UTC),
        provenance=Provenance(source="manual"),
    )
    base.update(overrides)
    return Decision(**base)


def _build_legacy_db(path, schema_sql: str, version: str, *, with_domain: bool = False):
    """Hand-craft a legacy store: entity + decision + tier-2 binding (status=orphaned, to
    prove volatile fields do NOT survive migration), initiative, capture ledger entry, and
    (0.3.0 only) an accepted domain with a stale `communities` mapping."""
    entity = Entity(
        canonical_name="old_fn",
        kind=EntityKind.CONCRETE,
        last_seen_node_id="n99",
        last_seen_graph_version="vOLD",
        last_seen_community="7",
    )
    decision = _decision(title="legacy decision")
    binding = AnchorBinding(
        record_id=decision.id,
        entity_id=entity.entity_id,
        tier=2,
        status="orphaned",
        relation="creates",
        weight=0.5,
    )
    initiative = Initiative(name="Legacy Initiative", description="pre-existing", tags=["x"])

    conn = sqlite3.connect(str(path))
    conn.executescript(schema_sql)
    conn.execute("INSERT INTO meta VALUES ('schema_version', ?)", (version,))
    conn.execute(
        "INSERT INTO entities VALUES (?, ?, ?)",
        (entity.entity_id, entity.canonical_name, entity.model_dump_json()),
    )
    conn.execute(
        "INSERT INTO decisions VALUES (?, ?, ?, ?)",
        (decision.id, decision.status.value, decision.supersedes, decision.model_dump_json()),
    )
    conn.execute(
        "INSERT INTO anchor_bindings VALUES (?, ?, ?)",
        (binding.record_id, binding.entity_id, binding.model_dump_json()),
    )
    conn.execute(
        "INSERT INTO initiatives VALUES (?, ?, ?)",
        (initiative.id, initiative.name, initiative.model_dump_json()),
    )
    conn.execute(
        "INSERT INTO capture_sessions VALUES (?, ?)", ("legacy-session", "2026-01-01T00:00:00Z")
    )
    domain = None
    if with_domain:
        domain = Domain(
            slug="payments",
            title="Payments",
            summary="Order settlement.",
            communities=["7"],
            status=DomainStatus.ACCEPTED,
            provenance=Provenance(source="manual"),
        )
        conn.execute(
            "INSERT INTO domains VALUES (?, ?, ?, ?, ?)",
            (
                domain.domain_id,
                domain.slug,
                domain.status.value,
                domain.supersedes,
                domain.model_dump_json(),
            ),
        )
    conn.commit()
    conn.close()
    return entity, decision, binding, initiative, domain


def test_migrate_0_2_0_shaped_store(tmp_path, capsys):
    db = tmp_path / "old_0_2_0.db"
    entity, decision, binding, initiative, _ = _build_legacy_db(db, _0_2_0_SCHEMA_SQL, "0.2.0")

    store = Store(db)
    try:
        # backup preserved, never deleted
        backup = tmp_path / "old_0_2_0.db.migrated-backup"
        assert backup.exists() and backup.is_file()
        assert db.exists() and db.is_dir()  # freed up -> now the canonical directory root
        assert "migrated" in capsys.readouterr().err

        assert store.schema_version == SCHEMA_VERSION

        got_decision = store.get_decision(decision.id)
        assert got_decision is not None and got_decision.title == "legacy decision"

        got_entity = store.get_entity(entity.entity_id)
        assert got_entity is not None and got_entity.canonical_name == "old_fn"

        bindings = store.bindings_for_record(decision.id)
        assert len(bindings) == 1
        assert bindings[0].entity_id == entity.entity_id
        assert bindings[0].tier == 2
        assert bindings[0].relation == "creates"
        assert bindings[0].weight == 0.5

        initiatives = list(store.iter_initiatives())
        assert len(initiatives) == 1 and initiatives[0].name == "Legacy Initiative"

        # canonical files exist on disk, in the expected shape
        entity_file = store.path / "entities" / f"{entity.entity_id}.json"
        entity_payload = json.loads(entity_file.read_text())
        assert set(entity_payload) == {"entity_id", "canonical_name", "kind", "descriptor"}

        binding_file = store.path / "bindings" / f"{decision.id}.json"
        items = json.loads(binding_file.read_text())
        assert items == [
            {"entity_id": entity.entity_id, "tier": 2, "relation": "creates", "weight": 0.5}
        ]
    finally:
        store.close()


def test_migrate_0_2_0_legacy_binding_json_with_decision_id_key_normalizes(tmp_path):
    """A genuine pre-rename legacy row serializes its AnchorBinding with the OLD
    `decision_id` JSON key -- this rename post-dates every _MIGRATABLE_SCHEMA_VERSIONS
    store, so real 0.2.0/0.3.0 data on disk literally has that key, never `record_id`.
    Unlike `_build_legacy_db` (which serializes via the CURRENT, already-renamed
    AnchorBinding model and so never reproduces this), this test hand-authors the JSON the
    way actual old code wrote it, to prove Store._validate_legacy_rows's legacy-key
    normalization (see store.py) makes migration succeed instead of a spurious
    "field required" validation failure."""
    db = tmp_path / "old_0_2_0_handwritten.db"
    entity = Entity(canonical_name="old_fn", kind=EntityKind.CONCRETE)
    decision = _decision(title="legacy decision")

    conn = sqlite3.connect(str(db))
    conn.executescript(_0_2_0_SCHEMA_SQL)
    conn.execute("INSERT INTO meta VALUES ('schema_version', '0.2.0')")
    conn.execute(
        "INSERT INTO entities VALUES (?, ?, ?)",
        (entity.entity_id, entity.canonical_name, entity.model_dump_json()),
    )
    conn.execute(
        "INSERT INTO decisions VALUES (?, ?, ?, ?)",
        (decision.id, decision.status.value, decision.supersedes, decision.model_dump_json()),
    )
    legacy_binding_json = json.dumps(
        {
            "decision_id": decision.id,
            "entity_id": entity.entity_id,
            "tier": 2,
            "weight": 1.0,
            "status": "live",
            "relation": "affects",
        }
    )
    conn.execute(
        "INSERT INTO anchor_bindings VALUES (?, ?, ?)",
        (decision.id, entity.entity_id, legacy_binding_json),
    )
    conn.commit()
    conn.close()

    store = Store(db)
    try:
        bindings = store.bindings_for_record(decision.id)
        assert len(bindings) == 1
        assert bindings[0].record_id == decision.id
        assert bindings[0].entity_id == entity.entity_id
    finally:
        store.close()


def test_migrate_0_3_0_shaped_store_with_domain(tmp_path):
    db = tmp_path / "old_0_3_0.db"
    _entity, decision, _binding, _initiative, domain = _build_legacy_db(
        db, _0_3_0_SCHEMA_SQL, "0.3.0", with_domain=True
    )

    store = Store(db)
    try:
        assert store.schema_version == SCHEMA_VERSION
        got_domain = store.get_domain(domain.domain_id)
        assert got_domain is not None
        assert got_domain.slug == "payments"
        assert got_domain.status == DomainStatus.ACCEPTED

        # domains/<id>.json never carries `communities` — sync refreshes it index-only
        domain_file = store.path / "domains" / f"{domain.domain_id}.json"
        assert "communities" not in json.loads(domain_file.read_text())

        assert store.get_decision(decision.id) is not None
    finally:
        store.close()


def test_migration_resets_volatile_fields_to_cold_defaults(tmp_path):
    """Volatile state (entity engine mapping, binding status, domain communities) is
    intentionally NOT carried over -- only canonical (identity/content) files are written
    during migration, and the index rebuild that follows resets them to cold defaults, same
    as any other canonical-only reload (design §3/§4). The next sync re-derives them."""
    db = tmp_path / "old_0_3_0.db"
    entity, decision, _binding, _initiative, domain = _build_legacy_db(
        db, _0_3_0_SCHEMA_SQL, "0.3.0", with_domain=True
    )

    store = Store(db)
    try:
        got_entity = store.get_entity(entity.entity_id)
        assert got_entity.last_seen_node_id is None
        assert got_entity.last_seen_graph_version is None
        assert got_entity.last_seen_community is None

        bindings = store.bindings_for_record(decision.id)
        assert bindings[0].status == "live"  # was "orphaned" in the legacy row

        got_domain = store.get_domain(domain.domain_id)
        assert got_domain.communities == []  # was ["7"] in the legacy row

        assert store.get_meta("volatile_stale") == "1"

        # local-only bookkeeping (capture ledger) is NOT part of the canonical migration
        # either -- it has no canonical file to rebuild from (design §1: "capture ledger"
        # is explicitly volatile).
        assert store.was_captured("legacy-session") is False
    finally:
        store.close()


def test_migrated_store_idempotent_reopen(tmp_path, capsys):
    db = tmp_path / "old_0_2_0.db"
    entity, decision, _binding, _initiative, _domain = _build_legacy_db(
        db, _0_2_0_SCHEMA_SQL, "0.2.0"
    )

    store = Store(db)
    store.close()
    capsys.readouterr()  # discard the first migration's stderr notice
    backup = tmp_path / "old_0_2_0.db.migrated-backup"
    backup_mtime = backup.stat().st_mtime_ns

    reopened = Store(db)
    try:
        # no second migration: nothing printed, backup untouched, data unchanged
        assert capsys.readouterr().err == ""
        assert backup.stat().st_mtime_ns == backup_mtime
        assert reopened.get_decision(decision.id) is not None
        assert reopened.get_entity(entity.entity_id) is not None
        assert reopened.schema_version == SCHEMA_VERSION
    finally:
        reopened.close()


def test_migration_fails_closed_on_corrupted_row(tmp_path, capsys):
    """Fail-closed regression: a garbled row mid-export must never leave a partial
    canonical layout on disk. Every legacy row must be pydantic-validated in FULL before
    anything is written, and the legacy db renamed only as the final commit step -- on
    failure the legacy file is untouched (no rename, no canonical layout at all), and a
    retry (reopening the same path) fails again the same way. Never partial."""
    db = tmp_path / "old_0_2_0.db"
    _build_legacy_db(db, _0_2_0_SCHEMA_SQL, "0.2.0")

    # A second decision row: syntactically valid JSON, but missing required Decision
    # fields (title/kind/context/choice/valid_from/provenance) -- fails pydantic validation.
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO decisions VALUES (?, ?, ?, ?)",
        ("bad-decision-id", "accepted", None, json.dumps({"id": "bad-decision-id"})),
    )
    conn.commit()
    conn.close()

    with pytest.raises(ValueError, match="decisions"):
        Store(db)
    capsys.readouterr()  # nothing meaningful printed on a failed migration

    # legacy db is completely untouched: still a plain file, not renamed
    assert db.exists() and db.is_file()
    backup = tmp_path / "old_0_2_0.db.migrated-backup"
    assert not backup.exists()

    # NO canonical layout leaked out anywhere -- not even the subdirectories
    for sub in ("decisions", "domains", "entities", "bindings", "initiatives"):
        assert not (tmp_path / sub).exists()
    assert {p.name for p in tmp_path.iterdir()} == {"old_0_2_0.db"}

    # retryable: reopening the same (still-corrupted) path fails again, never "succeeds"
    # partially.
    with pytest.raises(ValueError, match="decisions"):
        Store(db)
    assert {p.name for p in tmp_path.iterdir()} == {"old_0_2_0.db"}


def test_migration_leaves_legacy_backup_readable_as_plain_sqlite(tmp_path):
    """'never deleted; the store never destroys data' (design §4) -- the backup is a
    completely ordinary sqlite file, still openable by hand."""
    db = tmp_path / "old_0_2_0.db"
    _build_legacy_db(db, _0_2_0_SCHEMA_SQL, "0.2.0")
    Store(db).close()

    backup = tmp_path / "old_0_2_0.db.migrated-backup"
    conn = sqlite3.connect(str(backup))
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    assert row[0] == "0.2.0"
    conn.close()


# --- misdiagnosis guards: a modern store's own files are not legacy stores ------------------


def test_pointing_at_a_modern_stores_index_db_names_the_directory(tmp_path):
    """`index.db` is the obvious-looking "the database" file, so pointing at it instead of the
    store directory is an easy mistake. It used to take the legacy-migration branch and report
    `store schema_version '0.6.0' != code '0.6.0'` -- two IDENTICAL strings -- and advise "use a
    fresh store", which would destroy a perfectly healthy one. The error must name the real
    problem and the real fix."""
    store_dir = tmp_path / ".sidegraph"
    Store(store_dir).close()  # a real, healthy, current-format store
    assert (store_dir / "index.db").is_file()

    with pytest.raises(ValueError) as exc:
        Store(store_dir / "index.db")

    msg = str(exc.value)
    assert str(store_dir) in msg, "the error must name the directory to open instead"
    assert "fresh store" not in msg, "must not advise discarding a healthy store"


def test_pointing_at_any_file_inside_a_store_names_the_directory(tmp_path):
    """Generalizes the guard past `index.db` alone: the marker file, a canonical record, or a
    stray sqlite sidecar are all artifacts INSIDE a store, none of them a legacy store."""
    store_dir = tmp_path / ".sidegraph"
    Store(store_dir).close()

    for artifact in ("format", "index.db-wal", "notes.txt"):
        target = store_dir / artifact
        if not target.is_file():
            target.write_text("x", encoding="utf-8")
        with pytest.raises(ValueError) as exc:
            Store(target)
        assert str(store_dir) in str(exc.value), artifact


def test_a_non_migratable_legacy_version_states_the_real_condition(tmp_path):
    """The other half. A genuinely legacy single-file store stamped with a version this
    migrator does not handle must say so -- not print "X != Y" for two values that may well be
    equal. The old wording compared the stamp against the CODE's version, which is not the
    condition actually being tested."""
    legacy = tmp_path / "decisions.db"
    conn = sqlite3.connect(str(legacy))
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO meta VALUES ('schema_version', '0.1.0')")
    conn.commit()
    conn.close()

    with pytest.raises(ValueError) as exc:
        Store(legacy)

    msg = str(exc.value)
    assert "0.1.0" in msg
    assert "0.2.0" in msg and "0.3.0" in msg, "must name what this migrator DOES handle"
    assert "!= code" not in msg, "the old, false comparison must be gone"
