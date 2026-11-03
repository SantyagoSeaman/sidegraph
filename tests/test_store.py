"""Store write-path invariants — these ARE the contract (see CLAUDE.md, docs/data-model.md)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from sidegraph import (
    SCHEMA_VERSION,
    AnchorBinding,
    Decision,
    DecisionKind,
    DecisionStatus,
    Entity,
    Provenance,
    Store,
)
from tests.store_format2_support import format1_store


@pytest.fixture
def store(tmp_path) -> Store:
    with Store(format1_store(tmp_path / "test.db")) as s:
        yield s


def _decision(**overrides) -> Decision:
    base = dict(
        title="Use SQLite for the store",
        kind=DecisionKind.ADR,
        context="Need a repo-committable, serverless store.",
        choice="SQLite via stdlib sqlite3.",
        valid_from=datetime.now(UTC),
        provenance=Provenance(source="manual"),
    )
    if "supersedes" in overrides:
        base["status"] = "accepted"  # only an accepted successor closes
    base.update(overrides)
    return Decision(**base)


def test_store_stamps_schema_version(store: Store) -> None:
    assert store.schema_version == SCHEMA_VERSION


def test_add_and_get_decision(store: Store) -> None:
    d = store.add_decision(_decision())
    assert store.get_decision(d.id) is not None
    assert store.get_decision(d.id).title == "Use SQLite for the store"


def test_valid_to_before_valid_from_rejected() -> None:
    now = datetime.now(UTC)
    with pytest.raises(ValueError, match="valid_to must be >= valid_from"):
        _decision(valid_from=now, valid_to=now - timedelta(days=1))


def test_supersede_closes_predecessor(store: Store) -> None:
    old = store.add_decision(_decision(status="accepted"))
    new = store.add_decision(_decision(title="Switch to Kùzu", supersedes=old.id))

    reloaded_old = store.get_decision(old.id)
    assert reloaded_old.status == DecisionStatus.SUPERSEDED
    assert reloaded_old.valid_to is not None  # a superseded decision must be closed
    assert store.get_decision(new.id).supersedes == old.id


def test_supersede_unknown_decision_rejected(store: Store) -> None:
    with pytest.raises(ValueError, match="unknown decision"):
        store.add_decision(_decision(supersedes="01JUNKULIDDOESNOTEXIST00"))


def test_add_decision_existing_id_rejected(store: Store) -> None:
    """append-only: add_decision must never silently rewrite an existing row (that hole
    would erase history through a method documented as append-only). Ratify/supersede
    flip status via _write_decision directly — nothing legitimate re-adds the same id."""
    d = store.add_decision(_decision())
    with pytest.raises(ValueError, match="already exists"):
        store.add_decision(_decision(id=d.id, title="Rewritten in place"))
    # the original row must be untouched
    assert store.get_decision(d.id).title == "Use SQLite for the store"


def test_binding_requires_existing_entity(store: Store) -> None:
    d = store.add_decision(_decision())
    with pytest.raises(ValueError, match="unknown entity"):
        store.add_binding(AnchorBinding(record_id=d.id, entity_id="nope", tier=1))


def test_binding_roundtrip(store: Store) -> None:
    e = store.upsert_entity(Entity(canonical_name="sidegraph.store"))
    d = store.add_decision(_decision())
    store.add_binding(AnchorBinding(record_id=d.id, entity_id=e.entity_id, tier=1))

    bindings = store.bindings_for_entity(e.entity_id)
    assert len(bindings) == 1
    assert bindings[0].record_id == d.id


def _spy_publish_order(monkeypatch) -> list[tuple]:
    """Record, in call order, ``("fsync", "file")`` per ``os.fsync``, ``("link"|"replace",
    name)`` per publish and ``("fsync_dir", dir name, full)`` per ``maintenance.fsync_dir``
    (stubbed, so the order holds on a platform that cannot fsync a directory). The
    maintenance publisher's full-flush helper is forbidden."""
    import os
    from pathlib import Path

    from sidegraph import maintenance

    def _forbidden(*_a: object, **_kw: object) -> None:
        raise AssertionError("a canonical write must not use the full-flush helper")

    monkeypatch.setattr(maintenance, "fsync_fd", _forbidden)
    events: list[tuple] = []
    real_fsync, real_replace, real_link = os.fsync, os.replace, os.link

    def _fsync(fd: int) -> None:
        events.append(("fsync", "file"))
        real_fsync(fd)

    def _replace(src, dst) -> None:
        events.append(("replace", os.path.basename(dst)))
        real_replace(src, dst)

    def _link(src, dst, **kw) -> None:
        events.append(("link", os.path.basename(dst)))
        real_link(src, dst, **kw)

    def _fsync_dir(path: Path, *, full: bool = True) -> None:
        events.append(("fsync_dir", Path(path).name, full))

    monkeypatch.setattr(os, "fsync", _fsync)
    monkeypatch.setattr(os, "replace", _replace)
    monkeypatch.setattr(os, "link", _link)
    monkeypatch.setattr(maintenance, "fsync_dir", _fsync_dir)
    return events


def test_canonical_write_fsyncs_file_before_replace_and_directory_after(
    store: Store, monkeypatch
) -> None:
    """A record the tool reported as written must be crash-consistent: the tmp is flushed
    right before ``os.replace`` publishes it, and the directory entry right after. Plain
    ``os.fsync``, not the maintenance publisher's full drive-cache flush."""
    events = _spy_publish_order(monkeypatch)
    d = store.add_decision(_decision())
    at = events.index(("replace", f"{d.id}.json"))
    assert events[at - 1] == ("fsync", "file")
    assert events[at + 1] == ("fsync_dir", "decisions", False)


def test_first_open_marker_is_flushed_before_link_and_directory_after(
    tmp_path, monkeypatch
) -> None:
    """An empty ``format`` marker after a crash makes the whole store refuse to open."""
    events = _spy_publish_order(monkeypatch)
    root = tmp_path / "fresh"
    with Store(root):
        pass
    at = events.index(("link", "format"))
    assert events[at - 1] == ("fsync", "file")
    assert events[at + 1] == ("fsync_dir", "fresh", False)


def test_failed_file_fsync_removes_its_tmp_and_raises(store: Store, monkeypatch) -> None:
    import errno
    import os

    def _eio(fd: int) -> None:
        raise OSError(errno.EIO, "injected")

    monkeypatch.setattr(os, "fsync", _eio)
    with pytest.raises(OSError, match="injected"):
        store.add_decision(_decision())
    assert not list(store.path.rglob("*.tmp"))


def test_stamping_marker_is_flushed_before_link_and_directory_after(tmp_path, monkeypatch) -> None:
    """An empty timestamp would survive reopen (``is_file``) and disable the unratified-accept
    coverage, so the tmp is flushed before the link and the directory entry after."""
    events = _spy_publish_order(monkeypatch)
    with Store(tmp_path / "fresh"):
        pass
    at = events.index(("link", "stamping_live_since"))
    assert events[at - 1] == ("fsync", "file")
    assert events[at + 1] == ("fsync_dir", "fresh", False)


def test_new_store_directories_are_entered_durably(tmp_path, monkeypatch) -> None:
    """Syncing a directory does not persist its own name in its parent: every directory the
    open creates (the store and any missing ancestors, then the record subdirectories) has
    its parent synced after the creation, outermost first."""
    from pathlib import Path

    from sidegraph import maintenance

    synced: list[tuple[Path, bool]] = []

    def _spy(path: Path, *, full: bool = True) -> None:
        synced.append((Path(path), full))

    monkeypatch.setattr(maintenance, "fsync_dir", _spy)
    root = tmp_path / "a" / "b" / "s"
    with Store(root):
        pass
    paths = [p for p, _ in synced]
    for parent in (tmp_path, tmp_path / "a", tmp_path / "a" / "b"):
        assert parent in paths, parent
    assert paths.index(tmp_path) < paths.index(tmp_path / "a") < paths.index(root.parent)
    assert paths.index(root.parent) < paths.index(root)  # root's entry, then its subdirectories
    assert all(not full for _, full in synced)


def test_a_failed_directory_flush_at_first_open_is_redone_by_the_next_open(
    tmp_path, monkeypatch
) -> None:
    """The directory survives a failed parent fsync, so a later open must not assume it was
    made durable: it flushes the store's parent and the store itself again, and a failure
    there raises instead of being swallowed."""
    import errno
    from pathlib import Path

    from sidegraph import maintenance

    root = tmp_path / "s"
    real = maintenance.fsync_dir

    def _eio(path: Path, *, full: bool = True) -> None:
        raise OSError(errno.EIO, "injected")

    monkeypatch.setattr(maintenance, "fsync_dir", _eio)
    with pytest.raises(OSError, match="injected"):
        Store(root)
    assert root.is_dir()  # the directory outlived the failed flush

    synced: list[Path] = []

    def _spy(path: Path, *, full: bool = True) -> None:
        synced.append(Path(path))
        real(path, full=full)

    monkeypatch.setattr(maintenance, "fsync_dir", _spy)
    with Store(root) as reopened:
        assert tmp_path in synced and root in synced
        before = len(synced)
        reopened.add_decision(_decision())
        assert len(synced) > before


def test_opening_an_existing_store_flushes_its_root_once_and_writes_add_none(
    tmp_path, monkeypatch
) -> None:
    from pathlib import Path

    from sidegraph import maintenance

    root = tmp_path / "s"
    with Store(root):
        pass
    synced: list[Path] = []
    real = maintenance.fsync_dir

    def _spy(path: Path, *, full: bool = True) -> None:
        synced.append(Path(path))
        real(path, full=full)

    monkeypatch.setattr(maintenance, "fsync_dir", _spy)
    with Store(root) as s:
        assert synced.count(tmp_path) == 1 and synced.count(root) == 1
        for i in range(20):
            s.add_decision(_decision(title=f"d{i}"))
    assert synced.count(tmp_path) == 1 and synced.count(root) == 1
