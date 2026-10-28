"""First-seen stamps in the code-drift cache: the refresh publishes them in one transaction,
carries them across refreshes and hands back the total and the recent count as one pair.

see design/superpowers/specs/2026-10-27-drift-notice-delta-design.md (4.2, 4.3)
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sidegraph import doctor as doctor_mod
from sidegraph import drift_checks, server
from sidegraph import sync as sync_mod
from sidegraph.retrieval import DRIFT_CACHE_KEY, recent_drift
from sidegraph.schema import (
    AnchorBinding,
    Decision,
    DecisionKind,
    DecisionStatus,
    Descriptor,
    Entity,
    Provenance,
)
from sidegraph.store import Store, StoreBusyError
from sidegraph.store_layout import VOLATILE_STALE_KEY
from tests.drift_checks_support import require_check

T0 = datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC)


def D(n: int) -> datetime:
    return T0 + timedelta(days=n)


def iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


class _Held:
    """One held lock: `release()` ends it now, otherwise it ends after its delay."""

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._released = threading.Event()

    def release(self) -> None:
        self._stop.set()

    def is_set(self) -> bool:
        return self._released.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        return self._released.wait(timeout)


@pytest.fixture
def held_lock():
    """Hold a real index lock until the test ends or a delay passes, and always release it."""

    helds: list[_Held] = []

    def hold(
        store_dir: Path,
        statement: str = "BEGIN EXCLUSIVE",
        delay: float = 30.0,
        database_path: Path | None = None,
    ) -> _Held:
        conn = sqlite3.connect(
            database_path or store_dir / "index.db", timeout=0.1, check_same_thread=False
        )
        conn.execute(statement)
        held = _Held()
        helds.append(held)

        def release() -> None:
            try:
                held._stop.wait(delay)
                if conn.in_transaction:
                    conn.rollback()
            finally:
                conn.close()
                held._released.set()

        threading.Thread(target=release, daemon=True).start()
        return held

    yield hold
    for held in helds:
        held.release()
        assert held.wait(2), "lock holder did not clean up"


def _git(args: list[str], cwd: Path) -> None:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"


def _head(cwd: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def git_repo(tmp_path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], cwd=repo)
    _git(["config", "user.email", "test@example.com"], cwd=repo)
    _git(["config", "user.name", "Test"], cwd=repo)
    return repo


def _stamped_decision(store: Store, commit: str, file_path: str, name: str) -> Decision:
    d = store.add_decision(
        Decision(
            title=f"About {name}",
            kind=DecisionKind.GOTCHA,
            context="c",
            choice="ch",
            status=DecisionStatus.ACCEPTED,
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="agent", commit=commit),
        )
    )
    e = store.upsert_entity(
        Entity(canonical_name=name, descriptor=Descriptor(name=name, file_path=file_path))
    )
    store.add_binding(AnchorBinding(record_id=d.id, entity_id=e.entity_id, tier=2, status="live"))
    return d


def _commit_file(repo: Path, rel: str, content: str, msg: str) -> str:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    _git(["add", "-A"], cwd=repo)
    _git(["commit", "-q", "-m", msg], cwd=repo)
    return _head(repo)


def _cache(store: Store) -> dict:
    raw = store.get_meta(DRIFT_CACHE_KEY)
    assert raw is not None
    return json.loads(raw)


def _cached_ids(store: Store) -> set[str]:
    return {rid for ids in _cache(store)["by_commit"].values() for rid in ids}


class World:
    """Two stamped records, ``x`` on a.py and ``y`` on b.py, each on its own capture commit."""

    def __init__(self, repo: Path) -> None:
        self.repo = repo
        self.commit_a = _commit_file(repo, "a.py", "a\n", "c1")
        self.commit_b = _commit_file(repo, "b.py", "b\n", "c2")
        self.store = Store(repo / ".sidegraph")
        self.x = _stamped_decision(self.store, self.commit_a, "a.py", "A")
        self.y = _stamped_decision(self.store, self.commit_b, "b.py", "B")
        self.edits = 0

    def drift(self, name: str) -> None:
        self.edits += 1
        _commit_file(self.repo, name, f"changed {self.edits}\n", f"edit {self.edits}")

    def refresh(self, now: datetime) -> sync_mod.DriftRefresh | None:
        return sync_mod.refresh_code_drift_snapshot(self.store, now=now, repo_root=self.repo)


@pytest.fixture
def world(git_repo) -> World:
    return World(git_repo)


def test_first_refresh_seeds(world):
    world.drift("a.py")
    snap = world.refresh(D(0))
    assert snap == sync_mod.DriftRefresh(1, None)
    cache = _cache(world.store)
    assert cache["first_seen_since"] == iso(D(0))
    assert cache["first_seen"] == {}


def test_second_record_stamped_since_unchanged(world):
    world.drift("b.py")
    world.refresh(D(0))
    world.drift("a.py")
    snap = world.refresh(D(1))
    cache = _cache(world.store)
    assert cache["first_seen_since"] == iso(D(0))
    assert cache["first_seen"] == {world.x.id: iso(D(1))}
    assert snap is not None and snap.total == 2


def test_unchanged_refresh_keeps_stamp(world):
    world.drift("b.py")
    world.refresh(D(0))
    world.drift("a.py")
    world.refresh(D(1))
    world.refresh(D(2))
    assert _cache(world.store)["first_seen"] == {world.x.id: iso(D(1))}


def test_old_shape_cache_seeds_without_stamps(world):
    world.drift("a.py")
    world.store.set_meta(
        DRIFT_CACHE_KEY,
        json.dumps({"head": "h", "computed_at": "x", "by_commit": {world.commit_a: [world.x.id]}}),
    )
    world.refresh(D(0))
    cache = _cache(world.store)
    assert cache["first_seen"] == {}
    assert cache["first_seen_since"] == iso(D(0))


def test_check_between_cache_read_and_publish_gets_later_stamp(world, monkeypatch):
    world.drift("b.py")
    world.refresh(D(0))
    world.drift("a.py")
    world.refresh(D(1))
    assert _cache(world.store)["first_seen"][world.x.id] == iso(D(1))
    monkeypatch.setattr(server, "_store", world.store)
    monkeypatch.setattr(server, "_load_reader", lambda: None)
    # The scan has listed x already (git is unchanged by the check). Run the check on the
    # first cache access of the refresh, after its read and before its write lock.
    real_get, real_update = Store.get_meta, Store.update_meta_if
    state = {"fired": False}

    def inject() -> None:
        if state["fired"]:
            return
        state["fired"] = True
        require_check(world.repo, world.x.id, files=["a.py"])

    def get_meta(self, key):
        value = real_get(self, key)
        if key == DRIFT_CACHE_KEY:
            inject()
        return value

    def update_meta_if(self, key, decide):
        if key == DRIFT_CACHE_KEY:
            inject()
        return real_update(self, key, decide)

    monkeypatch.setattr(Store, "get_meta", get_meta)
    monkeypatch.setattr(Store, "update_meta_if", update_meta_if)
    world.refresh(D(2))
    monkeypatch.undo()
    assert state["fired"]
    assert _cache(world.store)["first_seen"][world.x.id] == iso(D(2))


def test_busy_returns_none_cache_untouched(world, held_lock, monkeypatch):
    world.drift("a.py")
    world.refresh(D(0))
    world.store.set_meta(VOLATILE_STALE_KEY, "0")
    before = world.store.get_meta(DRIFT_CACHE_KEY)
    store_dir = world.repo / ".sidegraph"
    attempts: list[str] = []
    real_update = Store.update_meta_if

    def spy(self, key, decide):
        try:
            return real_update(self, key, decide)
        except StoreBusyError:
            attempts.append("busy")
            raise

    monkeypatch.setattr(Store, "update_meta_if", spy)
    held_lock(store_dir, statement="BEGIN IMMEDIATE")
    hook_store = Store(store_dir, busy_timeout_seconds=0.0)
    started = time.monotonic()
    result = sync_mod.refresh_code_drift_snapshot(
        hook_store, now=D(1), repo_root=world.repo, busy_timeout_seconds=0.0
    )
    assert time.monotonic() - started < 0.5
    assert result is None
    assert attempts == ["busy"]
    assert hook_store.get_meta(DRIFT_CACHE_KEY) == before
    hook_store.close()


def _orphan(world: World) -> AnchorBinding:
    binding = world.store.bindings_for_record(world.x.id)[0]
    world.store.add_binding(binding.model_copy(update={"status": "orphaned"}))
    return binding


def test_orphan_then_return_keeps_old_stamp(world):
    world.drift("b.py")
    world.refresh(D(0))
    world.drift("a.py")
    world.refresh(D(1))
    binding = _orphan(world)
    world.refresh(D(2))
    assert world.x.id not in _cached_ids(world.store)
    assert _cache(world.store)["first_seen_absent"][world.x.id] == [iso(D(1)), iso(D(2))]
    world.store.add_binding(binding.model_copy(update={"status": "live"}))
    world.refresh(D(7))
    assert _cache(world.store)["first_seen"][world.x.id] == iso(D(1))


def test_seeded_orphan_rebind_stays_unstamped(world):
    world.drift("a.py")
    world.drift("b.py")
    world.refresh(D(0))
    binding = _orphan(world)
    world.refresh(D(1))
    assert world.x.id not in _cached_ids(world.store)
    assert _cache(world.store)["first_seen_absent"][world.x.id] == [None, iso(D(1))]
    world.store.add_binding(binding.model_copy(update={"status": "live"}))
    snap = world.refresh(D(9))
    cache = _cache(world.store)
    assert world.x.id not in cache["first_seen"]
    assert snap == sync_mod.DriftRefresh(2, (0, "in the last 7 days"))


def test_failed_batch_retains_ids_and_stamps(world, monkeypatch):
    world.refresh(D(0))
    world.drift("a.py")
    world.refresh(D(1))
    real = doctor_mod._git_diff_name_only

    def failing_for_a(repo_root, commit, paths, **kw):
        if commit == world.commit_a:
            raise ValueError("simulated failure")
        return real(repo_root, commit, paths, **kw)

    monkeypatch.setattr(doctor_mod, "_git_diff_name_only", failing_for_a)
    world.refresh(D(2))
    cache = _cache(world.store)
    assert cache["by_commit"][world.commit_a] == [world.x.id]
    assert cache["first_seen"] == {world.x.id: iso(D(1))}


def test_retained_comparison_never_stamps_now(world, monkeypatch):
    monkeypatch.setattr(server, "_store", world.store)
    monkeypatch.setattr(server, "_load_reader", lambda: None)
    world.refresh(D(0))
    world.drift("a.py")
    world.refresh(D(1))
    assert _cache(world.store)["first_seen"] == {world.x.id: iso(D(1))}
    require_check(world.repo, world.x.id, files=["a.py"])
    assert world.x.id not in _cached_ids(world.store)

    def raiser(*args, **kwargs):
        raise ValueError("synthetic Git failure")

    with monkeypatch.context() as patched:
        patched.setattr(drift_checks, "tree_entries", raiser)
        world.refresh(D(2))
    cache = _cache(world.store)
    assert world.x.id in _cached_ids(world.store)
    assert world.x.id not in cache["first_seen"]

    world.drift("a.py")  # the check no longer matches: a real re-arm
    world.drift("b.py")  # y drifts for the first time, with the comparison running
    world.refresh(D(3))
    cache = _cache(world.store)
    assert world.x.id in _cached_ids(world.store)
    assert world.x.id not in cache["first_seen"]
    assert cache["first_seen"][world.y.id] == iso(D(3))


def test_unreadable_checks_stamp_nothing_now_but_a_failed_comparison_does():
    entry = doctor_mod.DriftEntry("rec.json", "new1", ["a.py"])
    batch = doctor_mod.DriftBatch("c1", False, [entry])
    raw = json.dumps(
        {
            "first_seen_since": iso(T0),
            "by_commit": {"c1": []},
            "first_seen": {},
            "first_seen_absent": {},
        }
    )

    def scan(**flags):
        return doctor_mod.CodeDriftScan([batch], 0, True, False, "h", False, **flags)

    unreadable = scan(check_skipped=True, checks_unreadable=True)
    assert sync_mod.build_cache(raw, unreadable, D(1))["first_seen"] == {}
    comparison_failed = scan(check_skipped=True)
    assert sync_mod.build_cache(raw, comparison_failed, D(1))["first_seen"] == {"new1": iso(D(1))}
    assert sync_mod.build_cache(raw, scan(), D(1))["first_seen"] == {"new1": iso(D(1))}


def test_unavailable_section_scope_is_retained_never_stamped_now():
    unavailable = doctor_mod.DriftEntry(
        "rec.json", "sec1", ["spec.md"], doctor_mod.SECTION_UNAVAILABLE
    )
    plain = doctor_mod.DriftEntry("rec.json", "plain1", ["a.py"], "checked files changed")
    batch = doctor_mod.DriftBatch("c1", False, [unavailable, plain])
    raw = json.dumps(
        {
            "first_seen_since": iso(T0),
            "by_commit": {"c1": []},
            "first_seen": {},
            "first_seen_absent": {},
        }
    )
    scan = doctor_mod.CodeDriftScan([batch], 0, True, False, "h", False)
    cache = sync_mod.build_cache(raw, scan, D(1))
    assert cache["first_seen"] == {"plain1": iso(D(1))}
    assert sorted(cache["by_commit"]["c1"]) == ["plain1", "sec1"]


def test_latest_checks_failure_marks_the_scan_unreadable(world, monkeypatch):
    world.drift("a.py")

    def unreadable(store_dir):
        raise OSError("synthetic")

    monkeypatch.setattr(drift_checks, "latest_checks", unreadable)
    scan = doctor_mod.scan_code_drift(world.store.path, world.repo)
    assert scan.check_skipped and scan.checks_unreadable
    monkeypatch.undo()
    assert not doctor_mod.scan_code_drift(world.store.path, world.repo).checks_unreadable


def test_new_record_drifting_in_a_failed_comparison_refresh_is_stamped(world, monkeypatch):
    _use_server_store(world, monkeypatch)
    world.refresh(D(0))
    world.drift("a.py")
    world.refresh(D(1))
    require_check(world.repo, world.x.id, files=["a.py"])

    def raiser(*args, **kwargs):
        raise ValueError("synthetic Git failure")

    world.drift("b.py")  # y has no check: its drift comes from the capture diff, which ran
    with monkeypatch.context() as patched:
        patched.setattr(drift_checks, "tree_entries", raiser)
        world.refresh(D(9))
    assert _cache(world.store)["first_seen"][world.y.id] == iso(D(9))
    snap = world.refresh(D(10))
    assert snap is not None and snap.recent == (1, "in the last 7 days")


def test_retention_reason_filter_alone_keeps_a_checked_record_unstamped(world, monkeypatch):
    _use_server_store(world, monkeypatch)
    world.refresh(D(0))
    world.drift("a.py")
    world.refresh(D(1))
    require_check(world.repo, world.x.id, files=["a.py"])

    def invalid(*args, **kwargs):
        raise ValueError("synthetic invalid check")

    with monkeypatch.context() as patched:
        patched.setattr(drift_checks, "subject_digest", invalid)
        scan = doctor_mod.scan_code_drift(world.store.path, world.repo)
        assert not scan.check_skipped
        world.refresh(D(2))
    cache = _cache(world.store)
    assert world.x.id in _cached_ids(world.store)
    assert world.x.id not in cache["first_seen"]


def test_unnormalizable_previous_since_still_prints_the_total(world):
    world.drift("a.py")
    world.store.set_meta(
        DRIFT_CACHE_KEY,
        json.dumps(
            {
                "by_commit": {world.commit_a: [world.x.id]},
                "first_seen_since": "0001-01-01T00:00:00+01:00",
                "first_seen": {},
                "first_seen_absent": {},
            }
        ),
    )
    snap = world.refresh(D(0))
    assert snap == sync_mod.DriftRefresh(1, None)


def test_unreadable_tombstone_counts_nothing_new(world):
    world.drift("a.py")
    world.store.set_meta(
        DRIFT_CACHE_KEY,
        json.dumps(
            {
                "by_commit": {},
                "first_seen_since": iso(D(-30)),
                "first_seen": {},
                "first_seen_absent": {
                    world.x.id: ["0001-01-01T00:00:00+01:00", iso(D(-1))],
                },
            }
        ),
    )
    snap = world.refresh(D(0))
    assert snap == sync_mod.DriftRefresh(1, None)
    assert _cache(world.store)["first_seen"] == {}


@pytest.mark.parametrize("by_commit", [None, [], {"c": "x"}])
def test_malformed_previous_by_commit_counts_nothing_new(world, by_commit):
    world.drift("a.py")
    world.store.set_meta(
        DRIFT_CACHE_KEY,
        json.dumps(
            {
                "by_commit": by_commit,
                "first_seen_since": iso(D(-30)),
                "first_seen": {},
                "first_seen_absent": {},
            }
        ),
    )
    snap = world.refresh(D(0))
    assert snap == sync_mod.DriftRefresh(1, None)
    assert _cache(world.store)["first_seen"] == {}


def test_snapshot_pair_is_one_value(world, monkeypatch):
    world.drift("a.py")
    world.refresh(D(0))
    real_live = sync_mod.live_drifted_ids

    def publish_then_count(store, by_commit):
        cache = _cache(store)
        cache["first_seen"] = {world.x.id: iso(D(20))}
        store.set_meta(DRIFT_CACHE_KEY, json.dumps(cache))
        return real_live(store, by_commit)

    monkeypatch.setattr(sync_mod, "live_drifted_ids", publish_then_count)
    snap = world.refresh(D(20))
    assert snap == sync_mod.DriftRefresh(1, (0, "in the last 7 days"))
    assert snap.recent is not None and snap.recent[0] <= snap.total


def test_wrapper_returns_int_and_inactive_is_zero_none(world, tmp_path):
    world.drift("a.py")
    assert sync_mod.refresh_code_drift_cache(world.store, repo_root=world.repo) == 1
    snap = sync_mod.refresh_code_drift_snapshot(world.store, repo_root=world.repo)
    assert snap is not None and snap.total == 1
    bare = Store(tmp_path / "bare" / ".sidegraph")
    assert sync_mod.refresh_code_drift_snapshot(bare) == sync_mod.DriftRefresh(0, None)
    assert sync_mod.refresh_code_drift_cache(bare) == 0
    bare.close()


def test_repo_root_failure_returns_none(tmp_path):
    store = Store(tmp_path / ".sidegraph")
    d = store.add_decision(
        Decision(
            title="t",
            kind=DecisionKind.GOTCHA,
            context="c",
            choice="ch",
            status=DecisionStatus.ACCEPTED,
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="agent", commit="deadbeef"),
        )
    )
    e = store.upsert_entity(
        Entity(canonical_name="A", descriptor=Descriptor(name="A", file_path="a.py"))
    )
    store.add_binding(AnchorBinding(record_id=d.id, entity_id=e.entity_id, tier=2, status="live"))
    assert sync_mod.refresh_code_drift_snapshot(store) is None
    assert sync_mod.refresh_code_drift_cache(store) is None


# recent_drift on a published value, to keep the pair test honest about its source.
def test_recent_counts_from_the_published_stamps(world):
    world.drift("b.py")
    world.refresh(D(0))
    world.drift("a.py")
    snap = world.refresh(D(1))
    cache = _cache(world.store)
    live = frozenset({world.x.id, world.y.id})
    assert snap is not None and snap.recent == recent_drift(live, cache, D(1))
    assert snap.recent == (1, f"since {D(0).date().isoformat()}")


def _use_server_store(world: World, monkeypatch) -> None:
    monkeypatch.setattr(server, "_store", world.store)
    monkeypatch.setattr(server, "_load_reader", lambda: None)


def test_check_drops_stamp_so_redrift_is_new(world, monkeypatch):
    _use_server_store(world, monkeypatch)
    world.drift("b.py")
    world.refresh(D(0))
    world.drift("a.py")
    world.refresh(D(1))
    require_check(world.repo, world.x.id, files=["a.py"])
    cache = _cache(world.store)
    assert world.x.id not in cache["first_seen"]
    assert world.x.id not in cache["first_seen_absent"]
    world.drift("a.py")
    world.refresh(D(3))
    assert _cache(world.store)["first_seen"][world.x.id] == iso(D(3))


def test_old_client_check_keeps_old_stamp_within_30_days(world, monkeypatch):
    _use_server_store(world, monkeypatch)
    world.drift("b.py")
    world.refresh(D(0))
    world.drift("a.py")
    world.refresh(D(1))
    require_check(world.repo, world.x.id, files=["a.py"])
    # An older client's check rewrites by_commit only: the stamp stays behind.
    cache = _cache(world.store)
    cache["first_seen"][world.x.id] = iso(D(1))
    world.store.set_meta(DRIFT_CACHE_KEY, json.dumps(cache))
    world.refresh(D(2))
    assert _cache(world.store)["first_seen_absent"][world.x.id] == [iso(D(1)), iso(D(2))]
    world.drift("a.py")
    world.refresh(D(5))
    assert _cache(world.store)["first_seen"][world.x.id] == iso(D(1))


def test_check_removes_tombstone_only_id(world, monkeypatch):
    _use_server_store(world, monkeypatch)
    world.drift("b.py")
    world.refresh(D(0))
    world.drift("a.py")
    world.refresh(D(1))
    binding = _orphan(world)
    world.refresh(D(2))
    assert world.x.id in _cache(world.store)["first_seen_absent"]
    world.store.add_binding(binding.model_copy(update={"status": "live"}))
    require_check(world.repo, world.x.id, files=["a.py"])
    assert world.x.id not in _cache(world.store)["first_seen_absent"]
    world.drift("a.py")
    world.refresh(D(4))
    assert _cache(world.store)["first_seen"][world.x.id] == iso(D(4))


def test_check_leaves_cache_without_stamps_byte_identical(world, monkeypatch):
    _use_server_store(world, monkeypatch)
    world.drift("a.py")
    old = {"head": "h", "computed_at": "t", "by_commit": {world.commit_a: [world.x.id]}}
    world.store.set_meta(DRIFT_CACHE_KEY, json.dumps(old))
    require_check(world.repo, world.x.id, files=["a.py"])
    old["by_commit"] = {world.commit_a: []}
    assert world.store.get_meta(DRIFT_CACHE_KEY) == json.dumps(old)


def _marked_drift(world) -> None:
    """``x`` drifted and stamped at D(1), then marked as record_drift_check(clear=False) does."""
    world.drift("b.py")
    world.refresh(D(0))
    world.drift("a.py")
    world.refresh(D(1))
    cache = _cache(world.store)
    cache["first_seen_checked"] = [world.x.id]
    world.store.set_meta(DRIFT_CACHE_KEY, json.dumps(cache))


def test_a_mark_the_next_refresh_clears_ends_the_episode(world):
    """The refresh after a kept-advisory check clears the record: no tombstone, no mark, and
    the next re-drift is new (the pre-release panel's round-2 scenario)."""
    _marked_drift(world)
    _commit_file(world.repo, "a.py", "a\n", "back to the captured text")
    world.refresh(D(3))
    cache = _cache(world.store)
    assert world.x.id not in _cached_ids(world.store)
    assert world.x.id not in cache["first_seen_absent"]
    assert "first_seen_checked" not in cache

    world.drift("a.py")
    snap = world.refresh(D(4))
    assert _cache(world.store)["first_seen"][world.x.id] == iso(D(4))
    assert snap is not None and snap.recent is not None and snap.recent[0] >= 1


def test_a_mark_is_consumed_when_the_record_stays_drifted(world):
    """The check did not end the episode: the stamp lives on unmarked, so a later unrelated
    absence keeps its tombstone and the return restores the old stamp, never a false new
    (the pre-release panel's round-3 scenario)."""
    _marked_drift(world)
    world.refresh(D(2))
    cache = _cache(world.store)
    assert cache["first_seen"][world.x.id] == iso(D(1))
    assert "first_seen_checked" not in cache

    _commit_file(world.repo, "a.py", "a\n", "back to the captured text")
    world.refresh(D(3))
    assert _cache(world.store)["first_seen_absent"][world.x.id][0] == iso(D(1))
    world.drift("a.py")
    world.refresh(D(4))
    assert _cache(world.store)["first_seen"][world.x.id] == iso(D(1))


def test_without_the_mark_a_cleared_record_keeps_its_old_stamp(world):
    """The control: an unmarked id that clears and re-drifts within 30 days restores its
    stamp (a transient absence), so the mark is what makes the difference."""
    world.drift("b.py")
    world.refresh(D(0))
    world.drift("a.py")
    world.refresh(D(1))
    _commit_file(world.repo, "a.py", "a\n", "back to the captured text")
    world.refresh(D(3))
    world.drift("a.py")
    world.refresh(D(4))
    assert _cache(world.store)["first_seen"][world.x.id] == iso(D(1))
