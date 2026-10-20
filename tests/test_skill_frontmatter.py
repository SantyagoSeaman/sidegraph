"""Shipped skills expose valid YAML metadata to hosts that parse frontmatter strictly."""

from pathlib import Path

import pytest
import yaml

_SKILLS = Path(__file__).resolve().parent.parent / "plugin" / "sidegraph" / "skills"
_EXPECTED = {
    "check-plan",
    "explain-why",
    "heal-anchors",
    "health-check",
    "import-adrs",
    "manage-domains",
    "name-domains",
    "ratify-decisions",
    "record-decision",
    "record-fact",
    "setup",
    "stats",
    "triage-drift",
}
_FILES = sorted(_SKILLS.glob("*/SKILL.md"))


def test_all_shipped_skills_are_in_the_frontmatter_scan() -> None:
    assert {path.parent.name for path in _FILES} == _EXPECTED


@pytest.mark.parametrize("path", _FILES, ids=lambda path: path.parent.name)
def test_shipped_skill_frontmatter_is_valid_yaml(path: Path) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "---"
    end = lines.index("---", 1)
    metadata = yaml.safe_load("\n".join(lines[1:end]))
    assert isinstance(metadata, dict)
    assert metadata["name"] == path.parent.name
    assert isinstance(metadata["description"], str)
    assert metadata["description"].strip()
