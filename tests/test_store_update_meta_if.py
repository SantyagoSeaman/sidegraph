"""``Store.update_meta_if``: a host-supplied decision runs inside one immediate transaction.

A read of a meta row followed by a write is two transactions, so two processes can both read the
old value and both decide to write. ``update_meta_if`` reads, decides and writes under
``BEGIN IMMEDIATE``; the store never names the key or parses the value.
see design/superpowers/specs/2026-10-02-session-start-dedupe-atomic-design.md (D1, T3)
"""

from __future__ import annotations

import pytest

from sidegraph.store import Store


def test_update_meta_if_writes_the_string_decide_returns(tmp_path):
    store = Store(tmp_path / "s")
    seen: list[str | None] = []

    def decide(current: str | None) -> str | None:
        seen.append(current)
        return "v1"

    assert store.update_meta_if("k", decide) is True
    assert store.get_meta("k") == "v1"
    assert store.update_meta_if("k", lambda cur: f"{cur}+v2") is True
    assert store.get_meta("k") == "v1+v2"
    assert seen == [None]  # `decide` saw the absent row as None


def test_update_meta_if_writes_nothing_when_decide_returns_none(tmp_path):
    store = Store(tmp_path / "s")
    store.set_meta("k", "kept")

    assert store.update_meta_if("k", lambda cur: None) is False
    assert store.get_meta("k") == "kept"
    assert store.update_meta_if("absent", lambda cur: None) is False
    assert store.get_meta("absent") is None


def test_update_meta_if_rolls_back_and_reraises_when_decide_raises(tmp_path):
    store = Store(tmp_path / "s")
    store.set_meta("k", "kept")

    def decide(current: str | None) -> str | None:
        raise RuntimeError("decide failed")

    with pytest.raises(RuntimeError, match="decide failed"):
        store.update_meta_if("k", decide)
    assert store._conn.in_transaction is False  # the write lock is released
    assert store.get_meta("k") == "kept"


def test_update_meta_if_refuses_the_schema_version_key(tmp_path):
    store = Store(tmp_path / "s")
    before = store.schema_version

    with pytest.raises(ValueError):
        store.update_meta_if("schema_version", lambda cur: "9")
    assert store.schema_version == before
