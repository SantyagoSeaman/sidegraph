"""``_mutation(immediate=True)`` (design D6, entity-identity-uniqueness spec).

The upcoming atomic get-or-create (Task 2) needs a way to take SQLite's write lock (via
``BEGIN IMMEDIATE``) at the START of a check-then-create sequence, so a second connection
cannot slip its own lookup in between. The naive way to add that flag -- gate it on the
MUTATION DEPTH ("only BEGIN at depth 1") -- is wrong: Python's legacy transaction control
opens no transaction until the first DML statement, so an outer ``_mutation()`` that only
READS holds no lock at all, and a depth-gated nested ``immediate`` would then no-op right
through the hole this flag exists to close (design D6 -- review reproduced two ids minted
that way). The correct gate is ``self._conn.in_transaction``, checked at any depth.

All four tests below are a DECLARED EXCEPTION to "every new test observed failing against
unfixed code" (spec §4 item 8's third bullet): today's ``Store._mutation`` takes no
``immediate`` keyword at all, so every one of these fails with
``TypeError: _mutation() got an unexpected keyword argument 'immediate'`` -- the "cannot run
at all" shape, not a behavioural red. Report it as that, not as red-first evidence.
"""

from __future__ import annotations

import sqlite3

import pytest

from sidegraph.store import Store


def _other_with_short_timeout(tmp_path) -> Store:
    """A second ``Store`` on the SAME directory, standing in for a second process's own
    connection -- lowered ``busy_timeout`` so a genuinely-locked ``BEGIN IMMEDIATE`` raises
    in milliseconds instead of blocking for the default 5s."""
    other = Store(tmp_path / "s")
    other._conn.execute("PRAGMA busy_timeout = 100")
    return other


def test_immediate_takes_a_write_lock_at_depth_one(tmp_path):
    store = Store(tmp_path / "s")
    with store._mutation(immediate=True):
        other = _other_with_short_timeout(tmp_path)
        try:
            with pytest.raises(sqlite3.OperationalError, match="database is locked"):
                other._conn.execute("BEGIN IMMEDIATE")
        finally:
            other.close()


def test_immediate_nested_inside_a_read_only_outer_mutation_still_serializes(tmp_path):
    """The D6 hole: an outer ``_mutation()`` that only reads holds NO lock
    (``in_transaction`` is False, measured), so a depth-gated ``immediate`` would treat the
    nested call as a no-op. Gating on ``in_transaction`` instead means the nested immediate
    still takes the write lock for real -- red against a depth-gated implementation."""
    store = Store(tmp_path / "s")
    with store._mutation():
        store.get_entity("does-not-exist")  # read-only: opens no transaction
        assert store._conn.in_transaction is False
        with store._mutation(immediate=True):
            other = _other_with_short_timeout(tmp_path)
            try:
                with pytest.raises(sqlite3.OperationalError, match="database is locked"):
                    other._conn.execute("BEGIN IMMEDIATE")
            finally:
                other.close()


def test_immediate_inside_an_outer_mutation_that_already_wrote_does_not_double_begin(tmp_path):
    """The outer scope performs DML first (so the connection has already implicitly begun a
    transaction); the nested ``immediate=True`` must see ``in_transaction`` already True and
    skip its own ``BEGIN IMMEDIATE`` -- issuing a second ``BEGIN`` on an already-open
    transaction raises ``OperationalError: cannot start a transaction within a
    transaction``."""
    store = Store(tmp_path / "s")
    with store._mutation():
        store._conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            ("test-marker", "1"),
        )
        assert store._conn.in_transaction is True
        with store._mutation(immediate=True):
            pass  # must not raise "cannot start a transaction within a transaction"
        assert store._mutation_depth == 1


def test_the_outermost_scope_still_owns_commit_and_rollback_when_begin_happened_at_depth_two(
    tmp_path, monkeypatch
):
    """The nested ``immediate=True`` is the one that actually issues ``BEGIN IMMEDIATE``
    (the outer scope only read), but commit/rollback ownership must stay with the OUTERMOST
    scope regardless of which depth opened the transaction."""
    store = Store(tmp_path / "s")
    commit_calls = {"n": 0}
    original_commit = Store._commit

    def _counting_commit(self):
        commit_calls["n"] += 1
        return original_commit(self)

    monkeypatch.setattr(Store, "_commit", _counting_commit)

    with store._mutation():
        assert store._conn.in_transaction is False
        with store._mutation(immediate=True):
            assert store._conn.in_transaction is True
        # Back at depth 1: the transaction BEGIN IMMEDIATE opened at depth 2 is still open.
        assert store._mutation_depth == 1
        assert store._conn.in_transaction is True
    assert commit_calls["n"] == 1
    assert store._conn.in_transaction is False
    assert store._mutation_depth == 0

    # Rollback side of the same contract: an exception raised in the outer scope AFTER the
    # depth-2 immediate returns must still roll back the whole thing, from the outermost
    # scope, not leak a transaction.
    with pytest.raises(RuntimeError), store._mutation():
        with store._mutation(immediate=True):
            pass
        raise RuntimeError("boom")
    assert store._conn.in_transaction is False
    assert store._mutation_depth == 0


def test_immediate_inside_an_outer_immediate_does_not_double_begin(tmp_path):
    """The other half of "already in a transaction, skip the BEGIN" (alongside the
    DML-triggered-implicit-transaction case above): the OUTER scope itself already issued
    ``BEGIN IMMEDIATE``, so the nested ``immediate=True`` must see ``in_transaction`` already
    True and skip its own -- issuing a second ``BEGIN`` here would raise the same
    ``OperationalError: cannot start a transaction within a transaction``."""
    store = Store(tmp_path / "s")
    with store._mutation(immediate=True):
        assert store._conn.in_transaction is True
        with store._mutation(immediate=True):  # must not raise
            pass
        assert store._mutation_depth == 1
    assert store._conn.in_transaction is False
