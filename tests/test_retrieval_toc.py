"""``build_toc`` / ``render_toc`` — the SessionStart TOC precompute (see
docs/concepts/mind-model.md)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from sidegraph.engine.reader import GraphifyReader
from sidegraph.retrieval import build_toc, drill_down, render_toc, unratified_mistakes
from sidegraph.schema import (
    AnchorBinding,
    Decision,
    DecisionKind,
    DecisionStatus,
    Descriptor,
    Domain,
    DomainStatus,
    Entity,
    Initiative,
    Provenance,
    Scope,
)
from sidegraph.store import Store


def _domain(store: Store, slug: str, **overrides) -> Domain:
    base = dict(
        slug=slug,
        title=slug.title(),
        summary=f"{slug} summary",
        provenance=Provenance(source="manual"),
    )
    base.update(overrides)
    d = store.add_domain(Domain(**base))
    store.ratify_domains(accept=[d.domain_id])
    return store.get_domain(d.domain_id)


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


def _bind_entity(store: Store, record_id: str, entity_id: str, tier: int = 2) -> None:
    store.add_binding(
        AnchorBinding(record_id=record_id, entity_id=entity_id, tier=tier, status="live")
    )


def _entity_in_community(store: Store, name: str, file_path: str, community: str) -> Entity:
    """A concrete entity observed (as of the last sync) in ``community`` -- the membership
    signal a domain's decisions are joined on (``Entity.last_seen_community``)."""
    return store.upsert_entity(
        Entity(
            canonical_name=name,
            descriptor=Descriptor(name=name, file_path=file_path),
            last_seen_community=community,
        )
    )


def _bind_domain(store: Store, record_id: str, slug: str, status: str = "live") -> None:
    entity = store.find_abstract_entity(f"domain:{slug}")
    store.add_binding(
        AnchorBinding(
            record_id=record_id,
            entity_id=entity.entity_id,
            tier=1,
            status=status,
        )
    )


# -- build_toc ----------------------------------------------------------------------


def test_build_toc_empty_store(tmp_path):
    store = Store(tmp_path / "t.db")
    toc = build_toc(store, None)
    assert toc == {"domains": [], "initiatives": [], "global_mistakes": []}


def test_build_toc_reader_is_optional(tmp_path):
    """``reader`` is optional (it only adds the document branch of the mistake count) --
    callers that have no reader on hand (e.g. the ratify surfaces) must be able to omit it."""
    store = Store(tmp_path / "t.db")
    assert build_toc(store) == build_toc(store, None)


def test_build_toc_domain_shape_and_mistake_count(tmp_path):
    store = Store(tmp_path / "t.db")
    _domain(store, "payments", summary="Handles settlement.")

    mistake = _decision(
        store,
        DecisionKind.GOTCHA,
        title="watch the retry loop",
        status=DecisionStatus.ACCEPTED,
    )
    _bind_domain(store, mistake.id, "payments")
    adr = _decision(store, DecisionKind.ADR, title="use postgres")
    _bind_domain(store, adr.id, "payments")  # ADR is not a mistake kind

    toc = build_toc(store, None)
    assert len(toc["domains"]) == 1
    entry = toc["domains"][0]
    assert entry["slug"] == "payments"
    assert entry["title"] == "Payments"
    assert entry["summary"] == "Handles settlement."
    assert entry["parent_slug"] is None
    assert entry["mistakes"] == 1
    assert entry["subdomains"] == 0


def test_build_toc_excludes_superseded_and_orphaned_bindings_from_mistake_count(tmp_path):
    store = Store(tmp_path / "t.db")
    _domain(store, "payments")

    stale = _decision(store, DecisionKind.GOTCHA, title="old gotcha")
    _bind_domain(store, stale.id, "payments", status="orphaned")

    toc = build_toc(store, None)
    assert toc["domains"][0]["mistakes"] == 0


def test_build_toc_counts_a_mistake_anchored_inside_the_domains_community(tmp_path):
    """The count is what ``drill_down`` would serve, not only what is tagged to the domain.
    Most captured mistakes anchor to a code entity, not to ``domain:<slug>``, so a count of
    the tagged ones alone read "0 mistake(s)" over a domain that holds several."""
    store = Store(tmp_path / "t.db")
    domain = _domain(store, "trading", communities=["1"])
    assert domain.status.value == "accepted"  # a proposed domain would give 0 on both sides
    ent = _entity_in_community(store, "Trader", "trader/exec.py", "1")
    mistake = _decision(
        store, DecisionKind.GOTCHA, title="fills arrive twice", status=DecisionStatus.ACCEPTED
    )
    _bind_entity(store, mistake.id, ent.entity_id)

    toc = build_toc(store, None)
    assert [(d["slug"], d["mistakes"]) for d in toc["domains"]] == [("trading", 1)]


def _write_graph(tmp_path, nodes) -> GraphifyReader:
    graph_path = tmp_path / "g.json"
    graph_path.write_text(json.dumps({"built_at_commit": "abc", "nodes": nodes, "links": []}))
    return GraphifyReader(graph_path)


def _accepted_mistakes_served_by_drill_down(
    store: Store, reader: GraphifyReader | None, slug: str
) -> int:
    served = drill_down(slug, store, reader)
    assert served["found"] is True
    decisions = [store.get_decision(i) for i in served["decision_ids"]]
    return sum(
        1
        for d in decisions
        if d is not None
        and d.status == DecisionStatus.ACCEPTED
        and d.kind in (DecisionKind.GOTCHA, DecisionKind.CONSTRAINT, DecisionKind.LESSON)
    )


def test_build_toc_count_equals_the_accepted_mistakes_drill_down_serves(tmp_path):
    """One number, two surfaces: for every accepted domain, the TOC count equals the accepted
    mistakes in ``drill_down``'s result, all three branches of the union included (tagged to
    the domain, anchored inside its communities, anchored to a document it covers), and with
    the same reader on both sides -- the document branch needs one."""
    reader = _write_graph(
        tmp_path,
        [
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
        ],
    )
    store = Store(tmp_path / "t.db")
    trading = _domain(store, "trading", communities=["1"])
    docs = _domain(store, "adr-domain", communities=["5"])
    quiet = _domain(store, "quiet", communities=["7"])
    assert {d.slug for d in (trading, docs, quiet)} == {
        d.slug for d in store.iter_domains(status=DomainStatus.ACCEPTED)
    }

    accepted = DecisionStatus.ACCEPTED
    tagged = _decision(store, DecisionKind.LESSON, title="tagged lesson", status=accepted)
    _bind_domain(store, tagged.id, "trading")
    ent = _entity_in_community(store, "Trader", "trader/exec.py", "1")
    anchored = _decision(store, DecisionKind.GOTCHA, title="anchored gotcha", status=accepted)
    _bind_entity(store, anchored.id, ent.entity_id)
    _bind_entity(store, tagged.id, ent.entity_id)  # reachable twice, counted once
    adr = _decision(store, DecisionKind.CONSTRAINT, title="adr constraint", status=accepted)
    doc_entity = store.upsert_entity(
        Entity(
            canonical_name="ADR-005.md",
            descriptor=Descriptor(name="ADR-005.md", file_path="docs/ADR-005.md"),
            last_seen_node_id="adr_file",
            last_seen_community="9",
        )
    )
    _bind_entity(store, adr.id, doc_entity.entity_id)
    not_a_mistake = _decision(store, DecisionKind.ADR, title="plain adr", status=accepted)
    _bind_entity(store, not_a_mistake.id, ent.entity_id)

    toc = build_toc(store, reader)
    counts = {d["slug"]: d["mistakes"] for d in toc["domains"]}
    served = {
        slug: _accepted_mistakes_served_by_drill_down(store, reader, slug)
        for slug in ("trading", "adr-domain", "quiet")
    }
    assert counts == served
    assert counts == {"trading": 2, "adr-domain": 1, "quiet": 0}  # both sides are not just 0


def test_build_toc_count_skips_proposed_and_superseded_mistakes(tmp_path):
    """Only a ratified, still-valid mistake is counted. A proposal is not yet team memory,
    and a superseded one was abandoned: counting either would promise a gotcha that
    ``drill_down`` ranks as unratified or no longer shows."""
    store = Store(tmp_path / "t.db")
    _domain(store, "trading", communities=["1"])
    ent = _entity_in_community(store, "Trader", "trader/exec.py", "1")

    counted = _decision(
        store, DecisionKind.GOTCHA, title="ratified gotcha", status=DecisionStatus.ACCEPTED
    )
    proposed = _decision(store, DecisionKind.GOTCHA, title="still a proposal")
    old = _decision(store, DecisionKind.LESSON, title="old lesson", status=DecisionStatus.ACCEPTED)
    _decision(
        store,
        DecisionKind.LESSON,
        title="its successor",
        status=DecisionStatus.ACCEPTED,
        supersedes=old.id,
    )
    for d in (counted, proposed, old):
        _bind_entity(store, d.id, ent.entity_id)

    assert proposed.status == DecisionStatus.PROPOSED
    assert store.get_decision(old.id).status == DecisionStatus.SUPERSEDED
    toc = build_toc(store, None)
    assert toc["domains"][0]["mistakes"] == 1


def test_unratified_mistakes_holds_only_the_proposed_global_mistakes_build_toc_lists(tmp_path):
    """SessionStart reads this list live instead of building the whole TOC for it, so it has
    to be exactly that TOC section: proposed global records of a MISTAKE kind (gotcha, lesson,
    constraint), not every proposal in the global scope."""
    store = Store(tmp_path / "t.db")
    _decision(store, DecisionKind.GOTCHA, title="proposed gotcha", scope=Scope.GLOBAL)
    _decision(store, DecisionKind.ADR, title="proposed adr", scope=Scope.GLOBAL)
    _decision(
        store,
        DecisionKind.CONSTRAINT,
        title="accepted constraint",
        scope=Scope.GLOBAL,
        status=DecisionStatus.ACCEPTED,
    )

    lines = unratified_mistakes(store)

    assert len(lines) == 1
    assert "proposed gotcha" in lines[0]
    assert not any("proposed adr" in line or "accepted constraint" in line for line in lines)
    assert lines == build_toc(store).get("unratified", [])


def test_build_toc_parent_slug_and_subdomain_counts(tmp_path):
    store = Store(tmp_path / "t.db")
    root = _domain(store, "root")
    _domain(store, "child-a", parent_id=root.domain_id)
    _domain(store, "child-b", parent_id=root.domain_id)

    # a proposed (unratified) child must not count toward subdomains
    store.add_domain(
        Domain(
            slug="child-c",
            title="Child C",
            summary="s",
            parent_id=root.domain_id,
            provenance=Provenance(source="manual"),
        )
    )

    toc = build_toc(store, None)
    by_slug = {d["slug"]: d for d in toc["domains"]}
    assert by_slug["root"]["subdomains"] == 2
    assert by_slug["root"]["parent_slug"] is None
    assert by_slug["child-a"]["parent_slug"] == "root"
    assert by_slug["child-a"]["subdomains"] == 0


def test_build_toc_only_accepted_domains_included(tmp_path):
    store = Store(tmp_path / "t.db")
    _domain(store, "accepted-one")
    store.add_domain(
        Domain(
            slug="still-proposed",
            title="Still Proposed",
            summary="s",
            provenance=Provenance(source="manual"),
        )
    )

    toc = build_toc(store, None)
    assert {d["slug"] for d in toc["domains"]} == {"accepted-one"}


def test_build_toc_initiatives_and_global_mistakes(tmp_path):
    store = Store(tmp_path / "t.db")
    store.upsert_initiative(Initiative(name="Trading Core", description="core exec path"))
    _decision(
        store,
        DecisionKind.CONSTRAINT,
        title="never block the event loop",
        scope=Scope.GLOBAL,
        status=DecisionStatus.ACCEPTED,
    )
    _decision(store, DecisionKind.ADR, title="not a mistake", scope=Scope.GLOBAL)

    toc = build_toc(store, None)
    assert toc["initiatives"] == [{"name": "Trading Core", "description": "core exec path"}]
    assert len(toc["global_mistakes"]) == 1
    assert "never block the event loop" in toc["global_mistakes"][0]


# -- render_toc ---------------------------------------------------------------------


def test_render_toc_renders_domain_title_summary_and_mistake_count():
    cache = {
        "domains": [
            {
                "slug": "payments",
                "title": "Payments",
                "summary": "Handles settlement.",
                "parent_slug": None,
                "mistakes": 2,
                "subdomains": 0,
            },
        ],
        "initiatives": [],
        "global_mistakes": [],
    }
    text = render_toc(cache)
    assert "## Domains" in text
    assert "Payments" in text
    assert "Handles settlement." in text
    assert "(2 mistake(s))" in text
    # the standing get_task_context instruction now lives at host.hooks.session_start,
    # prepended once regardless of which renderer produced the rest of the text -- this
    # renderer itself only needs to still emit its own header.
    assert "# Sidegraph — project memory" in text


def test_render_toc_includes_subdomain_count_when_present():
    cache = {
        "domains": [
            {
                "slug": "root",
                "title": "Root",
                "summary": "s",
                "parent_slug": None,
                "mistakes": 0,
                "subdomains": 2,
            },
        ],
        "initiatives": [],
        "global_mistakes": [],
    }
    text = render_toc(cache)
    assert "· 2 subdomains" in text


def test_render_toc_omits_subdomain_count_when_zero():
    cache = {
        "domains": [
            {
                "slug": "leaf",
                "title": "Leaf",
                "summary": "s",
                "parent_slug": None,
                "mistakes": 0,
                "subdomains": 0,
            },
        ],
        "initiatives": [],
        "global_mistakes": [],
    }
    text = render_toc(cache)
    assert "subdomains" not in text


def test_render_toc_subdomain_count_from_build_toc_parent_child(tmp_path):
    store = Store(tmp_path / "t.db")
    root = _domain(store, "root")
    _domain(store, "child-a", parent_id=root.domain_id)

    toc = build_toc(store, None)
    text = render_toc(toc)
    root_line = next(line for line in text.splitlines() if line.startswith("- Root"))
    assert "· 1 subdomains" in root_line


def test_render_toc_truncates_long_summary():
    long_summary = "x" * 200
    cache = {
        "domains": [
            {
                "slug": "s",
                "title": "S",
                "summary": long_summary,
                "parent_slug": None,
                "mistakes": 0,
                "subdomains": 0,
            },
        ],
        "initiatives": [],
        "global_mistakes": [],
    }
    text = render_toc(cache)
    assert long_summary not in text  # the full 200-char summary never renders
    assert "…" in text


def test_render_toc_renders_initiatives_and_global_mistakes():
    cache = {
        "domains": [],
        "initiatives": [{"name": "Trading Core", "description": "core exec path"}],
        "global_mistakes": ["- [gotcha] watch for stale cache"],
    }
    text = render_toc(cache)
    assert "## Domains" not in text  # no domains -> section omitted
    assert "Trading Core" in text
    assert "core exec path" in text
    assert "watch for stale cache" in text


def test_render_toc_empty_cache_still_has_header():
    text = render_toc({"domains": [], "initiatives": [], "global_mistakes": []})
    assert "# Sidegraph — project memory" in text
    assert "## Domains" not in text


def test_render_toc_dedupes_when_summary_exactly_equals_title():
    """Label-bootstrapped domains sometimes have summary == title exactly (both derive
    from the same engine label before any real digest exists) -- render_toc drops the
    redundant "— summary" tail rather than repeat the title verbatim."""
    cache = {
        "domains": [
            {
                "slug": "widgets",
                "title": "Widgets",
                "summary": "Widgets",
                "parent_slug": None,
                "mistakes": 1,
                "subdomains": 0,
            },
        ],
        "initiatives": [],
        "global_mistakes": [],
    }
    text = render_toc(cache)
    line = next(line for line in text.splitlines() if line.startswith("- Widgets"))
    assert line == "- Widgets (1 mistake(s))"
    assert "—" not in line


def test_render_toc_keeps_both_parts_when_summary_only_starts_with_title():
    """A digest summary that merely STARTS WITH the title but continues with more
    information is NOT deduped -- only EXACT equality collapses to the title alone
    (unlike `_fmt_decision`'s startswith/prefix rule for decisions)."""
    cache = {
        "domains": [
            {
                "slug": "widgets",
                "title": "Widgets",
                "summary": "Widgets handles settlement",
                "parent_slug": None,
                "mistakes": 0,
                "subdomains": 0,
            },
        ],
        "initiatives": [],
        "global_mistakes": [],
    }
    text = render_toc(cache)
    line = next(line for line in text.splitlines() if line.startswith("- Widgets"))
    assert line == "- Widgets — Widgets handles settlement (0 mistake(s))"
