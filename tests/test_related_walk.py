"""The walk that finds related records takes the neighbour nodes with the most edges to the
seed nodes, so a file seed (which alone fills the map's node cap) still reaches a neighbour.
see design/superpowers/specs/2026-11-14-task-context-files-only-design.md (part D)
"""

import json
from datetime import UTC, datetime

from sidegraph import server
from sidegraph.engine.reader import GraphifyReader
from sidegraph.retrieval import RetrievalBudget, Seed, get_task_context, query_structure
from sidegraph.schema import (
    AnchorBinding,
    Decision,
    DecisionKind,
    DecisionStatus,
    Descriptor,
    Entity,
    Fact,
    Provenance,
)
from sidegraph.store import Store

BIG = "src/big.py"
BASE = datetime(2026, 1, 1, tzinfo=UTC)
SYMBOLS = 40  # more than the map's node cap at the default structure budget (4000 // 120 = 33)


def _graph(tmp_path, links):
    nodes = [{"id": "big", "label": "big.py", "file_type": "code", "source_file": BIG}]
    nodes += [
        {"id": f"s{i}", "label": f"sym{i}()", "file_type": "code", "source_file": BIG}
        for i in range(SYMBOLS)
    ]
    nodes += [
        {"id": name, "label": f"{name}()", "file_type": "code", "source_file": f"src/{name}.py"}
        for name in ("near", "far", "hub")
    ]
    # An engine artifact: a node with no file path (Graphify writes an empty string).
    nodes.append({"id": "any", "label": "Any", "file_type": "code", "source_file": ""})
    path = tmp_path / "graph.json"
    path.write_text(
        json.dumps(
            {
                "built_at_commit": "abc123",
                "nodes": nodes,
                "links": [
                    {
                        "source": link[0],
                        "target": link[1],
                        "relation": link[2] if len(link) > 2 else "calls",
                    }
                    for link in links
                ],
            }
        )
    )
    return GraphifyReader(path)


def _neighbour_world(tmp_path, links):
    """A file of 41 nodes and three nodes outside it, each with a stored entity that carries
    one accepted fact named after it."""
    reader = _graph(tmp_path, links)
    store = Store(tmp_path / "store")
    facts = {}
    for name in ("near", "far", "hub"):
        entity = store.upsert_entity(
            Entity(
                canonical_name=name,
                descriptor=Descriptor(name=name, file_path=f"src/{name}.py"),
                last_seen_node_id=name,
            )
        )
        fact = store.add_fact(
            Fact(
                statement=f"about {name}",
                source="s",
                status=DecisionStatus.ACCEPTED,
                valid_from=BASE,
                provenance=Provenance(source="manual"),
            )
        )
        store.add_binding(
            AnchorBinding(record_id=fact.id, entity_id=entity.entity_id, tier=2, status="live")
        )
        facts[name] = fact
    return store, reader, facts


def test_connected_neighbours_ranks_by_edges_to_the_seeds(tmp_path):
    reader = _graph(
        tmp_path,
        [
            ("s0", "far"),
            ("s1", "near"),
            ("near", "s2"),
            ("s3", "hub"),
            ("s0", "s1"),
            ("hub", "far"),
            ("s4", "any"),
            ("s5", "any"),
            ("s6", "any"),
            ("s7", "ghost"),  # a link to a node the graph does not hold
        ],
    )
    seeds = ["big", *(f"s{i}" for i in range(SYMBOLS)), "no-such-node"]
    names = [n.node_id for n in reader.connected_neighbours(seeds, 33)]
    # `any` has three edges to seeds, `near` two, `far` and `hub` one each. A node with no
    # file path counts like any other: a decision anchored to a path-less mention is reached
    # through it.
    assert names == ["any", "near", "far", "hub"]
    assert [n.node_id for n in reader.connected_neighbours(seeds, 2)] == ["any", "near"]
    assert reader.connected_neighbours(seeds, 0) == []
    assert reader.connected_neighbours(seeds, -1) == []
    # An edge between two non-seeds counts for neither; with one seed `hub` has no edge to it.
    assert [n.node_id for n in reader.connected_neighbours(["s0"], 33)] == ["far", "s1"]


def test_equal_counts_are_ordered_by_node_id_whatever_the_order_of_seeds_and_links(tmp_path):
    # The seeds' edges reach `hub` before `far`; both have one edge.
    reader = _graph(tmp_path, [("s0", "hub"), ("s1", "far")])
    seeds = [f"s{i}" for i in range(SYMBOLS)]
    assert [n.node_id for n in reader.connected_neighbours(seeds, 33)] == ["far", "hub"]
    assert [n.node_id for n in reader.connected_neighbours(seeds[::-1], 33)] == ["far", "hub"]
    assert [n.node_id for n in reader.connected_neighbours(seeds, 1)] == ["far"]
    # One seed whose links name `hub` before `far`: the link order does not decide either.
    one = _graph(tmp_path, [("s0", "hub"), ("s0", "far")])
    assert [n.node_id for n in one.connected_neighbours(["s0"], 33)] == ["far", "hub"]


def test_every_link_between_a_seed_and_a_neighbour_counts(tmp_path):
    # `hub` has two links to one seed (calls, imports); `far` has one link to one seed.
    # Node-id order alone would put `far` first.
    reader = _graph(tmp_path, [("s0", "hub", "calls"), ("s0", "hub", "imports"), ("s1", "far")])
    seeds = [f"s{i}" for i in range(SYMBOLS)]
    assert [n.node_id for n in reader.connected_neighbours(seeds, 33)] == ["hub", "far"]


def test_a_neighbour_is_found_by_the_engine_mapping_of_its_entity(tmp_path):
    # The stored entity's name is `Thing.near`, not the node's label `near()`; only
    # `last_seen_node_id` links it to the node.
    reader = _graph(tmp_path, [("s0", "near")])
    store = Store(tmp_path / "store")
    entity = store.upsert_entity(
        Entity(
            canonical_name="Thing.near",
            descriptor=Descriptor(name="Thing.near", file_path="src/other.py"),
            last_seen_node_id="near",
        )
    )
    fact = store.add_fact(
        Fact(
            statement="about the mapped neighbour",
            source="s",
            status=DecisionStatus.ACCEPTED,
            valid_from=BASE,
            provenance=Provenance(source="manual"),
        )
    )
    store.add_binding(
        AnchorBinding(record_id=fact.id, entity_id=entity.entity_id, tier=2, status="live")
    )
    ctx = get_task_context([Seed(file_path=BIG)], store, reader)
    assert ctx.shown_ids == [fact.id]


def test_a_file_seed_reaches_the_fact_of_a_neighbour(tmp_path):
    store, reader, facts = _neighbour_world(tmp_path, [("s39", "near")])
    ctx = get_task_context([Seed(file_path=BIG)], store, reader)
    assert ctx.shown_ids == [facts["near"].id]
    assert any("about near" in line for line in ctx.facts)


def test_a_file_seed_reaches_the_decision_of_a_neighbour_as_related(tmp_path):
    store, reader, _facts = _neighbour_world(tmp_path, [("s39", "near")])
    entity = store.find_entity("near", "src/near.py")
    decision = store.add_decision(
        Decision(
            title="near-adr",
            kind=DecisionKind.ADR,
            status=DecisionStatus.ACCEPTED,
            context="c",
            choice="do near-adr",
            valid_from=BASE,
            provenance=Provenance(source="manual"),
        )
    )
    store.add_binding(
        AnchorBinding(record_id=decision.id, entity_id=entity.entity_id, tier=2, status="live")
    )
    ctx = get_task_context([Seed(file_path=BIG)], store, reader)
    assert any("near-adr" in line for line in ctx.related)
    assert ctx.decisions == [] and ctx.mistakes == []


def test_the_most_connected_neighbour_wins_a_cap_of_one(tmp_path):
    store, reader, facts = _neighbour_world(
        tmp_path, [("s0", "far"), ("s1", "near"), ("s2", "near")]
    )
    budget = RetrievalBudget(structure_chars=120)  # one neighbour
    ctx = get_task_context([Seed(file_path=BIG)], store, reader, budget)
    assert ctx.shown_ids == [facts["near"].id]


def test_a_structure_budget_under_120_reaches_no_neighbour(tmp_path):
    store, reader, _facts = _neighbour_world(tmp_path, [("s0", "near")])
    ctx = get_task_context(
        [Seed(file_path=BIG)], store, reader, RetrievalBudget(structure_chars=119)
    )
    assert ctx.shown_ids == []


def test_the_query_decisions_description_describes_the_walk_as_it_is():
    doc = " ".join(server.query_decisions.__doc__.split())
    assert "walking the structural subgraph" not in doc
    assert "the nodes with the most edges to them" in doc


def test_the_structural_map_does_not_change(tmp_path):
    store, reader, _facts = _neighbour_world(tmp_path, [("s0", "near")])
    text = query_structure([Seed(file_path=BIG)], store, reader)
    lines = text.splitlines()[1:]
    # The map is the first 33 nodes of the file, as before: the neighbour is not on it.
    assert lines[0] == f"- big.py (code) [{BIG}]"
    assert len(lines) == 33
    assert not any("near" in line for line in lines)
