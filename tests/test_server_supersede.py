"""supersede_decision anchoring — the successor must not write unanchored (see CLAUDE.md
gap notes: a superseded decision written unanchored was invisible to task-seeded retrieval
exactly where a reversal matters most)."""

from __future__ import annotations

import json
from pathlib import Path

from sidegraph.engine.reader import GraphifyReader
from sidegraph.server import _add_decision_impl, _supersede_decision_impl
from sidegraph.store import Store

FIXTURE = Path(__file__).parent / "fixtures" / "mini_graph.json"


def _ambiguous_reader(tmp_path):
    """Two nodes named "run()" sharing a community — resolve() comes back ambiguous with
    the shared community still known (same fixture shape as test_server_capture.py's)."""
    data = {
        "built_at_commit": "x",
        "nodes": [
            {
                "id": "a",
                "label": "run()",
                "norm_label": "run()",
                "file_type": "code",
                "source_file": "m.py",
                "community": "7",
            },
            {
                "id": "b",
                "label": "run()",
                "norm_label": "run()",
                "file_type": "code",
                "source_file": "m.py",
                "community": "7",
            },
        ],
        "links": [],
    }
    graph_path = tmp_path / "g.json"
    graph_path.write_text(json.dumps(data))
    return GraphifyReader(graph_path)


def test_supersede_default_inherits_predecessor_bindings(tmp_path):
    store = Store(tmp_path / "t.db")
    reader = GraphifyReader(FIXTURE)
    old = _add_decision_impl(
        store,
        reader,
        title="Use Trader for exec",
        kind="adr",
        context="c",
        choice="ch",
        anchors=[{"name": "Trader", "file_path": "trader/exec.py", "relation": "creates"}],
    )
    assert old["bindings"] == 2  # leaf + community

    out = _supersede_decision_impl(
        store,
        reader,
        old["id"],
        title="Stop using Trader for exec",
        kind="adr",
        context="c2",
        choice="ch2",
    )

    assert out["bindings"] == 2
    old_entity_ids = {e["entity_id"] for e in old["entities"]}
    new_entity_ids = {e["entity_id"] for e in out["entities"]}
    assert old_entity_ids == new_entity_ids
    old_binds = {
        (b.entity_id, b.tier, b.weight, b.relation, b.status)
        for b in store.bindings_for_record(old["id"])
    }
    new_binds = {
        (b.entity_id, b.tier, b.weight, b.relation, b.status)
        for b in store.bindings_for_record(out["id"])
    }
    assert old_binds == new_binds


def test_supersede_explicit_anchors_used_instead_of_inheritance(tmp_path):
    store = Store(tmp_path / "t.db")
    reader = GraphifyReader(FIXTURE)
    old = _add_decision_impl(
        store,
        reader,
        title="Use Trader for exec",
        kind="adr",
        context="c",
        choice="ch",
        anchors=[{"name": "Trader", "file_path": "trader/exec.py"}],
    )

    out = _supersede_decision_impl(
        store,
        reader,
        old["id"],
        title="Use helper instead",
        kind="adr",
        context="c2",
        choice="ch2",
        anchors=[{"name": "helper", "file_path": "util/misc.py"}],
    )

    new_binds = store.bindings_for_record(out["id"])
    new_entity_names = {store.get_entity(b.entity_id).canonical_name for b in new_binds}
    old_entity_names = {
        store.get_entity(b.entity_id).canonical_name for b in store.bindings_for_record(old["id"])
    }
    # the successor is anchored to the new ref, not the predecessor's Trader anchors
    assert "Trader" not in new_entity_names
    assert new_entity_names.isdisjoint(old_entity_names)
    assert any("helper" in n for n in new_entity_names)


def test_supersede_without_anchors_and_predecessor_had_none(tmp_path):
    store = Store(tmp_path / "t.db")
    old = _add_decision_impl(store, None, title="t", kind="adr", context="c", choice="ch")
    assert old["bindings"] == 0

    out = _supersede_decision_impl(
        store,
        None,
        old["id"],
        title="t2",
        kind="adr",
        context="c2",
        choice="ch2",
    )

    assert out["bindings"] == 0
    assert out["entities"] == []
    assert out["id"]
    assert out["supersedes"] == old["id"]


def test_supersede_explicit_ambiguous_anchor_reports_anchors_skipped(tmp_path):
    """Gate finding: the supersede path used to discard `resolve_and_bind`'s per-anchor
    result entirely, so an ambiguous explicit anchor on a supersede silently produced no
    Tier-2 leaf AND no feedback about why -- unlike `add_decision`, which has carried
    `anchors_skipped` since Gate-5 finding S3. `supersede_decision` must return the same
    per-anchor feedback shape when explicit anchors are passed."""
    store = Store(tmp_path / "t.db")
    reader = _ambiguous_reader(tmp_path)
    old = _add_decision_impl(store, None, title="t", kind="adr", context="c", choice="ch")

    out = _supersede_decision_impl(
        store,
        reader,
        old["id"],
        title="t2",
        kind="adr",
        context="c2",
        choice="ch2",
        anchors=[{"name": "run"}],
    )

    assert out["anchors_skipped"] == [
        {"name": "run", "reason": "ambiguous", "candidates": ["a", "b"]}
    ]
    # no Tier-2 leaf was created for the ambiguous anchor -- only the Tier-1 community
    # binding, same shape as add_decision's ambiguous-anchor behavior.
    assert out["bindings"] == 1


def test_supersede_closes_predecessor(tmp_path):
    store = Store(tmp_path / "t.db")
    old = _add_decision_impl(store, None, title="t", kind="adr", context="c", choice="ch")

    out = _supersede_decision_impl(
        store,
        None,
        old["id"],
        title="t2",
        kind="adr",
        context="c2",
        choice="ch2",
    )

    reloaded_old = store.get_decision(old["id"])
    assert reloaded_old.status.value == "superseded"
    assert reloaded_old.valid_to is not None
    assert store.get_decision(out["id"]).supersedes == old["id"]


def test_supersede_stamps_session_author_source_and_commit(tmp_path, monkeypatch):
    """Staleness machinery D6: session_id/author/source are new optional params, stamped
    onto the successor's provenance the same way propose stamps a captured decision's;
    commit is best-effort git rev-parse HEAD, same helper propose uses -- resolved from
    the STORE's own repo (CORRECTION-2, code review), not the ambient process cwd, so the
    store lives INSIDE the repo while the process cwd is elsewhere."""
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    (repo / "f.txt").write_text("x")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=repo, check=True)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)  # NOT the repo -- proves cwd is irrelevant

    store = Store(repo / ".sidegraph")  # the store lives INSIDE the repo
    old = _add_decision_impl(store, None, title="t", kind="adr", context="c", choice="ch")

    out = _supersede_decision_impl(
        store,
        None,
        old["id"],
        title="t2",
        kind="adr",
        context="c2",
        choice="ch2",
        session_id="s1",
        author="alex",
        source="agent",
    )

    prov = store.get_decision(out["id"]).provenance
    assert prov.session_id == "s1"
    assert prov.author == "alex"
    assert prov.source == "agent"
    assert prov.commit == head


def test_supersede_stamps_human_provenance(tmp_path):
    """BEH-1 finding: supersede_decision is the human-asked path, same as
    supersede_fact (which already stamps "human") -- it was stamping "agent", the value
    reserved for the agent-initiated propose_decisions pipeline."""
    store = Store(tmp_path / "t.db")
    old = _add_decision_impl(store, None, title="t", kind="adr", context="c", choice="ch")

    out = _supersede_decision_impl(
        store,
        None,
        old["id"],
        title="t2",
        kind="adr",
        context="c2",
        choice="ch2",
    )

    assert store.get_decision(out["id"]).provenance.source == "human"


def test_supersede_falls_back_to_telemetry_session_marker(tmp_path):
    """E9b measured `session_id: None` on supersede-path successors: the D7.3 marker
    fallback was wired into propose only. The supersede path reads the same
    TELEMETRY_SESSION_KEY marker when the caller passes no session_id."""
    from datetime import UTC, datetime

    from sidegraph.config import TELEMETRY_SESSION_KEY

    store = Store(tmp_path / "t.db")
    old = _add_decision_impl(store, None, title="t", kind="adr", context="c", choice="ch")
    store.set_meta(TELEMETRY_SESSION_KEY, f"session-xyz|{datetime.now(UTC).isoformat()}")

    out = _supersede_decision_impl(
        store,
        None,
        old["id"],
        title="t2",
        kind="adr",
        context="c2",
        choice="ch2",
    )

    assert store.get_decision(out["id"]).provenance.session_id == "session-xyz"


def test_supersede_explicit_session_id_wins_over_fallback(tmp_path):
    from datetime import UTC, datetime

    from sidegraph.config import TELEMETRY_SESSION_KEY

    store = Store(tmp_path / "t.db")
    old = _add_decision_impl(store, None, title="t", kind="adr", context="c", choice="ch")
    store.set_meta(TELEMETRY_SESSION_KEY, f"marker-session|{datetime.now(UTC).isoformat()}")

    out = _supersede_decision_impl(
        store,
        None,
        old["id"],
        title="t2",
        kind="adr",
        context="c2",
        choice="ch2",
        session_id="explicit-session",
    )

    assert store.get_decision(out["id"]).provenance.session_id == "explicit-session"
