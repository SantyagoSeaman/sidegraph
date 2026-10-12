"""Admission is finite, immediate and before refresh; control requests remain available."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor

import fastmcp
import mcp.types as mt
import pytest
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import MiddlewareContext
from fastmcp.tools import ToolResult


def _context():
    return MiddlewareContext(
        message=mt.CallToolRequestParams(name="test", arguments={}),
        source="client",
        type="request",
        method="tools/call",
    )


def test_burst_refill_and_capacity_are_bounded():
    from sidegraph.tool_rate_limit import ToolCallRateLimit

    now = [0.0]
    limiter = ToolCallRateLimit(rate=2, capacity=3, clock=lambda: now[0])
    called = []

    async def dispatch(ctx):
        called.append(1)
        return ToolResult(content="ok")

    async def run():
        for _ in range(3):
            await limiter.on_call_tool(_context(), dispatch)
        with pytest.raises(ToolError):
            await limiter.on_call_tool(_context(), dispatch)
        now[0] = 0.25
        with pytest.raises(ToolError):
            await limiter.on_call_tool(_context(), dispatch)
        now[0] = 0.5
        await limiter.on_call_tool(_context(), dispatch)
        now[0] = 100
        for _ in range(3):
            await limiter.on_call_tool(_context(), dispatch)
        with pytest.raises(ToolError):
            await limiter.on_call_tool(_context(), dispatch)

    asyncio.run(run())
    assert len(called) == 7


def test_concurrent_threads_share_one_finite_bucket():
    from sidegraph.tool_rate_limit import ToolCallRateLimit

    limiter = ToolCallRateLimit(rate=1, capacity=3, clock=lambda: 0.0)

    async def dispatch(ctx):
        return ToolResult(content="ok")

    def call(_):
        try:
            asyncio.run(limiter.on_call_tool(_context(), dispatch))
        except ToolError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(call, range(20))) == 3


def test_exhaustion_does_not_dispatch_and_ping_still_works():
    from sidegraph.tool_rate_limit import ToolCallRateLimit

    app = fastmcp.FastMCP("policy-probe")
    app.add_middleware(ToolCallRateLimit(rate=1, capacity=1, clock=lambda: 0.0))
    called = []

    @app.tool
    def test() -> str:
        called.append(1)
        return "ok"

    async def run():
        async with fastmcp.Client(app) as client:
            assert not (await client.call_tool("test", {})).is_error
            refused = await client.call_tool("test", {}, raise_on_error=False)
            assert refused.is_error
            assert refused.content[0].text == "Tool call rate exceeded; wait briefly and retry."
            await client.ping()
            assert len(await client.list_tools()) == 1

    asyncio.run(run())
    assert len(called) == 1


def test_server_refuses_before_refresh_or_argument_lookup(tmp_path, monkeypatch):
    from sidegraph import server
    from sidegraph.store import Store
    from sidegraph.tool_rate_limit import ToolCallRateLimit

    limiter = ToolCallRateLimit(rate=1, capacity=1, clock=lambda: 0.0)
    existing = [m for m in server.mcp.middleware if not isinstance(m, ToolCallRateLimit)]
    monkeypatch.setattr(server.mcp, "middleware", [limiter, *existing])
    called = []
    original = server._classify_tool

    def classify(tool):
        called.append("classify")
        return original(tool)

    monkeypatch.setattr(server, "_refresh_store", lambda: called.append("refresh"))
    monkeypatch.setattr(server, "_classify_tool", classify)
    with Store(tmp_path / "store") as store:
        monkeypatch.setattr(server, "_store", store)

        async def run():
            async with fastmcp.Client(server.mcp) as client:
                await client.call_tool("list_proposed", {})
                assert called == ["refresh", "classify"]
                called.clear()
                result = await client.call_tool("list_proposed", {}, raise_on_error=False)
                assert result.is_error
                await client.ping()

        asyncio.run(run())
        assert called == []


@pytest.mark.parametrize("rate,capacity", [(0, 1), (float("inf"), 1), (float("nan"), 1), (1, 0)])
def test_invalid_policy_is_refused_at_configuration(rate, capacity):
    from sidegraph.tool_rate_limit import ToolCallRateLimit

    with pytest.raises(ValueError):
        ToolCallRateLimit(rate=rate, capacity=capacity)
