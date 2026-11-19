"""Hook entry-point parity and session-state isolation (Portal design, D10)."""

import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sidegraph.engine.reader import GraphifyReader
from sidegraph.host import emulation, hooks, launch
from sidegraph.host.emulation import WIRED_ON, emulate_hook
from sidegraph.retrieval import DRIFT_CACHE_KEY, TOC_CACHE_KEY
from sidegraph.store import Store
from sidegraph.store_layout import requirement_marker_text
from tests.graph_repo_support import GRAPH, git
from tests.store_record_support import _decision
from tests.test_grok_session_map import _initialize
from tests.test_hook_contention import _make_drift_repo
from tests.test_server_tool_emulation import _canonical, _preparation_state, _session_state
from tests.twin_support import ENTITY_ID, FACT_ID, build_twin_stores

_FAMILIES = ("claude", "codex", "grok", "muse")
_WIRING = {
    "session_start": _FAMILIES,
    "pre_tool_use": ("claude", "grok"),
    "agent_brief": ("claude", "grok"),
    "subagent_start": _FAMILIES,
    "stop": ("claude", "codex", "grok"),
    "mcp_instructions": ("grok",),
}
_ENTRY_POINTS = {
    "session_start": ("hooks", "session_start", "SessionStart"),
    "pre_tool_use": ("hooks", "pre_tool_use", "PreToolUse"),
    "agent_brief": ("hooks", "pre_tool_use", "PreToolUse"),
    "subagent_start": ("subagent", "subagent_start", "SubagentStart"),
    "stop": ("hooks", "stop", "Stop"),
}
_PARAMS = {
    "pre_tool_use": {"file_path": "pkg/m.py"},
    "agent_brief": {"prompt": "Review pkg/m.py"},
}


@pytest.fixture(autouse=True)
def _isolate_host_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("CLAUDE_PROJECT_DIR", "CLAUDE_PLUGIN_ROOT", "CLAUDE_CONFIG_DIR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("SIDEGRAPH_TELEMETRY", "on")


def _stop_payload(root: Path, family: str) -> dict:
    transcript = root / "scratch-transcript.jsonl"
    if family == "codex":
        rows = [
            {"type": "session_meta", "payload": {"source": "cli", "originator": "codex_cli_rs"}},
            *[
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": prompt}],
                    },
                }
                for prompt in ("Review the batch.", "Check the retry.")
            ],
        ]
    elif family == "grok":
        rows = [
            {
                "method": "session/update",
                "params": {
                    "update": {
                        "sessionUpdate": kind,
                        "content": {"type": "text", "text": text},
                    }
                },
            }
            for kind, text in (
                ("user_message_chunk", "Review the batch."),
                ("agent_message_chunk", "Done."),
                ("user_message_chunk", "Check the retry."),
            )
        ]
    else:
        rows = [
            {"type": "user", "message": {"role": "user", "content": prompt}}
            for prompt in ("Review the batch.", "Check the retry.")
        ]
    transcript.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return {"transcript_path": str(transcript), "stop_hook_active": False, "reason": "end_turn"}


def _entry_text(
    store_path: Path,
    event: str,
    family: str,
    params: dict,
    *,
    project_root: Path | None = None,
    expect_diagnostic: bool = False,
) -> str:
    root = project_root or store_path.parent
    if event == "mcp_instructions":
        return _initialize(root, store_path, "grok-shell-sidegraph") or ""
    module, function, hook_name = _ENTRY_POINTS[event]
    payload: dict = {"hook_event_name": hook_name, "session_id": "fresh-hook", "cwd": str(root)}
    if event == "session_start":
        payload.update(params)
    elif event == "pre_tool_use":
        file_path = params["file_path"]
        if family == "grok":
            payload.update(tool_name="read_file", tool_input={"target_file": file_path})
        else:
            payload.update(tool_name="Read", tool_input={"file_path": str(root / file_path)})
    elif event == "agent_brief":
        payload.update(
            tool_name="spawn_subagent" if family == "grok" else "Agent",
            tool_input={"prompt": params["prompt"]},
        )
    elif event == "stop":
        payload.update(_stop_payload(root, family))
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_PROJECT_DIR"}
    if family in ("claude", "muse"):
        env["CLAUDE_PROJECT_DIR"] = str(root)
    done = subprocess.run(
        [sys.executable, "-c", f"from sidegraph.host.{module} import {function}; {function}()"],
        cwd=root,
        env={
            **env,
            "SIDEGRAPH_DIR": (store_path.name if store_path.parent == root else str(store_path)),
            "SIDEGRAPH_HOST": family,
        },
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    if expect_diagnostic:
        assert json.loads(done.stderr)["sidegraph_hook_error"]
    else:
        assert not done.stderr, done.stderr
    answer = json.loads(done.stdout)
    output = answer.get("hookSpecificOutput", {})
    if event == "agent_brief" and output:
        return output["updatedInput"]["prompt"]
    return output.get("additionalContext", answer.get("reason", answer.get("systemMessage", "")))


_PARITY_CASES = [
    pytest.param(
        event,
        family,
        state,
        source,
        id=f"{event}-{family}-{state}" + (f"-{source}" if source else ""),
    )
    for event, families in _WIRING.items()
    for family in families
    for state in ("cold", "synced")
    for source in (("startup", "resume", "compact") if event == "session_start" else (None,))
]


@pytest.mark.parametrize(("event", "family", "state", "source"), _PARITY_CASES)
def test_emulate_hook_equals_entry_point(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    event: str,
    family: str,
    state: str,
    source: str | None,
) -> None:
    real, emulated = build_twin_stores(tmp_path, state)
    params = {"source": source} if source else _PARAMS.get(event, {})
    before = _canonical(emulated)
    expected = _entry_text(real, event, family, params)
    monkeypatch.chdir(emulated.parent)
    actual = emulate_hook(
        event,
        params,
        store_path=emulated,
        reader=GraphifyReader(emulated.parent / GRAPH),
        borrowed_from=None,
        host_family=family,
    )

    def mask(text: str, store_path: Path) -> str:
        return text.replace(str(store_path.parent), "<repo>")

    expect_emitted = state == "synced" or event in ("session_start", "stop", "mcp_instructions")
    assert bool(expected) is expect_emitted
    assert actual.emitted is expect_emitted
    assert mask(actual.text, emulated) == mask(expected, real)
    assert _canonical(emulated) == _canonical(real) == before
    if event == "session_start":
        with Store(emulated) as preview_store, Store(real) as real_store:
            assert _preparation_state(preview_store) == _preparation_state(real_store)


@pytest.mark.parametrize(
    ("event", "family"),
    [
        (event, family)
        for event, wired in _WIRING.items()
        for family in _FAMILIES
        if family not in wired
    ],
)
def test_not_wired_surfaces(tmp_path: Path, event: str, family: str) -> None:
    store_path = tmp_path / ".sidegraph"
    actual = emulate_hook(
        event, {}, store_path=store_path, reader=None, borrowed_from=None, host_family=family
    )
    assert not actual.emitted
    assert actual.text == ""
    assert actual.not_emitted_reason
    assert not store_path.exists()


@pytest.mark.parametrize("event", _WIRING)
def test_emulate_hook_writes_no_session_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, event: str
) -> None:
    _, store_path = build_twin_stores(tmp_path, "synced")
    monkeypatch.chdir(store_path.parent)
    log = store_path / "index.db-hooks.log"
    log.write_text("Previous hook run\n", encoding="utf-8")
    launch_path = launch.launch_commit_path()
    launch_path.parent.mkdir(parents=True)
    launch_path.write_text("1" * 40 + "\n", encoding="utf-8")
    monkeypatch.setattr(launch, "_resolved_main_commit", lambda: "2" * 40)
    family = "grok" if event == "mcp_instructions" else "claude"
    with Store(store_path) as store:
        store.set_meta("pretool_file:old:-:pkg/m.py", "2020-01-01T00:00:00+00:00")
        before = _session_state(store)
        result = emulate_hook(
            event,
            _PARAMS.get(event, {}),
            store_path=store_path,
            reader=GraphifyReader(store_path.parent / GRAPH),
            borrowed_from=None,
            host_family=family,
        )
        assert result.emitted
        assert _session_state(store) == before
    assert log.read_text(encoding="utf-8") == "Previous hook run\n"
    assert launch_path.read_text(encoding="utf-8") == "1" * 40 + "\n"


def test_firing_conditions_listed(tmp_path: Path) -> None:
    assert {event: frozenset(families) for event, families in _WIRING.items()} == WIRED_ON
    for event, families in _WIRING.items():
        result = emulate_hook(
            event,
            {},
            store_path=tmp_path / ".sidegraph",
            reader=None,
            borrowed_from=None,
            host_family=families[0],
        )
        assert result.firing_conditions
        assert all(result.firing_conditions)


@pytest.mark.parametrize("event", _WIRING)
@pytest.mark.parametrize("empty_directory", [False, True])
def test_missing_store_is_never_initialized(
    tmp_path: Path, event: str, empty_directory: bool
) -> None:
    path = tmp_path / ".sidegraph"
    if empty_directory:
        path.mkdir()
    result = emulate_hook(
        event,
        _PARAMS.get(event, {}),
        store_path=path,
        reader=None,
        borrowed_from=None,
        host_family=_WIRING[event][0],
    )
    assert path.exists() is empty_directory
    if empty_directory:
        assert not list(path.iterdir())
    if event == "session_start":
        assert result.emitted and "Run sidegraph-init" in result.text
    elif event == "mcp_instructions" and empty_directory:
        assert result.emitted
        assert result.text == _initialize(tmp_path, path, "grok-shell-sidegraph")
    else:
        assert not result.emitted and result.not_emitted_reason


@pytest.mark.parametrize("event", ["session_start", "stop"])
def test_emulate_hook_does_not_recreate_moved_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, event: str
) -> None:
    path = tmp_path / ".sidegraph"
    with Store(path):
        pass
    monkeypatch.setenv("SIDEGRAPH_DIR", str(path))
    monkeypatch.delenv("SIDEGRAPH_CAPTURE_NUDGE", raising=False)
    moved = tmp_path / "moved"
    recognize = emulation._has_existing_store

    def move_after_recognition(store_path: Path) -> bool:
        recognized = recognize(store_path)
        store_path.rename(moved)
        return recognized

    monkeypatch.setattr(emulation, "_has_existing_store", move_after_recognition)
    result = emulate_hook(
        event,
        {},
        store_path=path,
        reader=None,
        borrowed_from=None,
        host_family="claude",
        sync=False,
    )

    assert (moved / "format").is_file()
    assert not (path / "format").exists()
    assert not path.exists()
    if event == "session_start":
        assert result.emitted and "store moved or missing" in result.text
    else:
        assert not result.emitted and result.not_emitted_reason


@pytest.mark.parametrize("event", ["session_start", "stop"])
@pytest.mark.parametrize("sync", [False, True])
@pytest.mark.parametrize(
    "problem",
    ["future-version", "legacy", "bare-legacy", "bad-legacy", "corrupt-index", "interrupted"],
)
def test_refused_store_matches_entry_point(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, event: str, sync: bool, problem: str
) -> None:
    path = tmp_path / ".sidegraph"
    if problem == "future-version":
        path.mkdir()
        (path / "format").write_text(requirement_marker_text("99.0.0"), encoding="utf-8")
    elif problem in ("legacy", "bare-legacy"):
        if problem == "legacy":
            path.mkdir()
        db_path = path / "decisions.db" if path.is_dir() else path
        with sqlite3.connect(db_path) as db:
            db.execute("CREATE TABLE meta (key TEXT, value TEXT)")
        db.close()
    elif problem == "bad-legacy":
        path.mkdir()
        (path / "decisions.db").write_bytes(b"This is not a legacy store.\n")
    elif problem == "corrupt-index":
        Store(path).close()
        (path / "index.db").write_bytes(b"This is not a SQLite index.\n")
    else:
        path.with_name(".sidegraph.migration-incomplete").write_text(
            '{"staging": "staged-export", "backup": "backup.sqlite"}\n', encoding="utf-8"
        )
    monkeypatch.chdir(tmp_path)
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    result = emulate_hook(
        event,
        {},
        store_path=path,
        reader=None,
        borrowed_from=None,
        host_family="claude",
        sync=sync,
    )
    assert {
        p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()
    } == before
    expected = _entry_text(
        path,
        event,
        "claude",
        {},
        expect_diagnostic=problem in ("legacy", "bare-legacy", "bad-legacy", "corrupt-index")
        or (problem == "interrupted" and event == "session_start"),
    )
    assert result.text == expected
    assert result.emitted is (event == "session_start")


@pytest.mark.parametrize(
    "layout", ["subproject", "external", "external-explicit", "other-repo", "nested-explicit"]
)
@pytest.mark.parametrize("event", ["pre_tool_use", "agent_brief"])
def test_hot_hook_paths_follow_the_store_location(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, layout: str, event: str
) -> None:
    real, emulated = build_twin_stores(tmp_path, "synced")
    real_root, preview_root = real.parent, emulated.parent
    if layout.startswith("external") or layout == "other-repo":
        real = real.rename(tmp_path / "real-store")
        emulated = emulated.rename(tmp_path / "preview-store")
        if layout == "external":
            monkeypatch.setenv("SIDEGRAPH_DIR", str(emulated))
        if layout == "other-repo":
            git(tmp_path, "init", "-q")
    elif layout == "nested-explicit":
        for root in (real_root, preview_root):
            (root / ".config").mkdir()
        real = real.rename(real_root / ".config" / "store")
        emulated = emulated.rename(preview_root / ".config" / "store")
    else:
        git(tmp_path, "init", "-q")
        shutil.rmtree(real_root / ".git")
        shutil.rmtree(preview_root / ".git")
    monkeypatch.chdir(preview_root)

    def params(root: Path) -> dict:
        file = root / "pkg/m.py"
        return {"file_path": str(file)} if event == "pre_tool_use" else {"prompt": f"Review {file}"}

    expected = _entry_text(real, event, "claude", params(real_root), project_root=real_root)
    actual = emulate_hook(
        event,
        params(preview_root),
        store_path=emulated,
        reader=None,
        borrowed_from=None,
        host_family="claude",
    )
    assert expected and actual.emitted
    assert actual.text.replace(str(preview_root), "<repo>") == expected.replace(
        str(real_root), "<repo>"
    )


@pytest.mark.parametrize("family", ["claude", "codex", "muse"])
def test_session_start_uses_hook_store_spelling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, family: str
) -> None:
    real, preview = build_twin_stores(tmp_path, "synced")
    old = (datetime.now(UTC) - timedelta(days=2)).timestamp()
    for path in (real, preview):
        fact = path / "facts" / f"{FACT_ID}.json"
        body = json.loads(fact.read_text(encoding="utf-8"))
        fact.write_text(json.dumps({**body, "source": "Uncommitted correction"}) + "\n")
        os.utime(fact, (old, old))
    monkeypatch.chdir(preview.parent)
    if family == "codex":
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(preview.parent))
    expected = _entry_text(real, "session_start", family, {})
    spelling = ".sidegraph" if family == "codex" else str(real)
    assert f"store file(s) in {spelling} are not committed" in expected
    result = emulate_hook(
        "session_start",
        {},
        store_path=preview,
        reader=GraphifyReader(preview.parent / GRAPH),
        borrowed_from=None,
        host_family=family,
    )
    assert result.text.replace(str(preview.parent), "<repo>") == expected.replace(
        str(real.parent), "<repo>"
    )


@pytest.mark.parametrize("event", ["session_start", "subagent_start"])
def test_grok_discarded_hook_still_shows_raw_output(tmp_path: Path, event: str) -> None:
    _, path = build_twin_stores(tmp_path, "synced")
    result = emulate_hook(
        event,
        {},
        store_path=path,
        reader=None,
        borrowed_from=None,
        host_family="grok",
    )
    assert result.emitted and result.text
    assert "Grok discards this output." in result.firing_conditions


def test_grok_delivery_conditions(tmp_path: Path) -> None:
    _, path = build_twin_stores(tmp_path, "synced")

    def conditions(event: str, family: str) -> list[str]:
        return emulate_hook(
            event,
            _PARAMS.get(event, {}),
            store_path=path,
            reader=None,
            borrowed_from=None,
            host_family=family,
        ).firing_conditions

    assert any("auto mode" in condition for condition in conditions("agent_brief", "claude"))
    assert all("auto mode" not in condition for condition in conditions("agent_brief", "grok"))
    assert any("end_turn" in condition for condition in conditions("stop", "grok"))
    assert any("2-second" in condition for condition in conditions("mcp_instructions", "grok"))


def test_agent_brief_assumes_eligible_permission_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, path = build_twin_stores(tmp_path, "synced")
    monkeypatch.chdir(path.parent)
    result = emulate_hook(
        "agent_brief",
        {**_PARAMS["agent_brief"], "permission_mode": "auto"},
        store_path=path,
        reader=None,
        borrowed_from=None,
        host_family="claude",
    )
    assert result.emitted
    assert result.text.startswith(_PARAMS["agent_brief"]["prompt"] + "\n\n")


@pytest.mark.parametrize("event", ["session_start", "stop"])
def test_drift_refresh_matches_entry_point(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, event: str
) -> None:
    holder = tmp_path / "real"
    holder.mkdir()
    root, real, old_cache = _make_drift_repo(holder, cold=False)
    preview_root = tmp_path / "view" / "repo"
    shutil.copytree(root, preview_root)
    preview = preview_root / real.name
    monkeypatch.chdir(preview_root)
    expected = _entry_text(real, event, "claude", {})
    clause = (
        "1 record(s) need content review" if event == "session_start" else "1 drifted record(s)"
    )
    assert clause in expected
    actual = emulate_hook(
        event,
        {},
        store_path=preview,
        reader=None,
        borrowed_from=None,
        host_family="claude",
    )
    assert actual.text.replace(str(preview_root), "<repo>") == expected.replace(str(root), "<repo>")
    with Store(real) as observed, Store(preview) as emulated:
        assert emulated.get_meta(DRIFT_CACHE_KEY) != old_cache
        assert (
            json.loads(emulated.get_meta(DRIFT_CACHE_KEY))["by_commit"]
            == json.loads(observed.get_meta(DRIFT_CACHE_KEY))["by_commit"]
        )


def test_session_start_adopts_a_move_without_session_writes(tmp_path: Path) -> None:
    real, path = build_twin_stores(tmp_path, "moved")
    before = _canonical(path)
    log = path / "index.db-hooks.log"
    launch_path = launch.launch_commit_path()
    with Store(path) as store:
        before_session = _session_state(store)
    expected = _entry_text(real, "session_start", "claude", {})
    result = emulate_hook(
        "session_start",
        {},
        store_path=path,
        reader=GraphifyReader(path.parent / GRAPH),
        borrowed_from=None,
        host_family="claude",
    )
    after = _canonical(path)
    assert result.text.replace(str(path.parent), "<repo>") == expected.replace(
        str(real.parent), "<repo>"
    )
    assert result.prepared.synced and result.prepared.adopted_moves == 1
    assert after == _canonical(real)
    assert {key for key, data in after.items() if data != before.get(key)} == {
        Path(f"entities/{ENTITY_ID}.json")
    }
    with Store(real) as actual_store, Store(path) as emulated:
        assert _preparation_state(emulated) == _preparation_state(actual_store)
        assert _session_state(emulated) == before_session
    assert not log.exists()
    assert not launch_path.exists()


def test_grok_map_cut_at_2000(tmp_path: Path) -> None:
    store_path = tmp_path / ".sidegraph"
    with Store(store_path) as store:
        store.set_meta(
            TOC_CACHE_KEY,
            json.dumps(
                {
                    "domains": [
                        {"title": f"Domain {i}", "summary": "Code decisions.", "mistakes": 1}
                        for i in range(90)
                    ]
                }
            ),
        )
    result = emulate_hook(
        "mcp_instructions",
        {},
        store_path=store_path,
        reader=None,
        borrowed_from=None,
        host_family="grok",
    )
    assert result.text == _initialize(tmp_path, store_path, "grok-shell-sidegraph")
    assert 0 < len(result.text) <= 2000
    assert "Domain 0" in result.text and "Domain 89" not in result.text


@pytest.mark.parametrize("event", ["session_start", "stop"])
def test_sync_false_no_preparation(tmp_path: Path, event: str) -> None:
    _, path = build_twin_stores(tmp_path, "moved")
    with Store(path) as store:
        before = (_canonical(path), _preparation_state(store))
        result = emulate_hook(
            event,
            {},
            store_path=path,
            reader=GraphifyReader(path.parent / GRAPH),
            borrowed_from=None,
            host_family="claude",
            sync=False,
        )
        assert (_canonical(path), _preparation_state(store)) == before
    assert result.emitted
    assert not result.prepared.synced
    assert result.prepared.adopted_moves == 0
    assert result.prepared.skipped_reason == "The CLI graph differs from the agent's graph."


def test_unreadable_override_does_not_diagnose_the_default_graph(tmp_path: Path) -> None:
    _, path = build_twin_stores(tmp_path, "synced")
    result = emulate_hook(
        "session_start",
        {},
        store_path=path,
        reader=None,
        borrowed_from=None,
        host_family="claude",
        sync=False,
    )
    assert result.emitted and result.prepared.skipped_reason
    assert "could not be read" not in result.text


@pytest.mark.parametrize("sync", [False, True])
def test_session_start_render_failure_does_not_diagnose_store_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], sync: bool
) -> None:
    _, path = build_twin_stores(tmp_path, "synced")
    monkeypatch.chdir(path.parent)

    def fail_render(*args: object) -> None:
        raise ValueError("invalid cached section")

    monkeypatch.setattr(hooks, "_session_sections", fail_render)
    monkeypatch.setattr(sys, "argv", ["sidegraph-session-start"])
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    hooks.session_start()
    assert json.loads(capsys.readouterr().out) == {}
    actual = emulate_hook(
        "session_start",
        {},
        store_path=path,
        reader=None,
        borrowed_from=None,
        host_family="claude",
        sync=sync,
    )
    assert not actual.emitted and actual.not_emitted_reason
    assert actual.text == ""


@pytest.mark.parametrize("shape", ["legacy-file", "malformed-store"])
def test_grok_standing_instructions_on_a_refused_store(tmp_path: Path, shape: str) -> None:
    path = tmp_path / ".sidegraph"
    if shape == "legacy-file":
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE meta (key TEXT, value TEXT)")
        db.close()
    else:
        Store(path).close()
        (path / "decisions.db").write_bytes(b"Not SQLite.\n")
    expected = _initialize(tmp_path, path, "grok-shell-sidegraph")
    assert expected
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    result = emulate_hook(
        "mcp_instructions",
        {},
        store_path=path,
        reader=None,
        borrowed_from=None,
        host_family="grok",
    )
    assert result.emitted and result.text == expected
    assert {
        p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()
    } == before


def test_session_start_reports_a_missing_tracked_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    git(tmp_path, "init", "-q")
    path = tmp_path / ".sidegraph"
    Store(path).close()
    git(tmp_path, "add", ".sidegraph/format")
    git(
        tmp_path,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-qm",
        "Record format marker",
    )
    (path / "format").unlink()
    monkeypatch.chdir(tmp_path)
    expected = _entry_text(path, "session_start", "claude", {})
    assert "HEAD records it" in expected
    result = emulate_hook(
        "session_start",
        {},
        store_path=path,
        reader=None,
        borrowed_from=None,
        host_family="claude",
    )
    assert result.text == expected
    assert not (path / "format").exists()


def test_session_start_missing_untracked_marker_has_no_checkout_advice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / ".sidegraph"
    with Store(path) as store:
        store.add_decision(_decision())
    marker = path / "format"
    marker.unlink()
    monkeypatch.setenv("SIDEGRAPH_DIR", str(path))

    result = emulate_hook(
        "session_start",
        {},
        store_path=path,
        reader=None,
        borrowed_from=None,
        host_family="claude",
    )

    assert "format marker missing" in result.text
    assert "checkout HEAD -- format" not in result.text
    assert not marker.exists()
