"""The hook hot path's handle on ``<store>/index.db``: raw ``sqlite3``, no pydantic, no ``Store``.

PreToolUse runs before every Read, Grep, Edit, Write and Bash read command and must cost close to
nothing. It needs the records anchored to the files a call names, a per-agent claim on each file,
and (for telemetry) one touch row and a row per record it printed. Constructing a ``Store`` for
that paid for the package import and for a digest walk of every canonical file. ``HotIndex``
reads and writes the derived index directly and trusts it as the last full open (SessionStart,
the MCP server, a CLI) left it: it never reloads, never heals and never creates anything.

It returns ``None`` from :meth:`HotIndex.open` in every case where ``Store`` would have
repaired the index (missing, symlinked, another schema version, an older layout, unreadable),
or where its format marker refuses this version,
so the hook does nothing and prints ``{}``. The hot SQL (claim, meta read, event insert, shows
upsert) lives here as module constants that ``Store`` runs too, so the two cannot drift.

see design/superpowers/specs/2026-10-03-hot-path-light-index-design.md (D2, D3) and
design/superpowers/specs/2026-10-03-records-at-the-point-of-reading-design.md (D1, D2, D5)
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple
from urllib.parse import quote

from .store_layout import (
    INDEX_JOURNAL_PRAGMA,
    INDEX_STAMPS,
    MISTAKE_KINDS,
    VOLATILE_STALE_KEY,
    check_store_requirement,
    proposal_surfaces,
    symlinked_internals,
)

# The statements the hook and ``Store`` both run. ``Store`` imports these names.
GET_META_SQL = "SELECT value FROM meta WHERE key = ?"
CLAIM_META_SQL = "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO NOTHING"
INSERT_EVENT_SQL = (
    "INSERT INTO retrieval_events (session_id, at, kind, key, detail, agent) "
    "VALUES (?, ?, ?, ?, ?, ?)"
)
# The cumulative per-record counter ``Store.record_retrieval`` bumps once per rendered record.
# The PreToolUse hook runs the same statement once per record it prints.
SHOW_UPSERT_SQL = (
    "INSERT INTO retrieval_shows (record_id, shows, last_shown_at) "
    "VALUES (?, 1, ?) ON CONFLICT(record_id) DO UPDATE SET "
    "shows = shows + 1, last_shown_at = excluded.last_shown_at"
)
# Journal kinds the hook writes besides ``touch``: a record printed in a file-read block, and a
# record appended to a spawned subagent's brief (``agent`` is NULL there: the recipient does not
# exist yet at spawn). Stats pins the literals (it imports nothing from here).
INJECT_KIND = "inject"
INJECT_BRIEF_KIND = "inject_brief"

# One agent's claim on one file, in one statement: insert the key only while the agent holds
# fewer than ``cap`` keys under its prefix. The count is a range query over
# ``[prefix, prefix + END)``, which walks the primary-key index of ``meta`` (``LIKE`` would be
# wrong: ``_`` is a wildcard, and it cannot use the index). ``END`` is above every UTF-8 key, so a
# file name outside the basic plane still counts.
# see design/superpowers/specs/2026-10-03-records-at-the-point-of-reading-design.md (D2)
CLAIM_FILE_SQL = (
    "INSERT INTO meta (key, value) SELECT ?, ? "
    "WHERE (SELECT COUNT(*) FROM meta WHERE key >= ? AND key < ?) < ? "
    "ON CONFLICT(key) DO NOTHING"
)
_KEY_RANGE_END = "\U0010ffff"

# A rebuild holds the write lock for about 100 ms on a large store, and a hook would rather
# drop one touch than stall the user's tool call. ``Store`` sets no pragmas of its own and
# gets sqlite3's 5 s default; only the hook is impatient.
BUSY_TIMEOUT_SECONDS = 1.0

_SUPERSEDED = "superseded"
_REJECTED = "rejected"
_ACCEPTED = "accepted"
_PROPOSED = "proposed"
_CONCRETE = "concrete"


def _is_live(record: dict, now: datetime) -> bool:
    """A decision that is not superseded or rejected and has not expired."""
    if record["status"] in (_SUPERSEDED, _REJECTED):
        return False
    valid_to = record.get("valid_to")
    return valid_to is None or datetime.fromisoformat(valid_to) > now


class Record(NamedTuple):
    """One decision as the hook shows it: the raw row, and whether it is a proposal (it is
    marked ``[unratified]`` where it surfaces)."""

    decision: dict
    proposed: bool


class Shown(NamedTuple):
    """The records one claimed file's block printed, journaled in the claim's transaction."""

    session_id: str
    agent: str | None
    record_ids: Sequence[str]


class HotIndex:
    """An open connection on a store's ``index.db``. Use :meth:`open`; it may return ``None``."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        # Set when a write on this handle raised ``sqlite3.OperationalError`` (almost always the
        # busy timeout: a rebuild or another writer holds the lock). The hook reads it to stop
        # asking for more writes, since each one would wait out the timeout again.
        self.write_failed = False

    @classmethod
    def open(cls, store_path: str | os.PathLike[str]) -> HotIndex | None:
        """The handle for the store at ``store_path``, or ``None`` when ``Store`` would have
        healed something first: the index is missing, a store-owned entry is a symlink (a
        symlinked ``index.db`` would send writes elsewhere), ``schema_version`` is outside
        this code's two accepted stamps, the format marker refuses this version,
        ``retrieval_events`` has no ``agent`` column (an index from before per-agent
        hook state), or anything raises while opening. Never creates a file and never writes
        on open.
        # see design/superpowers/specs/2026-10-03-hot-path-light-index-design.md (D2)"""
        root = Path(store_path)
        conn: sqlite3.Connection | None = None
        try:
            if symlinked_internals(root):
                return None
            check_store_requirement(root)
            index = root / "index.db"
            if not index.is_file():
                return None
            # mode=rw never creates the file; quoted like gitio.open_index_ro (a path may
            # hold characters a URI treats as syntax).
            uri = f"file:{quote(os.fsencode(str(index)))}?mode=rw"
            conn = sqlite3.connect(uri, uri=True, timeout=BUSY_TIMEOUT_SECONDS)
            row = conn.execute(GET_META_SQL, ("schema_version",)).fetchone()
            if row is None or row[0] not in INDEX_STAMPS:
                conn.close()
                return None
            columns = {r[1] for r in conn.execute("PRAGMA table_info(retrieval_events)")}
            if "agent" not in columns:
                conn.close()
                return None
            # Set after every refusal check, so a refused open touches nothing (the pragma can
            # change a database another tool left in WAL). A rollback mode is per connection.
            # A pragma that cannot run (another connection holds the file in WAL) is not a
            # reason to refuse: the handle works in whatever mode the file is in.
            with contextlib.suppress(sqlite3.OperationalError):
                conn.execute(INDEX_JOURNAL_PRAGMA)
            return cls(conn)
        except Exception:
            if conn is not None:
                conn.close()
            return None

    def close(self) -> None:
        self._conn.close()

    # -- one-shot ledger and touch journal (writes) ------------------------------------------

    def get_meta(self, key: str) -> str | None:
        row = self._conn.execute(GET_META_SQL, (key,)).fetchone()
        return row[0] if row else None

    def _in_transaction[T](self, work: Callable[[sqlite3.Connection], T]) -> T:
        """Run ``work`` in one transaction. An ``OperationalError`` (the busy timeout) marks the
        handle as ``write_failed`` and propagates; the transaction is rolled back."""
        try:
            with self._conn:
                return work(self._conn)
        except sqlite3.OperationalError:
            self.write_failed = True
            raise

    def _write(self, sql: str, params: tuple) -> sqlite3.Cursor:
        """Run one write in its own transaction (see :meth:`_in_transaction`)."""
        return self._in_transaction(lambda conn: conn.execute(sql, params))

    def claim_meta(self, key: str, value: str) -> bool:
        """Set ``key`` only if absent; True iff this call set it. One statement, so two hooks
        claiming at once cannot both win. Same SQL as ``Store.claim_meta``."""
        return self._write(CLAIM_META_SQL, (key, value)).rowcount == 1

    def claim_file(
        self, prefix: str, rel_path: str, value: str, cap: int, *, shown: Shown | None = None
    ) -> bool:
        """Claim ``prefix + rel_path`` if absent and if fewer than ``cap`` keys sit under
        ``prefix``; True iff this call inserted it. One statement, so the count and the insert
        cannot be split by another hook: two processes cannot both take the last slot, and
        two cannot both take one key.

        When the claim inserted and ``shown`` is given, the records it printed are journaled in
        the SAME transaction (one ``inject`` row each, and one ``retrieval_shows`` bump each):
        a claim and its showing commit or vanish together, with no extra commit on the Read path.
        # see design/superpowers/specs/2026-10-03-records-at-the-point-of-reading-design.md (D2)
        # and design/superpowers/specs/2026-10-11-hook-showings-journal-design.md (D3)"""
        params = (prefix + rel_path, value, prefix, prefix + _KEY_RANGE_END, cap)

        def claim(conn: sqlite3.Connection) -> bool:
            if conn.execute(CLAIM_FILE_SQL, params).rowcount != 1:
                return False
            if shown is not None:
                self._journal_shows(
                    conn,
                    shown.session_id,
                    INJECT_KIND,
                    shown.agent,
                    [(rel_path, rid) for rid in shown.record_ids],
                )
            return True

        return self._in_transaction(claim)

    @staticmethod
    def _journal_shows(
        conn: sqlite3.Connection,
        session_id: str,
        kind: str,
        agent: str | None,
        rows: Sequence[tuple[str, str]],
    ) -> None:
        """One journal row and one shows bump per ``(file, record id)`` row, on ``conn``."""
        now = datetime.now(UTC).isoformat()
        conn.executemany(
            INSERT_EVENT_SQL, [(session_id, now, kind, rel, rid, agent) for rel, rid in rows]
        )
        conn.executemany(SHOW_UPSERT_SQL, [(rid, now) for _, rid in rows])

    def record_shows(self, session_id: str, rows: Sequence[tuple[str, str]]) -> None:
        """Journal the records a subagent's brief printed: one ``inject_brief`` row (``agent``
        NULL, the recipient being unknown at spawn) and one shows bump per ``(file, record id)``
        row, in one transaction of their own (the brief claims nothing to join).
        # see design/superpowers/specs/2026-10-11-hook-showings-journal-design.md (D4)"""
        if rows:
            self._in_transaction(
                lambda conn: self._journal_shows(conn, session_id, INJECT_BRIEF_KIND, None, rows)
            )

    def record_touch(self, session_id: str, path: str, tool: str, agent: str | None = None) -> None:
        """Append one ``touch`` row; the same insert as ``Store.record_touch``."""
        self._write(
            INSERT_EVENT_SQL,
            (session_id, datetime.now(UTC).isoformat(), "touch", path, tool, agent),
        )

    # -- reads ---------------------------------------------------------------------------------

    def memory_counts(self) -> tuple[int, int, int]:
        """``(records, files, mistakes)``: what a ``get_task_context(files=[...])`` could hand an
        agent, for the SubagentStart brief.

        A record counts when it is accepted, or proposed and inside its surfacing window (never
        under ``SIDEGRAPH_UNRATIFIED=off``), so a superseded, rejected or deprecated one does
        not; when its ``valid_to`` is unset or in the future; and when a live or degraded
        binding joins it to an entity whose descriptor names a file.
        ``files`` is the number of distinct files those bindings point at, and ``mistakes`` the
        records among them of a mistake kind. A row that fails to parse is skipped on its own.

        A new store or canonical reload leaves placeholder statuses; withhold counts until
        sync certifies them. The flag and binding rows share one statement snapshot.
        see design/superpowers/specs/2026-10-03-subagent-start-brief-design.md (D2)
        """
        now = datetime.now(UTC)
        file_of: dict[str, str] = {}
        for entity_id, data in self._conn.execute("SELECT entity_id, data FROM entities"):
            try:
                descriptor = json.loads(data).get("descriptor")
                path = descriptor.get("file_path") if descriptor else None
            except Exception:
                continue
            if path:
                file_of[entity_id] = path

        surfacing: dict[str, str] = {}  # record id -> kind, for the records that may surface
        for (data,) in self._conn.execute("SELECT data FROM decisions"):
            try:
                decision = json.loads(data)
                status = decision["status"]
                valid_to = decision.get("valid_to")
                if valid_to is not None and datetime.fromisoformat(valid_to) <= now:
                    continue
                if status == _PROPOSED:
                    if not proposal_surfaces(datetime.fromisoformat(decision["valid_from"])):
                        continue
                elif status != _ACCEPTED:
                    continue
                surfacing[decision["id"]] = decision["kind"]
            except Exception:
                continue

        records: set[str] = set()
        files: set[str] = set()
        for record_id, entity_id, data in self._conn.execute(
            "SELECT record_id, entity_id, data FROM anchor_bindings "
            "WHERE NOT EXISTS (SELECT 1 FROM meta WHERE key = ? AND value = '1')",
            (VOLATILE_STALE_KEY,),
        ):
            if record_id not in surfacing or entity_id not in file_of:
                continue
            try:
                status = json.loads(data).get("status")
            except Exception:
                continue
            if status in ("live", "degraded"):
                records.add(record_id)
                files.add(file_of[entity_id])
        mistakes = sum(1 for record_id in records if surfacing[record_id] in MISTAKE_KINDS)
        return len(records), len(files), mistakes

    def anchored_files(self) -> dict[str, list[str]]:
        """``{file: [entity ids]}`` for every file a concrete entity's descriptor names, from ONE
        pass over ``entities``. A subagent's brief is matched against all of them (the suffix
        fallback of ``text_paths``). A row that fails to parse is skipped on its own, never the
        whole scan.
        # see design/superpowers/specs/2026-10-03-records-in-subagent-briefs-design.md (D2)"""
        found: dict[str, list[str]] = {}
        for (data,) in self._conn.execute("SELECT data FROM entities").fetchall():
            try:
                entity = json.loads(data)
                descriptor = entity.get("descriptor")
                if entity.get("kind") != _CONCRETE or not descriptor:
                    continue
                path = descriptor.get("file_path")
                if path:
                    found.setdefault(path, []).append(entity["entity_id"])
            except Exception:
                continue
        return found

    def anchored_entities(self, rel_paths: Iterable[str]) -> dict[str, list[str]]:
        """``{file: [entity ids]}`` for the files among ``rel_paths`` that a concrete entity's
        descriptor names, from ONE pass over ``entities`` however many files are asked (a Bash
        line can name several). A file with no entity is absent. A row that fails to parse is
        skipped on its own, never the whole scan.
        # see design/superpowers/specs/2026-10-03-records-at-the-point-of-reading-design.md (D5)"""
        wanted = set(rel_paths)
        if not wanted:
            return {}
        return {path: ids for path, ids in self.anchored_files().items() if path in wanted}

    def records_for(self, entity_ids: Sequence[str]) -> list[Record]:
        """The live decisions bound to ``entity_ids``, in the order a reader should meet them:
        accepted mistakes, accepted rest, proposed last, newest first within each bucket.

        A decision counts once however many of the entities it is bound to. A proposal appears
        only inside its surfacing window (``store_layout.proposal_surfaces``) and is marked
        ``proposed``; a superseded, rejected or expired decision, and one reached only through
        an orphaned binding, does not appear. A row that fails to parse is skipped on its own.
        This is the order of the model layer's per-file list, which differs from
        ``get_task_context(files=[f])`` (that one orders per entity).
        Withhold selection after canonical reload until sync certifies binding statuses.
        # see design/superpowers/specs/2026-10-03-records-at-the-point-of-reading-design.md (D1)
        """
        now = datetime.now(UTC)
        mistakes: list[tuple[dict, datetime]] = []
        rest: list[tuple[dict, datetime]] = []
        seen: set[str] = set()
        for entity_id in entity_ids:
            try:
                bound = self._decisions_bound_to(entity_id, now)
            except Exception:
                continue
            for decision, valid_from in bound:
                if decision["id"] in seen:
                    continue
                seen.add(decision["id"])
                bucket = mistakes if decision["kind"] in MISTAKE_KINDS else rest
                bucket.append((decision, valid_from))

        def newest_first(items: list[tuple[dict, datetime]]) -> list[tuple[dict, datetime]]:
            return sorted(items, key=lambda item: item[1], reverse=True)

        def accepted(items: list[tuple[dict, datetime]]) -> list[tuple[dict, datetime]]:
            return [i for i in items if i[0]["status"] == _ACCEPTED]

        def proposed(items: list[tuple[dict, datetime]]) -> list[tuple[dict, datetime]]:
            return [i for i in items if i[0]["status"] == _PROPOSED and proposal_surfaces(i[1])]

        ordered = newest_first(accepted(mistakes)) + newest_first(accepted(rest))
        proposals = newest_first([*proposed(mistakes), *proposed(rest)])
        return [Record(d, False) for d, _ in ordered] + [Record(d, True) for d, _ in proposals]

    def _decisions_bound_to(self, entity_id: str, now: datetime) -> list[tuple[dict, datetime]]:
        """Live decisions bound to ``entity_id`` by a live or degraded binding, each with its
        parsed ``valid_from``. Computed-state parity: ``Store.valid_decisions_for_entity``.
        Unlike that Store helper, reset statuses after reload select nothing until sync;
        the status rows and their certification flag share one statement snapshot.
        """
        out: list[tuple[dict, datetime]] = []
        bindings = self._conn.execute(
            "SELECT data FROM anchor_bindings WHERE entity_id = ? "
            "AND NOT EXISTS (SELECT 1 FROM meta WHERE key = ? AND value = '1')",
            (entity_id, VOLATILE_STALE_KEY),
        ).fetchall()
        for (bdata,) in bindings:
            binding = json.loads(bdata)
            if binding.get("status") not in ("live", "degraded"):
                continue
            row = self._conn.execute(
                "SELECT data FROM decisions WHERE id = ?", (binding["record_id"],)
            ).fetchone()
            if row is None:
                continue
            decision = json.loads(row[0])
            if _is_live(decision, now):
                out.append((decision, datetime.fromisoformat(decision["valid_from"])))
        return out
