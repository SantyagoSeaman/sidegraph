"""The plugin's MCP entry point applies the project's committed ratify policy.

`sidegraph-init` commits the policy to `.claude/settings.json`'s `env` block. Claude Code
applies that block to its MCP servers; Codex does not, so `sidegraph.host.mcp_entry` reads
the committed value itself when the environment sets none. An explicit environment value
always wins, and a settings problem never stops the server.
"""

from __future__ import annotations

import json
import os
import sys

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
