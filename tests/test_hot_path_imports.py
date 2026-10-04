"""The hook hot path imports nothing heavy and never constructs a ``Store`` (T2).

PreToolUse runs before every Read, Grep, Edit and Write and every Bash read command, Stop
before every turn ends, and SubagentStart before every subagent's first turn. The package
import (pydantic, ulid, the models) and a full ``Store`` open each cost tens of milliseconds
that nobody sees and every session pays. The guard is structural, not a stopwatch (a wall-clock
tripwire flakes on CI runners): a fresh interpreter runs the entry point and reports which
modules it loaded.
``sidegraph.store`` absent already proves ``Store`` never ran.

see design/superpowers/specs/2026-10-03-hot-path-light-index-design.md (D3, D5),
design/superpowers/specs/2026-10-03-subagent-start-brief-design.md (T5),
design/superpowers/specs/2026-10-03-records-at-the-point-of-reading-design.md (T7) and
design/superpowers/specs/2026-10-03-records-in-subagent-briefs-design.md (T7)
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from tests.test_host_pretool import _seed_anchored_decision

# What the hot path must not load. ``pydantic_core`` and friends share the prefix.
_PROBE = (
    "import json, sys\n"
    "from {module} import {entry}\n"
    "{entry}()\n"
    "heavy = sorted(\n"
    "    m for m in sys.modules\n"
    "    if m.split('.')[0].startswith(('pydantic', 'ulid'))\n"
    "    or m in ('sidegraph.store', 'sidegraph.schema', 'sidegraph.retrieval')\n"
    ")\n"
    "sys.stderr.write('HEAVY=' + json.dumps(heavy))\n"
)

ANCHORED = "src/mod.py"


def _repo(tmp_path: Path) -> tuple[Path, Path]:
    """A project root with one anchored gotcha on ``src/mod.py`` and a store beside it."""
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / ANCHORED).write_text("x = 1\n")
    store_dir = root / ".sidegraph"
    _seed_anchored_decision(store_dir, ANCHORED, "Never call foo twice")
    return root, store_dir


def _run(
    entry: str,
    payload: dict,
    root: Path,
    store_dir: Path,
    extra_env: dict[str, str] | None = None,
    module: str = "sidegraph.host.hooks",
) -> tuple[dict, list[str]]:
    """Run one hook entry point of ``module`` in a fresh interpreter: its parsed stdout and the
    heavy modules it loaded."""
    env = {
        **os.environ,
        "CLAUDE_PROJECT_DIR": str(root),
        "SIDEGRAPH_DIR": str(store_dir),
        **(extra_env or {}),
    }
    done = subprocess.run(
        [sys.executable, "-c", _PROBE.format(module=module, entry=entry)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        cwd=root,
    )
    assert done.returncode == 0, done.stderr
    assert "HEAVY=" in done.stderr, done.stderr
    heavy = json.loads(done.stderr.split("HEAVY=")[-1])
    return json.loads(done.stdout), heavy


def _payload(tool: str, session: str, **tool_input) -> dict:
    return {"session_id": session, "tool_name": tool, "tool_input": tool_input}


def _touches(store_dir: Path) -> list[tuple[str, str, str]]:
    conn = sqlite3.connect(store_dir / "index.db")
    try:
        return conn.execute(
            "SELECT session_id, key, detail FROM retrieval_events WHERE kind = 'touch'"
        ).fetchall()
    finally:
        conn.close()


# -- T2 and T7: the PreToolUse paths: early exit, switches, Edit, Write, Read, Grep, Bash -----


def test_pretooluse_early_exit_loads_nothing_heavy(tmp_path):
    root, store_dir = _repo(tmp_path)
    out, heavy = _run("pre_tool_use", _payload("Glob", "g1", pattern="*"), root, store_dir)
    assert out == {}
    assert heavy == []


def test_pretooluse_with_both_switches_off_loads_nothing_heavy(tmp_path):
    root, store_dir = _repo(tmp_path)
    out, heavy = _run(
        "pre_tool_use",
        _payload("Read", "k1", file_path=str(root / ANCHORED)),
        root,
        store_dir,
        {"SIDEGRAPH_GREP_NUDGE": "off", "SIDEGRAPH_TELEMETRY": "off"},
    )
    assert out == {}
    assert heavy == []
    assert _touches(store_dir) == []


def test_pretooluse_edit_delivers_loads_nothing_heavy_and_still_records(tmp_path):
    """Was ``..._touch_only_...``: Edit records a touch and now also delivers the records of the
    file. The records query is where a ``Store`` would sneak in (T7)."""
    root, store_dir = _repo(tmp_path)
    out, heavy = _run(
        "pre_tool_use", _payload("Edit", "e1", file_path=str(root / ANCHORED)), root, store_dir
    )
    assert "Never call foo twice" in out["hookSpecificOutput"]["additionalContext"]
    assert heavy == []
    assert _touches(store_dir) == [("e1", ANCHORED, "Edit")]


def test_pretooluse_nudge_loads_nothing_heavy_and_names_the_record(tmp_path):
    root, store_dir = _repo(tmp_path)
    out, heavy = _run(
        "pre_tool_use", _payload("Read", "r1", file_path=str(root / ANCHORED)), root, store_dir
    )
    assert "Never call foo twice" in out["hookSpecificOutput"]["additionalContext"]
    assert heavy == []
    assert _touches(store_dir) == [("r1", ANCHORED, "Read")]


def test_pretooluse_grep_nudge_loads_nothing_heavy_and_names_the_record(tmp_path):
    """Grep reaches the nudge through ``tool_input["path"]``, not ``file_path``, and is the tool a
    heavy import would most likely hide behind (a branch on the tool name)."""
    root, store_dir = _repo(tmp_path)
    out, heavy = _run(
        "pre_tool_use",
        _payload("Grep", "gr1", pattern="foo", path=str(root / ANCHORED)),
        root,
        store_dir,
    )
    assert "Never call foo twice" in out["hookSpecificOutput"]["additionalContext"]
    assert heavy == []
    assert _touches(store_dir) == [("gr1", ANCHORED, "Grep")]


def test_pretooluse_pattern_only_grep_loads_nothing_heavy(tmp_path):
    """No path, so no file to deliver for and no touch row: nothing to join. Was the counting
    form, which scanned every decision row."""
    root, store_dir = _repo(tmp_path)
    out, heavy = _run("pre_tool_use", _payload("Grep", "gr2", pattern="def foo"), root, store_dir)
    assert out == {}
    assert heavy == []
    assert _touches(store_dir) == []


def test_pretooluse_write_delivers_loads_nothing_heavy_and_still_records(tmp_path):
    """Was ``..._write_...`` (a touch tool, not a nudge tool, printing ``{}``): Write of an
    anchored file delivers its records, and a Write that creates a file prints ``{}``."""
    root, store_dir = _repo(tmp_path)
    out, heavy = _run(
        "pre_tool_use", _payload("Write", "w1", file_path=str(root / ANCHORED)), root, store_dir
    )
    assert "Never call foo twice" in out["hookSpecificOutput"]["additionalContext"]
    assert heavy == []
    assert _touches(store_dir) == [("w1", ANCHORED, "Write")]
    created, heavy = _run(
        "pre_tool_use",
        _payload("Write", "w2", file_path=str(root / "src" / "brand_new.py")),
        root,
        store_dir,
    )
    assert created == {}
    assert heavy == []


def test_pretooluse_with_the_key_spent_loads_nothing_heavy(tmp_path):
    root, store_dir = _repo(tmp_path)
    payload = _payload("Read", "r1", file_path=str(root / ANCHORED))
    first, _ = _run("pre_tool_use", payload, root, store_dir)
    assert "hookSpecificOutput" in first
    out, heavy = _run("pre_tool_use", payload, root, store_dir)
    assert out == {}
    assert heavy == []


def test_pretooluse_bash_read_delivers_and_loads_nothing_heavy(tmp_path):
    """The Bash path: the path extractor, the one entity scan and the records query, for a line
    of two commands after a ``cd``. Red against a records query that builds a ``Store`` (T7).
    A Bash read is not a touch (D8)."""
    root, store_dir = _repo(tmp_path)
    out, heavy = _run(
        "pre_tool_use",
        _payload("Bash", "b1", command=f"cd {root / 'src'} && sed -n 1,5p mod.py | grep -n x"),
        root,
        store_dir,
    )
    assert "Never call foo twice" in out["hookSpecificOutput"]["additionalContext"]
    assert heavy == []
    assert _touches(store_dir) == []


def test_pretooluse_bash_that_reads_nothing_loads_nothing_heavy(tmp_path):
    """An older host fires the hook on every Bash call, and so does a plain ``Bash`` matcher: a
    line that names no file prints ``{}`` without opening the index."""
    root, store_dir = _repo(tmp_path)
    out, heavy = _run("pre_tool_use", _payload("Bash", "b2", command="git status"), root, store_dir)
    assert out == {}
    assert heavy == []


def test_the_unanchored_read_loads_nothing_heavy(tmp_path):
    """A file with nothing anchored to it prints nothing (the counting form is gone) after one
    scan of the entities."""
    root, store_dir = _repo(tmp_path)
    (root / "notes.md").write_text("x\n")
    out, heavy = _run(
        "pre_tool_use", _payload("Read", "n1", file_path=str(root / "notes.md")), root, store_dir
    )
    assert out == {}
    assert heavy == []


def test_pretooluse_agent_brief_loads_nothing_heavy_and_carries_the_record(tmp_path):
    """The Agent branch: the path extractor, the hop read, the one entity scan and the records
    query, for a brief that names a plan that names an anchored file. Red against a records
    query that builds a ``Store`` (T7 of the subagent-brief spec)."""
    root, store_dir = _repo(tmp_path)
    (root / "docs").mkdir()
    (root / "docs" / "plan.md").write_text(f"Step 1: change `{ANCHORED}`.\n")
    out, heavy = _run(
        "pre_tool_use",
        _payload("Agent", "ag1", description="d", prompt="Implement docs/plan.md step 1."),
        root,
        store_dir,
    )
    prompt = out["hookSpecificOutput"]["updatedInput"]["prompt"]
    assert "Never call foo twice" in prompt
    assert f"{ANCHORED} (named in docs/plan.md):" in prompt
    assert heavy == []
    assert _touches(store_dir) == []


def test_pretooluse_agent_brief_that_names_nothing_loads_nothing_heavy(tmp_path):
    root, store_dir = _repo(tmp_path)
    out, heavy = _run(
        "pre_tool_use",
        _payload("Agent", "ag2", description="d", prompt="Refactor the payments module."),
        root,
        store_dir,
    )
    assert out == {}
    assert heavy == []


def test_pretooluse_agent_brief_kill_switch_loads_nothing_heavy(tmp_path):
    root, store_dir = _repo(tmp_path)
    out, heavy = _run(
        "pre_tool_use",
        _payload("Agent", "ag3", description="d", prompt=f"Fix {ANCHORED}."),
        root,
        store_dir,
        {"SIDEGRAPH_AGENT_BRIEF": "off"},
    )
    assert out == {}
    assert heavy == []


# -- T5: the SubagentStart brief --------------------------------------------------------------

_SUBAGENT = "sidegraph.host.subagent"
_SUBAGENT_PAYLOAD = {"session_id": "s1", "hook_event_name": "SubagentStart", "agent_id": "a1"}


def test_subagent_start_brief_loads_nothing_heavy_and_states_the_counts(tmp_path):
    """The path that reads every decision, binding and entity row for the counts. Red against a
    hook that builds a ``Store`` for them (M2)."""
    root, store_dir = _repo(tmp_path)
    out, heavy = _run("subagent_start", _SUBAGENT_PAYLOAD, root, store_dir, module=_SUBAGENT)
    text = out["hookSpecificOutput"]["additionalContext"]
    assert "1 record anchored to code in 1 file, 1 of them recorded mistakes" in text
    assert heavy == []


def test_subagent_start_kill_switch_loads_nothing_heavy(tmp_path):
    root, store_dir = _repo(tmp_path)
    out, heavy = _run(
        "subagent_start",
        _SUBAGENT_PAYLOAD,
        root,
        store_dir,
        {"SIDEGRAPH_SUBAGENT_BRIEF": "off"},
        module=_SUBAGENT,
    )
    assert out == {}
    assert heavy == []


def test_subagent_start_without_a_store_loads_nothing_heavy(tmp_path):
    root = tmp_path / "bare"
    root.mkdir()
    out, heavy = _run(
        "subagent_start", _SUBAGENT_PAYLOAD, root, root / ".sidegraph", module=_SUBAGENT
    )
    assert out == {}
    assert heavy == []
    assert not (root / ".sidegraph").exists()
