"""Render one StatsReport as the screen of text a person reads, or as JSON.

Implements design/superpowers/specs/2026-09-18-usage-stats-design.md, §4 (Output), D1, D2, D7.

The wording is the product surface, and one rule outranks the rest: **this text states what
was shown, asked and touched, and never what was improved, prevented, saved or caused** (D2).
No timeline in the journal can separate "memory sent the agent there" from "the agent was
going anyway", so every sentence below is a count of an event, never a claim about its effect.
A banned-substring test runs over every state this module can print.

Three honesty rules shape the sentences that are not obvious from the layout:

* **Windowed vs cumulative.** The header says "window: N days", but the busiest-seeds and
  no-recorded-showing figures come from cumulative counter tables that the window does not cut.
  Those lines say "all time" so the header cannot be read as covering them.
* **One denominator per line.** The inventory counts accepted records only. Superseded and
  rejected records are the append-only history and get their own line, worded as *more*, so
  the two never read as the same population.
* **A stopped or immature recorder prints no number it cannot stand behind.** Below the
  maturity floor no percentage appears anywhere; with recording off, nothing derived from the
  journal appears at all (D7). Store-derived lines (inventory, history, funnel, silent
  domains) are unaffected by either state because they never came from the journal.
* **An index behind the canonical files prints no record-derived number either.** The model
  hands over ``None`` for those figures (see ``stats/model.py``), and each block says so in
  place of the number. The journal-derived lines are unaffected: the journals are live.

``render_json`` is the second renderer of the same report (D9) and states the same absences:
where the text withholds a figure, the JSON carries ``null`` for it, never a zero.
"""

from __future__ import annotations

import json
from typing import Any

from sidegraph.stats.model import ActivationStats, StatsReport

_LABEL_WIDTH = 12
_SCREEN_WIDTH = 80
_BODY_WIDTH = _SCREEN_WIDTH - _LABEL_WIDTH
_HANG = "  "  # continuation indent for a list that has to wrap
_SEED_CLIP = 30
# `build_report` cuts the silent-domain list at four names and reports no total, so a list of
# exactly this length may be a list that was cut. The renderer marks it rather than claiming
# the names are complete. Mirrors `silent[:4]` in model.py — keep the two in step.
_SILENT_CAP = 4
# What the three blocks that lose figures say when the index is behind the canonical files.
# The command is `sidegraph-init` because it opens a Store, which is what refreshes the index
# (the same next step `sidegraph-stats` names for an index that is missing altogether).
_BEHIND = "the index is behind the store files"
_NONE_YET = "no sessions recorded yet"


def _n(n: int) -> str:
    """Every number on the screen goes through here, so `12,345` never sits beside `10234`."""
    return f"{n:,}"


def _count(n: int, noun: str, plural: str | None = None) -> str:
    """`1 session`, `36 sessions`."""
    return f"{_n(n)} {noun if n == 1 else plural or noun + 's'}"


def _pct(part: int, whole: int) -> int:
    """Integer percentage, halves rounded up (banker's rounding would print 12% for 12.5%)."""
    return (200 * part + whole) // (2 * whole)


def _clip(name: str) -> str:
    """Keep the tail of a long seed: in a path or a dotted symbol the end is what identifies."""
    return name if len(name) <= _SEED_CLIP else "…" + name[-(_SEED_CLIP - 1) :]


def _pack(first: str, items: list[str], sep: str) -> list[str]:
    """Lay `items` out after `first`, wrapping between items and never inside one."""
    lines: list[str] = []
    current = first
    started = False
    for item in items:
        if started and len(current) + len(sep) + len(item) > _BODY_WIDTH:
            lines.append(current + sep.rstrip())
            current = _HANG + item
        else:
            current += (sep if started else "") + item
        started = True
    lines.append(current)
    return lines


def _block(label: str, lines: list[str]) -> list[str]:
    pad = " " * _LABEL_WIDTH
    return [(label.ljust(_LABEL_WIDTH) if i == 0 else pad) + ln for i, ln in enumerate(lines)]


def _retention_shown(report: StatsReport) -> bool:
    """Whether the retention figure is a fact about the journal. It comes from the journal, so
    it stands only where the journal is being read AND is not contradicted: with recording off
    it would sit above "recording is off", and a window that holds nothing of an older journal
    would read "(0 retained)" for a journal that is not empty. A journal that really is empty
    keeps its zero. Shared by the header and the JSON, which must agree."""
    a = report.activation
    return report.telemetry_enabled and not (a.sessions_total == 0 and a.outside_window)


def _header(report: StatsReport) -> str:
    prefix = "Sidegraph · "
    right = f"window: {_count(report.window_days, 'day')}"
    if _retention_shown(report):
        right += f" ({_n(report.retained_days)} retained)"
    room = _SCREEN_WIDTH - len(prefix) - len(right) - 2
    repo = report.repo if len(report.repo) <= room else report.repo[: room - 1] + "…"
    left = prefix + repo
    return left + " " * (_SCREEN_WIDTH - len(left) - len(right)) + right


def _activation(report: StatsReport) -> list[str]:
    a = report.activation
    if not report.telemetry_enabled:
        return ["recording is off (SIDEGRAPH_TELEMETRY=off)"]
    if not a.mature:
        if a.sessions_total == 0:
            return ["no sessions recorded in this window" if a.outside_window else _NONE_YET]
        return [
            "too little to summarize yet — "
            f"{_count(a.sessions_total, 'session')} over {_count(report.retained_days, 'day')}"
        ]
    lines = [
        f"memory was asked in {_n(a.sessions_with_retrieval)} of "
        f"{_count(a.sessions_total, 'session')}"
    ]
    # Asked-and-got-nothing is a different failure from not asking (a query that matched
    # nothing), so it is never folded into the silent count below it.
    if a.sessions_with_retrieval:
        if a.sessions_asked_but_empty:
            lines.append(f"{_n(a.sessions_asked_but_empty)} of those got nothing back")
        else:
            lines.append("every one of those got records back")
    lines.append(f"{_count(a.sessions_touch_only, 'session')} touched files without asking")
    fed = a.sessions_with_retrieval - a.sessions_asked_but_empty
    # The unit is a showing: one record, a decision or a fact, placed in one lookup's result.
    # The same record in several sessions is several showings, while the MEMORY block counts
    # distinct records, so the noun "records" here would invite dividing one figure by the
    # other. The line says which it is, because only the screen reaches the reader (the skill
    # forbids supplementing it).
    shown = _count(a.showings, "showing")
    if a.showings > 1:
        shown += " (repeats counted)"
    if fed > 0:
        # Averaged over the sessions that received records: a session that got nothing has
        # no records to average, and counting it would understate what a session gets.
        per_session = (2 * a.showings + fed) // (2 * fed)
        lines.append(f"{shown}, ~{_n(per_session)} per session that got any")
    else:
        lines.append(shown)
    if not a.render_journal:
        # No render row in the window — the table is missing or empty. The three counts below
        # are zero because nothing was recorded, so none of them is printed (D7). Deliberately
        # no advice on why: an old index and a server that predates the writer both land here,
        # and nothing in the report can tell them apart.
        when = "in this window" if a.outside_window else "yet"
        lines.append(f"budget and tried-and-abandoned counts: not recorded {when}")
        return lines
    if not a.budget_journal:
        # Rows exist, all of them drill-downs: they delivered records and applied no budget,
        # so the two budget counts are unrecorded while the abandoned count is real.
        when = "in this window" if a.outside_window else "yet"
        lines.append(f"budget counts: not recorded {when}")
        lines.append(_abandoned(a))
        return lines
    lines.append(
        f"{_n(a.degraded)} shortened to fit the budget, {_n(a.dropped_for_budget)} dropped by it"
    )
    lines.append(_abandoned(a))
    return lines


def _abandoned(a: ActivationStats) -> str:
    lookups = "lookup" if a.renders_with_abandoned == 1 else "lookups"
    return (
        f"{_n(a.renders_with_abandoned)} {lookups} included something already tried and abandoned"
    )


def _reach(report: StatsReport) -> list[str]:
    r = report.reach
    ratios = report.activation.mature and report.telemetry_enabled
    lines: list[str] = []
    if report.telemetry_enabled:
        if r.files_touched == 0:
            lines.append("no files touched in this window")
        elif r.files_touched_with_memory is None:
            # Counted from the touch journal, so still true; the memory half is not counted.
            lines.append(f"{_count(r.files_touched, 'file')} touched")
        else:
            pronoun = "it" if r.files_touched == 1 else "them"
            line = (
                f"{_count(r.files_touched, 'file')} touched, "
                f"{_n(r.files_touched_with_memory)} with memory anchored to {pronoun}"
            )
            if ratios:
                line += f" ({_pct(r.files_touched_with_memory, r.files_touched)}%)"
            lines.append(line)
        if r.busiest_seeds:
            # `retrieval_seeds` is a cumulative counter, not a windowed journal (see module doc).
            items = [f"{_clip(seed)} ×{_n(n)}" for seed, n in r.busiest_seeds]
            lines += _pack("asked about most, all time: ", items, " · ")
    if r.silent_domains is None:
        lines.append("memory and domains: not counted (index behind the store files)")
    elif r.silent_domains:
        # Tier-1 membership only: nothing binds to the domain's paired entity. Narrower than
        # "holds no decision", and the sentence says exactly that much.
        items = list(r.silent_domains) + (["…"] if len(r.silent_domains) >= _SILENT_CAP else [])
        bound_to = "it" if len(r.silent_domains) == 1 else "them"
        items[-1] += f" — no record is bound to {bound_to}"
        lines += _pack("silent domains: ", items, ", ")
    return lines or ["nothing to report yet"]


def _memory(report: StatsReport) -> list[str]:
    m = report.memory
    if m is None:
        return [f"not shown: {_BEHIND} → sidegraph-init"]
    ratios = report.activation.mature and report.telemetry_enabled
    lines = [
        f"accepted: {_count(m.decisions, 'decision')} · {_count(m.facts, 'fact')} "
        f"· {_count(m.domains, 'domain')}"
    ]
    # Journal-derived, cumulative: gone when recording is off, and without its percentage while
    # the window is too young for a ratio. The label says what the counter holds: no record of a
    # showing. It never says "never shown": a showing while recording was off, or one lost to a
    # writer error the server swallows, is in no counter, so the absence is not a measurement.
    if report.telemetry_enabled and m.surfaceable:
        head = "no recorded showing, all time: "
        tail = f"{_n(m.no_recorded_showing)} of {_n(m.surfaceable)} decisions and facts"
        if ratios:
            tail += f" ({_pct(m.no_recorded_showing, m.surfaceable)}%)"
        # A store with thousands of records makes this longer than the body is wide; the
        # figures then hang under the label rather than run past the screen edge.
        lines += [head + tail] if len(head + tail) <= _BODY_WIDTH else [head.rstrip(), _HANG + tail]
    if m.historical:
        lines.append(f"{_n(m.historical)} more kept as history")
    else:
        lines.append("no history yet")
    if m.accepted_in_window or m.rejected_in_window:
        auto = (
            f" ({_n(m.auto_accepted_in_window)} automatically)" if m.auto_accepted_in_window else ""
        )
        verdicts = f"{_n(m.accepted_in_window)} accepted{auto}, {_n(m.rejected_in_window)} rejected"
        lines.append(f"in this window: {verdicts}")
    else:
        lines.append("no accept or reject verdicts in this window")
    return lines


def _graph(report: StatsReport) -> list[str]:
    g = report.graph
    if g.unreadable:
        # `sidegraph-init` only tests that the file exists, so it would call this one found.
        return ["found, but could not be read → graphify update ."]
    if not g.available:
        return ["not built yet → sidegraph-init"]
    if g.nodes == 0:
        # Built and readable but empty: three zeroes would read as a measurement. The usual
        # cause is an engine run from the wrong directory (doctor's graph-root advisory).
        return ["built, but holds no nodes → graphify update . (from the repo root)"]
    return [
        f"{_count(g.nodes, 'node')} · {_count(g.files, 'file')} "
        f"· {_count(g.communities, 'community', 'communities')}"
    ]


def _anchors(report: StatsReport) -> list[str]:
    a = report.anchors
    if a is None:
        return [f"not shown: {_BEHIND}"]
    if a.live + a.degraded + a.orphaned == 0:
        return ["none yet"]
    line = f"{_n(a.live)} live · {_n(a.degraded)} degraded · {_n(a.orphaned)} orphaned"
    if a.degraded + a.orphaned:
        line += " → sidegraph-doctor"
    return [line]


def render_text(report: StatsReport) -> str:
    """The one screen a person reads, newline-terminated so a caller writes it as-is.

    See design/superpowers/specs/2026-09-18-usage-stats-design.md, §4 (Output). Pure: no I/O,
    no clock, no store — everything printed is a field of `report` (D9).
    """
    lines = [_header(report), ""]
    lines += _block("ACTIVATION", _activation(report))
    lines += _block("REACH", _reach(report))
    lines.append("")
    lines += _block("MEMORY", _memory(report))
    lines += _block("GRAPH", _graph(report))
    lines += _block("ANCHORS", _anchors(report))
    return "\n".join(lines) + "\n"


def render_json(report: StatsReport) -> str:
    """The same report as data, newline-terminated, carrying the absences the text does.

    Text withholds a figure whose source is absent: recording off, no render journal, no
    readable graph, a window that holds nothing of an older journal. The model keeps those
    figures as zeros (its own consumers, the renderers, gate on the flags beside them), so this
    renderer turns each into ``null`` under the same predicates. A consumer then never reads a
    zero where the text reader is told there is nothing to read. A zero that was measured (an
    empty journal, a built graph with no nodes, a render journal whose budget counts are zero)
    stays a zero. A block the model already hands over as ``None`` (an index behind the store
    files) passes through unchanged.

    See design/superpowers/specs/2026-09-18-usage-stats-design.md, D9. Pure, like ``render_text``.
    """
    data: dict[str, Any] = report.model_dump(mode="json")
    if not report.telemetry_enabled:
        # Everything the journal supplied. Store-derived figures stand.
        data["activation"] = None
        for key in ("files_touched", "files_touched_with_memory", "busiest_seeds"):
            data["reach"][key] = None
        if data["memory"] is not None:
            data["memory"]["no_recorded_showing"] = None
    elif not report.activation.render_journal:
        # No render row in the window: three counts and the intent tally are unrecorded.
        for key in ("degraded", "dropped_for_budget", "renders_with_abandoned"):
            data["activation"][key] = None
        data["activation"]["self_reported_intents"] = None
    elif not report.activation.budget_journal:
        # Only drill-downs: they applied no budget, so its two counts are unrecorded. The
        # abandoned count and the intent tally describe rows that exist and stand.
        for key in ("degraded", "dropped_for_budget"):
            data["activation"][key] = None
    if not _retention_shown(report):
        data["retained_days"] = None
    if not report.graph.available:
        for key in ("nodes", "files", "communities"):
            data["graph"][key] = None
    return json.dumps(data, indent=2) + "\n"
