"""Portable results for Agent view preparation and tool emulation."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Preparation:
    """Effects of preparing a view.
    See design/superpowers/specs/2026-11-17-portal-design.md (D10).
    """

    synced: bool
    adopted_moves: int
    skipped_reason: str | None


@dataclass(frozen=True)
class ToolEmulation:
    """The response text and preparation performed for it.
    See design/superpowers/specs/2026-11-17-portal-design.md (D10).
    """

    text: str
    prepared: Preparation
    budget: int | None
    cut: str | None
