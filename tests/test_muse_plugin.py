"""The experimental native Muse package exposes shared skills and a direct MCP entry."""

import json
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugin" / "sidegraph"


def _manifest() -> dict:
    path = PLUGIN / ".muse-plugin" / "plugin.json"
    assert path.is_file(), "The native Muse manifest must be packaged"
    return json.loads(path.read_text(encoding="utf-8"))


def test_native_muse_version_agrees_with_python_package() -> None:
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    assert _manifest()["version"] == version


def test_native_muse_exposes_every_shared_skill() -> None:
    """Omitting a shared skill must make its absence visible before release."""
    manifest = _manifest()
    expected = {
        path.parent.name: path.relative_to(PLUGIN).as_posix()
        for path in (PLUGIN / "skills").glob("*/SKILL.md")
    }
    skills = manifest["capabilities"]["skills"]
    actual = {item["id"]: item["path"] for item in skills}
    assert expected
    assert actual == expected
    assert len(skills) == len(actual), "Duplicate skill ids lose native registrations"


def test_native_muse_registers_the_proved_lifecycle_hooks() -> None:
    """Only the shipped stage is declared: SessionStart and SubagentStart, each its own source.

    Muse rejects two hooks on one file (``duplicate-hook-source``), so each event has an entry
    file and ``hook.py`` is shared, never declared. An unproved event is not advertised.
    see design/superpowers/specs/2026-10-28-muse-hooks-design.md (Adapter, Manifest)
    """
    hooks = _manifest()["capabilities"]["hooks"]
    assert [(h["id"], h["event"], h["command"]) for h in hooks] == [
        ("session-start", "SessionStart", ["python3", "muse/session_start.py"]),
        ("subagent-start", "SubagentStart", ["python3", "muse/subagent_start.py"]),
    ]
    sources = [h["command"][1] for h in hooks]
    assert len(set(sources)) == len(sources)
    assert "muse/hook.py" not in sources
    assert all((PLUGIN / source).is_file() for source in sources)
    assert all(isinstance(h["timeoutMs"], int) and h["timeoutMs"] > 1000 for h in hooks)


def test_native_muse_mcp_uses_the_public_runtime_without_a_shell() -> None:
    """The native server must start without sh or a foreign project placeholder."""
    manifest = _manifest()
    assert manifest["schemaVersion"] == 1
    assert manifest["compat"] == {"source": "native", "manifestDir": ".muse-plugin"}
    servers = manifest["capabilities"]["mcpServers"]
    assert len(servers) == 1
    server = servers[0]
    assert server["id"] == "sidegraph"
    assert server["command"] == [
        "uvx",
        "--from=git+https://github.com/SantyagoSeaman/sidegraph.git@main",
        "sidegraph-mcp",
        "--repo-root",
    ]
    assert server["env"] == {
        "SIDEGRAPH_DIR": ".sidegraph",
        "SIDEGRAPH_GRAPH": "graphify-out/graph.json",
    }
    assert "cwd" not in server
