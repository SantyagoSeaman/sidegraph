"""Keep the selected Graphify pin on every shipped install and verification surface."""

from __future__ import annotations

import json
import re
from pathlib import Path

from sidegraph.engine import reader

_ROOT = Path(__file__).resolve().parents[1]
_PIN_RE = re.compile(r"graphifyy(?:\[[^]]+\])?==([0-9.]+)")
_PIN_SURFACES = (
    "docs/getting-started/installation.md",
    "plugin/sidegraph/skills/setup/SKILL.md",
    ".github/workflows/ci.yml",
)
_LINK_SURFACES = [
    *_PIN_SURFACES,
    "CLAUDE.md",
    "docs/integrations/graphify.md",
    "docs/concepts/decision-memory.md",
]
# Public releases replace this internal twin with the mandatory CLAUDE.md above.
if (_ROOT / "CLAUDE.public.md").is_file():
    _LINK_SURFACES.append("CLAUDE.public.md")


def _pins() -> set[str]:
    found: set[str] = set()
    for rel in _PIN_SURFACES:
        versions = set(_PIN_RE.findall((_ROOT / rel).read_text(encoding="utf-8")))
        assert versions, f"no Graphify pin in {rel}"
        found |= versions
    for path in (_ROOT / "docs").rglob("*.md"):
        found |= set(_PIN_RE.findall(path.read_text(encoding="utf-8")))
    return found


def test_the_pin_outside_docs_matches_docs_and_is_a_tested_engine() -> None:
    pins = _pins()
    assert len(pins) == 1, pins
    assert pins <= reader.TESTED_ENGINE_VERSIONS, (pins, reader.TESTED_ENGINE_VERSIONS)


def test_no_surface_links_the_old_upstream() -> None:
    for rel in _LINK_SURFACES:
        assert "safishamsi/graphify" not in (_ROOT / rel).read_text(encoding="utf-8"), rel


def test_promoted_corpus_versions_match_the_pin() -> None:
    pins = _pins()
    assert len(pins) == 1, pins
    version = next(iter(pins))
    for corpus_id in ("self-corpus", "openspec"):
        corpus = _ROOT / "tests" / "fixtures" / "corpus" / corpus_id
        provenance = json.loads((corpus / "_provenance.json").read_text(encoding="utf-8"))
        graph = json.loads((corpus / "graph.json").read_text(encoding="utf-8"))
        assert provenance["graphify_version"] == f"graphify {version}", corpus_id
        assert graph["graph"]["graphify_version"] == version, corpus_id
