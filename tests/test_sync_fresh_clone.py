"""Fresh-clone healing (design contract, see design/superpowers/specs/
2026-07-10-derived-community-bindings-design.md Mechanics §2): a clone has no committed
community state at all -- `community:*` bindings/entities are index-only per Task 1, and
`index.db` itself is gitignored, never committed. The first `sync()` against the live graph
must self-heal: re-resolve the Tier-2 leaf and re-derive the Tier-1 community binding purely
in the index, without ever touching a byte of the committed store.

Simulates a clone by deleting `index.db` on an already-populated store and reopening a fresh
`Store` at the same path -- exactly what a `git clone` + first tool invocation looks like."""

from __future__ import annotations

import json

import pytest

from sidegraph.engine.reader import GraphifyReader
from sidegraph.server import _propose_decisions_impl, _ratify_decisions_impl
from sidegraph.store import Store
from sidegraph.sync import sync

GRAPH = {
    "built_at_commit": "vFresh",
    "nodes": [
        {
            "id": "n1",
            "label": "foo()",
            "norm_label": "foo()",
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


@pytest.fixture(autouse=True)
def _no_ambient_initiative(monkeypatch):
    monkeypatch.setattr("sidegraph.capture._derive_initiative", lambda: None)


def _canonical_snapshot(store_path) -> dict[str, tuple[bytes, int, int]]:
    """relpath -> (content, size, mtime_ns) for every canonical (git-committed) file --
    mirrors test_sync_clean.py's `_snapshot_canonical_dir` helper."""
    out: dict[str, tuple[bytes, int, int]] = {}
    for sub in ("decisions", "domains", "entities", "bindings", "initiatives"):
        d = store_path / sub
        if not d.is_dir():
            continue
        for f in d.iterdir():
            if f.suffix != ".json":
                continue
            st = f.stat()
            out[f"{sub}/{f.name}"] = (f.read_bytes(), st.st_size, st.st_mtime_ns)
    return out


def test_fresh_clone_regenerates_community_binding_index_only(tmp_path):
    graph_path = _write_graph(tmp_path, "g.json", GRAPH)
    store_path = tmp_path / "s"
    reader = GraphifyReader(graph_path)
    store = Store(store_path)

    results = _propose_decisions_impl(
        store,
        reader,
        [
            {
                "title": "foo matters",
                "kind": "gotcha",
                "context": "c",
                "choice": "keep",
                "anchors": [{"name": "foo", "file_path": "a.py"}],
            },
        ],
        session_id="s-fresh-clone",
    )
    assert results[0]["status"] == "written"
    decision_id = results[0]["decision_id"]
    _ratify_decisions_impl(store, accept=[decision_id])

    # index-only per Task 1: a live Tier-1 community:1 binding exists in the index right now...
    tier1 = [b for b in store.bindings_for_record(decision_id) if b.tier == 1]
    assert len(tier1) == 1
    assert store.get_entity(tier1[0].entity_id).canonical_name == "community:1"
    assert tier1[0].status == "live"
    # ...but it never reached a canonical file: the bindings file holds only the leaf.
    payload = json.loads((store_path / "bindings" / f"{decision_id}.json").read_text())
    assert [p["entity_id"] for p in payload] != [tier1[0].entity_id]
    assert len(payload) == 1  # leaf only

    before = _canonical_snapshot(store_path)
    assert before  # sanity: there IS something to protect

    store.close()
    (store_path / "index.db").unlink()

    # -- simulate a fresh clone: reopen against canonical files only -- no index.db, no
    # last_synced stamp, no community state (it was never committed to begin with). ------
    reopened = Store(store_path)
    reader2 = GraphifyReader(graph_path)  # a real clone re-syncs against the SAME live graph
    report = sync(reopened, reader2, force=True)

    # -- the leaf re-resolved and the community binding regenerated (not a vacuous pass) --
    by_name = {o.canonical_name: o for o in report.outcomes}
    # last_seen_node_id reset to None by the cold reload (design §3), so this is always
    # reported as "rebound" (not "unchanged") on the very first post-clone sync, even
    # though the node id itself didn't actually move.
    assert by_name["foo"].status == "rebound"
    assert by_name["foo"].node_id == "n1"

    tier1_after = [b for b in reopened.bindings_for_record(decision_id) if b.tier == 1]
    live = [b for b in tier1_after if b.status == "live"]
    assert len(live) == 1
    assert reopened.get_entity(live[0].entity_id).canonical_name == "community:1"

    # -- and the canonical snapshot never moved, across the ENTIRE clone+sync cycle ------
    after = _canonical_snapshot(store_path)
    assert after == before
