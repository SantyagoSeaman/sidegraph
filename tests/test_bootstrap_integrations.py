import json
import os
import shutil
from pathlib import Path

import pytest

from sidegraph.bootstrap.model import HostKind

FIXTURES = Path(__file__).parent / "fixtures" / "bootstrap" / "hosts"


@pytest.fixture(autouse=True)
def host_user_config(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """User-level host config is outside the test repo; never read the real home."""
    base = tmp_path_factory.mktemp("host-user")
    (base / "claude").mkdir()
    (base / "codex").mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: base))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(base / "claude"))
    monkeypatch.setenv("CODEX_HOME", str(base / "codex"))
    return base


@pytest.fixture
def claude_fixture() -> Path:
    return FIXTURES / "claude"


@pytest.fixture
def codex_fixture() -> Path:
    return FIXTURES / "codex"


def test_claude_complete_config_is_fully_supported(claude_fixture: Path):
    from sidegraph.bootstrap.integrations import verify_integration

    result = verify_integration(claude_fixture, HostKind.CLAUDE_CODE)

    assert result.fully_supported is True
    assert result.mcp == "verified"
    assert result.session_start == "verified"
    assert result.stop == "verified"
    assert result.pretool_read_grep == "verified"


def test_codex_never_claims_read_grep_pretool_support(codex_fixture: Path):
    from sidegraph.bootstrap.integrations import verify_integration

    result = verify_integration(
        codex_fixture,
        HostKind.CODEX,
        codex_config=codex_fixture / ".codex" / "config.toml",
    )

    assert result.mcp == "verified"
    assert result.session_start == "verified"
    assert result.stop == "verified"
    assert result.pretool_read_grep == "unsupported"
    assert result.fully_supported is False
    assert result.next_action is None


def test_malformed_config_is_incomplete_with_exact_next_action(tmp_path: Path):
    from sidegraph.bootstrap.integrations import verify_integration

    (tmp_path / ".mcp.json").write_text("not json\n")

    result = verify_integration(tmp_path, HostKind.CLAUDE_CODE)

    assert result.fully_supported is False
    assert result.next_action == "Repair .mcp.json, then rerun sidegraph-bootstrap"


def test_entrypoint_substrings_do_not_satisfy_a_supported_check(tmp_path: Path):
    """Replacing an exact command token with a lookalike must remain incomplete."""
    from sidegraph.bootstrap.integrations import verify_integration

    (tmp_path / ".mcp.json").write_text(
        '{"mcpServers": {"sidegraph": {"command": "sidegraph-mcp-shadow"}}}\n'
    )

    result = verify_integration(tmp_path, HostKind.CLAUDE_CODE)

    assert result.mcp == "missing"


def test_claude_mcp_entrypoint_in_args_is_verified(tmp_path: Path):
    """Published uvx configs put the executable in args, not command."""
    from sidegraph.bootstrap.integrations import verify_integration

    (tmp_path / ".mcp.json").write_text(
        """{
  "mcpServers": {
    "sidegraph": {
      "command": "uvx",
      "args": ["--from", "sidegraph", "sidegraph-mcp"]
    }
  }
}
"""
    )

    result = verify_integration(tmp_path, HostKind.CLAUDE_CODE)

    assert result.mcp == "verified"


def test_codex_canonical_hooks_and_entrypoints_in_args_are_verified(tmp_path: Path):
    """Codex supports project hooks at .codex/hooks.json and shell launchers."""
    from sidegraph.bootstrap.integrations import verify_integration

    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()
    config = codex_dir / "config.toml"
    config.write_text(
        """[mcp_servers.sidegraph]
command = "uvx"
args = ["--from", "sidegraph", "sidegraph-mcp"]
"""
    )
    (codex_dir / "hooks.json").write_text(
        """{
  "hooks": {
    "SessionStart": [{
      "hooks": [{
        "type": "command",
        "command": "bash",
        "args": ["-lc", "uvx --from sidegraph sidegraph-session-start"]
      }]
    }],
    "Stop": [{
      "hooks": [{
        "type": "command",
        "command": "bash",
        "args": ["-lc", "uvx --from sidegraph sidegraph-stop"]
      }]
    }]
  }
}
"""
    )

    result = verify_integration(tmp_path, HostKind.CODEX, codex_config=config)

    assert result.mcp == "verified"
    assert result.session_start == "verified"
    assert result.stop == "verified"
    assert result.next_action is None


def test_codex_stop_wrapped_in_sh_c_still_verifies(tmp_path: Path):
    """The plugin's Codex hot command is `sh -c '<script>' || printf …`, so the entry point is
    a word of the script, not of the outer command.
    design/superpowers/specs/2026-10-03-launch-from-session-commit-design.md (D3)"""
    from sidegraph.bootstrap.integrations import verify_integration

    config = tmp_path / ".codex" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text('[mcp_servers.sidegraph]\ncommand = "sidegraph-mcp"\n')

    def wrapped(entry: str) -> str:
        script = (
            'cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)" && '
            f'uvx --from "$u" {entry} || printf "{{}}\\n"'
        )
        return f"sh -c '{script}' || printf '{{}}\\n'"

    hooks = {
        "hooks": {
            "SessionStart": [
                {"hooks": [{"command": "uvx --from sidegraph sidegraph-session-start"}]}
            ],
            "Stop": [{"hooks": [{"command": wrapped("sidegraph-stop")}]}],
        }
    }
    (tmp_path / ".codex" / "hooks.json").write_text(json.dumps(hooks), encoding="utf-8")

    result = verify_integration(tmp_path, HostKind.CODEX, codex_config=config)

    assert result.session_start == "verified"
    assert result.stop == "verified"

    hooks["hooks"]["Stop"] = [{"hooks": [{"command": wrapped("sidegraph-other")}]}]
    (tmp_path / ".codex" / "hooks.json").write_text(json.dumps(hooks), encoding="utf-8")

    assert verify_integration(tmp_path, HostKind.CODEX, codex_config=config).stop == "missing"


@pytest.mark.parametrize(
    ("command", "found"),
    [
        pytest.param("sh -c 'uvx x sidegraph-stop'", True, id="sh"),
        pytest.param("/bin/sh -c 'uvx x sidegraph-stop'", True, id="bin-sh"),
        # The inner script need not parse: the outer words still count. It used to throw
        # the whole command away.
        pytest.param("sh -c 'echo \"' sidegraph-stop", True, id="inner-unparseable"),
        # The script's words extend the outer ones, they do not replace them.
        pytest.param("sh -c 'echo hi' sidegraph-stop", True, id="outer-word-kept"),
        pytest.param("sh -c 'uvx x sidegraph-other'", False, id="other-entry"),
        # Only `sh` and `/bin/sh` are unwrapped: another program's -c is not a script.
        pytest.param("bash -c 'uvx x sidegraph-stop'", False, id="bash"),
        pytest.param("echo -c 'uvx x sidegraph-stop'", False, id="not-a-shell"),
        pytest.param("sh -x -c 'uvx x sidegraph-stop'", False, id="-c-not-second"),
    ],
)
def test_sh_c_scripts_are_unwrapped_only_for_sh(command: str, found: bool) -> None:
    """The plugin's Codex hot command is ``sh -c '<script>'``: the verifier looks inside it.
    design/superpowers/specs/2026-10-03-launch-from-session-commit-design.md (D3)"""
    from sidegraph.bootstrap.integrations import _contains_entrypoint

    assert _contains_entrypoint((command,), "sidegraph-stop") is found


def test_swapped_or_misplaced_claude_commands_do_not_verify_hook_events(tmp_path: Path):
    """Searching the whole hooks document makes event-specific checks meaningless."""
    from sidegraph.bootstrap.integrations import verify_integration

    (tmp_path / ".mcp.json").write_text(
        '{"mcpServers": {"sidegraph": {"command": "sidegraph-mcp"}}}\n'
    )
    hooks = tmp_path / ".claude" / "settings.json"
    hooks.parent.mkdir(parents=True)
    hooks.write_text(
        """{
  "hooks": {
    "SessionStart": [{"command": "sidegraph-stop"}],
    "Stop": [{"command": "sidegraph-pre-tool-use"}],
    "PreToolUse": [{"command": "sidegraph-session-start"}]
  }
}
"""
    )

    result = verify_integration(tmp_path, HostKind.CLAUDE_CODE)

    assert result.session_start == "missing"
    assert result.stop == "missing"
    assert result.pretool_read_grep == "missing"
    assert result.fully_supported is False
    assert result.next_action == (
        "Configure sidegraph-session-start in .claude/settings.json, then rerun sidegraph-bootstrap"
    )


def test_codex_stop_must_be_configured_in_stop_event(tmp_path: Path):
    """A Stop command misplaced under SessionStart must not yield best-effort readiness."""
    from sidegraph.bootstrap.integrations import verify_integration

    config = tmp_path / ".codex" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text('[mcp_servers.sidegraph]\ncommand = "sidegraph-mcp"\n')
    hooks = tmp_path / ".codex" / "hooks" / "hooks.json"
    hooks.parent.mkdir(parents=True)
    hooks.write_text(
        """{
  "hooks": {
    "SessionStart": [
      {"command": "sidegraph-session-start"},
      {"command": "sidegraph-stop"}
    ]
  }
}
"""
    )

    result = verify_integration(tmp_path, HostKind.CODEX, codex_config=config)

    assert result.session_start == "verified"
    assert result.stop == "missing"
    assert result.next_action == (
        "Configure sidegraph-stop in .codex/hooks/hooks.json, then rerun sidegraph-bootstrap"
    )


def test_command_strings_survives_deeply_nested_config_without_recursion_error(
    tmp_path: Path,
) -> None:
    """Red against the recursive walk: a valid but deeply nested .mcp.json (a config file
    a user can edit by hand, or generate) made the old recursive _command_strings raise
    RecursionError well within what json.loads itself can parse -- reachable with no
    injection at all, from verify_integration alone, after apply_review has already
    written canonical state. The walk must be iterative so depth is bounded by heap, not
    the call stack."""
    from sidegraph.bootstrap.integrations import verify_integration

    depth = 5000
    nested = ('{"a":' * depth) + '{"command": "sidegraph-mcp"}' + ("}" * depth)
    mcp_path = tmp_path / ".mcp.json"
    mcp_path.write_text(nested, encoding="utf-8")

    result = verify_integration(tmp_path, HostKind.CLAUDE_CODE)

    assert result.mcp == "verified"


def _codex_plugin(path: Path, body: str = "enabled = true") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('[plugins."sidegraph@sidegraph"]\n' + body + "\n", encoding="utf-8")


def test_codex_plugin_in_codex_home_is_best_effort(tmp_path: Path, host_user_config: Path):
    from sidegraph.bootstrap.integrations import verify_integration

    _codex_plugin(host_user_config / "codex/config.toml")
    result = verify_integration(tmp_path, HostKind.CODEX)
    assert result.mcp == result.session_start == result.stop == "verified"
    assert result.pretool_read_grep == "unsupported"
    assert result.fully_supported is False
    assert result.next_action is None
    assert result.plugin == "sidegraph@sidegraph"


def test_codex_plugin_via_home_when_codex_home_unset(tmp_path: Path, monkeypatch):
    from sidegraph.bootstrap.integrations import verify_integration

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("CODEX_HOME")
    _codex_plugin(home / ".codex/config.toml")
    result = verify_integration(tmp_path, HostKind.CODEX)
    assert result.mcp == result.session_start == result.stop == "verified"
    assert result.plugin == "sidegraph@sidegraph"
    assert result.next_action is None


@pytest.mark.parametrize(
    "body", ["enabled = false", "", 'enabled = "true"'], ids=["disabled", "absent", "string"]
)
def test_codex_plugin_needs_explicit_enabled_true(tmp_path, host_user_config, body):
    """Over-reach guard; red on truthy/default-enabled plugin evidence."""
    from sidegraph.bootstrap.integrations import verify_integration

    _codex_plugin(host_user_config / "codex/config.toml", body)
    result = verify_integration(tmp_path, HostKind.CODEX)
    assert result.mcp == "missing"
    assert result.plugin is None
    assert result.next_action == (
        "Configure sidegraph-mcp in .codex/config.toml, then rerun sidegraph-bootstrap"
    )


def test_codex_malformed_user_config_is_no_evidence(tmp_path, host_user_config):
    from sidegraph.bootstrap.integrations import verify_integration

    (host_user_config / "codex/config.toml").write_text("[plugins\n", encoding="utf-8")
    result = verify_integration(tmp_path, HostKind.CODEX)
    assert result.mcp == "missing"
    assert result.plugin is None
    assert result.next_action == (
        "Configure sidegraph-mcp in .codex/config.toml, then rerun sidegraph-bootstrap"
    )


def test_codex_manual_ready_wins_over_plugin(codex_fixture, host_user_config):
    from sidegraph.bootstrap.integrations import verify_integration

    _codex_plugin(host_user_config / "codex/config.toml")
    result = verify_integration(codex_fixture, HostKind.CODEX)
    assert result.mcp == result.session_start == result.stop == "verified"
    assert result.plugin is None


def test_codex_host_config_paths_lists_user_config(tmp_path, host_user_config):
    from sidegraph.bootstrap.integrations import host_config_paths

    assert ("Codex user config", host_user_config / "codex/config.toml") in host_config_paths(
        tmp_path, HostKind.CODEX
    )


def _deny_config_stat(monkeypatch: pytest.MonkeyPatch, target: Path) -> None:
    """Fail beneath Path.is_file so Python 3.14 cannot hide the fixture's error."""
    real = os.stat

    def blocked(path, *args, **kwargs):
        if path == target or path == str(target):
            raise PermissionError("blocked")
        return real(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", blocked)


def test_codex_inaccessible_user_config_is_no_evidence(tmp_path, host_user_config, monkeypatch):
    from sidegraph.bootstrap.integrations import verify_integration

    _deny_config_stat(monkeypatch, host_user_config / "codex/config.toml")
    result = verify_integration(tmp_path, HostKind.CODEX)
    assert result.mcp == "missing"
    assert result.plugin is None


def test_codex_inaccessible_manual_config_still_reaches_plugin(
    tmp_path, host_user_config, monkeypatch
):
    from sidegraph.bootstrap.integrations import verify_integration

    _codex_plugin(host_user_config / "codex/config.toml")
    _deny_config_stat(monkeypatch, tmp_path / ".codex/config.toml")
    result = verify_integration(tmp_path, HostKind.CODEX)
    assert result.mcp == result.session_start == result.stop == "verified"
    assert result.plugin == "sidegraph@sidegraph"


def _claude_plugin(root: Path, plugins: dict[str, object]) -> None:
    path = root / ".claude/settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"enabledPlugins": plugins}) + "\n", encoding="utf-8")


def test_claude_plugin_only_repo_is_fully_supported(tmp_path: Path) -> None:
    from sidegraph.bootstrap.integrations import verify_integration

    _claude_plugin(tmp_path, {"sidegraph@sidegraph": True})
    result = verify_integration(tmp_path, HostKind.CLAUDE_CODE)
    assert result.fully_supported is True
    assert (
        result.mcp == result.session_start == result.stop == result.pretool_read_grep == "verified"
    )
    assert result.next_action is None
    assert result.plugin == "sidegraph@sidegraph"


def test_claude_partial_manual_falls_back_to_plugin(tmp_path: Path) -> None:
    from sidegraph.bootstrap.integrations import verify_integration

    (tmp_path / ".mcp.json").write_text("not JSON\n", encoding="utf-8")
    _claude_plugin(tmp_path, {"sidegraph@sidegraph": True})
    result = verify_integration(tmp_path, HostKind.CLAUDE_CODE)
    assert result.fully_supported is True
    assert result.next_action is None
    assert result.plugin == "sidegraph@sidegraph"


def test_manual_complete_wins_over_plugin(claude_fixture: Path, host_user_config: Path) -> None:
    from sidegraph.bootstrap.integrations import verify_integration

    (host_user_config / "claude/settings.json").write_text(
        '{"enabledPlugins": {"sidegraph@sidegraph": true}}\n', encoding="utf-8"
    )
    result = verify_integration(claude_fixture, HostKind.CLAUDE_CODE)
    assert result.fully_supported is True
    assert result.plugin is None


@pytest.mark.parametrize(
    "plugins",
    [
        {},
        {"sidegraph@sidegraph": False},
        {"sidegraph@sidegraph": "true"},
        {"sidegraph-extra@x": True},
    ],
)
def test_claude_plugin_overreach_guard(tmp_path: Path, plugins: dict[str, object]) -> None:
    """Red against nothing initially; prevents overly broad plugin evidence."""
    from sidegraph.bootstrap.integrations import verify_integration

    _claude_plugin(tmp_path, plugins)
    result = verify_integration(tmp_path, HostKind.CLAUDE_CODE)
    assert result.fully_supported is False
    assert result.plugin is None
    assert (
        result.next_action == "Configure sidegraph-mcp in .mcp.json, then rerun sidegraph-bootstrap"
    )


def test_claude_host_config_paths_lists_plugin_settings(tmp_path: Path, host_user_config: Path):
    from sidegraph.bootstrap.integrations import host_config_paths

    (tmp_path / ".git").mkdir()
    root = tmp_path / "pkg"
    root.mkdir()
    paths = host_config_paths(root, HostKind.CLAUDE_CODE)
    assert paths[:2] == (
        ("Claude Code MCP config", root / ".mcp.json"),
        ("Claude Code hooks config", root / ".claude/settings.json"),
    )
    assert ("Claude Code user settings", host_user_config / "claude/settings.json") in paths
    assert ("Claude Code local settings", root / ".claude/settings.local.json") in paths
    assert (
        "Claude Code repository local settings",
        tmp_path / ".claude/settings.local.json",
    ) in paths


@pytest.mark.parametrize("kind", ["permission", "too-deep"])
def test_claude_unreadable_manual_settings_still_reaches_user_plugin(
    tmp_path: Path, host_user_config: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    from sidegraph.bootstrap.integrations import verify_integration

    (host_user_config / "claude/settings.json").write_text(
        '{"enabledPlugins": {"sidegraph@sidegraph": true}}\n', encoding="utf-8"
    )
    if kind == "permission":
        _deny_config_stat(monkeypatch, tmp_path / ".claude/settings.json")
    else:
        path = tmp_path / ".claude/settings.json"
        path.parent.mkdir()
        path.write_text("[" * 50000 + "]" * 50000, encoding="utf-8")
    result = verify_integration(tmp_path, HostKind.CLAUDE_CODE)
    assert result.fully_supported is True
    assert result.plugin == "sidegraph@sidegraph"


@pytest.mark.parametrize(
    ("host", "rel", "field"),
    [
        ("claude-code", ".mcp.json", "mcp"),
        ("claude-code", ".claude/settings.json", "session_start"),
        ("codex", ".codex/config.toml", "mcp"),
        ("codex", ".codex/hooks.json", "session_start"),
    ],
)
def test_manual_stat_errors_are_invalid_on_every_python(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host: str, rel: str, field: str
) -> None:
    """Permission errors stay invalid even when pathlib suppresses them in is_file."""
    from sidegraph.bootstrap.integrations import verify_integration

    root = tmp_path / "repo"
    shutil.copytree(FIXTURES / ("claude" if host == "claude-code" else "codex"), root)
    _deny_config_stat(monkeypatch, root / rel)
    result = verify_integration(root, HostKind(host))
    assert getattr(result, field) == "invalid"
    assert result.plugin is None
    assert result.next_action == f"Repair {rel}, then rerun sidegraph-bootstrap"


def test_inaccessible_canonical_hooks_do_not_select_legacy(
    tmp_path: Path, host_user_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sidegraph.bootstrap.integrations import verify_integration

    root = tmp_path / "repo"
    shutil.copytree(FIXTURES / "codex", root)
    legacy = root / ".codex/hooks/hooks.json"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    (root / ".codex/hooks.json").write_bytes(legacy.read_bytes())
    _codex_plugin(host_user_config / "codex/config.toml")
    _deny_config_stat(monkeypatch, root / ".codex/hooks.json")
    result = verify_integration(root, HostKind.CODEX)
    assert result.plugin == "sidegraph@sidegraph"
    assert result.mcp == result.session_start == result.stop == "verified"


def test_inaccessible_legacy_hooks_names_the_legacy_repair_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sidegraph.bootstrap.integrations import verify_integration

    root = tmp_path / "repo"
    shutil.copytree(FIXTURES / "codex", root)
    (root / ".codex/hooks.json").unlink(missing_ok=True)
    _deny_config_stat(monkeypatch, root / ".codex/hooks/hooks.json")
    result = verify_integration(root, HostKind.CODEX)
    assert result.mcp == "verified"
    assert result.session_start == result.stop == "invalid"
    assert result.plugin is None
    assert result.next_action == ("Repair .codex/hooks/hooks.json, then rerun sidegraph-bootstrap")
