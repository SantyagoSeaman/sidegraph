"""``sidegraph-migrate``: switch a store to format 2, then run maintenance.

Two durable mutations on one ``Store`` handle, in this order (§6.1 as amended by rev 14):

1. publish ``format`` (tmp, ``fsync``, ``os.replace``, ``fsync`` of the store directory): from
   here the store is a valid format-2 store whose records are all per-record files;
2. ``Store.maintain(settled_days=0)``: its own preparation (empty shard files, the
   ``.gitattributes`` line), its own mutation and its own resumable steps.

Every entry condition is judged before the first write and before any ``Store`` is constructed
(construction initialises the store: ``.gitignore``, an index), so a refusal and a dry run leave
the tree exactly as it was. Maintenance stays the one resumable engine: an interruption after
step 1 leaves a store that ``sidegraph-migrate`` (when ``HEAD``'s marker is still below 2) or
``sidegraph-compact --settled-days 0`` completes.

# see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§6.1, §28)
"""

from __future__ import annotations

import contextlib
import shutil
import sqlite3
import stat
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import BinaryIO

from . import maintenance, maintenance_clock
from .maintenance import MaintenanceReport
from .schema import validate_record_body
from .store import (
    _SCHEMA_SQL,
    Store,
    canonical_digest,
    index_freshness,
    unknown_stamp_message,
)
from .store_format import (
    FORMAT2_MIN_SIDEGRAPH,
    SHARD_COUNTS,
    Marker,
    bucket_name,
    has_shard_files,
    inventory_from_disk,
    list_shard_files,
    marker_text,
)
from .store_layout import (
    FORMAT_MARKER_NAME,
    GITIGNORE_NAME,
    INDEX_JOURNAL_PRAGMA,
    MAX_FORMAT_MARKER_BYTES,
    SHARDS_SUBDIR,
    STAMPING_MARKER_NAME,
    StoreRequirementError,
    check_store_requirement,
    index_stamp,
    parse_store_marker,
    release_tuple,
    store_has_any_records,
    symlinked_internals,
)
from .verify import (
    FILENAME_ID_MISMATCH,
    PARSE_ERROR,
    SHARDS_WITHOUT_FORMAT2,
    SYMLINKED_STORE_ENTRY,
    UNSAFE_RECORD_ID,
    verify_snapshot,
)

# The snapshot findings that make packing ambiguous (§6.1). Every other finding, a
# ``duplicate-ulid`` included, is carried over: records move verbatim and maintenance skips an
# identity it cannot decide.
BLOCKING_FINDINGS = frozenset(
    {PARSE_ERROR, UNSAFE_RECORD_ID, FILENAME_ID_MISMATCH, SYMLINKED_STORE_ENTRY}
)

RESUME_HINT = "sidegraph-compact --settled-days 0"


class MigrationRefused(Exception):
    """An entry condition of the migration failed; ``reasons`` names each. Nothing was written."""

    def __init__(self, reasons: list[str]) -> None:
        super().__init__("; ".join(reasons))
        self.reasons = reasons


def recover_shard_count(listing_names: Iterable[str]) -> int | None:
    """The largest of 16, 64 and 256 for which every name ``00 … (N−1)`` (``<hex>.shard``) is in
    ``listing_names`` compared in lower case (a lone ``0A.shard`` is the bucket ``0a`` in the
    wrong case, a rename away), or ``None`` when no count has a complete set. Maintenance
    always keeps all N bucket files, so a store that ever ran it shows a complete set; the
    number of files is not the test (a stray file would make an incomplete set look complete).
    # see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§6.1)"""
    have = {name.lower() for name in listing_names}
    for count in sorted(SHARD_COUNTS, reverse=True):
        if all(bucket_name(b) in have for b in range(count)):
            return count
    return None


def _requirement_for_marker(existing: str | None) -> str:
    """``max(existing, 0.17.0)``, both release versions."""
    held = release_tuple(existing) if existing is not None else None
    floor = release_tuple(FORMAT2_MIN_SIDEGRAPH)
    assert floor is not None
    if existing is not None and held is not None and held > floor:
        return existing
    return FORMAT2_MIN_SIDEGRAPH


def _case_remedy(reason: str) -> str:
    if reason.startswith("shard-name-case:"):
        return (
            f"{reason} (rename it with `git mv` to the bucket's lower-case name, commit, and "
            "run the migration again)"
        )
    return reason


def _read_marker(store_dir: Path) -> tuple[bytes | None, Marker | None]:
    """The marker's bytes, read once, and what they parse to; ``(None, None)`` when there is
    none. Everything later derives from these bytes, and the publish expects exactly them."""
    path = store_dir / FORMAT_MARKER_NAME
    try:
        st = path.lstat()
    except (FileNotFoundError, NotADirectoryError):
        return None, None
    if not stat.S_ISREG(st.st_mode):
        raise MigrationRefused([f"the store does not open: {path} must be a regular file"])
    raw = path.read_bytes()
    if len(raw) > MAX_FORMAT_MARKER_BYTES:
        raise MigrationRefused([f"the store does not open: {path} is too large"])
    try:
        return raw, parse_store_marker(raw.decode("utf-8"), path)
    except (UnicodeDecodeError, StoreRequirementError) as exc:
        raise MigrationRefused([f"the store does not open: {exc}"]) from exc


def _branch_reason(wt: maintenance_clock.WorkTree | None, any_branch: bool) -> list[str]:
    """The default-branch condition; it does not apply outside a work tree or with
    ``--any-branch``."""
    if wt is None or any_branch:
        return []
    refusal = maintenance_clock.branch_refusal(wt)
    return [] if refusal is None else [refusal]


def _uncommitted_reason(wt: maintenance_clock.WorkTree | None) -> list[str]:
    """The uncommitted-changes condition; it does not apply outside a work tree."""
    if wt is None:
        return []
    changed = maintenance_clock.uncommitted_paths(wt)
    if not changed:
        return []
    shown = ", ".join(changed[:5]) + (f", and {len(changed) - 5} more" if len(changed) > 5 else "")
    return [
        f"uncommitted changes under {wt.store_rel}/ ({shown}), or tracked files marked "
        "assume-unchanged or skip-worktree: commit or stash them first"
    ]


def _put_scratch(out: BinaryIO, chunk: bytes) -> None:
    """One write into the probe's scratch copy (a seam: its failures are the scratch space's)."""
    out.write(chunk)


def _copy_to_scratch(source: Path, dest: Path) -> str | None:
    """Copy ``source`` into the scratch directory. A failure to READ the source is returned as
    its reason (an index that cannot be read is a refusal); a failure to WRITE the scratch
    copy is the machine's (a full disk) and propagates."""
    try:
        reader = source.open("rb")
    except OSError as exc:
        return str(exc)
    with reader, dest.open("wb") as out:
        while True:
            try:
                chunk = reader.read(1 << 20)
            except OSError as exc:
                return str(exc)
            if not chunk:
                return None
            _put_scratch(out, chunk)


def _index_reasons(store_dir: Path, marker: Marker | None) -> list[str]:
    """Whether the derived ``index.db`` would stop the store from opening, decided on a COPY of
    it (the database and its ``-wal``, ``-shm`` and ``-journal`` files) in a temporary
    directory: SQLite creates or rolls back files when it opens a database, so the original is
    never opened. The copy gets what ``Store`` does at open before it writes anything of its
    own: the journal mode and the schema, then the two ``meta`` reads of the freshness check
    and the verdict of :func:`sidegraph.store.index_freshness` against the digest of the store
    on disk. An index that cannot be read, is not a database, has a schema the current one
    cannot be laid over, or is current with a stamp nothing re-stamps is a refusal; it is
    derived and gitignored, so deleting it is the remedy. A failure of the scratch space
    (a full disk) is not the index's: it propagates.
    # see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§6.1)"""
    db = store_dir / "index.db"
    if not db.exists():
        return []
    remedy = "it is derived and gitignored: delete it and run the migration again"
    scratch = Path(tempfile.mkdtemp(prefix="sidegraph-index-probe-"))
    try:
        for suffix in ("", "-wal", "-shm", "-journal"):
            source = store_dir / f"index.db{suffix}"
            if source.exists():
                failure = _copy_to_scratch(source, scratch / source.name)
                if failure is not None:
                    return [f"the store does not open: {db} cannot be read ({failure}); {remedy}"]
        try:
            conn = sqlite3.connect(scratch / "index.db")
            try:
                with contextlib.suppress(sqlite3.OperationalError):  # as Store does
                    conn.execute(INDEX_JOURNAL_PRAGMA)
                conn.executescript(_SCHEMA_SQL)
                stored = conn.execute(
                    "SELECT value FROM meta WHERE key = ?", ("canonical_digest",)
                ).fetchone()
                stamped = conn.execute(
                    "SELECT value FROM meta WHERE key = 'schema_version'"
                ).fetchone()
            finally:
                conn.close()
        except sqlite3.DatabaseError as exc:
            if getattr(exc, "sqlite_errorcode", 0) & 0xFF == sqlite3.SQLITE_FULL:
                raise  # the scratch space ran out, which says nothing about the index
            return [f"the store does not open: {db} is not usable ({exc}); {remedy}"]
        expected = index_stamp(
            marker.requirement if marker else None, marker.major if marker else 1
        )
        found = stamped[0] if stamped else None
        current, _stats = canonical_digest(store_dir)
        if index_freshness(stored[0] if stored else None, current, found, expected) == "refuse":
            return [
                f"the store does not open: {db}: {unknown_stamp_message(found, expected)}; {remedy}"
            ]
        return []
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _requirement_reasons(store_dir: Path) -> list[str]:
    """``check_store_requirement`` as a reason, on every path (a resume included)."""
    try:
        check_store_requirement(store_dir, allow_unmigrated_shards=True)
    except StoreRequirementError as exc:
        return [f"the store does not open: {exc}"]
    return []


def _first_open_files(store_dir: Path) -> list[str]:
    """The tracked files ``Store`` creates on its first open here: a missing ``.gitignore``, and
    the creation marker of a store with neither it nor any record."""
    files = []
    if not (store_dir / GITIGNORE_NAME).exists():
        files.append(GITIGNORE_NAME)
    if not (store_dir / STAMPING_MARKER_NAME).exists() and not store_has_any_records(store_dir):
        files.append(STAMPING_MARKER_NAME)
    return files


def _note_written(report: MaintenanceReport, files: list[str], *, marker: bool) -> None:
    """Put the files the migration itself writes (the marker, a first open's) in front of the
    report's list. A run that stopped is not given the marker it never published."""
    front = [FORMAT_MARKER_NAME] if marker else []
    front += [f for f in files if f not in report.wrote]
    report.wrote[:0] = [f for f in front if f not in report.wrote]


def _snapshot_findings(store_dir: Path) -> tuple[list[str], list[str]]:
    """``(blocking reasons, carried findings)`` of the snapshot lint: symlinked store entries
    and the findings that make packing ambiguous stop the migration; every other finding is
    carried over and listed after the run. ``shards-without-format2`` and findings about the
    marker itself are what the migration settles, not carried."""
    linked = symlinked_internals(store_dir)
    if linked:
        return ["symlinked store entries: " + ", ".join(linked)], []
    try:
        findings = verify_snapshot(store_dir)
    except OSError as exc:
        return [f"the store cannot be read: {exc}"], []
    blocking = [f"{v.code}: {v.path}: {v.detail}" for v in findings if v.code in BLOCKING_FINDINGS]
    marker = str(store_dir / FORMAT_MARKER_NAME)
    carried = [
        f"{v.code}: {v.path}: {v.detail}"
        for v in findings
        if v.code not in BLOCKING_FINDINGS
        and v.code != SHARDS_WITHOUT_FORMAT2
        and v.path != marker  # what the migration is about to settle
    ]
    return blocking, carried


def _shard_count(store_dir: Path, shards: int) -> tuple[int | None, list[str]]:
    """``N`` for the marker and the reasons it cannot be had. A store without shard files takes
    ``shards``; one with them recovers N from the names and refuses without a complete set."""
    if not has_shard_files(store_dir):
        return shards, []
    found = list_shard_files(store_dir / SHARDS_SUBDIR, None)
    n = recover_shard_count(p.name for p in [*found.buckets.values(), *found.strays])
    if n is None:
        return None, [
            f"{store_dir / SHARDS_SUBDIR} holds shard files but no complete set 00 … (N-1) for "
            f"N of {', '.join(str(c) for c in SHARD_COUNTS)}: restore the missing bucket files "
            "from git, then run the migration again"
        ]
    return n, []


def _entry_reasons(store_dir: Path, wt: maintenance_clock.WorkTree | None, n: int) -> list[str]:
    """Maintenance's own entry conditions over the store as if its marker said major 2 with
    ``shards n`` (every other shard file is a stray): no identity without an effective record,
    no bad shard line, no case collision, judged before the marker is touched so the migration
    never leaves a store maintenance refuses."""
    inv = inventory_from_disk(
        store_dir, validate=validate_record_body, assume_major=2, assume_shards=n
    )
    try:
        maintenance.check_entry(
            inv, maintenance_clock.git_ls_files_shards(wt) if wt is not None else None, n
        )
    except maintenance.EntryRefusal as refusal:
        return [_case_remedy(r) for r in refusal.reasons]
    return []


def _open(store_dir: Path, *, unmigrated: bool) -> Store:
    """The store handle. A requirement or validation error is a refusal; a busy store, an I/O
    error and anything else propagate (the command maps them to exit 1)."""
    try:
        if unmigrated:
            return Store(store_dir, _allow_unmigrated_shards=True)
        return Store(store_dir)
    except (StoreRequirementError, ValueError) as exc:
        raise MigrationRefused([f"the store does not open: {exc}"]) from exc


def _publish_marker(store: Store, text: str, expect: bytes | None) -> None:
    """Step 1: the format marker, durably. ``expect`` is the marker as the entry checks read it
    (``None``: absent): a marker another process changed since stops the run unwritten."""
    marker = store.path / FORMAT_MARKER_NAME
    try:
        maintenance.publish_bytes(marker, text.encode("utf-8"), expect=expect)
    except maintenance.StoreChanged as exc:
        raise MigrationRefused([f"the format marker changed under the run ({exc})"]) from exc
    maintenance.fsync_dir(store.path)


def migrate(
    store_dir: Path, *, shards: int = 64, dry_run: bool, any_branch: bool
) -> MaintenanceReport:
    """Switch the store at ``store_dir`` to format 2 and run maintenance with no age limit.

    ``shards`` is the shard count of a store with no shard files yet; a store that already holds
    them keeps the count its files show. Raises :class:`MigrationRefused` (nothing written) when
    an entry condition fails and ``ValueError`` for a ``shards`` that is not 16, 64 or 256;
    ``ClockError`` (git broken) and a busy store propagate. ``dry_run`` plans without a ``Store``
    handle and writes nothing, the marker and the preparation step included. On a format-2 store
    it resumes only a migration whose marker is not yet committed (``HEAD``'s is below 2 or
    absent).

    # see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§6.1)"""
    if shards not in SHARD_COUNTS:
        raise ValueError(f"shards must be one of {SHARD_COUNTS}, got {shards}")
    store_dir = Path(store_dir)
    wt = maintenance_clock.resolve_work_tree(store_dir)
    entry_marker, marker = _read_marker(store_dir)
    requirement = _requirement_reasons(store_dir)  # a resume is held to it as well
    if marker is not None and marker.major == 2:
        if requirement:
            raise MigrationRefused(requirement)
        return _resume(store_dir, marker, wt, dry_run=dry_run, any_branch=any_branch)

    reasons = _branch_reason(wt, any_branch) + _uncommitted_reason(wt) + requirement
    if reasons:
        raise MigrationRefused(reasons)
    reasons = _index_reasons(store_dir, marker)
    blocking, carried = _snapshot_findings(store_dir)
    reasons += blocking
    n, more = _shard_count(store_dir, shards)
    reasons += more
    if reasons:
        raise MigrationRefused(reasons)
    assert n is not None
    reasons = _entry_reasons(store_dir, wt, n)
    if reasons:
        raise MigrationRefused(reasons)

    if dry_run:
        report = maintenance.preview(
            store_dir, settled_days=0, n=n, validate=validate_record_body, assume_major=2
        )
        _note_written(report, _first_open_files(store_dir), marker=not report.stopped)
        report.carried = carried
        return report

    text = marker_text(2, _requirement_for_marker(marker.requirement if marker else None), n)
    maintenance.stage("migrate:before_marker")
    created = _first_open_files(store_dir)
    store = _open(store_dir, unmigrated=True)
    try:
        with store._mutation():
            _publish_marker(store, text, entry_marker)
            maintenance.checkpoint("migrate:format")
        store.refresh_if_stale()  # now read as major 2
        report = store.maintain(settled_days=0, dry_run=False, any_branch=any_branch)
        _note_written(report, [f for f in created if (store_dir / f).exists()], marker=True)
        report.carried = carried
        return report
    finally:
        store.close()


def _resume(
    store_dir: Path,
    marker: Marker,
    wt: maintenance_clock.WorkTree | None,
    *,
    dry_run: bool,
    any_branch: bool,
) -> MaintenanceReport:
    """A format-2 store: finish an uncommitted migration, else refuse. Entry conditions apply
    to a format-1 store only; the branch condition is maintenance's and still holds."""
    if wt is None:
        raise MigrationRefused(
            [
                "this store is already format 2 and there is no git history to say whether a "
                f"migration is unfinished; `{RESUME_HINT}` finishes one"
            ]
        )
    head = maintenance_clock.head_blob(wt, FORMAT_MARKER_NAME)
    if head is not None:
        try:
            committed = parse_store_marker(
                head.decode("utf-8", "replace"), Path(FORMAT_MARKER_NAME)
            )
        except StoreRequirementError as exc:
            raise MigrationRefused([f"the marker at HEAD cannot be read: {exc}"]) from exc
        if committed.major >= 2:
            raise MigrationRefused(
                [
                    "this store is already format 2 at HEAD, so there is no migration to "
                    f"resume; `{RESUME_HINT}` finishes an interrupted run and plain "
                    "`sidegraph-compact` folds settled files"
                ]
            )
    reasons = _branch_reason(wt, any_branch) + _index_reasons(store_dir, marker)
    if reasons:  # (uncommitted changes are expected on a resume)
        raise MigrationRefused(reasons)
    planned = _first_open_files(store_dir)
    if dry_run:
        assert marker.shards is not None
        report = maintenance.preview(
            store_dir,
            settled_days=0,
            n=marker.shards,
            validate=validate_record_body,
            assume_major=None,
        )
        if not report.stopped:
            _note_written(report, planned, marker=False)
        return report
    store = _open(store_dir, unmigrated=False)
    try:
        report = store.maintain(settled_days=0, dry_run=False, any_branch=any_branch)
        _note_written(report, [f for f in planned if (store_dir / f).exists()], marker=False)
        return report
    finally:
        store.close()
