"""Registered MCP tool goldens and emulation parity (Portal design, D10)."""

import asyncio
import json
import os
from pathlib import Path

import fastmcp
import pytest
from pydantic import ValidationError

from sidegraph import delivery, nearest_anchored, retrieval, server, store_layout, sync
from sidegraph import store as store_module
from sidegraph.config import CURRENT_SESSION, SessionHint
from sidegraph.engine.reader import GraphifyReader
from sidegraph.schema import (
    AnchorBinding,
    Decision,
    DecisionKind,
    DecisionStatus,
    Descriptor,
    Entity,
    Provenance,
)
from sidegraph.store import Store
from sidegraph.store_layout import REBIND_DONE_KEY, VOLATILE_STALE_KEY
from sidegraph.sync import LAST_SYNCED_KEY
from tests.graph_repo_support import GRAPH, git, head, make_main
from tests.test_ratify_policy import _frozen_datetime
from tests.twin_support import (
    DECISION_ID,
    DOMAIN_ID,
    ENTITY_ID,
    NOW,
    PROPOSAL_ID,
    Scenario,
    build_twin_stores,
)

_GOLDEN = Path(__file__).parent / "fixtures" / "portal_goldens" / "tools.json"
_SCENARIOS: tuple[Scenario, ...] = ("cold", "synced", "moved", "worktree")
_CASES: dict[str, dict[str, dict]] = {
    "retrieve_decisions": {
        "overview": {},
        "query": {"query": "retry", "limit": 1},
        "files": {"files": ["pkg/m.py"], "status": "accepted"},
    },
    "get_task_context": {
        "files": {"files": ["pkg/m.py"]},
        "entity": {"entities": [{"name": "fn_0()"}], "memory_budget": 200},
    },
    "query_decisions": {
        "files": {"files": ["pkg/m.py"]},
        "small_budget": {"files": ["pkg/m.py"], "budget_chars": 1},
    },
    "query_structure": {"files": {"files": ["pkg/m.py"]}, "default": {}},
    "drill_down": {
        "found": {"domain_slug": "package", "limit": 1},
        "unknown": {"domain_slug": "missing"},
    },
    "find_entity": {"found": {"name": "fn_0()"}, "unknown": {"name": "missing"}},
    "get_entity_history": {
        "all": {"entity_id": ENTITY_ID},
        "filtered": {"entity_id": ENTITY_ID, "status": "accepted", "limit": 1},
    },
    "list_domains": {"all": {}, "filtered": {"query": "batch", "status": "accepted"}},
    "list_domain_candidates": {"default": {}, "filtered": {"min_members": 1, "limit": 0}},
    "list_facts": {"all": {}, "filtered": {"files": ["pkg/m.py"], "supports": [DECISION_ID]}},
    "list_proposed": {"default": {}, "small_budget": {"budget_chars": 1}},
    "verify_store": {"default": {}, "small_budget": {"budget_chars": 1}},
}


@pytest.fixture(autouse=True)
def _tool_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIDEGRAPH_TELEMETRY", "on")
    fixed = _frozen_datetime(NOW)
    for module in (delivery, nearest_anchored, retrieval, server, store_layout, store_module, sync):
        monkeypatch.setattr(module, "datetime", fixed)


def _mcp_text(monkeypatch: pytest.MonkeyPatch, store: Store, tool: str, args: dict) -> str:
    async def call() -> str:
        async with fastmcp.Client(server.mcp) as client:
            response = await client.call_tool(tool, args)
        (block,) = response.content
        assert block.type == "text"
        assert response.structured_content is None
        return block.text

    with monkeypatch.context() as patch:
        patch.chdir(store.path.parent)
        patch.setenv("SIDEGRAPH_DIR", str(store.path))
        patch.setattr(server, "_store", store)
        token = CURRENT_SESSION.set(SessionHint("twin-tool", "codex", agent_exact=True))
        try:
            return asyncio.run(call())
        finally:
            CURRENT_SESSION.reset(token)


def _mask(text: str, path: Path, scenario: Scenario) -> str:
    checkout = path.parent
    root = checkout.parents[1] if scenario == "worktree" else checkout
    reader = GraphifyReader(root / GRAPH)
    built_at = reader.built_at_commit()
    assert built_at is not None
    root_text = str(root)
    clipped_root = server._clip(root_text)
    text = text.replace(root_text, "<repo>").replace(server._dump(root_text)[1:-1], "<repo>")
    if clipped_root != root_text:
        text = text.replace(clipped_root, "<repo>…").replace(
            server._dump(clipped_root)[1:-1], "<repo>…"
        )
    return (
        text.replace(reader.graph_version(), "<graph-version>")
        .replace(built_at, "<build>")
        .replace(head(checkout), "<head>")
        .replace(server._server_version(), "<version>")
    )


_TOOL_CASES = [
    pytest.param(tool, case, id=f"{tool}-{case}")
    for tool, cases in _CASES.items()
    for case in cases
]


@pytest.mark.parametrize("scenario", _SCENARIOS)
@pytest.mark.parametrize(("tool", "case"), _TOOL_CASES)
def test_tool_goldens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario: Scenario, tool: str, case: str
) -> None:
    path, _ = build_twin_stores(tmp_path, scenario)
    with Store(path) as store:
        text = _mask(_mcp_text(monkeypatch, store, tool, _CASES[tool][case]), path, scenario)
    assert text == json.loads(_GOLDEN.read_text(encoding="utf-8"))[scenario][tool][case]


def _reader(path: Path, scenario: Scenario) -> tuple[GraphifyReader, Path | None]:
    checkout = path.parent
    borrowed_from = checkout.parents[1] if scenario == "worktree" else None
    return GraphifyReader((borrowed_from or checkout) / GRAPH), borrowed_from


@pytest.mark.parametrize("scenario", _SCENARIOS)
@pytest.mark.parametrize(("tool", "case"), _TOOL_CASES)
def test_emulate_tool_equals_tool_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario: Scenario, tool: str, case: str
) -> None:
    real_path, emulated_path = build_twin_stores(tmp_path, scenario)
    args = _CASES[tool][case]
    with Store(real_path) as real, Store(emulated_path) as emulated:
        entity_file = Path(f"entities/{ENTITY_ID}.json")
        before = _canonical(real_path)
        assert _canonical(emulated_path) == before
        expected = _mcp_text(monkeypatch, real, tool, args)
        reader, borrowed_from = _reader(emulated_path, scenario)
        actual = server.emulate_tool(
            tool, args, store=emulated, reader=reader, borrowed_from=borrowed_from
        )
        after = _canonical(real_path)
        assert _canonical(emulated_path) == after
        assert _preparation_state(emulated) == _preparation_state(real)
        assert actual.prepared.adopted_moves == int(after[entity_file] != before[entity_file])
    assert _mask(actual.text, emulated_path, scenario) == _mask(expected, real_path, scenario)


def test_emulate_find_entity_explains_miss_without_reporting_cut(tmp_path: Path) -> None:
    with Store(tmp_path / "t.db") as store:
        result = server.emulate_tool(
            "find_entity",
            _CASES["find_entity"]["unknown"],
            store=store,
            reader=None,
            borrowed_from=None,
        )
    assert result.cut is None


def test_emulate_find_entity_uses_graph_on_miss(tmp_path: Path) -> None:
    reader = GraphifyReader(Path(__file__).parent / "fixtures" / "member_names_python_graph.json")
    file_path = "src/sidegraph/engine/reader.py"
    with Store(tmp_path / "t.db") as store:
        entity = store.upsert_entity(
            Entity(
                canonical_name=".graph_version()",
                descriptor=Descriptor(name=".graph_version()", file_path=file_path),
                last_seen_node_id="src_sidegraph_engine_reader_graphifyreader_graph_version",
            )
        )
        result = server.emulate_tool(
            "find_entity",
            {"name": "GraphifyReader.graph_version", "file_path": file_path},
            store=store,
            reader=reader,
            borrowed_from=None,
        )
    assert json.loads(result.text)["entity_id"] == entity.entity_id


def test_emulate_find_entity_reports_omitted_same_file_candidates(tmp_path: Path) -> None:
    reader = GraphifyReader(Path(__file__).parent / "fixtures" / "member_names_python_graph.json")
    file_path = "src/sidegraph/engine/reader.py"
    with Store(tmp_path / "t.db") as store:
        for i in range(40):
            name = f"Reader{i}.graph_version"
            store.upsert_entity(
                Entity(
                    canonical_name=name,
                    descriptor=Descriptor(name=name, file_path=file_path),
                    last_seen_node_id="src_sidegraph_engine_reader_graphifyreader_graph_version",
                )
            )
        result = server.emulate_tool(
            "find_entity",
            {"name": "GraphifyReader.graph_version", "file_path": file_path, "budget_chars": 4000},
            store=store,
            reader=reader,
            borrowed_from=None,
        )
    out = json.loads(result.text)
    assert len(out["candidates"]) + out["candidates_omitted"] == 40
    assert result.cut == out["hint"]
    assert "Use entity_id." in out["hint"]


def _rows(store: Store, table: str) -> list[tuple]:
    return sorted((tuple(row) for row in store._conn.execute(f"SELECT * FROM {table}")), key=repr)


def _session_state(store: Store) -> tuple[dict[str, list[tuple]], list[tuple]]:
    tables = (
        "retrieval_shows",
        "retrieval_seeds",
        "retrieval_events",
        "render_events",
        "session_transcripts",
        "delivery_occurrences",
        "delivery_placements",
        "delivery_anchor_links",
        "capture_sessions",
    )
    prefixes = (
        "mcp_sent:",
        "pretool_file:",
        "telemetry:session",
        "integrity_notice:",
        "capture_rearm:",
        "pretool_reset:",
    )
    return (
        {table: _rows(store, table) for table in tables},
        [
            (key, value)
            for key, value in _rows(store, "meta")
            if key.startswith(prefixes) or key == "session_start"
        ],
    )


def _canonical(path: Path) -> dict[Path, bytes]:
    durable = set(store_layout.STORE_INTERNAL_NAMES) - {
        "index.db",
        *store_layout.SQLITE_SIDECAR_NAMES,
    }
    snapshot: dict[Path, bytes] = {}
    for file in path.rglob("*"):
        relative = file.relative_to(path)
        if relative.parts[0] in durable and file.suffix != ".tmp" and file.is_file():
            snapshot[relative] = file.read_bytes()
    return snapshot


def _preparation_state(store: Store) -> tuple[dict[str, list[tuple]], list[str | None]]:
    # Synthetic community identities are re-minted on each cold index. Compare their
    # mappings by community name and keep concrete entity ids exact.
    entities = _rows(store, "entities")
    bindings = _rows(store, "anchor_bindings")
    communities = {
        entity_id: name for entity_id, name, _ in entities if name.startswith("community:")
    }
    return (
        {
            "entities": sorted(
                (
                    communities.get(entity_id, entity_id),
                    name,
                    json.loads(data)
                    | {
                        "entity_id": communities.get(entity_id, entity_id),
                    },
                )
                for entity_id, name, data in entities
            ),
            "anchor_bindings": sorted(
                (
                    record_id,
                    communities.get(entity_id, entity_id),
                    json.loads(data)
                    | {
                        "entity_id": communities.get(entity_id, entity_id),
                    },
                    kept,
                )
                for record_id, entity_id, data, kept in bindings
            ),
            "domain_members": _rows(store, "domain_members"),
        },
        [store.get_meta(key) for key in (LAST_SYNCED_KEY, VOLATILE_STALE_KEY, REBIND_DONE_KEY)],
    )


@pytest.mark.parametrize("tool", _CASES)
def test_emulate_tool_writes_no_session_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool: str
) -> None:
    real_path, path = build_twin_stores(tmp_path, "cold")
    reader, borrowed_from = _reader(path, "cold")
    cases = _CASES[tool]
    case = {
        "retrieve_decisions": "files",
        "list_facts": "filtered",
    }.get(tool, next(iter(cases)))
    args = cases[case]
    with Store(path) as store, Store(real_path) as real:
        store.set_meta(f"mcp_sent:twin-tool:-:{DECISION_ID}", NOW.isoformat())
        before = _session_state(store)
        with monkeypatch.context() as patch:
            patch.chdir(path.parent)
            patch.setenv("SIDEGRAPH_DIR", str(path))
            patch.setattr(server, "_store", store)
            token = CURRENT_SESSION.set(SessionHint("twin-tool", "codex", agent_exact=True))
            try:
                result = server.emulate_tool(
                    tool, args, store=store, reader=reader, borrowed_from=borrowed_from
                )
            finally:
                CURRENT_SESSION.reset(token)
        assert _session_state(store) == before
        if tool in {"get_task_context", "query_decisions", "retrieve_decisions"}:
            assert DECISION_ID in result.text
            assert "[sent earlier]" not in result.text
            assert '"sent_earlier":true' not in result.text
        if tool in {
            "get_task_context",
            "query_decisions",
            "retrieve_decisions",
            "drill_down",
            "find_entity",
            "get_entity_history",
            "list_facts",
        }:
            real_before = _session_state(real)
            _mcp_text(monkeypatch, real, tool, args)
            assert _session_state(real) != real_before


@pytest.mark.parametrize("scenario", _SCENARIOS)
def test_emulate_tool_preparation_matches_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario: Scenario
) -> None:
    real_path, path = build_twin_stores(tmp_path, scenario)
    reader, borrowed_from = _reader(path, scenario)
    args = {"domain_slug": "package"}
    with Store(real_path) as real, Store(path) as store:
        before = _canonical(path)
        assert _canonical(real_path) == before
        _mcp_text(monkeypatch, real, "drill_down", args)
        result = server.emulate_tool(
            "drill_down", args, store=store, reader=reader, borrowed_from=borrowed_from
        )
        assert _preparation_state(store) == _preparation_state(real)
        assert _canonical(path) == _canonical(real_path)
        changed = {file for file, data in _canonical(path).items() if before.get(file) != data}
        assert changed == ({Path(f"entities/{ENTITY_ID}.json")} if scenario == "moved" else set())
        assert result.prepared.adopted_moves == int(scenario == "moved")
        assert result.prepared.synced is (scenario != "synced")


def test_prepare_reader_sync_false_skips(tmp_path: Path) -> None:
    path, _ = build_twin_stores(tmp_path, "moved")
    graph, _ = _reader(path, "moved")
    with Store(path) as store:
        before = (_canonical(path), _preparation_state(store), _session_state(store))
        reader, preparation = server.prepare_reader(store, graph, borrowed_from=None, sync=False)

        assert reader is graph
        assert not preparation.synced
        assert preparation.adopted_moves == 0
        assert preparation.skipped_reason
        assert (_canonical(path), _preparation_state(store), _session_state(store)) == before


def test_emulate_tool_rejects_open_portal_before_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with Store(tmp_path / "store") as store:
        monkeypatch.setattr(
            store, "refresh_if_stale", lambda: pytest.fail("open_portal reached store refresh")
        )
        with pytest.raises(ValueError, match="Unknown read tool: open_portal"):
            server.emulate_tool("open_portal", {}, store=store, reader=None, borrowed_from=None)


def test_emulate_tool_rejects_private_arguments_before_preparation(tmp_path: Path) -> None:
    path, _ = build_twin_stores(tmp_path, "moved")
    reader, borrowed_from = _reader(path, "moved")
    with Store(path) as store:
        before = (_canonical(path), _preparation_state(store))
        with pytest.raises(ValidationError, match="sent"):
            server.emulate_tool(
                "retrieve_decisions",
                {"files": ["pkg/m.py"], "sent": [DECISION_ID]},
                store=store,
                reader=reader,
                borrowed_from=borrowed_from,
            )

        assert (_canonical(path), _preparation_state(store)) == before


def test_emulate_tool_accepts_mcp_argument_coercion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_path, path = build_twin_stores(tmp_path, "cold")
    args = {"seeds": "pkg/m.py", "budget_chars": "4000"}
    reader, borrowed_from = _reader(path, "cold")
    with Store(real_path) as real, Store(path) as store:
        expected = _mcp_text(monkeypatch, real, "query_decisions", args)
        result = server.emulate_tool(
            "query_decisions", args, store=store, reader=reader, borrowed_from=borrowed_from
        )
        assert result.text == expected
        assert result.budget == 4000


def test_emulate_tool_refreshes_open_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real_path, path = build_twin_stores(tmp_path, "cold")
    with Store(real_path) as real, Store(path) as store:
        for directory in (real_path, path):
            file = directory / "decisions" / f"{PROPOSAL_ID}.json"
            record = json.loads(file.read_text(encoding="utf-8"))
            record["title"] = "Refresh this proposal after the pull"
            file.write_text(json.dumps(record) + "\n", encoding="utf-8")
        actual = server.emulate_tool(
            "list_proposed", {}, store=store, reader=None, borrowed_from=None
        )
        expected = _mcp_text(monkeypatch, real, "list_proposed", {})
    assert "Refresh this proposal after the pull" in expected
    assert actual.text == expected


def test_emulate_tool_reports_overview_budget_cut(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, _ = build_twin_stores(tmp_path, "cold")
    with Store(path) as store:
        for i in range(27):
            store.add_decision(
                Decision(
                    id=f"01M2B0F19RPYSVTTWG9D6{i:05d}",
                    title=f"Budgeted listing record {i}",
                    context="Existing overview data.",
                    choice="Keep the list bounded.",
                    kind=DecisionKind.ADR,
                    status=DecisionStatus.ACCEPTED,
                    valid_from=NOW,
                    provenance=Provenance(source="manual"),
                )
            )
        args = {"limit": 100, "budget_chars": 4000}
        expected = _mcp_text(monkeypatch, store, "retrieve_decisions", args)
        result = server.emulate_tool(
            "retrieve_decisions", args, store=store, reader=None, borrowed_from=None
        )
    assert "The newest list was cut by the character budget." in expected
    assert result.text == expected
    assert result.cut == json.loads(expected)["hint"]


def test_twin_stores_pin_commit_dates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ambient_date = "2000-01-01T00:00:00+00:00"
    monkeypatch.setenv("GIT_AUTHOR_DATE", ambient_date)
    monkeypatch.setenv("GIT_COMMITTER_DATE", ambient_date)
    real, emulated = build_twin_stores(tmp_path, "synced")
    expected = str(int(NOW.timestamp()))
    assert git(real.parent, "log", "-1", "--format=%ct") == expected
    assert head(real.parent) == head(emulated.parent)
    assert os.environ["GIT_COMMITTER_DATE"] == ambient_date


@pytest.mark.parametrize(
    ("files", "budget", "cut"),
    [
        (["pkg/m.py"], 1, True),
        (["pkg/m.py", "util/n.py"], 119, True),
        (["pkg/m.py"], 119, False),
        (["pkg/m.py"], 4000, False),
    ],
)
def test_emulate_tool_reports_structure_budget_cut(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, files: list[str], budget: int, cut: bool
) -> None:
    real_path, path = build_twin_stores(tmp_path, "cold")
    reader, borrowed_from = _reader(path, "cold")
    args = {"files": files, "budget_chars": budget}
    with Store(real_path) as real, Store(path) as store:
        expected = _mcp_text(monkeypatch, real, "query_structure", args)
        result = server.emulate_tool(
            "query_structure", args, store=store, reader=reader, borrowed_from=borrowed_from
        )
    assert "fn_0()" in expected if budget > 1 else expected == "No structural context found."
    assert result.text == expected
    assert result.cut == ("The structural map reached its budget." if cut else None)


def test_emulate_drill_down_rebuilds_membership_after_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_path, path = build_twin_stores(tmp_path, "synced")
    reader, borrowed_from = _reader(path, "synced")
    maybe_sync = server.maybe_sync

    def sync_then_invalidate(store, reader, **kwargs):
        report = maybe_sync(store, reader, **kwargs)
        assert report is not None and report.skipped
        # Simulate the index losing its certification after the lazy sync checked it.
        with store._mutation():
            store._conn.execute("DELETE FROM domain_members")
            store._uncertify_membership()
        return report

    monkeypatch.setattr(server, "maybe_sync", sync_then_invalidate)
    args = {"domain_slug": "package"}
    with Store(real_path) as real, Store(path) as store:
        expected = _mcp_text(monkeypatch, real, "drill_down", args)
        result = server.emulate_tool(
            "drill_down", args, store=store, reader=reader, borrowed_from=borrowed_from
        )
        assert result.text == expected
        members = real.domain_member_ids(DOMAIN_ID)
        assert members
        assert store.domain_member_ids(DOMAIN_ID) == members


@pytest.mark.parametrize(
    ("tool", "args", "budget", "cut"),
    [
        ("get_task_context", {"files": ["pkg/m.py"], "memory_budget": 1}, 1, True),
        ("get_task_context", {"files": ["pkg/m.py"]}, 6000, False),
        ("get_entity_history", {"entity_id": ENTITY_ID, "limit": 1}, 24000, True),
        ("get_entity_history", {"entity_id": ENTITY_ID}, 24000, False),
        ("drill_down", {"domain_slug": "package", "limit": 1}, 24000, True),
        ("drill_down", {"domain_slug": "package"}, 24000, False),
        ("find_entity", {"name": "fn_0()"}, 24000, False),
        ("find_entity", {"name": "a" * 1000, "budget_chars": 1}, 4000, True),
        ("list_proposed", {"budget_chars": 4000}, 4000, True),
        ("list_proposed", {}, 24000, False),
    ],
)
def test_emulate_tool_budget_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tool: str,
    args: dict,
    budget: int,
    cut: bool,
) -> None:
    path, _ = build_twin_stores(tmp_path, "cold")
    reader, borrowed_from = _reader(path, "cold")
    with Store(path) as store:
        if tool == "find_entity":
            store.upsert_entity(
                Entity(entity_id="01M2B0F19RPYSVTTWG9D6XSJ01", canonical_name="a" * 1000)
            )
        if tool == "list_proposed":
            store.add_decision(
                Decision(
                    id="01M2B0F19RPYSVTTWG9D6XSJ02",
                    title="Try an alternative batch",
                    context="A long proposal",
                    choice="a" * 4200,
                    kind=DecisionKind.ADR,
                    status=DecisionStatus.PROPOSED,
                    valid_from=NOW,
                    provenance=Provenance(source="manual"),
                )
            )
        expected = _mcp_text(monkeypatch, store, tool, args)
        result = server.emulate_tool(
            tool, args, store=store, reader=reader, borrowed_from=borrowed_from
        )
    assert result.text == expected
    assert result.budget == budget
    expected_cut = None
    if cut:
        match tool:
            case "get_task_context":
                expected_cut = "Some records did not fit the memory budget."
            case "list_proposed":
                expected_cut = expected.partition("\n")[0]
            case _:
                expected_cut = json.loads(expected)["hint"]
    assert result.cut == expected_cut


def test_emulate_tool_reports_response_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path, _ = build_twin_stores(tmp_path, "cold")
    reader, borrowed_from = _reader(path, "cold")
    hard_cap = server._hard_cap

    def small_cap(*args, **kwargs):
        return hard_cap(*args, **kwargs, cap=200)

    monkeypatch.setattr(server, "_hard_cap", small_cap)
    args = {"files": ["pkg/m.py"]}
    with Store(path) as store:
        expected = _mcp_text(monkeypatch, store, "query_decisions", args)
        result = server.emulate_tool(
            "query_decisions", args, store=store, reader=reader, borrowed_from=borrowed_from
        )
    assert result.text == expected
    assert result.cut == "The response exceeded the 40,000-character limit."


def test_emulate_tool_reports_overview_row_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, _ = build_twin_stores(tmp_path, "cold")
    args = {"limit": 1}
    with Store(path) as store:
        expected = _mcp_text(monkeypatch, store, "retrieve_decisions", args)
        result = server.emulate_tool(
            "retrieve_decisions", args, store=store, reader=None, borrowed_from=None
        )
    overview = json.loads(expected)
    assert overview["total"] == 2
    assert len(overview["newest"]) == 1
    assert result.text == expected
    assert result.cut == "The newest list reached its record limit."


@pytest.mark.parametrize("budget", [4000, 24000])
def test_emulate_tool_reports_clipped_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, budget: int
) -> None:
    path, _ = build_twin_stores(tmp_path, "cold")
    with Store(path) as store:
        # Both paths render the same diagnostic from the same visible location.
        finding = {"code": "parse-error", "path": str(path / "decisions"), "detail": "x" * 6000}
        monkeypatch.setattr(
            server, "_verify_store_impl", lambda _: {"clean": False, "violations": [finding]}
        )
        args = {"budget_chars": budget}
        expected = _mcp_text(monkeypatch, store, "verify_store", args)
        result = server.emulate_tool(
            "verify_store", args, store=store, reader=None, borrowed_from=None
        )
    assert result.text == expected
    assert len(json.loads(expected)["violations"]) == 1
    if budget == 4000:
        assert json.loads(expected)["violations"][0]["detail"].endswith("…")
        assert result.cut == "Long violation fields were clipped to fit the budget."
    else:
        assert json.loads(expected)["violations"][0]["detail"] == finding["detail"]
        assert result.cut is None


def test_twin_stores_have_equal_path_lengths(tmp_path: Path) -> None:
    real, emulated = build_twin_stores(tmp_path, "cold")
    assert len(str(real)) == len(str(emulated))


@pytest.mark.parametrize("budget", [500, 4000])
def test_emulate_tool_reports_shortened_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, budget: int
) -> None:
    repo = make_main(tmp_path)
    reader = GraphifyReader(repo / GRAPH)
    with Store(repo / ".sidegraph") as store:
        store.upsert_entity(
            Entity(
                entity_id=ENTITY_ID,
                canonical_name="fn_0()",
                descriptor=Descriptor(name="fn_0()", file_path="pkg/m.py"),
            )
        )
        store.add_decision(
            Decision(
                id=DECISION_ID,
                title="Choose a retry policy",
                context="Context marker " + "another word " * 120,
                choice="Retry in batches.",
                kind=DecisionKind.ADR,
                status=DecisionStatus.ACCEPTED,
                valid_from=NOW,
                provenance=Provenance(source="manual"),
            )
        )
        store.add_binding(AnchorBinding(record_id=DECISION_ID, entity_id=ENTITY_ID, tier=2))
        args = {"files": ["pkg/m.py"], "budget_chars": budget}
        expected = _mcp_text(monkeypatch, store, "query_decisions", args)
        result = server.emulate_tool(
            "query_decisions", args, store=store, reader=reader, borrowed_from=None
        )
    assert result.text == expected
    assert DECISION_ID in result.text
    if budget == 500:
        assert "Context marker" not in result.text
        assert result.cut == "Some records were shortened to fit the memory budget."
    else:
        assert "Context marker" in result.text
        assert result.cut is None


@pytest.mark.parametrize("budget", [50, 90])
def test_emulate_tool_omits_domain_summary_without_reporting_a_cut(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, budget: int
) -> None:
    real_path, path = build_twin_stores(tmp_path, "synced")
    reader, borrowed_from = _reader(path, "synced")
    args = {"files": ["pkg/m.py"], "budget_chars": budget}
    with Store(real_path) as real, Store(path) as store:
        expected = _mcp_text(monkeypatch, real, "query_structure", args)
        result = server.emulate_tool(
            "query_structure", args, store=store, reader=reader, borrowed_from=borrowed_from
        )
    assert result.text == expected
    assert "fn_0()" in result.text
    assert "Package: Batch processing." not in result.text
    assert result.cut is None


def test_emulate_tool_reports_ambiguous_id_budget_cut(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = make_main(tmp_path)
    prefixes = [f"01M2B0F19RPYSVTTWG9D6{i:04d}" for i in range(4)]
    with Store(repo / ".sidegraph") as store:
        for prefix in prefixes:
            for digit in range(10):
                store.add_decision(
                    Decision(
                        id=f"{prefix}{digit}",
                        title="Test ambiguous prefixes",
                        context="A repeated choice",
                        choice="Use a full ID.",
                        kind=DecisionKind.ADR,
                        status=DecisionStatus.ACCEPTED,
                        valid_from=NOW,
                        provenance=Provenance(source="manual"),
                    )
                )
        args = {"ids": prefixes, "budget_chars": 4000}
        wide = json.loads(
            _mcp_text(monkeypatch, store, "retrieve_decisions", args | {"budget_chars": 24000})
        )
        expected = _mcp_text(monkeypatch, store, "retrieve_decisions", args)
        result = server.emulate_tool(
            "retrieve_decisions", args, store=store, reader=None, borrowed_from=None
        )
    assert result.text == expected
    narrow = json.loads(expected)
    assert len(wide["id_lookup"]) == len(narrow["id_lookup"]) == 4
    assert sum(len(note["candidates"]) for note in narrow["id_lookup"]) < sum(
        len(note["candidates"]) for note in wide["id_lookup"]
    )
    assert result.cut == "Some ID lookup candidates or notes did not fit the budget."


def test_twin_mask_normalizes_clipped_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real, emulated = build_twin_stores(tmp_path, "cold")
    monkeypatch.setattr(server, "_ECHO_CHARS", len(str(tmp_path)) + 6)
    clipped_real = server._dump({"path": server._clip(str(real / "decisions"))})
    clipped_emulated = server._dump({"path": server._clip(str(emulated / "decisions"))})
    assert clipped_real != clipped_emulated
    assert _mask(clipped_real, real, "cold") == _mask(clipped_emulated, emulated, "cold")
