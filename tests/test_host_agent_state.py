"""Hook state is per agent: a Claude Code subagent gets its own deliveries and touch rows.

A subagent's PreToolUse payload carries its parent's ``session_id`` and ``transcript_path``,
plus its own ``agent_id`` and ``agent_type``; it fires no SessionStart and no Stop. Keyed on
the session alone, one agent's delivery covered the whole agent tree and every subagent after
it read blind. ``agent_id`` is read in the host seam only; the store takes it opaque. The main
agent's key carries ``-`` where a subagent's carries its id.
See design/superpowers/specs/2026-10-01-per-agent-hook-state-design.md and
design/superpowers/specs/2026-10-03-records-at-the-point-of-reading-design.md (D2).
"""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime, timedelta

import sidegraph.host.hooks as hooks
from sidegraph.stats.model import build_report
from sidegraph.store import Store
from tests.test_host_pretool import _seed_anchored_decision

SESSION = "3f2a9c1e-5b7d-4e8a-9c0b-1d2e3f4a5b6c"
# A redacted real shape: the keys a subagent's PreToolUse payload carries, with neutral values
# (no home directory, no account name, no project name).
_TRANSCRIPT = f"/work/repo/.claude/projects/-work-repo/{SESSION}.jsonl"
A = "a0b1c2d3e4f5a6b7c"
B = "b7a6f5e4d3c2b1a0f"
PATH_A = "src/alpha.py"
PATH_B = "src/beta.py"


def _payload(path: str, *, agent_id: str | None = None, agent_type: str | None = None) -> dict:
    payload = {
        "session_id": SESSION,
        "transcript_path": _TRANSCRIPT,
        "cwd": "/work/repo",
        "permission_mode": "default",
        "prompt_id": "5d1c0f9a-7e4b-4a3c-8b2d-6e5f4a3b2c1d",
        "tool_name": "Read",
        "tool_input": {"file_path": path},
    }
    if agent_id is not None:
        payload["agent_id"] = agent_id
    if agent_type is not None:
        payload["agent_type"] = agent_type
    return payload


def _main(path: str) -> dict:
    return _payload(path)


def _sub(agent_id: str, path: str, agent_type: str = "Explore") -> dict:
    return _payload(path, agent_id=agent_id, agent_type=agent_type)


def _run(monkeypatch, capsys, payload, db, extra_env=None):
    monkeypatch.setenv("SIDEGRAPH_DIR", str(db))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/work/repo")
    for key, value in (extra_env or {}).items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    hooks.pre_tool_use()
    return json.loads(capsys.readouterr().out)


def _nudged(out: dict) -> bool:
    return "hookSpecificOutput" in out


def _text(out: dict) -> str:
    return out["hookSpecificOutput"]["additionalContext"]


def _two_anchored_paths(db) -> None:
    _seed_anchored_decision(db, PATH_A, "Alpha gotcha")
    _seed_anchored_decision(db, PATH_B, "Beta gotcha")


def _nudge_keys(db) -> dict[str, str]:
    store = Store(db)
    try:
        rows = store._conn.execute("SELECT key, value FROM meta WHERE key LIKE 'pretool%'")
        return {row["key"]: row["value"] for row in rows}
    finally:
        store.close()


# -- T1-T3: one nudge of each form per agent ------------------------------------------------


def test_a_subagent_gets_its_own_path_specific_nudge_after_the_main_agent(
    tmp_path, monkeypatch, capsys
):
    """Red against a key per session: the main agent's nudge burned the one path-specific key
    and the subagent's first read returned ``{}``."""
    db = tmp_path / "db"
    _two_anchored_paths(db)

    main = _run(monkeypatch, capsys, _main(PATH_A), db)
    assert "Alpha gotcha" in _text(main)

    sub = _run(monkeypatch, capsys, _sub(A, PATH_B), db)
    assert _nudged(sub), "the subagent read an anchored file blind"
    assert "Beta gotcha" in _text(sub)

    # and neither agent is nudged twice
    assert _run(monkeypatch, capsys, _main(PATH_A), db) == {}
    assert _run(monkeypatch, capsys, _sub(A, PATH_B), db) == {}


def test_one_subagent_is_nudged_once(tmp_path, monkeypatch, capsys):
    db = tmp_path / "db"
    _two_anchored_paths(db)

    assert _nudged(_run(monkeypatch, capsys, _sub(A, PATH_A), db))
    assert _run(monkeypatch, capsys, _sub(A, PATH_A), db) == {}


def test_two_subagents_of_one_type_are_each_nudged_once(tmp_path, monkeypatch, capsys):
    """Red against a key per session, and against a key on ``agent_type``: A and B are both
    "Explore"."""
    db = tmp_path / "db"
    _two_anchored_paths(db)

    assert _nudged(_run(monkeypatch, capsys, _sub(A, PATH_A, "Explore"), db))
    assert _nudged(_run(monkeypatch, capsys, _sub(B, PATH_A, "Explore"), db))
    assert _run(monkeypatch, capsys, _sub(A, PATH_A, "Explore"), db) == {}
    assert _run(monkeypatch, capsys, _sub(B, PATH_A, "Explore"), db) == {}


# -- T4 / T4b: the main agent's key is ``-`` ---------------------------------------------------


def test_a_main_payload_without_agent_id_takes_the_dash_key(tmp_path, monkeypatch, capsys):
    """The main agent's key names the session and ``-`` where a subagent's names its id, so its
    prefix is not a prefix of any subagent's keys (the cap counts by prefix)."""
    db = tmp_path / "db"
    _two_anchored_paths(db)

    _run(monkeypatch, capsys, _main(PATH_A), db)

    assert set(_nudge_keys(db)) == {f"pretool_file:{SESSION}:-:{PATH_A}"}


def test_a_main_session_started_with_an_agent_is_still_the_main_agent(
    tmp_path, monkeypatch, capsys
):
    """``claude --agent reader`` carries ``agent_type`` and no ``agent_id``: it is the main
    agent, so it takes the ``-`` key. Keying on ``agent_type`` would fail this."""
    db = tmp_path / "db"
    _two_anchored_paths(db)

    out = _run(monkeypatch, capsys, _payload(PATH_A, agent_type="reader"), db)

    assert _nudged(out)
    assert set(_nudge_keys(db)) == {f"pretool_file:{SESSION}:-:{PATH_A}"}


def test_a_subagent_key_names_the_session_the_agent_and_the_file(tmp_path, monkeypatch, capsys):
    db = tmp_path / "db"
    _two_anchored_paths(db)

    _run(monkeypatch, capsys, _sub(A, PATH_A), db)

    (key,) = _nudge_keys(db)
    assert key == f"pretool_file:{SESSION}:{A}:{PATH_A}"
    # the value is the claim's timestamp, which is what lets SessionStart expire the key
    assert abs((datetime.now(UTC) - datetime.fromisoformat(_nudge_keys(db)[key])).seconds) < 60


def test_an_empty_or_non_string_agent_id_is_the_main_agent(tmp_path, monkeypatch, capsys):
    db = tmp_path / "db"
    _two_anchored_paths(db)

    for i, bad in enumerate(("", None, 7, ["x"])):
        payload = _main(PATH_A)
        payload["agent_id"] = bad
        payload["session_id"] = f"{SESSION}-{i}"
        payload["transcript_path"] = f"/work/repo/.claude/{SESSION}-{i}.jsonl"
        assert _nudged(_run(monkeypatch, capsys, payload, db))

    assert set(_nudge_keys(db)) == {f"pretool_file:{SESSION}-{i}:-:{PATH_A}" for i in range(4)}


# -- T5 / T7: touch rows carry the agent and sessions are still counted once ----------------


def test_a_subagent_touch_is_a_row_with_the_agent_under_the_parent_session(
    tmp_path, monkeypatch, capsys
):
    db = tmp_path / "db"
    Store(db).close()  # the hook records into an existing store; it never creates one
    _run(monkeypatch, capsys, _sub(A, "/work/repo/" + PATH_A), db)
    _run(monkeypatch, capsys, _main("/work/repo/" + PATH_B), db)

    store = Store(db)
    rows = store._conn.execute(
        "SELECT session_id, key, agent FROM retrieval_events WHERE kind = 'touch' ORDER BY id"
    ).fetchall()
    assert [(r["session_id"], r["key"], r["agent"]) for r in rows] == [
        (SESSION, PATH_A, A),
        (SESSION, PATH_B, None),
    ]


def test_stats_still_counts_the_agent_tree_as_one_session(tmp_path, monkeypatch, capsys):
    """Red against a session id that folds the agent in: each subagent would be a session."""
    db = tmp_path / "db"
    Store(db).close()  # the hook records into an existing store; it never creates one
    _run(monkeypatch, capsys, _main("/work/repo/" + PATH_A), db)
    _run(monkeypatch, capsys, _sub(A, "/work/repo/" + PATH_A), db)
    _run(monkeypatch, capsys, _sub(B, "/work/repo/" + PATH_B), db)

    report = build_report(db, None)

    assert report.activation.sessions_total == 1
    assert report.activation.sessions_touch_only == 1
    assert report.reach.files_touched == 2


# -- T8: SessionStart expires old nudge keys ------------------------------------------------


def _iso(days_ago: float) -> str:
    return (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()


def _run_session_start(monkeypatch, capsys, db) -> None:
    monkeypatch.setenv("SIDEGRAPH_DIR", str(db))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "next-session"})))
    hooks.session_start()
    capsys.readouterr()


def test_session_start_expires_stale_and_legacy_nudge_keys_and_keeps_the_rest(
    tmp_path, monkeypatch, capsys
):
    """Red against no prune at all, and against a ``LIKE`` match: ``_`` is a wildcard, so
    ``pretool_nudge:%`` also matches the guard key's ``pretoolXnudge:``."""
    db = tmp_path / "db"
    store = Store(db)
    for key, value in {
        "pretool_nudge:old-session": _iso(31),
        "pretool_nudge:legacy-session": "1",
        f"pretool_nudge_path:old-session:{A}": _iso(45),
        "pretool_nudge:fresh-session": _iso(1),
        f"pretool_nudge_path:fresh-session:{A}": _iso(2),
        "pretoolXnudge:other": _iso(90),
    }.items():
        store.set_meta(key, value)
    store.close()

    _run_session_start(monkeypatch, capsys, db)

    assert set(_nudge_keys(db)) == {
        "pretool_nudge:fresh-session",
        f"pretool_nudge_path:fresh-session:{A}",
        "pretoolXnudge:other",
    }


def test_session_start_expires_stale_per_file_keys_and_keeps_the_fresh_ones(
    tmp_path, monkeypatch, capsys
):
    """T9: the per-file keys are pruned with the same 30 days. Red against a prune list without
    the ``pretool_file:`` prefix (they would pile up, up to ten per agent). A key of the same
    shape under another prefix is not touched."""
    db = tmp_path / "db"
    store = Store(db)
    for key, value in {
        f"pretool_file:old-session:-:{PATH_A}": _iso(31),
        f"pretool_file:old-session:{A}:{PATH_B}": _iso(60),
        f"pretool_file:fresh-session:-:{PATH_A}": _iso(1),
        f"pretool_file:fresh-session:{A}:{PATH_B}": _iso(29),
        f"pretoolXfile:other:-:{PATH_A}": _iso(90),
    }.items():
        store.set_meta(key, value)
    store.close()

    _run_session_start(monkeypatch, capsys, db)

    assert set(_nudge_keys(db)) == {
        f"pretool_file:fresh-session:-:{PATH_A}",
        f"pretool_file:fresh-session:{A}:{PATH_B}",
        f"pretoolXfile:other:-:{PATH_A}",
    }


def test_a_failed_nudge_key_prune_does_not_cost_the_session_map(tmp_path, monkeypatch, capsys):
    """The map is SessionStart's one deliverable: a prune that raises must not take it down.
    Seeded with a decision so there is a map to lose; an empty store prints ``{}`` either way."""
    db = tmp_path / "db"
    _seed_anchored_decision(db, PATH_A, "Alpha gotcha")

    def boom(*args, **kwargs):
        raise RuntimeError("prune exploded")

    monkeypatch.setattr(Store, "prune_meta_prefixes", boom)
    monkeypatch.setenv("SIDEGRAPH_DIR", str(db))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "s"})))
    hooks.session_start()

    out = json.loads(capsys.readouterr().out)
    assert "hookSpecificOutput" in out, out
