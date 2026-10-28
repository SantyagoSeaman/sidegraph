"""``recent_drift`` and ``live_drifted_ids``: the recency clause's count, span and suppression.

see design/superpowers/specs/2026-10-27-drift-notice-delta-design.md (4.5)
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from sidegraph.retrieval import live_drifted_ids, recent_drift
from sidegraph.schema import Decision, DecisionKind, DecisionStatus, Provenance
from sidegraph.store import Store

NOW = datetime(2026, 10, 27, 12, 0, 0, tzinfo=UTC)


def iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def payload(since: datetime | None, stamps: dict[str, datetime]) -> dict[str, object]:
    out: dict[str, object] = {"first_seen": {k: iso(v) for k, v in stamps.items()}}
    if since is not None:
        out["first_seen_since"] = iso(since)
    return out


@pytest.fixture
def store(tmp_path) -> Store:
    return Store(tmp_path / ".sidegraph")


def _mk(store: Store, **overrides) -> Decision:
    base = dict(
        title="Watch the cache",
        kind=DecisionKind.GOTCHA,
        context="ctx",
        choice="always flush",
        status=DecisionStatus.ACCEPTED,
        valid_from=datetime.now(UTC),
        provenance=Provenance(source="manual"),
    )
    base.update(overrides)
    return store.add_decision(Decision(**base))


def test_recent_counts_live_stamped_in_window():
    old_seed = NOW - timedelta(days=60)
    stamps = {
        "a": NOW - timedelta(days=1),
        "b": NOW - timedelta(days=10),
        "gone": NOW - timedelta(days=1),
        "future": NOW + timedelta(hours=1),
    }
    result = recent_drift(frozenset({"a", "b", "future"}), payload(old_seed, stamps), NOW)
    assert result == (2, "in the last 7 days")


def test_live_drifted_ids_excludes_superseded_and_cleared(store):
    d = _mk(store)
    d2 = _mk(store, title="Successor", supersedes=d.id)
    kept = _mk(store, title="Other")
    # `kept` is stamped but absent from by_commit (an older client's check): not live here.
    assert live_drifted_ids(store, {"c1": [d.id, d2.id]}) == frozenset({d2.id})
    assert kept.id not in live_drifted_ids(store, {"c1": [d.id, d2.id]})
    assert live_drifted_ids(store, "not-a-dict") == frozenset()


def test_span_since_date_for_young_seed_and_window_for_old_seed():
    young = NOW - timedelta(days=3)
    assert recent_drift(frozenset({"a"}), payload(young, {"a": NOW}), NOW) == (
        1,
        f"since {young.date().isoformat()}",
    )
    old = NOW - timedelta(days=10)
    assert recent_drift(frozenset({"a"}), payload(old, {"a": NOW}), NOW) == (
        1,
        "in the last 7 days",
    )


def test_zero_count_suppression():
    live = frozenset({"a"})
    assert recent_drift(live, payload(NOW - timedelta(hours=12), {}), NOW) is None
    assert recent_drift(live, payload(NOW - timedelta(days=3), {}), NOW) == (
        0,
        f"since {(NOW - timedelta(days=3)).date().isoformat()}",
    )
    assert recent_drift(live, payload(NOW - timedelta(days=10), {}), NOW) == (
        0,
        "in the last 7 days",
    )


def test_missing_or_unparsable_since_returns_none():
    live = frozenset({"a"})
    assert recent_drift(live, payload(None, {"a": NOW}), NOW) is None
    assert recent_drift(live, {"first_seen_since": "junk", "first_seen": {}}, NOW) is None


def test_unparsable_stamp_skipped_and_non_dict_first_seen_reads_empty():
    since = NOW - timedelta(days=30)
    bad = {"first_seen_since": iso(since), "first_seen": {"a": "junk", "b": iso(NOW)}}
    assert recent_drift(frozenset({"a", "b"}), bad, NOW) == (1, "in the last 7 days")
    odd = {"first_seen_since": iso(since), "first_seen": ["a"]}
    assert recent_drift(frozenset({"a"}), odd, NOW) == (0, "in the last 7 days")


def test_sub_day_suppression_applies_only_to_a_zero_count():
    since = NOW - timedelta(hours=12)
    result = recent_drift(frozenset({"a"}), payload(since, {"a": NOW - timedelta(hours=1)}), NOW)
    assert result == (1, f"since {since.date().isoformat()}")


def test_window_boundaries_are_inclusive():
    cutoff = NOW - timedelta(days=7)
    assert recent_drift(frozenset({"a"}), payload(cutoff, {"a": cutoff}), NOW) == (
        1,
        "in the last 7 days",
    )


def test_unnormalizable_since_reads_as_missing():
    bad = {"first_seen_since": "0001-01-01T00:00:00+01:00", "first_seen": {}}
    assert recent_drift(frozenset({"a"}), bad, NOW) is None
