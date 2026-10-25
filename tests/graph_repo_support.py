"""Shared git-repository and graph.json builders for the stale-graph, borrowed-graph and
worktree tests.

``make_repo``/``stale_repo`` give one repository to every stale-graph surface (reader,
SessionStart, get_task_context, doctor, stats); ``make_main``/``add_worktree`` give the main
checkout and its linked worktrees; ``remember``/``serve``/``porcelain`` drive the MCP server
inside one. Support module: it holds no tests, and every test file may import from it.
# see design/superpowers/specs/2026-10-01-stale-graph-visible-design.md (4.1)
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple

from sidegraph import server
from sidegraph.schema import (
    AnchorBinding,
    Decision,
    DecisionKind,
    DecisionStatus,
    Descriptor,
    Entity,
    Provenance,
    Scope,
)
from sidegraph.store import Store

# Explicit mtimes, never the clock: a file edited before the build is older than graph.json,
# one edited after it is newer. Nanoseconds, as os.utime and st_mtime_ns want them.
T_FILE = 1_700_000_000 * 10**9
T_GRAPH = T_FILE + 100 * 10**9
T_AFTER = T_GRAPH + 100 * 10**9


class Fixture(NamedTuple):
    repo: Path
    graph: Path
    first: str  # commit A, the commit the graph is built at


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def head(repo: Path) -> str:
    return git(repo, "rev-parse", "HEAD")


def touch(path: Path, ns: int) -> None:
    os.utime(path, ns=(ns, ns))


def node(i: int, source_file: str) -> dict:
    return {
        "id": f"n{i}",
        "label": f"fn_{i}()",
        "norm_label": f"fn_{i}()",
        "file_type": "code",
        "source_file": source_file,
        "source_location": "L1",
        "community": 1,
    }


def write_graph(
    path: Path, built_at: str | None, source_files: list[str], mtime_ns: int = T_GRAPH
) -> None:
    """A minimal graph.json (shape of ``tests/fixtures/mini_graph.json``) at ``path``."""
    data: dict = {
        "directed": True,
        "multigraph": False,
        "nodes": [node(i, p) for i, p in enumerate(source_files)],
        "links": [],
    }
    if built_at is not None:
        data["built_at_commit"] = built_at
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    touch(path, mtime_ns)


def commit(repo: Path, message: str, files: dict[str, str | None], mtime_ns: int = T_AFTER) -> str:
    """Write (or, for ``None``, delete) files, commit them, and return the commit's sha.
    Written files get ``mtime_ns``: the edit is later than the build unless a test says so."""
    for rel, text in files.items():
        p = repo / rel
        if text is None:
            p.unlink()
            continue
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        touch(p, mtime_ns)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return head(repo)


_REPO_TEMPLATES: dict[tuple[str, bool], Fixture] = {}


def _repo_fixture(tmp_path: Path, graph_dir: str, stale: bool) -> Fixture:
    """Build the repository once per shape for the process and copy it, mtimes and all, so
    each test pays a directory copy and not a dozen git runs."""
    key = (graph_dir, stale)
    if key not in _REPO_TEMPLATES:
        holder = Path(tempfile.mkdtemp(prefix="sidegraph-repo-template-"))
        atexit.register(shutil.rmtree, holder, ignore_errors=True)
        _REPO_TEMPLATES[key] = _build_repo(holder, graph_dir, stale)
    template = _REPO_TEMPLATES[key]
    repo = tmp_path / "repo"
    shutil.copytree(template.repo, repo, symlinks=True)
    return Fixture(repo, repo / template.graph.relative_to(template.repo), template.first)


def _build_repo(tmp_path: Path, graph_dir: str, stale: bool) -> Fixture:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "commit.gpgsign", "false")
    first = commit(
        repo,
        "A",
        {".gitignore": "graphify-out/\n.cache/\n.sidegraph/\n", "pkg/m.py": "def a():\n    pass\n"},
        mtime_ns=T_FILE,
    )
    graph = repo / graph_dir / "graph.json"
    write_graph(graph, first, ["pkg/m.py"])
    if stale:
        commit(repo, "B", {"pkg/m.py": "def a():\n    return 1\n"})
    return Fixture(repo, graph, first)


def make_repo(tmp_path: Path, *, graph_dir: str = "graphify-out") -> Fixture:
    """A git repository whose commit A holds ``pkg/m.py``, and a graph.json built at A
    (ignored by git, like Graphify's real output) that holds exactly that file."""
    return _repo_fixture(tmp_path, graph_dir, stale=False)


def stale_repo(tmp_path: Path) -> Fixture:
    """The T1 repository: commit B edited ``pkg/m.py`` after the graph was built at A."""
    return _repo_fixture(tmp_path, "graphify-out", stale=True)


GRAPH = Path("graphify-out") / "graph.json"


_MAIN_TEMPLATES: dict[tuple[bool, tuple[str, ...]], Path] = {}


def make_main(
    tmp_path: Path, *, nested: bool = False, files: tuple[str, ...] = ("pkg/m.py",)
) -> Path:
    """The main checkout ``R``: one commit holding ``files`` (default ``pkg/m.py``) and a store
    (at ``R/.sidegraph``, or ``R/.config/sidegraph`` when ``nested``), and a gitignored graph
    built at that commit and holding those files (at ``R/graphify-out/graph.json``, or
    ``R/.config/graphify-out/``). Built once per shape for the process and copied, mtimes and
    all, so each test pays a directory copy and not a dozen git runs."""
    key = (nested, files)
    if key not in _MAIN_TEMPLATES:
        holder = Path(tempfile.mkdtemp(prefix="sidegraph-main-template-"))
        atexit.register(shutil.rmtree, holder, ignore_errors=True)
        _build_main(holder, nested=nested, files=files)
        _MAIN_TEMPLATES[key] = holder / "R"
    repo = tmp_path / "R"
    shutil.copytree(_MAIN_TEMPLATES[key], repo, symlinks=True)
    return repo


def _build_main(tmp_path: Path, *, nested: bool, files: tuple[str, ...]) -> Path:
    repo = tmp_path / "R"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "commit.gpgsign", "false")
    base = repo / ".config" if nested else repo
    Store(base / ("sidegraph" if nested else ".sidegraph")).close()
    first = commit(
        repo,
        "A",
        {".gitignore": "graphify-out/\n.worktrees/\n"}
        | {name: "def a():\n    pass\n" for name in files},
    )
    write_graph(base / GRAPH, first, list(files))
    return repo


def add_worktree(repo: Path, name: str = "w", *extra: str) -> Path:
    """``git worktree add R/.worktrees/<name>``: the tracked store, and no graph."""
    worktree = repo / ".worktrees" / name
    git(repo, "worktree", "add", "-q", *extra, str(worktree), "-b", f"wt-{name}")
    return worktree


def remember(
    store_path: Path,
    title: str,
    file_path: str,
    name: str = "m_py",
    scope: Scope = Scope.GLOBAL,
) -> None:
    """One accepted gotcha anchored to ``name`` in ``file_path``, in the store at
    ``store_path``."""
    store = Store(store_path)
    try:
        d = store.add_decision(
            Decision(
                title=title,
                kind=DecisionKind.GOTCHA,
                status=DecisionStatus.ACCEPTED,
                context="c",
                choice="ch",
                scope=scope,
                valid_from=datetime.now(UTC),
                provenance=Provenance(source="manual"),
            )
        )
        e = store.upsert_entity(
            Entity(
                canonical_name=name,
                descriptor=Descriptor(name=name, file_path=file_path),
            )
        )
        store.add_binding(
            AnchorBinding(record_id=d.id, entity_id=e.entity_id, tier=2, status="live")
        )
    finally:
        store.close()


def porcelain(repo: Path) -> str:
    return git(repo, "status", "--porcelain")


def serve(monkeypatch, checkout: Path) -> Store:
    """The MCP server started in ``checkout``: its store, its cwd."""
    monkeypatch.chdir(checkout)
    monkeypatch.setenv("SIDEGRAPH_DIR", str(checkout / ".sidegraph"))
    store = Store(checkout / ".sidegraph")
    monkeypatch.setattr(server, "_store", store)
    return store


def _impl(store, reader, files=None, entities=None, **kwargs) -> str:
    return server._get_task_context_impl(
        store, reader, files, entities, structure_budget=4000, memory_budget=6000, **kwargs
    )


def _block(out: str, heading: str) -> list[str]:
    assert heading in out, out
    return out.split(heading + "\n", 1)[1].split("\n\n", 1)[0].splitlines()
