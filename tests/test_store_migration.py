"""Migration of legacy (0.2.x / 0.3.x) single-file SQLite stores into the git-native
file-per-record canonical layout (see
docs/reference/store-format.md#migration-to-schema-040-from-schema-02x-and-03x). Legacy fixture dbs
are hand-crafted with raw sqlite3 (same technique the pre-rewrite schema_version gate
tests used — see tests/test_schema_descriptor.py) so this suite never depends on an OLD
copy of Store to produce them."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

import sidegraph.store as store_module
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


# --- record identity in the legacy migration (design/superpowers/specs/
# 2026-09-29-record-identity-design.md D9, D10) -------------------------------------------


def _fail_third_staged_move(monkeypatch):
    """Make the third ``os.replace`` whose source lives under a ``.sidegraph-migrating-*``
    staging directory (and is not a ``*.tmp`` write buffer) raise."""
    calls = {"n": 0}
    real = os.replace

    def fake(src, dst, *args, **kwargs):
        s = str(src)
        if ".sidegraph-migrating-" in s and not s.endswith(".tmp"):
            calls["n"] += 1
            if calls["n"] == 3:
                raise OSError("injected: third staged move")
        return real(src, dst, *args, **kwargs)

    monkeypatch.setattr(store_module.os, "replace", fake)


def _staging_dirs(parent) -> list:
    return [p for p in parent.iterdir() if p.name.startswith(".sidegraph-migrating-")]


def _assert_fully_migrated(s: Store, entity, decision, binding, domain) -> None:
    assert s.get_decision(decision.id) is not None
    assert s.get_entity(entity.entity_id) is not None
    assert [b.entity_id for b in s.bindings_for_record(decision.id)] == [binding.entity_id]
    assert s.get_domain(domain.domain_id) is not None


def test_legacy_row_with_unsafe_id_fails_closed(tmp_path, monkeypatch):
    db = tmp_path / "old.db"
    _build_legacy_db(db, _0_2_0_SCHEMA_SQL, "0.2.0")
    bad = _decision(id="../../x", title="crafted")
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO decisions VALUES (?, ?, ?, ?)",
        (bad.id, bad.status.value, None, bad.model_dump_json()),
    )
    conn.commit()
    conn.close()
    calls: list = []
    real = tempfile.mkdtemp

    def recording(*args, **kwargs):
        calls.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(store_module.tempfile, "mkdtemp", recording)
    with pytest.raises(ValueError, match="unsafe record id"):
        Store(db)
    assert calls == []
    assert db.is_file()
    assert {p.name for p in tmp_path.iterdir()} == {"old.db"}


def _insert_decision_row_without_id(db, pk: str) -> None:
    """A legacy decision row whose JSON omits ``id``: the models default it to a fresh ULID
    on every validation, so the SQL primary key is the only stable identity it has."""
    payload = json.loads(_decision(title="no id in json").model_dump_json())
    del payload["id"]
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO decisions VALUES (?, ?, ?, ?)",
        (pk, payload["status"], None, json.dumps(payload)),
    )
    conn.commit()
    conn.close()


def test_a_legacy_row_with_no_id_in_its_json_takes_the_sql_primary_key(tmp_path, monkeypatch):
    store_dir = tmp_path / "s"
    store_dir.mkdir()
    legacy = store_dir / "decisions.db"
    _build_legacy_db(legacy, _0_3_0_SCHEMA_SQL, "0.3.0", with_domain=True)
    pk = "01KPRIMARYKEYOFTHEIDLESSROW"
    _insert_decision_row_without_id(legacy, pk)
    with monkeypatch.context() as m:
        _fail_third_staged_move(m)
        with pytest.raises(OSError, match="injected"):
            Store(store_dir)
    Store(store_dir).close()
    names = sorted(p.name for p in (store_dir / "decisions").glob("*.json"))
    assert names.count(f"{pk}.json") == 1
    assert len(names) == 2  # the no-id row and the hand-built decision: no stray second file


def test_a_legacy_row_with_an_unsafe_primary_key_and_no_id_fails_closed(tmp_path, monkeypatch):
    db = tmp_path / "old.db"
    _build_legacy_db(db, _0_2_0_SCHEMA_SQL, "0.2.0")
    _insert_decision_row_without_id(db, "../../x")
    calls: list = []
    real = tempfile.mkdtemp

    def recording(*args, **kwargs):
        calls.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(store_module.tempfile, "mkdtemp", recording)
    before = db.read_bytes()
    with pytest.raises(ValueError, match="unsafe record id"):
        Store(db)
    assert calls == []
    assert db.read_bytes() == before
    assert {p.name for p in tmp_path.iterdir()} == {"old.db"}


def test_a_legacy_row_with_an_unsafe_primary_key_and_a_safe_json_id_fails_closed(
    tmp_path, monkeypatch
):
    db = tmp_path / "old.db"
    _build_legacy_db(db, _0_2_0_SCHEMA_SQL, "0.2.0")
    decision = _decision(title="safe json id, unsafe primary key")
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO decisions VALUES (?, ?, ?, ?)",
        ("../../x", decision.status.value, None, decision.model_dump_json()),
    )
    conn.commit()
    conn.close()
    calls: list = []
    real = tempfile.mkdtemp

    def recording(*args, **kwargs):
        calls.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(store_module.tempfile, "mkdtemp", recording)
    before = db.read_bytes()
    with pytest.raises(ValueError, match="unsafe record id"):
        Store(db)
    assert calls == []
    assert db.read_bytes() == before
    assert {p.name for p in tmp_path.iterdir()} == {"old.db"}


def test_dir_migration_interrupted_remigrates(tmp_path, monkeypatch):
    store_dir = tmp_path / "s"
    store_dir.mkdir()
    legacy = store_dir / "decisions.db"
    entity, decision, binding, _init, domain = _build_legacy_db(
        legacy, _0_3_0_SCHEMA_SQL, "0.3.0", with_domain=True
    )
    with monkeypatch.context() as m:
        _fail_third_staged_move(m)
        with pytest.raises(OSError, match="injected"):
            Store(store_dir)
    assert legacy.is_file()  # the legacy file is renamed only after every move landed
    assert _staging_dirs(tmp_path) == [] and _staging_dirs(store_dir) == []

    s = Store(store_dir)
    _assert_fully_migrated(s, entity, decision, binding, domain)
    s.close()
    assert (store_dir / "decisions.db.migrated-backup").is_file()
    assert not legacy.exists()


def test_bare_file_migration_interrupted_keeps_staging(tmp_path, monkeypatch):
    db = tmp_path / "old.db"
    _build_legacy_db(db, _0_3_0_SCHEMA_SQL, "0.3.0", with_domain=True)
    with monkeypatch.context() as m:
        _fail_third_staged_move(m)
        with pytest.raises(OSError, match="injected"):
            Store(db)
    (staging,) = _staging_dirs(tmp_path)
    sentinel = tmp_path / "old.db.migration-incomplete"
    assert sentinel.is_file()
    then = sentinel.stat().st_mtime - 120  # the crash was a while ago, not a live migration
    os.utime(sentinel, (then, then))
    with pytest.raises(ValueError, match="migration-incomplete|interrupted") as exc:
        Store(db)
    assert str(staging) in str(exc.value)
    assert "old.db.migrated-backup" in str(exc.value)


def test_sentinel_write_failure_leaves_the_legacy_file(tmp_path, monkeypatch):
    db = tmp_path / "old.db"
    entity, decision, binding, _init, domain = _build_legacy_db(
        db, _0_3_0_SCHEMA_SQL, "0.3.0", with_domain=True
    )
    real = Path.open

    def failing(self, *args, **kwargs):
        if self.name.endswith(".migration-incomplete"):
            raise OSError("injected: sentinel write")
        return real(self, *args, **kwargs)

    with monkeypatch.context() as m:
        m.setattr(Path, "open", failing)
        with pytest.raises(OSError, match="sentinel"):
            Store(db)
    assert db.is_file()
    assert not (tmp_path / "old.db.migrated-backup").exists()
    s = Store(db)
    _assert_fully_migrated(s, entity, decision, binding, domain)
    s.close()


def test_rename_failure_after_sentinel_cleans_up_and_migrates_on_reopen(tmp_path, monkeypatch):
    db = tmp_path / "old.db"
    entity, decision, binding, _init, domain = _build_legacy_db(
        db, _0_3_0_SCHEMA_SQL, "0.3.0", with_domain=True
    )
    real = Path.rename

    def failing(self, target):
        if str(target).endswith(".migrated-backup"):
            raise OSError("injected: rename")
        return real(self, target)

    with monkeypatch.context() as m:
        m.setattr(Path, "rename", failing)
        with pytest.raises(OSError, match="rename"):
            Store(db)
    assert not (tmp_path / "old.db.migration-incomplete").exists()
    assert _staging_dirs(tmp_path) == []
    assert db.is_file()
    s = Store(db)
    _assert_fully_migrated(s, entity, decision, binding, domain)
    s.close()


def test_store_dot_opens(tmp_path, monkeypatch):
    store_dir = tmp_path / "s"
    Store(store_dir).close()
    monkeypatch.chdir(store_dir)
    Store(".").close()


def _write_sentinel(db, staging_path, age_seconds: float = 120.0) -> Path:
    """Write a sentinel whose mtime is ``age_seconds`` back: by default an old one (a crashed
    migration); ``0`` is a young one, which stands for a live migration (design D10)."""
    sentinel = db.parent / "old.db.migration-incomplete"
    sentinel.write_text(
        json.dumps({"staging": str(staging_path), "backup": str(db) + ".migrated-backup"}),
        encoding="utf-8",
    )
    if age_seconds:
        then = sentinel.stat().st_mtime - age_seconds
        os.utime(sentinel, (then, then))
    return sentinel


def test_crash_between_sentinel_and_rename_heals(tmp_path):
    db = tmp_path / "old.db"
    entity, decision, binding, _init, domain = _build_legacy_db(
        db, _0_3_0_SCHEMA_SQL, "0.3.0", with_domain=True
    )
    staging = tmp_path / ".sidegraph-migrating-x"
    (staging / "decisions").mkdir(parents=True)
    (staging / "decisions" / "half.json").write_text("{}", encoding="utf-8")
    _write_sentinel(db, staging)
    s = Store(db)
    _assert_fully_migrated(s, entity, decision, binding, domain)
    s.close()
    assert not (tmp_path / "old.db.migration-incomplete").exists()
    assert not staging.exists()


def test_stale_sentinel_with_a_relative_dotdot_path_removes_its_staging(
    tmp_path, capsys, monkeypatch
):
    """``Path("../old.db").absolute()`` keeps the ``..``; the staging directory the sentinel
    names is normalised, so the parents must be compared normalised too."""
    db = tmp_path / "old.db"
    entity, decision, binding, _init, domain = _build_legacy_db(
        db, _0_3_0_SCHEMA_SQL, "0.3.0", with_domain=True
    )
    sub = tmp_path / "sub"
    sub.mkdir()
    staging = tmp_path / ".sidegraph-migrating-x"
    (staging / "decisions").mkdir(parents=True)
    _write_sentinel(db, staging)
    monkeypatch.chdir(sub)
    capsys.readouterr()
    s = Store("../old.db")
    _assert_fully_migrated(s, entity, decision, binding, domain)
    s.close()
    assert not staging.exists()
    assert "not a staging directory" not in capsys.readouterr().err


def test_stale_sentinel_never_deletes_a_foreign_directory(tmp_path, capsys):
    db = tmp_path / "old.db"
    entity, decision, binding, _init, domain = _build_legacy_db(
        db, _0_3_0_SCHEMA_SQL, "0.3.0", with_domain=True
    )
    precious = tmp_path / "precious"
    precious.mkdir()
    (precious / "keep.txt").write_text("keep", encoding="utf-8")
    _write_sentinel(db, precious)
    capsys.readouterr()
    s = Store(db)
    _assert_fully_migrated(s, entity, decision, binding, domain)
    s.close()
    assert (precious / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert "precious" in capsys.readouterr().err


def test_a_concurrent_opener_never_touches_the_foreign_sentinel(tmp_path, monkeypatch):
    """Two openers of one legacy file share a sentinel path. The second one's exclusive
    create fails: it aborts, and leaves the other's sentinel, the legacy file and everything
    else alone (design D10). The stale check is patched out to stand for the window between
    that check and the create, in which the other opener wrote its sentinel."""
    db = tmp_path / "old.db"
    _build_legacy_db(db, _0_3_0_SCHEMA_SQL, "0.3.0", with_domain=True)
    foreign = tmp_path / "old.db.migration-incomplete"
    foreign_bytes = json.dumps(
        {"staging": str(tmp_path / ".sidegraph-migrating-other"), "backup": "elsewhere"}
    ).encode()
    foreign.write_bytes(foreign_bytes)
    monkeypatch.setattr(Store, "_check_migration_sentinel", staticmethod(lambda raw, s: None))
    before = {p.name for p in tmp_path.iterdir()}

    with pytest.raises(ValueError, match="another process is migrating"):
        Store(db)

    assert foreign.read_bytes() == foreign_bytes
    assert db.is_file()
    assert not (tmp_path / "old.db.migrated-backup").exists()
    assert {p.name for p in tmp_path.iterdir()} == before
    assert _staging_dirs(tmp_path) == []


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()
    }


def test_a_young_sentinel_with_the_legacy_file_intact_is_a_live_migration(tmp_path):
    """Opener A wrote its sentinel and paused before the rename. Opener B must not call that
    stale and delete A's staging directory (design D10): it refuses and touches nothing."""
    db = tmp_path / "old.db"
    _build_legacy_db(db, _0_3_0_SCHEMA_SQL, "0.3.0", with_domain=True)
    staging = tmp_path / ".sidegraph-migrating-live"
    (staging / "decisions").mkdir(parents=True)
    (staging / "decisions" / "half.json").write_text("{}", encoding="utf-8")
    sentinel = _write_sentinel(db, staging, age_seconds=0)
    before = _snapshot(tmp_path)

    with pytest.raises(ValueError, match="migrating .*old.db.* now.*retry in a minute") as exc:
        Store(db)
    assert "do not delete the sentinel" in str(exc.value)

    assert sentinel.is_file() and staging.is_dir() and db.is_file()
    assert _snapshot(tmp_path) == before


def test_a_young_sentinel_with_the_legacy_file_gone_asks_for_a_retry(tmp_path):
    """The rename has happened and the sentinel is seconds old: A is still moving the staged
    files in. That is a retry, not the manual-recovery message."""
    db = tmp_path / "old.db"
    staging = tmp_path / ".sidegraph-migrating-live"
    (staging / "decisions").mkdir(parents=True)
    _write_sentinel(db, staging, age_seconds=0)

    with pytest.raises(ValueError, match="retry in a minute") as exc:
        Store(db)

    assert "interrupted" not in str(exc.value)
    assert staging.is_dir()


def test_an_old_sentinel_with_the_legacy_file_gone_still_gives_the_recovery_message(tmp_path):
    db = tmp_path / "old.db"
    staging = tmp_path / ".sidegraph-migrating-x"
    (staging / "decisions").mkdir(parents=True)
    _write_sentinel(db, staging, age_seconds=120)

    with pytest.raises(ValueError, match="interrupted") as exc:
        Store(db)
    # a crash between the last move and the sentinel's removal leaves staging empty
    assert "staging directory is empty, the migration had finished" in str(exc.value)


def test_an_unreadable_sentinel_mtime_counts_as_old(tmp_path):
    """No mtime, no evidence of a live migration: today's stale handling applies."""
    age = store_module._sentinel_age_seconds(tmp_path / "missing.migration-incomplete")
    assert age > store_module._LIVE_MIGRATION_SECONDS


def test_legacy_migration_refuses_symlinked_subdir(tmp_path):
    """The symlink check runs BEFORE the legacy dispatch: otherwise the migration moves the
    records through the link and renames the legacy file, then the check raises."""
    root = tmp_path / ".sidegraph"
    root.mkdir()
    legacy = root / "decisions.db"
    _build_legacy_db(legacy, _0_2_0_SCHEMA_SQL, "0.2.0")
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "decisions").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="is a symlink"):
        Store(root)
    assert legacy.is_file()
    assert list(outside.iterdir()) == []
