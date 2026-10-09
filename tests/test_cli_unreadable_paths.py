"""An unreadable store or graph path is reported by the command's own error path, never raised
as a traceback. Python 3.13 re-raises EACCES from ``Path.is_file/exists/is_dir`` and 3.14
swallows it, so most tests inject the error (every Python, root included); the ``chmod``
tests are the end-to-end check on 3.13.
See design/superpowers/specs/2026-09-30-graph-path-one-rule-design.md."""

from __future__ import annotations

import errno
import json
import os
from pathlib import Path

import pytest

import sidegraph.cli as cli
from sidegraph import config
from sidegraph.store import Store

ROOT = hasattr(os, "geteuid") and os.geteuid() == 0


def _graph(project: Path) -> Path:
    g = project / "graphify-out" / "graph.json"
    g.parent.mkdir(parents=True, exist_ok=True)
    g.write_text(json.dumps({"built_at_commit": "c", "nodes": [], "links": []}))
    return g


def _eacces_on(monkeypatch, *blocked: Path) -> None:
    """Python 3.13 re-raises EACCES from is_file/exists/is_dir; 3.14 swallows it. Make every
    Python and every uid (root included) behave like 3.13 for exactly these paths."""
    targets = {os.path.abspath(p) for p in blocked}
    for name in ("is_file", "exists", "is_dir", "is_symlink"):
        real = getattr(Path, name)

        def wrapper(self, *a, _real=real, **k):
            if os.path.abspath(self) in targets:
                raise PermissionError(errno.EACCES, "Permission denied", str(self))
            return _real(self, *a, **k)

        monkeypatch.setattr(Path, name, wrapper)

    real_stat = os.stat

    def stat(path, *a, **k):
        if os.path.abspath(path) in targets:
            raise PermissionError(errno.EACCES, "Permission denied", str(path))
        return real_stat(path, *a, **k)

    monkeypatch.setattr(os, "stat", stat)


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A healthy store in `proj`, a graph beside it, the shell in `elsewhere` which has its own
    graph (so the D2 hint has something to compare)."""
    proj, elsewhere = tmp_path / "proj", tmp_path / "elsewhere"
    Store(proj / ".sidegraph").close()
    g = _graph(proj)
    _graph(elsewhere)
    monkeypatch.chdir(elsewhere)
    return proj / ".sidegraph", g


def _doc(tmp_path: Path) -> str:
    d = tmp_path / "adr.md"
    d.write_text("# T\n\n## Context\n\nc\n\n## Decision\n\nd\n")
    return str(d)


def _sync(db, tp):
    return cli.sync_main(["--db", str(db)])


def _init(db, tp):
    return cli.init_main(["--db", str(db), "--no-settings"])


def _imp(db, tp):
    return cli.import_main(["--db", str(db), "--dry-run"])


def _imp_docs(db, tp):
    return cli.import_main(["--db", str(db), "--docs", _doc(tp), "--dry-run"])


def _boot(db, tp):
    return cli.domains_main(["bootstrap", "--db", str(db), "--dry-run"])


def _doctor(db, tp):
    return cli.doctor_main(["--db", str(db)])


def _stats(db, tp):
    return cli.stats_main(["--db", str(db)])


def _ratify(db, tp):
    return cli.ratify_main(["--db", str(db)])


RUNS = {
    "sync": _sync,
    "init": _init,
    "import": _imp,
    "import-docs": _imp_docs,
    "domains-bootstrap": _boot,
    "doctor": _doctor,
    "stats": _stats,
    "ratify": _ratify,
}


@pytest.mark.parametrize("cmd", list(RUNS))
def test_unreadable_store_is_reported_not_raised(cmd, world, tmp_path, monkeypatch, capsys):
    db, g = world
    _eacces_on(monkeypatch, db)
    rc = RUNS[cmd](db, tmp_path)
    out = capsys.readouterr()
    text = out.out + out.err
    assert isinstance(rc, int)
    if cmd in ("sync", "import", "import-docs", "domains-bootstrap", "doctor", "ratify"):
        assert rc == 1 and "store not readable (" in text, (rc, text)
    if cmd == "init":
        assert rc == 1 and "store not writable (" in text, (rc, text)


@pytest.mark.parametrize("cmd", list(RUNS))
def test_unreadable_graph_is_reported_not_raised(cmd, world, tmp_path, monkeypatch, capsys):
    db, g = world
    _eacces_on(monkeypatch, g)
    rc = RUNS[cmd](db, tmp_path)
    assert isinstance(rc, int)
    capsys.readouterr()


def test_helper_survives_an_unreadable_store(world, monkeypatch):
    db, g = world
    _eacces_on(monkeypatch, db)
    assert config.graph_path_for_store("graphify-out/graph.json", db) == g


def test_hint_survives_an_unreadable_graph(world, monkeypatch):
    db, g = world
    _eacces_on(monkeypatch, g)
    cli._graph_hint(None, g)


def test_ratify_reader_survives_unreadable_paths(world, monkeypatch):
    db, g = world
    _eacces_on(monkeypatch, g)
    assert cli._ratify_reader("graphify-out/graph.json", db) is None
    monkeypatch.undo()
    _eacces_on(monkeypatch, db)
    cli._ratify_reader("graphify-out/graph.json", db)


@pytest.mark.skipif(ROOT, reason="root ignores modes")
@pytest.mark.parametrize("cmd", ["sync", "init", "import", "domains-bootstrap", "stats"])
def test_chmod_parent_is_reported_not_raised(cmd, world, tmp_path, capsys):
    db, g = world
    parent = db.parent
    parent.chmod(0)
    try:
        rc = RUNS[cmd](db, tmp_path)
    finally:
        parent.chmod(0o755)
    assert isinstance(rc, int) and rc != 0
    capsys.readouterr()


def test_stats_unreadable_index_is_reported_not_raised(world, monkeypatch, capsys):
    db, g = world
    _eacces_on(monkeypatch, db / "index.db")
    rc = cli.stats_main(["--db", str(db)])
    err = capsys.readouterr().err
    assert rc == 2 and "cannot read the store index" in err and "no store index" not in err


def test_helper_survives_an_unreadable_format_marker(tmp_path, monkeypatch):
    legacy = tmp_path / "proj" / "sub" / "legacy.db"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"")
    _eacces_on(monkeypatch, legacy.parent / "format")
    assert (
        config.graph_path_for_store("graphify-out/graph.json", legacy)
        == legacy.parent / "graphify-out" / "graph.json"
    )


def test_hint_survives_an_unreadable_cwd_graph(world, monkeypatch):
    db, g = world
    _eacces_on(monkeypatch, db.parent.parent / "elsewhere" / "graphify-out" / "graph.json")
    cli._graph_hint(None, g)


def test_hint_is_silent_when_the_store_graph_cannot_be_read(world, monkeypatch, capsys):
    """An unreadable graph is not a missing one: no 'no graph at ... pass --graph' advice."""
    db, g = world
    _eacces_on(monkeypatch, g)
    cli._graph_hint(None, g)
    assert "no graph at" not in capsys.readouterr().err


def test_sidegraph_db_under_an_unreadable_directory_falls_to_its_parent(tmp_path, monkeypatch):
    target = tmp_path / "locked" / "sub" / "x.db"
    _eacces_on(monkeypatch, target)
    monkeypatch.setenv("SIDEGRAPH_DB", str(target))
    assert config.resolve_store_path(warn_on_create=False) == str(target.parent)


def test_default_store_lookup_survives_an_unreadable_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _eacces_on(monkeypatch, tmp_path / ".sidegraph")
    assert config.resolve_store_path(warn_on_create=False) == ".sidegraph"


@pytest.mark.skipif(ROOT, reason="root ignores modes")
def test_sync_with_a_sidegraph_db_under_an_unreadable_directory(tmp_path, monkeypatch, capsys):
    locked = tmp_path / "locked"
    (locked / "sub").mkdir(parents=True)
    monkeypatch.setenv("SIDEGRAPH_DB", str(locked / "sub" / "x.db"))
    locked.chmod(0)
    try:
        rc = cli.sync_main([])
    finally:
        locked.chmod(0o755)
    assert rc == 1
    capsys.readouterr()


@pytest.mark.skipif(ROOT, reason="root ignores modes")
def test_hint_does_not_recommend_another_graph_when_the_store_graph_is_unreadable(world, capsys):
    """Real permissions, no injection (so native 3.14 behaviour is exercised): the store's graph
    directory is mode 000, the cwd has a valid graph. Unreadable is not missing."""
    db, g = world
    g.parent.chmod(0)
    try:
        cli._graph_hint(None, g)
    finally:
        g.parent.chmod(0o755)
    err = capsys.readouterr().err
    assert "elsewhere" not in err and "--graph" not in err, err


@pytest.mark.skipif(ROOT, reason="root ignores modes")
def test_path_state_reports_present_missing_and_unknown(tmp_path):
    f = tmp_path / "f.json"
    f.write_text("{}")
    assert config.path_state(f) == "present"
    assert config.path_state(tmp_path / "nope") == "missing"
    assert config.path_state(f / "below-a-file") == "missing"
    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "x").write_text("")
    locked.chmod(0)
    try:
        assert config.path_state(locked / "x") == "unknown"
    finally:
        locked.chmod(0o755)
