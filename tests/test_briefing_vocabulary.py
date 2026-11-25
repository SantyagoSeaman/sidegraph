"""Ranked file and symbol lists in the project vocabulary briefing."""

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from sidegraph.host.briefing.blocks.vocabulary import render
from sidegraph.host.briefing.context import build, read_parts
from sidegraph.hot_index import HotIndex
from sidegraph.schema import DecisionKind, Descriptor, Entity
from sidegraph.store import Store
from sidegraph.store_layout import VOLATILE_STALE_KEY
from tests.briefing_fixtures import NOW, context, decision, record, rich_context
from tests.test_hot_index import _Seeder


def test_the_whole_block_on_the_rich_context() -> None:
    assert render(rich_context(), 900) == [
        "## Project vocabulary",
        "Files with the most recorded memory: src/a.py (3), src/b.py (2), src/c.py (1).",
        "Symbols with the most recorded memory: Store (3), Zeta (1), open_store (1).",
    ]


def test_at_most_eight_files_and_ten_symbols() -> None:
    row = record(decision(1, "A decision bound to many files"))
    ctx = context(
        by_file={f"src/{i:02}.py": (row,) for i in reversed(range(9))},
        symbols={f"Sym{i:02}": 1 for i in reversed(range(11))},
    )
    assert render(ctx, 900) == [
        "## Project vocabulary",
        "Files with the most recorded memory: src/00.py (1), src/01.py (1), src/02.py (1), "
        "src/03.py (1), src/04.py (1), src/05.py (1), src/06.py (1), src/07.py (1).",
        "Symbols with the most recorded memory: Sym00 (1), Sym01 (1), Sym02 (1), Sym03 (1), "
        "Sym04 (1), Sym05 (1), Sym06 (1), Sym07 (1), Sym08 (1), Sym09 (1).",
    ]


def test_a_list_is_cut_from_its_tail_to_fit() -> None:
    first = record(decision(1, "First"))
    second = record(decision(2, "Second"))
    third = record(decision(3, "Third"))
    files = context(
        by_file={
            "src/c.py": (first,),
            "src/b.py": (first, second, first),
            "🐍/a.py": (first, second, third),
        }
    )
    assert render(files, 85) == [
        "## Project vocabulary",
        "Files with the most recorded memory: 🐍/a.py (3), src/b.py (2).",
    ]
    assert render(files, 84) == [
        "## Project vocabulary",
        "Files with the most recorded memory: 🐍/a.py (3).",
    ]
    symbols = context(symbols={"Gamma": 1, "Beta": 2, "Alpha": 3})
    assert render(symbols, 81) == [
        "## Project vocabulary",
        "Symbols with the most recorded memory: Alpha (3), Beta (2).",
    ]


def test_one_empty_list_leaves_the_other() -> None:
    row = record(decision(1, "File decision"))
    assert render(context(by_file={"src/a.py": (row,)}), 900) == [
        "## Project vocabulary",
        "Files with the most recorded memory: src/a.py (1).",
    ]
    symbols = context(by_file={"src/" + "a" * 900 + ".py": (row,)}, symbols={"Open": 1})
    assert render(symbols, 900) == [
        "## Project vocabulary",
        "Symbols with the most recorded memory: Open (1).",
    ]


def test_nothing_anchored_gives_nothing() -> None:
    assert render(context(accepted=(decision(1, "Unanchored decision"),)), 900) == []


def test_names_keep_line_separators_distinct_from_literal_escapes() -> None:
    row = record(decision(1, "File decision"))
    ctx = context(
        by_file={r"src/a\nb.py": (row,), "src/a\nb.py": (row,)},
        symbols={
            r"open\u2028store": 1,
            "open\u2028store": 1,
            "scan\r\v\f\x1c\x1d\x1e\x85\u2029store": 1,
        },
    )
    assert render(ctx, 900) == [
        "## Project vocabulary",
        r"Files with the most recorded memory: src/a\nb.py (1), src/a\\nb.py (1).",
        r"Symbols with the most recorded memory: open\\u2028store (1), open\u2028store (1), "
        r"scan\r\u000b\f\u001c\u001d\u001e\u0085\u2029store (1).",
    ]


def test_the_output_ignores_the_journal_and_the_telemetry_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with closing(Store(tmp_path)) as store:
        seeder = _Seeder(store)
        seeder.anchor(seeder.decision("B decision", DecisionKind.GOTCHA), "src/b.py")
        first = seeder.decision("A decision", DecisionKind.GOTCHA)
        seeder.anchor(first, "src/a.py")
        file_entity = store.upsert_entity(
            Entity(
                canonical_name="src/a.py", descriptor=Descriptor(name="a.py", file_path="src/a.py")
            )
        )
        seeder.anchor(first, "src/a.py", entity=file_entity)
        store.set_meta(VOLATILE_STALE_KEY, "0")

    def render_index() -> list[str]:
        index = HotIndex.open(tmp_path)
        assert index is not None
        with closing(index), index.snapshot():
            parts = read_parts(index, frozenset({"anchors", "symbols"}), NOW)
        return render(build(parts, NOW, None, ()), 900)

    expected = [
        "## Project vocabulary",
        "Files with the most recorded memory: src/a.py (1), src/b.py (1).",
        "Symbols with the most recorded memory: Sym1 (1), Sym2 (1).",
    ]
    monkeypatch.setenv("SIDEGRAPH_TELEMETRY", "on")
    assert render_index() == expected
    with closing(sqlite3.connect(tmp_path / "index.db")) as conn, conn:
        conn.executemany(
            "INSERT INTO retrieval_seeds (seed, queries, last_seen_at) VALUES (?, ?, ?)",
            [("src/a.py", 2, NOW.isoformat()), ("src/b.py", 100, NOW.isoformat())],
        )
    assert render_index() == expected
    monkeypatch.setenv("SIDEGRAPH_TELEMETRY", "off")
    assert render_index() == expected
