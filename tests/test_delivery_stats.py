"""Read-only delivery reporting keeps new occurrence units apart from legacy activation."""

from __future__ import annotations

import json
import os
from urllib.parse import quote

from sidegraph.delivery import Delivery, Placement, Snapshot
from sidegraph.stats.model import build_report
from sidegraph.stats.render import render_json, render_text
from sidegraph.store import Store


def test_stats_reports_mcp_and_hook_units_without_adding_legacy_showings(tmp_path, monkeypatch):
    monkeypatch.setenv("SIDEGRAPH_TELEMETRY", "on")
    store = Store(tmp_path / "s")
    fact = Snapshot("synthetic", "fact", "fact", "proposed")
    assert store.record_delivery(Delivery("query_decisions", [Placement(fact)]))
    assert store.record_delivery(
        Delivery(
            "hook_read",
            [Placement(fact, "a.py"), Placement(fact, "b.py")],
            session_id="synthetic-session",
        )
    )
    before = (store.path / "index.db").read_bytes()
    report = build_report(store.path, None)
    data = json.loads(render_json(report))
    evidence = data.get("delivery_evidence")
    assert evidence is not None, "new evidence must be available separately"
    assert evidence["available"]
    assert evidence["mcp"]["occurrences"] == 1
    assert evidence["mcp"]["reported_placements"] == 1
    assert evidence["mcp"]["reported_anchor_links"] == 0
    assert evidence["hooks"]["occurrences"] == 1
    assert evidence["hooks"]["unique_retained_records"] == 1
    assert evidence["hooks"]["reported_placements"] == 2
    assert evidence["hooks"]["reported_anchor_links"] == 2
    assert evidence["hooks"]["placement_statuses"] == {"proposed": 2}
    assert data["activation"]["showings"] == 0
    text = render_text(report)
    assert "MCP" in text and "placements" in text
    assert (store.path / "index.db").read_bytes() == before
    store.close()


def test_old_index_reports_unavailable_evidence_without_migration(tmp_path, monkeypatch):
    monkeypatch.setenv("SIDEGRAPH_TELEMETRY", "on")
    store = Store(tmp_path / "s")
    for table in ("delivery_anchor_links", "delivery_placements", "delivery_occurrences"):
        store._conn.execute(f"DROP TABLE {table}")
    store._conn.commit()
    before = (store.path / "index.db").read_bytes()
    data = json.loads(render_json(build_report(store.path, None)))
    evidence = data.get("delivery_evidence")
    assert evidence is not None
    assert evidence["available"] is False
    assert evidence["mcp"] is None and evidence["hooks"] is None
    assert (store.path / "index.db").read_bytes() == before
    store.close()


def test_stopped_telemetry_withholds_new_journal_figures(tmp_path, monkeypatch):
    store = Store(tmp_path / "s")
    assert store.record_delivery(Delivery("query_decisions"))
    monkeypatch.setenv("SIDEGRAPH_TELEMETRY", "off")
    data = json.loads(render_json(build_report(store.path, None)))
    assert "delivery_evidence" in data
    assert data["delivery_evidence"] is None
    store.close()


def test_optional_stats_read_skips_lock_and_restores_legacy_timeout(tmp_path):
    import sqlite3
    import time

    from sidegraph.stats.model import _delivery_evidence

    store = Store(tmp_path / "s")
    conn = sqlite3.connect(
        f"file:{quote(os.fsencode(str(store.path / 'index.db')))}?mode=ro", uri=True, timeout=1.0
    )
    conn.row_factory = sqlite3.Row
    locker = sqlite3.connect(store.path / "index.db")
    locker.execute("BEGIN EXCLUSIVE")
    try:
        start = time.monotonic()
        evidence = _delivery_evidence(conn, "2026-01-01T00:00:00+00:00")
        assert not evidence.available
        assert time.monotonic() - start < 0.75
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 1000
    finally:
        locker.rollback()
        locker.close()
        conn.close()
        store.close()
