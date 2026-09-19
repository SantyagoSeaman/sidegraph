"""Graph metrics come through GraphifyReader only — the engine seam (CLAUDE.md)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from sidegraph.stats.model import build_report
from sidegraph.store import Store

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)


def _graph(tmp_path):
    """Keys match `_to_node` (reader.py:90): label / source_file, not name / file."""
    path = tmp_path / "graph.json"
    path.write_text(
        json.dumps(
            {
                "version": "v1",
                "nodes": [
                    {
                        "id": "n1",
                        "label": "a",
                        "file_type": "py",
                        "source_file": "a.py",
                        "community": "c1",
                    },
                    {
                        "id": "n2",
                        "label": "b",
                        "file_type": "py",
                        "source_file": "a.py",
                        "community": "c1",
                    },
                    {
                        "id": "n3",
                        "label": "c",
                        "file_type": "py",
                        "source_file": "b.py",
                        "community": "c2",
                    },
                ],
                "links": [],
            }
        )
    )
    return path


def test_graph_metrics_are_read_through_the_reader(tmp_path):
    Store(tmp_path / "s").close()
    r = build_report(tmp_path / "s", _graph(tmp_path), window_days=30, now=NOW)
    assert r.graph.available is True
    assert r.graph.nodes == 3
    assert r.graph.files == 2, "distinct source_file values, not node count"
    assert r.graph.communities == 2
    assert r.graph.graph_version is not None
    assert r.graph.graph_version.startswith("content:")


def test_a_missing_graph_is_reported_as_unavailable_not_as_zeroes(tmp_path):
    Store(tmp_path / "s").close()
    r = build_report(tmp_path / "s", tmp_path / "nope.json", window_days=30, now=NOW)
    assert r.graph.available is False
    assert r.graph.graph_version is None


def test_no_graph_path_is_reported_as_unavailable(tmp_path):
    Store(tmp_path / "s").close()
    r = build_report(tmp_path / "s", None, window_days=30, now=NOW)
    assert r.graph.available is False


def test_an_unreadable_graph_is_unavailable_not_an_exception(tmp_path):
    Store(tmp_path / "s").close()
    bad = tmp_path / "graph.json"
    bad.write_text("{ not json")
    r = build_report(tmp_path / "s", bad, window_days=30, now=NOW)
    assert r.graph.available is False


def test_an_unreadable_graph_is_told_apart_from_an_absent_one(tmp_path):
    """`sidegraph-init` only tests that the file exists, so "not built yet → sidegraph-init"
    sends the owner of a corrupt graph in a circle. The two states need two answers."""
    Store(tmp_path / "s").close()
    bad = tmp_path / "graph.json"
    bad.write_text("{ not json")

    unreadable = build_report(tmp_path / "s", bad, window_days=30, now=NOW).graph
    assert (unreadable.available, unreadable.unreadable) == (False, True)

    absent = build_report(tmp_path / "s", tmp_path / "nope.json", window_days=30, now=NOW).graph
    assert (absent.available, absent.unreadable) == (False, False)

    unset = build_report(tmp_path / "s", None, window_days=30, now=NOW).graph
    assert (unset.available, unset.unreadable) == (False, False)


def test_a_readable_graph_is_not_flagged_unreadable(tmp_path):
    Store(tmp_path / "s").close()
    g = build_report(tmp_path / "s", _graph(tmp_path), window_days=30, now=NOW).graph
    assert (g.available, g.unreadable) == (True, False)


def test_a_graph_of_the_wrong_shape_is_unreadable_not_a_crash(tmp_path):
    """Valid JSON that is not a graph (a list where the reader wants an object) raises
    something other than JSONDecodeError; it is the same user-facing state."""
    Store(tmp_path / "s").close()
    odd = tmp_path / "graph.json"
    odd.write_text("[1, 2, 3]")
    g = build_report(tmp_path / "s", odd, window_days=30, now=NOW).graph
    assert (g.available, g.unreadable) == (False, True)
