"""The pure first-seen stamping rule for the code-drift cache.

see design/superpowers/specs/2026-10-27-drift-notice-delta-design.md (4.2)
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from sidegraph.store_layout import DRIFT_TOMBSTONE_RETENTION, stamp_cache

T0 = datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC)


def D(n: int) -> datetime:
    return T0 + timedelta(days=n)


def iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def prev_cache(
    by_commit: dict[str, list[str]],
    first_seen: dict[str, datetime | None] | None = None,
    absent: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "first_seen_since": iso(T0),
        "by_commit": by_commit,
        "first_seen": {k: iso(v) for k, v in (first_seen or {}).items() if v},
        "first_seen_absent": absent or {},
    }


def test_retention_is_thirty_days() -> None:
    assert timedelta(days=30) == DRIFT_TOMBSTONE_RETENTION


def test_seed_when_prev_missing_malformed_or_without_since() -> None:
    for prev in (None, "x", {}, {"first_seen_since": "junk"}):
        since, first_seen, absent = stamp_cache(prev, {"a"}, set(), T0)
        assert since == iso(T0)
        assert first_seen == {}
        assert absent == {}


def test_new_id_stamped_now_and_carried_not_bumped() -> None:
    prev = prev_cache({"c": ["a"]}, {"a": T0})
    since, first_seen, absent = stamp_cache(prev, {"a", "x"}, set(), D(1))
    assert since == iso(T0)
    assert first_seen == {"a": iso(T0), "x": iso(D(1))}
    assert absent == {}
    again = {
        "first_seen_since": since,
        "by_commit": {"c": ["a", "x"]},
        "first_seen": first_seen,
        "first_seen_absent": absent,
    }
    _, second, _ = stamp_cache(again, {"a", "x"}, set(), D(2))
    assert second == {"a": iso(T0), "x": iso(D(1))}


def test_unstamped_present_id_stays_unstamped() -> None:
    prev = prev_cache({"c": ["a"]})
    _, first_seen, _ = stamp_cache(prev, {"a"}, set(), D(1))
    assert "a" not in first_seen


def test_transient_absence_restores_old_stamp() -> None:
    prev = prev_cache({"c": ["a"]}, {"a": T0})
    since, first_seen, absent = stamp_cache(prev, set(), set(), D(1))
    assert first_seen == {}
    assert absent == {"a": [iso(T0), iso(D(1))]}
    mid = {
        "first_seen_since": since,
        "by_commit": {},
        "first_seen": first_seen,
        "first_seen_absent": absent,
    }
    _, restored, absent2 = stamp_cache(mid, {"a"}, set(), D(6))
    assert restored["a"] == iso(T0)
    assert absent2 == {}


def test_tombstone_boundary_30_days() -> None:
    prev = prev_cache({}, absent={"a": [iso(T0), iso(D(1))]})
    _, first_seen, absent = stamp_cache(prev, {"a"}, set(), D(31))
    assert first_seen["a"] == iso(T0)
    assert absent == {}
    late = D(31) + timedelta(seconds=1)
    _, first_seen, absent = stamp_cache(prev, {"a"}, set(), late)
    assert first_seen["a"] == iso(late)
    assert absent == {}


def test_null_tombstone_restores_unstamped() -> None:
    prev = prev_cache({"c": ["a"]})
    since, first_seen, absent = stamp_cache(prev, set(), set(), D(1))
    assert absent == {"a": [None, iso(D(1))]}
    mid = {
        "first_seen_since": since,
        "by_commit": {},
        "first_seen": first_seen,
        "first_seen_absent": absent,
    }
    _, restored, absent2 = stamp_cache(mid, {"a"}, set(), D(10))
    assert "a" not in restored
    assert absent2 == {}


def test_retained_id_without_stamp_or_tombstone_is_unstamped() -> None:
    prev = prev_cache({})
    _, first_seen, _ = stamp_cache(prev, {"x"}, {"x"}, D(1))
    assert "x" not in first_seen
    withstone = prev_cache({}, absent={"x": [iso(T0), iso(D(1))]})
    _, first_seen, _ = stamp_cache(withstone, {"x"}, {"x"}, D(2))
    assert first_seen["x"] == iso(T0)


def test_expired_tombstones_dropped_returning_id_leaves_map() -> None:
    absent = {
        "old": [iso(T0), iso(T0)],
        "back": [iso(T0), iso(D(1))],
        "future": [iso(T0), iso(D(100))],
        "fresh": [None, iso(D(1))],
    }
    prev = prev_cache({}, absent=absent)
    _, first_seen, kept = stamp_cache(prev, {"back"}, set(), D(40))
    assert set(kept) == {"future"}
    assert kept["future"] == [iso(T0), iso(D(100))]
    assert "back" in first_seen


def test_old_client_stranded_stamp_becomes_tombstone() -> None:
    prev = prev_cache({"c": []}, {"x": T0})
    since, first_seen, absent = stamp_cache(prev, set(), set(), D(1))
    assert absent == {"x": [iso(T0), iso(D(1))]}
    mid = {
        "first_seen_since": since,
        "by_commit": {},
        "first_seen": first_seen,
        "first_seen_absent": absent,
    }
    _, later, _ = stamp_cache(mid, {"x"}, set(), D(5))
    assert later["x"] == iso(T0)
    # Same call: the id is not in the previous by_commit and has no tombstone yet.
    _, same_call, _ = stamp_cache(prev, {"x"}, set(), D(1))
    assert same_call == {"x": iso(D(1))}


@pytest.mark.parametrize("by_commit", [None, [], "x", {"c": "x"}, {"c": [1]}, {1: []}])
def test_malformed_previous_by_commit_seeds(by_commit):
    prev = {
        "first_seen_since": iso(T0),
        "by_commit": by_commit,
        "first_seen": {"a": iso(T0)},
        "first_seen_absent": {},
    }
    since, first_seen, absent = stamp_cache(prev, {"a", "x"}, set(), D(1))
    assert (since, first_seen, absent) == (iso(D(1)), {}, {})


def test_since_in_the_future_seeds():
    prev = prev_cache({"c": ["a"]}, {"a": T0})
    prev["first_seen_since"] = iso(D(365))
    since, first_seen, absent = stamp_cache(prev, {"a", "x"}, set(), D(1))
    assert (since, first_seen, absent) == (iso(D(1)), {}, {})


def test_timestamp_that_cannot_normalize_to_utc_seeds_or_is_dropped():
    junk = "0001-01-01T00:00:00+01:00"
    prev = prev_cache({"c": ["a"]}, {"a": T0})
    prev["first_seen_since"] = junk
    assert stamp_cache(prev, {"a"}, set(), D(1)) == (iso(D(1)), {}, {})
    prev = prev_cache({"c": ["a"]}, {"a": T0})
    prev["first_seen"]["a"] = junk
    _, first_seen, _ = stamp_cache(prev, {"a"}, set(), D(2))
    assert "a" not in first_seen


@pytest.mark.parametrize(
    "tombstone",
    [
        "junk",
        [1],
        ["nope", "2026-10-02T12:00:00+00:00"],
        ["0001-01-01T00:00:00+01:00", "2026-10-02T12:00:00+00:00"],
        [None, "0001-01-01T00:00:00+01:00"],
    ],
)
def test_unreadable_tombstone_seeds(tombstone):
    prev = prev_cache({}, absent={"x": tombstone})
    prev["first_seen_since"] = iso(D(-30))
    assert stamp_cache(prev, {"x"}, set(), D(1)) == (iso(D(1)), {}, {})


def test_carried_tombstone_expiry_boundary_30_days():
    prev = prev_cache({}, absent={"gone": [iso(T0), iso(D(1))]})
    _, _, absent = stamp_cache(prev, set(), set(), D(31))
    assert absent == {"gone": [iso(T0), iso(D(1))]}
    _, _, absent = stamp_cache(prev, set(), set(), D(31) + timedelta(seconds=1))
    assert absent == {}
