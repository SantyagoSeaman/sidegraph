"""File-node labels that carry a parent path read as basenames again, in the engine seam.

Graphify relabels a file whose basename another file shares with its shortest unique path
suffix (``sidegraph/cli.py``), and an incremental update keeps that label after the other file
is deleted. The reader gives such a node its basename back and keeps the engine label as an alias.
# see design/superpowers/specs/2026-10-14-graphify-upgrade-design.md (D2)"""

from __future__ import annotations

from sidegraph.doc_import import _file_node_descriptor
from sidegraph.engine.reader import RESOLVER_REVISION
from sidegraph.schema import Descriptor
from tests.graph_fixtures import BOOT_CLI, CLI, node, reader_of, two_cli_graph


def test_a_relabelled_file_node_reads_as_its_basename(tmp_path):
    # red against unfixed code: the name is "sidegraph/cli.py"
    n = two_cli_graph(tmp_path).get_node("src_sidegraph_cli")
    assert (n.name, n.norm_name) == ("cli.py", "cli.py")


def test_a_basename_with_its_file_resolves_to_that_files_node(tmp_path):
    # red against unfixed code: unresolved
    r = two_cli_graph(tmp_path)
    assert r.resolve(Descriptor(name="cli.py", file_path=CLI)).node_id == "src_sidegraph_cli"
    assert r.resolve(Descriptor(name="cli.py", file_path=BOOT_CLI)).node_id == (
        "src_sidegraph_bootstrap_cli"
    )


def test_a_path_less_shared_basename_stays_ambiguous_as_on_0_9_6(tmp_path):
    # red against unfixed code: unresolved
    r = two_cli_graph(tmp_path).resolve(Descriptor(name="cli.py"))
    assert r.status == "ambiguous"
    assert sorted(r.candidates) == ["src_sidegraph_bootstrap_cli", "src_sidegraph_cli"]


def test_the_engine_label_still_resolves(tmp_path):
    # characterisation, green before and after: guards the alias
    r = two_cli_graph(tmp_path)
    assert (
        r.resolve(Descriptor(name="sidegraph/cli.py", file_path=CLI)).node_id == "src_sidegraph_cli"
    )
    assert r.resolve(Descriptor(name="sidegraph/cli.py")).node_id == "src_sidegraph_cli"


def test_a_package_specifier_keeps_its_label(tmp_path):
    # red against the naive rule (a discarded design, declared); green on unfixed code
    assert two_cli_graph(tmp_path).get_node("inquirer_prompts").name == "@inquirer/prompts"


def test_a_suffix_label_left_after_the_other_file_was_deleted_reads_as_the_basename(tmp_path):
    # red against unfixed code AND against the shared-basename precondition (discarded, declared)
    r = reader_of(
        tmp_path,
        node("src_sidegraph_cli", "sidegraph/cli.py", CLI),
        node("main", "main()", CLI, location="L10"),
    )
    assert r.get_node("src_sidegraph_cli").name == "cli.py"
    assert r.resolve(Descriptor(name="cli.py", file_path=CLI)).status == "resolved"


def test_an_empty_colliding_file_without_a_line_is_still_a_file_node(tmp_path):
    # red against unfixed code; red against an "L1 only" rule (discarded, declared)
    r = reader_of(
        tmp_path,
        node("tests_init", "tests/__init__.py", "tests/__init__.py", location=None),
        node("src_init", "sidegraph/__init__.py", "src/sidegraph/__init__.py"),
    )
    assert r.get_node("tests_init").name == "__init__.py"


def test_nothing_is_renamed_on_a_0_9_6_shaped_graph(tmp_path):
    # red against nothing: the regression guard
    nodes = [
        node("a", "cli.py", CLI),
        node("b", "cli.py", BOOT_CLI, community=2),
        node("c", "main()", CLI, location="L10"),
    ]
    r = reader_of(tmp_path, *nodes)
    assert [n.name for n in r.list_nodes()] == ["cli.py", "cli.py", "main()"]


def test_doc_import_anchors_a_colliding_document_to_its_own_file_node(tmp_path):
    # red against unfixed code: None
    r = reader_of(
        tmp_path,
        node("a", "a/README.md", "docs/a/README.md", file_type="document"),
        node("b", "b/README.md", "docs/b/README.md", file_type="document", community=2),
    )
    assert _file_node_descriptor("docs/a/README.md", r) == Descriptor(
        name="README.md", file_path="docs/a/README.md"
    )


STORE = "src/sidegraph/store.py"


def _graph_0_9_6(tmp_path):
    return reader_of(tmp_path, node("src_sidegraph_store", "store.py", STORE))


def test_a_full_path_name_resolves_to_its_file_node_on_a_0_9_6_graph(tmp_path):
    # red against unfixed code: unresolved (intended change 2a)
    r = _graph_0_9_6(tmp_path).resolve(Descriptor(name=STORE, file_path=STORE))
    assert (r.status, r.node_id) == ("resolved", "src_sidegraph_store")


def test_a_suffix_name_resolves_to_its_file_node_on_a_0_9_6_graph(tmp_path):
    # red against unfixed code: unresolved
    r = _graph_0_9_6(tmp_path).resolve(Descriptor(name="sidegraph/store.py", file_path=STORE))
    assert r.node_id == "src_sidegraph_store"


def test_a_path_name_of_another_file_or_without_a_file_does_not_resolve(tmp_path):
    # red against nothing: a guard on the rung's two preconditions
    r = _graph_0_9_6(tmp_path)
    assert r.resolve(Descriptor(name="other/store.py", file_path=STORE)).status == "unresolved"
    assert r.resolve(Descriptor(name=STORE)).status == "unresolved"


def test_a_full_path_name_resolves_on_a_0_9_90_graph(tmp_path):
    # red against unfixed code: unresolved
    assert two_cli_graph(tmp_path).resolve(Descriptor(name=CLI, file_path=CLI)).node_id == (
        "src_sidegraph_cli"
    )


def test_the_resolver_revision_moved_so_each_store_resyncs_once():
    """Path names moved the revision to 3; the type-split moved it to 4."""
    assert RESOLVER_REVISION == 4


def test_a_package_specifier_anchor_stays_live_across_a_sync(tmp_path):
    # red against the 313-3 rung: the store folds "@inquirer/core" to "core", which the seam
    # (rightly) keeps as a specifier, so the rebind orphans it
    from datetime import UTC, datetime

    from sidegraph.anchoring import resolve_and_bind
    from sidegraph.schema import Decision, DecisionKind, DecisionStatus, Provenance
    from sidegraph.store import Store
    from sidegraph.sync import rebind_entity

    r = reader_of(
        tmp_path,
        node("inq", "@inquirer/core", "@inquirer/core", location=None),
        node("f", "update.ts", "src/update.ts"),
    )
    s = Store(tmp_path / ".sidegraph")
    d = s.add_decision(
        Decision(
            title="t",
            kind=DecisionKind.ADR,
            status=DecisionStatus.ACCEPTED,
            context="c",
            choice="ch",
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual"),
        )
    )
    out = resolve_and_bind(d.id, Descriptor(name="@inquirer/core"), r, s)
    leaf = next(b for b in out if b.tier == 2)
    assert leaf.status == "live"
    assert rebind_entity(s.get_entity(leaf.entity_id), s, r).status == "unchanged"


def test_an_empty_file_whose_collision_partner_was_deleted_resolves_by_basename(tmp_path):
    # red against the 313-3 rung: no L1 and no collision left, so the label keeps its suffix
    r = reader_of(
        tmp_path,
        node("tests_init", "tests/__init__.py", "tests/__init__.py", location=None),
        node("t", "test_x.py", "tests/test_x.py"),
    )
    got = r.resolve(Descriptor(name="__init__.py", file_path="tests/__init__.py"))
    assert (got.status, got.node_id) == ("resolved", "tests_init")


def test_a_non_file_node_keeps_its_slash_label_and_is_not_a_path_rung_target(tmp_path):
    # guards the file-type gates of the relabel rule and of the path rung
    r = reader_of(
        tmp_path,
        node("rat", "src/notes.py", "src/notes.py", file_type="rationale"),
        node("a", "notes.py", "src/notes.py"),
    )
    assert r.get_node("rat").name == "src/notes.py"
    only = reader_of(tmp_path, node("rat", "src/notes.py", "src/notes.py", file_type="rationale"))
    assert (
        only.resolve(Descriptor(name="notes.py", file_path="src/notes.py")).status == "unresolved"
    )
