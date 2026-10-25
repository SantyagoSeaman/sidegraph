"""Tier-1 consumption of a tier-2-frozen artifact (design/superpowers/specs/
2026-07-25-tier2-harness-design.md §3).

The live tier produces, the deterministic tier consumes. This file is the consuming half,
and it is only a corruption guard: it checks that the promoted renders exist, are non-empty
and newline-terminated, and carry a section heading. It does not re-render anything and
compare bytes, so it cannot tell that retrieval's output changed.

Skips cleanly until tier-2 has run and promoted something. That matters: the main repo
must never depend on the sandbox being present, which is the whole point of the split.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_EXPECTED = Path(__file__).resolve().parent / "fixtures" / "corpus" / "self-corpus" / "expected"

pytestmark = pytest.mark.skipif(
    not _EXPECTED.is_dir(),
    reason="no frozen renders promoted yet — tier-2 has not run for this corpus",
)


def _renders() -> list[Path]:
    return sorted(_EXPECTED.glob("*.md"))


def test_promoted_renders_are_intact() -> None:
    """Cheap corruption guard: a truncated or empty promote would otherwise sit here looking
    like a passing pin. Each render must also be a get_task_context render, not some other
    file that happened to land in expected/: section headings are the shape retrieval always
    emits."""
    renders = _renders()
    assert renders, f"{_EXPECTED} exists but holds no *.md renders"
    for path in renders:
        text = path.read_text(encoding="utf-8")
        assert text.strip(), path
        assert text.endswith("\n"), path
        assert "##" in text, f"{path}: no section heading — is this a retrieval render?"
