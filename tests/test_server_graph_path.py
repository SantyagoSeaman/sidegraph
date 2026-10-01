"""The MCP server pairs the store it serves with that store's own graph, by the same rule the
CLI uses (design/superpowers/specs/2026-09-30-graph-path-one-rule-design.md)."""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path

import pytest

import sidegraph
import sidegraph.cli as cli
from sidegraph import server
from sidegraph.engine.reader import GraphifyReader
from sidegraph.store import Store
from sidegraph.sync import LAST_SYNCED_KEY


def _node(i):
    return {
        "id": f"n{i}",
        "label": f"f{i}()",
        "norm_label": f"f{i}()",
        "file_type": "code",
        "source_file": f"m{i}.py",
        "community": i,
    }


def _write_graph(project: Path, commit: str, nodes: int) -> Path:
    g = project / "graphify-out" / "graph.json"
    g.parent.mkdir(parents=True, exist_ok=True)
    g.write_text(
        json.dumps(
            {"built_at_commit": commit, "nodes": [_node(i) for i in range(nodes)], "links": []}
        )
    )
    return g


def _version(g: Path) -> str:
    return GraphifyReader(str(g)).graph_version()


@pytest.fixture
def two_projects(tmp_path):
    a, b = tmp_path / "A", tmp_path / "B"
    Store(a / ".sidegraph").close()
    Store(b / ".sidegraph").close()
    ga, gb = _write_graph(a, "commitA", 1), _write_graph(b, "commitB", 3)
    assert _version(ga) != _version(gb)
    return a, b, ga, gb


def _stamp(store_dir: Path):
    s = Store(store_dir)
    try:
        return s.get_meta(LAST_SYNCED_KEY)
    finally:
        s.close()


@pytest.fixture
def mcp_in_b(two_projects, monkeypatch):
    """The MCP server, started in project B's directory, serving project A's store."""
    a, b, ga, gb = two_projects
    monkeypatch.chdir(b)
    monkeypatch.setenv("SIDEGRAPH_DIR", str(a / ".sidegraph"))
    monkeypatch.setattr(server, "_store", Store(a / ".sidegraph"))
    return a, b, ga, gb


@pytest.mark.parametrize(
    "env", ["graphify-out/graph.json", None], ids=["env-relative", "env-unset"]
)
def test_mcp_sync_anchors_keeps_the_version_the_cli_stamped(mcp_in_b, monkeypatch, env):
    a, b, ga, gb = mcp_in_b
    if env is None:
        monkeypatch.delenv("SIDEGRAPH_GRAPH", raising=False)
    else:
        monkeypatch.setenv("SIDEGRAPH_GRAPH", env)
    assert cli.sync_main([]) == 0
    assert _stamp(a / ".sidegraph") == _version(ga)

    out = server._sync_anchors_impl(server._get_store(), server._load_reader())

    assert out.get("synced") is False, out
    assert out.get("error") is None, out
    assert _stamp(a / ".sidegraph") == _version(ga)


def test_lazy_read_path_sync_keeps_the_version_the_cli_stamped(mcp_in_b, monkeypatch):
    a, b, ga, gb = mcp_in_b
    monkeypatch.setenv("SIDEGRAPH_GRAPH", "graphify-out/graph.json")
    assert cli.sync_main([]) == 0

    reader = server._synced_reader()

    assert reader is not None and Path(reader.path).resolve() == ga.resolve()
    assert _stamp(a / ".sidegraph") == _version(ga)


def test_empty_env_falls_back_to_the_stores_default_graph(mcp_in_b, monkeypatch):
    a, b, ga, gb = mcp_in_b
    monkeypatch.setenv("SIDEGRAPH_GRAPH", "")

    reader = server._load_reader()

    assert reader is not None and Path(reader.path).resolve() == ga.resolve()


def test_unreadable_graph_error_names_the_resolved_path(tmp_path, monkeypatch):
    proj, cwd = tmp_path / "proj", tmp_path / "cwd"
    Store(proj / ".sidegraph").close()
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("SIDEGRAPH_DIR", str(proj / ".sidegraph"))
    monkeypatch.setattr(server, "_store", Store(proj / ".sidegraph"))

    out = server._sync_anchors_impl(server._get_store(), server._load_reader())

    assert out["synced"] is False
    assert str(proj / "graphify-out" / "graph.json") in out["error"], out["error"]


def test_nested_store_does_not_fall_back_to_the_cwd_graph(tmp_path, monkeypatch):
    Store(tmp_path / ".config" / "sidegraph").close()
    _write_graph(tmp_path, "commitRoot", 2)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SIDEGRAPH_DIR", ".config/sidegraph")
    monkeypatch.setenv("SIDEGRAPH_GRAPH", "graphify-out/graph.json")
    monkeypatch.setattr(server, "_store", Store(tmp_path / ".config" / "sidegraph"))

    out = server._sync_anchors_impl(server._get_store(), server._load_reader())

    assert server._load_reader() is None
    assert out["synced"] is False
    assert str(tmp_path / ".config" / "graphify-out" / "graph.json") in out["error"], out
    assert str(tmp_path / "graphify-out" / "graph.json") in out["error"], out
    assert "SIDEGRAPH_GRAPH" in out["error"], out


def test_default_layout_reads_the_cwd_graph(tmp_path, monkeypatch):
    Store(tmp_path / ".sidegraph").close()
    g = _write_graph(tmp_path, "c", 1)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SIDEGRAPH_DIR", ".sidegraph")
    monkeypatch.setattr(server, "_store", Store(tmp_path / ".sidegraph"))
    monkeypatch.delenv("SIDEGRAPH_GRAPH", raising=False)
    assert Path(server._load_reader().path).resolve() == g.resolve()
    monkeypatch.setenv("SIDEGRAPH_GRAPH", "graphify-out/graph.json")
    assert Path(server._load_reader().path).resolve() == g.resolve()


def test_absolute_env_is_used_as_given(mcp_in_b, monkeypatch):
    a, b, ga, gb = mcp_in_b
    monkeypatch.setenv("SIDEGRAPH_GRAPH", str(gb))
    assert Path(server._load_reader().path).resolve() == gb.resolve()


def _imports(path: Path) -> set[str]:
    names: set[str] = set()
    for n in ast.walk(ast.parse(path.read_text())):
        if isinstance(n, ast.ImportFrom):
            names.add(("." * n.level) + (n.module or ""))
            names.update(("." * n.level) + (n.module or "") + "." + a.name for a in n.names)
        elif isinstance(n, ast.Import):
            names.update(a.name for a in n.names)
    return names


def test_server_does_not_import_cli():
    src = Path(sidegraph.__file__).parent
    bad = {
        i
        for i in _imports(src / "server.py")
        if i.split(".")[-1] == "cli" or i in {".cli", "sidegraph.cli"}
    }
    assert not bad, bad


def test_reader_pairs_with_the_store_in_use_not_the_env(two_projects, monkeypatch):
    a, b, ga, gb = two_projects
    monkeypatch.chdir(b)
    monkeypatch.setenv("SIDEGRAPH_DIR", str(b / ".sidegraph"))  # the env names B ...
    monkeypatch.setenv("SIDEGRAPH_GRAPH", "graphify-out/graph.json")
    monkeypatch.setattr(server, "_store", Store(a / ".sidegraph"))  # ... the store in use is A
    assert Path(server._load_reader().path).resolve() == ga.resolve()


def test_sync_anchors_does_not_point_a_corrupt_graph_at_another_projects(two_projects, monkeypatch):
    """A's graph exists but is unreadable JSON; B's (the cwd's) is fine. The error must not
    advertise B's graph for A's store."""
    a, b, ga, gb = two_projects
    ga.write_text("{not json")
    monkeypatch.chdir(b)
    monkeypatch.setenv("SIDEGRAPH_DIR", str(a / ".sidegraph"))
    monkeypatch.setenv("SIDEGRAPH_GRAPH", "graphify-out/graph.json")
    monkeypatch.setattr(server, "_store", Store(a / ".sidegraph"))

    out = server._sync_anchors_impl(server._get_store(), server._load_reader())

    assert out["synced"] is False
    assert str(ga) in out["error"]
    assert str(gb) not in out["error"], out["error"]


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores modes")
def test_sync_anchors_does_not_point_an_unreadable_graph_at_another_projects(
    two_projects, monkeypatch
):
    """A's graph directory is mode 000 (real permissions, no injection): the graph is unreadable,
    not missing, so the error must not advertise B's graph for A's store."""
    a, b, ga, gb = two_projects
    monkeypatch.chdir(b)
    monkeypatch.setenv("SIDEGRAPH_DIR", str(a / ".sidegraph"))
    monkeypatch.setenv("SIDEGRAPH_GRAPH", "graphify-out/graph.json")
    monkeypatch.setattr(server, "_store", Store(a / ".sidegraph"))
    ga.parent.chmod(0)
    try:
        out = server._sync_anchors_impl(server._get_store(), server._load_reader())
    finally:
        ga.parent.chmod(0o755)

    assert out["synced"] is False
    assert str(ga) in out["error"]
    assert str(gb) not in out["error"], out["error"]


def test_sync_anchors_error_survives_an_unreadable_cwd(tmp_path, monkeypatch):
    """``os.path.abspath`` of a relative value calls getcwd(), which raises when the process cwd
    is unreadable: the hint is skipped and the error is still returned, not raised."""
    a = tmp_path / "A"
    Store(a / ".sidegraph").close()
    monkeypatch.setenv("SIDEGRAPH_DIR", str(a / ".sidegraph"))
    monkeypatch.setenv("SIDEGRAPH_GRAPH", "graphify-out/graph.json")
    monkeypatch.setattr(server, "_store", Store(a / ".sidegraph"))

    def denied():
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(os, "getcwd", denied)
    out = server._sync_anchors_impl(server._get_store(), server._load_reader())

    assert out["synced"] is False
    assert "graph not readable" in out["error"] and "beside the server's cwd" not in out["error"]


def test_sync_anchors_error_survives_an_unreadable_cwd_with_a_relative_store(tmp_path, monkeypatch):
    """The plugin sets ``SIDEGRAPH_DIR=.sidegraph``: the store path stays relative, so resolving
    its graph calls getcwd() too. With the cwd unreadable the error is returned, not raised."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SIDEGRAPH_DIR", ".sidegraph")
    monkeypatch.delenv("SIDEGRAPH_GRAPH", raising=False)
    store = Store(".sidegraph")
    monkeypatch.setattr(server, "_store", store)

    def denied():
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(os, "getcwd", denied)
    out = server._sync_anchors_impl(store, server._load_reader())

    assert out["synced"] is False
    assert out["error"].startswith("graph not readable (")
    assert "beside the server's cwd" not in out["error"]
