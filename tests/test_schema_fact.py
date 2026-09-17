"""Fact model invariants. see design/superpowers/specs/2026-07-10-facts-layer-design.md"""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from sidegraph.schema import DecisionStatus, Fact, Provenance

NOW = datetime.now(UTC)


def _fact(**kw):
    base = dict(
        statement="Leiden renumbers communities on every rebuild",
        source="observed live on a real project corpus 2026-07-06",
        valid_from=NOW,
        provenance=Provenance(source="test"),
    )
    base.update(kw)
    return Fact(**base)


def test_fact_defaults():
    f = _fact()
    assert f.status == DecisionStatus.PROPOSED
    assert f.supports == []
    assert f.valid_to is None and f.supersedes is None
    assert len(f.id) == 26  # ULID


def test_fact_validity_window_enforced():
    with pytest.raises(ValidationError):
        _fact(valid_to=NOW - timedelta(days=1))


def test_fact_requires_nonempty_statement_and_source():
    with pytest.raises(ValidationError):
        _fact(statement="   ")
    with pytest.raises(ValidationError):
        _fact(source="")
