import json

import pytest

from sidegraph.engine.reader import GraphifyReader
from sidegraph.retrieval import Seed, get_task_context
from sidegraph.server import _propose_decisions_impl, _ratify_decisions_impl
from sidegraph.store import Store
from sidegraph.sync import sync

GRAPH_A = {
    "built_at_commit": "vA",
    "nodes": [
        {
            "id": "n_gate",
            "label": "fee_gate()",
            "norm_label": "fee_gate()",
            "file_type": "code",
            "source_file": "risk/gate.py",
            "community": 1,
        },
        {
            "id": "n_kill",
            "label": "kill_switch()",
            "norm_label": "kill_switch()",
            "file_type": "code",
            "source_file": "risk/kill.py",
            "community": 1,
        },
    ],
    "links": [],
}
# Rebuild: identical nodes, Leiden renumbered 1 -> 7 (the dogfood case).
GRAPH_B = {
    **GRAPH_A,
    "built_at_commit": "vB",
    "nodes": [dict(n, community=7) for n in GRAPH_A["nodes"]],
}


@pytest.fixture(autouse=True)
def _no_ambient_initiative(monkeypatch):
    monkeypatch.setattr("sidegraph.capture._derive_initiative", lambda: None)


def _write(tmp_path, name, data):
    p = tmp_path / name
    p.write_text(json.dumps(data))
    return p


def test_community_fallback_survives_renumbering(tmp_path):
    store = Store(tmp_path / "e.db")
    reader_a = GraphifyReader(_write(tmp_path, "a.json", GRAPH_A))

    # Capture a gotcha anchored to fee_gate (leaf + community:1) and ratify it.
    results = _propose_decisions_impl(
        store,
        reader_a,
        [
            {
                "title": "gate before trade",
                "kind": "gotcha",
                "context": "c",
                "choice": "keep",
                "anchors": [{"name": "fee_gate", "file_path": "risk/gate.py"}],
            }
        ],
        session_id="s-repoint",
    )
    _ratify_decisions_impl(store, accept=[results[0]["decision_id"]])

    # Community fallback BEFORE the rebuild: seeding by the *other* file in the cluster
    # surfaces the decision via community:1.
    ctx = get_task_context([Seed(file_path="risk/kill.py")], store, reader_a)
    assert any("gate before trade" in r for r in ctx.related)

    # Rebuild renumbers 1 -> 7; sync re-points.
    reader_b = GraphifyReader(_write(tmp_path, "b.json", GRAPH_B))
    report = sync(store, reader_b)
    assert sum(o.repointed for o in report.outcomes) >= 1

    # The fallback works again under the NEW numbering.
    ctx2 = get_task_context([Seed(file_path="risk/kill.py")], store, reader_b)
    assert any("gate before trade" in r for r in ctx2.related)

    # Idempotent: nothing further to re-point on a forced second pass.
    report2 = sync(store, reader_b, force=True)
    assert sum(o.repointed for o in report2.outcomes) == 0


GRAPH_C = {
    "built_at_commit": "vC",
    "nodes": [
        # fee_gate renamed away; kill_switch survives; cluster renumbered 1 -> 9.
        {
            "id": "n_gate2",
            "label": "fee_gate_v2()",
            "norm_label": "fee_gate_v2()",
            "file_type": "code",
            "source_file": "risk/gate.py",
            "community": 9,
        },
        {
            "id": "n_kill",
            "label": "kill_switch()",
            "norm_label": "kill_switch()",
            "file_type": "code",
            "source_file": "risk/kill.py",
            "community": 9,
        },
    ],
    "links": [],
}


def test_orphaned_decision_reachable_via_fallback_after_renumber(tmp_path):
    store = Store(tmp_path / "e.db")
    reader_a = GraphifyReader(_write(tmp_path, "a.json", GRAPH_A))
    results = _propose_decisions_impl(
        store,
        reader_a,
        [
            {
                "title": "gate before trade",
                "kind": "gotcha",
                "context": "c",
                "choice": "keep",
                "anchors": [{"name": "fee_gate", "file_path": "risk/gate.py"}],
            }
        ],
        session_id="s-orphan",
    )
    _ratify_decisions_impl(store, accept=[results[0]["decision_id"]])

    # Rename + renumber in ONE rebuild (the dogfood-verified failure mode).
    reader_c = GraphifyReader(_write(tmp_path, "c.json", GRAPH_C))
    report = sync(store, reader_c)
    assert {d["title"] for d in report.stale_decisions} == {"gate before trade"}

    # The orphaned decision is still reachable via the CURRENT community numbering.
    ctx = get_task_context([Seed(file_path="risk/gate.py")], store, reader_c)
    assert any("gate before trade" in r for r in ctx.related)
