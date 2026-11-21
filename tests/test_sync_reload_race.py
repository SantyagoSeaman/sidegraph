"""A sync acknowledges only the stale mark it started from."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from sidegraph import domain_snapshot
from sidegraph import sync as sync_module
from sidegraph.store import Store
from sidegraph.store_layout import VOLATILE_STALE_KEY
from tests.test_hot_index import _reload_hot_pair, _synced_hot_pair
from tests.test_kept_statuses import _read, _titles
from tests.test_store_snapshot_wiring import _snap


def _edit_and_rebuild(root: Path, record: Path) -> None:
    data = json.loads(record.read_text(encoding="utf-8"))
    data["context"] += " A new revision."
    record.write_text(json.dumps(data) + "\n", encoding="utf-8")
    Store(root).close()


def _interrupt_after_gone(store, reader, monkeypatch, interrupt: Callable[[], None]) -> None:
    rebind_entity = sync_module.rebind_entity
    interrupted = False

    def rebind(entity, *args, **kwargs):
        nonlocal interrupted
        outcome = rebind_entity(entity, *args, **kwargs)
        if entity.canonical_name == "gone":
            interrupt()
            interrupted = True
        return outcome

    with monkeypatch.context() as patch:
        patch.setattr(sync_module, "rebind_entity", rebind)
        report = sync_module.sync(store, reader)
    # sync catches errors in the ladder. An interruption that raised did not run to completion.
    assert not report.skipped and interrupted, report.outcomes


def _read_context(tmp_path, monkeypatch, capsys, session: str) -> str:
    return _read(tmp_path, monkeypatch, capsys, "live.py", session)["hookSpecificOutput"][
        "additionalContext"
    ]


def test_a_rebuild_during_the_pass_leaves_the_index_cold(tmp_path, monkeypatch, capsys):
    root, reader, entities, record = _synced_hot_pair(tmp_path)
    gone = entities[1]
    with Store(root) as store:
        descriptor = gone.descriptor.model_copy(update={"file_path": "live.py"})
        store.upsert_entity(gone.model_copy(update={"descriptor": descriptor}))
        sync_module.sync(store, reader, force=True)
        assert [b.status for b in store.bindings_for_entity(gone.entity_id)] == ["orphaned"]
    _reload_hot_pair(root, record)

    live_titles = ["Legitimate live memory"]
    with Store(root) as store:
        _interrupt_after_gone(store, reader, monkeypatch, lambda: _edit_and_rebuild(root, record))
        context = _read_context(tmp_path, monkeypatch, capsys, "interrupted")
        assert (store.get_meta(VOLATILE_STALE_KEY), _titles(root, entities)[0]) == (
            "1",
            live_titles,
        )
        assert live_titles[0] in context and "Orphan must stay silent" not in context

        assert not sync_module.sync(store, reader).skipped
        assert store.get_meta(VOLATILE_STALE_KEY) == "0"
        assert [b.status for b in store.bindings_for_entity(gone.entity_id)] == ["orphaned"]
        assert _titles(root, entities)[0] == live_titles
        context = _read_context(tmp_path, monkeypatch, capsys, "healed")
        assert live_titles[0] in context and "Orphan must stay silent" not in context


def test_a_flag_set_during_the_pass_survives_it(tmp_path, monkeypatch):
    root, reader, _, record = _synced_hot_pair(tmp_path)
    _reload_hot_pair(root, record)

    def mark_again() -> None:
        with Store(root) as other:
            other.set_meta(VOLATILE_STALE_KEY, "1")

    with Store(root) as store:
        _interrupt_after_gone(store, reader, monkeypatch, mark_again)
        assert store.get_meta(VOLATILE_STALE_KEY) == "1"


def test_a_mark_between_the_start_reads_survives_the_pass(tmp_path, monkeypatch):
    """A new mark after the flag read cannot be acknowledged by the pass."""
    root, reader, _, record = _synced_hot_pair(tmp_path)
    _reload_hot_pair(root, record)

    with Store(root) as store:
        get_meta = store.get_meta
        marked = False

        def mark_after_flag_read(key: str) -> str | None:
            nonlocal marked
            value = get_meta(key)
            if key == VOLATILE_STALE_KEY and not marked:
                with Store(root) as other:
                    other.set_meta(VOLATILE_STALE_KEY, "1")
                marked = True
            return value

        monkeypatch.setattr(store, "get_meta", mark_after_flag_read)
        assert not sync_module.sync(store, reader).skipped
        assert marked and get_meta(VOLATILE_STALE_KEY) == "1"


def test_a_snapshot_change_during_the_pass_survives_it(tmp_path, monkeypatch):
    root, reader, _, record = _synced_hot_pair(tmp_path)
    _reload_hot_pair(root, record)

    def change_snapshot() -> None:
        domain_snapshot.write_snapshot(root, _snap())
        Store(root).close()

    with Store(root) as store:
        _interrupt_after_gone(store, reader, monkeypatch, change_snapshot)
        assert store.get_meta(VOLATILE_STALE_KEY) == "1"


def test_an_uninterrupted_pass_clears_the_flag(tmp_path):
    """Passes on base. Guards a fix that leaves every index cold."""
    root, reader, _, record = _synced_hot_pair(tmp_path)
    _reload_hot_pair(root, record)
    with Store(root) as store:
        assert not sync_module.sync(store, reader).skipped
        assert store.get_meta(VOLATILE_STALE_KEY) == "0"


def test_the_generation_counts_every_mark(tmp_path):
    root, _, _, record = _synced_hot_pair(tmp_path)
    with Store(root) as store:
        # Read the on-disk key even on a base checkout that lacks the constant.
        def generation() -> int:
            return int(store.get_meta("volatile_stale_gen") or "0")

        before = generation()
        _edit_and_rebuild(root, record)
        rebuilt = generation()
        assert rebuilt > before

        store.set_meta(VOLATILE_STALE_KEY, "1")
        marked = generation()
        assert marked > rebuilt

        domain_snapshot.write_snapshot(root, _snap())
        Store(root).close()
        snapshot = generation()
        assert snapshot > marked

        store.set_meta(VOLATILE_STALE_KEY, "0")
        assert generation() == snapshot
