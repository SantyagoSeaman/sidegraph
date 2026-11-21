import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sidegraph.engine.reader import GraphifyReader
from sidegraph.retrieval import (
    RetrievalBudget,
    Seed,
    _fmt_node,
    get_task_context,
    query_structure,
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


# -- Structural map cut at the walk limit ----------------------------------------------


def _accepted_domain(store, slug, communities, summary="s", *, reader=None):
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
    accepted = store.get_domain(d.domain_id)
    if reader is not None:
        store.replace_domain_members(
            accepted.domain_id,
            [
                (n.node_id, n.file_path, n.community)
                for n in reader.list_nodes()
                if n.community in accepted.communities
            ],
        )
    return accepted


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


def test_structure_reports_a_capped_walk_without_domain_summaries(tmp_path):
    """All three retained node lines fit, but the capped walk omits five nodes."""
    g = _chain_graph(8, 20, ["c0"] * 8)
    r = _chain_reader(tmp_path, g)
    s = Store(tmp_path / "t.db")
    _accepted_domain(s, "widgets", ["c0"], reader=r)
    cuts: list[None] = []
    text = query_structure(
        [Seed(file_path="mod.py")], s, r, 400, on_budget_cut=lambda: cuts.append(None)
    )

    assert cuts == [None]
    lines = text.splitlines()[1:]
    assert not any(line.startswith("- [domain]") for line in lines)
    assert "drill_down" not in text
    assert len(lines) == 3
