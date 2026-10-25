"""Maintenance of a format-2 store: which per-record files and archive segments fold into shard
lines, which identities are skipped, what the entry conditions refuse, and the publisher that
writes the shards and removes what they now hold.

The planner is pure: it reads an :class:`~sidegraph.store_format.Inventory` and an
:class:`Eligibility` and returns a :class:`Plan`; it writes nothing and runs no git. The
publisher (:func:`publish_plan`) is the only code that writes shard files or deletes a
per-record file or an archive segment. Maintenance moves bodies verbatim and transforms nothing.

# see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§6.2)
"""

from __future__ import annotations

import contextlib
import errno
import json
import os
import re
import stat
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import maintenance_clock
from .maintenance_clock import WorkTree
from .store_format import (
    GITATTRIBUTES_LINE,
    Inventory,
    Occurrence,
    UnencodableBody,
    Validator,
    archive_body,
    bucket,
    bucket_name,
    classify_shard_files,
    decode_line,
    encode_body,
    encode_line,
    inventory_from_disk,
    render_shard,
    split_lines,
)
from .store_layout import (
    ARCHIVE_SUBDIR,
    FORMAT_MARKER_NAME,
    GITATTRIBUTES_NAME,
    SHARDS_SUBDIR,
    symlinked_internals,
)
from .store_rules import (
    Effective,
    _same_body,
    archive_candidate,
    classify_transition,
    compatible,
    deletable,
    effective_all,
    is_derived_entity_body,
    keeps_keys,
    kind_of,
    superseding,
)

try:  # not on Windows
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

SHADOW_DIVERGENCE = "shadow-divergence"
DUPLICATE_ULID = "duplicate-ulid"
BINDING_ENTRY_DROPPED = "binding-entry-dropped"

# Terminal statuses of the two record types an archive segment holds. Written out here so the
# planner stays pydantic-free; a test pins them to ``verify._ARCHIVE_TERMINAL_STATUSES``.
_TERMINAL_STATUSES = {
    "decision": frozenset({"superseded", "rejected", "deprecated"}),
    "domain": frozenset({"superseded", "dropped"}),
}

_DAY_SECONDS = 86400

Identity = tuple[str, str]


@dataclass(frozen=True)
class Eligibility:
    """What decides whether a per-record file may fold: the settled clock and the dirty set.

    ``landing`` maps a root-relative path to its landing time (``None`` only without a git work
    tree, which requires ``settled_days == 0``); ``dirty`` holds root-relative paths that are
    untracked or differ from the recorded ``HEAD``; ``store_rel`` is the store's path relative
    to the work-tree root. :meth:`eligible` is the one converter between a store-relative
    occurrence path and these root-relative paths.

    # see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§6.2)
    """

    settled_days: int
    now: int
    landing: dict[str, int] | None
    dirty: frozenset[str]
    store_rel: str

    def __post_init__(self) -> None:
        if self.landing is None and self.settled_days != 0:
            raise ValueError("outside a git work tree maintenance requires settled_days == 0")

    def root_path(self, path: str) -> str:
        """The root-relative form of a store-relative path."""
        return f"{self.store_rel}/{path}" if self.store_rel else path

    def reason(self, path: str) -> str | None:
        """Why the file at store-relative ``path`` may not fold; ``None`` when it may."""
        rooted = self.root_path(path)
        if rooted in self.dirty:
            return "untracked or modified"
        if self.landing is None:
            return None
        landed = self.landing.get(rooted)
        if landed is None:
            return "no commit in the log"
        if self.now - landed < self.settled_days * _DAY_SECONDS:
            return "not settled"
        return None

    def eligible(self, path: str) -> bool:
        return self.reason(path) is None


@dataclass(frozen=True)
class Skip:
    """An identity maintenance leaves alone, with the finding that excluded it."""

    record_type: str
    record_id: str
    code: str


class EntryRefusal(Exception):
    """An entry condition of a format-2 maintenance run failed; ``reasons`` names each."""

    def __init__(self, reasons: list[str]) -> None:
        super().__init__("; ".join(reasons))
        self.reasons = reasons


def check_entry(inv: Inventory, git_paths: list[str] | None, n: int | None = None) -> None:
    """Refuse (raise :class:`EntryRefusal`) unless the store can be maintained: no identity
    without an effective record, no bad or conflicted shard line (one whose identity cannot be
    recovered included), no case collision in ``shards/`` over the directory listing and over
    ``git_paths`` (the names git tracks under ``<store>/shards``; ``None`` skips that check),
    no bucket file numbered ``>= n`` (``n`` defaults to the marker's shard count).

    # see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§6.2)"""
    reasons: list[str] = []
    for (record_type, record_id), eff in sorted(effective_all(inv).items()):
        if eff.reason is not None:
            reasons.append(f"gap: {record_type} {record_id} has no effective record ({eff.reason})")
    for occ in inv.occurrences:
        if occ.kind != "shard":
            continue
        where = f"{occ.path}:{occ.line_no}"
        if occ.in_conflict:
            reasons.append(f"shard-conflict: {where}")
        elif occ.error is not None:
            reasons.append(f"bad-shard-line: {where}: {occ.error}")
    if n is None and inv.marker is not None:
        n = inv.marker.shards
    if n is not None and inv.listing is not None:
        reasons.extend(
            f"bucket-out-of-range: {path.name} is not a bucket of {n} shards"
            for b, path in sorted(inv.listing.buckets.items())
            if b >= n
        )
    collisions = list(inv.listing.case_collisions) if inv.listing is not None else []
    if git_paths is not None:
        tracked = classify_shard_files([Path(p) for p in git_paths], n)
        collisions.extend(tracked.case_collisions)
    for name in sorted(set(collisions)):
        reasons.append(f"shard-name-case: {name}")
    if reasons:
        raise EntryRefusal(reasons)


@dataclass(frozen=True)
class LineMove:
    """A misplaced line (wrong bucket, or in a stray file) of a non-skipped identity: copied
    verbatim into ``bucket``, then removed from ``source``, the shard file name holding it."""

    record_type: str
    record_id: str
    source: str
    bucket: int
    raw_line: bytes


@dataclass(frozen=True)
class LineWrite:
    """A line written into ``bucket``: a replacement of the identity's line, or an insert for a
    new identity. ``origin`` is the file or segment path (store-relative) whose body it carries."""

    record_type: str
    record_id: str
    bucket: int
    raw_line: bytes
    origin: str


@dataclass
class Plan:
    """The outcome of planning a run. ``target`` is each bucket's final content as
    ``(type, id, line)`` triples: the planned line of every non-skipped identity, and every other
    line (a skipped identity's, an unparsed one) byte for byte in the bucket file it is in; a
    stray file's lines are in no bucket. ``kept`` lists ``(path, reason)`` for every per-record
    file, and every archive segment, that stays."""

    n: int
    target: dict[int, list[tuple[str, str, bytes]]] = field(default_factory=dict)
    moves: list[LineMove] = field(default_factory=list)
    replacements: list[LineWrite] = field(default_factory=list)
    inserts: list[LineWrite] = field(default_factory=list)
    file_deletions: list[str] = field(default_factory=list)
    segment_deletions: list[str] = field(default_factory=list)
    skipped: list[Skip] = field(default_factory=list)
    kept: list[tuple[str, str]] = field(default_factory=list)


# -- skips -----------------------------------------------------------------------------------


def _is_derived_fn(eff: dict[Identity, Effective]) -> Callable[[str], bool]:
    """Whether an ``entity_id`` resolves to a derived entity; an unresolved one is not derived."""

    def is_derived(entity_id: str) -> bool:
        found = eff.get(("entity", entity_id))
        return found is not None and is_derived_entity_body(found.raw)

    return is_derived


def _entity_ids(body: object) -> list[str]:
    if not isinstance(body, list):
        return []
    return [
        e["entity_id"] for e in body if isinstance(e, dict) and isinstance(e.get("entity_id"), str)
    ]


def _skip_codes(
    record_type: str, occs: list[Occurrence], is_derived: Callable[[str], bool]
) -> list[str]:
    """The findings that exclude one identity (§6.2 "Skipped identities"), in a fixed order."""
    kind = kind_of(record_type)
    files = [o for o in occs if o.kind == "file" and o.readable]
    shards = [o for o in occs if o.kind == "shard" and o.readable]
    archives = [o for o in occs if o.kind == "archive" and o.archive_type_known and o.readable]
    codes: list[str] = []
    shard = shards[0] if shards else None
    if record_type != "bindings" and shard is not None and isinstance(shard.raw, dict):
        for file_occ in files:
            if isinstance(file_occ.raw, dict) and classify_transition(
                kind, shard.raw, file_occ.raw
            ):
                codes.append(SHADOW_DIVERGENCE)
                break
    duplicate = any(
        not _same_body(archives[0], other) for other in archives[1:]
    )  # two differing archive lines
    for line in archives:
        if not isinstance(line.raw, dict):
            continue
        for file_occ in files:
            if isinstance(file_occ.raw, dict) and not compatible(kind, line.raw, file_occ.raw):
                duplicate = True
        for shard_occ in shards:
            if isinstance(shard_occ.raw, dict) and not (
                compatible(kind, line.raw, shard_occ.raw)
                or superseding(kind, line.raw, shard_occ.raw)
            ):
                duplicate = True
    if duplicate:
        codes.append(DUPLICATE_ULID)
    if record_type == "bindings" and shard is not None:
        for file_occ in files:
            have = set(_entity_ids(file_occ.raw))
            lost = [
                e for e in _entity_ids(shard.raw) if e not in have and not is_derived(e)
            ]  # an unresolved entity counts as kept
            if lost:
                codes.append(BINDING_ENTRY_DROPPED)
                break
    return codes


# -- the plan --------------------------------------------------------------------------------


def _encode(record_type: str, record_id: str, body: object, path: str, plan: Plan) -> bytes | None:
    """The shard line for a body, or ``None`` after reporting ``path`` as unencodable."""
    try:
        return encode_line(record_type, record_id, body)
    except UnencodableBody:
        plan.kept.append((path, "unencodable"))
        return None


def _own_bucket(record_id: str, n: int) -> int | None:
    try:
        return bucket(record_id, n)
    except UnencodableBody:
        return None


@dataclass
class _Final:
    """The planned shard line of one non-skipped identity and the body it holds."""

    line: bytes
    body: object


def plan_maintenance(inv: Inventory, elig: Eligibility, n: int) -> Plan:
    """Plan a maintenance run over ``inv`` (§6.2 step 2 and the deletions of step 4).

    Skipped identities are untouched: every copy stays byte for byte where it is. For the
    others, one line per identity ends up in its bucket: an eligible per-record file's body when
    ``keeps_keys`` holds from the shard body, a ``superseding`` archive body under the same key
    rule, an archive candidate when there is no file and no shard line, else the existing line;
    misplaced lines move verbatim. A file is deleted when its identity's final body equals it,
    a segment when every line of it is ``deletable`` against its identity's final body.
    Unencodable bodies keep their source and the run goes on.

    # see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§6.2)"""
    if inv.marker is not None and inv.marker.shards not in (None, n):
        raise ValueError(f"shard count {n} differs from the marker's {inv.marker.shards}")
    if inv.listing is not None and any(b >= n for b in inv.listing.buckets):
        raise ValueError(f"a bucket file is numbered {n} or more: shard count {n} is wrong")
    plan = Plan(n=n)
    eff = effective_all(inv)
    by_id = inv.by_identity()
    is_derived = _is_derived_fn(eff)
    skip_codes: dict[Identity, list[str]] = {}
    for identity, occs in by_id.items():
        codes = _skip_codes(identity[0], occs, is_derived)
        if codes:
            skip_codes[identity] = codes
            plan.skipped.extend(Skip(identity[0], identity[1], c) for c in codes)
    plan.skipped.sort(key=lambda s: (s.record_type, s.record_id, s.code))

    bucket_of_file = (
        {p.name: b for b, p in inv.listing.buckets.items()} if inv.listing is not None else {}
    )
    finals: dict[Identity, _Final] = {}
    touched: set[Identity] = set()
    for identity in sorted(by_id):
        record_type, record_id = identity
        found = eff[identity]
        own = _own_bucket(record_id, n)
        if identity in skip_codes:
            plan.kept.extend(  # never moved, rewritten or deleted: its files are reported
                (o.path, f"skipped: {skip_codes[identity][0]}")
                for o in by_id[identity]
                if o.kind == "file"
            )
            continue
        if found.raw is None or found.reason is not None or own is None:
            continue  # no effective record: untouched, and entry refuses the run
        touched.add(identity)
        final = _plan_identity(
            identity, by_id[identity], own, elig, is_derived, plan, bucket_of_file
        )
        if final is not None:
            finals[identity] = final

    # target buckets: untouched shard lines stay byte for byte in their bucket file
    lines: dict[int, list[tuple[str, str, bytes]]] = {b: [] for b in range(n)}
    for occ in inv.occurrences:
        if occ.kind != "shard":
            continue
        b = bucket_of_file.get(Path(occ.path).name)
        if b is None or b >= n or (occ.record_type, occ.record_id) in touched:
            continue
        lines[b].append((occ.record_type or "", occ.record_id or "", occ.raw_bytes))
    for (record_type, record_id), final in finals.items():
        own = _own_bucket(record_id, n)
        assert own is not None
        lines[own].append((record_type, record_id, final.line))
    plan.target = {b: sorted(rows, key=lambda t: (t[0], t[1])) for b, rows in lines.items()}

    _plan_segments(inv, by_id, skip_codes, finals, plan)
    plan.moves.sort(key=lambda m: (m.record_type, m.record_id, m.source))
    plan.file_deletions.sort()
    plan.segment_deletions.sort()
    first_reason: dict[str, str] = {}
    for path, reason in plan.kept:  # one row per path: the first, most specific reason
        first_reason.setdefault(path, reason)
    plan.kept = sorted(first_reason.items())
    return plan


def _plan_identity(
    identity: Identity,
    occs: list[Occurrence],
    own: int,
    elig: Eligibility,
    is_derived: Callable[[str], bool],
    plan: Plan,
    bucket_of_file: dict[str, int],
) -> _Final | None:
    """Plan one non-skipped identity with an effective record: its final line (``None`` when it
    has no shard line and gets none), its moves, replacement or insert, and its file's fate."""
    record_type, record_id = identity
    kind = kind_of(record_type)
    is_bindings = record_type == "bindings"
    own_name = bucket_name(own)
    shards = sorted(
        (o for o in occs if o.kind == "shard" and o.readable),
        key=lambda o: (o.path, o.line_no or 0),
    )
    files = [o for o in occs if o.kind == "file"]
    shard = shards[0] if shards else None
    for occ in shards:
        if Path(occ.path).name != own_name:
            plan.moves.append(
                LineMove(record_type, record_id, Path(occ.path).name, own, occ.raw_bytes)
            )
    # the line that stays: the first copy in the bucket, else the first misplaced one (moved)
    in_bucket = [o for o in shards if Path(o.path).name == own_name]
    kept_copies = in_bucket or shards
    final = _Final(kept_copies[0].raw_bytes, kept_copies[0].raw) if kept_copies else None

    def write(body: object, origin: str) -> _Final | None:
        line = _encode(record_type, record_id, body, origin, plan)
        if line is None:
            return None
        item = LineWrite(record_type, record_id, own, line, origin)
        (plan.replacements if shard is not None else plan.inserts).append(item)
        return _Final(line, body)

    if files:
        file_occ = files[0]
        reason = elig.reason(file_occ.path)
        if reason is not None:
            plan.kept.append((file_occ.path, reason))
            return final
        if shard is not None and not keeps_keys(
            shard.raw, file_occ.raw, bindings=is_bindings, is_derived=is_derived
        ):
            plan.kept.append((file_occ.path, "keys would be lost"))
            return final
        if shard is not None and _same_body(shard, file_occ):
            plan.file_deletions.append(file_occ.path)  # the normal pre-deletion state
            return final
        written = write(file_occ.raw, file_occ.path)
        if written is None:
            return final
        plan.file_deletions.append(file_occ.path)
        return written
    candidate = archive_candidate(occs)
    if candidate is None or not isinstance(candidate.raw, dict):
        return final
    # a readable nonterminal candidate is packed like any other: terminal status governs only
    # whether its segment may be deleted (_segment_problem)
    if shard is None:
        return write(candidate.raw, candidate.path)
    if (
        isinstance(shard.raw, dict)
        and superseding(kind, candidate.raw, shard.raw)
        and keeps_keys(shard.raw, candidate.raw)
    ):
        return write(candidate.raw, candidate.path) or final
    return final


def _terminal(record_type: str, body: dict) -> bool:
    return body.get("status") in _TERMINAL_STATUSES.get(record_type, frozenset())


def _plan_segments(
    inv: Inventory,
    by_id: dict[Identity, list[Occurrence]],
    skip_codes: dict[Identity, list[str]],
    finals: dict[Identity, _Final],
    plan: Plan,
) -> None:
    """A segment is deleted only when every line is readable, of a known type, terminal, of an
    identity not skipped, and ``deletable`` against that identity's final shard body."""
    segments: dict[str, list[Occurrence]] = {
        path: [] for path in sorted(inv.images) if path.startswith("archive/")
    }
    for occ in inv.occurrences:
        if occ.kind == "archive":
            segments.setdefault(occ.path, []).append(occ)
    for path, segment_lines in segments.items():
        problem = _segment_problem(segment_lines, skip_codes, finals)
        if problem is None:
            plan.segment_deletions.append(path)
        else:
            plan.kept.append((path, problem))


def _segment_problem(
    segment_lines: list[Occurrence],
    skip_codes: dict[Identity, list[str]],
    finals: dict[Identity, _Final],
) -> str | None:
    if not segment_lines:
        return "no records"
    for occ in segment_lines:
        if not occ.parseable or (occ.archive_type_known and not occ.readable):
            return "unreadable line"
        if not occ.archive_type_known:
            return "unknown record type"
        if not isinstance(occ.raw, dict):
            return "unreadable line"
        assert occ.record_type is not None and occ.record_id is not None
        identity = (occ.record_type, occ.record_id)
        if not _terminal(occ.record_type, occ.raw):
            return "nonterminal line"
        if identity in skip_codes:
            return "skipped identity"
        final = finals.get(identity)
        if final is None or not isinstance(final.body, dict):
            return "no shard line"
        if not deletable(kind_of(occ.record_type), occ.raw, final.body):
            return "not deletable"
    return None


# -- self-healing states ---------------------------------------------------------------------


def self_healing_counts(inv: Inventory, n: int) -> dict[str, int]:
    """The states maintenance heals and verify does not report (doctor's soft notice): lines in
    the wrong bucket of a bucket file (``misplaced``), redundant shard lines of one identity with
    equal bodies (``equal_duplicate``), and stray shard files (``stray``). A skipped identity's
    lines, and a stray file holding one, are not counted: maintenance leaves them where they are.

    # see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§6.3)"""
    bucket_files = (
        {p.name: b for b, p in inv.listing.buckets.items()} if inv.listing is not None else {}
    )
    eff = effective_all(inv)
    is_derived = _is_derived_fn(eff)
    skipped = {
        identity
        for identity, occs in inv.by_identity().items()
        if _skip_codes(identity[0], occs, is_derived)
    }  # maintenance never moves, collapses or unlinks these, so they are not "healed"
    held_by_skipped = set()
    misplaced = 0
    groups: dict[Identity, list[Occurrence]] = {}
    for occ in inv.occurrences:
        if occ.kind != "shard" or not occ.readable:
            continue
        assert occ.record_type is not None and occ.record_id is not None
        if (occ.record_type, occ.record_id) in skipped:
            held_by_skipped.add(Path(occ.path).name)
            continue
        groups.setdefault((occ.record_type, occ.record_id), []).append(occ)
        b = bucket_files.get(Path(occ.path).name)
        if b is not None and _own_bucket(occ.record_id, n) != b:
            misplaced += 1
    extra = 0
    for occs in groups.values():
        distinct: list[Occurrence] = []
        for occ in occs:
            if any(_same_body(occ, seen) for seen in distinct):
                extra += 1
            else:
                distinct.append(occ)
    strays = inv.listing.strays if inv.listing is not None else []
    stray = sum(1 for p in strays if p.name not in held_by_skipped)
    return {"misplaced": misplaced, "equal_duplicate": extra, "stray": stray}


# -- the publisher ---------------------------------------------------------------------------
#
# Writes shard files and deletes what they now hold. A crash at any instruction, and a writer or
# a checkout that runs beside it, must never lose a record: every shard publish compares the
# target with the image this run expects to replace, every removal of a copy is preceded by the
# durable presence of an equal one, and every deletion re-reads what it relies on.
# see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§6.2)

_BUCKET_FILE = re.compile(r"[0-9a-f]{2}\.shard")
_RECORD_DIR_TYPES = {
    "decisions": "decision",
    "facts": "fact",
    "entities": "entity",
    "domains": "domain",
    "bindings": "bindings",
}
_ARCHIVE_ID_FIELD = {"decision": "id", "domain": "domain_id"}

Line = tuple[str, str, bytes]
ALREADY_GONE = "already gone"


def _refuse_links(store_dir: Path) -> None:
    """Raise :class:`StoreChanged` if any store entry is now a symlink (a link made after the
    earlier check must not be written or deleted through). The window between this check and an
    individual replace or unlink stays open (fd-anchored directory handles are out of scope;
    the same accepted timing class as R12)."""
    linked = symlinked_internals(store_dir)
    if linked:
        raise StoreChanged("symlinked store entries: " + ", ".join(linked))


class StoreChanged(Exception):
    """A file this run was about to replace or remove is not what the run last wrote or read:
    something else (a pull, a checkout, a writer) changed it. The run stops; the next run is
    idempotent. see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§6.2)"""


def checkpoint(label: str) -> None:
    """A no-op seam after each durable step of a run (``prep:<name>``, ``A:<name>``,
    ``B:<name>``, ``C:<name>``, ``unlink:<name>``, ``delete:<path>``). The crash tests replace
    it with a function that records the label or raises to simulate a kill."""


def stage(label: str) -> None:
    """A no-op seam between the stages of a run, where the interleaving tests run a writer,
    a checkout or a ``HEAD`` move (``between_pass_and_mutation``, ``after_step1``,
    ``after_step2``, ``after_head_reread``, ``before_unlink:<name>``, ``before_step4``)."""


# The errors that mean "this operation does not exist here", the only ones fsync helpers swallow.
_FULLSYNC_UNSUPPORTED = frozenset({errno.ENOTSUP, errno.EINVAL, errno.ENOTTY, errno.EOPNOTSUPP})
_DIR_OPEN_UNSUPPORTED = frozenset({errno.EACCES, errno.EPERM, errno.EISDIR})
_DIR_FSYNC_UNSUPPORTED = frozenset({errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP})


def _is_windows() -> bool:
    """Whether directories cannot be opened here; a seam so tests need not patch ``os.name``."""
    return os.name == "nt"


def fsync_fd(fd: int) -> None:
    """Flush ``fd`` to the device: ``fcntl.F_FULLFSYNC`` on macOS where it is available (plain
    ``os.fsync`` does not flush the drive cache there; a directory descriptor accepts it), else
    ``os.fsync``. see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§6.2)"""
    if sys.platform == "darwin" and fcntl is not None:
        full = getattr(fcntl, "F_FULLFSYNC", None)
        if full is not None:
            try:
                fcntl.fcntl(fd, full)
                return
            except OSError as exc:
                if exc.errno not in _FULLSYNC_UNSUPPORTED:
                    raise  # EIO, ENOSPC, EDQUOT ...: the device failed, the run must stop
                # a filesystem that does not know the full flush still takes a plain fsync
    os.fsync(fd)


def fsync_dir(path: Path) -> None:
    """Make the entries of directory ``path`` durable; a no-op only on Windows (which cannot open
    a directory) or where the filesystem does not fsync one. Any other error (EIO,
    ENOSPC, EDQUOT ...) propagates so a device failure stops the run before its deletions.
    see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§6.2)"""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError as exc:
        if _is_windows() and exc.errno in _DIR_OPEN_UNSUPPORTED:
            return  # only Windows cannot open a directory; on POSIX EACCES is a real failure
        raise
    try:
        fsync_fd(fd)
    except OSError as exc:
        if exc.errno not in _DIR_FSYNC_UNSUPPORTED:
            raise
    finally:
        os.close(fd)


def _read_if_regular(path: Path) -> bytes | None:
    """The bytes of a regular file, ``None`` when absent. A symlink or any other non-regular
    entry raises :class:`StoreChanged`: it is not a file this run may replace or read through."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(st.st_mode):
        raise StoreChanged(f"{path.name} is not a regular file")
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def publish_bytes(path: Path, data: bytes, *, expect: bytes | None) -> None:
    """Replace ``path`` with ``data``: write a unique tmp beside it, ``fsync_fd`` it, re-read
    the target and compare it with ``expect`` (``None``: the target must be absent), raise
    :class:`StoreChanged` when they differ, else ``os.replace``. The tmp is removed on failure.
    see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§6.2)"""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex[:12]}.tmp")
    # O_BINARY: on Windows os.open is text mode and os.write would turn "\n" into "\r\n"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o666)
    try:
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view) :]
            fsync_fd(fd)
        finally:
            os.close(fd)
        if _read_if_regular(path) != expect:
            raise StoreChanged(f"{path.name} changed under the run")
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp)
        raise


def may_unlink(name: str, n: int) -> bool:
    """Whether maintenance may remove the shard file called ``name``: never one whose
    lower-cased name is a bucket of ``n`` shards (on a case-insensitive filesystem ``0A.shard``
    is the bucket ``0a.shard`` itself).
    # see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§6.2)"""
    lower = name.lower()
    return not (_BUCKET_FILE.fullmatch(lower) is not None and int(lower[:2], 16) < n)


@dataclass
class MaintenanceReport:
    """What a run did, or (dry run) would do. ``folded`` lists the per-record files and archive
    segments whose bodies the shards hold after the run, ``moved`` the lines moved between
    shard files, ``deleted_files`` and ``deleted_segments`` what was removed, ``skipped`` the
    identities left alone, ``kept`` ``(path, reason)`` for every file or segment that stays.
    ``stopped`` is set when the run refused or stopped early, with the reason. In a real run
    ``folded`` and ``moved`` list only what was actually published; what the plan intended
    and the run did not reach is in ``planned_folded`` and ``planned_moved``. In a dry run
    they are the intentions.

    # see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§6.2)"""

    folded: list[str] = field(default_factory=list)
    moved: list[str] = field(default_factory=list)
    deleted_files: list[str] = field(default_factory=list)
    deleted_segments: list[str] = field(default_factory=list)
    skipped: list[Skip] = field(default_factory=list)
    kept: list[tuple[str, str]] = field(default_factory=list)
    shallow: bool = False
    stopped: str | None = None
    dry_run: bool = False
    planned_folded: list[str] = field(default_factory=list)
    planned_moved: list[str] = field(default_factory=list)
    # every tracked path the run published or unlinked (a dry run: would), store-relative
    wrote: list[str] = field(default_factory=list)
    empty_created: list[str] = field(default_factory=list)  # ``wrote`` entries still 0 bytes
    # findings of the store as the command found it that do not stop it, printed after the report
    carried: list[str] = field(default_factory=list)


def _planned_folds(plan: Plan) -> list[str]:
    folded = {w.origin for w in [*plan.replacements, *plan.inserts]} | set(plan.file_deletions)
    return sorted(folded)


def _move_text(m: LineMove) -> str:
    return f"{m.record_type} {m.record_id}: {m.source} -> {bucket_name(m.bucket)}"


def report_from_plan(plan: Plan, *, shallow: bool, dry_run: bool) -> MaintenanceReport:
    """The report of a plan before anything ran. A dry run reports the plan as what it would
    fold and move; a real run starts with empty ``folded``/``moved`` (filled as the publisher
    performs them) and the plan in ``planned_*``."""
    folded, moved = _planned_folds(plan), [_move_text(m) for m in plan.moves]
    return MaintenanceReport(
        deleted_files=list(plan.file_deletions) if dry_run else [],
        deleted_segments=list(plan.segment_deletions) if dry_run else [],
        folded=folded if dry_run else [],
        moved=moved if dry_run else [],
        skipped=list(plan.skipped),
        kept=list(plan.kept),
        shallow=shallow,
        dry_run=dry_run,
        planned_folded=[] if dry_run else folded,
        planned_moved=[] if dry_run else moved,
    )


# a few new empty buckets are named; a first fold's many are one line
_COLLAPSE_EMPTY_ABOVE = 4


_CARRIED_SHOWN = 20


def render_report(r: MaintenanceReport) -> str:
    """The text a run prints: what changed, what was skipped and why, the commit instruction.
    see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§6.2)"""
    out: list[str] = []
    if r.dry_run:
        out.append("Dry run: nothing was written.")
    if r.shallow:
        out.append(
            "Note: this clone is shallow; a file last changed before the boundary takes the "
            "boundary commit's time, so little may fold."
        )
    if r.stopped:
        out.append(f"Stopped: {r.stopped}")
    sections = [
        (
            r.folded,
            "per-record files and segments folded into shards",
            "per-record files and segments would be folded into shards",
        ),
        (r.moved, "shard lines moved", "shard lines would be moved"),
        (r.deleted_files, "per-record files deleted", "per-record files would be deleted"),
        (r.deleted_segments, "archive segments deleted", "archive segments would be deleted"),
    ]
    for items, done_title, would_title in sections:
        if items:
            out.append(f"{len(items)} {would_title if r.dry_run else done_title}:")
            out.extend(f"  {item}" for item in items)
    for title, planned, done in (
        ("per-record files and segments to fold", r.planned_folded, r.folded),
        ("shard lines to move", r.planned_moved, r.moved),
    ):
        unperformed = [item for item in planned if item not in done]
        if r.stopped and unperformed:
            out.append(f"{len(unperformed)} {title}, planned and NOT applied:")
            out.extend(f"  {item}" for item in unperformed)
    if r.skipped:
        out.append(f"{len(r.skipped)} identities skipped (left exactly where they are):")
        out.extend(f"  {s.record_type} {s.record_id}: {s.code}" for s in r.skipped)
    if r.kept:
        out.append(f"{len(r.kept)} files and segments kept:")
        out.extend(f"  {path}: {reason}" for path, reason in r.kept)
    if not (r.folded or r.moved or r.deleted_files or r.deleted_segments or r.wrote or r.stopped):
        out.append("No records to fold or move.")
    if r.wrote:
        title = "files would be written or removed" if r.dry_run else "files written or removed"
        out.append(f"{len(r.wrote)} {title}:")
        empty = set(r.empty_created) if len(r.empty_created) > _COLLAPSE_EMPTY_ABOVE else set()
        out.extend(f"  {path}" for path in r.wrote if path not in empty)
        if empty:
            verb = "would be created" if r.dry_run else "created"
            out.append(f"  {len(empty)} empty shard files {verb}")
    if r.carried:
        out.append(f"{len(r.carried)} findings carried over, not blocking (sidegraph-verify):")
        out.extend(f"  {line}" for line in r.carried[:_CARRIED_SHOWN])
        if len(r.carried) > _CARRIED_SHOWN:
            out.append(f"  and {len(r.carried) - _CARRIED_SHOWN} more")
    if not r.dry_run and r.wrote:
        if r.stopped:
            out.append(
                "The run stopped after writing these files; re-run to finish, or review and "
                "commit them."
            )
        else:
            out.append(
                "Maintenance never commits: review the store changes and commit them yourself."
            )
    return "\n".join(out) + "\n"


def _identity_and_body(raw: bytes) -> tuple[str, str, object]:
    decoded = decode_line(raw)
    return decoded.record_type or "", decoded.record_id or "", decoded.body


def _same_line_body(a: bytes, b: bytes) -> bool:
    """Two shard lines have equal parsed bodies (JSON types included); unparsed lines are
    equal only when their bytes are."""
    da, db = decode_line(a), decode_line(b)
    if da.error is not None or db.error is not None:
        return a == b
    try:
        return encode_body(da.body) == encode_body(db.body)
    except UnencodableBody:
        return a == b


class _Run:
    """The state of one publish: the images this run expects on disk, edited phase by phase."""

    def __init__(
        self,
        store_dir: Path,
        plan: Plan,
        expected: dict[Path, bytes],
        report: MaintenanceReport,
        *,
        dry: bool = False,
    ) -> None:
        self.dry = dry  # simulate: update the images and the report, touch no file
        self.report = report
        self.store_dir = store_dir
        self.shards_dir = store_dir / SHARDS_SUBDIR
        self.plan = plan
        self.n = plan.n
        self.expected = expected
        self.skipped = {(s.record_type, s.record_id) for s in plan.skipped}

    def bucket_path(self, b: int) -> Path:
        return self.shards_dir / bucket_name(b)

    def lines(self, path: Path) -> list[Line]:
        return [
            (*_identity_and_body(raw)[:2], raw) for _no, raw in split_lines(self.expected[path])
        ]

    def publish(self, path: Path, image: bytes) -> None:
        """Publish ``image`` over the file as this run last saw it; the image becomes expected."""
        if not self.dry:
            publish_bytes(path, image, expect=self.expected.get(path))
        self.expected[path] = image
        self.note(path)
        rel = path.relative_to(self.store_dir).as_posix()
        if image and rel in self.report.empty_created:
            self.report.empty_created.remove(rel)

    def note(self, path: Path) -> None:
        """Record a tracked path this run wrote or removed, once."""
        rel = path.relative_to(self.store_dir).as_posix()
        if rel not in self.report.wrote:
            self.report.wrote.append(rel)

    def sync(self, path: Path) -> None:
        if not self.dry:
            fsync_dir(path)

    def holds_equal(self, path: Path, record_type: str, record_id: str, raw: bytes) -> bool:
        """Whether the image expected at ``path`` has a line of the same identity with an equal
        body."""
        return any(
            (t, i) == (record_type, record_id) and _same_line_body(line, raw)
            for t, i, line in self.lines(path)
        )

    def check_on_disk(self, path: Path) -> None:
        """Raise :class:`StoreChanged` unless ``path`` holds exactly the expected image."""
        if self.dry:
            return
        if _read_if_regular(path) != self.expected.get(path):
            raise StoreChanged(f"{path.name} changed under the run")


def _prepare(run: _Run) -> None:
    """The preparation step, every run: ``shards/``, every missing bucket as a 0-byte file, the
    ``.gitattributes`` line, then ``fsync`` of every shard file already on disk, of the format
    marker and of both directories, before phase A."""
    if not run.dry:
        run.shards_dir.mkdir(exist_ok=True)
    checkpoint("prep:shards")
    existing = sorted(p for p in run.expected if p.parent == run.shards_dir)
    for b in range(run.n):
        path = run.bucket_path(b)
        if path in run.expected:
            continue
        if not run.dry:
            publish_bytes(path, b"", expect=None)
        run.expected[path] = b""
        run.note(path)
        run.report.empty_created.append(path.relative_to(run.store_dir).as_posix())
        checkpoint(f"prep:{path.name}")
    attrs = run.store_dir / GITATTRIBUTES_NAME
    current = _read_if_regular(attrs)
    if current is None or GITATTRIBUTES_LINE not in current.decode("utf-8", "replace").splitlines():
        base = b"" if current is None else current
        if base and not base.endswith(b"\n"):
            base += b"\n"
        if not run.dry:
            publish_bytes(attrs, base + GITATTRIBUTES_LINE.encode("utf-8") + b"\n", expect=current)
        run.note(attrs)
        checkpoint(f"prep:{GITATTRIBUTES_NAME}")
    if run.dry:
        return
    for path in [*existing, run.store_dir / FORMAT_MARKER_NAME]:
        try:
            # Windows flushes a regular file only through a handle with write access
            fd = os.open(
                path, (os.O_RDWR if _is_windows() else os.O_RDONLY) | getattr(os, "O_BINARY", 0)
            )
        except FileNotFoundError:
            raise StoreChanged(f"{path.name} vanished under the run") from None
        try:
            fsync_fd(fd)
        finally:
            os.close(fd)
    run.sync(run.shards_dir)
    run.sync(run.store_dir)
    checkpoint("prep:fsync")


def _phase_a(run: _Run) -> None:
    """Add: every bucket that should receive a misplaced line it lacks gets it, verbatim."""
    adds: dict[int, list[Line]] = {}
    for mv in run.plan.moves:
        path = run.bucket_path(mv.bucket)
        pending = adds.setdefault(mv.bucket, [])
        if run.holds_equal(path, mv.record_type, mv.record_id, mv.raw_line) or any(
            (t, i) == (mv.record_type, mv.record_id) and _same_line_body(line, mv.raw_line)
            for t, i, line in pending
        ):
            continue
        pending.append((mv.record_type, mv.record_id, mv.raw_line))
    published = False
    for b in sorted(adds):
        if not adds[b]:
            continue
        path = run.bucket_path(b)
        run.publish(path, render_shard([*run.lines(path), *adds[b]]))
        published = True
        checkpoint(f"A:{path.name}")
    if published:
        run.sync(run.shards_dir)


def _phase_b(run: _Run) -> None:
    """Remove: misplaced lines whose destination now holds an equal one, and equal duplicates
    of one identity inside a bucket; a stray file is unlinked when nothing is left in it."""
    by_source: dict[str, list[LineMove]] = {}
    for mv in run.plan.moves:
        by_source.setdefault(mv.source, []).append(mv)
    bucket_paths = {run.bucket_path(b) for b in range(run.n)}
    changed = False
    for path in sorted(run.expected, key=lambda p: (p not in bucket_paths, p.name)):
        if path.parent != run.shards_dir:
            continue
        moves = by_source.get(path.name, [])
        misplaced = {(m.record_type, m.record_id): m for m in moves}
        keep: list[Line] = []
        dropped_for: set[int] = set()
        gone: set[tuple[str, str]] = set()
        for t, i, raw in run.lines(path):
            ident = (t, i)
            if ident in run.skipped:
                keep.append((t, i, raw))
                continue
            move = misplaced.get(ident)
            if move is not None:
                if run.holds_equal(run.bucket_path(move.bucket), t, i, raw):
                    dropped_for.add(move.bucket)
                    gone.add(ident)
                    continue
                keep.append((t, i, raw))  # no equal survivor: never drop the last copy
                continue
            if path in bucket_paths and any(
                (kt, ki) == ident and _same_line_body(kl, raw) for kt, ki, kl in keep
            ):
                continue  # an equal duplicate in its own bucket collapses to the first
            keep.append((t, i, raw))
        before = run.lines(path)
        empty_stray = not before and path not in bucket_paths and may_unlink(path.name, run.n)
        if len(keep) == len(before) and not empty_stray:
            continue  # nothing to drop (an empty stray still goes: it holds no record)
        for b in sorted(dropped_for):
            run.check_on_disk(run.bucket_path(b))  # the survivors are still what we published
        changed = True
        if not keep and path not in bucket_paths and may_unlink(path.name, run.n):
            stage(f"before_unlink:{path.name}")
            run.check_on_disk(path)
            for b in sorted(dropped_for):
                run.check_on_disk(run.bucket_path(b))
            if not run.dry:
                os.unlink(path)
            del run.expected[path]
            run.note(path)
            checkpoint(f"unlink:{path.name}")
        else:
            run.publish(path, render_shard(keep))
            checkpoint(f"B:{path.name}")
        run.report.moved.extend(
            _move_text(m) for m in moves if (m.record_type, m.record_id) in gone
        )
    if changed:
        run.sync(run.shards_dir)


def _phase_c(run: _Run) -> None:
    """Replace and insert: every bucket is brought to its final content from its phase-B image
    (a replacement changes one line in place; unedited buckets are only canonicalised)."""
    replace: dict[int, dict[tuple[str, str], bytes]] = {}
    for w in [*run.plan.replacements, *run.plan.inserts]:
        replace.setdefault(w.bucket, {})[(w.record_type, w.record_id)] = w.raw_line
    published = False
    for b in range(run.n):
        path = run.bucket_path(b)
        edits = replace.get(b, {})
        lines = [line for line in run.lines(path) if (line[0], line[1]) not in edits]
        lines.extend((t, i, raw) for (t, i), raw in edits.items())
        image = render_shard(lines)
        if image == run.expected[path]:
            continue
        run.publish(path, image)
        published = True
        checkpoint(f"C:{path.name}")
        for w in [*run.plan.replacements, *run.plan.inserts]:
            if w.bucket == b and w.origin not in run.report.folded:
                run.report.folded.append(w.origin)  # a segment of many lines counts once
    if published:
        run.sync(run.shards_dir)


def _witness_holds(run: _Run, path: Path) -> bool:
    """Read the bucket a deletion relies on from disk, now, and compare its bytes with the image
    this run expects. Called before EVERY unlink (§28): no stat shortcut, since a rewrite that
    keeps size and mtime would pass it."""
    try:
        return _read_if_regular(path) == run.expected.get(path)
    except StoreChanged:
        return False


def _shard_body(run: _Run, record_type: str, record_id: str) -> list[object] | None:
    """The parsed bodies of the identity's lines in its bucket image; ``None`` when it has none."""
    path = run.bucket_path(bucket(record_id, run.n))
    bodies = [
        decode_line(raw).body for t, i, raw in run.lines(path) if (t, i) == (record_type, record_id)
    ]
    return bodies or None


def _delete_file(run: _Run, rel: str) -> str | None:
    """Unlink the per-record file ``rel`` if its shard line holds exactly its body. Returns the
    reason it stays, ``None`` when it was removed."""
    directory, _, name = rel.partition("/")
    record_type = _RECORD_DIR_TYPES.get(directory)
    if record_type is None or not name.endswith(".json"):
        return "not a per-record file"
    record_id = name[: -len(".json")]
    try:
        witness_path = run.bucket_path(bucket(record_id, run.n))
    except UnencodableBody:
        return "unencodable id"
    if not _witness_holds(run, witness_path):
        return "shard changed since it was written"
    bodies = _shard_body(run, record_type, record_id)
    if bodies is None:
        return "no shard line"
    path = run.store_dir / rel
    try:
        data = _read_if_regular(path)
        on_disk = None if data is None else json.loads(data.decode("utf-8"))
        if data is None:
            return ALREADY_GONE
        wanted = encode_body(on_disk)
        if any(body is None or encode_body(body) != wanted for body in bodies):
            return "changed since the plan"
    except (StoreChanged, ValueError, RecursionError):
        return "unreadable or changed since the plan"
    os.unlink(path)
    run.note(path)
    checkpoint(f"delete:{rel}")
    return None


def _segment_identities(data: bytes) -> list[tuple[str, str, dict]] | None:
    """``(record_type, id, body)`` of each line of an archive segment; ``None`` when any line is
    not a readable archive record."""
    out: list[tuple[str, str, dict]] = []
    for raw in data.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line.decode("utf-8"))
        except (ValueError, RecursionError):
            return None
        if not isinstance(parsed, dict) or parsed.get("record_type") not in _ARCHIVE_ID_FIELD:
            return None
        record_type = parsed["record_type"]
        body = archive_body(parsed)
        record_id = body.get(_ARCHIVE_ID_FIELD[record_type])
        if not isinstance(record_id, str):
            return None
        out.append((record_type, record_id, body))
    return out


def _delete_segment(run: _Run, rel: str) -> str | None:
    """Unlink the archive segment ``rel`` if it is unchanged since step 1 and every line is
    ``deletable`` against its identity's shard body. Returns the reason it stays."""
    path = run.store_dir / rel
    image = run.expected.get(path)
    if image is None:
        return "not read in step 1"
    try:
        if _read_if_regular(path) != image:
            return "changed since the plan"
    except StoreChanged:
        return "changed since the plan"
    lines = _segment_identities(image)
    if not lines:
        return "unreadable line"
    for record_type, record_id, body in lines:
        try:
            witness_path = run.bucket_path(bucket(record_id, run.n))
        except UnencodableBody:
            return "unencodable id"
        if not _witness_holds(run, witness_path):
            return "shard changed since it was written"
        bodies = _shard_body(run, record_type, record_id)
        if bodies is None or not all(
            isinstance(s, dict) and deletable(kind_of(record_type), body, s) for s in bodies
        ):
            return "not deletable"
    os.unlink(path)
    run.note(path)
    checkpoint(f"delete:{rel}")
    return None


def _delete(run: _Run, report: MaintenanceReport) -> None:
    """Step 4: remove the per-record files and archive segments the shards now hold, each only
    after its witnesses were re-read; fsync each affected directory after its unlinks."""
    removed_files: set[str] = set()
    for rel in run.plan.file_deletions:
        reason = _delete_file(run, rel)
        if reason is None:
            report.deleted_files.append(rel)
            if rel not in report.folded:
                report.folded.append(rel)  # its shard line already held it
            removed_files.add(rel.partition("/")[0])
        elif reason != ALREADY_GONE:
            report.kept.append((rel, reason))
    for directory in sorted(removed_files):
        fsync_dir(run.store_dir / directory)
    removed_segment = False
    for rel in run.plan.segment_deletions:
        reason = _delete_segment(run, rel)
        if reason is None:
            report.deleted_segments.append(rel)
            removed_segment = True
        else:
            report.kept.append((rel, reason))
    if removed_segment:
        fsync_dir(run.store_dir / ARCHIVE_SUBDIR)
    report.kept = sorted(set(report.kept))


def simulate_writes(
    store_dir: Path, plan: Plan, expected: dict[Path, bytes]
) -> tuple[list[str], list[str]]:
    """``(wrote, empty_created)``: every tracked path a real run of ``plan`` would publish or
    unlink (store-relative) and those of them that stay empty shard files, by
    running the phases over the expected images without touching a file; the per-record files
    and segments the plan deletes are added.
    # see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§6.2)"""
    scratch = MaintenanceReport(dry_run=True)
    run = _Run(store_dir, plan, dict(expected), scratch, dry=True)
    _prepare(run)
    _phase_a(run)
    _phase_b(run)
    _phase_c(run)
    wrote = list(scratch.wrote)
    wrote.extend(rel for rel in [*plan.file_deletions, *plan.segment_deletions] if rel not in wrote)
    return wrote, list(scratch.empty_created)


def publish_plan(
    store_dir: Path,
    plan: Plan,
    expected: dict[Path, bytes],
    wt: WorkTree | None,
    head0: str | None,
) -> MaintenanceReport:
    """Carry out ``plan`` (§6.2 step 3 and step 4). ``expected`` maps the absolute path of every
    shard file and archive segment to the bytes step 1 read; each phase renders from it plus
    its own edits and updates it with what it published, never from the pre-run bytes. A
    publish that finds its target changed raises nothing but ends the run, and so does a
    ``HEAD`` that moved since ``head0`` before the deletions; the report says why in ``stopped``.

    # see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§6.2)"""
    report = report_from_plan(plan, shallow=wt.shallow if wt else False, dry_run=False)
    run = _Run(store_dir, plan, dict(expected), report)
    try:
        _refuse_links(store_dir)  # after every seam, immediately before the first write
        _prepare(run)
        _phase_a(run)
        _phase_b(run)
        _phase_c(run)
    except StoreChanged as exc:
        report.folded.sort()
        report.stopped = f"the store changed under the run ({exc}); nothing was deleted"
        return report
    report.folded.sort()
    stage("before_step4")
    try:
        _refuse_links(store_dir)  # and again before the first unlink
    except StoreChanged as exc:
        report.stopped = f"the store changed under the run ({exc}); nothing was deleted"
        return report
    if wt is not None and maintenance_clock.read_head(wt) != head0:
        report.stopped = "HEAD moved before the deletions; nothing was deleted"
        return report
    _delete(run, report)
    return report


def plan_run(
    store_dir: Path,
    wt: WorkTree | None,
    head0: str | None,
    landing: dict[str, int] | None,
    now: int,
    *,
    settled_days: int,
    n: int,
    validate: Validator,
    report: MaintenanceReport,
    head_moved: Callable[[str], bool],
    assume_major: int | None = None,
) -> tuple[Inventory, Plan] | None:
    """Steps 1 and 2 of a run, shared by a real run, a dry run and the migration preview: the
    inventory, the entry conditions, the dirty set and the plan. ``None`` when the run stops
    here: ``report.stopped`` says why (an entry condition, ``HEAD`` moving, or a store entry
    that is a symlink). ``assume_major=2`` reads shard files under a marker below major 2,
    classified with ``n`` (migration only). Writes nothing.

    # see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§6.1, §6.2)"""
    if head_moved("before the run took the store"):
        return None
    inv = inventory_from_disk(
        store_dir, validate=validate, assume_major=assume_major, assume_shards=n
    )
    try:
        check_entry(inv, maintenance_clock.git_ls_files_shards(wt) if wt is not None else None, n)
    except EntryRefusal as refusal:
        report.stopped = "refused: " + "; ".join(refusal.reasons)
        return None
    stage("after_step1")
    if head_moved("while the inventory was built"):
        return None
    dirty = maintenance_clock.dirty_paths(wt, head0 or "") if wt is not None else frozenset()
    elig = Eligibility(settled_days, now, landing, dirty, wt.store_rel if wt is not None else "")
    plan = plan_maintenance(inv, elig, n)
    stage("after_step2")
    if head_moved("while the plan was made"):
        return None
    linked = symlinked_internals(store_dir)
    if linked:  # a link opened after the handle: never publish through it
        report.stopped = "refused: symlinked store entries: " + ", ".join(linked)
        return None
    return inv, plan


def preview(
    store_dir: Path, *, settled_days: int, n: int, validate: Validator, assume_major: int | None
) -> MaintenanceReport:
    """What a run on the store would do, planned without a ``Store`` handle: nothing is opened
    that creates a file, so the tree (and ``index.db``) is untouched. ``assume_major=2`` plans a
    store whose marker is still below major 2 as if it said ``shards n``.

    # see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§6.1)"""
    wt = maintenance_clock.resolve_work_tree(store_dir)
    if wt is None and settled_days != 0:
        stopped = MaintenanceReport(dry_run=True)
        stopped.stopped = "outside a git work tree there is no clock: use --settled-days 0"
        return stopped
    head0 = maintenance_clock.read_head(wt) if wt is not None else None
    landing = maintenance_clock.landing_times(wt) if wt is not None else None
    report = MaintenanceReport(shallow=wt.shallow if wt is not None else False, dry_run=True)

    def head_moved(step: str) -> bool:
        if wt is None or maintenance_clock.read_head(wt) == head0:
            return False
        report.stopped = f"HEAD moved {step}: run maintenance again"
        return True

    planned = plan_run(
        store_dir,
        wt,
        head0,
        landing,
        int(time.time()),
        settled_days=settled_days,
        n=n,
        validate=validate,
        report=report,
        head_moved=head_moved,
        assume_major=assume_major,
    )
    if planned is None:
        return report
    inv, plan = planned
    result = report_from_plan(plan, shallow=report.shallow, dry_run=True)
    images = {store_dir / rel: data for rel, data in inv.images.items()}
    result.wrote, result.empty_created = simulate_writes(store_dir, plan, images)
    return result
