"""``drill_down`` — the Axis-1 drill-down operation (see
docs/concepts/mind-model.md)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from sidegraph.engine.reader import GraphifyReader
from sidegraph.retrieval import drill_down
from sidegraph.schema import (
    AnchorBinding,
    Decision,
    DecisionKind,
    Descriptor,
    Domain,
    Entity,
    Provenance,
)
from sidegraph.store import Store

FIXTURE = Path(__file__).parent / "fixtures" / "mini_graph.json"


def _domain(store: Store, slug: str, **overrides) -> Domain:
    base = dict(
        slug=slug,
        title=slug.title(),
        summary=f"{slug} summary",
        provenance=Provenance(source="manual"),
    )
    base.update(overrides)
    return store.add_domain(Domain(**base))


def _accept(store: Store, domain: Domain) -> Domain:
    store.ratify_domains(accept=[domain.domain_id])
    return store.get_domain(domain.domain_id)


def _decision(store: Store, kind: DecisionKind, **overrides) -> Decision:
    base = dict(
        title="t",
        kind=kind,
        context="c",
        choice="ch",
        valid_from=datetime.now(UTC),
        provenance=Provenance(source="manual"),
    )
    base.update(overrides)
    return store.add_decision(Decision(**base))


def _bind_domain(store: Store, record_id: str, slug: str) -> None:
    entity = store.find_abstract_entity(f"domain:{slug}")
    store.add_binding(
        AnchorBinding(
            record_id=record_id,
            entity_id=entity.entity_id,
            tier=1,
            status="live",
        )
    )


def _entity_in_community(store: Store, name: str, file_path: str, community: str) -> Entity:
    """A concrete code/doc entity observed (as of the last sync) in ``community`` — the
    membership signal ``drill_down``'s community join keys on (``last_seen_community``, the
    same sync-refreshed mapping ``domain.communities`` itself comes from)."""
    return store.upsert_entity(
        Entity(
            canonical_name=name,
            descriptor=Descriptor(name=name, file_path=file_path),
            last_seen_community=community,
        )
    )


def _bind_entity(
    store: Store, record_id: str, entity_id: str, *, tier: int = 2, status: str = "live"
) -> None:
    store.add_binding(
        AnchorBinding(
            record_id=record_id,
            entity_id=entity_id,
            tier=tier,
            status=status,
        )
    )


# -- unknown slug -------------------------------------------------------------------------


def test_drill_down_unknown_slug_no_domains_at_all(tmp_path):
    store = Store(tmp_path / "t.db")
    out = drill_down("no-such-slug", store, None)
    assert out == {"found": False, "candidates": []}


def test_drill_down_unknown_slug_lists_accepted_candidates_sorted_and_capped(tmp_path):
    store = Store(tmp_path / "t.db")
    for i in range(12):
        _accept(store, _domain(store, f"domain-{i:02d}"))
    out = drill_down("nope", store, None)
    assert out["found"] is False
    assert len(out["candidates"]) == 10
    assert out["candidates"] == sorted(out["candidates"])
    assert out["candidates"][0] == "domain-00"


def test_drill_down_unknown_slug_candidates_exclude_proposed_and_dropped(tmp_path):
    store = Store(tmp_path / "t.db")
    proposed = _domain(store, "proposed-one")
    dropped = _domain(store, "dropped-one")
    store.ratify_domains(drop=[dropped.domain_id])
    accepted = _accept(store, _domain(store, "accepted-one"))

    out = drill_down("nope", store, None)
    assert out["candidates"] == [accepted.slug]
    assert proposed.slug not in out["candidates"]
    assert dropped.slug not in out["candidates"]


# -- found: domain shape -------------------------------------------------------------------


def test_drill_down_found_domain_shape(tmp_path):
    store = Store(tmp_path / "t.db")
    _accept(store, _domain(store, "payments", title="Payments", summary="Handles orders."))

    out = drill_down("payments", store, None)
    assert out["found"] is True
    assert out["domain"] == {
        "slug": "payments",
        "title": "Payments",
        "summary": "Handles orders.",
        "parent_slug": None,
        "status": "accepted",
    }


def test_drill_down_resolves_parent_slug(tmp_path):
    store = Store(tmp_path / "t.db")
    root = _accept(store, _domain(store, "root", title="Root"))
    _accept(store, _domain(store, "child", title="Child", parent_id=root.domain_id))

    out = drill_down("child", store, None)
    assert out["domain"]["parent_slug"] == "root"


def test_drill_down_still_found_for_proposed_domain_but_no_decisions(tmp_path):
    """A domain not yet accepted resolves (find_domain_by_slug excludes only superseded)
    but has no paired entity yet, so decisions/subdomains/members degrade to empty rather
    than erroring."""
    store = Store(tmp_path / "t.db")
    _domain(store, "not-yet")  # proposed, never accepted
    out = drill_down("not-yet", store, None)
    assert out["found"] is True
    assert out["decisions"] == []
    assert out["subdomains"] == []


# -- subdomains ------------------------------------------------------------------------


def test_drill_down_subdomains_accepted_only(tmp_path):
    store = Store(tmp_path / "t.db")
    root = _accept(store, _domain(store, "root", title="Root"))
    _accept(store, _domain(store, "child-a", title="Child A", parent_id=root.domain_id))
    _domain(store, "child-b", title="Child B", parent_id=root.domain_id)  # still proposed

    out = drill_down("root", store, None)
    assert [s["slug"] for s in out["subdomains"]] == ["child-a"]
    assert out["subdomains"][0]["title"] == "Child A"
    assert "summary" in out["subdomains"][0]


def test_drill_down_subdomains_sorted_by_slug(tmp_path):
    store = Store(tmp_path / "t.db")
    root = _accept(store, _domain(store, "root"))
    _accept(store, _domain(store, "zebra", parent_id=root.domain_id))
    _accept(store, _domain(store, "alpha", parent_id=root.domain_id))

    out = drill_down("root", store, None)
    assert [s["slug"] for s in out["subdomains"]] == ["alpha", "zebra"]


# -- members: reader absent / communities / path_prefixes -------------------------------


def test_drill_down_reader_none_members_empty_with_note(tmp_path):
    store = Store(tmp_path / "t.db")
    _accept(store, _domain(store, "payments", communities=["1"]))
    out = drill_down("payments", store, None)
    assert out["members"] == []
    assert "note" in out and "reader" in out["note"]
    # store-only fields still returned despite the missing reader
    assert out["domain"]["slug"] == "payments"


def test_drill_down_members_via_current_communities(tmp_path):
    store = Store(tmp_path / "t.db")
    reader = GraphifyReader(FIXTURE)
    _accept(store, _domain(store, "trading", communities=["1"]))

    out = drill_down("trading", store, reader)
    assert any("Trader" in m for m in out["members"])
    assert any("place_order" in m for m in out["members"])
    assert not any("helper" in m for m in out["members"])  # community "2", not claimed
    assert "note" not in out


def test_drill_down_members_via_path_prefixes(tmp_path):
    store = Store(tmp_path / "t.db")
    reader = GraphifyReader(FIXTURE)
    _accept(store, _domain(store, "trading", communities=[], path_prefixes=["trader"]))

    out = drill_down("trading", store, reader)
    assert any("Trader" in m for m in out["members"])
    assert not any("helper" in m for m in out["members"])


def test_drill_down_members_excludes_pathless_and_ranks_path_prefix_first(tmp_path):
    """Gate-5 finding 4: pathless nodes (Graphify's `source_file: ""` artifacts) must never
    appear in the member sample, and a member matching the domain's own `path_prefixes` must
    rank ahead of one that's only in the domain's community (both under the same cap)."""
    import json

    nodes = [
        {
            "id": "pathless",
            "label": "Anon",
            "norm_label": "anon",
            "file_type": "code",
            "source_file": "",
            "community": "5",
        },
        {
            "id": "other",
            "label": "Other",
            "norm_label": "other",
            "file_type": "code",
            "source_file": "misc/other.py",
            "community": "5",
        },
        {
            "id": "prefixed",
            "label": "Prefixed",
            "norm_label": "prefixed",
            "file_type": "code",
            "source_file": "trader/prefixed.py",
            "community": "5",
        },
    ]
    graph_path = tmp_path / "g.json"
    graph_path.write_text(json.dumps({"built_at_commit": "abc", "nodes": nodes, "links": []}))
    reader = GraphifyReader(graph_path)

    store = Store(tmp_path / "t.db")
    _accept(store, _domain(store, "trading", communities=["5"], path_prefixes=["trader"]))

    out = drill_down("trading", store, reader)
    members = out["members"]
    assert not any("Anon" in m for m in members)  # pathless dropped
    assert any("Other" in m for m in members)  # still a member via community
    prefixed_idx = next(i for i, m in enumerate(members) if "Prefixed" in m)
    other_idx = next(i for i, m in enumerate(members) if "Other" in m)
    assert prefixed_idx < other_idx  # path-prefix match ranks first


def test_drill_down_members_capped(tmp_path):
    import json

    nodes = [
        {
            "id": f"n{i}",
            "label": f"Sym{i}",
            "norm_label": f"sym{i}",
            "file_type": "code",
            "source_file": f"pkg/m{i}.py",
            "source_location": "L1",
            "community": "5",
        }
        for i in range(30)
    ]
    graph_path = tmp_path / "big.json"
    graph_path.write_text(json.dumps({"built_at_commit": "abc", "nodes": nodes, "links": []}))
    reader = GraphifyReader(graph_path)

    store = Store(tmp_path / "t.db")
    _accept(store, _domain(store, "bigdomain", communities=["5"]))

    out = drill_down("bigdomain", store, reader)
    assert len(out["members"]) == 20


# -- decisions: mistakes first ------------------------------------------------------------


def test_drill_down_decisions_mistakes_first(tmp_path):
    store = Store(tmp_path / "t.db")
    _accept(store, _domain(store, "payments"))
    adr = _decision(store, DecisionKind.ADR, title="use postgres")
    _bind_domain(store, adr.id, "payments")
    gotcha = _decision(store, DecisionKind.GOTCHA, title="watch the retry loop")
    _bind_domain(store, gotcha.id, "payments")

    out = drill_down("payments", store, None)
    assert len(out["decisions"]) == 2
    assert "watch the retry loop" in out["decisions"][0]
    assert "use postgres" in out["decisions"][1]


def test_drill_down_decisions_carry_id_suffix(tmp_path):
    """Staleness machinery D4: drill_down renders at the detailed tier deliberately (see
    the test above), so its lines carry the ``(id: ...)`` suffix too -- redundant with the
    out-of-band ``decision_ids`` field, but harmless-to-useful (design D4)."""
    store = Store(tmp_path / "t.db")
    _accept(store, _domain(store, "payments"))
    adr = _decision(store, DecisionKind.ADR, title="use postgres")
    _bind_domain(store, adr.id, "payments")

    out = drill_down("payments", store, None)
    assert f"(id: {adr.id})" in out["decisions"][0]


def test_drill_down_decisions_render_detailed_with_rejected_and_consequences(tmp_path):
    # Review follow-up (Minor 2): drill_down is an unbudgeted, explicit "go deeper" call —
    # it should render its decision lines at the generous/detailed tier (rejected +
    # consequences included), not the tight related one-liner that silently dropped them.
    store = Store(tmp_path / "t.db")
    _accept(store, _domain(store, "payments"))
    adr = _decision(
        store,
        DecisionKind.ADR,
        title="use postgres",
        rejected="Considered a NoSQL store but transactions were load-bearing.",
        consequences="Migrations now run through the standard postgres tooling.",
    )
    _bind_domain(store, adr.id, "payments")

    out = drill_down("payments", store, None)
    line = out["decisions"][0]
    assert "(rejected: " in line
    assert "transactions were load-bearing" in line
    assert "(consequences: " in line
    assert "standard postgres tooling" in line


def test_drill_down_decisions_exclude_orphaned_and_superseded(tmp_path):
    store = Store(tmp_path / "t.db")
    _accept(store, _domain(store, "payments"))
    stale = _decision(store, DecisionKind.GOTCHA, title="old gotcha")
    entity = store.find_abstract_entity("domain:payments")
    store.add_binding(
        AnchorBinding(
            record_id=stale.id,
            entity_id=entity.entity_id,
            tier=1,
            status="orphaned",
        )
    )

    out = drill_down("payments", store, None)
    assert out["decisions"] == []


# -- decisions: community-membership join (finding I) --------------------------------------


def test_drill_down_surfaces_decision_anchored_into_domain_community(tmp_path):
    """Finding I: a decision anchored to a code/doc entity living in one of the domain's
    communities — NOT tagged to the `domain:<slug>` entity — must surface under the domain
    (previously 0). This is the imported-ADR shape: memory anchors to the code/doc, not the
    domain abstraction."""
    store = Store(tmp_path / "t.db")
    _accept(store, _domain(store, "trading", communities=["1"]))
    ent = _entity_in_community(store, "Trader", "trader/exec.py", "1")
    d = _decision(store, DecisionKind.ADR, title="use event sourcing")
    _bind_entity(store, d.id, ent.entity_id)

    out = drill_down("trading", store, None)
    assert any("use event sourcing" in line for line in out["decisions"])


def test_drill_down_community_join_deduped_across_two_member_entities(tmp_path):
    """A single decision anchored to TWO entities that both live in the domain's communities
    appears exactly once."""
    store = Store(tmp_path / "t.db")
    _accept(store, _domain(store, "trading", communities=["1", "2"]))
    e1 = _entity_in_community(store, "Trader", "trader/exec.py", "1")
    e2 = _entity_in_community(store, "Helper", "util/misc.py", "2")
    d = _decision(store, DecisionKind.ADR, title="one shared decision")
    _bind_entity(store, d.id, e1.entity_id)
    _bind_entity(store, d.id, e2.entity_id)

    out = drill_down("trading", store, None)
    matches = [line for line in out["decisions"] if "one shared decision" in line]
    assert len(matches) == 1


def test_drill_down_community_join_deduped_with_domain_tag(tmp_path):
    """A decision reachable via BOTH the `domain:<slug>` tag AND the community join appears
    exactly once."""
    store = Store(tmp_path / "t.db")
    _accept(store, _domain(store, "trading", communities=["1"]))
    d = _decision(store, DecisionKind.ADR, title="both paths")
    _bind_domain(store, d.id, "trading")
    ent = _entity_in_community(store, "Trader", "trader/exec.py", "1")
    _bind_entity(store, d.id, ent.entity_id)

    out = drill_down("trading", store, None)
    matches = [line for line in out["decisions"] if "both paths" in line]
    assert len(matches) == 1


def test_drill_down_community_join_does_not_over_surface_other_domain(tmp_path):
    """Isolation: a decision anchored into a DIFFERENT domain's community must not appear
    under this one — only the domain that actually covers the community shows it."""
    store = Store(tmp_path / "t.db")
    _accept(store, _domain(store, "trading", communities=["1"]))
    _accept(store, _domain(store, "utils", communities=["2"]))
    ent = _entity_in_community(store, "Helper", "util/misc.py", "2")
    d = _decision(store, DecisionKind.ADR, title="utils only decision")
    _bind_entity(store, d.id, ent.entity_id)

    out_trading = drill_down("trading", store, None)
    assert not any("utils only decision" in line for line in out_trading["decisions"])
    out_utils = drill_down("utils", store, None)
    assert any("utils only decision" in line for line in out_utils["decisions"])


def test_drill_down_mistakes_first_across_tag_and_community_join(tmp_path):
    """The union is ordered the same way the domain-tagged path was: mistakes first, then
    the rest — across both sources."""
    store = Store(tmp_path / "t.db")
    _accept(store, _domain(store, "trading", communities=["1"]))
    adr = _decision(store, DecisionKind.ADR, title="tagged adr")
    _bind_domain(store, adr.id, "trading")
    ent = _entity_in_community(store, "Trader", "trader/exec.py", "1")
    gotcha = _decision(store, DecisionKind.GOTCHA, title="community gotcha")
    _bind_entity(store, gotcha.id, ent.entity_id)

    out = drill_down("trading", store, None)
    assert len(out["decisions"]) == 2
    assert "community gotcha" in out["decisions"][0]
    assert "tagged adr" in out["decisions"][1]


def test_drill_down_community_join_excludes_superseded(tmp_path):
    """Same validity filter as the tagged path: a superseded decision anchored into the
    community does not surface (its successor does)."""
    store = Store(tmp_path / "t.db")
    _accept(store, _domain(store, "trading", communities=["1"]))
    ent = _entity_in_community(store, "Trader", "trader/exec.py", "1")
    old = _decision(store, DecisionKind.ADR, title="old approach")
    _bind_entity(store, old.id, ent.entity_id)
    new = _decision(store, DecisionKind.ADR, title="new approach", supersedes=old.id)
    _bind_entity(store, new.id, ent.entity_id)

    out = drill_down("trading", store, None)
    assert not any("old approach" in line for line in out["decisions"])
    assert any("new approach" in line for line in out["decisions"])


def test_drill_down_community_join_excludes_orphaned_binding(tmp_path):
    """An orphaned binding to a community entity is skipped, exactly like the tagged path's
    orphaned-binding exclusion."""
    store = Store(tmp_path / "t.db")
    _accept(store, _domain(store, "trading", communities=["1"]))
    ent = _entity_in_community(store, "Trader", "trader/exec.py", "1")
    d = _decision(store, DecisionKind.GOTCHA, title="orphaned gotcha")
    _bind_entity(store, d.id, ent.entity_id, status="orphaned")

    out = drill_down("trading", store, None)
    assert not any("orphaned gotcha" in line for line in out["decisions"])


def test_drill_down_community_join_no_communities_is_noop(tmp_path):
    """A domain with no communities (e.g. path-prefix-only or proposed) surfaces only its
    domain-tagged decisions — the community join contributes nothing."""
    store = Store(tmp_path / "t.db")
    _accept(store, _domain(store, "trading", communities=[]))
    # an entity in some community with a decision — must NOT leak in without a covered community
    ent = _entity_in_community(store, "Trader", "trader/exec.py", "1")
    d = _decision(store, DecisionKind.ADR, title="uncovered decision")
    _bind_entity(store, d.id, ent.entity_id)

    out = drill_down("trading", store, None)
    assert out["decisions"] == []


# -- decisions: document-coverage join (doc-corpus gap) -------------------------------------
#
# Root cause (accuracy eval on a real ADR/spec corpus): doc-import anchors an ADR decision to
# the ADR file's OWN file-level node (`doc_import._file_node_descriptor` — a node whose name
# equals the file's basename). Graphify clusters ALL doc file-level nodes into one hub
# community, so that entity's `last_seen_community` is never among a domain's `communities` —
# those come from the doc's HEADING nodes, which live in per-doc communities. Branch (b)'s
# community join therefore misses the decision even though the domain plainly covers that
# document (via its headings). The fix: a decision anchored to a whole-FILE document entity
# surfaces when that file's path is covered by one of the domain's member nodes.


def _write_graph(tmp_path, nodes) -> GraphifyReader:
    import json

    graph_path = tmp_path / "g.json"
    graph_path.write_text(json.dumps({"built_at_commit": "abc", "nodes": nodes, "links": []}))
    return GraphifyReader(graph_path)


def _entity_with_node(
    store: Store, name: str, file_path: str, node_id: str, community: str | None = None
) -> Entity:
    """A concrete entity whose ``last_seen_node_id`` maps onto a real reader node — the
    document-branch's whole-file classification looks this id up via ``reader.get_node``
    (mirrors the same sync-refreshed field ``last_seen_community`` already uses)."""
    return store.upsert_entity(
        Entity(
            canonical_name=name,
            descriptor=Descriptor(name=name, file_path=file_path),
            last_seen_node_id=node_id,
            last_seen_community=community,
        )
    )


def _doc_corpus_nodes() -> list[dict]:
    """One ADR doc: a file-level DOCUMENT node in the hub community ("9", never claimed by
    any domain) plus a HEADING node from the same file in the domain's own community ("5")
    — exactly the shape a real doc corpus produces."""
    return [
        {
            "id": "adr_file",
            "label": "ADR-005.md",
            "norm_label": "adr-005.md",
            "file_type": "document",
            "source_file": "docs/ADR-005.md",
            "community": "9",
        },
        {
            "id": "adr_heading",
            "label": "ADR-005: Some Decision",
            "norm_label": "adr-005: some decision",
            "file_type": "document",
            "source_file": "docs/ADR-005.md",
            "community": "5",
        },
    ]


def test_drill_down_surfaces_document_decision_via_file_coverage(tmp_path):
    """The fix: a decision anchored to a document's OWN file-level node (hub community "9",
    not claimed by the domain) surfaces under a domain whose member nodes include a HEADING
    from that same file (community "5") — the document-coverage join."""
    reader = _write_graph(tmp_path, _doc_corpus_nodes())
    store = Store(tmp_path / "t.db")
    _accept(store, _domain(store, "adr-domain", communities=["5"]))
    ent = _entity_with_node(store, "ADR-005.md", "docs/ADR-005.md", "adr_file", community="9")
    d = _decision(store, DecisionKind.ADR, title="doc decision content")
    _bind_entity(store, d.id, ent.entity_id)

    out = drill_down("adr-domain", store, reader)
    assert any("doc decision content" in line for line in out["decisions"])


def test_drill_down_document_branch_does_not_over_surface_on_code_corpus(tmp_path):
    """The guard: a domain covering community "1" (fn_x's community — a domain member, so
    "code/a.py" IS a covered file_path) must NOT surface a decision anchored to fn_y, a
    DIFFERENT function in the SAME file but a DIFFERENT community ("2", uncovered). On a code
    corpus one file spans many communities/entities, so "the file is covered" alone must never
    be enough — only a whole-document anchor qualifies, and fn_y is file_type "code", not a
    document."""
    nodes = [
        {
            "id": "fn_x",
            "label": "fn_x",
            "norm_label": "fn_x",
            "file_type": "code",
            "source_file": "code/a.py",
            "community": "1",
        },
        {
            "id": "fn_y",
            "label": "fn_y",
            "norm_label": "fn_y",
            "file_type": "code",
            "source_file": "code/a.py",
            "community": "2",
        },
    ]
    reader = _write_graph(tmp_path, nodes)
    store = Store(tmp_path / "t.db")
    _accept(store, _domain(store, "trading-code", communities=["1"]))
    ent = _entity_with_node(store, "fn_y", "code/a.py", "fn_y", community="2")
    d = _decision(store, DecisionKind.ADR, title="fn_y only decision")
    _bind_entity(store, d.id, ent.entity_id)

    out = drill_down("trading-code", store, reader)
    assert not any("fn_y only decision" in line for line in out["decisions"])


def test_drill_down_document_branch_deduped_with_domain_tag(tmp_path):
    """A decision reachable via BOTH the domain tag (a) AND the document-coverage join (c)
    appears exactly once."""
    reader = _write_graph(tmp_path, _doc_corpus_nodes())
    store = Store(tmp_path / "t.db")
    _accept(store, _domain(store, "adr-domain", communities=["5"]))
    d = _decision(store, DecisionKind.ADR, title="tagged and doc-covered")
    _bind_domain(store, d.id, "adr-domain")
    ent = _entity_with_node(store, "ADR-005.md", "docs/ADR-005.md", "adr_file", community="9")
    _bind_entity(store, d.id, ent.entity_id)

    out = drill_down("adr-domain", store, reader)
    matches = [line for line in out["decisions"] if "tagged and doc-covered" in line]
    assert len(matches) == 1


def test_drill_down_document_branch_excludes_superseded(tmp_path):
    """Same validity filter as branches (a)/(b): a superseded document-anchored decision does
    not surface (its successor does)."""
    reader = _write_graph(tmp_path, _doc_corpus_nodes())
    store = Store(tmp_path / "t.db")
    _accept(store, _domain(store, "adr-domain", communities=["5"]))
    ent = _entity_with_node(store, "ADR-005.md", "docs/ADR-005.md", "adr_file", community="9")
    old = _decision(store, DecisionKind.ADR, title="old adr text")
    _bind_entity(store, old.id, ent.entity_id)
    new = _decision(store, DecisionKind.ADR, title="new adr text", supersedes=old.id)
    _bind_entity(store, new.id, ent.entity_id)

    out = drill_down("adr-domain", store, reader)
    assert not any("old adr text" in line for line in out["decisions"])
    assert any("new adr text" in line for line in out["decisions"])


def test_drill_down_document_branch_excludes_orphaned_binding(tmp_path):
    """An orphaned binding to a document entity is skipped, exactly like the tagged/community
    paths' orphaned-binding exclusion."""
    reader = _write_graph(tmp_path, _doc_corpus_nodes())
    store = Store(tmp_path / "t.db")
    _accept(store, _domain(store, "adr-domain", communities=["5"]))
    ent = _entity_with_node(store, "ADR-005.md", "docs/ADR-005.md", "adr_file", community="9")
    d = _decision(store, DecisionKind.GOTCHA, title="orphaned doc gotcha")
    _bind_entity(store, d.id, ent.entity_id, status="orphaned")

    out = drill_down("adr-domain", store, reader)
    assert not any("orphaned doc gotcha" in line for line in out["decisions"])


def test_drill_down_document_branch_skipped_without_reader_no_crash(tmp_path):
    """Branch (c) needs a live reader to compute the domain's covered file_paths and classify
    the anchor as whole-document; with ``reader=None`` it is skipped outright (no crash) and
    (a)/(b) keep working — the same reader=None contract the rest of drill_down documents."""
    store = Store(tmp_path / "t.db")
    _accept(store, _domain(store, "adr-domain", communities=["5"]))
    tagged = _decision(store, DecisionKind.ADR, title="tagged still works")
    _bind_domain(store, tagged.id, "adr-domain")
    ent = _entity_with_node(store, "ADR-005.md", "docs/ADR-005.md", "adr_file", community="9")
    doc_decision = _decision(store, DecisionKind.ADR, title="doc decision needs reader")
    _bind_entity(store, doc_decision.id, ent.entity_id)

    out = drill_down("adr-domain", store, None)
    assert any("tagged still works" in line for line in out["decisions"])
    assert not any("doc decision needs reader" in line for line in out["decisions"])
