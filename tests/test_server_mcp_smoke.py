"""End-to-end smoke test through the REAL fastmcp dispatch path.

This is the path the ``tests/test_server_*.py`` unit tests do *not* exercise: they all call
the module's ``_*_impl`` functions directly with an injected ``Store``, bypassing fastmcp
entirely. fastmcp 3 runs sync ``@mcp.tool`` callables on a worker thread by default
(``run_in_thread=True``), which is exactly what broke the module-level, single-thread
``Store`` on a fresh install (see CLAUDE.md). Using fastmcp's in-memory ``Client`` against
the real ``sidegraph.server.mcp`` instance reproduces that dispatch without a subprocess.

This test fails against the pre-fix ``Store`` (single sqlite3 connection, no lock) with:
    fastmcp.exceptions.ToolError: Error calling tool 'add_decision': SQLite objects
    created in a thread can only be used in that same thread.
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import json

import fastmcp

import sidegraph.server as server_module
from sidegraph.store import Store


def test_mcp_add_and_retrieve_decisions_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(server_module, "_store", Store(tmp_path / "mcp_smoke.db"))

    async def _run():
        async with fastmcp.Client(server_module.mcp) as client:
            add_result = await client.call_tool(
                "add_decision",
                {
                    "title": "Use SQLite for the store",
                    "kind": "adr",
                    "context": "Need a serverless, repo-committable store.",
                    "choice": "SQLite via stdlib sqlite3.",
                },
            )
            retrieve_result = await client.call_tool("retrieve_decisions", {})
            return add_result, retrieve_result

    add_result, retrieve_result = asyncio.run(_run())

    added = add_result.data
    assert added["status"] == "accepted"
    assert added["id"]

    decisions = retrieve_result.data
    assert any(d["id"] == added["id"] for d in decisions)


def test_mcp_server_info_reports_own_package_version():
    """Gate-5 finding N1: the initialize handshake's serverInfo.version must be sidegraph's
    own installed package version (importlib.metadata), not fastmcp's default."""

    async def _run():
        async with fastmcp.Client(server_module.mcp) as client:
            return client.initialize_result.serverInfo

    info = asyncio.run(_run())
    assert info.name == "sidegraph"
    assert info.version == importlib.metadata.version("sidegraph")


def test_mcp_add_domain_propose_domains_and_ratify_roundtrip(tmp_path, monkeypatch):
    """Same worker-thread dispatch concern as above, exercised for the new mind-model
    tools: add_domain, propose_domains, and the unified ratify."""
    monkeypatch.setattr(server_module, "_store", Store(tmp_path / "mcp_smoke_domains.db"))

    async def _run():
        async with fastmcp.Client(server_module.mcp) as client:
            add_result = await client.call_tool(
                "add_domain",
                {"slug": "payments", "title": "Payments", "summary": "Order settlement."},
            )
            propose_result = await client.call_tool(
                "propose_domains",
                {"drafts": [{"slug": "shipping", "title": "Shipping", "summary": "Delivery."}]},
            )
            manual_id = add_result.data["domain_id"]
            proposed_id = propose_result.data[0]["domain_id"]
            ratify_result = await client.call_tool("ratify", {"accept": [manual_id, proposed_id]})
            return add_result, propose_result, ratify_result, manual_id, proposed_id

    add_result, propose_result, ratify_result, manual_id, proposed_id = asyncio.run(_run())

    assert add_result.data["status"] == "proposed"
    assert propose_result.data[0]["status"] == "proposed"
    assert ratify_result.data[manual_id] == "accepted"
    assert ratify_result.data[proposed_id] == "accepted"


def test_mcp_drill_down_and_thin_tools_roundtrip(tmp_path, monkeypatch):
    """Same worker-thread dispatch concern as above, exercised for the M5 mind-model
    tools: drill_down, query_structure, query_decisions."""
    monkeypatch.setattr(server_module, "_store", Store(tmp_path / "mcp_smoke_m5.db"))
    monkeypatch.setenv("SIDEGRAPH_GRAPH", str(tmp_path / "no-such-graph.json"))

    async def _run():
        async with fastmcp.Client(server_module.mcp) as client:
            add_result = await client.call_tool(
                "add_domain",
                {"slug": "payments", "title": "Payments", "summary": "Order settlement."},
            )
            domain_id = add_result.data["domain_id"]
            await client.call_tool("ratify", {"accept": [domain_id]})

            found_result = await client.call_tool("drill_down", {"domain_slug": "payments"})
            missing_result = await client.call_tool("drill_down", {"domain_slug": "no-such"})
            structure_result = await client.call_tool(
                "query_structure", {"files": ["some/file.py"]}
            )
            decisions_result = await client.call_tool(
                "query_decisions", {"files": ["some/file.py"]}
            )
            return found_result, missing_result, structure_result, decisions_result

    found, missing, structure, decisions = asyncio.run(_run())

    assert found.data["found"] is True
    assert found.data["domain"]["slug"] == "payments"
    assert missing.data["found"] is False
    assert isinstance(structure.data, str)
    assert isinstance(decisions.data, str)


def test_mcp_list_domain_candidates_roundtrip(tmp_path, monkeypatch):
    """Same worker-thread dispatch concern as above, exercised for the read-only
    list_domain_candidates tool (§1 domain-onboarding design) -- writes nothing, so a
    no-graph-present shape is enough to prove the real fastmcp dispatch path works."""
    monkeypatch.setattr(server_module, "_store", Store(tmp_path / "mcp_smoke_candidates.db"))
    monkeypatch.setenv("SIDEGRAPH_GRAPH", str(tmp_path / "no-such-graph.json"))

    async def _run():
        async with fastmcp.Client(server_module.mcp) as client:
            return await client.call_tool("list_domain_candidates", {})

    result = asyncio.run(_run())

    assert result.data["total_candidates"] == 0
    assert result.data["already_claimed"] == 0
    assert result.data["groups"] == []
    assert result.data["ungrouped"] == []
    assert "note" in result.data


def _many_communities_graph(n: int) -> dict:
    """``n`` distinct significant (5-member) communities -- a monorepo-scale fixture (real
    finding: Airflow returns 2,578 candidates at default settings, BUG B)."""
    nodes = [
        {
            "id": f"c{c}n{i}",
            "label": f"Thing{c}_{i}",
            "norm_label": f"thing{c}_{i}",
            "file_type": "code",
            "source_file": f"area{c}/f{i}.py",
            "community": c,
        }
        for c in range(n)
        for i in range(5)
    ]
    return {"built_at_commit": "v1", "nodes": nodes, "links": []}


def test_mcp_list_domain_candidates_default_limit_truncates(tmp_path, monkeypatch):
    """BUG B: the TOOL's own default (no `limit` passed at all) caps a large candidate list
    at 100 and flags the cut -- the `name-domains` skill's default call must never see a
    multi-thousand-candidate dump."""
    monkeypatch.setattr(server_module, "_store", Store(tmp_path / "mcp_smoke_default_limit.db"))
    graph_path = tmp_path / "graph.json"
    graph_path.write_text(json.dumps(_many_communities_graph(150)))
    monkeypatch.setenv("SIDEGRAPH_GRAPH", str(graph_path))

    async def _run():
        async with fastmcp.Client(server_module.mcp) as client:
            return await client.call_tool("list_domain_candidates", {})

    result = asyncio.run(_run())

    all_candidates = [c for g in result.data["groups"] for c in g["candidates"]] + result.data[
        "ungrouped"
    ]
    assert len(all_candidates) == 100
    assert result.data["total_candidates"] == 100
    assert result.data["total_significant"] == 150
    assert result.data["truncated"] is True


def test_mcp_list_domain_candidates_limit_zero_is_unlimited(tmp_path, monkeypatch):
    """The "all" convention: `limit=0` is the public sentinel for "no limit" (translated to
    the collector's own `None` internally)."""
    monkeypatch.setattr(server_module, "_store", Store(tmp_path / "mcp_smoke_unlimited.db"))
    graph_path = tmp_path / "graph.json"
    graph_path.write_text(json.dumps(_many_communities_graph(150)))
    monkeypatch.setenv("SIDEGRAPH_GRAPH", str(graph_path))

    async def _run():
        async with fastmcp.Client(server_module.mcp) as client:
            return await client.call_tool("list_domain_candidates", {"limit": 0})

    result = asyncio.run(_run())

    all_candidates = [c for g in result.data["groups"] for c in g["candidates"]] + result.data[
        "ungrouped"
    ]
    assert len(all_candidates) == 150
    assert result.data["truncated"] is False


def test_mcp_list_domain_candidates_explicit_limit_override(tmp_path, monkeypatch):
    monkeypatch.setattr(server_module, "_store", Store(tmp_path / "mcp_smoke_explicit_limit.db"))
    graph_path = tmp_path / "graph.json"
    graph_path.write_text(json.dumps(_many_communities_graph(150)))
    monkeypatch.setenv("SIDEGRAPH_GRAPH", str(graph_path))

    async def _run():
        async with fastmcp.Client(server_module.mcp) as client:
            return await client.call_tool("list_domain_candidates", {"limit": 25})

    result = asyncio.run(_run())

    all_candidates = [c for g in result.data["groups"] for c in g["candidates"]] + result.data[
        "ungrouped"
    ]
    assert len(all_candidates) == 25
    assert result.data["truncated"] is True


def test_mcp_supersede_decision_stamps_new_optional_params(tmp_path, monkeypatch):
    """Staleness machinery D6, dispatched through the real fastmcp path (same worker-thread
    dispatch concern as every other smoke test here): session_id/author/source are new
    optional params on supersede_decision, and must actually reach the tool."""
    monkeypatch.setattr(server_module, "_store", Store(tmp_path / "mcp_smoke_supersede.db"))

    async def _run():
        async with fastmcp.Client(server_module.mcp) as client:
            add_result = await client.call_tool(
                "add_decision",
                {"title": "old way", "kind": "adr", "context": "c", "choice": "ch"},
            )
            supersede_result = await client.call_tool(
                "supersede_decision",
                {
                    "old_decision_id": add_result.data["id"],
                    "title": "new way",
                    "kind": "adr",
                    "context": "c2",
                    "choice": "ch2",
                    "session_id": "s1",
                    "author": "alex",
                    "source": "agent",
                },
            )
            return add_result, supersede_result

    add_result, supersede_result = asyncio.run(_run())
    assert supersede_result.data["supersedes"] == add_result.data["id"]

    store = Store(tmp_path / "mcp_smoke_supersede.db")
    prov = store.get_decision(supersede_result.data["id"]).provenance
    assert prov.session_id == "s1"
    assert prov.author == "alex"
    assert prov.source == "agent"


def test_mcp_list_domains_and_supersede_domain_roundtrip(tmp_path, monkeypatch):
    """Same worker-thread dispatch concern as above, exercised for the two domain-
    onboarding-wave tools that close the "no full listing"/"no MCP rename" gaps:
    list_domains and supersede_domain."""
    monkeypatch.setattr(server_module, "_store", Store(tmp_path / "mcp_smoke_domains2.db"))

    async def _run():
        async with fastmcp.Client(server_module.mcp) as client:
            add_result = await client.call_tool(
                "add_domain",
                {"slug": "payments", "title": "Payments", "summary": "Order settlement."},
            )
            domain_id = add_result.data["domain_id"]
            await client.call_tool("ratify", {"accept": [domain_id]})

            listed = await client.call_tool("list_domains", {})
            filtered = await client.call_tool("list_domains", {"status": "accepted"})
            supersede_result = await client.call_tool(
                "supersede_domain",
                {
                    "old_slug_or_id": "payments",
                    "new_slug": "payments-v2",
                    "new_title": "Payments v2",
                    "new_summary": "Order settlement, v2.",
                },
            )
            return listed, filtered, supersede_result, domain_id

    listed, filtered, supersede_result, domain_id = asyncio.run(_run())

    assert len(listed.data) == 1
    assert listed.data[0]["slug"] == "payments"
    assert len(filtered.data) == 1

    assert supersede_result.data["supersedes"] == domain_id
    assert supersede_result.data["status"] == "proposed"
