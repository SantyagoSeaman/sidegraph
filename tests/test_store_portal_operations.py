"""Portal operation journal and reservation (Portal design D8)."""

import sqlite3
from pathlib import Path
from typing import Literal

import pytest

from sidegraph.store import Store
from tests.store_format2_support import decision_body, put_file


def test_started_operation_blocks_another_on_same_draft(tmp_path: Path) -> None:
    store = Store(tmp_path / "store")
    store.put_operation("first", "hash1", "draft", None, ["new"])
    store.set_operation_state("first", "started")

    with pytest.raises(sqlite3.IntegrityError):
        store.put_operation("second", "hash2", "draft", None, ["other"])

    first = store.get_operation("first")
    assert first is not None and first["state"] == "started"
    store.release_operation("first")
    assert store.get_operation("first") == first
    assert store.get_operation("second") is None


def test_planned_operation_is_replaced_by_a_new_one(tmp_path: Path) -> None:
    store = Store(tmp_path / "store")
    store.put_operation("first", "hash1", "draft", None, ["new"])
    store.put_operation("second", "hash2", "draft", None, ["other"])

    assert store.get_operation("first") is None
    second = store.get_operation("second")
    assert second is not None
    assert second["state"] == "planned"
    assert second["planned"] == ["other"]
    store.release_operation("second")
    assert store.get_operation("second") is None


def test_save_as_new_row_has_no_draft_id(tmp_path: Path) -> None:
    store = Store(tmp_path / "store")
    store.put_operation("edit", "hash1", "draft", None, ["edit-result"])
    store.set_operation_state("edit", "started")
    store.put_operation("copy", "hash2", None, "draft", ["copy-result"])

    row = store.get_operation("copy")
    assert row is not None
    assert row["draft_id"] is None
    assert row["record_id"] == "draft"


@pytest.mark.parametrize("draft_id", ["draft", None])
@pytest.mark.parametrize("state", ["planned", "started", "done"])
def test_repeated_operation_key_reuses_its_planned_ids(
    tmp_path: Path, draft_id: str | None, state: Literal["planned", "started", "done"]
) -> None:
    with Store(tmp_path / "store") as store:
        store.put_operation("key", "digest", draft_id, None, ["original"])
        store.set_operation_state("key", state)
        original = store.get_operation("key")
        store.put_operation("key", "digest", draft_id, None, ["retry"])

        assert original is not None
        assert original["planned"] == ["original"]
        assert store.get_operation("key") == original

        with pytest.raises(sqlite3.IntegrityError):
            store.put_operation("key", "other", draft_id, None, ["other"])
        assert store.get_operation("key") == original


def test_conflicting_operation_key_leaves_other_plan_in_outer_mutation(tmp_path: Path) -> None:
    with Store(tmp_path / "store") as store:
        store.put_operation("taken", "digest1", None, "record", ["copy"])
        store.put_operation("original", "digest2", "draft", None, ["version"])
        original = store.get_operation("original")

        with store._mutation(), pytest.raises(sqlite3.IntegrityError):
            store.put_operation("taken", "other", "draft", None, ["replacement"])

        assert original is not None
        assert store.get_operation("original") == original


def test_setting_state_on_a_replaced_plan_refuses(tmp_path: Path) -> None:
    with Store(tmp_path / "store") as store:
        store.put_operation("first", "digest1", "draft", None, ["one"])
        store.put_operation("second", "digest2", "draft", None, ["two"])

        with pytest.raises(ValueError, match="portal operation not found"):
            store.set_operation_state("first", "started")


def test_invalid_replacement_leaves_the_planned_row_inside_outer_mutation(tmp_path: Path) -> None:
    with Store(tmp_path / "store") as store:
        store.put_operation("first", "digest1", "draft", None, ["original"])
        original = store.get_operation("first")

        previous_limit = store._conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 512)
        try:
            with store._mutation(), pytest.raises(sqlite3.DataError):
                store.put_operation("second", "digest2", "draft", None, ["x" * 4096])
        finally:
            store._conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, previous_limit)

        assert original is not None
        assert store.get_operation("first") == original
        assert store.get_operation("second") is None


def test_operation_state_joins_outer_mutation(tmp_path: Path) -> None:
    store = Store(tmp_path / "store")
    store.put_operation("key", "hash", "draft", None, ["new"])

    with pytest.raises(RuntimeError, match="abort"), store._mutation():
        store.set_operation_state("key", "started")
        row = store.get_operation("key")
        assert row is not None and row["state"] == "started"
        raise RuntimeError("abort")

    row = store.get_operation("key")
    assert row is not None and row["state"] == "planned"


def test_operations_survive_reload_and_prune(tmp_path: Path) -> None:
    store = Store(tmp_path / "store")
    store.put_operation("key", "hash", "draft", None, ["new", "fact"])
    store.set_operation_state("key", "done")
    before = store.get_operation("key")
    assert before is not None
    assert before["request_digest"] == "hash"
    assert before["planned"] == ["new", "fact"]
    assert before["state"] == "done"

    put_file(store.path, "decision", decision_body("D1"))
    store.refresh_if_stale()
    assert store.get_decision("D1") is not None
    assert store.get_operation("key") == before

    assert store.prune_telemetry_events(older_than_days=0) == 1
    assert store.get_operation("key") is None
