"""markdown-it-py is imported directly by md_sections, so wheels must require it.

The check reads ``[project].dependencies`` of both shapes the file takes: the ranged internal
file and the exactly pinned file of the public snapshot.
"""

import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"


def _markdown_it_requirement(dependencies: list[str]) -> Requirement:
    found = [Requirement(d) for d in dependencies if d.startswith("markdown-it-py")]
    assert len(found) == 1
    return found[0]


def _dependencies() -> list[str]:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["dependencies"]


def _locked_version() -> str:
    """The markdown-it-py version of the repository's lock: what a pinned snapshot declares."""
    packages = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))["package"]
    return next(p["version"] for p in packages if p["name"] == "markdown-it-py")


def _pinned(dependencies: list[str]) -> list[str]:
    """The snapshot's shape: the markdown-it-py entry as an exact pin (the other entries do not
    matter here)."""
    pin = f"markdown-it-py=={_locked_version()}"
    return [pin if d.startswith("markdown-it-py") else d for d in dependencies]


@pytest.mark.parametrize("shape", ["ranged", "pinned"])
def test_markdown_it_is_a_declared_runtime_requirement(shape: str) -> None:
    dependencies = _dependencies() if shape == "ranged" else _pinned(_dependencies())
    specifier = _markdown_it_requirement(dependencies).specifier
    assert specifier.contains(_locked_version())
    assert not specifier.contains("3.9.9")
    assert not specifier.contains("5.0.0")
