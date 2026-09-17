"""MCP `add_domain` / `propose_domains` — the manual and agent-in-session domain
authoring paths (§4.2/§4.3, mind-model layer M2)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sidegraph.engine.reader import GraphifyReader
from sidegraph.retrieval import drill_down
from sidegraph.schema import Descriptor, Domain, DomainStatus, Provenance
from sidegraph.server import (
    _add_domain_impl,
    _list_domain_candidates_impl,
    _list_domains_impl,
    _propose_domains_impl,
    _ratify_impl,
    _supersede_domain_impl,
)
from sidegraph.store import Store

FIXTURE = Path(__file__).parent / "fixtures" / "mini_graph.json"


# -- add_domain (manual path, §4.3) -------------------------------------------------------


def test_add_domain_lands_proposed(tmp_path):
    store = Store(tmp_path / "t.db")
    out = _add_domain_impl(
        store,
        None,
        slug="payments",
        title="Payments",
        summary="Order settlement.",
    )
    assert out["status"] == "proposed"
    domain = store.get_domain(out["domain_id"])
    assert domain.status == DomainStatus.PROPOSED
    assert domain.slug == "payments"


def test_add_domain_manual_provenance(tmp_path):
    store = Store(tmp_path / "t.db")
    out = _add_domain_impl(
        store,
        None,
        slug="payments",
        title="Payments",
        summary="Order settlement.",
    )
    domain = store.get_domain(out["domain_id"])
    assert domain.provenance.source == "manual"
    assert domain.provenance.author == "agent"  # default


def test_add_domain_author_override(tmp_path):
    store = Store(tmp_path / "t.db")
    out = _add_domain_impl(
        store,
        None,
        slug="payments",
        title="Payments",
        summary="s.",
        author="alex",
    )
    assert store.get_domain(out["domain_id"]).provenance.author == "alex"


def test_add_domain_stamps_graph_version(tmp_path):
    store = Store(tmp_path / "t.db")
    reader = GraphifyReader(FIXTURE)
    out = _add_domain_impl(
        store,
        reader,
        slug="payments",
        title="Payments",
        summary="s.",
    )
    domain = store.get_domain(out["domain_id"])
    assert domain.provenance.graph_version == reader.graph_version()


def test_add_domain_with_path_prefixes_and_communities(tmp_path):
    store = Store(tmp_path / "t.db")
    out = _add_domain_impl(
        store,
        None,
        slug="payments",
        title="Payments",
        summary="s.",
        path_prefixes=["payments/"],
        communities=["7"],
    )
    domain = store.get_domain(out["domain_id"])
    assert domain.path_prefixes == ["payments/"]
    assert domain.communities == ["7"]


def test_add_domain_with_seed_anchors(tmp_path):
    """add_domain (§4.3, direct-write path) gains seed_anchors too (§2a amendment) --
    committed, durable authoring intent alongside its existing communities/path_prefixes
    immediate-seed params (kept, unremoved: a different, direct-write shape)."""
    store = Store(tmp_path / "t.db")
    out = _add_domain_impl(
        store,
        None,
        slug="payments",
        title="Payments",
        summary="s.",
        seed_anchors=[{"name": "OrderBook", "file_path": "trader/order_book.py"}],
    )
    domain = store.get_domain(out["domain_id"])
    assert domain.seed_anchors == [Descriptor(name="OrderBook", file_path="trader/order_book.py")]


def test_add_domain_resolves_parent_slug(tmp_path):
    store = Store(tmp_path / "t.db")
    root_out = _add_domain_impl(store, None, slug="root", title="Root", summary="s.")
    child_out = _add_domain_impl(
        store,
        None,
        slug="child",
        title="Child",
        summary="s.",
        parent_slug="root",
    )
    child = store.get_domain(child_out["domain_id"])
    assert child.parent_id == root_out["domain_id"]


def test_add_domain_unknown_parent_slug_raises(tmp_path):
    store = Store(tmp_path / "t.db")
    with pytest.raises(ValueError, match="parent_slug"):
        _add_domain_impl(
            store,
            None,
            slug="child",
            title="Child",
            summary="s.",
            parent_slug="no-such",
        )


def test_add_domain_slug_collision_raises(tmp_path):
    store = Store(tmp_path / "t.db")
    _add_domain_impl(store, None, slug="payments", title="Payments", summary="s.")
    with pytest.raises(ValueError, match="slug"):
        _add_domain_impl(store, None, slug="payments", title="Payments Again", summary="s.")


# -- propose_domains (agent in-session path, §4.2) -----------------------------------------


def _draft(**over):
    base = dict(slug="payments", title="Payments", summary="Order settlement and refunds.")
    base.update(over)
    return base


def test_propose_domains_writes_proposed(tmp_path):
    store = Store(tmp_path / "t.db")
    results = _propose_domains_impl(store, None, [_draft()], session_id="s1", author="alex")
    assert results[0]["status"] == "proposed"
    domain = store.get_domain(results[0]["domain_id"])
    assert domain.status == DomainStatus.PROPOSED
    assert domain.provenance.session_id == "s1"
    assert domain.provenance.author == "alex"
    assert domain.provenance.source == "agent"


def test_propose_domains_redacts_title_and_summary(tmp_path):
    store = Store(tmp_path / "t.db")
    results = _propose_domains_impl(
        store,
        None,
        [_draft(summary="internal note: api_key=sk-live-abc123 must stay off this domain")],
    )
    domain = store.get_domain(results[0]["domain_id"])
    assert "sk-live-abc123" not in domain.summary
    assert "[REDACTED]" in domain.summary
    assert results[0]["redactions"] >= 1


def test_propose_domains_dedups_same_slug_any_status(tmp_path):
    store = Store(tmp_path / "t.db")
    first = _propose_domains_impl(store, None, [_draft()])
    assert first[0]["status"] == "proposed"

    second = _propose_domains_impl(store, None, [_draft(title="Payments Again")])
    assert second[0]["status"] == "skipped"
    assert second[0]["domain_id"] == first[0]["domain_id"]

    # dropped domains still count as "any status" for the dedup skip
    store.ratify_domains(drop=[first[0]["domain_id"]])
    third = _propose_domains_impl(store, None, [_draft(title="Payments Once More")])
    assert third[0]["status"] == "skipped"


def test_propose_domains_accepted_slug_also_dedups(tmp_path):
    store = Store(tmp_path / "t.db")
    first = _propose_domains_impl(store, None, [_draft()])
    store.ratify_domains(accept=[first[0]["domain_id"]])
    second = _propose_domains_impl(store, None, [_draft(title="Payments Again")])
    assert second[0]["status"] == "skipped"


def test_propose_domains_resolves_parent_slug(tmp_path):
    store = Store(tmp_path / "t.db")
    root = _propose_domains_impl(store, None, [_draft(slug="root", title="Root")])
    child = _propose_domains_impl(
        store, None, [_draft(slug="child", title="Child", parent_slug="root")]
    )
    assert child[0]["status"] == "proposed"
    child_domain = store.get_domain(child[0]["domain_id"])
    assert child_domain.parent_id == root[0]["domain_id"]


def test_propose_domains_unknown_parent_slug_rejected(tmp_path):
    store = Store(tmp_path / "t.db")
    results = _propose_domains_impl(store, None, [_draft(parent_slug="no-such-domain")])
    assert results[0]["status"] == "rejected"
    assert "parent_slug" in results[0]["reason"]


def test_propose_domains_malformed_draft_rejected_batch_continues(tmp_path):
    store = Store(tmp_path / "t.db")
    results = _propose_domains_impl(store, None, [{"title": "no slug/summary"}, _draft()])
    assert results[0]["status"] == "rejected" and results[0]["reason"]
    assert results[1]["status"] == "proposed"


def test_propose_domains_bad_slug_rejected(tmp_path):
    store = Store(tmp_path / "t.db")
    results = _propose_domains_impl(store, None, [_draft(slug="Not_A_Slug")])
    assert results[0]["status"] == "rejected"


def test_propose_domains_stamps_graph_version(tmp_path):
    store = Store(tmp_path / "t.db")
    reader = GraphifyReader(FIXTURE)
    results = _propose_domains_impl(store, reader, [_draft()])
    domain = store.get_domain(results[0]["domain_id"])
    assert domain.provenance.graph_version == reader.graph_version()


# -- DraftDomain.seed_anchors: agent-curated merges through the ratify gate, durably
# (§2a amendment) -------------------------------------------------------------------------


def test_propose_domains_with_seed_anchors_persists_through_ratify_and_drill_down(
    tmp_path,
):
    """A curated/merged draft (several seed_anchors, no clean single path prefix) carries
    its `seed_anchors` straight through propose -> ratify. Unlike the old raw-community-id
    shape, `communities` is NOT populated at propose time (it's a volatile, engine-derived
    mapping) -- ratify itself resolves seed_anchors -> communities immediately (reusing
    sync's recompute helper) so drill_down shows the union of the anchored communities'
    members without a separate, explicit sync call."""
    store = Store(tmp_path / "t.db")
    # A locally-built graph (not the shared 2-community FIXTURE): a 2-community union needs
    # >= 10 total communities to stay under sync's 20% overbroad-claim cap (see
    # sync._DOMAIN_CLAIM_CAP) -- padded with filler communities for exactly that headroom.
    nodes = [
        {
            "id": "m_cls",
            "label": "Trader",
            "norm_label": "trader",
            "file_type": "code",
            "source_file": "trader/exec.py",
            "community": 1,
        },
        {
            "id": "o_fn",
            "label": "helper",
            "norm_label": "helper",
            "file_type": "code",
            "source_file": "util/misc.py",
            "community": 2,
        },
    ] + [
        {
            "id": f"filler-{i}",
            "label": f"Filler{i}",
            "norm_label": f"filler{i}",
            "file_type": "code",
            "source_file": f"misc/f{i}.py",
            "community": f"filler-{i}",
        }
        for i in range(8)
    ]
    graph_path = tmp_path / "order_exec_graph.json"
    graph_path.write_text(json.dumps({"built_at_commit": "v1", "nodes": nodes, "links": []}))
    reader = GraphifyReader(graph_path)  # community "1" (Trader), "2" (helper)

    results = _propose_domains_impl(
        store,
        reader,
        [
            _draft(
                slug="order-exec",
                title="Order Execution",
                seed_anchors=[
                    {"name": "Trader", "file_path": "trader/exec.py"},
                    {"name": "helper", "file_path": "util/misc.py"},
                ],
            )
        ],
    )
    assert results[0]["status"] == "proposed"
    domain = store.get_domain(results[0]["domain_id"])
    assert domain.seed_anchors == [
        Descriptor(name="Trader", file_path="trader/exec.py"),
        Descriptor(name="helper", file_path="util/misc.py"),
    ]
    assert domain.communities == []  # not yet resolved -- ratify resolves it below
    assert domain.path_prefixes == []  # untouched when the draft never set it

    _ratify_impl(store, accept=[domain.domain_id], reader=reader)
    ratified = store.get_domain(domain.domain_id)
    assert sorted(ratified.communities) == ["1", "2"]

    result = drill_down("order-exec", store, reader)
    assert result["found"] is True
    rendered = " ".join(result["members"])
    assert "Trader" in rendered  # community "1"
    assert "helper" in rendered  # community "2"


def test_ratify_rescues_seed_anchors_when_path_prefix_is_overbroad(tmp_path):
    """The primary onboarding path (finding D): a curated domain accepted via the skill can
    carry BOTH a broad ``path_prefixes`` and precise ``seed_anchors``. The overbroad-cap must
    drop only the path contribution, never the anchors -- so ratify-time
    ``refresh_domain_communities_now`` populates ``communities`` from the anchors, not [].
    And -- the silent-guard fix -- the ratify result string now tells the author their path
    rule was rejected, instead of leaving them to discover it only at the next full sync."""
    store = Store(tmp_path / "t.db")
    reader = _reader_over(tmp_path, _overbroad_graph_nodes())

    results = _propose_domains_impl(
        store,
        reader,
        [
            _draft(
                slug="trading",
                title="Trading",
                seed_anchors=[
                    {"name": "RiskGate", "file_path": "risk/gate.py"},
                    {"name": "Settings", "file_path": "config/settings.py"},
                ],
                path_prefixes=["trader"],  # deliberately broad: 3/10 communities
            )
        ],
    )
    domain_id = results[0]["domain_id"]
    out = _ratify_impl(store, accept=[domain_id], reader=reader)

    ratified = store.get_domain(domain_id)
    # The broad path was dropped by the cap; the two seed_anchors were NOT -- membership is
    # exactly the anchor-resolved communities, never [].
    assert sorted(ratified.communities) == ["config", "risk"]

    # ...and NOW the author is told their path rule was dropped. All three facts must be
    # present, because any one alone misleads: not applied, domain still works, which rule.
    entry = out[domain_id]
    assert entry.startswith("accepted")
    assert "path rule too broad" in entry
    assert "3/10 communities" in entry
    assert "not applied" in entry
    assert "seed_anchors, if any, still applied" in entry


def test_ratify_of_a_within_cap_domain_reports_no_marker(tmp_path):
    """The marker must not fire for an ordinary domain, or it becomes noise the author
    learns to ignore -- which is how the silent-guard problem comes back."""
    store = Store(tmp_path / "t.db")
    reader = _reader_over(tmp_path, _overbroad_graph_nodes())
    domain = _proposed_domain(
        store,
        slug="narrow",
        path_prefixes=["risk/gate.py"],  # exact-path match => 1/10, well under the cap
        seed_anchors=[{"name": "RiskGate", "file_path": "risk/gate.py"}],
    )

    out = _ratify_impl(store, accept=[domain.domain_id], drop=None, reader=reader)

    assert out[domain.domain_id] == "accepted"


# -- list_domain_candidates (read-only tool, §1 design) -------------------------------------


def _snapshot_dir(path: Path) -> list[tuple[str, int, int]]:
    """(relpath, size, mtime_ns) for every file under ``path`` — proves a call touched
    nothing on disk when compared before/after."""
    return sorted(
        (str(p.relative_to(path)), p.stat().st_size, p.stat().st_mtime_ns)
        for p in path.rglob("*")
        if p.is_file()
    )


def _candidates_graph() -> dict:
    """Five communities exercising every guard the tool must reflect:
    - "10": 6 members, 100% under "trader/" -> derives path_prefixes, groups there.
    - "20": 5 members, 3/5 "domainB" + 2/5 "domainC" (60% < 80%) -> no clean majority,
      lands in `ungrouped`.
    - "30": 5 members -> claimed by a pre-existing accepted domain before the tool runs.
    - "40": 5 members, labeled "RealThing" -- but "RealThing" is actually community "50"'s
      own entity (Gate-5 label-mismatch guard); must fall back to its own god node's name.
    - "50": 1 member ("RealThing" itself) -- below min_members, never a candidate on its
      own; exists only so `_label_names_other_community` has something to resolve against.
    """
    nodes = [
        {
            "id": f"t{i}",
            "label": f"Trader{i}",
            "norm_label": f"trader{i}",
            "file_type": "code",
            "source_file": f"trader/f{i}.py",
            "community": 10,
        }
        for i in range(1, 7)
    ]
    nodes += [
        {
            "id": f"m{i}",
            "label": f"Mixed{i}",
            "norm_label": f"mixed{i}",
            "file_type": "code",
            "source_file": f"domainB/x{i}.py",
            "community": 20,
        }
        for i in range(1, 4)
    ] + [
        {
            "id": f"m{i}",
            "label": f"Mixed{i}",
            "norm_label": f"mixed{i}",
            "file_type": "code",
            "source_file": f"domainC/y{i}.py",
            "community": 20,
        }
        for i in range(4, 6)
    ]
    nodes += [
        {
            "id": f"c{i}",
            "label": f"Claimed{i}",
            "norm_label": f"claimed{i}",
            "file_type": "code",
            "source_file": f"domainD/z{i}.py",
            "community": 30,
        }
        for i in range(1, 6)
    ]
    nodes += [
        {
            "id": f"foo{i}",
            "label": f"Foo{i}",
            "norm_label": f"foo{i}",
            "file_type": "code",
            "source_file": f"foo/f{i}.py",
            "community": 40,
        }
        for i in range(1, 6)
    ]
    nodes += [
        {
            "id": "real",
            "label": "RealThing",
            "norm_label": "realthing",
            "file_type": "code",
            "source_file": "bar/real.py",
            "community": 50,
        }
    ]
    links = [{"relation": "calls", "source": "foo1", "target": f"foo{i}"} for i in range(2, 6)]
    return {"built_at_commit": "v1", "nodes": nodes, "links": links}


def _candidates_reader(tmp_path: Path) -> GraphifyReader:
    (tmp_path / "graph.json").write_text(json.dumps(_candidates_graph()))
    (tmp_path / ".graphify_labels.json").write_text(
        json.dumps({"10": "Trader Domain", "40": "RealThing"})
    )
    return GraphifyReader(tmp_path / "graph.json")


def test_list_domain_candidates_writes_nothing_and_groups_by_shared_prefix(tmp_path):
    store = Store(tmp_path / "t.db")
    reader = _candidates_reader(tmp_path)

    claimed = store.add_domain(
        Domain(
            slug="claimed-area",
            title="Claimed Area",
            summary="Pre-existing.",
            communities=["30"],
            provenance=Provenance(source="manual"),
        )
    )
    store.ratify_domains(accept=[claimed.domain_id])

    before = _snapshot_dir(store.path)
    result = _list_domain_candidates_impl(store, reader)
    after = _snapshot_dir(store.path)
    assert before == after  # pure read: nothing on disk changed

    assert result["graph_version"] == reader.graph_version()
    assert result["total_candidates"] == 3  # communities 10, 20, 40 (30 claimed, 50 below)
    assert result["already_claimed"] == 1
    assert result["skipped"] == {"below_threshold": 1, "filtered": 0}

    groups_by_path = {g["path"]: g for g in result["groups"]}
    assert "trader" in groups_by_path
    trader_candidates = groups_by_path["trader"]["candidates"]
    assert [c["community"] for c in trader_candidates] == ["10"]
    trader = trader_candidates[0]
    assert trader["members"] == 6
    assert len(trader["top_members"]) <= 3
    assert trader["has_label"] is True
    assert groups_by_path["trader"]["member_total"] == 6

    # label-mismatch guard: community "40" is labeled "RealThing", but that entity
    # actually lives in community "50" -- the label must be rejected, never surfaced.
    all_candidates = [c for g in result["groups"] for c in g["candidates"]] + result["ungrouped"]
    by_community = {c["community"]: c for c in all_candidates}
    assert by_community["40"]["suggested_title"] != "RealThing"
    assert by_community["40"]["has_label"] is False

    # no clean shared prefix (60% < 80%) -> ungrouped, not silently dropped.
    ungrouped_communities = {c["community"] for c in result["ungrouped"]}
    assert ungrouped_communities == {"20"}

    # claimed ("30") and below-threshold ("50") communities never surface anywhere.
    all_output_communities = set(by_community) | ungrouped_communities
    assert "30" not in all_output_communities
    assert "50" not in all_output_communities


def test_list_domain_candidates_respects_min_members_and_paths_knobs(tmp_path):
    store = Store(tmp_path / "t.db")
    reader = _candidates_reader(tmp_path)

    result = _list_domain_candidates_impl(store, reader, paths=["trader"])
    all_candidates = [c for g in result["groups"] for c in g["candidates"]] + result["ungrouped"]
    assert {c["community"] for c in all_candidates} == {"10"}
    assert result["skipped"]["filtered"] > 0


def _many_communities_graph(n: int) -> dict:
    """``n`` distinct significant (5-member) communities, no labels, no links, no shared
    path prefixes -- a large-scale candidate-count fixture (BUG B: default `limit`) standing
    in for a monorepo-scale corpus (real finding: Airflow returns 2,578 candidates at
    default settings)."""
    nodes = [
        {
            "id": f"c{c}n{i}",
            "label": f"Thing{c}_{i}",
            "norm_label": f"thing{c}_{i}",
            "file_type": "code",
            "source_file": f"area{c}/f{i}.py",
            "community": c,
        }
        for c in range(n)
        for i in range(5)
    ]
    return {"built_at_commit": "v1", "nodes": nodes, "links": []}


def _many_communities_reader(tmp_path: Path, n: int) -> GraphifyReader:
    (tmp_path / "graph.json").write_text(json.dumps(_many_communities_graph(n)))
    return GraphifyReader(tmp_path / "graph.json")


def test_list_domain_candidates_explicit_limit_truncates_and_flags(tmp_path):
    """BUG B: an explicit `limit` (what the MCP tool applies as its default, 100) caps the
    candidate count and signals the cut -- `truncated=True`, `total_significant` showing
    the FULL count independent of what got returned."""
    store = Store(tmp_path / "t.db")
    reader = _many_communities_reader(tmp_path, 150)

    result = _list_domain_candidates_impl(store, reader, limit=100)

    all_candidates = [c for g in result["groups"] for c in g["candidates"]] + result["ungrouped"]
    assert len(all_candidates) == 100
    assert result["total_candidates"] == 100
    assert result["total_significant"] == 150
    assert result["truncated"] is True
    assert "note" in result


def test_list_domain_candidates_explicit_limit_widens(tmp_path):
    store = Store(tmp_path / "t.db")
    reader = _many_communities_reader(tmp_path, 150)

    result = _list_domain_candidates_impl(store, reader, limit=30)

    all_candidates = [c for g in result["groups"] for c in g["candidates"]] + result["ungrouped"]
    assert len(all_candidates) == 30
    assert result["total_candidates"] == 30
    assert result["total_significant"] == 150
    assert result["truncated"] is True


def test_list_domain_candidates_unlimited_returns_everything_untruncated(tmp_path):
    """The "all" convention: `limit=None` (what the tool's `limit=0` sentinel resolves to)
    returns the full list, unflagged."""
    store = Store(tmp_path / "t.db")
    reader = _many_communities_reader(tmp_path, 150)

    result = _list_domain_candidates_impl(store, reader, limit=None)

    all_candidates = [c for g in result["groups"] for c in g["candidates"]] + result["ungrouped"]
    assert len(all_candidates) == 150
    assert result["total_candidates"] == 150
    assert result["total_significant"] == 150
    assert result["truncated"] is False
    assert "note" not in result


def test_list_domain_candidates_anchor_resolves_back_to_same_community(tmp_path):
    """Each candidate carries a durable `anchor` (the community's god-node resolved to a
    name+file_path Descriptor, §2a amendment) -- feeding it back through
    reader.resolve resolves to the SAME community the candidate came from, exactly what
    the name-domains skill needs to build a Domain.seed_anchors entry from a candidate."""
    store = Store(tmp_path / "t.db")
    reader = _candidates_reader(tmp_path)

    result = _list_domain_candidates_impl(store, reader)
    all_candidates = [c for g in result["groups"] for c in g["candidates"]] + result["ungrouped"]
    trader = next(c for c in all_candidates if c["community"] == "10")

    assert trader["anchor"] is not None
    anchor = Descriptor(**trader["anchor"])
    resolved = reader.resolve(anchor)
    assert resolved.status == "resolved"
    assert resolved.community == "10"


# -- list_domains (read-only listing tool, item 1) -------------------------------------------


def _new_domain(**over) -> Domain:
    base = dict(
        slug="payments",
        title="Payments",
        summary="Order settlement and refunds.",
        provenance=Provenance(source="manual"),
    )
    base.update(over)
    return Domain(**base)


def test_list_domains_returns_all_regardless_of_status(tmp_path):
    store = Store(tmp_path / "t.db")
    accepted1 = store.add_domain(_new_domain(slug="payments", title="Payments"))
    store.ratify_domains(accept=[accepted1.domain_id])
    accepted2 = store.add_domain(_new_domain(slug="shipping", title="Shipping"))
    store.ratify_domains(accept=[accepted2.domain_id])
    store.add_domain(_new_domain(slug="billing", title="Billing"))
    dropped = store.add_domain(_new_domain(slug="refunds", title="Refunds"))
    store.ratify_domains(drop=[dropped.domain_id])

    out = _list_domains_impl(store)
    assert len(out) == 4
    by_slug = {d["slug"]: d for d in out}
    assert by_slug["payments"]["status"] == "accepted"
    assert by_slug["shipping"]["status"] == "accepted"
    assert by_slug["billing"]["status"] == "proposed"
    assert by_slug["refunds"]["status"] == "dropped"


def test_list_domains_filters_by_status(tmp_path):
    store = Store(tmp_path / "t.db")
    accepted = store.add_domain(_new_domain(slug="payments"))
    store.ratify_domains(accept=[accepted.domain_id])
    store.add_domain(_new_domain(slug="billing"))

    accepted_only = _list_domains_impl(store, status="accepted")
    assert [d["slug"] for d in accepted_only] == ["payments"]

    proposed_only = _list_domains_impl(store, status="proposed")
    assert [d["slug"] for d in proposed_only] == ["billing"]


def test_list_domains_writes_nothing(tmp_path):
    store = Store(tmp_path / "t.db")
    store.add_domain(_new_domain(slug="payments"))
    before = _snapshot_dir(store.path)
    _list_domains_impl(store)
    _list_domains_impl(store, status="proposed")
    after = _snapshot_dir(store.path)
    assert before == after


def test_list_domains_reports_member_path_and_seed_anchor_counts(tmp_path):
    store = Store(tmp_path / "t.db")
    store.add_domain(
        _new_domain(
            communities=["7", "8"],
            path_prefixes=["payments/"],
            seed_anchors=[Descriptor(name="OrderBook", file_path="trader/order_book.py")],
        )
    )
    row = _list_domains_impl(store)[0]
    assert row["member_count"] == 2
    assert row["path_prefixes"] == ["payments/"]
    assert row["seed_anchor_count"] == 1
    assert row["id"]
    assert row["title"] == "Payments"
    assert row["summary"] == "Order settlement and refunds."


def test_list_domains_reports_parent_and_child_slugs(tmp_path):
    store = Store(tmp_path / "t.db")
    root = store.add_domain(_new_domain(slug="root", title="Root"))
    store.add_domain(_new_domain(slug="child", title="Child", parent_id=root.domain_id))

    by_slug = {d["slug"]: d for d in _list_domains_impl(store)}
    assert by_slug["root"]["child_slugs"] == ["child"]
    assert by_slug["root"]["parent_slug"] is None
    assert by_slug["child"]["parent_slug"] == "root"
    assert by_slug["child"]["child_slugs"] == []


# -- supersede_domain (MCP wrapper over Store.supersede_domain, item 2) ----------------------


def test_supersede_domain_closes_old_and_writes_proposed_successor(tmp_path):
    store = Store(tmp_path / "t.db")
    old = store.add_domain(
        _new_domain(
            slug="payments",
            title="Payments v1",
            summary="v1 summary",
            communities=["7"],
            path_prefixes=["payments/"],
        )
    )
    store.ratify_domains(accept=[old.domain_id])

    out = _supersede_domain_impl(
        store,
        None,
        old.domain_id,
        "payments-v2",
        "Payments v2",
        "v2 summary",
        path_prefixes=["payments/", "billing/"],
    )
    assert out["supersedes"] == old.domain_id
    # Same "one gate, no exceptions" rule add_domain/propose_domains follow: the successor
    # lands proposed, not auto-accepted -- a human still ratifies it.
    assert out["status"] == "proposed"

    reloaded_old = store.get_domain(old.domain_id)
    assert reloaded_old.status == DomainStatus.SUPERSEDED
    assert reloaded_old.summary == "v1 summary"  # history retrievable, unmodified

    new_domain = store.get_domain(out["domain_id"])
    assert new_domain.slug == "payments-v2"
    assert new_domain.title == "Payments v2"
    assert new_domain.summary == "v2 summary"
    assert new_domain.path_prefixes == ["payments/", "billing/"]
    assert new_domain.supersedes == old.domain_id
    assert new_domain.status == DomainStatus.PROPOSED


def test_supersede_domain_resolves_old_by_slug_not_just_id(tmp_path):
    store = Store(tmp_path / "t.db")
    old = store.add_domain(_new_domain(slug="payments"))
    out = _supersede_domain_impl(store, None, "payments", "payments-v2", "Payments v2", "v2")
    assert out["supersedes"] == old.domain_id


def test_supersede_domain_carries_seed_anchors(tmp_path):
    store = Store(tmp_path / "t.db")
    old = store.add_domain(_new_domain())
    out = _supersede_domain_impl(
        store,
        None,
        old.domain_id,
        "payments-v2",
        "Payments v2",
        "v2",
        seed_anchors=[{"name": "OrderBook", "file_path": "trader/order_book.py"}],
    )
    new_domain = store.get_domain(out["domain_id"])
    assert new_domain.seed_anchors == [
        Descriptor(name="OrderBook", file_path="trader/order_book.py")
    ]


def test_supersede_domain_resolves_parent_slug(tmp_path):
    store = Store(tmp_path / "t.db")
    root = store.add_domain(_new_domain(slug="root", title="Root"))
    old = store.add_domain(_new_domain(slug="payments"))
    out = _supersede_domain_impl(
        store, None, old.domain_id, "payments-v2", "Payments v2", "v2", parent_slug="root"
    )
    new_domain = store.get_domain(out["domain_id"])
    assert new_domain.parent_id == root.domain_id


def test_supersede_domain_unknown_old_ref_raises(tmp_path):
    store = Store(tmp_path / "t.db")
    with pytest.raises(ValueError, match="old_slug_or_id"):
        _supersede_domain_impl(store, None, "no-such", "new-slug", "New", "s.")


def test_supersede_domain_unknown_parent_slug_raises(tmp_path):
    store = Store(tmp_path / "t.db")
    old = store.add_domain(_new_domain())
    with pytest.raises(ValueError, match="parent_slug"):
        _supersede_domain_impl(
            store, None, old.domain_id, "payments-v2", "Payments v2", "v2", parent_slug="nope"
        )


def test_supersede_domain_stamps_manual_provenance_and_author(tmp_path):
    store = Store(tmp_path / "t.db")
    old = store.add_domain(_new_domain())
    out = _supersede_domain_impl(
        store, None, old.domain_id, "payments-v2", "Payments v2", "v2", author="alex"
    )
    new_domain = store.get_domain(out["domain_id"])
    assert new_domain.provenance.source == "manual"
    assert new_domain.provenance.author == "alex"


def test_supersede_domain_stamps_graph_version(tmp_path):
    store = Store(tmp_path / "t.db")
    reader = GraphifyReader(FIXTURE)
    old = store.add_domain(_new_domain())
    out = _supersede_domain_impl(store, reader, old.domain_id, "payments-v2", "Payments v2", "v2")
    new_domain = store.get_domain(out["domain_id"])
    assert new_domain.provenance.graph_version == reader.graph_version()


# -- ratify schedules a heal when membership could not be resolved ----------------------


def _overbroad_graph_nodes() -> list[dict]:
    """3 communities under trader/ (path="trader" matches 3/10 = 30% > 20% cap) + 2 anchor
    communities elsewhere + 5 filler for headroom. Shared by the overbroad-path-rule tests
    below (one for the cap tripping, one for the within-cap twin)."""
    return (
        [
            {
                "id": f"t{i}",
                "label": f"T{i}",
                "norm_label": f"t{i}",
                "file_type": "code",
                "source_file": f"trader/mod{i}.py",
                "community": f"trader-{i}",
            }
            for i in range(3)
        ]
        + [
            {
                "id": "risk_gate",
                "label": "RiskGate",
                "norm_label": "riskgate",
                "file_type": "code",
                "source_file": "risk/gate.py",
                "community": "risk",
            },
            {
                "id": "cfg",
                "label": "Settings",
                "norm_label": "settings",
                "file_type": "code",
                "source_file": "config/settings.py",
                "community": "config",
            },
        ]
        + [
            {
                "id": f"f{i}",
                "label": f"F{i}",
                "norm_label": f"f{i}",
                "file_type": "code",
                "source_file": f"misc/f{i}.py",
                "community": f"filler-{i}",
            }
            for i in range(5)
        ]
    )


def _reader_over(tmp_path: Path, nodes: list[dict]) -> GraphifyReader:
    graph_path = tmp_path / "g.json"
    graph_path.write_text(json.dumps({"built_at_commit": "v1", "nodes": nodes, "links": []}))
    return GraphifyReader(graph_path)


def _proposed_domain(store, slug: str, **kw):
    """A status=proposed domain, ready to ratify. Defaults to one seed anchor so ratify has
    something to resolve; pass path_prefixes=/seed_anchors= to override."""
    kw.setdefault("seed_anchors", [{"name": "foo()", "file_path": "a.py"}])
    return store.add_domain(
        Domain(
            slug=slug,
            title=slug,
            summary=f"{slug} summary",
            status=DomainStatus.PROPOSED,
            provenance=Provenance(source="manual"),
            **kw,
        )
    )


def _reader_with_two_communities(tmp_path):
    """The same two-node GRAPH used in tests/test_sync_volatile_heal.py, as a reader."""
    graph = {
        "built_at_commit": "v1",
        "nodes": [
            {
                "id": "n1",
                "label": "foo()",
                "source_file": "a.py",
                "source_location": "L1",
                "file_type": "code",
                "community": "1",
            },
            {
                "id": "n2",
                "label": "bar()",
                "source_file": "b.py",
                "source_location": "L1",
                "file_type": "code",
                "community": "2",
            },
        ],
        "links": [],
    }
    graph_path = tmp_path / "graph.json"
    graph_path.write_text(json.dumps(graph), encoding="utf-8")
    return GraphifyReader(graph_path)


def test_ratify_without_a_reader_schedules_a_heal(tmp_path):
    """A ratify writes only store files, whose digest is stamped by _touch_digest and is
    therefore reload-invisible, and graph_version has not moved -- so without this flag the
    next sync is GATED and the domain never gains membership at all."""
    from sidegraph.store import VOLATILE_STALE_KEY

    store = Store(tmp_path / "t.db")
    domain = _proposed_domain(store, slug="orphaned-at-ratify")
    store.set_meta(VOLATILE_STALE_KEY, "0")

    _ratify_impl(store, accept=[domain.domain_id], drop=None)  # reader defaults to None

    assert store.get_meta(VOLATILE_STALE_KEY) == "1"


def test_ratify_whose_refresh_raises_still_schedules_a_heal(tmp_path, monkeypatch):
    """The failure twin: a reader IS present but the refresh throws. The domain lands in the
    SAME unresolved state as the no-reader case, so it needs the same flag -- otherwise the
    gap this task closes is simply reopened in the error path."""
    from sidegraph import sync as sync_mod
    from sidegraph.store import VOLATILE_STALE_KEY

    store = Store(tmp_path / "t.db")
    reader = _reader_with_two_communities(tmp_path)
    domain = _proposed_domain(store, slug="refresh-explodes")
    store.set_meta(VOLATILE_STALE_KEY, "0")
    monkeypatch.setattr(
        sync_mod,
        "refresh_domain_communities_now",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("resolve blew up")),
    )

    out = _ratify_impl(store, accept=[domain.domain_id], drop=None, reader=reader)

    assert out[domain.domain_id] == "accepted"  # a ratify never fails on this
    assert store.get_meta(VOLATILE_STALE_KEY) == "1"
