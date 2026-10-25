"""With a snapshot, sync gives each node its recorded domains (D5.2) and every consumer reads
the result from the membership index.

see design/superpowers/specs/2026-10-14-graphify-upgrade-design.md (D5)"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime

import sidegraph.server as server_module
from sidegraph import domain_snapshot
from sidegraph import sync as sync_mod
from sidegraph.anchoring import resolve_and_bind
from sidegraph.cli import ratify_main, sync_main
from sidegraph.domains import _claimed_communities
from sidegraph.engine.reader import GraphifyReader
from sidegraph.schema import Decision, DecisionKind, Descriptor, Domain, Provenance
from sidegraph.server import _drill_down_impl, _ratify_impl
from sidegraph.store import MEMBERSHIP_STAMP_KEY, VOLATILE_STALE_KEY, Store
from sidegraph.sync import (
    activate_accepted_domain,
    ensure_domain_membership,
    report_as_dict,
    sync,
    take_snapshot,
)
from tests.graph_fixtures import write_graph
from tests.snapshot_support import (
    CORE_IDS,
    DOCS_IDS,
    EXTRA,
    HELPERS,
    LONELY_IDS,
    NEW_IDS,
    PAYMENTS_IDS,
    STUB,
    TOOL,
    accept,
    call_tool,
    legacy_graph,
    members,
    mention_graph,
    nodes_of,
    seed_members,
    standard_domains,
    tier1_names,
)
from tests.test_sync_membership import lib_graph


def _snapshotted(tmp_path, mention_data=None):
    store = Store(tmp_path / ".sidegraph")
    domains = standard_domains(store)
    legacy = GraphifyReader(write_graph(tmp_path, "legacy.json", legacy_graph()))
    mention = GraphifyReader(write_graph(tmp_path, "mention.json", mention_data or mention_graph()))
    sync(store, legacy)
    domain_snapshot.write_snapshot(store.path, take_snapshot(store, legacy, refresh=False))
    return store, domains, legacy, mention


def _with(*rows_and_communities):
    data = mention_graph()
    for row, community in rows_and_communities:
        data["nodes"] += nodes_of((row,), community)
    return data


def _tier1(store, reader, name, path):
    d = store.add_decision(
        Decision(
            title="t",
            kind=DecisionKind.ADR,
            context="c",
            choice="ch",
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual"),
        )
    )
    resolve_and_bind(d.id, Descriptor(name=name, file_path=path), reader, store)
    return tier1_names(store, d.id)


def test_snapshot_sets_hold_after_communities_merge(tmp_path):
    """Red against unfixed code (rules on the merged graph): payments absorbs lonely.py and
    core's .extra()."""
    store, _d, _l, mention = _snapshotted(tmp_path)
    sync(store, mention)
    got = members(store)
    assert got["payments"] == PAYMENTS_IDS and got["docs"] == DOCS_IDS


def test_a_new_symbol_in_a_listed_file_takes_the_file_default(tmp_path):
    """Red against unfixed code: .extra() sits in community 1, which payments claims."""
    store, domains, _l, mention = _snapshotted(tmp_path)
    sync(store, mention)
    assert EXTRA[0] in store.domain_member_ids(domains["core"].domain_id)
    assert EXTRA[0] not in store.domain_member_ids(domains["payments"].domain_id)


def test_a_domainless_node_stays_domainless_after_its_community_merges(tmp_path):
    """Red against unfixed code: lonely.py joins payments with community 1."""
    store, _d, _l, mention = _snapshotted(tmp_path)
    sync(store, mention)
    assert all(LONELY_IDS.isdisjoint(ids) for ids in members(store).values())


def test_a_file_new_since_the_snapshot_takes_its_community_s_majority(tmp_path):
    """Guard (core claims community 2 under the rules too); the vote is pinned in
    test_domain_snapshot_format."""
    store, domains, _l, mention = _snapshotted(tmp_path)
    sync(store, mention)
    assert store.domain_member_ids(domains["core"].domain_id) >= NEW_IDS


def test_a_new_file_under_a_prefix_with_no_voters_is_in_that_domain(tmp_path):
    """Red against the vote-only rule (discarded, declared): tool.py's community 9 has no
    listed node to vote."""
    store, domains, _l, mention = _snapshotted(tmp_path, _with((TOOL, 9)))
    sync(store, mention)
    assert TOOL[0] in store.domain_member_ids(domains["payments"].domain_id)


def test_a_domain_accepted_after_the_snapshot_gets_rule_members_on_new_files_only(tmp_path):
    """Red against full rules for a domain the snapshot does not cover: misc's seed sits in
    merged community 1, so misc would take every listed node of it, and as the newest domain it
    would win capture's Tier-1 pick on Invoice."""
    store, _d, _l, mention = _snapshotted(tmp_path, _with((HELPERS, 1)))
    misc = accept(store, "misc", seeds=[("lonely", "src/misc/lonely.py")])
    sync(store, mention)
    assert store.domain_member_ids(misc.domain_id) == {HELPERS[0]}
    assert _tier1(store, mention, "Invoice", "src/pay/model.py") == ["domain:payments"]


def test_a_path_prefix_adds_a_new_file_but_never_a_node_of_a_listed_file(tmp_path):
    """Red against the rules: a payments rule widened to src/ sweeps in util.py, which the
    snapshot gave to core. new.py, a file the snapshot does not list, joins by the prefix."""
    store, domains, _l, mention = _snapshotted(tmp_path)
    accept(
        store,
        "payments",
        seeds=[("Invoice", "src/pay/model.py")],
        prefixes=["src"],
        supersedes=domains["payments"],
    )
    sync(store, mention)
    held = members(store)["payments"]
    assert held >= NEW_IDS
    assert held.isdisjoint(CORE_IDS | {EXTRA[0]} | LONELY_IDS)


def test_a_renamed_domain_carries_its_snapshot_entries(tmp_path):
    """Red against unfixed code: billing has no rule, so it would have no member."""
    store, domains, _l, mention = _snapshotted(tmp_path)
    accept(store, "billing", supersedes=domains["payments"])
    sync(store, mention)
    assert members(store)["billing"] == PAYMENTS_IDS


def test_a_new_domain_reusing_a_dropped_snapshot_slug_does_not_inherit(tmp_path):
    """Red against slug-keyed heirs: the new, unrelated payments would take the old entries."""
    store, domains, _l, mention = _snapshotted(tmp_path)
    store.ratify_domains(drop=[domains["payments"].domain_id])
    fresh = accept(store, "payments")
    sync(store, mention)
    assert store.domain_member_ids(fresh.domain_id) == set()


def test_a_split_holds_nothing_listed_and_is_reported(tmp_path, capsys):
    """Red against an heir rule that hands the slug to the first successor, and against a
    split that only the human ratify answers mention."""
    store, domains, _l, mention = _snapshotted(tmp_path)
    accept(store, "billing-a", supersedes=domains["payments"])
    accept(store, "billing-b", supersedes=domains["payments"])
    report = sync(store, mention)
    got = members(store)
    assert got["billing-a"] == set() and got["billing-b"] == set()
    expected = [{"slug": "payments", "domains": ["billing-a", "billing-b"]}]
    assert report.snapshot_splits == expected
    assert report_as_dict(report)["snapshot_splits"] == expected
    store.close()
    sync_main(
        ["--db", str(tmp_path / ".sidegraph"), "--graph", str(tmp_path / "mention.json"), "--force"]
    )
    assert "sidegraph-domains snapshot --refresh --domain billing-a --domain billing-b" in (
        capsys.readouterr().out
    )


def test_reading_the_snapshot_over_its_own_graph_changes_no_membership(tmp_path):
    """The round-trip property, end to end, for every node, path-less ones included (guard;
    the lossless rule itself is pinned in test_domain_snapshot_format)."""
    store = Store(tmp_path / ".sidegraph")
    standard_domains(store)
    legacy = GraphifyReader(write_graph(tmp_path, "legacy.json", legacy_graph()))
    sync(store, legacy)
    before = members(store)
    domain_snapshot.write_snapshot(store.path, take_snapshot(store, legacy, refresh=False))
    sync(store, legacy, force=True)
    assert members(store) == before


def test_a_stub_outside_a_capped_path_rule_stays_out_after_the_round_trip(tmp_path):
    """Red against a snapshot without path-less entries: lib's prefix rule is capped (5 of 5
    communities), so the stub in community 0 is in no domain; read back, it would vote with
    lib0, community 0's only listed node, into lib."""
    store = Store(tmp_path / ".sidegraph")
    data = lib_graph()
    data["nodes"] += nodes_of((STUB,), 0)
    reader = GraphifyReader(write_graph(tmp_path, "lib.json", data))
    lib = accept(store, "lib", prefixes=["lib"])
    sync(store, reader)
    assert STUB[0] not in store.domain_member_ids(lib.domain_id)
    domain_snapshot.write_snapshot(store.path, take_snapshot(store, reader, refresh=False))
    sync(store, reader, force=True)
    assert STUB[0] not in store.domain_member_ids(lib.domain_id)


def test_a_split_and_its_end_reach_the_next_capture_without_a_sync(tmp_path):
    """Red against status changes that leave the membership index certified: after billing-b's
    accept, capture would read billing-a's sole-heir rows and bind domain:billing-a; drill-down
    would list Invoice under it. No sync runs after the first."""
    store, domains, _l, mention = _snapshotted(tmp_path)
    sync(store, mention)
    payments = domains["payments"]
    accept(store, "billing-a", supersedes=payments)
    assert _tier1(store, mention, "Invoice", "src/pay/model.py") == ["domain:billing-a"]
    b = store.supersede_domain(
        payments.domain_id,
        Domain(
            slug="billing-b",
            title="Billing B",
            summary="b",
            supersedes=payments.domain_id,
            provenance=Provenance(source="manual"),
        ),
    )
    assert _ratify_impl(store, accept=[b.domain_id], reader=mention)[b.domain_id].startswith(
        "accepted"
    )
    assert members(store)["billing-a"] == set()  # rebuilt by ratify, before its TOC
    assert _tier1(store, mention, "Invoice", "src/pay/model.py") == ["community:1"]
    out = _drill_down_impl(store, mention, "billing-a")
    assert not any("Invoice" in m for m in out["members"])
    assert _ratify_impl(store, drop=[b.domain_id], reader=mention)[b.domain_id] == "dropped"
    assert members(store)["billing-a"] == PAYMENTS_IDS
    assert _tier1(store, mention, "Invoice", "src/pay/model.py") == ["domain:billing-a"]


def test_the_cli_ratify_rebuilds_the_index_after_a_drop_ends_a_split(tmp_path):
    """Red against a CLI ratify that opens no reader for a drop: billing-a's frozen rows stay
    empty until the next sync or capture, and the TOC it caches misses them."""
    store, domains, _l, mention = _snapshotted(tmp_path)
    accept(store, "billing-a", supersedes=domains["payments"])
    b = accept(store, "billing-b", supersedes=domains["payments"])
    sync(store, mention)
    assert members(store)["billing-a"] == set()
    store.close()
    assert (
        ratify_main(
            [
                "--db",
                str(tmp_path / ".sidegraph"),
                "--graph",
                str(tmp_path / "mention.json"),
                "--drop",
                b.domain_id,
            ]
        )
        == 0
    )
    with Store(tmp_path / ".sidegraph") as again:
        assert members(again)["billing-a"] == PAYMENTS_IDS


def test_a_status_change_waits_for_a_running_membership_pass(tmp_path, monkeypatch):
    """Red against a pass that computes outside its writer reservation: billing-b's accept,
    from another connection, would commit while the pass computes billing-a's sole-heir rows,
    and the pass would then publish them. Serialized, the accept waits for the pass and then
    uncertifies what it certified."""
    store, domains, _l, mention = _snapshotted(tmp_path)
    payments = domains["payments"]
    accept(store, "billing-a", supersedes=payments)
    b = store.supersede_domain(
        payments.domain_id,
        Domain(
            slug="billing-b",
            title="Billing B",
            summary="b",
            supersedes=payments.domain_id,
            provenance=Provenance(source="manual"),
        ),
    )
    sync(store, mention)
    store.delete_meta(MEMBERSHIP_STAMP_KEY)  # the next ensure recomputes
    other = Store(store.path)  # a second writer: another process, as far as SQLite knows
    entered, release = threading.Event(), threading.Event()
    real = sync_mod._MembershipView.membership

    def membership(self, domain):
        if not entered.is_set():
            entered.set()
            release.wait(10)
        return real(self, domain)

    monkeypatch.setattr(sync_mod._MembershipView, "membership", membership)
    running = threading.Thread(target=ensure_domain_membership, args=(store, mention))
    running.start()
    assert entered.wait(10)
    attempting = threading.Event()

    def accept_b():
        attempting.set()
        other.ratify_domains(accept=[b.domain_id])

    ratify = threading.Thread(target=accept_b)
    ratify.start()
    assert attempting.wait(10)
    ratify.join(0.25)  # an unblocked accept commits in milliseconds
    assert ratify.is_alive()  # blocked on the pass's reservation
    release.set()
    running.join(10)
    ratify.join(10)
    other.close()
    assert members(store)["billing-a"] == PAYMENTS_IDS  # computed before the accept
    assert store.get_meta(MEMBERSHIP_STAMP_KEY) is None  # and uncertified by it
    assert ensure_domain_membership(store, mention) is not None
    assert members(store)["billing-a"] == set()  # the split


def test_a_pull_during_a_pass_is_never_read_as_current(tmp_path, monkeypatch):
    """Red against a capture that trusts rows certified for other bytes than the snapshot on
    disk: the pull lands while the pass computes the rule-era rows, and lonely() would bind
    payments, which the pulled snapshot does not give it."""
    store = Store(tmp_path / ".sidegraph")
    standard_domains(store)
    legacy = GraphifyReader(write_graph(tmp_path, "legacy.json", legacy_graph()))
    mention = GraphifyReader(write_graph(tmp_path, "mention.json", mention_graph()))
    sync(store, legacy)
    pulled = take_snapshot(store, legacy, refresh=False)
    sync(store, mention)
    store.delete_meta(MEMBERSHIP_STAMP_KEY)
    real = sync_mod._MembershipView.membership
    pulls: list[int] = []

    def membership(self, domain):
        if not pulls:  # the pull: git writes the file, it takes no reservation
            pulls.append(domain_snapshot.write_snapshot(store.path, pulled))
        return real(self, domain)

    monkeypatch.setattr(sync_mod._MembershipView, "membership", membership)
    assert _tier1(store, mention, "lonely", "src/misc/lonely.py") == ["community:1"]
    assert store.get_meta(MEMBERSHIP_STAMP_KEY) != sync_mod.membership_stamp(store, mention)
    assert _tier1(store, mention, "Invoice", "src/pay/model.py") == ["domain:payments"]
    assert store.get_meta(MEMBERSHIP_STAMP_KEY) == sync_mod.membership_stamp(store, mention)


def test_an_invalid_snapshot_is_recomputed_once_for_many_anchors(tmp_path, monkeypatch):
    """Red against an ensure that recomputes a state it already saw fail: each of the five
    bindings would recompute every domain, and fail again."""
    store, _d, _l, mention = _snapshotted(tmp_path)
    sync(store, mention)
    domain_snapshot.snapshot_path(store.path).write_text("{}", encoding="utf-8")
    calls: list[int] = []
    real = sync_mod._refresh_domains

    def spy(store_, reader_, **kw):
        calls.append(1)
        return real(store_, reader_, **kw)

    monkeypatch.setattr(sync_mod, "_refresh_domains", spy)
    for name, path in (
        ("Invoice", "src/pay/model.py"),
        ("charge", "src/pay/api.py"),
        ("helper", "src/core/util.py"),
        ("lonely", "src/misc/lonely.py"),
        ("extra", "src/core/util.py"),
    ):
        _tier1(store, mention, name, path)
    assert calls == [1]


def test_an_invalid_snapshot_keeps_membership_and_is_reported(tmp_path):
    """Red against a sync that falls back to the rules on a broken file (absorbing lonely.py)."""
    store, _d, _l, mention = _snapshotted(tmp_path)
    before = members(store)
    domain_snapshot.snapshot_path(store.path).write_text("{}", encoding="utf-8")
    report = sync(store, mention)
    assert {f["slug"] for f in report.domain_failures} == {"payments", "core", "docs"}
    assert all(f["error"].startswith("SnapshotError") for f in report.domain_failures)
    assert members(store) == before


def test_accepting_a_domain_with_a_snapshot_schedules_a_full_pass(tmp_path):
    """Red against unfixed activation: a split or a carried rename leaves another heir stale."""
    store, _d, legacy, _m = _snapshotted(tmp_path)
    store.set_meta(VOLATILE_STALE_KEY, "0")
    misc = accept(store, "misc", seeds=[("lonely", "src/misc/lonely.py")])
    assert activate_accepted_domain(misc, store, legacy).resolved
    assert store.get_meta(VOLATILE_STALE_KEY) == "1"


def test_capture_binds_the_snapshot_domain_on_a_merged_community(tmp_path):
    """Red against the community claim: payments claims merged community 1, so both anchors
    would bind domain:payments."""
    store, _d, _l, mention = _snapshotted(tmp_path)
    sync(store, mention)
    assert _tier1(store, mention, "extra", "src/core/util.py") == ["domain:core"]
    assert _tier1(store, mention, "lonely", "src/misc/lonely.py") == ["community:1"]


def test_bootstrap_counts_a_community_a_snapshot_domain_holds_in_part(tmp_path):
    """Red against the communities-only rule: merged community 1 is in no domain's
    communities, so bootstrap would offer it again."""
    store, _d, _l, mention = _snapshotted(tmp_path)
    sync(store, mention)
    assert "1" in _claimed_communities(store)


def test_without_a_snapshot_the_claimed_communities_are_today_s(tmp_path):
    """Guard (red against counting every member community, a discarded design, declared)."""
    store = Store(tmp_path / ".sidegraph")
    reader = GraphifyReader(write_graph(tmp_path, "legacy.json", legacy_graph()))
    part = accept(store, "part")
    seed_members(store, reader, part, {"model_invoice"})
    assert "1" not in _claimed_communities(store)


def _draft(title, name, path):
    return {
        "title": title,
        "kind": "gotcha",
        "context": "c",
        "choice": "ch",
        "anchors": [{"name": name, "file_path": path}],
    }


def test_a_pulled_snapshot_reaches_a_running_server_s_next_capture(tmp_path, monkeypatch):
    """Red against a server that keeps rule membership while its graph is unchanged: the
    capture right after the pull, before any retrieval call, would bind lonely() to payments,
    and drill-down would list it."""
    root = tmp_path / "proj" / ".sidegraph"
    legacy_path = write_graph(tmp_path, "legacy.json", legacy_graph())
    mention_path = write_graph(tmp_path, "mention.json", mention_graph())
    with Store(root) as store:
        standard_domains(store)
        sync(store, GraphifyReader(mention_path))
        pulled = take_snapshot(store, GraphifyReader(legacy_path), refresh=False)
    monkeypatch.setenv("SIDEGRAPH_GRAPH", str(mention_path))
    monkeypatch.setenv("SIDEGRAPH_DIR", str(root))
    monkeypatch.setattr(server_module, "_store", None)
    before = call_tool("add_decision", _draft("before", "lonely", "src/misc/lonely.py"))
    assert tier1_names(server_module._store, json.loads(before.content[0].text)["id"]) == [
        "domain:payments"
    ]
    domain_snapshot.write_snapshot(root, pulled)
    after = call_tool("add_decision", _draft("after", "lonely", "src/misc/lonely.py"))
    assert not after.is_error, after
    assert tier1_names(server_module._store, json.loads(after.content[0].text)["id"]) == [
        "community:1"
    ]
    text = call_tool("drill_down", {"domain_slug": "payments"}).content[0].text
    assert "lonely" not in text and "Invoice" in text
