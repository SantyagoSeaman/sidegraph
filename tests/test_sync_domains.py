"""Domain community refresh + empty-domain flag on the sync pass (see
docs/concepts/mind-model.md)."""

from __future__ import annotations

import json

from sidegraph.domains import bootstrap_domains
from sidegraph.engine.reader import GraphifyReader
from sidegraph.schema import Descriptor, Domain, DomainStatus, Provenance
from sidegraph.store import Store
from sidegraph.sync import sync


def _write_graph(tmp_path, name, data):
    p = tmp_path / name
    p.write_text(json.dumps(data))
    return p


def _reader(tmp_path, name, data):
    return GraphifyReader(_write_graph(tmp_path, name, data))


def _accept(store: Store, domain: Domain) -> Domain:
    added = store.add_domain(domain)
    store.ratify_domains(accept=[added.domain_id])
    return store.get_domain(added.domain_id)


# -- Gate-5 reproduction: the exact release-gate BLOCKER, at small scale ------------------
#
# `bootstrap_domains` followed docs/guides/naming-your-domains.md on a reference repo and
# derived `path_prefixes=["tests"]` for the "ExchangeBackend" community because >= 80% of
# its anchorable members happened to be test files -- true of any god-node community that
# clusters with its own tests, not evidence "tests/" is THAT community's home. Sync's
# REPLACE refresh (see sync._recompute_domain_communities) then legitimately, and
# silently, expanded the domain's `communities` from its own home community to 146 of 304
# -- every other community that also ships tests/. New decisions tier-1-anchored to that
# domain landed on the wrong community, TOC mistake counts went wrong, and the damage is
# silent and append-only-permanent (the ratify listing didn't show path_prefixes at all).
#
# This reproduces the shape at small scale: one community ("eb") that clusters with its
# own tests (8 of 9 anchorable members under tests/), inside a graph where MOST OTHER
# communities also ship a tests/ file of their own (the exact backdrop that let the old,
# unguarded >=80%-majority rule treat "tests/" as this community's private stabilizer).
def _exchange_backend_nodes(community: str) -> list[dict]:
    return [
        {
            "id": f"{community}-src",
            "label": "ExchangeBackend",
            "norm_label": "exchangebackend",
            "file_type": "code",
            "source_file": "src/exchange_backend.py",
            "community": community,
        },
    ] + [
        {
            "id": f"{community}-t{i}",
            "label": f"test_exchange_{i}",
            "norm_label": f"test_exchange_{i}",
            "file_type": "code",
            "source_file": f"tests/test_exchange_{i}.py",
            "community": community,
        }
        for i in range(8)  # 8/9 ~= 89% under tests/ -- clears the 80% majority threshold
    ]


def _other_module_nodes(community: str) -> list[dict]:
    """Every OTHER community also ships its own tests/ file -- the backdrop that made a
    raw >=80%-majority rule (with no shared-dir/breadth guard) insufficient: "most
    communities have some tests/ presence" is what let the bug's REPLACE refresh treat
    tests/ as direct evidence for swallowing every one of them."""
    return [
        {
            "id": f"{community}-src",
            "label": f"Module{community}",
            "norm_label": f"module{community}",
            "file_type": "code",
            "source_file": f"src/module_{community}.py",
            "community": community,
        },
        {
            "id": f"{community}-test",
            "label": f"test_module_{community}",
            "norm_label": f"test_module_{community}",
            "file_type": "code",
            "source_file": f"tests/test_module_{community}.py",
            "community": community,
        },
    ]


def test_gate5_tests_majority_community_does_not_swallow_others_on_sync(tmp_path):
    other_communities = [f"m{i}" for i in range(20)]
    nodes = _exchange_backend_nodes("eb")
    for c in other_communities:
        nodes += _other_module_nodes(c)
    graph_v1 = {"built_at_commit": "v1", "nodes": nodes, "links": []}
    reader_v1 = _reader(tmp_path, "g1.json", graph_v1)

    store = Store(tmp_path / "t.db")
    bootstrap_domains(store, reader_v1, min_members=5)

    eb = store.find_domain_by_slug("exchangebackend")
    assert eb is not None
    # Guard 1 (domains._SHARED_DIR_NAMES): "tests" is never derived as a stabilizer, even
    # though it's an 89% majority of this community's own members.
    assert eb.path_prefixes == []
    assert eb.communities == ["eb"]
    store.ratify_domains(accept=[eb.domain_id])

    # A later rebuild: same structure (tests/ still present in every community, nothing
    # about ExchangeBackend's own files changed) -- "sync against a graph where tests/
    # files appear in most communities" from the gate scenario.
    reader_v2 = _reader(tmp_path, "g2.json", graph_v1)
    report = sync(store, reader_v2, force=True)

    healed = store.find_domain_by_slug("exchangebackend")
    # No swallow: with no derived path_prefixes, refresh falls back to survivors of the
    # domain's OWN previously-recorded community -- never guesses new ones from the
    # tests/-heavy backdrop.
    assert healed.communities == ["eb"]
    assert len(healed.communities) == 1
    assert report.overbroad_domains == []  # no path rule was ever derived to trip the cap


# Padding: 9 unrelated filler communities on top of the "payments" one under test, so the
# graph has 10 communities total. Without this, a graph with only ONE community makes ANY
# single-community path-prefix match sit at 100% of "all current communities" -- which
# would trip the refresh claim cap (sync._DOMAIN_CLAIM_CAP, 20%) on every legitimate
# single-community domain, not just an over-broad one. The cap is meant to catch a claim
# that's disproportionate relative to a REAL graph (see test_refresh_caps_overbroad_... below,
# which deliberately keeps its graph small enough to trip it) -- these two fixtures are
# about ordinary re-anchoring after Leiden renumbering, so they're padded to a size where
# "one community out of many" reads as the normal case it is.
_FILLER_COMMUNITIES = [
    {
        "id": f"filler-{i}",
        "label": f"Filler{i}",
        "norm_label": f"filler{i}",
        "file_type": "code",
        "source_file": f"unrelated/f{i}.py",
        "community": f"filler-c{i}",
    }
    for i in range(9)
]

GRAPH_OLD = {
    "built_at_commit": "v1",
    "nodes": [
        {
            "id": "n1",
            "label": "Alpha",
            "norm_label": "alpha",
            "file_type": "code",
            "source_file": "payments/a.py",
            "community": 1,
        },
        {
            "id": "n2",
            "label": "Beta",
            "norm_label": "beta",
            "file_type": "code",
            "source_file": "payments/b.py",
            "community": 1,
        },
    ]
    + _FILLER_COMMUNITIES,
    "links": [],
}

# Leiden renumbered 1 -> 7; payments/ files still exist under the new id.
GRAPH_RENUMBERED = {
    "built_at_commit": "v2",
    "nodes": [
        {
            "id": "n1",
            "label": "Alpha",
            "norm_label": "alpha",
            "file_type": "code",
            "source_file": "payments/a.py",
            "community": 7,
        },
        {
            "id": "n2",
            "label": "Beta",
            "norm_label": "beta",
            "file_type": "code",
            "source_file": "payments/b.py",
            "community": 7,
        },
    ]
    + _FILLER_COMMUNITIES,
    "links": [],
}


def test_refresh_adopts_new_community_via_path_prefixes(tmp_path):
    store = Store(tmp_path / "t.db")
    _accept(
        store,
        Domain(
            slug="payments",
            title="Payments",
            summary="s",
            communities=["1"],
            path_prefixes=["payments"],
            provenance=Provenance(source="manual"),
        ),
    )
    reader = _reader(tmp_path, "g.json", GRAPH_RENUMBERED)
    report = sync(store, reader)

    d = store.find_domain_by_slug("payments")
    assert d.communities == ["7"]  # old id gone, new id adopted
    assert report.domains_refreshed == 1
    assert report.empty_domains == []


def test_no_prefix_domain_keeps_only_surviving_old_ids(tmp_path):
    store = Store(tmp_path / "t.db")
    _accept(
        store,
        Domain(
            slug="mixed",
            title="Mixed",
            summary="s",
            communities=["1", "99"],
            provenance=Provenance(source="manual"),  # no path_prefixes
        ),
    )
    # "1" still exists in the current graph, "99" does not.
    reader = _reader(tmp_path, "g.json", GRAPH_OLD)
    report = sync(store, reader)

    d = store.find_domain_by_slug("mixed")
    assert d.communities == ["1"]  # only the surviving id kept; nothing speculative
    assert report.domains_refreshed == 1


def test_no_change_is_a_no_op_write(tmp_path):
    store = Store(tmp_path / "t.db")
    _accept(
        store,
        Domain(
            slug="payments",
            title="Payments",
            summary="s",
            communities=["1"],
            path_prefixes=["payments"],
            provenance=Provenance(source="manual"),
        ),
    )
    reader = _reader(tmp_path, "g.json", GRAPH_OLD)  # same communities as recorded
    report = sync(store, reader)

    assert report.domains_refreshed == 0
    assert store.find_domain_by_slug("payments").communities == ["1"]


def test_empty_domain_flagged_when_no_prefix_and_all_old_ids_gone(tmp_path):
    store = Store(tmp_path / "t.db")
    _accept(
        store,
        Domain(
            slug="ghost",
            title="Ghost",
            summary="s",
            communities=["42"],
            provenance=Provenance(source="manual"),  # no path_prefixes
        ),
    )
    reader = _reader(tmp_path, "g.json", GRAPH_OLD)  # "42" doesn't exist anymore
    report = sync(store, reader)

    assert report.empty_domains == [{"slug": "ghost", "title": "Ghost"}]
    d = store.find_domain_by_slug("ghost")
    assert d.communities == []
    assert d.status == DomainStatus.ACCEPTED  # status never auto-changed


def test_empty_domain_flagged_when_path_prefixes_match_nothing(tmp_path):
    store = Store(tmp_path / "t.db")
    _accept(
        store,
        Domain(
            slug="vanished",
            title="Vanished",
            summary="s",
            communities=["1"],
            path_prefixes=["nowhere/"],
            provenance=Provenance(source="manual"),
        ),
    )
    reader = _reader(tmp_path, "g.json", GRAPH_RENUMBERED)  # no node under nowhere/
    report = sync(store, reader)

    assert report.empty_domains == [{"slug": "vanished", "title": "Vanished"}]


def test_domain_with_live_path_prefix_never_flagged_empty(tmp_path):
    """A domain still matching >=1 current node via path_prefixes is never flagged empty,
    even in the (unusual) case those matches carry no community label."""
    store = Store(tmp_path / "t.db")
    _accept(
        store,
        Domain(
            slug="payments",
            title="Payments",
            summary="s",
            communities=["1"],
            path_prefixes=["payments"],
            provenance=Provenance(source="manual"),
        ),
    )
    graph = {
        "built_at_commit": "v3",
        "nodes": [
            {
                "id": "n1",
                "label": "Alpha",
                "norm_label": "alpha",
                "file_type": "code",
                "source_file": "payments/a.py",
            },  # no "community" key at all
        ],
        "links": [],
    }
    reader = _reader(tmp_path, "g.json", graph)
    report = sync(store, reader)

    assert report.empty_domains == []
    assert store.find_domain_by_slug("payments").communities == []


# -- fix: boundary-safe path-prefix matching on the sync-side refresh (same probe as
# domains.py's bootstrap filter: "payments" must not loosely match "payments_v2/...") ----


def test_refresh_path_prefix_matching_is_boundary_safe(tmp_path):
    store = Store(tmp_path / "t.db")
    _accept(
        store,
        Domain(
            slug="payments",
            title="Payments",
            summary="s",
            communities=["1"],
            path_prefixes=["payments"],
            provenance=Provenance(source="manual"),
        ),
    )
    graph = {
        "built_at_commit": "v9",
        "nodes": [
            {
                "id": "n1",
                "label": "Alpha",
                "norm_label": "alpha",
                "file_type": "code",
                "source_file": "payments_v2/a.py",
                "community": 55,
            },
        ],
        "links": [],
    }
    reader = _reader(tmp_path, "g.json", graph)
    report = sync(store, reader)

    # "payments_v2/..." must NOT match the "payments" prefix (boundary-safe): no live
    # match today, and the old community "1" is gone too -> flagged empty, never adopts
    # the sibling directory's community "55".
    d = store.find_domain_by_slug("payments")
    assert d.communities == []
    assert report.empty_domains == [{"slug": "payments", "title": "Payments"}]


def test_proposed_and_dropped_domains_are_never_refreshed(tmp_path):
    store = Store(tmp_path / "t.db")
    proposed = store.add_domain(
        Domain(
            slug="idle",
            title="Idle",
            summary="s",
            communities=["1"],
            provenance=Provenance(source="manual"),
        )
    )
    reader = _reader(tmp_path, "g.json", GRAPH_RENUMBERED)  # "1" no longer exists
    report = sync(store, reader)

    assert report.domains_refreshed == 0
    assert report.empty_domains == []
    assert store.get_domain(proposed.domain_id).communities == ["1"]  # untouched


# -- refresh claim cap (Gate-5 fix 2, sync._DOMAIN_CLAIM_CAP): defense in depth on the
# sync side, independent of bootstrap's own derivation guard -- a MANUALLY authored
# path_prefixes rule (never blocked at write time; human intent, see
# docs/guides/naming-your-domains.md) is still capped here if it turns out, against the
# live graph, to claim too much. ---------------------------------------------------------


def test_refresh_caps_overbroad_path_derived_claim_keeps_old_mapping(tmp_path):
    store = Store(tmp_path / "t.db")
    _accept(
        store,
        Domain(
            # A human explicitly authored `--path tests` for a "test infra" domain -- allowed
            # at write time (manual authoring is never blocked), but the cap still protects
            # once it meets a real graph.
            slug="test-infra",
            title="Test Infra",
            summary="s",
            communities=["1"],
            path_prefixes=["tests"],
            provenance=Provenance(source="manual"),
        ),
    )
    # 6 communities total; "tests/" files sit in 4 of them (4/6 ~= 67% > 20%).
    nodes = [
        {
            "id": f"c{c}src",
            "label": f"Thing{c}",
            "norm_label": f"thing{c}",
            "file_type": "code",
            "source_file": f"domain{c}/f.py",
            "community": c,
        }
        for c in range(1, 7)
    ] + [
        {
            "id": f"c{c}test",
            "label": f"test_thing{c}",
            "norm_label": f"test_thing{c}",
            "file_type": "code",
            "source_file": f"tests/test_thing{c}.py",
            "community": c,
        }
        for c in (1, 2, 3, 4)
    ]
    graph = {"built_at_commit": "v9", "nodes": nodes, "links": []}
    reader = _reader(tmp_path, "g.json", graph)

    report = sync(store, reader)

    d = store.find_domain_by_slug("test-infra")
    # Never-guess: the over-broad claim is never written; the previous mapping is kept.
    assert d.communities == ["1"]
    assert report.overbroad_domains == [
        {"slug": "test-infra", "title": "Test Infra", "matched": 4, "total": 6},
    ]
    assert report.domains_refreshed == 0
    assert report.empty_domains == []  # kept mapping is non-empty -- never also flagged empty


def test_refresh_single_community_claim_never_capped_even_on_tiny_graph(tmp_path):
    """Dead-zone regression: a SINGLE-community path-derived claim can never "swallow"
    anything else, so it must never be capped, no matter how large `1/total` happens to be
    on a small graph. Before this floor, a tiny graph (here: 3 total communities, so
    `1/3 ~= 33% > 20%`) tripped the cap on a perfectly legitimate single-community claim,
    keeping the stale pre-renumber community id forever -- with no path_prefixes-driven way
    to ever heal, since every future recompute against the same small graph re-derives the
    same single-community candidate and gets capped again."""
    store = Store(tmp_path / "t.db")
    _accept(
        store,
        Domain(
            slug="payments",
            title="Payments",
            summary="s",
            communities=["1"],
            path_prefixes=["payments"],
            provenance=Provenance(source="manual"),
        ),
    )
    # Exactly 3 total communities -- 1/3 ~= 33% > 20% would trip a floor-less cap even
    # though the candidate resolves to exactly ONE community.
    graph = {
        "built_at_commit": "v2",
        "nodes": [
            {
                "id": "n1",
                "label": "Alpha",
                "norm_label": "alpha",
                "file_type": "code",
                "source_file": "payments/a.py",
                "community": 7,
            },  # renumbered 1 -> 7
            {
                "id": "n2",
                "label": "Other",
                "norm_label": "other",
                "file_type": "code",
                "source_file": "other/b.py",
                "community": 8,
            },
            {
                "id": "n3",
                "label": "Third",
                "norm_label": "third",
                "file_type": "code",
                "source_file": "third/c.py",
                "community": 9,
            },
        ],
        "links": [],
    }
    reader = _reader(tmp_path, "g.json", graph)
    report = sync(store, reader)

    d = store.find_domain_by_slug("payments")
    assert d.communities == ["7"]  # healed -- old id gone, new id adopted
    assert report.overbroad_domains == []
    assert report.domains_refreshed == 1


def test_refresh_claim_at_or_below_cap_is_written_normally(tmp_path):
    """Boundary: the cap is "> 20%", not ">= 20%" -- a claim sitting exactly at the
    threshold must still be written."""
    store = Store(tmp_path / "t.db")
    _accept(
        store,
        Domain(
            slug="payments",
            title="Payments",
            summary="s",
            communities=["1"],
            path_prefixes=["payments"],
            provenance=Provenance(source="manual"),
        ),
    )
    # 5 communities total; "payments/" matches exactly 1 of them (1/5 = 20%, not > 20%).
    nodes = [
        {
            "id": "n1",
            "label": "Alpha",
            "norm_label": "alpha",
            "file_type": "code",
            "source_file": "payments/a.py",
            "community": 1,
        },
    ] + [
        {
            "id": f"c{c}",
            "label": f"Thing{c}",
            "norm_label": f"thing{c}",
            "file_type": "code",
            "source_file": f"domain{c}/f.py",
            "community": c,
        }
        for c in range(2, 6)
    ]
    graph = {"built_at_commit": "v9", "nodes": nodes, "links": []}
    reader = _reader(tmp_path, "g.json", graph)

    report = sync(store, reader)

    assert store.find_domain_by_slug("payments").communities == ["1"]
    assert report.overbroad_domains == []


# -- seed_anchors (§2a amendment): durable membership via entity anchors, resolved via
# reader.resolve(desc).community exactly like a decision's own Tier-1 anchor -------------


def _seed_anchor_graph(entity_community, other_community="other"):
    """A two-community graph: "OrderBook" @ trader/order_book.py in `entity_community`,
    plus an unrelated node elsewhere so a single-community claim never trips the refresh
    cap (only one community "matters", but the graph has >= 2 total)."""
    return {
        "built_at_commit": "v1",
        "nodes": [
            {
                "id": "anchor-node",
                "label": "OrderBook",
                "norm_label": "orderbook",
                "file_type": "code",
                "source_file": "trader/order_book.py",
                "community": entity_community,
            },
            {
                "id": "other-node",
                "label": "Helper",
                "norm_label": "helper",
                "file_type": "code",
                "source_file": "util/helper.py",
                "community": other_community,
            },
        ],
        "links": [],
    }


def test_seed_anchor_resolves_communities_with_no_path_prefixes(tmp_path):
    store = Store(tmp_path / "t.db")
    anchor = Descriptor(name="OrderBook", file_path="trader/order_book.py")
    _accept(
        store,
        Domain(
            slug="order-book",
            title="Order Book",
            summary="s",
            seed_anchors=[anchor],  # NO path_prefixes -- seed_anchors is the only rule
            provenance=Provenance(source="manual"),
        ),
    )
    reader = _reader(tmp_path, "g.json", _seed_anchor_graph("40"))
    report = sync(store, reader)

    d = store.find_domain_by_slug("order-book")
    assert d.communities == ["40"]
    assert report.domains_refreshed == 1
    assert report.empty_domains == []


def test_seed_anchor_follows_entity_across_leiden_renumber(tmp_path):
    """Durability, not just resolution: the SAME entity (same name+file) simulates a
    Leiden renumber by landing in a completely different community id on rebuild --
    seed_anchors follows the entity there, unlike a raw `communities` id which would just
    evaporate (the bug this amendment fixes)."""
    store = Store(tmp_path / "t.db")
    anchor = Descriptor(name="OrderBook", file_path="trader/order_book.py")
    _accept(
        store,
        Domain(
            slug="order-book",
            title="Order Book",
            summary="s",
            seed_anchors=[anchor],
            provenance=Provenance(source="manual"),
        ),
    )
    reader_v1 = _reader(tmp_path, "g1.json", _seed_anchor_graph("40"))
    sync(store, reader_v1)
    assert store.find_domain_by_slug("order-book").communities == ["40"]

    # Leiden renumbers on rebuild: same entity, new community id.
    graph_v2 = _seed_anchor_graph("99")
    graph_v2["built_at_commit"] = "v2"
    reader_v2 = _reader(tmp_path, "g2.json", graph_v2)
    report = sync(store, reader_v2)

    d = store.find_domain_by_slug("order-book")
    assert d.communities == ["99"]  # durable -- followed the entity, not the stale id
    assert report.domains_refreshed == 1


def test_seed_anchor_unresolved_abstains_and_falls_back_to_survivors(tmp_path):
    """The anchored entity is gone entirely (renamed/deleted, no successor anywhere) --
    never-guess: the domain falls back to whatever of its OLD `communities` still survive,
    exactly like an unmatched path_prefixes rule does today."""
    store = Store(tmp_path / "t.db")
    anchor = Descriptor(name="NoSuchEntity", file_path="nowhere/x.py")
    _accept(
        store,
        Domain(
            slug="ghost",
            title="Ghost",
            summary="s",
            communities=["1"],  # stale baseline; "1" survives in the fixture below
            seed_anchors=[anchor],
            provenance=Provenance(source="manual"),
        ),
    )
    graph = {
        "built_at_commit": "v1",
        "nodes": [
            {
                "id": "n1",
                "label": "Alpha",
                "norm_label": "alpha",
                "file_type": "code",
                "source_file": "a.py",
                "community": 1,
            },
        ],
        "links": [],
    }
    reader = _reader(tmp_path, "g.json", graph)
    report = sync(store, reader)

    d = store.find_domain_by_slug("ghost")
    assert d.communities == ["1"]  # survivor kept; never guessed a replacement
    assert report.domains_refreshed == 0


def test_seed_anchor_and_path_prefixes_union_together(tmp_path):
    """A domain authored with BOTH a path_prefixes stabilizer AND a seed_anchor covering a
    DIFFERENT community -- the recomputed set is the union of both rules' direct evidence,
    not just one or the other. Padded with filler communities (>= 10 total) so a
    2-community claim sits comfortably under the 20% refresh cap -- this test is about
    union behavior, not the cap."""
    store = Store(tmp_path / "t.db")
    anchor = Descriptor(name="OrderBook", file_path="trader/order_book.py")
    _accept(
        store,
        Domain(
            slug="combined",
            title="Combined",
            summary="s",
            path_prefixes=["payments"],
            seed_anchors=[anchor],
            provenance=Provenance(source="manual"),
        ),
    )
    nodes = [
        {
            "id": "p1",
            "label": "Pay",
            "norm_label": "pay",
            "file_type": "code",
            "source_file": "payments/a.py",
            "community": 5,
        },
        {
            "id": "anchor-node",
            "label": "OrderBook",
            "norm_label": "orderbook",
            "file_type": "code",
            "source_file": "trader/order_book.py",
            "community": 6,
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
    graph = {"built_at_commit": "v1", "nodes": nodes, "links": []}
    reader = _reader(tmp_path, "g.json", graph)
    report = sync(store, reader)

    d = store.find_domain_by_slug("combined")
    assert d.communities == ["5", "6"]
    assert report.domains_refreshed == 1


# -- fix: an overbroad path_prefixes rule must not zero out a domain's precise seed_anchors
# (found dogfooding on a big C++ monorepo -- a domain with BOTH `path_prefixes=["src"]`
# (broad: matches most communities) AND `seed_anchors` (precise, hand-picked entities) came
# back with EMPTY communities after sync, because the old code unioned path-matched +
# anchor-resolved communities FIRST and applied `_DOMAIN_CLAIM_CAP` to the whole union --
# the broad path dragged the deliberate, precise anchors down with it. The cap exists to
# stop a FUZZY path rule from silently claiming half the graph; it must never discard
# PRECISE, hand-authored seed_anchors, which are as trustworthy as a hand-picked list. -----


def test_broad_path_with_seed_anchors_keeps_anchor_membership_and_flags_overbroad(tmp_path):
    """The reported bug, reproduced at small scale: `path_prefixes=["src"]` matches 3 of 10
    communities (30% > the 20% cap) while `seed_anchors` resolve to 2 OTHER communities. The
    cap must reject only the path contribution -- the domain ends up with EXACTLY the
    anchor-resolved communities, never empty, and the path rule is still flagged overbroad
    (the human should still know their path rule is too wide, even though anchors saved
    membership this time)."""
    store = Store(tmp_path / "t.db")
    anchors = [
        Descriptor(name="AnchorOne", file_path="include/anchor_one.h"),
        Descriptor(name="AnchorTwo", file_path="include/anchor_two.h"),
    ]
    _accept(
        store,
        Domain(
            slug="cpp-core",
            title="Cpp Core",
            summary="s",
            path_prefixes=["src"],
            seed_anchors=anchors,
            provenance=Provenance(source="manual"),
        ),
    )
    # 3 communities under src/ (broad), 2 anchor communities (precise), 5 filler -- 10 total.
    nodes = (
        [
            {
                "id": f"src-{i}",
                "label": f"SrcThing{i}",
                "norm_label": f"srcthing{i}",
                "file_type": "code",
                "source_file": f"src/thing_{i}.cpp",
                "community": f"s{i}",
            }
            for i in range(3)
        ]
        + [
            {
                "id": "anchor-1",
                "label": "AnchorOne",
                "norm_label": "anchorone",
                "file_type": "code",
                "source_file": "include/anchor_one.h",
                "community": "a1",
            },
            {
                "id": "anchor-2",
                "label": "AnchorTwo",
                "norm_label": "anchortwo",
                "file_type": "code",
                "source_file": "include/anchor_two.h",
                "community": "a2",
            },
        ]
        + [
            {
                "id": f"filler-{i}",
                "label": f"Filler{i}",
                "norm_label": f"filler{i}",
                "file_type": "code",
                "source_file": f"misc/f{i}.py",
                "community": f"m{i}",
            }
            for i in range(5)
        ]
    )
    graph = {"built_at_commit": "v1", "nodes": nodes, "links": []}
    reader = _reader(tmp_path, "g.json", graph)
    report = sync(store, reader)

    d = store.find_domain_by_slug("cpp-core")
    # Exactly the seed_anchor-resolved communities -- NOT empty, and NOT diluted by the
    # (rejected) path contribution.
    assert d.communities == ["a1", "a2"]
    assert report.domains_refreshed == 1
    assert report.empty_domains == []
    # The path rule is still flagged overbroad -- worth telling the human -- even though the
    # anchors rescued membership. matched=3 (communities the path claimed), total=10.
    assert report.overbroad_domains == [
        {"slug": "cpp-core", "title": "Cpp Core", "matched": 3, "total": 10},
    ]


def test_seed_anchors_only_domain_never_capped_regardless_of_spread(tmp_path):
    """seed_anchors are precise, per-entity authoring -- never subject to
    `_DOMAIN_CLAIM_CAP`, no matter how many communities they resolve to. Here 3 anchors
    resolve to 3 of 10 communities (30% > 20%, which WOULD trip the cap if it applied) and
    there is no path_prefixes rule at all."""
    store = Store(tmp_path / "t.db")
    anchors = [
        Descriptor(name="AnchorOne", file_path="a/one.py"),
        Descriptor(name="AnchorTwo", file_path="a/two.py"),
        Descriptor(name="AnchorThree", file_path="a/three.py"),
    ]
    _accept(
        store,
        Domain(
            slug="wide-anchors",
            title="Wide Anchors",
            summary="s",
            seed_anchors=anchors,  # no path_prefixes
            provenance=Provenance(source="manual"),
        ),
    )
    nodes = [
        {
            "id": f"anchor-{i}",
            "label": name,
            "norm_label": name.lower(),
            "file_type": "code",
            "source_file": f"a/{name.split('Anchor')[1].lower()}.py",
            "community": f"a{i}",
        }
        for i, name in enumerate(["AnchorOne", "AnchorTwo", "AnchorThree"])
    ] + [
        {
            "id": f"filler-{i}",
            "label": f"Filler{i}",
            "norm_label": f"filler{i}",
            "file_type": "code",
            "source_file": f"misc/f{i}.py",
            "community": f"m{i}",
        }
        for i in range(7)
    ]
    graph = {"built_at_commit": "v1", "nodes": nodes, "links": []}
    reader = _reader(tmp_path, "g.json", graph)
    report = sync(store, reader)

    d = store.find_domain_by_slug("wide-anchors")
    assert d.communities == ["a0", "a1", "a2"]
    assert report.domains_refreshed == 1
    assert report.overbroad_domains == []  # anchors alone are never subject to the cap


# -- sync-clean regression (CLAUDE.md invariant #1 extended): resolving seed_anchors is a
# volatile (index-only) refresh -- the committed domains/<id>.json must never be touched --


def _snapshot_canonical_dir(store: Store) -> dict[str, tuple[bytes, int, int]]:
    """relpath -> (content, size, mtime_ns) for every canonical (git-committed) file --
    mirrors test_sync_clean.py's own helper (duplicated on purpose: keeps this file's
    seed_anchors regression self-contained, same convention _ratified_domain uses across
    cli.py/server.py)."""
    out: dict[str, tuple[bytes, int, int]] = {}
    for sub in ("decisions", "domains", "entities", "bindings", "initiatives"):
        d = store.path / sub
        if not d.is_dir():
            continue
        for f in d.iterdir():
            if f.suffix != ".json":
                continue
            st = f.stat()
            out[f"{sub}/{f.name}"] = (f.read_bytes(), st.st_size, st.st_mtime_ns)
    return out


def test_seed_anchor_domain_sync_never_touches_canonical_files(tmp_path):
    store = Store(tmp_path / "t.db")
    anchor = Descriptor(name="OrderBook", file_path="trader/order_book.py")
    _accept(
        store,
        Domain(
            slug="order-book",
            title="Order Book",
            summary="s",
            seed_anchors=[anchor],
            provenance=Provenance(source="manual"),
        ),
    )
    before = _snapshot_canonical_dir(store)
    assert before  # sanity: there IS something to protect

    reader = _reader(tmp_path, "g.json", _seed_anchor_graph("40"))
    report = sync(store, reader)

    # the mutation actually happened (not a vacuous pass)
    assert store.find_domain_by_slug("order-book").communities == ["40"]
    assert report.domains_refreshed == 1

    after = _snapshot_canonical_dir(store)
    assert after == before  # not one byte (or mtime) of a committed file moved
