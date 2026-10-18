"""Optional, content-free delivery evidence in the derived index.

An occurrence, a rendered record placement and an anchor relation are different units.
Callers own the connection and transaction; this module never opens or commits one.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, suppress
from datetime import UTC, datetime, timedelta
from typing import NamedTuple

from . import __version__

RETENTION_DAYS = 30
MAX_OCCURRENCES = 20_000
MAX_PLACEMENTS = 50_000
MAX_LINKS = 100_000
PLACEMENTS_PER_OCCURRENCE = 1_024
LINKS_PER_OCCURRENCE = 4_096
META_KEY = "delivery_ledger:v1"
_TABLES = ("delivery_occurrences", "delivery_placements", "delivery_anchor_links")
_IDENTIFIER = re.compile(r"[A-Za-z0-9_.:/@+~-]+\Z")
_SURFACES = {"get_task_context", "query_decisions", "drill_down", "hook_read", "hook_brief"}
_REASONS = {"graph_unavailable", "structure_cut", "core_error"}
_KINDS = {"adr", "constraint", "gotcha", "lesson", "fact"}
_STATUSES = {"accepted", "proposed", "superseded", "rejected"}
_MAX_COUNTER = 2**63 - 1


class Snapshot(NamedTuple):
    """Identity and trust as rendered, without a body or a lookup of current status."""

    record_id: str
    record_type: str
    kind: str
    status: str

    @property
    def key(self) -> tuple[str, str]:
        return self.record_type, self.record_id


class Placement(NamedTuple):
    record: Snapshot
    file_group: str | None = None


class Delivery(NamedTuple):
    surface: str
    placements: Sequence[Placement] = ()
    session_id: str | None = None
    agent_id: str | None = None
    host_call_id: str | None = None
    structure: bool | None = False
    unresolved: bool | None = False
    reason: str | None = None
    considered: frozenset[tuple[str, str]] | None = None
    placed: frozenset[tuple[str, str]] | None = None
    degraded: frozenset[tuple[str, str]] | None = None
    error: bool = False


_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS delivery_occurrences (
        id INTEGER PRIMARY KEY, occurrence_id TEXT UNIQUE NOT NULL, at TEXT NOT NULL,
        surface TEXT NOT NULL, outcome TEXT NOT NULL, reason TEXT,
        session_id TEXT, agent_id TEXT, host_call_id TEXT, runtime_version TEXT,
        structure_present INTEGER, unresolved INTEGER,
        unique_record_count INTEGER NOT NULL, placement_count INTEGER NOT NULL,
        anchor_link_count INTEGER NOT NULL, retained_placements INTEGER NOT NULL,
        retained_links INTEGER NOT NULL, metadata_omitted INTEGER NOT NULL,
        considered INTEGER, rank_refused INTEGER, hard_cap_removed INTEGER, degraded INTEGER
    )""",
    "CREATE INDEX IF NOT EXISTS idx_delivery_at ON delivery_occurrences(at)",
    """CREATE TABLE IF NOT EXISTS delivery_placements (
        id INTEGER PRIMARY KEY, occurrence INTEGER NOT NULL, record_id TEXT NOT NULL,
        record_type TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL, file_group TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_delivery_placement ON delivery_placements(occurrence)",
    """CREATE TABLE IF NOT EXISTS delivery_anchor_links (
        id INTEGER PRIMARY KEY, occurrence INTEGER NOT NULL, placement INTEGER NOT NULL,
        path TEXT NOT NULL, basis TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_delivery_link ON delivery_anchor_links(occurrence)",
)


@contextmanager
def no_wait(conn: sqlite3.Connection) -> Iterator[None]:
    """Optional reads and writes cannot add a second lock wait to a delivery."""
    previous = int(conn.execute("PRAGMA busy_timeout").fetchone()[0])
    try:
        conn.execute("PRAGMA busy_timeout=0")
        yield
    finally:
        with suppress(sqlite3.Error):
            conn.execute(f"PRAGMA busy_timeout={previous}")


def available(conn: sqlite3.Connection) -> bool:
    """A missing optional ledger never refuses an otherwise usable index."""
    return len(
        conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN (?,?,?)", _TABLES
        ).fetchall()
    ) == len(_TABLES)


def create_schema(conn: sqlite3.Connection) -> None:
    """Execute individually inside the caller's optional atomic mutation."""
    for statement in _SCHEMA:
        conn.execute(statement)


def identifier(value: str | None) -> str | None:
    if not isinstance(value, str) or len(value.encode("utf-8")) > 256:
        return None
    return value if _IDENTIFIER.fullmatch(value) else None


def _path(value: object) -> str | None:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 1024:
        return None
    if value.startswith(("/", "\\")) or "\\" in value or ":" in value:
        return None
    if any(part in ("", ".", "..") for part in value.split("/")):
        return None
    return value if all(ord(c) >= 32 and ord(c) != 127 for c in value) else None


def _snapshot_valid(record: Snapshot) -> bool:
    return (
        identifier(record.record_id) is not None
        and record.record_type in ("decision", "fact")
        and record.kind in _KINDS
        and record.status in _STATUSES
    )


def _meta(conn: sqlite3.Connection) -> dict:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (META_KEY,)).fetchone()
    try:
        value = json.loads(row[0]) if row else {}
    except (ValueError, TypeError):
        value = {}
    # Never preserve arbitrary old/free-form fields in this bounded entry.
    result: dict = {"version": 1}
    for key in ("capacity_occurrences", "capacity_placements", "capacity_links"):
        count = value.get(key, 0) if isinstance(value, dict) else 0
        result[key] = min(max(count, 0), _MAX_COUNTER) if type(count) is int else 0
    for key in ("recording_since", "capacity_evicted_through"):
        stamp = value.get(key) if isinstance(value, dict) else None
        if not isinstance(stamp, str):
            continue
        try:
            parsed = datetime.fromisoformat(stamp)
            if parsed.tzinfo is not None:
                result[key] = parsed.astimezone(UTC).isoformat()
        except (ValueError, TypeError):
            pass
    return result


def _save_meta(conn: sqlite3.Connection, value: dict) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO meta(key,value) VALUES (?,?)",
        (META_KEY, json.dumps(value, separators=(",", ":"))),
    )


def _delete_through(conn: sqlite3.Connection, threshold: int, *, capacity: bool) -> None:
    if capacity:
        counts = [
            conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {column}<=?", (threshold,)
            ).fetchone()[0]
            for table, column in zip(_TABLES, ("id", "occurrence", "occurrence"), strict=True)
        ]
        newest = conn.execute(
            "SELECT MAX(at) FROM delivery_occurrences WHERE id<=?", (threshold,)
        ).fetchone()[0]
        meta = _meta(conn)
        for key, count in zip(
            ("capacity_occurrences", "capacity_placements", "capacity_links"), counts, strict=True
        ):
            meta[key] = min(_MAX_COUNTER, meta[key] + count)
        if newest:
            meta["capacity_evicted_through"] = max(
                newest, meta.get("capacity_evicted_through", newest)
            )
        _save_meta(conn, meta)
    for table in ("delivery_anchor_links", "delivery_placements"):
        conn.execute(f"DELETE FROM {table} WHERE occurrence<=?", (threshold,))
    conn.execute("DELETE FROM delivery_occurrences WHERE id<=?", (threshold,))


def prune(conn: sqlite3.Connection, *, now: datetime | None = None) -> None:
    """Whole oldest occurrences only; children do not have legacy timestamp columns."""
    cutoff = ((now or datetime.now(UTC)) - timedelta(days=RETENTION_DAYS)).isoformat()
    # At can be out of insertion order (clock correction), so age deletion selects exact ids.
    for table in ("delivery_anchor_links", "delivery_placements"):
        conn.execute(
            f"DELETE FROM {table} WHERE occurrence IN "
            "(SELECT id FROM delivery_occurrences WHERE at<?)",
            (cutoff,),
        )
    conn.execute("DELETE FROM delivery_occurrences WHERE at<?", (cutoff,))
    threshold = 0
    for table, cap in zip(_TABLES, (MAX_OCCURRENCES, MAX_PLACEMENTS, MAX_LINKS), strict=True):
        lo = conn.execute(f"SELECT id FROM {table} ORDER BY id LIMIT 1").fetchone()
        hi = conn.execute(f"SELECT id FROM {table} ORDER BY id DESC LIMIT 1").fetchone()
        if lo is None or hi is None or hi[0] - lo[0] + 1 <= cap:
            continue
        if table == "delivery_occurrences":
            victim = hi[0] - cap
        else:
            victim = conn.execute(
                f"SELECT MAX(occurrence) FROM {table} WHERE id<=?", (hi[0] - cap,)
            ).fetchone()[0]
        threshold = max(threshold, victim or 0)
    if threshold:
        _delete_through(conn, threshold, capacity=True)


def _anchor_paths(conn: sqlite3.Connection, record_id: str) -> list[str]:
    paths: set[str] = set()
    for row in conn.execute(
        "SELECT e.data FROM anchor_bindings b JOIN entities e ON e.entity_id=b.entity_id "
        "WHERE b.record_id=?",
        (record_id,),
    ):
        data = json.loads(row[0])
        path = (data.get("descriptor") or {}).get("file_path")
        if isinstance(path, str) and path:
            paths.add(path)
    return sorted(paths)


def append(conn: sqlite3.Connection, delivery: Delivery, *, now: datetime | None = None) -> None:
    """Append all bounded evidence, or let the owning transaction roll it all back."""
    if delivery.surface not in _SURFACES:
        raise ValueError("unknown delivery surface")
    now = now or datetime.now(UTC)
    at = now.astimezone(UTC).isoformat()
    placements = delivery.placements
    keys = {p.record.key for p in placements}
    considered = delivery.considered
    placed = delivery.placed
    refused: int | None
    hard_cut: int | None
    degraded: int | None
    if considered is not None and placed is not None:
        if not keys <= placed <= considered:
            raise ValueError("inconsistent delivery populations")
        refused, hard_cut = len(considered - placed), len(placed - keys)
        degraded = len(keys & (delivery.degraded or frozenset()))
    else:
        refused = hard_cut = degraded = None
    if delivery.error:
        outcome = "error"
    elif placements:
        outcome = "records"
    elif delivery.structure is None:
        outcome = "unknown"
    elif delivery.structure:
        outcome = "structure"
    elif delivery.unresolved is None:
        outcome = "unknown"
    elif delivery.unresolved:
        outcome = "unresolved"
    else:
        outcome = "valid_empty"
    reason = delivery.reason if delivery.reason in _REASONS else None
    omitted = any(
        raw is not None and identifier(raw) is None
        for raw in (delivery.session_id, delivery.agent_id, delivery.host_call_id)
    )
    retained: list[tuple[Placement, str | None, list[tuple[str, str]]]] = []
    total_links = kept_links = 0
    anchors: dict[tuple[str, str], list[str]] = {}
    for i, placement in enumerate(placements):
        record, group = placement
        if group is None:
            if record.key not in anchors:
                anchors[record.key] = _anchor_paths(conn, record.record_id)
            paths, basis = anchors[record.key], "journal_anchor"
        else:
            paths, basis = [group], "render_file"
        total_links += len(paths)
        if i >= PLACEMENTS_PER_OCCURRENCE or not _snapshot_valid(record):
            omitted = True
            continue
        safe_group = _path(group)
        omitted |= group is not None and safe_group is None
        links = []
        for path in paths:
            safe = _path(path)
            if safe is None or kept_links >= LINKS_PER_OCCURRENCE:
                omitted = True
                continue
            links.append((safe, basis))
            kept_links += 1
        retained.append((placement, safe_group, links))
    version = __version__ if len(__version__.encode("utf-8")) <= 64 else None
    row = conn.execute(
        """INSERT INTO delivery_occurrences (
        occurrence_id,at,surface,outcome,reason,session_id,agent_id,host_call_id,runtime_version,
        structure_present,unresolved,unique_record_count,placement_count,anchor_link_count,
        retained_placements,retained_links,metadata_omitted,considered,rank_refused,
        hard_cap_removed,degraded) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            os.urandom(16).hex(),
            at,
            delivery.surface,
            outcome,
            reason,
            identifier(delivery.session_id),
            identifier(delivery.agent_id),
            identifier(delivery.host_call_id),
            version,
            delivery.structure,
            delivery.unresolved,
            len(keys),
            len(placements),
            total_links,
            len(retained),
            kept_links,
            int(omitted),
            len(considered) if considered is not None else None,
            refused,
            hard_cut,
            degraded,
        ),
    )
    occurrence = row.lastrowid
    for placement, group, links in retained:
        record = placement.record
        row = conn.execute(
            "INSERT INTO delivery_placements"
            "(occurrence,record_id,record_type,kind,status,file_group) "
            "VALUES (?,?,?,?,?,?)",
            (occurrence, *record, group),
        )
        conn.executemany(
            "INSERT INTO delivery_anchor_links(occurrence,placement,path,basis) VALUES (?,?,?,?)",
            [(occurrence, row.lastrowid, path, basis) for path, basis in links],
        )
    meta = _meta(conn)
    if "recording_since" not in meta:
        meta["recording_since"] = at
        _save_meta(conn, meta)
    prune(conn, now=now)
