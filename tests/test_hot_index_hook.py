"""The PreToolUse hook on top of ``HotIndex`` (T5, hook half; D3).

Where the handle refuses the index, the hook does nothing: it prints ``{}`` and writes
nothing anywhere, including through a symlink. And a tool call never creates a store.

see design/superpowers/specs/2026-10-03-hot-path-light-index-design.md (D2, D3)
"""

from __future__ import annotations

import io
import json
import sqlite3
import time

import pytest

import sidegraph.host.hooks as hooks
from sidegraph.hot_index import HotIndex
from tests.test_hot_index import _REFUSED, FILE_A, _broken_project, _fixture_store, _snapshot


@pytest.mark.parametrize("how", sorted(_REFUSED))
def test_the_hook_prints_nothing_and_writes_nothing_on_a_refused_index(
    tmp_path, monkeypatch, capsys, how
):
    root, store_dir, elsewhere = _broken_project(tmp_path, how)
    monkeypatch.setenv("SIDEGRAPH_DIR", str(store_dir))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(root))
    before = _snapshot(root, elsewhere)
    outs = []
    for tool, session in (("Edit", "e1"), ("Read", "r1")):
        payload = {
            "session_id": session,
            "tool_name": tool,
            "tool_input": {"file_path": str(root / FILE_A)},
        }
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
        hooks.pre_tool_use()
        outs.append(json.loads(capsys.readouterr().out))
    assert outs == [{}, {}]
    assert _snapshot(root, elsewhere) == before


def test_a_project_without_a_store_gets_no_store_and_no_stderr(tmp_path, monkeypatch, capsys):
    """The declared exception of D3: a Read or Edit used to create ``.sidegraph/`` (and print
    "creating new store" when delivery ran with telemetry off). Neither is intended."""
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / FILE_A).write_text("x = 1\n")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(root))
    monkeypatch.chdir(root)
    for tool, session in (("Edit", "e1"), ("Read", "r1")):
        payload = {
            "session_id": session,
            "tool_name": tool,
            "tool_input": {"file_path": str(root / FILE_A)},
        }
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
        hooks.pre_tool_use()
        captured = capsys.readouterr()
        assert json.loads(captured.out) == {}
        assert captured.err == ""
    assert not (root / ".sidegraph").exists()
    monkeypatch.setenv("SIDEGRAPH_TELEMETRY", "off")
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(
            json.dumps(
                {
                    "session_id": "r2",
                    "tool_name": "Read",
                    "tool_input": {"file_path": str(root / FILE_A)},
                }
            )
        ),
    )
    hooks.pre_tool_use()
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {}
    assert captured.err == ""
    assert not (root / ".sidegraph").exists()


def test_a_locked_index_costs_the_hook_one_busy_wait_not_two(tmp_path, monkeypatch, capsys):
    """Another connection holds the write lock (a rebuild, a long write). Red against a hook
    that keeps going after its touch failed: the touch insert waits out the 1 s busy timeout,
    and then the file claim waits it out again (a Read took 2.3 s). Once a write on the shared
    handle has failed on the lock, delivery gets no index, prints ``{}`` and never tries its own
    write."""
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / FILE_A).write_text("x = 1\n")
    store_dir = root / ".sidegraph"
    _fixture_store(store_dir).close()
    monkeypatch.setenv("SIDEGRAPH_DIR", str(store_dir))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(root))
    claims: list[str] = []
    real_claim = HotIndex.claim_file

    def spy(self, prefix, rel_path, value, cap):
        claims.append(prefix + rel_path)
        return real_claim(self, prefix, rel_path, value, cap)

    monkeypatch.setattr(HotIndex, "claim_file", spy)
    payload = {
        "session_id": "locked-1",
        "tool_name": "Read",
        "tool_input": {"file_path": str(root / FILE_A)},
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))

    holder = sqlite3.connect(store_dir / "index.db", isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")  # a RESERVED lock: readers pass, writers wait
    try:
        started = time.perf_counter()
        hooks.pre_tool_use()
        elapsed = time.perf_counter() - started
    finally:
        holder.execute("ROLLBACK")
        holder.close()

    assert json.loads(capsys.readouterr().out) == {}
    assert elapsed < 1.8, f"two busy waits: {elapsed:.2f}s"
    assert claims == []
    conn = sqlite3.connect(store_dir / "index.db")
    try:
        touches = conn.execute("SELECT COUNT(*) FROM retrieval_events WHERE kind='touch'")
        keys = conn.execute("SELECT COUNT(*) FROM meta WHERE key LIKE 'pretool_file%'")
        assert (touches.fetchone()[0], keys.fetchone()[0]) == (0, 0)
    finally:
        conn.close()


@pytest.mark.parametrize("index_exists", [True, False], ids=["usable", "refused"])
def test_one_read_opens_the_index_at_most_once(tmp_path, monkeypatch, capsys, index_exists):
    """D3: the touch and the nudge of one Read share a single open, and a refusal is remembered
    as well, so a store the hook cannot use is looked at once. Red against an opener that does
    not remember (M14): each step opens for itself."""
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / FILE_A).write_text("x = 1\n")
    store_dir = root / ".sidegraph"
    _fixture_store(store_dir).close()
    if not index_exists:
        (store_dir / "index.db").unlink()
    monkeypatch.setenv("SIDEGRAPH_DIR", str(store_dir))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(root))
    opens: list[str] = []
    real_open = HotIndex.open

    def counting(store_path):
        opens.append(str(store_path))
        return real_open(store_path)

    monkeypatch.setattr(HotIndex, "open", staticmethod(counting))
    payload = {
        "session_id": "once-1",
        "tool_name": "Read",
        "tool_input": {"file_path": str(root / FILE_A)},
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    hooks.pre_tool_use()
    out = json.loads(capsys.readouterr().out)
    assert ("hookSpecificOutput" in out) is index_exists  # delivery ran (or was refused)
    if index_exists:  # the touch ran too, so both steps asked for the index
        conn = sqlite3.connect(store_dir / "index.db")
        try:
            touches = conn.execute("SELECT key FROM retrieval_events WHERE kind='touch'")
            assert touches.fetchall() == [(FILE_A,)]
        finally:
            conn.close()
    assert len(opens) == 1, opens
