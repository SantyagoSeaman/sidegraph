"""Algorithmic-complexity regression tests for the engine seam (GraphifyReader).

Root cause fixed here: ``neighbors()`` (and ``resolve()``/``nodes_in_file()``) used to
re-scan the FULL ``links``/``nodes`` list on every single call -- O(E) or O(V) per call.
``rationale_nodes()`` calls ``neighbors()`` once or twice PER rationale node, and
``import_rationales()`` calls ``resolve()`` up to 3x per rationale node, so on a
113K-node / 232K-edge graph with 23K rationale nodes this became billions of comparisons
in pure Python and `sidegraph-import` hung indefinitely (killed after 90s CPU, zero
output). The fix builds each index ONCE, lazily, memoized on the reader instance, turning
these into O(degree) / O(matches) per call.

These tests assert the algorithmic property directly (the full list is iterated at most
once, regardless of how many queries follow) rather than relying on wall-clock timing,
plus one synthetic-scale test that demonstrates the practical effect.
"""

from __future__ import annotations

import json

from sidegraph.engine.reader import GraphifyReader
from sidegraph.schema import Descriptor


class _CountingList(list):
    """A list that counts how many times it has been fully iterated (``for x in it``).

    Used to prove an index is built from a single pass over the underlying data, not
    re-scanned on every subsequent call.
    """

    def __init__(self, *args):
        super().__init__(*args)
        self.iterations = 0

    def __iter__(self):
        self.iterations += 1
        return super().__iter__()


def _write_graph(tmp_path, graph, name="g.json"):
    p = tmp_path / name
    p.write_text(json.dumps(graph))
    return GraphifyReader(p)


def _small_graph():
    return {
        "built_at_commit": "x",
        "nodes": [
            {
                "id": "a",
                "label": "Alpha",
                "norm_label": "alpha",
                "file_type": "code",
                "source_file": "a.py",
                "community": "1",
            },
            {
                "id": "b",
                "label": "Beta",
                "norm_label": "beta",
                "file_type": "code",
                "source_file": "b.py",
                "community": "1",
            },
            {
                "id": "c",
                "label": "Gamma",
                "norm_label": "gamma",
                "file_type": "code",
                "source_file": "a.py",
                "community": "2",
            },
        ],
        "links": [
            {"source": "a", "target": "b", "relation": "calls"},
            {"source": "b", "target": "c", "relation": "calls"},
        ],
    }


def test_neighbors_scans_links_at_most_once_across_many_calls(tmp_path):
    r = _write_graph(tmp_path, _small_graph())
    counting_links = _CountingList(r._links)
    r._links = counting_links

    for _ in range(200):
        r.neighbors("a")
        r.neighbors("b", relations=["calls"])

    # Before the fix: 400 full scans (one per neighbors() call). After: exactly one, to
    # build the adjacency index; every call after that reuses it.
    assert counting_links.iterations <= 1


def test_rationale_nodes_scans_links_at_most_once_regardless_of_node_count(tmp_path):
    n_rationale = 50
    nodes = []
    links = []
    for i in range(n_rationale):
        nodes.append(
            {
                "id": f"rat_{i}",
                "label": f"reason {i}",
                "norm_label": f"reason {i}",
                "file_type": "rationale",
                "source_file": f"f{i}.py",
                "community": "1",
            }
        )
        nodes.append(
            {
                "id": f"code_{i}",
                "label": f"fn_{i}",
                "norm_label": f"fn_{i}",
                "file_type": "code",
                "source_file": f"f{i}.py",
                "community": "1",
            }
        )
        links.append({"source": f"rat_{i}", "target": f"code_{i}", "relation": "rationale_for"})
    graph = {"built_at_commit": "x", "nodes": nodes, "links": links}
    r = _write_graph(tmp_path, graph)
    counting_links = _CountingList(r._links)
    r._links = counting_links

    result = r.rationale_nodes()

    assert len(result) == n_rationale
    # rationale_nodes() calls neighbors() once or twice PER rationale node (50 nodes here
    # -> 50-100 calls). Before the fix that was 50-100 full scans of `links`; after, one.
    assert counting_links.iterations <= 1


def test_resolve_scans_nodes_at_most_once_across_many_calls(tmp_path):
    r = _write_graph(tmp_path, _small_graph())
    counting_nodes = _CountingList(r._nodes)
    r._nodes = counting_nodes

    for name in ["Alpha", "Beta", "Gamma", "does-not-exist"] * 50:
        r.resolve(Descriptor(name=name))

    assert counting_nodes.iterations <= 1


def test_nodes_in_file_scans_nodes_at_most_once_across_many_calls(tmp_path):
    r = _write_graph(tmp_path, _small_graph())
    counting_nodes = _CountingList(r._nodes)
    r._nodes = counting_nodes

    for file_path in ["a.py", "b.py", "nope.py"] * 50:
        r.nodes_in_file(file_path)

    assert counting_nodes.iterations <= 1


def _synthetic_large_graph(n_code=3000, n_rationale=2000, n_filler_edges=48000):
    """``n_code`` code + ``n_rationale`` rationale nodes, one deterministic rationale_for
    edge per rationale node (rationale_i -> code_i, so each expected target is known
    analytically -- no need for a naive O(N*E) reference scan to check correctness) plus
    ~``n_filler_edges`` filler `calls` edges among code nodes."""
    nodes = [
        {
            "id": f"code_{i}",
            "label": f"fn_{i}",
            "norm_label": f"fn_{i}",
            "file_type": "code",
            "source_file": f"src/f{i}.py",
            "community": str(i % 50),
        }
        for i in range(n_code)
    ]
    nodes += [
        {
            "id": f"rat_{i}",
            "label": f"reason {i}",
            "norm_label": f"reason {i}",
            "file_type": "rationale",
            "source_file": f"src/f{i % n_code}.py",
            "community": str(i % 50),
        }
        for i in range(n_rationale)
    ]
    links = [
        {"source": f"rat_{i}", "target": f"code_{i % n_code}", "relation": "rationale_for"}
        for i in range(n_rationale)
    ]
    # Filler edges: connect each code node to a handful of others via `calls`, never
    # `rationale_for`, so they can't affect any rationale node's resolved targets.
    per_node = max(1, n_filler_edges // n_code)
    for i in range(n_code):
        for k in range(1, per_node + 1):
            links.append(
                {"source": f"code_{i}", "target": f"code_{(i + k) % n_code}", "relation": "calls"}
            )
    return {"built_at_commit": "x", "nodes": nodes, "links": links}


def test_rationale_nodes_resolve_their_targets_on_a_synthetic_graph(tmp_path):
    """Correctness only; the single-scan tests above pin the O(E) fix, not a wall clock."""
    n_code, n_rationale = 300, 200
    graph = _synthetic_large_graph(n_code, n_rationale, n_filler_edges=4800)
    r = _write_graph(tmp_path, graph)
    assert len(r._nodes) == n_code + n_rationale
    assert len(r._links) >= 5000

    result = r.rationale_nodes()

    assert len(result) == n_rationale
    by_id = {rn.node_id: rn for rn in result}
    for i in range(n_rationale):
        rn = by_id[f"rat_{i}"]
        assert [t.node_id for t in rn.targets] == [f"code_{i % n_code}"]
