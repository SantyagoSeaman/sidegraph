"""Tier-1 consumption of a tier-2-frozen artifact (design/superpowers/specs/
2026-07-25-tier2-harness-design.md §3).

The live tier produces, the deterministic tier consumes. This file is the consuming half:
it asserts a promoted render byte-for-byte, so a diff here means retrieval's output
actually changed — never that a non-deterministic run wobbled.

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


def test_at_least_one_render_is_promoted() -> None:
    assert _renders(), f"{_EXPECTED} exists but holds no *.md renders"


def test_every_render_is_non_empty_and_newline_terminated() -> None:
    """Cheap corruption guard: a truncated or empty promote would otherwise sit here
    looking like a passing pin."""
    for path in _renders():
        text = path.read_text(encoding="utf-8")
        assert text.strip(), path
        assert text.endswith("\n"), path


def test_renders_carry_the_retrieval_shape() -> None:
    """The artifact must be a get_task_context render, not some other file that happened
    to land in expected/. Section headings are the shape retrieval always emits."""
    for path in _renders():
        text = path.read_text(encoding="utf-8")
        assert "##" in text, f"{path}: no section heading — is this a retrieval render?"
