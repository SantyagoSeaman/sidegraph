"""Sync indexes each accepted domain's effective membership; without a snapshot, by its rules.

see design/superpowers/specs/2026-10-14-graphify-upgrade-design.md (D5)"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
import time

import pytest

from sidegraph import sync as sync_mod
from sidegraph.cli import sync_main
from sidegraph.engine.reader import ANCHORABLE_FILE_TYPES, GraphifyReader
from sidegraph.retrieval import TOC_CACHE_KEY
from sidegraph.schema import DomainStatus
from sidegraph.store import (
    MEMBERSHIP_FAILED_KEY,
    MEMBERSHIP_POPULATED_KEY,
    MEMBERSHIP_STAMP_KEY,
    VOLATILE_STALE_KEY,
    Store,
    StoreBusyError,
)
from sidegraph.sync import (
    MembershipRefresh,
    SyncReport,
    activate_accepted_domain,
    ensure_domain_membership,
    refresh_domain_communities_now,
    sync,
)
from tests.graph_fixtures import write_graph
from tests.snapshot_support import (
    CORE_IDS,
    DOCS_IDS,
    PAYMENTS_IDS,
    accept,
    legacy_graph,
    members,
    standard_domains,
    touch_a_record,
)

EXPECTED = {"payments": PAYMENTS_IDS, "core": CORE_IDS, "docs": DOCS_IDS}


def _legacy(tmp_path):
    return GraphifyReader(write_graph(tmp_path, "legacy.json", legacy_graph()))


def test_sync_indexes_each_domain_s_members_by_its_rules(tmp_path):
    """Red against unfixed sync: no rows (empty sets)."""
    store = Store(tmp_path / ".sidegraph")
    standard_domains(store)
    sync(store, _legacy(tmp_path))
    assert members(store) == EXPECTED


def test_every_node_of_a_listed_community_is_a_member(tmp_path):
    """Guard: the invariant every community lookup rests on (red against a rows pass that
    leaves out path-less nodes: stub_basemodel sits in payments' community)."""
    store = Store(tmp_path / ".sidegraph")
    standard_domains(store)
    reader = _legacy(tmp_path)
    sync(store, reader)
    anchorable = [n for n in reader.list_nodes() if n.file_type in ANCHORABLE_FILE_TYPES]
    for d in store.iter_domains(status=DomainStatus.ACCEPTED):
        held = store.domain_member_ids(d.domain_id)
        assert d.communities
        for n in anchorable:
            if n.community in d.communities:
                assert n.node_id in held, (d.slug, n.node_id)


def lib_graph() -> dict:
    """Five communities, one file under lib/ each: a path rule on lib/ claims 5 of 5."""
    nodes = [
        {
            "id": f"lib{i}",
            "label": f"mod{i}.py",
            "norm_label": f"mod{i}.py",
            "file_type": "code",
            "source_file": f"lib/mod{i}.py",
            "community": i,
        }
        for i in range(5)
    ]
    return {"graph": {}, "nodes": nodes, "links": []}


def test_a_capped_path_rule_still_indexes_the_nodes_under_its_prefix(tmp_path):
    """Red against unfixed sync (no rows). The cap keeps the claim off the communities, as
    before; the nodes under the prefix were already members at read time."""
    store = Store(tmp_path / ".sidegraph")
    lib = accept(store, "lib", prefixes=["lib"])
    report = sync(store, GraphifyReader(write_graph(tmp_path, "lib.json", lib_graph())))
    assert [o["slug"] for o in report.overbroad_domains] == ["lib"]
    assert store.get_domain(lib.domain_id).communities == []
    assert store.domain_member_ids(lib.domain_id) == {f"lib{i}" for i in range(5)}


def test_domains_refreshed_counts_member_changes_once_the_index_is_populated(tmp_path):
    """Red against counting rows only for a domain that already had some: lib's members come
    back on the third pass, which readers see as a change, and it would count 0. The capped
    rule keeps lib's communities at [] throughout, so only its rows move. The first population,
    and the refill after a reload, are not changes."""
    root = tmp_path / ".sidegraph"
    lib_path = write_graph(tmp_path, "lib.json", lib_graph())
    moved = lib_graph()
    for node in moved["nodes"]:
        node["source_file"] = node["source_file"].replace("lib/", "other/")
    moved_path = write_graph(tmp_path, "moved.json", moved)
    with Store(root) as store:
        accept(store, "lib", prefixes=["lib"])
        assert sync(store, GraphifyReader(lib_path)).domains_refreshed == 0  # populating
        assert sync(store, GraphifyReader(moved_path)).domains_refreshed == 1  # members -> none
        assert sync(store, GraphifyReader(lib_path)).domains_refreshed == 1  # none -> members
    touch_a_record(root)  # a reload drops the rows and the population marker
    with Store(root) as store:
        assert sync(store, GraphifyReader(lib_path)).domains_refreshed == 0


def test_a_failed_first_population_leaves_the_index_unpopulated(tmp_path, monkeypatch):
    """Red against a marker set whatever failed: lib's first rows never got written, and the
    pass that finally writes them would count lib as refreshed."""
    store = Store(tmp_path / ".sidegraph")
    accept(store, "lib", prefixes=["lib"])
    reader = GraphifyReader(write_graph(tmp_path, "lib.json", lib_graph()))
    real = store.replace_domain_members

    def boom(domain_id, rows):
        raise ValueError("disk full")

    monkeypatch.setattr(store, "replace_domain_members", boom)
    assert [f["slug"] for f in sync(store, reader).domain_failures] == ["lib"]
    assert store.get_meta(MEMBERSHIP_POPULATED_KEY) is None
    monkeypatch.setattr(store, "replace_domain_members", real)
    assert sync(store, reader, force=True).domains_refreshed == 0
    assert store.get_meta(MEMBERSHIP_POPULATED_KEY) == "1"


def test_ratify_time_activation_indexes_the_new_domain(tmp_path):
    """Red against unfixed activation: communities only, no rows."""
    store = Store(tmp_path / ".sidegraph")
    reader = _legacy(tmp_path)
    sync(store, reader)
    core = accept(store, "core", seeds=[("helper", "src/core/util.py")])
    assert activate_accepted_domain(core, store, reader).resolved
    assert store.domain_member_ids(core.domain_id) == CORE_IDS


def test_ensure_rebuilds_an_index_a_reload_emptied_and_then_holds(tmp_path):
    """Red against no rebuild: capture and drill-down would read an empty index after any pull."""
    root = tmp_path / ".sidegraph"
    reader = _legacy(tmp_path)
    with Store(root) as store:
        standard_domains(store)
        sync(store, reader)
    touch_a_record(root)
    with Store(root) as store:
        assert members(store)["payments"] == set()
        assert ensure_domain_membership(store, reader) is not None
        assert members(store) == EXPECTED
        assert ensure_domain_membership(store, reader) is None


def test_a_skipped_sync_rebuilds_an_index_whose_stamp_is_stale(tmp_path):
    """Red against a skip that trusts the graph stamp alone."""
    store = Store(tmp_path / ".sidegraph")
    domains = standard_domains(store)
    reader = _legacy(tmp_path)
    sync(store, reader)
    store.replace_domain_members(domains["payments"].domain_id, [])
    store.set_meta(MEMBERSHIP_STAMP_KEY, "stale")
    store.set_meta(VOLATILE_STALE_KEY, "0")
    assert sync(store, reader).skipped
    assert members(store) == EXPECTED


def _poison_resolve(monkeypatch):
    """A seed anchor that raises whenever it resolves, in every reader: a domain refresh that
    fails until its cause is fixed."""
    real = GraphifyReader.resolve

    def resolve(self, desc):
        if desc.file_path == "src/poison.py":
            raise RuntimeError("malformed descriptor")
        return real(self, desc)

    monkeypatch.setattr(GraphifyReader, "resolve", resolve)


def _broken(store):
    """The standard domains plus one whose seed raises once _poison_resolve is installed."""
    standard_domains(store)
    return accept(store, "broken", seeds=[("nope", "src/poison.py")])


def _spy_refresh(monkeypatch) -> list[int]:
    calls: list[int] = []
    real = sync_mod._refresh_domains

    def spy(store_, reader_, **kw):
        calls.append(1)
        return real(store_, reader_, **kw)

    monkeypatch.setattr(sync_mod, "_refresh_domains", spy)
    return calls


def test_a_failed_domain_refresh_is_reported_by_every_later_sync(tmp_path, monkeypatch):
    """Red against a pass that certifies the index whatever failed: the second sync would skip,
    and the failure would go quiet while nothing healed."""
    store = Store(tmp_path / ".sidegraph")
    _broken(store)
    _poison_resolve(monkeypatch)
    reader = _legacy(tmp_path)
    for _ in range(2):
        report = sync(store, reader)
        assert not report.skipped
        assert [f["slug"] for f in report.domain_failures] == ["broken"]
        assert "malformed descriptor" in report.domain_failures[0]["error"]
    assert store.get_meta(MEMBERSHIP_STAMP_KEY) is None
    assert {slug: members(store)[slug] for slug in EXPECTED} == EXPECTED


def test_a_failed_forced_refresh_drops_the_certification_it_found(tmp_path, monkeypatch):
    """Red against a failed pass that only skips writing a stamp: the stamp the first, clean
    pass wrote for the same graph would stay, and the ordinary sync after it would skip."""
    store = Store(tmp_path / ".sidegraph")
    _broken(store)
    reader = _legacy(tmp_path)
    assert sync(store, reader).domain_failures == []  # nope() is simply unresolved here
    assert store.get_meta(MEMBERSHIP_STAMP_KEY) is not None
    _poison_resolve(monkeypatch)
    assert [f["slug"] for f in sync(store, reader, force=True).domain_failures] == ["broken"]
    assert store.get_meta(MEMBERSHIP_STAMP_KEY) is None
    report = sync(store, reader)
    assert not report.skipped and [f["slug"] for f in report.domain_failures] == ["broken"]


def test_ensure_reports_a_recorded_failure_without_recomputing(tmp_path, monkeypatch):
    """Red against an ensure that recomputes a state it already saw fail: capture calls it for
    every anchor, so an importer would recompute every domain once per anchor. A status change
    (here, the broken domain's drop) is a new state, and it recomputes."""
    store = Store(tmp_path / ".sidegraph")
    broken = _broken(store)
    _poison_resolve(monkeypatch)
    reader = _legacy(tmp_path)
    sync(store, reader)
    calls = _spy_refresh(monkeypatch)
    for _ in range(3):
        again = ensure_domain_membership(store, reader)
        assert again is not None and not again.recomputed
        assert [f["slug"] for f in again.failures] == ["broken"]
    assert calls == []
    store.ratify_domains(drop=[broken.domain_id])
    healed = ensure_domain_membership(store, reader)
    assert calls == [1] and healed is not None and healed.failures == []
    assert ensure_domain_membership(store, reader) is None


def test_a_failed_member_write_leaves_that_domain_as_it_was(tmp_path, monkeypatch):
    """Red against a write in two committing steps: core's communities would move to the
    recomputed claim while its rows stayed behind. The other domains still publish."""
    store = Store(tmp_path / ".sidegraph")
    domains = standard_domains(store)
    reader = _legacy(tmp_path)
    sync(store, reader)
    core_id = domains["core"].domain_id
    store.refresh_domain_communities(core_id, ["9"])
    store.replace_domain_members(domains["payments"].domain_id, [])
    rows_before = store.domain_member_ids(core_id)
    real = store.replace_domain_members

    def boom(domain_id, rows):
        if domain_id == core_id:
            raise ValueError("disk full")
        return real(domain_id, rows)

    monkeypatch.setattr(store, "replace_domain_members", boom)
    report = sync(store, reader, force=True)
    assert [f["slug"] for f in report.domain_failures] == ["core"]
    assert store.get_domain(core_id).communities == ["9"]
    assert store.domain_member_ids(core_id) == rows_before
    assert store.domain_member_ids(domains["payments"].domain_id) == PAYMENTS_IDS


def test_a_pass_compares_with_the_domain_as_stored(tmp_path):
    """Red against a write that compares with a Domain read earlier: after a sync on another
    graph moved core to community 5, an activation handed the older object (community 2)
    would skip the community write and leave communities ["5"] beside rows of community 2."""
    store = Store(tmp_path / ".sidegraph")
    domains = standard_domains(store)
    legacy = _legacy(tmp_path)
    moved = legacy_graph()
    for node in moved["nodes"]:
        if node["id"] in CORE_IDS:
            node["community"] = 5
    sync(store, legacy)
    stale = store.get_domain(domains["core"].domain_id)
    sync(store, GraphifyReader(write_graph(tmp_path, "moved.json", moved)))
    assert store.get_domain(stale.domain_id).communities == ["5"]
    refresh_domain_communities_now(stale, store, legacy)
    assert store.get_domain(stale.domain_id).communities == ["2"]
    assert store.member_communities([stale.domain_id]) == {"2"}
    assert store.get_meta(MEMBERSHIP_STAMP_KEY) is None  # certified for the moved graph


def test_a_failed_activation_inside_a_caller_s_reservation_leaves_nothing_half_written(
    tmp_path, monkeypatch
):
    """Red against an activation without its own savepoint: inside a caller's reservation the
    caught failure does not roll back, and core's communities would move to ["5"] while its rows
    stayed in community 2."""
    store = Store(tmp_path / ".sidegraph")
    domains = standard_domains(store)
    sync(store, _legacy(tmp_path))
    moved = legacy_graph()
    for node in moved["nodes"]:
        if node["id"] in CORE_IDS:
            node["community"] = 5
    reader = GraphifyReader(write_graph(tmp_path, "moved.json", moved))
    core = store.get_domain(domains["core"].domain_id)
    stamp = store.get_meta(MEMBERSHIP_STAMP_KEY)
    real = store.replace_domain_members

    def boom(domain_id, rows):
        if domain_id == core.domain_id:
            raise ValueError("disk full")
        return real(domain_id, rows)

    monkeypatch.setattr(store, "replace_domain_members", boom)
    with store._mutation(), contextlib.suppress(ValueError):
        refresh_domain_communities_now(core, store, reader)
    assert store.get_domain(core.domain_id).communities == ["2"]
    assert store.member_communities([core.domain_id]) == {"2"}
    assert store.get_meta(MEMBERSHIP_STAMP_KEY) == stamp


def _hold(store: Store, mode: str) -> sqlite3.Connection:
    """Another connection holding the index's writer lock: ``IMMEDIATE`` (reserved; reads go
    on) or ``EXCLUSIVE`` (reads wait too)."""
    holder = sqlite3.connect(str(store.path / "index.db"), timeout=0, isolation_level=None)
    holder.execute(f"BEGIN {mode}")
    return holder


def _stale_index(tmp_path):
    """A synced store whose membership is no longer certified, with a short busy timeout."""
    store = Store(tmp_path / ".sidegraph")
    standard_domains(store)
    reader = _legacy(tmp_path)
    sync(store, reader)
    store.set_meta(MEMBERSHIP_STAMP_KEY, "stale")
    return store, reader


BUSY = [
    {
        "slug": "*",
        "title": "every accepted domain",
        "error": "StoreBusyError: store writer reservation is busy",
        "busy": True,
    }
]


def test_a_pass_behind_an_exclusive_writer_rolls_back_within_one_timeout(tmp_path):
    """Red against a rolled-back pass that lists the accepted domains to report them: the
    listing waits on the same lock and raises out of the pass."""
    store, reader = _stale_index(tmp_path)
    store._conn.execute("PRAGMA busy_timeout = 50")
    holder = _hold(store, "EXCLUSIVE")
    try:
        started = time.monotonic()
        out = sync_mod._refresh_domains(store, reader)
        assert time.monotonic() - started < 0.5
    finally:
        holder.rollback()
        holder.close()
    assert out is not None and out.rolled_back and not out.recomputed
    assert out.failures == BUSY and out.failed_ids == []


def test_a_sync_behind_a_held_writer_reports_the_busy_pass(tmp_path):
    """Red against a rolled-back pass reported domain by domain, or followed by a TOC rebuild
    that waits on the same writer."""
    store, reader = _stale_index(tmp_path)
    store._conn.execute("PRAGMA busy_timeout = 50")
    holder = _hold(store, "IMMEDIATE")
    try:
        started = time.monotonic()
        report = sync(store, reader)
        assert time.monotonic() - started < 0.5
    finally:
        holder.rollback()
        holder.close()
    assert not report.skipped and report.domain_failures == BUSY
    assert store.get_meta(MEMBERSHIP_STAMP_KEY) == "stale"  # nothing recorded
    assert store.get_meta(MEMBERSHIP_FAILED_KEY) is None
    assert sync(store, reader).domain_failures == []  # the next pass runs


def test_a_busy_pass_leaves_the_toc_to_the_next_pass(tmp_path, monkeypatch):
    """Red against a skipped sync that rebuilds a stale TOC after a rolled-back pass."""
    store, reader = _stale_index(tmp_path)
    store.delete_meta(TOC_CACHE_KEY)
    toc: list[int] = []
    monkeypatch.setattr(sync_mod, "_rebuild_toc_cache", lambda *a, **k: toc.append(1))

    @contextlib.contextmanager
    def busy(self, **kw):
        raise StoreBusyError("store writer reservation is busy")
        yield

    monkeypatch.setattr(Store, "_mutation", busy)
    report = sync(store, reader)
    assert report.domain_failures == BUSY and toc == []


@pytest.mark.parametrize("force", [False, True])
def test_a_sync_whose_pass_meets_an_exclusive_writer_reports_without_reading_more(
    tmp_path, monkeypatch, force
):
    """Red against a rolled-back report that still reads stale decisions and slug conflicts:
    the writer takes its exclusive lock after ensure's fast path, those reads wait out the
    timeout behind it, and the busy report is lost to an OperationalError."""
    store, reader = _stale_index(tmp_path)
    store._conn.execute("PRAGMA busy_timeout = 50")
    holders: list[sqlite3.Connection] = []
    real = sync_mod._refresh_domains

    def after_a_writer_takes_the_lock(store_, reader_, **kw):
        holders.append(_hold(store_, "EXCLUSIVE"))
        return real(store_, reader_, **kw)

    monkeypatch.setattr(sync_mod, "_refresh_domains", after_a_writer_takes_the_lock)
    try:
        started = time.monotonic()
        report = sync(store, reader, force=force)
        assert time.monotonic() - started < 0.5
    finally:
        for holder in holders:
            holder.rollback()
            holder.close()
    assert report.domain_failures == BUSY
    assert report.stale_decisions == [] and report.slug_conflicts == []  # not read


def test_a_full_sync_stops_after_a_nonbusy_whole_pass_failure(tmp_path, monkeypatch):
    """The full pass must return its held report before any later store read or stamp write."""
    store, reader = _stale_index(tmp_path)

    def unexpected(*args, **kwargs):
        raise AssertionError("sync touched the store after its membership pass rolled back")

    def failed(*args, **kwargs):
        monkeypatch.setattr(sync_mod, "_stale_decisions", unexpected)
        monkeypatch.setattr(sync_mod, "_rebuild_toc_cache", unexpected)
        monkeypatch.setattr(store, "domain_slug_conflicts", unexpected)
        monkeypatch.setattr(store, "set_meta", unexpected)
        return MembershipRefresh.rolled_back_by(RuntimeError("view broken"))

    monkeypatch.setattr(sync_mod, "_refresh_domains", failed)
    report = sync(store, reader, force=True)
    assert report.domain_failures[0]["slug"] == "*"
    assert report.domain_failures[0]["busy"] is False
    assert report.stale_decisions == [] and report.slug_conflicts == []


def test_a_whole_pass_failure_is_recorded_once_and_trusted_nowhere(tmp_path, monkeypatch):
    """Red against a whole-pass failure that rolls back unrecorded: every capture anchor and
    every lazy sync would recompute a pass that fails the same way each time."""
    store, reader = _stale_index(tmp_path)
    calls: list[int] = []

    def broken(*a, **kw):
        calls.append(1)
        raise RuntimeError("view broken")

    monkeypatch.setattr(sync_mod, "_apply", broken)
    for _ in range(3):
        out = ensure_domain_membership(store, reader)
        assert out is not None and [f["slug"] for f in out.failures] == ["*"]
        assert out.failures[0]["busy"] is False
    assert calls == [1]
    assert sync_mod.membership_exclusions(store, reader) is None  # capture binds no row
    assert store.get_meta(MEMBERSHIP_STAMP_KEY) == "stale"  # the certification is left alone


def test_full_sync_reuses_a_recorded_whole_pass_failure_until_forced(tmp_path, monkeypatch):
    """An early rollback leaves the graph stamp old; that must not force repeated computation."""
    store = Store(tmp_path / ".sidegraph")
    standard_domains(store)
    reader = _legacy(tmp_path)
    calls: list[int] = []
    monkeypatch.setattr(sync_mod, "_apply", _broken_apply(calls))
    for _ in range(3):
        report = sync(store, reader)
        assert [f["slug"] for f in report.domain_failures] == ["*"]
    assert calls == [1]
    assert [f["slug"] for f in sync(store, reader, force=True).domain_failures] == ["*"]
    assert calls == [1, 1]


def _broken_apply(calls: list[int]):
    def broken(*a, **kw):
        calls.append(1)
        raise RuntimeError("view broken")

    return broken


def test_a_whole_pass_failure_outranks_the_certification_it_found(tmp_path, monkeypatch):
    """Red against a certification checked before the whole-pass record: the forced pass leaves
    the earlier pass's certification for the same identity, so ensure would report nothing and
    capture would bind from rows the failing pass could not vouch for."""
    store = Store(tmp_path / ".sidegraph")
    standard_domains(store)
    reader = _legacy(tmp_path)
    sync(store, reader)
    certified = store.get_meta(MEMBERSHIP_STAMP_KEY)
    monkeypatch.setattr(sync_mod, "_apply", _broken_apply([]))
    assert [f["slug"] for f in sync(store, reader, force=True).domain_failures] == ["*"]
    assert store.get_meta(MEMBERSHIP_STAMP_KEY) == certified
    out = ensure_domain_membership(store, reader)
    assert out is not None and [f["slug"] for f in out.failures] == ["*"]
    assert sync_mod.membership_exclusions(store, reader) is None


def test_a_status_change_waits_for_a_failure_s_record(tmp_path, monkeypatch):
    """Red against a record written after the failed pass released its reservation: the drop
    lands first and uncertifies, and the record written after it makes the next ensure report
    a failure of the state the drop replaced instead of recomputing."""
    root = tmp_path / ".sidegraph"
    reader = _legacy(tmp_path)
    with Store(root) as setup:
        docs = standard_domains(setup)["docs"]
        sync(setup, reader)
        setup.set_meta(MEMBERSHIP_STAMP_KEY, "stale")
    store, other = Store(root), Store(root)
    calls: list[int] = []
    monkeypatch.setattr(sync_mod, "_apply", _broken_apply(calls))
    failed, release = threading.Event(), threading.Event()
    real_record = sync_mod._record_whole_failure

    def paused_record(*a, **kw):
        failed.set()
        release.wait(5)
        return real_record(*a, **kw)

    monkeypatch.setattr(sync_mod, "_record_whole_failure", paused_record)
    worker = threading.Thread(target=ensure_domain_membership, args=(store, reader))
    worker.start()
    assert failed.wait(5)
    drop = threading.Thread(target=other.ratify_domains, kwargs={"drop": [docs.domain_id]})
    drop.start()
    drop.join(0.5)
    waited = drop.is_alive()
    release.set()
    worker.join(10)
    drop.join(10)
    other.close()
    assert waited  # the drop waited for the record
    assert store.get_meta(MEMBERSHIP_FAILED_KEY) is None  # and then cleared it
    ensure_domain_membership(store, reader)
    assert calls == [1, 1]  # the new state is recomputed
    store.close()


def test_a_failure_that_ends_its_transaction_records_nothing(tmp_path, monkeypatch):
    """Red against a record written after the error ended the pass's transaction, fenced or
    not: another connection's drop lands in between and leaves the certification and the
    failure record as the pass read them (None, None), so no comparison can see it, and the
    record would make the next ensure report the replaced state without recomputing."""
    root = tmp_path / ".sidegraph"
    reader = _legacy(tmp_path)
    with Store(root) as setup:
        docs = standard_domains(setup)["docs"]
        sync(setup, reader)
        setup.delete_meta(MEMBERSHIP_STAMP_KEY)  # (certification, failure record): (None, None)
    store, other = Store(root), Store(root)
    calls: list[int] = []

    def transaction_lost(*a, **kw):
        calls.append(1)
        store._conn.execute("ROLLBACK")  # what SQLite does with some errors
        other.ratify_domains(drop=[docs.domain_id])  # free to land, and leaves (None, None)
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(sync_mod, "_apply", transaction_lost)
    out = ensure_domain_membership(store, reader)
    assert out is not None and [f["slug"] for f in out.failures] == ["*"]
    assert store.get_meta(MEMBERSHIP_FAILED_KEY) is None  # nothing recorded
    monkeypatch.setattr(sync_mod, "_apply", _broken_apply(calls))
    ensure_domain_membership(store, reader)
    assert calls == [1, 1]
    store.close()
    other.close()


def test_per_domain_transaction_loss_preserves_a_newer_pass(tmp_path, monkeypatch):
    """Real per-domain isolation must stop before settlement reacquires a lost transaction."""
    store, reader = _stale_index(tmp_path)
    other = Store(store.path)
    calls = 0
    real = sync_mod._MembershipView.membership

    def lose_transaction(self, domain):
        nonlocal calls
        calls += 1
        if calls == 2:
            store._conn.execute("ROLLBACK")
            newer = sync_mod._refresh_domains(other, reader)
            assert newer is not None and not newer.failures
            raise sqlite3.OperationalError("disk I/O error")
        return real(self, domain)

    monkeypatch.setattr(sync_mod._MembershipView, "membership", lose_transaction)
    try:
        out = ensure_domain_membership(store, reader)
        assert out is not None and out.rolled_back
        assert store.get_meta(MEMBERSHIP_STAMP_KEY) == sync_mod.membership_stamp(store, reader)
        assert store.get_meta(MEMBERSHIP_FAILED_KEY) is None
        assert members(store) == EXPECTED
    finally:
        store.close()
        other.close()


def test_a_nested_pass_cannot_hide_the_callers_lost_transaction(tmp_path, monkeypatch):
    """Loss must escape both pass handlers so the owning caller cannot commit only later work."""
    store, reader = _stale_index(tmp_path)

    def lose_transaction(*args, **kwargs):
        store._conn.execute("ROLLBACK")
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(sync_mod, "_apply", lose_transaction)
    with pytest.raises(sqlite3.OperationalError, match="disk I/O error"), store._mutation():
        store.set_meta("caller_before_loss", "1")
        ensure_domain_membership(store, reader)
        store.set_meta("caller_after_loss", "1")
    assert store.get_meta("caller_before_loss") is None
    assert store.get_meta("caller_after_loss") is None
    assert store.get_meta(MEMBERSHIP_FAILED_KEY) is None


def test_a_pass_inside_a_caller_s_reservation_keeps_the_large_cache_until_it_ends(tmp_path):
    """Red against a cache restored when the pass's own scope exits: the caller's transaction
    still holds the pass's uncommitted rows, and its later writes would spill on the 2 MB
    default. Restored after the caller's commit and after its rollback."""
    store, reader = _stale_index(tmp_path)

    def cache() -> int:
        return store._conn.execute("PRAGMA cache_size").fetchone()[0]

    before = cache()
    with store._mutation():
        assert ensure_domain_membership(store, reader) is not None  # a pass, nested
        assert cache() == -65536 and store._conn.in_transaction
    assert cache() == before
    store.set_meta(MEMBERSHIP_STAMP_KEY, "stale")
    with pytest.raises(RuntimeError), store._mutation():
        ensure_domain_membership(store, reader)
        raise RuntimeError("the caller fails")
    assert cache() == before


def test_a_pass_runs_with_a_large_cache_and_restores_it(tmp_path, monkeypatch):
    """Red against a pass on sqlite3's 2 MB default: a pass that writes every row spills, the
    spill takes the exclusive lock, and readers wait for the commit. The cache comes back after
    a pass, a failed pass and an activation alike."""
    store = Store(tmp_path / ".sidegraph")
    domains = standard_domains(store)
    reader = _legacy(tmp_path)
    before = store._conn.execute("PRAGMA cache_size").fetchone()[0]
    seen: list[int] = []
    real = sync_mod._MembershipView.membership

    def membership(self, domain):
        seen.append(store._conn.execute("PRAGMA cache_size").fetchone()[0])
        return real(self, domain)

    monkeypatch.setattr(sync_mod._MembershipView, "membership", membership)
    sync(store, reader)
    refresh_domain_communities_now(domains["core"], store, reader)
    assert seen and set(seen) == {-65536}
    monkeypatch.setattr(sync_mod, "_settle", lambda *a, **k: 1 / 0)
    assert sync_mod._refresh_domains(store, reader).rolled_back
    assert store._conn.execute("PRAGMA cache_size").fetchone()[0] == before


def test_a_busy_pass_is_reported_as_busy_and_is_not_a_finding(tmp_path, monkeypatch, capsys):
    """Red against a busy pass printed under "FAILED (fix, then re-run with --force)" and
    counted by --check: nothing failed, another writer held the store, and the next sync runs
    the pass. A whole-pass failure that is not busy stays a finding."""
    root = tmp_path / ".sidegraph"
    graph = write_graph(tmp_path, "legacy.json", legacy_graph())
    Store(root).close()
    argv = ["--db", str(root), "--graph", str(graph), "--check"]

    def report_of(exc):
        fields = MembershipRefresh.rolled_back_by(exc).report_fields()
        return SyncReport(from_version="v", to_version="v", **fields)

    monkeypatch.setattr(
        "sidegraph.cli.sync",
        lambda *a, **k: report_of(StoreBusyError("store writer reservation is busy")),
    )
    assert sync_main(argv) == 0
    out = capsys.readouterr().out
    assert "the store's writer was busy" in out and "FAILED" not in out
    monkeypatch.setattr("sidegraph.cli.sync", lambda *a, **k: report_of(RuntimeError("broken")))
    assert sync_main(argv) == 2
    assert "FAILED" in capsys.readouterr().out


def test_concurrent_ensures_compute_one_pass(tmp_path, monkeypatch):
    """Red against an ensure that decides only before it takes the reservation: the second
    caller waits for the first one's pass and then computes the same identity again."""
    root = tmp_path / ".sidegraph"
    reader = _legacy(tmp_path)
    with Store(root) as store:
        standard_domains(store)
    first, second = Store(root), Store(root)
    applied: list[int] = []
    computing, release, second_in = threading.Event(), threading.Event(), threading.Event()
    real_apply, real_refresh = sync_mod._apply, sync_mod._refresh_domains

    def slow_apply(*a, **kw):
        applied.append(1)
        computing.set()
        release.wait(5)
        return real_apply(*a, **kw)

    def refresh(store_, reader_, **kw):
        if store_ is second:
            second_in.set()  # past its fast path: the index was not certified
        return real_refresh(store_, reader_, **kw)

    monkeypatch.setattr(sync_mod, "_apply", slow_apply)
    monkeypatch.setattr(sync_mod, "_refresh_domains", refresh)
    results: dict[str, object] = {}
    one = threading.Thread(target=lambda: results.update(a=ensure_domain_membership(first, reader)))
    two = threading.Thread(
        target=lambda: results.update(b=ensure_domain_membership(second, reader))
    )
    one.start()
    assert computing.wait(5)
    two.start()
    assert second_in.wait(5)
    time.sleep(0.1)  # the second is waiting for the reservation
    release.set()
    one.join(10)
    two.join(10)
    first.close()
    second.close()
    assert applied == [1]
    assert results["a"] is not None and results["b"] is None


def test_check_fails_on_every_run_while_a_domain_refresh_fails(tmp_path, monkeypatch, capsys):
    """Guard (green before this change too: --check forces a full pass on every run)."""
    root = tmp_path / ".sidegraph"
    graph = write_graph(tmp_path, "legacy.json", legacy_graph())
    with Store(root) as store:
        _broken(store)
    _poison_resolve(monkeypatch)
    argv = ["--db", str(root), "--graph", str(graph), "--json", "--check"]
    for _ in range(2):
        assert sync_main(argv) == 2
        out = json.loads(capsys.readouterr().out)
        assert [f["slug"] for f in out["domain_failures"]] == ["broken"]
