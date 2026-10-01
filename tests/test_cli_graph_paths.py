"""Every CLI command pairs a store with its own project's graph by default: the default (and
``$SIDEGRAPH_GRAPH``) resolves against the STORE's project, a ``--graph`` the user typed
resolves against the shell. See
design/superpowers/specs/2026-09-29-cli-graph-and-store-paths-design.md (D1, D2)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import sidegraph.cli as cli
from sidegraph.cli import (
    doctor_main,
    domains_main,
    import_main,
    init_main,
    ratify_main,
    stats_main,
    sync_main,
)
from sidegraph.engine.reader import GraphifyReader
from sidegraph.server import _add_domain_impl
from sidegraph.store import Store
from sidegraph.sync import LAST_SYNCED_KEY


def _node(i: int) -> dict:
    return {
        "id": f"n{i}",
        "label": f"fn_{i}()",
        "norm_label": f"fn_{i}()",
        "file_type": "code",
        "source_file": f"m{i}.py",
        "community": i,
    }


def _write_graph(project: Path, commit: str, nodes: int) -> Path:
    """`<project>/graphify-out/graph.json` with `nodes` nodes and a distinct commit stamp."""
    target = project / "graphify-out" / "graph.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(
            {"built_at_commit": commit, "nodes": [_node(i) for i in range(nodes)], "links": []}
        )
    )
    return target


def _version(graph: Path) -> str:
    return GraphifyReader(str(graph)).graph_version()


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch):
    for var in ("SIDEGRAPH_GRAPH", "SIDEGRAPH_DIR", "SIDEGRAPH_DB", "CLAUDE_PROJECT_DIR"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def two_projects(tmp_path):
    """Projects A and B, each with a store and its own graph: distinct `graph_version`, and a
    distinct node count (A 1, B 3) so stats' `--json` `graph.nodes` tells them apart."""
    a, b = tmp_path / "A", tmp_path / "B"
    graphs = {}
    for name, project, commit, nodes in (("a", a, "commitA", 1), ("b", b, "commitB", 3)):
        Store(project / ".sidegraph").close()
        graphs[name] = _write_graph(project, commit, nodes)
    assert _version(graphs["a"]) != _version(graphs["b"])
    return a, b, graphs["a"], graphs["b"]


@pytest.fixture
def reader_spy(monkeypatch):
    """Replaces `sidegraph.cli.GraphifyReader` (the name `cli` looks up); records the path
    argument of every construction and still builds the real reader."""
    seen: list[str] = []

    class Spy(GraphifyReader):
        def __init__(self, path, *args, **kwargs):
            seen.append(str(path))
            super().__init__(path, *args, **kwargs)

    monkeypatch.setattr(cli, "GraphifyReader", Spy)
    return seen


def _same_file(recorded: str, expected: Path) -> bool:
    return Path(recorded).resolve() == expected.resolve()


# -- T1: the default graph follows the store ---------------------------------------------------


def _run_sync(db: Path, tmp_path: Path) -> None:
    sync_main(["--db", str(db)])


def _run_import_docs(db: Path, tmp_path: Path) -> None:
    doc = tmp_path / "adr.md"
    doc.write_text("# T\n\n## Context\n\nc\n\n## Decision\n\nCalls `fn_0` on success.\n")
    import_main(["--db", str(db), "--docs", str(doc), "--dry-run"])


def _run_import_rationale(db: Path, tmp_path: Path) -> None:
    import_main(["--db", str(db), "--dry-run"])


def _run_bootstrap(db: Path, tmp_path: Path) -> None:
    domains_main(["bootstrap", "--db", str(db), "--dry-run"])


def _run_doctor(db: Path, tmp_path: Path) -> None:
    doctor_main(["--db", str(db)])


@pytest.mark.parametrize(
    "run",
    [_run_sync, _run_import_docs, _run_import_rationale, _run_bootstrap, _run_doctor],
    ids=["sync", "import-docs", "import-rationale", "domains-bootstrap", "doctor"],
)
def test_default_graph_follows_the_store(run, two_projects, reader_spy, tmp_path, monkeypatch):
    a, b, graph_a, _ = two_projects
    monkeypatch.chdir(b)

    run(a / ".sidegraph", tmp_path)

    assert reader_spy, "the command never built a reader"
    assert all(_same_file(p, graph_a) for p in reader_spy), reader_spy


def test_sync_stamps_the_stores_own_graph_version(two_projects, monkeypatch):
    a, b, graph_a, _ = two_projects
    monkeypatch.chdir(b)

    assert sync_main(["--db", str(a / ".sidegraph")]) == 0

    assert Store(a / ".sidegraph").get_meta(LAST_SYNCED_KEY) == _version(graph_a)


def test_an_empty_graph_env_falls_back_to_the_default(two_projects, monkeypatch):
    a, b, graph_a, _ = two_projects
    monkeypatch.chdir(b)
    monkeypatch.setenv("SIDEGRAPH_GRAPH", "")

    assert sync_main(["--db", str(a / ".sidegraph")]) == 0

    assert Store(a / ".sidegraph").get_meta(LAST_SYNCED_KEY) == _version(graph_a)


# -- T1b: typed is cwd-relative, env is store-relative -----------------------------------------


def _drive_sync(a: Path, extra: list[str], capsys) -> tuple[str, str]:
    assert sync_main(["--db", str(a / ".sidegraph"), *extra]) == 0
    return "sync", Store(a / ".sidegraph").get_meta(LAST_SYNCED_KEY)


def _drive_stats(a: Path, extra: list[str], capsys) -> tuple[str, int]:
    assert stats_main(["--db", str(a / ".sidegraph"), "--json", *extra]) == 0
    return "stats", json.loads(capsys.readouterr().out)["graph"]["nodes"]


def _drive_ratify(a: Path, extra: list[str], capsys, spy: list[str]) -> tuple[str, str]:
    store = Store(a / ".sidegraph")
    slug = "pay-typed" if extra else "pay-env"
    dom = _add_domain_impl(store, None, slug=slug, title="Pay", summary="S.", communities=[])
    spy.clear()
    assert ratify_main(["--db", str(a / ".sidegraph"), "--accept", dom["domain_id"], *extra]) == 0
    return "ratify", spy[-1]


def _observed(command: str, a: Path, extra: list[str], capsys, spy) -> object:
    if command == "sync":
        return _drive_sync(a, extra, capsys)[1]
    if command == "stats":
        return _drive_stats(a, extra, capsys)[1]
    return _drive_ratify(a, extra, capsys, spy)[1]


def _expected(command: str, which: str, graph_a: Path, graph_b: Path) -> object:
    graph = graph_a if which == "a" else graph_b
    if command == "sync":
        return _version(graph)
    if command == "stats":
        return 1 if which == "a" else 3
    return graph


@pytest.mark.parametrize("command", ["sync", "ratify", "stats"])
def test_typed_graph_is_cwd_relative_and_env_is_store_relative(
    command, two_projects, reader_spy, monkeypatch, capsys
):
    a, b, graph_a, graph_b = two_projects
    monkeypatch.chdir(b)

    # typed: the shell's graph (B's)
    got = _observed(command, a, ["--graph", "graphify-out/graph.json"], capsys, reader_spy)
    if command == "ratify":
        assert _same_file(got, graph_b), got
    else:
        assert got == _expected(command, "b", graph_a, graph_b)

    # env, no flag: the store's project graph (A's)
    monkeypatch.setenv("SIDEGRAPH_GRAPH", "graphify-out/graph.json")
    got = _observed(command, a, [], capsys, reader_spy)
    if command == "ratify":
        assert _same_file(got, graph_a), got
    else:
        assert got == _expected(command, "a", graph_a, graph_b)


# -- T2, T2b, T3: copy-and-sync -----------------------------------------------------------------


def _repo_and_copy(tmp_path: Path) -> tuple[Path, Path, Path]:
    repo, copy = tmp_path / "repo", tmp_path / "copy"
    graph = _write_graph(repo, "commitR", 2)
    Store(copy / ".sidegraph").close()
    return repo, copy, graph


def test_copy_sync_hint_when_store_anchored_graph_is_missing(tmp_path, monkeypatch, capsys):
    repo, copy, _ = _repo_and_copy(tmp_path)
    monkeypatch.chdir(repo)

    rc = sync_main(["--db", str(copy / ".sidegraph")])

    captured = capsys.readouterr()
    assert rc != 0
    assert f"graph not readable ({copy / 'graphify-out' / 'graph.json'}" in captured.out
    assert f"no graph at {copy / 'graphify-out' / 'graph.json'}" in captured.err
    assert f"{repo / 'graphify-out' / 'graph.json'} exists" in captured.err
    assert f"--graph {repo / 'graphify-out' / 'graph.json'}" in captured.err
    assert "SIDEGRAPH_GRAPH" not in captured.err


def test_the_hint_mentions_the_env_var_when_the_value_came_from_it(tmp_path, monkeypatch, capsys):
    repo, copy, _ = _repo_and_copy(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("SIDEGRAPH_GRAPH", "graphify-out/graph.json")

    assert sync_main(["--db", str(copy / ".sidegraph")]) != 0

    assert "or set SIDEGRAPH_GRAPH to an absolute path" in capsys.readouterr().err


def test_note_when_both_graphs_exist(tmp_path, monkeypatch, capsys):
    root = tmp_path / "root"
    svc_graph = _write_graph(root / "svc", "commitSvc", 2)
    root_graph = _write_graph(root, "commitRoot", 3)
    Store(root / "svc" / ".sidegraph").close()
    monkeypatch.chdir(root)

    assert sync_main(["--db", str(root / "svc" / ".sidegraph")]) == 0

    err = capsys.readouterr().err
    assert Store(root / "svc" / ".sidegraph").get_meta(LAST_SYNCED_KEY) == _version(svc_graph)
    assert _version(svc_graph) != _version(root_graph)
    assert f"using {svc_graph} (the store's project); {root_graph} also exists" in err


def test_copy_sync_with_typed_relative_graph_works(tmp_path, monkeypatch, capsys):
    repo, copy, graph = _repo_and_copy(tmp_path)
    monkeypatch.chdir(repo)

    rc = sync_main(["--db", str(copy / ".sidegraph"), "--graph", "graphify-out/graph.json"])

    assert rc == 0
    assert Store(copy / ".sidegraph").get_meta(LAST_SYNCED_KEY) == _version(graph)


# -- T4: init prints the store-anchored graph --------------------------------------------------


def test_init_reports_the_store_anchored_graph(two_projects, tmp_path, monkeypatch, capsys):
    a, _, graph_a, _ = two_projects
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    assert init_main(["--db", str(a / ".sidegraph"), "--no-settings"]) == 0

    out = capsys.readouterr().out
    assert f"found graph: {graph_a}" in out
    assert "missing graph" not in out


def test_init_prints_the_graph_hint_in_a_nested_store_setup(tmp_path, monkeypatch, capsys):
    root = tmp_path / "proj"
    _write_graph(root, "commitN", 2)
    monkeypatch.chdir(root)
    monkeypatch.setenv("SIDEGRAPH_DIR", ".config/sidegraph")
    monkeypatch.setenv("SIDEGRAPH_GRAPH", "graphify-out/graph.json")

    assert init_main(["--no-settings"]) == 0

    captured = capsys.readouterr()
    assert f"missing graph: {root / '.config' / 'graphify-out' / 'graph.json'}" in captured.out
    assert f"--graph {root / 'graphify-out' / 'graph.json'}" in captured.err
    assert "absolute path" in captured.err


# -- T9: the disclosed nested-store change -----------------------------------------------------


def test_nested_store_env_graph_hints(tmp_path, monkeypatch, capsys):
    root = tmp_path / "proj"
    _write_graph(root, "commitN", 2)
    Store(root / ".config" / "sidegraph").close()
    monkeypatch.chdir(root)
    monkeypatch.setenv("SIDEGRAPH_DIR", ".config/sidegraph")
    monkeypatch.setenv("SIDEGRAPH_GRAPH", "graphify-out/graph.json")

    rc = sync_main(["--check"])

    captured = capsys.readouterr()
    assert rc == 1
    assert f"no graph at {root / '.config' / 'graphify-out' / 'graph.json'}" in captured.err
    assert f"--graph {root / 'graphify-out' / 'graph.json'}" in captured.err


# -- T10: a symlinked .sidegraph keeps the link's project as the root ---------------------------


def test_symlinked_dot_sidegraph_finds_the_project_graph(tmp_path, monkeypatch, capsys):
    proj = tmp_path / "proj"
    graph = _write_graph(proj, "commitL", 2)
    target = tmp_path / "vault" / "stores" / "proj"
    Store(target).close()
    os.symlink(target, proj / ".sidegraph", target_is_directory=True)
    monkeypatch.chdir(proj)

    assert sync_main([]) == 0

    assert Store(target).get_meta(LAST_SYNCED_KEY) == _version(graph)


# -- SIDEGRAPH_DB naming a dangling link scaffolds nothing -------------------------------------


@pytest.mark.parametrize("typed_graph", [True, False], ids=["typed-graph", "default-graph"])
def test_sidegraph_db_dangling_link_scaffolds_nothing(typed_graph, tmp_path, monkeypatch, capsys):
    proj = tmp_path / "proj"
    graph = _write_graph(proj, "commitD", 2)
    dangling = proj / ".sidegraph"
    dangling.symlink_to(tmp_path / "nowhere" / "store", target_is_directory=True)
    monkeypatch.chdir(proj)
    monkeypatch.setenv("SIDEGRAPH_DB", str(dangling))

    rc = sync_main(["--graph", str(graph)] if typed_graph else [])

    assert rc != 0
    assert "store not readable" in capsys.readouterr().out
    assert sorted(os.listdir(proj)) == [".sidegraph", "graphify-out"]
    assert not (tmp_path / "nowhere").exists()


# -- the legacy-file step applies only to a legacy file ----------------------------------------


def test_an_absent_store_under_dot_sidegraph_is_stamped_with_its_own_parents_graph(
    tmp_path, monkeypatch
):
    proj = tmp_path / "proj"
    _write_graph(proj, "commitOuter", 2)
    inner = _write_graph(proj / ".sidegraph", "commitInner", 3)
    assert _version(inner) != _version(proj / "graphify-out" / "graph.json")
    monkeypatch.chdir(tmp_path)

    assert sync_main(["--db", str(proj / ".sidegraph" / "nested")]) == 0

    assert Store(proj / ".sidegraph" / "nested").get_meta(LAST_SYNCED_KEY) == _version(inner)


# -- _graph_hint is best-effort ----------------------------------------------------------------


def test_graph_hint_never_raises_when_a_graph_vanishes(tmp_path, monkeypatch):
    _write_graph(tmp_path / "here", "commitH", 1)
    other = _write_graph(tmp_path / "other", "commitO", 1)
    monkeypatch.chdir(tmp_path / "here")

    def boom(self, other_path):
        raise FileNotFoundError("gone")

    monkeypatch.setattr(Path, "samefile", boom)

    cli._graph_hint(None, other)  # must not raise


def test_graph_hint_ignores_a_directory_named_like_the_graph(tmp_path, monkeypatch, capsys):
    proj = tmp_path / "proj"
    (proj / "graphify-out" / "graph.json").mkdir(parents=True)
    monkeypatch.chdir(proj)

    cli._graph_hint(None, tmp_path / "elsewhere" / "graph.json")

    assert capsys.readouterr().err == ""


# -- an empty typed --graph is absent ----------------------------------------------------------


@pytest.mark.parametrize("typed", ["", "  "])
def test_an_empty_typed_graph_is_treated_as_absent(typed, two_projects, monkeypatch):
    a, b, graph_a, _ = two_projects
    monkeypatch.chdir(b)

    assert cli._resolve_cli_graph(typed, a / ".sidegraph") == graph_a


# -- init and sync agree for a legacy single-file --db -----------------------------------------


def test_init_reports_the_same_graph_as_sync_for_a_legacy_file_store(tmp_path, monkeypatch, capsys):
    import sqlite3

    from sidegraph.cli import _resolve_cli_graph
    from tests.test_store_migration import _0_2_0_SCHEMA_SQL

    proj = tmp_path / "proj"
    graph = _write_graph(proj, "commitLg", 2)
    legacy = proj / ".sidegraph" / "decisions.db"
    legacy.parent.mkdir(parents=True)
    conn = sqlite3.connect(legacy)
    conn.executescript(_0_2_0_SCHEMA_SQL)
    conn.execute("INSERT INTO meta VALUES ('schema_version', '0.2.0')")
    conn.commit()
    conn.close()
    monkeypatch.chdir(tmp_path)
    assert _resolve_cli_graph(None, legacy) == graph  # what sync resolves, before any migration

    assert init_main(["--db", str(legacy), "--no-settings"]) == 0

    assert f"found graph: {graph}" in capsys.readouterr().out


# -- the legacy hop survives the migration of `.sidegraph/decisions.db` -------------------------


def _legacy_db_store(proj: Path) -> Path:
    """`<proj>/.sidegraph/decisions.db` as a 0.2.0 single file (the first open migrates it to
    a DIRECTORY with the same name)."""
    import sqlite3

    from tests.test_store_migration import _0_2_0_SCHEMA_SQL

    legacy = proj / ".sidegraph" / "decisions.db"
    legacy.parent.mkdir(parents=True)
    conn = sqlite3.connect(legacy)
    conn.executescript(_0_2_0_SCHEMA_SQL)
    conn.execute("INSERT INTO meta VALUES ('schema_version', '0.2.0')")
    conn.commit()
    conn.close()
    return legacy


def _stamp(store_path: Path) -> str | None:
    store = Store(store_path)
    try:
        return store.get_meta(LAST_SYNCED_KEY)
    finally:
        store.close()


def test_a_migrated_legacy_store_still_reads_the_project_graph(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    graph = _write_graph(proj, "commitProj", 2)
    legacy = _legacy_db_store(proj)
    monkeypatch.chdir(tmp_path)

    assert sync_main(["--db", str(legacy)]) == 0  # migrates: the file becomes a directory
    assert legacy.is_dir()
    assert sync_main(["--db", str(legacy)]) == 0  # the same path, now a `.db` directory
    assert _stamp(legacy) == _version(graph)


def test_a_migrated_legacy_store_ignores_a_graph_inside_dot_sidegraph(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    graph = _write_graph(proj, "commitProj", 2)
    legacy = _legacy_db_store(proj)
    monkeypatch.chdir(tmp_path)
    assert sync_main(["--db", str(legacy)]) == 0
    _write_graph(proj / ".sidegraph", "commitWrong", 3)  # a different graph, wrong place

    assert sync_main(["--db", str(legacy)]) == 0
    assert _stamp(legacy) == _version(graph)


def test_init_then_sync_on_a_legacy_db_store_read_the_same_graph(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    graph = _write_graph(proj, "commitProj", 2)
    legacy = _legacy_db_store(proj)
    monkeypatch.chdir(tmp_path)

    assert init_main(["--db", str(legacy), "--no-settings"]) == 0
    assert legacy.is_dir()
    assert sync_main(["--db", str(legacy)]) == 0
    assert _stamp(legacy) == _version(graph)


# -- an unreadable working directory skips the hint ---------------------------------------------


def _deny_getcwd(monkeypatch) -> None:
    """A real chmod of the cwd does not make getcwd() fail on Linux, so patch it."""

    def denied():
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(os, "getcwd", denied)


def test_graph_hint_is_silent_when_the_cwd_is_unreadable(two_projects, monkeypatch, capsys):
    _, _, graph_a, _ = two_projects
    monkeypatch.delenv("SIDEGRAPH_GRAPH", raising=False)
    _deny_getcwd(monkeypatch)

    cli._graph_hint(None, graph_a)

    assert capsys.readouterr().err == ""


def test_sync_survives_an_unreadable_cwd(two_projects, monkeypatch):
    a, _, graph_a, _ = two_projects
    monkeypatch.delenv("SIDEGRAPH_GRAPH", raising=False)
    _deny_getcwd(monkeypatch)

    assert sync_main(["--db", str(a / ".sidegraph")]) == 0

    assert Store(a / ".sidegraph").get_meta(LAST_SYNCED_KEY) == _version(graph_a)
