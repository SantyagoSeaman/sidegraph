"""Every MCP tool carries honest annotations, and a host's auto-reviewer can read them.

A host with an auto-reviewer (Codex) reviews an MCP call unless the tool is annotated read-only,
or non-destructive and closed-world. The two sets in ``server.py`` say what each tool does:
no tool leaves the machine (``openWorldHint`` false), no tool deletes a record
(``destructiveHint`` false), and ``readOnlyHint`` is true only for a tool that never changes a
tracked file. The honesty half of that claim is the moved-symbol test in
``tests/test_server_borrowed_graph.py``.
see design/superpowers/specs/2026-10-04-tool-annotations-and-argument-names-design.md (D1, T1)
"""

from __future__ import annotations

import asyncio

import fastmcp

from sidegraph import server

READ_ONLY = {
    "list_facts",
    "find_entity",
    "get_entity_history",
    "list_proposed",
    "list_domains",
    "list_domain_candidates",
    "verify_store",
}
# Tools that run the lazy sync, which can adopt a moved symbol into a tracked entity file.
LOCAL_WRITE_SYNC = {
    "retrieve_decisions",
    "get_task_context",
    "query_structure",
    "query_decisions",
    "drill_down",
}
LOCAL_WRITE = {
    "add_decision",
    "supersede_decision",
    "add_fact",
    "supersede_fact",
    "propose_decisions",
    "ratify",
    "ratify_decisions",
    "add_domain",
    "supersede_domain",
    "propose_domains",
    "sync_anchors",
    "add_anchors",
}


def _listed_tools() -> dict:
    async def run():
        async with fastmcp.Client(server.mcp) as client:
            return {t.name: t for t in await client.list_tools()}

    return asyncio.run(run())


def test_t1_every_tool_is_annotated_closed_world_and_non_destructive():
    """Red today: no tool has annotations."""
    tools = _listed_tools()

    missing = sorted(name for name, t in tools.items() if t.annotations is None)
    assert missing == []
    assert {n for n, t in tools.items() if t.annotations.openWorldHint is not False} == set()
    assert {n for n, t in tools.items() if t.annotations.destructiveHint is not False} == set()


def test_t1_read_only_is_true_exactly_for_the_read_only_set():
    tools = _listed_tools()

    hint = {n: getattr(t.annotations, "readOnlyHint", None) for n, t in tools.items()}
    assert {n for n, v in hint.items() if v is True} == READ_ONLY
    assert {n for n, v in hint.items() if v is False} == LOCAL_WRITE_SYNC | LOCAL_WRITE


def test_t1_the_three_sets_cover_every_registered_tool():
    """A tool added later must be classified here, and so annotated, before this passes."""
    registered = set(_listed_tools())

    assert not (READ_ONLY & LOCAL_WRITE_SYNC or READ_ONLY & LOCAL_WRITE)
    assert not LOCAL_WRITE_SYNC & LOCAL_WRITE
    assert registered == READ_ONLY | LOCAL_WRITE_SYNC | LOCAL_WRITE
    assert len(registered) == 24
