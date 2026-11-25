"""Plain data for briefing blocks.

See design/superpowers/specs/2026-11-20-sessionstart-briefing-design.md (D5).
"""

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import PurePosixPath

from ...hot_index import HotIndex, Record, _is_live
from ...store_layout import DRIFT_CACHE_KEY, proposal_surfaces
from ..hooks import _renderable_records

FALLBACK_WINDOW = timedelta(days=14)
WORKING_SET_MAX_FILES = 40
OnError = Callable[[Exception], None] | None


@dataclass(frozen=True)
class BriefingContext:
    """The data available to a briefing renderer."""

    now: datetime
    accepted: tuple[dict, ...] = ()
    by_file: Mapping[str, tuple[Record, ...]] = field(default_factory=dict)
    working_set: tuple[str, ...] = ()
    since: datetime | None = None
    since_is_fallback: bool = False
    replaced_titles: Mapping[str, str] = field(default_factory=dict)
    queue_count: int = 0
    symbols: Mapping[str, int] = field(default_factory=dict)
    drifted: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Parts:
    """The parts read from one index snapshot."""

    accepted: tuple[dict, ...] = ()
    fact_dates: tuple[datetime, ...] = ()
    predecessors: Mapping[str, str] = field(default_factory=dict)
    by_file: Mapping[str, tuple[Record, ...]] = field(default_factory=dict)
    queue_count: int = 0
    symbols: Mapping[str, int] = field(default_factory=dict)
    drifted: frozenset[str] = frozenset()


def has_accepted_anchor(index: HotIndex, on_error: OnError = None) -> bool:
    """Stop at the first file with a deliverable accepted decision."""
    for ids in index.anchored_files().values():
        records = index.records_for(ids, on_error=on_error)
        if index.lock_lost:
            return False
        if any(r.record_type == "decision" and not r.proposed for r in records):
            return True
    return False


def read_parts(
    index: HotIndex, needs: frozenset[str], now: datetime, on_error: OnError = None
) -> Parts:
    """Read the declared parts while the caller holds the read snapshot."""
    accepted: list[tuple[datetime, str, dict]] = []
    predecessors: dict[str, str] = {}
    fact_dates: list[datetime] = []
    queue_count = 0
    if needs & {"decisions", "window", "queue"}:
        for row in index.rows("decisions"):
            status = row.get("status")
            if status == "superseded" and "window" in needs:
                if isinstance(row.get("id"), str) and isinstance(row.get("title"), str):
                    predecessors[row["id"]] = row["title"]
                continue
            if status != "accepted" and not (status == "proposed" and "queue" in needs):
                continue
            try:
                date = datetime.fromisoformat(row["valid_from"])
                if date.utcoffset() is None:
                    continue
                if status == "proposed":
                    queue_count += proposal_surfaces(date)
                elif all(
                    isinstance(row.get(key), str) for key in ("id", "kind", "title", "choice")
                ) and _is_live(row, now):
                    accepted.append((date, row["id"], row))
            except (KeyError, TypeError, ValueError):
                continue

    if needs & {"window", "queue"}:
        for row in index.rows("facts"):
            try:
                date = datetime.fromisoformat(row["valid_from"])
            except (KeyError, TypeError, ValueError):
                continue
            if date.utcoffset() is None:
                continue
            if "window" in needs and row.get("status") == "accepted":
                fact_dates.append(date)
            if "queue" in needs and row.get("status") == "proposed":
                queue_count += proposal_surfaces(date)

    by_file: dict[str, tuple[Record, ...]] = {}
    if "anchors" in needs:
        for filename, entity_ids in index.anchored_files().items():
            records = index.records_for(entity_ids, on_error=on_error)
            if index.lock_lost:
                return Parts()
            kept = tuple(r for r in _renderable_records(records) if not r.proposed)
            if kept:
                by_file[filename] = kept

    symbol_ids: dict[str, set[str]] = {}
    if "symbols" in needs:
        for row in index.rows("entities"):
            descriptor = row.get("descriptor")
            entity_id = row.get("entity_id")
            if row.get("kind") != "concrete" or not isinstance(descriptor, dict):
                continue
            path, name = descriptor.get("file_path"), descriptor.get("name")
            if (
                not isinstance(path, str)
                or not path
                or not isinstance(name, str)
                or not name
                or not isinstance(entity_id, str)
                or name == PurePosixPath(path).name
            ):
                continue
            records = index.records_for([entity_id], on_error=on_error)
            if index.lock_lost:
                return Parts()
            ids = {
                r.decision["id"] for r in records if r.record_type == "decision" and not r.proposed
            }
            if ids:
                symbol_ids.setdefault(name, set()).update(ids)

    drifted: frozenset[str] = frozenset()
    if needs:
        raw = index.get_meta(DRIFT_CACHE_KEY)
        try:
            cache = json.loads(raw) if raw is not None else None
        except (TypeError, ValueError):
            cache = None
        by_commit = cache.get("by_commit") if isinstance(cache, dict) else None
        if isinstance(by_commit, dict) and all(isinstance(ids, list) for ids in by_commit.values()):
            drifted = frozenset(
                rid for ids in by_commit.values() for rid in ids if isinstance(rid, str)
            )

    accepted.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return Parts(
        accepted=tuple(row for _, _, row in accepted),
        fact_dates=tuple(fact_dates),
        predecessors=predecessors,
        by_file=by_file,
        queue_count=queue_count,
        symbols={name: len(ids) for name, ids in symbol_ids.items()},
        drifted=drifted,
    )


def build(
    parts: Parts, now: datetime, prev_start: datetime | None, files: tuple[str, ...]
) -> BriefingContext:
    """Apply the session window and rank the changed files with memory."""
    dated = [(datetime.fromisoformat(row["valid_from"]), row) for row in parts.accepted]
    since = prev_start
    if since is None:
        dates = [date for date, _ in dated] + list(parts.fact_dates)
        eligible = [date for date in dates if date <= now]
        anchor = max(eligible, default=now)
        try:
            since = anchor - FALLBACK_WINDOW
        except OverflowError:
            # The cutoff may fit in an earlier record's offset even if it underflows here.
            oldest = min(eligible, default=now)
            since = oldest + max(timedelta(), anchor - oldest - FALLBACK_WINDOW)
    replaced_titles = {}
    for date, row in dated:
        old_id = row.get("supersedes")
        if date >= since and isinstance(old_id, str) and old_id in parts.predecessors:
            replaced_titles[old_id] = parts.predecessors[old_id]

    ranked = sorted(
        (
            (-sum(r.record_type == "decision" for r in parts.by_file[path]), path)
            for path in files
            if path in parts.by_file
        ),
    )
    return BriefingContext(
        now=now,
        accepted=parts.accepted,
        by_file=parts.by_file,
        working_set=tuple(path for _, path in ranked[:WORKING_SET_MAX_FILES]),
        since=since,
        since_is_fallback=prev_start is None,
        replaced_titles=replaced_titles,
        queue_count=parts.queue_count,
        symbols=parts.symbols,
        drifted=parts.drifted,
    )
