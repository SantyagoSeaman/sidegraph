"""The README's tool and skill counts (its badges and the "What you can do" line) match what ships.

Red against a new MCP tool or skill added without updating the README.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

from sidegraph.server import mcp

ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")


def _tool_count() -> int:
    return len(asyncio.run(mcp.list_tools()))


def _skill_count() -> int:
    return len(list((ROOT / "plugin" / "sidegraph" / "skills").glob("*/SKILL.md")))


def test_the_mcp_tools_badge_and_line_match_the_server() -> None:
    tools = _tool_count()
    assert re.findall(r"img\.shields\.io/badge/MCP_tools-(\d+)-", README) == [str(tools)]
    mentions = re.findall(r"(\d+) MCP tools", README)
    assert mentions and set(mentions) == {str(tools)}


def test_the_skills_badge_and_line_match_the_plugin() -> None:
    skills = _skill_count()
    assert re.findall(r"img\.shields\.io/badge/skills-(\d+)-", README) == [str(skills)]
    mentions = re.findall(r"(\d+) skills ·", README) + re.findall(r"All (\d+) skills", README)
    assert len(mentions) >= 2 and set(mentions) == {str(skills)}
