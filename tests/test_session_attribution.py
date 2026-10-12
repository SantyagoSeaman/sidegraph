"""Session attribution: the MCP server names its own session instead of trusting the last
SessionStart of any host (design/superpowers/specs/2026-10-11-session-attribution-design.md)."""

from __future__ import annotations

import ast
import asyncio
import contextvars
import io
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import fastmcp
import mcp.types as mt
import pytest

import sidegraph.server as server
from sidegraph import capture
from sidegraph.config import (
    CURRENT_SESSION,
    TELEMETRY_SESSION_GROUP_KEY,
    TELEMETRY_SESSION_KEY,
    TELEMETRY_SESSION_STAMP_PREFIX,
    UNATTRIBUTED_SESSION,
    SessionHint,
)
from sidegraph.host import hooks, mcp_entry
from sidegraph.host.session_identity import bridge_codex_thread, hint_for
from sidegraph.store import Store

ROOT = Path(__file__).resolve().parent.parent
P = TELEMETRY_SESSION_STAMP_PREFIX
THREAD = "01a12cbb-555e-7ce2-8a9a-390477aabc05"
ROLLOUT = f"rollout-2026-10-11T21-50-39-{THREAD}"


def _now(age: timedelta = timedelta(0)) -> str:
    return (datetime.now(UTC) - age).isoformat()


def _stamp(store: Store, key: str, family: str, age: timedelta = timedelta(0)) -> None:
    store.set_meta(P + key, f"{_now(age)}|{family}")


def _call(
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    client: str,
    calls: list[tuple[str, dict, dict | None]],
    env: dict[str, str] | None = None,
) -> None:
    """Run tool calls through the real server with the host entry's middleware installed."""
    monkeypatch.setattr(server, "_store", store)
    monkeypatch.setenv("SIDEGRAPH_GRAPH", str(tmp_path / "no-graph.json"))
    for name, value in (env or {}).items():
        monkeypatch.setenv(name, value)
    mcp_entry.install(server.mcp)  # restored by the autouse middleware fixture

    async def run() -> None:
        async with fastmcp.Client(
            server.mcp, client_info=mt.Implementation(name=client, version="1")
        ) as c:
            for tool, args, meta in calls:
                await c.call_tool(tool, args, meta=meta)

    asyncio.run(run())


ADD = ("add_decision", {"title": "t", "kind": "adr", "context": "c", "choice": "ch"}, None)
ASK = ("get_task_context", {"files": ["src/a.py"]}, None)


def _written_session(store: Store) -> str | None:
    [decision] = list(store.iter_decisions())
    return decision.provenance.session_id


def _journal_sessions(store: Store) -> set[str]:
    return {e["session_id"] for e in store.retrieval_events()}


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / ".sidegraph")
    yield s
    s.close()


# -- T1-T3, T10: the resolver through the real middleware --------------------------------------


def test_t1_two_hosts_do_not_cross_attribute(store, monkeypatch, tmp_path):
    """Red today: both readers return the last stamper (codex-B)."""
    _stamp(store, "claude-A", "claude")
    _stamp(store, "codex-B", "codex")
    store.set_meta(TELEMETRY_SESSION_KEY, f"codex-B|{_now()}")  # what the real hook also writes

    _call(
        store,
        monkeypatch,
        tmp_path,
        "claude-code",
        [ADD, ASK],
        env={"CLAUDE_CODE_SESSION_ID": "claude-A"},
    )

    assert _written_session(store) == "claude-A"
    assert _journal_sessions(store) == {"claude-A"}


def test_t2_codex_thread_id_is_bridged_to_the_rollout_stamp(store, monkeypatch, tmp_path):
    _stamp(store, ROLLOUT, "codex")
    _stamp(store, "claude-A", "claude")
    store.set_meta(TELEMETRY_SESSION_KEY, f"claude-A|{_now()}")

    _call(store, monkeypatch, tmp_path, "codex-mcp-client", [(*ADD[:2], {"threadId": THREAD})])

    assert _written_session(store) == ROLLOUT


def test_t2_a_codex_thread_with_no_stamp_keeps_the_bare_id(store, monkeypatch, tmp_path):
    _stamp(store, "claude-A", "claude")

    _call(store, monkeypatch, tmp_path, "codex-mcp-client", [(*ADD[:2], {"threadId": THREAD})])

    assert _written_session(store) == THREAD


def test_t2_an_old_codex_stamp_still_bridges(store, monkeypatch, tmp_path):
    """The bridge needs the stamp to be retained, not fresh."""
    _stamp(store, ROLLOUT, "codex", age=timedelta(hours=20))

    _call(store, monkeypatch, tmp_path, "codex-mcp-client", [(*ADD[:2], {"threadId": THREAD})])

    assert _written_session(store) == ROLLOUT


def test_t3_an_unknown_client_ignores_an_inherited_claude_id(store, monkeypatch, tmp_path):
    """Guard, red against nothing: a Grok started from a Claude shell inherits the id."""
    _stamp(store, "claude-A", "claude")

    _call(
        store,
        monkeypatch,
        tmp_path,
        "grok",
        [ADD, ASK],
        env={"CLAUDE_CODE_SESSION_ID": "claude-A"},
    )

    assert _written_session(store) is None
    assert _journal_sessions(store) == {UNATTRIBUTED_SESSION}


def test_t4_two_live_same_family_stamps_resolve_to_nothing(store, monkeypatch, tmp_path):
    """Red today: the legacy key, which the real hook also writes, names the last stamper."""
    _stamp(store, "claude-X", "claude")
    _stamp(store, "grok-G", "claude")
    store.set_meta(TELEMETRY_SESSION_KEY, f"grok-G|{_now()}")

    _call(store, monkeypatch, tmp_path, "claude-code", [ADD, ASK])

    assert _written_session(store) is None
    assert _journal_sessions(store) == {UNATTRIBUTED_SESSION}


def test_t5_one_fresh_same_family_stamp_is_used_and_other_families_do_not_count(
    store, monkeypatch, tmp_path
):
    """Red today: today reads only the legacy key."""
    _stamp(store, "claude-X", "claude")
    _stamp(store, "codex-B", "codex")

    _call(store, monkeypatch, tmp_path, "claude-code", [ADD, ASK])

    assert _written_session(store) == "claude-X"
    assert _journal_sessions(store) == {"claude-X"}


def test_t5_a_lone_stale_stamp_names_nothing(store, monkeypatch, tmp_path):
    """Guard, red against nothing: the TTL half."""
    _stamp(store, "claude-X", "claude", age=timedelta(hours=30))

    _call(store, monkeypatch, tmp_path, "claude-code", [ADD])

    assert _written_session(store) is None


def test_t5_the_journal_window_is_shorter_than_the_provenance_window(store, monkeypatch, tmp_path):
    _stamp(store, "claude-X", "claude", age=timedelta(hours=13))

    _call(store, monkeypatch, tmp_path, "claude-code", [ADD, ASK])

    assert _written_session(store) == "claude-X"  # 24 h
    assert _journal_sessions(store) == {UNATTRIBUTED_SESSION}  # 12 h


def test_t7_legacy_only_store_still_attributes_to_the_same_family(store, monkeypatch, tmp_path):
    """Guard, red against nothing: a store whose hooks are all old."""
    store.set_meta(TELEMETRY_SESSION_KEY, f"old-A|{_now()}")

    _call(store, monkeypatch, tmp_path, "claude-code", [ADD])

    assert _written_session(store) == "old-A"


def test_the_legacy_key_is_ignored_once_any_stamp_exists(store, monkeypatch, tmp_path):
    """A legacy id written by an old Codex hook must not name a Claude call that has no
    Claude stamp."""
    _stamp(store, "codex-B", "codex")
    store.set_meta(TELEMETRY_SESSION_KEY, f"rollout-old-codex|{_now()}")

    _call(store, monkeypatch, tmp_path, "claude-code", [ADD])

    assert _written_session(store) is None


def test_t12_naive_and_future_stamps_are_not_fresh(store):
    token = CURRENT_SESSION.set(SessionHint(None, "claude"))
    try:
        store.set_meta(P + "naive", f"{datetime.now().isoformat()}|claude")
        assert capture.resolve_session(store, timedelta(hours=24)) is None
        store.set_meta(P + "naive", f"{_now(-timedelta(hours=1))}|claude")
        assert capture.resolve_session(store, timedelta(hours=24)) is None
        store.set_meta(P + "naive", f"{_now()}|claude")
        assert capture.resolve_session(store, timedelta(hours=24)) == "naive"
    finally:
        CURRENT_SESSION.reset(token)


def test_t13_a_cli_writer_next_to_a_live_stamp_writes_no_session(store):
    """Guard, red against nothing: outside the MCP middleware CURRENT_SESSION is unset."""
    _stamp(store, "claude-X", "claude")
    store.set_meta(TELEMETRY_SESSION_KEY, f"claude-X|{_now()}")

    assert capture.resolve_session(store, timedelta(hours=24)) is None
    assert capture._session_id_fallback(store) is None


# -- T10: malformed input never raises ---------------------------------------------------------


@pytest.mark.parametrize(
    "meta",
    [None, {}, {"threadId": 7}, {"threadId": ""}, {"threadId": None}, {"threadId": ["x"]}],
)
def test_t10_a_malformed_codex_meta_resolves_to_no_id(meta):
    hint = hint_for("codex-mcp-client", meta, {}, lambda: pytest.fail("no thread, no lookup"))
    assert hint == SessionHint(None, "codex")


def test_t10_a_failing_stamp_lookup_gives_the_bare_thread_id():
    def boom():
        raise RuntimeError("store would not open")

    assert hint_for("codex-mcp-client", {"threadId": THREAD}, {}, boom) == SessionHint(
        THREAD, "codex"
    )


def test_t10_claude_without_an_env_id_names_its_family_only():
    assert hint_for("claude-code", None, {}, list) == SessionHint(None, "claude")
    assert hint_for("claude-code", None, {"CLAUDE_CODE_SESSION_ID": ""}, list) == SessionHint(
        None, "claude"
    )
    assert hint_for("grok", {"threadId": THREAD}, {"CLAUDE_CODE_SESSION_ID": "x"}, list) == (
        SessionHint(None, None)
    )


def test_t10_bridge_matches_the_tail_only():
    assert bridge_codex_thread(THREAD, ["a", ROLLOUT]) == ROLLOUT
    assert bridge_codex_thread(THREAD, [THREAD]) == THREAD
    assert bridge_codex_thread(THREAD, [f"{THREAD}-extra", "x"]) == THREAD


def test_t10_a_missing_fastmcp_context_resolves_to_no_identity_and_the_hint_is_reset():
    seen = []

    async def call_next(_ctx):
        seen.append(CURRENT_SESSION.get())
        return "ok"

    def drive(coro):
        """Run a coroutine that never suspends in THIS context (asyncio.run copies it, which
        would hide a missing reset)."""
        try:
            coro.send(None)
        except StopIteration as stop:
            return stop.value
        raise AssertionError("the middleware suspended")

    context = contextvars.copy_context()
    middleware = mcp_entry._BindSession()
    result = context.run(
        drive, middleware.on_call_tool(SimpleNamespace(fastmcp_context=None), call_next)
    )

    assert result == "ok" and seen == [SessionHint(None, None)]
    assert context.run(CURRENT_SESSION.get) is None, "the hint must not outlive the call"


def test_t10_an_unreadable_request_context_sets_no_hint():
    class Broken:
        @property
        def fastmcp_context(self):
            raise RuntimeError("no context")

    async def run():
        seen = []

        async def call_next(_ctx):
            seen.append(CURRENT_SESSION.get())
            return "ok"

        await mcp_entry._BindSession().on_call_tool(Broken(), call_next)
        return seen

    assert asyncio.run(run()) == [None]


def test_t10_a_store_that_fails_to_open_yields_the_bare_thread_id(store, monkeypatch, tmp_path):
    def boom():
        raise RuntimeError("cannot open")

    monkeypatch.setattr(server, "_get_store", boom)
    seen: list[SessionHint | None] = []

    async def run():
        async def call_next(_ctx):
            seen.append(CURRENT_SESSION.get())

        fake = SimpleNamespace(
            fastmcp_context=SimpleNamespace(
                session=SimpleNamespace(
                    client_params=SimpleNamespace(
                        clientInfo=SimpleNamespace(name="codex-mcp-client")
                    )
                ),
                request_context=SimpleNamespace(
                    meta=SimpleNamespace(model_dump=lambda: {"threadId": THREAD})
                ),
            )
        )
        await mcp_entry._BindSession().on_call_tool(fake, call_next)

    asyncio.run(run())
    assert seen == [SessionHint(THREAD, "codex")]


# -- T6, T14: SessionStart stamps -------------------------------------------------------------


def _session_start(monkeypatch, capsys, db: Path, payload: dict, host: str | None = None) -> None:
    monkeypatch.setenv("SIDEGRAPH_DIR", str(db))
    if host is None:
        monkeypatch.delenv("SIDEGRAPH_HOST", raising=False)
    else:
        monkeypatch.setenv("SIDEGRAPH_HOST", host)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    hooks.session_start()
    capsys.readouterr()


@pytest.mark.parametrize(
    ("host", "family"),
    [("claude", "claude"), ("codex", "codex"), (None, "unknown"), ("grok", "unknown")],
)
def test_t6_session_start_writes_a_per_session_stamp_and_the_legacy_key(
    tmp_path, monkeypatch, capsys, host, family
):
    db = tmp_path / "db"
    _session_start(monkeypatch, capsys, db, {"session_id": "abc-123"}, host)

    s = Store(db)
    stamps = s.meta_with_prefix(P)
    legacy = s.get_meta(TELEMETRY_SESSION_KEY)
    s.close()

    assert list(stamps) == [P + "abc-123"]
    iso, _, fam = stamps[P + "abc-123"].partition("|")
    assert (
        fam == family
        and abs((datetime.now(UTC) - datetime.fromisoformat(iso)).total_seconds()) < 60
    )
    assert legacy is not None and legacy.partition("|")[0] == "abc-123"


def test_t14_an_unknown_family_stamp_never_matches_a_family_hint(store):
    _stamp(store, "mystery", "unknown")
    token = CURRENT_SESSION.set(SessionHint(None, "claude"))
    try:
        assert capture.resolve_session(store, timedelta(hours=24)) is None
    finally:
        CURRENT_SESSION.reset(token)


def test_t6_session_start_prunes_stamps_older_than_25h_and_keeps_the_rest(
    tmp_path, monkeypatch, capsys
):
    db = tmp_path / "db"
    s = Store(db)
    _stamp(s, "old", "claude", age=timedelta(hours=26))
    _stamp(s, "recent", "claude", age=timedelta(hours=24, minutes=59))
    s.set_meta(TELEMETRY_SESSION_GROUP_KEY, "umbrella")
    s.close()

    _session_start(monkeypatch, capsys, db, {"session_id": "new"}, "claude")

    s = Store(db)
    assert sorted(s.meta_with_prefix(P)) == [P + "new", P + "recent"]
    assert s.get_meta(TELEMETRY_SESSION_GROUP_KEY) == "umbrella"
    assert s.get_meta(TELEMETRY_SESSION_KEY) is not None
    s.close()


def test_t6_stamps_are_pruned_and_written_with_telemetry_off(tmp_path, monkeypatch, capsys):
    db = tmp_path / "db"
    s = Store(db)
    _stamp(s, "old", "claude", age=timedelta(hours=26))
    s.close()
    monkeypatch.setenv("SIDEGRAPH_TELEMETRY", "off")

    _session_start(monkeypatch, capsys, db, {"session_id": "new"}, "claude")
    monkeypatch.setenv("SIDEGRAPH_TELEMETRY", "off")

    s = Store(db)
    assert list(s.meta_with_prefix(P)) == [P + "new"]
    s.close()


def test_meta_with_prefix_is_a_prefix_range_that_excludes_its_neighbours(store):
    store.set_meta("telemetry:session", "x")
    store.set_meta("telemetry:session_group", "y")
    store.set_meta("telemetry:session:a", "1")
    store.set_meta("telemetry:session:b", "2")
    store.set_meta("telemetry:sessionz", "z")

    assert store.meta_with_prefix(P) == {"telemetry:session:a": "1", "telemetry:session:b": "2"}
    with pytest.raises(ValueError):
        store.meta_with_prefix("")


# -- T9: the seam -----------------------------------------------------------------------------


@pytest.mark.parametrize("module", ["server", "capture", "store", "config"])
def test_t9_core_imports_nothing_from_the_host_seam(module):
    tree = ast.parse((ROOT / "src" / "sidegraph" / f"{module}.py").read_text())
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.append(("." * node.level) + (node.module or ""))
            imported += [f"{'.' * node.level}{node.module or ''}.{a.name}" for a in node.names]
        elif isinstance(node, ast.Import):
            imported += [a.name for a in node.names]
    assert [name for name in imported if "host" in name.split(".")] == []


# -- T14: manifests ---------------------------------------------------------------------------

MANIFESTS = {
    "plugin/sidegraph/hooks/hooks.json": "claude",
    "plugin/sidegraph/hooks/hooks.public.json": "claude",
    "plugin/sidegraph/codex/hooks.json": "codex",
    "plugin/sidegraph/codex/hooks.public.json": "codex",
}


@pytest.mark.parametrize("path", sorted(MANIFESTS))
def test_t14_every_session_start_command_sets_its_host_family(path):
    if ".public." in path and not (ROOT / path).exists():
        # The release snapshot renames the public twin to hooks.json, which is checked as such.
        pytest.skip("public snapshot: the twin ships as hooks.json")
    data = json.loads((ROOT / path).read_text())
    commands = [
        hook["command"]
        for group in data["hooks"]["SessionStart"]
        for hook in group["hooks"]
        if "sidegraph-session-start" in hook["command"]
    ]
    assert commands, path
    for command in commands:
        assert f"SIDEGRAPH_HOST={MANIFESTS[path]} " in command, (path, command)


@pytest.mark.parametrize(
    "doc",
    [
        "docs/getting-started/claude-code-setup.md",
        "docs/getting-started/codex-setup.md",
        "docs/integrations/codex.md",
    ],
)
def test_t14_the_manual_recipes_set_the_host_family(doc):
    family = "codex" if "codex" in doc else "claude"
    assert f"SIDEGRAPH_HOST={family}" in (ROOT / doc).read_text()


# -- T15: entry points -----------------------------------------------------------------------


def test_t15_the_console_script_is_the_host_entry():
    text = (ROOT / "pyproject.toml").read_text()
    assert 'sidegraph-mcp = "sidegraph.host.mcp_entry:main"' in text


def test_t15_main_runs_prepare_before_the_server_and_help_before_prepare(monkeypatch, capsys):
    calls: list[str] = []
    monkeypatch.setattr(server.mcp, "run", lambda: calls.append("run"))
    monkeypatch.setattr(sys, "argv", ["sidegraph-mcp"])
    server.main(prepare=lambda mcp: calls.append("prepare"))
    assert calls == ["prepare", "run"]

    calls.clear()
    monkeypatch.setattr(sys, "argv", ["sidegraph-mcp", "--help"])
    with pytest.raises(SystemExit):
        server.main(prepare=lambda mcp: calls.append("prepare"))
    assert calls == [] and "usage: sidegraph-mcp" in capsys.readouterr().out


def test_t15_the_host_entry_installs_the_middleware(monkeypatch):
    seen: list[object] = []
    monkeypatch.setattr(server, "main", lambda *, prepare=None: seen.append(prepare))
    mcp_entry.main()
    assert seen == [mcp_entry.install]


# -- T8: over real stdio -----------------------------------------------------------------------


def _stdio_journal(tmp_path: Path, module: str, client: str, env: dict[str, str]) -> set[str]:
    from fastmcp.client.transports import StdioTransport

    sd = tmp_path / ".sidegraph"
    Store(sd).close()
    e = {
        "SIDEGRAPH_DIR": str(sd),
        "SIDEGRAPH_TELEMETRY": "on",
        "SIDEGRAPH_GRAPH": str(tmp_path / "no-graph.json"),
        **env,
    }
    transport = StdioTransport(
        command=sys.executable, args=["-I", "-m", module], env=e, cwd=str(tmp_path)
    )

    async def run() -> None:
        async with fastmcp.Client(
            transport, client_info=mt.Implementation(name=client, version="1")
        ) as c:
            await c.call_tool("get_task_context", {"files": ["src/a.py"]})

    asyncio.run(run())
    s = Store(sd)
    try:
        return _journal_sessions(s)
    finally:
        s.close()


@pytest.mark.slow
def test_t8_the_host_entry_attributes_over_real_stdio(tmp_path):
    got = _stdio_journal(
        tmp_path, "sidegraph.host.mcp_entry", "claude-code", {"CLAUDE_CODE_SESSION_ID": "claude-A"}
    )
    assert got == {"claude-A"}


@pytest.mark.slow
def test_t8_python_m_server_runs_without_host_identity(tmp_path):
    got = _stdio_journal(
        tmp_path, "sidegraph.server", "claude-code", {"CLAUDE_CODE_SESSION_ID": "claude-A"}
    )
    assert got == {UNATTRIBUTED_SESSION}


@pytest.mark.slow
def test_t15_the_installed_console_script_answers_help():
    exe = Path(sys.executable).parent / (
        "sidegraph-mcp.exe" if os.name == "nt" else "sidegraph-mcp"
    )
    out = subprocess.run([str(exe), "--help"], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0 and "usage: sidegraph-mcp" in out.stdout


def _console_script() -> Path:
    """`sidegraph-mcp` next to the interpreter: `bin/` on POSIX, `Scripts\\*.exe` on Windows."""
    name = "sidegraph-mcp.exe" if os.name == "nt" else "sidegraph-mcp"
    return Path(sys.executable).parent / name


@pytest.mark.slow
def test_t15_the_console_script_initializes_and_lists_the_tools(tmp_path):
    """The smoke variant for the moved entry: same initialize and tool list as
    `python -m sidegraph.server`."""
    from fastmcp.client.transports import StdioTransport

    env = {
        "SIDEGRAPH_DIR": str(tmp_path / ".sidegraph"),
        "SIDEGRAPH_GRAPH": str(tmp_path / "no-graph.json"),
    }
    transport = StdioTransport(command=str(_console_script()), args=[], env=env, cwd=str(tmp_path))

    async def run() -> int:
        async with fastmcp.Client(transport) as client:
            return len(await client.list_tools())

    assert asyncio.run(run()) == 24
