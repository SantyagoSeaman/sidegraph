"""Owned, append-only decision store — git-native canonical files + a derived local index.

The store is a **repo-committed sidecar** — never the engine's ``graph.json`` (which is
read-only input and regenerated on every commit). It enforces the write-path invariants
(see CLAUDE.md and ``docs/concepts/data-model.md``):

- append-only: no hard deletes; reversal = ``valid_to`` + a new ``supersedes`` record;
- ``valid_to >= valid_from`` (also checked in the Pydantic model);
- a ``superseded`` decision must have a successor;
- every :class:`AnchorBinding` references an existing :class:`Entity`;
- provenance is always present;
- the committed store is **sync-clean**: a graph rebuild must never produce a git diff.

See ``docs/reference/store-format.md`` for the full layout reference.
A single repo-committed SQLite file cannot be merged by git (two branches ratifying
different decisions collide on one binary blob with no meaningful resolution), so the
canonical, git-committed record is now **file-per-record JSON** under ``self.path``::

    decisions/<ulid>.json   full Decision row (status/valid_to included — human facts)
    facts/<ulid>.json       full Fact row (status/valid_to included — non-derivable facts)
    domains/<ulid>.json     Domain row MINUS the volatile `communities` field
    entities/<ulid>.json    identity ONLY: entity_id, canonical_name, kind, descriptor
    bindings/<decision_ulid>.json   that decision's anchors: [{entity_id, tier, relation,
                             weight}] — NO status
    initiatives/<ulid>.json full Initiative row (no volatile fields on this model)
    index.db                DERIVED, gitignored: every table below plus the volatile
                             fields (entity last_seen_*, binding status, domain
                             communities, capture ledger, toc_cache, schema/digest meta)

``index.db`` keeps the exact same tables/queries this store has always used — reads are
unchanged. Writes to a canonical record update the matching file (atomic: tmp + ``os.replace``)
**and** the index, under the same lock. A write that touches ONLY a sanctioned volatile
field (an entity's engine mapping, a binding's status, a domain's ``communities`` — see
``sync.py``) touches ONLY the index; the committed files are untouched, so a
``sidegraph-sync`` pass never dirties git (Invariant #1, extended).

Thread-safety: fastmcp 3 dispatches sync ``@mcp.tool`` calls onto worker threads, so a
single process-wide :class:`Store` (see ``server.py``) is used from multiple threads. The
index connection is opened with ``check_same_thread=False`` and every use of it (reads and
writes alike) is serialized through an instance-level :class:`threading.RLock`. Iterator
methods fetch eagerly under the lock and yield afterwards, so the lock is never held across
a paused generator.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from . import store_layout
from .hot_index import CLAIM_META_SQL, GET_META_SQL, INSERT_EVENT_SQL
from .schema import (
    SCHEMA_VERSION,
    AnchorBinding,
    Decision,
    DecisionStatus,
    Descriptor,
    Domain,
    DomainStatus,
    Entity,
    EntityKind,
    Fact,
    Initiative,
    Scope,
    canonicalize,
    is_safe_record_id,
)
from .store_layout import symlinked_internals

# One definition, two consumers: __init__ bootstraps with executescript (not inside a
# transaction, so its implicit COMMIT is harmless), while _reload_index_from_canonical must
# issue these individually -- executescript would commit the rebuild's transaction out from
# under it and re-publish the DROPs (design D2, measured).
_SCHEMA_STATEMENTS: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
)""",
    """CREATE TABLE IF NOT EXISTS entities (
    entity_id TEXT PRIMARY KEY,
    canonical_name TEXT NOT NULL,
    data TEXT NOT NULL
)""",
    """CREATE TABLE IF NOT EXISTS decisions (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    supersedes TEXT,
    data TEXT NOT NULL
)""",
    """CREATE TABLE IF NOT EXISTS facts (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    supersedes TEXT,
    data TEXT NOT NULL
)""",
    """CREATE TABLE IF NOT EXISTS anchor_bindings (
    record_id TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    data TEXT NOT NULL,
    PRIMARY KEY (record_id, entity_id)
)""",
    # The key above leads with record_id, so a lookup by entity_id alone (every
    # `bindings_for_entity`, and `build_toc` makes one per entity in a domain's communities)
    # scanned the whole table: 0.115 s of a 0.149 s TOC build on a real 15-domain store.
    # Derived, like the table: it comes back with it on a rebuild and on any open.
    # see design/superpowers/specs/2026-10-01-session-start-text-design.md (D2b)
    "CREATE INDEX IF NOT EXISTS idx_anchor_bindings_entity ON anchor_bindings (entity_id)",
    """CREATE TABLE IF NOT EXISTS initiatives (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    data TEXT NOT NULL
)""",
    """CREATE TABLE IF NOT EXISTS capture_sessions (
    session_id TEXT PRIMARY KEY,
    captured_at TEXT NOT NULL
)""",
    """CREATE TABLE IF NOT EXISTS domains (
    domain_id TEXT PRIMARY KEY,
    slug TEXT NOT NULL,
    status TEXT NOT NULL,
    supersedes TEXT,
    data TEXT NOT NULL
)""",
    """CREATE TABLE IF NOT EXISTS retrieval_shows (
    record_id     TEXT PRIMARY KEY,
    shows         INTEGER NOT NULL,
    last_shown_at TEXT NOT NULL
)""",
    """CREATE TABLE IF NOT EXISTS retrieval_seeds (
    seed         TEXT PRIMARY KEY,
    queries      INTEGER NOT NULL,
    last_seen_at TEXT NOT NULL
)""",
    """CREATE TABLE IF NOT EXISTS retrieval_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    at         TEXT NOT NULL,
    kind       TEXT NOT NULL,
    key        TEXT NOT NULL,
    detail     TEXT,
    agent      TEXT
)""",
    """CREATE INDEX IF NOT EXISTS idx_retrieval_events_session
    ON retrieval_events (session_id)""",
    """CREATE TABLE IF NOT EXISTS canonical_stat (
    subdir   TEXT NOT NULL,
    stem     TEXT NOT NULL,
    size     INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    PRIMARY KEY (subdir, stem)
)""",
    # A journal like retrieval_events, and deliberately NOT in the DROP list of
    # _reload_index_from_canonical: it survives a `git pull` by absence from that list
    # (usage-stats spec D4), so history is not lost every time canonical files change.
    """CREATE TABLE IF NOT EXISTS render_events (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id         TEXT NOT NULL,
    at                 TEXT NOT NULL,
    intent             TEXT,
    selected           INTEGER NOT NULL,
    emitted            INTEGER NOT NULL,
    degraded           INTEGER NOT NULL,
    dropped_for_budget INTEGER NOT NULL,
    chars_used         INTEGER NOT NULL,
    had_rejected       INTEGER NOT NULL,
    had_superseded     INTEGER NOT NULL
)""",
    """CREATE INDEX IF NOT EXISTS idx_render_events_session
    ON render_events (session_id)""",
)

_SCHEMA_SQL = ";\n".join(_SCHEMA_STATEMENTS) + ";\n"

# Legacy (pre-0.4.0) single-SQLite-file stores that are safe to migrate forward on open (see
# ``Store._migrate_legacy`` and docs/reference/store-format.md#migration-to-040-from-02x-and-03x).
# Anything else (unknown/garbled/future versions) is a hard rejection — migration
# tooling beyond this one forward step remains deferred (CLAUDE.md invariant #3).
_MIGRATABLE_SCHEMA_VERSIONS = frozenset({"0.2.0", "0.3.0"})

# Stores stamped with these versions have fully forward-compatible canonical files;
# on open we rebuild the derived index (which re-stamps SCHEMA_VERSION) instead of
# raising. see design/superpowers/specs/2026-07-10-facts-layer-design.md
# "0.5.0" added for derived community bindings (see design/superpowers/specs/
# 2026-07-10-derived-community-bindings-design.md): a 0.5.0 store's canonical files are
# forward-compatible as-is (a reload just tolerates any pre-existing community entity/
# binding entries — see _is_derived_entity and _write_bindings_canonical_for_record).
_RELOADABLE_SCHEMA_VERSIONS = frozenset({"0.4.0", "0.5.0"})

# The record directories, the archive directory and the marker file names are defined in
# store_layout.py (the hook hot path needs them without pydantic); bound here under the names
# this module has always used. See there for what each one is.
_CANONICAL_SUBDIRS = store_layout.CANONICAL_SUBDIRS
_ARCHIVE_SUBDIR = store_layout.ARCHIVE_SUBDIR
_FORMAT_MARKER_NAME = store_layout.FORMAT_MARKER_NAME
_GITIGNORE_NAME = store_layout.GITIGNORE_NAME
_STAMPING_MARKER_NAME = store_layout.STAMPING_MARKER_NAME

# Terminal = a status after which append-only rules guarantee the record can never change
# again (see CLAUDE.md invariant #2 and design §7). PROPOSED/ACCEPTED decisions can still be
# ratified/dropped/superseded; PROPOSED/ACCEPTED domains can still be ratified/superseded —
# neither is ever a compaction candidate. DEPRECATED has no write path today (dead enum
# value — nothing in this codebase ever sets it) but is terminal by the same definition, so
# it is included for forward-compat rather than silently left hot forever the day something
# does start setting it.
_TERMINAL_DECISION_STATUSES = frozenset(
    {DecisionStatus.SUPERSEDED, DecisionStatus.REJECTED, DecisionStatus.DEPRECATED}
)
_TERMINAL_DOMAIN_STATUSES = frozenset({DomainStatus.SUPERSEDED, DomainStatus.DROPPED})

# Committed format marker: a one-line ``<path>/format`` file (``sidegraph-store <version>``)
# written at layout creation and migration, checked on every open — see
# ``Store._ensure_format_marker``.
_FORMAT_MARKER_PREFIX = "sidegraph-store "

# Committed layout convenience (design §1/§5): ``<path>/.gitignore`` ignoring the derived
# index (and its sqlite WAL/SHM sidecars via the ``index.db*`` glob) and any crash-debris
# ``*.tmp`` — see ``Store._ensure_gitignore``.
_GITIGNORE_CONTENT = "index.db*\n*.tmp\n"

# ``Store.record_types`` binds each id twice (one ``IN`` list per table), so a chunk of 400 is
# 800 variables: under the 999 of an old SQLite build. A real entity has far fewer bindings.
_RECORD_TYPES_CHUNK = 400

# Glob patterns matching the store's OWN root-level tmp artifacts (the format marker, the
# stamping marker, and .gitignore -- everything else lives under the canonical subdirs,
# _CANONICAL_SUBDIRS). Matches ``_atomic_write_text_race_tolerant``'s per-attempt UNIQUE tmp
# naming (``<name>.<random>.tmp``, never a fixed ``<name>.tmp`` -- see that function's
# docstring). Scoped by a real prefix/suffix pattern, not a blanket ``*.tmp`` glob
# (review Minor-3): the store root is also a directory a user may keep other files in (a
# build artifact, a scratch file, ...) -- sweeping every ``*.tmp`` there on open would
# delete something that was never ours.
_ROOT_TMP_GLOB_PATTERNS = (
    f"{_FORMAT_MARKER_NAME}.*.tmp",
    f"{_GITIGNORE_NAME}.*.tmp",
    f"{_STAMPING_MARKER_NAME}.*.tmp",
)

# Attempts _atomic_write_text_race_tolerant makes before giving up and re-raising. Each
# concurrent opener sweeps stale tmp debris exactly once, at its own open start (see
# Store._sweep_stale_tmp_files) -- so a bounded handful of retries is enough for the set
# of "still mid-sweep" openers to converge to empty and a write to finally stick.
_RACE_TOLERANT_ATTEMPTS = 3

# How old a *.tmp file must be before the open-time sweep will remove it. The sweep cannot
# tell crash debris from another process's in-flight buffer, and it used to delete live ones
# (design D4): a buffer exists for microseconds, debris is old by definition, so age is the
# one signal that separates them without a lock or a liveness check. The trade: debris from a
# crash less than a minute ago survives until the next open after that. Harmless -- the sweep
# exists to stop unbounded accumulation, not to be prompt.
_TMP_DEBRIS_MIN_AGE_SECONDS = 60.0

# A legacy migration takes seconds, so a migration sentinel younger than this is a LIVE
# migration by another process, never a crash's leftover (design D10): the open-time check
# refuses instead of cleaning up under it.
_LIVE_MIGRATION_SECONDS = 60.0

# Attempts Store._write_archive_segment makes to publish a new segment under an unused
# name before giving up (review Important-1). Higher than _RACE_TOLERANT_ATTEMPTS above:
# that helper's racers are all writing IDENTICAL content and converge once the single
# winner lands, but two concurrent compacts can pick genuinely DIFFERENT terminal-record
# sets (different content, different hash suffix -- see _ARCHIVE_SUBDIR's docstring
# amendment) that only collide on the human-readable <date>-<seq> prefix; each retry here
# can therefore collide with a DIFFERENT racer, not just re-attempt against one winner.
_ARCHIVE_SEGMENT_PUBLISH_ATTEMPTS = 10

# Meta key: set to "1" whenever __init__ reloads the index from canonical files (missing
# index, digest mismatch, or a fresh migration) and cleared by a completed `sidegraph-sync`
# pass (see sync.py) — see design §3.
VOLATILE_STALE_KEY = "volatile_stale"

_CANONICAL_DIGEST_KEY = "canonical_digest"

# Meta key: JSON list of the canonical files the last reload left out: a record-identity
# problem (design/superpowers/specs/2026-09-29-record-identity-design.md D3, D4), a parse error
# (a file that does not parse, validate or serialise) or an archive segment with lines it could
# not read (design/superpowers/specs/2026-10-03-store-survives-a-bad-file-design.md D1, D4).
# Each entry is {"path", "reason", "size", "mtime_ns"}; deleted when empty. ``__init__`` repeats
# the warning from it on every open and reloads once if a listed file changed or vanished.
SKIPPED_CANONICAL_KEY = "skipped_canonical_files"

# Skip-list reasons for a file that cannot be read as a record (design/superpowers/specs/
# 2026-10-03-store-survives-a-bad-file-design.md D2): the words ``sidegraph-verify`` uses
# (``verify.PARSE_ERROR``, ``verify.BAD_ARCHIVE_SEGMENT``) for the same files.
SKIP_PARSE_ERROR = "parse-error"
SKIP_BAD_ARCHIVE_SEGMENT = "bad-archive-segment"

# What makes ONE file unreadable as a record, as opposed to the store unreadable: invalid JSON,
# invalid UTF-8, a model that rejects the payload and a payload that cannot be serialised are
# all ``ValueError``s on this interpreter; JSON nested deeper than the stack is a
# ``RecursionError``. An ``OSError`` is deliberately not here: it can be transient.
_UNPARSEABLE = (ValueError, RecursionError)

# Folded into the canonical digest (D5): an index certified by code that predates the
# identity rule mismatches once, reloads through the rule and is re-certified — otherwise a
# poisoned index with a matching digest would keep serving a crafted id on the fast path.
_IDENTITY_RULE = "record-identity-v1"


def _atomic_write_text(path: Path, text: str) -> os.stat_result:
    """Write ``text`` to ``path`` atomically (tmp + ``os.replace``, same directory so the
    replace is same-filesystem). A crash strictly between the tmp write and the replace
    leaves the ORIGINAL file (or its absence) intact — see design §2/§Testing. Writes the
    canonical records (via ``_atomic_write_json`` below); the format marker and
    ``.gitignore`` do NOT come through here — they use
    ``_atomic_write_text_race_tolerant``, which has its own tmp+replace implementation.

    The tmp name is per-call UNIQUE (``<name>.<pid>.<random>.tmp``), matching
    ``_atomic_write_text_race_tolerant`` (design D5): a FIXED ``<name>.tmp`` means two
    writers of the SAME target share one inode, so one's ``open(..., "w")`` truncation can
    land inside the other's ``os.replace`` window and atomically install an EMPTY file as
    the "committed" canonical record — silent content corruption, not a crash. That
    corruption class is already known-real on this project (it is why the race-tolerant
    variant has unique names); this closes the same hole for every canonical record write.
    The name still ends in ``.tmp``, so the sweep's globs (``_sweep_stale_tmp_files``) and
    the store's own ``.gitignore`` (``*.tmp``) still cover it.

    Returns the TMP file's stat, taken BEFORE the replace (digest-integrity design §3, the
    rule the whole ``canonical_stat`` mechanism rests on): the stat a caller records must
    describe the content it is about to publish, captured at or before the write becomes
    visible, never after — a stat taken after the replace could describe some OTHER
    writer's file that replaced this one in the interim (two sessions rewriting the same
    record, replace order inverted against commit order). ``os.replace`` preserves the
    inode's ``(size, mtime_ns)`` (POSIX; measured), so the tmp's pre-replace stat is exactly
    the published file's stat too — no second ``stat()`` call needed, and no window in
    which to observe someone else's replace instead of this one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex[:12]}.tmp")
    tmp.write_text(text, encoding="utf-8")
    st = tmp.stat()
    os.replace(tmp, path)
    return st


def _record_file(root: Path, subdir: str, record_id: str) -> Path:
    """``root/<subdir>/<record_id>.json``, refusing an id that is not a single path segment.

    Every canonical file's name IS its record's id, so an id like ``../../x`` would be a
    path outside the store (see design/superpowers/specs/2026-09-29-record-identity-design.md
    D1, D2). The one place an id becomes a path: writes, reads and deletes all come here."""
    if not is_safe_record_id(record_id):
        raise ValueError(f"unsafe record id {record_id!r}: must be a single path segment")
    return root / subdir / f"{record_id}.json"


def _record_identity_problem(data: object, id_field: str, stem: str) -> str | None:
    """Why the canonical file named ``<stem>.json`` cannot be indexed as a record, or None.

    A file is a record only when its JSON is an object whose ``id_field`` is a safe id equal
    to the filename stem (design/superpowers/specs/2026-09-29-record-identity-design.md D3,
    D12): the id then IS the filename, and a crafted or missing id can neither become a
    path nor mint a fresh ULID on every rebuild."""
    if not isinstance(data, dict):
        return "not a JSON object"
    if id_field not in data:
        return f"missing {id_field}"
    value = data[id_field]
    if not is_safe_record_id(value):
        return f"unsafe {id_field} {value!r}"
    if value != stem:
        return f"{id_field} {value!r} does not match the filename"
    return None


def _parse_record_file[M: BaseModel](
    path: Path, id_field: str, model: type[M]
) -> tuple[M | None, str | None]:
    """Step 1 of the reload for one record file: read, parse, check the identity, validate and
    serialise it. Returns ``(record, None)``, or ``(None, reason)`` when the file must be left
    out: ``reason`` is the identity problem, or ``parse-error`` for a file that does not parse,
    validate or serialise (design/superpowers/specs/2026-10-03-store-survives-a-bad-file-design.md
    D1, D2). The serialisation check makes a payload the index write could not store fail
    here, where it skips one file, and not inside the unguarded index write. An ``OSError`` on
    the read propagates."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        problem = _record_identity_problem(data, id_field, path.stem)
        if problem is not None:
            return None, problem
        record = model.model_validate(data)
        record.model_dump_json()
    except _UNPARSEABLE:
        return None, SKIP_PARSE_ERROR
    return record, None


def _parse_bindings_file(path: Path) -> list[AnchorBinding] | None:
    """Step 1 of the reload for one ``bindings/<record id>.json``: a JSON list of objects, every
    item a valid ``AnchorBinding``, all of them serialisable. ``None`` means the whole file is
    left out (``parse-error``): all items are checked before any is returned, so a half-valid
    file never leaves half of its bindings in the index
    (design/superpowers/specs/2026-10-03-store-survives-a-bad-file-design.md D3)."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, list) or not all(isinstance(item, dict) for item in raw):
            return None
        bindings = [AnchorBinding.model_validate({**item, "record_id": path.stem}) for item in raw]
        for binding in bindings:
            binding.model_dump_json()
    except _UNPARSEABLE:
        return None
    return bindings


def _atomic_write_json(path: Path, obj: object) -> os.stat_result:
    """Write ``obj`` as pretty, stably-sorted JSON to ``path`` atomically (see
    ``_atomic_write_text``, including the returned pre-replace stat).

    ``ensure_ascii=False``: non-ASCII decision prose (Cyrillic, em-dashes, ...) must appear
    verbatim in the committed file, not as ``\\uXXXX`` escapes — these files are meant to be
    read and diffed by humans in a normal git workflow."""
    text = json.dumps(obj, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    return _atomic_write_text(path, text)


def _atomic_write_text_race_tolerant(path: Path, text: str) -> None:
    """Like ``_atomic_write_text``, but safe for the specific way ``Store``'s FIRST-OPEN
    writes (the format marker, ``.gitignore``) can race across THREADS (fastmcp's
    worker-thread dispatch) or genuinely separate PROCESSES (a hook racing a long-lived
    MCP server, or two CLI invocations opening the same fresh directory at once) — review
    Important-2a plus a residual finding a cross-process stress probe surfaced on top of
    it. Both writers always produce IDENTICAL content (the same running
    ``SCHEMA_VERSION`` / the same fixed ``.gitignore`` body), so there is never a real
    disagreement to resolve — only races over WHO gets to write it.

    Unlike ``_atomic_write_text``, each attempt here writes to a per-attempt UNIQUE tmp
    filename (``<name>.<random>.tmp``, via ``uuid4``), never a fixed ``<name>.tmp``. This
    matters beyond just avoiding a name clash: a FIXED shared tmp name lets one writer's
    ``open(..., "w")`` (which truncates in place) land on the SAME inode ANOTHER writer's
    ``os.replace`` is about to consume — the second writer's truncate can zero out the
    first writer's already-written bytes a moment before that first writer's replace call
    fires, atomically installing an EMPTY file as the "committed" marker (silent content
    corruption, not a crash — this is what a cross-process stress probe caught: an
    ``unrecognized store format marker ''`` on a subsequent open). A unique tmp name per
    attempt makes that impossible: no two attempts, in this process or any other, ever
    write through the same path, so nothing can be truncated out from under a writer that
    still owns its own tmp file.

    What a unique name does NOT prevent: another process's open-time debris SWEEP
    (``Store._sweep_stale_tmp_files``) deleting THIS attempt's tmp file before its
    ``os.replace`` runs — the sweep has no way to tell a truly stale leftover from a live
    in-flight buffer, unique name or not. That still surfaces as ``FileNotFoundError`` on
    ``os.replace``. If the target already exists by then, some attempt (ours or a
    concurrent one) already landed the identical content, so it's a success, not a
    failure. If not, the WHOLE cycle (a fresh unique tmp, a fresh write, a fresh replace)
    is retried, up to ``_RACE_TOLERANT_ATTEMPTS`` times: every opener sweeps only once, at
    its own open start, so the set of "still mid-sweep" openers shrinks to empty within a
    bounded number of rounds and a later attempt sticks. Exhausting every attempt
    re-raises the last ``FileNotFoundError`` — a genuine, non-race failure (e.g. the
    store's parent directory itself disappeared) must not be swallowed forever.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    last_error: FileNotFoundError | None = None
    for _ in range(_RACE_TOLERANT_ATTEMPTS):
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex[:12]}.tmp")
        tmp.write_text(text, encoding="utf-8")
        try:
            os.replace(tmp, path)
            return
        except FileNotFoundError as e:
            if path.is_file():
                return
            last_error = e
    assert last_error is not None  # the loop always sets it before falling through
    raise last_error


def refuse_symlinked_internals(store_path: Path) -> None:
    """Raise ``ValueError`` naming the first symlinked store-owned entry, if any: a symlinked
    ``decisions/`` would send every record write to the link's target, and a symlinked
    ``index.db`` would make SQLite write there.
    # see design/superpowers/specs/2026-09-29-store-symlinks-and-bootstrap-guards-design.md D1"""
    found = symlinked_internals(store_path)
    if found:
        raise ValueError(
            f"{store_path / found[0]} inside the store is a symlink; the store refuses to use "
            "a symlinked directory or store file inside itself. Replace it with a real one."
        )


def _store_has_any_records(path: Path) -> bool:
    """True iff the store rooted at ``path`` holds a record of ANY kind — the complete
    inventory of places one can live, per this module's own docstring layout: a hot file
    under one of the six ``_CANONICAL_SUBDIRS`` (``*.json``), or an archive segment under
    ``_ARCHIVE_SUBDIR`` (``*.jsonl``, ``Store.compact``'s output). Both are checked because
    ``Store.compact`` can move EVERY decision/domain out of its hot directory into an
    archive segment — a store in that state has zero hot files but is emphatically not
    new; only ``archive/`` still proves it. Used solely to decide whether
    ``Store._ensure_stamping_marker`` may write its creation marker: that marker must
    never appear on a store that already had a record, by any route, before this open."""
    for sub in _CANONICAL_SUBDIRS:
        d = path / sub
        if d.is_dir() and any(d.glob("*.json")):
            return True
    archive_dir = path / _ARCHIVE_SUBDIR
    return archive_dir.is_dir() and any(archive_dir.glob("*.jsonl"))


def _entity_identity_payload(entity: Entity) -> dict:
    """The canonical (git-committed) subset of an Entity: identity only, never the
    engine-mapping fields (``last_seen_*``) sync refreshes — see design §1."""
    return {
        "entity_id": entity.entity_id,
        "canonical_name": entity.canonical_name,
        "kind": entity.kind.value,
        "descriptor": entity.descriptor.model_dump() if entity.descriptor else None,
    }


def _domain_canonical_payload(domain: Domain) -> dict:
    """The canonical (git-committed) subset of a Domain: everything except the volatile
    ``communities`` mapping, which ``sync.py``'s ``refresh_domain_communities`` refreshes
    index-only — see design §1."""
    data = domain.model_dump(mode="json")
    data.pop("communities", None)
    return data


def _is_derived_entity(entity: Entity) -> bool:
    """True iff ``entity`` is engine-volatile DERIVED state that must never reach a
    canonical (git-committed) file — currently: Tier-1 community abstract entities
    (``canonical_name`` starting with ``"community:"``). Communities are snapshot labels
    Leiden renumbers on every rebuild, not stable identities — see design/superpowers/specs/
    2026-07-10-derived-community-bindings-design.md. The scope condition is the ENTITY, not
    the tier number: ``domain:*``/``tag:*``/initiative abstract entities stay canonical."""
    return entity.kind == EntityKind.ABSTRACT and entity.canonical_name.startswith("community:")


def _binding_identity_payload(binding: AnchorBinding) -> dict:
    """The canonical (git-committed) subset of an AnchorBinding: the anchor itself, never
    ``status`` (live/degraded/orphaned), which sync flips index-only — see design §1.
    ``record_id`` is implied by the enclosing ``bindings/<record_id>.json`` file, so it
    is not part of this payload either."""
    return {
        "entity_id": binding.entity_id,
        "tier": binding.tier,
        "relation": binding.relation,
        "weight": binding.weight,
    }


def _archive_record_line(record_type: Literal["decision", "domain"], payload: dict) -> str:
    """One ``archive/<date>-<seq>.jsonl`` line: the record's canonical payload (exactly
    what its hot file contained — see ``_write_decision_canonical`` /
    ``_domain_canonical_payload``) plus a ``record_type`` discriminator so the loader knows
    which model to reconstruct (design §7). Same ``json.dumps`` settings as
    ``_atomic_write_json`` (``sort_keys=True``, ``ensure_ascii=False``) minus ``indent`` —
    flattened onto one line, since a segment is one record per line, not one record per
    file."""
    return json.dumps({"record_type": record_type, **payload}, sort_keys=True, ensure_ascii=False)


def _warn_hot_archive_mismatch(kind: str, record_id: str) -> None:
    """A ULID present in BOTH a hot canonical file and an archive segment with DIFFERENT
    content — terminal records are supposed to be immutable once archived, so this is
    corruption-shaped (e.g. a hand-edited archive segment, or a bug). Never destroy data:
    the hot file always wins (see ``_reload_index_from_canonical`` / ``Store.compact``);
    this only surfaces the disagreement for a human to investigate."""
    print(
        f"sidegraph: WARNING {kind} {record_id!r} exists in BOTH a hot canonical file and "
        "an archive segment with DIFFERENT content — archive segments are meant to be "
        "immutable; keeping the hot file and ignoring the stale archive copy. This is "
        "corruption-shaped (e.g. a hand-edited archive segment); investigate manually.",
        file=sys.stderr,
    )


def parse_skip_list(value: str) -> list[dict]:
    """The entries of a stored ``skipped_canonical_files`` meta value. Bad JSON, a value that is
    not a list and entries without a string ``path`` count as empty, never as an error: this is
    read by the store, the integrity check and ``sidegraph-doctor``, none of which may fail on
    it."""
    try:
        entries = json.loads(value)
    except ValueError:
        return []
    if not isinstance(entries, list):
        return []
    return [e for e in entries if isinstance(e, dict) and isinstance(e.get("path"), str)]


def skipped_segment_text(path: str) -> str:
    """What to tell a person about a skip-list entry for an archive segment: the sentence the
    stderr warning and the integrity notice share, so the two cannot drift apart
    (design/superpowers/specs/2026-10-03-store-survives-a-bad-file-design.md D7)."""
    return (
        f"archive segment {Path(path).name} has lines that could not be read; they are left "
        "out and the rest of it loaded. Restore it with git; never delete a segment."
    )


def skipped_refusal_text(subdir: str, stem: str) -> str:
    """The refusal for a write over ``<subdir>/<stem>.json`` while the reload has left it out:
    one text for the canonical writers' guard and for a caller that checks ``Store.is_skipped``
    first, so both give the person the same message."""
    return (
        f"refusing to overwrite {subdir}/{stem}.json: the last reload left it out of the index "
        "because it could not be read. Restore it with git, or fix it by hand, then reopen "
        "the store."
    )


def _warn_skipped_canonical(entries: list[dict]) -> None:
    """A reload skipped canonical files whose identity does not hold (see
    ``_record_identity_problem``) or that could not be read. Repeated on every open while the
    list is non-empty.

    Never says "remove": removing a record file breaks the append-only store, and removing an
    archive segment destroys every archived record in it, whose hot files are gone. A record
    file and an archive segment get one warning each, since the heal differs
    (design/superpowers/specs/2026-10-03-store-survives-a-bad-file-design.md D7)."""
    segments = [e for e in entries if e.get("reason") == SKIP_BAD_ARCHIVE_SEGMENT]
    files = [e for e in entries if e.get("reason") != SKIP_BAD_ARCHIVE_SEGMENT]
    if files:
        shown = "; ".join(f"{e['path']} ({e['reason']})" for e in files[:3])
        more = f" and {len(files) - 3} more" if len(files) > 3 else ""
        print(
            f"sidegraph: WARNING {len(files)} record file(s) were left out of the index: "
            f"{shown}{more}. Run `sidegraph-verify` to list them. Restore them with git, or "
            "fix them by hand; a newer Sidegraph version may have written them.",
            file=sys.stderr,
        )
    for e in segments:
        print(f"sidegraph: WARNING {skipped_segment_text(e['path'])}", file=sys.stderr)


_ARCHIVE_ID_FIELDS = {"decision": "id", "domain": "domain_id"}
_ARCHIVE_MODELS: dict[str, type[BaseModel]] = {"decision": Decision, "domain": Domain}


def _archive_line_problem(record_type: object, payload: dict) -> str | None:
    """Why an archive line cannot be indexed, or None. A line of an unrecognized (or
    non-string) ``record_type`` is fine here: it is skipped silently by the callers
    (forward-compat). Otherwise its id field must be present, a string and a safe filename
    stem. Shared with ``bootstrap.catalog`` so both readers skip the same lines."""
    id_field = _ARCHIVE_ID_FIELDS.get(record_type) if isinstance(record_type, str) else None
    if id_field is None:
        return None
    if id_field not in payload:
        return f"missing {id_field}"
    if not is_safe_record_id(payload[id_field]):
        return f"unsafe {id_field} {payload[id_field]!r}"
    return None


def _archive_payload_problem(record_type: object, payload: dict) -> str | None:
    """Why a well-formed archive line's payload cannot become a record, or None: the model
    rejects it, or it cannot be serialised for the index. A line of an unrecognized
    ``record_type`` has nothing to check (it is skipped silently by the callers). Only the
    tolerant read uses it, so that a line the reload would choke on at ``model_validate``
    is skipped with its segment's name known
    (design/superpowers/specs/2026-10-03-store-survives-a-bad-file-design.md D4)."""
    model = _ARCHIVE_MODELS.get(record_type) if isinstance(record_type, str) else None
    if model is None:
        return None
    try:
        model.model_validate(payload).model_dump_json()
    except _UNPARSEABLE as e:
        return f"its {record_type} does not validate ({type(e).__name__})"
    return None


def _warn_skipped_archive_line(segment: Path, reason: str) -> None:
    """An archive segment line whose record cannot be indexed: it is skipped and the rest
    of the segment loads (segments are immutable, so it is never rewritten)."""
    print(
        f"sidegraph: WARNING skipping a line of archive segment {segment.name}: {reason}.",
        file=sys.stderr,
    )


def _warn_domain_slug_conflict(slug: str, domain_ids: list[str]) -> None:
    """A slug held by more than one LIVE (proposed|accepted) domain at once — design §6's
    cross-branch race: two branches independently proposed/accepted a domain with the same
    slug; both files merge in cleanly (different ``domain_id``s, no file conflict), so
    nothing at write time ever catches this after the merge. ``find_domain_by_slug``
    still resolves deterministically in the meantime (accepted > proposed, newest first),
    so retrieval never breaks — but the duplicate itself is never auto-resolved (never
    guess which one a human meant to keep); this only surfaces it, same convention as
    ``_warn_hot_archive_mismatch``."""
    print(
        f"sidegraph: WARNING domain slug {slug!r} is held by {len(domain_ids)} live "
        f"domains ({', '.join(domain_ids)}) — likely a cross-branch merge race (design "
        "§6); drop one (sidegraph-ratify --drop <loser-id>), or supersede it under a "
        "different slug.",
        file=sys.stderr,
    )


def _parse_segment_seq(stem: str, date_str: str) -> int | None:
    """The ``<seq>`` component of an archive segment's filename stem, given its date.

    Tolerates both the current ``<date>-<seq>-<hash12>.jsonl`` shape and the original
    bare ``<date>-<seq>.jsonl`` shape (review Minor-4: the hash suffix was added after
    N5 shipped, to fix same-day cross-branch compacts colliding on filename — see
    ``_ARCHIVE_SUBDIR``'s docstring and design §7's amendment note. No released store
    ever wrote the bare shape, but nothing stops a hand-authored/older fixture from
    having one, and the loader must never choke on it). Returns ``None`` for a stem that
    doesn't start with ``<date_str>-`` or whose seq component isn't a plain integer."""
    prefix = f"{date_str}-"
    if not stem.startswith(prefix):
        return None
    rest = stem[len(prefix) :]
    seq_part = rest.split("-", 1)[0]
    return int(seq_part) if seq_part.isdigit() else None


def _hot_file_matches(path: Path, expected_payload: dict) -> bool | None:
    """Compare a hot canonical file's ON-DISK content against ``expected_payload`` (never
    trust an in-memory snapshot for a decision as destructive as unlinking a file — review
    Minor-6). Returns ``True``/``False`` if the file exists and does/doesn't match, or
    ``None`` if there is no hot file there at all (nothing to compare, nothing to
    remove)."""
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8")) == expected_payload


def _hot_file_skipped_by_reload(path: Path, id_field: str, listed: frozenset[str]) -> bool:
    """True when ``path`` exists and the reload left it out of the index: its path is in the
    skip list (``listed``, as ``<subdir>/<name>``), or it parses and fails the record identity
    rule. Such a file is not a second copy of the archived record, so the compact leftover
    comparison ignores it (design D3). A file that is neither listed nor parseable is not
    decided here: the comparison reports it as it always did.
    see design/superpowers/specs/2026-10-03-store-survives-a-bad-file-design.md D6"""
    if not path.is_file():
        return False
    if f"{path.parent.name}/{path.name}" in listed:
        return True
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return False
    return _record_identity_problem(data, id_field, path.stem) is not None


class CompactedRecord(BaseModel):
    """One line of ``sidegraph-compact``'s report / ``--dry-run`` listing — see
    :meth:`Store.compact`."""

    ulid: str
    kind: Literal["decision", "domain"]
    status: str
    title: str


class CompactReport(BaseModel):
    """Result of :meth:`Store.compact` (design §7) — mirrors the report-object convention
    other feature modules use (e.g. ``sync.py``'s ``RebindOutcome``)."""

    #: The new segment's path relative to the store root (e.g.
    #: ``"archive/2026-07-08-1-a1b2c3d4e5f6.jsonl"`` — ``<date>-<seq>-<hash12>``), or
    #: ``None`` when no NEW segment was written (nothing selected, or ``dry_run=True``).
    segment_path: str | None = None
    decisions_compacted: int = 0
    domains_compacted: int = 0
    #: TERMINAL decisions excluded because ``--older-than`` was given and either the
    #: decision hasn't been terminal long enough, or (defensive) its ``valid_to`` is
    #: unexpectedly unset. Decisions only — see ``domains_excluded_age_unknown`` below for
    #: the separate domain count (review Minor-7: the two have different root causes and
    #: read confusingly merged into one number — "too recent" vs. "no timestamp exists at
    #: all to check").
    skipped_age_filtered: int = 0
    #: TERMINAL domains excluded because ``--older-than`` was given. Always every eligible
    #: domain, unconditionally: unlike a decision's ``valid_to``, nothing on ``Domain``
    #: records when it entered a terminal status, so there is no age to even evaluate.
    domains_excluded_age_unknown: int = 0
    #: Hot files removed because they duplicated a record ALREADY in an existing archive
    #: segment (crash-window debris from a prior compact that wrote its segment but was
    #: interrupted before removing the hot file) — no new segment was written for these.
    cleaned_up_hot_files: int = 0
    items: list[CompactedRecord] = Field(default_factory=list)
    dry_run: bool = False

    @property
    def total_compacted(self) -> int:
        return self.decisions_compacted + self.domains_compacted


def _ratifier_identity(actor: str | None = None) -> str | None:
    """Best-effort `git config user.name` for the ratifier stamp (design D4), extended
    for auto-ratification (design D2/T17) with an explicit override.

    An explicit, non-blank ``actor`` — the auto-ratify stamp, ``"auto:<policy>"``
    (design D2) — wins outright: no git lookup, returned as-is. ``actor`` that is blank
    or whitespace-only is treated exactly like an absent one and falls through to the
    git lookup below, never stored verbatim: the caller can never chain an empty or
    whitespace string into a trust field, extending this function's existing principle
    (a wrong identity is worse than an absent one) to a caller-supplied value as well as
    a failed git lookup.

    None on ANY failure — no git, no configured name, sandboxed env, or a blank actor
    with no git identity behind it either. The stamp never guesses (the anchors'
    never-guess stance, applied to identity): a wrong or blank name in a trust field is
    worse than an absent one. Module-level so tests monkeypatch it.
    """
    if actor is not None and actor.strip():
        return actor

    import subprocess

    try:
        out = subprocess.run(
            ["git", "config", "user.name"], capture_output=True, text=True, timeout=5
        )
        name = out.stdout.strip()
        return name or None
    except Exception:
        return None


def _sentinel_age_seconds(sentinel: Path) -> float:
    """Seconds since the sentinel was written; infinite when its mtime cannot be read, so
    that an unreadable sentinel is treated as old (design D10)."""
    try:
        return time.time() - sentinel.stat().st_mtime
    except OSError:
        return float("inf")


class Store:
    """Thin persistence layer that owns the invariants. Use as a context manager.

    ``Store(path)`` accepts a directory (canonical layout; created if missing — the
    convention, default ``.sidegraph``) or a legacy single-file store (a bare ``*.db``
    path, or a directory containing an un-migrated ``decisions.db``): opening either
    triggers a one-time export to the canonical layout (see ``_migrate_legacy`` and design
    §4). This is a persistence swap, not an API change — every public method, invariant,
    and result shape is unchanged from the pre-0.4.0 single-file store.
    """

    def __init__(self, path: str | Path = ".sidegraph") -> None:
        raw = Path(path)
        self._lock = threading.RLock()
        # Depth counter for `_mutation` (design D1/D2): only the OUTERMOST `_mutation` scope
        # commits or rolls back -- see that method's docstring. Lives beside `_lock` since
        # the two are set up together and only ever mutate together.
        self._mutation_depth = 0
        self.path = raw

        # Before the legacy dispatch: `_migrate_legacy` would otherwise move records through
        # a symlinked subdirectory before anything refused it.
        if raw.is_dir():
            refuse_symlinked_internals(raw)

        # A bare-file migration (``Store("old.db")``) that was interrupted after its point of
        # no return leaves a sentinel beside the legacy file (design D10). A nameless path
        # such as ``Store(".")`` cannot be a legacy file and has no sentinel.
        sentinel = self._migration_sentinel_path(raw)
        if sentinel is not None and sentinel.is_file():
            self._check_migration_sentinel(raw, sentinel)

        if raw.is_file():
            self._reject_file_inside_a_store(raw)
            self._migrate_legacy(raw)
        else:
            legacy = raw / "decisions.db"
            if legacy.is_file():
                self._migrate_legacy(legacy)

        self._ensure_canonical_subdirs()
        self._sweep_stale_tmp_files()
        self._ensure_format_marker()
        self._ensure_stamping_marker()
        self._ensure_gitignore()
        self._conn = sqlite3.connect(str(self.path / "index.db"), check_same_thread=False)
        try:
            self._conn.row_factory = sqlite3.Row
            self._conn.executescript(_SCHEMA_SQL)
            self._migrate_retrieval_events_agent()
            self._refresh_freshness()
            self._warn_skipped_files()
        except BaseException:
            self._conn.close()
            raise

    def _migrate_retrieval_events_agent(self) -> None:
        """Add ``retrieval_events.agent`` to an index that predates it. Never fails the open.

        A fresh index gets the column from ``CREATE TABLE``; this reaches the ones built
        before it. The check is a read, so an up-to-date index pays no write lock. Two hook
        processes can both see the column missing and both ALTER: the loser's "duplicate
        column name" means the column is there, which is success. Any other failure (a locked
        or read-only index) is swallowed too: the column is a convenience for per-agent touch
        counts, and the next open retries.
        see design/superpowers/specs/2026-10-01-per-agent-hook-state-design.md (D3)
        """
        try:
            columns = {row[1] for row in self._conn.execute("PRAGMA table_info(retrieval_events)")}
            if "agent" not in columns:
                self._conn.execute("ALTER TABLE retrieval_events ADD COLUMN agent TEXT")
        except sqlite3.OperationalError:
            pass

    # -- canonical layout / migration ----------------------------------------

    @staticmethod
    def _migration_sentinel_path(raw: Path) -> Path | None:
        """``<name>.migration-incomplete`` beside ``raw``, or None when ``raw`` has no name
        (``Store(".")``: ``with_name`` would raise on it)."""
        if not raw.name:
            return None
        return raw.absolute().with_name(raw.name + ".migration-incomplete")

    @staticmethod
    def _check_migration_sentinel(raw: Path, sentinel: Path) -> None:
        """Resolve a leftover migration sentinel before opening ``raw`` (design D10).

        A sentinel younger than ``_LIVE_MIGRATION_SECONDS`` is another process's live
        migration, in either state below: refuse and touch nothing (a stale cleanup here
        deleted the live opener's staging directory). Only an older sentinel is resolved:

        Legacy file still intact: the interruption came before the rename, so the sentinel
        is stale. Remove it, and the staging directory it names ONLY when that is a real
        directory (not a symlink) in the legacy file's own directory with a staging name: a
        file must never choose what gets ``rmtree``-d. Legacy file gone: the migration was
        interrupted after the rename, and the staged export is the only copy of the
        records still to be moved, so refuse to open and say how to finish by hand. An
        unreadable mtime counts as old."""
        if _sentinel_age_seconds(sentinel) < _LIVE_MIGRATION_SECONDS:
            raise ValueError(
                f"another process is migrating {raw} now, or was a moment ago (sentinel "
                f"{sentinel} is younger than {_LIVE_MIGRATION_SECONDS:.0f}s); nothing was "
                "changed; retry in a minute, and do not delete the sentinel; if no other sidegraph "
                "process is running and this persists past a minute, the next open gives the "
                "recovery steps."
            )
        try:
            info = json.loads(sentinel.read_text(encoding="utf-8"))
            staging_text = str(info["staging"])
            backup_text = str(info["backup"])
        except (OSError, ValueError, KeyError, TypeError):
            staging_text = backup_text = "(unreadable sentinel)"
        if not raw.is_file():
            raise ValueError(
                f"an earlier legacy migration of {raw} was interrupted (sentinel {sentinel}): "
                f"the staged export is at {staging_text} and the original is at "
                f"{backup_text}. Move each subdirectory of the staging directory into "
                f"{raw}, then delete the staging directory and the sentinel. If the staging "
                "directory is empty, the migration had finished: delete it and the sentinel."
            )
        staging = Path(staging_text)
        if (
            staging.is_dir()
            and not staging.is_symlink()
            and Path(os.path.abspath(staging)).parent == Path(os.path.abspath(raw)).parent
            and staging.name.startswith(".sidegraph-migrating-")
        ):
            shutil.rmtree(staging, ignore_errors=True)
        elif staging_text != "(unreadable sentinel)":
            print(
                f"sidegraph: WARNING stale migration sentinel {sentinel} names {staging_text}, "
                "which is not a staging directory next to the legacy file; leaving it alone.",
                file=sys.stderr,
            )
        sentinel.unlink(missing_ok=True)

    def _reject_file_inside_a_store(self, target: Path) -> None:
        """A file sitting inside a canonical store is an artifact OF that store — never a
        legacy single-file store, and never something to migrate.

        ``index.db`` is the obvious-looking "the database" file, so opening it instead of the
        store directory is an easy mistake. It used to fall through to ``_migrate_legacy``,
        which read the derived index's own ``schema_version`` (the CURRENT one), found it
        outside the migratable set, and reported a mismatch between two identical strings
        while advising the operator to discard a perfectly healthy store. Fail here instead,
        naming what to open.

        Detection is the format marker, not the filename: any artifact next to a valid marker
        is inside a store, whatever it is called.
        """
        marker = target.parent / _FORMAT_MARKER_NAME
        if not marker.is_file():
            return
        try:
            text = marker.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return  # unreadable marker: fall through, the ordinary paths will complain
        if not text.startswith(_FORMAT_MARKER_PREFIX):
            return
        raise ValueError(
            f"{target} is a file inside the sidegraph store at {target.parent} — open the "
            f"store DIRECTORY, not a file within it (e.g. Store({str(target.parent)!r})). "
            "Nothing is wrong with the store itself."
        )

    def _ensure_canonical_subdirs(self) -> None:
        for sub in _CANONICAL_SUBDIRS:
            (self.path / sub).mkdir(parents=True, exist_ok=True)

    def _unlink_if_stale(self, path: Path) -> None:
        """Remove ``path`` only if it is older than ``_TMP_DEBRIS_MIN_AGE_SECONDS`` (design
        D4) — the age gate that keeps the sweep below from ever seeing a live in-flight tmp
        buffer as a candidate for removal in the first place; see
        ``_sweep_stale_tmp_files``. Tolerates ``FileNotFoundError`` from ``stat`` the same
        TOCTOU way ``missing_ok=True`` already tolerates it on the unlink below: the file
        can vanish between the caller's glob and this stat (another opener's sweep, or its
        own writer's ``os.replace`` finally landing) — already gone is just as harmless as
        never having found it there."""
        try:
            age = time.time() - path.stat().st_mtime
        except FileNotFoundError:
            return  # already gone -- another opener's sweep, or its os.replace consumed it
        if age > _TMP_DEBRIS_MIN_AGE_SECONDS:
            path.unlink(missing_ok=True)

    def _sweep_stale_tmp_files(self) -> None:
        """Remove stray ``*.tmp`` files left by a crash strictly between a writer's tmp
        write and its ``os.replace`` swap (see ``_atomic_write_text``/
        ``_atomic_write_text_race_tolerant``): the original file (or its absence) is
        already the durable truth, so a leftover tmp is pure crash debris — harmless to
        the invariants but left unswept it would accumulate forever and could confuse a
        naive directory listing. Covers both the canonical record dirs (a real ``*.tmp``
        glob -- every file there is ours) and the store root (only the glob patterns
        matching the store's OWN root-level tmp artifacts, ``_ROOT_TMP_GLOB_PATTERNS`` --
        never a blanket root-level ``*.tmp`` glob, which would delete a file a user
        happens to keep in the store root that was never ours; see review Minor-3).

        Age-gated (design D4): a bare ``*.tmp`` glob can't distinguish crash debris from
        another process's tmp file that is mid-write RIGHT NOW — this used to delete a live
        buffer out from under its writer, whose only defence was
        ``_atomic_write_text_race_tolerant``'s bounded retry, a constant (3) smaller than
        the number of concurrent openers it has to survive above that concurrency
        (``FileNotFoundError`` on ``os.replace``, 1/1000 opens measured). Every removal below
        goes through ``_unlink_if_stale``, which only unlinks a ``*.tmp`` file older than
        ``_TMP_DEBRIS_MIN_AGE_SECONDS`` — a live buffer exists for microseconds, so it is
        never old enough to match; only genuine crash debris is. This replaces the retry
        bound as the mechanism that keeps the sweep from destroying a live write; the retry
        loop itself stays (a writer that stalls past the age gate — e.g. the machine
        sleeps — still needs it, see design §5).

        ``_unlink_if_stale`` internally uses ``missing_ok=True`` on the actual unlink
        (review residual on Important-2a/Minor-3): a bare ``if f.is_file(): f.unlink()`` is
        a check-then-act TOCTOU against another concurrent opener's ``os.replace``
        consuming that exact tmp file between the check and the unlink -- a cross-process
        stress probe hit this reliably. ``unlink``ing a path that's already gone by the
        time the syscall runs is just as harmless as never having found it there at all, so
        tolerating that (rather than crashing this open entirely) is correct, not merely
        convenient.
        """
        for pattern in _ROOT_TMP_GLOB_PATTERNS:
            for f in self.path.glob(pattern):
                self._unlink_if_stale(f)
        for sub in _CANONICAL_SUBDIRS:
            d = self.path / sub
            if not d.is_dir():
                continue
            for f in d.glob("*.tmp"):
                self._unlink_if_stale(f)
        # archive/ is NOT one of _CANONICAL_SUBDIRS (see its definition) — it only exists
        # once a compact actually ran, and only its own *.tmp glob is swept, same as any
        # other canonical dir; segment .jsonl files themselves are never touched here.
        archive_dir = self.path / _ARCHIVE_SUBDIR
        if archive_dir.is_dir():
            for f in archive_dir.glob("*.tmp"):
                self._unlink_if_stale(f)

    def _ensure_format_marker(self) -> None:
        """Committed, git-visible format marker (``<path>/format``, one line:
        ``sidegraph-store <version>``) — a correctness gate that lives OUTSIDE the
        gitignored ``index.db``, so a teammate on an incompatible store format is rejected
        the moment they open it, not just when their local index happens to agree with
        their code. Missing marker on an existing layout (N1-era stores predate this file)
        is backfilled, never rejected. An unknown MAJOR component IS a hard rejection, same
        spirit as the ``schema_version`` gate in ``_refresh_freshness``.

        Written atomically (tmp + ``os.replace``, via ``_atomic_write_text_race_tolerant``):
        a crash mid-write must never leave a truncated marker behind — that would
        hard-reject every subsequent open of an otherwise-healthy store. Race-tolerant
        because two openers can reach this on a store's first-ever open — see
        ``_atomic_write_text_race_tolerant``'s docstring."""
        marker = self.path / _FORMAT_MARKER_NAME
        if not marker.is_file():
            _atomic_write_text_race_tolerant(marker, f"{_FORMAT_MARKER_PREFIX}{SCHEMA_VERSION}\n")
            return
        text = marker.read_text(encoding="utf-8").strip()
        if not text.startswith(_FORMAT_MARKER_PREFIX):
            raise ValueError(
                f"unrecognized store format marker {text!r} at {marker}; "
                "migration tooling is deferred — use a fresh store"
            )
        stamped_version = text[len(_FORMAT_MARKER_PREFIX) :]
        stamped_major = stamped_version.split(".", 1)[0]
        current_major = SCHEMA_VERSION.split(".", 1)[0]
        if stamped_major != current_major:
            raise ValueError(
                f"store format marker {text!r} (major {stamped_major!r}) is incompatible "
                f"with code's {SCHEMA_VERSION!r} (major {current_major!r}); "
                "migration tooling is deferred — use a fresh store"
            )

    def _ensure_stamping_marker(self) -> None:
        """Committed, git-visible creation marker (``<path>/stamping_live_since``, one
        line: an aware UTC ISO-8601 timestamp) recording the moment THIS store was
        genuinely new — written ONLY on the open that finds it new, never backfilled onto
        a pre-existing store (contrast ``_ensure_format_marker``, which explicitly DOES
        backfill).

        Why this exists: ``doctor.py``'s ``unratified-accept`` check scopes its scan to
        records created at/after the earliest ``ratified_at`` stamp anywhere in the store,
        because the signature it looks for (accepted, agent-sourced, no ratifier stamp) is
        indistinguishable from an ordinary pre-ratification-stamp record before that point
        (see that check's docstring). A store that has never ratified anything has no
        stamp to scope from, so it produces zero findings — forever, even after a later
        ratification arms the check, because every record already in the store predates
        that first stamp. A store adopting an auto-ratification policy from scratch starts
        exactly there: inside the blind window, at the moment the gate matters most. This
        marker gives that check an alternative scope-start that does not depend on any
        ratification ever having happened: the instant stamping became live for this
        store, recorded by a version that already writes this file.

        "Genuinely new" is derived, not assumed: a store is new here iff, at the moment
        this runs (after ``_ensure_canonical_subdirs`` has created the six canonical
        subdirectories, so they exist to check), it holds NO record of any kind —
        ``_store_has_any_records`` checks every hot canonical subdir AND ``archive/``,
        which together are the complete inventory of where a record can live (module
        docstring). That second half is deliberate: a store whose records were ALL moved
        into archive segments by ``Store.compact`` has empty hot directories but is an
        EXISTING store with real history, not a new one — treating it as new would stamp
        ``stamping_live_since`` at the moment of this open, years after the store (and its
        never-ratified records, if any) actually came into being, which is not "no
        backfill", it is exactly the backfill this marker must never do. A store that is
        merely emptied, never archived (nothing has ever been written to it), correctly
        has no records anywhere and correctly counts as new.

        Idempotent by construction, the same way ``_ensure_format_marker`` is: once
        written, the marker file itself is present, so every later open of this same
        store — however many records it accumulates in between — takes the `is_file()`
        branch below and never re-derives or rewrites it. A legacy store that already had
        records the very first time a stamping version opened it never has this file at
        all, on any subsequent open, ever — there is no path back into the "new" branch
        once records exist.

        Published KEEP-FIRST, not via ``_atomic_write_text_race_tolerant`` (review round 2,
        Minor 1 — a real bug, not a nit): two openers (worker threads, or two genuinely
        separate processes) can reach a store's first-ever open at once, and unlike the
        format marker/``.gitignore`` (where every racer writes IDENTICAL content, so
        whoever's ``os.replace`` lands last is harmless), each racer here stamps its OWN
        ``datetime.now(UTC)``. ``_atomic_write_text_race_tolerant``'s ``os.replace`` is
        LAST-WRITER-WINS: forced interleaving (measured) shows opener A can pass the
        "no records yet" check, opener B then open, publish an EARLIER marker, and write a
        real bypass record — and A, resuming, unconditionally overwrites B's marker with
        A's LATER timestamp, leaving a record that predates the file that is supposed to
        bound "before this store existed". A marker whose entire job is "the earliest
        instant this store could have held a record" must never be replaced by a LATER
        one. So this publishes via ``os.link`` (an exclusive create — raises
        ``FileExistsError`` if ``marker`` already exists, never silently clobbers it,
        same idiom ``_write_archive_segment`` uses and for the same reason) onto a
        per-attempt UNIQUE tmp name (never a fixed one — a fixed shared tmp name would
        reopen the truncation-race hole ``_atomic_write_text_race_tolerant`` closes for
        its own callers). Whichever racer's ``os.link`` reaches the filesystem FIRST wins
        and stays forever: every later attempt — a losing racer finishing its own
        ``__init__`` right after, or any later ``Store`` open entirely — sees
        ``marker.is_file()`` true at THIS method's very first line and never attempts a
        write at all, so a published marker can never be replaced, only ever raced to be
        the first one written. (A losing racer's OWN computed timestamp can occasionally
        be a few microseconds smaller than the winner's — real-time submission order, not
        computation order, decides the race — but that residual imprecision is bounded to
        the width of a single first-open race, not "years later", and is exactly the
        "equally valid near-simultaneous now" case this marker's precision was always only
        good for.)

        Advisory write, never a load-bearing one (review round 2, Minor 2 — a regression
        this fix must not introduce): a read-only store directory or a full disk must
        raise on nothing here. ``format`` failing to write IS supposed to fail an open —
        it is a correctness gate ``_ensure_format_marker``'s own docstring describes as
        such. This marker only feeds one advisory doctor check; failing an open over it
        would make Store() strictly LESS robust than before this feature existed, on a
        plausible shape (a sandboxed/read-only-mounted project that has run sidegraph but
        recorded nothing yet — e.g. a SessionStart hook in a read-only sandbox). Any
        ``OSError`` anywhere in the attempt — the tmp write, or ``os.link`` raising
        anything other than ``FileExistsError`` — is swallowed; the store then behaves
        exactly like a legacy store (no marker), the safe direction, and a later open on
        writable storage still gets a real chance (the "no records yet" gate is re-checked
        fresh every open, not remembered from a failed attempt).
        """
        marker = self.path / _STAMPING_MARKER_NAME
        if marker.is_file():
            return
        if _store_has_any_records(self.path):
            return
        stamp = datetime.now(UTC).isoformat()
        tmp = marker.with_name(f"{marker.name}.{os.getpid()}.{uuid.uuid4().hex[:12]}.tmp")
        with suppress(OSError):  # advisory write only -- never fail Store.open (Minor 2)
            tmp.write_text(f"{stamp}\n", encoding="utf-8")
            with suppress(FileExistsError):  # keep-first: another opener already won
                os.link(tmp, marker)
        # The cleanup needs its own suppression, not the block above (fix round 2
        # re-review, Minor A): `missing_ok=True` only tolerates a MISSING file, so an
        # unlink that raises for any other reason -- a read-only directory being the
        # realistic one -- escaped and failed `Store.open` from the one write this method
        # promises can never do that. The debris it leaves behind is swept by
        # `_sweep_stale_tmp_files` on a later open, so skipping it costs nothing.
        with suppress(OSError):
            tmp.unlink(missing_ok=True)

    def _ensure_gitignore(self) -> None:
        """Committed layout convenience (design §1/§5): write ``<path>/.gitignore``
        (``index.db*`` + ``*.tmp``) when missing, so the derived index and any crash
        debris never show up in ``git status``. Written by ANY ``Store`` open that touches
        the canonical layout — not just ``sidegraph-init`` — so a bare
        ``sidegraph-mcp``/hook invocation against a pre-existing (pre-N2) store also
        leaves git clean. NEVER overwrites an existing ``.gitignore``: a user may have
        hand-edited it."""
        gitignore = self.path / _GITIGNORE_NAME
        if gitignore.is_file():
            return
        _atomic_write_text_race_tolerant(gitignore, _GITIGNORE_CONTENT)

    def _migrate_legacy(self, legacy_file: Path) -> None:
        """One-time export of a legacy single-file SQLite store (schema_version in
        ``_MIGRATABLE_SCHEMA_VERSIONS``) into the canonical file-per-record layout at
        ``self.path`` (design §4). Raises (without touching the legacy file) if its stamp
        isn't forward-migratable. On success, renames the legacy file to
        ``<name>.migrated-backup`` (never deleted — the store never destroys data) and
        prints one stderr notice.

        Fail-closed: every legacy row is read AND pydantic-validated in FULL before a
        single byte is written anywhere or the legacy file is touched. A garbled row
        raises a clear error naming the offending table and id, leaving the legacy db
        exactly as it was — retryable, never partially migrated. Only once every row is
        known-good is the export written, and only into a private staging directory (see
        below); the legacy file's RENAME to ``.migrated-backup`` comes last in the directory case,
        so an interruption leaves it and the next open re-migrates (design D10 covers the
        bare-file case, where the rename must come first).

        Staging, not writing ``self.path`` directly, is required because ``self.path`` can
        BE ``legacy_file`` itself (the bare-file dispatch, e.g. ``Store("old.db")``) — a
        plain file, not yet a directory a canonical subdir could be created under. Writing
        to an isolated temp directory first (same filesystem as ``legacy_file`` so the
        final move is a cheap, atomic-per-entry rename) sidesteps that ordering constraint
        entirely while still keeping every write fully isolated from the real store until
        the data is proven good.

        Volatile fields (entity ``last_seen_*``, binding ``status``, domain
        ``communities``) are NOT carried over: only canonical (identity/content) files are
        written here, and ``__init__``'s freshness check (index missing -> reload) rebuilds
        the index from exactly those files afterwards, exactly like any other
        canonical-only rebuild — the next sync re-derives volatile state from the live
        graph, same as design §3's git-pull-absorption path.
        """
        conn = sqlite3.connect(str(legacy_file))
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
            stamped = row["value"] if row else None
            if stamped not in _MIGRATABLE_SCHEMA_VERSIONS:
                # State the condition actually being tested. The old wording compared the
                # stamp against the RUNNING CODE's version, which is not what this branch
                # checks — so a file stamped with the current version produced the nonsense
                # "'0.6.0' != '0.6.0'", and its "use a fresh store" advice would have
                # discarded real data. Say what this migrator handles and stop.
                handled = ", ".join(repr(v) for v in sorted(_MIGRATABLE_SCHEMA_VERSIONS))
                raise ValueError(
                    f"{legacy_file} is stamped schema_version {stamped!r}, which this "
                    f"migration path does not handle — it migrates {handled} single-file "
                    "stores only (migration tooling beyond that is deferred)."
                )
            legacy_rows = self._read_legacy_rows(conn)
        finally:
            conn.close()

        # Validate EVERYTHING before touching the filesystem at all — a garbled row raises
        # here, before the staging directory even exists.
        validated = self._validate_legacy_rows(legacy_file, legacy_rows)

        staging = Path(tempfile.mkdtemp(prefix=".sidegraph-migrating-", dir=legacy_file.parent))
        # Bare-file case: ``self.path`` IS the legacy file, so it must be renamed away before
        # the staged directories can move in. A sentinel beside it, written BEFORE that
        # point of no return, tells the next open the staged export is all that is left.
        bare = legacy_file == self.path
        sentinel = self._migration_sentinel_path(legacy_file) if bare else None
        backup = legacy_file.with_name(legacy_file.name + ".migrated-backup")
        created_sentinel = False  # True only once THIS call made the sentinel file
        try:
            for sub in _CANONICAL_SUBDIRS:
                (staging / sub).mkdir(parents=True, exist_ok=True)

            for entity in validated["entities"]:
                _atomic_write_json(
                    _record_file(staging, "entities", entity.entity_id),
                    _entity_identity_payload(entity),
                )
            for decision in validated["decisions"]:
                _atomic_write_json(
                    _record_file(staging, "decisions", decision.id),
                    decision.model_dump(mode="json"),
                )
            for domain in validated["domains"]:
                _atomic_write_json(
                    _record_file(staging, "domains", domain.domain_id),
                    _domain_canonical_payload(domain),
                )
            for initiative in validated["initiatives"]:
                _atomic_write_json(
                    _record_file(staging, "initiatives", initiative.id),
                    initiative.model_dump(mode="json"),
                )

            by_decision: dict[str, list[dict]] = {}
            for binding in validated["anchor_bindings"]:
                by_decision.setdefault(binding.record_id, []).append(
                    _binding_identity_payload(binding)
                )
            for record_id, items in by_decision.items():
                ordered = sorted(items, key=lambda x: (x["tier"], x["entity_id"]))
                _atomic_write_json(_record_file(staging, "bindings", record_id), ordered)

            # -- commit (design D10). Directory case: move the staged subdirectories in
            # FIRST and rename the legacy file LAST, so an interruption leaves the legacy
            # file and the next open simply re-migrates (per-file ``os.replace`` makes that
            # idempotent). Bare-file case: the rename must come first (see above), guarded
            # by the sentinel, which is unlinked again if the rename itself raises.
            #
            # The sentinel is created EXCLUSIVELY and unlinked only by the call that created
            # it: two openers of one legacy file share its path, and a plain overwrite let
            # the loser delete the winner's sentinel (design D10). The open-time
            # check (`_check_migration_sentinel`) refuses a sentinel younger than
            # `_LIVE_MIGRATION_SECONDS` as a live migration, so it never cleans up under
            # one; only a live migration older than that (not a realistic case for a
            # seconds-long move) could still be called stale. No cross-process lock.
            if sentinel is not None:
                try:
                    try:
                        fh = sentinel.open("x", encoding="utf-8")
                    except FileExistsError:
                        raise ValueError(
                            f"another process is migrating {legacy_file} (sentinel {sentinel} "
                            "exists); retry once it has finished."
                        ) from None
                    created_sentinel = True
                    with fh:
                        fh.write(
                            json.dumps({"staging": str(staging.absolute()), "backup": str(backup)})
                        )
                    legacy_file.rename(backup)
                except BaseException:
                    if created_sentinel:
                        sentinel.unlink(missing_ok=True)
                    raise

            self.path.mkdir(parents=True, exist_ok=True)
            for sub in _CANONICAL_SUBDIRS:
                target = self.path / sub
                if target.exists():
                    # pre-existing subdir (empty by construction of the dispatch above, or
                    # left by an interrupted earlier attempt) — move each staged file into
                    # it individually.
                    for f in (staging / sub).iterdir():
                        os.replace(f, target / f.name)
                else:
                    os.replace(staging / sub, target)

            if sentinel is not None:
                sentinel.unlink(missing_ok=True)
            else:
                legacy_file.rename(backup)
        finally:
            # Keep staging only when the export is now the sole copy: the sentinel exists
            # and the legacy path is no longer a file (it is a directory again, or gone).
            if not (
                sentinel is not None
                and created_sentinel
                and sentinel.exists()
                and not legacy_file.is_file()
            ):
                shutil.rmtree(staging, ignore_errors=True)

        print(
            f"sidegraph: migrated legacy store {legacy_file} (schema {stamped}) -> "
            f"canonical layout at {self.path} (backup: {backup})",
            file=sys.stderr,
        )

    @staticmethod
    def _read_legacy_rows(conn: sqlite3.Connection) -> dict[str, list[tuple[str, str]]]:
        """Every row of every legacy table as ``(primary_key, data_json)`` pairs — the
        primary key travels alongside the payload purely so a validation failure (see
        ``_validate_legacy_rows``) can name the offending row even when its JSON itself is
        the thing that's garbled."""

        def _table_exists(name: str) -> bool:
            return (
                conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
                ).fetchone()
                is not None
            )

        return {
            "entities": [
                (r["entity_id"], r["data"])
                for r in conn.execute("SELECT entity_id, data FROM entities")
            ],
            "decisions": [
                (r["id"], r["data"]) for r in conn.execute("SELECT id, data FROM decisions")
            ],
            "anchor_bindings": [
                # legacy (0.2.0/0.3.0) tables literally have a `decision_id` column -- this
                # reads the real on-disk legacy schema, not the current AnchorBinding model.
                (f"{r['decision_id']}:{r['entity_id']}", r["data"])
                for r in conn.execute("SELECT decision_id, entity_id, data FROM anchor_bindings")
            ],
            "initiatives": (
                [(r["id"], r["data"]) for r in conn.execute("SELECT id, data FROM initiatives")]
                if _table_exists("initiatives")
                else []
            ),
            "domains": (
                [
                    (r["domain_id"], r["data"])
                    for r in conn.execute("SELECT domain_id, data FROM domains")
                ]
                if _table_exists("domains")
                else []
            ),
        }

    _LEGACY_ROW_MODELS: dict[str, type[BaseModel]] = {
        "entities": Entity,
        "decisions": Decision,
        "domains": Domain,
        "initiatives": Initiative,
        "anchor_bindings": AnchorBinding,
    }

    # The field of each legacy model that becomes a canonical filename (a binding's file is
    # named after its ``record_id``).
    _LEGACY_ID_FIELD: dict[str, str] = {
        "entities": "entity_id",
        "decisions": "id",
        "domains": "domain_id",
        "initiatives": "id",
        "anchor_bindings": "record_id",
    }

    @classmethod
    def _validate_legacy_rows(
        cls, legacy_file: Path, rows: dict[str, list[tuple[str, str]]]
    ) -> dict[str, list]:
        """Pydantic-validate every legacy row BEFORE any canonical file is written or the
        legacy file is renamed (fail-closed migration, design §4): a single garbled row
        raises a clear ``ValueError`` naming the offending table and id, and nothing about
        the legacy db has been touched yet — the caller can simply retry later."""
        out: dict[str, list] = {}
        for table, model in cls._LEGACY_ROW_MODELS.items():
            validated = []
            for row_id, data in rows.get(table, []):
                try:
                    if table == "anchor_bindings":
                        # A genuine 0.2.0/0.3.0-era row was serialized by code that named
                        # this field `decision_id` (the AnchorBinding rename post-dates
                        # every _MIGRATABLE_SCHEMA_VERSIONS store) -- normalize the legacy
                        # key before validating against the current model.
                        payload = json.loads(data)
                        if "decision_id" in payload and "record_id" not in payload:
                            payload["record_id"] = payload.pop("decision_id")
                        validated.append(model.model_validate(payload))
                    else:
                        # The SQL primary key is the row's stable identity. It is checked
                        # raw (before any model default could mint an id), and it fills an
                        # id the JSON omits: `default_factory` would otherwise mint a fresh
                        # ULID per validation, so a retried migration wrote a second file.
                        if not is_safe_record_id(row_id):
                            raise ValueError(
                                f"unsafe record id {row_id!r}: must be a single path segment"
                            )
                        payload = json.loads(data)
                        if isinstance(payload, dict):
                            payload.setdefault(cls._LEGACY_ID_FIELD[table], row_id)
                        validated.append(model.model_validate_json(json.dumps(payload)))
                    # Every id becomes a filename (design D9): refuse before staging exists.
                    record_id = getattr(validated[-1], cls._LEGACY_ID_FIELD[table])
                    if not is_safe_record_id(record_id):
                        raise ValueError(
                            f"unsafe record id {record_id!r}: must be a single path segment"
                        )
                except Exception as e:
                    raise ValueError(
                        f"legacy store {legacy_file} has an invalid row in table "
                        f"{table!r} (id={row_id!r}): {e}"
                    ) from e
            out[table] = validated
        return out

    # -- canonical file writers (git-committed) ------------------------------

    def _record_canonical_stat(self, subdir: str, stem: str, st: os.stat_result) -> None:
        """Insert/update ``canonical_stat``'s row for ``(subdir, stem)`` — called from
        every one of the seven canonical writers, in the SAME transaction as the canonical
        file write and (for six of the seven) the caller's own index write (digest-integrity
        design, Task 1). Recording it HERE, beside the file write, rather than at the
        caller's index-write site, is what makes the ordering between the two stop
        mattering (design D4a): ``upsert_entity``/``_write_domain`` touch the digest BEFORE
        their index write while ``_write_decision``/``_write_fact`` do the reverse, and an
        id-based (or call-site-based) check would have refused the stamp on every new
        entity/domain. The stat row and the index row now always commit or roll back
        together regardless of which one the caller writes first."""
        self._conn.execute(
            "INSERT INTO canonical_stat (subdir, stem, size, mtime_ns) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(subdir, stem) DO UPDATE SET "
            "size=excluded.size, mtime_ns=excluded.mtime_ns",
            (subdir, stem, st.st_size, st.st_mtime_ns),
        )

    def _delete_canonical_stat(self, subdir: str, stem: str) -> None:
        """Remove ``canonical_stat``'s row for ``(subdir, stem)`` — called wherever a
        canonical file is actually removed (``compact``, Task 2): the row must not outlive
        the file it describes being GONE-by-design, mirroring ``_hot_file_matches``'s own
        skip (a file kept because it diverged from the archive keeps its row too — this is
        simply never called for that case)."""
        self._conn.execute(
            "DELETE FROM canonical_stat WHERE subdir = ? AND stem = ?", (subdir, stem)
        )

    def is_skipped(self, subdir: str, stem: str) -> bool:
        """True while the last reload left ``<subdir>/<stem>.json`` out of the index because it
        could not be read (it is in the meta skip list). The list is read here, not cached, so a
        long-lived process sees a list another process changed. A caller that is about to write
        by a record id the caller supplied asks this before it mints anything, so a refusal
        leaves no orphan behind (the MCP ``add_anchors`` does).
        see design/superpowers/specs/2026-10-03-store-survives-a-bad-file-design.md D5"""
        relpath = f"{subdir}/{stem}.json"
        return any(e["path"] == relpath for e in self._read_skip_list())

    def _refuse_if_skipped(self, subdir: str, stem: str) -> None:
        """Refuse to replace ``<subdir>/<stem>.json`` while the last reload left it out of the
        index: the index holds none of its content, so a recomputed write (a bindings file
        rebuilt from the index, say) would destroy what is in the file — a conflicted merge
        result a human has yet to resolve. Every canonical writer calls this before its
        atomic write; no legitimate flow writes a listed path.
        see design/superpowers/specs/2026-10-03-store-survives-a-bad-file-design.md D5"""
        if self.is_skipped(subdir, stem):
            raise ValueError(skipped_refusal_text(subdir, stem))

    def _write_entity_canonical(self, entity: Entity) -> None:
        self._refuse_if_skipped("entities", entity.entity_id)
        st = _atomic_write_json(
            _record_file(self.path, "entities", entity.entity_id),
            _entity_identity_payload(entity),
        )
        self._record_canonical_stat("entities", entity.entity_id, st)

    def _write_decision_canonical(self, decision: Decision) -> None:
        self._refuse_if_skipped("decisions", decision.id)
        st = _atomic_write_json(
            _record_file(self.path, "decisions", decision.id), decision.model_dump(mode="json")
        )
        self._record_canonical_stat("decisions", decision.id, st)

    def _write_fact_canonical(self, fact: Fact) -> None:
        self._refuse_if_skipped("facts", fact.id)
        st = _atomic_write_json(
            _record_file(self.path, "facts", fact.id), fact.model_dump(mode="json")
        )
        self._record_canonical_stat("facts", fact.id, st)

    def _write_domain_canonical(self, domain: Domain) -> None:
        self._refuse_if_skipped("domains", domain.domain_id)
        st = _atomic_write_json(
            _record_file(self.path, "domains", domain.domain_id),
            _domain_canonical_payload(domain),
        )
        self._record_canonical_stat("domains", domain.domain_id, st)

    def _write_initiative_canonical(self, initiative: Initiative) -> None:
        self._refuse_if_skipped("initiatives", initiative.id)
        st = _atomic_write_json(
            _record_file(self.path, "initiatives", initiative.id),
            initiative.model_dump(mode="json"),
        )
        self._record_canonical_stat("initiatives", initiative.id, st)

    def _write_bindings_file(self, record_id: str, items: list[dict]) -> None:
        self._refuse_if_skipped("bindings", record_id)
        ordered = sorted(items, key=lambda x: (x["tier"], x["entity_id"]))
        st = _atomic_write_json(_record_file(self.path, "bindings", record_id), ordered)
        self._record_canonical_stat("bindings", record_id, st)

    def _write_bindings_canonical_for_record(self, record_id: str) -> None:
        """Recompute the FULL canonical anchor set for ``record_id`` from the index
        (source of truth for "current") and rewrite its bindings file. Called only when
        some binding's identity (entity_id/tier/relation/weight) actually changed — a pure
        status flip never reaches here (see ``add_binding``).

        Filters out any binding whose entity is DERIVED (``community:*`` abstract
        entities — see ``_is_derived_entity``): those are index-only snapshot labels and
        must never land in a committed file, even when a genuine (non-derived) identity
        change on the SAME record is what triggered this rewrite in the first place —
        this is also how a stale community entry left by pre-0.6.0 code (tolerated on
        reload) decays away, lazily, the next time this record's canonical file is
        legitimately rewritten. One ``get_entity`` lookup per binding here is fine —
        rewrites are rare (see design doc, Mechanics §1)."""
        items = []
        for b in self.bindings_for_record(record_id):
            entity = self.get_entity(b.entity_id)
            if entity is not None and _is_derived_entity(entity):
                continue
            items.append(_binding_identity_payload(b))
        self._write_bindings_file(record_id, items)

    # -- digest / freshness (design §3) --------------------------------------

    def _compute_canonical_digest(self) -> tuple[str, dict[tuple[str, str], tuple[int, int]]]:
        """sha256 over sorted (relpath, size, mtime_ns) of every canonical record file, plus
        the format marker — cheap enough to run on every open (hundreds/thousands of small
        JSONs). The marker is included like any other committed file, but since it never
        changes within a version (``_ensure_format_marker`` writes it once and never again),
        its presence here never causes spurious digest churn.

        Also covers ``archive/*.jsonl`` segments (design §7): a segment is write-once and
        never rewritten in place, but it is still new committed content the first time it
        shows up (freshly compacted locally, or absorbed from a teammate's branch via git
        pull) — without it here, a reopen after either would keep serving the stale index
        (missing the archived records / still showing their now-removed hot files) until
        something else happened to bust the digest.

        Returns ``(digest, stats)`` where ``stats`` maps every walked file's
        ``(subdir, stem)`` — every ``_CANONICAL_SUBDIRS`` entry plus ``archive/*.jsonl``
        segments, NOT the format marker (write-once, never rewritten, so it never needs a
        ``canonical_stat`` row to prove freshness against) — to its ``(size, mtime_ns)``,
        accumulated in this SAME walk (digest-integrity design, Task 3): the walk already
        stats every file for the digest itself, so this is free. ``_touch_digest`` compares
        ``stats`` against ``canonical_stat`` before stamping."""
        entries: list[str] = []
        stats: dict[tuple[str, str], tuple[int, int]] = {}
        marker = self.path / _FORMAT_MARKER_NAME
        if marker.is_file():
            st = marker.stat()
            entries.append(f"{_FORMAT_MARKER_NAME}\0{st.st_size}\0{st.st_mtime_ns}")
        entries.append(f"identity-rule\0{_IDENTITY_RULE}")
        for sub in _CANONICAL_SUBDIRS:
            d = self.path / sub
            if not d.is_dir():
                continue
            for f in d.iterdir():
                if f.suffix != ".json":
                    continue
                st = f.stat()
                entries.append(f"{sub}/{f.name}\0{st.st_size}\0{st.st_mtime_ns}")
                stats[(sub, f.stem)] = (st.st_size, st.st_mtime_ns)
        archive_dir = self.path / _ARCHIVE_SUBDIR
        if archive_dir.is_dir():
            for f in archive_dir.iterdir():
                if f.suffix != ".jsonl":
                    continue
                st = f.stat()
                entries.append(f"{_ARCHIVE_SUBDIR}/{f.name}\0{st.st_size}\0{st.st_mtime_ns}")
                stats[(_ARCHIVE_SUBDIR, f.stem)] = (st.st_size, st.st_mtime_ns)
        entries.sort()
        h = hashlib.sha256()
        for e in entries:
            h.update(e.encode())
            h.update(b"\n")
        return h.hexdigest(), stats

    def _canonical_stat_mismatch(self, stats: dict[tuple[str, str], tuple[int, int]]) -> bool:
        """True iff any ``(subdir, stem)`` the digest walk just saw has NO ``canonical_stat``
        row, or a row with a DIFFERENT ``(size, mtime_ns)`` — i.e. this index did not load
        that file in the state it is currently in on disk (design D1/D4). One-directional
        (design D2): a row whose file is now GONE is normal and never flags a mismatch —
        ``compact`` deletes those rows itself (Task 2), and a derived ``community:*`` entity
        or an archive-loaded row never had a canonical file to begin with."""
        if not stats:
            return False
        rows = self._conn.execute("SELECT subdir, stem, size, mtime_ns FROM canonical_stat")
        table = {(r["subdir"], r["stem"]): (r["size"], r["mtime_ns"]) for r in rows}
        return any(table.get(key) != st for key, st in stats.items())

    def _touch_digest(self) -> None:
        """Recompute + store the canonical digest right after a canonical-file write, so
        THIS process's own legitimate writes never look like an external change (git pull,
        manual edit) to the next Store that opens this path.

        Refuses to stamp — and CLEARS the existing stamp — when the digest walk saw a
        canonical file this index never loaded in its current state (digest-integrity
        design D1/D3, rev 4). An earlier draft merely withheld the new stamp, on the
        assumption that doing so always leaves ``stored`` behind ``current`` for the next
        open. That assumption is false, and needs no crash at all: S1 replaces a record's
        file; S2 replaces over it and FULLY completes, stamping a digest that validly
        matches current disk; S1 then lands its own (now stale) index write and refuses to
        stamp. Merely withholding changes nothing — S2's certificate still matches disk, so
        ``stored == current`` while the index has drifted under it, permanently, since
        nothing ever busts that digest again. Declining to issue a new certificate does
        nothing about the one already on the wall — a refusal means "this index may not
        match disk", and that has to invalidate the existing claim, not just withhold the
        next one. Deleting the ``canonical_digest`` meta row does that: ``stored`` becomes
        ``None``, and ``_refresh_freshness`` already treats a missing row as "reload"
        unconditionally, regardless of what ``current`` happens to compute to.

        Still not reloading mid-write (D3): that would nest a second transaction owner
        inside ``_mutation`` and drop the caller's own uncommitted work, since the reload
        drops and repopulates every record table. Clearing costs one reload on the next
        open — the existing, tested heal path, just entered unconditionally instead of by
        a value comparison that this scenario can defeat. ``BaseException`` is not needed
        here: this never raises on the refusal path, it silently clears and returns, by
        design (D7 — a refusal means another process wrote concurrently, which is normal in
        this project's shape and not worth a routine warning)."""
        digest, stats = self._compute_canonical_digest()
        if self._canonical_stat_mismatch(stats):
            self._conn.execute("DELETE FROM meta WHERE key = ?", (_CANONICAL_DIGEST_KEY,))
            return  # invalidate, don't merely withhold (D1/D3)
        self._conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (_CANONICAL_DIGEST_KEY, digest),
        )

    def _refresh_freshness(self) -> None:
        """On open: missing/stale digest -> full reload of canonical files into the index,
        volatile fields reset to cold defaults (design §3). Digest match -> fast path, but
        still guard against a hand-corrupted ``schema_version`` stamp (the index's OWN
        claim of freshness is only trustworthy if its declared version agrees with the
        running code) — UNLESS the stamped version is a known-reloadable one
        (``_RELOADABLE_SCHEMA_VERSIONS``), in which case a full reload re-stamps it instead
        of hard-failing (see ``_RELOADABLE_SCHEMA_VERSIONS``)."""
        with self._lock:
            stored = self._conn.execute(
                "SELECT value FROM meta WHERE key = ?", (_CANONICAL_DIGEST_KEY,)
            ).fetchone()
            current, _current_stats = self._compute_canonical_digest()
            if stored is None or stored["value"] != current:
                self._reload_index_from_canonical(current)
            else:
                stamped = self._conn.execute(
                    "SELECT value FROM meta WHERE key = 'schema_version'"
                ).fetchone()
                stamped_value = stamped["value"] if stamped else None
                if stamped_value != SCHEMA_VERSION:
                    if stamped_value in _RELOADABLE_SCHEMA_VERSIONS:
                        self._reload_index_from_canonical(current)
                    else:
                        raise ValueError(
                            f"store schema_version {stamped_value!r} != code "
                            f"{SCHEMA_VERSION!r}; migration tooling is deferred — use a "
                            "fresh store"
                        )
            # No commit here: the rebuild owns its transaction (design D1/D9) and the fast
            # path is SELECT-only, which opens none. This commit used to be the only thing
            # that closed the rebuild's implicit transaction; leaving it now would invite
            # removing the rebuild's own commit, which would silently restore mechanism 1.

    def refresh_if_stale(self) -> None:
        """Re-check the canonical files against the index and rebuild it when they changed.

        ``__init__`` does this once, and a ``Store`` held for a long time (the MCP server keeps
        one for the life of the process) never would again: a ``git pull`` or another process's
        write would stay invisible until a restart. A holder calls this before each unit of work.
        No change costs one digest walk (about 8 ms on a store of 1,500 files); a change costs a
        full rebuild (about 90 ms). It runs under the store's lock, so it cannot interleave with
        a read or a write on another thread.
        see design/superpowers/specs/2026-10-03-hot-path-light-index-design.md (D4)
        """
        with self._lock:
            self._refresh_freshness()

    def _warn_skipped_files(self) -> None:
        """Repeat the skip warning from meta on every open while the list is non-empty
        (design D4). Each listed file is stat-ed first: one that vanished or changed since
        the reload means the list is stale (a long-lived Store re-certified the digest after
        the user fixed the file, and ``_canonical_stat_mismatch`` is one-directional), so
        reload once, which rewrites the list."""
        entries = self._read_skip_list()
        if not entries:
            return
        for e in entries:
            try:
                st = (self.path / e["path"]).stat()
                fresh = (st.st_size, st.st_mtime_ns) == (e["size"], e["mtime_ns"])
            except (OSError, KeyError, TypeError):
                fresh = False
            if not fresh:
                with self._lock:
                    current, _stats = self._compute_canonical_digest()
                    self._reload_index_from_canonical(current)
                entries = self._read_skip_list()
                break
        if entries:
            _warn_skipped_canonical(entries)

    def _read_skip_list(self) -> list[dict]:
        """The meta skip list, tolerating a missing key, bad JSON or a wrong shape as empty."""
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key = ?", (SKIPPED_CANONICAL_KEY,)
        ).fetchone()
        return [] if row is None else parse_skip_list(row["value"])

    def _reload_index_from_canonical(self, digest: str) -> None:
        """Rebuild the ENTIRE index from the canonical files on disk. Every volatile field
        resets to its cold default (design §3): entity ``last_seen_*`` -> None, binding
        ``status`` -> "live" ("bindings live-as-written"), domain ``communities`` -> [].
        The capture ledger and non-schema meta (e.g. ``last_synced_graph_version``,
        the TOC cache) are untouched — they are pure local bookkeeping with no canonical
        file to rebuild from, not tied to the digest at all.

        The RECORD tables (entities/decisions/facts/domains/anchor_bindings/initiatives)
        are dropped and recreated here, not just cleared with ``DELETE FROM`` — a real
        pre-0.5.0 store's ``index.db`` was built by OLDER code against an OLDER
        ``_SCHEMA_SQL`` (e.g. ``anchor_bindings.decision_id`` before this branch renamed
        it to ``record_id``), and ``__init__``'s ``executescript(_SCHEMA_SQL)`` is
        ``CREATE TABLE IF NOT EXISTS`` — a no-op against an existing table, so it never
        heals column drift. These tables are fully DERIVED (every row is rebuilt below
        from the canonical files this loop reads), so dropping and recreating them is
        safe and heals any DDL drift; re-running the schema statements immediately after
        recreates exactly the dropped tables (``IF NOT EXISTS``) without touching
        ``meta`` or ``capture_sessions``, which are NOT record tables and must survive
        this reload untouched: ``meta`` is re-stamped by this same method below, and
        ``capture_sessions`` is the local once-per-session capture ledger — dropping it
        would replay capture nudges the store already saw.

        The whole body — the six DROPs, the CREATEs, every reload loop, the archive
        merge, the slug-conflict warning, and the three meta stamps — runs inside one
        explicit ``BEGIN IMMEDIATE`` … ``COMMIT`` (design D1). SQLite DDL is fully
        transactional; the previous code just never opened a transaction around it, so
        the bare ``DROP TABLE`` statements autocommitted one at a time and a concurrent
        reader could observe the record tables gone (``OperationalError: no such table:
        domains``, 2/1600 opens measured) — including a long-lived MCP server that never
        re-opens and so has no way to retry. Under one transaction, a concurrent reader
        instead sees the OLD committed rows for the entire rebuild and the NEW ones only
        after this method's ``commit()`` — never a missing table, on every platform, with
        no new file, lock, or timeout. An exception anywhere in the body rolls the whole
        rebuild back, so a failed reload leaves the previous index intact instead of
        half-dropped (see ``test_store_rebuild_atomicity.py``).

        The CREATEs are issued as individual ``execute()`` calls against
        ``_SCHEMA_STATEMENTS``, never ``self._conn.executescript(_SCHEMA_SQL)`` (design
        D2 — measured, not stylistic): ``executescript`` issues an implicit ``COMMIT``
        before running its script, which would publish the just-executed DROPs and
        restore exactly this bug. Do not bring ``executescript`` back into this method.
        """
        with self._lock:
            # Two concurrent rebuilds now serialize on SQLite's RESERVED lock instead of
            # interleaving, so the default 5s busy timeout becomes a new failure mode under
            # load. Raise it for the rebuild ONLY -- raising it on connect would apply to
            # every statement for the connection's lifetime, stalling an ordinary tool call
            # behind a wedged writer for 30s where it stalls 5s today (design D3). A
            # measured rebuild of this repo's own store (683 canonical files) is 40ms.
            previous_timeout = self._conn.execute("PRAGMA busy_timeout").fetchone()[0]
            self._conn.execute("PRAGMA busy_timeout = 30000")
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                try:
                    self._conn.execute("DROP TABLE IF EXISTS entities")
                    self._conn.execute("DROP TABLE IF EXISTS decisions")
                    self._conn.execute("DROP TABLE IF EXISTS facts")
                    self._conn.execute("DROP TABLE IF EXISTS anchor_bindings")
                    self._conn.execute("DROP TABLE IF EXISTS domains")
                    self._conn.execute("DROP TABLE IF EXISTS initiatives")
                    # canonical_stat is DROPped with the record tables, not upserted over:
                    # a row for a file that is ABSENT during this reload must not survive it.
                    # Left in place, a file returning later with an identical (size, mtime_ns)
                    # -- mv out and back, rsync -a, a backup restore -- would match the stale
                    # row and get certified as loaded, recreating the exact poisoned state
                    # this design exists to kill, through this design's own mechanism
                    # (branch review). Recreated by the _SCHEMA_STATEMENTS loop just below,
                    # inside this same transaction, so a failed reload still rolls back whole.
                    self._conn.execute("DROP TABLE IF EXISTS canonical_stat")
                    for statement in _SCHEMA_STATEMENTS:
                        self._conn.execute(statement)

                    # Every loop below STATS the file BEFORE reading its content
                    # (digest-integrity design §3's rule, applied to the reload): a
                    # concurrent writer could replace the file between the stat and the
                    # read, and stat-before-read means that race can only make the
                    # recorded stat OLDER than the content this reload actually indexed --
                    # never newer. An older stat is safe (it just fails to match on a
                    # later digest walk and forces one more reload -- D3's tolerated heal
                    # path); a stat taken AFTER the read could describe content NEWER than
                    # what got parsed, which is the same lie in the other direction (§3).
                    # A file whose identity does not hold (``_record_identity_problem``) is
                    # NOT indexed but still gets its ``canonical_stat`` row -- this reload
                    # did examine it in its current state, so a later write must not clear
                    # the digest over it (design D3/D4) -- and joins the skip list.
                    #
                    # Each file's work is two steps (design/superpowers/specs/
                    # 2026-10-03-store-survives-a-bad-file-design.md D1). Step 1, read and
                    # validate, is guarded: a file that does not parse, validate or serialise
                    # is skipped like one whose identity fails, and the rest loads. Step 2,
                    # the ``_index_write_*`` call, is NOT guarded: an index write that raises
                    # still aborts the rebuild and rolls it back.
                    skipped: list[dict] = []
                    indexed_stems: dict[str, set[str]] = {"decisions": set(), "domains": set()}

                    def skip(subdir: str, f: Path, reason: str, st: os.stat_result) -> None:
                        skipped.append(
                            {
                                "path": f"{subdir}/{f.name}",
                                "reason": reason,
                                "size": st.st_size,
                                "mtime_ns": st.st_mtime_ns,
                            }
                        )

                    def load[M: BaseModel](
                        subdir: str, f: Path, id_field: str, model: type[M]
                    ) -> M | None:
                        st = f.stat()
                        record, problem = _parse_record_file(f, id_field, model)
                        self._record_canonical_stat(subdir, f.stem, st)
                        if problem is not None:
                            skip(subdir, f, problem, st)
                            return None
                        # After step 1, never before: a hot file that fails validation must
                        # not hide its archived copy (D1).
                        indexed_stems.get(subdir, set()).add(f.stem)
                        return record

                    for f in sorted((self.path / "entities").glob("*.json")):
                        entity = load("entities", f, "entity_id", Entity)
                        if entity is not None:
                            self._index_write_entity(entity)
                    for f in sorted((self.path / "decisions").glob("*.json")):
                        decision = load("decisions", f, "id", Decision)
                        if decision is not None:
                            self._index_write_decision(decision)
                    for f in sorted((self.path / "facts").glob("*.json")):
                        fact = load("facts", f, "id", Fact)
                        if fact is not None:
                            self._index_write_fact(fact)
                    for f in sorted((self.path / "domains").glob("*.json")):
                        domain = load("domains", f, "domain_id", Domain)
                        if domain is not None:
                            self._index_write_domain(domain)
                    for f in sorted((self.path / "bindings").glob("*.json")):
                        record_id = f.stem
                        st = f.stat()
                        if not is_safe_record_id(record_id):
                            skip("bindings", f, "unsafe filename", st)
                            self._record_canonical_stat("bindings", record_id, st)
                            continue
                        bindings = _parse_bindings_file(f)
                        if bindings is None:
                            skip("bindings", f, SKIP_PARSE_ERROR, st)
                        else:
                            for binding in bindings:
                                self._index_write_binding(binding)
                        # An empty `[]` bindings file (hand edit / merge artifact — §3's
                        # debris shape) produces no index rows above, but still gets its
                        # row here: the check reads THIS table, not the record tables, so
                        # there is no branch left to wedge on it (spec item 7). So does a
                        # file that was skipped: this reload examined it in its current state.
                        self._record_canonical_stat("bindings", record_id, st)
                    for f in sorted((self.path / "initiatives").glob("*.json")):
                        initiative = load("initiatives", f, "id", Initiative)
                        if initiative is not None:
                            self._index_write_initiative(initiative)

                    # Archive segments (design Task 2 item 3): stat every segment BEFORE
                    # `_archived_records()` reads its content below, same stat-before-read
                    # rule as the loops above -- and record a row for EVERY segment file,
                    # regardless of whether any of its individual records end up
                    # hot-shadowed a few lines down: the row is at segment-FILE
                    # granularity (this is what makes a `git pull`ed segment absorbed by
                    # this reload get a row too, whether or not its records are shadowed).
                    archive_dir = self.path / _ARCHIVE_SUBDIR
                    archive_stats: dict[str, os.stat_result] = {}
                    if archive_dir.is_dir():
                        for f in sorted(archive_dir.glob("*.jsonl")):
                            archive_stats[f.stem] = f.stat()

                    # Archived decisions/domains (design §7): a hot file for the same id
                    # ALWAYS wins (it was just indexed above) — either it's the
                    # crash-window duplicate a compact leaves behind (byte-identical,
                    # silently a no-op here; ``Store.compact`` is what actually cleans
                    # those up) or, if it genuinely differs, that's corruption-shaped and
                    # never something a mere reload should resolve by preferring the
                    # archive over live disk state. Only ids with NO hot file are indexed
                    # from the archive.
                    # Tolerant here only (D4): a line that cannot be read costs that line,
                    # and its segment joins the skip list once.
                    bad_segments: list[Path] = []
                    archived_decisions, archived_domains = self._archived_records(
                        tolerate_corrupt_lines=True, bad_segments=bad_segments
                    )
                    for segment in bad_segments:
                        st = archive_stats.get(segment.stem) or segment.stat()
                        skip(_ARCHIVE_SUBDIR, segment, SKIP_BAD_ARCHIVE_SEGMENT, st)
                    # Built from the files actually indexed, not every stem: an archived
                    # copy of a SKIPPED id must still load (design D3).
                    hot_decision_ids = indexed_stems["decisions"]
                    hot_domain_ids = indexed_stems["domains"]
                    for did, payload in archived_decisions.items():
                        hot_path = _record_file(self.path, "decisions", did)
                        if did in hot_decision_ids:
                            if json.loads(hot_path.read_text(encoding="utf-8")) != payload:
                                _warn_hot_archive_mismatch("decision", did)
                            continue
                        self._index_write_decision(Decision.model_validate(payload))
                    for dmid, payload in archived_domains.items():
                        hot_path = _record_file(self.path, "domains", dmid)
                        if dmid in hot_domain_ids:
                            if json.loads(hot_path.read_text(encoding="utf-8")) != payload:
                                _warn_hot_archive_mismatch("domain", dmid)
                            continue
                        self._index_write_domain(Domain.model_validate(payload))

                    for stem, st in archive_stats.items():
                        self._record_canonical_stat(_ARCHIVE_SUBDIR, stem, st)

                    # design §6: warn about a cross-branch domain-slug race NOW, with the
                    # full, just-merged domain set in hand -- see
                    # _compute_domain_slug_conflicts. This is a one-shot stderr notice for
                    # whoever's process just did the reload; it is NOT persisted anywhere
                    # (see Store.domain_slug_conflicts -- fix, review round 3: a
                    # single-clone workflow with nothing else ever busting the digest would
                    # otherwise never re-run this, so a resolved conflict could report as
                    # unresolved forever. domain_slug_conflicts() recomputes live instead).
                    # This also reads the tables THIS transaction just (re)wrote, so it
                    # must run before the commit below — outside the transaction it would
                    # be reading a rebuild that had already been published, the very race
                    # D1 closes.
                    for conflict in self._compute_domain_slug_conflicts():
                        _warn_domain_slug_conflict(conflict["slug"], conflict["domain_ids"])

                    for key, value in (
                        ("schema_version", SCHEMA_VERSION),
                        (_CANONICAL_DIGEST_KEY, digest),
                        (VOLATILE_STALE_KEY, "1"),
                    ):
                        self._conn.execute(
                            "INSERT INTO meta (key, value) VALUES (?, ?) "
                            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                            (key, value),
                        )
                    # In the reload's own transaction (design D4): the list is exactly as
                    # current as the tables it explains.
                    if skipped:
                        self._conn.execute(
                            "INSERT INTO meta (key, value) VALUES (?, ?) "
                            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                            (SKIPPED_CANONICAL_KEY, json.dumps(skipped)),
                        )
                    else:
                        self._conn.execute(
                            "DELETE FROM meta WHERE key = ?", (SKIPPED_CANONICAL_KEY,)
                        )
                    self._conn.commit()
                except BaseException:
                    self._conn.rollback()
                    raise
            finally:
                self._conn.execute(f"PRAGMA busy_timeout = {previous_timeout}")

    def _compute_domain_slug_conflicts(self) -> list[dict]:
        """Slugs currently held by MORE than one LIVE (``proposed``/``accepted`` — the same
        "live" set ``_validate_domain_write``'s all-holders uniqueness query uses) domain at
        once. Superseded/dropped domains intentionally free their slug back up and are
        never part of a conflict — mirrors that method's own status filter exactly, so
        "would this write have been rejected had it happened in one process" and "is this
        flagged as a merge race" never disagree.

        Reads directly off the INDEX (not the canonical files), so it reflects whatever is
        CURRENTLY loaded — every hot file plus every merged-in archive entry, always
        up to date with the running process's own writes too (see ``domain_slug_conflicts``,
        the public, live-computing wrapper around this).

        Returns a list of ``{"slug": str, "domain_ids": list[str]}``, sorted by slug then
        by ``domain_id`` (ULID, so also creation order) for a deterministic report."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT domain_id, slug FROM domains WHERE status IN (?, ?)",
                (DomainStatus.PROPOSED.value, DomainStatus.ACCEPTED.value),
            ).fetchall()
        by_slug: dict[str, list[str]] = {}
        for row in rows:
            by_slug.setdefault(row["slug"], []).append(row["domain_id"])
        return [
            {"slug": slug, "domain_ids": sorted(ids)}
            for slug, ids in sorted(by_slug.items())
            if len(ids) > 1
        ]

    def domain_slug_conflicts(self) -> list[dict]:
        """Slugs currently held by more than one live domain — see
        ``_compute_domain_slug_conflicts`` for the exact definition. Computed LIVE, on
        every call (review round 3 fix: an earlier version cached this at index-reload time
        only, in index meta — but nothing else in a single-clone workflow ever busts the
        digest to force a fresh reload, so a conflict resolved in-process, or even in a
        brand-new process against the same already-fresh store, would have kept reporting
        as unresolved forever). A single indexed ``SELECT`` is cheap enough to run on every
        ``sidegraph-sync`` pass without caching it at all.

        ``sidegraph-sync`` surfaces this list in its report (see
        ``sync.SyncReport.slug_conflicts``); ``Store.__init__`` also warns to stderr the
        moment a cold load/digest-mismatch reload detects one, for a bare CLI invocation
        that never looks at a report at all (see ``_warn_domain_slug_conflict``, called
        from ``_reload_index_from_canonical`` — that warning is a one-shot notice, never
        persisted; this method is the source of truth)."""
        return self._compute_domain_slug_conflicts()

    def _commit(self) -> None:
        self._conn.commit()

    @contextmanager
    def _mutation(self, *, immediate: bool = False) -> Iterator[None]:
        """Every public write runs inside this: commit on success, roll back on any failure
        (design D1/D2 — the fix for Defect A, external review).

        Canonical files are already on disk by the time anything here fails and cannot be
        un-replaced (see ``supersede_domain``'s ordering comment). What must not survive is a
        half-staged SQLite transaction: the connection is shared, so a long-lived process
        that returns one error would keep the write lock and every hook and CLI after it
        would get ``database is locked`` (measured: 2.1s wait, then failure).

        Reentrant (design D2): ``ratify_domains`` -> ``get_or_create_abstract_entity`` ->
        ``upsert_entity`` is a real nesting chain today, and only the OUTERMOST scope may
        commit or roll back — an earlier draft of the design spec claimed nesting was
        hypothetical; it is not, and ``ratify_domains`` wraps per item instead of once
        around the whole method for exactly this reason (D2a).

        The commit is INSIDE the try on purpose (design H1): COMMIT is exactly where
        SQLITE_BUSY lands, so a commit raising must roll back too, not propagate untouched.
        An earlier draft of the design put the commit after the whole try/except/finally and
        review caught that it silently reintroduces Defect A on its single most likely
        trigger.

        ``BaseException``, not ``Exception`` — matches ``_reload_index_from_canonical``'s
        existing guard (design D3, that method's own transaction, NOT this helper): a
        ``KeyboardInterrupt`` mid-write must roll back before propagating.

        ``immediate=True`` (design D6, entity-identity-uniqueness spec) issues ``BEGIN
        IMMEDIATE`` before yielding, so a get-or-create's check-then-create runs as one
        atomic unit and a second connection cannot slip its own lookup in between (external
        review, finding 2). Gated on the CONNECTION (``self._conn.in_transaction``), NOT on
        the depth. Python's legacy transaction control opens nothing until the first DML, so
        an outer ``_mutation()`` that has only READ holds no lock at all — measured,
        ``in_transaction`` is ``False`` after a read inside a mutation. An earlier draft
        gated this on depth instead (a nested immediate at depth 2 is a no-op because "the
        outer transaction already holds RESERVED"); that reasoning is false whenever the
        outer scope hasn't written yet, and review reproduced the consequence directly:
        outer plain mutation -> nested depth-2 immediate treated as a no-op -> two ids
        minted again, straight through the hole this flag exists to close. Checking
        ``in_transaction`` instead is correct at any depth: if the outer scope already
        performed DML (so a transaction is already open), the nested ``BEGIN IMMEDIATE`` is
        skipped — issuing a second ``BEGIN`` on an open transaction would raise
        ``OperationalError: cannot start a transaction within a transaction`` — and if it
        hasn't, the nested call is the one that actually takes the write lock, for real,
        while the outermost scope still owns commit and rollback either way.
        """
        with self._lock:
            self._mutation_depth += 1
            try:
                if immediate and not self._conn.in_transaction:
                    self._conn.execute("BEGIN IMMEDIATE")
                yield
                if self._mutation_depth == 1:
                    self._commit()
            except BaseException:
                if self._mutation_depth == 1:
                    self._conn.rollback()
                raise
            finally:
                self._mutation_depth -= 1

    # -- index writers (derived; index.db only) ------------------------------

    def _index_write_entity(self, entity: Entity) -> None:
        self._conn.execute(
            "INSERT INTO entities (entity_id, canonical_name, data) VALUES (?, ?, ?) "
            "ON CONFLICT(entity_id) DO UPDATE SET canonical_name=excluded.canonical_name, "
            "data=excluded.data",
            (entity.entity_id, entity.canonical_name, entity.model_dump_json()),
        )

    def _index_write_decision(self, decision: Decision) -> None:
        self._conn.execute(
            "INSERT INTO decisions (id, status, supersedes, data) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET status=excluded.status, data=excluded.data",
            (
                decision.id,
                decision.status.value,
                decision.supersedes,
                decision.model_dump_json(),
            ),
        )

    def _index_write_fact(self, fact: Fact) -> None:
        self._conn.execute(
            "INSERT INTO facts (id, status, supersedes, data) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET status=excluded.status, data=excluded.data",
            (
                fact.id,
                fact.status.value,
                fact.supersedes,
                fact.model_dump_json(),
            ),
        )

    def _index_write_domain(self, domain: Domain) -> None:
        self._conn.execute(
            "INSERT INTO domains (domain_id, slug, status, supersedes, data) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(domain_id) DO UPDATE SET slug=excluded.slug, "
            "status=excluded.status, supersedes=excluded.supersedes, data=excluded.data",
            (
                domain.domain_id,
                domain.slug,
                domain.status.value,
                domain.supersedes,
                domain.model_dump_json(),
            ),
        )

    def _index_write_binding(self, binding: AnchorBinding) -> None:
        self._conn.execute(
            "INSERT INTO anchor_bindings (record_id, entity_id, data) VALUES (?, ?, ?) "
            "ON CONFLICT(record_id, entity_id) DO UPDATE SET data=excluded.data",
            (binding.record_id, binding.entity_id, binding.model_dump_json()),
        )

    def _index_write_initiative(self, initiative: Initiative) -> None:
        self._conn.execute(
            "INSERT INTO initiatives (id, name, data) VALUES (?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name, data=excluded.data",
            (initiative.id, initiative.name, initiative.model_dump_json()),
        )

    def _index_get_binding(self, record_id: str, entity_id: str) -> AnchorBinding | None:
        row = self._conn.execute(
            "SELECT data FROM anchor_bindings WHERE record_id = ? AND entity_id = ?",
            (record_id, entity_id),
        ).fetchone()
        return AnchorBinding.model_validate_json(row["data"]) if row else None

    # -- entities -------------------------------------------------------------

    def upsert_entity(self, entity: Entity) -> Entity:
        """Persist an entity. Entities are created lazily by the capture path.

        The canonical file (``entities/<id>.json`` — identity only) is written ONLY when
        the entity is new, or one of its identity fields actually changed (e.g. a rename
        that moves its ``descriptor`` — see ``sync.rebind_entity``'s "moved" outcome). A
        pure engine-mapping refresh (``last_seen_node_id``/``last_seen_graph_version``/
        ``last_seen_community`` — see ``sync.py``) touches ONLY the index; the committed
        file is untouched (design §1: "sync never writes a committed file again").

        DERIVED entities (``community:*`` abstract entities — see ``_is_derived_entity``)
        never get a canonical file either, even on this direct path: the guard lives HERE,
        not just in ``get_or_create_abstract_entity``'s caller-side special case, so any
        current or future caller that constructs a brand-new ``community:*`` ``Entity`` and
        upserts it directly gets the same index-only treatment — the invariant is airtight
        by construction, not by caller discipline (see design/superpowers/specs/
        2026-07-10-derived-community-bindings-design.md)."""
        with self._mutation():
            existing = self.get_entity(entity.entity_id)
            identity_changed = existing is None or _entity_identity_payload(
                existing
            ) != _entity_identity_payload(entity)
            if identity_changed and not _is_derived_entity(entity):
                self._write_entity_canonical(entity)
                self._touch_digest()
            self._index_write_entity(entity)
        return entity

    def get_entity(self, entity_id: str) -> Entity | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT data FROM entities WHERE entity_id = ?", (entity_id,)
            ).fetchone()
        return Entity.model_validate_json(row["data"]) if row else None

    def find_entity(self, name: str, file_path: str | None) -> Entity | None:
        """Dedup lookup by canonical name + file_path (see schema.canonicalize).

        Deterministic under a duplicate (design D4): a duplicate logical identity is LEGAL
        (two branches each minting the same name produce two ULIDs -> two files -> a clean
        git merge, see D2), so more than one row can match here. The LOWEST ``entity_id``
        wins — ULIDs sort lexicographically by mint time, so "lowest" means "the one that
        existed first", and it is stable across processes and index rebuilds. Without this,
        a bare table scan returns whichever row SQLite happens to hand back first, which is
        an accident of insertion order, not a rule — see
        ``tests/test_entity_duplicate_resolution.py``'s inversion construction, which pins
        exactly that. Still a full Python scan (real, and a separate change — see spec §5);
        not this wave's subject."""
        target = canonicalize(name)
        with self._lock:
            rows = self._conn.execute("SELECT data FROM entities").fetchall()
        matches: list[Entity] = []
        for row in rows:
            e = Entity.model_validate_json(row["data"])
            desc_file = e.descriptor.file_path if e.descriptor else None
            e_name = e.descriptor.name if e.descriptor else e.canonical_name
            if canonicalize(e_name) == target and desc_file == file_path:
                matches.append(e)
        return min(matches, key=lambda e: e.entity_id) if matches else None

    def get_or_create_entity(self, descriptor: Descriptor) -> Entity:
        """Atomic get-or-create for a CONCRETE entity (design D3).

        The lookup and the mint run in one ``BEGIN IMMEDIATE`` transaction, so a second
        process cannot execute its own lookup in between and mint a rival id for the same
        logical entity — ``self._lock`` is per-instance and never protected against that
        (external review, finding 2; see ``get_or_create_abstract_entity``'s docstring for
        the full mechanism). Collapses the longhand ``find_entity`` + ``upsert_entity``
        sequence that used to be written out at two call sites (``anchoring.py``,
        ``capture.py``) onto one place, so a third caller cannot repeat the same omission.

        The nested ``upsert_entity`` runs at depth 2 — it neither begins nor commits.

        A descriptor with NO ``file_path`` falls back to a name-only lookup before minting.
        ``find_entity`` matches on name AND path, so a path-less descriptor could never find
        the entity that already existed for that symbol with a real path — it minted a
        path-less twin that resolves to nothing in the graph, hiding every decision anchored
        through it (38 twin names across the four committed stores). ``doc_import`` produces
        exactly such descriptors for backticked mentions: it has no path it could know.
        Adoption requires the candidates to agree on ONE path — a name owned by two files is
        genuinely ambiguous and keeps the old behaviour rather than guessing (measured: all
        38 were unambiguous). Abstract entities carry no descriptor and are never adopted."""
        with self._mutation(immediate=True):
            existing = self.resolve_descriptor(descriptor.name, descriptor.file_path)
            if existing is not None:
                return existing
            return self.upsert_entity(Entity(canonical_name=descriptor.name, descriptor=descriptor))

    def resolve_descriptor(self, name: str, file_path: str | None) -> Entity | None:
        """One descriptor -> one existing entity: the lookup EVERY caller must use, read or
        write, or the two disagree.

        ``get_or_create_entity``'s find-half, exposed so the read path resolves a descriptor
        to the same entity the write path bound it to. Splitting them is how the twin bug
        worked in the first place, and re-splitting them broke capture's dedup: once the
        write adopts the carrier, a reader still keying on ``(name, None)`` finds nothing,
        so an identical re-proposal sails through ``_is_duplicate``/``_is_duplicate_fact``
        (external review, finding 1 — demonstrated, not theorised). Returns None when the
        descriptor names nothing yet; only ``get_or_create_entity`` mints.

        ONE scan for the path-less case, not two. Every entity lookup here is a full Python
        scan that re-parses each row, and ``resolve_seeds`` runs this per graph node — a
        path-less node is not exotic (45 in this repo's own graph, 6646 in airflow's). Asking
        ``_adopt_path_carrying_entity`` and then falling back to ``find_entity`` scanned
        twice and measured 2.00 ms/call against ``find_entity``'s 0.99 ms at 219 entities:
        13 s of pure scanning on an airflow-sized seed. Both answers come out of the same
        candidate list instead.

        ``""`` counts as no path. Graphify emits ``source_file: ""`` — an empty STRING, not
        a missing key — for every path-less node (measured: 6646/6646 in airflow's graph,
        45/45 in this repo's), so a reader-fed lookup arrives as ``(name, "")``. Keying
        adoption on ``is None`` alone left the read path inert while the write path adopted,
        which is the very split this method exists to close; ``retrieval.py`` already
        documents the same trap for its own truthiness check. No entity can carry ``""`` as
        its ``file_path`` (descriptors hold a real path or None), so folding the two costs
        nothing elsewhere."""
        if file_path:
            return self.find_entity(name, file_path)
        candidates = self.find_entities_by_name(name)
        adopted = self._adopt_path_carrying_entity(candidates)
        if adopted is not None:
            return adopted
        # No carrier: reproduce find_entity(name, None) — same predicate (``desc_file is
        # None``, which an abstract entity satisfies by having no descriptor at all), same
        # D4 tie-break — off the list already in hand.
        exact = [
            e for e in candidates if (e.descriptor.file_path if e.descriptor else None) is None
        ]
        return min(exact, key=lambda e: e.entity_id) if exact else None

    def _adopt_path_carrying_entity(self, candidates: list[Entity]) -> Entity | None:
        """The CONCRETE entity a path-less descriptor should adopt, out of ``candidates``
        (every entity sharing its canonical name), when they agree on exactly one path.
        Returns None when nothing carries a path, or when two files do — the caller then
        falls back to the plain path-less lookup. Ties on one path resolve by lowest
        ``entity_id``, the store's one duplicate rule (design D4)."""
        concrete = [
            e for e in candidates if e.descriptor is not None and e.descriptor.file_path is not None
        ]
        if len({e.descriptor.file_path for e in concrete if e.descriptor}) != 1:
            return None
        return min(concrete, key=lambda e: e.entity_id)

    def find_entities_by_name(self, name: str) -> list[Entity]:
        """Name-only scan across all entities, ignoring ``file_path`` — the fallback
        ``find_entity`` (the MCP tool) uses when no exact descriptor match exists. May
        return more than one entity when a name is reused across files; the caller decides
        whether that's ambiguous (never guess which one the query meant)."""
        target = canonicalize(name)
        with self._lock:
            rows = self._conn.execute("SELECT data FROM entities").fetchall()
        out: list[Entity] = []
        for row in rows:
            e = Entity.model_validate_json(row["data"])
            e_name = e.descriptor.name if e.descriptor else e.canonical_name
            if canonicalize(e_name) == target:
                out.append(e)
        return out

    def get_or_create_abstract_entity(self, canonical_name: str) -> Entity:
        """Get-or-create an abstract Entity (community / domain / tag / initiative anchor).
        Reused.

        The whole check-then-create sequence runs inside ONE ``BEGIN IMMEDIATE`` transaction
        (design D1/D3, entity-identity-uniqueness spec), not merely under ``self._lock``:
        ``self._lock`` is per-``Store``-instance, so it serializes two THREADS sharing one
        instance but does nothing at all across two separate ``Store`` instances (an MCP
        server, a hook, and a CLI each hold their own) — those raced their lookups straight
        through it and each minted a different id for the same logical entity (external
        review, finding 2). ``BEGIN IMMEDIATE`` takes SQLite's write lock before the lookup
        even runs, so a second connection's own ``BEGIN IMMEDIATE`` blocks until this one
        commits, and its lookup then sees this one's row instead of racing it.

        ``community:*`` names are DERIVED (see ``_is_derived_entity``): the new entity gets
        an index row only — never an ``entities/<id>.json`` canonical file — since Leiden
        renumbers communities on every rebuild and a committed file would just accumulate
        one dead entity per renumbering forever. Every other abstract name (``domain:*``,
        ``tag:*``, initiative anchors, ...) is unchanged: a brand-new one still writes its
        canonical file. The derived check itself now lives entirely in ``upsert_entity``
        (see its docstring) — this just calls it unconditionally. The nested
        ``upsert_entity`` runs at depth 2 — it neither begins nor commits (see
        ``_mutation``).

        Deterministic under a duplicate (design D4): sorts matches and returns the LOWEST
        ``entity_id`` — see ``find_entity``'s docstring for why. Left unsorted, this inline
        lookup could disagree with ``find_abstract_entity``'s own read path about which
        duplicate wins; sorting both the same way means the create path and the read path
        always agree."""
        with self._mutation(immediate=True):
            rows = self._conn.execute(
                "SELECT data FROM entities WHERE canonical_name = ?", (canonical_name,)
            ).fetchall()
            matches = [
                e
                for e in (Entity.model_validate_json(row["data"]) for row in rows)
                if e.kind == EntityKind.ABSTRACT
            ]
            if matches:
                return min(matches, key=lambda e: e.entity_id)
            entity = Entity(canonical_name=canonical_name, kind=EntityKind.ABSTRACT)
            return self.upsert_entity(entity)

    # -- decisions ----------------------------------------------------------

    def add_decision(self, decision: Decision, *, close_predecessor: bool = True) -> Decision:
        """Append a decision, enforcing the write-path invariants.

        If ``decision`` supersedes another, the predecessor is closed (its ``valid_to`` is
        set and its status flipped to ``superseded``) in the same transaction — keeping the
        "a superseded decision must have a successor" invariant true at all times. This is
        the default (``close_predecessor=True``) and unchanged for every existing caller
        (``capture.propose``, ``server._supersede_decision_impl``, doc-import's non-
        ``--propose`` path).

        The close only ever applies to a predecessor that is STILL OPEN (status
        ``accepted`` or ``proposed`` — mirrors :meth:`ratify`'s own guard). A predecessor
        that is ALREADY terminal (``superseded``/``rejected``/``deprecated`` — see design
        §7) is left completely untouched: its hot file (which may already have been
        compacted into an archive segment — see :meth:`compact`) is never rewritten, and
        its status never flips again. Without this guard, superseding an archived
        ``rejected`` decision would resurrect a mutated copy of it as a fresh hot file —
        permanently corruption-shaped from compaction's point of view (every teammate's
        next cold reload would warn about a hot/archive mismatch, and the record could
        never compact again). The successor's ``supersedes`` field still records the
        intended relationship either way — append-only semantics: the link is a fact that
        happened, even when the target itself can no longer be edited.

        The SUCCESSOR is written FIRST, and the predecessor is only flipped afterwards
        (mirrors :meth:`ratify`'s order): if the successor's write fails partway, nothing
        has flipped the predecessor's canonical file yet, so the store is left in the
        tolerated "deferred supersession" shape (both records still live, successor simply
        absent) rather than a durable "superseded with zero successors" record that a later
        digest reload would silently adopt as-is.

        ``close_predecessor=False`` (doc-import's ``--propose`` + edited-doc path, design
        §3 option (b)) writes the successor with ``supersedes`` set but leaves the
        predecessor exactly as it is — an accepted decision must never be silently closed
        by a mere proposal nobody has reviewed yet. The existence check below (``supersedes``
        must reference a real decision) still runs either way; only the CLOSE side effect is
        skipped. The predecessor closes later, when a human actually ratifies the successor
        — see :meth:`ratify`'s deferred-supersession logic.

        Raises if ``decision.id`` already exists: this method is append-only, and without
        the guard an existing id would silently rewrite the row via the ``ON CONFLICT DO
        UPDATE`` in ``_write_decision``, erasing history. Nothing legitimate re-adds the
        same id — ``ratify``/``drop`` and the supersede path above call ``_write_decision``
        directly.
        """
        with self._mutation():
            if self.get_decision(decision.id) is not None:
                raise ValueError(
                    f"decision {decision.id} already exists — add_decision is append-only; "
                    "use its supersedes field or ratify/drop instead"
                )
            predecessor = None
            if decision.supersedes:
                predecessor = self.get_decision(decision.supersedes)
                if predecessor is None:
                    raise ValueError(
                        f"supersedes references unknown decision {decision.supersedes!r}"
                    )

            self._write_decision(decision)

            if (
                close_predecessor
                and predecessor is not None
                and predecessor.status in (DecisionStatus.ACCEPTED, DecisionStatus.PROPOSED)
            ):
                predecessor.status = DecisionStatus.SUPERSEDED
                predecessor.valid_to = predecessor.valid_to or max(
                    decision.valid_from, predecessor.valid_from
                )
                self._write_decision(predecessor)
        return decision

    def _write_decision(self, decision: Decision) -> None:
        with self._lock:
            self._write_decision_canonical(decision)
            self._index_write_decision(decision)
            self._touch_digest()

    def _write_fact(self, fact: Fact) -> None:
        with self._lock:
            self._write_fact_canonical(fact)
            self._index_write_fact(fact)
            self._touch_digest()

    def get_decision(self, decision_id: str) -> Decision | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT data FROM decisions WHERE id = ?", (decision_id,)
            ).fetchone()
        return Decision.model_validate_json(row["data"]) if row else None

    def iter_decisions(self) -> Iterator[Decision]:
        with self._lock:
            rows = self._conn.execute("SELECT data FROM decisions").fetchall()
        for row in rows:
            yield Decision.model_validate_json(row["data"])

    def find_decision_by_title(
        self, canonical_title: str, source: str, ref: str | None
    ) -> Decision | None:
        """Idempotency lookup for bulk importers (see importer.py): the first non-superseded
        decision whose canonicalized title matches ``canonical_title``, whose
        ``provenance.source`` equals ``source``, AND whose ``provenance.ref`` equals ``ref``.
        ``ref`` keys on the rationale's origin (its ``file_path``, falling back to
        ``node_id`` when the rationale has none) — title alone over-dedups: identical first
        lines in different files are distinct memories and must both import (S2 review).
        Full-table scan — acceptable at import volume (hundreds, not a hot retrieval path)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT data FROM decisions WHERE status != ?",
                (DecisionStatus.SUPERSEDED.value,),
            ).fetchall()
        for row in rows:
            d = Decision.model_validate_json(row["data"])
            if (
                d.provenance.source == source
                and d.provenance.ref == ref
                and canonicalize(d.title) == canonical_title
            ):
                return d
        return None

    def find_decisions_by_ref(
        self, source: str, ref: str, *, statuses: tuple[DecisionStatus, ...] | None = None
    ) -> list[Decision]:
        """Idempotency lookup for doc-import (see doc_import.py): EVERY OPEN (status
        ``accepted`` or ``proposed``) decision whose ``provenance.source`` equals
        ``source`` and whose ``provenance.ref`` equals ``ref`` — title-AGNOSTIC, unlike
        ``find_decision_by_title``. Importer #1 never supersedes, so it needs an exact
        title match too (a changed title there just means "a different rationale");
        doc-import must find every CURRENTLY-OPEN record for a given doc path, even when
        titles differ (the doc was edited) or a proposal is still awaiting ratification.

        Replaces the former singular ``find_decision_by_ref`` (returned only the FIRST
        non-superseded match in whatever order sqlite's full-table scan happened to
        produce). That was the root cause of a real regression: with ``--propose``, an
        edited ref can have TWO open records at once — a still-``accepted`` predecessor
        and a newer ``proposed`` draft — and the single-result lookup would return
        whichever came first in scan order (typically the older accepted row, since it was
        inserted first), silently hiding the pending proposal from the idempotency check.
        Re-running the importer then compared the fresh parse against the WRONG record,
        never matched, and proposed a fresh duplicate every single run. Returning the full
        set lets the caller compare against every open record and pick the right
        predecessor deliberately instead of trusting scan order.

        ``rejected`` (a human explicitly declined it via ``ratify``/``drop``) and
        ``superseded`` records are excluded — a dead-end draft or a closed predecessor
        must never block or feed a fresh import; only genuinely open state counts.

        ``statuses`` overrides that default set. Doc-import passes ``REJECTED`` alongside
        the open ones: a rejected record is invisible to the default filter, so an
        unchanged ``status: rejected`` document was re-imported as a brand-new record on
        every single run (review finding 1 — three runs, three identical records, and the
        report line said "0 existing").

        Deterministic order: sorted by ``id`` ascending (a ULID, so this is also creation
        order — oldest first). Full-table scan, like ``find_decision_by_title``; same
        volume rationale.
        """
        wanted = statuses or (DecisionStatus.ACCEPTED, DecisionStatus.PROPOSED)
        placeholders = ", ".join("?" for _ in wanted)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT data FROM decisions WHERE status IN ({placeholders})",
                tuple(st.value for st in wanted),
            ).fetchall()
        out: list[Decision] = []
        for row in rows:
            d = Decision.model_validate_json(row["data"])
            if d.provenance.source == source and d.provenance.ref == ref:
                out.append(d)
        out.sort(key=lambda d: d.id)
        return out

    # -- facts ----------------------------------------------------------------
    #
    # Append-only, mirroring decisions: add_fact writes a brand-new row; supersession
    # closes the predecessor (status -> superseded) and writes the successor first, in the
    # same transaction (see add_decision for the parallel). A fact's `supports` links it to
    # the decision(s) it informed — every id must reference an existing Decision.

    def add_fact(self, fact: Fact, *, close_predecessor: bool = True) -> Fact:
        """Append a fact, enforcing the write-path invariants — mirrors :meth:`add_decision`.

        Every id in ``fact.supports`` must reference an existing :class:`Decision` (facts
        inform decisions; the reverse link isn't a thing). If ``fact`` supersedes another
        fact, the predecessor is closed (``valid_to`` set, status flipped to
        ``superseded``) in the same transaction — same "a superseded record must have a
        successor" guarantee ``add_decision`` gives, and the same STILL-OPEN guard (only a
        predecessor with status ``accepted``/``proposed`` is closed). The SUCCESSOR is
        written FIRST, mirroring ``add_decision``'s crash-ordering rationale.

        Raises if ``fact.id`` already exists: append-only, mirroring ``add_decision`` — an
        existing id must never be silently rewritten via ``_index_write_fact``'s
        ``ON CONFLICT DO UPDATE``.
        """
        with self._mutation():
            if self.get_fact(fact.id) is not None:
                raise ValueError(
                    f"fact {fact.id} already exists — add_fact is append-only; "
                    "use its supersedes field instead"
                )
            for sid in fact.supports:
                if self.get_decision(sid) is None:
                    raise ValueError(f"supports references unknown decision: {sid}")

            predecessor = None
            if fact.supersedes:
                predecessor = self.get_fact(fact.supersedes)
                if predecessor is None:
                    raise ValueError(f"supersedes references unknown fact {fact.supersedes!r}")

            self._write_fact(fact)

            if (
                close_predecessor
                and predecessor is not None
                and predecessor.status in (DecisionStatus.ACCEPTED, DecisionStatus.PROPOSED)
            ):
                predecessor.status = DecisionStatus.SUPERSEDED
                predecessor.valid_to = predecessor.valid_to or max(
                    fact.valid_from, predecessor.valid_from
                )
                self._write_fact(predecessor)
        return fact

    def get_fact(self, fact_id: str) -> Fact | None:
        with self._lock:
            row = self._conn.execute("SELECT data FROM facts WHERE id = ?", (fact_id,)).fetchone()
        return Fact.model_validate_json(row["data"]) if row else None

    def record_types(self, ids: Iterable[str]) -> dict[str, Literal["decision", "fact"]]:
        """The type of each record id, from one read of ``index.db`` rather than one lookup per
        id: ``"decision"`` or ``"fact"``. An id found in neither table is ABSENT from the
        result, so the caller decides what a dangling id means. A repeated id is asked once.

        Parses no record, so it is the cheap way to ask "what is this binding's record?" before
        loading it. The ids go in chunks of :data:`_RECORD_TYPES_CHUNK`; below that it is one
        statement. An id in both tables (impossible for ULIDs minted per record) is a decision,
        which is the order ``get_entity_history`` always used.
        see design/superpowers/specs/2026-10-04-find-entity-unknown-record-type-design.md
        """
        wanted = list(dict.fromkeys(ids))
        out: dict[str, Literal["decision", "fact"]] = {}
        for start in range(0, len(wanted), _RECORD_TYPES_CHUNK):
            chunk = wanted[start : start + _RECORD_TYPES_CHUNK]
            marks = ", ".join("?" * len(chunk))
            with self._lock:
                rows = self._conn.execute(
                    f"SELECT id, 'decision' AS kind FROM decisions WHERE id IN ({marks}) "
                    f"UNION ALL SELECT id, 'fact' AS kind FROM facts WHERE id IN ({marks})",
                    chunk + chunk,
                ).fetchall()
            for row in rows:
                if row["kind"] == "decision" or row["id"] not in out:
                    out[row["id"]] = row["kind"]
        return out

    def iter_facts(self) -> Iterator[Fact]:
        with self._lock:
            rows = self._conn.execute("SELECT data FROM facts").fetchall()
        for row in rows:
            yield Fact.model_validate_json(row["data"])

    def iter_proposed_facts(self) -> Iterator[Fact]:
        """Facts awaiting ratification (status == proposed) — mirrors :meth:`iter_proposed`."""
        with self._lock:
            rows = self._conn.execute("SELECT data FROM facts WHERE status = 'proposed'").fetchall()
        for row in rows:
            yield Fact.model_validate_json(row["data"])

    def facts_for_decision(self, decision_id: str) -> list[Fact]:
        """Live + proposed facts (status ACCEPTED/PROPOSED, any validity) whose
        ``supports`` contains ``decision_id`` — filtered in Python; volumes are small (same
        rationale as ``find_decision_by_title``'s full-table scan)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT data FROM facts WHERE status IN (?, ?)",
                (DecisionStatus.ACCEPTED.value, DecisionStatus.PROPOSED.value),
            ).fetchall()
        out: list[Fact] = []
        for row in rows:
            f = Fact.model_validate_json(row["data"])
            if decision_id in f.supports:
                out.append(f)
        return out

    def valid_facts_for_entity(self, entity_id: str, as_of: datetime | None = None) -> list[Fact]:
        """Currently-valid, non-superseded facts bound to entity_id via a live|degraded
        binding (orphaned bindings are skipped) — mirrors :meth:`valid_decisions_for_entity`.
        ``as_of`` defaults to now (UTC)."""
        as_of = as_of or datetime.now(UTC)
        out: list[Fact] = []
        for b in self.bindings_for_entity(entity_id):
            if b.status not in ("live", "degraded"):
                continue
            f = self.get_fact(b.record_id)
            if f is None or f.status in (DecisionStatus.SUPERSEDED, DecisionStatus.REJECTED):
                continue
            if f.valid_to is None or f.valid_to > as_of:
                out.append(f)
        return out

    # -- anchor bindings ----------------------------------------------------

    def add_binding(self, binding: AnchorBinding) -> AnchorBinding:
        """Link a record (a decision OR a fact) to an entity. The entity must already
        exist, and ``binding.record_id`` must resolve to a real decision or fact —
        neither existing is a hard error.

        The canonical file (``bindings/<record_id>.json`` — the anchor set, no
        ``status``) is rewritten ONLY when this binding is new, or its identity
        (entity_id/tier/relation/weight) actually changed. A pure status flip (sync's
        live/degraded/orphaned transitions — see ``sync._set_leaf_status``,
        ``_reconcile_tier1_communities``) touches ONLY the index.

        An identity change on a binding whose entity is DERIVED (a ``community:*``
        abstract entity — see ``_is_derived_entity``) never triggers a canonical rewrite
        either: community rebinding is index-only, at capture time as much as at sync
        time (see design/superpowers/specs/2026-07-10-derived-community-bindings-design.md).
        Such a binding would be filtered back out of the payload anyway (see
        ``_write_bindings_canonical_for_record``) — skipping the rewrite here just avoids
        a pointless write (and ``_touch_digest`` bump) whose result is byte-identical to
        what's already on disk.
        """
        with self._mutation():
            entity = self.get_entity(binding.entity_id)
            if entity is None:
                raise ValueError(f"binding references unknown entity {binding.entity_id!r}")
            if (
                self.get_decision(binding.record_id) is None
                and self.get_fact(binding.record_id) is None
            ):
                raise ValueError(f"binding references unknown record {binding.record_id!r}")
            existing = self._index_get_binding(binding.record_id, binding.entity_id)
            identity_changed = existing is None or _binding_identity_payload(
                existing
            ) != _binding_identity_payload(binding)
            self._index_write_binding(binding)
            if identity_changed and not _is_derived_entity(entity):
                self._write_bindings_canonical_for_record(binding.record_id)
                self._touch_digest()
        return binding

    def bindings_for_entity(self, entity_id: str) -> list[AnchorBinding]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT data FROM anchor_bindings WHERE entity_id = ?", (entity_id,)
            ).fetchall()
        return [AnchorBinding.model_validate_json(r["data"]) for r in rows]

    def bindings_for_record(self, record_id: str) -> list[AnchorBinding]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT data FROM anchor_bindings WHERE record_id = ?", (record_id,)
            ).fetchall()
        return [AnchorBinding.model_validate_json(r["data"]) for r in rows]

    def valid_decisions_for_entity(
        self, entity_id: str, as_of: datetime | None = None
    ) -> list[Decision]:
        """Currently-valid, non-superseded decisions bound to entity_id via a live|degraded
        binding (orphaned bindings are skipped). ``as_of`` defaults to now (UTC)."""
        as_of = as_of or datetime.now(UTC)
        out: list[Decision] = []
        for b in self.bindings_for_entity(entity_id):
            if b.status not in ("live", "degraded"):
                continue
            d = self.get_decision(b.record_id)
            if d is None or d.status in (DecisionStatus.SUPERSEDED, DecisionStatus.REJECTED):
                continue
            if d.valid_to is None or d.valid_to > as_of:
                out.append(d)
        return out

    def superseded_for_entity(self, entity_id: str) -> list[Decision]:
        """Superseded decisions bound to entity_id (for the 'tried, reverted' one-liner)."""
        out: list[Decision] = []
        for b in self.bindings_for_entity(entity_id):
            d = self.get_decision(b.record_id)
            if d is not None and d.status == DecisionStatus.SUPERSEDED:
                out.append(d)
        return out

    def decisions_by_scope(self, scope: Scope, as_of: datetime | None = None) -> list[Decision]:
        """Currently-valid, non-superseded decisions with the given scope."""
        as_of = as_of or datetime.now(UTC)
        out: list[Decision] = []
        for d in self.iter_decisions():
            if (
                d.scope == scope
                and d.status not in (DecisionStatus.SUPERSEDED, DecisionStatus.REJECTED)
                and (d.valid_to is None or d.valid_to > as_of)
            ):
                out.append(d)
        return out

    def find_abstract_entity(self, canonical_name: str) -> Entity | None:
        """Read-only lookup of an abstract entity by canonical_name (never creates).

        Deterministic under a duplicate (design D4): returns the LOWEST ``entity_id`` when
        more than one row matches — see ``find_entity``'s docstring for the full reasoning
        (a duplicate is legal, D2; "lowest" is stable, not merely "first-returned")."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT data FROM entities WHERE canonical_name = ?", (canonical_name,)
            ).fetchall()
        matches = [
            e
            for e in (Entity.model_validate_json(row["data"]) for row in rows)
            if e.kind == EntityKind.ABSTRACT
        ]
        return min(matches, key=lambda e: e.entity_id) if matches else None

    def iter_initiatives(self) -> Iterator[Initiative]:
        with self._lock:
            rows = self._conn.execute("SELECT data FROM initiatives").fetchall()
        for row in rows:
            yield Initiative.model_validate_json(row["data"])

    def iter_concrete_entities(self) -> Iterator[Entity]:
        """Concrete entities with a descriptor — the sync job's rebind population."""
        with self._lock:
            rows = self._conn.execute("SELECT data FROM entities").fetchall()
        for row in rows:
            e = Entity.model_validate_json(row["data"])
            if e.kind == EntityKind.CONCRETE and e.descriptor is not None:
                yield e

    # -- domains --------------------------------------------------------------
    #
    # Append-only, mirroring decisions: add_domain writes a brand-new row; supersede_domain
    # closes the predecessor (status -> superseded) and writes a new row with `supersedes`
    # set in the same transaction (see add_decision for the parallel). Slug uniqueness is
    # enforced only against *live* domains (proposed | accepted) so a superseded or dropped
    # domain frees its slug back up — see docs/concepts/mind-model.md#domain-lifecycle.

    def add_domain(self, domain: Domain) -> Domain:
        """Append a new Domain, enforcing slug-uniqueness (among live domains) and
        parent existence + acyclicity.

        Raises if ``domain.domain_id`` already exists: append-only, mirroring
        ``add_decision`` — without the guard, an existing id would silently rewrite the
        row via the ``ON CONFLICT DO UPDATE`` in ``_write_domain``, erasing history.
        Reversal goes through ``supersede_domain`` instead.
        """
        with self._mutation():
            if self.get_domain(domain.domain_id) is not None:
                raise ValueError(f"domain {domain.domain_id} already exists — use supersede_domain")
            self._validate_domain_write(domain)
            self._write_domain(domain)
        return domain

    def supersede_domain(self, old_id: str, new_domain: Domain) -> Domain:
        """Append-only reversal: close ``old_id`` (status -> superseded) and write
        ``new_domain`` (whose ``supersedes`` must equal ``old_id``) in the same
        transaction. The same slug is allowed because the successor's slug-uniqueness
        check excludes ``old_id`` — see ``_validate_domain_write``'s ``exclude_id``.

        The successor is validated FIRST, before the predecessor's row is touched: if
        validation raises (e.g. a slug collision against some *other* live domain, or a
        bad parent), nothing has been written yet — neither the index nor a canonical
        file — so there is nothing to leak. Once validation passes, the SUCCESSOR is
        written first and the predecessor is flipped only afterwards (mirrors
        ``add_decision``/``ratify``'s order): a canonical-file write is a durable
        filesystem replace that ``self._conn.rollback()`` cannot undo, so flipping the
        predecessor FIRST (the old order) could leave a durable "superseded with zero
        successors" domain on disk if the successor's write then failed. Writing the
        successor first means a failure there leaves the predecessor untouched — the
        tolerated "both still live" shape — and the trailing rollback below still guards
        the INDEX transaction for the (now much narrower) window between the two writes.

        Also part of that up-front validation: ``new_domain.domain_id`` must not already
        exist. Without this guard a successor reusing ``old_id`` (or any THIRD domain's
        id) would silently rewrite that row in place via the ``ON CONFLICT DO UPDATE`` in
        ``_write_domain`` — erasing history for an id that was supposed to stay append-
        only, exactly like ``add_domain``'s own existing-id guard.
        """
        with self._mutation():
            old = self.get_domain(old_id)
            if old is None:
                raise ValueError(f"supersede references unknown domain {old_id!r}")
            if new_domain.supersedes != old_id:
                raise ValueError("new_domain.supersedes must equal old_id")
            if self.get_domain(new_domain.domain_id) is not None:
                raise ValueError(
                    f"domain {new_domain.domain_id} already exists — successor must be a new domain"
                )
            self._validate_domain_write(new_domain, exclude_id=old_id)
            self._write_domain(new_domain)
            old.status = DomainStatus.SUPERSEDED
            self._write_domain(old)
        return new_domain

    def _validate_domain_write(self, domain: Domain, *, exclude_id: str | None = None) -> None:
        """Slug-uniqueness (among live: proposed | accepted) + parent existence and
        acyclicity. Shared by add_domain and supersede_domain.

        ``exclude_id`` lets supersede_domain validate the successor while the
        predecessor is still live (not yet flipped to superseded): without it, a
        same-slug supersede would spuriously collide with its own still-live
        predecessor.

        Checks EVERY live holder of the slug, not just one (review round 3 fix: the prior
        version picked a single, arbitrary row via a bare ``SELECT`` with no ``ORDER BY``
        — harmless with at most one live holder, which write-time uniqueness normally
        guarantees, but a cross-branch merge can legitimately leave TWO live domains
        sharing a slug — see design §6 and ``domain_slug_conflicts``. In that shape, the
        old single-row check could non-deterministically raise or pass depending on which
        of the two rows sqlite happened to return, even when ``exclude_id`` correctly named
        the OTHER one: resolving the conflict via ``supersede_domain`` on either duplicate
        could spuriously fail. Naming every blocking id in the error is also strictly more
        useful than naming just whichever one won the race.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT domain_id FROM domains WHERE slug = ? AND status IN (?, ?)",
                (domain.slug, DomainStatus.PROPOSED.value, DomainStatus.ACCEPTED.value),
            ).fetchall()
            blocking = sorted(
                r["domain_id"] for r in rows if r["domain_id"] not in (domain.domain_id, exclude_id)
            )
            if blocking:
                raise ValueError(
                    f"slug {domain.slug!r} already used by a live domain ({', '.join(blocking)})"
                )
            if domain.parent_id is not None:
                self._check_domain_parent_acyclic(domain.domain_id, domain.parent_id)

    def _check_domain_parent_acyclic(self, domain_id: str, parent_id: str) -> None:
        """``parent_id`` must reference an existing domain, and walking the parent chain
        from it must never loop back to ``domain_id`` (self-parent included)."""
        seen: set[str] = set()
        current: str | None = parent_id
        while current is not None:
            if current == domain_id or current in seen:
                raise ValueError(f"parent_id {parent_id!r} would create a cycle")
            seen.add(current)
            parent = self.get_domain(current)
            if parent is None:
                raise ValueError(f"parent_id references unknown domain {current!r}")
            current = parent.parent_id

    def _write_domain(self, domain: Domain, *, write_canonical: bool = True) -> None:
        """``write_canonical=False`` is the volatile-only path: used exclusively by
        ``refresh_domain_communities`` to update ONLY the index (the canonical
        ``domains/<id>.json`` file never contains ``communities`` in the first place, so
        skipping it isn't just an optimization — it's what keeps a routine sync pass from
        touching a committed file at all)."""
        with self._lock:
            if write_canonical:
                self._write_domain_canonical(domain)
                self._touch_digest()
            self._index_write_domain(domain)

    def get_domain(self, domain_id: str) -> Domain | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT data FROM domains WHERE domain_id = ?", (domain_id,)
            ).fetchone()
        return Domain.model_validate_json(row["data"]) if row else None

    def find_domain_by_slug(self, slug: str) -> Domain | None:
        """Best non-superseded domain with this slug (mirrors the "non-superseded"
        convention used for decisions, e.g. find_decision_by_title: only SUPERSEDED is
        excluded, so a dropped domain is still findable by slug).

        Deterministic preference among candidates: accepted > proposed > dropped, and
        newest first within a status (domain_id is a ULID, so it sorts by creation time).
        Without an explicit order, sqlite returns rows in rowid/insertion order, which
        previously meant a drop-then-recreate at the same slug could resolve back to the
        older, dropped row instead of the live recreation.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT data FROM domains WHERE slug = ? AND status != ? "
                "ORDER BY CASE status WHEN ? THEN 0 WHEN ? THEN 1 WHEN ? THEN 2 ELSE 3 END, "
                "domain_id DESC",
                (
                    slug,
                    DomainStatus.SUPERSEDED.value,
                    DomainStatus.ACCEPTED.value,
                    DomainStatus.PROPOSED.value,
                    DomainStatus.DROPPED.value,
                ),
            ).fetchall()
        for row in rows:
            return Domain.model_validate_json(row["data"])
        return None

    def find_domains_by_community(self, community_id: str) -> list[Domain]:
        """ALL ACCEPTED domains whose ``communities`` contains ``community_id``, newest
        first — the retrieval read path's bucket-C union (``retrieval.rank_decisions``,
        Gate-5 finding 2). Two accepted domains can legitimately cover the same community
        at once (not prevented at write time — an "orphan window" between one domain's
        acceptance and a later re-scope), and retrieval must surface every one of them: a
        decision tier-1-bound to an OLDER covering domain must still surface even after a
        NEWER domain also claims the community. ``domain_id`` is a ULID, so ``ORDER BY
        domain_id DESC`` is newest-first (matters only to callers that want just the
        newest — see ``find_domain_by_community`` below).
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT data FROM domains WHERE status = ? ORDER BY domain_id DESC",
                (DomainStatus.ACCEPTED.value,),
            ).fetchall()
        out: list[Domain] = []
        for row in rows:
            d = Domain.model_validate_json(row["data"])
            if community_id in d.communities:
                out.append(d)
        return out

    def find_domain_by_community(self, community_id: str) -> Domain | None:
        """The single newest ACCEPTED domain covering ``community_id`` — what
        ``anchoring.resolve_and_bind`` uses to pick ONE Tier-1 binding target at capture
        time. This is deliberately narrower than ``find_domains_by_community`` above: a
        binding write is a one-time pick, and "newest accepted domain" is a fine,
        deterministic tie-break for it — the asymmetry with retrieval (which needs the
        FULL set, not just this newest one) is why the two methods exist side by side
        rather than retrieval calling this one and taking ``[0]`` itself.
        """
        domains = self.find_domains_by_community(community_id)
        return domains[0] if domains else None

    def refresh_domain_communities(self, domain_id: str, communities: list[str]) -> Domain:
        """Sanctioned mutable-field update for ``Domain.communities`` — the domain analog
        of ``Entity.last_seen_*`` (see CLAUDE.md): a durable->engine mapping refreshed by
        ``sync``, NOT content. Updates ONLY the ``communities`` field in place; title,
        summary, parent_id, and status are untouched and stay governed exclusively by
        ``supersede_domain``/``ratify_domains``. No-ops (no write) when the value is
        unchanged, so repeated sync passes over a stable graph never touch the row —
        writes ONLY the index (the canonical ``domains/<id>.json`` file never contains
        ``communities`` — see ``_write_domain``), so this never dirties git.
        """
        with self._mutation():
            d = self.get_domain(domain_id)
            if d is None:
                raise ValueError(f"domain {domain_id!r} not found")
            if d.communities == communities:
                return d
            updated = d.model_copy(update={"communities": communities})
            self._write_domain(updated, write_canonical=False)
        return updated

    def iter_domains(self, status: DomainStatus | None = None) -> Iterator[Domain]:
        with self._lock:
            if status is None:
                rows = self._conn.execute("SELECT data FROM domains").fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT data FROM domains WHERE status = ?", (status.value,)
                ).fetchall()
        for row in rows:
            yield Domain.model_validate_json(row["data"])

    def ratify_domains(
        self,
        accept: list[str] | None = None,
        drop: list[str] | None = None,
        *,
        actor: str | None = None,
    ) -> dict[str, str]:
        """Bulk ratification of proposed domains — mirrors the decisions ratify result
        shape (server.py's ``_ratify_decisions_impl``): a dict of domain_id -> outcome
        string ("accepted" | "dropped" | "error: ..."), so a batch partially succeeds
        instead of one bad id aborting the rest.

        ``actor`` (keyword-only, design D2/T17) stamps every domain accepted in this
        call with that identity instead of the git one — the auto-ratify caller's
        ``"auto:<policy>"`` stamp. Forwarded to ``_ratifier_identity`` unconditionally
        (even at its default ``None``, which that function treats exactly like never
        having called it with an argument at all): every existing caller (MCP,
        ``sidegraph-ratify``) passes nothing, so behavior is byte-identical to before —
        the git identity lookup runs unchanged.

        Accepting flips proposed -> accepted (PROPOSED only) AND mints (get-or-create,
        idempotent) the paired abstract entity ``domain:<slug>`` so AnchorBinding machinery
        works unchanged.

        Dropping flips proposed OR accepted -> dropped; no entity is minted (an already-
        minted ``domain:<slug>`` abstract entity from a prior accept is left as-is — see
        CLAUDE.md invariant #2, entities are never deleted either). Extended beyond
        proposed-only in review round 3 (design §6, resolving a cross-branch slug
        conflict — see ``domain_slug_conflicts``): two branches can each independently
        ACCEPT a domain with the same slug, and ``supersede_domain`` cannot resolve that
        shape at all (a successor keeping the same slug would just collide with the OTHER
        still-live duplicate — see ``_validate_domain_write``). Domains are the owned
        abstraction layer, not a memory record — unlike ``Decision``, whose drop is
        deliberately proposal-only (see :meth:`drop`, UNCHANGED by this), retiring an
        accepted ``Domain`` is a legitimate, append-only-safe operation: the file stays,
        only its status flips, and it remains fully retrievable (and, once terminal,
        compactable — see design §7) via ``DomainStatus.DROPPED``, which already existed
        for exactly this "no longer wanted" case.

        Each accept/drop item is wrapped in its OWN ``_mutation()`` (design D2a) — not one
        shared transaction for the whole batch (that's the point: one bad id must not abort
        the rest), and deliberately not the whole METHOD in a single ``_mutation()`` either:
        that would roll back an EARLIER item that already committed the moment a LATER one
        failed, which is exactly the behavior this method's own contract rules out. Within a
        single accept, the entity mint's nested call (``get_or_create_abstract_entity`` ->
        ``upsert_entity`` -> its own ``_mutation()``) runs at depth 2 and so does not commit
        or roll back on its own (design D2/D2a) — only the item's own outermost
        ``_mutation()`` (depth 1) does, committing the domain's status flip and the entity
        mint together, once, at the end of that item. What *is* shared across the whole call
        is lock scope, not transaction scope: the entire loop still runs under
        ``self._lock`` (an outer acquisition around both loops, not replaced by each item's
        own nested one — ``RLock`` makes nesting the two free), so no other thread's write
        interleaves mid-batch.
        """
        out: dict[str, str] = {}
        with self._lock:
            for domain_id in accept or []:
                try:
                    with self._mutation():
                        d = self.get_domain(domain_id)
                        if d is None or d.status != DomainStatus.PROPOSED:
                            raise ValueError(f"domain {domain_id!r} is not proposed")
                        d.status = DomainStatus.ACCEPTED
                        d.ratified_at = datetime.now(UTC)
                        d.ratified_by = _ratifier_identity(actor)
                        self._write_domain(d)
                        self.get_or_create_abstract_entity(f"domain:{d.slug}")
                    out[domain_id] = "accepted"
                except ValueError as e:
                    out[domain_id] = f"error: {e}"
            for domain_id in drop or []:
                if domain_id in out:
                    out[domain_id] = f"{out[domain_id]} (drop ignored)"
                    continue
                try:
                    with self._mutation():
                        d = self.get_domain(domain_id)
                        if d is None or d.status not in (
                            DomainStatus.PROPOSED,
                            DomainStatus.ACCEPTED,
                        ):
                            raise ValueError(f"domain {domain_id!r} is not proposed or accepted")
                        d.status = DomainStatus.DROPPED
                        self._write_domain(d)
                    out[domain_id] = "dropped"
                except ValueError as e:
                    out[domain_id] = f"error: {e}"
        return out

    # -- ratification (Stage 5) ----------------------------------------------

    def iter_proposed(self) -> Iterator[Decision]:
        """Decisions awaiting ratification (status == proposed)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT data FROM decisions WHERE status = 'proposed'"
            ).fetchall()
        for row in rows:
            yield Decision.model_validate_json(row["data"])

    def pending_ratification_counts(self) -> tuple[int, int, int]:
        """``(decisions, standalone_facts, domains)`` awaiting ratification — the queue
        SessionStart's pending-ratification line summarizes. See design/superpowers/specs/
        2026-07-10-ratification-ux-and-mcp-gaps-design.md.

        Mirrors ``server._list_proposed_impl``'s decomposition (same "items to review"
        mental model): proposed decisions; proposed facts NOT supporting any proposed
        decision (facts riding a proposed decision's cascade are covered by it and must
        not be double-counted — see ``Store.ratify``); proposed domains."""
        proposals = list(self.iter_proposed())
        nested = {
            f.id
            for d in proposals
            for f in self.facts_for_decision(d.id)
            if f.status == DecisionStatus.PROPOSED
        }
        standalone = [f for f in self.iter_proposed_facts() if f.id not in nested]
        domains = list(self.iter_domains(status=DomainStatus.PROPOSED))
        return (len(proposals), len(standalone), len(domains))

    def ratify(
        self,
        decision_id: str,
        *,
        actor: str | None = None,
        cascade_guard: Callable[[Sequence[Fact]], bool] | None = None,
    ) -> tuple[Decision, list[Fact]]:
        """Flip a proposed decision to accepted (the human's one-tap gesture).

        Deferred supersession (design §3 option (b), shared by every ratify path — MCP
        ``ratify``/``ratify_decisions`` and ``sidegraph-ratify`` alike, since both route
        through this method): when the now-accepted decision ``supersedes`` a predecessor
        that is STILL OPEN (status ``accepted`` or ``proposed`` — i.e. never closed at
        write time, see ``add_decision(close_predecessor=False)``), that predecessor is
        closed (``valid_to`` + status ``superseded``) in this SAME operation — this is what
        actually performs the supersession a doc-import ``--propose`` deferred. A
        predecessor that's already ``superseded`` (the ordinary path, where
        ``add_decision`` closed it immediately at write time) is left alone — a no-op here,
        not a double-close. Dropping the proposal instead (:meth:`drop`) never touches the
        predecessor at all.

        ``actor`` (keyword-only, design D2/T17): an explicit stamp for this ratification,
        used by auto-ratification (the ``"auto:<policy>"`` stamp, never called by a human
        path) instead of the git identity. Forwarded to ``_ratifier_identity``
        unconditionally, including at its default ``None`` — every existing caller
        passes nothing, so behavior is byte-identical to before it existed: the git
        identity lookup runs unchanged. A blank/whitespace ``actor`` is handled by
        ``_ratifier_identity`` itself (falls back to the git identity, never stores the
        blank string).

        ``cascade_guard`` (keyword-only, design D2 checkpoint-2 fix, Ruling Q): closes the
        race window between an outer eligibility check and this method's own write (external
        review finding A1, reproduced by ``scratchpad/probe_race.py`` — a fact landing
        between the two could self-certify with no anchor). When supplied, the whole
        transition runs under ``_mutation(immediate=True)`` so the write lock is taken
        BEFORE the cascade set is even read — see ``_mutation``'s own docstring for why
        ``immediate`` is required here: an outer mutation that has only READ so far holds no
        lock, so without it a second connection could still slip a write in between. The
        cascade set (still-``proposed`` facts whose ``supports`` names this decision, read
        fresh under that lock) is handed to the guard; a ``False`` return raises
        ``ValueError`` BEFORE any write happens — no status flip, no stamp, no predecessor
        close — leaving the decision proposed. Omitted (every existing human path — MCP,
        CLI, doc-import): behavior is byte-identical to before this parameter existed, plain
        ``_mutation()``, no guard, no re-check. Only ``capture._auto_ratify``'s decision route
        ever supplies one.

        Cascade (facts layer, design §2026-07-10): every still-``proposed`` :class:`Fact`
        whose ``supports`` names this decision rides its verdict — flipped to ``accepted``
        here, in the same locked body, and returned alongside the decision. The cascaded
        facts inherit ``ratified_by`` directly from ``d`` below, so an explicit ``actor``
        reaches them the same way the git identity always did — no separate threading.
        """
        with self._mutation(immediate=cascade_guard is not None):
            d = self.get_decision(decision_id)
            if d is None or d.status != DecisionStatus.PROPOSED:
                raise ValueError(f"decision {decision_id!r} is not proposed")

            if cascade_guard is not None:
                cascade_candidates = [
                    fact for fact in self.iter_proposed_facts() if decision_id in fact.supports
                ]
                if not cascade_guard(cascade_candidates):
                    raise ValueError(
                        f"cascade guard refused auto-ratification of decision {decision_id!r}"
                    )

            d.status = DecisionStatus.ACCEPTED
            d.ratified_at = datetime.now(UTC)
            d.ratified_by = _ratifier_identity(actor)
            self._write_decision(d)
            if d.supersedes:
                predecessor = self.get_decision(d.supersedes)
                if predecessor is not None and predecessor.status in (
                    DecisionStatus.ACCEPTED,
                    DecisionStatus.PROPOSED,
                ):
                    predecessor.status = DecisionStatus.SUPERSEDED
                    predecessor.valid_to = predecessor.valid_to or max(
                        datetime.now(UTC), predecessor.valid_from
                    )
                    self._write_decision(predecessor)

            cascaded: list[Fact] = []
            for fact in self.iter_proposed_facts():
                if decision_id in fact.supports:
                    fact.status = DecisionStatus.ACCEPTED
                    fact.ratified_at = d.ratified_at
                    fact.ratified_by = d.ratified_by
                    self._write_fact(fact)
                    cascaded.append(fact)
        return d, cascaded

    def drop(self, decision_id: str) -> tuple[Decision, list[Fact]]:
        """Reject a proposed decision. Append-only: sets valid_to + rejected, never deletes.

        Cascade (facts layer, design §2026-07-10): every still-``proposed`` :class:`Fact`
        for which ALL ``supports`` targets are now ``rejected`` is dropped too (``valid_to``
        + status ``rejected``) — a fact with any surviving supporter is kept; a fact with no
        supporters at all (``supports == []``) never cascades. Runs in the same locked body,
        after the decision flip, and the dropped facts are returned alongside it.
        """
        with self._mutation():
            d = self.get_decision(decision_id)
            if d is None or d.status != DecisionStatus.PROPOSED:
                raise ValueError(f"decision {decision_id!r} is not proposed")
            d.status = DecisionStatus.REJECTED
            d.valid_to = d.valid_to or max(datetime.now(UTC), d.valid_from)
            self._write_decision(d)

            cascaded: list[Fact] = []
            for fact in self.iter_proposed_facts():
                if self._all_supporters_rejected(fact):
                    fact.status = DecisionStatus.REJECTED
                    fact.valid_to = fact.valid_to or max(datetime.now(UTC), fact.valid_from)
                    self._write_fact(fact)
                    cascaded.append(fact)
        return d, cascaded

    def _all_supporters_rejected(self, fact: Fact) -> bool:
        """Drop-cascade predicate: true iff ``fact.supports`` is non-empty and every
        decision it names is now ``rejected`` — an empty ``supports`` never cascades."""
        if not fact.supports:
            return False
        return all(
            (dec := self.get_decision(sid)) is not None and dec.status == DecisionStatus.REJECTED
            for sid in fact.supports
        )

    def ratify_fact(self, fact_id: str, *, actor: str | None = None) -> Fact:
        """Flip a proposed fact to accepted directly (no supporting decision involved).

        ``actor`` (keyword-only, design D2/T17): same contract as :meth:`ratify` — an
        explicit stamp for auto-ratification's standalone-fact path, defaulting to
        ``None`` so every existing caller (which passes nothing) is unaffected and still
        gets the plain git-identity lookup.
        """
        with self._mutation():
            f = self.get_fact(fact_id)
            if f is None or f.status != DecisionStatus.PROPOSED:
                raise ValueError(f"fact {fact_id!r} is not proposed")
            f.status = DecisionStatus.ACCEPTED
            f.ratified_at = datetime.now(UTC)
            f.ratified_by = _ratifier_identity(actor)
            self._write_fact(f)
        return f

    def drop_fact(self, fact_id: str) -> Fact:
        """Reject a proposed fact directly. Append-only: sets valid_to + rejected."""
        with self._mutation():
            f = self.get_fact(fact_id)
            if f is None or f.status != DecisionStatus.PROPOSED:
                raise ValueError(f"fact {fact_id!r} is not proposed")
            f.status = DecisionStatus.REJECTED
            f.valid_to = f.valid_to or max(datetime.now(UTC), f.valid_from)
            self._write_fact(f)
        return f

    # -- capture ledger (Stage 5) --------------------------------------------
    #
    # Local-only bookkeeping (design §1: "capture ledger" is explicitly volatile) — no
    # canonical file, index.db only.

    def was_captured(self, session_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM capture_sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
        return row is not None

    def mark_captured(self, session_id: str) -> None:
        with self._mutation():
            self._conn.execute(
                "INSERT OR IGNORE INTO capture_sessions (session_id, captured_at) VALUES (?, ?)",
                (session_id, datetime.now(UTC).isoformat()),
            )

    # -- retrieval telemetry (design/superpowers/specs/
    # 2026-07-25-retrieval-telemetry-design.md) -----------------------------
    #
    # Local-only bookkeeping, index.db only — no canonical file. Same precedent as the
    # capture ledger above: derived state that must survive `_reload_index_from_canonical`
    # (its DROP list names the six record tables explicitly), because losing the history on
    # every `git pull` would make the counts meaningless for the long-lived question they
    # answer.

    def record_retrieval(self, record_ids: Iterable[str], seeds: Iterable[str]) -> None:
        """Count records that reached a render, and areas that were asked about.

        Derived state, index-only: this is a *read* path, and writing anything canonical
        here would make retrieval dirty git — the sync-clean invariant's most direct
        possible violation. Deduped per call: one render showing a record twice is one
        showing, and a seed counts once per query however many records came back, or the
        ratio the doctor check rests on stops meaning anything.
        """
        now = datetime.now(UTC).isoformat()
        with self._mutation():
            for rid in dict.fromkeys(record_ids):
                self._conn.execute(
                    "INSERT INTO retrieval_shows (record_id, shows, last_shown_at) "
                    "VALUES (?, 1, ?) ON CONFLICT(record_id) DO UPDATE SET "
                    "shows = shows + 1, last_shown_at = excluded.last_shown_at",
                    (rid, now),
                )
            for seed in dict.fromkeys(seeds):
                self._conn.execute(
                    "INSERT INTO retrieval_seeds (seed, queries, last_seen_at) "
                    "VALUES (?, 1, ?) ON CONFLICT(seed) DO UPDATE SET "
                    "queries = queries + 1, last_seen_at = excluded.last_seen_at",
                    (seed, now),
                )

    def retrieval_shows(self) -> dict[str, int]:
        with self._lock:
            return {
                row["record_id"]: row["shows"]
                for row in self._conn.execute("SELECT record_id, shows FROM retrieval_shows")
            }

    def retrieval_seed_queries(self) -> dict[str, int]:
        with self._lock:
            return {
                row["seed"]: row["queries"]
                for row in self._conn.execute("SELECT seed, queries FROM retrieval_seeds")
            }

    # -- coverage telemetry: the ordered journal (design/superpowers/specs/
    # 2026-07-26-retrieval-coverage-telemetry-design.md, D1) ------------------
    #
    # Same index-only contract as the two counter tables above, for the same reason: this
    # is a read path, and writing anything canonical here would make retrieval dirty git.
    # What the counters cannot express is *when* and *in which session*, which is the whole
    # question this journal exists to answer.

    def _append_event(
        self,
        session_id: str,
        kind: str,
        key: str,
        detail: str | None,
        agent: str | None = None,
    ) -> None:
        with self._mutation():
            self._conn.execute(
                INSERT_EVENT_SQL,
                (session_id, datetime.now(UTC).isoformat(), kind, key, detail, agent),
            )

    def record_touch(self, session_id: str, path: str, tool: str, agent: str | None = None) -> None:
        """Record that a file was touched during a session.

        All four values are **opaque** to the core: ``session_id`` is whatever the host
        calls a session, ``tool`` whatever it calls the tool and ``agent`` whatever it calls
        an agent inside that session (``None`` for the session's own agent). The core never
        interprets any of them — this keeps the host seam one-directional (see CLAUDE.md;
        ``capture_sessions`` is the precedent). ``path`` must already be repo-relative;
        normalizing it is the host's job, because only the host knows the root it was given
        (D3). The agent never changes which session the row belongs to, so session counts
        stay what they were.
        see design/superpowers/specs/2026-10-01-per-agent-hook-state-design.md (D3)
        """
        self._append_event(session_id, "touch", path, tool, agent)

    def record_retrieval_events(
        self,
        session_id: str,
        seeds: Iterable[str],
        shows: Iterable[tuple[str, str]],
    ) -> None:
        """Record what a retrieval asked about and what it surfaced.

        ``shows`` are ``(record_id, anchored_file_path)`` pairs — one event per pair, since
        a record anchored to three files can be reached by a touch of any of them. Both
        sequences are deduped per call: one render showing a record twice is one showing.
        """
        now = datetime.now(UTC).isoformat()
        with self._mutation():
            for seed in dict.fromkeys(seeds):
                self._conn.execute(
                    "INSERT INTO retrieval_events (session_id, at, kind, key, detail) "
                    "VALUES (?, ?, 'seed', ?, NULL)",
                    (session_id, now, seed),
                )
            for record_id, path in dict.fromkeys(shows):
                self._conn.execute(
                    "INSERT INTO retrieval_events (session_id, at, kind, key, detail) "
                    "VALUES (?, ?, 'show_anchor', ?, ?)",
                    (session_id, now, path, record_id),
                )

    def retrieval_events(self, session_id: str | None = None) -> list[dict[str, str | None]]:
        """Journal rows in insertion order, optionally scoped to one session."""
        sql = "SELECT session_id, at, kind, key, detail FROM retrieval_events"
        params: tuple[str, ...] = ()
        if session_id is not None:
            sql += " WHERE session_id = ?"
            params = (session_id,)
        sql += " ORDER BY id"
        with self._lock:
            return [dict(row) for row in self._conn.execute(sql, params)]

    def record_render_event(
        self,
        session_id: str,
        *,
        intent: str | None,
        selected: int,
        emitted: int,
        degraded: int,
        dropped_for_budget: int,
        chars_used: int,
        had_rejected: bool,
        had_superseded: bool,
    ) -> None:
        """Record what one retrieval render selected and what survived the budget.

        ``degraded`` and ``dropped_for_budget`` are kept apart deliberately: the ranker
        retries a detailed line at the tight tier before giving up, so one "dropped" number
        would conflate a record that shrank with one that vanished — the two demand opposite
        fixes (design/superpowers/specs/2026-09-18-usage-stats-design.md, D5).
        """
        with self._mutation():
            self._conn.execute(
                "INSERT INTO render_events (session_id, at, intent, selected, emitted, "
                "degraded, dropped_for_budget, chars_used, had_rejected, had_superseded) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    session_id,
                    datetime.now(UTC).isoformat(),
                    intent,
                    selected,
                    emitted,
                    degraded,
                    dropped_for_budget,
                    chars_used,
                    int(had_rejected),
                    int(had_superseded),
                ),
            )

    def render_events(self, session_id: str | None = None) -> list[dict[str, object]]:
        """Render-journal rows in insertion order, optionally scoped to one session.

        See design/superpowers/specs/2026-09-18-usage-stats-design.md, D5.
        """
        sql = (
            "SELECT session_id, at, intent, selected, emitted, degraded, "
            "dropped_for_budget, chars_used, had_rejected, had_superseded FROM render_events"
        )
        params: tuple[str, ...] = ()
        if session_id is not None:
            sql += " WHERE session_id = ?"
            params = (session_id,)
        sql += " ORDER BY id"
        with self._lock:
            return [dict(row) for row in self._conn.execute(sql, params)]

    # Every append-only journal table. The prune sweeps this list, so a journal added
    # without being listed here would grow without bound -- the reason it is one constant.
    _JOURNAL_TABLES: tuple[str, ...] = ("retrieval_events", "render_events")

    def prune_telemetry_events(self, older_than_days: int = 30) -> int:
        """Delete events older than the retention window across EVERY journal table.

        Called once per session from ``SessionStart`` (D7) — an append-only journal with no
        pruning is a predictable disk-growth bug, and doing it off every hot path keeps the
        cost invisible. Was ``prune_retrieval_events``, which named one table and so gave a
        second journal no retention at all (usage-stats spec, D4).
        """
        cutoff = (datetime.now(UTC) - timedelta(days=older_than_days)).isoformat()
        deleted = 0
        with self._mutation():
            # Safe to interpolate: a private constant of literal identifiers, never input.
            for table in self._JOURNAL_TABLES:
                deleted += self._conn.execute(
                    f"DELETE FROM {table} WHERE at < ?", (cutoff,)
                ).rowcount
        return deleted

    # -- initiatives --------------------------------------------------------

    def upsert_initiative(self, initiative: Initiative) -> Initiative:
        with self._mutation():
            self._write_initiative_canonical(initiative)
            self._index_write_initiative(initiative)
            self._touch_digest()
        return initiative

    # -- compaction (design §7) ------------------------------------------------
    #
    # Terminal-status decisions/domains never change again under append-only rules, so they
    # need neither mergeability nor PR review. ``compact`` packs them into an immutable
    # ``archive/<date>-<seq>.jsonl`` segment and removes their individual hot files in the
    # same operation — CLAUDE.md invariant #2 (append-only) still holds: the records MOVE,
    # they are never lost or mutated (``_reload_index_from_canonical`` above and the loader
    # helpers below still surface them exactly as before). Entities, bindings, and
    # initiatives stay hot in v1 — a compacted decision's bindings remain live, ordinary hot
    # files (retrieval of superseded history still needs them). Explicit, human-run
    # maintenance only: nothing in sync/retrieval/ratify calls this.

    def _list_archive_segments(self) -> list[Path]:
        archive_dir = self.path / _ARCHIVE_SUBDIR
        if not archive_dir.is_dir():
            return []
        return sorted(archive_dir.glob("*.jsonl"))

    @staticmethod
    def _read_archive_segment(
        path: Path,
        *,
        tolerate_corrupt_lines: bool = False,
        bad_lines: list[int] | None = None,
    ) -> list[dict]:
        """Parse every line of one archive segment. The segment is read as bytes and decoded
        line by line, so one line of invalid UTF-8 costs only that line.

        A malformed line (review Minor-5: a merge-mangled segment, truncated write, or hand
        edit) raises a ``ValueError`` naming the offending segment PATH and LINE NUMBER — a
        bare ``JSONDecodeError`` gives no clue which of potentially many segments is at fault
        (mirrors ``_validate_legacy_rows``'s "name the offending row" convention).

        With ``tolerate_corrupt_lines`` the line is skipped instead, with a warning, and its
        number is appended to ``bad_lines`` when one is given: only the reload asks for this,
        because a compaction is a mutation and must not run over a segment it cannot fully read
        (design/superpowers/specs/2026-10-03-store-survives-a-bad-file-design.md D4)."""
        out: list[dict] = []
        for lineno, raw_line in enumerate(path.read_bytes().splitlines(), start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line.decode("utf-8")))
            except _UNPARSEABLE as e:
                if not tolerate_corrupt_lines:
                    raise ValueError(f"corrupt archive segment {path} at line {lineno}: {e}") from e
                _warn_skipped_archive_line(path, f"line {lineno} cannot be read")
                if bad_lines is not None:
                    bad_lines.append(lineno)
        return out

    def _archived_records(
        self,
        *,
        tolerate_corrupt_lines: bool = False,
        bad_segments: list[Path] | None = None,
    ) -> tuple[dict[str, dict], dict[str, dict]]:
        """Every archived decision/domain payload (``record_type`` stripped), keyed by id,
        across ALL segments — oldest segment first, first occurrence wins. Two segments
        legitimately containing the same id (e.g. compaction run independently on two
        branches, later merged) are guaranteed byte-identical by design (terminal records
        never change once archived), so which one wins is never a correctness question —
        see design §7.

        ``tolerate_corrupt_lines`` (the reload only; see ``_read_archive_segment``) also skips
        a line whose payload fails the model, and appends each segment that had any such line
        to ``bad_segments``, once, for the skip list
        (design/superpowers/specs/2026-10-03-store-survives-a-bad-file-design.md D4)."""
        decisions: dict[str, dict] = {}
        domains: dict[str, dict] = {}
        for segment in self._list_archive_segments():
            bad_lines: list[int] = []
            unreadable = False
            for raw in self._read_archive_segment(
                segment, tolerate_corrupt_lines=tolerate_corrupt_lines, bad_lines=bad_lines
            ):
                # One bad line in an immutable segment must wedge nothing (design D8): a
                # non-object, or an id that is not a safe filename stem, is skipped with a
                # warning before ``setdefault`` (a list id would raise ``TypeError``).
                if not isinstance(raw, dict):
                    _warn_skipped_archive_line(segment, "not a JSON object")
                    continue
                record_type = raw.get("record_type")
                payload = {k: v for k, v in raw.items() if k != "record_type"}
                problem = _archive_line_problem(record_type, payload)
                if problem is not None:
                    _warn_skipped_archive_line(segment, problem)
                    continue
                if tolerate_corrupt_lines:
                    problem = _archive_payload_problem(record_type, payload)
                    if problem is not None:
                        _warn_skipped_archive_line(segment, problem)
                        unreadable = True
                        continue
                if record_type == "decision":
                    decisions.setdefault(payload["id"], payload)
                elif record_type == "domain":
                    domains.setdefault(payload["domain_id"], payload)
                # an unrecognized record_type is silently skipped -- forward-compat with a
                # future record kind this version of the code doesn't know how to load yet,
                # rather than a hard failure that blocks opening the whole store.
            if (bad_lines or unreadable) and bad_segments is not None:
                bad_segments.append(segment)
        return decisions, domains

    def _archived_record_ids(self) -> tuple[set[str], set[str]]:
        decisions, domains = self._archived_records()
        return set(decisions), set(domains)

    def _leftover_archived_hot(
        self, archived_decisions: dict[str, dict], archived_domains: dict[str, dict]
    ) -> tuple[set[str], set[str], int]:
        """Ids whose hot canonical file duplicates an ALREADY-archived record exactly —
        crash-window debris from a compact that durably wrote its segment but was
        interrupted before removing the hot file (see :meth:`compact`). These are safe to
        remove without writing a new segment: the archive already has them.

        A hot file that instead DIFFERS from its archived counterpart is corruption-shaped,
        not crash debris — it is left exactly as it is (never destroy data) and surfaced via
        a stderr warning. Returns ``(decision_ids_safe_to_remove, domain_ids_safe_to_remove,
        mismatch_count)``.
        """
        safe_decisions: set[str] = set()
        safe_domains: set[str] = set()
        mismatches = 0
        listed = frozenset(e["path"] for e in self._read_skip_list())
        for did, archived_payload in archived_decisions.items():
            hot = _record_file(self.path, "decisions", did)
            match = (
                None
                if _hot_file_skipped_by_reload(hot, "id", listed)
                else _hot_file_matches(hot, archived_payload)
            )
            if match is True:
                safe_decisions.add(did)
            elif match is False:
                mismatches += 1
                _warn_hot_archive_mismatch("decision", did)
        for dmid, archived_payload in archived_domains.items():
            hot = _record_file(self.path, "domains", dmid)
            match = (
                None
                if _hot_file_skipped_by_reload(hot, "domain_id", listed)
                else _hot_file_matches(hot, archived_payload)
            )
            if match is True:
                safe_domains.add(dmid)
            elif match is False:
                mismatches += 1
                _warn_hot_archive_mismatch("domain", dmid)
        return safe_decisions, safe_domains, mismatches

    def _next_archive_seq(self, archive_dir: Path, date_str: str) -> int:
        """The next unused ``<seq>`` for ``date_str``, given segments already on disk —
        re-globbed fresh on every call (see ``_write_archive_segment``'s retry loop: a
        concurrent writer may have just published between one call and the next, and
        "next available" must reflect that, not a stale snapshot)."""
        existing = [
            seq
            for f in archive_dir.glob(f"{date_str}-*.jsonl")
            if (seq := _parse_segment_seq(f.stem, date_str)) is not None
        ]
        return max(existing, default=0) + 1

    def _write_archive_segment(self, decisions: list[Decision], domains: list[Domain]) -> str:
        """Write ONE new segment containing every one of ``decisions``/``domains``,
        ULID-sorted together, one record per line (see ``_archive_record_line``). Segments
        are write-once (design §7) and this always creates a brand-new file — but unlike
        every other committed file, publishing one is NOT a plain tmp+``os.replace``:

        Review Important-1 (fault-injection finding): the original implementation used a
        FIXED tmp filename and unconditional ``os.replace``. Two compacts racing (two
        processes, or two threads via fastmcp's worker dispatch) could both open that same
        fixed tmp name for writing at once — one writer's truncating ``open(mode="w")``
        can zero out bytes the other had already written, and ``os.replace`` would then
        atomically install that TRUNCATED (even 0-byte) content as "the segment" while the
        caller had already unlinked the hot files it was supposed to be a durable copy of
        — silent, permanent data loss on the next cold reload. ``os.replace`` unconditionally
        overwriting an existing target is also a second problem on its own: it can clobber
        an already-published segment, violating "write once, never rewritten".

        Fixed by: (1) writing to a tmp file with a per-attempt UNIQUE name (pid+uuid, same
        convention as ``_atomic_write_text_race_tolerant`` — no two writers, in this
        process or any other, ever share a tmp path, so no truncation race is possible);
        (2) fsync-ing that tmp file's content before it is ever reachable under a real
        name (review Minor-3 — compaction is the one operation where a power-loss gap
        between "hot files gone" and "segment durable" would delete the only surviving
        copy of a record); (3) publishing via ``os.link`` (an exclusive create — raises
        ``FileExistsError`` if the target name is already taken, never silently clobbers
        it) instead of ``os.replace``; (4) on that ``FileExistsError``, recomputing the
        next available ``<seq>`` and retrying under a NEW name, up to
        ``_ARCHIVE_SEGMENT_PUBLISH_ATTEMPTS`` times; (5) fsync-ing the archive DIRECTORY
        after a successful link, so the new directory entry itself survives a crash, not
        just the file's bytes. The tmp file is always unlinked afterward (its content
        lives on under the published name via the hard link) — success, failure, or
        exhausted retries alike.

        The published name is ``<date>-<seq>-<hash12>.jsonl`` (review Minor-4 amendment —
        see ``_ARCHIVE_SUBDIR``'s docstring for why the content hash is part of the name).
        Any OTHER exception from the publish attempt (anything but ``FileExistsError`` —
        e.g. a genuine disk error) propagates immediately: the caller (``compact``) must
        never proceed to remove a single hot file once this raises, since there is then no
        durable segment guaranteed to contain their content.

        Returns the new segment's path relative to ``self.path``.
        """
        records: list[tuple[str, str]] = [
            (d.id, _archive_record_line("decision", d.model_dump(mode="json"))) for d in decisions
        ] + [
            (dom.domain_id, _archive_record_line("domain", _domain_canonical_payload(dom)))
            for dom in domains
        ]
        records.sort(key=lambda item: item[0])
        text = "\n".join(line for _, line in records) + "\n"
        content_bytes = text.encode("utf-8")
        content_hash12 = hashlib.sha256(content_bytes).hexdigest()[:12]

        archive_dir = self.path / _ARCHIVE_SUBDIR
        archive_dir.mkdir(parents=True, exist_ok=True)
        date_str = datetime.now(UTC).date().isoformat()

        tmp = archive_dir / f"archive-segment.{os.getpid()}.{uuid.uuid4().hex[:12]}.tmp"
        target: Path | None = None
        try:
            with open(tmp, "wb") as fh:
                fh.write(content_bytes)
                fh.flush()
                os.fsync(fh.fileno())

            for _ in range(_ARCHIVE_SEGMENT_PUBLISH_ATTEMPTS):
                seq = self._next_archive_seq(archive_dir, date_str)
                candidate = archive_dir / f"{date_str}-{seq}-{content_hash12}.jsonl"
                try:
                    os.link(tmp, candidate)
                    target = candidate
                    break
                except FileExistsError:
                    continue
            else:
                raise OSError(
                    f"could not publish archive segment for {date_str} after "
                    f"{_ARCHIVE_SEGMENT_PUBLISH_ATTEMPTS} attempts (persistent seq "
                    "collision with concurrent writers)"
                )
        finally:
            tmp.unlink(missing_ok=True)

        try:
            dir_fd = os.open(archive_dir, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass  # best-effort directory-entry durability -- unsupported on some platforms

        assert target is not None  # the loop always sets it before falling through to here
        # Record the stat row only AFTER the link has actually won (digest-integrity design
        # §3): the retry loop above can change the final name, so recording any earlier
        # candidate name would describe a name that was never published. `target` and `tmp`
        # are hard links to the SAME inode, so `target.stat()` here is identical to a stat
        # taken on `tmp` right after its fsync -- either way it is THIS process's own
        # published bytes, never a later replace (archive segments are write-once and never
        # replaced again, so there is no "later writer" window to lose a race in at all).
        self._record_canonical_stat(_ARCHIVE_SUBDIR, target.stem, target.stat())
        return f"{_ARCHIVE_SUBDIR}/{target.name}"

    def compact(
        self, *, older_than_days: int | None = None, dry_run: bool = False
    ) -> CompactReport:
        """Pack every TERMINAL-status decision/domain into a new archive segment and remove
        its hot canonical file, in one operation (design §7; see the class-level comment
        above this section for the append-only guarantee this preserves).

        Facts are NOT compacted in this wave (deferred) — ``facts/`` stays entirely hot
        regardless of status; a compacted fact is future scope, not this task's.

        Terminal = decisions ``superseded``/``rejected``/``deprecated``, domains
        ``superseded``/``dropped`` (see ``_TERMINAL_DECISION_STATUSES`` /
        ``_TERMINAL_DOMAIN_STATUSES``) — statuses append-only rules guarantee can never
        change again. ``proposed``/``accepted`` records of either kind are never touched.

        ``older_than_days``, when given, additionally requires the record's best available
        TERMINAL timestamp to be at least that many days in the past:

        - a decision's ``valid_to`` — always stamped the moment ``add_decision``/``ratify``/
          ``drop`` actually closes it, so this is a reliable signal for every real record.
          Excluded decisions (too young, or defensively when ``valid_to`` is unexpectedly
          ``None`` despite a terminal status) are counted in
          ``CompactReport.skipped_age_filtered``.
        - a domain has NO field anywhere that records when it entered a terminal status (no
          ``valid_to``, nothing in ``Provenance`` either) — rather than guess from e.g. its
          ULID's creation timestamp (which is when it was PROPOSED, not when it became
          superseded/dropped — those can be arbitrarily far apart), every domain is
          conservatively EXCLUDED (kept hot) whenever ``older_than_days`` is set. Counted
          separately, in ``CompactReport.domains_excluded_age_unknown`` (review Minor-7:
          "too recent" and "no timestamp exists to check at all" are different situations
          and read confusingly merged into one number).

        Excluded-by-age records of either kind are left completely untouched — not
        archived, not removed.

        ``dry_run=True`` computes and returns the full report (including which records WOULD
        be archived/cleaned up) without writing or removing anything — not the new segment,
        not a hot file, not even the digest.

        Idempotent / crash-safe: a prior run that durably wrote its segment but crashed
        before removing the now-redundant hot files leaves those files as harmless
        byte-identical duplicates of their archived copy. This run detects them (see
        ``_leftover_archived_hot``) and removes them WITHOUT writing a second, duplicate
        segment for records that are already durably archived — ``CompactReport.
        cleaned_up_hot_files`` counts these separately from freshly-archived records.

        Every hot-file removal (fresh candidates AND crash-window leftovers alike) is
        gated on the file's ON-DISK content still matching exactly what got archived
        (review Minor-6): a candidate is read from the INDEX, which should always mirror
        its hot file, but a removal is destructive enough that this never just trusts that
        invariant — a mismatch (something changed the file out from under this pass, e.g.
        an external hand edit) leaves the file in place and surfaces a warning instead of
        silently discarding newer state nothing else preserved a copy of.
        """
        with self._mutation():
            archived_decisions, archived_domains = self._archived_records()

            cutoff = (
                datetime.now(UTC) - timedelta(days=older_than_days)
                if older_than_days is not None
                else None
            )

            new_decisions: list[Decision] = []
            new_domains: list[Domain] = []
            skipped_age_filtered = 0
            domains_excluded_age_unknown = 0

            for d in self.iter_decisions():
                if d.status not in _TERMINAL_DECISION_STATUSES or d.id in archived_decisions:
                    continue
                if cutoff is not None and (d.valid_to is None or d.valid_to > cutoff):
                    skipped_age_filtered += 1
                    continue
                new_decisions.append(d)

            for dom in self.iter_domains():
                if dom.status not in _TERMINAL_DOMAIN_STATUSES or dom.domain_id in archived_domains:
                    continue
                if cutoff is not None:
                    # No terminal-timestamp field exists on Domain at all -- always
                    # conservative when age-filtering is active.
                    domains_excluded_age_unknown += 1
                    continue
                new_domains.append(dom)

            new_decisions.sort(key=lambda d: d.id)
            new_domains.sort(key=lambda dom: dom.domain_id)

            items = sorted(
                [
                    CompactedRecord(
                        ulid=d.id, kind="decision", status=d.status.value, title=d.title[:60]
                    )
                    for d in new_decisions
                ]
                + [
                    CompactedRecord(
                        ulid=dom.domain_id,
                        kind="domain",
                        status=dom.status.value,
                        title=dom.title[:60],
                    )
                    for dom in new_domains
                ],
                key=lambda item: item.ulid,
            )

            safe_decisions, safe_domains, _mismatches = self._leftover_archived_hot(
                archived_decisions, archived_domains
            )

            if dry_run:
                return CompactReport(
                    segment_path=None,
                    decisions_compacted=len(new_decisions),
                    domains_compacted=len(new_domains),
                    skipped_age_filtered=skipped_age_filtered,
                    domains_excluded_age_unknown=domains_excluded_age_unknown,
                    cleaned_up_hot_files=len(safe_decisions) + len(safe_domains),
                    items=items,
                    dry_run=True,
                )

            segment_relpath = None
            if new_decisions or new_domains:
                segment_relpath = self._write_archive_segment(new_decisions, new_domains)

            # Fresh candidates: only remove a hot file if its ON-DISK content still
            # matches exactly what was just archived (Minor-6) -- never trust the INDEX
            # snapshot alone for a destructive removal. Its canonical_stat row is removed
            # in the SAME step (digest-integrity design Task 2): a file kept because it
            # MISMATCHED (the `continue` below) keeps its row too -- mirroring
            # `_hot_file_matches`'s own skip exactly, so a diverged-but-kept file is never
            # treated as though this index stopped having loaded it.
            for d in new_decisions:
                p = _record_file(self.path, "decisions", d.id)
                if _hot_file_matches(p, d.model_dump(mode="json")) is False:
                    _warn_hot_archive_mismatch("decision", d.id)
                    continue
                p.unlink(missing_ok=True)
                self._delete_canonical_stat("decisions", d.id)
            for dom in new_domains:
                p = _record_file(self.path, "domains", dom.domain_id)
                if _hot_file_matches(p, _domain_canonical_payload(dom)) is False:
                    _warn_hot_archive_mismatch("domain", dom.domain_id)
                    continue
                p.unlink(missing_ok=True)
                self._delete_canonical_stat("domains", dom.domain_id)
            # Crash-window leftovers: _leftover_archived_hot already did the compare above.
            for did in safe_decisions:
                (_record_file(self.path, "decisions", did)).unlink(missing_ok=True)
                self._delete_canonical_stat("decisions", did)
            for dmid in safe_domains:
                (_record_file(self.path, "domains", dmid)).unlink(missing_ok=True)
                self._delete_canonical_stat("domains", dmid)

            cleaned_up = len(safe_decisions) + len(safe_domains)
            if new_decisions or new_domains or cleaned_up:
                self._touch_digest()

            return CompactReport(
                segment_path=segment_relpath,
                decisions_compacted=len(new_decisions),
                domains_compacted=len(new_domains),
                skipped_age_filtered=skipped_age_filtered,
                domains_excluded_age_unknown=domains_excluded_age_unknown,
                cleaned_up_hot_files=cleaned_up,
                items=items,
                dry_run=False,
            )

    @property
    def schema_version(self) -> str:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()
        return row["value"]

    def get_meta(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute(GET_META_SQL, (key,)).fetchone()
        return row["value"] if row else None

    def canonical_digest(self) -> str | None:
        """The digest this index is certified against: the stamp of the canonical files as
        this process last wrote or loaded them (see ``_touch_digest``), or ``None`` when it
        was cleared (a write that could not be certified) and the next open will reload.

        It moves on every canonical write and on every reload, and on nothing else, so a
        derived value that depends only on canonical records can record it and later ask
        whether the store changed, without walking the files. ``None`` means "cannot tell".
        # see design/superpowers/specs/2026-10-01-session-start-text-design.md (D2b)
        """
        return self.get_meta(_CANONICAL_DIGEST_KEY)

    def set_meta(self, key: str, value: str) -> None:
        if key == "schema_version":
            raise ValueError(
                "schema_version is stamped at store creation and must not be overwritten"
            )
        with self._mutation():
            self._conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def claim_meta(self, key: str, value: str) -> bool:
        """Set ``key`` only if it is absent; True iff this call set it.

        One statement, so two processes claiming the same key at once cannot both win: a
        ``get_meta`` followed by a ``set_meta`` leaves a window between them in which both
        read "absent". A hook that nudges once per agent claims its key here and nudges only
        on True. Refuses ``schema_version``, as ``set_meta`` does.
        see design/superpowers/specs/2026-10-01-per-agent-hook-state-design.md (D2)
        """
        if key == "schema_version":
            raise ValueError(
                "schema_version is stamped at store creation and must not be overwritten"
            )
        with self._mutation():
            cursor = self._conn.execute(CLAIM_META_SQL, (key, value))
            return cursor.rowcount == 1

    def update_meta_if(self, key: str, decide: Callable[[str | None], str | None]) -> bool:
        """Read ``key``, let ``decide`` choose the new value, and write it, all under one
        ``BEGIN IMMEDIATE``; True iff a value was written.

        ``decide`` gets the current value (``None`` when the row is absent) and returns the
        string to store, or ``None`` to leave the row untouched. A ``get_meta`` followed by a
        ``set_meta`` is two transactions, so two processes can both read the old value and
        both decide to write; here the second one waits for the first's commit and decides
        against what it wrote. An exception from ``decide`` rolls back and propagates. The
        store never names the key or parses the value: the host passes both. Refuses
        ``schema_version``, as ``set_meta`` does.
        see design/superpowers/specs/2026-10-02-session-start-dedupe-atomic-design.md (D1)
        """
        if key == "schema_version":
            raise ValueError(
                "schema_version is stamped at store creation and must not be overwritten"
            )
        with self._mutation(immediate=True):
            row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
            new = decide(row["value"] if row else None)
            if new is None:
                return False
            self._conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, new),
            )
            return True

    def prune_meta_prefixes(self, prefixes: Iterable[str], older_than: timedelta) -> int:
        """Delete one-shot meta rows under ``prefixes`` that have expired; return how many.

        A row has expired when its value is an ISO timestamp older than ``older_than``, or
        the legacy ``"1"`` that one-shot keys carried before they held a timestamp. Any other
        value is kept. Keys are matched with ``substr`` and never ``LIKE``: ``_`` is a
        ``LIKE`` wildcard and the host's prefixes contain it. An empty prefix would match
        every key and is refused; ``schema_version`` is never deleted.
        see design/superpowers/specs/2026-10-01-per-agent-hook-state-design.md (D4)
        """
        wanted = tuple(prefixes)
        if any(not prefix for prefix in wanted):
            raise ValueError("an empty prefix matches every meta key; name what to prune")
        cutoff = (datetime.now(UTC) - older_than).isoformat()
        deleted = 0
        with self._mutation():
            for prefix in wanted:
                deleted += self._conn.execute(
                    "DELETE FROM meta WHERE substr(key, 1, ?) = ? AND key != 'schema_version' "
                    "AND (value = '1' OR (value GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-"
                    "[0-9][0-9]T*' AND value < ?))",
                    (len(prefix), prefix, cutoff),
                ).rowcount
        return deleted

    def delete_meta(self, key: str) -> None:
        """Remove a derived meta key (a no-op when absent). Refuses ``schema_version``, as
        ``set_meta`` does."""
        if key == "schema_version":
            raise ValueError("schema_version is stamped at store creation and must not be removed")
        with self._mutation():
            self._conn.execute("DELETE FROM meta WHERE key = ?", (key,))

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
