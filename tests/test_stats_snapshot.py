"""End-to-end over a fixture store with a frozen clock (spec §5, test 1).

The only test that runs ``build_report`` → ``render_text`` over a store written through the
real ``Store`` API. Journal timestamps come from the wall clock, so the fixture rewrites them
after writing: the report is then a pure function of the fixture and ``NOW``.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sidegraph.schema import (
    AnchorBinding,
    Decision,
    DecisionKind,
    DecisionStatus,
    Descriptor,
    Provenance,
)
from sidegraph.stats.model import build_report
from sidegraph.stats.render import render_text
from sidegraph.store import Store

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)


def _fixture(tmp_path: Path, *, journal_age: timedelta) -> Path:
    """Six sessions that each asked about ``store.py``, were shown its one decision and touched
    the file. ``journal_age`` back-dates every journal row: a freshly written journal has zero
    retained days, which is below the maturity floor."""
    store_dir = tmp_path / "s"
    store = Store(store_dir)
    e = store.get_or_create_entity(Descriptor(name="store", file_path="store.py"))
    d = store.add_decision(
        Decision(
            title="use sqlite",
            kind=DecisionKind.ADR,
            status=DecisionStatus.ACCEPTED,
            context="c",
            choice="ch",
            rejected="postgres",
            valid_from=NOW - timedelta(days=5),
            provenance=Provenance(source="manual"),
        )
    )
    store.add_binding(AnchorBinding(record_id=d.id, entity_id=e.entity_id, tier=2))
    for i in range(6):
        sid = f"s{i}"
        # The server's `_record` writes both: the cumulative counters and the journal rows.
        store.record_retrieval([d.id], ["store.py"])
        store.record_retrieval_events(sid, seeds=["store.py"], shows=[(d.id, "store.py")])
        store.record_touch(sid, "store.py", "Edit")
        store.record_render_event(
            sid,
            intent=None,
            selected=1,
            emitted=1,
            degraded=0,
            dropped_for_budget=0,
            chars_used=180,
            had_rejected=True,
            had_superseded=False,
        )
    store.close()
    stamp = (NOW - journal_age).isoformat()
    with closing(sqlite3.connect(store_dir / "index.db")) as conn, conn:
        conn.execute("UPDATE retrieval_events SET at = ?", (stamp,))
        conn.execute("UPDATE render_events SET at = ?", (stamp,))
    return store_dir


def _text(store_dir: Path) -> str:
    return render_text(build_report(store_dir, None, window_days=30, now=NOW))


def test_six_sessions_in_one_day_are_reported_as_too_few_days_not_as_a_ratio(tmp_path):
    """The real fresh-store case: enough sessions, but the journal is hours old. The floor is
    two-sided (5 sessions AND 3 days), so no ratio is printed; the counts that need no
    denominator still are."""
    text = _text(_fixture(tmp_path, journal_age=timedelta(hours=1)))
    assert "ACTIVATION" in text
    assert "6 sessions" in text and "0 days" in text
    assert "%" not in text, "below the floor no percentage appears anywhere"
    assert "6 of 6 sessions" not in text
    assert "asked about most, all time: store.py ×6" in text
    assert "no recorded showing, all time: 0 of 1 decisions and facts" in text
    assert "sidegraph-init" in text, "no graph in this fixture"


def test_the_same_store_a_few_days_later_renders_its_ratios(tmp_path):
    text = _text(_fixture(tmp_path, journal_age=timedelta(days=5)))
    assert "ACTIVATION" in text
    assert "6 of 6 sessions" in text
    assert "%" in text, "above both floors, so ratios render"
    assert "1 files touched" not in text, "grammar: use a singular form"
    assert "no recorded showing, all time: 0 of 1 decisions and facts (0%)" in text
    assert "1 file touched, 1 with memory anchored to it (100%)" in text
    assert "sidegraph-init" in text, "no graph in this fixture"


def test_a_file_bound_only_to_a_fact_is_reported_as_carrying_memory_not_a_decision(tmp_path):
    """Zero decisions and one fact: the REACH count is right and its noun must match the
    MEMORY line beneath it, which says `0 decisions · 1 fact`."""
    from sidegraph.schema import Fact

    store_dir = tmp_path / "s"
    store = Store(store_dir)
    e = store.get_or_create_entity(Descriptor(name="store", file_path="store.py"))
    f = store.add_fact(
        Fact(
            statement="s",
            source="src",
            status=DecisionStatus.ACCEPTED,
            valid_from=NOW - timedelta(days=5),
            provenance=Provenance(source="manual"),
        )
    )
    store.add_binding(AnchorBinding(record_id=f.id, entity_id=e.entity_id, tier=2))
    store.record_touch("s1", "store.py", "Edit")
    store.close()
    text = _text(store_dir)
    assert "1 file touched, 1 with memory anchored to it" in text
    assert "accepted: 0 decisions · 1 fact" in text
    assert "with a decision" not in text


def test_a_record_shown_while_recording_was_off_is_not_reported_as_never_shown(
    tmp_path, monkeypatch
):
    """The ranker showed the record; recording was off, so no counter moved. Once recording is
    back on, the report holds an absence of a record, and a line claiming the record was never
    shown would state as fact something the report cannot know."""
    from sidegraph.server import _get_task_context_impl
    from tests.test_server_telemetry import _store_with_seed_mistake

    store, reader, decision = _store_with_seed_mistake(tmp_path)
    decision.status = DecisionStatus.ACCEPTED  # the stats count accepted records only
    store._write_decision(decision)
    store._conn.commit()
    monkeypatch.setenv("SIDEGRAPH_TELEMETRY", "off")
    shown = _get_task_context_impl(store, reader, ["trader/exec.py"], None, 4000, 6000)
    monkeypatch.delenv("SIDEGRAPH_TELEMETRY")
    store.close()
    assert "deadlock" in shown, "the record really was shown; the counter cannot know it"

    text = render_text(build_report(tmp_path / "t.db", None, window_days=30, now=NOW))

    (line,) = [ln for ln in text.splitlines() if "1 of 1 decisions and facts" in ln]
    assert "never" not in line and "not shown" not in line, line
    assert "no recorded showing" in line, line


def test_one_record_delivered_in_six_sessions_is_not_worded_as_six_records(tmp_path):
    """The delivery count is per session, so one record shown in six sessions counts six times,
    while two blocks lower `1 of 1 decisions and facts` counts distinct records. Dividing one by
    the other means nothing, and only the line itself is in front of the reader."""
    text = _text(_fixture(tmp_path, journal_age=timedelta(days=5)))

    (line,) = [ln for ln in text.splitlines() if "per session that got any" in ln]
    assert "6 showings" in line and "repeats counted" in line, line
    assert "record" not in line, "a noun the denominator below also uses invites the division"
