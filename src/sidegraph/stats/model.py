"""Compute one StatsReport from the store's index, its journals and the graph.

Implements design/superpowers/specs/2026-09-18-usage-stats-design.md, §4 (Aggregator).

Read-only by construction (spec D8), which here means two rules, not one:

* ``index.db`` is opened ``mode=ro`` and never with SQLite's lock-disabling URI flag (the
  one D8 names) — the journals make it a live write target and this command runs
  mid-session, where that flag would switch off locking on a file another process is
  writing.
* **No ``Store`` is ever constructed.** ``Store.__init__`` calls ``_refresh_freshness``,
  which rebuilds the index from canonical files whenever the digest is stale. Opening a
  store to "just read" it would rewrite the index after every git pull. Records are read as
  JSON off the same read-only connection instead.

Not opening a ``Store`` also means not running its freshness check, so a ``git pull`` that
changes canonical records leaves the index describing the old ones. The report therefore
*detects* that read-only, by comparing the index's own ``canonical_stat`` table against the
files on disk, and states it: every figure derived from the record tables comes back
``None`` rather than a number that was true before the pull. The journals are written live
and are never behind, so the activation figures stand.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import BaseModel, ValidationError

from sidegraph.schema import (
    AnchorBinding,
    Decision,
    DecisionStatus,
    Domain,
    DomainStatus,
    Entity,
    Fact,
)

# Below either floor, no ratio is computed anywhere in the report: a fresh install rendering
# "0 of 0 sessions (0%)" reads as a broken product (D7). The floor is the one uncalibrated
# number in the spec — see its §8 before changing it.
MIN_SESSIONS = 5
MIN_DAYS = 3

# Statuses after which append-only rules guarantee a record never changes again. Mirrors
# `store._TERMINAL_DECISION_STATUSES` / `_TERMINAL_DOMAIN_STATUSES`, which are private there,
# so mirrored rather than imported: this module reaches into the store for nothing (D8). A
# domain has no `rejected`/`deprecated`; `dropped` is its rejected analogue.
_TERMINAL_RECORD_STATUSES = frozenset(
    {DecisionStatus.SUPERSEDED, DecisionStatus.REJECTED, DecisionStatus.DEPRECATED}
)
_TERMINAL_DOMAIN_STATUSES = frozenset({DomainStatus.SUPERSEDED, DomainStatus.DROPPED})

# The files `canonical_stat` has a row for. Mirrors `store._CANONICAL_SUBDIRS` and
# `store._ARCHIVE_SUBDIR` (private there, mirrored here for the same reason as the statuses
# above); a test pins the two together, because a subdirectory missing from this walk is a
# pull the staleness check cannot see.
_CANONICAL_SUBDIRS = ("decisions", "facts", "domains", "entities", "bindings", "initiatives")
_ARCHIVE_SUBDIR = "archive"


class ActivationStats(BaseModel):
    sessions_total: int
    sessions_with_retrieval: int
    sessions_asked_but_empty: int
    sessions_touch_only: int
    # One record, a decision or a fact, placed in one lookup's result, so one record shown in six
    # sessions is six here. `MemoryStats` counts distinct records; the two are different units
    # and are never a numerator and denominator.
    showings: int
    degraded: int
    dropped_for_budget: int
    renders_with_abandoned: int
    mature: bool
    # False when `render_events` holds no row in the window: no table (an index from before the
    # journal existed) or a table nothing has written to yet (upgraded, but the server that
    # writes it has not run). `degraded` / `dropped_for_budget` / `renders_with_abandoned` are
    # then zero because nothing was recorded, not because nothing happened — the renderer must
    # not print them. With rows present a zero is a finding and stays a number.
    render_journal: bool = True
    # False when no row in the window came from a lookup that applied a budget: `drill_down`
    # writes a render row (its records are showings) and applies none, so a window of only those
    # has `render_journal` true and nothing recorded about a budget. `degraded` and
    # `dropped_for_budget` are then zero for want of a budget, and the renderers withhold them
    # exactly as they do for a missing journal; `renders_with_abandoned` still stands.
    budget_journal: bool = True
    # True when either journal holds a row OLDER than the window. Only a `--window` narrower
    # than the journal's retention can set it. It is what lets the renderer tell "nothing has
    # been recorded" from "nothing was recorded in this window": the same empty counts, and a
    # false absence when the older rows are ignored.
    outside_window: bool = False
    # Renders in the window by the label the caller passed as `intent`, most used first. In
    # the JSON only, by design (spec D3): a prompt can always forget to pass one, so the label
    # is self-reported and the count is not a census of who asked. A render that carried none
    # is not counted here at all.
    self_reported_intents: dict[str, int] = {}


class ReachStats(BaseModel):
    """``files_touched_with_memory`` and ``silent_domains`` come from the record tables, so
    they are ``None`` (not zero, not empty) when the index is behind the canonical files."""

    files_touched: int
    files_touched_with_memory: int | None
    busiest_seeds: list[tuple[str, int]]
    silent_domains: list[str] | None


class MemoryStats(BaseModel):
    decisions: int
    facts: int
    domains: int
    historical: int
    # Accepted decisions and facts with no showing in the cumulative counter. An absence of a
    # record, not a measurement that the record was never shown: a showing while recording
    # was off, or one lost to a swallowed writer error, is not in the counter.
    no_recorded_showing: int
    surfaceable: int
    accepted_in_window: int
    rejected_in_window: int
    auto_accepted_in_window: int


class GraphStats(BaseModel):
    """``available`` and ``unreadable`` are never both true. Neither true means there is no
    graph file at all (never built); ``unreadable`` means a file is there and could not be
    parsed — a different next step, because ``sidegraph-init`` only tests that the file exists.
    """

    available: bool = False
    unreadable: bool = False
    nodes: int = 0
    files: int = 0
    communities: int = 0
    graph_version: str | None = None


class AnchorStats(BaseModel):
    live: int = 0
    degraded: int = 0
    orphaned: int = 0


class StatsReport(BaseModel):
    repo: str
    window_days: int
    retained_days: int
    telemetry_enabled: bool
    activation: ActivationStats
    reach: ReachStats
    # True when the canonical files on disk differ from what the index loaded (a `git pull`, a
    # hand edit). `memory` and `anchors` are then None: they are derived from the record
    # tables, which describe the files as they were. Never a guess at what changed.
    index_stale: bool = False
    memory: MemoryStats | None
    graph: GraphStats
    anchors: AnchorStats | None


class UnreadableRecordError(ValueError):
    """A record row whose ``data`` column does not parse as its model. Carries the table and
    the record's id so a caller can name what is unreadable: the alternative is a bare
    pydantic error that names neither, from a command whose contract is exit 2 with a
    message."""

    def __init__(self, table: str, record_id: str, reason: str) -> None:
        self.table = table
        self.record_id = record_id
        super().__init__(f"{table} record {record_id} does not parse: {reason}")


def _rows[M: BaseModel](
    conn: sqlite3.Connection, table: str, model: type[M], id_expr: str
) -> list[M]:
    """Parse a record table's JSON column. The index stores each record's canonical JSON in
    `data`, which is what `Store` itself reloads from — so this reads the same truth without
    opening a store. ``id_expr`` is the SQL that names a row in that table (the key column,
    or a binding's two), used only to say which record could not be read."""
    out: list[M] = []
    for row in conn.execute(f"SELECT {id_expr} AS rid, data FROM {table}"):
        try:
            out.append(model.model_validate_json(row["data"]))
        except ValidationError as e:
            # The first error line only: pydantic's full rendering can quote the whole record.
            raise UnreadableRecordError(table, row["rid"], str(e).splitlines()[0]) from e
    return out


def _canonical_files(store_dir: Path) -> dict[tuple[str, str], tuple[int, int]]:
    """``(subdir, stem) -> (size, mtime_ns)`` for every canonical file on disk, keyed and
    valued exactly as the store's ``canonical_stat`` table is (``.json`` in each canonical
    subdirectory, ``.jsonl`` segments under ``archive/``). Read-only: ``stat`` and
    ``iterdir`` only. The store's tmp files (``<name>.<random>.tmp``) fall out by suffix."""
    found: dict[tuple[str, str], tuple[int, int]] = {}
    for sub, suffix in [(s, ".json") for s in _CANONICAL_SUBDIRS] + [(_ARCHIVE_SUBDIR, ".jsonl")]:
        directory = store_dir / sub
        if not directory.is_dir():
            continue
        for f in directory.iterdir():
            if f.suffix != suffix:
                continue
            try:
                st = f.stat()
            except FileNotFoundError:  # removed between the listing and the stat
                continue
            found[(sub, f.stem)] = (st.st_size, st.st_mtime_ns)
    return found


def _index_is_stale(conn: sqlite3.Connection, store_dir: Path) -> bool:
    """True when the index did not load the canonical files in the state they are in now.

    Compares ``canonical_stat`` (what the index recorded for each file when it loaded it)
    against the disk, in both directions: a file that differs or has no row, and a row whose
    file is gone (a pull that deleted a record). Unlike the store's own write-time guard,
    which is deliberately one-directional, this asks the reader's question — "does this
    index still describe the files?" — and a row with no file means it does not: compaction
    and every store write delete their own rows. The digest is not re-derived (spec D8: no
    second implementation of it); ``canonical_stat`` is the store's own per-file record.

    An index with no ``canonical_stat`` table at all predates the mechanism and has loaded
    nothing it can vouch for, so it is stale whenever a canonical file exists.
    """
    has_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'canonical_stat'"
    ).fetchone()
    recorded: dict[tuple[str, str], tuple[int, int]] = {}
    if has_table:
        recorded = {
            (r["subdir"], r["stem"]): (r["size"], r["mtime_ns"])
            for r in conn.execute("SELECT subdir, stem, size, mtime_ns FROM canonical_stat")
        }
    return recorded != _canonical_files(store_dir)


# The `intent` a `drill_down` render row is written under (server.DRILL_DOWN_INTENT; this module
# imports nothing from the server, and a test pins the two together). Such a row says how many
# records a drill-down delivered and nothing about a budget, because a drill-down applies none.
_DRILL_DOWN_INTENT = "drill_down"


def _is_drill_down(row: sqlite3.Row) -> bool:
    return (row["intent"] or "").strip() == _DRILL_DOWN_INTENT


def _intent_counts(renders: Sequence[sqlite3.Row]) -> dict[str, int]:
    """Count render rows per non-empty intent label, most used first (ties by name)."""
    counts: dict[str, int] = {}
    for r in renders:
        label = (r["intent"] or "").strip()
        if label:
            counts[label] = counts.get(label, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def _empty_bucket() -> dict[str, set[str]]:
    return {"seed": set(), "show": set(), "touch": set()}


def build_report(
    store_dir: Path,
    graph_path: Path | None,
    *,
    window_days: int = 30,
    now: datetime | None = None,
) -> StatsReport:
    """One typed report over the index, its journals and the graph.

    ``now`` is injectable so tests can freeze the clock; production passes None.
    See design/superpowers/specs/2026-09-18-usage-stats-design.md, §4 and D8.
    """
    from sidegraph.config import telemetry_enabled

    now = now or datetime.now(UTC)
    cutoff_dt = now - timedelta(days=window_days)
    cutoff = cutoff_dt.isoformat()
    # Resolved so a relative `.sidegraph` still yields its repo's name: the default store path
    # is relative, and `Path('.sidegraph').parent.name` is "".
    store_dir = Path(store_dir).resolve()

    # `isolation_level=None` because Python's sqlite3 opens no transaction for a SELECT: each
    # query would be its own read, and a writer landing between two of them (this runs
    # mid-session, which is when one is writing) makes a report that matches neither the state
    # before it nor the one after. One explicit read transaction on this one connection pins a
    # single snapshot for every figure below. Still `mode=ro`; no retry, no lock of our own.
    with closing(
        sqlite3.connect(f"file:{store_dir / 'index.db'}?mode=ro", uri=True, isolation_level=None)
    ) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")

        sessions: dict[str, dict[str, set[str]]] = {}
        for row in conn.execute(
            "SELECT session_id, kind, key, detail FROM retrieval_events WHERE at >= ?",
            (cutoff,),
        ):
            b = sessions.setdefault(row["session_id"], _empty_bucket())
            if row["kind"] == "touch":
                b["touch"].add(row["key"])
            elif row["kind"] == "seed":
                b["seed"].add(row["key"])
            elif row["detail"]:
                # A show row is keyed by anchor PATH with the record id in `detail`; count
                # records, or eight records anchored to one file collapse into one.
                b["show"].add(row["detail"])

        # An index created before this feature has no `render_events` table (the store adds it on
        # its next open, so the state ends at the next session start). Only that table
        # degrades: a missing `retrieval_events` is an unrecognised store and still raises.
        has_render_table = (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'render_events'"
            ).fetchone()
            is not None
        )
        renders = (
            list(conn.execute("SELECT * FROM render_events WHERE at >= ?", (cutoff,)))
            if has_render_table
            else []
        )
        # A render row is proof that a lookup ran, and it can be the ONLY row a
        # session leaves: a name-only entity call gives `_record` no journal seeds and
        # nothing shown, so `record_retrieval_events` writes nothing while the render event
        # is still written (server.py). Without this fold that session is missing from the
        # denominator while its budget drops are still summed, and one that also touched a
        # file is reported as silence.
        rendered = {r["session_id"] for r in renders}
        for sid in rendered:
            sessions.setdefault(sid, _empty_bucket())
        # Retention spans both journals for the same reason: a store whose sessions are
        # render-only would otherwise never clear the day floor.
        first_at = conn.execute(
            "SELECT min(at) AS a FROM ("
            "SELECT at FROM retrieval_events WHERE at >= ?"
            + (" UNION ALL SELECT at FROM render_events WHERE at >= ?" if has_render_table else "")
            + ")",
            (cutoff, cutoff) if has_render_table else (cutoff,),
        ).fetchone()["a"]
        outside_window = (
            conn.execute(
                "SELECT 1 FROM retrieval_events WHERE at < ? LIMIT 1", (cutoff,)
            ).fetchone()
            is not None
            or has_render_table
            and conn.execute(
                "SELECT 1 FROM render_events WHERE at < ? LIMIT 1", (cutoff,)
            ).fetchone()
            is not None
        )
        shows_by_record = {
            row["record_id"]: row["shows"]
            for row in conn.execute("SELECT record_id, shows FROM retrieval_shows")
        }
        seed_counts = [
            (r["seed"], r["queries"])
            for r in conn.execute(
                "SELECT seed, queries FROM retrieval_seeds ORDER BY queries DESC, seed LIMIT 3"
            )
        ]

        # The record tables are only worth reading while they still describe the files. When
        # the index is behind, nothing derived from them is computed at all: an omission is
        # stated by the renderer, a stale number would read as current.
        index_stale = _index_is_stale(conn, store_dir)
        decisions: list[Decision] = []
        facts: list[Fact] = []
        domains: list[Domain] = []
        entities: list[Entity] = []
        bindings: list[AnchorBinding] = []
        if not index_stale:
            decisions = _rows(conn, "decisions", Decision, "id")
            facts = _rows(conn, "facts", Fact, "id")
            domains = _rows(conn, "domains", Domain, "domain_id")
            entities = _rows(conn, "entities", Entity, "entity_id")
            bindings = _rows(
                conn, "anchor_bindings", AnchorBinding, "record_id || ' -> ' || entity_id"
            )
        # Called while the connection is still open, on rows already fetched: a reopen here
        # would be a second read path, and a `Store` a write path (D8).
        anchors = None if index_stale else _anchor_stats(bindings)

    retained_days = max((now - datetime.fromisoformat(first_at)).days, 0) if first_at else 0
    # A session "asked" when the journals hold a seed, a show OR a render row for it: a seed
    # row is a query; a show row with no seed row is a drill_down of an older server, whose
    # `domain:<slug>` seeds server._record drops from the journal; a render row alone is a call
    # that matched nothing. Asked-and-fed vs asked-and-empty are two different states
    # ("nothing matched" is its own failure mode) and neither is silence.
    asking = {s for s, b in sessions.items() if b["seed"] or b["show"] or s in rendered}
    # A session was delivered records when it has a show row OR a render row that placed a
    # record. The show journal is keyed by anchor PATH, so a record bound to an entity with no
    # file (an abstract or global anchor) writes no show row at all; the render journal is
    # written regardless of anchors and `emitted` counts every record it placed, decisions and
    # facts alike, so it is what says what reached the session. Show rows keep their own job,
    # the join against touched files, which is why they stay keyed by path.
    emitted: dict[str, int] = {}
    for r in renders:
        emitted[r["session_id"]] = emitted.get(r["session_id"], 0) + r["emitted"]
    fed = {s for s in asking if sessions[s]["show"] or emitted.get(s, 0) > 0}

    def showings_of(session_id: str) -> int:
        """The render journal's count when the session has a render row, whatever it says: a
        session whose render placed one anchored and one global decision has one show row and
        two showings, and the show rows undercount it. Only a session with no render row (it
        predates the journal) is counted from its show rows, one per distinct record. A
        `drill_down` writes a render row of its own, so it counts here like any lookup."""
        if session_id in rendered:
            return emitted[session_id]
        return len(sessions[session_id]["show"])

    # Rows a budget ran for. A drill-down row is a delivery with no budget behind it, so its
    # zeros are not "the budget clipped nothing": they stay out of every budget figure, and a
    # window holding only drill-downs has no budget recorded at all.
    budgeted = [r for r in renders if not _is_drill_down(r)]
    activation = ActivationStats(
        sessions_total=len(sessions),
        sessions_with_retrieval=len(asking),
        sessions_asked_but_empty=len(asking - fed),
        sessions_touch_only=len(sessions) - len(asking),
        showings=sum(showings_of(sid) for sid in sessions),
        degraded=sum(r["degraded"] for r in budgeted),
        dropped_for_budget=sum(r["dropped_for_budget"] for r in budgeted),
        # A superseded record surfacing is "something already tried and abandoned" exactly as
        # a record with a non-empty `rejected` is, so either flag makes the render count once.
        # Counted over EVERY render row: a drill-down delivered records too, and these two flags
        # describe what was delivered, not what a budget did.
        renders_with_abandoned=sum(1 for r in renders if r["had_rejected"] or r["had_superseded"]),
        mature=len(sessions) >= MIN_SESSIONS and retained_days >= MIN_DAYS,
        render_journal=bool(renders),
        budget_journal=bool(budgeted),
        outside_window=outside_window,
        self_reported_intents=_intent_counts(budgeted),
    )

    bound_entity_ids = {b.entity_id for b in bindings}
    touched = {p for b in sessions.values() for p in b["touch"]}
    anchored_paths = {
        e.descriptor.file_path
        for e in entities
        if e.entity_id in bound_entity_ids and e.descriptor and e.descriptor.file_path
    }
    # A domain is "silent" when nothing binds to its paired abstract entity
    # (`domain:<slug>`, minted at acceptance — schema.py, `Domain` docstring). This is Tier-1
    # membership only, deliberately narrower than `retrieval._domain_decisions`, which needs
    # a graph reader; the renderer's wording matches what is actually measured.
    entity_by_name = {e.canonical_name: e.entity_id for e in entities}
    silent = sorted(
        d.title
        for d in domains
        if d.status == DomainStatus.ACCEPTED
        and entity_by_name.get(f"domain:{d.slug}") not in bound_entity_ids
    )

    return StatsReport(
        repo=store_dir.parent.name,
        window_days=window_days,
        retained_days=retained_days,
        telemetry_enabled=telemetry_enabled(),
        activation=activation,
        reach=ReachStats(
            files_touched=len(touched),
            files_touched_with_memory=None if index_stale else len(touched & anchored_paths),
            busiest_seeds=seed_counts,
            silent_domains=None if index_stale else silent[:4],
        ),
        index_stale=index_stale,
        memory=(
            None
            if index_stale
            else _memory_stats(decisions, facts, domains, shows_by_record, cutoff_dt)
        ),
        graph=_graph_stats(graph_path),
        anchors=anchors,
    )


def _graph_stats(graph_path: Path | None) -> GraphStats:
    """Graph metrics, through GraphifyReader and nothing else (CLAUDE.md, engine seam).

    Implements design/superpowers/specs/2026-09-18-usage-stats-design.md, §4 (graph half).

    Deliberately limited to the reader's public surface: nodes, distinct source files,
    communities, version. No edge count — that would mean reaching past the surface for a
    number the report does not use. How many areas are NAMED is a store fact (accepted
    Domain records), never ``community_labels``, which is the engine's own sidecar.
    A missing graph is ``GraphStats()``; one that is there but cannot be parsed is
    ``unreadable=True`` — kept apart because their next steps differ. Never zeroes, never a
    raise.
    """
    if graph_path is None or not Path(graph_path).exists():
        return GraphStats()
    from sidegraph.engine.reader import GraphifyReader

    try:
        reader = GraphifyReader(graph_path)
        nodes = reader.list_nodes()
        return GraphStats(
            available=True,
            nodes=len(nodes),
            files=len({n.file_path for n in nodes if n.file_path}),
            communities=len(reader.communities()),
            graph_version=reader.graph_version(),
        )
    except Exception:
        # Any failure here — truncated JSON, valid JSON of the wrong shape, a permission error
        # — is the same state for the reader of the report: a file exists and gave nothing.
        return GraphStats(unreadable=True)


def _memory_stats(
    decisions: Sequence[Decision],
    facts: Sequence[Fact],
    domains: Sequence[Domain],
    shows_by_record: dict[str, int],
    cutoff: datetime,
) -> MemoryStats:
    """Inventory and the ratification funnel, both computed from the records themselves
    (spec D6: ``drop()`` persists status ``rejected`` append-only, so no writer is needed).

    ``decisions`` / ``facts`` / ``domains`` count **accepted** records only, the same
    population as ``surfaceable`` / ``no_recorded_showing`` (one denominator per block), and
    ``historical`` is the append-only history beside them: every terminal-status row across
    all three record types (superseded, rejected, deprecated; a domain's rejected analogue
    is ``dropped``), the same set the store treats as terminal. Pending proposals are in
    neither number.

    The funnel windows on the moment the verdict landed. A rejection lands at ``valid_to``.
    An accept lands at ``ratified_at`` (``Store.ratify`` stamps it and leaves ``valid_from``
    at proposal time, so ``valid_from`` would date a slow ratification to when it was
    proposed); a record written already-accepted has no ``ratified_at`` and lands at
    ``valid_from``. There is deliberately **no upper bound** at ``now``: ``drop()`` stamps
    ``valid_to`` from the real clock while tests freeze ``now``.
    """
    records: list[Decision | Fact] = [*decisions, *facts]
    accepted = [r for r in records if r.status == DecisionStatus.ACCEPTED]
    surfaceable = len(accepted)
    no_recorded_showing = sum(1 for r in accepted if not shows_by_record.get(r.id))

    def accepted_at(r: Decision | Fact) -> datetime:
        return r.ratified_at or r.valid_from

    historical = sum(1 for r in records if r.status in _TERMINAL_RECORD_STATUSES) + sum(
        1 for d in domains if d.status in _TERMINAL_DOMAIN_STATUSES
    )

    accepted_in_window = [r for r in accepted if accepted_at(r) >= cutoff]
    rejected_in_window = [
        r
        for r in records
        if r.status == DecisionStatus.REJECTED and r.valid_to is not None and r.valid_to >= cutoff
    ]
    return MemoryStats(
        decisions=sum(1 for d in decisions if d.status == DecisionStatus.ACCEPTED),
        facts=sum(1 for f in facts if f.status == DecisionStatus.ACCEPTED),
        domains=sum(1 for d in domains if d.status == DomainStatus.ACCEPTED),
        historical=historical,
        no_recorded_showing=no_recorded_showing,
        surfaceable=surfaceable,
        accepted_in_window=len(accepted_in_window),
        rejected_in_window=len(rejected_in_window),
        auto_accepted_in_window=sum(
            1 for r in accepted_in_window if (r.ratified_by or "").startswith("auto:")
        ),
    )


def _anchor_stats(bindings: Sequence[AnchorBinding]) -> AnchorStats:
    """Count binding ``status`` values over rows the caller already fetched.

    Takes rows, not a path or a connection: it is called inside ``build_report``'s
    ``closing(...)`` block and never reopens the index (spec D8). A status outside
    live/degraded/orphaned is not counted rather than guessed into a bucket.
    """
    counts = {"live": 0, "degraded": 0, "orphaned": 0}
    for b in bindings:
        if b.status in counts:
            counts[b.status] += 1
    return AnchorStats(**counts)
