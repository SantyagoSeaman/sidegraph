import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sidegraph.engine.reader import GraphifyReader
from sidegraph.retrieval import (
    RetrievalBudget,
    Seed,
    _fmt_node,
    _structure_fallback_lines,
    get_task_context,
    rank_decisions,
)
from sidegraph.schema import (
    AnchorBinding,
    Decision,
    DecisionKind,
    DecisionStatus,
    Descriptor,
    Domain,
    Entity,
    Fact,
    Provenance,
)
from sidegraph.store import Store

FIXTURE = Path(__file__).parent / "fixtures" / "mini_graph.json"

# A seed node with a source_file, linked to two pathless engine artifact nodes:
# - `art1` ("Any") has no `source_file` key at all (Graphify omits the key entirely for some
#   artifacts) -> NodeRef.file_path is None.
# - `art2` ("Exception") has `source_file: ""` (Graphify emits an *empty string*, not a missing
#   key, for others on real code corpora) -> NodeRef.file_path is "".
# Both shapes used to render as dead-weight `- Any (code)` / `- Exception (code)` lines in the
# structural map; both must be dropped identically.
PATHLESS_GRAPH = {
    "built_at_commit": "abc123",
    "nodes": [
        {
            "id": "seed1",
            "label": "Trader",
            "norm_label": "trader",
            "file_type": "code",
            "source_file": "trader/exec.py",
            "source_location": "L10",
            "community": "1",
        },
        {"id": "art1", "label": "Any", "norm_label": "any", "file_type": "code", "community": "1"},
        {
            "id": "art2",
            "label": "Exception",
            "norm_label": "exception",
            "file_type": "code",
            "source_file": "",
            "community": "1",
        },
    ],
    "links": [
        {"source": "seed1", "target": "art1", "relation": "uses"},
        {"source": "seed1", "target": "art2", "relation": "uses"},
    ],
}


def _pathless_reader(tmp_path):
    p = tmp_path / "pathless.json"
    p.write_text(json.dumps(PATHLESS_GRAPH))
    return GraphifyReader(p)


# Two same-community nodes with deliberately long names/lines (~141 chars each) so a
# structure_chars budget can fit the first leaf line but overflow on the second WITHOUT
# forcing node_cap (= structure_chars // 120) below 2 -- see get_task_context's budget
# fallback (§5 NFR2, "summaries instead of leaves").
OVERFLOW_GRAPH = {
    "built_at_commit": "abc123",
    "nodes": [
        {
            "id": "n1",
            "label": "A" * 120,
            "norm_label": "a",
            "file_type": "code",
            "source_file": "mod.py",
            "source_location": "L1",
            "community": "9",
        },
        {
            "id": "n2",
            "label": "B" * 120,
            "norm_label": "b",
            "file_type": "code",
            "source_file": "mod.py",
            "source_location": "L2",
            "community": "9",
        },
    ],
    "links": [{"source": "n1", "target": "n2", "relation": "calls"}],
}


def _overflow_reader(tmp_path):
    p = tmp_path / "overflow.json"
    p.write_text(json.dumps(OVERFLOW_GRAPH))
    return GraphifyReader(p)


def _bind_task_decision(store, entity, title, status, kind=DecisionKind.ADR):
    decision = store.add_decision(
        Decision(
            title=title,
            kind=kind,
            status=status,
            context="c",
            choice="ch",
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual"),
        )
    )
    store.add_binding(
        AnchorBinding(record_id=decision.id, entity_id=entity.entity_id, tier=2, status="live")
    )
    return decision


def _task_store_and_reader(tmp_path):
    store = Store(tmp_path / "trust.db")
    reader = GraphifyReader(FIXTURE)
    entity = store.upsert_entity(
        Entity(
            canonical_name="Trader",
            descriptor=Descriptor(name="Trader", file_path="trader/exec.py"),
        )
    )
    return store, reader, entity


def test_proposal_never_displaces_accepted_record_under_tight_budget(tmp_path):
    store, reader, entity = _task_store_and_reader(tmp_path)
    accepted = _bind_task_decision(store, entity, "Accepted", DecisionStatus.ACCEPTED)
    proposed = _bind_task_decision(
        store,
        entity,
        "Newer proposed gotcha",
        DecisionStatus.PROPOSED,
        kind=DecisionKind.GOTCHA,
    )

    ctx = get_task_context(
        [Seed(file_path="trader/exec.py")],
        store,
        reader,
        RetrievalBudget(memory_chars=120),
    )

    assert accepted.id in ctx.shown_ids
    assert proposed.id not in ctx.shown_ids


def test_proposed_gotcha_is_rendered_only_in_unratified_bucket(tmp_path):
    store, reader, entity = _task_store_and_reader(tmp_path)
    proposed = _bind_task_decision(
        store,
        entity,
        "Proposed",
        DecisionStatus.PROPOSED,
        kind=DecisionKind.GOTCHA,
    )

    ctx = get_task_context([Seed(file_path="trader/exec.py")], store, reader)

    assert all(proposed.id not in line for line in ctx.mistakes)
    assert any(proposed.id in line for line in ctx.unratified)
    assert "## Unratified proposals" in ctx.render()


def test_proposed_fact_cannot_spend_budget_needed_by_accepted_decision(tmp_path):
    store, reader, entity = _task_store_and_reader(tmp_path)
    accepted_gotcha = _bind_task_decision(
        store,
        entity,
        "Accepted gotcha",
        DecisionStatus.ACCEPTED,
        kind=DecisionKind.GOTCHA,
    )
    accepted = _bind_task_decision(store, entity, "Accepted", DecisionStatus.ACCEPTED)
    proposed = store.add_fact(
        Fact(
            statement="Newer proposed evidence",
            source="test",
            supports=[accepted_gotcha.id],
            status=DecisionStatus.PROPOSED,
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual"),
        )
    )
    store.add_binding(
        AnchorBinding(record_id=proposed.id, entity_id=entity.entity_id, tier=2, status="live")
    )

    ctx = get_task_context(
        [Seed(file_path="trader/exec.py")],
        store,
        reader,
        RetrievalBudget(memory_chars=160),
    )

    assert accepted.id in ctx.shown_ids
    assert proposed.id not in ctx.shown_ids


def test_partition_by_trust_preserves_input_order():
    from sidegraph import retrieval

    records = [
        Decision(
            title=title,
            kind=DecisionKind.ADR,
            status=status,
            context="c",
            choice="ch",
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual"),
        )
        for title, status in (
            ("Proposed one", DecisionStatus.PROPOSED),
            ("Accepted one", DecisionStatus.ACCEPTED),
            ("Proposed two", DecisionStatus.PROPOSED),
            ("Accepted two", DecisionStatus.ACCEPTED),
        )
    ]

    accepted, proposed = retrieval.partition_by_trust(records)

    assert [item.title for item in accepted] == ["Accepted one", "Accepted two"]
    assert [item.title for item in proposed] == ["Proposed one", "Proposed two"]


def test_unratified_facts_keep_direct_before_related_priority(tmp_path):
    store = Store(tmp_path / "fact-priority.db")
    direct = store.upsert_entity(Entity(canonical_name="Direct"))
    related = store.upsert_entity(Entity(canonical_name="Related"))
    old = datetime.now(UTC) - timedelta(days=1)
    facts = [
        (
            direct,
            Fact(
                statement="Older direct proposal",
                source="test",
                status=DecisionStatus.PROPOSED,
                valid_from=old,
                provenance=Provenance(source="manual"),
            ),
        ),
        (
            related,
            Fact(
                statement="Newer related proposal",
                source="test",
                status=DecisionStatus.PROPOSED,
                valid_from=datetime.now(UTC),
                provenance=Provenance(source="manual"),
            ),
        ),
    ]
    for entity, fact in facts:
        store.add_fact(fact)
        store.add_binding(
            AnchorBinding(record_id=fact.id, entity_id=entity.entity_id, tier=2, status="live")
        )

    ctx = rank_decisions([direct], [related], [], store, RetrievalBudget(memory_chars=1000))

    assert ctx.unratified[0].find("Older direct proposal") != -1
    assert ctx.unratified[1].find("Newer related proposal") != -1


def test_accepted_fact_supporting_proposal_precedes_unratified_memory(tmp_path):
    store = Store(tmp_path / "accepted-evidence.db")
    entity = store.upsert_entity(Entity(canonical_name="Seed"))
    proposal = _bind_task_decision(store, entity, "Proposal", DecisionStatus.PROPOSED)
    fact = store.add_fact(
        Fact(
            statement="Accepted evidence",
            source="test",
            supports=[proposal.id],
            status=DecisionStatus.ACCEPTED,
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual"),
        )
    )

    ctx = rank_decisions([entity], [], [], store, RetrievalBudget(memory_chars=1000))

    assert fact.id in ctx.shown_ids
    assert any("Accepted evidence" in line for line in ctx.facts)
    assert ctx.render().index("Accepted evidence") < ctx.render().index("Proposal")


def test_unratified_mixed_records_are_newest_first_within_direct_partition(tmp_path):
    store = Store(tmp_path / "mixed-priority.db")
    direct = store.upsert_entity(Entity(canonical_name="Direct"))
    related = store.upsert_entity(Entity(canonical_name="Related"))
    now = datetime.now(UTC)
    direct_decision = store.add_decision(
        Decision(
            title="Old direct decision",
            kind=DecisionKind.ADR,
            status=DecisionStatus.PROPOSED,
            context="c",
            choice="ch",
            valid_from=now - timedelta(days=2),
            provenance=Provenance(source="manual"),
        )
    )
    direct_fact = store.add_fact(
        Fact(
            statement="New direct fact",
            source="test",
            status=DecisionStatus.PROPOSED,
            valid_from=now - timedelta(days=1),
            provenance=Provenance(source="manual"),
        )
    )
    related_decision = store.add_decision(
        Decision(
            title="Newest related decision",
            kind=DecisionKind.ADR,
            status=DecisionStatus.PROPOSED,
            context="c",
            choice="ch",
            valid_from=now,
            provenance=Provenance(source="manual"),
        )
    )
    for record, entity in (
        (direct_decision, direct),
        (direct_fact, direct),
        (related_decision, related),
    ):
        store.add_binding(
            AnchorBinding(record_id=record.id, entity_id=entity.entity_id, tier=2, status="live")
        )

    ctx = rank_decisions([direct], [related], [], store, RetrievalBudget(memory_chars=1000))

    assert "New direct fact" in ctx.unratified[0]
    assert "Old direct decision" in ctx.unratified[1]
    assert "Newest related decision" in ctx.unratified[2]


def test_context_has_structure_and_seed_mistake(tmp_path):
    r = GraphifyReader(FIXTURE)
    s = Store(tmp_path / "t.db")
    e = s.upsert_entity(
        Entity(
            canonical_name="Trader",
            descriptor=Descriptor(name="Trader", file_path="trader/exec.py"),
        )
    )
    d = Decision(
        title="race",
        kind=DecisionKind.GOTCHA,
        status=DecisionStatus.ACCEPTED,
        context="c",
        choice="lock it",
        valid_from=datetime.now(UTC),
        provenance=Provenance(source="manual"),
    )
    s._write_decision(d)
    s._conn.commit()
    s.add_binding(AnchorBinding(record_id=d.id, entity_id=e.entity_id, tier=2, status="live"))
    ctx = get_task_context([Seed(name="Trader", file_path="trader/exec.py")], s, r)
    assert ctx.structure  # subgraph nodes present
    assert any("race" in m for m in ctx.mistakes)  # seed gotcha surfaced, mistakes-first
    rendered = ctx.render()
    assert rendered.index("race") < rendered.index("Structural map")


def test_context_no_reader_memory_only(tmp_path):
    s = Store(tmp_path / "t.db")
    ctx = get_task_context([Seed(file_path="trader/exec.py")], s, None)
    assert ctx.structure == []  # no reader -> no structure


def test_context_structure_budget_bounds_size(tmp_path):
    r = GraphifyReader(FIXTURE)
    s = Store(tmp_path / "t.db")
    ctx = get_task_context(
        [Seed(file_path="trader/exec.py")],
        s,
        r,
        RetrievalBudget(structure_chars=10, memory_chars=0),
    )
    assert sum(len(x) for x in ctx.structure) <= 10


def test_fmt_node_omits_missing_line():
    from sidegraph.engine.reader import NodeRef
    from sidegraph.retrieval import _fmt_node

    line = _fmt_node(
        NodeRef(node_id="x", name="f", norm_name="f", file_type="code", file_path="a.py", line=None)
    )
    assert line == "- f (code) [a.py]"
    assert "None" not in line


def test_structure_skips_pathless_nodes(tmp_path):
    r = _pathless_reader(tmp_path)
    s = Store(tmp_path / "t.db")
    ctx = get_task_context([Seed(file_path="trader/exec.py")], s, r)
    assert any("Trader" in line for line in ctx.structure)
    assert not any("Any" in line for line in ctx.structure)  # pathless (missing key) dropped
    assert not any("Exception" in line for line in ctx.structure)  # pathless ("") dropped too


def test_structure_budget_not_consumed_by_pathless_nodes(tmp_path):
    r = _pathless_reader(tmp_path)
    s = Store(tmp_path / "t.db")
    from sidegraph.engine.reader import NodeRef

    exact_len = len(
        _fmt_node(
            NodeRef(
                node_id="seed1",
                name="Trader",
                norm_name="trader",
                file_type="code",
                file_path="trader/exec.py",
                line="L10",
            )
        )
    )
    # Budget sized for exactly the one pathed line -- if the pathless node consumed any of it
    # (or broke the loop), the pathed line would be dropped too.
    ctx = get_task_context(
        [Seed(file_path="trader/exec.py")],
        s,
        r,
        RetrievalBudget(structure_chars=exact_len, memory_chars=0),
    )
    assert ctx.structure == ["- Trader (code) [trader/exec.py:L10]"]
    assert sum(len(x) for x in ctx.structure) <= exact_len


def test_pathless_peripheral_entity_still_feeds_decisions(tmp_path):
    """Structural-map rendering changes; seed-entity resolution and decision gathering must
    not -- a decision anchored to a pathless peripheral entity still surfaces in `related`,
    whether the node's file_path is None (missing `source_file` key) or "" (empty string)."""
    r = _pathless_reader(tmp_path)
    s = Store(tmp_path / "t.db")
    e_none = s.upsert_entity(
        Entity(canonical_name="Any", descriptor=Descriptor(name="Any", file_path=None))
    )
    e_empty = s.upsert_entity(
        Entity(canonical_name="Exception", descriptor=Descriptor(name="Exception", file_path=""))
    )
    d_none = Decision(
        title="any-note",
        kind=DecisionKind.ADR,
        status=DecisionStatus.ACCEPTED,
        context="c",
        choice="choice",
        valid_from=datetime.now(UTC),
        provenance=Provenance(source="manual"),
    )
    d_empty = Decision(
        title="exception-note",
        kind=DecisionKind.ADR,
        status=DecisionStatus.ACCEPTED,
        context="c",
        choice="choice",
        valid_from=datetime.now(UTC),
        provenance=Provenance(source="manual"),
    )
    s._write_decision(d_none)
    s._write_decision(d_empty)
    s._conn.commit()
    s.add_binding(
        AnchorBinding(record_id=d_none.id, entity_id=e_none.entity_id, tier=2, status="live")
    )
    s.add_binding(
        AnchorBinding(record_id=d_empty.id, entity_id=e_empty.entity_id, tier=2, status="live")
    )

    ctx = get_task_context([Seed(file_path="trader/exec.py")], s, r)
    assert not any("Any" in line for line in ctx.structure)  # still dropped from the map
    assert not any("Exception" in line for line in ctx.structure)  # "" dropped identically
    assert any("any-note" in line for line in ctx.related)  # but still resolved (None)
    assert any("exception-note" in line for line in ctx.related)  # and resolved ("") too


# -- structure-budget fallback: summaries instead of leaves (M5, spec §5 NFR2) -----------


def _accepted_domain(store, slug, communities, summary="s"):
    d = store.add_domain(
        Domain(
            slug=slug,
            title=slug.title(),
            summary=summary,
            communities=communities,
            provenance=Provenance(source="manual"),
        )
    )
    store.ratify_domains(accept=[d.domain_id])
    return store.get_domain(d.domain_id)


def test_structure_overflow_falls_back_to_domain_summary_when_covered(tmp_path):
    r = _overflow_reader(tmp_path)
    s = Store(tmp_path / "t.db")
    _accepted_domain(s, "payments", ["9"])
    ctx = get_task_context(
        [Seed(file_path="mod.py")],
        s,
        r,
        RetrievalBudget(structure_chars=250, memory_chars=0),
    )
    # the first (fitting) leaf line is unaffected...
    assert any(line.startswith("- A") for line in ctx.structure)
    # ...the overflowing second leaf never renders as a raw leaf line...
    assert not any(line.startswith("- B") for line in ctx.structure)
    # ...instead the domain covering its community fills the leftover budget.
    assert any("[domain] Payments" in line for line in ctx.structure)
    assert sum(len(x) for x in ctx.structure) <= 250


def test_structure_overflow_old_behavior_without_domains(tmp_path):
    """No accepted domain covers the overflow node's community -> byte-identical to
    pre-M5 truncation: the overflow is silently dropped, nothing appended."""
    r = _overflow_reader(tmp_path)
    s = Store(tmp_path / "t.db")
    ctx = get_task_context(
        [Seed(file_path="mod.py")],
        s,
        r,
        RetrievalBudget(structure_chars=250, memory_chars=0),
    )
    assert any(line.startswith("- A") for line in ctx.structure)
    assert not any(line.startswith("- B") for line in ctx.structure)
    assert not any("[domain]" in line for line in ctx.structure)
    assert len(ctx.structure) == 1


def test_structure_overflow_fallback_respects_remaining_budget(tmp_path):
    """A domain whose summary can't fit the leftover budget doesn't render at all (never
    a budget violation)."""
    r = _overflow_reader(tmp_path)
    s = Store(tmp_path / "t.db")
    _accepted_domain(s, "payments", ["9"], summary="x" * 500)
    ctx = get_task_context(
        [Seed(file_path="mod.py")],
        s,
        r,
        RetrievalBudget(structure_chars=250, memory_chars=0),
    )
    assert not any("[domain]" in line for line in ctx.structure)
    assert sum(len(x) for x in ctx.structure) <= 250


# -- structure-budget fallback: walk saturation (M5 follow-up) ---------------------------
#
# `node_cap = structure_chars // 120` truncates the BFS *walk* before any line is rendered.
# Real `_fmt_node` lines run ~40-80 chars on real corpora -- well under the 120-chars/node
# the cap assumes -- so the capped subgraph's lines always fit `structure_chars` and the
# render-overflow trigger above never fires; the cut instead manifests as a silently
# truncated walk that render overflow can never see. These fixtures use chains of nodes
# with realistic-length labels so the walk saturates `node_cap` on its own.


def _chain_graph(n: int, label_len: int, communities: list[str], fname: str = "mod.py") -> dict:
    """`n` nodes in file `fname`, chained node0->node1->...->node(n-1) (so `subgraph`'s BFS
    visits them in that order), each labelled with `label_len` filler chars and assigned
    `communities[i]`."""
    nodes = []
    links = []
    for i in range(n):
        nodes.append(
            {
                "id": f"n{i}",
                "label": "N" * label_len + str(i),
                "norm_label": f"n{i}",
                "file_type": "code",
                "source_file": fname,
                "source_location": f"L{i + 1}",
                "community": communities[i],
            }
        )
        if i > 0:
            links.append({"source": f"n{i - 1}", "target": f"n{i}", "relation": "calls"})
    return {"built_at_commit": "abc123", "nodes": nodes, "links": links}


def _chain_reader(tmp_path, graph: dict, name: str = "chain.json") -> GraphifyReader:
    p = tmp_path / name
    p.write_text(json.dumps(graph))
    return GraphifyReader(p)


def test_structure_fallback_fires_on_walk_saturation_without_render_overflow(tmp_path):
    """(a) Realistic-shaped fixture: short ~40-char lines, an 8-node chain in one community.
    structure_chars=400 -> node_cap=3, so the walk is capped at the first 3 nodes even
    though all three lines fit comfortably (no render overflow at all). The accepted domain
    covering the seed's own community still surfaces as a fallback summary line -- the
    walk-saturation trigger alone."""
    g = _chain_graph(8, 20, ["c0"] * 8)
    r = _chain_reader(tmp_path, g)
    s = Store(tmp_path / "t.db")
    _accepted_domain(s, "widgets", ["c0"])
    budget = RetrievalBudget(structure_chars=400, memory_chars=0)
    ctx = get_task_context([Seed(file_path="mod.py")], s, r, budget)

    leaves = [
        line
        for line in ctx.structure
        if not line.startswith("- [domain]") and not line.startswith("—")
    ]
    assert len(leaves) == 3  # node_cap=3 -- the walk-truncated leaves, all fit, none overflow
    assert any(line.startswith("- [domain] Widgets") for line in ctx.structure)
    assert any(line.startswith("—") for line in ctx.structure)  # fallback group header
    assert sum(len(x) for x in ctx.structure) <= 400


def test_structure_fallback_silent_when_walk_unsaturated_and_no_overflow(tmp_path):
    """(b) Big default budget against the tiny FIXTURE graph: the whole connected component
    (3 nodes) fits well inside node_cap (33), and every rendered line fits structure_chars
    -- neither trigger fires. Byte-identical to pre-fix behavior even though an accepted
    domain covers a seed community: proves walk saturation doesn't fire just because a
    domain happens to exist."""
    r = GraphifyReader(FIXTURE)
    s = Store(tmp_path / "t.db")
    _accepted_domain(s, "trading", ["1"])  # covers m_cls/m_fn's community
    ctx = get_task_context([Seed(file_path="trader/exec.py")], s, r)  # default budget
    assert not any("[domain]" in line for line in ctx.structure)
    assert len(ctx.structure) == 3  # m_cls, m_fn, o_fn -- the whole reachable graph, unaffected


def test_structure_fallback_dedups_across_both_triggers_and_caps_at_five(tmp_path):
    """(c) A 10-node chain where structure_chars=900 makes node_cap=7: the walk saturates at
    7 nodes (n0..n6), and rendering those overflows partway through (n0..n4 fit, n5 doesn't)
    -- both triggers fire in the same call. n5 (the render-overflow node) deliberately
    shares its community ("c0") with n0, so that community reaches the fallback candidate
    list twice: once via the render-overflow node, once via the (uncapped) seed
    communities. 6 communities have accepted domains covering them (more than the cap of
    5), so this also proves the cap holds across the combined candidate list."""
    communities = ["c0", "c1", "c2", "c3", "c4", "c0", "c6", "c7", "c8", "c9"]
    g = _chain_graph(10, 130, communities)
    r = _chain_reader(tmp_path, g)
    s = Store(tmp_path / "t.db")
    for cid in ["c0", "c1", "c2", "c3", "c6", "c7"]:  # 6 domains, one (c7) must never surface
        _accepted_domain(s, cid, [cid])
    budget = RetrievalBudget(structure_chars=900, memory_chars=0)
    ctx = get_task_context([Seed(file_path="mod.py")], s, r, budget)

    domain_lines = [line for line in ctx.structure if line.startswith("- [domain]")]
    assert len(domain_lines) == 5  # cap holds despite 6 covered domains
    assert len(set(domain_lines)) == 5  # no line duplicated
    assert sum(line.startswith("- [domain] C0:") for line in domain_lines) == 1  # deduped
    assert not any(line.startswith("- [domain] C7:") for line in domain_lines)  # cap excluded it
    assert sum(len(x) for x in ctx.structure) <= 900


def test_structure_fallback_saturation_without_domains_is_noop(tmp_path):
    """(d) Same walk-saturated fixture as (a), but no accepted domain covers the seed
    community -> nothing appended, matching the render-overflow trigger's own
    no-accepted-domains behavior (`test_structure_overflow_old_behavior_without_domains`)."""
    g = _chain_graph(8, 20, ["c0"] * 8)
    r = _chain_reader(tmp_path, g)
    s = Store(tmp_path / "t.db")
    budget = RetrievalBudget(structure_chars=400, memory_chars=0)
    ctx = get_task_context([Seed(file_path="mod.py")], s, r, budget)
    assert not any("[domain]" in line for line in ctx.structure)
    assert len(ctx.structure) == 3


# -- structure-budget fallback: title/summary dedup + group header (M5 polish) -----------


def test_structure_fallback_dedupes_when_summary_exactly_equals_title(tmp_path):
    """Label-bootstrapped domains (`domains.bootstrap_domains`'s labeled path) sometimes
    have summary == title exactly -- both derive from the same engine label before any
    real digest exists. The fallback line drops the redundant "": summary" tail rather
    than repeat the title verbatim."""
    s = Store(tmp_path / "t.db")
    _accepted_domain(s, "widgets", ["c0"], summary="Widgets")  # summary == title exactly
    lines = _structure_fallback_lines(["c0"], s, remaining_budget=1000)
    body = [line for line in lines if line.startswith("- [domain]")]
    assert body == ["- [domain] Widgets"]
    assert not any(": Widgets" in line for line in lines)


def test_structure_fallback_keeps_both_parts_when_summary_only_starts_with_title(tmp_path):
    """A digest summary that merely STARTS WITH the title but continues with more
    information is NOT deduped -- only EXACT equality collapses to the title alone
    (unlike `_fmt_decision`'s startswith/prefix rule for decisions: digest summaries
    legitimately begin with the god-node name and their remainder is informative)."""
    s = Store(tmp_path / "t.db")
    _accepted_domain(s, "widgets", ["c0"], summary="Widgets handles settlement retries")
    lines = _structure_fallback_lines(["c0"], s, remaining_budget=1000)
    body = [line for line in lines if line.startswith("- [domain]")]
    assert body == ["- [domain] Widgets: Widgets handles settlement retries"]


def test_structure_fallback_prepends_group_header_when_lines_emitted(tmp_path):
    s = Store(tmp_path / "t.db")
    _accepted_domain(s, "widgets", ["c0"])
    lines = _structure_fallback_lines(["c0"], s, remaining_budget=1000)
    assert lines[0] == (
        "— named areas covering this neighborhood (map truncated; drill_down for detail):"
    )
    assert len(lines) == 2  # header + the one domain line


def test_structure_fallback_omits_header_when_no_room_left(tmp_path):
    """The header is best-effort, charged against the same leftover budget as the lines:
    when there's no room left after the domain lines already selected, it's dropped
    rather than pushing the render over budget."""
    s = Store(tmp_path / "t.db")
    _accepted_domain(s, "widgets", ["c0"])
    exact_line = "- [domain] Widgets: s"
    lines = _structure_fallback_lines(["c0"], s, remaining_budget=len(exact_line))
    assert lines == [exact_line]  # exact-fit budget: no room for the header


def test_structure_fallback_no_header_when_no_domain_lines_fit(tmp_path):
    s = Store(tmp_path / "t.db")
    lines = _structure_fallback_lines(["no-such-community"], s, remaining_budget=1000)
    assert lines == []


def test_structure_fallback_unions_all_accepted_domains_covering_community(tmp_path):
    """Same "orphan window" shape as bucket C in `rank_decisions` (Gate-5 finding 2, see
    `store.find_domains_by_community`'s docstring): two ACCEPTED domains can legitimately
    cover the SAME community at once. The old singular `find_domain_by_community` lookup
    here only ever surfaced the NEWEST covering domain's summary line -- an OLDER covering
    domain's summary silently never appeared even though it's still accepted and still
    covers the community. Both must surface now (deduped by domain id, cap unchanged)."""
    s = Store(tmp_path / "t.db")
    older = _accepted_domain(s, "widgets-old", ["c0"], summary="Older widgets summary")
    newer = _accepted_domain(s, "widgets-new", ["c0"], summary="Newer widgets summary")
    assert older.domain_id < newer.domain_id  # ULID monotonic -- "older" really is older

    lines = _structure_fallback_lines(["c0"], s, remaining_budget=1000)
    body = [line for line in lines if line.startswith("- [domain]")]
    assert any("Older widgets summary" in line for line in body)
    assert any("Newer widgets summary" in line for line in body)
