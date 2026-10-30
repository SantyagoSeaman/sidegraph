"""``pre_tool_use`` on Grok Build: its tool names and its session-relative paths (T1-T6, T8, T9).

Grok runs the Claude plugin's hooks but keeps its own tool names in ``tool_name``
(``run_terminal_command``, ``read_file``, ``grep``, ``search_replace``, ``spawn_subagent``) and
sends a path as the model wrote it, relative to the session directory in ``cwd``. The payloads
here have the shape probe P1 captured on Grok 1.0.70: camelCase keys beside their snake_case
aliases, a PascalCase ``hook_event_name``, a nested ``cwd`` and a UUID session id. ``read_file``
names its path ``target_file``. The hook rewrites a copy of the payload to Claude's names and
absolute paths before it records or delivers; a Claude name is never touched.

see design/superpowers/specs/2026-10-29-grok-pretooluse-if-design.md (D-G3, T1-T9)
"""

from __future__ import annotations

import io
import json
import uuid
from pathlib import Path

import pytest

import sidegraph.host.hooks as hooks
from sidegraph.store import Store
from tests.test_host_pretool_records import Project, _text


@pytest.fixture
def project(tmp_path: Path):
    p = Project(tmp_path / "repo")
    yield p
    p.close()


def _grok(project: Project, tool: str, session: str, cwd: Path, **tool_input) -> dict:
    """The stdin Grok writes for a PreToolUse, in the shape P1 captured."""
    return {
        "hookEventName": "pre_tool_use",
        "sessionId": session,
        "cwd": str(cwd),
        "workspaceRoot": str(project.root),
        "toolName": tool,
        "toolInput": tool_input,
        "hook_event_name": "PreToolUse",
        "session_id": session,
        "tool_name": tool,
        "tool_input": tool_input,
    }


def _call(monkeypatch, capsys, project: Project, payload: dict) -> dict:
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project.root))
    monkeypatch.setenv("SIDEGRAPH_DIR", str(project.store_dir))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    hooks.pre_tool_use()
    return json.loads(capsys.readouterr().out)


def _touches(project: Project, session: str) -> list[tuple[str | None, str | None]]:
    """``(key, tool)`` of every touch row the session wrote (delivery rows carry a record id)."""
    store = Store(project.store_dir)
    try:
        return [
            (e["key"], e["detail"])
            for e in store.retrieval_events(session)
            if e["detail"] in hooks._TOUCH_TOOLS
        ]
    finally:
        store.close()


def _nested(project: Project, rel: str, title: str) -> tuple[str, Path]:
    """A record anchored to ``rel``; returns the rel path and the session directory above it
    (the file's own directory), the way a Grok session launched in ``src/sidegraph`` sees it."""
    project.record(rel, title)
    return rel, (project.root / rel).parent


def test_t1_a_grok_shell_read_delivers_what_the_bash_payload_does(project, monkeypatch, capsys):
    """Red against unfixed code: ``run_terminal_command`` is not in ``_POINT_OF_READ_TOOLS``, so
    the hook printed ``{}`` for a ``sed`` of an anchored file."""
    rel, cwd = _nested(project, "src/sidegraph/schema.py", "Models live in one module")
    bash = {
        "session_id": str(uuid.uuid4()),
        "cwd": str(project.root),
        "tool_name": "Bash",
        "tool_input": {"command": f"sed -n 1p {rel}"},
    }
    expected = _text(_call(monkeypatch, capsys, project, bash))

    payload = _grok(
        project, "run_terminal_command", str(uuid.uuid4()), cwd, command="sed -n 1p schema.py"
    )
    assert _text(_call(monkeypatch, capsys, project, payload)) == expected


def test_t2_a_grok_read_file_delivers_and_records_a_read_touch(project, monkeypatch, capsys):
    """Red against unfixed code: the name is rejected, and ``_touch_path`` knows no
    ``target_file``. The path is relative to the nested session directory."""
    rel, cwd = _nested(project, "src/sidegraph/schema.py", "Models live in one module")
    session = str(uuid.uuid4())
    out = _call(
        monkeypatch,
        capsys,
        project,
        _grok(project, "read_file", session, cwd, target_file="schema.py"),
    )
    assert f"Recorded for {rel}" in _text(out)
    assert _touches(project, session) == [(rel, "Read")]


def test_t3_a_grok_grep_and_search_replace_deliver_from_a_nested_cwd(project, monkeypatch, capsys):
    """Red against unfixed code (names rejected). The touch labels are part of the contract:
    Grok's one edit tool is recorded as ``Edit``, never split into ``Write``."""
    rel_a, cwd = _nested(project, "src/sidegraph/store.py", "Store is append-only")
    rel_b, _ = _nested(project, "src/sidegraph/retrieval.py", "Mistakes rank first")

    grep_session = str(uuid.uuid4())
    grep = _grok(project, "grep", grep_session, cwd, pattern="class", path="store.py")
    assert f"Recorded for {rel_a}" in _text(_call(monkeypatch, capsys, project, grep))
    assert _touches(project, grep_session) == [(rel_a, "Grep")]

    edit_session = str(uuid.uuid4())
    edit = _grok(
        project,
        "search_replace",
        edit_session,
        cwd,
        file_path="retrieval.py",
        old_string="a",
        new_string="b",
    )
    assert f"Recorded for {rel_b}" in _text(_call(monkeypatch, capsys, project, edit))
    assert _touches(project, edit_session) == [(rel_b, "Edit")]


def test_t3b_a_grok_write_delivers_and_records_a_write_touch(project, monkeypatch, capsys):
    """Red against the branch before ``write`` was mapped: Grok's always-on ``write`` tool
    (``file_path`` + ``content``, absolute or session-relative) reaches the ``Write`` matcher and
    was left unmapped. It is recorded as ``Write``, not ``Edit``."""
    rel_a, cwd = _nested(project, "src/sidegraph/store.py", "Store is append-only")
    rel_b, _ = _nested(project, "src/sidegraph/retrieval.py", "Mistakes rank first")

    relative_session = str(uuid.uuid4())
    relative = _grok(project, "write", relative_session, cwd, file_path="store.py", content="x")
    assert f"Recorded for {rel_a}" in _text(_call(monkeypatch, capsys, project, relative))
    assert _touches(project, relative_session) == [(rel_a, "Write")]

    absolute_session = str(uuid.uuid4())
    absolute = _grok(
        project, "write", absolute_session, cwd, file_path=str(project.root / rel_b), content="x"
    )
    assert f"Recorded for {rel_b}" in _text(_call(monkeypatch, capsys, project, absolute))
    assert _touches(project, absolute_session) == [(rel_b, "Write")]


def test_t4_a_grok_subagent_spawn_gets_the_brief_appended(project, monkeypatch, capsys):
    """Red against unfixed code: ``spawn_subagent`` is not in ``_AGENT_TOOLS``. The brief rides
    in ``updatedInput.prompt``, the call's other fields come back as sent, and no
    ``additionalContext`` is set."""
    project.record("src/payments.py", "Never retry a charge")
    prompt = "Fix the refund path in src/payments.py."
    payload = _grok(
        project,
        "spawn_subagent",
        str(uuid.uuid4()),
        project.root,
        prompt=prompt,
        description="refund",
    )
    out = _call(monkeypatch, capsys, project, payload)
    hso = out["hookSpecificOutput"]
    assert "additionalContext" not in hso
    assert "permissionDecision" not in hso
    assert hso["updatedInput"]["description"] == "refund"
    assert hso["updatedInput"]["prompt"].startswith(prompt + "\n\n")
    assert "Never retry a charge" in hso["updatedInput"]["prompt"]


def test_t5_claude_tool_names_keep_their_input(project, monkeypatch, capsys):
    """Guard, red against nothing: a Claude name is never remapped, so an ``Edit`` and a ``Read``
    with an absolute path behave as before. A failure means the map touched a Claude name."""
    project.record("src/a.py", "A")
    project.record("src/b.py", "B")
    for tool, name in (("Read", "a"), ("Edit", "b")):
        session = str(uuid.uuid4())
        path = str(project.root / "src" / f"{name}.py")
        out = _call(
            monkeypatch,
            capsys,
            project,
            {
                "session_id": session,
                "cwd": str(project.root),
                "tool_name": tool,
                "tool_input": {"file_path": path},
            },
        )
        assert f"Recorded for src/{name}.py" in _text(out)
        assert _touches(project, session) == [(f"src/{name}.py", tool)]


def test_t6_unknown_names_print_nothing_and_record_nothing(project, monkeypatch, capsys):
    """Guard, red against a too-wide map that sends every unknown name down the Read path. On
    unfixed code it already passes. ``hashline_edit`` is a real Grok tool whose input keys are
    unprobed, so it stays unmapped."""
    rel, cwd = _nested(project, "src/sidegraph/schema.py", "Models live in one module")
    for tool, tool_input in (
        ("hashline_edit", {"file_path": "schema.py"}),
        ("list_dir", {"path": "schema.py"}),
        ("made_up_tool", {"target_file": "schema.py", "file_path": rel}),
    ):
        session = str(uuid.uuid4())
        assert (
            _call(monkeypatch, capsys, project, _grok(project, tool, session, cwd, **tool_input))
            == {}
        )
        assert _touches(project, session) == [], tool


def test_t8_a_pathless_grok_grep_names_no_file(project, monkeypatch, capsys):
    """Guard, red against a map that invents a path from ``cwd``: the rewritten copy of a search
    without ``path`` has no ``path`` and no ``file_path`` key, and end to end the hook prints
    nothing and records nothing. (The end-to-end half alone is green for an invented directory,
    which names no file.)"""
    _, cwd = _nested(project, "src/sidegraph/schema.py", "Models live in one module")
    session = str(uuid.uuid4())
    payload = _grok(project, "grep", session, cwd, pattern="def pre_tool_use")
    mapped = hooks._claude_shaped(payload)
    assert mapped["tool_name"] == "Grep"
    assert "path" not in mapped["tool_input"] and "file_path" not in mapped["tool_input"]
    assert _call(monkeypatch, capsys, project, payload) == {}
    assert _touches(project, session) == []


def test_t9_a_claude_read_keeps_resolving_a_relative_path_from_the_store_root(
    project, monkeypatch, capsys
):
    """Guard, red against a path rule applied to Claude names: ``Read`` of ``schema.py`` with a
    nested ``cwd`` still means the file at the store root, not the one beside the session."""
    project.record("schema.py", "Root schema")
    _, cwd = _nested(project, "src/sidegraph/schema.py", "Nested schema")
    session = str(uuid.uuid4())
    payload = {
        "session_id": session,
        "cwd": str(cwd),
        "tool_name": "Read",
        "tool_input": {"file_path": "schema.py"},
    }
    text = _text(_call(monkeypatch, capsys, project, payload))
    assert "Recorded for schema.py" in text
    assert "Root schema" in text and "Nested schema" not in text
    assert _touches(project, session) == [("schema.py", "Read")]


# --- Session identity: Grok names every transcript `updates.jsonl` -------------------------


def _grok_transcript(home: Path, session: str) -> str:
    """Where Grok 1.0.70 writes a session's transcript: one directory per session id, and
    the file inside always named ``updates.jsonl`` (probe P1)."""
    return str(home / "sessions" / "%2Frepo" / session / "updates.jsonl")


def test_each_grok_session_gets_its_own_key(tmp_path):
    """Red against keying by the transcript stem alone: every Grok session would be
    ``updates``, so one claim per file served every Grok session in the repository."""
    first, second = str(uuid.uuid4()), str(uuid.uuid4())
    keys = [
        hooks._session_identity({"session_id": s, "transcript_path": _grok_transcript(tmp_path, s)})
        for s in (first, second)
    ]
    assert keys == [(first, None), (second, None)]


def test_claude_and_codex_keys_are_unchanged(tmp_path):
    """Red against nothing: a guard that the Grok rule needs the parent directory to BE the
    session id. A Claude transcript sits in its project directory and is named after the
    session; a Codex rollout sits in dated directories under an umbrella session id; a parent
    that merely looks like an id but differs keeps the stem."""
    claude = str(uuid.uuid4())
    assert hooks._session_identity(
        {"session_id": claude, "transcript_path": f"/h/.claude/projects/-repo/{claude}.jsonl"}
    ) == (claude, None)
    assert hooks._session_identity(
        {
            "session_id": "umbrella-1",
            "transcript_path": "/h/.codex/sessions/2026/10/29/rollout-2026-10-29-thread-7.jsonl",
        }
    ) == ("rollout-2026-10-29-thread-7", "umbrella-1")
    other = str(uuid.uuid4())
    assert hooks._session_identity(
        {"session_id": claude, "transcript_path": f"/h/x/{other}/updates.jsonl"}
    ) == ("updates", claude)


def test_two_grok_sessions_each_get_a_files_records(project, monkeypatch, capsys, tmp_path):
    """Red against keying by the stem: the second session's read of the same file found the
    first session's claim under ``updates`` and printed nothing (seen live on Grok Build 1.0.70)."""
    rel, cwd = _nested(project, "src/sidegraph/schema.py", "Models live in one module")
    for _ in range(2):
        session = str(uuid.uuid4())
        payload = _grok(project, "read_file", session, cwd, target_file="schema.py")
        payload["transcript_path"] = payload["transcriptPath"] = _grok_transcript(tmp_path, session)
        out = _call(monkeypatch, capsys, project, payload)
        assert f"Recorded for {rel}" in _text(out)
