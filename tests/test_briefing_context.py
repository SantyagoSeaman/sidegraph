"""Read only the needed briefing data and build the window and working set (D3–D6)."""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest

from sidegraph.host.briefing.context import Parts, build, has_accepted_anchor, read_parts
from sidegraph.hot_index import HotIndex
from sidegraph.retrieval import TOC_CACHE_KEY
from sidegraph.schema import (
    AnchorBinding,
    DecisionKind,
    DecisionStatus,
    Descriptor,
    Domain,
    Entity,
    Fact,
)
from sidegraph.store import Store
from sidegraph.store_layout import DRIFT_CACHE_KEY, VOLATILE_STALE_KEY
from tests.briefing_fixtures import NOW, decision, record, rid
from tests.test_hot_index import _Seeder

G = DecisionKind.GOTCHA
C = DecisionKind.CONSTRAINT


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Store]:
    monkeypatch.delenv("SIDEGRAPH_UNRATIFIED", raising=False)
    monkeypatch.delenv("SIDEGRAPH_PROPOSAL_WINDOW_DAYS", raising=False)
    with closing(Store(tmp_path)) as store:
        yield store


@pytest.fixture
def index(store: Store, tmp_path: Path) -> Iterator[HotIndex]:
    store.set_meta(VOLATILE_STALE_KEY, "0")
    hot = HotIndex.open(tmp_path)
    assert hot is not None
    with closing(hot):
        yield hot


def _read(index: HotIndex, *needs: str) -> Parts:
    with index.snapshot():
        return read_parts(index, frozenset(needs), datetime.now(UTC))


def _fact(store: Store, statement: str, *, status: str = "accepted") -> Fact:
    return store.add_fact(
        Fact(
            statement=statement,
            source="test",
            status=status,
            valid_from=datetime.now(UTC),
            provenance={"source": "manual"},
        )
    )


def _replace_row(path: Path, table: str, row_id: str, row: dict) -> None:
    with closing(sqlite3.connect(path / "index.db")) as conn, conn:
        conn.execute(f"UPDATE {table} SET data=? WHERE id=?", (json.dumps(row), row_id))


def test_the_gate_wants_an_accepted_decision(store: Store, index: HotIndex) -> None:
    s = _Seeder(store)
    s.anchor(s.decision("Proposal", G, DecisionStatus.PROPOSED), "a.py")
    assert index.memory_counts() == (1, 1, 1)
    assert has_accepted_anchor(index) is False


def test_the_gate_stops_at_the_first_accepted_decision(
    store: Store,
    index: HotIndex,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = _Seeder(store)
    proposed = s.anchor(s.decision("Proposal", G, DecisionStatus.PROPOSED), "a.py")
    accepted = s.anchor(s.decision("Accepted", G), "b.py")
    later = s.anchor(s.decision("Later", G), "c.py")
    monkeypatch.setattr(
        index,
        "anchored_files",
        lambda: {
            "a.py": [proposed.entity_id],
            "b.py": [accepted.entity_id],
            "c.py": [later.entity_id],
        },
    )
    calls = Mock(wraps=index.records_for)
    monkeypatch.setattr(index, "records_for", calls)
    assert has_accepted_anchor(index) is True
    assert [call.args for call in calls.call_args_list] == [
        ([proposed.entity_id],),
        ([accepted.entity_id],),
    ]


def test_accepted_leaves_out_what_is_not_live_and_accepted(store: Store, index: HotIndex) -> None:
    s = _Seeder(store)
    old = s.decision("Superseded", G, age_days=10)
    first = s.decision("Replacement", G, age_days=2, supersedes=old.id)
    second = s.decision("Current", C, age_days=1)
    s.decision("Rejected", G, DecisionStatus.REJECTED)
    s.decision("Deprecated", G, DecisionStatus.DEPRECATED)
    s.decision("Proposed constraint", C, DecisionStatus.PROPOSED)
    s.decision("Expired constraint", C, age_days=4, valid_to=datetime.now(UTC) - timedelta(days=1))
    parts = _read(index, "decisions", "window")
    assert [row["id"] for row in parts.accepted] == [second.id, first.id]
    assert parts.predecessors == {old.id: old.title}
    assert build(parts, datetime.now(UTC), None, ()).replaced_titles == {old.id: old.title}


def test_accepted_is_sorted_by_instant_not_by_string(
    tmp_path: Path,
    store: Store,
    index: HotIndex,
) -> None:
    s = _Seeder(store)
    earlier = s.decision("Earlier", G)
    later = s.decision("Later", G)
    for row, valid_from in ((earlier, "2026-11-19T12:00+02:00"), (later, "2026-11-19T11:30+00:00")):
        _replace_row(
            tmp_path, "decisions", row.id, row.model_dump(mode="json") | {"valid_from": valid_from}
        )
    assert [row["id"] for row in _read(index, "decisions").accepted] == [later.id, earlier.id]


def test_no_part_holds_a_proposal(store: Store, index: HotIndex) -> None:
    s = _Seeder(store)
    accepted = s.decision("Accepted", G)
    entity = s.anchor(accepted, "a.py")
    s.anchor(s.decision("Proposed", C, DecisionStatus.PROPOSED), "a.py", entity=entity)
    proposed_fact = _fact(store, "Proposed fact", status="proposed")
    store.add_binding(
        AnchorBinding(record_id=proposed_fact.id, entity_id=entity.entity_id, tier=2, status="live")
    )
    parts = _read(index, "decisions", "anchors", "window", "queue")
    assert [(row.decision["id"], row.proposed) for row in parts.by_file["a.py"]] == [
        (accepted.id, False)
    ]
    assert [row["id"] for row in parts.accepted] == [accepted.id]
    assert parts.fact_dates == ()
    assert parts.queue_count == 2


def test_a_fact_over_512_characters_is_not_in_by_file(store: Store, index: HotIndex) -> None:
    s = _Seeder(store)
    entity = s.anchor(s.decision("Decision", G), "a.py")
    short = _fact(store, "Short fact")
    long = _fact(store, "x" * 513)
    for fact in (short, long):
        store.add_binding(
            AnchorBinding(record_id=fact.id, entity_id=entity.entity_id, tier=2, status="live")
        )
    assert [
        row.decision["id"]
        for row in _read(index, "anchors").by_file["a.py"]
        if row.record_type == "fact"
    ] == [short.id]


def test_the_queue_count_follows_the_surfacing_rule(
    store: Store,
    index: HotIndex,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = _Seeder(store)
    s.decision("Fresh", C, DecisionStatus.PROPOSED)
    s.decision("Aged", C, DecisionStatus.PROPOSED, age_days=40)
    _fact(store, "Fresh fact", status="proposed")
    assert _read(index, "queue").queue_count == 2
    monkeypatch.setenv("SIDEGRAPH_UNRATIFIED", "off")
    assert _read(index, "queue").queue_count == 0


def test_one_bad_row_is_skipped_alone(tmp_path: Path, store: Store, index: HotIndex) -> None:
    s = _Seeder(store)
    good = s.decision("Good", G)
    for n, change in enumerate(
        ({"title": 1}, {"valid_from": "yesterday"}, {"valid_to": "2026-12-01"})
    ):
        bad = s.decision(f"Bad {n}", G)
        _replace_row(tmp_path, "decisions", bad.id, bad.model_dump(mode="json") | change)
    fact = _fact(store, "Naive date")
    _replace_row(
        tmp_path, "facts", fact.id, fact.model_dump(mode="json") | {"valid_from": "2026-11-20"}
    )
    parts = _read(index, "decisions", "window")
    assert [row["id"] for row in parts.accepted] == [good.id]
    assert parts.fact_dates == ()


def test_only_the_needed_parts_are_read(index: HotIndex, monkeypatch: pytest.MonkeyPatch) -> None:
    spies = {
        name: Mock(wraps=getattr(index, name))
        for name in ("rows", "anchored_files", "records_for", "get_meta")
    }
    for name, spy in spies.items():
        monkeypatch.setattr(index, name, spy)
    assert _read(index) == Parts()
    assert all(spy.call_count == 0 for spy in spies.values())
    _read(index, "decisions")
    spies["rows"].assert_called_once_with("decisions")
    spies["get_meta"].assert_called_once_with(DRIFT_CACHE_KEY)
    spies["anchored_files"].assert_not_called()
    spies["records_for"].assert_not_called()


def test_symbols_count_distinct_decisions_per_name(store: Store, index: HotIndex) -> None:
    s = _Seeder(store)
    shared = s.decision("Shared", G)
    another = s.decision("Another", C)
    for path in ("a.py", "b.py"):
        entity = store.upsert_entity(
            Entity(
                canonical_name=f"{path}:main", descriptor=Descriptor(name="main", file_path=path)
            )
        )
        s.anchor(shared, path, entity=entity)
    s.anchor(another, "b.py", entity=entity)
    assert _read(index, "symbols").symbols == {"main": 2}


def test_symbols_leave_out_the_file_level_entity(store: Store, index: HotIndex) -> None:
    s = _Seeder(store)
    entity = store.upsert_entity(
        Entity(canonical_name="src/a.py", descriptor=Descriptor(name="a.py", file_path="src/a.py"))
    )
    s.anchor(s.decision("A file-level decision", G), "src/a.py", entity=entity)
    assert _read(index, "symbols").symbols == {}


def test_the_context_does_not_depend_on_domains(
    tmp_path: Path,
    store: Store,
    index: HotIndex,
) -> None:
    s = _Seeder(store)
    entity = store.upsert_entity(
        Entity(
            canonical_name="Example",
            descriptor=Descriptor(name="Example", file_path="a.py"),
            last_seen_node_id="node-a",
        )
    )
    s.anchor(s.decision("Anchored", G), "a.py", entity=entity)
    domain = store.add_domain(
        Domain(
            slug="area",
            title="Area",
            summary="An example domain.",
            status="accepted",
            provenance={"source": "manual"},
        )
    )
    assert store.replace_domain_members(domain.domain_id, [("node-a", "a.py", None)])
    store.set_meta(TOC_CACHE_KEY, "cached")
    store.set_meta(DRIFT_CACHE_KEY, json.dumps({"by_commit": {"abc": [rid(1)]}}))
    needs = ("decisions", "anchors", "window", "queue", "symbols")
    before = _read(index, *needs)
    assert before.accepted and before.by_file and before.drifted == {rid(1)}
    with closing(sqlite3.connect(tmp_path / "index.db")) as conn, conn:
        matched = conn.execute(
            "SELECT COUNT(*) FROM domain_members AS m JOIN entities AS e "
            "ON m.node_id = json_extract(e.data, '$.last_seen_node_id')"
        ).fetchone()
        assert matched == (1,)
        assert conn.execute("DELETE FROM domain_members").rowcount == 1
        assert conn.execute("DELETE FROM domains").rowcount == 1
        conn.execute("DELETE FROM meta WHERE key=?", (TOC_CACHE_KEY,))
    assert _read(index, *needs) == before


def test_newer_rows_that_are_not_accepted_do_not_move_the_anchor(
    store: Store,
    index: HotIndex,
) -> None:
    s = _Seeder(store)
    accepted = s.decision("Accepted", G, age_days=3)
    s.decision("Proposed", G, DecisionStatus.PROPOSED, age_days=1)
    s.decision("Rejected", G, DecisionStatus.REJECTED, age_days=1)
    _fact(store, "Proposed fact", status="proposed")
    parts = _read(index, "decisions", "window")
    assert build(parts, datetime.now(UTC), None, ()).since == accepted.valid_from - timedelta(
        days=14
    )
    accepted_fact = _fact(store, "Accepted fact")
    parts = _read(index, "decisions", "window")
    assert build(parts, datetime.now(UTC), None, ()).since == (
        accepted_fact.valid_from - timedelta(days=14)
    )


def test_the_fallback_anchor_excludes_a_future_date() -> None:
    parts = Parts(accepted=(decision(1, "Past", days_ago=3), decision(2, "Future", days_ago=-5)))
    assert build(parts, NOW, None, ()).since == NOW - timedelta(days=17)
    parts = Parts(accepted=parts.accepted, fact_dates=(NOW - timedelta(days=1),))
    assert build(parts, NOW, None, ()).since == NOW - timedelta(days=15)


def test_the_fallback_is_stable_on_a_past_corpus() -> None:
    parts = Parts(accepted=(decision(1, "Past"),))
    assert (
        build(parts, NOW, None, ()).since == build(parts, NOW + timedelta(days=365), None, ()).since
    )


def test_no_date_qualifies() -> None:
    parts = Parts(accepted=(decision(1, "Future", days_ago=-1),))
    ctx = build(parts, NOW, None, ())
    assert ctx.since == NOW - timedelta(days=14)
    assert ctx.since_is_fallback


def test_prev_start_is_the_window() -> None:
    prev_start = NOW - timedelta(minutes=20)
    ctx = build(Parts(), NOW, prev_start, ())
    assert ctx.since == prev_start
    assert not ctx.since_is_fallback


def test_the_working_set_is_ordered_and_capped() -> None:
    row = record(decision(1, "A decision"))
    files = tuple(f"{n:02}.py" for n in range(45))
    parts = Parts(by_file={path: (row,) for path in files} | {"44.py": (row, row)})
    assert build(parts, NOW, None, (*reversed(files), "missing.py")).working_set == (
        "44.py",
        *files[:39],
    )


def test_the_fallback_is_representable_for_the_earliest_accepted_date() -> None:
    earliest = datetime.min.replace(tzinfo=UTC)
    row = decision(1, "Earliest") | {"valid_from": earliest.isoformat()}
    assert build(Parts(accepted=(row,)), NOW, None, ()).since == earliest


def test_the_fallback_keeps_earlier_instants_across_offsets_at_the_date_floor() -> None:
    dates = ("0001-01-01T00:00:00+00:00", "0001-01-03T00:00:00-12:00")
    rows = tuple(decision(n, "Recorded") | {"valid_from": date} for n, date in enumerate(dates))
    since = build(Parts(accepted=rows), NOW, None, ()).since
    assert since is not None and all(datetime.fromisoformat(date) >= since for date in dates)


def test_the_fallback_excludes_older_instants_across_offsets_at_the_date_floor() -> None:
    dates = ("0001-01-01T00:00:00+00:00", "0001-01-14T23:00:00-12:00")
    rows = tuple(decision(n, "Recorded") | {"valid_from": date} for n, date in enumerate(dates))
    since = build(Parts(accepted=rows), NOW, None, ()).since
    assert since == datetime(1, 1, 1, 11, tzinfo=UTC)
