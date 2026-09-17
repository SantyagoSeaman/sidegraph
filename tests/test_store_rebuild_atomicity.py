"""The rebuild must be invisible to other readers until it commits (design D1).

Mechanism 1 was: _reload_index_from_canonical DROPs the six record tables with bare DDL,
which autocommits, so a concurrent reader saw them vanish. These tests are deterministic --
no rounds, no timing -- because a transactional rebuild makes the failure reproducible on
demand instead of once in 800 opens.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest

import sidegraph.store as store_module
from sidegraph.schema import Decision, DecisionKind, DecisionStatus, Provenance
from sidegraph.store import Store


def _decision(title: str = "an adr") -> Decision:
    return Decision(
        title=title,
        kind=DecisionKind.ADR,
        status=DecisionStatus.ACCEPTED,
        context="c",
        choice="ch",
        valid_from=datetime.now(UTC),
        provenance=Provenance(source="manual"),
    )


def test_a_concurrent_reader_never_sees_a_missing_table(tmp_path, monkeypatch):
    """A second connection reading the record tables while a rebuild is in flight. Under D1
    it sees the pre-rebuild state; without D1 it sees an empty index.

    The literal `no such table` window (between the DROPs and the executescript that recreates
    them) is two adjacent statements with no test seam, so what a probe can observe is the
    torn read one step later -- same destruction, and it is the assertion below on the ROW
    COUNT that catches it."""
    store = Store(tmp_path / "s")
    store.add_decision(_decision())
    store.close()

    reader = sqlite3.connect(str(tmp_path / "s" / "index.db"))
    reader.row_factory = sqlite3.Row
    seen: dict[str, object] = {}

    original = Store._compute_domain_slug_conflicts

    def _probe(self):
        # Called as the LAST step of the rebuild body, with the transaction still open.
        seen["in_transaction"] = self._conn.in_transaction
        try:
            seen["domains"] = reader.execute("SELECT COUNT(*) FROM domains").fetchone()[0]
            seen["decisions"] = reader.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
        except sqlite3.OperationalError as e:
            seen["error"] = str(e)
        return original(self)

    monkeypatch.setattr(Store, "_compute_domain_slug_conflicts", _probe)

    # Bust the digest so this open takes the rebuild path.
    conn = sqlite3.connect(str(tmp_path / "s" / "index.db"))
    conn.execute("UPDATE meta SET value = 'stale' WHERE key = 'canonical_digest'")
    conn.commit()
    conn.close()

    reopened = Store(tmp_path / "s")
    reopened.close()
    reader.close()

    assert "error" not in seen, seen["error"]
    # The two assertions below are guards against DIFFERENT mutations, and neither is
    # redundant -- keep both. Together they are jointly complete against any single stray
    # commit in the rebuild body, which is why an earlier comment here calling this one
    # "documentation only" was wrong and got someone close to deleting it.
    #
    # Catches a LATE commit -- one placed after the decision re-INSERT, where the row count
    # below has already been restored to 1 and so stays green. Mutation-verified: a bare
    # commit() before the archive merge fails ONLY this line.
    # (It proves nothing about the UNFIXED code -- an implicit transaction is open there
    # too -- so it is not the red-first evidence; it is a regression guard.)
    assert seen["in_transaction"] is True, "rebuild body is not inside a transaction"
    # Catches an EARLY commit -- anything that publishes the dropped-and-recreated tables
    # before the rows are back, including executescript reintroduced among the CREATEs
    # (design D2). The reader can only see the pre-rebuild row if the whole rebuild, DROPs
    # included, is still uncommitted. Mutation-verified: executescript back inside the
    # transaction fails this line.
    assert seen["decisions"] == 1


def test_a_failed_rebuild_leaves_the_previous_index_intact(tmp_path, monkeypatch):
    """D1's other half: an exception mid-rebuild must roll back, not leave the six record
    tables dropped. Against the unfixed code the DROPs have already autocommitted."""
    store = Store(tmp_path / "s")
    store.add_decision(_decision())
    store.close()

    def _boom(self):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(Store, "_compute_domain_slug_conflicts", _boom)

    conn = sqlite3.connect(str(tmp_path / "s" / "index.db"))
    conn.execute("UPDATE meta SET value = 'stale' WHERE key = 'canonical_digest'")
    conn.commit()
    conn.close()

    with pytest.raises(RuntimeError):
        Store(tmp_path / "s")

    check = sqlite3.connect(str(tmp_path / "s" / "index.db"))
    assert check.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 1
    check.close()


def test_constructor_closes_connection_when_freshness_raises(tmp_path, monkeypatch):
    connections = []
    original_connect = sqlite3.connect

    def tracked_connect(*args, **kwargs):
        connection = original_connect(*args, **kwargs)
        connections.append(connection)
        return connection

    def fail_freshness(self):
        raise RuntimeError("freshness failure")

    monkeypatch.setattr(store_module.sqlite3, "connect", tracked_connect)
    monkeypatch.setattr(Store, "_refresh_freshness", fail_freshness)

    with pytest.raises(RuntimeError, match="freshness failure"):
        Store(tmp_path / "s")

    assert len(connections) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        connections[0].execute("SELECT 1")
