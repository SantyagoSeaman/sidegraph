"""Agent view wire parity and preparation boundaries (Portal design, D10)."""

import json
from collections.abc import Callable
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from sidegraph import server
from sidegraph.cli import stats_main
from sidegraph.host import launch, mcp_entry
from sidegraph.portal import contract
from sidegraph.portal.context import PortalContext
from sidegraph.retrieval import TOC_CACHE_KEY
from sidegraph.store import Store
from tests.graph_repo_support import GRAPH, head, write_graph
from tests.test_server_tool_emulation import (
    _canonical,
    _mask,
    _mcp_text,
    _preparation_state,
    _reader,
    _session_state,
)
from tests.test_session_start_budget import _ascii_domains
from tests.twin_support import DECISION_ID, ENTITY_ID, Scenario, build_twin_stores

_API = "/api/agent-view"
_FAMILIES = {"claude", "codex", "grok", "muse"}
_TOOLS = {
    "retrieve_decisions": {"files": ["pkg/m.py"]},
    "get_task_context": {"files": ["pkg/m.py"]},
    "query_decisions": {"files": ["pkg/m.py"]},
    "query_structure": {"files": ["pkg/m.py"]},
    "drill_down": {"domain_slug": "package"},
    "find_entity": {"name": "fn_0()"},
    "get_entity_history": {"entity_id": ENTITY_ID},
    "list_domains": {},
    "list_domain_candidates": {},
    "list_facts": {},
    "list_proposed": {},
    "verify_store": {},
}
_WIRING = {
    "session_start": _FAMILIES,
    "pre_tool_use": {"claude", "grok"},
    "agent_brief": {"claude", "grok"},
    "subagent_start": _FAMILIES,
    "stop": {"claude", "codex", "grok"},
    "mcp_instructions": {"grok"},
}
_HOOK_PARAMS = {"pre_tool_use": {"file_path": "pkg/m.py"}, "agent_brief": {"prompt": "pkg/m.py"}}


def _emulate(client: TestClient, path: str, data: dict) -> contract.Emulation:
    response = client.post(f"{_API}/{path}", json=data)
    assert response.status_code == 200, response.text
    result = contract.Emulation.model_validate(response.json())
    assert result.chars == len(result.text)
    assert "Fresh session" in result.assumptions
    assert not result.emitted or result.not_emitted_reason is None
    assert result.emitted or (not result.text and result.not_emitted_reason)
    return result


def _usage(path: Path, capsys: pytest.CaptureFixture[str]) -> dict:
    capsys.readouterr()
    assert stats_main(["--db", str(path), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    keys = ("activation", "reach", "delivery_evidence", "hook_runs", "cited_back")
    return {key: report[key] for key in keys}


def test_catalog_twelve_tools_six_surfaces_four_families_with_wired_on(
    portal_ctx: Callable[..., PortalContext],
    portal_client: Callable[..., TestClient],
) -> None:
    with portal_client(portal_ctx()) as client:
        response = client.get(f"{_API}/catalog")
    assert response.status_code == 200, response.text
    result = contract.Catalog.model_validate(response.json())
    assert len(result.tools) == len(_TOOLS) == 12
    assert {tool["name"] for tool in result.tools} == _TOOLS.keys()
    tools = {tool["name"]: tool["params_schema"] for tool in result.tools}
    assert tools["drill_down"]["required"] == ["domain_slug"]
    assert len(result.hook_surfaces) == len(_WIRING) == 6
    assert {hook["event"]: set(hook["wired_on"]) for hook in result.hook_surfaces} == _WIRING
    assert all(hook["firing_conditions"] for hook in result.hook_surfaces)
    schemas = {hook["event"]: hook["params_schema"] for hook in result.hook_surfaces}
    assert schemas["session_start"]["properties"]["source"]["enum"] == [
        "startup",
        "resume",
        "compact",
    ]
    assert schemas["pre_tool_use"]["properties"]["file_path"]["type"] == "string"
    assert schemas["agent_brief"]["properties"]["prompt"]["type"] == "string"
    assert len(result.host_families) == 4
    assert set(result.host_families) == _FAMILIES


@pytest.mark.parametrize("scenario", ["cold", "synced", "moved", "worktree"])
@pytest.mark.parametrize("name", _TOOLS)
def test_tool_route_equals_emulate_tool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    scenario: Scenario,
    name: str,
    portal_ctx: Callable[..., PortalContext],
    portal_client: Callable[..., TestClient],
) -> None:
    real_path, path = build_twin_stores(tmp_path, scenario)
    reader, borrowed_from = _reader(path, scenario)
    ctx = portal_ctx(
        store_path=path,
        graph_path=(borrowed_from or path.parent) / GRAPH,
        borrowed_from=borrowed_from,
    )
    args = _TOOLS[name]
    monkeypatch.setenv("SIDEGRAPH_TELEMETRY", "on")
    usage = _usage(path, capsys)
    with ctx.request_store() as store:
        session_state = _session_state(store)
    with Store(real_path) as real, portal_client(ctx) as client:
        before = _canonical(path)
        mcp_text = _mcp_text(monkeypatch, real, name, args)
        actual = _emulate(client, "tool", {"name": name, "args": args})
        assert _usage(path, capsys) == usage
        after = _canonical(path)
        assert after == _canonical(real_path)
        assert {file for file, data in after.items() if before.get(file) != data} == (
            {Path(f"entities/{ENTITY_ID}.json")} if actual.prepared.adopted_moves else set()
        )
        with ctx.request_store() as store:
            assert _preparation_state(store) == _preparation_state(real)
            assert _session_state(store) == session_state
            expected = server.emulate_tool(
                name,
                args,
                store=store,
                reader=reader,
                borrowed_from=borrowed_from,
            )
        assert actual.text == expected.text
        assert actual.budget == expected.budget
        assert actual.cut == expected.cut
        assert _mask(actual.text, path, scenario) == _mask(mcp_text, real_path, scenario)
        if name == "retrieve_decisions":
            assert actual.ids_in_text.count(DECISION_ID) == 1
            assert actual.prepared.adopted_moves == int(scenario == "moved")


def test_tool_accepts_long_mcp_read_argument(
    monkeypatch: pytest.MonkeyPatch,
    portal_ctx: Callable[..., PortalContext],
    portal_client: Callable[..., TestClient],
) -> None:
    args = {"query": "a" * 262_145}
    ctx = portal_ctx()
    with ctx.request_store() as store, portal_client(ctx) as client:
        expected = _mcp_text(monkeypatch, store, "list_domains", args)
        actual = _emulate(
            client, "tool", {"name": "list_domains", "args": args, "host_family": None}
        )
    assert actual.text == expected


@pytest.mark.parametrize("host_field", [{}, {"host_family": None}])
def test_hook_route_default_family_from_context(
    host_field: dict,
    portal_ctx: Callable[..., PortalContext],
    portal_client: Callable[..., TestClient],
) -> None:
    with portal_client(portal_ctx(host_family="grok", mode="chat")) as client:
        result = _emulate(client, "hook", {"event": "mcp_instructions", "params": {}, **host_field})
        override = _emulate(
            client,
            "hook",
            {"event": "mcp_instructions", "params": {}, "host_family": "claude"},
        )
    assert result.emitted and result.text == mcp_entry._render_grok_map(None)
    assert result.budget == 2000 and result.cut is None
    assert not override.emitted and "claude" in (override.not_emitted_reason or "")

    with portal_client(portal_ctx(host_family="", mode="chat")) as client:
        result = _emulate(client, "hook", {"event": "mcp_instructions", "params": {}})
    assert not result.emitted and "claude" in (result.not_emitted_reason or "")


def test_agent_view_links_record_ids_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    portal_ctx: Callable[..., PortalContext],
    portal_client: Callable[..., TestClient],
) -> None:
    path, _ = build_twin_stores(tmp_path, "synced")
    monkeypatch.chdir(path.parent)
    ctx = portal_ctx(store_path=path, graph_path=path.parent / GRAPH)
    with portal_client(ctx) as client:
        tool = _emulate(
            client, "tool", {"name": "get_entity_history", "args": {"entity_id": ENTITY_ID}}
        )
        hook = _emulate(
            client,
            "hook",
            {
                "event": "agent_brief",
                "params": {"prompt": f"Review pkg/m.py for {ENTITY_ID} and {DECISION_ID}."},
            },
        )
    assert ENTITY_ID in tool.text and ENTITY_ID not in tool.ids_in_text
    assert DECISION_ID in tool.ids_in_text
    assert ENTITY_ID in hook.text and ENTITY_ID not in hook.ids_in_text
    assert DECISION_ID in hook.ids_in_text


def test_session_start_reports_trimmed_map(
    portal_ctx: Callable[..., PortalContext],
    portal_client: Callable[..., TestClient],
) -> None:
    ctx = portal_ctx()
    with ctx.request_store() as store:
        store.set_meta(TOC_CACHE_KEY, json.dumps({"domains": _ascii_domains(400)}))
    with portal_client(ctx) as client:
        result = _emulate(client, "hook", {"event": "session_start", "params": {}})
    assert result.budget == 9500
    assert result.cut == result.text.splitlines()[-1]
    assert result.cut.startswith("Sidegraph: ")
    assert "omitted to fit the hook output limit" in result.cut


def test_grok_instructions_report_trimmed_map(
    portal_ctx: Callable[..., PortalContext],
    portal_client: Callable[..., TestClient],
) -> None:
    ctx = portal_ctx(host_family="grok")
    with ctx.request_store() as store:
        store.set_meta(TOC_CACHE_KEY, json.dumps({"domains": _ascii_domains(400)}))
    with portal_client(ctx) as client:
        result = _emulate(client, "hook", {"event": "mcp_instructions", "params": {}})
    assert result.budget == 2000 and result.chars <= 2000
    assert result.cut and "cut to fit Grok" in result.text


def test_not_wired_surface_reports_not_emitted(
    portal_ctx: Callable[..., PortalContext],
    portal_client: Callable[..., TestClient],
) -> None:
    with portal_client(portal_ctx()) as client:
        result = _emulate(
            client,
            "hook",
            {
                "event": "pre_tool_use",
                "params": {"file_path": "pkg/m.py"},
                "host_family": "codex",
            },
        )
    assert not result.emitted and result.text == ""
    assert "not wired on codex" in (result.not_emitted_reason or "")
    assert result.firing_conditions


def test_cli_graph_differs_runs_no_sync_and_says_so(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    portal_ctx: Callable[..., PortalContext],
    portal_client: Callable[..., TestClient],
) -> None:
    path, _ = build_twin_stores(tmp_path, "moved")
    monkeypatch.chdir(path.parent)
    override = path.parent / "alternate.json"
    write_graph(override, head(path.parent), ["alternate.py"])
    ctx = portal_ctx(store_path=path, graph_path=override, graph_overridden=True)
    with ctx.request_store() as store:
        before = (_canonical(path), _preparation_state(store))
    with portal_client(ctx) as client:
        tool = _emulate(
            client, "tool", {"name": "query_structure", "args": {"files": ["alternate.py"]}}
        )
        hook = _emulate(client, "hook", {"event": "session_start", "params": {}})
    for result in (tool, hook):
        assert result.prepared == contract.Preparation(
            synced=False,
            adopted_moves=0,
            skipped_reason="The CLI graph differs from the agent's graph.",
        )
    assert "fn_0()" in tool.text and "alternate.py" in tool.text
    with ctx.request_store() as store:
        assert (_canonical(path), _preparation_state(store)) == before


def test_adopted_moves_reported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    portal_ctx: Callable[..., PortalContext],
    portal_client: Callable[..., TestClient],
) -> None:
    path, _ = build_twin_stores(tmp_path, "moved")
    monkeypatch.chdir(path.parent)
    ctx = portal_ctx(store_path=path, graph_path=path.parent / GRAPH)
    with portal_client(ctx) as client:
        result = _emulate(
            client, "hook", {"event": "session_start", "params": {"source": "startup"}}
        )
        subsequent = _emulate(
            client, "tool", {"name": "get_task_context", "args": {"files": ["pkg/m.py"]}}
        )
    assert result.prepared == contract.Preparation(synced=True, adopted_moves=1)
    assert subsequent.prepared.adopted_moves == 0
    assert DECISION_ID in subsequent.ids_in_text
    with ctx.request_store() as store:
        assert store.get_entity(ENTITY_ID).descriptor.file_path == "pkg/m.py"


def test_agent_view_routes_are_post(
    portal_ctx: Callable[..., PortalContext],
    portal_client: Callable[..., TestClient],
) -> None:
    with portal_client(portal_ctx()) as client:
        for method, path in [("GET", "tool"), ("GET", "hook"), ("POST", "catalog")]:
            response = client.request(method, f"{_API}/{path}")
            assert response.status_code == 405, response.text
            assert contract.ErrorBody.model_validate(response.json()).error.code == "invalid"


def test_agent_view_on_empty_store_no_graph(
    portal_ctx: Callable[..., PortalContext],
    portal_client: Callable[..., TestClient],
) -> None:
    with portal_client(portal_ctx()) as client:
        catalog = client.get(f"{_API}/catalog")
        assert catalog.status_code == 200, catalog.text
        assert contract.Catalog.model_validate(catalog.json()).tools
        for name, args in _TOOLS.items():
            result = _emulate(client, "tool", {"name": name, "args": args})
            assert result.prepared.adopted_moves == 0 and not result.prepared.synced
        for event, families in _WIRING.items():
            result = _emulate(
                client,
                "hook",
                {
                    "event": event,
                    "params": _HOOK_PARAMS.get(event, {}),
                    "host_family": min(families),
                },
            )
            assert result.prepared.adopted_moves == 0 and not result.prepared.synced


@pytest.mark.parametrize("scenario", ["cold", "synced", "moved"])
def test_stats_usage_unchanged_after_every_route(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    scenario: Scenario,
    portal_ctx: Callable[..., PortalContext],
    portal_client: Callable[..., TestClient],
) -> None:
    path, _ = build_twin_stores(tmp_path, scenario)
    monkeypatch.chdir(path.parent)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("SIDEGRAPH_TELEMETRY", "on")
    monkeypatch.setattr(launch, "_resolved_main_commit", lambda: "2" * 40)
    ctx = portal_ctx(store_path=path, graph_path=path.parent / GRAPH)
    launch_path = launch.launch_commit_path()
    launch_path.parent.mkdir(parents=True)
    launch_path.write_text("1" * 40 + "\n", encoding="utf-8")
    log = path / "index.db-hooks.log"
    log.write_text("Previous hook run\n", encoding="utf-8")

    def state() -> tuple:
        stats = _usage(path, capsys)
        with ctx.request_store() as store:
            return stats, _session_state(store), log.read_bytes(), launch_path.read_bytes()

    before = state()
    with portal_client(ctx) as client:
        catalog = client.get(f"{_API}/catalog")
        assert catalog.status_code == 200, catalog.text
        assert contract.Catalog.model_validate(catalog.json()).tools
        assert state() == before
        for event, families in _WIRING.items():
            assert _emulate(
                client,
                "hook",
                {
                    "event": event,
                    "params": _HOOK_PARAMS.get(event, {}),
                    "host_family": min(families),
                },
            ).emitted
            assert state() == before, event
        for name, args in _TOOLS.items():
            assert _emulate(client, "tool", {"name": name, "args": args}).emitted
            assert state() == before, name


@pytest.mark.parametrize(
    ("route", "body"),
    [
        ("tool", {"name": "open_portal", "args": {}}),
        ("tool", {"name": "add_decision", "args": {}}),
        ("tool", {"name": "drill_down", "args": {}}),
        ("tool", {"name": "retrieve_decisions", "args": {"status": "bogus"}}),
        ("tool", {"name": "retrieve_decisions", "args": {"kind": "bogus"}}),
        ("tool", {"name": "list_facts", "args": {"status": "bogus"}}),
        ("tool", {"name": "get_task_context", "args": {"entities": [{"name": 5}]}}),
        ("tool", {"name": "list_domains", "args": []}),
        ("hook", {"event": "unknown", "params": {}}),
        ("hook", {"event": "session_start", "params": {"source": "other"}}),
        ("hook", {"event": "session_start", "params": {}, "host_family": ["grok"]}),
        ("hook", {"event": "session_start", "params": {}, "host_family": ""}),
    ],
)
def test_invalid_agent_view_requests(
    route: str,
    body: dict,
    portal_ctx: Callable[..., PortalContext],
    portal_client: Callable[..., TestClient],
) -> None:
    with portal_client(portal_ctx(), raise_server_exceptions=False) as client:
        response = client.post(f"{_API}/{route}", json=body)
    assert response.status_code == 422, response.text
    assert contract.ErrorBody.model_validate(response.json()).error.code == "invalid"
