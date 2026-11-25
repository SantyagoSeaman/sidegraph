"""The index operations used by the SessionStart briefing (D3–D5)."""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sidegraph.host.briefing.blocks import BLOCKS
from sidegraph.host.briefing.context import build, read_parts
from sidegraph.host.briefing.core import compose
from sidegraph.hot_index import HotIndex
from sidegraph.schema import DecisionKind, Fact
from sidegraph.store import Store
from sidegraph.store_layout import VOLATILE_STALE_KEY
from tests.test_hot_index import _Seeder


@pytest.fixture
def index(tmp_path: Path) -> Iterator[HotIndex]:
    store = Store(tmp_path)
    store.set_meta(VOLATILE_STALE_KEY, "0")
    store.close()
    hot = HotIndex.open(tmp_path, busy_timeout_seconds=0)
    assert hot is not None
    with closing(hot):
        yield hot


def test_rows_returns_every_parsable_row_of_each_table(tmp_path: Path) -> None:
    store = Store(tmp_path)
    seeder = _Seeder(store)
    entity_ids = set()
    decision_ids = []
    for n in range(3):
        decision = seeder.decision(f"Decision {n}", DecisionKind.GOTCHA)
        decision_ids.append(decision.id)
        entity_ids.add(seeder.anchor(decision, f"{n}.py").entity_id)
        if n < 2:
            store.add_fact(
                Fact(
                    statement=f"Fact {n}",
                    source="test",
                    status="accepted",
                    provenance=decision.provenance,
                    valid_from=decision.valid_from,
                )
            )
    store.close()
    with closing(sqlite3.connect(tmp_path / "index.db")) as conn, conn:
        conn.executemany(
            "UPDATE decisions SET data=? WHERE id=?",
            zip(("not json", "[1]"), decision_ids[:2], strict=True),
        )
    index = HotIndex.open(tmp_path)
    assert index is not None
    with closing(index):
        assert [row["id"] for row in index.rows("decisions")] == decision_ids[2:]
        assert len(index.rows("facts")) == 2
        assert {row["entity_id"] for row in index.rows("entities")} == entity_ids


def test_rows_refuses_another_table(index: HotIndex) -> None:
    with pytest.raises(ValueError):
        index.rows("meta")  # type: ignore[arg-type]


def test_snapshot_holds_one_view_until_it_ends(tmp_path: Path, index: HotIndex) -> None:
    with closing(sqlite3.connect(tmp_path / "index.db", timeout=0)) as other:
        sql = "UPDATE meta SET value='1' WHERE key=?"
        with index.snapshot():
            assert index.get_meta(VOLATILE_STALE_KEY) == "0"
            other.execute(sql, (VOLATILE_STALE_KEY,))
            with pytest.raises(sqlite3.OperationalError):
                other.commit()
            other.rollback()
            assert index.get_meta(VOLATILE_STALE_KEY) == "0"
        with other:
            other.execute(sql, (VOLATILE_STALE_KEY,))
        assert index.get_meta(VOLATILE_STALE_KEY) == "1"


def test_snapshot_leaves_no_transaction_open(index: HotIndex) -> None:
    with index.snapshot():
        assert index._conn.in_transaction
    assert not index._conn.in_transaction
    with pytest.raises(RuntimeError), index.snapshot():
        raise RuntimeError("aborted")
    assert not index._conn.in_transaction


def test_update_meta_reserves_before_it_reads(index: HotIndex) -> None:
    seen: list[str] = []
    index._conn.set_trace_callback(seen.append)
    index.update_meta("k", lambda old: "v")
    begin = next(i for i, s in enumerate(seen) if s == "BEGIN IMMEDIATE")
    select = next(i for i, s in enumerate(seen) if s.startswith("SELECT") and "meta" in s)
    assert begin < select


def test_update_meta_holds_the_reservation_while_deciding(tmp_path: Path, index: HotIndex) -> None:
    with closing(sqlite3.connect(tmp_path / "index.db", timeout=0)) as other:

        def decide(old: str | None) -> str:
            assert old is None
            with pytest.raises(sqlite3.OperationalError):
                other.execute("BEGIN IMMEDIATE")
            return "v"

        assert index.update_meta("k", decide) is True
        assert index.get_meta("k") == "v"


def test_update_meta_writes_nothing_when_decide_returns_none(index: HotIndex) -> None:
    assert index.update_meta("k", lambda old: "v") is True

    def decline(old: str | None) -> None:
        assert old == "v"

    assert index.update_meta("k", decline) is False
    assert index.get_meta("k") == "v"


def test_rows_skips_a_payload_with_the_wrong_id(tmp_path: Path, index: HotIndex) -> None:
    with closing(Store(tmp_path)) as store:
        seeder = _Seeder(store)
        good = seeder.decision("Valid payload", DecisionKind.GOTCHA)
        bad = seeder.decision("Mismatched payload", DecisionKind.GOTCHA)
    with closing(sqlite3.connect(tmp_path / "index.db")) as conn, conn:
        data = bad.model_copy(update={"id": good.id}).model_dump_json()
        conn.execute("UPDATE decisions SET data=? WHERE id=?", (data, bad.id))
    assert [row["title"] for row in index.rows("decisions")] == [good.title]


def test_a_constraint_with_a_lone_surrogate_leaves_healthy_constraints_visible(
    tmp_path: Path, index: HotIndex
) -> None:
    with closing(Store(tmp_path)) as store:
        seeder = _Seeder(store)
        healthy = seeder.decision("Keep the healthy constraint 🧭", DecisionKind.CONSTRAINT)
        damaged = seeder.decision("Damaged constraint", DecisionKind.CONSTRAINT)
    with closing(sqlite3.connect(tmp_path / "index.db")) as conn, conn:
        data = json.dumps({**damaged.model_dump(mode="json"), "title": "Damaged \ud800 title"})
        conn.execute("UPDATE decisions SET data=? WHERE id=?", (data, damaged.id))

    now = datetime.now(UTC)
    with index.snapshot():
        parts = read_parts(index, frozenset({"decisions"}), now)
    ctx = build(parts, now, None, ())
    text = compose(ctx, frozenset({"constraints"}), blocks=BLOCKS)

    assert "## Constraints in force (1)" in text
    assert f"- {healthy.title} (id {healthy.id})" in text
