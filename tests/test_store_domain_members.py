"""The effective-membership index: rows (domain, member node), derived and index-only.

see design/superpowers/specs/2026-10-14-graphify-upgrade-design.md (D5.3)"""

from __future__ import annotations

import contextlib
import os

import pytest

from sidegraph.schema import Domain, DomainStatus, Provenance
from sidegraph.store import (
    MEMBERSHIP_FAILED_KEY,
    MEMBERSHIP_POPULATED_KEY,
    MEMBERSHIP_STAMP_KEY,
    VOLATILE_STALE_KEY,
    Store,
)
from sidegraph.store_layout import (
    DOMAIN_SNAPSHOT_NAME,
    read_snapshot_bytes,
    snapshot_signature,
    snapshot_signature_of,
)

ROWS = [("n1", "src/a.py", "1"), ("n2", None, "1"), ("n3", "docs/a.md", "1~doc")]


def _accepted(store: Store, slug: str) -> Domain:
    d = store.add_domain(
        Domain(slug=slug, title=slug, summary="s", provenance=Provenance(source="manual"))
    )
    store.ratify_domains(accept=[d.domain_id])
    return store.get_domain(d.domain_id)


def test_members_are_stored_and_read_back(tmp_path):
    """Red against the unfixed store: no table, no API (AttributeError)."""
    store = Store(tmp_path / ".sidegraph")
    a, b = _accepted(store, "a"), _accepted(store, "b")
    assert store.replace_domain_members(a.domain_id, ROWS) is True
    store.replace_domain_members(b.domain_id, [("n9", "src/b.py", "2")])
    assert store.domain_member_ids(a.domain_id) == {"n1", "n2", "n3"}
    assert store.domain_member_files(a.domain_id) == {"src/a.py", "docs/a.md"}
    assert store.replace_domain_members(a.domain_id, list(reversed(ROWS))) is False
    assert store.member_communities([a.domain_id]) == {"1", "1~doc"}
    assert store.member_communities([a.domain_id, b.domain_id]) == {"1", "1~doc", "2"}


def test_lookup_by_node_returns_accepted_domains_newest_first(tmp_path):
    store = Store(tmp_path / ".sidegraph")
    older, newer = _accepted(store, "older"), _accepted(store, "newer")
    for d in (older, newer):
        store.replace_domain_members(d.domain_id, ROWS[:1])
    assert [d.slug for d in store.find_domains_for_node("n1")] == ["newer", "older"]
    assert store.find_domains_for_node("n9") == []


def test_a_dropped_or_superseded_domain_loses_its_members(tmp_path):
    """Red against rows that outlive the status change."""
    store = Store(tmp_path / ".sidegraph")
    a, b = _accepted(store, "a"), _accepted(store, "b")
    for d in (a, b):
        store.replace_domain_members(d.domain_id, ROWS)
    store.ratify_domains(drop=[a.domain_id])
    store.supersede_domain(
        b.domain_id,
        Domain(
            slug="b",
            title="b",
            summary="s2",
            supersedes=b.domain_id,
            provenance=Provenance(source="manual"),
        ),
    )
    assert store.domain_member_ids(a.domain_id) == set()
    assert store.domain_member_ids(b.domain_id) == set()


def test_every_domain_status_change_uncertifies_the_index(tmp_path):
    """Red against status changes that keep the certification: a second accepted heir splits a
    covered domain and a drop ends the split, with no graph change, and capture would read the
    rows the change made stale."""
    store = Store(tmp_path / ".sidegraph")
    a = _accepted(store, "a")
    p = store.add_domain(
        Domain(slug="p", title="p", summary="s", provenance=Provenance(source="manual"))
    )

    def certified() -> None:
        store.set_meta(MEMBERSHIP_STAMP_KEY, "graph|absent")
        store.set_meta(MEMBERSHIP_FAILED_KEY, "{}")
        store.set_meta(MEMBERSHIP_POPULATED_KEY, "1")

    def uncertified() -> None:
        assert store.get_meta(MEMBERSHIP_STAMP_KEY) is None
        assert store.get_meta(MEMBERSHIP_FAILED_KEY) is None
        assert store.get_meta(MEMBERSHIP_POPULATED_KEY) == "1"  # the rows are still there

    certified()
    store.ratify_domains(accept=[p.domain_id])
    uncertified()
    certified()
    store.ratify_domains(drop=[p.domain_id])
    uncertified()
    certified()
    store.supersede_domain(
        a.domain_id,
        Domain(
            slug="a",
            title="a",
            summary="s2",
            supersedes=a.domain_id,
            provenance=Provenance(source="manual"),
        ),
    )
    uncertified()
    certified()
    store.add_domain(
        Domain(
            slug="c",
            title="c",
            summary="s",
            status=DomainStatus.ACCEPTED,
            provenance=Provenance(source="manual"),
        )
    )
    uncertified()
    certified()
    store.add_domain(
        Domain(slug="d", title="d", summary="s", provenance=Provenance(source="manual"))
    )
    store.ratify_domains(accept=["01NOSUCHDOMAIN"])
    assert store.get_meta(MEMBERSHIP_STAMP_KEY) == "graph|absent"  # a proposal, a refusal


def test_a_savepoint_undoes_only_its_own_writes(tmp_path):
    """Red against nothing existing (AttributeError). A membership pass publishes every
    domain in one reservation and must undo one domain's half-written membership alone."""
    store = Store(tmp_path / ".sidegraph")
    with store._mutation():
        store.set_meta("probe_a", "1")
        with contextlib.suppress(RuntimeError), store._savepoint():
            store.set_meta("probe_b", "1")
            raise RuntimeError("half written")
        store.set_meta("probe_c", "1")
    assert [store.get_meta(k) for k in ("probe_a", "probe_b", "probe_c")] == ["1", None, "1"]


def test_a_reload_drops_the_index_and_its_stamp(tmp_path):
    """Red against a reload that keeps rows (or their stamp) for a store it no longer certifies."""
    root = tmp_path / ".sidegraph"
    with Store(root) as store:
        d = _accepted(store, "a")
        store.replace_domain_members(d.domain_id, ROWS)
        store.set_meta(MEMBERSHIP_STAMP_KEY, "graph|absent")
        store.set_meta(MEMBERSHIP_FAILED_KEY, "{}")
        store.set_meta(MEMBERSHIP_POPULATED_KEY, "1")
        store.set_meta(VOLATILE_STALE_KEY, "0")
    domain_file = root / "domains" / f"{d.domain_id}.json"
    st = domain_file.stat()
    os.utime(domain_file, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    with Store(root) as store:
        assert store.domain_member_ids(d.domain_id) == set()
        assert store.get_meta(MEMBERSHIP_STAMP_KEY) is None
        assert store.get_meta(MEMBERSHIP_FAILED_KEY) is None
        assert store.get_meta(MEMBERSHIP_POPULATED_KEY) is None
        assert store.get_meta(VOLATILE_STALE_KEY) == "1"


def test_the_snapshot_signature_is_the_hash_of_its_content(tmp_path):
    """Red against a stat signature: rewriting other bytes with the same size and mtime would
    keep the identity a membership pass certified."""
    assert (read_snapshot_bytes(tmp_path), snapshot_signature(tmp_path)) == (None, "absent")
    path = tmp_path / DOMAIN_SNAPSHOT_NAME
    path.write_bytes(b'{"a": 1}\n')
    first = snapshot_signature(tmp_path)
    st = path.stat()
    path.write_bytes(b'{"b": 2}\n')
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert snapshot_signature(tmp_path) != first
    assert snapshot_signature(tmp_path) == snapshot_signature_of(read_snapshot_bytes(tmp_path))
    assert first.startswith("sha256:")


def test_a_large_cache_lasts_until_the_owning_transaction_ends(tmp_path):
    """Red against no ``large_cache`` (TypeError), and against a cache restored when the scope
    that asked for it exits: inside an outer reservation the transaction is still open, and its
    uncommitted pages would spill on the 2 MB default before the commit. Restored after a
    commit and after a rollback alike."""
    store = Store(tmp_path / ".sidegraph")

    def cache() -> int:
        return store._conn.execute("PRAGMA cache_size").fetchone()[0]

    before = cache()
    with store._mutation():
        with store._mutation(large_cache=True):
            assert cache() == -65536
        assert cache() == -65536 and store._conn.in_transaction
    assert cache() == before
    with pytest.raises(RuntimeError), store._mutation():
        with store._mutation(large_cache=True):
            store.set_meta("probe", "1")
        raise RuntimeError("the owning transaction rolls back")
    assert cache() == before and store.get_meta("probe") is None
