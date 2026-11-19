"""Agent view catalog and emulation endpoints (D10)."""

import re
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
from typing import cast, get_args

from fastmcp.exceptions import ToolError
from pydantic import JsonValue
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from sidegraph import server
from sidegraph.gitio import open_index_ro
from sidegraph.host import emulation, hooks, mcp_entry
from sidegraph.store_layout import LegacyStoreError, StoreRequirementError

from . import contract
from .context import PortalContext

_TOOLS = (server._READ_TOOLS | server._LAZY_READ_TOOLS) - {"open_portal"}
_FAMILIES = get_args(contract.HostFamily.__value__)
_SESSION_SOURCES = ("startup", "resume", "compact")
_HOOK_PROPERTIES: dict[str, dict[str, dict[str, JsonValue]]] = {
    "session_start": {"source": {"type": "string", "enum": list(_SESSION_SOURCES)}},
    "pre_tool_use": {"file_path": {"type": "string"}, "tool_name": {"type": "string"}},
    "agent_brief": {"prompt": {"type": "string"}},
}
_RECORD_ID = re.compile(r"\b[0-7][0-9A-HJKMNP-TV-Z]{25}\b")


def _response(
    result: server.ToolEmulation | emulation.HookEmulation,
    store_path: Path,
    *,
    event: str | None = None,
) -> JSONResponse:
    text = result.text
    if isinstance(result, emulation.HookEmulation):
        emitted, reason, conditions = (
            result.emitted,
            result.not_emitted_reason,
            result.firing_conditions,
        )
        budget, cut = None, None
        if emitted and event == "session_start":
            budget = hooks.HOOK_CONTEXT_MAX_CHARS
            last_line = text.rstrip("\n").rpartition("\n")[2]
            if last_line.startswith("Sidegraph: ") and (
                " omitted to fit the hook output limit: " in last_line
            ):
                cut = last_line
        elif emitted and event == "mcp_instructions":
            budget = mcp_entry.GROK_MAP_MAX_CHARS
            if text.endswith(mcp_entry._GROK_MAP_CUT):
                cut = "The memory map was cut to the host budget."
    else:
        emitted, reason, conditions = True, None, []
        budget, cut = result.budget, result.cut
    candidates = dict.fromkeys(_RECORD_ID.findall(text))
    record_ids = []
    if candidates and (index := open_index_ro(store_path, busy_timeout_seconds=2.0)) is not None:
        with closing(index):
            record_ids = [
                record_id
                for record_id in candidates
                if index.execute(
                    "SELECT 1 FROM decisions WHERE id = ? "
                    "UNION ALL SELECT 1 FROM facts WHERE id = ? "
                    "UNION ALL SELECT 1 FROM domains WHERE domain_id = ? LIMIT 1",
                    (record_id, record_id, record_id),
                ).fetchone()
                is not None
            ]
    value = contract.Emulation(
        text=text,
        emitted=emitted,
        not_emitted_reason=reason,
        chars=len(text),
        budget=budget,
        cut=cut,
        assumptions=["Fresh session"],
        firing_conditions=conditions,
        prepared=contract.Preparation(**asdict(result.prepared)),
        ids_in_text=record_ids,
    )
    return JSONResponse(value.model_dump(mode="json", exclude_none=True))


def routes(ctx: PortalContext) -> list[Route]:
    """Register Agent view routes (design/superpowers/specs/2026-11-17-portal-design.md, D10)."""

    async def catalog(_request: Request) -> JSONResponse:
        value = contract.Catalog.model_validate(
            {
                "tools": [
                    {"name": tool.name, "params_schema": tool.parameters}
                    for tool in await server.mcp.list_tools(run_middleware=False)
                    if tool.name in _TOOLS
                ],
                "hook_surfaces": [
                    {
                        "event": event,
                        "params_schema": {
                            "type": "object",
                            "properties": _HOOK_PROPERTIES.get(event, {}),
                            "additionalProperties": False,
                        },
                        "firing_conditions": list(emulation._FIRING_CONDITIONS[event]),
                        "wired_on": sorted(families),
                    }
                    for event, families in emulation.WIRED_ON.items()
                ],
                "host_families": _FAMILIES,
            }
        )
        return JSONResponse(value.model_dump(mode="json"))

    def tool(name: str, args: dict) -> JSONResponse:
        from .app import _error

        with ctx.request_store() as store:
            try:
                result = server.emulate_tool(
                    name,
                    args,
                    store=store,
                    reader=ctx.reader(),
                    borrowed_from=ctx.borrowed_from,
                    sync=not ctx.server_graph_differs,
                )
            except (LegacyStoreError, StoreRequirementError):
                raise
            except (ToolError, ValueError):
                return _error("invalid", "The tool arguments are invalid.", field="args")
        return _response(result, ctx.store_path)

    def hook(event: str, params: dict, host_family: contract.HostFamily) -> JSONResponse:
        result = emulation.emulate_hook(
            cast(contract.HookEvent, event),
            params,
            store_path=ctx.store_path,
            reader=ctx.reader(),
            borrowed_from=ctx.borrowed_from,
            host_family=host_family,
            sync=not ctx.server_graph_differs,
        )
        return _response(result, ctx.store_path, event=event)

    async def emulate(request: Request) -> JSONResponse:
        from .app import _error

        is_tool = request.url.path.endswith("/tool")
        name_key, args_key = ("name", "args") if is_tool else ("event", "params")
        try:
            body = await request.json()
        except (ValueError, RecursionError):
            return _error("invalid", "Expected a JSON object.")
        keys = {name_key, args_key}
        if not isinstance(body, dict) or body.keys() not in (keys, keys | {"host_family"}):
            return _error("invalid", "Expected the surface name and its arguments.")
        family = body.get("host_family")
        if family is None:
            family = ctx.host_family or "claude"
        if family not in _FAMILIES:
            return _error("invalid", "Unknown host family.", field="host_family")
        name, args = body[name_key], body[args_key]
        if not isinstance(name, str) or name not in (_TOOLS if is_tool else emulation.WIRED_ON):
            return _error("invalid", "Unknown Agent view surface.", field=name_key)
        if not isinstance(args, dict):
            return _error("invalid", "Expected an argument object.", field=args_key)
        if is_tool:
            return await run_in_threadpool(tool, name, args)
        if (
            any(
                key not in _HOOK_PROPERTIES.get(name, {}) or not isinstance(value, str)
                for key, value in args.items()
            )
            or args.get("source", "startup") not in _SESSION_SOURCES
        ):
            return _error("invalid", "The hook parameters are invalid.", field=args_key)
        return await run_in_threadpool(hook, name, args, cast(contract.HostFamily, family))

    handlers = {
        "/api/agent-view/catalog": catalog,
        "/api/agent-view/tool": emulate,
        "/api/agent-view/hook": emulate,
    }
    return [
        Route(route.path, handlers[route.path], methods=[route.method])
        for route in contract.ROUTES
        if route.module == "api_agent"
    ]
