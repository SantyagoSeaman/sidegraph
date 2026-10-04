"""The SubagentStart brief: ``sidegraph-subagent-start`` (T1, T2, T3).

A Claude Code or Codex subagent loads no SessionStart context, and Explore and Plan agents load
no ``CLAUDE.md`` either, so nothing standing tells one that decision memory exists. The hook
gives each subagent a short brief: the call to make, and one sentence on what memory holds. The
counts in that sentence are what ``get_task_context`` could return, read from the index through
``HotIndex``; where the index cannot be used the hook prints ``{}`` and writes nothing.

see design/superpowers/specs/2026-10-03-subagent-start-brief-design.md (D1, D2, D4, D5)
"""

from __future__ import annotations

import io
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import sidegraph.host.subagent as subagent
from sidegraph.host.hooks import STANDING_SEARCH_INSTRUCTION
from sidegraph.schema import AnchorBinding, DecisionKind, DecisionStatus, Entity, EntityKind
from sidegraph.store import Store
from tests.test_hot_index import _REFUSED, FILE_A, _broken_project, _Seeder, _snapshot

NOW = datetime.now(UTC)

# The seeded store of T1. Counted: six live, surfacing, file-anchored records on four files
# (a gotcha on a.py, two ADRs on b.py, a replacing lesson on a.py, a fresh constraint proposal
# on f.py, and a gotcha whose only binding is degraded on g.py), four of them mistakes. Not
# counted: a superseded record (c.py), an expired one (d.py), a proposal past the window (e.py),
# an orphaned binding (h.py), a deprecated record (i.py) and a record whose only binding is on
# an abstract entity.
_DEFAULT = (6, 4, 4)  # records, files, mistakes
_UNRATIFIED_OFF = (5, 3, 3)  # the fresh proposal on f.py stops surfacing

_COUNTS = re.compile(
    r"(\d+) records anchored to code in (\d+) files, (\d+) of them recorded mistakes"
)

# Codex's output contract for a hook: any other key is an unknown field to it.
_CODEX_OUTPUT_KEYS = {
    "continue",
    "stopReason",
    "suppressOutput",
    "systemMessage",
    "hookSpecificOutput",
}


def _tier0_only(store: Store, s: _Seeder) -> None:
    """A live accepted gotcha whose only binding is Tier-0, on an abstract entity: it holds no
    file, so ``get_task_context(files=[...])`` has nothing to match it to."""
    abstract = store.upsert_entity(Entity(canonical_name="Payments", kind=EntityKind.ABSTRACT))
    tier0 = s.decision("Tier-0 only", DecisionKind.GOTCHA)
    store.add_binding(AnchorBinding(record_id=tier0.id, entity_id=abstract.entity_id, tier=0))


def _seed_dead(store: Store, s: _Seeder) -> None:
    """Records a subagent could never be handed: each fails one rule of the count."""
    G = DecisionKind.GOTCHA
    old = s.decision("Superseded on c", G, age_days=30)
    s.anchor(old, "src/c.py")
    # the replacement is itself expired, so it does not count either
    s.decision(
        "Replacement, expired", G, age_days=10, valid_to=NOW - timedelta(days=1), supersedes=old.id
    )
    s.anchor(
        s.decision("Expired on d", G, age_days=10, valid_to=NOW - timedelta(days=1)), "src/d.py"
    )
    s.anchor(s.decision("Old proposal on e", G, DecisionStatus.PROPOSED, age_days=40), "src/e.py")
    s.anchor(s.decision("Orphaned on h", G), "src/h.py", status="orphaned")
    s.anchor(s.decision("Deprecated on i", G, DecisionStatus.DEPRECATED), "src/i.py")
    _tier0_only(store, s)


def _seed(store_dir: Path) -> None:
    store = Store(store_dir)
    s = _Seeder(store)
    G, L, A, C = (
        DecisionKind.GOTCHA,
        DecisionKind.LESSON,
        DecisionKind.ADR,
        DecisionKind.CONSTRAINT,
    )
    s.anchor(s.decision("Gotcha on a", G), "src/a.py")
    s.anchor(s.decision("ADR on b", A), "src/b.py")
    s.anchor(s.decision("A second ADR on b", A), "src/b.py")
    s.anchor(s.decision("Fresh proposal on f", C, DecisionStatus.PROPOSED, age_days=5), "src/f.py")
    # the superseded record sits on its own file, so counting it would move M as well as N
    old = s.decision("Superseded on c", G, age_days=30)
    s.anchor(old, "src/c.py")
    s.anchor(s.decision("Replacement on a", L, age_days=3, supersedes=old.id), "src/a.py")
    s.anchor(
        s.decision("Expired on d", L, age_days=10, valid_to=NOW - timedelta(days=1)), "src/d.py"
    )
    s.anchor(s.decision("Old proposal on e", G, DecisionStatus.PROPOSED, age_days=40), "src/e.py")
    s.anchor(s.decision("Orphaned on h", G), "src/h.py", status="orphaned")
    # a degraded binding still hands the record to get_task_context, on a file of its own
    s.anchor(s.decision("Degraded on g", G), "src/g.py", status="degraded")
    # get_task_context never returns a deprecated record, so it is on a file of its own too
    s.anchor(s.decision("Deprecated on i", G, DecisionStatus.DEPRECATED), "src/i.py")
    _tier0_only(store, s)
    store.close()


def _project(tmp_path: Path, *, seeded: bool = True) -> tuple[Path, Path]:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / FILE_A).write_text("x = 1\n")
    store_dir = root / ".sidegraph"
    if seeded:
        _seed(store_dir)
    return root, store_dir


CLAUDE_PAYLOAD = {
    "session_id": "parent-session",
    "transcript_path": "/x/parent-session.jsonl",
    "cwd": "/repo",
    "hook_event_name": "SubagentStart",
    "prompt_id": "p1",
    "agent_id": "a1b2c3",
    "agent_type": "Explore",
}
CODEX_PAYLOAD = {
    "session_id": "workspace-session",
    "turn_id": "t1",
    "transcript_path": None,
    "cwd": "/repo",
    "hook_event_name": "SubagentStart",
    "model": "gpt-5",
    "permission_mode": "default",
    "agent_id": "a1b2c3",
    "agent_type": "default",
}


def _run(monkeypatch, capsys, root: Path, store_dir: Path, payload, extra_env=None) -> dict:
    monkeypatch.setenv("SIDEGRAPH_DIR", str(store_dir))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(root))
    for key, value in (extra_env or {}).items():
        monkeypatch.setenv(key, value)
    stdin = payload if isinstance(payload, str) else json.dumps(payload)
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    subagent.subagent_start()
    captured = capsys.readouterr()
    assert captured.err == ""
    lines = captured.out.splitlines()
    assert len(lines) == 1, captured.out
    return json.loads(lines[0])


def _brief(out: dict) -> str:
    assert set(out) == {"hookSpecificOutput"}, out
    specific = out["hookSpecificOutput"]
    assert specific["hookEventName"] == "SubagentStart"
    assert set(specific) == {"hookEventName", "additionalContext"}
    return specific["additionalContext"]


def _numbers(text: str) -> tuple[int, int, int]:
    found = _COUNTS.search(text)
    assert found, text
    return int(found[1]), int(found[2]), int(found[3])


# -- T1: the brief over a seeded store ---------------------------------------------------------


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        pytest.param({}, _DEFAULT, id="default"),
        pytest.param({"SIDEGRAPH_UNRATIFIED": "off"}, _UNRATIFIED_OFF, id="unratified-off"),
    ],
)
def test_t1_a_claude_subagent_gets_the_call_and_the_counts(
    tmp_path, monkeypatch, capsys, env, expected
):
    """Red against a missing entry point, a brief without the call (M1), counts that include
    the superseded and expired records the store holds (M5), counts that include a deprecated
    record, and counts that leave out a degraded binding (the ``status == "live"`` mutant)."""
    root, store_dir = _project(tmp_path)
    text = _brief(_run(monkeypatch, capsys, root, store_dir, CLAUDE_PAYLOAD, env))
    assert "get_task_context(files=" in text
    assert text.startswith(STANDING_SEARCH_INSTRUCTION)
    assert _numbers(text) == expected
    assert len(text) < 700


def test_t1_a_single_record_reads_in_the_singular(tmp_path, monkeypatch, capsys):
    root, store_dir = _project(tmp_path, seeded=False)
    store = Store(store_dir)
    s = _Seeder(store)
    s.anchor(s.decision("The only one", DecisionKind.GOTCHA), "src/only.py")
    store.close()
    text = _brief(_run(monkeypatch, capsys, root, store_dir, CLAUDE_PAYLOAD))
    assert "1 record anchored to code in 1 file, 1 of them recorded mistakes" in text


# -- T2: a Codex-shaped payload ----------------------------------------------------------------


def test_t2_a_codex_subagent_gets_the_same_brief_in_keys_codex_accepts(
    tmp_path, monkeypatch, capsys
):
    root, store_dir = _project(tmp_path)
    out = _run(monkeypatch, capsys, root, store_dir, CODEX_PAYLOAD)
    assert set(out) <= _CODEX_OUTPUT_KEYS
    text = _brief(out)
    assert "get_task_context(files=" in text
    assert text.startswith(STANDING_SEARCH_INSTRUCTION)
    assert _numbers(text) == _DEFAULT
    assert len(text) < 700


# -- T3: where there is nothing to say, say nothing and write nothing ---------------------------


@pytest.mark.parametrize("how", sorted(_REFUSED))
def test_t3_a_refused_index_prints_nothing_and_writes_nothing(tmp_path, monkeypatch, capsys, how):
    """Red against a raw open that bypasses ``HotIndex``'s guards: a missing index would be
    created, and a symlinked one written through."""
    root, store_dir, elsewhere = _broken_project(tmp_path, how)
    before = _snapshot(root, elsewhere)
    assert _run(monkeypatch, capsys, root, store_dir, CLAUDE_PAYLOAD) == {}
    assert _snapshot(root, elsewhere) == before


def test_t3_a_project_without_a_store_gets_no_store(tmp_path, monkeypatch, capsys):
    root, store_dir = _project(tmp_path, seeded=False)
    assert _run(monkeypatch, capsys, root, store_dir, CLAUDE_PAYLOAD) == {}
    assert not store_dir.exists()


@pytest.mark.parametrize("dead", [False, True], ids=["empty", "only-dead-records"])
def test_t3_a_store_with_nothing_to_ask_about_prints_nothing(tmp_path, monkeypatch, capsys, dead):
    """N is 0 for an empty store and for one whose records are all superseded, expired,
    orphaned, past the proposal window or bound to no file."""
    root, store_dir = _project(tmp_path, seeded=False)
    store = Store(store_dir)
    if dead:
        _seed_dead(store, _Seeder(store))
    store.close()
    before = _snapshot(root)
    assert _run(monkeypatch, capsys, root, store_dir, CLAUDE_PAYLOAD) == {}
    assert _snapshot(root) == before


def test_t3_the_kill_switch_prints_nothing(tmp_path, monkeypatch, capsys):
    root, store_dir = _project(tmp_path)
    before = _snapshot(root)
    out = _run(
        monkeypatch, capsys, root, store_dir, CLAUDE_PAYLOAD, {"SIDEGRAPH_SUBAGENT_BRIEF": "off"}
    )
    assert out == {}
    assert _snapshot(root) == before


def test_t3_the_kill_switch_means_off_and_nothing_else(tmp_path, monkeypatch, capsys):
    root, store_dir = _project(tmp_path)
    out = _run(
        monkeypatch, capsys, root, store_dir, CLAUDE_PAYLOAD, {"SIDEGRAPH_SUBAGENT_BRIEF": "on"}
    )
    assert "hookSpecificOutput" in out


@pytest.mark.parametrize("stdin", ["not json", "{", "[1, 2]", '"text"'])
def test_t3_malformed_stdin_prints_nothing_and_writes_nothing(tmp_path, monkeypatch, capsys, stdin):
    root, store_dir = _project(tmp_path)
    before = _snapshot(root)
    assert _run(monkeypatch, capsys, root, store_dir, stdin) == {}
    assert _snapshot(root) == before


@pytest.mark.parametrize("event", ["Stop", "SessionStart", "PreToolUse", "subagentstart", ""])
def test_t3_a_payload_naming_another_event_prints_nothing(tmp_path, monkeypatch, capsys, event):
    """A mis-wired entry must not emit a brief tagged SubagentStart for some other event."""
    root, store_dir = _project(tmp_path)
    before = _snapshot(root)
    payload = {**CLAUDE_PAYLOAD, "hook_event_name": event}
    assert _run(monkeypatch, capsys, root, store_dir, payload) == {}
    assert _snapshot(root) == before


@pytest.mark.parametrize("stdin", ["", "  \n", "{}", '{"session_id": "s"}'])
def test_t3_a_payload_naming_no_event_still_gets_the_brief(tmp_path, monkeypatch, capsys, stdin):
    """Empty stdin counts as ``{}``, and a payload with no ``hook_event_name`` is not refused."""
    root, store_dir = _project(tmp_path)
    assert _numbers(_brief(_run(monkeypatch, capsys, root, store_dir, stdin))) == _DEFAULT


def test_t3_a_failure_while_counting_prints_nothing(tmp_path, monkeypatch, capsys):
    from sidegraph.hot_index import HotIndex

    def boom(self):
        raise RuntimeError("boom")

    root, store_dir = _project(tmp_path)
    monkeypatch.setattr(HotIndex, "memory_counts", boom)
    assert _run(monkeypatch, capsys, root, store_dir, CLAUDE_PAYLOAD) == {}
