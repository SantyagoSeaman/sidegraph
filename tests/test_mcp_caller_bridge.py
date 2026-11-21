"""The Claude MCP caller bridge binds exact hints only for a matching hook row."""

import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from sidegraph import server
from sidegraph.config import CURRENT_SESSION, SessionHint
from sidegraph.host import mcp_entry
from sidegraph.host.session_identity import caller_row, hint_for
from sidegraph.schema import Decision, Provenance
from sidegraph.store import Store
from sidegraph.store_layout import StoreLinkRefused
from tests.graph_repo_support import git
from tests.test_cap_reset import _session_start
from tests.test_host_pretool_records import SESSION, Project, _payload, _run

ENV = {"CLAUDE_CODE_SESSION_ID": "s1"}
META = {"claudecode/toolUseId": "toolu_1"}
STAMP = "2026-11-20T00:00:00+00:00"
MAIN_ROW = f"{STAMP}|s1|-"
CALLER_KEY = "mcp_caller:toolu_1"


def test_t1_matching_row_binds_main_agent_exactly():
    hint = hint_for("claude-code", META, ENV, list, caller=lambda _: MAIN_ROW)
    assert hint == SessionHint("s1", "claude", agent=None, agent_exact=True)


def test_t2_row_from_another_session_leaves_the_env_hint_inexact():
    hint = hint_for("claude-code", META, ENV, list, caller=lambda _: f"{STAMP}|s2|-")
    assert hint == SessionHint("s1", "claude")


@pytest.mark.parametrize(
    "row", [None, "", "garbage", f"{STAMP}||-", f"{STAMP}|s1|", f"{STAMP}|s1|a7|extra"]
)
def test_t3_missing_or_malformed_row_leaves_the_env_hint_inexact(row):
    hint = hint_for("claude-code", META, ENV, list, caller=lambda _: row)
    assert hint == SessionHint("s1", "claude")


@pytest.mark.parametrize(
    ("meta", "env", "expected"),
    [({}, ENV, "s1"), ({"claudecode/toolUseId": ""}, ENV, "s1"), (META, {}, None)],
)
def test_t3_incomplete_caller_identity_skips_the_lookup(meta, env, expected):
    calls = []
    hint = hint_for("claude-code", meta, env, list, caller=calls.append)
    assert hint == SessionHint(expected, "claude")
    assert calls == []


def test_t3_lookup_failure_leaves_the_env_hint_inexact():
    def lookup(_tool_use_id):
        raise RuntimeError("index unavailable")

    hint = hint_for("claude-code", META, ENV, list, caller=lookup)
    assert hint == SessionHint("s1", "claude")


def test_t4_matching_row_binds_the_child_agent_exactly():
    hint = hint_for("claude-code", META, ENV, list, caller=lambda _: f"{STAMP}|s1|a7")
    assert hint == SessionHint("s1", "claude", agent="a7", agent_exact=True)


@pytest.mark.parametrize(("agent", "encoded"), [(None, "-"), ("a7", "a7")])
def test_t8_bridge_writer_format_is_readable(agent, encoded):
    row = caller_row("s1", agent, datetime(2026, 11, 20, tzinfo=UTC))
    assert row == f"{STAMP}|s1|{encoded}"
    assert hint_for("claude-code", META, ENV, list, caller=lambda _: row) == SessionHint(
        "s1", "claude", agent=agent, agent_exact=True
    )


def _bound_hint(tool, *, run_tool=None, tool_use_id="toolu_1"):
    fake = SimpleNamespace(
        message=SimpleNamespace(name=tool),
        fastmcp_context=SimpleNamespace(
            session=SimpleNamespace(
                client_params=SimpleNamespace(clientInfo=SimpleNamespace(name="claude-code"))
            ),
            request_context=SimpleNamespace(
                meta=SimpleNamespace(model_dump=lambda: {"claudecode/toolUseId": tool_use_id})
            ),
        ),
    )

    async def call_next(_ctx):
        if run_tool is not None:
            run_tool()
        return CURRENT_SESSION.get()

    return asyncio.run(mcp_entry._BindSession().on_call_tool(fake, call_next))


@pytest.mark.parametrize("tool", ["get_task_context", "query_decisions", "retrieve_decisions"])
@pytest.mark.parametrize("read_fails", [False, True])
def test_t8_middleware_binds_the_matching_row_or_falls_back(monkeypatch, tool, read_fails):
    calls = []

    def lookup(tool_use_id):
        calls.append(tool_use_id)
        if read_fails:
            raise RuntimeError("index unavailable")
        return MAIN_ROW

    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s1")
    monkeypatch.setattr(mcp_entry, "_stored_caller", lookup)
    assert _bound_hint(tool) == SessionHint("s1", "claude", agent_exact=not read_fails)
    assert calls == ["toolu_1"]


@pytest.mark.parametrize(
    ("tool", "env_session"), [("add_decision", "s1"), ("get_task_context", None)]
)
def test_t8b_middleware_skips_the_lookup_outside_the_bridge(monkeypatch, tool, env_session):
    calls = []
    if env_session is None:
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    else:
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", env_session)
    monkeypatch.setattr(mcp_entry, "_stored_caller", calls.append)
    assert _bound_hint(tool) == SessionHint(env_session, "claude")
    assert calls == []


@pytest.fixture
def caller_store_dir(tmp_path, monkeypatch):
    path = tmp_path / ".sidegraph"
    path.mkdir()
    (path / "format").write_text("sidegraph-store 1.0.0\nrequires-sidegraph >= 0.17.0\n")
    with Store(path) as store:
        store.set_meta(CALLER_KEY, MAIN_ROW)
    monkeypatch.setenv("SIDEGRAPH_DIR", str(path))
    monkeypatch.setattr(server, "_store", None)
    return path


def test_t8_reads_an_open_store_and_a_cold_store_without_writing(caller_store_dir, monkeypatch):
    with Store(caller_store_dir) as store, monkeypatch.context() as warm:
        warm.setattr(server, "_store", store)
        assert mcp_entry._stored_caller("toolu_1") == MAIN_ROW

    def snapshot():
        return {
            p.relative_to(caller_store_dir): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in caller_store_dir.rglob("*")
            if p.is_file()
        }

    before = snapshot()
    assert mcp_entry._stored_caller("toolu_1") == MAIN_ROW
    assert mcp_entry._stored_caller("toolu_missing") is None
    assert snapshot() == before


def test_t8_cold_bridge_ignores_an_index_without_a_store(caller_store_dir):
    (caller_store_dir / "format").unlink()
    assert (caller_store_dir / "index.db").is_file()
    assert mcp_entry._stored_caller("toolu_1") is None


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need a privilege")
def test_t8_cold_bridge_ignores_a_symlinked_index(caller_store_dir):
    index = caller_store_dir / "index.db"
    index.symlink_to(index.rename(caller_store_dir / "linked.db"))
    assert mcp_entry._stored_caller("toolu_1") is None


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need a privilege")
def test_t8_cold_bridge_refuses_a_store_link_outside_the_repo(caller_store_dir, monkeypatch):
    repo = caller_store_dir.parent / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    link = repo / ".sidegraph"
    link.symlink_to(caller_store_dir, target_is_directory=True)
    monkeypatch.setenv("SIDEGRAPH_DIR", str(link))
    with pytest.raises(StoreLinkRefused, match="leaves its repository"):
        mcp_entry._stored_caller("toolu_1")


@pytest.mark.parametrize("cold", [False, True], ids=["warm", "cold"])
def test_t8_blob_row_cannot_bind_an_exact_caller_or_write_sent_marks(tmp_path, monkeypatch, cold):
    path = tmp_path / ".sidegraph"
    monkeypatch.setenv("SIDEGRAPH_DIR", str(path))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s1")

    def listing():
        answer = json.loads(server.retrieve_decisions(kind="adr").content[0].text)
        assert answer["decisions"][0]["choice"] == "Full body"

    with Store(path) as store:
        store.add_decision(
            Decision(
                title="Bridge test",
                kind="adr",
                context="A malformed bridge row",
                choice="Full body",
                status="accepted",
                valid_from=datetime.now(UTC),
                provenance=Provenance(source="manual"),
            )
        )
        store.set_meta(CALLER_KEY, MAIN_ROW.encode())  # SQLite BLOB, not TEXT.
        monkeypatch.setattr(server, "_store", None if cold else store)
        try:
            hint = _bound_hint("retrieve_decisions", run_tool=listing)
            assert (hint, store.meta_with_prefix("mcp_sent:")) == (SessionHint("s1", "claude"), {})
        finally:
            if cold and server._store is not None:
                server._store.close()


@pytest.fixture
def bridge_project(tmp_path):
    project = Project(tmp_path / "project")
    yield project
    project.close()


@pytest.mark.parametrize(
    ("tool", "agent", "encoded"),
    [
        ("mcp__plugin_sidegraph_sidegraph__get_task_context", "a7", "a7"),
        ("mcp__sidegraph__retrieve_decisions", None, "-"),
        ("mcp__sidegraph__query_decisions", None, "-"),
    ],
)
def test_t5_hook_claims_the_callers_row(bridge_project, monkeypatch, capsys, tool, agent, encoded):
    payload = _payload(tool, agent=agent)
    payload.update(tool_use_id="toolu_1", transcript_path=f"/tmp/{SESSION}.jsonl")
    assert _run(monkeypatch, capsys, bridge_project, payload, {"SIDEGRAPH_HOST": "claude"}) == {}
    raw = bridge_project.store.get_meta(CALLER_KEY)
    assert raw is not None
    stamp, session, caller = raw.split("|")
    assert datetime.fromisoformat(stamp).utcoffset() == UTC.utcoffset(None)
    assert (session, caller) == (SESSION, encoded)

    assert _run(monkeypatch, capsys, bridge_project, {**payload, "agent_id": "another"}) == {}
    assert bridge_project.store.get_meta(CALLER_KEY) == raw


def test_t6_caller_row_hook_emits_no_delivery_run(bridge_project, monkeypatch, capsys):
    payload = {**_payload("mcp__sidegraph__get_task_context"), "tool_use_id": "toolu_1"}
    assert _run(monkeypatch, capsys, bridge_project, payload) == {}
    log = bridge_project.store_dir / "index.db-hooks.log"
    assert not log.exists() or not log.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("host", "tool", "use_id", "session"),
    [
        ("grok", "mcp__sidegraph__get_task_context", "toolu_1", SESSION),
        ("claude", "mcp__sidegraph__add_decision", "toolu_1", SESSION),
        ("claude", "mcp__other__get_task_context", "toolu_1", SESSION),
        ("claude", "mcp__sidegraph__get_task_context", None, SESSION),
        ("claude", "mcp__sidegraph__get_task_context", "toolu_1", ""),
    ],
)
def test_t7_hook_skips_calls_without_a_supported_caller(
    bridge_project, monkeypatch, capsys, host, tool, use_id, session
):
    payload = _payload(tool, session=session)
    if use_id is not None:
        payload["tool_use_id"] = use_id
    assert _run(monkeypatch, capsys, bridge_project, payload, {"SIDEGRAPH_HOST": host}) == {}
    assert bridge_project.store.meta_with_prefix("mcp_caller:") == {}


def test_t7_hook_with_no_index_does_not_create_one(bridge_project, monkeypatch, capsys):
    path = bridge_project.root / "without-index"
    path.mkdir()
    payload = {**_payload("mcp__sidegraph__get_task_context"), "tool_use_id": "toolu_1"}
    assert _run(monkeypatch, capsys, bridge_project, payload, {"SIDEGRAPH_DIR": str(path)}) == {}
    assert list(path.iterdir()) == []


def test_t9_hook_rows_keep_mcp_deliveries_separate_and_compaction_restores_bodies(
    bridge_project, monkeypatch, capsys
):
    record = bridge_project.record(
        "src/bridge.py", "Bridge record", choice="Use one key per agent."
    )
    monkeypatch.setattr(server, "_store", bridge_project.store)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SESSION)
    monkeypatch.setenv("SIDEGRAPH_GRAPH", str(bridge_project.root / "no-graph.json"))

    def read(tool_use_id, agent=None):
        payload = _payload("mcp__sidegraph__get_task_context", agent=agent)
        assert (
            _run(monkeypatch, capsys, bridge_project, {**payload, "tool_use_id": tool_use_id}) == {}
        )
        answer = []

        def get_context():
            result = server.get_task_context(
                entities=[{"name": "Sym1", "file_path": "src/bridge.py"}]
            )
            answer.append(result)

        _bound_hint("get_task_context", run_tool=get_context, tool_use_id=tool_use_id)
        [text] = answer
        assert record.title in text
        return text

    pointer = f"[sent earlier] {record.title}"
    full_body = f"{record.title}: {record.choice} (context: {record.context})"
    first_parent = read("toolu_parent_first")
    assert full_body in first_parent and pointer not in first_parent
    assert pointer in read("toolu_parent_second")
    first_child = read("toolu_child_first", "a7")
    assert full_body in first_child and pointer not in first_child
    assert pointer in read("toolu_parent_after_child")
    _session_start(monkeypatch, capsys, bridge_project, "compact")
    after_compact = read("toolu_parent_after_compact")
    assert full_body in after_compact and pointer not in after_compact
    assert pointer in read("toolu_child_after_parent_compact", "a7")


@pytest.mark.parametrize(
    "manifest",
    sorted((Path(__file__).resolve().parents[1] / "plugin/sidegraph/hooks").glob("hooks*.json")),
    ids=lambda path: path.name,
)
def test_t13_claude_manifest_uses_the_shared_hook_for_mcp_reads(manifest):
    groups = json.loads(manifest.read_text(encoding="utf-8"))["hooks"]["PreToolUse"]
    mcp_groups = [group for group in groups if group.get("matcher", "").startswith("mcp__")]
    assert len(mcp_groups) == 1
    [mcp] = mcp_groups
    assert mcp["matcher"] == (
        "mcp__plugin_sidegraph_sidegraph__get_task_context|"
        "mcp__plugin_sidegraph_sidegraph__query_decisions|"
        "mcp__plugin_sidegraph_sidegraph__retrieve_decisions"
    )
    [read] = [group for group in groups if group.get("matcher") == "Read|Grep|Edit|Write"]
    assert mcp["hooks"] == read["hooks"]
    assert groups[groups.index(mcp) - 1]["matcher"] == "Agent|Task"
