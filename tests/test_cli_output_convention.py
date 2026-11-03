"""One output convention for the console commands: a refusal or an operational error writes
its message to stderr, leaves stdout empty and exits 1 (2 is reserved for findings and for
argparse usage errors). Hook entry points and ``sidegraph-bootstrap`` are not covered.
See docs/reference/cli.md ("Exit codes and streams")."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from sidegraph import cli

# Each case drives one command into a refusal that needs nothing but a path that holds no
# store (or, for the graph readers, a graph that is not there).
CASES: list[tuple[str, Callable[[list[str]], int], Callable[[Path, Path], list[str]]]] = [
    ("compact", cli.compact_main, lambda db, t: ["--db", str(db)]),
    ("migrate", cli.migrate_main, lambda db, t: ["--db", str(db)]),
    ("require", cli.require_main, lambda db, t: ["--db", str(db), "--sidegraph", "0.1.0"]),
    ("verify", cli.verify_main, lambda db, t: ["--db", str(db)]),
    ("doctor", cli.doctor_main, lambda db, t: ["--db", str(db)]),
    ("stats", cli.stats_main, lambda db, t: ["--db", str(db)]),
    ("viz", cli.viz_main, lambda db, t: ["--db", str(db), "--out", str(t / "g")]),
    ("export-okf", cli.export_okf_main, lambda db, t: ["--db", str(db), "--out", str(t / "b")]),
    ("sync", cli.sync_main, lambda db, t: ["--db", str(db), "--graph", str(t / "no.json")]),
    (
        "domains",
        cli.domains_main,
        lambda db, t: ["bootstrap", "--db", str(db), "--graph", str(t / "no.json")],
    ),
]


@pytest.mark.parametrize(("name", "main", "argv"), CASES, ids=[c[0] for c in CASES])
def test_a_refusal_goes_to_stderr_and_exits_1(name, main, argv, tmp_path, capsys) -> None:
    rc = main(argv(tmp_path / "nothing", tmp_path))
    captured = capsys.readouterr()
    assert rc == 1, (name, captured)
    assert captured.out == "", (name, captured.out)
    assert captured.err.strip(), name


def test_a_ratify_error_line_goes_to_stderr_and_exits_1(tmp_path, capsys) -> None:
    from sidegraph.store import Store

    db = tmp_path / "store"
    Store(db)
    rc = cli.ratify_main(["--db", str(db), "--accept", "NOPE"])
    captured = capsys.readouterr()
    assert rc == 1
    assert captured.out == ""
    assert captured.err.startswith("error NOPE:")
