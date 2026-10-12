"""Repo-relative paths come out in the POSIX spelling anchors and the hot index key on.

``os.path.relpath`` returns backslashes on Windows, so every site that joins its result to an
anchored path must convert it. The assertions are exact strings: they pass trivially on POSIX and
bite on Windows (the ``windows-core`` CI job runs this file).
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path

import pytest

import sidegraph.host.hooks as hooks
from sidegraph import bash_paths, server, text_paths
from tests.test_host_pretool_records import SESSION, Project

RELS = ["src/sample.py", "src/pkg/deep/nested.py", "src/ünï.py"]
SPACED = "src/with space/sample.py"  # prose splits on a space


@pytest.fixture
def root(tmp_path: Path) -> Path:
    base = tmp_path / "repo"
    for rel in [*RELS, SPACED]:
        (base / rel).parent.mkdir(parents=True, exist_ok=True)
        (base / rel).write_text("x = 1\n")
    return base


@pytest.mark.parametrize("rel", [*RELS, SPACED])
def test_touch_path_is_posix(root: Path, rel: str) -> None:
    assert hooks._touch_path({"file_path": str(root / rel)}, str(root)) == rel
    assert hooks._touch_path({"path": rel}, str(root)) == rel
    # The native relative spelling (src\\sample.py on Windows), as a Grep `path` can carry.
    assert hooks._touch_path({"path": str(Path(rel))}, str(root)) == rel


@pytest.mark.parametrize("rel", RELS)
def test_text_paths_are_posix(root: Path, rel: str) -> None:
    for spelling in (rel, str(Path(rel)), str(root / rel)):
        assert text_paths.text_paths(f"see `{spelling}` now", str(root), []) == [rel]
        assert text_paths.brief_files(f"edit {spelling}", str(root), []) == [(None, rel)]


@pytest.mark.parametrize("rel", [*RELS, SPACED])
def test_bash_read_paths_are_posix(root: Path, rel: str) -> None:
    assert bash_paths.read_paths(f'cat "{rel}"', str(root), str(root)) == [rel]
    assert bash_paths.read_paths(f'cat "{Path(rel)}"', str(root), str(root)) == [rel]
    assert bash_paths.read_paths(f'cat "{root / rel}"', str(root), str(root)) == [rel]


def test_a_file_outside_the_root_names_nothing(root: Path) -> None:
    # The guards run on the native relpath (..\\ on Windows) before the POSIX conversion.
    outside = root.parent / "other" / "x.py"
    outside.parent.mkdir(parents=True)
    outside.write_text("x = 1\n")
    assert hooks._touch_path({"file_path": str(outside)}, str(root)) is None
    assert text_paths.text_paths(f"see `{outside}` now", str(root), []) == []
    assert bash_paths.read_paths(f'cat "{outside}"', str(root), str(root)) == []


def test_a_root_level_name_starting_with_two_dots_is_inside(root: Path) -> None:
    (root / "..notes.py").write_text("x = 1\n")
    assert hooks._touch_path({"file_path": str(root / "..notes.py")}, str(root)) == "..notes.py"


def test_bash_read_from_a_subdirectory_is_root_relative(root: Path) -> None:
    cwd = root / "src" / "pkg"
    assert bash_paths.read_paths("cat deep/nested.py", str(cwd), str(root)) == [
        "src/pkg/deep/nested.py"
    ]


def test_seed_normalisation_is_posix(root: Path) -> None:
    store = Project(root).store
    try:
        seeds = [str(root / "src" / "pkg" / "deep" / "nested.py"), "src/sample.py"]
        assert server._normalized_seeds(store, seeds) == ["src/pkg/deep/nested.py", "src/sample.py"]
    finally:
        store.close()


def test_pretool_read_with_a_native_absolute_path_delivers_the_record(
    root: Path, monkeypatch, capsys
) -> None:
    project = Project(root)
    try:
        project.anchored("src/sample.py")
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(root))
        monkeypatch.setenv("SIDEGRAPH_DIR", str(project.store_dir))
        payload = {
            "cwd": str(root),
            "session_id": SESSION,
            "tool_name": "Read",
            "tool_input": {"file_path": str(root / "src" / "sample.py")},
        }
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
        hooks.pre_tool_use()
        out = json.loads(capsys.readouterr().out)
        assert "Gotcha of src/sample.py" in out["hookSpecificOutput"]["additionalContext"]
    finally:
        project.close()


@pytest.mark.parametrize(
    "transcript, expected",
    [
        (r"C:\Users\runner\.claude\projects\-repo\abc-123.jsonl", "abc-123"),
        (
            r"C:\Users\runner\.codex\sessions\2026\10\12\rollout-2026-10-12T11-44-42-th1.jsonl",
            "rollout-2026-10-12T11-44-42-th1",
        ),
        ("/srv/hosts/claude/projects/-repo/abc-123.jsonl", "abc-123"),
    ],
)
def test_session_key_is_the_transcript_stem_on_every_platform(
    transcript: str, expected: str
) -> None:
    # A Windows transcript path once keyed the whole path ("C:\\...\\abc-123"), so hook journals
    # never met the session id the MCP server resolves from the host.
    key, _group = hooks._session_identity({"session_id": "abc-123", "transcript_path": transcript})
    assert key == expected


# --- doc import (the same class: os.path.relpath backslashes against POSIX globs/keys) ---

DOC_RELS = ["docs/adr/0001-x.md", "docs/adr/sub/0002-nested.md", "docs/adr/0003-ünï.md"]


@pytest.fixture
def doc_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    for rel in DOC_RELS:
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("# x\n")
    return tmp_path


@pytest.mark.parametrize("rel", DOC_RELS)
def test_doc_import_repo_relative_is_posix(doc_root: Path, rel: str) -> None:
    from sidegraph.doc_import import _matches_ingest_globs, _repo_relative

    for spelling in (rel, str(Path(rel)), str(doc_root / rel)):
        assert _repo_relative(spelling, doc_root) == rel
    # The glob input is what the import feeds it: the converted native spelling.
    keyed = _repo_relative(str(Path(rel)), doc_root)
    assert _matches_ingest_globs(keyed, ("docs/adr/*.md", "docs/adr/sub/*.md"))


def test_doc_import_in_repo_is_exact(tmp_path: Path) -> None:
    from sidegraph.doc_import import _in_repo

    assert not _in_repo("../x.md")  # the POSIX spelling, whatever os.sep is
    assert _in_repo("..notes/x.md")
    assert not _in_repo(os.path.join(os.pardir, "x.md"))
    assert not _in_repo(os.pardir)
    assert not _in_repo(str(tmp_path / "x.md"))
    assert _in_repo("..notes.md")
    assert _in_repo(os.path.join("docs", "..notes", "x.md"))


def test_doc_import_profile_import_keys_are_posix(doc_root: Path) -> None:
    from sidegraph.doc_import import import_docs
    from sidegraph.engine.reader import GraphifyReader
    from sidegraph.store import Store
    from tests.test_doc_import import IDENTIFIER_GRAPH, _adr, _write_graph

    flat = [rel for rel in DOC_RELS if rel.count("/") == 2]  # the profile globs are one level
    body = _adr("Use SQLite", decision="Calls `submit_order`.")
    for rel in flat:
        (doc_root / rel).write_text(body)
    reader = GraphifyReader(_write_graph(doc_root, "g.json", IDENTIFIER_GRAPH))
    store = Store(doc_root / "s.db")
    paths = [os.path.relpath(doc_root / rel) for rel in flat]

    report = import_docs(store, reader, paths)

    assert report.skipped_outside_profile == 0
    assert report.imported == len(flat)
    refs = sorted(d.provenance.ref for d in store.iter_decisions())
    assert all("\\" not in ref for ref in refs)
    assert set(refs) == set(flat)


# --- doc import: the remaining conversion sites (links, outside documents, other drives) ---


@pytest.fixture
def linked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """repo/docs/adr/0001-x.md, plus out/ beside the repo, with the cwd in the repo."""
    repo = tmp_path / "repo"
    (repo / "docs" / "adr").mkdir(parents=True)
    (repo / "docs" / "adr" / "0001-x.md").write_text("# x\n")
    (tmp_path / "out").mkdir()
    monkeypatch.chdir(repo)
    return repo


def _link(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=target.is_dir())
    except OSError:
        pytest.skip("symlinks unavailable here")


def test_doc_import_symlinked_prefix_keys_the_repo_path(linked: Path) -> None:
    from sidegraph.doc_import import _repo_relative

    alias = linked.parent / "alias"
    _link(alias, linked)
    assert _repo_relative(str(alias / "docs" / "adr" / "0001-x.md"), linked) == "docs/adr/0001-x.md"


def test_doc_import_outside_directory_link_keys_the_repo_path(linked: Path) -> None:
    from sidegraph.doc_import import _repo_relative

    linkdir = linked.parent / "out" / "linkdir"
    _link(linkdir, linked / "docs" / "adr")
    assert _repo_relative(str(linkdir / "0001-x.md"), linked) == "docs/adr/0001-x.md"


def test_doc_import_outside_file_link_into_the_profile_is_imported(linked: Path) -> None:
    from sidegraph.doc_import import import_docs
    from sidegraph.engine.reader import GraphifyReader
    from sidegraph.store import Store
    from tests.test_doc_import import IDENTIFIER_GRAPH, _adr, _write_graph

    target = linked / "docs" / "adr" / "0001-x.md"
    target.write_text(_adr("Use SQLite", decision="Calls `submit_order`."))
    link = linked.parent / "out" / "link.md"
    _link(link, target)
    reader = GraphifyReader(_write_graph(linked, "g.json", IDENTIFIER_GRAPH))
    store = Store(linked / "s.db")

    report = import_docs(store, reader, [str(link)])

    assert (report.imported, report.skipped_outside_profile) == (1, 0)


def test_doc_import_absolute_outside_document_binds_a_posix_file_node(linked: Path) -> None:
    from sidegraph.doc_import import import_docs
    from sidegraph.engine.reader import GraphifyReader
    from sidegraph.store import Store
    from tests.test_doc_import import _SUBMIT_NODE, _adr, _file_node, _graph_of, _write_graph

    outside = linked.parent / "out" / "x.md"
    outside.write_text(_adr("Out", decision="Calls `submit_order`."))
    node_key = Path(os.path.relpath(outside, linked)).as_posix()
    graph = _graph_of(_SUBMIT_NODE, _file_node(node_key))
    reader = GraphifyReader(_write_graph(linked, "g.json", graph))
    store = Store(linked / "s.db")

    report = import_docs(store, reader, [str(outside)], any_doc=True)

    assert report.imported == 1
    decision = next(store.iter_decisions())
    names = {
        store.get_entity(b.entity_id).canonical_name
        for b in store.bindings_for_record(decision.id)
        if b.tier == 2
    }
    assert "x.md" in names


def test_doc_import_document_on_another_drive_does_not_crash(
    linked: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``ntpath.relpath`` raises ValueError across drives; simulate it for any document that
    lies outside the repository."""
    from sidegraph import doc_import
    from sidegraph.engine.reader import GraphifyReader
    from sidegraph.store import Store
    from tests.test_doc_import import IDENTIFIER_GRAPH, _adr, _write_graph

    outside = linked.parent / "out" / "x.md"
    outside.write_text(_adr("Out", decision="Calls `submit_order`."))
    real_relpath = os.path.relpath

    def cross_drive(path, start=os.curdir):
        if Path(path).name == "x.md":
            raise ValueError("path is on mount 'D:', start on mount 'C:'")
        return real_relpath(path, start)

    monkeypatch.setattr(doc_import.os.path, "relpath", cross_drive)
    assert doc_import._relpath(outside, linked) is None
    assert doc_import._repo_relative(str(outside), linked) == str(outside)
    reader = GraphifyReader(_write_graph(linked, "g.json", IDENTIFIER_GRAPH))
    store = Store(linked / "s.db")

    report = doc_import.import_docs(store, reader, [str(outside)], any_doc=True)

    assert report.imported == 1
    assert next(store.iter_decisions()).provenance.ref == str(outside)
    # Inside the repo a link whose resolved target sits on another drive is refused, not a crash.
    link = linked / "docs" / "adr" / "far.md"
    _link(link, outside)
    report = doc_import.import_docs(store, reader, [str(link)], any_doc=True)
    assert report.skipped_outside_repo == 1


def test_doc_import_relative_outside_document_key_is_posix(
    linked: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulates Windows separators: a POSIX file literally named ``a\\x.md`` stands for the
    native ``a\\x.md`` path, so with ``os.sep`` patched the stored key must read ``a/x.md``."""
    from sidegraph import doc_import
    from sidegraph.engine.reader import GraphifyReader
    from sidegraph.store import Store
    from tests.test_doc_import import IDENTIFIER_GRAPH, _adr, _write_graph

    if os.sep != "/":
        pytest.skip("the simulation is for POSIX hosts")
    (linked.parent / "out" / "a\\x.md").write_text(_adr("Out", decision="Calls `submit_order`."))
    reader = GraphifyReader(_write_graph(linked, "g.json", IDENTIFIER_GRAPH))
    store = Store(linked / "s.db")
    monkeypatch.setattr(os, "sep", "\\")

    report = doc_import.import_docs(store, reader, ["../out/a\\x.md"], any_doc=True)

    assert report.imported == 1
    assert next(store.iter_decisions()).provenance.ref == "../out/a/x.md"
