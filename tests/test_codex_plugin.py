"""Codex-side plugin packaging contract.

Sidegraph ships two plugin manifests that must stay in lockstep: the Claude Code plugin
(`.claude-plugin/marketplace.json` + `plugin/sidegraph/.claude-plugin/plugin.json`) and the
Codex plugin added in the same commit (`.agents/plugins/marketplace.json` +
`plugin/sidegraph/.codex-plugin/plugin.json`). These checks are the mechanical guard that
the two never drift apart: same plugin directory, same version as `pyproject.toml`, a Codex
MCP config that parses, and an `agents/openai.yaml` beside every skill declaring the
`sidegraph` MCP dependency. See `docs/integrations/codex.md#plugin-install-path` for what was
verified live against a real Codex CLI install (codex-cli 0.154.0) versus what this test
suite alone can prove offline.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parent.parent
_PLUGIN_DIR = _ROOT / "plugin" / "sidegraph"
_CLAUDE_MARKETPLACE = _ROOT / ".claude-plugin" / "marketplace.json"
_CODEX_MARKETPLACE = _ROOT / ".agents" / "plugins" / "marketplace.json"
_CLAUDE_PLUGIN_JSON = _PLUGIN_DIR / ".claude-plugin" / "plugin.json"
_CODEX_PLUGIN_JSON = _PLUGIN_DIR / ".codex-plugin" / "plugin.json"
_PYPROJECT = _ROOT / "pyproject.toml"
_SKILLS_DIR = _PLUGIN_DIR / "skills"


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text())


def test_both_marketplaces_point_at_the_same_plugin_dir() -> None:
    """Claude's marketplace uses a plain string `source`; Codex's uses `{source, path}` —
    both must resolve to `./plugin/sidegraph`, the one plugin directory both hosts share."""
    claude = _load_json(_CLAUDE_MARKETPLACE)
    codex = _load_json(_CODEX_MARKETPLACE)

    assert claude["plugins"][0]["source"] == "./plugin/sidegraph"

    codex_source = codex["plugins"][0]["source"]
    assert codex_source == {"source": "local", "path": "./plugin/sidegraph"}

    assert claude["plugins"][0]["name"] == codex["plugins"][0]["name"] == "sidegraph"


def test_codex_marketplace_plugin_dir_exists() -> None:
    codex = _load_json(_CODEX_MARKETPLACE)
    rel_path = codex["plugins"][0]["source"]["path"]
    assert (_ROOT / rel_path.removeprefix("./")).is_dir()


def test_three_version_strings_agree() -> None:
    """pyproject.toml, the Claude plugin.json, and the Codex plugin.json must ship the same
    version — a mismatch here means one manifest was edited and the others weren't."""
    pyproject_version = tomllib.loads(_PYPROJECT.read_text())["project"]["version"]
    claude_version = _load_json(_CLAUDE_PLUGIN_JSON)["version"]
    codex_version = _load_json(_CODEX_PLUGIN_JSON)["version"]

    assert claude_version == pyproject_version
    assert codex_version == pyproject_version


def test_package_version_agrees_with_pyproject() -> None:
    """`sidegraph.__version__` must also track pyproject.toml — a fourth place the version
    string can drift out from under the three manifests above."""
    import sidegraph

    pyproject_version = tomllib.loads(_PYPROJECT.read_text())["project"]["version"]
    assert sidegraph.__version__ == pyproject_version


def test_codex_plugin_json_fields() -> None:
    plugin = _load_json(_CODEX_PLUGIN_JSON)
    assert plugin["name"] == "sidegraph"
    assert plugin["skills"] == "./skills/"
    assert plugin["mcpServers"] == "./codex/mcp.json"
    assert plugin["hooks"] == "./codex/hooks.json"
    assert plugin["interface"]["displayName"] == "Sidegraph"
    # Resolved relative to the plugin root (../.codex-plugin/plugin.json's directory is
    # plugin/sidegraph/.codex-plugin, but the manifest's own "./" fields are plugin-root
    # relative, the same convention the reference tfmodsearch plugin uses).
    assert (_PLUGIN_DIR / "skills").is_dir()
    assert (_PLUGIN_DIR / "codex" / "mcp.json").is_file()
    assert (_PLUGIN_DIR / "codex" / "hooks.json").is_file()


_MCP = _PLUGIN_DIR / "codex" / "mcp.json"
_MCP_PUBLIC = _PLUGIN_DIR / "codex" / "mcp.public.json"
_HOOKS = _PLUGIN_DIR / "codex" / "hooks.json"
_HOOKS_PUBLIC = _PLUGIN_DIR / "codex" / "hooks.public.json"


def test_codex_mcp_config_parses_and_names_sidegraph() -> None:
    """The base file (`mcp.json`) ships on both sides: in this repo it is the dev form, and
    in the public snapshot `tools/release-public.sh` has already renamed `mcp.public.json`
    onto it, so the dev form is gone. Either way it must parse, key its server 'sidegraph',
    and launch `sidegraph-mcp`. Also check the `.public.` twin here when it exists (this
    repo only) rather than in a separate pass over the same file."""
    for path in (p for p in (_MCP, _MCP_PUBLIC) if p.is_file()):
        config = _load_json(path)
        assert "sidegraph" in config, f"{path.name} must key its server as 'sidegraph'"
        entry = config["sidegraph"]
        assert entry["command"]
        assert isinstance(entry["args"], list) and entry["args"]
        launch = " ".join(entry["args"])
        assert "sidegraph-mcp" in launch, f"{path.name} does not launch sidegraph-mcp"


def test_codex_mcp_config_public_twin_uses_uvx_not_uv_run() -> None:
    """Same `.public.` convention `tools/release-public.sh` already applies to
    `plugin/sidegraph/.mcp.public.json`: the dev variant runs the working tree via `uv run`,
    the public variant installs straight from the public repo via `uvx --from git+...`.

    In this repo both twins coexist, so compare them directly. In the public snapshot the
    release script has already renamed `mcp.public.json` onto `mcp.json` and removed the dev
    form, so there is nothing to compare `mcp.json` against — but the substitution itself
    guarantees `mcp.json` is the public form, and that is exactly what this asserts instead
    of skipping."""
    base_command = " ".join(_load_json(_MCP)["sidegraph"]["args"])
    if _MCP_PUBLIC.is_file():
        public_command = " ".join(_load_json(_MCP_PUBLIC)["sidegraph"]["args"])
        assert "uv run" in base_command and "uvx" not in base_command
        assert "uvx --from git+" in public_command and "uv run" not in public_command
    else:
        assert "uvx --from git+" in base_command and "uv run" not in base_command


def test_codex_hooks_config_parses() -> None:
    """Same shape as test_codex_mcp_config_parses_and_names_sidegraph above: the base
    `hooks.json` ships on both sides and must always parse with SessionStart/Stop hooks that
    cd to the repo root first."""
    for path in (p for p in (_HOOKS, _HOOKS_PUBLIC) if p.is_file()):
        hooks = _load_json(path)["hooks"]
        assert set(hooks.keys()) == {"SessionStart", "Stop"}
        for event_hooks in hooks.values():
            command = event_hooks[0]["hooks"][0]["command"]
            assert "git rev-parse --show-toplevel" in command


def test_every_skill_has_openai_yaml_naming_sidegraph_dependency() -> None:
    skill_dirs = sorted(p for p in _SKILLS_DIR.iterdir() if (p / "SKILL.md").is_file())
    assert len(skill_dirs) == 12, f"expected 12 skills, found {len(skill_dirs)}"

    for skill_dir in skill_dirs:
        openai_yaml = skill_dir / "agents" / "openai.yaml"
        assert openai_yaml.is_file(), f"{skill_dir.name} is missing agents/openai.yaml"
        manifest = yaml.safe_load(openai_yaml.read_text())
        assert isinstance(manifest["policy"]["allow_implicit_invocation"], bool)
        tools = manifest["dependencies"]["tools"]
        assert any(t.get("type") == "mcp" and t.get("value") == "sidegraph" for t in tools), (
            f"{skill_dir.name}/agents/openai.yaml does not declare the sidegraph MCP dependency"
        )


def test_implicit_invocation_matches_human_driven_vs_agent_observed_split() -> None:
    """Onboarding/maintenance skills a human explicitly asks for stay `false`; skills meant
    to fire from something the agent observes on its own (a plan forming, a `SessionStart`/
    sync signal, a hard-won lesson mid-session) stay `true` — the call is drawn from each
    skill's own SKILL.md trigger text, not a fixed list."""
    human_driven = {"setup", "import-adrs", "name-domains", "manage-domains", "stats"}
    agent_observed = {
        "check-plan",
        "explain-why",
        "heal-anchors",
        "ratify-decisions",
        "record-decision",
        "record-fact",
        "triage-drift",
    }
    assert human_driven | agent_observed == {p.name for p in _SKILLS_DIR.iterdir() if p.is_dir()}

    for name in human_driven:
        manifest = yaml.safe_load((_SKILLS_DIR / name / "agents" / "openai.yaml").read_text())
        assert manifest["policy"]["allow_implicit_invocation"] is False, name

    for name in agent_observed:
        manifest = yaml.safe_load((_SKILLS_DIR / name / "agents" / "openai.yaml").read_text())
        assert manifest["policy"]["allow_implicit_invocation"] is True, name
