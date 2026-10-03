"""``GraphifyReader.resolve_member_name`` finds ``Type.member`` with no file at all.

The file-bound fallback of ``resolve`` needs a file; a seed that names only ``Type.member`` has
none. This one matches across the whole graph, by the owner edge, and answers ``resolved``
(one node), ``ambiguous`` (several) or ``unresolved``. Member forms only: a plain name already
resolves through ``resolve(Descriptor(name, None))``. The identifier, code-node and case rules
are those of the file-bound fallback, with the case-twin rule applied to each candidate's own
file.
# see design/superpowers/specs/2026-10-02-tolerant-seeds-design.md (D2, test A8)
"""

from __future__ import annotations

import json
from pathlib import Path

from sidegraph.engine.reader import RESOLVER_REVISION, GraphifyReader


def _graph(tmp_path: Path, nodes: list[dict], links: list[dict] | None = None) -> GraphifyReader:
    full = [
        {
            "id": n["id"],
            "label": n["label"],
            "norm_label": n["label"].lower(),
            "file_type": n.get("file_type", "code"),
            "source_file": n["file"],
            "community": n.get("community", 1),
        }
        for n in nodes
    ]
    p = tmp_path / "g.json"
    p.write_text(json.dumps({"built_at_commit": "x", "nodes": full, "links": links or []}))
    return GraphifyReader(p)


def _two_owners(tmp_path: Path) -> GraphifyReader:
    """``A.run`` in ``a.py``, ``B.run`` in ``b.py``: the owner is what tells them apart."""
    return _graph(
        tmp_path,
        [
            {"id": "a", "label": "A", "file": "a.py"},
            {"id": "ra", "label": ".run()", "file": "a.py", "community": 3},
            {"id": "b", "label": "B", "file": "b.py"},
            {"id": "rb", "label": ".run()", "file": "b.py", "community": 4},
        ],
        [
            {"source": "a", "target": "ra", "relation": "method"},
            {"source": "b", "target": "rb", "relation": "method"},
        ],
    )


def test_a_type_dot_member_with_no_file_resolves_through_the_owner_edge(tmp_path):
    r = _two_owners(tmp_path)
    for name in ["A.run", "A::run", "A#run", "A.run()", ".A.run()"]:
        res = r.resolve_member_name(name)
        assert (res.status, res.node_id, res.community) == ("resolved", "ra", "3"), name
    res = r.resolve_member_name("B.run")
    assert (res.status, res.node_id, res.community) == ("resolved", "rb", "4")


def test_a_plain_name_is_not_a_member_form(tmp_path):
    r = _two_owners(tmp_path)
    for name in ["run", "A", "", "A.", ".run()", "::run", "A.()"]:
        assert r.resolve_member_name(name).status == "unresolved", name


def test_the_owner_must_be_named_with_its_case_kept(tmp_path):
    """Red against a case-insensitive owner check (mutation M6): ``a.run`` is not ``A.run``."""
    r = _two_owners(tmp_path)
    assert r.resolve_member_name("a.run").status == "unresolved"
    assert r.resolve_member_name("C.run").status == "unresolved"


def test_an_edgeless_member_has_no_owner_to_match(tmp_path):
    r = _graph(
        tmp_path,
        [
            {"id": "a", "label": "A", "file": "x.py"},
            {"id": "free", "label": ".run()", "file": "x.py"},
        ],
    )
    assert r.resolve_member_name("A.run").status == "unresolved"


def test_the_same_member_on_the_same_type_in_two_files_is_ambiguous(tmp_path):
    r = _graph(
        tmp_path,
        [
            {"id": "a1", "label": "A", "file": "a.py", "community": 2},
            {"id": "m1", "label": ".run()", "file": "a.py", "community": 2},
            {"id": "a2", "label": "A", "file": "a_ext.py", "community": 2},
            {"id": "m2", "label": ".run()", "file": "a_ext.py", "community": 2},
        ],
        [
            {"source": "a1", "target": "m1", "relation": "method"},
            {"source": "a2", "target": "m2", "relation": "method"},
        ],
    )
    res = r.resolve_member_name("A.run")
    assert res.status == "ambiguous"
    assert sorted(res.candidates) == ["m1", "m2"]
    assert res.node_id is None and res.community == "2"


def test_a_case_only_twin_in_the_candidates_own_file_drops_that_candidate(tmp_path):
    """The twin rule is per candidate file: ``Message`` beside ``.message()`` in ``a.py`` drops
    that candidate only; the one in ``b.py``, which has no twin, still resolves."""
    nodes = [
        {"id": "a", "label": "A", "file": "a.py"},
        {"id": "am", "label": ".message()", "file": "a.py"},
        {"id": "twin", "label": "Message", "file": "a.py"},
        {"id": "b", "label": "A", "file": "b.py"},
        {"id": "bm", "label": ".message()", "file": "b.py"},
    ]
    both = [
        {"source": "a", "target": "am", "relation": "method"},
        {"source": "b", "target": "bm", "relation": "method"},
    ]
    res = _graph(tmp_path, nodes, both).resolve_member_name("A.message")
    assert (res.status, res.node_id) == ("resolved", "bm")
    alone = _graph(tmp_path, nodes[:3], both[:1])
    assert alone.resolve_member_name("A.message").status == "unresolved"


def test_a_twin_in_another_file_does_not_drop_a_candidate(tmp_path):
    r = _graph(
        tmp_path,
        [
            {"id": "a", "label": "A", "file": "a.py"},
            {"id": "am", "label": ".message()", "file": "a.py"},
            {"id": "twin", "label": "Message", "file": "elsewhere.py"},
        ],
        [{"source": "a", "target": "am", "relation": "method"}],
    )
    res = r.resolve_member_name("A.message")
    assert (res.status, res.node_id) == ("resolved", "am")


def test_only_code_nodes_and_identifiers_count(tmp_path):
    r = _graph(
        tmp_path,
        [
            {"id": "a", "label": "A", "file": "x.py"},
            {"id": "doc", "label": ".run()", "file": "x.py", "file_type": "rationale"},
            {"id": "see", "label": "see A", "file": "x.py"},
            {"id": "sr", "label": ".run()", "file": "x.py"},
        ],
        [
            {"source": "a", "target": "doc", "relation": "method"},
            {"source": "see", "target": "sr", "relation": "method"},
        ],
    )
    assert r.resolve_member_name("A.run").status == "unresolved"
    assert r.resolve_member_name("see A.run").status == "unresolved"


def test_the_owner_is_narrowed_to_its_last_segment_and_generics_are_stripped(tmp_path):
    r = _graph(
        tmp_path,
        [
            {"id": "s", "label": "Stack", "file": "s.swift"},
            {"id": "p", "label": ".push()", "file": "s.swift"},
        ],
        [{"source": "s", "target": "p", "relation": "method"}],
    )
    for name in ["Stack<Element>.push", "Outer.Stack.push", "Stack<A.B>.push"]:
        res = r.resolve_member_name(name)
        assert (res.status, res.node_id) == ("resolved", "p"), name


def test_a_member_with_two_owner_edges_matches_when_one_of_them_is_the_named_type(tmp_path):
    r = _graph(
        tmp_path,
        [
            {"id": "a", "label": "A", "file": "x.py"},
            {"id": "b", "label": "B", "file": "x.py"},
            {"id": "m", "label": ".run()", "file": "x.py"},
        ],
        [
            {"source": "a", "target": "m", "relation": "method"},
            {"source": "b", "target": "m", "relation": "method"},
        ],
    )
    assert r.resolve_member_name("A.run").node_id == "m"
    assert r.resolve_member_name("B.run").node_id == "m"


def test_the_resolver_revision_is_unchanged(tmp_path):
    """Only retrieval calls the new method; ``resolve()`` and anchoring answer as before, so the
    revision folded into ``sync_stamp`` does not move."""
    assert RESOLVER_REVISION == 2
