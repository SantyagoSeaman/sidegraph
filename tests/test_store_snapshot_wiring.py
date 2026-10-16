"""The domain snapshot: a store file the freshness check tracks by its own signature, never
through the canonical digest (D5.3).

see design/superpowers/specs/2026-10-14-graphify-upgrade-design.md (D5)"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime

from sidegraph import domain_snapshot as ds
from sidegraph.store import (
    _TMP_DEBRIS_MIN_AGE_SECONDS,
    MEMBERSHIP_STAMP_KEY,
    SNAPSHOT_SIGNATURE_KEY,
    VOLATILE_STALE_KEY,
    Store,
)


def _snap(commit=None):
    return ds.build_snapshot(
        [],
        {},
        {},
        taken_at=datetime(2026, 10, 14, tzinfo=UTC),
        engine_version=None,
        engine_generation="legacy",
        graph_version="content:abc",
        store_commit=commit,
    )


def _settled(store):
    store.replace_domain_members("01X", [("n1", "src/a.py", "1")])
    store.set_meta(MEMBERSHIP_STAMP_KEY, "graph|absent")
    store.set_meta(VOLATILE_STALE_KEY, "0")


def test_the_canonical_digest_ignores_the_snapshot(tmp_path):
    """Red against a digest that includes it (a discarded design, declared): an already-running
    0.13 server would see a digest it never agrees with, reload and re-stamp the index forever."""
    with Store(tmp_path / ".sidegraph") as store:
        before = store._compute_canonical_digest()[0]
        ds.write_snapshot(store.path, _snap())
        assert store._compute_canonical_digest()[0] == before


def test_a_snapshot_change_invalidates_membership_without_a_reload(tmp_path):
    """Red against no signature check: the stamp stays, sync skips the full pass the snapshot
    change needs."""
    with Store(tmp_path / ".sidegraph") as store:
        for commit in (None, "a" * 40):
            _settled(store)
            ds.write_snapshot(store.path, _snap(commit))
            store.refresh_if_stale()
            assert store.get_meta(MEMBERSHIP_STAMP_KEY) is None
            assert store.get_meta(VOLATILE_STALE_KEY) == "1"
            assert store.domain_member_ids("01X") == {"n1"}  # no canonical reload


def test_a_pulled_snapshot_is_seen_at_the_next_open(tmp_path):
    root = tmp_path / ".sidegraph"
    with Store(root) as store:
        _settled(store)
    ds.write_snapshot(root, _snap())
    with Store(root) as store:
        assert store.get_meta(MEMBERSHIP_STAMP_KEY) is None
        assert store.get_meta(VOLATILE_STALE_KEY) == "1"


def test_a_store_without_a_snapshot_writes_nothing_to_notice_it(tmp_path, monkeypatch):
    """Red against a check that records "absent" on the first open of every store: the open
    would need a write it does not use, and fail wherever writes fail (as in
    test_bootstrap_apply's commit fault)."""
    root = tmp_path / ".sidegraph"
    Store(root).close()
    monkeypatch.setattr(
        Store, "_commit", lambda self: (_ for _ in ()).throw(RuntimeError("no writes here"))
    )
    with Store(root) as store:
        store.refresh_if_stale()
        assert store.get_meta(SNAPSHOT_SIGNATURE_KEY) is None


def test_an_unchanged_snapshot_changes_nothing(tmp_path):
    """Guard: the check runs on every refresh (every MCP tool call) and must not churn."""
    with Store(tmp_path / ".sidegraph") as store:
        ds.write_snapshot(store.path, _snap())
        store.refresh_if_stale()
        _settled(store)
        store.refresh_if_stale()
        assert store.get_meta(MEMBERSHIP_STAMP_KEY) == "graph|absent"
        assert store.get_meta(VOLATILE_STALE_KEY) == "0"


def test_crash_debris_of_a_snapshot_write_is_swept(tmp_path):
    """Red against the root sweep patterns without the snapshot."""
    root = tmp_path / ".sidegraph"
    Store(root).close()
    debris = root / "domain-snapshot.json.123.0123456789ab.tmp"
    debris.write_text("{}", encoding="utf-8")
    old = time.time() - 2 * _TMP_DEBRIS_MIN_AGE_SECONDS
    os.utime(debris, (old, old))
    Store(root).close()
    assert not debris.exists()
