"""Domain model invariants — these ARE the contract (see CLAUDE.md,
docs/concepts/mind-model.md#domain-lifecycle)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sidegraph.schema import Descriptor, Domain, DomainStatus, Provenance, matches_path_prefix


def _domain(**overrides) -> Domain:
    base = dict(
        slug="payments",
        title="Payments",
        summary="Handles order settlement and refunds.",
        provenance=Provenance(source="manual"),
    )
    base.update(overrides)
    return Domain(**base)


def test_domain_defaults() -> None:
    d = _domain()
    assert d.status == DomainStatus.PROPOSED
    assert d.parent_id is None
    assert d.supersedes is None
    assert d.communities == []
    assert d.path_prefixes == []
    assert d.seed_anchors == []
    assert d.domain_id  # minted


# -- seed_anchors (§2a amendment: durable domain membership via entity anchors, mirrors
# a decision's own anchoring) ---------------------------------------------------------


def test_domain_accepts_seed_anchors() -> None:
    anchor = Descriptor(name="OrderBook", file_path="trader/order_book.py")
    d = _domain(seed_anchors=[anchor])
    assert d.seed_anchors == [anchor]


def test_domain_requires_non_empty_summary() -> None:
    with pytest.raises(ValidationError, match="summary"):
        _domain(summary="")
    with pytest.raises(ValidationError, match="summary"):
        _domain(summary="   ")


def test_domain_requires_non_empty_title() -> None:
    with pytest.raises(ValidationError, match="title"):
        _domain(title="")
    with pytest.raises(ValidationError, match="title"):
        _domain(title="   ")


@pytest.mark.parametrize(
    "bad_slug", ["Payments", "payments_v2", "-payments", "payments!", "", "PAYMENTS"]
)
def test_domain_rejects_non_kebab_slug(bad_slug: str) -> None:
    with pytest.raises(ValidationError, match="slug"):
        _domain(slug=bad_slug)


@pytest.mark.parametrize("good_slug", ["payments", "payments-v2", "a", "a1-b2"])
def test_domain_accepts_kebab_slug(good_slug: str) -> None:
    assert _domain(slug=good_slug).slug == good_slug


# -- matches_path_prefix (boundary-safe path_prefixes matching, M4 fix) -------------------


def test_matches_path_prefix_rejects_sibling_directory_with_shared_text_prefix() -> None:
    assert not matches_path_prefix("payments_v2/x.py", "payments")


def test_matches_path_prefix_accepts_true_subdirectory() -> None:
    assert matches_path_prefix("payments/x.py", "payments")


def test_matches_path_prefix_accepts_exact_file_match() -> None:
    assert matches_path_prefix("payments", "payments")


def test_matches_path_prefix_handles_prefix_with_trailing_slash() -> None:
    assert matches_path_prefix("payments/x.py", "payments/")
    assert not matches_path_prefix("payments_v2/x.py", "payments/")


def test_matches_path_prefix_rejects_plain_substring() -> None:
    assert not matches_path_prefix("paymentsx", "payments")
