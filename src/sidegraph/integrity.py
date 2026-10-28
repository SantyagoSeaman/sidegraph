"""The integrity registry: one list of checks that says what a human should fix.

Sidegraph used to degrade silently: a stale graph, records with every anchor orphaned, files the
store could not index, a store that would not open. Each signal was either manual
(``sidegraph-doctor``, ``sidegraph-stats``) or aimed at the model. This module is the one place
that decides *whether something is wrong*, so that SessionStart, doctor and stats agree and each
renders the verdict in its own way:

- a ``Problem`` carries a model-facing ``line`` (SessionStart text), a human-facing ``notice``
  (sent to the user, at most once a day, by the host), a ``summary`` and ``fix`` (stats), and
  ``findings`` (doctor rows);
- a ``Check`` is a detector plus the surfaces it appears on;
- ``run`` calls the checks listed for a surface and **never raises**.

Portable core: this module imports nothing from ``engine/`` or ``host/``. The graph reader is
duck-typed (``.path``, ``.freshness()``, ``.engine_version()``,
``.engine_version_tested()``), as ``doctor`` always did. Host-specific checks (the
stray-store line) are built by ``host/hooks.py`` and passed to ``run``.

**Detectors are pure reads**, because doctor and stats are pure reads by contract and cannot
construct a ``Store`` (its ``__init__`` can write). They read canonical files, ``Inputs.index``
(a ``mode=ro`` connection), the reader and git (read-only commands through
``gitenv.git_env()``, under the detector's own deadline). Only ``pending-ratification`` reads
``Inputs.store``, and it is session-only. The git-reading detectors (``refresh-hook-missing``,
``store-uncommitted``, ``branch-only-records``) share one rule: read-only commands, one budget.

**Not run is not clean.** A detector returning ``None`` means "ran and found nothing", and the
host deletes that check's notice key on it. A detector whose evidence is absent raises
``_NotRun`` instead, so a statuses-not-computed index or an unknown freshness never reads as
"fixed".
# see design/superpowers/specs/2026-10-02-integrity-self-check-design.md (D1, D2, D4)
# see design/superpowers/specs/2026-10-02-stranded-store-writes-design.md (section 5, step 1)
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError
from ulid import ULID

from . import githooks, maintenance
from .config import _store_project_root, path_state
from .freshness import GraphFreshness, staleness_phrase
from .gitenv import git_env
from .schema import validate_record_body
from .store import (
    _TERMINAL_DECISION_STATUSES,
    _TERMINAL_DOMAIN_STATUSES,
    SKIP_BAD_ARCHIVE_SEGMENT,
    SKIPPED_CANONICAL_KEY,
    VOLATILE_STALE_KEY,
    Store,
    parse_skip_list,
    skipped_segment_text,
)
from .store_format import (
    FORMAT_MARKER_NAME,
    Inventory,
    inventory_from_disk,
    inventory_from_git_tree,
    parse_marker,
)
from .store_layout import (
    GITATTRIBUTES_NAME,
    SHARDS_SUBDIR,
    VERIFICATIONS_SUBDIR,
    StoreRequirementError,
    store_marker,
    store_requirement,
)
from .store_view import SUBDIR_OF, effective_bodies, effective_records

Severity = Literal["broken", "degraded", "advisory"]
Surface = Literal["session", "notice", "doctor", "stats"]

#: Severity ranks: the host re-sends a notice when the rank rises (D5), sorts by it, and stats
#: keeps only the top two.
SEVERITY_RANK: dict[str, int] = {"broken": 3, "degraded": 2, "advisory": 1}

# `pending-ratification` earns a notice when its oldest proposal is this old (days): the same
# age at which a proposal leaves the surfaced map (`retrieval.proposal_surfaces`).
_PENDING_NOTICE_DAYS = 30

# `orphaned-records` is `degraded` when at least `_ORPHANED_RECENT_COUNT` of the orphaned
# records were written in the last `_ORPHANED_RECENT_DAYS` days: a burst of new orphans means
# anchoring is failing now. Old orphans are curation debt (`advisory`). Unmeasured beyond two
# points: one store's 7 old orphaned facts, and one field corpus's burst of 32 new ones.
_ORPHANED_RECENT_DAYS = 14
_ORPHANED_RECENT_COUNT = 3

_MODEL_CLAUSE = "Sidegraph memory tools will fail until it is fixed."
_GRAPH_FIX = githooks.GRAPH_REBUILD_COMMAND

# `refresh-hook-missing` runs a few read-only git commands (one `rev-parse`, one `config`, about
# 5.5 ms each); this is their shared budget, in seconds.
_REFRESH_HOOK_BUDGET = 2.0

# `store-uncommitted` and `branch-only-records` each run a handful of read-only git commands
# (about 5.5 ms each, a few per branch for the second); this is the shared budget of each, in
# seconds. A scan that runs out of it is partial.
_STRANDED_BUDGET = 2.0

# A store file nobody committed for this long (hours) is stranded, not in flight: a day is the
# line a write session crosses before the record is forgotten in the checkout.
_UNCOMMITTED_HOURS = 24

# A branch whose tip is older than this (days) holds records that will not reach the default
# branch on their own.
_BRANCH_STALE_DAYS = 7

# The branches the line and the notice name; the scan itself covers every unmerged branch.
_BRANCHES_NAMED = 3

# The store's canonical layout, as git sees it: the record directories a branch scan reads, the
# other directories a write touches, and the root files. Anything else under the store is not a
# store write.
_HOT_RECORD_DIRS = ("decisions", "facts", "domains")
_STORE_DIRS = (
    *_HOT_RECORD_DIRS,
    "initiatives",
    "bindings",
    "entities",
    "archive",
    SHARDS_SUBDIR,
    VERIFICATIONS_SUBDIR,
)
_RECORD_DIRS = (*_HOT_RECORD_DIRS, "bindings", "entities")  # the dirs maintenance empties
_ULID_NAMED_DIRS = (
    *_HOT_RECORD_DIRS,
    "initiatives",
    "entities",
    VERIFICATIONS_SUBDIR,
)  # named by the id minted at write
_ROOT_FILES = (
    "format",
    ".gitignore",
    GITATTRIBUTES_NAME,
    "stamping_live_since",
    "domain-snapshot.json",
)

# A record is stranded unless it is terminal: a decision or fact superseded, rejected or
# deprecated, or a domain superseded or dropped.
_TERMINAL_STATUS_VALUES = frozenset(
    {s.value for s in _TERMINAL_DECISION_STATUSES} | {s.value for s in _TERMINAL_DOMAIN_STATUSES}
)


class _NotRun(Exception):
    """Raised by a detector whose evidence is absent. ``run`` treats it like any exception:
    no problem, and no clean id."""


class _BudgetExhausted(_NotRun):
    """A git call timed out, or the detector's budget was already spent. ``branch-only-records``
    catches it and reports what it had found (partial); anywhere else it is a plain ``_NotRun``."""


@dataclass(frozen=True)
class RecordFinding:
    """A doctor row whose logical subject cannot be inferred from a shared shard path."""

    path: str
    detail: str
    record_id: str
    record_type: str


@dataclass(frozen=True)
class Problem:
    check: str  # the check id
    severity: Severity
    summary: str  # short noun phrase for stats ("code graph stale")
    fix: str  # the exact command or action ("graphify update .")
    line: str | None  # model-facing SessionStart line, None = none
    notice: str | None  # human-facing sentence, None = not for the human
    findings: tuple[tuple[str, str], ...] = ()  # subjectless doctor (path, detail) pairs
    record_findings: tuple[RecordFinding, ...] = ()


@dataclass(frozen=True)
class Check:
    id: str
    surfaces: frozenset[Surface]
    detect: Callable[[Inputs], Problem | None]


@dataclass
class Inputs:
    """Everything a detector may read. A field a surface cannot supply stays ``None`` and the
    detectors that need it raise ``_NotRun``."""

    store_dir: Path
    now: datetime
    store: Store | None = None  # SessionStart only
    index: sqlite3.Connection | None = None  # read-only; None = index checks do not run
    reader: Any | None = None  # a GraphifyReader, duck-typed
    graph_path: Path | None = None  # where the graph was looked for
    borrowed_from: Path | None = None
    open_error: BaseException | None = None  # Store() raised (D3)
    drift_count: int | None = None  # the refresh's live drifted count (D4 check 2)
    drift_recent: tuple[int, str] | None = None  # (new count, span) from the same refresh
    known_freshness: GraphFreshness | None = None  # a caller that already computed it
    _freshness: GraphFreshness | None = field(default=None, init=False, repr=False)

    def freshness(self) -> GraphFreshness:
        """``known_freshness``, else ``reader.freshness()``; computed once per ``Inputs``.
        ``_NotRun`` without either."""
        if self._freshness is None:
            if self.known_freshness is not None:
                self._freshness = self.known_freshness
            elif self.reader is not None:
                self._freshness = self.reader.freshness()
            else:
                raise _NotRun
        return self._freshness


@dataclass(frozen=True)
class RunResult:
    problems: list[Problem]  # registry order
    clean: frozenset[str]  # ids whose detector ran and found nothing


# -- 0 store-unreadable ---------------------------------------------------------------------


def _index_is_to_blame(error: sqlite3.DatabaseError, store_dir: Path) -> bool:
    """Whether "remove ``index.db``" is advice that can work: not for ``SQLITE_CANTOPEN`` (a
    read-only store directory, a missing parent: the index is not what failed to open), and not
    when there is no ``index.db`` to remove (a store path that is a plain file)."""
    code = getattr(error, "sqlite_errorcode", None)
    if isinstance(code, int) and (code & 0xFF) == sqlite3.SQLITE_CANTOPEN:
        return False
    return (store_dir / "index.db").is_file()


def _first_line(error: BaseException) -> str:
    """The first line of the message, whitespace collapsed, at most 200 characters. A pydantic
    ``ValidationError`` runs over several lines and quotes the record (``input_value=...``): the
    notice goes to the user's warning area and the line to the model, so only the headline goes."""
    lines = str(error).splitlines()
    return " ".join(lines[0].split())[:200] if lines else ""


def _open_error_fix(error: BaseException, store_dir: Path) -> str:
    """The sentence that tells the human what to do about a store that would not open. The first
    match wins, and the order matters: ``json.JSONDecodeError`` is a ``ValueError``."""
    if isinstance(error, StoreRequirementError) and error.required is not None:
        return f"Upgrade Sidegraph to >= {error.required}, then start a new session."
    if isinstance(error, json.JSONDecodeError | ValidationError):
        return (
            "Run `sidegraph-verify` from the repository root to find the file; a merge conflict "
            "left in .sidegraph/ is the usual cause."
        )
    if isinstance(error, sqlite3.DatabaseError) and _index_is_to_blame(error, store_dir):
        return (
            f"The index is derived and safe to delete: remove {store_dir}/index.db and start a "
            "new session."
        )
    if isinstance(error, ValueError) and "migration tooling is deferred" in str(error):
        return (
            "This store was written by a different Sidegraph version: upgrade Sidegraph (or use "
            "the version that wrote it)."
        )
    return "Run `sidegraph-verify` from the repository root for details."


def _detect_store_unreadable(inputs: Inputs) -> Problem | None:
    """``Store()`` raised: memory is off for the session. The only ``broken`` check.
    see design/superpowers/specs/2026-10-02-integrity-self-check-design.md (D3, D4 check 0)"""
    error = inputs.open_error
    if error is None:
        return None
    notice = (
        f"Sidegraph cannot open its store at {inputs.store_dir}, so memory is off for this "
        f"session ({type(error).__name__}: {_first_line(error)}). "
        f"{_open_error_fix(error, inputs.store_dir)}"
    )
    return Problem(
        check="store-unreadable",
        severity="broken",
        summary="store cannot be opened",
        fix="sidegraph-verify",
        line=f"{notice} {_MODEL_CLAUSE}",
        notice=notice,
    )


# -- 1 pending-ratification -----------------------------------------------------------------


def _detect_pending_ratification(inputs: Inputs) -> Problem | None:
    """The queue of proposals nobody ratified: today's SessionStart line, plus a notice for the
    human once the oldest one is ``_PENDING_NOTICE_DAYS`` old. ``SIDEGRAPH_RATIFY_NUDGE=off``
    switches the whole check off, as it always switched the line.
    see design/superpowers/specs/2026-10-02-integrity-self-check-design.md (D4 check 1)"""
    if os.environ.get("SIDEGRAPH_RATIFY_NUDGE") == "off" or inputs.store is None:
        raise _NotRun
    store = inputs.store
    nd, nf, ndom = store.pending_ratification_counts()
    total = nd + nf + ndom
    if not total:
        return None
    # Domains carry no valid_from and are left out of the age scan, never guessed.
    stamps = [d.valid_from for d in store.iter_proposed()]
    stamps += [f.valid_from for f in store.iter_proposed_facts()]
    age_days = max(0, (inputs.now - min(stamps)).days) if stamps else None
    oldest = f"; oldest {age_days} days" if age_days is not None else ""
    line = (
        f"Sidegraph: {total} record(s) awaiting ratification "
        f"({nd} decisions, {nf} facts, {ndom} domains{oldest}) — review "
        "with the ratify MCP tool or sidegraph-ratify."
    )
    notice = None
    if age_days is not None and age_days >= _PENDING_NOTICE_DAYS:
        notice = (
            f"Sidegraph: {total} record(s) await ratification, the oldest for {age_days} days. "
            "Review them with `sidegraph-ratify`, or ask the agent to use the ratify tool."
        )
    return Problem(
        check="pending-ratification",
        severity="advisory",
        summary=f"{total} record(s) awaiting ratification",
        fix="sidegraph-ratify",
        line=line,
        notice=notice,
    )


# -- 2 code-drift ---------------------------------------------------------------------------


def _detect_code_drift(inputs: Inputs) -> Problem | None:
    """Records needing review after code changed or a scoped check stopped applying.

    The count is the refresh's own
    return value: when the refresh could not run (outside git) it returns ``None`` and the old
    cache must not be counted. ``drift_recent``, when the refresh could certify it, adds a
    recency clause after the count; this check only renders it.
    ``SIDEGRAPH_DRIFT_NUDGE=off`` switches the check off (the host still refreshes the cache).
    No notice: superseding drifted records is the agent's work.
    see design/superpowers/specs/2026-10-02-integrity-self-check-design.md (D4 check 2)
    and design/superpowers/specs/2026-10-27-drift-notice-delta-design.md (4.5)"""
    n = inputs.drift_count
    if n is None or os.environ.get("SIDEGRAPH_DRIFT_NUDGE") == "off":
        raise _NotRun
    if not n:
        return None
    clause = ""
    if inputs.drift_recent is not None:
        new, span = inputs.drift_recent
        clause = f" ({new} new {span})" if new else f" (none new {span})"
    line = (
        f"Sidegraph: {n} record(s) need content review{clause}: code changed or a prior check "
        "no longer applies — task-relevant ones carry a [drifted] tag in retrieval; "
        "full list: sidegraph-doctor; record_drift_check if still true, "
        "supersede if no longer true."
    )
    return Problem(
        check="code-drift",
        severity="advisory",
        summary=f"{n} drifted record(s)",
        fix="sidegraph-doctor",
        line=line,
        notice=None,
    )


# -- 3 graph-borrowed -----------------------------------------------------------------------


def _detect_graph_borrowed(inputs: Inputs) -> Problem | None:
    """A linked worktree reading the main checkout's graph: where the map comes from. Asks for
    no action, so no notice.
    see design/superpowers/specs/2026-10-01-worktree-borrowed-graph-design.md (D2)"""
    if inputs.reader is None or inputs.borrowed_from is None:
        return None
    line = (
        "Sidegraph: this worktree has no code graph of its own, so memory "
        f"reads the main checkout's ({inputs.reader.path}); code that exists only on this "
        "branch is not in it."
    )
    return Problem(
        check="graph-borrowed",
        severity="advisory",
        summary="borrowed code graph",
        fix=_GRAPH_FIX,
        line=line,
        notice=None,
    )


# -- 4 graph-stale --------------------------------------------------------------------------


def _detect_graph_stale(inputs: Inputs) -> Problem | None:
    """The graph's build commit leaves a file the graph should hold unreflected. ``unknown`` is
    not evidence that the graph is current (git failed or timed out), so it is not run, and a
    recorded notice survives it. A borrowed graph is the main checkout's: the text names that
    checkout, which is where it is rebuilt.
    see design/superpowers/specs/2026-10-01-stale-graph-visible-design.md (D4, D6)
    see design/superpowers/specs/2026-10-02-integrity-self-check-design.md (D4 check 4)"""
    freshness = inputs.freshness()
    if freshness.state == "unknown":
        raise _NotRun
    if freshness.state != "stale":
        return None
    phrase = staleness_phrase(freshness)
    if inputs.borrowed_from is not None:
        line = (
            f"Sidegraph: the main checkout's code graph ({inputs.borrowed_from}) is "
            f"stale ({phrase}): rebuild it there by running {githooks.GRAPH_REBUILD}"
        )
    else:
        line = (
            f"Sidegraph: the code graph is stale ({phrase}), "
            "so memory cannot see or anchor to code added after the build. "
            f"Rebuild it from the repository root by running {githooks.GRAPH_REBUILD}"
        )
    findings: tuple[tuple[str, str], ...] = ()
    if inputs.reader is not None:
        examples = ", ".join(freshness.sample)
        if freshness.changed > len(freshness.sample):
            examples += ", …"
        findings = (
            (
                str(inputs.reader.path),
                f"the code graph is stale: {phrase} (e.g. {examples}); "
                "memory cannot see or anchor to code added after the build — rebuild from the "
                f"repository root by running {githooks.GRAPH_REBUILD}, then run `sidegraph-sync`",
            ),
        )
    return Problem(
        check="graph-stale",
        severity="degraded",
        summary="code graph stale",
        fix=_GRAPH_FIX,
        line=line,
        notice=line,
        findings=findings,
    )


# -- 6 graph-missing ------------------------------------------------------------------------


def _detect_graph_missing(inputs: Inputs) -> Problem | None:
    """No reader: the graph is absent or unreadable. Advisory and model-line only, because the
    docs promise the graph is optional and a doc-only corpus runs without one (R9).
    ``path_state`` ``unknown`` ("cannot look") stays silent, as its docstring asks.
    see design/superpowers/specs/2026-10-02-integrity-self-check-design.md (D4 check 6, R9)"""
    if inputs.reader is not None:
        return None
    graph_path = inputs.graph_path
    if graph_path is None:
        raise _NotRun
    state = path_state(graph_path)
    if state == "unknown":
        raise _NotRun
    if state == "present":
        line = (
            f"Sidegraph: the code graph at {graph_path} could not be read, so memory cannot "
            "match files to records or anchor new ones. Rebuild it from the repository root "
            f"by running {githooks.GRAPH_REBUILD}"
        )
        summary = "code graph unreadable"
    else:
        line = (
            f"Sidegraph: no code graph at {graph_path}, so memory cannot match files to "
            "records or anchor new ones. Build it from the repository root "
            f"by running {githooks.GRAPH_REBUILD}"
        )
        summary = "code graph missing"
    return Problem(
        check="graph-missing",
        severity="advisory",
        summary=summary,
        fix=_GRAPH_FIX,
        line=line,
        notice=None,
    )


# -- 6b engine-version ----------------------------------------------------------------------

#: The engine-version advisory's first case, the stamp substituted: one definition for the
#: SessionStart line, the doctor finding and the store-behind-the-engine case that joins it.
_ENGINE_UNTESTED = (
    "the code graph was built by Graphify {version}, a release this Sidegraph was not tested "
    "with, so anchors and domains may read differently; pin Graphify to a tested release, "
    'listed under "Engine version pinning" in Sidegraph\'s Graphify integration guide'
)


def _engine_untested_sentence(version: str | None) -> str:
    """The engine-version sentence for a graph built by Graphify ``version``, untested here.
    ``None`` cannot reach case 1 (an unstamped graph counts as tested); it renders as "an
    unknown release" so the type matches ``engine_version()``."""
    display = " ".join(version.split()) if version else "an unknown release"
    return _ENGINE_UNTESTED.format(version=display)


# Graphify's first release with doc-to-code mention edges. The reader decides the generation
# (engine/reader.py, _MENTION_EDGES_SINCE); this copy is only the number the line names.
_MENTION_EDGES_RELEASE = "0.9.62"
# The public migration guide for 0.14, in the URL form README.md uses for user-facing pointers.
_MIGRATION_GUIDE_URL = (
    "https://github.com/SantyagoSeaman/sidegraph/blob/main/docs/guides/migrating-to-0.14.md"
)
_ENGINE_BEHIND_TEXT = (
    "the graph comes from a Graphify release that adds doc-to-code mention edges "
    f"({_MENTION_EDGES_RELEASE} or later) and this store has not been migrated (no minimum "
    f"Sidegraph version); follow the migration guide for 0.14: {_MIGRATION_GUIDE_URL}"
)


def _detect_engine_version(inputs: Inputs) -> Problem | None:
    """The graph comes from a Graphify release this Sidegraph was not tested with, so anchors and
    domains may read differently. Advisory: a model line and a doctor finding, no notice. Not
    run without a reader; the stamp and the tested set are engine facts the seam answers
    (``engine_version``, ``engine_version_tested``), so this module stays engine-free.
    A mention-edge graph over a store without a minimum requirement also points to the
    migration guide. A malformed marker leaves that case silent; case 1 still runs.
    see design/superpowers/specs/2026-10-14-graphify-upgrade-design.md (D1, D6)"""
    reader = inputs.reader
    if reader is None:
        raise _NotRun
    sentences: list[str] = []
    if not reader.engine_version_tested():
        sentences.append(_engine_untested_sentence(reader.engine_version()))
    # Case 2: a graph with doc-to-code mention edges over a store that was not migrated (no
    # minimum Sidegraph version). A marker that does not parse is refused by every entry point
    # that reads the store, so this case says nothing about it, and case 1 still runs.
    if reader.engine_generation() == "mention-edges":
        try:
            behind = store_requirement(inputs.store_dir) is None
        except (StoreRequirementError, OSError):
            behind = False
        if behind:
            sentences.append(_ENGINE_BEHIND_TEXT)
    if not sentences:
        return None
    text = "; ".join(sentences) + "."
    return Problem(
        check="engine-version",
        severity="advisory",
        summary=(
            "code graph built by an untested Graphify release"
            if not reader.engine_version_tested()
            else "store not migrated for the code graph's Graphify release"
        ),
        fix=(
            "pin a tested Graphify release"
            if not reader.engine_version_tested()
            else "follow the migration guide for 0.14"
        ),
        line=f"Sidegraph: {text}",
        notice=None,
        findings=((str(reader.path), text),),
    )


# -- 7 orphaned-records ---------------------------------------------------------------------


def _ulid_time(record_id: str) -> datetime | None:
    """When a ULID id was minted, or ``None`` for an id that is not one (a hand-edited store:
    its age is unknowable, so it never counts as recent)."""
    try:
        return ULID.from_str(record_id).datetime
    except (ValueError, TypeError):
        return None


def _leaf_rows(index: sqlite3.Connection, table: str) -> list[tuple[str, int, int]]:
    """``(record id, Tier-2 bindings, orphaned ones)`` for each open record of ``table`` that
    has a Tier-2 binding. Status is index-only state (store-format.md). ``table`` is one of two
    literals below, never caller input."""
    terminal = sorted(s.value for s in _TERMINAL_DECISION_STATUSES)
    marks = ", ".join("?" * len(terminal))
    rows = index.execute(
        "SELECT r.id, COUNT(*), "
        "SUM(CASE WHEN json_extract(b.data, '$.status') = 'orphaned' THEN 1 ELSE 0 END) "
        f"FROM {table} r JOIN anchor_bindings b ON b.record_id = r.id "
        f"WHERE r.status NOT IN ({marks}) AND json_extract(b.data, '$.tier') = 2 "
        "GROUP BY r.id",
        terminal,
    ).fetchall()
    return [(str(r[0]), int(r[1]), int(r[2])) for r in rows]


def binding_statuses_computed(index: sqlite3.Connection) -> bool:
    """Whether the index's binding statuses have been computed. A reload resets every binding to
    ``live`` and sets ``volatile_stale`` until a sync recomputes them, so until then no check
    that reads a status has anything to say. Shared by ``orphaned-records`` and by doctor's
    skipped-checks list. An index with no ``meta`` table (a hand-built one) counts as computed,
    the rule ``doctor._check_binding_statuses`` applies too."""
    has_meta = index.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'meta'"
    ).fetchone()
    if not has_meta:
        return True
    row = index.execute("SELECT value FROM meta WHERE key = ?", (VOLATILE_STALE_KEY,)).fetchone()
    return row is None or row[0] != "1"


def _record_locations(store_dir: Path) -> Callable[[str, str], str]:
    """``(table, record id) -> path`` of where the record is stored: ``<table>/<id>.json`` on a
    per-record store, the file, shard or archive segment that holds its effective record on a
    packed one (a packed record has no file to name).

    # see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§7.2)"""
    packed = effective_records(store_dir)

    def where(table: str, record_id: str) -> str:
        if packed is not None:
            rec = packed.get((_TABLE_TYPES[table], record_id))
            if rec is not None:
                return str(rec.source_path(store_dir))
        return str(store_dir / table / f"{record_id}.json")

    return where


_TABLE_TYPES = {subdir: record_type for record_type, subdir in SUBDIR_OF.items()}


def _detect_orphaned_records(inputs: Inputs) -> Problem | None:
    """Open decisions and facts whose every Tier-2 binding is orphaned: retrieval reaches them
    only through their file or domain. The rule of ``sync._stale_decisions``, extended to facts.

    Severity keys on recent records: a burst of new orphans means anchoring is failing now
    (``degraded``, a notice); old ones are curation debt (``advisory``, a line). Not run while
    ``volatile_stale`` is set: a reloaded index reads every binding ``live`` until a sync
    recomputes the statuses, so "clean" there would be a verdict on nothing.
    see design/superpowers/specs/2026-10-02-integrity-self-check-design.md (D4 check 7)"""
    index = inputs.index
    if index is None:
        raise _NotRun
    if not binding_statuses_computed(index):
        raise _NotRun
    decisions = _leaf_rows(index, "decisions")
    facts = _leaf_rows(index, "facts")
    open_with_leaf = len(decisions) + len(facts)
    orphaned = [("decisions", *r) for r in decisions if r[1] == r[2]]
    orphaned += [("facts", *r) for r in facts if r[1] == r[2]]
    if not orphaned:
        return None
    cutoff = inputs.now - timedelta(days=_ORPHANED_RECENT_DAYS)
    recent = 0
    for _table, record_id, _leaves, _orphans in orphaned:
        minted = _ulid_time(record_id)
        if minted is not None and minted >= cutoff:
            recent += 1
    n = len(orphaned)
    line = (
        f"Sidegraph: {n} of {open_with_leaf} open record(s) have every code anchor orphaned, "
        "so retrieval reaches them only through their file or domain. If the graph is stale, "
        "rebuilding it re-anchors them; otherwise `sidegraph-doctor` lists them and the "
        "heal-anchors skill repairs them."
    )
    degraded = recent >= _ORPHANED_RECENT_COUNT
    where = _record_locations(inputs.store_dir)
    findings = tuple(
        RecordFinding(
            where(table, record_id),
            f"every code anchor of this record is orphaned ({leaves} leaf anchor(s)); "
            "retrieval reaches it only through its file or domain — re-anchor it with "
            "add_anchors or the heal-anchors skill, or supersede it if the code is gone",
            record_id=record_id,
            record_type={"decisions": "decision", "facts": "fact"}[table],
        )
        for table, record_id, leaves, _orphans in sorted(orphaned, key=lambda o: o[1])
    )
    return Problem(
        check="orphaned-records",
        severity="degraded" if degraded else "advisory",
        summary=f"{n} record(s) with every anchor orphaned",
        fix="sidegraph-doctor",
        line=line,
        notice=line if degraded else None,
        record_findings=findings,
    )


# -- 8 store-files-skipped ------------------------------------------------------------------


def _detect_store_files_skipped(inputs: Inputs) -> Problem | None:
    """Canonical files the last reload left out of memory (``Store`` warned on stderr, where a
    hook's user never looks). The list is meta ``skipped_canonical_files``; an absent, empty or
    malformed value is clean.
    see design/superpowers/specs/2026-10-02-integrity-self-check-design.md (D4 check 8)"""
    index = inputs.index
    if index is None:
        raise _NotRun
    row = index.execute("SELECT value FROM meta WHERE key = ?", (SKIPPED_CANONICAL_KEY,)).fetchone()
    entries = parse_skip_list(row[0]) if row is not None else []
    if not entries:
        return None
    first = entries[0]
    if first.get("reason") == SKIP_BAD_ARCHIVE_SEGMENT:
        # A segment is not a file to fix or remove: its other lines loaded, and deleting it
        # would destroy every archived record in it. Say what ``Store`` warns on stderr.
        also = (
            f" {len(entries) - 1} more store file(s) were left out as well."
            if len(entries) > 1
            else ""
        )
        text = (
            f"Sidegraph: {skipped_segment_text(first['path'])} Run `sidegraph-verify` to list "
            f"the lines.{also}"
        )
    else:
        more = f", and {len(entries) - 1} more" if len(entries) > 1 else ""
        text = (
            f"Sidegraph: {len(entries)} store file(s) could not be indexed and are left out of "
            f"memory: {first['path']} ({first.get('reason') or 'no reason recorded'}){more}. Run "
            "`sidegraph-verify` to list them, then fix or restore them with git."
        )
    return Problem(
        check="store-files-skipped",
        severity="degraded",
        summary=f"{len(entries)} store file(s) skipped",
        fix="sidegraph-verify",
        line=text,
        notice=text,
    )


# -- 8b refresh-helper-outdated ------------------------------------------------------------


def _detect_refresh_helper_outdated(inputs: Inputs) -> Problem | None:
    """A hook calls a helper with directory-based locking or missing ``GRAPHIFY_NO_AUTO_REFRESH=1``
    (an earlier ``sidegraph-init`` wrote it). Not run outside a git repository, with no helper, or
    when no hook calls it (``refresh-hook-missing`` says that); one hook that calls it is enough,
    so a partly wired repository and a recorded decline do not hide it. The fix is plain
    ``sidegraph-init``: it refreshes a helper any hook runs without touching a recorded decline,
    which ``--hooks`` would reset. Same read-only git rule and budget as
    ``refresh-hook-missing``.
    see design/superpowers/specs/2026-10-14-graphify-upgrade-design.md (D7)"""
    deadline = time.monotonic() + _REFRESH_HOOK_BUDGET
    project = _store_project_root(inputs.store_dir)
    info = githooks.repo_info(project, timeout=deadline - time.monotonic())
    if info is None:
        raise _NotRun
    current = githooks.helper_is_current(info)
    if current is None or not githooks.helper_in_use(info):
        raise _NotRun
    old_lock = githooks.helper_uses_directory_lock(info)
    if current and not old_lock:
        return None
    if old_lock:
        return Problem(
            check="refresh-helper-outdated",
            severity="advisory",
            summary="graph refresh helper uses directory-based locking",
            fix="sidegraph-init",
            line=(
                "Sidegraph: the graph refresh helper uses directory-based locking, which can "
                "allow overlapping rebuilds with uutils mkdir. Ask the user to run "
                "`sidegraph-init`, which refreshes the helper and keeps their recorded hook "
                "choice; do not run it unasked."
            ),
            notice=(
                "Sidegraph: the graph refresh helper uses directory-based locking, which can "
                "allow overlapping rebuilds with uutils mkdir. Refresh it with `sidegraph-init`."
            ),
            findings=(
                (
                    str(info.helper),
                    "the installed graph refresh helper uses directory-based "
                    "locking; run `sidegraph-init` to refresh it",
                ),
            ),
        )
    return Problem(
        check="refresh-helper-outdated",
        severity="advisory",
        summary="graph refresh helper is out of date",
        fix="sidegraph-init",
        line=(
            "Sidegraph: the graph refresh hook in this repository runs Graphify without "
            "GRAPHIFY_NO_AUTO_REFRESH=1, so Graphify 0.9.87 and later can rewrite the agent "
            "skills they installed on the first rebuild after an upgrade. Ask the user to run "
            "`sidegraph-init`, which refreshes the helper and keeps their recorded hook choice; "
            "do not run it unasked."
        ),
        notice=(
            "Sidegraph: the graph refresh hook's helper is out of date: it runs Graphify without "
            "GRAPHIFY_NO_AUTO_REFRESH=1, so Graphify 0.9.87 and later can rewrite your agent "
            "skills after an upgrade. Refresh it with `sidegraph-init`."
        ),
        findings=(
            (
                str(info.helper),
                "the installed graph refresh helper runs Graphify without "
                "GRAPHIFY_NO_AUTO_REFRESH=1; run `sidegraph-init` to refresh it",
            ),
        ),
    )


# -- 9 refresh-hook-missing -----------------------------------------------------------------


def _detect_refresh_hook_missing(inputs: Inputs) -> Problem | None:
    """No git hook keeps the graph fresh: the helper is missing, or one of the three hook files
    (in the ``core.hooksPath`` directory when it is set) does not mention it, so a call added by
    hand counts. Clean when the person declined (``sidegraph.graphRefresh`` reads ``false``).

    Not run without a reader, outside a git repository, or when the reader's graph is not
    ``<main checkout>/graphify-out/graph.json``, the one graph the helper rebuilds (a linked
    worktree that borrows it is checked; one with a graph of its own and a repository with no
    main checkout are not): a repository without a graph is never nagged.
    It runs git read-only, through ``gitenv.git_env()``, under a 2 s budget, which amends D2 of
    the integrity spec (``store-uncommitted`` and ``branch-only-records`` do the same).
    Advisory, but with a notice: the fix is the human's decision, as with a long-pending queue.
    The repository and the recorded choice are read from the store's LOGICAL project root
    (``config._store_project_root``), the directory ``sidegraph-init`` installs the hook from,
    so a symlinked store checks the hooks of the project that holds the link.
    see design/superpowers/specs/2026-10-02-graph-refresh-hook-design.md (D7)
    see design/superpowers/specs/2026-10-02-store-git-root-symlink-design.md (D4)"""
    if inputs.reader is None:
        raise _NotRun
    deadline = time.monotonic() + _REFRESH_HOOK_BUDGET
    project = _store_project_root(inputs.store_dir)
    info = githooks.repo_info(project, timeout=deadline - time.monotonic())
    if info is None:
        raise _NotRun
    try:
        target = githooks.rebuild_target(info, inputs.store_dir)
        if target is None or Path(inputs.reader.path).resolve() != target.resolve():
            raise _NotRun
    except OSError:
        raise _NotRun from None
    if deadline - time.monotonic() <= 0:
        raise _NotRun
    choice = githooks.read_choice(project, timeout=deadline - time.monotonic())
    if choice is not None and choice.declined:
        return None
    if githooks.status(info).wired:
        return None
    return Problem(
        check="refresh-hook-missing",
        severity="advisory",
        summary="no git hook keeps the code graph fresh",
        fix="sidegraph-init --hooks",
        line=(
            "Sidegraph: no git hook keeps the code graph fresh in this repository, so it goes "
            "stale as code changes. Ask the user whether to install one "
            "(`sidegraph-init --hooks`) or to turn this reminder off "
            "(`sidegraph-init --no-hooks`); do not install it unasked."
        ),
        notice=(
            "Sidegraph: no git hook keeps the code graph fresh in this repository, so it goes "
            "stale as code changes. Install it with `sidegraph-init --hooks`, or stop this "
            "reminder with `sidegraph-init --no-hooks`."
        ),
    )


# -- 10 store-uncommitted -------------------------------------------------------------------


def _git(
    args: list[str],
    cwd: Path,
    deadline: float,
    *,
    stdin: bytes | None = None,
    ok: tuple[int, ...] = (0,),
) -> bytes:
    """One read-only git call from ``cwd`` through ``gitenv.git_env()``; its stdout.

    ``_BudgetExhausted`` when ``deadline`` (a ``time.monotonic`` value) has passed or git runs
    past it, ``_NotRun`` when git cannot run or exits with a status outside ``ok``. A status in
    ``ok`` other than 0 (``symbolic-ref -q`` and ``rev-parse --verify -q`` exit 1 for "no") gives
    the empty stdout.
    # see design/superpowers/specs/2026-10-02-stranded-store-writes-design.md (section 5, step 1)
    """
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _BudgetExhausted
    try:
        done = subprocess.run(
            ["git", *args],
            cwd=cwd,
            env=git_env(),
            input=stdin,
            capture_output=True,
            timeout=remaining,
        )
    except subprocess.TimeoutExpired:
        raise _BudgetExhausted from None
    except (OSError, subprocess.SubprocessError, ValueError):
        raise _NotRun from None
    if done.returncode not in ok:
        raise _NotRun
    return done.stdout


def _text(raw: bytes) -> str:
    """Git's bytes as text, lossless for a path that is not UTF-8."""
    return raw.decode("utf-8", "surrogateescape")


def _parse_status(raw: bytes) -> list[tuple[str, str]]:
    """``(XY, path)`` for each entry of ``git status --porcelain=v1 -z``. An ``R`` or ``C`` entry
    is followed by one more NUL field, the source path, which has no status prefix and is
    consumed here, not read as an entry of its own.
    # see design/superpowers/specs/2026-10-02-stranded-store-writes-design.md (D1 step 3)"""
    fields = _text(raw).split("\0")
    if fields and fields[-1] == "":
        fields.pop()
    entries: list[tuple[str, str]] = []
    it = iter(fields)
    for entry in it:
        xy, path = entry[:2], entry[3:]
        if "R" in xy or "C" in xy:
            next(it, None)
        entries.append((xy, path))
    return entries


# A file name is trusted as a write time only inside this window: a 26-character Crockford name
# dated before the project existed, or more than a day ahead of the clock, is not a ULID this
# store minted (a hand-made file, a clock gone wrong), so its mtime is used instead.
_ULID_TRUSTED_FROM = datetime(2020, 1, 1, tzinfo=UTC)
_ULID_FUTURE_SLACK = timedelta(days=1)


def _written_at(stem: str, now: datetime) -> datetime | None:
    """When the ULID ``stem`` says its file was written, or ``None`` when it is not a ULID or
    not a plausible one."""
    minted = _ulid_time(stem)
    if minted is None or minted < _ULID_TRUSTED_FROM or minted > now + _ULID_FUTURE_SLACK:
        return None
    return minted


def _mtime(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, UTC)
    except OSError:
        return None


def _age_phrase(age: timedelta) -> str:
    """``"{h} hours"`` below 48 hours, ``"{d} days"`` from there (whole units, rounded down)."""
    hours = int(age.total_seconds() // 3600)
    return f"{hours} hours" if hours < 48 else f"{hours // 24} days"


@dataclass(frozen=True)
class _Stranded:
    """One uncommitted store file: where it sits (its first path segment under the store, or
    the root file's name), whether the worktree lost it, and how old it is (``None`` when
    unknowable)."""

    first: str
    deleted: bool
    age: timedelta | None


def _stranded_age(
    store_dir: Path, rel: str, first: str, xy: str, deleted: bool, now: datetime
) -> timedelta | None:
    """How long an uncommitted entry has been there, from the most stable source it has.

    A new record or entity file is named by the ULID minted when it was written, which no
    ``git stash -u`` / ``pop`` can rewrite, unlike its mtime; a name that is not a plausible ULID
    (``_written_at``) falls back to the mtime. A modified file, and anything under
    ``bindings/`` (named for the record's older ULID), has its mtime. A deleted file has only
    its own subdirectory's mtime, a lower bound; the store root's moves at every open, so a
    deleted root file has no age. Unknowable: ``None``.
    # see design/superpowers/specs/2026-10-02-stranded-store-writes-design.md (D1 step 4)"""
    if deleted:
        if first == rel:  # a root file: the store root's mtime is not evidence
            return None
        stamp = _mtime(store_dir / first)
    else:
        stamp = None
        if xy == "??" and first in _ULID_NAMED_DIRS:
            stamp = _written_at(Path(rel).stem, now)
        if stamp is None:
            stamp = _mtime(store_dir / rel)
    return None if stamp is None else now - stamp


def _stranded_kinds(entries: list[_Stranded]) -> str:
    """The kinds the text lists. Deleted hot files beside a new archive segment are a
    compaction (``Store.compact``, the only legitimate deletion of a per-record store). On a
    packed store the only writer of a shard is maintenance, so a changed shard file means a
    maintenance run: the record files it deleted and the archive segments it removed or
    wrote are part of it. Any other deletion is a deleted file.

    # see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§7.2)"""
    deleted_hot = [e for e in entries if e.deleted and e.first in _HOT_RECORD_DIRS]
    archive = [e for e in entries if not e.deleted and e.first == "archive"]
    shards = [e for e in entries if not e.deleted and e.first == SHARDS_SUBDIR]
    maintenance = bool(shards)
    compaction = bool(deleted_hot and archive) and not maintenance
    absorbed: list[_Stranded] = []
    if maintenance:
        absorbed = [
            e
            for e in entries
            if e in shards or e.first == "archive" or (e.deleted and e.first in _RECORD_DIRS)
        ]
    elif compaction:
        absorbed = [*deleted_hot, *archive]
    by_dir = {
        "decisions": "decision",
        "facts": "fact",
        "domains": "domain",
        "bindings": "binding",
        "entities": "entity",
        VERIFICATIONS_SUBDIR: "verification",
    }
    counts: dict[str, int] = {kind: 0 for kind in by_dir.values()}
    other = deleted = 0
    for e in entries:
        if any(e is a for a in absorbed):
            continue
        if e.deleted:
            deleted += 1
        elif e.first in by_dir:
            counts[by_dir[e.first]] += 1
        else:
            other += 1
    parts = ["a maintenance run"] if maintenance else []
    parts += [f"{n} {kind} file(s)" for kind, n in counts.items() if n]
    if other:
        parts.append(f"{other} other")
    if deleted:
        parts.append(f"{deleted} deleted file(s)")
    if compaction:
        parts.append("a compaction")
    return ", ".join(parts)


def _detect_store_uncommitted(inputs: Inputs) -> Problem | None:
    """Store files nobody committed for a day, so no other checkout or teammate sees them (a
    ``git clean`` or a re-clone loses them).

    ``git status`` gives the paths, and the store's prefix in git's terms (``--show-prefix``, so a
    store symlinked into the repository maps) turns them into store paths. Fully staged entries
    are left out: they are being committed, and a ``doctor --check`` in a pre-commit hook must
    not block the commit that fixes the problem. Not run outside a git repository, when git fails
    or when the budget is spent.
    see design/superpowers/specs/2026-10-02-stranded-store-writes-design.md (D1, T1-T7)"""
    store_dir = inputs.store_dir
    deadline = time.monotonic() + _STRANDED_BUDGET
    where = _text(_git(["rev-parse", "--show-toplevel", "--show-prefix"], store_dir, deadline))
    lines = where.split("\n")
    if len(lines) != 3 or lines[2]:  # a toplevel holding a newline cannot be told apart
        raise _NotRun
    prefix = lines[1]
    raw = _git(
        [
            "--no-optional-locks",
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            "--",
            ".",
        ],
        store_dir,
        deadline,
    )
    entries: list[_Stranded] = []
    for xy, path in _parse_status(raw):
        if xy[0] not in " ?!" and xy[1] == " ":  # fully staged: being committed right now
            continue
        if not path.startswith(prefix):
            continue
        rel = path[len(prefix) :]
        first = rel.split("/", 1)[0]
        if first == VERIFICATIONS_SUBDIR and rel.endswith(".tmp"):
            continue  # unpublished crash debris is not history to commit
        in_layout = first in _STORE_DIRS and first != rel
        if not in_layout and rel not in _ROOT_FILES:
            continue
        deleted = xy[1] == "D"
        age = _stranded_age(store_dir, rel, first, xy, deleted, inputs.now)
        entries.append(_Stranded(first=first, deleted=deleted, age=age))
    ages = [e.age for e in entries if e.age is not None]
    if not ages or max(ages) < timedelta(hours=_UNCOMMITTED_HOURS):
        return None
    commit_path = prefix or "./"
    text = (
        f"Sidegraph: {len(entries)} store file(s) in {store_dir} are not committed, the oldest "
        f"for {_age_phrase(max(ages))} ({_stranded_kinds(entries)}), so other checkouts and "
        f"teammates do not see them. Commit {commit_path} in a pull request."
    )
    return Problem(
        check="store-uncommitted",
        severity="degraded",
        summary=f"{len(entries)} store file(s) uncommitted",
        fix=f"commit {commit_path}",
        line=text,
        notice=text,
        findings=((str(store_dir), text.removeprefix("Sidegraph: ")),),
    )


# -- 11 branch-only-records -----------------------------------------------------------------


def _default_branch(cwd: Path, deadline: float) -> str:
    """The full refname records must reach: ``origin/HEAD`` when it resolves (in a PR-only flow
    the local ``main`` lags), else the first of ``origin/main``, ``origin/master``, ``main`` and
    ``master`` that exists (the remote branches first, for the same reason). ``_NotRun`` when
    there is none.
    # see design/superpowers/specs/2026-10-02-stranded-store-writes-design.md (D2 default branch)"""
    remote = _text(
        _git(["symbolic-ref", "-q", "refs/remotes/origin/HEAD"], cwd, deadline, ok=(0, 1))
    )
    remote = remote.strip()
    if remote and _git(["rev-parse", "--verify", "-q", remote], cwd, deadline, ok=(0, 1)).strip():
        return remote
    candidates = (
        "refs/remotes/origin/main",
        "refs/remotes/origin/master",
        "refs/heads/main",
        "refs/heads/master",
    )
    existing = _text(_git(["for-each-ref", "--format=%(refname)", *candidates], cwd, deadline))
    for ref in candidates:
        if ref in existing.split():
            return ref
    raise _NotRun


def _short_ref(ref: str) -> str:
    for prefix in ("refs/remotes/", "refs/heads/"):
        if ref.startswith(prefix):
            return ref[len(prefix) :]
    return ref


@dataclass(frozen=True)
class _BranchHit:
    name: str  # the short branch name
    tip: datetime
    total: int  # open records only this branch holds
    proposed: int  # ... of which await ratification


def _marker_blob(ref: str, prefix: str, cwd: Path, deadline: float) -> str:
    """The object id of the format marker in ``ref``'s tree, ``""`` when it has none."""
    return _text(
        _git(
            ["rev-parse", "--verify", "-q", f"{ref}:{prefix}{FORMAT_MARKER_NAME}"],
            cwd,
            deadline,
            ok=(0, 1),
        )
    ).strip()


def _marker_major(blob: str, cwd: Path, deadline: float) -> int:
    """The marker major a marker blob says (``1`` when there is none or it does not parse)."""
    if not blob:
        return 1
    text = _text(_git(["cat-file", "-p", blob], cwd, deadline))
    try:
        return parse_marker(
            text, Path(FORMAT_MARKER_NAME), malformed=lambda _p, detail: ValueError(detail)
        ).major
    except ValueError:
        return 1


def _tree_inventory(top: Path, tree: str, store_rel: str, deadline: float) -> Inventory:
    """``inventory_from_git_tree`` under the detector's budget: each git call gets what is left
    of it, and one that outlives it is ``_BudgetExhausted``, as for every other call here."""

    def remaining() -> float:
        left = deadline - time.monotonic()
        if left <= 0:
            raise _BudgetExhausted
        return left

    try:
        return inventory_from_git_tree(
            top, tree, store_rel, validate=validate_record_body, remaining=remaining
        )
    except subprocess.TimeoutExpired:
        raise _BudgetExhausted from None
    except (OSError, subprocess.SubprocessError, ValueError):
        raise _NotRun from None


def _packed_branch_records(
    ref: str, default: str, cwd: Path, deadline: float, scan: dict[str, Any]
) -> dict[str, str] | None:
    """``_branch_records`` for a store whose tree on either side is format 2, else ``None``.

    A record counts per identity over the two trees' inventories, not per file: a packed record
    has no file on the default branch, so a branch that edits it (a new per-record file) adds no
    record, and a branch that adds a new identity does. Each side's record is its effective one
    (``store_rules.effective``). The key is the identity's per-record path, as the callers only
    need it to tell records apart.

    ``scan`` is the one scan's memory: where the store is, and the default branch's marker and
    inventory, built once however many branches are looked at. A branch whose marker blob is the
    default branch's needs no marker read of its own.

    # see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§7.2)"""
    if "where" not in scan:
        where = _text(_git(["rev-parse", "--show-toplevel", "--show-prefix"], cwd, deadline))
        lines = where.split("\n")
        if len(lines) != 3 or lines[2]:
            raise _NotRun
        scan["where"] = (Path(lines[0]), lines[1])
        scan["default_blob"] = _marker_blob(default, lines[1], cwd, deadline)
        scan["default_major"] = _marker_major(scan["default_blob"], cwd, deadline)
    top, prefix = scan["where"]
    blob = _marker_blob(ref, prefix, cwd, deadline)
    major = (
        scan["default_major"]
        if blob == scan["default_blob"]
        else _marker_major(blob, cwd, deadline)
    )
    if 2 not in (major, scan["default_major"]):
        return None
    store_rel = prefix.rstrip("/") or "."
    if "default_inventory" not in scan:
        scan["default_inventory"] = _tree_inventory(top, default, store_rel, deadline)
    mine = effective_bodies(_tree_inventory(top, ref, store_rel, deadline), lenient=True)
    theirs = scan["default_inventory"].by_identity()  # any occurrence: a quarantined one exists
    records: dict[str, str] = {}
    for (record_type, record_id), rec in mine.items():
        subdir = SUBDIR_OF.get(record_type)  # a record type this version does not know: not ours
        if subdir not in _HOT_RECORD_DIRS or (record_type, record_id) in theirs:
            continue
        status = rec.raw.get("status") if isinstance(rec.raw, dict) else None
        if isinstance(status, str):
            records[f"{subdir}/{record_id}.json"] = status
    return records


def _branch_records(
    ref: str, default: str, cwd: Path, deadline: float, scan: dict[str, Any] | None = None
) -> dict[str, str]:
    """``{path: status}`` for each record ``ref`` adds that the default branch lacks.

    Three dots count only the branch's own additions. The diff is told ``--no-renames`` (a
    record deleted and a similar one added, as a supersede and a compaction do, would be one
    ``R`` entry and ``--diff-filter=A`` would drop it) and ``--no-relative`` (a user's
    ``diff.relative`` would make the paths store-relative, which ``<rev>:<path>`` cannot find: a
    false clean). A squash- or rebase-merged branch stays unmerged by ancestry while its records
    are already on the default branch, so those are dropped by asking the default branch for each
    path (``cat-file --batch-check``). The rest are read from the branch (``cat-file --batch``)
    with their top-level status, terminal ones included: the caller lets the newest branch that
    holds a record decide its status. A file that does not parse is skipped.
    # see design/superpowers/specs/2026-10-02-stranded-store-writes-design.md (D2 per branch)"""
    packed = _packed_branch_records(ref, default, cwd, deadline, {} if scan is None else scan)
    if packed is not None:
        return packed
    added = _git(
        [
            "diff",
            "--name-only",
            "-z",
            "--no-renames",
            "--no-relative",
            "--diff-filter=A",
            f"{default}...{ref}",
            "--",
            *_HOT_RECORD_DIRS,
        ],
        cwd,
        deadline,
    )
    paths = [p for p in _text(added).split("\0") if p and "\n" not in p]
    if not paths:
        return {}
    asked = "".join(f"{default}:{p}\n" for p in paths).encode("utf-8", "surrogateescape")
    exists = _text(_git(["cat-file", "--batch-check=%(objecttype)"], cwd, deadline, stdin=asked))
    answers = exists.split("\n")[:-1]
    if len(answers) != len(paths):
        raise _NotRun
    new = [p for p, answer in zip(paths, answers, strict=True) if answer.endswith(" missing")]
    if not new:
        return {}
    asked = "".join(f"{ref}:{p}\n" for p in new).encode("utf-8", "surrogateescape")
    data = _git(["cat-file", "--batch"], cwd, deadline, stdin=asked)
    records: dict[str, str] = {}
    pos = 0
    for path in new:
        end = data.find(b"\n", pos)
        if end < 0:
            raise _NotRun
        header, pos = data[pos:end], end + 1
        if header.endswith(b" missing"):
            continue
        try:
            size = int(header.rsplit(b" ", 1)[1])
            body = data[pos : pos + size]
            pos += size + 1
            status = json.loads(body).get("status")
        except (ValueError, IndexError, AttributeError):
            continue
        if isinstance(status, str):
            records[path] = status
    return records


def _detect_branch_only_records(inputs: Inputs) -> Problem | None:
    """Open records that exist only on local branches not merged into the default branch: they
    reach it when the branch merges, which a stale branch may never do. Every unmerged branch is
    scanned, oldest tip first, under one budget; a scan that runs out of it reports "at least"
    what it found, and one that found nothing is not run, so a recorded notice survives it. A
    record counts once, for the newest branch that holds it.

    A line for the model whenever a branch holds records; a notice for the human only when a
    branch holding records has a tip older than ``_BRANCH_STALE_DAYS``. The fix is "merge the
    branch", never "ratify there". No doctor finding (a CI checkout has no local branches) and no
    stats item.
    see design/superpowers/specs/2026-10-02-stranded-store-writes-design.md (D2, T8-T14)"""
    cwd = inputs.store_dir
    deadline = time.monotonic() + _STRANDED_BUDGET
    default = _default_branch(cwd, deadline)
    current = _text(_git(["symbolic-ref", "-q", "HEAD"], cwd, deadline, ok=(0, 1))).strip()
    listing = _text(
        _git(
            [
                "for-each-ref",
                f"--no-merged={default}",
                "--format=%(refname)%00%(committerdate:unix)",
                "refs/heads",
            ],
            cwd,
            deadline,
        )
    )
    branches: list[tuple[int, str]] = []
    for row in listing.split("\n"):
        ref, _, unix = row.partition("\0")
        if ref and ref != current and unix.isdigit():
            branches.append((int(unix), ref))
    branches.sort()  # oldest tip first

    # A record rides on every branch cut from the one that wrote it, and on a local default
    # branch that merged it: it counts once, for the newest tip that holds it (the scan is oldest
    # first, so a later branch overwrites), and that branch's copy decides its status.
    owner: dict[str, tuple[int, str, str]] = {}
    partial = False
    scan: dict[str, Any] = {}
    try:
        for tip_unix, ref in branches:
            for path, status in _branch_records(ref, default, cwd, deadline, scan).items():
                owner[path] = (tip_unix, ref, status)
    except _BudgetExhausted:
        partial = True
    held: dict[str, list[str]] = {}
    for _tip, ref, status in owner.values():
        if status not in _TERMINAL_STATUS_VALUES:
            held.setdefault(ref, []).append(status)
    hits = [
        _BranchHit(
            _short_ref(ref),
            datetime.fromtimestamp(tip_unix, UTC),
            len(held[ref]),
            held[ref].count("proposed"),
        )
        for tip_unix, ref in branches
        if ref in held
    ]
    if not hits:
        if partial:
            raise _NotRun
        return None

    short = _short_ref(default)
    n = sum(h.total for h in hits)
    awaiting = sum(h.proposed for h in hits)
    named = sorted(hits, key=lambda h: -h.total)[:_BRANCHES_NAMED]  # stable: oldest first on ties
    rest = len(hits) - len(named)
    where = ", ".join(f"{h.name}: {h.total}" for h in named)
    if rest:
        where += f", and {rest} more branches"
    line = (
        f"Sidegraph: {'at least ' if partial else ''}{n} record(s)"
        f"{f' ({awaiting} awaiting ratification)' if awaiting else ''} exist only on branches "
        f"not merged into {short} ({where}); they reach {short} when those merge."
        f"{' The branch scan ran out of time before it looked at every branch.' if partial else ''}"
    )
    stale = [h for h in hits if inputs.now - h.tip > timedelta(days=_BRANCH_STALE_DAYS)]
    notice = None
    if stale:
        names = ", ".join(h.name for h in stale[:_BRANCHES_NAMED])
        if len(stale) > _BRANCHES_NAMED:
            names += ", …"
        notice = (
            f"Sidegraph: {sum(h.total for h in stale)} record(s) sit on branches untouched for "
            f"over a week ({names}): merge those branches, or the records never reach {short}."
        )
    return Problem(
        check="branch-only-records",
        severity="advisory",
        summary=f"{n} record(s) only on unmerged branches",
        fix="merge the branches",
        line=line,
        notice=notice,
    )


# -- the registry ---------------------------------------------------------------------------

#: The checks, in SessionStart line order. ``stray-store`` is a host check
#: (``host.hooks.host_checks``) and sits between ``graph-stale`` and ``graph-missing``: the hook
#: passes ``CHECKS[:5] + host_checks(location) + CHECKS[5:]``.
STORE_UNREADABLE = Check("store-unreadable", frozenset({"session"}), _detect_store_unreadable)

# -- store-not-migrated and maintenance-due ------------------------------------------------

# More per-record files than this makes the soft notice due: the number is the author's choice,
# and the packed-format benchmark may move it.
_MAINTENANCE_FILE_THRESHOLD = 200

# The public guide, in the URL form README.md uses for user-facing pointers: a repo-relative
# path does not exist in a user's repository.
_MIGRATION_GUIDE = (
    "https://github.com/SantyagoSeaman/sidegraph/blob/main/docs/guides/migrating-to-0.17.md"
)

# The directories that hold per-record files, and the one that holds legacy archive segments.
_PER_RECORD_DIRS = ("decisions", "facts", "domains", "entities", "bindings")


def _count_record_files(
    store_dir: Path, limit: int, dirs: tuple[str, ...] = _PER_RECORD_DIRS
) -> int:
    """How many per-record files the store holds, counting stops once it passes ``limit``. A
    directory listing only: no file is opened, and only regular files count (a directory or a
    symlink named ``x.json`` is not a record)."""
    count = 0
    for name in dirs:
        try:
            with os.scandir(store_dir / name) as entries:
                for entry in entries:
                    if entry.name.endswith(".json") and entry.is_file(follow_symlinks=False):
                        count += 1
                        if count > limit:
                            return count
        except (FileNotFoundError, NotADirectoryError):
            continue
    return count


def _holds_any_record(store_dir: Path) -> bool:
    """Whether the store holds any record: a per-record file, or a non-empty archive segment."""
    if _count_record_files(store_dir, 0, (*_PER_RECORD_DIRS, "initiatives")) > 0:
        return True
    try:
        with os.scandir(store_dir / "archive") as entries:
            return any(
                e.name.endswith(".jsonl")
                and e.is_file(follow_symlinks=False)
                and e.stat(follow_symlinks=False).st_size > 0
                for e in entries
            )
    except (FileNotFoundError, NotADirectoryError):
        return False


def _marker_or_not_run(store_dir: Path) -> Any:
    """The parsed format marker (``None`` when there is none); a marker that does not parse is
    ``store-unreadable``'s to report, so these checks are not run."""
    try:
        return store_marker(store_dir)
    except (StoreRequirementError, OSError):
        raise _NotRun from None


def _detect_store_not_migrated(inputs: Inputs) -> Problem | None:
    """A store in the pre-0.17 layout (marker major below 2, or no marker) that holds a record:
    the packed format is the one 0.17 is built for, and the migration is the owner's decision on
    the default branch, so this is the urgent notice, ``degraded`` (the highest tier short of
    ``broken``: the store works). An empty store has nothing to migrate and says nothing, and a
    directory with no marker file is not read as a layout (the first ``Store`` open stamps the
    major-0 marker, and SessionStart opens the store before it runs the checks).
    # see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§6.3)"""
    marker = _marker_or_not_run(inputs.store_dir)
    if marker is None or marker.major >= 2:
        return None  # no marker: ``Store`` backfills one on its first open, SessionStart's
    if not _holds_any_record(inputs.store_dir):
        return None
    text = (
        "Sidegraph: the store uses the pre-0.17 layout, one file per record. Run "
        "`sidegraph-migrate` on the default branch (or the `sidegraph:compact` skill) after "
        f"the team checklist in {_MIGRATION_GUIDE}."
    )
    return Problem(
        check="store-not-migrated",
        severity="degraded",
        summary="store not migrated to the packed layout",
        fix="sidegraph-migrate",
        line=text,
        notice=text,
        findings=((str(inputs.store_dir), text),),
    )


def _maintenance_reasons(store_dir: Path, shards: int) -> list[str]:
    """Why maintenance has work to do on a format-2 store, in the order the notice names them.
    Counting files is a directory listing; the rest reads the shards once under the model check
    and plans a run in which no per-record file may fold (no git call at SessionStart, so
    "settled" cannot be known): only a segment already covered by existing shard lines counts,
    which maintenance would delete whatever the clock says."""
    reasons: list[str] = []
    files = _count_record_files(store_dir, _MAINTENANCE_FILE_THRESHOLD)
    if files > _MAINTENANCE_FILE_THRESHOLD:
        reasons.append(f"more than {_MAINTENANCE_FILE_THRESHOLD} per-record files")
        return reasons  # enough to be due: the shards need not be read
    inv = inventory_from_disk(store_dir, validate=validate_record_body, on_unreadable=_ignore)
    nothing_folds = maintenance.Eligibility(0, int(time.time()), {}, frozenset(), "")
    plan = maintenance.plan_maintenance(inv, nothing_folds, shards)
    if plan.segment_deletions:
        reasons.append(f"{len(plan.segment_deletions)} archive segment(s) it can remove")
    healing = maintenance.self_healing_counts(inv, shards)
    if healing["misplaced"] or healing["equal_duplicate"]:
        reasons.append(
            f"{healing['misplaced'] + healing['equal_duplicate']} misplaced or duplicated "
            "shard line(s)"
        )
    if healing["stray"]:
        reasons.append(f"{healing['stray']} stray shard file(s)")
    return reasons


def _ignore(_path: str, _error: OSError) -> None:
    """``on_unreadable`` for a read that only counts: the file counts as not regular."""


def _detect_maintenance_due(inputs: Inputs) -> Problem | None:
    """A packed store that maintenance would tidy: more than ``_MAINTENANCE_FILE_THRESHOLD``
    per-record files, or a legacy archive segment it would delete, or a misplaced or duplicated
    shard line, or a stray shard file (verify does not report these: maintenance heals them).
    A segment kept because a line is not ``deletable`` does not count. Advisory: nothing is
    broken, and running maintenance is the human's choice.
    # see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§6.3)"""
    marker = _marker_or_not_run(inputs.store_dir)
    if marker is None or marker.major < 2 or marker.shards is None:
        return None
    reasons = _maintenance_reasons(inputs.store_dir, marker.shards)
    if not reasons:
        return None
    text = (
        f"Sidegraph: the store has work for maintenance ({'; '.join(reasons)}). Run "
        "`sidegraph-compact` on the default branch (or the `sidegraph:compact` skill); nothing "
        "is broken."
    )
    return Problem(
        check="maintenance-due",
        severity="advisory",
        summary="store maintenance is due",
        fix="sidegraph-compact",
        line=text,
        notice=text,
        findings=((str(inputs.store_dir), text),),
    )


CHECKS: tuple[Check, ...] = (
    STORE_UNREADABLE,
    Check("pending-ratification", frozenset({"session"}), _detect_pending_ratification),
    Check("code-drift", frozenset({"session"}), _detect_code_drift),
    Check("graph-borrowed", frozenset({"session"}), _detect_graph_borrowed),
    Check("graph-stale", frozenset({"session", "doctor", "stats"}), _detect_graph_stale),
    Check("graph-missing", frozenset({"session"}), _detect_graph_missing),
    Check("engine-version", frozenset({"session", "doctor"}), _detect_engine_version),
    Check("orphaned-records", frozenset({"session", "doctor", "stats"}), _detect_orphaned_records),
    Check("store-files-skipped", frozenset({"session", "stats"}), _detect_store_files_skipped),
    Check(
        "refresh-helper-outdated",
        frozenset({"session", "doctor"}),
        _detect_refresh_helper_outdated,
    ),
    Check("refresh-hook-missing", frozenset({"session"}), _detect_refresh_hook_missing),
    Check(
        "store-uncommitted", frozenset({"session", "doctor", "stats"}), _detect_store_uncommitted
    ),
    Check("branch-only-records", frozenset({"session"}), _detect_branch_only_records),
    Check("store-not-migrated", frozenset({"session", "doctor"}), _detect_store_not_migrated),
    Check("maintenance-due", frozenset({"session", "doctor"}), _detect_maintenance_due),
)


def run(inputs: Inputs, surface: Surface, checks: Sequence[Check] = CHECKS) -> RunResult:
    """Run the checks listed for ``surface`` over ``inputs``; never raises.

    Each detector runs in its own ``try``: an exception (``_NotRun`` included) yields neither a
    problem nor a clean id, and the remaining checks still run. Hooks must never crash.
    # see design/superpowers/specs/2026-10-02-integrity-self-check-design.md (D1)
    """
    problems: list[Problem] = []
    clean: set[str] = set()
    for check in checks:
        if surface not in check.surfaces:
            continue
        try:
            problem = check.detect(inputs)
        except Exception:
            continue
        if problem is None:
            clean.add(check.id)
        else:
            problems.append(problem)
    return RunResult(problems, frozenset(clean))
