"""Legacy (pre-0.1.0) single-file SQLite stores are refused, never migrated (see
docs/reference/store-format.md#legacy-single-file-stores). Fixture dbs are hand-crafted with
raw sqlite3, so this suite never depends on an old copy of Store."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from sidegraph.store import Store
from sidegraph.store_layout import (
    InterruptedMigrationError,
    LegacyStoreError,
    open_would_write,
)

_LEGACY_SCHEMA_SQL = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE entities (
    entity_id TEXT PRIMARY KEY, canonical_name TEXT NOT NULL, data TEXT NOT NULL
);
CREATE TABLE decisions (
    id TEXT PRIMARY KEY, status TEXT NOT NULL, supersedes TEXT, data TEXT NOT NULL
);
"""


def _legacy_db(path: Path, version: str | None = "0.3.0") -> None:
    conn = sqlite3.connect(str(path))
    conn.executescript(_LEGACY_SCHEMA_SQL)
    if version is not None:
        conn.execute("INSERT INTO meta VALUES ('schema_version', ?)", (version,))
    conn.commit()
    conn.close()


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()
    }


def test_a_legacy_single_file_store_is_refused_untouched(tmp_path):
    legacy = tmp_path / "old.db"
    _legacy_db(legacy, "0.2.0")
    before = _snapshot(tmp_path)

    with pytest.raises(LegacyStoreError) as exc:
        Store(legacy)

    msg = str(exc.value)
    assert str(legacy) in msg
    assert "0.2.0" in msg
    assert "predates Sidegraph 0.1.0" in msg
    assert "0.23.x" in msg
    assert _snapshot(tmp_path) == before
    assert legacy.is_file()


def test_a_directory_holding_a_legacy_decisions_db_is_refused_untouched(tmp_path):
    store_dir = tmp_path / ".sidegraph"
    store_dir.mkdir()
    legacy = store_dir / "decisions.db"
    _legacy_db(legacy, "0.3.0")
    before = _snapshot(tmp_path)

    with pytest.raises(ValueError) as exc:
        Store(store_dir)

    msg = str(exc.value)
    assert str(legacy) in msg
    assert "0.3.0" in msg
    assert "predates Sidegraph 0.1.0" in msg
    assert _snapshot(tmp_path) == before
    assert sorted(p.name for p in store_dir.iterdir()) == ["decisions.db"]


def test_an_unreadable_legacy_stamp_is_reported_as_unknown(tmp_path):
    legacy = tmp_path / "old.db"
    # a SQLite header over a body no query can read: still a legacy store, stamp unknown
    content = b"SQLite format 3\x00" + b"\xff" * 100
    legacy.write_bytes(content)

    with pytest.raises(ValueError, match="unknown") as exc:
        Store(legacy)

    assert "predates Sidegraph 0.1.0" in str(exc.value)
    assert legacy.read_bytes() == content


def test_a_plain_file_at_the_store_path_is_not_called_a_legacy_store(tmp_path):
    plain = tmp_path / "old.db"
    plain.write_text("not a sqlite file", encoding="utf-8")

    with pytest.raises(sqlite3.DatabaseError, match="not a database") as exc:
        Store(plain)

    assert "0.23" not in str(exc.value)
    assert plain.read_text(encoding="utf-8") == "not a sqlite file"


def test_a_store_with_an_earlier_migrations_backup_still_opens(tmp_path):
    store_dir = tmp_path / ".sidegraph"
    Store(store_dir).close()
    backup = store_dir / "decisions.db.migrated-backup"
    _legacy_db(backup, "0.3.0")

    Store(store_dir).close()

    assert backup.is_file()


def test_pointing_at_a_modern_stores_index_db_names_the_directory(tmp_path):
    """`index.db` is the obvious-looking "the database" file, so pointing at it instead of the
    store directory is an easy mistake. The error must name the real problem and the real fix,
    not diagnose a healthy store as a legacy one."""
    store_dir = tmp_path / ".sidegraph"
    Store(store_dir).close()  # a real, healthy, current-format store
    assert (store_dir / "index.db").is_file()

    with pytest.raises(ValueError) as exc:
        Store(store_dir / "index.db")

    msg = str(exc.value)
    assert str(store_dir) in msg, "the error must name the directory to open instead"
    assert "fresh store" not in msg, "must not advise discarding a healthy store"
    assert "predates" not in msg


def test_pointing_at_any_file_inside_a_store_names_the_directory(tmp_path):
    """The marker file, a canonical record, or a stray sqlite sidecar are all artifacts INSIDE
    a store, none of them a legacy store."""
    store_dir = tmp_path / ".sidegraph"
    Store(store_dir).close()

    for artifact in ("format", "index.db-wal", "notes.txt"):
        target = store_dir / artifact
        if not target.is_file():
            target.write_text("x", encoding="utf-8")
        with pytest.raises(ValueError) as exc:
            Store(target)
        assert str(store_dir) in str(exc.value), artifact


def test_store_dot_opens(tmp_path, monkeypatch):
    store_dir = tmp_path / "s"
    Store(store_dir).close()
    monkeypatch.chdir(store_dir)
    Store(".").close()


def test_a_symlinked_subdir_is_refused_before_the_legacy_refusal(tmp_path):
    root = tmp_path / ".sidegraph"
    root.mkdir()
    legacy = root / "decisions.db"
    _legacy_db(legacy, "0.2.0")
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "decisions").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="is a symlink"):
        Store(root)
    assert legacy.is_file()
    assert list(outside.iterdir()) == []


def test_a_wal_mode_legacy_db_is_refused_without_writing_sidecars(tmp_path):
    store_dir = tmp_path / ".sidegraph"
    store_dir.mkdir()
    legacy = store_dir / "decisions.db"
    _legacy_db(legacy, "0.3.0")
    conn = sqlite3.connect(str(legacy))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.close()
    before = _snapshot(tmp_path)

    with pytest.raises(LegacyStoreError, match="0.3.0"):
        Store(store_dir)

    assert _snapshot(tmp_path) == before
    assert sorted(p.name for p in store_dir.iterdir()) == ["decisions.db"]


def test_a_locked_legacy_db_is_refused_without_waiting(tmp_path):
    legacy = tmp_path / "old.db"
    _legacy_db(legacy, "0.2.0")
    holder = sqlite3.connect(str(legacy), isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        started = time.monotonic()
        with pytest.raises(LegacyStoreError):
            Store(legacy)
        assert time.monotonic() - started < 0.5
    finally:
        holder.close()


def _write_sentinel(db: Path, staging: Path) -> Path:
    sentinel = db.with_name(db.name + ".migration-incomplete")
    sentinel.write_text(
        json.dumps({"staging": str(staging), "backup": str(db) + ".migrated-backup"}),
        encoding="utf-8",
    )
    return sentinel


def test_an_interrupted_bare_file_migration_is_refused_with_the_backup_and_staging(tmp_path):
    """The legacy file is gone (renamed to its backup), the records sit in the staging
    directory: opening the path must not create a fresh empty store over them."""
    db = tmp_path / "old.db"
    staging = tmp_path / ".sidegraph-migrating-abc"
    (staging / "decisions").mkdir(parents=True)
    _legacy_db(tmp_path / "old.db.migrated-backup", "0.2.0")
    sentinel = _write_sentinel(db, staging)
    before = _snapshot(tmp_path)

    with pytest.raises(InterruptedMigrationError) as exc:
        Store(db)

    msg = str(exc.value)
    assert str(sentinel) in msg and str(staging) in msg and "old.db.migrated-backup" in msg
    assert "0.23.x" in msg
    assert not db.exists()
    assert _snapshot(tmp_path) == before
    assert staging.is_dir() and sentinel.is_file()


def test_a_sentinel_beside_a_legacy_file_is_refused_and_left_alone(tmp_path):
    db = tmp_path / "old.db"
    _legacy_db(db, "0.2.0")
    staging = tmp_path / ".sidegraph-migrating-abc"
    staging.mkdir()
    sentinel = _write_sentinel(db, staging)

    with pytest.raises(InterruptedMigrationError):
        Store(db)

    assert db.is_file() and sentinel.is_file() and staging.is_dir()


def test_an_unreadable_sentinel_is_still_refused(tmp_path):
    db = tmp_path / "old.db"
    sentinel = db.with_name("old.db.migration-incomplete")
    sentinel.write_text("not json", encoding="utf-8")

    with pytest.raises(InterruptedMigrationError, match="unreadable sentinel"):
        Store(db)


@pytest.mark.parametrize("spelling", [".", ".."])
def test_the_sentinel_guard_holds_for_dot_spellings(tmp_path, monkeypatch, spelling):
    """`Path(".").name` is empty: the guard must resolve the path before looking for its
    sentinel, or a partly migrated directory opens as a fresh empty store."""
    store_dir = tmp_path / "old.db"
    store_dir.mkdir()
    staging = tmp_path / ".sidegraph-migrating-abc"
    staging.mkdir()
    sentinel = _write_sentinel(store_dir, staging)
    cwd = store_dir if spelling == "." else store_dir / "sub"
    cwd.mkdir(exist_ok=True)
    monkeypatch.chdir(cwd)
    before = _snapshot(tmp_path)

    with pytest.raises(InterruptedMigrationError, match="interrupted"):
        Store(spelling)

    assert sentinel.is_file()
    assert _snapshot(tmp_path) == before


def test_a_healthy_directory_still_opens_through_a_dot_spelling(tmp_path, monkeypatch):
    store_dir = tmp_path / "s"
    Store(store_dir).close()
    sub = store_dir / "sub"
    sub.mkdir()
    monkeypatch.chdir(sub)
    Store("..").close()


def test_a_symlinked_store_root_is_refused_by_the_sentinel_beside_its_target(tmp_path):
    """The migration left its sentinel beside the real directory; opening through an alias
    must find it there, not look beside the alias and open an empty store."""
    db = tmp_path / "old.db"
    db.mkdir()
    staging = tmp_path / ".sidegraph-migrating-abc"
    staging.mkdir()
    _write_sentinel(db, staging)
    alias = tmp_path / "alias"
    alias.symlink_to(db, target_is_directory=True)
    before = _snapshot(tmp_path)

    with pytest.raises(InterruptedMigrationError, match="interrupted"):
        Store(alias)

    assert _snapshot(tmp_path) == before
    assert "interrupted migration" in (open_would_write(alias) or "")
