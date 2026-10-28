"""markdown-it-py is imported directly by md_sections, so wheels must require it."""

import tomllib
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def test_markdown_it_is_a_declared_runtime_requirement() -> None:
    deps = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["dependencies"]
    pinned = [d for d in deps if d.startswith("markdown-it-py")]
    assert len(pinned) == 1
    assert ">=4" in pinned[0] and "<5" in pinned[0]
