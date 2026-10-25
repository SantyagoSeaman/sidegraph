"""Shared setup for the domain-membership tests: a store and a graph on disk, and a pass that
always fails.
# see design/superpowers/specs/2026-10-14-graphify-upgrade-design.md (D5.3)
"""

from __future__ import annotations

from sidegraph.engine.reader import GraphifyReader
from sidegraph.store import Store
from tests.graph_fixtures import write_graph
from tests.snapshot_support import legacy_graph


def _setup(tmp_path, data=None):
    return Store(tmp_path / ".sidegraph"), GraphifyReader(
        write_graph(tmp_path, "g.json", data or legacy_graph())
    )


def _legacy(tmp_path):
    return GraphifyReader(write_graph(tmp_path, "legacy.json", legacy_graph()))


def _broken_apply(calls: list[int]):
    def broken(*a, **kw):
        calls.append(1)
        raise RuntimeError("view broken")

    return broken
