"""Kimi Code: the root manifest ships the skills, and the guide carries the MCP entry users add.

The manifest declares no MCP server on purpose. Kimi Code starts a plugin's server in the
plugin's own folder, so a bundled server cannot find the project's store.
"""

import json
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / ".kimi-plugin" / "plugin.json"
GUIDE = ROOT / "docs" / "integrations" / "kimi.md"


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


def test_kimi_manifest_declares_no_mcp_server_and_no_hooks() -> None:
    """A bundled server would start in the plugin's folder and read no project's store."""
    manifest = _manifest()
    assert "mcpServers" not in manifest
    assert "hooks" not in manifest


def test_the_guide_entry_launches_the_pin_in_the_session_directory() -> None:
    """The copied entry is the whole MCP setup. `cwd` or `env` would tie it to one project."""
    blocks = re.findall(r"```json\n(.*?)```", GUIDE.read_text(encoding="utf-8"), re.DOTALL)
    assert len(blocks) == 1
    assert json.loads(blocks[0]) == {
        "mcpServers": {
            "sidegraph": {
                "command": "uvx",
                "args": [
                    "--from",
                    f"sidegraph=={_version()}",
                    "--python",
                    "3.13",
                    "sidegraph-mcp",
                    "--repo-root",
                ],
            }
        }
    }
