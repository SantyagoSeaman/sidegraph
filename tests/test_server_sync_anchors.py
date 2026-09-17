"""sync_anchors: the diagnostic/heal MCP tool (Gap 3). See design/superpowers/specs/
2026-07-10-ratification-ux-and-mcp-gaps-design.md. Reuses the store+graph fixture
approach from tests/test_sync_run.py."""

from __future__ import annotations

import json

from sidegraph.engine.reader import GraphifyReader
from sidegraph.schema import Descriptor, Entity
from sidegraph.server import _sync_anchors_impl
from sidegraph.store import Store

GRAPH = {
    "built_at_commit": "vA",
    "nodes": [
        # stable: same id, same file -> unchanged
        {
            "id": "s1",
            "label": "f_stable()",
            "norm_label": "f_stable()",
            "file_type": "code",
            "source_file": "a.py",
            "community": 1,
        },
    ],
    "links": [],
}


def _write_graph(tmp_path, name, data):
    p = tmp_path / name
    p.write_text(json.dumps(data))
    return p


def _entity(store, name, file, node_id, version="v0"):
    e = Entity(
        canonical_name=name,
        descriptor=Descriptor(name=name, file_path=file),
        last_seen_node_id=node_id,
        last_seen_graph_version=version,
    )
    return store.upsert_entity(e)


def test_sync_anchors_reports_and_syncs(tmp_path):
    reader = GraphifyReader(_write_graph(tmp_path, "g.json", GRAPH))
    store = Store(tmp_path / "srv.db")
    _entity(store, "f_stable", "a.py", "s1")

    out = _sync_anchors_impl(store, reader, force=True)

    assert out["synced"] is True
    assert out["to_version"]
    assert isinstance(out["outcomes"], list)
    for key in (
        "stale_decisions",
        "empty_domains",
        "overbroad_domains",
        "slug_conflicts",
        "repointed",
        "domains_refreshed",
        "counts",
    ):
        assert key in out


def test_sync_anchors_skip_without_force(tmp_path):
    reader = GraphifyReader(_write_graph(tmp_path, "g.json", GRAPH))
    store = Store(tmp_path / "srv.db")
    _entity(store, "f_stable", "a.py", "s1")

    _sync_anchors_impl(store, reader, force=True)
    out = _sync_anchors_impl(store, reader)  # same graph version -> skipped

    assert out["synced"] is False
    assert "error" not in out


def test_sync_anchors_no_graph_is_explanatory(tmp_path):
    store = Store(tmp_path / "srv.db")

    out = _sync_anchors_impl(store, None)

    assert out["synced"] is False
    assert "graph not readable" in out["error"]


def test_sync_anchors_outcomes_filtered_to_non_ok(tmp_path):
    """``outcomes`` only carries entities worth a human's attention -- unchanged (and
    rebound) entities are noise, same filter sidegraph-sync's own printer applies (see
    cli.sync_main)."""
    reader = GraphifyReader(_write_graph(tmp_path, "g.json", GRAPH))
    store = Store(tmp_path / "srv.db")
    _entity(store, "f_stable", "a.py", "s1")  # exact match -> unchanged
    _entity(store, "gone_fn", "b.py", "r1")  # no match anywhere -> orphaned

    out = _sync_anchors_impl(store, reader, force=True)

    statuses = {o["status"] for o in out["outcomes"]}
    assert "unchanged" not in statuses
    assert "orphaned" in statuses
    assert all(set(o) == {"status", "canonical_name", "detail"} for o in out["outcomes"])
