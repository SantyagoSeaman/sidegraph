"""Minimal accepted Decision/Fact/Domain builders for store tests.

Support module (no tests): shared by the mutation-guard and cross-process mutation tests.
# see design/data-model.md
"""

from __future__ import annotations

from datetime import UTC, datetime

from sidegraph.schema import (
    Decision,
    DecisionKind,
    DecisionStatus,
    Domain,
    Fact,
    Provenance,
)


def _decision(title: str = "an adr", **overrides) -> Decision:
    base = dict(
        title=title,
        kind=DecisionKind.ADR,
        status=DecisionStatus.ACCEPTED,
        context="c",
        choice="ch",
        valid_from=datetime.now(UTC),
        provenance=Provenance(source="manual"),
    )
    base.update(overrides)
    return Decision(**base)


def _fact(statement: str = "a fact", **overrides) -> Fact:
    base = dict(
        statement=statement,
        source="test",
        status=DecisionStatus.ACCEPTED,
        valid_from=datetime.now(UTC),
        provenance=Provenance(source="manual"),
    )
    base.update(overrides)
    return Fact(**base)


def _domain(slug: str = "a-domain", **overrides) -> Domain:
    base = dict(
        slug=slug,
        title=slug,
        summary="A domain used for mutation-guard testing.",
        provenance=Provenance(source="manual"),
    )
    base.update(overrides)
    return Domain(**base)
