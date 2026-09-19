"""The skill must show the output, not narrate it (design D10)."""

from __future__ import annotations

from pathlib import Path

import yaml

SKILL = Path("plugin/sidegraph/skills/stats/SKILL.md")


def test_the_skill_exists_with_a_codex_twin():
    assert SKILL.exists()
    assert (SKILL.parent / "agents" / "openai.yaml").exists()


def test_the_skill_forbids_narrating_the_numbers():
    text = SKILL.read_text().lower()
    assert "verbatim" in text
    assert "causal" in text


def test_the_skill_names_the_command_it_runs():
    assert "sidegraph-stats" in SKILL.read_text()


def test_the_skill_frontmatter_names_it_and_is_explicit_only():
    """Same field set as the siblings; `stats` is something a person asks for, never something
    an agent should volunteer mid-task, so its Codex twin is explicit-invocation only."""
    head = SKILL.read_text().split("---")[1]
    meta = yaml.safe_load(head)
    assert meta["name"] == "stats"
    assert "/sidegraph:stats" in meta["description"]
    twin = yaml.safe_load((SKILL.parent / "agents" / "openai.yaml").read_text())
    assert twin["policy"]["allow_implicit_invocation"] is False
