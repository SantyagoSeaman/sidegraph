"""The compact skill runs the CLI in the order the packed-store design fixes (§6.5)."""

from __future__ import annotations

from pathlib import Path

import yaml

SKILL = Path("plugin/sidegraph/skills/compact/SKILL.md")


def test_skill_contract() -> None:
    t = SKILL.read_text()
    low = t.lower()
    assert t.index("sidegraph-compact --dry-run") < t.index("confirm")
    assert "sidegraph-migrate --dry-run" in t and "team checklist" in t
    assert "explicit yes" in low and "sidegraph-verify" in t and "verbatim" in low
    assert "never commit" in low and "--any-branch" in t  # only when the human asks
    assert "mcp__" not in t and "verify_store" not in t  # runs the CLI, never an MCP tool
    assert (SKILL.parent / "agents" / "openai.yaml").exists()


def test_skill_is_explicit_only_and_named() -> None:
    meta = yaml.safe_load(SKILL.read_text().split("---")[1])
    assert meta["name"] == "compact"
    assert "/sidegraph:compact" in meta["description"]
    twin = yaml.safe_load((SKILL.parent / "agents" / "openai.yaml").read_text())
    assert twin["policy"]["allow_implicit_invocation"] is False
