"""The format-2 (packed store) grammar: shard lines, bucket function, marker, shard listing.

Stdlib only and pydantic-free, so the hook hot path can import it. The occurrence inventory at
the end reads a store (on disk or in a git tree) and takes the model check as a callback.

see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§5.2 lines, §5.3 listing,
§5.5 marker)
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import stat
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from .gitenv import git_env
from .store_layout import (
    ARCHIVE_SUBDIR,
    FORMAT_MARKER_NAME,
    NOT_A_FILE_REASON,
    SHARDS_SUBDIR,
    is_regular_file,
    is_safe_record_id,
    require_listable_record_dirs,
    stat_entry,
)

SHARD_COUNTS: tuple[int, ...] = (16, 64, 256)
DEFAULT_SHARD_COUNT = 64
FORMAT2_MARKER_VERSION = "2.0.0"
FORMAT2_INDEX_STAMP = "0.7.0+format2"
# The Sidegraph a fresh store requires: the first release that reads format 2.
FORMAT2_MIN_SIDEGRAPH = "0.17.0"
SHARD_TYPES = ("decision", "fact", "entity", "domain", "bindings")
GITATTRIBUTES_LINE = "*.shard -text"

_SHARD_SUFFIX = ".shard"
_MARKER_PREFIX = "sidegraph-store "
_REQUIREMENT_PREFIX = "requires-sidegraph >= "
_FIRST_LINE_RE = re.compile(r"sidegraph-store ([0-9]+)\.([0-9]+)\.([0-9]+)")
_REQUIREMENT_RE = re.compile(r"requires-sidegraph >= ([0-9]+\.[0-9]+\.[0-9]+)")
_SHARDS_LINE_RE = re.compile(r"shards (16|64|256)")
# The fixed envelope prefix, matched on the raw bytes so the identity of a line survives a body
# that no longer parses. The id may carry JSON escapes.
_PREFIX_RE = re.compile(rb'\{"type":"([^"\\]*)","id":"((?:[^"\\]|\\.)*)","body":')
_BUCKET_NAME_RE = re.compile(r"[0-9a-f]{2}\.shard")


class UnencodableBody(ValueError):
    """A record body that cannot be written as UTF-8 (a lone surrogate): it is reported, not
    packed. see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§5.2)"""


def encode_body(body: object) -> str:
    """The canonical one-line JSON of a record body: sorted keys, compact separators, raw
    non-ASCII. Raises :class:`UnencodableBody` on a lone surrogate.
    # see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§5.2)"""
    text = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    try:
        text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise UnencodableBody(f"body is not encodable as UTF-8: {exc.reason}") from exc
    return text


def archive_body(payload: dict[str, object]) -> dict[str, object]:
    """A copy of an archive payload minus its ``record_type`` key (the line's ``type`` carries
    it); unknown keys are kept.
    # see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§5.2)"""
    return {k: v for k, v in payload.items() if k != "record_type"}


def encode_line(record_type: str, record_id: str, body: object) -> bytes:
    """One shard line, envelope keys in the fixed order ``type``, ``id``, ``body``; no
    trailing newline.
    # see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§5.2)"""
    envelope = (
        '{"type":'
        + json.dumps(record_type, ensure_ascii=False)
        + ',"id":'
        + json.dumps(record_id, ensure_ascii=False)
        + ',"body":'
        + encode_body(body)
        + "}"
    )
    try:
        return envelope.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise UnencodableBody(f"line is not encodable as UTF-8: {exc.reason}") from exc


@dataclass(frozen=True)
class ShardLine:
    """A decoded shard line. ``error`` is set when the line is damaged; ``record_type`` and
    ``record_id`` are still recovered from the envelope prefix when it is intact."""

    record_type: str | None
    record_id: str | None
    body: object | None
    raw: bytes
    error: str | None


def decode_line(raw: bytes) -> ShardLine:
    """Parse one line (as :func:`split_lines` returns it). A damaged body keeps the identity
    recovered from the fixed prefix.
    # see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§5.2)"""
    try:
        # Strict UTF-8 first: json.loads on bytes would accept a BOM, UTF-16 and UTF-32.
        text = raw.decode("utf-8")
        if text.startswith("\ufeff"):
            return _damaged(raw, "line starts with a byte-order mark")
        parsed = json.loads(text, object_pairs_hook=_no_duplicate_keys)
    except (ValueError, RecursionError) as exc:  # JSONDecodeError and UnicodeDecodeError
        return _damaged(raw, f"line is not valid JSON: {exc!r:.200}")
    if (
        not isinstance(parsed, dict)
        or list(parsed) != ["type", "id", "body"]
        or not isinstance(parsed["type"], str)
        or not isinstance(parsed["id"], str)
    ):
        return _damaged(raw, "line is not a {type, id, body} envelope")
    return ShardLine(parsed["type"], parsed["id"], parsed["body"], raw, None)


def _no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    out: dict[str, object] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate key {key!r}")
        out[key] = value
    return out


def _damaged(raw: bytes, error: str) -> ShardLine:
    match = _PREFIX_RE.match(raw[3:] if raw.startswith(b"\xef\xbb\xbf") else raw)
    if match is None:
        return ShardLine(None, None, None, raw, error)
    try:
        record_type = match.group(1).decode("utf-8")
        record_id = json.loads(b'"' + match.group(2) + b'"')
    except ValueError:
        return ShardLine(None, None, None, raw, error)
    return ShardLine(record_type, record_id, None, raw, error)


def split_lines(data: bytes) -> list[tuple[int, bytes]]:
    """``(1-based line number, line bytes)``: split on byte ``\\n`` only (U+2028 is data), one
    trailing ``\\r`` stripped, blank lines dropped.
    # see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§5.2)"""
    out: list[tuple[int, bytes]] = []
    for number, line in enumerate(data.split(b"\n"), start=1):
        if line.endswith(b"\r"):
            line = line[:-1]
        if line.strip():
            out.append((number, line))
    return out


def conflict_regions(data: bytes) -> list[tuple[int, int]]:
    """Inclusive 1-based line ranges from a ``<<<<<<<`` line to the next ``>>>>>>>`` line; an
    unclosed region runs to the last line.
    # see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§5.3)"""
    regions: list[tuple[int, int]] = []
    start: int | None = None
    lines = data.split(b"\n")
    for number, line in enumerate(lines, start=1):
        if start is None and line.startswith(b"<<<<<<<"):
            start = number
        elif start is not None and line.startswith(b">>>>>>>"):
            regions.append((start, number))
            start = None
    if start is not None:
        regions.append((start, len(lines) - 1 if lines[-1] == b"" else len(lines)))
    return regions


def render_shard(lines: Iterable[tuple[str, str, bytes]]) -> bytes:
    """A shard file's bytes from ``(type, id, line)`` triples: sorted by ``(type, id)``, one
    newline per line, ``b""`` when empty.
    # see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§5.2)"""
    ordered = sorted(lines, key=lambda t: (t[0], t[1]))
    return b"".join(line + b"\n" for _, _, line in ordered)


def bucket(record_id: str, n: int) -> int:
    """The shard bucket of a record id among ``n`` shards.
    # see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§5.3)"""
    try:
        digest = hashlib.sha256(record_id.encode("utf-8")).hexdigest()
    except UnicodeEncodeError as exc:
        raise UnencodableBody(f"id is not encodable as UTF-8: {exc.reason}") from exc
    return int(digest[:2], 16) * n // 256


def bucket_name(b: int) -> str:
    """The shard file name of bucket ``b``."""
    return f"{b:02x}{_SHARD_SUFFIX}"


@dataclass(frozen=True)
class Marker:
    """A parsed format marker. ``requirement`` is ``None`` for major 0, ``shards`` for
    anything below major 2."""

    major: int
    requirement: str | None
    shards: int | None


def parse_marker(text: str, marker: Path, *, malformed: Callable[[Path, str], Exception]) -> Marker:
    """Parse a format marker of major 0 (one line), 1 (plus one requirement line) or 2 (plus
    the requirement line and ``shards N``, in that order, N in :data:`SHARD_COUNTS`). Blank
    lines and surrounding whitespace are ignored. Anything else raises ``malformed(marker,
    detail)``; no default shard count is assumed.
    # see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§5.5)"""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    first = lines[0] if lines else ""
    head = _FIRST_LINE_RE.fullmatch(first)
    if head is None:
        raise malformed(marker, f"{first!r} is not '{_MARKER_PREFIX}X.Y.Z'")
    major = int(head.group(1))
    extra = lines[1:]
    if major == 0:
        if extra:
            raise malformed(marker, f"a {first!r} marker has one line")
        return Marker(0, None, None)
    if major not in (1, 2):
        raise malformed(marker, f"{first!r} is not a major this parser reads")
    if len(extra) != major:
        raise malformed(
            marker, f"a {first!r} marker needs {major} line(s) after it, found {len(extra)}"
        )
    req = _REQUIREMENT_RE.fullmatch(extra[0])
    if req is None:
        raise malformed(marker, f"{extra[0]!r} is not '{_REQUIREMENT_PREFIX}X.Y.Z'")
    if major == 1:
        return Marker(1, req.group(1), None)
    if first != _MARKER_PREFIX + FORMAT2_MARKER_VERSION:
        raise malformed(marker, f"{first!r} is not '{_MARKER_PREFIX}{FORMAT2_MARKER_VERSION}'")
    count = _SHARDS_LINE_RE.fullmatch(extra[1])
    if count is None or int(count.group(1)) not in SHARD_COUNTS:
        raise malformed(marker, f"{extra[1]!r} is not 'shards N' with N in {SHARD_COUNTS}")
    return Marker(2, req.group(1), int(count.group(1)))


def marker_text(major: int, requirement: str | None, shards: int | None) -> str:
    """The marker file text of a major (0: ``0.1.0``, 1: ``1.0.0`` + requirement, 2:
    ``2.0.0`` + requirement + ``shards N``).
    # see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§5.5)"""
    if major == 0:
        return f"{_MARKER_PREFIX}0.1.0\n"
    if requirement is None:
        raise ValueError(f"a major {major} marker needs a requirement")
    if major == 1:
        return f"{_MARKER_PREFIX}1.0.0\n{_REQUIREMENT_PREFIX}{requirement}\n"
    if major != 2 or shards not in SHARD_COUNTS:
        raise ValueError(f"cannot write a marker for major {major}, shards {shards}")
    return (
        f"{_MARKER_PREFIX}{FORMAT2_MARKER_VERSION}\n{_REQUIREMENT_PREFIX}{requirement}\n"
        f"shards {shards}\n"
    )


@dataclass(frozen=True)
class ShardListing:
    """The ``*.shard`` files of a shards directory. ``buckets`` maps a bucket number to its
    canonically named file; ``strays`` are every other ``.shard`` file, sorted by name (a
    case-collision file is in both ``strays`` and ``case_collisions``)."""

    buckets: dict[int, Path]
    strays: list[Path]
    case_collisions: list[str]


def _regular_files(directory: Path) -> list[Path]:
    try:
        entries = sorted(directory.iterdir(), key=lambda p: p.name)
    except (FileNotFoundError, NotADirectoryError):
        return []  # absence is expected; an unlistable directory is not and propagates
    out: list[Path] = []
    for entry in entries:
        try:
            if stat.S_ISREG(entry.lstat().st_mode):
                out.append(entry)
        except FileNotFoundError:
            continue  # removed between listing and stat
    return out


def list_shard_files(shards_dir: Path, n: int | None) -> ShardListing:
    """Classify the regular files of ``shards_dir`` by lower-cased name. ``n`` is the marker's
    shard count (``None``: accept any two-hex-digit bucket). ``*.tmp`` and non-``.shard`` files
    are ignored.
    # see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§5.3)"""
    return classify_shard_files(_regular_files(shards_dir), n)


def classify_shard_files(files: Iterable[Path], n: int | None) -> ShardListing:
    """The pure half of :func:`list_shard_files`: classify already-listed regular files (a git
    tree's entries, too) by lower-cased name.
    # see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§5.3)"""
    shard_files = [p for p in files if p.name.lower().endswith(_SHARD_SUFFIX)]
    by_lower: dict[str, list[Path]] = {}
    for path in shard_files:
        by_lower.setdefault(path.name.lower(), []).append(path)
    buckets: dict[int, Path] = {}
    strays: list[Path] = []
    collisions: list[str] = []
    limit = 256 if n is None else n
    for path in shard_files:
        name = path.name
        lower = name.lower()
        is_bucket_name = _BUCKET_NAME_RE.fullmatch(lower) is not None and int(lower[:2], 16) < limit
        if name == lower and is_bucket_name:
            buckets[int(name[:2], 16)] = path  # its case variants are strays and collisions
            continue
        strays.append(path)
        if (name != lower and is_bucket_name) or len(by_lower[lower]) > 1:
            collisions.append(name)
    strays.sort(key=lambda p: p.name)
    return ShardListing(buckets, strays, sorted(collisions))


def has_shard_files(store_dir: Path) -> bool:
    """Whether ``<store_dir>/shards`` holds any ``.shard`` regular file, whatever its case."""
    return any(
        p.name.lower().endswith(_SHARD_SUFFIX)
        for p in _regular_files(Path(store_dir) / SHARDS_SUBDIR)
    )


# -- occurrence inventory ------------------------------------------------------------------
#
# see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§5.3)

Validator = Callable[[str, object], str | None]
"""``(record_type, body) -> model error text | None``: the model check as a callback, so this
module stays pydantic-free (``schema.validate_record_body``)."""

_UNSAFE_FILENAME_REASON = "unsafe filename"
SYMLINK_REASON = "symlink"
_RECORD_DIRS: tuple[tuple[str, str], ...] = (
    ("entities", "entity"),
    ("decisions", "decision"),
    ("facts", "fact"),
    ("domains", "domain"),
    ("bindings", "bindings"),
)
_ID_FIELDS = {"decision": "id", "fact": "id", "entity": "entity_id", "domain": "domain_id"}
_ARCHIVE_ID_FIELDS = {"decision": "id", "domain": "domain_id"}
_UNPARSEABLE = (ValueError, RecursionError)


@dataclass(frozen=True)
class Occurrence:
    """One occurrence of a record: a per-record file, a shard line or a legacy archive line.

    ``path`` is store-relative POSIX; ``k`` counts an identity's lines within one file, in file
    order; ``raw`` is the parsed JSON body (an archive line's minus ``record_type``) or ``None``
    when it did not parse; ``raw_bytes`` is the line as read without ``\\n`` and the one stripped
    ``\\r`` (the whole file for a file), so a move copies it and never re-encodes it. ``error`` is
    why the occurrence is not readable (``None`` when it is); ``parseable`` is whether ``raw`` was
    obtained; ``in_conflict`` marks a line inside a git conflict region (or a file holding one).
    ``record_type`` and ``record_id`` are ``None`` when no identity could be recovered, and an
    archive line of a ``record_type`` this version does not know has no identity and
    ``archive_type_known`` false.

    # see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§5.3)
    """

    record_type: str | None
    record_id: str | None
    kind: Literal["file", "shard", "archive"]
    path: str
    k: int
    line_no: int | None
    raw: object | None
    raw_bytes: bytes
    error: str | None
    parseable: bool
    in_conflict: bool
    archive_type_known: bool

    @property
    def readable(self) -> bool:
        return self.error is None


@dataclass
class Inventory:
    """Every occurrence of every record, plus what was read to build it. ``images`` holds each
    shard file and archive segment as read (store-relative path to bytes); ``stats`` holds
    ``(st_ino, st_size, st_mtime_ns)`` of each file read, **taken before the read** (stat-before-
    read, store.py's reload rule), so the reload's ``canonical_stat`` never certifies bytes newer
    than the ones parsed. ``stats`` is empty for a git tree. ``unrecoverable`` lists the shard
    lines from which no identity could be recovered.
    # see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§5.3)"""

    occurrences: list[Occurrence]
    marker: Marker | None
    listing: ShardListing | None
    unrecoverable: list[Occurrence]
    images: dict[str, bytes] = field(default_factory=dict)
    stats: dict[str, tuple[int, int, int]] = field(default_factory=dict)

    def by_identity(self) -> dict[tuple[str, str], list[Occurrence]]:
        """Occurrences grouped by ``(record_type, record_id)``, in inventory order; occurrences
        without an identity are in no group."""
        out: dict[tuple[str, str], list[Occurrence]] = {}
        for occ in self.occurrences:
            if occ.record_type is not None and occ.record_id is not None:
                out.setdefault((occ.record_type, occ.record_id), []).append(occ)
        return out


@dataclass(frozen=True)
class _Entry:
    """A file of the store as the builder sees it: ``data`` is ``None`` for an entry that is
    not a regular file (a directory, FIFO, symlink or submodule named like a record)."""

    rel: str
    data: bytes | None
    symlink: bool = False


class MalformedMarker(ValueError):
    """The ``format`` marker does not parse."""


def _malformed(marker: Path, detail: str) -> Exception:
    return MalformedMarker(f"malformed format marker {marker}: {detail}")


def _file_occurrence(
    subdir: str, record_type: str, name: str, entry: _Entry, validate: Validator
) -> Occurrence:
    stem = name[: -len(".json")]
    record_id = stem if is_safe_record_id(stem) else None
    data = entry.data
    raw: object | None = None
    error: str | None = None
    conflict = False
    if data is None:
        if entry.symlink:
            error = SYMLINK_REASON
        else:
            error = NOT_A_FILE_REASON if record_id is not None else _UNSAFE_FILENAME_REASON
    else:
        conflict = bool(conflict_regions(data))
        try:
            raw = json.loads(data.decode("utf-8"))
        except _UNPARSEABLE as exc:
            error = f"file is not valid JSON: {exc!r:.200}"
        else:
            if record_id is None:
                error = _UNSAFE_FILENAME_REASON
            else:
                error = _body_problem(record_type, record_id, raw, validate)
            if not _right_shape(record_type, raw):
                raw = None
    return Occurrence(
        record_type, record_id, "file", entry.rel, 0, None, raw, data or b"", error,
        raw is not None, conflict, False,
    )  # fmt: skip


def _right_shape(record_type: str, body: object) -> bool:
    return isinstance(body, list) if record_type == "bindings" else isinstance(body, dict)


def _body_problem(
    record_type: str, record_id: str, body: object, validate: Validator
) -> str | None:
    """Why a parsed body is not a readable record of identity ``(record_type, record_id)``: the
    wrong JSON shape, an id field that disagrees with the identity, or a model error."""
    if not _right_shape(record_type, body):
        want = "a list" if record_type == "bindings" else "an object"
        return f"{record_type} body is not {want}"
    if record_type != "bindings":
        id_field = _ID_FIELDS[record_type]
        assert isinstance(body, dict)
        if id_field not in body:
            return f"missing {id_field}"
        if body[id_field] != record_id:
            return f"{id_field} {body[id_field]!r} does not match the identity {record_id!r}"
    return validate(record_type, body)


def _regions_contain(regions: list[tuple[int, int]], line_no: int) -> bool:
    return any(lo <= line_no <= hi for lo, hi in regions)


def _shard_occurrences(rel: str, data: bytes, validate: Validator) -> list[Occurrence]:
    regions = conflict_regions(data)
    seen: dict[tuple[str | None, str | None], int] = {}
    out: list[Occurrence] = []
    for line_no, line in split_lines(data):
        decoded = decode_line(line)
        identity = (decoded.record_type, decoded.record_id)
        k = seen.get(identity, 0)
        seen[identity] = k + 1
        error = decoded.error
        raw = decoded.body
        if error is None:
            assert decoded.record_type is not None and decoded.record_id is not None
            if decoded.record_type not in SHARD_TYPES:
                error = f"unknown record type {decoded.record_type!r}"
            elif not is_safe_record_id(decoded.record_id):
                error = f"unsafe id {decoded.record_id!r}"
            else:
                error = _body_problem(decoded.record_type, decoded.record_id, raw, validate)
        out.append(
            Occurrence(
                decoded.record_type, decoded.record_id, "shard", rel, k, line_no, raw, line, error,
                decoded.error is None, _regions_contain(regions, line_no), False,
            )
        )  # fmt: skip
    return out


def _archive_occurrences(rel: str, data: bytes, validate: Validator) -> list[Occurrence]:
    """Lines of one archive segment, split and tolerated the way ``Store._archived_records``
    reads them: byte ``splitlines``, strip, skip blanks, strict UTF-8 per line."""
    seen: dict[tuple[str | None, str | None], int] = {}
    out: list[Occurrence] = []
    for line_no, raw_line in enumerate(data.splitlines(), start=1):
        line = raw_line.strip()  # the parse buffer; ``raw_line`` is the line as read
        if not line:
            continue
        record_type: str | None = None
        record_id: str | None = None
        body: object | None = None
        error: str | None = None
        known = False
        try:
            parsed = json.loads(line.decode("utf-8"))
        except _UNPARSEABLE as exc:
            parsed = None
            error = f"line is not valid JSON: {exc!r:.200}"
        if error is None and not isinstance(parsed, dict):
            error = "line is not a JSON object"
        if error is None:
            assert isinstance(parsed, dict)
            declared = parsed.get("record_type")
            body = archive_body(parsed)
            if isinstance(declared, str):
                record_type = declared
            known = isinstance(declared, str) and declared in _ARCHIVE_ID_FIELDS
            if known:
                assert record_type is not None
                id_field = _ARCHIVE_ID_FIELDS[record_type]
                if id_field not in body:
                    error = f"missing {id_field}"
                elif not is_safe_record_id(body[id_field]):
                    error = f"unsafe {id_field} {body[id_field]!r}"
                else:
                    record_id = str(body[id_field])
                    error = validate(record_type, body)
        k = seen.get((record_type, record_id), 0)
        seen[(record_type, record_id)] = k + 1
        out.append(
            Occurrence(
                record_type, record_id, "archive", rel, k, line_no, body, raw_line, error,
                body is not None, False, known,
            )
        )  # fmt: skip
    return out


def _build(
    entries: list[_Entry],
    *,
    marker: Marker | None,
    read_shards: bool,
    validate: Validator,
    assume_shards: int | None = None,
) -> tuple[list[Occurrence], ShardListing | None, dict[str, bytes]]:
    by_rel = {e.rel: e for e in entries}
    occurrences: list[Occurrence] = []
    for subdir, record_type in _RECORD_DIRS:
        prefix = subdir + "/"
        for rel in sorted(r for r in by_rel if r.startswith(prefix) and r.endswith(".json")):
            if "/" in rel[len(prefix) :]:
                continue
            name = rel[len(prefix) :]
            occurrences.append(_file_occurrence(subdir, record_type, name, by_rel[rel], validate))
    images: dict[str, bytes] = {}
    listing: ShardListing | None = None
    if read_shards:
        shard_prefix = SHARDS_SUBDIR + "/"
        regular = [
            Path(rel)
            for rel, e in sorted(by_rel.items())
            if rel.startswith(shard_prefix)
            and "/" not in rel[len(shard_prefix) :]
            and e.data is not None
        ]
        n = marker.shards if marker is not None and marker.major == 2 else assume_shards
        listing = classify_shard_files(regular, n)
        # only the classified shard files: a README or a ``*.tmp`` in ``shards/`` is not one
        for path in sorted([*listing.buckets.values(), *listing.strays], key=lambda p: p.name):
            rel = path.as_posix()
            data = by_rel[rel].data
            assert data is not None
            images[rel] = data
            occurrences.extend(_shard_occurrences(rel, data, validate))
    archive_prefix = ARCHIVE_SUBDIR + "/"
    for rel in sorted(r for r in by_rel if r.startswith(archive_prefix) and r.endswith(".jsonl")):
        if "/" in rel[len(archive_prefix) :]:
            continue
        data = by_rel[rel].data
        if data is None:
            continue  # a directory or FIFO named like a segment is not one
        images[rel] = data
        occurrences.extend(_archive_occurrences(rel, data, validate))
    return occurrences, listing, images


def _finish(
    entries: list[_Entry],
    marker: Marker | None,
    assume_major: int | None,
    validate: Validator,
    assume_shards: int | None = None,
) -> Inventory:
    major = assume_major if assume_major is not None else (marker.major if marker else None)
    occurrences, listing, images = _build(
        entries,
        marker=marker,
        read_shards=major == 2,
        validate=validate,
        assume_shards=assume_shards,
    )
    unrecoverable = [
        o
        for o in occurrences
        if o.kind == "shard" and (o.record_type is None or o.record_id is None)
    ]
    return Inventory(occurrences, marker, listing, unrecoverable, images)


def inventory_from_disk(
    store_dir: Path,
    *,
    validate: Validator,
    assume_major: int | None = None,
    assume_shards: int | None = None,
    on_unreadable: Callable[[str, OSError], None] | None = None,
    lenient_marker: bool = False,
) -> Inventory:
    """The occurrence inventory of the store at ``store_dir``. Shards are read only when the
    marker says major 2, or when ``assume_major=2`` (migration, under a major-1 or absent
    marker); ``assume_shards`` is then the shard count that classifies the files (a name
    beyond it is a stray), as the marker's would. Every file is ``stat``-ed before it is read.
    A file whose read fails propagates the error, except when ``on_unreadable`` is given: it is
    then called with the store-relative path and the error, and the file counts as not a regular
    file (``sidegraph-verify`` reports it as a finding instead of aborting). A marker that does
    not parse raises :class:`MalformedMarker`, or reads as no marker with ``lenient_marker``.
    # see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§5.3)"""
    store_dir = Path(store_dir)
    # a directory that cannot be listed would read as "no records": refuse before any walk
    require_listable_record_dirs(store_dir)
    marker_path = store_dir / FORMAT_MARKER_NAME
    marker: Marker | None = None
    stats: dict[str, tuple[int, int, int]] = {}
    with contextlib.suppress(FileNotFoundError):  # an absent marker is a fresh or major-less store
        st = marker_path.stat()  # before the read, like every other file
        stats[FORMAT_MARKER_NAME] = (st.st_ino, st.st_size, st.st_mtime_ns)
        try:
            try:
                text = marker_path.read_text(encoding="utf-8")
            except UnicodeDecodeError as exc:
                raise _malformed(marker_path, "not UTF-8 text") from exc
            marker = parse_marker(text, marker_path, malformed=_malformed)
        except MalformedMarker:
            if not lenient_marker:
                raise
    entries: list[_Entry] = []

    def take(path: Path, rel: str) -> None:
        try:
            st = stat_entry(path)
        except FileNotFoundError:
            return  # removed between listing and stat
        stats[rel] = (st.st_ino, st.st_size, st.st_mtime_ns)  # before the read, never after
        if not is_regular_file(st):
            entries.append(_Entry(rel, None))
            return
        try:
            entries.append(_Entry(rel, path.read_bytes()))
        except FileNotFoundError:
            del stats[rel]
        except OSError as exc:
            if on_unreadable is None:
                raise
            on_unreadable(rel, exc)
            entries.append(_Entry(rel, None))

    for subdir, _ in _RECORD_DIRS:
        for path in sorted((store_dir / subdir).glob("*.json")):
            take(path, f"{subdir}/{path.name}")
    major = assume_major if assume_major is not None else (marker.major if marker else None)
    if major == 2:
        n = marker.shards if marker is not None and marker.major == 2 else assume_shards
        found = list_shard_files(store_dir / SHARDS_SUBDIR, n)
        for path in {p for p in [*found.buckets.values(), *found.strays]}:
            take(path, f"{SHARDS_SUBDIR}/{path.name}")
    archive_dir = store_dir / ARCHIVE_SUBDIR
    if archive_dir.is_dir():
        for path in sorted(archive_dir.glob("*.jsonl")):
            take(path, f"{ARCHIVE_SUBDIR}/{path.name}")
    inv = _finish(entries, marker, assume_major, validate, assume_shards)
    inv.stats = stats
    return inv


def _git(
    repo_root: Path,
    args: list[str],
    *,
    stdin: bytes | None = None,
    timeout: float | None = None,
) -> bytes:
    """Run git in ``repo_root``. ``stdin=DEVNULL`` unless the call consumes input (a pipe
    carrying it): a child that inherits the parent's stdin reads it.
    # see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§5.3)"""
    result = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        input=stdin,
        stdin=subprocess.DEVNULL if stdin is None else None,
        capture_output=True,
        check=True,
        env=git_env(),
        timeout=timeout,
    )
    return result.stdout


def _cat_file_batch(
    repo_root: Path, oids: list[str], timeout: float | None = None
) -> dict[str, bytes]:
    """The blobs of ``oids`` through ONE ``git cat-file --batch``, the ids fed one per line
    through a pipe on stdin."""
    out = _git(
        repo_root,
        ["cat-file", "--batch"],
        stdin="".join(f"{o}\n" for o in oids).encode(),
        timeout=timeout,
    )
    blobs: dict[str, bytes] = {}
    pos = 0
    for oid in oids:
        end = out.index(b"\n", pos)
        header = out[pos:end].split(b" ")
        if len(header) != 3 or header[1] != b"blob":
            raise ValueError(f"git cat-file did not return blob {oid}: {out[pos:end]!r}")
        size = int(header[2])
        blobs[oid] = out[end + 1 : end + 1 + size]
        pos = end + 1 + size + 1
    return blobs


def inventory_from_git_tree(
    repo_root: Path,
    ref: str,
    store_rel: str,
    *,
    validate: Validator,
    remaining: Callable[[], float | None] | None = None,
) -> Inventory:
    """The occurrence inventory of the store at ``store_rel`` in ``ref``'s tree: one
    ``git ls-tree -r -z`` and one ``git cat-file --batch``. ``stats`` is empty. Shards are read
    only when the tree's marker says major 2. A committed symlink named like a record file is an
    occurrence with ``error == "symlink"`` (git cannot follow it in a tree, and the blob is the
    link's target text, which today's ``verify --against`` reads as unparseable content); the
    disk inventory follows a live symlink as the store always has, so the two views differ for
    that entry by design, and a verify task can map the reason.
    ``remaining`` is asked before each git call for the seconds that call may take (it may raise
    to refuse the call); a call that outlives it raises ``subprocess.TimeoutExpired``. Without
    it the calls have no limit, as before.
    # see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§5.3)"""
    root = store_rel.strip("/")
    prefix = "" if root in ("", ".") else root + "/"
    args = ["ls-tree", "-r", "-z", ref]
    if prefix:
        args += ["--", root]
    listing = _git(repo_root, args, timeout=remaining() if remaining else None)
    links: set[str] = set()
    kinds: dict[str, tuple[bool, str]] = {}  # store-relative path -> (is regular blob, oid)
    for item in listing.split(b"\0"):
        if not item:
            continue
        meta, _, name_bytes = item.partition(b"\t")
        mode, otype, oid = meta.decode("ascii").split(" ")
        name = name_bytes.decode("utf-8", "surrogateescape")
        if not name.startswith(prefix):
            continue
        kinds[name[len(prefix) :]] = (otype == "blob" and mode in ("100644", "100755"), oid)
        if mode == "120000":
            links.add(name[len(prefix) :])
    wanted = [oid for _, (regular, oid) in sorted(kinds.items()) if regular]
    blobs = (
        _cat_file_batch(repo_root, list(dict.fromkeys(wanted)), remaining() if remaining else None)
        if wanted
        else {}
    )
    entries = [
        _Entry(rel, blobs[oid] if regular else None, rel in links)
        for rel, (regular, oid) in sorted(kinds.items())
    ]
    marker: Marker | None = None
    marker_entry = next(
        (e for e in entries if e.rel == FORMAT_MARKER_NAME and e.data is not None), None
    )
    if marker_entry is not None and marker_entry.data is not None:
        try:
            marker_text_ = marker_entry.data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise _malformed(Path(FORMAT_MARKER_NAME), "not UTF-8 text") from exc
        marker = parse_marker(marker_text_, Path(FORMAT_MARKER_NAME), malformed=_malformed)
    return _finish(entries, marker, None, validate)
