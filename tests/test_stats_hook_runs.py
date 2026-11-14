"""The HOOK RUNS block of sidegraph-stats: counts from the hook run log, by hook and by cause."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from sidegraph.cli import stats_main
from sidegraph.host import hook_log
from sidegraph.host.hook_log import RunRow
from sidegraph.stats.model import HookRunCounts, HookRunStats, hook_run_stats
from sidegraph.stats.render import render_json, render_text
from sidegraph.store import Store
from tests.test_stats_render import BANNED, _report


def _runs() -> HookRunStats:
    return HookRunStats(
        hooks={
            "PreToolUse": HookRunCounts(
                runs=8168,
                delivered=353,
                partial=3,
                lock=3,
                error=1,
                empty={
                    "repeat": 1200,
                    "cap": 330,
                    "cold": 700,
                    "no-entity": 3100,
                    "no-record": 432,
                    "unrecorded": 2044,
                    "unknown": 5,
                },
                delivered_with_causes=350,
                causes_since=datetime(2026, 11, 13, 9, 30, tzinfo=UTC),
            ),
            "SessionStart": HookRunCounts(
                runs=134, delivered=120, partial=0, lock=0, error=0, empty={"duplicate": 14}
            ),
            "SubagentStart": HookRunCounts(
                runs=38,
                delivered=30,
                partial=0,
                lock=0,
                error=0,
                empty={"cold": 8},
                delivered_with_causes=30,
            ),
        }
    )


BLOCK = """\
HOOK RUNS   runs that opened the index, last 30 days (run log)
            PreToolUse: 8,168 runs; delivered on 353 (3 cut short by a lock)
              a record was bound on 2,580 since 2026-11-13: delivered 350 ·
              already delivered 1,200 · ten-file cap 330 · not synced 700
              other runs: no anchor 3,100 · no current record 432 ·
              cause not recorded 2,044 · cause unknown 5 · lock 3 · error 1
            SessionStart: 134 runs; delivered on 120
              other runs: duplicate start 14
            SubagentStart: 38 runs; delivered on 30
              a record was bound on 38: delivered 30 · not synced 8
"""


def test_rows_are_counted_by_hook_outcome_and_cause_inside_the_window():
    rows = [
        RunRow(50, "PreToolUse", "delivered", 9),  # before the cutoff: left out
        RunRow(100, "PreToolUse", "delivered", 9),
        RunRow(101, "PreToolUse", "empty", 9, "cap"),
        RunRow(102, "PreToolUse", "empty", 9, "cap"),
        RunRow(103, "PreToolUse", "lock", 500),
        RunRow(104, "PreToolUse", "lock", 500, "partial"),
        RunRow(105, "SubagentStart", "error", 9),
    ]
    stats = hook_run_stats(rows, 100)
    assert stats is not None
    assert stats.model_dump() == {
        "hooks": {
            "PreToolUse": {
                "runs": 5,
                "delivered": 2,
                "partial": 1,
                "lock": 1,
                "error": 0,
                "empty": {"cap": 2},
                "delivered_with_causes": 1,  # the partial one; the first delivery is older
                "causes_since": datetime.fromtimestamp(0.101, UTC),
            },
            "SubagentStart": {
                "runs": 1,
                "delivered": 0,
                "partial": 0,
                "lock": 0,
                "error": 1,
                "empty": {},
                "delivered_with_causes": None,
                "causes_since": None,
            },
        }
    }
    assert hook_run_stats(rows, 1000) is None
    assert hook_run_stats([], 0) is None


def test_the_share_counts_only_runs_the_log_explains():
    """Red against a share that sets every delivered run of the window beside causes that only
    newer lines carry: after an upgrade the window holds both kinds of line."""
    old = [RunRow(at, "PreToolUse", "delivered", 9) for at in range(10, 20)]
    old += [RunRow(at, "PreToolUse", "empty", 9) for at in range(20, 60)]  # no cause yet
    new = [RunRow(100, "PreToolUse", "empty", 9, "cap"), RunRow(101, "PreToolUse", "delivered", 9)]
    counts = hook_run_stats(old + new, 0).hooks["PreToolUse"]
    assert (counts.delivered, counts.delivered_with_causes) == (11, 1)
    assert counts.causes_since == datetime.fromtimestamp(0.1, UTC)
    text = render_text(_report(hook_runs=HookRunStats(hooks={"PreToolUse": counts})))
    assert "a record was bound on 2 since 1970-01-01: delivered 1 ·\n" in text
    assert "other runs: cause not recorded 40" in text

    # An older writer still at work after the first line with a cause: no share at all.
    mixed = hook_run_stats([*old, *new, RunRow(102, "PreToolUse", "empty", 9)], 0)
    counts = mixed.hooks["PreToolUse"]
    assert counts.delivered_with_causes is None and counts.causes_since is None
    text = render_text(_report(hook_runs=mixed))
    assert "a record was bound" not in text
    assert "other runs: ten-file cap 1 · cause not recorded 41" in text

    # Nothing older than the first line with a cause: no "since".
    counts = hook_run_stats(new, 0).hooks["PreToolUse"]
    assert (counts.delivered_with_causes, counts.causes_since) == (1, None)


def test_the_block_prints_fixed_words_only(tmp_path):
    """Red against a block that prints a log field as it finds it. A damaged or hand-edited
    log can hold another second field, a cause word this version does not know, or a word cut
    short: the text of the file never reaches the report, text or JSON, and the totals still
    add up (the odd hook is one count under ``other``, the odd cause one count under ``other``)."""
    store = tmp_path / "store"
    store.mkdir()
    (store / hook_log.LOG_NAME).write_text(
        "\n1000 src/secret.py empty 5 cold"
        "\n1001 PreToolUse empty 5 improved"
        "\n1002 PreToolUse empty 5 col"
        "\n1003 PreToolUse empty 5 cap"
        "\n1004 PreToolUse delivered 5",
        encoding="ascii",
    )
    stats = hook_run_stats(hook_log.read_runs(store), 0)
    assert stats is not None
    assert sorted(stats.hooks) == ["PreToolUse", "other"]
    assert (stats.hooks["PreToolUse"].runs, stats.hooks["other"].runs) == (4, 1)
    assert stats.hooks["PreToolUse"].empty == {"other": 2, "cap": 1}
    text = render_text(_report(hook_runs=stats))
    data = render_json(_report(hook_runs=stats))
    for shown in (text, data):
        for word in ("secret", "improved", "col\n", "col ", '"col"'):
            assert word not in shown.replace("cold", "")
    assert "other cause 2" in text
    assert "other hooks: 1 run; delivered on 0" in text


def test_the_block_states_counts_with_the_denominator_of_the_delivered_share():
    text = render_text(_report(hook_runs=_runs()))
    assert BLOCK in text
    assert text.index("HOOK RUNS") < text.index("REACH")
    for line in text.splitlines():
        assert len(line) <= 80, line
    for word in BANNED:
        assert word not in BLOCK.lower(), word


def test_a_report_without_a_run_log_has_no_block():
    assert "HOOK RUNS" not in render_text(_report())
    assert json.loads(render_json(_report()))["hook_runs"] is None


def test_json_carries_the_counts_and_withholds_them_with_recording_off():
    data = json.loads(render_json(_report(hook_runs=_runs())))
    assert data["hook_runs"]["hooks"]["SubagentStart"]["empty"] == {"cold": 8}
    assert data["hook_runs"]["hooks"]["PreToolUse"]["partial"] == 3
    off = json.loads(render_json(_report(hook_runs=_runs(), telemetry_enabled=False)))
    assert off["hook_runs"] is None


def test_the_command_reads_the_run_log_beside_the_store(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SIDEGRAPH_GRAPH", raising=False)
    store_dir = tmp_path / ".sidegraph"
    Store(store_dir).close()
    monkeypatch.setenv("SIDEGRAPH_DIR", str(store_dir))
    hook_log.record(store_dir, "PreToolUse", "empty", 18, "cap")
    hook_log.record(store_dir, "PreToolUse", "delivered", 20)
    hook_log.record(store_dir, "PreToolUse", "empty", 18, "no-entity")
    hook_log.record(store_dir, "PreToolUse", "lock", 510, "partial")

    def snapshot() -> dict[str, tuple[int, bytes]]:
        return {
            str(p.relative_to(store_dir)): (p.stat().st_mtime_ns, p.read_bytes())
            for p in sorted(store_dir.rglob("*"))
            if p.is_file()
        }

    before = snapshot()
    assert stats_main([]) == 0
    text = capsys.readouterr().out
    assert "PreToolUse: 4 runs; delivered on 2 (1 cut short by a lock)" in text
    assert "a record was bound on 3: delivered 2 · ten-file cap 1" in text
    assert "other runs: no anchor 1" in text
    assert stats_main(["--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["hook_runs"]["hooks"]["PreToolUse"]["empty"] == {"cap": 1, "no-entity": 1}
    assert snapshot() == before  # a read: no file was written, created or removed

    monkeypatch.setenv("SIDEGRAPH_TELEMETRY", "off")
    assert stats_main([]) == 0
    assert "HOOK RUNS" not in capsys.readouterr().out
