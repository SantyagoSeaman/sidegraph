"""The whole chain, asserted positively (spec rows 13 and 16).

SessionStart publishes a key -> a retrieval records what it surfaced -> the hook records
what the agent then touched -> the redirect is reconstructable from the journal alone.
"""

from __future__ import annotations

import io
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import sidegraph.host.hooks as hooks
from sidegraph.engine.reader import GraphifyReader
from sidegraph.schema import AnchorBinding, Decision, DecisionKind, Descriptor, Entity, Provenance
from sidegraph.server import _get_task_context_impl
from sidegraph.store import Store

FIXTURE = Path(__file__).parent / "fixtures" / "mini_graph.json"


def test_redirect_is_reconstructable_from_the_journal(tmp_path, monkeypatch, capsys):
    root = tmp_path / "repo"
    (root / "trader").mkdir(parents=True)
    (root / "trader" / "exec.py").write_text("x = 1\n")
    (root / "trader" / "risk.py").write_text("y = 2\n")
    db = root / ".sidegraph"

    # A gotcha anchored to BOTH files: the agent will seed on one and touch the other.
    reader = GraphifyReader(FIXTURE)
    store = Store(db)
    seeded = store.upsert_entity(
        Entity(
            canonical_name="Trader",
            descriptor=Descriptor(name="Trader", file_path="trader/exec.py"),
        )
    )
    other = store.upsert_entity(
        Entity(
            canonical_name="Risk",
            descriptor=Descriptor(name="Risk", file_path="trader/risk.py"),
        )
    )
    decision = Decision(
        title="deadlock",
        kind=DecisionKind.GOTCHA,
        context="c",
        choice="order locks",
        valid_from=datetime.now(UTC),
        provenance=Provenance(source="manual"),
    )
    store._write_decision(decision)
    store._conn.commit()
    store.add_binding(AnchorBinding(record_id=decision.id, entity_id=seeded.entity_id, tier=2))
    store.add_binding(AnchorBinding(record_id=decision.id, entity_id=other.entity_id, tier=2))
    store.close()

    monkeypatch.setenv("SIDEGRAPH_DB", str(db))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(root))

    # 1. SessionStart publishes the key.
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "e2e-1"})))
    hooks.session_start()
    capsys.readouterr()

    # 2. The agent asks about exec.py; the gotcha surfaces, carrying risk.py too.
    store = Store(db)
    _get_task_context_impl(store, reader, ["trader/exec.py"], None, 4000, 6000)
    store.close()

    # 3. The agent then opens risk.py — a file it never asked about.
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(
            json.dumps(
                {
                    "session_id": "e2e-1",
                    "tool_name": "Read",
                    "tool_input": {"file_path": str(root / "trader" / "risk.py")},
                }
            )
        ),
    )
    hooks.pre_tool_use()
    capsys.readouterr()

    # 4. The redirect: an anchor shown, outside the seed set, touched afterwards.
    store = Store(db)
    events = store.retrieval_events("e2e-1")
    store.close()

    seeds = {e["key"] for e in events if e["kind"] == "seed"}
    shown = {e["key"] for e in events if e["kind"] == "show_anchor"}
    touched = [e["key"] for e in events if e["kind"] == "touch"]

    assert seeds == {"trader/exec.py"}
    assert "trader/risk.py" in shown
    assert "trader/risk.py" in touched
    assert (shown - seeds) & set(touched) == {"trader/risk.py"}

    order = [e["kind"] for e in events]
    assert order.index("show_anchor") < order.index("touch"), "the show must precede the touch"


def test_the_whole_chain_leaves_git_clean(tmp_path, monkeypatch, capsys):
    """GUARD (declared exception, spec row 13): passes before and after, and exists so a
    later change cannot make a read path write canonically."""
    root = tmp_path / "repo"
    root.mkdir()
    (root / "trader").mkdir()
    (root / "trader" / "exec.py").write_text("x = 1\n")
    db = root / ".sidegraph"
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)

    reader = GraphifyReader(FIXTURE)
    store = Store(db)
    store.upsert_entity(
        Entity(
            canonical_name="Trader",
            descriptor=Descriptor(name="Trader", file_path="trader/exec.py"),
        )
    )
    store.close()
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=root,
        check=True,
    )

    monkeypatch.setenv("SIDEGRAPH_DB", str(db))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(root))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "clean-1"})))
    hooks.session_start()
    capsys.readouterr()

    store = Store(db)
    _get_task_context_impl(store, reader, ["trader/exec.py"], None, 4000, 6000)
    store.close()

    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(
            json.dumps(
                {
                    "session_id": "clean-1",
                    "tool_name": "Read",
                    "tool_input": {"file_path": str(root / "trader" / "exec.py")},
                }
            )
        ),
    )
    hooks.pre_tool_use()
    capsys.readouterr()

    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True, check=True
    )
    assert status.stdout == "", f"a read path dirtied git: {status.stdout}"
