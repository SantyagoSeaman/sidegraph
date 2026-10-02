"""``GraphifyReader.resolve`` falls back from ``Type.member`` to the member's own node.

Graphify labels a member ``.playClip()`` and links it to its type with a ``method`` edge (enum
cases with ``case_of``); an agent writes ``AudioPlayback.playClip``. Canonical names alone
never meet, so the fallback splits the name, matches the member case-sensitively inside the
descriptor's file, and checks the owner through the graph's own edges. Never without a file;
never when an edge names a different owner; ``unresolved`` (not ``ambiguous``) when narrowing
fails, so the write path keeps its orphaned, healable leaf.
# see design/superpowers/specs/2026-10-01-member-anchor-names-design.md (D1, tests T1-T10)

Fixtures: ``member_names_swift_graph.json`` is a Swift-shaped slice (an ``AudioPlayback`` type
with ``.name()`` method nodes, a nested ``State`` enum with ``case_of`` cases, a nested
``Message`` struct that sits beside a ``.message()`` method); ``member_names_python_graph.json``
is the ``engine/reader.py`` slice of this repository's own graph.
The spec's ledger (T1-T10, numbered the same here) is written against a private corpus, so the
shipped fixture renames its identifiers: the playback type is ``AudioPlayback``, in
``Sources/App/AudioPlayback.swift``.
"""

from __future__ import annotations

import json
from pathlib import Path

from sidegraph.engine.reader import GraphifyReader
from sidegraph.schema import Descriptor

FIXTURES = Path(__file__).parent / "fixtures"
SWIFT_FILE = "Sources/App/AudioPlayback.swift"
PY_FILE = "src/sidegraph/engine/reader.py"

_PREFIX = "sources_app_audioplayback"
PLAY_CLIP = f"{_PREFIX}_audioplayback_playclip"
STOP = f"{_PREFIX}_audioplayback_stop"
MESSAGE_METHOD = f"{_PREFIX}_audioplayback_message"
MESSAGE_STRUCT = f"{_PREFIX}_message"
STATE = f"{_PREFIX}_state"
STATE_IDLE = f"{_PREFIX}_state_idle"


def swift() -> GraphifyReader:
    return GraphifyReader(FIXTURES / "member_names_swift_graph.json")


def python() -> GraphifyReader:
    return GraphifyReader(FIXTURES / "member_names_python_graph.json")


def _resolve(reader: GraphifyReader, name: str, file_path: str | None = SWIFT_FILE):
    return reader.resolve(Descriptor(name=name, file_path=file_path))


def _graph(tmp_path: Path, nodes: list[dict], links: list[dict] | None = None) -> GraphifyReader:
    """A tiny hand-built graph: ``nodes`` are (id, label, file) triples as dicts."""
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


# -- T1, T2: the member shape ---------------------------------------------------------


def test_t1_type_dot_member_resolves_to_the_method_node():
    # red against unfixed code: canonical "audioplayback.playclip" matches no node -> unresolved
    r = _resolve(swift(), "AudioPlayback.playClip")
    assert r.status == "resolved"
    assert r.node_id == PLAY_CLIP
    assert r.community == "1"


def test_t2_other_separators_and_a_call_decoration_resolve_to_the_same_node():
    for name in [
        "AudioPlayback::playClip",
        "AudioPlayback#playClip",
        "AudioPlayback.playClip()",
        "AudioPlayback.playClip(clip: Int)",
        ".AudioPlayback.playClip()",
    ]:
        r = _resolve(swift(), name)
        assert (r.status, r.node_id) == ("resolved", PLAY_CLIP), name


# -- T3: nested owner -----------------------------------------------------------------


def test_t3_nested_owner_is_narrowed_by_its_last_segment():
    # red against unfixed code; also against mutation M3 (split at the FIRST separator, which
    # leaves member "State.idle" and no node of that label)
    r = _resolve(swift(), "AudioPlayback.State.idle")
    assert (r.status, r.node_id) == ("resolved", STATE_IDLE)
    r = _resolve(swift(), "State.idle")
    assert (r.status, r.node_id) == ("resolved", STATE_IDLE)
    # A nested type is a member of its outer type. `State` owns cases by `case_of` edges, and
    # those must not read as owning `State`: red against an owner index read in both directions.
    r = _resolve(swift(), "AudioPlayback.State")
    assert (r.status, r.node_id) == ("resolved", STATE)


# -- T4: the owner check --------------------------------------------------------------


def test_t4_a_lone_member_with_a_different_owner_is_rejected():
    # The sole `.stop()` in the file belongs to AudioPlayback by a `method` edge. Red against
    # rev 1 of the spec (mutation M4: skip the owner check), which resolved it.
    assert _resolve(swift(), "AudioPlayback.stop").node_id == STOP
    r = _resolve(swift(), "Nope.stop")
    assert (r.status, r.node_id) == ("unresolved", None)
    # a case_of member is held to its owner the same way
    r = _resolve(swift(), "AudioPlayback.idle")
    assert r.status == "unresolved"
    # and the owner is compared with its case kept: red against a case-insensitive owner check
    r = _resolve(swift(), "audioPlayback.playClip")
    assert (r.status, r.node_id) == ("unresolved", None)


# -- T5, T6: a case-only twin is never guessed -----------------------------------------


def test_t5_a_type_and_a_method_that_differ_only_by_case_bind_neither():
    # `Message` (a nested struct) and `.message()` (a method) differ only by case. The store
    # dedups an anchor by its lowercased name, so the two spellings share ONE entity: binding
    # either node would put the other spelling's record on it. Red against the first cut of
    # this branch, which bound the struct for `Message` and the method for `message`.
    for name in ["AudioPlayback.Message", "AudioPlayback.message"]:
        r = _resolve(swift(), name)
        assert (r.status, r.node_id) == ("unresolved", None), name


def test_the_twin_rule_looks_only_inside_the_given_file(tmp_path):
    r = _graph(
        tmp_path,
        [
            {"id": "a", "label": "A", "file": "x.py"},
            {"id": "m", "label": ".message()", "file": "x.py"},
            {"id": "other", "label": "Message", "file": "y.py"},
        ],
        [{"source": "a", "target": "m", "relation": "method"}],
    )
    res = r.resolve(Descriptor(name="A.message", file_path="x.py"))
    assert (res.status, res.node_id) == ("resolved", "m")


def test_t6_a_property_with_no_node_does_not_borrow_the_type_of_the_same_name():
    # `state` is a property Graphify does not node; `State` is the enum, a case-only twin.
    r = _resolve(swift(), "AudioPlayback.state")
    assert (r.status, r.node_id) == ("unresolved", None)


# -- T7: a file is required -----------------------------------------------------------


def test_t7_no_file_means_no_fallback():
    # Red against R2 (mutation M2): `playclip` alone would match across the whole repository.
    assert swift().resolve(Descriptor(name="AudioPlayback.playClip")).status == "unresolved"
    assert _resolve(swift(), "AudioPlayback.playClip", file_path="").status == "unresolved"
    assert _resolve(swift(), "AudioPlayback.playClip", file_path="  ").status == "unresolved"


def test_member_in_another_file_does_not_resolve():
    assert _resolve(
        swift(), "AudioPlayback.playClip", file_path="Sources/App/Other.swift"
    ).status == ("unresolved")


# -- T8: the exact match wins ---------------------------------------------------------


def test_t8_an_exact_match_is_used_and_the_fallback_is_not(tmp_path):
    # Guard, red against nothing: passes before and after. A node whose own label is
    # `Config.load` is the exact match; the `.load()` method of `Config` is not reached.
    r = _graph(
        tmp_path,
        [
            {"id": "cls", "label": "Config", "file": "src/cfg.py"},
            {"id": "m", "label": ".load()", "file": "src/cfg.py"},
            {"id": "alias", "label": "Config.load", "file": "src/cfg.py"},
        ],
        [{"source": "cls", "target": "m", "relation": "method"}],
    )
    res = r.resolve(Descriptor(name="Config.load", file_path="src/cfg.py"))
    assert (res.status, res.node_id) == ("resolved", "alias")


# -- T9: edgeless members -------------------------------------------------------------


def test_t9_two_edgeless_members_under_different_owners_stay_unresolved(tmp_path):
    # Static functions carry no `method` edge, so the owner cannot narrow them. Red against R4
    # (mutation M6: return `ambiguous`), which would drop the leaf on the write path.
    r = _graph(
        tmp_path,
        [
            {"id": "a", "label": "A", "file": "x.py"},
            {"id": "b", "label": "B", "file": "x.py"},
            {"id": "ra", "label": ".run()", "file": "x.py"},
            {"id": "rb", "label": ".run()", "file": "x.py"},
        ],
    )
    res = r.resolve(Descriptor(name="A.run", file_path="x.py"))
    assert (res.status, res.node_id, res.candidates) == ("unresolved", None, [])


def test_a_lone_edgeless_member_resolves(tmp_path):
    r = _graph(
        tmp_path,
        [
            {"id": "a", "label": "A", "file": "x.py"},
            {"id": "ra", "label": ".run()", "file": "x.py"},
        ],
    )
    res = r.resolve(Descriptor(name="A.run", file_path="x.py"))
    assert (res.status, res.node_id) == ("resolved", "ra")


def test_several_same_named_members_are_narrowed_by_their_owner_edge(tmp_path):
    r = _graph(
        tmp_path,
        [
            {"id": "a", "label": "A", "file": "x.py"},
            {"id": "b", "label": "B", "file": "x.py"},
            {"id": "ra", "label": ".run()", "file": "x.py", "community": 3},
            {"id": "rb", "label": ".run()", "file": "x.py", "community": 4},
        ],
        [
            {"source": "a", "target": "ra", "relation": "method"},
            {"source": "b", "target": "rb", "relation": "method"},
        ],
    )
    res = r.resolve(Descriptor(name="B.run", file_path="x.py"))
    assert (res.status, res.node_id, res.community) == ("resolved", "rb", "4")
    assert r.resolve(Descriptor(name="C.run", file_path="x.py")).status == "unresolved"


def test_a_member_tied_to_the_named_owner_wins_over_an_edgeless_namesake(tmp_path):
    # Of several candidates that survive the owner check, only the one an owner edge ties to
    # the named type stays; the edgeless `.run()` (a free function, say) is dropped.
    r = _graph(
        tmp_path,
        [
            {"id": "a", "label": "A", "file": "x.py"},
            {"id": "free", "label": ".run()", "file": "x.py"},
            {"id": "ra", "label": ".run()", "file": "x.py"},
        ],
        [{"source": "a", "target": "ra", "relation": "method"}],
    )
    res = r.resolve(Descriptor(name="A.run", file_path="x.py"))
    assert (res.status, res.node_id) == ("resolved", "ra")


def test_owner_generics_are_stripped(tmp_path):
    r = _graph(
        tmp_path,
        [
            {"id": "s", "label": "Stack", "file": "s.swift"},
            {"id": "p", "label": ".push()", "file": "s.swift"},
        ],
        [{"source": "s", "target": "p", "relation": "method"}],
    )
    for name in ["Stack<Element>.push", "Stack<Dictionary<String, Int>>.push", "Stack<A.B>.push"]:
        res = r.resolve(Descriptor(name=name, file_path="s.swift"))
        assert (res.status, res.node_id) == ("resolved", "p"), name


def test_a_path_qualified_name_still_never_resolves():
    # `src/sidegraph/engine/reader.py` splits into the member `py`, which no node carries.
    r = python().resolve(Descriptor(name="src/sidegraph/engine/reader.py", file_path=PY_FILE))
    assert r.status == "unresolved"


def test_a_degenerate_name_does_not_fall_back():
    for name in ["AudioPlayback.", "::playClip", "AudioPlayback.()", ".", "#"]:
        assert _resolve(swift(), name).status == "unresolved", name


# -- T10: the Python shape ------------------------------------------------------------


def test_t10_python_class_dot_method_resolves():
    r = python().resolve(Descriptor(name="GraphifyReader.resolve", file_path=PY_FILE))
    assert (r.status, r.node_id) == (
        "resolved",
        "src_sidegraph_engine_reader_graphifyreader_resolve",
    )
    # a private member keeps its underscore; only the leading `.` is decoration
    r = python().resolve(Descriptor(name="GraphifyReader._resolve_index", file_path=PY_FILE))
    assert r.node_id == "src_sidegraph_engine_reader_graphifyreader_resolve_index"
    r = python().resolve(Descriptor(name="ResolveResult.resolve", file_path=PY_FILE))
    assert r.status == "unresolved"


# -- code identifiers only ---------------------------------------------------------------
# The fallback is for `Type.member`, not for any text with a dot in it. Prose, a numbered
# heading and a hyphenated file name all split at a `.`, and the pieces were matched against
# nodes of every kind. `file_type == "code"` is what separates members from rationale
# sentences and doc headings in both real graphs (every target of a `method` or `case_of` edge
# is `code`); it does not separate the file-name shape, which only the identifier rule stops.


def test_prose_with_a_dotted_name_in_it_binds_no_node():
    # red against the first cut: owner "see reader" is prose, and the edgeless class matches
    assert _resolve(python(), "see reader.NodeRef", PY_FILE).status == "unresolved"
    assert _resolve(python(), "Community.NodeRef", PY_FILE).status == "resolved"


def test_a_rationale_sentence_with_a_call_in_it_binds_nothing(tmp_path):
    r = _graph(
        tmp_path,
        [
            {
                "id": "r",
                "label": "check() returns the verdict",
                "file": "x.py",
                "file_type": "rationale",
            },
        ],
    )
    assert r.resolve(Descriptor(name="Callers run x.check() first", file_path="x.py")).status == (
        "unresolved"
    )
    # and a rationale node is not a member even under a well-formed owner
    assert r.resolve(Descriptor(name="X.check", file_path="x.py")).status == "unresolved"


def test_a_doc_heading_is_not_a_member(tmp_path):
    r = _graph(
        tmp_path,
        [
            {"id": "h", "label": "Testing", "file": "docs/g.md", "file_type": "document"},
            {"id": "c", "label": "Testing", "file": "docs/g.py"},
        ],
    )
    # a numbered heading: owner "8" is no identifier, whatever kind of node the member is
    assert r.resolve(Descriptor(name="8. Testing", file_path="docs/g.md")).status == "unresolved"
    assert r.resolve(Descriptor(name="8. Testing", file_path="docs/g.py")).status == "unresolved"
    # a well-formed owner, but the candidate is a heading
    assert r.resolve(Descriptor(name="Guide.Testing", file_path="docs/g.md")).status == (
        "unresolved"
    )
    assert r.resolve(Descriptor(name="Guide.Testing", file_path="docs/g.py")).status == "resolved"


def test_a_hyphenated_file_name_is_not_a_qualified_name(tmp_path):
    r = _graph(tmp_path, [{"id": "sh", "label": "sh", "file": ".mcp.json"}])
    res = r.resolve(Descriptor(name="verify-release.sh", file_path=".mcp.json"))
    assert (res.status, res.node_id) == ("unresolved", None)


def test_identifiers_are_unicode_aware(tmp_path):
    r = _graph(
        tmp_path,
        [
            {"id": "t", "label": "Тип", "file": "x.py"},
            {"id": "m", "label": ".метод()", "file": "x.py"},
        ],
        [{"source": "t", "target": "m", "relation": "method"}],
    )
    res = r.resolve(Descriptor(name="Тип.метод", file_path="x.py"))
    assert (res.status, res.node_id) == ("resolved", "m")
