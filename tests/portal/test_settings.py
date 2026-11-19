"""User config and the portal shell's session metadata."""

import json
import os
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest

from sidegraph.portal import contract
from sidegraph.store import Store
from sidegraph.store_layout import VOLATILE_STALE_KEY
from sidegraph.sync import LAST_SYNCED_KEY


@pytest.fixture
def config_file() -> Path:
    return Path(os.environ["XDG_CONFIG_HOME"]) / "sidegraph" / "portal.json"


@pytest.mark.parametrize("unreadable", [False, True])
def test_settings_default_when_missing_or_unreadable(config_file: Path, unreadable: bool) -> None:
    from sidegraph.portal.settings import load

    if unreadable:
        config_file.mkdir(parents=True)
    assert load().model_dump() == {
        "theme": "dark",
        "poll_seconds": 5,
        "editor_url": "vscode://file/{path}:{line}",
        "idle_minutes": 30,
    }


@pytest.mark.parametrize(("supplied", "clamped"), [(1, 2), (100, 60)])
def test_poll_seconds_clamped(config_file: Path, supplied: int, clamped: int) -> None:
    from sidegraph.portal.settings import load

    config_file.parent.mkdir(parents=True)
    config_file.write_text(json.dumps({"poll_seconds": supplied, "idle_minutes": 0}))
    settings = load()
    assert settings.poll_seconds == clamped
    assert settings.idle_minutes == 0.05


def test_theme_is_rendered_and_saved_atomically(
    portal_ctx,
    portal_client,
    config_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sidegraph.portal import settings

    ctx = portal_ctx()
    replace = os.replace
    replacements = []

    def check_replace(source: Path, target: Path) -> None:
        assert Path(source).parent == config_file.parent
        assert Path(target) == config_file
        replacements.append(target)
        replace(source, target)

    monkeypatch.setattr(settings.os, "replace", check_replace)
    with portal_client(ctx) as client:
        assert 'class="th-dark"' in client.get("/").text
        response = client.post("/api/settings", json={"theme": "light"})
        assert response.status_code == 200
        assert contract.Settings.model_validate(response.json()).theme == "light"
        assert 'class="th-light"' in client.get("/").text
        assert replacements == [config_file]
    assert json.loads(config_file.read_text())["theme"] == "light"
    monkeypatch.setattr(settings.os, "replace", replace)
    with portal_client(portal_ctx()) as next_client:
        assert 'class="th-light"' in next_client.get("/").text


@pytest.mark.parametrize("mode", ["cli", "chat"])
@pytest.mark.parametrize("prefix", ["", "index-only:"])
def test_session_has_store_and_graph_status(
    portal_ctx,
    portal_client,
    tmp_path: Path,
    mode: str,
    prefix: str,
) -> None:
    graph = tmp_path / "graph.json"
    graph.write_text('{"nodes":[],"links":[]}')
    ctx = portal_ctx(mode=mode, graph_path=graph)
    reader = ctx.reader()
    assert reader is not None
    with Store(ctx.store_path) as store:
        store.set_meta(VOLATILE_STALE_KEY, "1")
        store.set_meta(LAST_SYNCED_KEY, "old")
    with portal_client(ctx) as client:
        response = client.get("/api/session")
        assert response.status_code == 200
        session = contract.Session.model_validate(response.json())
        assert session.host_family == "claude"
        assert session.mode == mode
        assert session.index_cold
        assert session.graph_newer_than_sync
        assert session.store_changes is None
        assert session.started_at == ctx.started_at
        assert session.reviewing_until == (
            ctx.started_at + timedelta(minutes=10) if mode == "chat" else None
        )
        with Store(ctx.store_path) as store:
            store.set_meta(VOLATILE_STALE_KEY, "0")
            store.set_meta(LAST_SYNCED_KEY, prefix + reader.sync_stamp())
        session = contract.Session.model_validate(client.get("/api/session").json())
        assert not session.index_cold
        assert not session.graph_newer_than_sync


def test_footer_runs_no_optional_locks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from subprocess import CompletedProcess

    from sidegraph import gitio

    store = tmp_path / ".sidegraph"
    store.mkdir()
    (tmp_path / ".git").mkdir()

    def status(args: list[str], cwd: Path, *, keep_repository_env: bool) -> CompletedProcess[str]:
        assert not keep_repository_env
        assert args == [
            "--no-optional-locks",
            "status",
            "--porcelain",
            "--untracked-files=all",
            "--",
            str(store),
        ]
        assert cwd == tmp_path
        return CompletedProcess(args, 0, stdout=" M .sidegraph/a\n?? .sidegraph/b\n")

    monkeypatch.setattr(gitio, "_run_git", status)
    assert gitio.store_change_count(store) == 2


def test_unwritable_settings_return_contract_error(
    portal_ctx, portal_client, config_file: Path
) -> None:
    config_file.mkdir(parents=True)
    with portal_client(portal_ctx(), raise_server_exceptions=False) as client:
        response = client.post("/api/settings", json={"theme": "light"})
        assert response.status_code == contract.ERROR_CODES["refused"]
        error = contract.ErrorBody.model_validate(response.json()).error
        assert error.code == "refused"
        assert error.details["reason"] == "settings_unwritable"
        assert 'class="th-dark"' in client.get("/").text


@pytest.mark.parametrize(
    "contents",
    [b'{"editor_url": "custom", "idle_minutes": 8,}', b"[]", b'{"theme": NaN}'],
)
def test_unreadable_settings_refused_without_overwriting(
    portal_ctx, portal_client, config_file: Path, contents: bytes
) -> None:
    config_file.parent.mkdir(parents=True)
    config_file.write_bytes(contents)
    with portal_client(portal_ctx()) as client:
        response = client.post("/api/settings", json={"theme": "light"})
        assert response.status_code == 409, response.text
        error = contract.ErrorBody.model_validate(response.json()).error
        assert error.code == "refused"
        assert error.details["reason"] == "settings_unreadable"
        assert str(config_file) in error.message
        assert 'class="th-dark"' in client.get("/").text
    assert config_file.read_bytes() == contents


def test_theme_update_preserves_unknown_settings(
    portal_ctx, portal_client, config_file: Path
) -> None:
    config_file.parent.mkdir(parents=True)
    future = {"options": ["текст", None, False, 2, 3.25]}
    config_file.write_text(json.dumps({"theme": "dark", "future": future}), encoding="utf-8")
    with portal_client(portal_ctx()) as client:
        response = client.post("/api/settings", json={"theme": "light"})
        assert response.status_code == 200, response.text
    saved = json.loads(config_file.read_text(encoding="utf-8"))
    assert saved["theme"] == "light"
    assert saved["future"] == future


def test_theme_update_keeps_active_settings_until_restart(
    portal_ctx, portal_client, config_file: Path
) -> None:
    ctx = portal_ctx()
    config_file.parent.mkdir(parents=True)
    config_file.write_text('{"poll_seconds": 11, "idle_minutes": 1}')
    with portal_client(ctx) as client:
        result = contract.Settings.model_validate(
            client.post("/api/settings", json={"theme": "light"}).json()
        )
        active = contract.Session.model_validate(client.get("/api/session").json()).settings
        assert result == active
        assert active.poll_seconds == 5 and active.idle_minutes == 30
        assert active.theme == "light"
    assert json.loads(config_file.read_text()) == {
        "theme": "light",
        "poll_seconds": 11,
        "idle_minutes": 1,
    }


def test_theme_update_preserves_existing_unedited_values(
    portal_ctx, portal_client, config_file: Path
) -> None:
    config_file.parent.mkdir(parents=True)
    raw = {"theme": "dark", "poll_seconds": 1, "idle_minutes": "8", "editor_url": None}
    config_file.write_text(json.dumps(raw), encoding="utf-8")
    with portal_client(portal_ctx()) as client:
        response = client.post("/api/settings", json={"theme": "light"})
        assert response.status_code == 200, response.text
        effective = contract.Settings.model_validate(response.json())
        assert effective.poll_seconds == 2 and effective.idle_minutes == 30
        assert effective.editor_url == "vscode://file/{path}:{line}"
    assert json.loads(config_file.read_text(encoding="utf-8")) == {**raw, "theme": "light"}


def test_footer_counts_each_changed_file(tmp_path: Path, portal_ctx, portal_client) -> None:
    subprocess.run(["git", "init", "-q", "-b", "portal-test", str(tmp_path)], check=True)
    ctx = portal_ctx()
    for index in range(3):
        (ctx.store_path / f"file{index}.txt").write_text("untracked")
    with portal_client(ctx) as client:
        session = contract.Session.model_validate(client.get("/api/session").json())
        assert session.repo_name == tmp_path.name
        assert session.branch == "portal-test"
        # The format marker, stamping marker and .gitignore also live under the store.
        assert session.store_changes == 6


def test_server_graph_differs_only_for_explicit_different_graph(portal_ctx, tmp_path: Path) -> None:
    from sidegraph.config import default_graph_path

    ctx = portal_ctx()
    graph = default_graph_path(ctx.store_path)
    equivalent = graph.parent / "subfolder" / ".." / graph.name
    assert not portal_ctx(graph_path=equivalent, graph_overridden=True).server_graph_differs
    other = tmp_path / "other.json"
    assert not portal_ctx(graph_path=other).server_graph_differs
    assert portal_ctx(graph_path=other, graph_overridden=True).server_graph_differs


@pytest.mark.skipif(os.name == "nt", reason="directory symlinks require Windows developer mode")
def test_server_graph_paths_can_use_a_symlinked_project(tmp_path: Path, portal_ctx) -> None:
    real_project = tmp_path / "project"
    graph = real_project / "graphify-out" / "graph.json"
    graph.parent.mkdir(parents=True)
    graph.write_text('{"nodes":[],"links":[]}')
    alias = tmp_path / "alias"
    alias.symlink_to(real_project, target_is_directory=True)
    ctx = portal_ctx(store_path=alias / ".sidegraph", graph_path=graph, graph_overridden=True)
    assert not ctx.server_graph_differs


@pytest.mark.parametrize("nested", [False, True])
def test_store_that_is_its_own_checkout_has_repo_details(
    portal_ctx, portal_client, nested: bool
) -> None:
    ctx = portal_ctx()
    if nested:
        subprocess.run(
            ["git", "init", "-q", "-b", "outer-branch", str(ctx.store_path.parent)], check=True
        )
    subprocess.run(["git", "init", "-q", "-b", "store-branch", str(ctx.store_path)], check=True)
    with portal_client(ctx) as client:
        session = contract.Session.model_validate(client.get("/api/session").json())
        assert session.repo_name == ctx.store_path.name
        assert session.branch == "store-branch"
        assert session.store_changes == 3


def test_footer_counts_git_line_endings_only(portal_ctx, tmp_path: Path) -> None:
    from sidegraph.gitio import store_change_count

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "config", "core.quotePath", "false"], cwd=tmp_path, check=True)
    ctx = portal_ctx()
    (ctx.store_path / "line\u2028separator").write_text("untracked")
    assert store_change_count(ctx.store_path) == 4


def test_explicit_graph_can_match_server_borrowing(portal_ctx, tmp_path: Path, monkeypatch) -> None:
    from sidegraph.portal import __main__ as launch

    ctx = portal_ctx()
    main_checkout = tmp_path / "main"
    graph = main_checkout / "graphify-out" / "graph.json"
    graph.parent.mkdir(parents=True)
    graph.write_text('{"nodes":[],"links":[]}')
    monkeypatch.setattr(launch, "borrowed_graph_path", lambda _store: graph)
    monkeypatch.setattr(launch, "main_checkout_root", lambda _store: main_checkout)
    observed = []
    create_app = launch.create_app

    def capture_app(launch_ctx):
        observed.append(launch_ctx)
        return create_app(launch_ctx)

    async def stop_after_binding(_server, sockets):
        assert sockets

    monkeypatch.setattr(launch, "create_app", capture_app)
    monkeypatch.setattr(launch.uvicorn.Server, "serve", stop_after_binding)
    assert launch.main(["--db", str(ctx.store_path), "--graph", str(graph), "--no-browser"]) == 0
    assert observed[0].borrowed_from == main_checkout


def test_explicit_override_detects_later_change_in_borrowing(
    portal_ctx, tmp_path: Path, monkeypatch
) -> None:
    from sidegraph.portal import context

    graph = tmp_path / "main" / "graphify-out" / "graph.json"
    ctx = portal_ctx(graph_path=graph, graph_overridden=True)
    assert ctx.server_graph_differs
    monkeypatch.setattr(context, "borrowed_graph_path", lambda _store: graph)
    # The launch used an override without borrowed provenance. It cannot sync as the MCP
    # reader after the server starts borrowing that file.
    assert ctx.server_graph_differs
