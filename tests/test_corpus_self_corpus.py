"""Tier-1 corpus test: the promoted self-corpus graph snapshot (spec §3, build order §12.2).

This is the first test that runs `GraphifyReader` against a graph the ENGINE actually produced,
rather than a hand-authored slice. That is the point of the two-tier design: the live tier
produces what the deterministic tier consumes. Everything here is offline — no engine, no
network, no LLM, no installs.

The fixture is generated in the private testbed (`harness/fixtures.py`) and copied here only by
`tools/promote_fixtures.py`. Regenerate it there; never hand-edit it here.
"""

from __future__ import annotations

import json
from pathlib import Path

from sidegraph.engine.reader import GraphifyReader
from sidegraph.schema import Descriptor

_ROOT = Path(__file__).resolve().parent.parent
_CORPUS = _ROOT / "tests" / "fixtures" / "corpus" / "self-corpus"
_GRAPH = _CORPUS / "graph.json"

# The scope declared in the testbed's corpora.toml. Duplicated here on purpose: this is the
# main repo's independent statement of what it accepted, so a widening in the testbed shows up
# as a failure here rather than as a silently larger fixture.
_DECLARED_SCOPE = ("src/sidegraph/engine/", "src/sidegraph/host/")

# tests/ ships wholesale and the public branch is full-cloned by `plugin marketplace add`, so a
# fixture's size lands on every consumer. 1024 KB is the check-added-large-files cap; this is
# the guard that keeps "widening a snapshot is just a manifest edit" honest.
_MAX_FIXTURE_KB = 1024


def _graph() -> dict:
    return json.loads(_GRAPH.read_text(encoding="utf-8"))


def test_fixture_is_present_and_stamped():
    stamp = json.loads((_CORPUS / "_provenance.json").read_text(encoding="utf-8"))
    assert stamp["corpus_id"] == "self-corpus"
    assert stamp["visibility"] == "public"
    assert stamp["generated_from_commit"]


def test_fixture_id_is_in_the_committed_allowlist():
    """The gate enforces this too. Asserting it here as well means a fixture that arrived by
    some path other than promote_fixtures.py still cannot sit in the tree unnoticed."""
    allow = (_CORPUS.parent / "PUBLIC_CORPORA.txt").read_text(encoding="utf-8")
    entries = {ln.strip() for ln in allow.splitlines() if ln.strip() and not ln.startswith("#")}
    assert "self-corpus" in entries


def test_every_node_is_inside_the_declared_scope():
    """The snapshot is scoped, and the scope is the leak boundary as well as the size control:
    a node from outside it means the generator's subsetting stopped working."""
    stray = sorted(
        {
            n["source_file"]
            for n in _graph()["nodes"]
            if not str(n.get("source_file", "")).startswith(_DECLARED_SCOPE)
        }
    )
    assert not stray, f"nodes outside the declared scope: {stray}"


def test_no_link_dangles_outside_the_snapshot():
    """Subsetting keeps only links whose BOTH ends survive. A dangling endpoint would describe
    a graph that cannot exist, and the reader's adjacency index would happily build on it."""
    g = _graph()
    ids = {n["id"] for n in g["nodes"]}
    dangling = [
        link for link in g["links"] if link["source"] not in ids or link["target"] not in ids
    ]
    assert not dangling, f"{len(dangling)} links point outside the snapshot"


def test_fixture_stays_under_the_large_file_cap():
    kb = _GRAPH.stat().st_size / 1024
    assert kb < _MAX_FIXTURE_KB, (
        f"fixture is {kb:.0f} KB, at/over the {_MAX_FIXTURE_KB} KB cap — narrow graph_scope "
        "in the testbed's corpora.toml rather than raising the cap"
    )


def test_fixture_is_deterministically_ordered():
    """Regenerating must produce no diff. If the generator ever stopped sorting, this catches
    it here rather than as inexplicable churn in a later PR."""
    g = _graph()
    node_ids = [n["id"] for n in g["nodes"]]
    assert node_ids == sorted(node_ids)
    link_keys = [(link["source"], link["target"]) for link in g["links"]]
    assert link_keys == sorted(link_keys)


# --- the reader, against a real engine-produced graph ---------------------------------------


def test_reader_loads_the_snapshot():
    r = GraphifyReader(_GRAPH)
    nodes = r.list_nodes()
    assert len(nodes) == len(_graph()["nodes"])


def test_reader_resolves_a_real_symbol_from_the_engine_seam():
    """GraphifyReader resolving GraphifyReader — the fixture describes the very class reading
    it, which is what makes this corpus a useful self-test."""
    r = GraphifyReader(_GRAPH)
    res = r.resolve(Descriptor(name="GraphifyReader", file_path="src/sidegraph/engine/reader.py"))
    assert res.status == "resolved"
    assert res.node_id == "src_sidegraph_engine_reader_graphifyreader"


def test_reader_lists_nodes_of_a_real_file():
    r = GraphifyReader(_GRAPH)
    found = r.nodes_in_file("src/sidegraph/engine/reader.py")
    assert found
    assert all(n.file_path == "src/sidegraph/engine/reader.py" for n in found)


def test_reader_walks_real_containment_edges():
    r = GraphifyReader(_GRAPH)
    kids = r.neighbors("src_sidegraph_engine_reader", relations=["contains"])
    assert kids, "the reader module node should contain the symbols defined in it"
    assert any(n.node_id == "src_sidegraph_engine_reader_graphifyreader" for n in kids)


def test_both_declared_seams_are_actually_represented():
    """A scope listing two prefixes but yielding nodes from only one would be a silently
    half-empty fixture that every test above still passes against."""
    files = {n["source_file"] for n in _graph()["nodes"]}
    assert any(f.startswith("src/sidegraph/engine/") for f in files)
    assert any(f.startswith("src/sidegraph/host/") for f in files)


def test_snapshot_carries_prose_from_docstrings_not_only_paths():
    """Recorded deliberately, because it bounds what a graph of a PRIVATE corpus would expose:
    the engine emits `rationale` nodes whose labels are docstring/comment TEXT, not just file
    paths and symbol names. Harmless for this corpus — it is ours, Apache-2.0 — and precisely
    why private corpora are never promoted in any derived form (leak-safety layer L0)."""
    kinds = {n.get("file_type") for n in _graph()["nodes"]}
    assert "rationale" in kinds
