"""Which Graphify built the graph, read in the engine seam only.
# see design/superpowers/specs/2026-10-14-graphify-upgrade-design.md (D1)"""

from __future__ import annotations

import pytest

from sidegraph.engine.reader import GraphifyReader
from tests.graph_fixtures import node, reader_of, write_graph

DOC = node("d", "guide.md", "docs/guide.md", file_type="document")
CODE = node("c", "run()", "src/run.py", location="L3")
MENTION = {"relation": "references", "source": "d", "target": "c", "context": "run"}


def stamped(version):
    return {"schema_version": 1, "graphify_version": version}


def test_a_stamped_tested_graph(tmp_path):
    r = reader_of(tmp_path, graph=stamped("0.9.90"))
    assert (r.engine_version(), r.engine_generation(), r.engine_version_tested()) == (
        "0.9.90",
        "mention-edges",
        True,
    )


def test_a_0_9_6_graph_is_legacy_and_counts_as_tested(tmp_path):
    r = reader_of(tmp_path, DOC, CODE, graph={})
    assert (r.engine_version(), r.engine_generation(), r.engine_version_tested()) == (
        None,
        "legacy",
        True,
    )


def test_an_unstamped_graph_with_a_mention_edge_has_mention_edges(tmp_path):
    # the `update --no-cluster` case
    assert reader_of(tmp_path, DOC, CODE, links=[MENTION]).engine_generation() == "mention-edges"


def test_an_llm_references_edge_without_context_is_not_a_mention(tmp_path):
    # red against a mention rule that ignores `context` (a discarded design, declared)
    llm = {"relation": "references", "source": "d", "target": "c", "confidence": "INFERRED"}
    assert reader_of(tmp_path, DOC, CODE, links=[llm]).engine_generation() == "legacy"


def test_node_kind_alone_does_not_make_a_graph_mention_edges(tmp_path):
    # red against the spec's node_kind signal (0.9.61 already tags pages), declared
    r = reader_of(
        tmp_path,
        {**DOC, "node_kind": "page"},
        CODE,
        links=[{"relation": "contains", "source": "d", "target": "c"}],
    )
    assert r.engine_generation() == "legacy"


def test_a_references_edge_between_two_code_nodes_is_not_a_mention(tmp_path):
    other = node("c2", "go()", "src/go.py", location="L1")
    link = {"relation": "references", "source": "c", "target": "c2", "context": "go"}
    assert reader_of(tmp_path, CODE, other, links=[link]).engine_generation() == "legacy"


def test_an_untested_or_unparsable_stamp(tmp_path):
    r = reader_of(tmp_path, graph=stamped("0.9.95"))
    assert (r.engine_generation(), r.engine_version_tested()) == ("mention-edges", False)
    (tmp_path / "u").mkdir()
    u = reader_of(tmp_path / "u", graph=stamped("next"))
    assert (u.engine_version(), u.engine_generation(), u.engine_version_tested()) == (
        "next",
        "unknown",
        False,
    )


@pytest.mark.parametrize("bad", ["  ", 90])  # blank / not a string
def test_a_blank_or_non_string_stamp_reads_as_absent(tmp_path, bad):
    assert reader_of(tmp_path, graph=stamped(bad)).engine_version() is None


def test_the_generation_is_computed_once_per_reader(tmp_path):
    # red against an unmemoised scan (declared): the second call sees the emptied links
    r = reader_of(tmp_path, DOC, CODE, links=[MENTION])
    assert r.engine_generation() == "mention-edges"
    r._links = []
    assert r.engine_generation() == "mention-edges"


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("0.9.61", ("0.9.61", "legacy", False)),
        ("0.9.62", ("0.9.62", "mention-edges", False)),
        ("0.9.90.dev3", ("0.9.90.dev3", "mention-edges", False)),
        (" 0.9.90\n", ("0.9.90", "mention-edges", True)),
        ("next\nbuild", ("next\nbuild", "unknown", False)),
        pytest.param("9" * 5000 + ".0.0", None, id="oversized-numeric-head"),
    ],
)
def test_complete_version_metadata_and_boundary(tmp_path, version, expected):
    reader = reader_of(tmp_path, graph=stamped(version))
    if expected is None:
        expected = (version, "unknown", False)
    assert (
        reader.engine_version(),
        reader.engine_generation(),
        reader.engine_version_tested(),
    ) == expected


@pytest.mark.parametrize("meta", ["0.9.90", {"graphify_version": True}])  # not a dict / bad field
def test_malformed_metadata_has_the_absent_stamp_contract(tmp_path, meta):
    reader = reader_of(tmp_path, graph=meta)
    assert (
        reader.engine_version(),
        reader.engine_generation(),
        reader.engine_version_tested(),
    ) == (None, "legacy", True)


@pytest.mark.parametrize("context", [None, {}])
def test_only_a_string_context_is_mention_evidence(tmp_path, context):
    reader = reader_of(tmp_path, DOC, CODE, links=[{**MENTION, "context": context}])
    assert reader.engine_generation() == "legacy"


def test_a_stamp_takes_precedence_over_mention_edges(tmp_path):
    reader = reader_of(tmp_path, DOC, CODE, links=[MENTION], graph=stamped("0.9.61"))
    assert reader.engine_generation() == "legacy"


@pytest.mark.parametrize("links", [None, [None, 1, "bad"]])  # not a list / not link dicts
def test_malformed_link_shapes_supply_no_mention_evidence(tmp_path, links):
    reader = GraphifyReader(
        write_graph(
            tmp_path,
            "graph.json",
            {
                "nodes": [DOC, CODE],
                "links": links,
            },
        )
    )
    assert reader.engine_generation() == "legacy"


def test_bad_and_dangling_links_do_not_hide_a_later_valid_mention(tmp_path):
    reader = reader_of(
        tmp_path,
        DOC,
        CODE,
        links=[
            None,
            {**MENTION, "target": "missing"},
            {**MENTION, "source": "c", "target": "d"},
        ],
    )
    assert reader.engine_generation() == "mention-edges"
