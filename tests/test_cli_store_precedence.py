"""``sidegraph-sync`` and ``sidegraph-ratify`` resolve their store through the one rule in
``config.resolve_store_path``: explicit ``--db`` > ``$SIDEGRAPH_DIR`` > ``$SIDEGRAPH_DB``
(deprecated, still honoured) > an existing ``.sidegraph/`` > a new default ``.sidegraph``. The
full matrix lives in tests/test_config.py; these are the CLI-level checks that both commands
actually wire through it, with the same stderr contract.
see design/superpowers/specs/2026-09-30-graph-path-one-rule-design.md
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

import sidegraph.config as config
from sidegraph.cli import ratify_main, sync_main
from sidegraph.schema import Descriptor, Entity
from sidegraph.server import _propose_decisions_impl
from sidegraph.store import Store

EMPTY_GRAPH = {"built_at_commit": "vB", "nodes": [], "links": []}


@dataclass(frozen=True)
class Command:
    """One command under test: how to seed a store with the markers the command prints (returned),
    how to run it, and what it prints for an empty store."""

    seed: Callable[[Path], tuple[str, ...]]
    run: Callable[[Path, list[str]], int]
    empty_says: str


def _seed_ratify(db_path: Path) -> tuple[str, ...]:
    store = Store(db_path)
    _propose_decisions_impl(
        store,
        None,
        [{"title": "Marker", "kind": "lesson", "context": "c", "choice": "ch", "anchors": []}],
    )
    return ("Marker",)


def _seed_sync(db_path: Path) -> tuple[str, ...]:
    Store(db_path).upsert_entity(
        Entity(
            canonical_name="old_fn",
            descriptor=Descriptor(name="old_fn", file_path="b.py"),
            last_seen_node_id="r1",
        )
    )
    return ("orphaned", "old_fn")  # the graph lacks old_fn: sync reports it orphaned


def _graph_for(tmp_path: Path) -> list[str]:
    graph = tmp_path / "b.json"
    graph.write_text(json.dumps(EMPTY_GRAPH))
    return ["--graph", str(graph)]


COMMANDS = {
    "ratify": Command(
        seed=_seed_ratify,
        run=lambda tmp_path, argv: ratify_main(argv),
        empty_says="No proposed decisions",
    ),
    "sync": Command(
        seed=_seed_sync,
        run=lambda tmp_path, argv: sync_main([*argv, *_graph_for(tmp_path)]),
        empty_says="no tracked entities",
    ),
}


@pytest.fixture(params=list(COMMANDS))
def command(request: pytest.FixtureRequest) -> Command:
    return COMMANDS[request.param]


def _reset_deprecation_warning(monkeypatch):
    monkeypatch.setattr(config, "_deprecation_warned", False)


def test_bare_prefers_existing_dot_sidegraph_store(command, tmp_path, capsys, monkeypatch):
    markers = command.seed(tmp_path / ".sidegraph")  # ".sidegraph" IS the store

    monkeypatch.delenv("SIDEGRAPH_DIR", raising=False)
    monkeypatch.delenv("SIDEGRAPH_DB", raising=False)
    monkeypatch.chdir(tmp_path)
    assert command.run(tmp_path, []) == 0
    captured = capsys.readouterr()
    assert all(m in captured.out for m in markers), (markers, captured.out)
    assert captured.err == ""  # store already existed -> no warning


def test_bare_creates_default_dot_sidegraph_with_warning(command, tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("SIDEGRAPH_DIR", raising=False)
    monkeypatch.delenv("SIDEGRAPH_DB", raising=False)
    monkeypatch.chdir(tmp_path)
    assert command.run(tmp_path, []) == 0
    captured = capsys.readouterr()
    assert command.empty_says in captured.out  # fresh, empty store
    assert (tmp_path / ".sidegraph" / "format").is_file()
    assert "warning: creating new store at .sidegraph" in captured.err
    assert "--db or set SIDEGRAPH_DIR" in captured.err


def test_sidegraph_dir_wins_over_dot_sidegraph_and_sidegraph_db(
    command, tmp_path, capsys, monkeypatch
):
    Store(tmp_path / ".sidegraph")  # exists but must be ignored -- SIDEGRAPH_DIR wins
    via_db_env = tmp_path / "via-db-env.db"
    Store(via_db_env)  # exists but must be ignored -- SIDEGRAPH_DIR wins over SIDEGRAPH_DB too
    dir_env = tmp_path / "via-dir-env"
    markers = command.seed(dir_env)

    monkeypatch.setenv("SIDEGRAPH_DIR", str(dir_env))
    monkeypatch.setenv("SIDEGRAPH_DB", str(via_db_env))
    monkeypatch.chdir(tmp_path)
    assert command.run(tmp_path, []) == 0
    captured = capsys.readouterr()
    assert all(m in captured.out for m in markers), (markers, captured.out)
    assert captured.err == ""  # SIDEGRAPH_DIR wins outright -- no deprecation note either


def test_sidegraph_db_back_compat_wins_over_dot_sidegraph(command, tmp_path, capsys, monkeypatch):
    """SIDEGRAPH_DB is deprecated but still honoured (design §5) -- and still takes
    precedence over an existing ``.sidegraph/`` when SIDEGRAPH_DIR is unset."""
    _reset_deprecation_warning(monkeypatch)
    Store(tmp_path / ".sidegraph")  # exists but must be ignored -- SIDEGRAPH_DB wins
    custom_db = tmp_path / "custom.db"
    markers = command.seed(custom_db)

    monkeypatch.delenv("SIDEGRAPH_DIR", raising=False)
    monkeypatch.setenv("SIDEGRAPH_DB", str(custom_db))
    monkeypatch.chdir(tmp_path)
    assert command.run(tmp_path, []) == 0
    captured = capsys.readouterr()
    assert all(m in captured.out for m in markers), (markers, captured.out)
    assert "SIDEGRAPH_DB is deprecated" in captured.err
    assert "creating new store" not in captured.err  # store already existed -> no such warning


def test_explicit_db_wins_over_everything(command, tmp_path, capsys, monkeypatch):
    Store(tmp_path / ".sidegraph")
    Store(tmp_path / "dir-env")
    Store(tmp_path / "env.db")
    explicit_db = tmp_path / "explicit.db"
    markers = command.seed(explicit_db)

    monkeypatch.setenv("SIDEGRAPH_DIR", str(tmp_path / "dir-env"))
    monkeypatch.setenv("SIDEGRAPH_DB", str(tmp_path / "env.db"))
    monkeypatch.chdir(tmp_path)
    assert command.run(tmp_path, ["--db", str(explicit_db)]) == 0
    captured = capsys.readouterr()
    assert all(m in captured.out for m in markers), (markers, captured.out)
    assert captured.err == ""
