"""The plugin's MCP entry point applies the project's committed ratify policy.

`sidegraph-init` commits the policy to `.claude/settings.json`'s `env` block. Claude Code
applies that block to its MCP servers; Codex does not, so `sidegraph.host.mcp_entry` reads
the committed value itself when the environment sets none. An explicit environment value
always wins, and a settings problem never stops the server.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from sidegraph import server
from sidegraph.capture import RatifyPolicy
from sidegraph.host import mcp_entry

VAR = "SIDEGRAPH_RATIFY_POLICY"


def _settings(root, payload) -> None:
    (root / ".claude").mkdir(exist_ok=True)
    text = payload if isinstance(payload, str) else json.dumps(payload)
    (root / ".claude" / "settings.json").write_text(text, encoding="utf-8")


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(VAR, raising=False)
    return tmp_path


def test_the_committed_policy_applies_when_the_environment_sets_none(project):
    _settings(project, {"env": {VAR: "auto-low-risk"}})
    mcp_entry.apply_project_ratify_policy()
    assert os.environ[VAR] == "auto-low-risk"
    assert server._ratify_policy() is RatifyPolicy.AUTO_LOW_RISK


def test_an_explicit_environment_value_wins(project, monkeypatch):
    _settings(project, {"env": {VAR: "auto-low-risk"}})
    monkeypatch.setenv(VAR, "manual")
    mcp_entry.apply_project_ratify_policy()
    assert os.environ[VAR] == "manual"


def test_a_blank_environment_value_counts_as_unset(project, monkeypatch):
    _settings(project, {"env": {VAR: "auto-low-risk"}})
    monkeypatch.setenv(VAR, "  ")
    mcp_entry.apply_project_ratify_policy()
    assert os.environ[VAR] == "auto-low-risk"


def test_no_settings_file_leaves_the_default(project):
    mcp_entry.apply_project_ratify_policy()
    assert VAR not in os.environ
    assert server._ratify_policy() is RatifyPolicy.MANUAL


def test_settings_without_the_key_leave_the_default(project):
    _settings(project, {"env": {"OTHER": "1"}})
    mcp_entry.apply_project_ratify_policy()
    assert VAR not in os.environ


@pytest.mark.parametrize("payload", ["{not json", "[]", '{"env": null}'])
def test_unusable_settings_leave_the_default(project, payload):
    _settings(project, payload)
    mcp_entry.apply_project_ratify_policy()
    assert VAR not in os.environ


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_a_symlinked_settings_file_is_not_followed(project, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside") / "settings.json"
    outside.write_text(json.dumps({"env": {VAR: "auto-all"}}), encoding="utf-8")
    (project / ".claude").mkdir()
    (project / ".claude" / "settings.json").symlink_to(outside)
    mcp_entry.apply_project_ratify_policy()
    assert VAR not in os.environ


def test_a_failing_reader_never_stops_the_server(project, monkeypatch):
    def boom(_root):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(mcp_entry, "current_ratify_policy", boom)
    mcp_entry.apply_project_ratify_policy()
    assert VAR not in os.environ


def test_main_applies_the_policy_before_serving(project, monkeypatch):
    _settings(project, {"env": {VAR: "auto-low-risk"}})
    seen = {}
    monkeypatch.setattr(
        server, "main", lambda prepare=None: seen.setdefault("v", os.environ.get(VAR))
    )
    monkeypatch.setattr(sys, "argv", ["sidegraph-mcp"])
    mcp_entry.main()
    assert seen["v"] == "auto-low-risk"


def _repository(parent: Path, name: str) -> Path:
    root = parent / name
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, capture_output=True, check=True)
    return root


@pytest.fixture
def selected_project(tmp_path, monkeypatch):
    launch = _repository(tmp_path, "launch")
    selected = _repository(tmp_path, "selected")
    monkeypatch.chdir(launch)
    monkeypatch.setenv("SIDEGRAPH_DIR", str(selected / ".sidegraph"))
    return launch, selected


@pytest.mark.parametrize("store_exists", [False, True])
@pytest.mark.parametrize(
    ("launch_policy", "selected_policy"),
    [("auto-all", "manual"), ("manual", "auto-low-risk")],
)
def test_policy_follows_selected_project_even_before_store_exists(
    selected_project, store_exists, launch_policy, selected_policy
):
    """Launch A's automatic policy must not override the policy of selected store B."""
    launch, selected = selected_project
    _settings(launch, {"env": {VAR: launch_policy}})
    _settings(selected, {"env": {VAR: selected_policy}})
    store_path = selected / ".sidegraph"
    if store_exists:
        store_path.mkdir()
    before = {p: p.read_bytes() for p in launch.parent.rglob("*") if p.is_file()}

    mcp_entry.apply_project_ratify_policy()

    assert os.environ[VAR] == selected_policy
    assert server._ratify_policy().value == selected_policy
    assert Path.cwd() == launch
    assert store_path.exists() is store_exists
    assert {p: p.read_bytes() for p in launch.parent.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize("payload", [None, "{bad json", {"env": {}}])
def test_unusable_selected_settings_never_borrow_launch_auto_policy(selected_project, payload):
    """No usable policy in B means manual, even when launch project A says auto-all."""
    launch, selected = selected_project
    _settings(launch, {"env": {VAR: "auto-all"}})
    if payload is not None:
        _settings(selected, payload)

    mcp_entry.apply_project_ratify_policy()

    assert VAR not in os.environ
    assert server._ratify_policy() is RatifyPolicy.MANUAL
    assert not (selected / ".sidegraph").exists()


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_store_symlink_policy_uses_logical_project_not_storage_repository(
    selected_project, monkeypatch
):
    """The selected B link owns the policy, rather than cwd A or the storage target C."""
    launch, selected = selected_project
    storage = _repository(launch.parent, "storage")
    _settings(launch, {"env": {VAR: "auto-all"}})
    _settings(selected, {"env": {VAR: "manual"}})
    _settings(storage, {"env": {VAR: "auto-low-risk"}})
    target = storage / "shared-store"
    target.mkdir()
    (selected / ".sidegraph").symlink_to(target, target_is_directory=True)
    roots = []
    reader = mcp_entry.current_ratify_policy

    def observed_reader(root):
        roots.append(root)
        return reader(root)

    monkeypatch.setattr(mcp_entry, "current_ratify_policy", observed_reader)
    mcp_entry.apply_project_ratify_policy()

    assert os.environ[VAR] == "manual"
    assert roots == [selected]
    assert Path.cwd() == launch


def test_store_resolver_failure_keeps_default_and_allows_server_start(
    selected_project, monkeypatch
):
    """Resolving policy ownership must stay inside the never-stop-server boundary."""
    launch, _selected = selected_project
    _settings(launch, {"env": {VAR: "auto-all"}})
    resolver_calls = []

    def broken_resolver(**kwargs):
        resolver_calls.append(True)
        raise OSError("store path unavailable")

    monkeypatch.setattr(mcp_entry, "resolve_store_location", broken_resolver, raising=False)
    seen = []
    monkeypatch.setattr(server, "main", lambda prepare=None: seen.append(os.environ.get(VAR)))
    monkeypatch.setattr(sys, "argv", ["sidegraph-mcp"])
    mcp_entry.main()
    assert resolver_calls == [True]
    assert seen == [None]
    assert server._ratify_policy() is RatifyPolicy.MANUAL


@pytest.mark.parametrize("value", ["manual", "auto-all", "invalid-policy"])
def test_explicit_policy_does_not_resolve_store_or_read_settings(
    selected_project, monkeypatch, value
):
    """An inherited or explicitly set environment value wins before any fallback lookup."""
    monkeypatch.setenv(VAR, value)

    def forbidden(*args, **kwargs):
        pytest.fail("explicit policy must bypass project lookup")

    monkeypatch.setattr(mcp_entry, "current_ratify_policy", forbidden)
    monkeypatch.setattr(mcp_entry, "resolve_store_location", forbidden)
    mcp_entry.apply_project_ratify_policy()
    assert os.environ[VAR] == value


def test_blank_policy_uses_selected_project_fallback(selected_project, monkeypatch):
    launch, selected = selected_project
    _settings(launch, {"env": {VAR: "auto-all"}})
    _settings(selected, {"env": {VAR: "manual"}})
    monkeypatch.setenv(VAR, " \t ")
    mcp_entry.apply_project_ratify_policy()
    assert os.environ[VAR] == "manual"


@pytest.mark.parametrize("layout", ["default-nested", "relative-nested", "non-git", "legacy"])
def test_policy_lookup_keeps_existing_store_location_contracts(
    selected_project, monkeypatch, layout, capsys
):
    """Representative compatibility cases use real config helpers without creating a store."""
    launch, selected = selected_project
    _settings(launch, {"env": {VAR: "auto-all"}})
    if layout == "non-git":
        selected = launch.parent / "plain-project"
        selected.mkdir()
        store_path = selected / "memory"
        monkeypatch.setenv("SIDEGRAPH_DIR", str(store_path))
    elif layout == "legacy":
        selected = launch.parent / "legacy-project"
        selected.mkdir()
        store_path = selected / ".sidegraph" / "decisions.db"
        store_path.parent.mkdir()
        store_path.write_bytes(b"legacy fixture is not opened")
        monkeypatch.setenv("SIDEGRAPH_DIR", str(store_path))
    else:
        nested = selected / "sub" / "deep"
        nested.mkdir(parents=True)
        monkeypatch.chdir(nested)
        if layout == "default-nested":
            store_path = selected / ".sidegraph"
            monkeypatch.delenv("SIDEGRAPH_DIR")
        else:
            store_path = selected / "memory"
            monkeypatch.setenv("SIDEGRAPH_DIR", "memory")
        store_path.mkdir()
    _settings(selected, {"env": {VAR: "manual"}})
    cwd_before = Path.cwd()
    before = {p: p.read_bytes() for p in launch.parent.rglob("*") if p.is_file()}
    existed = store_path.exists()

    mcp_entry.apply_project_ratify_policy()

    assert os.environ[VAR] == "manual"
    assert Path.cwd() == cwd_before
    assert store_path.exists() is existed
    assert {p: p.read_bytes() for p in launch.parent.rglob("*") if p.is_file()} == before
    assert "creating new store" not in capsys.readouterr().err
