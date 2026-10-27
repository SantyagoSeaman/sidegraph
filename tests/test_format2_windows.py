"""Format-2 acceptance: portable workflow plus real NTFS boundaries on Windows CI."""

from __future__ import annotations

import errno
import json
import os
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path

import pytest

from sidegraph.cli import verify_main
from sidegraph.maintenance import publish_bytes
from sidegraph.maintenance_clock import ClockError
from sidegraph.migrate import migrate
from sidegraph.schema import Decision
from sidegraph.store import Store
from sidegraph.verify import verify_snapshot
from tests.store_format2_support import (
    OLD,
    canonical_bytes,
    commit_all,
    effective_snapshot,
    format1_committed,
    git,
    own_bucket,
    put_file,
    shard_bytes,
)

windows = pytest.mark.skipif(os.name != "nt", reason="requires native NTFS behavior")


@pytest.fixture
def repo():
    """A short scratch root keeps the ordinary case separate from the long-path case."""
    with tempfile.TemporaryDirectory(prefix="sg367-") as scratch:
        root = Path(scratch) / "repo"
        root.mkdir()
        git(root, "init", "-q", "-b", "main")
        git(root, "config", "user.name", "test")
        git(root, "config", "user.email", "test@example.com")
        git(root, "config", "gc.auto", "0")
        git(root, "config", "core.longpaths", "true")
        yield root


def compact(store: Path):
    with Store(store) as opened:
        return opened.maintain(settled_days=0, dry_run=False, any_branch=True)


def seed(repo: Path) -> Path:
    store = format1_committed(repo)
    # Use the full current-writer shape, rather than the minimal schema fixture.
    path = store / "decisions" / "D1.json"
    body = Decision.model_validate(json.loads(path.read_text(encoding="utf-8")))
    put_file(store, "decision", body.model_dump(mode="json"))
    commit_all(repo, "seed current writer defaults", date=OLD)
    return store


def accept_packed_decision(store: Path, repo: Path) -> dict:
    with Store(store) as opened:
        opened.ratify("D1", actor="native acceptance")
    assert (store / "decisions" / "D1.json").is_file()
    expected = effective_snapshot(store)
    assert expected[("decision", "D1")]["status"] == "accepted"
    commit_all(repo, "accept packed decision", date=OLD)
    return expected


def checkout_shards(repo: Path, store: Path) -> None:
    """Exercise Git's checkout conversion, not just the binary Python publisher."""
    expected = shard_bytes(store)
    for path in (store / "shards").glob("*.shard"):
        path.unlink()
    git(repo, "checkout", "--", (store / "shards").relative_to(repo).as_posix())
    assert shard_bytes(store) == expected


@pytest.mark.parametrize("autocrlf", ["true", "false"])
@pytest.mark.parametrize("long_path", [False, True], ids=["short", "over-260"])
def test_migrate_edit_compact_verify(repo, capsys, autocrlf, long_path):
    git(repo, "config", "core.autocrlf", autocrlf)
    store = seed(repo)
    if long_path:
        nested = repo
        while len(str(nested / ".sidegraph")) <= 275:
            nested = nested / ("native-path-" + "x" * 45)
        nested.mkdir(parents=True)
        relocated = nested / ".sidegraph"
        store.rename(relocated)
        store = relocated
        # Prepare from the short Git root: Git for Windows can reject a long -C
        # directory before Sidegraph even gets an opportunity to handle it.
        commit_all(repo, "move store under long path", date=OLD)
    baseline = git(repo, "rev-parse", "HEAD").strip()
    before = effective_snapshot(store)
    # Force an actual checkout under the requested conversion policy.
    git(repo, "ls-files", "--error-unmatch", str((store / "format").relative_to(repo)))
    (store / "format").unlink()
    git(repo, "checkout", "--", ".")
    untouched = canonical_bytes(store)
    try:
        migrated_report = migrate(store, dry_run=False, any_branch=True)
    except ClockError as exc:
        if os.name != "nt" or not long_path:
            raise
        assert "git " in str(exc)
        assert canonical_bytes(store) == untouched
        assert effective_snapshot(store) == before
        # The same contents and Git configuration must work at a short path;
        # an arbitrary broken-Git failure cannot satisfy this operational case.
        restored = repo / ".sidegraph"
        store.rename(restored)
        store = restored
        baseline = commit_all(repo, "restore short store path", date=OLD)
        migrated_report = migrate(store, dry_run=False, any_branch=True)
    assert migrated_report.stopped is None
    assert effective_snapshot(store) == before
    assert verify_main(["--db", str(store), "--against", baseline]) == 0, capsys.readouterr().out
    migrated = commit_all(repo, "migrate", date=OLD)
    checkout_shards(repo, store)
    assert verify_main(["--db", str(store), "--against", baseline]) == 0, capsys.readouterr().out
    expected = accept_packed_decision(store, repo)
    assert verify_main(["--db", str(store), "--against", migrated]) == 0, capsys.readouterr().out
    accepted = git(repo, "rev-parse", "HEAD").strip()
    report = compact(store)
    assert report.stopped is None
    assert report.skipped == []
    assert effective_snapshot(store) == expected
    assert not (store / "decisions" / "D1.json").exists(), report
    packed = shard_bytes(store)
    assert len(packed) == 64
    assert any(packed.values())
    assert all(b"\r" not in data for data in packed.values())
    assert verify_main(["--db", str(store), "--json"]) == 0, capsys.readouterr().out
    assert verify_main(["--db", str(store), "--against", accepted]) == 0, capsys.readouterr().out
    commit_all(repo, "compact edited record", date=OLD)
    checkout_shards(repo, store)
    assert effective_snapshot(store) == expected
    assert verify_main(["--db", str(store), "--against", accepted]) == 0, capsys.readouterr().out
    finished = canonical_bytes(store)
    assert compact(store).stopped is None
    assert canonical_bytes(store) == finished


@windows
def test_open_shard_blocks_replacement_without_losing_the_hot_shadow(repo):
    store = seed(repo)
    migrate(store, dry_run=False, any_branch=True)
    commit_all(repo, "migrate", date=OLD)
    expected = accept_packed_decision(store, repo)
    target = store / "shards" / own_bucket(store, "D1")
    old_bytes = target.read_bytes()
    source = store / "decisions" / "D1.json"
    source_bytes = source.read_bytes()
    with target.open("rb"):
        with pytest.raises(PermissionError):
            compact(store)
        assert target.read_bytes() == old_bytes
        assert source.read_bytes() == source_bytes
        assert effective_snapshot(store) == expected
        assert not list((store / "shards").glob("*.tmp"))
    assert compact(store).stopped is None
    assert effective_snapshot(store) == expected
    assert not source.exists()
    assert verify_snapshot(store) == []


def test_publisher_temp_name_limit_preserves_existing_target(tmp_path):
    # The target fits a 255-byte component; the publisher's unique suffix does not.
    target = tmp_path / ("x" * 240)
    target.write_bytes(b"original\n")
    with pytest.raises(OSError) as caught:
        publish_bytes(target, b"replacement\n", expect=b"original\n")
    assert caught.value.errno in {errno.ENAMETOOLONG, errno.ENOENT, errno.EINVAL}
    assert target.read_bytes() == b"original\n"
    assert list(tmp_path.iterdir()) == [target]


@windows
def test_case_alias_is_refused_and_never_unlinked(repo):
    store = seed(repo)
    migrate(store, dry_run=False, any_branch=True)
    commit_all(repo, "migrate", date=OLD)
    lower = store / "shards" / "0a.shard"
    upper = store / "shards" / "0A.shard"
    # Two-step rename changes the directory entry's spelling on NTFS.
    intermediate = lower.with_name("rename.tmp")
    lower.rename(intermediate)
    intermediate.rename(upper)
    assert lower.exists() and upper.exists() and lower.samefile(upper)
    before = canonical_bytes(store)
    assert "shard-name-case" in {f.code for f in verify_snapshot(store)}
    report = compact(store)
    assert report.stopped and "case" in report.stopped.lower()
    assert canonical_bytes(store) == before
    assert upper.exists()


@contextmanager
def junction(link: Path, target: Path):
    """Create a real directory junction; remove only the link during teardown."""
    subprocess.run(
        ["cmd", "/d", "/c", "mklink", "/J", str(link), str(target)],
        check=True,
        capture_output=True,
        timeout=15,
    )
    try:
        assert link.is_junction() and not link.is_symlink()
        yield
    finally:
        link.rmdir()


@windows
@pytest.mark.parametrize("name", ["decisions", "shards", "shards/05.shard"])
def test_real_internal_junction_is_refused_before_any_store_write(tmp_path, name):
    root, outside = tmp_path / "store", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_bytes(b"untouched\n")
    link = root / name
    link.parent.mkdir(parents=True, exist_ok=True)
    with junction(link, outside):
        with pytest.raises(ValueError, match="is a symlink"):
            Store(root)
        assert not (root / "format").exists()
        assert not (root / "index.db").exists()
        assert list(outside.iterdir()) == [sentinel]
        assert sentinel.read_bytes() == b"untouched\n"


@windows
def test_real_junction_added_after_open_stops_maintenance(repo):
    store = seed(repo)
    migrate(store, dry_run=False, any_branch=True)
    commit_all(repo, "migrate", date=OLD)
    expected = accept_packed_decision(store, repo)
    original = store / "shards"
    relocated = repo / "outside-shards"
    with Store(store) as opened:
        original.rename(relocated)
        try:
            before = {p.name: p.read_bytes() for p in relocated.iterdir()}
            with junction(original, relocated):
                report = opened.maintain(settled_days=0, dry_run=False, any_branch=True)
                assert report.stopped and "symlinked store entries" in report.stopped
                assert {p.name: p.read_bytes() for p in relocated.iterdir()} == before
                assert (store / "decisions" / "D1.json").exists()
        finally:
            relocated.rename(original)
    assert effective_snapshot(store) == expected


@windows
def test_real_junction_store_root_is_allowed(tmp_path):
    target, link = tmp_path / "shared", tmp_path / "store"
    target.mkdir()
    with junction(link, target), Store(link):
        assert (target / "format").exists()
        assert (target / "index.db").exists()
