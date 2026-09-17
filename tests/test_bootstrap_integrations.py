from pathlib import Path

import pytest

from sidegraph.bootstrap.model import HostKind

FIXTURES = Path(__file__).parent / "fixtures" / "bootstrap" / "hosts"


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
