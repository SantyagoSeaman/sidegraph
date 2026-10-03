"""``sidegraph-stats`` leads with one HEALTH line: ``ok``, or what is broken and the fix.

The report runs the integrity registry on the surface ``"stats"`` against its own read-only
snapshot connection and keeps the ``broken`` and ``degraded`` problems; advisory ones stay in the
MEMORY and ANCHORS blocks where they already show. The graph is opened and its freshness computed
once, and both are shared with the registry.
see design/superpowers/specs/2026-10-02-integrity-self-check-design.md (D7, T13)
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from sidegraph.stats.model import HealthItem, build_report
from sidegraph.stats.render import render_json, render_text
from sidegraph.store import SKIPPED_CANONICAL_KEY, Store
from tests.test_integrity import add_record, settled
from tests.test_stats_render import _report

NOW = datetime.now(UTC)


def _store_with(tmp_path, build):
    store = settled(Store(tmp_path / "s"))
    build(store)
    store.close()
    return tmp_path / "s"


def _health(store_dir, graph=None):
    return build_report(store_dir, graph, window_days=30, now=NOW).health


# -- the model ------------------------------------------------------------------------------


def test_a_healthy_store_has_no_health_items(tmp_path):
    store_dir = _store_with(tmp_path, lambda s: add_record(s, leaves=("live",)))
    assert _health(store_dir) == []


def test_t13_recent_orphaned_records_are_a_health_item(tmp_path):
    """Red against unfixed code: ``StatsReport`` has no ``health`` (AttributeError)."""

    def build(store):
        for _ in range(3):
            add_record(store, age_days=1)

    assert _health(_store_with(tmp_path, build)) == [
        HealthItem(
            check="orphaned-records",
            severity="degraded",
            summary="3 record(s) with every anchor orphaned",
            fix="sidegraph-doctor",
        )
    ]


def test_old_orphans_are_advisory_and_stay_out_of_health(tmp_path):
    def build(store):
        for _ in range(7):
            add_record(store, age_days=60)

    store_dir = _store_with(tmp_path, build)
    report = build_report(store_dir, None, window_days=30, now=NOW)
    assert report.health == []
    assert report.anchors is not None and report.anchors.orphaned == 7  # shown where it was


def test_skipped_store_files_are_a_health_item(tmp_path):
    def build(store):
        store.set_meta(
            SKIPPED_CANONICAL_KEY, json.dumps([{"path": "decisions/A.json", "reason": "bad id"}])
        )

    (item,) = _health(_store_with(tmp_path, build))
    assert (item.check, item.severity, item.summary, item.fix) == (
        "store-files-skipped",
        "degraded",
        "1 store file(s) skipped",
        "sidegraph-verify",
    )


def test_a_stale_graph_is_a_health_item_and_freshness_is_computed_once(tmp_path, monkeypatch):
    """The graph block and the registry share one ``freshness()`` call: git runs once."""
    from sidegraph.engine.reader import GraphifyReader
    from tests.test_graph_freshness import stale_repo

    fx = stale_repo(tmp_path)
    calls: list[int] = []
    real = GraphifyReader.freshness

    def counting(self, *args, **kwargs):
        calls.append(1)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(GraphifyReader, "freshness", counting)
    store_dir = _store_with(tmp_path, lambda s: None)

    report = build_report(store_dir, fx.graph, window_days=30, now=NOW)

    assert [(i.check, i.summary, i.fix) for i in report.health] == [
        ("graph-stale", "code graph stale", "graphify update .")
    ]
    assert report.graph.freshness == "stale"
    assert len(calls) == 1


def test_an_index_behind_the_store_files_runs_no_index_check(tmp_path):
    """Stats nulls the index-derived figures then, and the registry gets no index either."""

    def build(store):
        for _ in range(3):
            add_record(store, age_days=1)

    store_dir = _store_with(tmp_path, build)
    record = next((store_dir / "decisions").glob("*.json"))
    record.write_text(record.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    report = build_report(store_dir, None, window_days=30, now=NOW)

    assert report.index_stale is True
    assert report.health == []


def test_an_unreadable_graph_is_not_a_health_item(tmp_path):
    store_dir = _store_with(tmp_path, lambda s: None)
    bad = tmp_path / "graph.json"
    bad.write_text("{ not json")

    assert _health(store_dir, bad) == []


# -- the render -----------------------------------------------------------------------------

STALE = HealthItem(
    check="graph-stale", severity="degraded", summary="code graph stale", fix="graphify update ."
)
ORPHANED = HealthItem(
    check="orphaned-records",
    severity="degraded",
    summary="3 record(s) with every anchor orphaned",
    fix="sidegraph-doctor",
)


def _lines(report):
    return render_text(report).splitlines()


def test_health_is_the_first_block_after_the_header_and_says_ok_when_nothing_is_wrong():
    lines = _lines(_report())
    assert lines[1] == ""
    assert lines[2] == "HEALTH      ok"
    assert lines[3].startswith("ACTIVATION")


def test_one_item_reads_summary_arrow_fix():
    assert _lines(_report(health=[STALE]))[2] == "HEALTH      code graph stale → graphify update ."


def test_several_items_name_the_first_and_point_at_doctor():
    lines = _lines(_report(health=[STALE, ORPHANED]))
    assert lines[2:4] == [
        "HEALTH      2 problems: code graph stale",
        "              → graphify update .; all: sidegraph-doctor",
    ]


def test_a_line_wider_than_the_body_hangs_the_fix_under_it():
    """The same hanging rule as the stale-graph line: the head stays, the arrow and command
    move to their own line."""
    wide = HealthItem(
        check="x",
        severity="broken",
        summary="a summary of fifty characters, give or take a few",
        fix="sidegraph-verify --all-the-things-it-knows",
    )
    lines = _lines(_report(health=[wide]))
    assert lines[2:4] == [
        "HEALTH      a summary of fifty characters, give or take a few",
        "              → sidegraph-verify --all-the-things-it-knows",
    ]
    assert all(len(ln) <= 80 for ln in lines)


def test_health_is_in_the_json_as_a_list_of_four_key_items():
    data = json.loads(render_json(_report(health=[STALE, ORPHANED])))
    assert data["health"] == [
        {
            "check": "graph-stale",
            "severity": "degraded",
            "summary": "code graph stale",
            "fix": "graphify update .",
        },
        {
            "check": "orphaned-records",
            "severity": "degraded",
            "summary": "3 record(s) with every anchor orphaned",
            "fix": "sidegraph-doctor",
        },
    ]
    assert json.loads(render_json(_report()))["health"] == []
