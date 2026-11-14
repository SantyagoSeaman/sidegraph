"""A gated supersede_decision successor follows the configured ratification policy, as a
superseding draft does: accepted at write time under auto-all when it has a live anchor,
proposed otherwise.
see design/superpowers/specs/2026-11-12-ratification-needs-a-human-verdict-design.md (D4)"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import fastmcp
import pytest

from sidegraph import server
from sidegraph.engine.reader import GraphifyReader
from sidegraph.schema import DecisionStatus
from sidegraph.server import _add_decision_impl
from sidegraph.store import Store
from sidegraph.sync import sync

FIXTURE = Path(__file__).parent / "fixtures" / "mini_graph.json"
ANCHORS = [{"name": "Trader", "file_path": "trader/exec.py"}]


@pytest.fixture
def world(tmp_path):
    """An accepted predecessor with a live anchor in a fresh store, with the fixture graph."""
    reader = GraphifyReader(FIXTURE)
    with Store(tmp_path / "store") as store:
        old = _add_decision_impl(
            store,
            reader,
            title="Batch limit is 100",
            kind="constraint",
            context="c",
            choice="ch",
            anchors=ANCHORS,
        )
        yield store, reader, old


def _supersede_through_the_tool(store, reader, old_id, monkeypatch, **extra):
    monkeypatch.setattr(server, "_store", store)
    monkeypatch.setattr(server, "_load_reader", lambda: reader)

    async def run():
        async with fastmcp.Client(server.mcp) as client:
            return await client.call_tool(
                "supersede_decision",
                {
                    "old_decision_id": old_id,
                    "title": "Batch limit is 250",
                    "kind": "constraint",
                    "context": "c2",
                    "choice": "ch2",
                    **extra,
                },
                raise_on_error=False,
            )

    result = asyncio.run(run())
    assert not result.is_error
    return result.structured_content


@pytest.mark.parametrize("auto_accept", [None, "on"])
def test_supersede_tool_follows_auto_all_when_the_successor_has_a_live_anchor(
    world, monkeypatch, auto_accept
):
    """Red against unfixed code (accepted, but with no stamp) and against a tool that never
    reads the policy: under auto-all a superseding draft ratifies itself, and so does this.
    The deprecated auto-accept switch does not change that: this tool reads the policy
    variable alone."""
    store, reader, old = world
    monkeypatch.setenv("SIDEGRAPH_RATIFY_POLICY", "auto-all")
    monkeypatch.delenv("SIDEGRAPH_AUTO_ACCEPT", raising=False)
    if auto_accept:
        monkeypatch.setenv("SIDEGRAPH_AUTO_ACCEPT", auto_accept)
    sync(store, reader)  # a new index is cold until a sync; the successor waits then

    out = _supersede_through_the_tool(store, reader, old["id"], monkeypatch)

    assert out["status"] == "accepted" and out["ratified_by"] == "auto:auto-all"
    successor = store.get_decision(out["id"])
    assert successor.status == DecisionStatus.ACCEPTED
    assert successor.ratified_by == "auto:auto-all"
    assert successor.provenance.source == "agent"
    assert store.get_decision(old["id"]).status == DecisionStatus.SUPERSEDED


def test_supersede_tool_stays_proposed_under_auto_all_without_a_live_anchor(tmp_path, monkeypatch):
    """Red against unfixed code, and against a policy applied with no eligibility gate: with
    no graph every anchor is orphaned, so the successor waits."""
    monkeypatch.setenv("SIDEGRAPH_RATIFY_POLICY", "auto-all")
    with Store(tmp_path / "store") as store:
        old = _add_decision_impl(
            store, None, title="t", kind="constraint", context="c", choice="ch", anchors=ANCHORS
        )

        out = _supersede_through_the_tool(store, None, old["id"], monkeypatch)

        assert out["status"] == "proposed" and out["ratified_by"] is None
        assert store.get_decision(out["id"]).status == DecisionStatus.PROPOSED
        assert store.get_decision(old["id"]).status == DecisionStatus.ACCEPTED


@pytest.mark.parametrize(
    "environment",
    [
        {},
        {"SIDEGRAPH_RATIFY_POLICY": "manual"},
        {"SIDEGRAPH_RATIFY_POLICY": "auto-low-risk"},
        {"SIDEGRAPH_AUTO_ACCEPT": "on"},
    ],
)
def test_supersede_tool_stays_proposed_unless_the_policy_is_auto_all(
    world, monkeypatch, environment
):
    """Red against unfixed code. In the last case the deprecated auto-accept switch is on by
    itself: it keeps its scope, the draft pipeline, and does not accept this tool's write."""
    store, reader, old = world
    monkeypatch.delenv("SIDEGRAPH_RATIFY_POLICY", raising=False)
    monkeypatch.delenv("SIDEGRAPH_AUTO_ACCEPT", raising=False)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    out = _supersede_through_the_tool(store, reader, old["id"], monkeypatch)

    assert out["status"] == "proposed" and out["ratified_by"] is None
    assert store.get_decision(old["id"]).status == DecisionStatus.ACCEPTED


def test_source_human_is_not_stamped_by_the_policy(world, monkeypatch):
    """Over-reach guard: the owner-asked path lands accepted by itself; the policy has
    nothing to ratify. Red against unfixed code only through the new result keys."""
    store, reader, old = world
    monkeypatch.setenv("SIDEGRAPH_RATIFY_POLICY", "auto-all")

    out = _supersede_through_the_tool(store, reader, old["id"], monkeypatch, source="human")

    assert out["status"] == "accepted" and out["ratified_by"] is None
    assert out["auto_ratify_error"] is None
    assert store.get_decision(out["id"]).ratified_by is None


@pytest.mark.parametrize("kind", ["constraint", "gotcha", "lesson"])
def test_supersede_tool_stays_proposed_under_auto_low_risk_for_every_kind(
    tmp_path, monkeypatch, kind
):
    """Red against a signal that says ``has_supersedes=False``: ``gotcha`` and ``lesson`` are
    the kinds ``auto-low-risk`` admits, and only the supersedes flag keeps a successor of
    that kind, with a live anchor, in the queue."""
    monkeypatch.setenv("SIDEGRAPH_RATIFY_POLICY", "auto-low-risk")
    monkeypatch.delenv("SIDEGRAPH_AUTO_ACCEPT", raising=False)
    reader = GraphifyReader(FIXTURE)
    with Store(tmp_path / "store") as store:
        old = _add_decision_impl(
            store, reader, title="Old", kind=kind, context="c", choice="ch", anchors=ANCHORS
        )
        monkeypatch.setattr(server, "_store", store)
        monkeypatch.setattr(server, "_load_reader", lambda: reader)

        async def run():
            async with fastmcp.Client(server.mcp) as client:
                return await client.call_tool(
                    "supersede_decision",
                    {
                        "old_decision_id": old["id"],
                        "title": "New",
                        "kind": kind,
                        "context": "c2",
                        "choice": "ch2",
                    },
                    raise_on_error=False,
                )

        result = asyncio.run(run())

        assert not result.is_error
        out = result.structured_content
        assert out["status"] == "proposed" and out["ratified_by"] is None
        assert store.get_decision(old["id"]).status == DecisionStatus.ACCEPTED


@pytest.mark.parametrize("mode", ["warm", "touch", "delete"])
def test_supersede_tool_stays_proposed_under_auto_all_on_an_index_rebuilt_since_the_sync(
    tmp_path, monkeypatch, mode
):
    """Red for ``touch`` and ``delete`` against unfixed code: the sync found the predecessor's only
    anchor orphaned, but a rebuilt index holds the placeholder ``live`` until the next sync, so the
    copied binding read as live and the successor was accepted. ``warm`` is the control."""
    graph = tmp_path / "graph.json"
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    for node in data["nodes"]:
        node.pop("community", None)  # no group binding: the leaf is the only anchor
    graph.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setenv("SIDEGRAPH_RATIFY_POLICY", "auto-all")
    store_dir = tmp_path / "store"
    with Store(store_dir) as store:
        old = _add_decision_impl(
            store,
            GraphifyReader(graph),
            title="Batch limit is 100",
            kind="constraint",
            context="c",
            choice="ch",
            anchors=ANCHORS,
        )
        gone = {n["id"] for n in data["nodes"] if n.get("source_file") == "trader/exec.py"}
        data["nodes"] = [n for n in data["nodes"] if n["id"] not in gone]
        key = "links" if "links" in data else "edges"
        data[key] = [
            e for e in data[key] if e.get("source") not in gone and e.get("target") not in gone
        ]
        graph.write_text(json.dumps(data), encoding="utf-8")
        sync(store, GraphifyReader(graph), force=True)
        assert {b.status for b in store.bindings_for_record(old["id"])} == {"orphaned"}
    if mode == "touch":
        record = next((store_dir / "decisions").glob("*.json"))
        record.write_text(record.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    if mode == "delete":
        for path in store_dir.glob("index.db*"):
            path.unlink()

    with Store(store_dir) as store:
        out = _supersede_through_the_tool(store, GraphifyReader(graph), old["id"], monkeypatch)

        assert out["status"] == "proposed" and out["ratified_by"] is None
        assert store.get_decision(out["id"]).status == DecisionStatus.PROPOSED
        assert store.get_decision(old["id"]).status == DecisionStatus.ACCEPTED
