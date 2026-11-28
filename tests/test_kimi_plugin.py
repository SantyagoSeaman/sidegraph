"""The Kimi Code manifest at the repository root: shared skills and the pinned MCP launch."""

import json
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / ".kimi-plugin" / "plugin.json"


def _manifest() -> dict:
    assert MANIFEST.is_file(), "Kimi installs from the repository root and needs this manifest"
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def _version() -> str:
    return tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]


def test_kimi_version_agrees_with_python_package() -> None:
    assert _manifest()["version"] == _version()


def test_kimi_points_at_the_shared_skills() -> None:
    """Kimi requires a `./` path inside the plugin root; the skills live under plugin/sidegraph."""
    skills = _manifest()["skills"]
    assert skills.startswith("./")
    skills_dir = (ROOT / skills).resolve()
    assert skills_dir == (ROOT / "plugin" / "sidegraph" / "skills").resolve()
    assert list(skills_dir.glob("*/SKILL.md"))


def test_kimi_mcp_launches_the_pin_without_a_shell() -> None:
    """The server is an inline stdio entry on the exact PyPI pin, like every other host."""
    servers = _manifest()["mcpServers"]
    assert list(servers) == ["sidegraph"]
    assert servers["sidegraph"] == {
        "transport": "stdio",
        "command": "uvx",
        "args": [
            f"--from=sidegraph=={_version()}",
            "--python",
            "3.13",
            "sidegraph-mcp",
            "--repo-root",
        ],
    }


def test_kimi_declares_no_hooks() -> None:
    """Basic integration only: MCP and skills. A hook is advertised only once it is proved."""
    assert "hooks" not in _manifest()
