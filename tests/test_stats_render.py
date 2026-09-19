"""The wording IS the deliverable (design 2026-09-18-usage-stats-design.md, D1/D2/D7).

Every state the renderer can be handed is exercised against the same causal-word ban, because
the promise is about the surface, not about the happy path.
"""

from __future__ import annotations

import pytest

from sidegraph.stats.model import (
    ActivationStats,
    AnchorStats,
    GraphStats,
    MemoryStats,
    ReachStats,
    StatsReport,
)
from sidegraph.stats.render import render_text

# The brief's six, plus the wider family a well-meaning edit reaches for. The renderer states
# what was shown, asked and touched; none of these belongs on any surface (D2).
BANNED = (
    "improve",
    "prevent",
    "caused",
    "thanks to",
    "helped",
    "saved",
    "because",
    "led to",
    "resulted",
    "reduced",
    "avoided",
    "boost",
    "benefit",
    "due to",
)
LABELS = ("ACTIVATION", "REACH", "MEMORY", "GRAPH", "ANCHORS")


def _report(**over):
    base = StatsReport(
        repo="demo",
        window_days=30,
        retained_days=13,
        telemetry_enabled=True,
        activation=ActivationStats(
            sessions_total=60,
            sessions_with_retrieval=24,
            sessions_asked_but_empty=3,
            sessions_touch_only=36,
            showings=397,
            degraded=3,
            dropped_for_budget=0,
            renders_with_abandoned=11,
            mature=True,
        ),
        reach=ReachStats(
            files_touched=259,
            files_touched_with_memory=36,
            busiest_seeds=[("store.py", 12), ("capture.py", 9), ("cli.py", 6)],
            silent_domains=["CLI"],
        ),
        memory=MemoryStats(
            decisions=242,
            facts=119,
            domains=19,
            historical=57,
            no_recorded_showing=230,
            surfaceable=361,
            accepted_in_window=19,
            rejected_in_window=9,
            auto_accepted_in_window=17,
        ),
        graph=GraphStats(available=True, nodes=1842, files=214, communities=31),
        anchors=AnchorStats(live=191, degraded=0, orphaned=8),
    )
    return base.model_copy(update=over)


def _act(**over):
    return _report().activation.model_copy(update=over)


def _reach(**over):
    return _report().reach.model_copy(update=over)


def _memory(**over):
    return _report().memory.model_copy(update=over)


def _immature():
    return _report(
        activation=_act(
            sessions_total=2,
            sessions_with_retrieval=1,
            sessions_asked_but_empty=0,
            sessions_touch_only=1,
            mature=False,
        )
    )


def _stale(**over):
    """The index is behind the canonical files: every record-derived figure is None."""
    return _report(
        index_stale=True,
        memory=None,
        anchors=None,
        reach=_reach(files_touched_with_memory=None, silent_domains=None),
        **over,
    )


def _narrowed_window():
    """`--window 7` over a journal whose only rows are older than that."""
    return _report(
        window_days=7,
        retained_days=0,
        activation=_act(
            sessions_total=0,
            sessions_with_retrieval=0,
            sessions_asked_but_empty=0,
            sessions_touch_only=0,
            showings=0,
            mature=False,
            render_journal=False,
            outside_window=True,
        ),
        reach=_reach(files_touched=0, files_touched_with_memory=0, busiest_seeds=[]),
    )


def _lines(text: str) -> list[str]:
    return text.splitlines()


def _block(text: str, label: str) -> list[str]:
    """The label's first line plus its continuation lines (indented 12 spaces)."""
    lines = _lines(text)
    start = next(i for i, ln in enumerate(lines) if ln.startswith(label))
    out = [lines[start]]
    for ln in lines[start + 1 :]:
        if not ln.startswith(" " * 12):
            break
        out.append(ln)
    return out


def _states() -> dict[str, StatsReport]:
    zeros = _report(
        activation=ActivationStats(
            sessions_total=0,
            sessions_with_retrieval=0,
            sessions_asked_but_empty=0,
            sessions_touch_only=0,
            showings=0,
            degraded=0,
            dropped_for_budget=0,
            renders_with_abandoned=0,
            mature=False,
            render_journal=False,
        ),
        reach=ReachStats(
            files_touched=0, files_touched_with_memory=0, busiest_seeds=[], silent_domains=[]
        ),
        memory=MemoryStats(
            decisions=0,
            facts=0,
            domains=0,
            historical=0,
            no_recorded_showing=0,
            surfaceable=0,
            accepted_in_window=0,
            rejected_in_window=0,
            auto_accepted_in_window=0,
        ),
        anchors=AnchorStats(),
        retained_days=0,
    )
    wrapped = _report(
        activation=_act(renders_with_abandoned=1),
        reach=_reach(
            files_touched=8,
            files_touched_with_memory=1,  # 12.5% — a half rounds up, not to even
            busiest_seeds=[
                ("src/sidegraph/engine/reader.py::GraphifyReader._to_node", 40),
                ("store.py", 12),
                ("capture.py", 9),
            ],
            silent_domains=[
                "Anchoring & Sync",
                "Release & Leak Safety",
                "User Documentation",
                "Decision Store",
            ],
        ),
        memory=_memory(
            decisions=5,
            facts=3,
            domains=1,
            historical=1,
            no_recorded_showing=1,
            surfaceable=8,
            accepted_in_window=1,
            rejected_in_window=0,
            auto_accepted_in_window=0,
        ),
    )
    quiet_and_off = _report(
        telemetry_enabled=False,
        reach=_reach(silent_domains=[]),
        memory=_memory(
            decisions=0,
            facts=0,
            domains=0,
            historical=0,
            surfaceable=0,
            no_recorded_showing=0,
            accepted_in_window=0,
            rejected_in_window=0,
            auto_accepted_in_window=0,
        ),
        anchors=AnchorStats(),
    )
    # No sessions asked means no render rows: build_report counts any session with a render
    # row as asking, so degraded/rejected can only be zero here and the journal is "empty".
    nothing_asked = _report(
        activation=_act(
            sessions_with_retrieval=0,
            sessions_asked_but_empty=0,
            sessions_touch_only=60,
            showings=0,
            degraded=0,
            dropped_for_budget=0,
            renders_with_abandoned=0,
            render_journal=False,
        ),
        reach=_reach(files_touched=0, files_touched_with_memory=0, busiest_seeds=[]),
    )
    return {
        "narrowed-window": _narrowed_window(),
        "stale-index": _stale(),
        "stale-index-recording-off": _stale(telemetry_enabled=False),
        "mature": _report(),
        "immature": _immature(),
        "telemetry-off": _report(telemetry_enabled=False),
        "no-graph": _report(graph=GraphStats(available=False)),
        "empty-graph": _report(graph=GraphStats(available=True)),
        "unreadable-graph": _report(graph=GraphStats(unreadable=True)),
        "no-render-journal": _report(activation=_act(render_journal=False)),
        "drill-down-only": _report(activation=_act(budget_journal=False)),
        "all-zero": zeros,
        "all-asks-empty": _report(
            activation=_act(sessions_with_retrieval=24, sessions_asked_but_empty=24)
        ),
        # The four screens the golden table added later. The 80-column and causal-word sweeps
        # walk this dict, so a screen missing here is a screen no sweep reads.
        "large-numbers": _big(),
        "wrapped-and-rounded": wrapped,
        "quiet-and-off": quiet_and_off,
        "nothing-asked": nothing_asked,
    }


# --- the headline and the layout --------------------------------------------------------


def test_activation_leads_the_report():
    body = "\n".join(ln for ln in render_text(_report()).splitlines() if ln.strip())
    first_block = body.split("REACH")[0]
    assert "ACTIVATION" in first_block
    assert "24 of 60 sessions" in first_block
    assert "MEMORY" not in first_block, "inventory is reference, not headline (D1)"


def test_header_puts_the_repo_left_and_the_window_right():
    first = _lines(render_text(_report()))[0]
    assert first.startswith("Sidegraph · demo")
    assert first.endswith("window: 30 days (13 retained)")


def test_a_one_day_window_is_not_written_as_one_days():
    first = _lines(render_text(_report(window_days=1, retained_days=1)))[0]
    assert first.endswith("window: 1 day (1 retained)")


def test_blocks_come_in_the_specified_order_with_one_blank_line_before_memory():
    lines = _lines(render_text(_report()))
    starts = [next(i for i, ln in enumerate(lines) if ln.startswith(lab)) for lab in LABELS]
    assert starts == sorted(starts)
    assert lines[1] == "", "one blank line under the header"
    assert lines[starts[2] - 1] == "", "the blank line sits between REACH and MEMORY"
    blank_lines = [i for i, ln in enumerate(lines) if not ln.strip()]
    assert blank_lines == [1, starts[2] - 1], "no other blank lines inside the screen"


def test_labels_are_twelve_wide_and_continuations_align_under_them():
    lines = _lines(render_text(_report()))[2:]
    for ln in lines:
        if not ln.strip():
            continue
        if ln.startswith(" "):
            assert ln.startswith(" " * 12) and not ln.startswith(" " * 13), ln
        else:
            assert ln.split()[0] in LABELS, ln
            assert ln[:12].rstrip() in LABELS and ln[11] == " " and ln[12] != " ", ln


def test_the_screen_fits_eighty_columns_and_ends_with_one_newline():
    for name, rep in _states().items():
        text = render_text(rep)
        assert text.endswith("\n") and not text.endswith("\n\n"), name
        assert all(len(ln) <= 80 for ln in _lines(text)), name


def test_a_very_long_seed_name_is_clipped_not_allowed_to_wrap_the_screen():
    long_seed = "src/sidegraph/engine/reader.py::GraphifyReader._to_node_with_a_long_tail"
    rep = _report(reach=_reach(busiest_seeds=[(long_seed, 40), ("store.py", 12)]))
    text = render_text(rep)
    assert all(len(ln) <= 80 for ln in _lines(text))
    assert "store.py ×12" in text
    assert long_seed not in text


# --- what the report may say ------------------------------------------------------------


def test_no_causal_word_appears_anywhere():
    for name, rep in _states().items():
        text = render_text(rep).lower()
        for word in BANNED:
            assert word not in text, (
                f"causal wording is forbidden on every surface (D2): {name}: {word!r}"
            )


def test_an_immature_window_states_itself_and_prints_no_ratio_anywhere():
    text = render_text(_immature())
    assert "too little to summarize" in text
    assert "%" not in text, "D7 withholds every ratio, REACH included"


def test_an_immature_activation_block_is_one_line_naming_sessions_and_days():
    lines = _block(render_text(_immature()), "ACTIVATION")
    assert lines == ["ACTIVATION  too little to summarize yet — 2 sessions over 13 days"]


def test_immature_wording_counts_one_session_and_one_day_in_the_singular():
    rep = _report(
        retained_days=1,
        activation=_act(sessions_total=1, sessions_with_retrieval=1, mature=False),
    )
    assert "1 session over 1 day" in render_text(rep)


def test_an_immature_report_keeps_the_counts_it_can_state_without_a_ratio():
    reach = _block(render_text(_immature()), "REACH")
    assert reach[0] == "REACH       259 files touched, 36 with memory anchored to them"


def test_mature_ratios_are_integers_rounded_half_up():
    text = render_text(_report())
    assert "36 with memory anchored to them (14%)" in text  # 13.9
    assert "230 of 361 decisions and facts (64%)" in text  # 63.7
    assert "." not in "".join(ln for ln in _lines(text) if "%" in ln).replace("store.py", "")


def test_telemetry_off_says_so():
    assert "SIDEGRAPH_TELEMETRY=off" in render_text(_report(telemetry_enabled=False))


def test_telemetry_off_prints_nothing_derived_from_the_journal():
    text = render_text(_report(telemetry_enabled=False))
    assert _block(text, "ACTIVATION") == ["ACTIVATION  recording is off (SIDEGRAPH_TELEMETRY=off)"]
    for stale in (
        "files touched",
        "asked about",
        "never surfaced",
        "not shown yet",
        "no recorded showing",
        "records shown",
        "showings",
        "%",
    ):
        assert stale not in text, f"a stopped recorder must not leave a number behind: {stale!r}"
    assert "no record is bound to" in text, "store-derived lines still stand"
    assert "242 decisions" in text


def test_a_missing_graph_names_the_next_command():
    text = render_text(_report(graph=GraphStats(available=False)))
    assert "sidegraph-init" in text
    assert "GRAPH       not built yet → sidegraph-init" in text
    assert "nodes" not in text


def test_an_empty_but_built_graph_gets_its_own_line_not_a_row_of_zeroes():
    text = render_text(_report(graph=GraphStats(available=True)))
    assert "0 nodes" not in text and "0 files" not in text
    (line,) = _block(text, "GRAPH")
    assert "no nodes" in line and "graphify update" in line


def test_an_unreadable_graph_does_not_send_the_reader_to_sidegraph_init():
    """`sidegraph-init` answers "found graph" for any file that exists, so pointing a corrupt
    graph at it is a dead end. The line must say the file is there and what to run instead."""
    text = render_text(_report(graph=GraphStats(unreadable=True)))
    (line,) = _block(text, "GRAPH")
    assert "could not be read" in line and "graphify update" in line
    assert "sidegraph-init" not in text.split("GRAPH", 1)[1].split("ANCHORS", 1)[0]
    assert "not built" not in line and "0 nodes" not in text


def test_a_missing_render_journal_prints_no_number_it_never_recorded():
    """A store from before the render journal has zeroes only because nothing could be
    recorded. "0 dropped by it" would read as a measurement, so the lines are replaced, and
    the counts the retrieval journal does hold are untouched."""
    text = render_text(_report(activation=_act(render_journal=False)))
    assert "shortened" not in text and "dropped by it" not in text
    assert "included something already tried" not in text
    assert "not recorded yet" in text
    # No advice about the cause. An empty journal is also what a current install shows when
    # nobody called a budgeted lookup (drill_down writes no render row), so "update your
    # server" would be false there and nothing in the report can tell the two apart.
    (line,) = [ln for ln in _block(text, "ACTIVATION") if "not recorded yet" in ln]
    assert line.strip() == "budget and tried-and-abandoned counts: not recorded yet"
    assert "version" not in text and "begin" not in text
    assert "memory was asked in 24 of 60 sessions" in text
    assert "397 showings (repeats counted)" in text


def test_a_window_with_only_drill_downs_states_the_budget_as_unrecorded_and_keeps_the_rest():
    """A drill-down applies no budget, so its zeros are not "0 dropped by it". Its
    tried-and-abandoned flags are real, so that count stays."""
    text = render_text(_report(activation=_act(budget_journal=False, renders_with_abandoned=2)))
    assert "shortened" not in text and "dropped by it" not in text
    lines = [ln.strip() for ln in _block(text, "ACTIVATION")]
    assert "budget counts: not recorded yet" in lines
    assert "2 lookups included something already tried and abandoned" in lines
    assert "budget and tried-and-abandoned counts" not in text


def test_a_drill_down_only_window_in_a_narrowed_window_says_so():
    text = render_text(_report(activation=_act(budget_journal=False, outside_window=True)))
    assert "budget counts: not recorded in this window" in text


def test_a_current_render_journal_prints_the_budget_lines_as_before():
    text = render_text(_report())
    assert "not recorded yet" not in text
    assert "3 shortened to fit the budget, 0 dropped by it" in text


def test_healthy_anchors_do_not_advertise_doctor():
    clean = _report(anchors=AnchorStats(live=191, degraded=0, orphaned=0))
    assert "sidegraph-doctor" not in render_text(clean)


def test_degraded_or_orphaned_anchors_point_at_doctor():
    assert "ANCHORS     191 live · 0 degraded · 8 orphaned → sidegraph-doctor" in render_text(
        _report()
    )
    only_degraded = _report(anchors=AnchorStats(live=5, degraded=1, orphaned=0))
    assert "→ sidegraph-doctor" in render_text(only_degraded)


def test_a_store_with_no_bindings_says_none_yet_and_advertises_nothing():
    text = render_text(_report(anchors=AnchorStats()))
    assert "ANCHORS     none yet" in text
    assert "sidegraph-doctor" not in text


# --- the four rulings -------------------------------------------------------------------


def test_asked_and_got_nothing_is_its_own_line_apart_from_silence():
    lines = _block(render_text(_report()), "ACTIVATION")
    joined = "\n".join(lines)
    assert "3 of those got nothing back" in joined
    assert "36 sessions touched files without asking" in joined
    assert "39 sessions" not in joined, "the failure mode must not be folded into the silent count"


def test_when_every_asking_session_got_records_the_line_says_so_rather_than_a_zero():
    rep = _report(activation=_act(sessions_asked_but_empty=0, sessions_touch_only=36))
    assert "every one of those got records back" in render_text(rep)


def test_when_nothing_was_asked_there_is_no_of_those_line():
    rep = _report(
        activation=_act(
            sessions_with_retrieval=0,
            sessions_asked_but_empty=0,
            sessions_touch_only=60,
            showings=0,
        )
    )
    text = render_text(rep)
    assert "memory was asked in 0 of 60 sessions" in text
    assert "of those" not in text
    assert "~" not in text, "no per-session average over zero sessions"


def test_a_session_that_only_got_nothing_back_does_not_divide_by_zero():
    rep = _report(
        activation=_act(
            sessions_with_retrieval=24,
            sessions_asked_but_empty=24,
            sessions_touch_only=36,
            showings=0,
        )
    )
    text = render_text(rep)
    assert "24 of those got nothing back" in text
    assert "0 showings" in text and "~" not in text


def test_the_per_session_average_is_over_sessions_that_received_records():
    text = render_text(_report())  # 397 records over 24 - 3 = 21 sessions
    assert "397 showings (repeats counted), ~19 per session that got any" in text


def test_the_budget_line_separates_shortened_from_dropped():
    text = render_text(_report(activation=_act(degraded=3, dropped_for_budget=2)))
    assert "3 shortened to fit the budget, 2 dropped by it" in text


def test_the_tried_and_abandoned_line_counts_lookups():
    assert "11 lookups included something already tried and abandoned" in render_text(_report())


def test_the_cumulative_lines_say_all_time_and_never_borrow_the_window():
    text = render_text(_report())
    (busiest,) = [ln for ln in _lines(text) if "asked about most" in ln]
    assert "all time" in busiest and "window" not in busiest
    assert "store.py ×12 · capture.py ×9 · cli.py ×6" in busiest
    (unshown,) = [ln for ln in _lines(text) if "no recorded showing" in ln]
    assert "all time" in unshown and "window" not in unshown
    assert "230 of 361 decisions and facts (64%)" in unshown


def test_the_ratification_funnel_is_the_one_memory_line_that_says_it_is_windowed():
    text = render_text(_report())
    assert "in this window: 19 accepted (17 automatically), 9 rejected" in text


def test_a_funnel_with_no_verdicts_says_so():
    rep = _report(
        memory=_memory(accepted_in_window=0, rejected_in_window=0, auto_accepted_in_window=0)
    )
    assert "no accept or reject verdicts in this window" in render_text(rep)


def test_history_has_its_own_line_and_the_inventory_says_it_is_accepted_only():
    lines = _block(render_text(_report()), "MEMORY")
    assert lines[0] == "MEMORY      accepted: 242 decisions · 119 facts · 19 domains"
    assert "MEMORY".ljust(12) + "accepted: 242 decisions" in lines[0]
    assert any(ln.strip() == "57 more kept as history" for ln in lines)


def test_an_empty_history_states_itself():
    rep = _report(memory=_memory(historical=0))
    assert "no history yet" in render_text(rep)


def test_the_silent_domains_line_states_what_is_measured():
    text = render_text(_report(reach=_reach(silent_domains=["CLI", "Flow Profiles"])))
    assert "silent domains: CLI, Flow Profiles — no record is bound to them" in text
    assert "hold no decision" not in text and "hold" not in text


def test_a_silent_domain_list_at_the_aggregators_cap_is_marked_as_possibly_cut():
    four = ["A", "B", "C", "D"]
    cut = render_text(_report(reach=_reach(silent_domains=four)))
    assert "silent domains: A, B, C, D, … — no record is bound to them" in cut
    three = render_text(_report(reach=_reach(silent_domains=four[:3])))
    assert "…" not in three


def test_a_single_silent_domain_reads_in_the_singular():
    assert "silent domains: CLI — no record is bound to it" in render_text(_report())


def test_no_silent_domains_means_no_silent_line():
    assert "no decision is bound" not in render_text(_report(reach=_reach(silent_domains=[])))


def test_zero_files_touched_is_a_sentence_not_a_ratio_of_zeroes():
    rep = _report(reach=_reach(files_touched=0, files_touched_with_memory=0))
    text = render_text(rep)
    assert "no files touched" in text
    assert "0 of them" not in text and "(0%)" not in text


def test_the_no_showing_line_drops_its_percentage_while_the_window_is_immature():
    text = render_text(_immature())
    (line,) = [ln for ln in _lines(text) if "no recorded showing" in ln]
    assert "no recorded showing, all time: 230 of 361 decisions and facts" in line
    assert "%" not in line


def test_no_screen_says_a_record_was_never_shown():
    """The counter holds an absence of a record. A showing while recording was off, or one lost
    to a writer error the server swallows, is in no counter, so "never" would state as fact
    what the report cannot know."""
    for name, rep in _states().items():
        text = render_text(rep).lower()
        for claim in ("never surfaced", "never shown", "not shown yet"):
            assert claim not in text, f"{name}: {claim!r}"


# --- fix round 1: separators, markers, edges ---------------------------------------------


def _big():
    """Every count at five or six digits: a bare `{n}` anywhere prints without a separator."""
    return _report(
        activation=ActivationStats(
            sessions_total=12345,
            sessions_with_retrieval=10234,
            sessions_asked_but_empty=234,
            sessions_touch_only=2111,
            showings=456789,
            degraded=3456,
            dropped_for_budget=1234,
            renders_with_abandoned=5678,
            mature=True,
        ),
        reach=ReachStats(
            files_touched=45678,
            files_touched_with_memory=12345,
            busiest_seeds=[("store.py", 12345), ("capture.py", 9876)],
            silent_domains=["CLI"],
        ),
        memory=MemoryStats(
            decisions=30000,
            facts=6153,
            domains=19,
            historical=12000,
            no_recorded_showing=23023,
            surfaceable=36153,
            accepted_in_window=1234,
            rejected_in_window=1200,
            auto_accepted_in_window=1000,
        ),
        graph=GraphStats(available=True, nodes=123456, files=12345, communities=1234),
        anchors=AnchorStats(live=191000, degraded=7900, orphaned=8000),
    )


def test_every_number_is_grouped_in_thousands():
    import re

    text = render_text(_big())
    # Any run of five or more bare digits is an un-grouped number. The window (30), the
    # 12-wide label column and the header carry none.
    assert re.findall(r"(?<![\d,])\d{4,}(?![\d,])", text) == [], text
    for grouped in (
        "10,234 of 12,345 sessions",
        "45,678 files touched, 12,345 with",
        "×12,345 · capture.py ×9,876",
        "23,023 of 36,153 decisions and facts",
        "191,000 live · 7,900 degraded · 8,000 orphaned",
        "123,456 nodes · 12,345 files · 1,234 communities",
        "3,456 shortened to fit the budget, 1,234 dropped by it",
        "5,678 lookups",
        "1,234 accepted (1,000 automatically), 1,200 rejected",
    ):
        assert grouped in text, grouped


def test_the_immature_never_shown_line_keeps_its_all_time_marker():
    line = next(ln for ln in _lines(render_text(_immature())) if "230 of 361" in ln)
    assert "all time" in line and "window" not in line


def test_an_empty_journal_says_no_sessions_not_zero_sessions_over_zero_days():
    rep = _states()["all-zero"]
    assert _block(render_text(rep), "ACTIVATION") == ["ACTIVATION  no sessions recorded yet"]


def test_a_long_repo_name_is_clipped_and_the_header_ends_at_column_eighty():
    long = _lines(render_text(_report(repo="r" * 60)))[0]
    assert len(long) == 80 and long.endswith("window: 30 days (13 retained)")
    assert "…" in long and long.startswith("Sidegraph · rrr")
    assert len(_lines(render_text(_report()))[0]) == 80, "the right edge is pinned at 80"


def test_a_single_touched_file_reads_in_the_singular():
    rep = _report(reach=_reach(files_touched=1, files_touched_with_memory=1))
    assert "1 file touched, 1 with memory anchored to it (100%)" in render_text(rep)
    two = _report(reach=_reach(files_touched=2, files_touched_with_memory=1))
    assert "2 files touched, 1 with memory anchored to them (50%)" in render_text(two)
    none = _report(reach=_reach(files_touched=3, files_touched_with_memory=0))
    assert "3 files touched, 0 with memory anchored to them (0%)" in render_text(none)


# --- fix round 1: one golden screen per state --------------------------------------------
# Every word the renderer prints is pinned here at once. The screens were read line by line
# when they were written; a wording change is a deliberate edit of this table, never a
# side effect. `_golden_reports()` builds each input so a case is readable next to its output.


def _golden_reports() -> dict[str, StatsReport]:
    states = _states()
    return {
        "mature": states["mature"],
        "immature": states["immature"],
        "empty-journal": states["all-zero"],
        "telemetry-off": states["telemetry-off"],
        "no-graph": states["no-graph"],
        "empty-graph": states["empty-graph"],
        "unreadable-graph": states["unreadable-graph"],
        "no-render-journal": states["no-render-journal"],
        "large-numbers": states["large-numbers"],
        "all-asks-empty": states["all-asks-empty"],
        "wrapped-and-rounded": states["wrapped-and-rounded"],
        "quiet-and-off": states["quiet-and-off"],
        "nothing-asked": states["nothing-asked"],
    }


GOLDEN = {
    "mature": """\
Sidegraph · demo                                   window: 30 days (13 retained)

ACTIVATION  memory was asked in 24 of 60 sessions
            3 of those got nothing back
            36 sessions touched files without asking
            397 showings (repeats counted), ~19 per session that got any
            3 shortened to fit the budget, 0 dropped by it
            11 lookups included something already tried and abandoned
REACH       259 files touched, 36 with memory anchored to them (14%)
            asked about most, all time: store.py ×12 · capture.py ×9 · cli.py ×6
            silent domains: CLI — no record is bound to it

MEMORY      accepted: 242 decisions · 119 facts · 19 domains
            no recorded showing, all time: 230 of 361 decisions and facts (64%)
            57 more kept as history
            in this window: 19 accepted (17 automatically), 9 rejected
GRAPH       1,842 nodes · 214 files · 31 communities
ANCHORS     191 live · 0 degraded · 8 orphaned → sidegraph-doctor
""",
    "immature": """\
Sidegraph · demo                                   window: 30 days (13 retained)

ACTIVATION  too little to summarize yet — 2 sessions over 13 days
REACH       259 files touched, 36 with memory anchored to them
            asked about most, all time: store.py ×12 · capture.py ×9 · cli.py ×6
            silent domains: CLI — no record is bound to it

MEMORY      accepted: 242 decisions · 119 facts · 19 domains
            no recorded showing, all time: 230 of 361 decisions and facts
            57 more kept as history
            in this window: 19 accepted (17 automatically), 9 rejected
GRAPH       1,842 nodes · 214 files · 31 communities
ANCHORS     191 live · 0 degraded · 8 orphaned → sidegraph-doctor
""",
    "empty-journal": """\
Sidegraph · demo                                    window: 30 days (0 retained)

ACTIVATION  no sessions recorded yet
REACH       no files touched in this window

MEMORY      accepted: 0 decisions · 0 facts · 0 domains
            no history yet
            no accept or reject verdicts in this window
GRAPH       1,842 nodes · 214 files · 31 communities
ANCHORS     none yet
""",
    "telemetry-off": """\
Sidegraph · demo                                                 window: 30 days

ACTIVATION  recording is off (SIDEGRAPH_TELEMETRY=off)
REACH       silent domains: CLI — no record is bound to it

MEMORY      accepted: 242 decisions · 119 facts · 19 domains
            57 more kept as history
            in this window: 19 accepted (17 automatically), 9 rejected
GRAPH       1,842 nodes · 214 files · 31 communities
ANCHORS     191 live · 0 degraded · 8 orphaned → sidegraph-doctor
""",
    "no-graph": """\
Sidegraph · demo                                   window: 30 days (13 retained)

ACTIVATION  memory was asked in 24 of 60 sessions
            3 of those got nothing back
            36 sessions touched files without asking
            397 showings (repeats counted), ~19 per session that got any
            3 shortened to fit the budget, 0 dropped by it
            11 lookups included something already tried and abandoned
REACH       259 files touched, 36 with memory anchored to them (14%)
            asked about most, all time: store.py ×12 · capture.py ×9 · cli.py ×6
            silent domains: CLI — no record is bound to it

MEMORY      accepted: 242 decisions · 119 facts · 19 domains
            no recorded showing, all time: 230 of 361 decisions and facts (64%)
            57 more kept as history
            in this window: 19 accepted (17 automatically), 9 rejected
GRAPH       not built yet → sidegraph-init
ANCHORS     191 live · 0 degraded · 8 orphaned → sidegraph-doctor
""",
    "empty-graph": """\
Sidegraph · demo                                   window: 30 days (13 retained)

ACTIVATION  memory was asked in 24 of 60 sessions
            3 of those got nothing back
            36 sessions touched files without asking
            397 showings (repeats counted), ~19 per session that got any
            3 shortened to fit the budget, 0 dropped by it
            11 lookups included something already tried and abandoned
REACH       259 files touched, 36 with memory anchored to them (14%)
            asked about most, all time: store.py ×12 · capture.py ×9 · cli.py ×6
            silent domains: CLI — no record is bound to it

MEMORY      accepted: 242 decisions · 119 facts · 19 domains
            no recorded showing, all time: 230 of 361 decisions and facts (64%)
            57 more kept as history
            in this window: 19 accepted (17 automatically), 9 rejected
GRAPH       built, but holds no nodes → graphify update . (from the repo root)
ANCHORS     191 live · 0 degraded · 8 orphaned → sidegraph-doctor
""",
    "unreadable-graph": """\
Sidegraph · demo                                   window: 30 days (13 retained)

ACTIVATION  memory was asked in 24 of 60 sessions
            3 of those got nothing back
            36 sessions touched files without asking
            397 showings (repeats counted), ~19 per session that got any
            3 shortened to fit the budget, 0 dropped by it
            11 lookups included something already tried and abandoned
REACH       259 files touched, 36 with memory anchored to them (14%)
            asked about most, all time: store.py ×12 · capture.py ×9 · cli.py ×6
            silent domains: CLI — no record is bound to it

MEMORY      accepted: 242 decisions · 119 facts · 19 domains
            no recorded showing, all time: 230 of 361 decisions and facts (64%)
            57 more kept as history
            in this window: 19 accepted (17 automatically), 9 rejected
GRAPH       found, but could not be read → graphify update .
ANCHORS     191 live · 0 degraded · 8 orphaned → sidegraph-doctor
""",
    "no-render-journal": """\
Sidegraph · demo                                   window: 30 days (13 retained)

ACTIVATION  memory was asked in 24 of 60 sessions
            3 of those got nothing back
            36 sessions touched files without asking
            397 showings (repeats counted), ~19 per session that got any
            budget and tried-and-abandoned counts: not recorded yet
REACH       259 files touched, 36 with memory anchored to them (14%)
            asked about most, all time: store.py ×12 · capture.py ×9 · cli.py ×6
            silent domains: CLI — no record is bound to it

MEMORY      accepted: 242 decisions · 119 facts · 19 domains
            no recorded showing, all time: 230 of 361 decisions and facts (64%)
            57 more kept as history
            in this window: 19 accepted (17 automatically), 9 rejected
GRAPH       1,842 nodes · 214 files · 31 communities
ANCHORS     191 live · 0 degraded · 8 orphaned → sidegraph-doctor
""",
    "large-numbers": """\
Sidegraph · demo                                   window: 30 days (13 retained)

ACTIVATION  memory was asked in 10,234 of 12,345 sessions
            234 of those got nothing back
            2,111 sessions touched files without asking
            456,789 showings (repeats counted), ~46 per session that got any
            3,456 shortened to fit the budget, 1,234 dropped by it
            5,678 lookups included something already tried and abandoned
REACH       45,678 files touched, 12,345 with memory anchored to them (27%)
            asked about most, all time: store.py ×12,345 · capture.py ×9,876
            silent domains: CLI — no record is bound to it

MEMORY      accepted: 30,000 decisions · 6,153 facts · 19 domains
            no recorded showing, all time:
              23,023 of 36,153 decisions and facts (64%)
            12,000 more kept as history
            in this window: 1,234 accepted (1,000 automatically), 1,200 rejected
GRAPH       123,456 nodes · 12,345 files · 1,234 communities
ANCHORS     191,000 live · 7,900 degraded · 8,000 orphaned → sidegraph-doctor
""",
    "all-asks-empty": """\
Sidegraph · demo                                   window: 30 days (13 retained)

ACTIVATION  memory was asked in 24 of 60 sessions
            24 of those got nothing back
            36 sessions touched files without asking
            397 showings (repeats counted)
            3 shortened to fit the budget, 0 dropped by it
            11 lookups included something already tried and abandoned
REACH       259 files touched, 36 with memory anchored to them (14%)
            asked about most, all time: store.py ×12 · capture.py ×9 · cli.py ×6
            silent domains: CLI — no record is bound to it

MEMORY      accepted: 242 decisions · 119 facts · 19 domains
            no recorded showing, all time: 230 of 361 decisions and facts (64%)
            57 more kept as history
            in this window: 19 accepted (17 automatically), 9 rejected
GRAPH       1,842 nodes · 214 files · 31 communities
ANCHORS     191 live · 0 degraded · 8 orphaned → sidegraph-doctor
""",
    "wrapped-and-rounded": """\
Sidegraph · demo                                   window: 30 days (13 retained)

ACTIVATION  memory was asked in 24 of 60 sessions
            3 of those got nothing back
            36 sessions touched files without asking
            397 showings (repeats counted), ~19 per session that got any
            3 shortened to fit the budget, 0 dropped by it
            1 lookup included something already tried and abandoned
REACH       8 files touched, 1 with memory anchored to them (13%)
            asked about most, all time: …r.py::GraphifyReader._to_node ×40 ·
              store.py ×12 · capture.py ×9
            silent domains: Anchoring & Sync, Release & Leak Safety,
              User Documentation, Decision Store, … — no record is bound to them

MEMORY      accepted: 5 decisions · 3 facts · 1 domain
            no recorded showing, all time: 1 of 8 decisions and facts (13%)
            1 more kept as history
            in this window: 1 accepted, 0 rejected
GRAPH       1,842 nodes · 214 files · 31 communities
ANCHORS     191 live · 0 degraded · 8 orphaned → sidegraph-doctor
""",
    "quiet-and-off": """\
Sidegraph · demo                                                 window: 30 days

ACTIVATION  recording is off (SIDEGRAPH_TELEMETRY=off)
REACH       nothing to report yet

MEMORY      accepted: 0 decisions · 0 facts · 0 domains
            no history yet
            no accept or reject verdicts in this window
GRAPH       1,842 nodes · 214 files · 31 communities
ANCHORS     none yet
""",
    "nothing-asked": """\
Sidegraph · demo                                   window: 30 days (13 retained)

ACTIVATION  memory was asked in 0 of 60 sessions
            60 sessions touched files without asking
            0 showings
            budget and tried-and-abandoned counts: not recorded yet
REACH       no files touched in this window
            silent domains: CLI — no record is bound to it

MEMORY      accepted: 242 decisions · 119 facts · 19 domains
            no recorded showing, all time: 230 of 361 decisions and facts (64%)
            57 more kept as history
            in this window: 19 accepted (17 automatically), 9 rejected
GRAPH       1,842 nodes · 214 files · 31 communities
ANCHORS     191 live · 0 degraded · 8 orphaned → sidegraph-doctor
""",
}


@pytest.mark.parametrize("name", list(GOLDEN))
def test_golden_screen(name):
    assert render_text(_golden_reports()[name]) == GOLDEN[name]


def test_the_sweeps_read_every_screen_the_golden_table_pins():
    """`_states()` feeds the 80-column and causal-word sweeps; `GOLDEN` pins screens. A golden
    input that is not also a sweep input is a screen the causal-claim guard never reads."""
    swept = list(_states().values())
    for name, rep in _golden_reports().items():
        assert rep in swept, f"golden screen {name!r} is outside the sweeps in _states()"


def test_the_golden_table_covers_every_state_and_no_line_ends_in_space():
    assert set(GOLDEN) == set(_golden_reports())
    for name, text in GOLDEN.items():
        assert all(ln == ln.rstrip() for ln in text.splitlines()), name


def test_a_multi_year_window_groups_both_header_numbers():
    first = _lines(render_text(_report(window_days=3650, retained_days=1234)))[0]
    assert first.endswith("window: 3,650 days (1,234 retained)")
    assert len(first) == 80


# --- an index behind the canonical files (fix wave, finding 1) ----------------------------


def test_a_stale_index_states_itself_and_names_what_to_run():
    text = render_text(_stale())
    assert _block(text, "MEMORY") == [
        "MEMORY      not shown: the index is behind the store files → sidegraph-init"
    ]
    assert _block(text, "ANCHORS") == ["ANCHORS     not shown: the index is behind the store files"]


def test_a_stale_index_prints_no_number_from_the_record_half():
    text = render_text(_stale())
    for figure in ("242", "119", "361", "230", "191", "silent domains", "anchored to"):
        assert figure not in text, figure
    reach = "\n".join(_block(text, "REACH"))
    assert "%" not in reach, "no ratio over a numerator that was not counted"
    assert "259 files touched" in reach, "the journal half of REACH is still stated"
    assert "memory and domains: not counted" in reach


def test_a_stale_index_still_prints_the_journal_half_of_the_screen():
    text = render_text(_stale())
    assert "memory was asked in 24 of 60 sessions" in text
    assert "asked about most, all time: store.py ×12" in text


def test_a_stale_index_is_stated_in_reach_even_with_recording_off():
    text = render_text(_stale(telemetry_enabled=False))
    assert _block(text, "REACH") == [
        "REACH       memory and domains: not counted (index behind the store files)"
    ]
    assert "nothing to report yet" not in text


def test_a_current_index_prints_none_of_the_staleness_wording():
    assert "behind the store files" not in render_text(_report())


# --- wording that named more than it counted (fix wave, findings 4, 5, 11, 12) ---------------


def test_reach_says_memory_where_the_count_includes_facts():
    """A file bound only to a fact is counted, so the noun cannot be "decision"."""
    rep = _report(reach=_reach(files_touched=1, files_touched_with_memory=1))
    reach = _block(render_text(rep), "REACH")
    assert reach[0] == "REACH       1 file touched, 1 with memory anchored to it (100%)"
    assert "decision" not in "\n".join(reach).lower().replace("silent domains", "")


def test_the_silent_domains_line_says_record_not_decision():
    text = render_text(_report(reach=_reach(silent_domains=["CLI", "Flow Profiles"])))
    assert "silent domains: CLI, Flow Profiles — no record is bound to them" in text
    assert "no decision is bound" not in text


def test_the_header_carries_no_journal_figure_while_recording_is_off():
    first = _lines(render_text(_report(telemetry_enabled=False)))[0]
    assert first.endswith("window: 30 days"), first
    assert "retained" not in first
    assert len(first) == 80, "the right edge stays pinned"


def test_a_narrowed_window_over_an_older_journal_does_not_claim_the_journal_is_empty():
    """`--window 7` over a 20-day-old touch: the window holds nothing, the journal does."""
    text = render_text(_narrowed_window())
    assert _block(text, "ACTIVATION") == ["ACTIVATION  no sessions recorded in this window"]
    assert "recorded yet" not in text
    assert "retained" not in _lines(text)[0], "'(0 retained)' would call the journal empty"
    assert _lines(text)[0].endswith("window: 7 days")


def test_an_empty_journal_keeps_its_zero_retained_and_its_yet():
    """Nothing outside the window either: the journal IS empty, and the old wording is true."""
    text = render_text(_states()["all-zero"])
    assert "(0 retained)" in _lines(text)[0]
    assert "no sessions recorded yet" in text


def test_the_budget_line_says_in_this_window_when_older_rows_exist():
    rep = _report(activation=_act(render_journal=False, outside_window=True))
    text = render_text(rep)
    assert "budget and tried-and-abandoned counts: not recorded in this window" in text
    assert "not recorded yet" not in text
    assert "not recorded yet" in render_text(_report(activation=_act(render_journal=False)))


def test_the_history_line_does_not_name_a_narrower_set_than_it_counts():
    """`historical` also holds deprecated decisions and dropped domains."""
    text = render_text(_report())
    assert "57 more kept as history" in text
    assert "superseded or rejected" not in text
    assert "no history yet" in render_text(_states()["all-zero"])


def test_intent_is_json_only_the_screen_never_prints_it():
    """Spec D3, as amended: a prompt can forget to pass it, so it is not given a text line."""
    rep = _report(activation=_act(self_reported_intents={"check-plan": 3, "explain-why": 1}))
    text = render_text(rep)
    assert "check-plan" not in text and "explain-why" not in text
    assert "intent" not in text.lower()
    assert text == render_text(_report()), "the screen is identical with and without it"


# --- the JSON carries the absences the text does (fix wave 2, finding 4) -----------------
#
# Text withholds a figure whose source is absent. The serialized report is the second renderer
# of the same model (D9), so it must not print a zero where the text says there is nothing to
# read: a consumer has no other way to tell an empty source from a source that was not there.


def _data(rep: StatsReport) -> dict:
    import json

    from sidegraph.stats.render import render_json

    return json.loads(render_json(rep))


def test_json_with_recording_off_carries_no_journal_figure():
    d = _data(_report(telemetry_enabled=False))
    assert d["activation"] is None
    assert d["retained_days"] is None
    assert d["reach"]["files_touched"] is None
    assert d["reach"]["files_touched_with_memory"] is None
    assert d["reach"]["busiest_seeds"] is None
    assert d["memory"]["no_recorded_showing"] is None
    assert d["memory"]["decisions"] == 242, "the store-derived figures still stand"
    assert d["reach"]["silent_domains"] == ["CLI"]


def test_json_without_a_render_journal_carries_no_budget_figure():
    d = _data(_report(activation=_act(render_journal=False, degraded=0, dropped_for_budget=0)))
    a = d["activation"]
    for field in ("degraded", "dropped_for_budget", "renders_with_abandoned"):
        assert a[field] is None, field
    assert a["self_reported_intents"] is None
    assert a["render_journal"] is False
    assert a["sessions_total"] == 60, "the sessions were counted, from a journal that exists"


def test_json_of_a_drill_down_only_window_carries_no_budget_figure_but_keeps_the_abandoned_count():
    d = _data(
        _report(
            activation=_act(
                budget_journal=False, degraded=0, dropped_for_budget=0, renders_with_abandoned=2
            )
        )
    )
    a = d["activation"]
    assert a["degraded"] is None and a["dropped_for_budget"] is None
    assert a["renders_with_abandoned"] == 2
    assert a["self_reported_intents"] == {}, "lookups were recorded, none self-labelled"
    assert a["budget_journal"] is False and a["render_journal"] is True


def test_json_with_a_render_journal_keeps_a_real_zero_as_a_zero():
    d = _data(_report(activation=_act(degraded=0, dropped_for_budget=0, renders_with_abandoned=0)))
    a = d["activation"]
    assert (a["degraded"], a["dropped_for_budget"], a["renders_with_abandoned"]) == (0, 0, 0)
    assert a["self_reported_intents"] == {}, "a journal with no labelled lookup is an empty count"


@pytest.mark.parametrize("graph", [GraphStats(available=False), GraphStats(unreadable=True)])
def test_json_without_a_readable_graph_carries_no_graph_size(graph):
    g = _data(_report(graph=graph))["graph"]
    assert (g["nodes"], g["files"], g["communities"], g["graph_version"]) == (None,) * 4


def test_json_keeps_a_built_graph_that_holds_no_nodes_as_a_measured_zero():
    g = _data(_report(graph=GraphStats(available=True)))["graph"]
    assert (g["nodes"], g["files"], g["communities"]) == (0, 0, 0)


def test_json_drops_retention_where_the_header_does():
    """A window that holds nothing of a journal that is not empty: `(0 retained)` would read as
    an empty journal, so the text drops it, and so must the JSON."""
    d = _data(_narrowed_window())
    assert "retained" not in _lines(render_text(_narrowed_window()))[0]
    assert d["retained_days"] is None
    assert d["activation"]["sessions_total"] == 0, "nothing in the window is a real zero"


def test_json_for_an_empty_journal_keeps_its_zero_retained():
    d = _data(_states()["all-zero"])
    assert d["retained_days"] == 0
    assert d["activation"]["sessions_total"] == 0


def test_json_is_valid_for_every_state_the_text_can_print():
    for name, rep in _states().items():
        assert _data(rep)["repo"] == "demo", name
