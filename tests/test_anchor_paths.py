"""An anchor file_path given with backslashes (Windows) or as an absolute path under the
repository is stored repo-relative, at the MCP and the capture boundary, the way seeds are read.
# see design/superpowers/specs/2026-10-14-graphify-upgrade-design.md (D2, rider)"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from sidegraph import capture, worktree_seeds
from sidegraph.anchoring import anchor_roots, normalise_anchor_path, resolve_and_bind
from sidegraph.schema import Decision, DecisionKind, Descriptor, Provenance
from sidegraph.server import (
    _add_anchors_impl,
    _add_decision_impl,
    _add_fact_impl,
)
from sidegraph.store import Store
from tests.graph_fixtures import node, reader_of

REL = "src/sample.py"


def _setup(tmp_path):
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / REL).write_text("def sample():\n    pass\n")
    reader = reader_of(
        root, node("src_sample", "sample.py", REL), node("fn", "sample()", REL, location="L1")
    )
    return root, reader, Store(root / ".sidegraph")


def test_the_path_rule(tmp_path, monkeypatch):
    root = tmp_path
    monkeypatch.setattr(worktree_seeds, "WINDOWS", False)
    assert normalise_anchor_path("src\\sample.py", root) == "src\\sample.py"  # a legal POSIX name
    assert normalise_anchor_path(str(root / REL), root) == REL
    assert normalise_anchor_path("/elsewhere/src/sample.py", root) == "/elsewhere/src/sample.py"
    assert normalise_anchor_path(" ", root) is None and normalise_anchor_path(None, root) is None
    monkeypatch.setattr(worktree_seeds, "WINDOWS", True)
    assert normalise_anchor_path("src\\sample.py", root) == REL


def test_an_absolute_anchor_path_binds_live_through_the_mcp(tmp_path):
    # red against unfixed code: orphaned leaf, descriptor holds the absolute path
    root, reader, store = _setup(tmp_path)
    d = store.add_decision(
        Decision(
            title="t",
            kind=DecisionKind.ADR,
            context="c",
            choice="ch",
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual"),
        )
    )
    out = _add_anchors_impl(
        store, reader, d.id, [{"name": "sample()", "file_path": str(root / REL)}]
    )
    assert [b["canonical_name"] for b in out["bound"]] == ["sample()"] and out["orphaned"] == []
    assert store.find_entity("sample", REL) is not None


def test_a_backslash_anchor_path_binds_live_through_capture(tmp_path, monkeypatch):
    # red against unfixed code: orphaned with reason file-not-in-graph
    monkeypatch.setattr(worktree_seeds, "WINDOWS", True)
    _root, reader, store = _setup(tmp_path)
    draft = {
        "title": "Sample stays pure",
        "kind": "gotcha",
        "context": "c",
        "choice": "ch",
        "anchors": [{"name": "sample()", "file_path": "src\\sample.py"}],
    }
    (result,) = capture.propose([draft], store, reader)
    assert result.status == "written" and result.anchors_orphaned == []
    assert store.find_entity("sample", REL) is not None


def _decision(store):
    return store.add_decision(
        Decision(
            title="t",
            kind=DecisionKind.ADR,
            context="c",
            choice="ch",
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual"),
        )
    )


def _entities_at(store, rel):
    return [e for e in store.iter_concrete_entities() if e.descriptor.file_path == rel]


def test_drive_paths_are_made_relative_and_another_drive_is_left_alone(monkeypatch):
    monkeypatch.setattr(worktree_seeds, "WINDOWS", True)
    root = Path("C:/repo")
    assert normalise_anchor_path("C:\\Repo\\src\\x.py", root) == "src/x.py"
    assert normalise_anchor_path("c:/repo/src/x.py", root) == "src/x.py"
    assert normalise_anchor_path("D:\\other\\x.py", root) == "D:/other/x.py"
    monkeypatch.setattr(worktree_seeds, "WINDOWS", False)
    assert normalise_anchor_path("C:/repo/src/x.py", Path("/repo")) == "C:/repo/src/x.py"


def test_a_leading_dot_slash_goes(monkeypatch):
    monkeypatch.setattr(worktree_seeds, "WINDOWS", False)
    assert normalise_anchor_path("./src/x.py", Path("/repo")) == "src/x.py"
    assert normalise_anchor_path(".\\x.py", Path("/repo")) == ".\\x.py"  # a legal POSIX name
    monkeypatch.setattr(worktree_seeds, "WINDOWS", True)
    assert normalise_anchor_path(".\\src\\x.py", Path("C:/repo")) == "src/x.py"


def test_an_absolute_name_and_path_fold_to_one_identity_through_the_mcp(tmp_path):
    # red against the first rung: the name stayed absolute, a second entity was minted
    root, reader, store = _setup(tmp_path)
    absolute = str(root / REL)
    out = _add_anchors_impl(
        store, reader, _decision(store).id, [{"name": absolute, "file_path": absolute}]
    )
    assert out["orphaned"] == [] and len(out["bound"]) == 1
    assert [(e.canonical_name, e.descriptor.file_path) for e in _entities_at(store, REL)] == [
        ("sample.py", REL)
    ]
    assert not [e for e in store.iter_concrete_entities() if e.descriptor.file_path != REL]


def test_an_absolute_name_and_path_fold_to_one_identity_through_capture(tmp_path):
    root, reader, store = _setup(tmp_path)
    absolute = str(root / REL)
    draft = {
        "title": "Sample stays pure",
        "kind": "gotcha",
        "context": "c",
        "choice": "ch",
        "anchors": [{"name": absolute, "file_path": absolute}],
    }
    (result,) = capture.propose([draft], store, reader)
    assert result.status == "written" and result.anchors_orphaned == []
    assert [e.canonical_name for e in _entities_at(store, REL)] == ["sample.py"]


def test_resolve_and_bind_normalises_before_it_resolves(tmp_path, monkeypatch):
    # red against resolving first: the leaf is orphaned although the stored path resolves
    monkeypatch.setattr(worktree_seeds, "WINDOWS", True)
    _root, reader, store = _setup(tmp_path)
    out = resolve_and_bind(
        _decision(store).id, Descriptor(name="sample()", file_path="src\\sample.py"), reader, store
    )
    assert out.status == "resolved"
    (leaf,) = [b for b in out if b.tier == 2]
    assert leaf.status == "live"


def _nested(tmp_path, graph_path):
    """A store in ``repo/sub`` (project) inside the repository ``repo``; the graph holds
    ``graph_path`` for the file ``repo/sub/src/x.py``."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    sub = repo / "sub"
    (sub / "src").mkdir(parents=True)
    (sub / "src/x.py").write_text("def f():\n    pass\n")
    reader = reader_of(
        sub, node("x", "x.py", graph_path), node("f", "f()", graph_path, location="L1")
    )
    return sub, reader, Store(sub / ".sidegraph")


def test_the_candidate_roots_are_the_repository_then_the_project(tmp_path):
    sub, _reader, store = _nested(tmp_path, "src/x.py")
    assert anchor_roots(store.path) == [tmp_path / "repo", sub]
    assert anchor_roots(_setup(tmp_path / "other")[2].path) == [tmp_path / "other" / "repo"]


def test_a_nested_store_keeps_the_root_whose_path_the_graph_holds(tmp_path):
    # red against "repository root first": sub/src/x.py is not in the project's graph
    sub, reader, store = _nested(tmp_path, "src/x.py")
    out = _add_anchors_impl(
        store, reader, _decision(store).id, [{"name": "f()", "file_path": str(sub / "src/x.py")}]
    )
    assert [b["canonical_name"] for b in out["bound"]] == ["f()"] and out["orphaned"] == []
    assert store.find_entity("f", "src/x.py") is not None


def test_a_graph_rooted_at_the_repository_keeps_the_repository_root(tmp_path):
    # red against "project root only": src/x.py is not in a graph rooted at the repository
    sub, reader, store = _nested(tmp_path, "sub/src/x.py")
    out = _add_anchors_impl(
        store, reader, _decision(store).id, [{"name": "f()", "file_path": str(sub / "src/x.py")}]
    )
    assert out["orphaned"] == [] and store.find_entity("f", "sub/src/x.py") is not None


def test_an_absolute_path_binds_live_through_add_decision(tmp_path):
    root, reader, store = _setup(tmp_path)
    out = _add_decision_impl(
        store,
        reader,
        "t",
        "adr",
        "c",
        "ch",
        anchors=[{"name": "sample()", "file_path": str(root / REL)}],
    )
    assert out["anchors_orphaned"] == [] and store.find_entity("sample", REL) is not None


def test_an_absolute_path_binds_through_add_fact_with_and_without_a_reader(tmp_path):
    root, reader, store = _setup(tmp_path)
    anchors = [{"name": "sample()", "file_path": str(root / REL)}]
    out = _add_fact_impl(store, reader, "Samples are pure.", "observed", anchors=anchors)
    assert out["anchors_orphaned"] == [] and store.find_entity("sample", REL) is not None
    root2, _reader2, store2 = _setup(tmp_path / "again")
    _add_fact_impl(
        store2,
        None,
        "Samples are pure.",
        "observed",
        anchors=[{"name": "sample()", "file_path": str(root2 / REL)}],
    )
    assert store2.find_entity("sample", REL) is not None  # no reader: still repo-relative


def test_an_absolute_path_binds_live_through_capture_propose(tmp_path):
    root, reader, store = _setup(tmp_path)
    draft = {
        "title": "Sample stays pure",
        "kind": "gotcha",
        "context": "c",
        "choice": "ch",
        "anchors": [{"name": "sample()", "file_path": str(root / REL)}],
    }
    (result,) = capture.propose([draft], store, reader)
    assert result.anchors_orphaned == [] and store.find_entity("sample", REL) is not None


def test_a_name_missing_from_a_known_file_is_reported_for_that_file_through_add_decision(tmp_path):
    # the orphan reason reads the normalised path: the file IS in the graph, the name is not
    root, reader, store = _setup(tmp_path)
    out = _add_decision_impl(
        store,
        reader,
        "t",
        "adr",
        "c",
        "ch",
        anchors=[{"name": "nope()", "file_path": str(root / REL)}],
    )
    assert [o["reason"] for o in out["anchors_orphaned"]] == ["name-not-in-file"]


def test_a_draft_anchored_by_an_absolute_path_dedups_against_the_relative_one(tmp_path):
    root, reader, store = _setup(tmp_path)
    draft = {"title": "Sample stays pure", "kind": "gotcha", "context": "c", "choice": "ch"}
    (first,) = capture.propose(
        [{**draft, "anchors": [{"name": "sample()", "file_path": REL}]}], store, reader
    )
    assert first.status == "written"
    (again,) = capture.propose(
        [{**draft, "anchors": [{"name": "sample()", "file_path": str(root / REL)}]}], store, reader
    )
    assert again.status == "deduped"
