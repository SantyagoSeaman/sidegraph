"""MCP flags/schemas describe complete failures and avoid duplicate text output."""

from __future__ import annotations

import asyncio

import fastmcp
import mcp.types as mt
import pytest

from sidegraph import server
from sidegraph.store import Store
from sidegraph.write_boundary import CONFIGURATION_ERROR


@pytest.fixture
def isolated_server(tmp_path, monkeypatch):
    with Store(tmp_path / "store") as store:
        monkeypatch.setattr(server, "_store", store)
        monkeypatch.setattr(server, "_load_reader", lambda: None)
        yield store


def call(name, arguments=None):
    async def run():
        async with fastmcp.Client(server.mcp) as client:
            return await client.call_tool(name, arguments or {}, raise_on_error=False)

    return asyncio.run(run())


@pytest.mark.parametrize(
    "name,args",
    [
        ("sync_anchors", {}),
        ("add_anchors", {"record_id": "missing-record", "anchors": []}),
    ],
)
def test_complete_operation_failure_keeps_payload_and_sets_mcp_error(isolated_server, name, args):
    result = call(name, args)
    assert result.is_error
    assert isinstance(result.content[0], mt.TextContent)
    assert result.structured_content["error"]
    if name == "add_anchors":
        assert result.structured_content == {"error": "unknown record"}
    else:
        assert result.structured_content["synced"] is False


@pytest.mark.parametrize(
    "name", ["get_task_context", "query_structure", "query_decisions", "list_proposed"]
)
def test_text_tool_advertises_and_emits_content_only(isolated_server, name):
    async def run():
        async with fastmcp.Client(server.mcp) as client:
            tools = {t.name: t for t in await client.list_tools()}
            result = await client.call_tool(name, {}, raise_on_error=False)
            return tools[name], result

    tool, result = asyncio.run(run())
    assert tool.outputSchema is None
    assert not result.is_error
    assert len(result.content) == 1
    assert isinstance(result.content[0], mt.TextContent)
    assert result.structured_content is None


def test_unknown_tool_has_a_safe_actionable_diagnostic(isolated_server):
    result = call("untrusted-name-canary")
    assert result.is_error
    assert result.content[0].text == "Unknown tool. Call tools/list for available tools."
    assert "untrusted-name-canary" not in result.content[0].text


def test_missing_registered_tool_is_still_a_configuration_error(isolated_server, monkeypatch):
    original = server.mcp.get_tool

    async def get_tool(name, *args, **kwargs):
        if name == "add_decision":
            return None
        return await original(name, *args, **kwargs)

    monkeypatch.setattr(server.mcp, "get_tool", get_tool)
    result = call(
        "add_decision", {"title": "Test", "kind": "adr", "context": "Test", "choice": "Test"}
    )
    assert result.is_error
    assert result.content[0].text == CONFIGURATION_ERROR


def test_dictionary_and_partial_batch_results_keep_their_contract(isolated_server):
    added = call(
        "add_decision", {"title": "Test", "kind": "adr", "context": "Test", "choice": "Test"}
    )
    assert not added.is_error
    assert added.structured_content["status"] == "accepted"
    partial = call(
        "propose_decisions",
        {
            "drafts": [
                {"title": "Proposal", "kind": "adr", "context": "Test", "choice": "Test"},
                {"title": "Invalid", "choice": ""},
            ]
        },
    )
    assert not partial.is_error
    assert partial.data[0]["status"] == "written"
    assert partial.data[1]["status"] == "rejected"
    assert partial.data[1]["reason"]
    verdict = call("ratify", {"accept": ["unknown-record"]})
    assert not verdict.is_error
    assert "unknown-record" in verdict.structured_content


@pytest.mark.parametrize(
    "name,args",
    [
        ("list_facts", {}),
        ("get_entity_history", {"entity_id": "nothing"}),
        ("drill_down", {"domain_slug": "nothing"}),
        ("list_domain_candidates", {}),
    ],
)
def test_bounded_json_tool_advertises_no_schema_and_emits_one_text_block(
    isolated_server, name, args
):
    async def run():
        async with fastmcp.Client(server.mcp) as client:
            tools = {t.name: t for t in await client.list_tools()}
            result = await client.call_tool(name, args, raise_on_error=False)
            return tools[name], result

    tool, result = asyncio.run(run())
    assert tool.outputSchema is None
    assert not result.is_error
    assert len(result.content) == 1
    assert isinstance(result.content[0], mt.TextContent)
    assert result.structured_content is None
    assert result.data is None
