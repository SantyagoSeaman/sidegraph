"""The MCP server refreshes its store on every tool call (T6).

The server keeps one ``Store`` for the life of the process, and a ``Store`` checks the
canonical files only when it is constructed. Until PreToolUse stopped constructing one, the
hook's open was what kept a long-lived server current: a ``git pull`` mid-session reached
``get_task_context`` because the next Read happened to rebuild the index. The server now owns
that, through fastmcp middleware so that no tool can skip it.

The tests go through ``fastmcp.Client``: a direct call to a tool function bypasses middleware.

see design/superpowers/specs/2026-10-03-hot-path-light-index-design.md (D4)
"""

from __future__ import annotations

import asyncio
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import fastmcp
import pytest

import sidegraph.server as server_module
from sidegraph.schema import AnchorBinding, Decision, DecisionKind, DecisionStatus, Provenance
from sidegraph.store import Store

GRAPH = Path(__file__).parent / "fixtures" / "mini_graph.json"
ANCHOR = {"name": "Trader", "file_path": "trader/exec.py"}


def _call(tool: str, arguments: dict):
    async def _run():
        async with fastmcp.Client(server_module.mcp) as client:
            return await client.call_tool(tool, arguments, raise_on_error=False)

    return asyncio.run(_run())


def _pull_a_record(local: Path, remote: Path) -> None:
    """A teammate's gotcha on the same file, written into a copy of the store and then
    "pulled": its new canonical files copied beside the server's own."""
    shutil.copytree(local, remote, ignore=shutil.ignore_patterns("index.db*"))
    teammate = Store(remote)
    decision = teammate.add_decision(
        Decision(
            title="PULLED never call bar",
            kind=DecisionKind.GOTCHA,
            status=DecisionStatus.ACCEPTED,
            context="c",
            choice="call it never",
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual"),
        )
    )
    trader = next(e for e in teammate.iter_concrete_entities() if e.descriptor.name == "Trader")
    teammate.add_binding(
        AnchorBinding(record_id=decision.id, entity_id=trader.entity_id, tier=2, status="live")
    )
    teammate.close()
    pulled = 0
    for src in remote.rglob("*.json"):
        dst = local / src.relative_to(remote)
        if not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            pulled += 1
    assert pulled >= 2  # the decision file and its binding


def test_a_record_pulled_onto_disk_reaches_the_next_get_task_context(tmp_path, monkeypatch):
    """Red against a server that never re-opens its store: the pulled record stays invisible
    until the process restarts. No hook runs between the two calls."""
    monkeypatch.setenv("SIDEGRAPH_GRAPH", str(GRAPH))
    local = tmp_path / "local" / ".sidegraph"
    monkeypatch.setenv("SIDEGRAPH_DIR", str(local))
    monkeypatch.setattr(server_module, "_store", None)

    added = _call(
        "add_decision",
        {
            "title": "FIRST never call foo twice",
            "kind": "gotcha",
            "context": "c",
            "choice": "call it once",
            "anchors": [ANCHOR],
        },
    )
    assert not added.is_error, added
    before = _call("get_task_context", {"files": ["trader/exec.py"]})
    assert "FIRST never call foo twice" in before.content[0].text
    assert "PULLED never call bar" not in before.content[0].text
    assert server_module._store is not None  # the server's store, open since the first call

    _pull_a_record(local, tmp_path / "remote" / ".sidegraph")

    after = _call("get_task_context", {"files": ["trader/exec.py"]})
    assert not after.is_error, after
    text = after.content[0].text
    assert "PULLED never call bar" in text
    assert "FIRST never call foo twice" in text  # a reload does not lose the local record


def test_every_tool_call_refreshes_the_store_first(tmp_path, monkeypatch):
    """The middleware covers every registered tool: one refresh per call, whatever the tool
    and whatever its arguments (an invalid call is refused after the middleware ran)."""
    monkeypatch.setenv("SIDEGRAPH_GRAPH", str(tmp_path / "no-graph.json"))
    monkeypatch.setenv("SIDEGRAPH_DIR", str(tmp_path / ".sidegraph"))
    store = Store(tmp_path / ".sidegraph")
    monkeypatch.setattr(server_module, "_store", store)
    calls: list[str] = []
    real = Store.refresh_if_stale

    def counting(self):
        calls.append("refresh")
        return real(self)

    monkeypatch.setattr(Store, "refresh_if_stale", counting)

    async def _run():
        async with fastmcp.Client(server_module.mcp) as client:
            names = sorted(t.name for t in await client.list_tools())
            served = {}
            for name in names:
                before = len(calls)
                await client.call_tool(name, {}, raise_on_error=False)
                served[name] = len(calls) - before
            return names, served

    names, served = asyncio.run(_run())
    assert len(names) >= 20
    assert {n: c for n, c in served.items() if c != 1} == {}, served


def _open_a_server_store(tmp_path: Path, monkeypatch) -> Path:
    """A server whose store is already open and holds one record on ``trader/exec.py``."""
    monkeypatch.setenv("SIDEGRAPH_GRAPH", str(GRAPH))
    local = tmp_path / "local" / ".sidegraph"
    monkeypatch.setenv("SIDEGRAPH_DIR", str(local))
    monkeypatch.setattr(server_module, "_store", None)
    added = _call(
        "add_decision",
        {
            "title": "FIRST never call foo twice",
            "kind": "gotcha",
            "context": "c",
            "choice": "call it once",
            "anchors": [ANCHOR],
        },
    )
    assert not added.is_error, added
    return local


def test_a_vanishing_record_file_does_not_fail_the_tool_call(tmp_path, monkeypatch, capsys):
    """A git checkout, pull or compact creates and deletes record files while the digest walk
    lists them, so the walk can raise ``FileNotFoundError``. Red against a middleware that lets
    it out: the tool call fails (73 of 80 ``list_domains`` calls did under such a race). The
    call must answer from the previous index, say so once on stderr, and the pulled record
    must show up on the next call, once the walk succeeds."""
    local = _open_a_server_store(tmp_path, monkeypatch)
    _pull_a_record(local, tmp_path / "remote" / ".sidegraph")
    capsys.readouterr()

    real = Store._compute_canonical_digest
    walks = {"raised": 0}

    def vanishing(self):
        if not walks["raised"]:
            walks["raised"] += 1
            raise FileNotFoundError(2, "No such file or directory", "decisions/vanished.json")
        return real(self)

    monkeypatch.setattr(Store, "_compute_canonical_digest", vanishing)

    during = _call("get_task_context", {"files": ["trader/exec.py"]})
    assert walks["raised"] == 1
    assert not during.is_error, during
    assert "FIRST never call foo twice" in during.content[0].text
    assert "PULLED never call bar" not in during.content[0].text  # the previous index
    stderr = capsys.readouterr().err.splitlines()
    assert len([line for line in stderr if "vanished.json" in line]) == 1, stderr

    after = _call("get_task_context", {"files": ["trader/exec.py"]})
    assert not after.is_error, after
    assert "PULLED never call bar" in after.content[0].text


@pytest.mark.parametrize(
    "error",
    [
        ValueError("store schema_version '0.0.1' != code; use a fresh store"),
        sqlite3.OperationalError("database is locked"),
    ],
    ids=["schema-mismatch", "sqlite"],
)
def test_only_a_filesystem_error_is_answered_from_the_previous_index(tmp_path, monkeypatch, error):
    """The narrow catch: a schema mismatch or an index failure is not a race to ride out, so
    the tool call still fails loudly instead of answering from an index nobody verified."""
    _open_a_server_store(tmp_path, monkeypatch)

    def failing(self):
        raise error

    monkeypatch.setattr(Store, "_compute_canonical_digest", failing)
    result = _call("get_task_context", {"files": ["trader/exec.py"]})
    assert result.is_error
