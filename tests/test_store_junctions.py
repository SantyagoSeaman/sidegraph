"""Portable admission tests for junctions; real NTFS coverage is in the Windows acceptance."""

from __future__ import annotations

from pathlib import Path

import pytest

from sidegraph import store_layout
from sidegraph.store import Store
from sidegraph.verify import verify_snapshot
from tests.store_format2_support import decision_body, make_store, put_file


def junction_result(monkeypatch, target: Path) -> None:
    """Model the Windows filesystem result at pathlib's boundary, without emulating a link."""
    original = Path.is_junction
    monkeypatch.setattr(Path, "is_junction", lambda p: p == target or original(p))


@pytest.mark.parametrize("name", [*store_layout.CANONICAL_SUBDIRS, "archive", "shards"])
def test_store_rejects_an_internal_junction_before_creating_metadata(tmp_path, monkeypatch, name):
    root = tmp_path / "store"
    root.mkdir()
    internal = root / name
    internal.mkdir()
    assert not internal.is_symlink()
    junction_result(monkeypatch, internal)
    with pytest.raises(ValueError, match="is a symlink"):
        Store(root)
    assert not (root / "format").exists()
    assert not (root / "index.db").exists()


def test_junction_named_like_a_shard_is_an_internal_link(tmp_path, monkeypatch):
    root = make_store(tmp_path)
    internal = root / "shards" / "05.shard"
    internal.mkdir(parents=True)
    junction_result(monkeypatch, internal)
    with pytest.raises(ValueError, match="is a symlink"):
        Store(root)


def test_verification_reports_a_junction_without_listing_its_target(tmp_path, monkeypatch):
    root = make_store(tmp_path)
    internal = root / "shards"
    internal.mkdir()
    junction_result(monkeypatch, internal)
    assert [f.code for f in verify_snapshot(root)] == ["symlinked-store-entry"]


def test_maintenance_stops_if_a_junction_appears_after_open(tmp_path, monkeypatch):
    root = make_store(tmp_path)
    source = put_file(root, "decision", decision_body("D1"))
    with Store(root) as opened:
        before = source.read_bytes()
        internal = root / "shards"
        internal.mkdir(exist_ok=True)
        junction_result(monkeypatch, internal)
        report = opened.maintain(settled_days=0, dry_run=False, any_branch=True)
        assert report.stopped and report.stopped.startswith("refused: symlinked store entries")
        assert source.read_bytes() == before


def test_a_deliberately_linked_store_root_is_still_allowed(tmp_path, monkeypatch):
    root = tmp_path / "shared"
    root.mkdir()
    junction_result(monkeypatch, root)
    with Store(root) as opened:
        assert opened.path == root
        assert (root / "format").exists()
