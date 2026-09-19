"""Aggregation for sidegraph-stats (design 2026-09-18-usage-stats-design.md, §4)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sidegraph.schema import (
    AnchorBinding,
    Decision,
    DecisionKind,
    DecisionStatus,
    Descriptor,
    Domain,
    Fact,
    Provenance,
)
from sidegraph.stats.model import build_report
from sidegraph.store import Store

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)


def _seed_sessions(store, asking: int, touch_only: int):
    for i in range(asking):
        sid = f"ask-{i}"
        store.record_retrieval_events(sid, seeds=["a.py"], shows=[(f"rec-{i}", "a.py")])
        store.record_touch(sid, "a.py", "Edit")
        store.record_render_event(
            sid,
            intent=None,
            selected=3,
            emitted=1,
            degraded=1,
            dropped_for_budget=1,
            chars_used=100,
            had_rejected=True,
            had_superseded=False,
        )
    for i in range(touch_only):
        store.record_touch(f"quiet-{i}", f"b{i}.py", "Read")


def _backdate(store, days: int):
    """Journal rows are stamped with the REAL clock, while these tests freeze ``NOW``. Without
    moving them back, every row lands at or after ``NOW`` and ``retained_days`` is 0, so no
    fixture could ever clear the 3-day maturity floor."""
    at = (NOW - timedelta(days=days)).isoformat()
    with store._mutation():
        store._conn.execute("UPDATE retrieval_events SET at = ?", (at,))
        store._conn.execute("UPDATE render_events SET at = ?", (at,))


def _decision(store, *, status=DecisionStatus.ACCEPTED, **kw):
    return store.add_decision(
        Decision(
            title=kw.pop("title", "t"),
            kind=DecisionKind.ADR,
            status=status,
            context="c",
            choice="ch",
            valid_from=kw.pop("valid_from", NOW - timedelta(days=1)),
            provenance=Provenance(source="manual"),
            **kw,
        )
    )


def _assert_partition(tmp_path, a):
    """The three session buckets must cover EVERY session the journals name.

    `total` is recomputed here straight from both journal tables, not from the report: an
    identity over the report's own fields (fed + empty + touch_only == total) holds by
    construction and cannot see a session that fell out of `total` itself."""
    import sqlite3
    from contextlib import closing

    with closing(sqlite3.connect(tmp_path / "s" / "index.db")) as conn:
        journal = {
            row[0]
            for row in conn.execute(
                "SELECT session_id FROM retrieval_events UNION SELECT session_id FROM render_events"
            )
        }
    fed = a.sessions_with_retrieval - a.sessions_asked_but_empty
    assert a.sessions_total == len(journal)
    assert fed + a.sessions_asked_but_empty + a.sessions_touch_only == len(journal)


def test_activation_counts_asking_and_silent_sessions(tmp_path):
    store = Store(tmp_path / "s")
    _seed_sessions(store, asking=6, touch_only=4)
    _backdate(store, 5)
    store.close()
    r = build_report(tmp_path / "s", None, window_days=30, now=NOW)
    assert r.activation.sessions_total == 10
    assert r.activation.sessions_with_retrieval == 6
    assert r.activation.sessions_touch_only == 4
    assert r.activation.showings == 6
    assert r.activation.degraded == 6
    assert r.activation.dropped_for_budget == 6
    assert r.activation.renders_with_abandoned == 6
    assert r.activation.mature is True


def test_showings_counts_records_not_anchor_paths(tmp_path):
    """A show row is keyed by PATH with the record id in `detail` (store.py:2905-2910), so
    counting distinct keys would collapse eight records anchored to one file into one.
    """
    store = Store(tmp_path / "s")
    store.record_retrieval_events(
        "s1", seeds=["a.py"], shows=[("rec-1", "a.py"), ("rec-2", "a.py"), ("rec-3", "a.py")]
    )
    store.close()
    r = build_report(tmp_path / "s", None, window_days=30, now=NOW)
    assert r.activation.showings == 3


def test_below_the_floor_the_report_is_marked_immature(tmp_path):
    store = Store(tmp_path / "s")
    _seed_sessions(store, asking=1, touch_only=1)
    store.close()
    r = build_report(tmp_path / "s", None, window_days=30, now=NOW)
    assert r.activation.mature is False, "2 sessions is below the 5-session floor"


def test_an_empty_journal_is_immature_not_zero_percent(tmp_path):
    Store(tmp_path / "s").close()
    r = build_report(tmp_path / "s", None, window_days=30, now=NOW)
    assert r.activation.sessions_total == 0
    assert r.activation.mature is False


def test_events_outside_the_window_are_excluded(tmp_path):
    store = Store(tmp_path / "s")
    _seed_sessions(store, asking=6, touch_only=0)
    _backdate(store, 90)  # both journals: a render row is a session too
    store.close()
    r = build_report(tmp_path / "s", None, window_days=30, now=NOW)
    assert r.activation.sessions_total == 0
    assert r.activation.dropped_for_budget == 0
    assert r.retained_days == 0, "retention is measured inside the window, not over the journal"


def test_the_funnel_reads_rejections_from_the_records_not_a_table(tmp_path):
    """Spec D6: drop() persists status 'rejected' append-only, so no writer is needed."""
    store = Store(tmp_path / "s")
    d = store.add_decision(
        Decision(
            title="t",
            kind=DecisionKind.ADR,
            status=DecisionStatus.PROPOSED,
            context="c",
            choice="ch",
            valid_from=NOW - timedelta(days=1),
            provenance=Provenance(source="manual"),
        )
    )
    store.drop(d.id)
    store.close()
    r = build_report(tmp_path / "s", None, window_days=30, now=NOW)
    assert r.memory.rejected_in_window == 1
    assert r.memory.accepted_in_window == 0


def test_a_rejection_is_windowed_on_when_the_verdict_landed(tmp_path):
    store = Store(tmp_path / "s")
    _decision(
        store,
        status=DecisionStatus.REJECTED,
        valid_from=NOW - timedelta(days=90),
        valid_to=NOW - timedelta(days=60),
    )
    store.close()
    r = build_report(tmp_path / "s", None, window_days=30, now=NOW)
    assert r.memory.rejected_in_window == 0


def test_build_report_does_not_rebuild_the_index(tmp_path):
    """Constructing a Store would: __init__ -> _refresh_freshness -> full reload on a stale
    digest (store.py:620). This module must read, not open (spec D8).
    """
    store = Store(tmp_path / "s")
    _seed_sessions(store, asking=6, touch_only=0)
    store.close()
    index = tmp_path / "s" / "index.db"
    before = index.stat().st_mtime_ns
    build_report(tmp_path / "s", None, window_days=30, now=NOW)
    assert index.stat().st_mtime_ns == before


@pytest.mark.parametrize(
    ("sessions", "days", "mature"),
    [
        (4, 10, False),  # the session half of the floor, alone
        (5, 10, True),
        (6, 2, False),  # the day half of the floor, alone
        (6, 3, True),
    ],
)
def test_maturity_needs_both_the_session_floor_and_the_day_floor(tmp_path, sessions, days, mature):
    store = Store(tmp_path / "s")
    _seed_sessions(store, asking=sessions, touch_only=0)
    _backdate(store, days)
    store.close()
    r = build_report(tmp_path / "s", None, window_days=30, now=NOW)
    assert r.activation.mature is mature
    assert r.retained_days == days


def test_reach_counts_touched_files_that_carry_a_binding(tmp_path):
    store = Store(tmp_path / "s")
    d = _decision(store)
    e = store.get_or_create_entity(Descriptor(name="run", file_path="a.py"))
    store.add_binding(AnchorBinding(record_id=d.id, entity_id=e.entity_id, tier=2))
    store.record_touch("s1", "a.py", "Edit")
    store.record_touch("s1", "a.py", "Read")
    store.record_touch("s2", "b.py", "Read")
    # An entity for b.py exists but nothing binds to it: touching b.py is not "carrying memory".
    store.get_or_create_entity(Descriptor(name="helper", file_path="b.py"))
    store.record_retrieval(["x"], seeds=["a.py", "b.py"])
    store.record_retrieval(["x"], seeds=["a.py"])
    store.close()
    r = build_report(tmp_path / "s", None, window_days=30, now=NOW)
    assert r.reach.files_touched == 2
    assert r.reach.files_touched_with_memory == 1
    assert r.reach.busiest_seeds == [("a.py", 2), ("b.py", 1)]


def test_a_domain_is_silent_until_something_binds_to_its_entity(tmp_path):
    store = Store(tmp_path / "s")
    quiet, busy = (
        store.add_domain(
            Domain(slug=slug, title=title, summary="why", provenance=Provenance(source="manual"))
        )
        for slug, title in (("quiet", "Quiet"), ("busy", "Busy"))
    )
    store.ratify_domains(accept=[quiet.domain_id, busy.domain_id])
    store.add_domain(  # still proposed: nobody has accepted it, so it cannot be "silent"
        Domain(slug="pending", title="Pending", summary="why", provenance=Provenance(source="x"))
    )
    d = _decision(store)
    entity = store.get_or_create_abstract_entity("domain:busy")
    store.add_binding(AnchorBinding(record_id=d.id, entity_id=entity.entity_id, tier=1))
    store.close()
    r = build_report(tmp_path / "s", None, window_days=30, now=NOW)
    assert r.reach.silent_domains == ["Quiet"]
    assert r.memory.domains == 2, "accepted only: the proposed domain is not counted"


def test_silent_domains_are_capped_at_four_alphabetically(tmp_path):
    store = Store(tmp_path / "s")
    ids = [
        store.add_domain(
            Domain(slug=f"d{n}", title=f"D{n}", summary="why", provenance=Provenance(source="x"))
        ).domain_id
        for n in range(6)
    ]
    store.ratify_domains(accept=ids)
    store.close()
    r = build_report(tmp_path / "s", None, window_days=30, now=NOW)
    assert r.reach.silent_domains == ["D0", "D1", "D2", "D3"]


def test_no_recorded_showing_counts_accepted_records_the_counter_has_not_seen(tmp_path):
    store = Store(tmp_path / "s")
    seen = _decision(store)
    _decision(store)  # accepted, never shown
    _decision(store, status=DecisionStatus.PROPOSED)  # not surfaceable: not ratified
    store.record_retrieval([seen.id], seeds=[])
    store.close()
    r = build_report(tmp_path / "s", None, window_days=30, now=NOW)
    assert (r.memory.decisions, r.memory.surfaceable, r.memory.no_recorded_showing) == (2, 2, 1)


def test_an_accept_is_dated_by_ratification_not_by_the_proposal(tmp_path):
    """`ratify()` stamps ratified_at and leaves valid_from at proposal time."""
    store = Store(tmp_path / "s")
    old = _decision(store, status=DecisionStatus.PROPOSED, valid_from=NOW - timedelta(days=60))
    store.ratify(old.id, actor="auto:policy")
    _decision(store, valid_from=NOW - timedelta(days=60))  # accepted at write, long ago
    _decision(store, valid_from=NOW - timedelta(days=60), ratified_by="auto:old")
    store.close()
    r = build_report(tmp_path / "s", None, window_days=30, now=NOW)
    assert r.memory.accepted_in_window == 1
    assert r.memory.auto_accepted_in_window == 1


def test_a_human_accept_is_not_counted_as_automatic(tmp_path):
    store = Store(tmp_path / "s")
    d = _decision(store, status=DecisionStatus.PROPOSED)
    store.ratify(d.id, actor="alice")
    store.close()
    r = build_report(tmp_path / "s", None, window_days=30, now=NOW)
    assert (r.memory.accepted_in_window, r.memory.auto_accepted_in_window) == (1, 0)


def test_anchor_statuses_are_counted_from_the_index_rows(tmp_path):
    store = Store(tmp_path / "s")
    d = _decision(store)
    for i, status in enumerate(["live", "live", "degraded", "orphaned"]):
        e = store.get_or_create_entity(Descriptor(name=f"f{i}", file_path=f"f{i}.py"))
        store.add_binding(
            AnchorBinding(record_id=d.id, entity_id=e.entity_id, tier=2, status=status)
        )
    store.close()
    r = build_report(tmp_path / "s", None, window_days=30, now=NOW)
    assert (r.anchors.live, r.anchors.degraded, r.anchors.orphaned) == (2, 1, 1)


def test_the_reader_never_opens_the_index_for_writing(tmp_path, monkeypatch):
    """Spec §5 #9: the connect string carries mode=ro and never immutable=1."""
    import sqlite3

    store = Store(tmp_path / "s")
    store.close()
    seen: list[str] = []
    real = sqlite3.connect

    def spy(target, *a, **kw):
        seen.append(str(target))
        return real(target, *a, **kw)

    monkeypatch.setattr(sqlite3, "connect", spy)
    build_report(tmp_path / "s", None, window_days=30, now=NOW)
    assert seen and all("mode=ro" in t and "immutable" not in t for t in seen)


def test_render_counters_are_windowed_and_split_by_the_rejected_flag(tmp_path):
    store = Store(tmp_path / "s")
    kw = dict(intent=None, selected=3, emitted=1, degraded=2, dropped_for_budget=4, chars_used=9)
    store.record_render_event("a", had_rejected=True, had_superseded=False, **kw)
    store.record_render_event("b", had_rejected=False, had_superseded=False, **kw)
    store.record_render_event("old", had_rejected=True, had_superseded=False, **kw)
    with store._mutation():
        store._conn.execute(
            "UPDATE render_events SET at = ? WHERE session_id = 'old'",
            ((NOW - timedelta(days=90)).isoformat(),),
        )
    store.close()
    r = build_report(tmp_path / "s", None, window_days=30, now=NOW)
    a = r.activation
    assert (a.degraded, a.dropped_for_budget, a.renders_with_abandoned) == (4, 8, 1)


def test_a_session_that_asked_and_got_nothing_is_asking_and_not_silent(tmp_path):
    """Ruling: asked-but-empty is its own failure mode, kept out of `touch_only`."""
    store = Store(tmp_path / "s")
    store.record_retrieval_events("empty", seeds=["a.py"], shows=[])
    store.record_retrieval_events("fed", seeds=["a.py"], shows=[("rec-1", "a.py")])
    store.record_touch("quiet", "b.py", "Read")
    store.close()
    a = build_report(tmp_path / "s", None, window_days=30, now=NOW).activation
    assert a.sessions_total == 3
    assert a.sessions_with_retrieval == 2
    assert a.sessions_asked_but_empty == 1
    assert a.sessions_touch_only == 1
    _assert_partition(tmp_path, a)


def test_a_session_with_shows_and_no_seed_row_still_asked(tmp_path):
    """drill_down passes only `domain:<slug>` seeds and server._record drops those from the
    journal (server.py, `journal_seeds`), so a drill-down-only session carries show rows and NO
    seed row. It was delivered records: asking, not empty, not silent. Constructed on purpose:
    the real journal has never produced this shape."""
    store = Store(tmp_path / "s")
    store.record_retrieval_events("drill", seeds=[], shows=[("rec-1", "a.py")])
    store.close()
    a = build_report(tmp_path / "s", None, window_days=30, now=NOW).activation
    assert a.sessions_total == 1
    assert a.sessions_with_retrieval == 1
    assert a.sessions_asked_but_empty == 0
    assert a.sessions_touch_only == 0
    _assert_partition(tmp_path, a)


def test_memory_counts_accepted_records_and_history_is_its_own_line(tmp_path):
    store = Store(tmp_path / "s")
    old = _decision(store)
    _decision(store, supersedes=old.id)  # closes `old` -> superseded; the successor is accepted
    rejected = _decision(store, status=DecisionStatus.PROPOSED)
    store.drop(rejected.id)
    _decision(store, status=DecisionStatus.PROPOSED)  # pending: neither accepted nor history
    store.close()
    m = build_report(tmp_path / "s", None, window_days=30, now=NOW).memory
    assert m.decisions == 1
    assert m.historical == 2


def test_history_spans_all_three_record_types(tmp_path):
    store = Store(tmp_path / "s")
    live_fact = store.add_fact(
        Fact(
            statement="x",
            source="y",
            status=DecisionStatus.ACCEPTED,
            valid_from=NOW,
            provenance=Provenance(source="manual"),
        )
    )
    store.add_fact(
        Fact(
            statement="x2",
            source="y",
            supersedes=live_fact.id,
            valid_from=NOW,
            status=DecisionStatus.ACCEPTED,
            provenance=Provenance(source="manual"),
        )
    )
    keep, drop = (
        store.add_domain(
            Domain(slug=slug, title=slug, summary="why", provenance=Provenance(source="x"))
        )
        for slug in ("keep", "drop")
    )
    store.ratify_domains(accept=[keep.domain_id, drop.domain_id])
    store.ratify_domains(drop=[drop.domain_id])
    store.close()
    m = build_report(tmp_path / "s", None, window_days=30, now=NOW).memory
    assert (m.facts, m.domains) == (1, 1)
    assert m.historical == 2, "one superseded fact + one dropped domain"


def test_a_render_row_alone_makes_a_session_that_asked_and_got_nothing(tmp_path):
    """A name-only entity call gives `_record` no journal seeds and nothing shown, so
    `record_retrieval_events` writes no row while `record_render_event` still writes one
    (server.py). get_task_context ran, so the session asked; it must be in the denominator
    and in asked-but-empty, and its budget drops are counted with it."""
    store = Store(tmp_path / "s")
    kw = dict(intent=None, selected=3, emitted=0, degraded=0, chars_used=0)
    kw |= dict(had_rejected=False, had_superseded=False)
    store.record_render_event("render-only", dropped_for_budget=3, **kw)
    store.record_render_event("render+touch", dropped_for_budget=0, **kw)
    store.record_touch("render+touch", "a.py", "Edit")
    store.record_touch("quiet", "b.py", "Read")
    store.record_retrieval_events("fed", seeds=["a.py"], shows=[("rec-1", "a.py")])
    store.close()
    a = build_report(tmp_path / "s", None, window_days=30, now=NOW).activation
    _assert_partition(tmp_path, a)
    assert a.sessions_total == 4
    assert a.sessions_with_retrieval == 3  # render-only, render+touch, fed
    assert a.sessions_asked_but_empty == 2  # render-only, render+touch
    assert a.sessions_touch_only == 1  # quiet
    assert a.dropped_for_budget == 3


def test_render_only_sessions_count_toward_retained_days(tmp_path):
    """A session known only from its render row is still evidence of when the journal began,
    or a store whose sessions were all render-only can never clear the day floor."""
    store = Store(tmp_path / "s")
    for i in range(5):
        store.record_render_event(
            f"r{i}",
            intent=None,
            selected=1,
            emitted=0,
            degraded=0,
            dropped_for_budget=1,
            chars_used=0,
            had_rejected=False,
            had_superseded=False,
        )
    _backdate(store, 5)
    store.close()
    a = build_report(tmp_path / "s", None, window_days=30, now=NOW)
    assert a.retained_days == 5
    assert a.activation.mature is True


def test_deprecated_is_history_like_the_stores_own_terminal_set(tmp_path):
    """Mirrors store._TERMINAL_DECISION_STATUSES (superseded, rejected, deprecated)."""
    store = Store(tmp_path / "s")
    _decision(store)
    _decision(store, status=DecisionStatus.DEPRECATED, valid_to=NOW)
    store.close()
    m = build_report(tmp_path / "s", None, window_days=30, now=NOW).memory
    assert (m.decisions, m.historical) == (1, 1)


CUT = NOW - timedelta(days=30)


def _at_cutoff(store, table: str) -> None:
    with store._mutation():
        store._conn.execute(f"UPDATE {table} SET at = ?", (CUT.isoformat(),))


def _retrieval_row_at_cutoff(store):
    store.record_touch("s", "a.py", "Read")
    _at_cutoff(store, "retrieval_events")


def _render_row_at_cutoff(store):
    store.record_render_event(
        "s",
        intent=None,
        selected=1,
        emitted=0,
        degraded=1,
        dropped_for_budget=1,
        chars_used=0,
        had_rejected=False,
        had_superseded=False,
    )
    _at_cutoff(store, "render_events")


def _accepted_at_cutoff(store):
    _decision(store, valid_from=CUT)


def _rejected_at_cutoff(store):
    _decision(
        store, status=DecisionStatus.REJECTED, valid_from=CUT - timedelta(days=5), valid_to=CUT
    )


@pytest.mark.parametrize(
    ("seed", "read", "expected"),
    [
        # each case names the `>= cutoff` it pins; `>` at that site drops the row at the boundary
        pytest.param(
            _retrieval_row_at_cutoff,
            lambda r: r.activation.sessions_total,
            1,
            id="retrieval_events-session-query",
        ),
        pytest.param(
            _retrieval_row_at_cutoff, lambda r: r.retained_days, 30, id="first_at-retrieval-branch"
        ),
        pytest.param(
            _render_row_at_cutoff,
            lambda r: r.activation.degraded,
            1,
            id="render_events-counter-query",
        ),
        pytest.param(
            _render_row_at_cutoff, lambda r: r.retained_days, 30, id="first_at-render-branch"
        ),
        pytest.param(
            _accepted_at_cutoff, lambda r: r.memory.accepted_in_window, 1, id="funnel-accepted"
        ),
        pytest.param(
            _rejected_at_cutoff, lambda r: r.memory.rejected_in_window, 1, id="funnel-rejected"
        ),
    ],
)
def test_the_window_includes_a_row_exactly_at_the_cutoff(tmp_path, seed, read, expected):
    store = Store(tmp_path / "s")
    seed(store)
    store.close()
    r = build_report(tmp_path / "s", None, window_days=30, now=NOW)
    assert read(r) == expected


def _drop_render_journal(store_dir):
    """Recreate a store as it was before the render journal existed: the index has no
    `render_events` table. `Store()` would put it back on open, so this edits the file directly."""
    import sqlite3
    from contextlib import closing

    with closing(sqlite3.connect(store_dir / "index.db")) as conn:
        conn.execute("DROP TABLE render_events")
        conn.commit()


def test_an_index_from_before_the_render_journal_reports_instead_of_raising(tmp_path):
    store = Store(tmp_path / "s")
    for i in range(6):
        store.record_retrieval_events(f"ask-{i}", seeds=["a.py"], shows=[(f"rec-{i}", "a.py")])
    _backdate(store, days=10)
    store.close()
    _drop_render_journal(tmp_path / "s")

    r = build_report(tmp_path / "s", None, window_days=30, now=NOW)

    a = r.activation
    assert a.render_journal is False
    # What the retrieval journal alone can say is still said.
    assert (a.sessions_total, a.sessions_with_retrieval, a.showings) == (6, 6, 6)
    assert r.retained_days == 10


def _render(store, sid, *, degraded=0, dropped=0):
    store.record_render_event(
        sid,
        intent=None,
        selected=3,
        emitted=3,
        degraded=degraded,
        dropped_for_budget=dropped,
        chars_used=100,
        had_rejected=False,
        had_superseded=False,
    )


def test_a_table_with_no_rows_in_the_window_is_not_recorded_either(tmp_path):
    """The same fact as a missing table, to a reader: nothing was logged. The table exists
    and is empty whenever a store was upgraded but the server writing it has not run yet, and
    sessions that asked are already in the retrieval journal, so zeroes would read as a
    measurement ("the budget never clipped anything")."""
    store = Store(tmp_path / "s")
    for i in range(6):
        store.record_retrieval_events(f"ask-{i}", seeds=["a.py"], shows=[(f"rec-{i}", "a.py")])
    _backdate(store, days=10)
    store.close()

    a = build_report(tmp_path / "s", None, window_days=30, now=NOW).activation

    assert a.render_journal is False
    assert (a.sessions_with_retrieval, a.showings) == (6, 6)


def test_render_rows_only_outside_the_window_are_not_a_recording_either(tmp_path):
    store = Store(tmp_path / "s")
    _render(store, "old", degraded=2)
    _backdate(store, days=90)
    store.close()
    r = build_report(tmp_path / "s", None, window_days=30, now=NOW)
    assert r.activation.render_journal is False


def test_a_genuine_zero_is_still_a_zero_when_renders_exist(tmp_path):
    """The other side of the rule: a window WITH renders where the budget clipped nothing is a
    finding, and must stay a number the renderer can print."""
    store = Store(tmp_path / "s")
    for i in range(6):
        store.record_retrieval_events(f"ask-{i}", seeds=["a.py"], shows=[(f"rec-{i}", "a.py")])
        _render(store, f"ask-{i}", degraded=0, dropped=0)
    _backdate(store, days=10)
    store.close()

    a = build_report(tmp_path / "s", None, window_days=30, now=NOW).activation

    assert a.render_journal is True
    assert (a.degraded, a.dropped_for_budget) == (0, 0)


def test_a_missing_render_table_is_not_confused_with_a_missing_retrieval_table(tmp_path):
    """Only the one table this feature added degrades. An index that lacks the older
    `retrieval_events` is a different, unrecognised store and must still raise, so the CLI can
    call it an operational error rather than print a report of invented zeroes."""
    import sqlite3
    from contextlib import closing

    Store(tmp_path / "s").close()
    with closing(sqlite3.connect(tmp_path / "s" / "index.db")) as conn:
        conn.execute("DROP TABLE retrieval_events")
        conn.commit()
    with pytest.raises(sqlite3.OperationalError):
        build_report(tmp_path / "s", None, window_days=30, now=NOW)


def test_the_repo_name_survives_a_relative_store_path(tmp_path, monkeypatch):
    """`sidegraph-stats` run at a repo root resolves the default store to the relative
    `.sidegraph`, whose `.parent` is `.` — a name of "" and a header reading `Sidegraph ·`."""
    (tmp_path / "myrepo").mkdir()
    Store(tmp_path / "myrepo" / ".sidegraph").close()
    monkeypatch.chdir(tmp_path / "myrepo")

    r = build_report(Path(".sidegraph"), None, window_days=30, now=NOW)

    assert r.repo == "myrepo"


def test_render_rows_and_asking_sessions_cannot_come_apart_the_wrong_way(tmp_path):
    """The renderer's screens are built from reports the aggregator can actually produce, so
    the boundary is pinned here rather than assumed: a session with a render row counts as
    asking, hence `render_journal` True never sits beside `sessions_with_retrieval == 0`; and
    a window where only files were touched has no render rows at all."""
    only_touch = Store(tmp_path / "touch")
    for i in range(6):
        only_touch.record_touch(f"quiet-{i}", f"f{i}.py", "Read")
    _backdate(only_touch, days=10)
    only_touch.close()
    quiet = build_report(tmp_path / "touch", None, window_days=30, now=NOW).activation
    assert (quiet.sessions_with_retrieval, quiet.render_journal) == (0, False)
    assert (quiet.degraded, quiet.dropped_for_budget, quiet.renders_with_abandoned) == (0, 0, 0)

    render_only = Store(tmp_path / "render")
    _render(render_only, "r1", degraded=1)
    _backdate(render_only, days=10)
    render_only.close()
    rendered = build_report(tmp_path / "render", None, window_days=30, now=NOW).activation
    assert rendered.render_journal is True
    assert rendered.sessions_with_retrieval == 1, "a render row is proof memory was asked"


# --- the index can be behind the canonical files (fix wave, finding 1) -------------------
#
# `Store.__init__` refreshes a stale index, and this command must not construct a Store, so a
# `git pull` that changes canonical records leaves `index.db` describing the old ones. The
# report states that instead of measuring it: the record-derived figures come back None.


def _canonical_file(tmp_path: Path, subdir: str, record_id: str) -> Path:
    return tmp_path / "s" / subdir / f"{record_id}.json"


def _pull_a_change(tmp_path: Path, decision_id: str) -> None:
    """Rewrite a decision's canonical JSON the way `git pull` would, leaving index.db alone."""
    import json

    path = _canonical_file(tmp_path, "decisions", decision_id)
    data = json.loads(path.read_text())
    data["status"] = "rejected"
    path.write_text(json.dumps(data, indent=2) + "\n")


def test_a_canonical_change_the_index_has_not_loaded_is_stated_not_measured(tmp_path):
    """The reproduction: the report kept printing the old value after a pull."""
    store = Store(tmp_path / "s")
    d = _decision(store)
    store.close()
    before = build_report(tmp_path / "s", None, window_days=30, now=NOW)
    assert before.memory is not None and before.memory.decisions == 1
    assert before.index_stale is False

    _pull_a_change(tmp_path, d.id)
    after = build_report(tmp_path / "s", None, window_days=30, now=NOW)
    assert after.index_stale is True
    assert after.memory is None and after.anchors is None
    assert after.reach.files_touched_with_memory is None and after.reach.silent_domains is None

    Store(tmp_path / "s").close()  # what the next session start does
    healed = build_report(tmp_path / "s", None, window_days=30, now=NOW)
    assert healed.index_stale is False
    assert healed.memory is not None and healed.memory.decisions == 0


def test_a_stale_index_still_reports_the_journals_it_can_stand_behind(tmp_path):
    """Only the record half is behind: the journals are written live, not rebuilt."""
    store = Store(tmp_path / "s")
    d = _decision(store)
    _seed_sessions(store, asking=6, touch_only=0)
    _backdate(store, 5)
    store.close()
    _pull_a_change(tmp_path, d.id)
    r = build_report(tmp_path / "s", None, window_days=30, now=NOW)
    assert r.index_stale is True
    assert r.activation.sessions_with_retrieval == 6 and r.activation.mature is True
    assert r.reach.files_touched == 1, "the touch journal is live too"


def _populated(tmp_path: Path) -> Store:
    store = Store(tmp_path / "s")
    old = _decision(store)
    new = _decision(store, supersedes=old.id)
    store.add_fact(
        Fact(
            statement="s",
            source="src",
            supports=[new.id],
            status=DecisionStatus.ACCEPTED,
            valid_from=NOW,
            provenance=Provenance(source="manual"),
        )
    )
    e = store.get_or_create_entity(Descriptor(name="f", file_path="a.py"))
    store.add_binding(AnchorBinding(record_id=new.id, entity_id=e.entity_id, tier=2))
    store.get_or_create_abstract_entity("community:7")  # index-only, never a canonical file
    return store


def test_a_store_nobody_changed_is_never_reported_stale(tmp_path):
    """No false alarm on an ordinary run: every state the store's own writes and rebuilds
    leave behind must compare clean against the disk."""

    def stale() -> bool:
        return build_report(tmp_path / "s", None, window_days=30, now=NOW).index_stale

    store = _populated(tmp_path)
    store.close()
    assert stale() is False, "after the store's own writes"
    Store(tmp_path / "s").close()
    assert stale() is False, "after a plain reopen"
    (tmp_path / "s" / "index.db").unlink()
    Store(tmp_path / "s").close()
    assert stale() is False, "after the index was rebuilt from the canonical files"
    store = Store(tmp_path / "s")
    assert store.compact().decisions_compacted >= 1
    store.close()
    assert stale() is False, "after compaction moved hot files into an archive segment"


def test_a_canonical_file_the_index_never_loaded_is_stale(tmp_path):
    """A pull that ADDS a record: a file on disk with no canonical_stat row."""
    store = _populated(tmp_path)
    d = _decision(store)
    store.close()
    src = _canonical_file(tmp_path, "decisions", d.id)
    src.with_name("01ARZ3NDEKTSV4RRFFQ69G5FAV.json").write_text(src.read_text())
    assert build_report(tmp_path / "s", None, window_days=30, now=NOW).index_stale is True


def test_a_canonical_file_removed_behind_the_index_is_stale(tmp_path):
    """A pull that DELETES a record: a canonical_stat row with no file."""
    store = _populated(tmp_path)
    d = _decision(store)
    store.close()
    _canonical_file(tmp_path, "decisions", d.id).unlink()
    assert build_report(tmp_path / "s", None, window_days=30, now=NOW).index_stale is True


def test_the_staleness_check_writes_nothing(tmp_path):
    import hashlib

    store = Store(tmp_path / "s")
    d = _decision(store)
    store.close()
    _pull_a_change(tmp_path, d.id)
    index = tmp_path / "s" / "index.db"

    def snapshot():
        files = sorted(p for p in (tmp_path / "s").rglob("*") if p.is_file())
        return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}

    before = snapshot()
    assert build_report(tmp_path / "s", None, window_days=30, now=NOW).index_stale is True
    assert snapshot() == before
    assert index.exists()


def test_an_index_without_canonical_stat_is_stale_not_a_crash(tmp_path):
    """An index older than the freshness table: every canonical file is one the index never
    loaded, so the honest answer is stale, not an OperationalError."""
    import sqlite3
    from contextlib import closing

    store = Store(tmp_path / "s")
    _decision(store)
    store.close()
    with closing(sqlite3.connect(tmp_path / "s" / "index.db")) as conn:
        conn.execute("DROP TABLE canonical_stat")
        conn.commit()
    assert build_report(tmp_path / "s", None, window_days=30, now=NOW).index_stale is True


def test_the_canonical_subdirectories_mirror_the_stores(tmp_path):
    """The walk here is a copy of the store's (this module imports nothing from it). A
    subdirectory added there and not here would be a pull this check cannot see."""
    from sidegraph import store as store_module
    from sidegraph.stats import model

    assert model._CANONICAL_SUBDIRS == store_module._CANONICAL_SUBDIRS
    assert model._ARCHIVE_SUBDIR == store_module._ARCHIVE_SUBDIR


# --- a session that was delivered records is not "asked and got nothing" (finding 2) ---------
#
# The show journal is keyed by anchor PATH, so a record bound to an entity with no file (an
# abstract or global anchor) leaves no show row. The render journal is written regardless, and
# is what says the session was delivered something.


def _pathless_delivery(tmp_path, monkeypatch, *, sid="s1"):
    """Drive the real writer: a global-scope decision has no anchor at all, so it is delivered
    to whoever asks and can never leave a show row. The session asks about a file."""
    from sidegraph import server
    from sidegraph.engine.reader import GraphifyReader
    from sidegraph.schema import Scope

    store = Store(tmp_path / "s")
    d = _decision(store, scope=Scope.GLOBAL, title="a-global-rule")
    monkeypatch.setattr(server, "_session_key", lambda _s: sid)
    reader = GraphifyReader(Path(__file__).parent / "fixtures" / "mini_graph.json")
    text = server._get_task_context_impl(store, reader, ["trader/exec.py"], None, 4000, 6000)
    return store, d, text


def test_a_session_delivered_pathless_records_is_not_asked_but_empty(tmp_path, monkeypatch):
    store, d, text = _pathless_delivery(tmp_path, monkeypatch)
    assert "a-global-rule" in text, "the render did deliver the record"
    assert [e for e in store.retrieval_events() if e["kind"] == "show_anchor"] == [], (
        "and left no show row in the journal: nothing anchors it to a path"
    )
    store.close()
    a = build_report(tmp_path / "s", None, window_days=30, now=NOW).activation
    assert a.sessions_with_retrieval == 1
    assert a.sessions_asked_but_empty == 0
    assert a.showings == 1, "not '0 records shown' beside 'every one got records'"
    _assert_partition(tmp_path, a)


def _pathless_fact_delivery(tmp_path, monkeypatch, *, sid="s1"):
    """Drive the real writer: a fact bound to an entity that carries no file, reached by a
    name-only lookup. The lookup journals no seed (there is no file to key one on) and shows
    no record (a show row is keyed by anchor path), so the render row is the only trace of it.
    """
    import json

    from sidegraph import server
    from sidegraph.engine.reader import GraphifyReader

    graph = tmp_path / "graph.json"
    graph.write_text(
        json.dumps(
            {
                "directed": False,
                "multigraph": False,
                "built_at_commit": "abc",
                "nodes": [
                    {
                        "id": "n1",
                        "label": "Concept",
                        "norm_label": "concept",
                        "file_type": "code",
                        "source_file": "",
                        "source_location": "",
                        "community": 1,
                        "_origin": "ast",
                    }
                ],
                "links": [],
            }
        )
    )
    store = Store(tmp_path / "s")
    entity = store.get_or_create_entity(Descriptor(name="Concept"))
    fact = store.add_fact(
        Fact(
            statement="the-pathless-fact",
            source="measured",
            valid_from=NOW - timedelta(days=1),
            provenance=Provenance(source="manual"),
        )
    )
    store.add_binding(AnchorBinding(record_id=fact.id, entity_id=entity.entity_id, tier=2))
    monkeypatch.setattr(server, "_session_key", lambda _s: sid)
    text = server._get_task_context_impl(
        store, GraphifyReader(graph), None, [{"name": "Concept"}], 4000, 6000
    )
    return store, text


def test_a_session_delivered_one_pathless_fact_is_asking_and_fed(tmp_path, monkeypatch):
    """Red before wave 3: `emitted` counted decisions only, so this render read as nothing
    delivered and the session as asked-but-empty with zero showings."""
    store, text = _pathless_fact_delivery(tmp_path, monkeypatch)
    assert "the-pathless-fact" in text, "the render did deliver the fact"
    assert store.retrieval_events() == [], "and left no seed and no show row behind it"
    assert [e["emitted"] for e in store.render_events()] == [1]
    store.close()
    a = build_report(tmp_path / "s", None, window_days=30, now=NOW).activation
    assert a.sessions_with_retrieval == 1
    assert a.sessions_asked_but_empty == 0
    assert a.showings == 1
    _assert_partition(tmp_path, a)


def test_a_render_of_an_anchored_and_a_global_decision_is_two_showings(tmp_path, monkeypatch):
    """Red before wave 3: the anchored decision leaves one show row and the global one none, so
    the path-keyed count won and the report said one showing for a render that placed two."""
    from sidegraph import server
    from sidegraph.engine.reader import GraphifyReader
    from sidegraph.schema import Scope

    store = Store(tmp_path / "s")
    entity = store.get_or_create_entity(Descriptor(name="Trader", file_path="trader/exec.py"))
    anchored = _decision(store, title="an-anchored-rule")
    store.add_binding(AnchorBinding(record_id=anchored.id, entity_id=entity.entity_id, tier=2))
    _decision(store, scope=Scope.GLOBAL, title="a-global-rule")
    monkeypatch.setattr(server, "_session_key", lambda _s: "s1")
    reader = GraphifyReader(Path(__file__).parent / "fixtures" / "mini_graph.json")
    text = server._get_task_context_impl(store, reader, ["trader/exec.py"], None, 4000, 6000)
    assert "an-anchored-rule" in text and "a-global-rule" in text
    assert len([e for e in store.retrieval_events() if e["kind"] == "show_anchor"]) == 1
    assert [e["emitted"] for e in store.render_events()] == [2]
    store.close()
    a = build_report(tmp_path / "s", None, window_days=30, now=NOW).activation
    assert a.showings == 2
    assert a.sessions_asked_but_empty == 0
    _assert_partition(tmp_path, a)


def test_a_render_that_placed_nothing_is_still_asked_and_empty(tmp_path):
    """The other side of the same rule: `emitted == 0` is what keeps the empty state."""
    store = Store(tmp_path / "s")
    store.record_render_event(
        "s1",
        intent=None,
        selected=2,
        emitted=0,
        degraded=0,
        dropped_for_budget=2,
        chars_used=0,
        had_rejected=False,
        had_superseded=False,
    )
    store.close()
    a = build_report(tmp_path / "s", None, window_days=30, now=NOW).activation
    assert (a.sessions_with_retrieval, a.sessions_asked_but_empty) == (1, 1)
    assert a.showings == 0


def test_a_session_with_render_rows_is_counted_from_the_render_journal(tmp_path):
    """The render journal states what each lookup placed, so it is authoritative for a session
    that has render rows. Two lookups that each placed two records are four showings, and the
    session's two show rows do not cap that: show rows are keyed by path and are kept for the
    join against touched files, not for this count."""
    store = Store(tmp_path / "s")
    store.record_retrieval_events("s1", seeds=["a.py"], shows=[("r1", "a.py"), ("r2", "a.py")])
    for _ in range(2):
        store.record_render_event(
            "s1",
            intent=None,
            selected=2,
            emitted=2,
            degraded=0,
            dropped_for_budget=0,
            chars_used=10,
            had_rejected=False,
            had_superseded=False,
        )
    store.close()
    a = build_report(tmp_path / "s", None, window_days=30, now=NOW).activation
    assert a.showings == 4
    assert a.sessions_asked_but_empty == 0


def test_a_session_from_before_the_render_journal_is_counted_from_its_show_rows(tmp_path):
    """No render row for the session: the journal did not exist, or the session never rendered.
    Its show rows are the only record of what it was shown, counted per distinct record exactly
    as before."""
    store = Store(tmp_path / "s")
    store.record_retrieval_events(
        "old", seeds=["a.py"], shows=[("r1", "a.py"), ("r2", "a.py"), ("r3", "b.py")]
    )
    store.close()
    a = build_report(tmp_path / "s", None, window_days=30, now=NOW).activation
    assert (a.sessions_with_retrieval, a.sessions_asked_but_empty) == (1, 0)
    assert a.showings == 3
    assert a.render_journal is False


def test_each_session_is_counted_from_its_own_best_journal(tmp_path):
    """One store, both shapes: the pre-journal session contributes its show rows and the
    session with a render row contributes that row, so the two sources are never mixed within
    a session and never summed for one."""
    store = Store(tmp_path / "s")
    store.record_retrieval_events("old", seeds=["a.py"], shows=[("r1", "a.py"), ("r2", "a.py")])
    store.record_retrieval_events("new", seeds=["a.py"], shows=[("r3", "a.py")])
    store.record_render_event(
        "new",
        intent=None,
        selected=1,
        emitted=3,
        degraded=0,
        dropped_for_budget=0,
        chars_used=10,
        had_rejected=False,
        had_superseded=False,
    )
    store.close()
    a = build_report(tmp_path / "s", None, window_days=30, now=NOW).activation
    assert a.showings == 2 + 3


# --- drill_down writes a render event, and it is not a budget (fix wave 3) -------------------
#
# A drill-down delivers records and applies no budget. Its row carries the records returned as
# `emitted` under the reserved intent, with zeros for every budget field. The zeros are the
# absence of a budget, so they must not turn "the budget was never recorded" into "the budget
# clipped nothing".


def _drill_down_store(tmp_path, monkeypatch, *, rejected="", sid="s1"):
    """A domain with one decision, and the real `drill_down` writer run once for `sid`."""
    from sidegraph import server

    store = Store(tmp_path / "s")
    dom = server._add_domain_impl(store, None, slug="payments", title="Payments", summary="s.")
    server._ratify_impl(store, accept=[dom["domain_id"]])
    d = _decision(store, title="use postgres", rejected=rejected)
    entity = store.find_abstract_entity("domain:payments")
    store.add_binding(
        AnchorBinding(record_id=d.id, entity_id=entity.entity_id, tier=1, status="live")
    )
    monkeypatch.setattr(server, "_session_key", lambda _s: sid)
    out = server._drill_down_impl(store, None, "payments")
    assert any("use postgres" in line for line in out["decisions"]), "the drill-down delivered it"
    return store


def test_a_session_with_a_drill_down_and_an_ordinary_lookup_counts_both(tmp_path, monkeypatch):
    """Red before: the drill-down wrote no render row, so once the session had one from an
    ordinary lookup the render journal was authoritative and the drill-down's record fell out."""
    from sidegraph import server
    from sidegraph.engine.reader import GraphifyReader
    from sidegraph.schema import Scope

    store = _drill_down_store(tmp_path, monkeypatch)
    _decision(store, scope=Scope.GLOBAL, title="a-global-rule")
    reader = GraphifyReader(Path(__file__).parent / "fixtures" / "mini_graph.json")
    text = server._get_task_context_impl(store, reader, ["trader/exec.py"], None, 4000, 6000)
    assert "a-global-rule" in text
    assert sorted(e["emitted"] for e in store.render_events()) == [1, 1]
    store.close()
    a = build_report(tmp_path / "s", None, window_days=30, now=NOW).activation
    assert a.showings == 2, "one from the lookup and one from the drill-down"
    assert a.sessions_asked_but_empty == 0
    _assert_partition(tmp_path, a)


def test_a_drill_down_only_session_is_asking_fed_and_reports_its_records(tmp_path, monkeypatch):
    store = _drill_down_store(tmp_path, monkeypatch)
    store.close()
    a = build_report(tmp_path / "s", None, window_days=30, now=NOW).activation
    assert a.sessions_with_retrieval == 1
    assert a.sessions_asked_but_empty == 0
    assert a.showings == 1
    _assert_partition(tmp_path, a)


def test_a_drill_down_only_window_records_no_budget_and_does_not_look_exercised(
    tmp_path, monkeypatch
):
    """Red before: a drill-down writes a row, so `render_journal` was true and the report
    printed `0 shortened to fit the budget, 0 dropped by it` for a window in which no budget
    ever ran."""
    store = _drill_down_store(tmp_path, monkeypatch)
    store.close()
    a = build_report(tmp_path / "s", None, window_days=30, now=NOW).activation
    assert a.budget_journal is False, "no budgeted lookup was recorded"
    assert a.render_journal is True, "but a lookup that delivered records was"


def test_an_ordinary_lookup_beside_a_drill_down_makes_the_budget_recorded(tmp_path, monkeypatch):
    store = _drill_down_store(tmp_path, monkeypatch)
    _render_row(store, "s2")
    store.close()
    a = build_report(tmp_path / "s", None, window_days=30, now=NOW).activation
    assert a.budget_journal is True and a.render_journal is True
    assert (a.degraded, a.dropped_for_budget) == (0, 0), "a measured zero, from a lookup that ran"


def test_a_drill_downs_budget_fields_are_never_summed(tmp_path):
    """The writer puts zeros there; the reader does not rely on it. A row under the reserved
    intent carrying non-zero budget fields still adds nothing to them."""
    store = Store(tmp_path / "s")
    store.record_render_event(
        "s1",
        intent="drill_down",
        selected=3,
        emitted=3,
        degraded=7,
        dropped_for_budget=9,
        chars_used=99,
        had_rejected=False,
        had_superseded=False,
    )
    _render_row(store, "s2")
    store.close()
    a = build_report(tmp_path / "s", None, window_days=30, now=NOW).activation
    assert (a.degraded, a.dropped_for_budget) == (0, 0)
    assert a.showings == 3 + 1


def test_a_drill_downs_tried_and_abandoned_flags_still_count(tmp_path, monkeypatch):
    """`had_rejected` and `had_superseded` apply to a drill-down, unlike the budget fields."""
    store = _drill_down_store(tmp_path, monkeypatch, rejected="mysql: no")
    store.close()
    a = build_report(tmp_path / "s", None, window_days=30, now=NOW).activation
    assert a.renders_with_abandoned == 1
    assert a.budget_journal is False


def test_one_lookup_whose_session_changes_mid_call_is_one_session_and_one_showing(
    tmp_path, monkeypatch
):
    """A `SessionStart` landing between `_record`'s two journal writes used to split one call
    into a show-only session and a render-only session: two sessions, two showings."""
    from sidegraph import server

    store = Store(tmp_path / "s")
    dom = server._add_domain_impl(store, None, slug="payments", title="Payments", summary="s.")
    server._ratify_impl(store, accept=[dom["domain_id"]])
    d = _decision(store, title="use postgres")
    for entity in (
        store.find_abstract_entity("domain:payments"),
        store.get_or_create_entity(Descriptor(name="Trader", file_path="trader/exec.py")),
    ):
        store.add_binding(AnchorBinding(record_id=d.id, entity_id=entity.entity_id, tier=2))
    keys = iter(["session-before", "session-after", "session-after"])
    monkeypatch.setattr(server, "_session_key", lambda _s: next(keys))
    server._drill_down_impl(store, None, "payments")
    assert [e["session_id"] for e in store.retrieval_events() if e["kind"] == "show_anchor"] == [
        "session-before"
    ]
    store.close()
    a = build_report(tmp_path / "s", None, window_days=30, now=NOW).activation
    assert (a.sessions_total, a.showings) == (1, 1)
    _assert_partition(tmp_path, a)


def test_the_reserved_intent_is_not_a_self_reported_label(tmp_path):
    store = Store(tmp_path / "s")
    for sid, intent in [("a", "drill_down"), ("b", "check-plan")]:
        _render_row(store, sid, intent=intent)
    store.close()
    a = build_report(tmp_path / "s", None, window_days=30, now=NOW).activation
    assert a.self_reported_intents == {"check-plan": 1}


def test_the_reserved_intent_matches_the_one_the_server_writes():
    """`stats/` imports nothing from the server, so the label is mirrored and pinned."""
    from sidegraph import server
    from sidegraph.stats import model

    assert model._DRILL_DOWN_INTENT == server.DRILL_DOWN_INTENT


# --- a narrowed window over an older journal (fix wave, finding 11) --------------------------


def test_a_narrowed_window_over_an_older_journal_knows_the_journal_is_not_empty(tmp_path):
    store = Store(tmp_path / "s")
    store.record_touch("old", "a.py", "Read")
    _backdate(store, 20)
    store.close()
    narrow = build_report(tmp_path / "s", None, window_days=7, now=NOW)
    assert narrow.activation.sessions_total == 0 and narrow.retained_days == 0
    assert narrow.activation.outside_window is True
    wide = build_report(tmp_path / "s", None, window_days=30, now=NOW)
    assert wide.activation.sessions_total == 1
    assert wide.activation.outside_window is False


def test_outside_window_reads_the_render_journal_too(tmp_path):
    store = Store(tmp_path / "s")
    _seed_sessions(store, asking=1, touch_only=0)
    _backdate(store, 20)
    with store._mutation():
        store._conn.execute("DELETE FROM retrieval_events")
    store.close()
    a = build_report(tmp_path / "s", None, window_days=7, now=NOW).activation
    assert a.sessions_total == 0 and a.outside_window is True


def test_an_empty_journal_has_nothing_outside_the_window(tmp_path):
    Store(tmp_path / "s").close()
    assert build_report(tmp_path / "s", None, now=NOW).activation.outside_window is False


# --- the two columns that were written and never read (fix wave, finding 6) -------------------


def _render_row(store, sid, *, intent=None, rejected=False, superseded=False):
    store.record_render_event(
        sid,
        intent=intent,
        selected=1,
        emitted=1,
        degraded=0,
        dropped_for_budget=0,
        chars_used=10,
        had_rejected=rejected,
        had_superseded=superseded,
    )


def test_a_superseded_record_surfacing_is_something_already_tried_and_abandoned(tmp_path):
    """The line claims to count lookups that included something tried and abandoned; a
    superseded record IS that, and `had_superseded` was stored and never read."""
    store = Store(tmp_path / "s")
    _render_row(store, "rejected-only", rejected=True)
    _render_row(store, "superseded-only", superseded=True)
    _render_row(store, "both", rejected=True, superseded=True)
    _render_row(store, "neither")
    store.close()
    a = build_report(tmp_path / "s", None, window_days=30, now=NOW).activation
    assert a.renders_with_abandoned == 3, "one row that is both counts once, not twice"


def test_intent_is_reported_by_label_and_windowed(tmp_path):
    store = Store(tmp_path / "s")
    for sid, intent in [
        ("a", "explain-why"),
        ("b", "explain-why"),
        ("c", "explain-why"),
        ("d", "check-plan"),
        ("d2", "zzz-tie"),
        ("d3", "aaa-tie"),
        ("e", None),
        ("f", ""),
        ("g", "  "),
    ]:
        _render_row(store, sid, intent=intent)
    store.close()
    a = build_report(tmp_path / "s", None, window_days=30, now=NOW).activation
    assert a.self_reported_intents == {
        "explain-why": 3,
        "aaa-tie": 1,
        "check-plan": 1,
        "zzz-tie": 1,
    }
    assert list(a.self_reported_intents) == ["explain-why", "aaa-tie", "check-plan", "zzz-tie"], (
        "most used first (the reverse of alphabetical here), ties by name"
    )


def test_intent_rows_outside_the_window_are_not_counted(tmp_path):
    store = Store(tmp_path / "s")
    _render_row(store, "a", intent="check-plan")
    _backdate(store, 90)
    store.close()
    a = build_report(tmp_path / "s", None, window_days=30, now=NOW).activation
    assert a.self_reported_intents == {}


def test_a_store_with_no_labelled_lookup_has_no_intents(tmp_path):
    Store(tmp_path / "s").close()
    assert build_report(tmp_path / "s", None, now=NOW).activation.self_reported_intents == {}


# --- one snapshot, not one autocommit per query (fix wave 2, finding 1) ------------------
#
# The command is meant to be run mid-session, which is when another process is writing. Python's
# sqlite3 opens no transaction for a SELECT, so without an explicit one every query is its own
# read and a writer can land between two of them.


def test_a_writer_landing_between_two_reads_cannot_split_the_report(tmp_path, monkeypatch):
    """A session's touch row and its render row are committed together. A report that read the
    touch journal before the commit and the render journal after it names the session, counts a
    delivered record, and reports zero files touched: a state that never existed."""
    import sqlite3
    from contextlib import closing

    Store(tmp_path / "s").close()
    index = tmp_path / "s" / "index.db"
    real_connect = sqlite3.connect
    outcome: dict[str, int] = {"attempts": 0, "committed": 0, "refused": 0}

    def write_one_session() -> None:
        """One transaction, on a second connection, exactly as a live server would."""
        at = NOW.isoformat()
        outcome["attempts"] += 1
        with closing(real_connect(index, timeout=0)) as w:
            try:
                with w:
                    w.execute(
                        "INSERT INTO retrieval_events (session_id, at, kind, key, detail) "
                        "VALUES ('w1', ?, 'touch', 'a.py', 'Edit')",
                        (at,),
                    )
                    w.execute(
                        "INSERT INTO render_events (session_id, at, intent, selected, emitted, "
                        "degraded, dropped_for_budget, chars_used, had_rejected, had_superseded) "
                        "VALUES ('w1', ?, NULL, 1, 1, 0, 0, 10, 0, 0)",
                        (at,),
                    )
            except sqlite3.OperationalError:
                outcome["refused"] += 1  # the reader's snapshot holds the database
            else:
                outcome["committed"] += 1

    class Interleaving(sqlite3.Connection):
        """Lets a writer in before every statement that follows the first journal read."""

        read_started = False

        def execute(self, sql, *args):  # type: ignore[no-untyped-def, override]
            if self.read_started and outcome["committed"] == 0:
                write_one_session()
            if "FROM retrieval_events" in sql:
                self.read_started = True
            return super().execute(sql, *args)

    monkeypatch.setattr(
        sqlite3, "connect", lambda *a, **kw: real_connect(*a, factory=Interleaving, **kw)
    )
    a = build_report(tmp_path / "s", None, window_days=30, now=NOW)

    assert outcome["attempts"] >= 1, "the interleaving never ran; the test proves nothing"
    figures = (
        a.activation.sessions_total,
        a.activation.showings,
        a.reach.files_touched,
    )
    assert figures in {(0, 0, 0), (1, 1, 1)}, (
        f"the report matches neither the state before the writer nor the one after: {figures}"
    )
