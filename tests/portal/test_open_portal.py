"""Exercise the MCP tool and the real parent side of the Portal launch protocol."""

import os
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest

from sidegraph import server
from sidegraph.config import CURRENT_SESSION, SessionHint
from sidegraph.engine.reader import GraphifyReader
from sidegraph.store import Store
from sidegraph.sync import LAST_SYNCED_KEY
from tests.graph_repo_support import add_worktree, git, make_main, write_graph
from tests.store_record_support import _decision
from tests.test_bounded_listings import call

_slow = pytest.mark.slow


@pytest.fixture(autouse=True)
def portal_process(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(server, "_store", None)
    monkeypatch.setattr(server, "_portal_child", None, raising=False)
    yield
    if server._portal_child is not None:
        child, write_fd, _ = server._portal_child
        os.close(write_fd)
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()


@pytest.fixture
def store_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / ".sidegraph"
    with Store(path):
        pass
    monkeypatch.setenv("SIDEGRAPH_DIR", str(path))
    return path


@pytest.fixture
def children(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[list[tuple[subprocess.Popen[bytes], dict]]]:
    popen = subprocess.Popen
    spawned: list[tuple[subprocess.Popen[bytes], dict]] = []

    def capture(args, **kwargs):
        child = popen(args, **kwargs)
        if args[:2] == [sys.executable, "-m"]:
            spawned.append((child, kwargs))
        return child

    monkeypatch.setattr(subprocess, "Popen", capture)
    yield spawned
    for child, _ in spawned:
        if child.poll() is None:
            child.kill()
            child.wait()


def _launch() -> str:
    result = server._open_portal_impl(open_browser=False, session_host="")
    assert "url" in result, result
    return result["url"]


def _session(url: str) -> dict:
    location, token = url.split("#t=")
    with httpx.Client(timeout=5, trust_env=False) as client:
        # The child prints before uvicorn starts accepting requests.
        response = client.get(f"{location}api/session", headers={"X-Sidegraph-Token": token})
        response.raise_for_status()
        return response.json()


def _use_stub(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    monkeypatch.setattr(server, "_PORTAL_MODULE", "tests.portal.open_portal_stub", raising=False)
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parents[2]))
    monkeypatch.setenv("PORTAL_STUB_MODE", mode)


@_slow
def test_second_call_same_url(store_path: Path, children: list) -> None:
    first = _launch()
    assert _launch() == first
    assert len(children) == 1
    assert _session(first)["mode"] == "chat"


@_slow
def test_respawn_after_child_died(store_path: Path, children: list) -> None:
    first = _launch()
    child, _ = children[0]
    child.kill()
    child.wait(timeout=5)
    second = _launch()
    assert len(children) == 2
    assert second != first
    assert _session(second)["mode"] == "chat"


@_slow
@pytest.mark.parametrize("already_open", [False, True])
def test_subdirectory_cwd_same_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, children: list, already_open: bool
) -> None:
    git(tmp_path, "init", "-q", "-b", "portal-project")
    path = tmp_path / ".sidegraph"
    subdir = tmp_path / "subdir"
    subdir.mkdir()
    with Store(path) as store:
        if already_open:
            monkeypatch.setattr(server, "_store", store)
            monkeypatch.setenv("SIDEGRAPH_DIR", "different-store")
        monkeypatch.chdir(subdir)
        session = _session(_launch())
        command = children[0][0].args
        assert Path(command[command.index("--db") + 1]).samefile(path)
        assert session["branch"] == "portal-project"
    assert not (subdir / ".sidegraph").exists()


@_slow
def test_open_store_path_starting_with_dash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    with Store("--portal-store") as store:
        monkeypatch.setattr(server, "_store", store)
        assert _session(_launch())["mode"] == "chat"


@_slow
@pytest.mark.skipif(sys.platform == "win32", reason="directory symlinks require developer mode")
@pytest.mark.parametrize("already_open", [False, True])
def test_store_path_resolves_parent_after_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, children: list, already_open: bool
) -> None:
    git(tmp_path, "init", "-q")
    nested = tmp_path / "project" / "nested"
    nested.mkdir(parents=True)
    (tmp_path / "via").symlink_to(nested, target_is_directory=True)
    path = "via/../store"
    expected = tmp_path / "project" / "store"
    mcp_graph = tmp_path / "graphify-out" / "graph.json"
    other_graph = expected.parent / "graphify-out" / "graph.json"
    write_graph(mcp_graph, None, ["mcp.py"])
    write_graph(other_graph, None, ["other.py"])
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SIDEGRAPH_DIR", path)
    with Store(tmp_path / "store"), Store(path) as store:
        if already_open:
            monkeypatch.setattr(server, "_store", store)
        assert Path(server._graph_path(store)) == mcp_graph
        assert Path(store.path).samefile(expected)
        url = _launch()
        command = children[0][0].args
        assert Path(command[command.index("--db") + 1]).samefile(expected)
        store.set_meta(LAST_SYNCED_KEY, GraphifyReader(other_graph).sync_stamp())
        assert _session(url)["graph_newer_than_sync"] is True
        store.set_meta(LAST_SYNCED_KEY, GraphifyReader(mcp_graph).sync_stamp())
        assert _session(url)["graph_newer_than_sync"] is False


@_slow
@pytest.mark.skipif(sys.platform == "win32", reason="directory symlinks require developer mode")
def test_store_directory_alias_keeps_graph_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, children: list
) -> None:
    git(tmp_path, "init", "-q")
    actual = tmp_path / "data" / "store"
    alias = tmp_path / ".sidegraph"
    graph = tmp_path / "graphify-out" / "graph.json"
    write_graph(graph, None, ["alias.py"])
    write_graph(actual.parent / graph.relative_to(tmp_path), None, ["data.py"])
    with Store(actual) as store:
        alias.symlink_to(actual, target_is_directory=True)
        monkeypatch.chdir(tmp_path)
        url = _launch()
        command = children[0][0].args
        assert Path(command[command.index("--db") + 1]) == alias
        store.set_meta(LAST_SYNCED_KEY, GraphifyReader(graph).sync_stamp())
        assert _session(url)["graph_newer_than_sync"] is False


@_slow
@pytest.mark.skipif(sys.platform == "win32", reason="directory symlinks require developer mode")
def test_missing_store_hint_names_destination_after_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, children: list
) -> None:
    git(tmp_path, "init", "-q")
    nested = tmp_path / "project" / "nested"
    nested.mkdir(parents=True)
    (tmp_path / "via").symlink_to(nested, target_is_directory=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SIDEGRAPH_DIR", "via/../store")
    missing = nested.parent / "store"
    with Store(tmp_path / "store"):
        result = call("open_portal", {"open_browser": False})
        assert result.is_error
        assert f"--db set to {missing} to initialize" in result.content[0].text
    assert not missing.exists()
    assert children == []


@_slow
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX parent traversal")
def test_missing_store_hint_reports_an_unreachable_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, children: list
) -> None:
    git(tmp_path, "init", "-q")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SIDEGRAPH_DIR", "unknown/../store")
    with Store(tmp_path / "store"):
        result = call("open_portal", {"open_browser": False})
        assert result.is_error
        assert str(tmp_path / "unknown") in result.content[0].text
        assert "Check the configured store path." in result.content[0].text
        assert "sidegraph-init" not in result.content[0].text
    assert children == []


@_slow
@pytest.mark.parametrize("from_subdirectory", [False, True])
@pytest.mark.parametrize("layout", ["absent", "empty", "unrelated_json", "legacy_text"])
def test_no_store_returns_error_and_creates_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    children: list,
    from_subdirectory: bool,
    layout: str,
) -> None:
    git(tmp_path, "init", "-q")
    cwd = tmp_path / "subdir" if from_subdirectory else tmp_path
    cwd.mkdir(exist_ok=True)
    monkeypatch.chdir(cwd)
    destination = tmp_path / ".sidegraph"
    if layout != "absent":
        destination.mkdir()
    if layout == "unrelated_json":
        entities = destination / "entities"
        entities.mkdir()
        (entities / "catalog.json").write_text('{"currency":"GBP"}\n', encoding="utf-8")
    elif layout == "legacy_text":
        (destination / "decisions.db").write_text("plain text\n", encoding="utf-8")
    before = set(destination.rglob("*"))
    result = call("open_portal", {"open_browser": False})
    assert result.is_error
    assert "sidegraph-init" in result.content[0].text
    assert "sidegraph-portal" not in result.content[0].text
    assert str(tmp_path / ".sidegraph") in result.content[0].text
    assert children == []
    if from_subdirectory:
        assert not (cwd / ".sidegraph").exists()
    assert destination.exists() is (layout != "absent")
    assert set(destination.rglob("*")) == before
    assert server._store is None


@_slow
@pytest.mark.parametrize(
    ("layout", "diagnostic"),
    [
        pytest.param(
            "external_link",
            "leaves its repository through a link",
            marks=pytest.mark.skipif(
                sys.platform == "win32", reason="directory symlinks require developer mode"
            ),
        ),
        ("bad_legacy", "could not inspect the Portal store"),
        ("file", "set the store path to its directory"),
    ],
)
def test_refuses_invalid_store_before_spawning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    children: list,
    layout: str,
    diagnostic: str,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    monkeypatch.chdir(repo)
    path = repo / ".sidegraph"
    if layout == "external_link":
        outside = tmp_path / "outside"
        with Store(outside):
            pass
        path.symlink_to(outside, target_is_directory=True)
    elif layout == "bad_legacy":
        with Store(path):
            pass
        (path / "decisions.db").write_text("plain text\n", encoding="utf-8")
    else:
        path.write_text("plain text\n", encoding="utf-8")
    result = call("open_portal", {"open_browser": False})
    assert result.is_error
    assert diagnostic in result.content[0].text
    assert children == []
    assert server._store is None


@_slow
def test_markerless_store_with_record_reports_missing_format(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    records = tmp_path / ".sidegraph" / "decisions"
    records.mkdir(parents=True)
    record = _decision()
    (records / f"{record.id}.json").write_text(record.model_dump_json() + "\n", encoding="utf-8")
    monkeypatch.setenv("SIDEGRAPH_DIR", str(records.parent))
    with pytest.raises(httpx.HTTPStatusError) as exc:
        _session(_launch())
    assert exc.value.response.status_code == 409
    assert exc.value.response.json()["error"]["details"]["reason"] == "store_format"
    assert not (records.parent / "format").exists()


@_slow
def test_worktree_child_uses_borrowed_graph(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, children: list
) -> None:
    main = make_main(tmp_path, nested=True)
    worktree = add_worktree(main)
    monkeypatch.setenv("SIDEGRAPH_DIR", ".config/sidegraph")
    monkeypatch.chdir(worktree)
    assert not (worktree / ".config" / "graphify-out" / "graph.json").exists()
    assert _session(_launch())["graph_newer_than_sync"] is True
    assert "--graph" not in children[0][0].args


@_slow
@pytest.mark.parametrize(
    ("mode", "reason"),
    [
        ("silent", "15 seconds"),
        ("malformed", "invalid startup response"),
        ("error", "Cannot bind the stub port."),
    ],
)
def test_silent_malformed_or_error_child_returns_error_within_15s_and_reaps(
    monkeypatch: pytest.MonkeyPatch, store_path: Path, children: list, mode: str, reason: str
) -> None:
    _use_stub(monkeypatch, mode)
    started = time.monotonic()
    result = server._open_portal_impl(open_browser=False, session_host="")
    assert time.monotonic() - started < 16
    assert reason in result["error"]
    child, _ = children[0]
    assert child.returncode is not None
    assert child.stdout is not None and child.stdout.closed


@_slow
@pytest.mark.parametrize("long_marker", [False, True])
def test_startup_error_uses_utf8_without_changing_child_environment(
    store_path: Path, monkeypatch: pytest.MonkeyPatch, children: list, long_marker: bool
) -> None:
    marker = "сломано" + ("x" * 3980 if long_marker else "")
    (store_path / "format").write_text(marker + "\n", encoding="utf-8")
    monkeypatch.setenv("PYTHONIOENCODING", "utf-16")
    result = server._open_portal_impl(open_browser=False, session_host="")
    assert "сломано" in result["error"]
    assert result["error"].endswith(" (truncated)") is long_marker
    child, options = children[0]
    assert options.get("env", os.environ)["PYTHONIOENCODING"] == "utf-16"
    assert child.returncode is not None


@_slow
def test_simultaneous_startup_calls_stay_within_15_seconds(
    store_path: Path, monkeypatch: pytest.MonkeyPatch, children: list
) -> None:
    _use_stub(monkeypatch, "silent")
    ready = threading.Barrier(2)

    def call_together() -> tuple[float, dict]:
        ready.wait(timeout=5)
        started = time.monotonic()
        result = server._open_portal_impl(open_browser=False, session_host="")
        return time.monotonic() - started, result

    with ThreadPoolExecutor(max_workers=2) as callers:
        first = callers.submit(call_together)
        second = callers.submit(call_together)
        results = [first.result(timeout=35), second.result(timeout=35)]
    for elapsed, result in results:
        assert elapsed < 16, results
        assert "15 seconds" in result["error"]
    assert children and all(child.returncode is not None for child, _ in children)


@_slow
def test_tool_classified(store_path: Path) -> None:
    from tests.test_server_annotations import _listed_tools

    tool = _listed_tools()["open_portal"]
    assert tool.annotations.readOnlyHint is False
    result = call("open_portal", {"open_browser": False})
    assert not result.is_error, result.content
    assert "http://127.0.0.1:" in result.content[0].text
    assert "clickable link" in result.content[0].text


@_slow
@pytest.mark.parametrize(("family", "expected"), [("codex", "codex"), ("unknown", None)])
def test_host_family_from_current_session(
    store_path: Path, children: list, family: str, expected: str | None
) -> None:
    token = CURRENT_SESSION.set(SessionHint("portal-test", family))
    try:
        result = call("open_portal", {"open_browser": False})
    finally:
        CURRENT_SESSION.reset(token)
    assert not result.is_error, result.content
    command = children[0][0].args
    assert command[command.index("--host-family") + 1] == family
    assert _session(server._portal_child[2])["host_family"] == expected


@_slow
def test_no_browser_flag_when_open_browser_false(store_path: Path, children: list) -> None:
    _launch()
    assert "--no-browser" in children[0][0].args


@pytest.mark.skipif(sys.platform != "win32", reason="Windows handle inheritance")
def test_windows_spawn_passes_only_read_handle(
    store_path: Path, monkeypatch: pytest.MonkeyPatch, children: list
) -> None:
    import _winapi
    import msvcrt

    _use_stub(monkeypatch, "windows")
    sentinel = _winapi.CreateEventW(0, True, False, None)
    try:
        os.set_handle_inheritable(sentinel, True)
        monkeypatch.setenv("PORTAL_STUB_SENTINEL", str(sentinel))
        _launch()
        child, options = children[0]
        assert server._portal_child is not None
        _, write_fd, _ = server._portal_child
        command = child.args
        read_handle = int(command[command.index("--lifetime-fd") + 1])
        assert options["startupinfo"].lpAttributeList["handle_list"] == [read_handle]
        assert options["close_fds"] is True
        assert not os.get_handle_inheritable(msvcrt.get_osfhandle(write_fd))
        os.close(write_fd)
        server._portal_child = None
        assert child.wait(timeout=5) == 0
        assert _winapi.WaitForSingleObject(sentinel, 0) == _winapi.WAIT_TIMEOUT
    finally:
        _winapi.CloseHandle(sentinel)
