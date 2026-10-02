"""A session launched in a subdirectory uses the repository's store, not a new empty one.

The Claude Code manifests ``cd "${CLAUDE_PROJECT_DIR}"`` and set ``SIDEGRAPH_DIR=.sidegraph``;
launched in ``R/sub``, both anchor the store to ``R/sub``. These tests drive the hooks and the
MCP server's store the way that launch does: ``CLAUDE_PROJECT_DIR=R/sub``, cwd ``R/sub``, and
the store only at ``R``. Resolution itself is covered in ``test_config_ancestor_lookup.py``.
see design/superpowers/specs/2026-10-01-subdirectory-launch-design.md
"""

from __future__ import annotations

import io
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

import sidegraph.host.hooks as hooks
import sidegraph.server as server
from sidegraph.schema import (
    AnchorBinding,
    Decision,
    DecisionKind,
    DecisionStatus,
    Descriptor,
    Entity,
    Provenance,
    Scope,
)
from sidegraph.store import Store

FIXTURE = Path(__file__).parent / "fixtures" / "mini_graph.json"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """``R`` with ``R/.git/`` and ``R/sub``; no store yet."""
    root = tmp_path / "R"
    (root / ".git").mkdir(parents=True)
    (root / "sub").mkdir()
    return root


def _remember(store_path: Path, title: str, file_path: str | None = None) -> None:
    """One accepted global gotcha in the store at ``store_path``; anchored to ``file_path``
    when one is given."""
    store = Store(store_path)
    try:
        d = store.add_decision(
            Decision(
                title=title,
                kind=DecisionKind.GOTCHA,
                status=DecisionStatus.ACCEPTED,
                context="c",
                choice="ch",
                scope=Scope.GLOBAL,
                valid_from=datetime.now(UTC),
                provenance=Provenance(source="manual"),
            )
        )
        if file_path is not None:
            e = store.upsert_entity(
                Entity(
                    canonical_name="a_py",
                    descriptor=Descriptor(name="a_py", file_path=file_path),
                )
            )
            store.add_binding(
                AnchorBinding(record_id=d.id, entity_id=e.entity_id, tier=2, status="live")
            )
    finally:
        store.close()


def _launch(monkeypatch, directory: Path, **env: str) -> None:
    """What the manifests do: cd to the launch directory, relative store."""
    monkeypatch.chdir(directory)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(directory))
    monkeypatch.setenv("SIDEGRAPH_DIR", ".sidegraph")
    for key, value in env.items():
        monkeypatch.setenv(key, value)


def _session_start(monkeypatch, capsys) -> str:
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    hooks.session_start()
    return json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]


def _pre_tool_use(monkeypatch, capsys, file_path: Path, session: str = "s1") -> dict:
    payload = {
        "session_id": session,
        "tool_name": "Read",
        "tool_input": {"file_path": str(file_path)},
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    hooks.pre_tool_use()
    return json.loads(capsys.readouterr().out)


def _touch_keys(store_path: Path, session: str = "s1") -> list[str | None]:
    store = Store(store_path)
    try:
        return [e["key"] for e in store.retrieval_events(session)]
    finally:
        store.close()


# -- the MCP server ----------------------------------------------------------------------


def test_t3_server_store_from_a_subdirectory_is_the_repository_store(repo, monkeypatch):
    _remember(repo / ".sidegraph", "Retry budget lives in the gateway")
    monkeypatch.chdir(repo / "sub")
    monkeypatch.setenv("SIDEGRAPH_DIR", ".sidegraph")
    monkeypatch.setattr(server, "_store", None)
    store = server._get_store()
    try:
        assert Path(store.path).resolve() == (repo / ".sidegraph").resolve()
    finally:
        store.close()
    assert not (repo / "sub" / ".sidegraph").exists()


# -- SessionStart ------------------------------------------------------------------------


def test_t9_session_start_reads_the_repository_store_and_creates_none(repo, monkeypatch, capsys):
    _remember(repo / ".sidegraph", "Retry budget lives in the gateway")
    _launch(monkeypatch, repo / "sub")
    ctx = _session_start(monkeypatch, capsys)
    assert "Retry budget lives in the gateway" in ctx
    assert not (repo / "sub" / ".sidegraph").exists()


def test_t11_the_graph_follows_a_nested_store(repo, monkeypatch, capsys):
    """The graph lives beside the store's own project (``R/.config``), not under
    ``CLAUDE_PROJECT_DIR``."""
    nested = repo / ".config"
    (nested / "graphify-out").mkdir(parents=True)
    shutil.copy(FIXTURE, nested / "graphify-out" / "graph.json")
    Store(nested / "sidegraph").close()
    _launch(
        monkeypatch,
        repo,
        SIDEGRAPH_DIR=".config/sidegraph",
        SIDEGRAPH_GRAPH="graphify-out/graph.json",
    )
    assert "## Communities" in _session_start(monkeypatch, capsys)


STRAY_LINE = "uses an empty store"


def test_t13_an_empty_store_beside_a_populated_one_is_named(repo, monkeypatch, capsys):
    _remember(repo / ".sidegraph", "Retry budget lives in the gateway")
    Store(repo / "sub" / ".sidegraph").close()  # what an older Sidegraph created
    _launch(monkeypatch, repo / "sub")
    ctx = _session_start(monkeypatch, capsys)
    line = next(ln for ln in ctx.splitlines() if STRAY_LINE in ln)
    assert str(repo / "sub" / ".sidegraph") in line
    assert str(repo / ".sidegraph") in line
    assert "remove" in line
    assert (repo / "sub" / ".sidegraph").exists()  # reported, never removed


def test_t14_a_populated_store_at_the_anchor_is_not_called_empty(repo, monkeypatch, capsys):
    """A real per-package store: red against nothing, guards the emptiness check (M8)."""
    _remember(repo / ".sidegraph", "Retry budget lives in the gateway")
    _remember(repo / "sub" / ".sidegraph", "Package-level rule")
    _launch(monkeypatch, repo / "sub")
    ctx = _session_start(monkeypatch, capsys)
    assert STRAY_LINE not in ctx
    assert "Package-level rule" in ctx


def test_an_empty_store_with_nothing_above_is_not_called_stray(repo, monkeypatch, capsys):
    Store(repo / "sub" / ".sidegraph").close()
    _launch(monkeypatch, repo / "sub")
    assert STRAY_LINE not in _session_start(monkeypatch, capsys)


def test_the_stray_line_is_silent_for_a_legacy_sidegraph_db(repo, monkeypatch, capsys):
    """``SIDEGRAPH_DB`` is never looked up, so removing the store would not redirect it."""
    _remember(repo / ".sidegraph", "Retry budget lives in the gateway")
    Store(repo / "sub" / ".sidegraph").close()
    _launch(monkeypatch, repo / "sub")
    monkeypatch.delenv("SIDEGRAPH_DIR")
    monkeypatch.setenv("SIDEGRAPH_DB", ".sidegraph")
    assert STRAY_LINE not in _session_start(monkeypatch, capsys)


# -- PreToolUse: touch rows and the path-specific nudge ----------------------------------


def test_t10_a_touch_in_a_subdirectory_launch_is_repo_relative(repo, monkeypatch, capsys):
    Store(repo / ".sidegraph").close()
    target = repo / "sub" / "a.py"
    target.write_text("x = 1\n")
    _launch(monkeypatch, repo / "sub")
    _pre_tool_use(monkeypatch, capsys, target)
    assert _touch_keys(repo / ".sidegraph") == ["sub/a.py"]
    assert not (repo / "sub" / ".sidegraph").exists()


def test_t10b_the_nudge_names_the_repo_relative_anchor(repo, monkeypatch, capsys):
    _remember(repo / ".sidegraph", "Retry budget lives in the gateway", file_path="sub/a.py")
    target = repo / "sub" / "a.py"
    target.write_text("x = 1\n")
    _launch(monkeypatch, repo / "sub")
    out = _pre_tool_use(monkeypatch, capsys, target)
    text = out["hookSpecificOutput"]["additionalContext"]
    assert "Retry budget lives in the gateway" in text
    assert "anchored to sub/a.py" in text


def test_t10c_a_nested_store_keeps_touches_repo_relative(repo, monkeypatch, capsys):
    Store(repo / ".config" / "sidegraph").close()
    (repo / "src").mkdir()
    target = repo / "src" / "a.py"
    target.write_text("x = 1\n")
    _launch(monkeypatch, repo, SIDEGRAPH_DIR=".config/sidegraph")
    _pre_tool_use(monkeypatch, capsys, target)
    assert _touch_keys(repo / ".config" / "sidegraph") == ["src/a.py"]


# -- Stop --------------------------------------------------------------------------------


def test_t12_stop_marks_the_capture_ledger_in_the_repository_store(repo, monkeypatch, capsys):
    Store(repo / ".sidegraph").close()
    transcript = repo.parent / "s1.jsonl"  # the ledger key is the transcript's name
    lines: list[dict] = []
    for i in range(hooks._MIN_USER_PROMPTS):
        lines.append({"type": "user", "message": {"role": "user", "content": f"prompt {i}"}})
        lines.append(
            {
                "type": "assistant",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
            }
        )
    transcript.write_text("".join(json.dumps(line) + "\n" for line in lines))
    _launch(monkeypatch, repo / "sub")
    payload = {"session_id": "s1", "stop_hook_active": False, "transcript_path": str(transcript)}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    hooks.stop()
    assert json.loads(capsys.readouterr().out)["decision"] == "block"
    store = Store(repo / ".sidegraph")
    try:
        assert store.was_captured("s1")
    finally:
        store.close()
    assert not (repo / "sub" / ".sidegraph").exists()


def test_stop_ledger_peek_looks_in_the_repository_store(repo, monkeypatch, capsys):
    """The read-only peek at the capture ledger takes the same lookup as the write path: a
    session already captured in ``R/.sidegraph`` exits before the transcript is parsed (M11:
    the peek resolves ``R/sub/.sidegraph``, finds no index and falls through to the parse)."""
    store = Store(repo / ".sidegraph")
    store.mark_captured("s1")
    store.close()
    _launch(monkeypatch, repo / "sub")
    parsed: list[object] = []

    def spy(path):
        parsed.append(path)
        raise AssertionError("the transcript was parsed")

    monkeypatch.setattr(hooks, "_transcript_stats", spy)
    payload = {
        "session_id": "s1",
        "stop_hook_active": False,
        "transcript_path": str(repo.parent / "s1.jsonl"),
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    hooks.stop()
    assert json.loads(capsys.readouterr().out) == {}
    assert parsed == []
    assert not (repo / "sub" / ".sidegraph").exists()
