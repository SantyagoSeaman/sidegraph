"""The plugin's MCP entry point lets a committed ratify policy only tighten.

`sidegraph-init` commits the policy to `.claude/settings.json`'s `env` block. Claude Code
applies that block to its MCP servers itself; Codex, Grok and Muse do not. For them
`sidegraph.host.mcp_entry` reads the committed value and applies it only when it is stricter
than the environment's policy (`manual` < `auto-low-risk` < `auto-all`). A committed file
never loosens the policy and never supplies one when the environment sets none. A settings
problem never stops the server.
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


@pytest.mark.parametrize("committed", ["auto-low-risk", "auto-all"])
def test_a_committed_policy_never_supplies_one_when_the_environment_sets_none(project, committed):
    """Red target: unfixed code copies the committed auto value into the unset environment."""
    _settings(project, {"env": {VAR: committed}})
    mcp_entry.apply_project_ratify_policy()
    assert VAR not in os.environ
    assert server._ratify_policy() is RatifyPolicy.MANUAL


@pytest.mark.parametrize(
    ("env", "committed", "effective"),
    [
        ("auto-all", "manual", "manual"),
        ("auto-all", "auto-low-risk", "auto-low-risk"),
        ("auto-low-risk", "manual", "manual"),
    ],
)
def test_a_stricter_committed_policy_tightens_the_environment(
    project, monkeypatch, env, committed, effective
):
    """Red target: unfixed code lets the environment value win outright."""
    _settings(project, {"env": {VAR: committed}})
    monkeypatch.setenv(VAR, env)
    mcp_entry.apply_project_ratify_policy()
    assert os.environ[VAR] == effective
    assert server._ratify_policy().value == effective


@pytest.mark.parametrize(
    ("env", "committed"),
    [
        ("manual", "auto-all"),
        ("manual", "auto-low-risk"),
        ("auto-low-risk", "auto-all"),
        ("auto-all", "auto-all"),
        ("auto-low-risk", "auto-low-risk"),
        ("manual", "manual"),
    ],
)
def test_a_looser_or_equal_committed_policy_leaves_the_environment(
    project, monkeypatch, env, committed
):
    """Red target: none, guards against over-reach (a committed file never loosens)."""
    _settings(project, {"env": {VAR: committed}})
    monkeypatch.setenv(VAR, env)
    mcp_entry.apply_project_ratify_policy()
    assert os.environ[VAR] == env


@pytest.mark.parametrize("committed", ["", "  ", "AUTO-ALL-please", None, 0])
def test_an_unknown_or_blank_committed_value_counts_as_manual(project, monkeypatch, committed):
    """Red target: unfixed code lets the environment value win outright; a committed `""` was
    also read as absent, so env `auto-all` stayed `auto-all` (the `is None` guard)."""
    _settings(project, {"env": {VAR: committed}})
    monkeypatch.setenv(VAR, "auto-all")
    mcp_entry.apply_project_ratify_policy()
    assert os.environ[VAR] == "manual"
    assert server._ratify_policy() is RatifyPolicy.MANUAL


@pytest.mark.parametrize("committed", ["auto-all", "auto-low-risk"])
def test_an_unknown_environment_value_stays_manual_whatever_is_committed(
    project, monkeypatch, committed
):
    """Red target: none, guards against over-reach (an unknown value is already manual, and
    a committed file never loosens it: env `bogus` must not be read as `auto-all`)."""
    _settings(project, {"env": {VAR: committed}})
    monkeypatch.setenv(VAR, "invalid-policy")
    mcp_entry.apply_project_ratify_policy()
    assert os.environ[VAR] == "invalid-policy"
    assert server._ratify_policy() is RatifyPolicy.MANUAL


@pytest.mark.parametrize("value", ["", "  "])
def test_a_blank_environment_value_counts_as_unset(project, monkeypatch, value):
    """Red target: unfixed code replaces a blank environment value with the committed one."""
    _settings(project, {"env": {VAR: "auto-low-risk"}})
    monkeypatch.setenv(VAR, value)
    mcp_entry.apply_project_ratify_policy()
    assert os.environ[VAR] == value
    assert server._ratify_policy() is RatifyPolicy.MANUAL


@pytest.mark.parametrize("payload", ["{not json", "[]", '{"env": null}', {"env": {}}])
def test_an_unreadable_committed_file_leaves_the_environment(project, monkeypatch, payload):
    """Red target: none, guards against over-reach (no committed value, no change)."""
    _settings(project, payload)
    monkeypatch.setenv(VAR, "auto-all")
    mcp_entry.apply_project_ratify_policy()
    assert os.environ[VAR] == "auto-all"


def test_no_committed_file_leaves_the_environment(project, monkeypatch):
    """Red target: none, guards against over-reach (no committed value, no change)."""
    monkeypatch.setenv(VAR, "auto-all")
    mcp_entry.apply_project_ratify_policy()
    assert os.environ[VAR] == "auto-all"


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
def test_a_symlinked_settings_file_is_not_followed(project, tmp_path_factory, monkeypatch):
    """Red target: none, guards against over-reach (a link is never followed)."""
    monkeypatch.setenv(VAR, "auto-all")
    outside = tmp_path_factory.mktemp("outside") / "settings.json"
    outside.write_text(json.dumps({"env": {VAR: "manual"}}), encoding="utf-8")
    (project / ".claude").mkdir()
    (project / ".claude" / "settings.json").symlink_to(outside)
    mcp_entry.apply_project_ratify_policy()
    assert os.environ[VAR] == "auto-all"


def test_a_failing_reader_never_stops_the_server(project, monkeypatch):
    """Red target: none, guards against over-reach (a settings problem leaves the env as it was)."""
    monkeypatch.setenv(VAR, "auto-all")

    def boom(_root):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(mcp_entry, "current_ratify_policy", boom)
    mcp_entry.apply_project_ratify_policy()
    assert os.environ[VAR] == "auto-all"


def test_main_applies_the_policy_before_serving(project, monkeypatch):
    """Red target: unfixed code leaves the environment's auto-all untouched at serve time."""
    _settings(project, {"env": {VAR: "manual"}})
    monkeypatch.setenv(VAR, "auto-all")
    seen = {}
    monkeypatch.setattr(
        server, "main", lambda prepare=None: seen.setdefault("v", os.environ.get(VAR))
    )
    monkeypatch.setattr(sys, "argv", ["sidegraph-mcp"])
    mcp_entry.main()
    assert seen["v"] == "manual"


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
    [("auto-all", "manual"), ("auto-low-risk", "auto-low-risk")],
)
def test_policy_follows_selected_project_even_before_store_exists(
    selected_project, monkeypatch, store_exists, launch_policy, selected_policy
):
    """Red target: unfixed code lets the environment's auto-all win over the selected
    project's stricter committed value; launch project A never speaks for selected store B."""
    launch, selected = selected_project
    _settings(launch, {"env": {VAR: launch_policy}})
    _settings(selected, {"env": {VAR: selected_policy}})
    monkeypatch.setenv(VAR, "auto-all")
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
def test_unusable_selected_settings_never_borrow_launch_policy(
    selected_project, monkeypatch, payload
):
    """Red target: none, guards against over-reach. No usable committed value in B leaves
    the environment as it was, and launch project A's stricter file is never read for B."""
    launch, selected = selected_project
    monkeypatch.setenv(VAR, "auto-low-risk")
    _settings(launch, {"env": {VAR: "manual"}})
    if payload is not None:
        _settings(selected, payload)

    mcp_entry.apply_project_ratify_policy()

    assert os.environ[VAR] == "auto-low-risk"
    assert not (selected / ".sidegraph").exists()


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_store_symlink_policy_uses_logical_project_not_storage_repository(
    selected_project, monkeypatch
):
    """Red target: unfixed code lets the environment's auto-all win. The selected B link owns
    the committed policy, rather than cwd A or the storage target C."""
    launch, selected = selected_project
    monkeypatch.setenv(VAR, "auto-all")
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
    """Red target: unfixed code never resolves the store when the environment sets a policy.
    Resolving policy ownership stays inside the never-stop-server boundary and leaves the
    environment as it was."""
    launch, _selected = selected_project
    monkeypatch.setenv(VAR, "auto-all")
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
    assert seen == ["auto-all"]


@pytest.mark.parametrize("value", ["", " \t "])
def test_an_unset_or_blank_policy_does_not_resolve_store_or_read_settings(
    selected_project, monkeypatch, value
):
    """Red target: unfixed code resolves the store and reads the committed file when the
    environment sets none; nothing it could read changes the outcome."""
    monkeypatch.setenv(VAR, value)

    def forbidden(*args, **kwargs):
        pytest.fail("an unset policy must bypass project lookup")

    monkeypatch.setattr(mcp_entry, "current_ratify_policy", forbidden)
    monkeypatch.setattr(mcp_entry, "resolve_store_location", forbidden)
    mcp_entry.apply_project_ratify_policy()
    assert os.environ[VAR] == value


@pytest.mark.parametrize("layout", ["default-nested", "relative-nested", "non-git", "legacy"])
def test_policy_lookup_keeps_existing_store_location_contracts(
    selected_project, monkeypatch, layout, capsys
):
    """Red target: unfixed code lets the environment's auto-all win. Representative
    compatibility cases use real config helpers without creating a store."""
    launch, selected = selected_project
    monkeypatch.setenv(VAR, "auto-all")
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
