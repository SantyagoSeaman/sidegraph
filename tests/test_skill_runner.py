"""The plugin skills run their CLIs through the exact PyPI pin on every host.

Every command line in a shared skill is written whole with the pinned launch
(``uvx --from sidegraph==<V> --python 3.13 <cli>``), and each skill carries one fallback
sentence for a Sidegraph checkout, which runs itself. Reads only ``plugin/`` and
``pyproject.toml``.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_SKILLS_DIR = _ROOT / "plugin" / "sidegraph" / "skills"
_VERSION = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
    "version"
]

# The launch literals are duplicated here on purpose: a shipped test never reads tools/.
RUNNER = f"uvx --from sidegraph=={_VERSION} --python 3.13"
CHECKOUT = "uv run --project <checkout> --package sidegraph --frozen --no-active"
FALLBACK = f"Inside a Sidegraph checkout, use `{CHECKOUT} <cli>` instead."

# Skills that never run a CLI: they name no ``sidegraph-<letter>`` command and no ``<command>``.
NO_CLI_SKILLS = {"check-plan", "explain-why", "health-check", "record-fact"}

# Full command lines per skill (fenced or inline), each written with the runner.
EXPECTED_RUNNER_LINES = {
    "compact": 4,
    "heal-anchors": 3,
    "import-adrs": 4,
    "manage-domains": 1,
    "name-domains": 1,
    "ratify-decisions": 1,
    "record-decision": 1,
    "setup": 6,
    "stats": 1,
    "triage-drift": 1,
}

_CLI_TOKEN = re.compile(r"sidegraph-[a-z]|<command>")


def _all_skills() -> dict[str, str]:
    return {
        p.parent.name: p.read_text(encoding="utf-8") for p in sorted(_SKILLS_DIR.glob("*/SKILL.md"))
    }


def _cli_skills() -> dict[str, str]:
    return {n: t for n, t in _all_skills().items() if _CLI_TOKEN.search(t)}


def _split(text: str) -> tuple[list[str], list[str]]:
    """Fenced lines and inline code spans of a skill body, fallback sentence removed."""
    text = text.replace(FALLBACK, "")
    fenced: list[str] = []
    prose: list[str] = []
    inside = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            inside = not inside
        elif inside:
            fenced.append(line.strip())
        else:
            prose.append(line)
    spans = re.findall(r"`([^`\n]+)`", "\n".join(prose))
    return fenced, spans


def test_the_no_cli_list_is_exact() -> None:
    skills = _all_skills()
    for name in NO_CLI_SKILLS:
        assert name in skills, f"{name} is not a skill any more"
        assert not _CLI_TOKEN.search(skills[name]), f"{name} names a CLI: take it off the list"
    assert set(_cli_skills()) == set(EXPECTED_RUNNER_LINES)


@pytest.mark.parametrize("name", sorted(EXPECTED_RUNNER_LINES))
def test_skill_runs_its_clis_through_the_pinned_launch(name: str) -> None:
    text = _cli_skills()[name]
    fenced, spans = _split(text)

    # (a) Nothing in the skill reaches back to the branch, the bundled runtime or a uv setting.
    for needle in (
        "git+",
        "${CLAUDE_PLUGIN_ROOT}",
        "--project",
        "UV_PROJECT_ENVIRONMENT",
        "UV_PYTHON",
    ):
        assert needle not in text.replace(FALLBACK, ""), f"{name}: carries {needle!r}"

    # (b) Every line or code span that launches something starts with the pinned launch.
    for item in [*fenced, *spans]:
        # A span that merely names the tool (`uv run`, `uvx`) launches nothing.
        if re.search(r"\buv run \S|\buvx --from", item):
            assert item.startswith(RUNNER), f"{name}: launches without the pin: {item[:90]}"
    for line in fenced:
        assert not re.match(r"(sidegraph-[a-z]|<command>)", line), f"{name}: bare line: {line[:60]}"
    runner_lines = [i for i in [*fenced, *spans] if i.startswith(RUNNER)]
    assert len(runner_lines) == EXPECTED_RUNNER_LINES[name], f"{name}: {len(runner_lines)}"

    # (c) The fallback sentence appears exactly once.
    assert text.count(FALLBACK) == 1, f"{name}: fallback sentence count {text.count(FALLBACK)}"


def test_fallback_sentence_content() -> None:
    # (d) No placeholder in it: Claude would substitute it and the test would read true.
    assert "${" not in FALLBACK
    # (e) It names the checkout run and nothing that reaches the network.
    assert CHECKOUT in FALLBACK
    assert "uvx" not in FALLBACK and "git+" not in FALLBACK


def test_runner_count_across_skills() -> None:
    total = sum(t.count(RUNNER) for t in _cli_skills().values())
    assert total >= 23


def test_no_skill_names_the_bundled_runtime() -> None:
    """The plugin ships no runtime folder any more: no skill may send the reader to one."""
    for name, text in _all_skills().items():
        assert "bundled runtime" not in text, name
        assert "plugin/runtime" not in text and '/runtime"' not in text, name
