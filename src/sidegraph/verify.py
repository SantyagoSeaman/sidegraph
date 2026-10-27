"""Integrity lint for the decision store — ``sidegraph-verify``.

See design/superpowers/specs/2026-07-11-ci-integrity-design.md ruling 2. Two layers:

- **Snapshot layer** (:func:`verify_snapshot`, always runs): every hot/archived file
  parses against its schema and the store's referential invariants — see this section's
  docstring below.
- **Transition layer** (:func:`verify_against`, ``--against <git-ref>``, CI mode): for
  each store file that CHANGED vs a git ref, classify the modification against the
  store's OWN write rules — append-only as a checkable contract, not a blanket "committed
  files never change" lint (which would be wrong: supersede legally closes a predecessor,
  ratify legally flips statuses, sync legally refreshes engine-mapping fields, compact
  legally deletes a hot file once archived). See the "transition layer" section near the
  bottom of this module for the mutable-field tables (derived from ``store.py``'s write
  methods, not invented) and :func:`classify_transition`/:func:`verify_against`.

**PURE READ.** :func:`verify_snapshot` opens the canonical files under a store directory
directly (``json.loads`` + ``Model.model_validate``) and never constructs a
:class:`sidegraph.store.Store`. Opening a ``Store`` is NOT a read-only operation — its
``__init__`` runs ``_refresh_freshness``, which can WRITE: a stale/missing digest triggers
``_reload_index_from_canonical`` (rewrites ``index.db``'s digest + ``schema_version``
meta), and an unmigrated legacy single-file store gets migrated in place. A lint command
must never mutate the very thing it is inspecting.

Reading the canonical files directly (rather than the derived ``index.db``) is also *the
point*, not just a side constraint: the index's own reload path is deliberately tolerant of
several on-disk shapes this module exists specifically to flag —
:func:`~sidegraph.store.Store._reload_index_from_canonical` silently treats a hot file that
duplicates an already-archived record as harmless "crash-window debris" whenever the two
copies are byte-identical (see
``docs/reference/store-format.md#archive-segments-sidegraph-compact``), and only warns (never
fails, never surfaces in any report) on a genuine hot/archive mismatch. ``verify_snapshot`` is
stricter on purpose, with exactly ONE sanctioned exception (design amendment, Task-2 review):
two or more archive segments carrying byte-IDENTICAL payloads for the same id are the store's
own tolerated cross-branch merge shape (independent ``sidegraph-compact`` runs on two
branches, later merged — see ``store._archived_records``'s "guaranteed byte-identical by
design") and are exempt from :data:`DUPLICATE_ULID`. Every other multi-location shape is
flagged: two+ hot files sharing an internal id (the store's file-per-record model allows at
most one), a hot file coexisting with ANY archive copy, or archive segments whose payloads for
the same id actually differ. A hot file whose *filename* doesn't match its own internal id is
a separate finding, :data:`FILENAME_ID_MISMATCH` — the store always writes
``<record's own id>.json``, so a mismatch is never legitimate.
"""

from __future__ import annotations

import json
import operator
import os
import re
import stat
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal, NamedTuple, overload

from pydantic import BaseModel, ValidationError

from . import domain_snapshot
from .config import _store_project_root
from .gitenv import git_env
from .schema import (
    SCHEMA_VERSION,
    AnchorBinding,
    Decision,
    Domain,
    DriftCheck,
    Entity,
    Fact,
    Initiative,
    is_safe_record_id,
    validate_record_body,
)
from .store import (
    _FORMAT_MARKER_NAME,
    _MIGRATABLE_SCHEMA_VERSIONS,
    _RELOADABLE_SCHEMA_VERSIONS,
    _TERMINAL_DECISION_STATUSES,
    _TERMINAL_DOMAIN_STATUSES,
    _archive_line_problem,
    _archive_payload_problem,
    symlinked_internals,
)
from .store_format import (
    FORMAT2_MARKER_VERSION,
    SYMLINK_REASON,
    Inventory,
    MalformedMarker,
    Marker,
    Occurrence,
    ShardListing,
    has_shard_files,
    inventory_from_disk,
    inventory_from_git_tree,
    parse_marker,
)
from .store_layout import (
    DOMAIN_SNAPSHOT_NAME,
    MAX_FORMAT_MARKER_BYTES,
    NOT_A_FILE_REASON,
    REQUIREMENT_MARKER_VERSION,
    SHARDS_SUBDIR,
    VERIFICATIONS_SUBDIR,
    StoreRequirementError,
    hot_matches_archive,
    is_regular_file,
    parse_format_marker,
    require_listable_record_dirs,
    requirement_met,
    running_version,
    same_modulo_absent,
    stat_entry,
    store_marker,
    store_requirement,
)

# Re-exported under the same names: the transition code lives in store_rules (pydantic-free).
from .store_rules import (  # noqa: F401
    _DECISION_STATUS_TRANSITIONS,
    _DOMAIN_STATUS_TRANSITIONS,
    _FACT_STATUS_TRANSITIONS,
    _KIND_ID_FIELDS,
    ABSENT,
    DECISION_MUTABLE_FIELDS,
    DOMAIN_MUTABLE_FIELDS,
    ENTITY_MUTABLE_FIELDS,
    FACT_MUTABLE_FIELDS,
    ILLEGAL_DELETION,
    ILLEGAL_FIELD_CHANGE,
    ILLEGAL_STATUS_JUMP,
    VALID_TO_CHANGED,
    VALID_TO_UNSET,
    Effective,
    IdentityDiff,
    Violation,
    _same_body,
    _strict_equal,
    _synthetic_path,
    _violation,
    classify_transition,
    compatible,
    deletable,
    diff_occurrences,
    effective_all,
    is_derived_entity_body,
    keeps_keys,
    kind_of,
    same_modulo_legacy_defaults,
    same_raw_body,
    superseding,
    without_legacy_defaults,
)

# Violation codes — pinned constants (design ruling 2). The CLI's --json output and every
# test assert against these exact strings, never a free-form message.
PARSE_ERROR = "parse-error"
UNKNOWN_SCHEMA_VERSION = "unknown-schema-version"
MALFORMED_REQUIREMENT = "malformed-requirement"
REQUIREMENT_ABOVE_VERIFIER = "requirement-above-verifier"
BAD_DOMAIN_SNAPSHOT = "bad-domain-snapshot"
SNAPSHOT_WITHOUT_REQUIREMENT = "snapshot-without-requirement"
BAD_VALIDITY_WINDOW = "bad-validity-window"
SUPERSEDED_WITHOUT_SUCCESSOR = "superseded-without-successor"
DANGLING_SUPERSEDES = "dangling-supersedes"
DANGLING_BINDING_ENTITY = "dangling-binding-entity"
DANGLING_FACT_SUPPORT = "dangling-fact-support"
DANGLING_DRIFT_CHECK = "dangling-drift-check"
DANGLING_PARENT = "dangling-parent"
PARENT_CYCLE = "parent-cycle"
DUPLICATE_ULID = "duplicate-ulid"
BAD_ARCHIVE_SEGMENT = "bad-archive-segment"
FILENAME_ID_MISMATCH = "filename-id-mismatch"
UNSAFE_RECORD_ID = "unsafe-record-id"
SYMLINKED_STORE_ENTRY = "symlinked-store-entry"
# At least one ``shards/*.shard`` beside a marker whose major is below 2.
SHARDS_WITHOUT_FORMAT2 = "shards-without-format2"
BAD_SHARD_LINE = "bad-shard-line"
SHARD_CONFLICT = "shard-conflict"
DUPLICATE_PACKED_RECORD = "duplicate-packed-record"
SHADOW_DIVERGENCE = "shadow-divergence"
BINDING_ENTRY_DROPPED = "binding-entry-dropped"
SHARD_NAME_CASE = "shard-name-case"
# The ``--against`` layer's findings for a packed store (§7.1): a shard line whose body no REF
# occurrence of the identity holds, and a marker that went from major 2 to below it.
SHARD_BODY_WITHOUT_SOURCE = "shard-body-without-source"
FORMAT_DOWNGRADE = "format-downgrade"
# The one line the CLIs print when a symlinked store entry stops the run at the links.
SYMLINK_STOP_NOTE = (
    "remaining checks skipped (the store has symlinked entries — remove the links and rerun)"
)


# A store's default committed ``format`` marker (see ``store._ensure_format_marker``)
# carries the running code's CURRENT ``SCHEMA_VERSION`` at creation. An explicit requirement
# uses REQUIREMENT_MARKER_VERSION instead; a mere reload rewrites neither marker.
# A store this code can actually OPEN (see
# ``store._RELOADABLE_SCHEMA_VERSIONS``/``_MIGRATABLE_SCHEMA_VERSIONS``) may therefore still
# carry an older marker; all three sets together are exactly "known to this running code",
# mirroring what ``Store.__init__`` itself tolerates without raising.
_KNOWN_SCHEMA_VERSIONS = frozenset(
    {SCHEMA_VERSION} | _RELOADABLE_SCHEMA_VERSIONS | _MIGRATABLE_SCHEMA_VERSIONS
)

# A ``1.0.0`` marker carries a minimum Sidegraph version (D6 of
# design/superpowers/specs/2026-10-14-graphify-upgrade-design.md).
# A ``2.0.0`` marker is the packed store.
# see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§5.5)
_KNOWN_MARKER_VERSIONS = _KNOWN_SCHEMA_VERSIONS | {
    REQUIREMENT_MARKER_VERSION,
    FORMAT2_MARKER_VERSION,
}

# Substring pydantic's wrapped ``ValueError`` carries for the ``Decision``/``Fact``
# validity-window ``model_validator`` (see schema.py) — see ``_check_temporal_dir``.
_VALIDITY_WINDOW_MSG = "valid_to must be >= valid_from"

# Both DecisionStatus.SUPERSEDED and DomainStatus.SUPERSEDED serialize to this same literal
# (see schema.py) — the supersedes-chain check is generic across decisions/facts/domains, so
# it compares against the raw string rather than importing either enum.
_SUPERSEDED = "superseded"


# -- generic file loading -----------------------------------------------------------------


def _iter_json_files(dir_path: Path) -> list[Path]:
    if not dir_path.is_dir():
        return []
    return sorted(dir_path.glob("*.json"))


def _load_raw_json(path: Path) -> tuple[object | None, Violation | None]:
    """Read+parse one file as JSON. Never raises — a decode/read failure becomes a
    :data:`PARSE_ERROR` violation instead, so one corrupt file never aborts the whole
    snapshot pass. A file that is not UTF-8, or is nested past the recursion limit, is one too,
    naming its path (design/superpowers/specs/2026-10-03-store-survives-a-bad-file-design.md
    D2)."""
    try:
        # Before any read: a directory or FIFO matched by ``*.json`` is not a file, and a read
        # of a FIFO blocks (the reload skips such an entry the same way).
        if not is_regular_file(stat_entry(path)):
            return None, _violation(PARSE_ERROR, path, f"unreadable: {NOT_A_FILE_REASON}")
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        return None, _violation(PARSE_ERROR, path, f"unreadable: {e}")
    except ValueError as e:  # UnicodeDecodeError: the file is not UTF-8
        return None, _violation(PARSE_ERROR, path, f"invalid UTF-8: {e}")
    try:
        return json.loads(text), None
    except json.JSONDecodeError as e:
        return None, _violation(PARSE_ERROR, path, f"invalid JSON: {e}")
    except RecursionError:  # nested deeper than the stack: the reload skips such a file too
        return None, _violation(PARSE_ERROR, path, "invalid JSON: nested too deeply")


def _check_filename_matches_id(path: Path, rid: str) -> Violation | None:
    """The store always names a canonical record file after its OWN internal id
    (``_write_decision_canonical`` et al. — ``<id>.json``, never anything else). A mismatch
    is never legitimate: either a hand-rename, or a hand-copy that also needs its own
    :data:`DUPLICATE_ULID` finding (see :func:`_check_duplicate_ulids`) — the two codes are
    independent and can both fire for the same file."""
    if path.stem != rid:
        return _violation(
            FILENAME_ID_MISMATCH, path, f"filename {path.stem!r} does not match record id {rid!r}"
        )
    return None


def _check_safe_stem(path: Path) -> Violation | None:
    """A record's id IS its filename (design/superpowers/specs/2026-09-29-record-identity-
    design.md D7): a stem failing :func:`is_safe_record_id` is an :data:`UNSAFE_RECORD_ID`
    finding, and the store's reload skips such a file whole. Checked BEFORE the file is
    parsed, so the finding does not depend on the content: a file that is not JSON, not UTF-8
    or not an object is the same skipped file, and ``parse-error`` would hide why."""
    if not is_safe_record_id(path.stem):
        return _violation(UNSAFE_RECORD_ID, path, f"filename {path.stem!r} is not a safe id")
    return None


def _stem_skipped_record(path: Path, id_field: str) -> tuple[str, dict] | None:
    """``(id, payload)`` of a file skipped for its stem when it holds a parseable object with a
    SAFE string id, else None: nothing legitimate references an unsafe id. The skip hides the
    file from the index, not from its neighbours: a record that points at that id has not lost
    anything, so the cross-reference checks must not report it (fixing the filename is the
    heal; editing the referrer would be an illegal field change). Never adds a finding: a file
    that does not parse has no id to offer."""
    raw, err = _load_raw_json(path)
    if err is not None or not isinstance(raw, dict) or not is_safe_record_id(raw.get(id_field)):
        return None
    return raw[id_field], raw


def _check_safe_record_id(path: Path, rid: object) -> Violation | None:
    """The other half of :func:`_check_safe_stem`: the id INSIDE a parsed record failing
    :func:`is_safe_record_id` is an :data:`UNSAFE_RECORD_ID` finding, and the reload skips
    that file whole too. A non-string id (list, dict) lands here too instead of crashing the
    callers' ``setdefault``."""
    if not is_safe_record_id(rid):
        return _violation(UNSAFE_RECORD_ID, path, f"record id {rid!r} is not a safe filename")
    return None


# -- per-directory checks ------------------------------------------------------------------


class _WalkResult(NamedTuple):
    """What a record-directory walk hands the cross-reference checks: ``skipped_by_id`` maps
    the id of each file skipped for its STEM (and holding a parseable object with a string id)
    to its payload, and the safe stem of each file that does not decode to an empty payload.
    It is kept apart from ``raw_by_id``/``path_by_id``/``all_paths_by_id`` so the file is no
    duplicate and takes over no record's findings, yet the references into it still resolve."""

    raw_by_id: dict[str, dict]
    path_by_id: dict[str, Path]
    all_paths_by_id: dict[str, list[Path]]
    skipped_by_id: dict[str, dict]
    violations: list[Violation]


def _is_regular_record_file(path: Path) -> bool:
    """Whether ``path`` is a regular file; ``False`` when it cannot even be stat-ed, so one
    entry's file-system error never aborts the report (``_load_raw_json`` reports it)."""
    try:
        return is_regular_file(stat_entry(path))
    except OSError:
        return False


def _check_temporal_dir(dir_path: Path, model: type[BaseModel]) -> _WalkResult:
    """``decisions/`` or ``facts/``: both carry the ``valid_to >= valid_from``
    ``model_validator`` (schema.py), which this function reports under its OWN code
    (:data:`BAD_VALIDITY_WINDOW`) rather than lumping it into :data:`PARSE_ERROR` — pydantic
    wraps that check into a ``ValidationError`` indistinguishable, by error TYPE, from a
    missing/mistyped field, so the error MESSAGE is what disambiguates the two: a hand-edited
    ``valid_from``/``valid_to`` produces exactly one error, and only that one, from a
    model-level (not field-level) validator.

    Returns ``(raw_by_id, path_by_id, all_paths_by_id, violations)``. ``path_by_id`` keeps
    only the FIRST hot file seen for a given internal id — used by the cross-reference checks,
    which need exactly one representative payload per id. ``all_paths_by_id`` keeps EVERY hot
    file carrying that id, because a second hot file for the same id is itself a finding
    (:data:`DUPLICATE_ULID` — see :func:`_check_duplicate_ulids`), never something to silently
    overwrite. A record stays in ``raw_by_id`` even when it fails full model validation (either
    code) as long as its JSON decodes and carries an ``id`` — its
    ``status``/``supersedes``/``supports`` are still real, checkable data; only a file that
    fails to decode at all contributes no record, but if it is a regular file its safe stem
    joins ``skipped_by_id`` with an empty payload (a stem-skipped file's readable payload for
    the same id wins): a reference to it is not dangling, because fixing or restoring the file
    is the heal, so one bad file gives one finding. Its own references are unknown, so a
    predecessor it supersedes may still read as superseded without a successor. A file the
    reload skips for an unsafe
    stem or an unsafe id gets :data:`UNSAFE_RECORD_ID` and nothing else, and joins no pool, so
    it is no duplicate and takes over no record's findings. A stem-skipped file's id is still
    returned in ``skipped_by_id``: a reference to it is not dangling, because renaming the file
    back is the heal. An unsafe id is not: nothing legitimate can reference it.
    """
    raw_by_id: dict[str, dict] = {}
    path_by_id: dict[str, Path] = {}
    all_paths_by_id: dict[str, list[Path]] = {}
    skipped_by_id: dict[str, dict] = {}
    undecodable: dict[str, dict] = {}
    violations: list[Violation] = []
    for path in _iter_json_files(dir_path):
        unsafe = _check_safe_stem(path)
        if unsafe is not None:
            violations.append(unsafe)
            peeked = _stem_skipped_record(path, "id")  # the reload skips this file whole
            if peeked is not None:
                skipped_by_id.setdefault(*peeked)
            continue
        raw, err = _load_raw_json(path)
        if err is not None or not isinstance(raw, dict) or "id" not in raw:
            violations.append(
                err or _violation(PARSE_ERROR, path, "not a record object (missing 'id')")
            )
            if _is_regular_record_file(path):  # a directory named *.json is no record
                undecodable.setdefault(path.stem, {})  # its stem is safe: see the docstring
            continue
        rid = raw["id"]
        unsafe = _check_safe_record_id(path, rid)
        if unsafe is not None:
            violations.append(unsafe)
            continue  # skipped whole by the reload too; a non-string id is unhashable as well
        all_paths_by_id.setdefault(rid, []).append(path)
        mismatch = _check_filename_matches_id(path, rid)
        if mismatch is not None:
            violations.append(mismatch)
        if rid not in raw_by_id:
            raw_by_id[rid] = raw
            path_by_id[rid] = path
        try:
            # ``model_dump_json`` too: the reload runs it on every record, so a payload that
            # validates but cannot be serialised (a lone surrogate) is left out of the index.
            model.model_validate(raw).model_dump_json()
        except ValidationError as e:
            errors = e.errors()
            if len(errors) == 1 and _VALIDITY_WINDOW_MSG in errors[0]["msg"]:
                violations.append(_violation(BAD_VALIDITY_WINDOW, path, errors[0]["msg"]))
            else:
                violations.append(_violation(PARSE_ERROR, path, str(e)))
        except ValueError as e:  # pydantic's serialisation error
            violations.append(_violation(PARSE_ERROR, path, f"cannot be serialised: {e}"))
    # A readable payload under that id (a stem-skipped file) wins over an unknown one.
    skipped = {**undecodable, **skipped_by_id}
    return _WalkResult(raw_by_id, path_by_id, all_paths_by_id, skipped, violations)


def _check_plain_dir(dir_path: Path, model: type[BaseModel], id_field: str) -> _WalkResult:
    """``domains/``, ``entities/``, ``initiatives/`` — no validity window, straightforward
    schema validation. ``id_field`` is ``"domain_id"``/``"entity_id"``/``"id"``. See
    :func:`_check_temporal_dir` for the ``path_by_id`` vs. ``all_paths_by_id`` split."""
    raw_by_id: dict[str, dict] = {}
    path_by_id: dict[str, Path] = {}
    all_paths_by_id: dict[str, list[Path]] = {}
    skipped_by_id: dict[str, dict] = {}
    undecodable: dict[str, dict] = {}
    violations: list[Violation] = []
    for path in _iter_json_files(dir_path):
        unsafe = _check_safe_stem(path)
        if unsafe is not None:
            violations.append(unsafe)
            peeked = _stem_skipped_record(path, id_field)  # the reload skips this file whole
            if peeked is not None:
                skipped_by_id.setdefault(*peeked)
            continue
        raw, err = _load_raw_json(path)
        if err is not None or not isinstance(raw, dict) or id_field not in raw:
            violations.append(
                err or _violation(PARSE_ERROR, path, f"not a record object (missing {id_field!r})")
            )
            if _is_regular_record_file(path):  # a directory named *.json is no record
                undecodable.setdefault(path.stem, {})  # its stem is safe: see the docstring
            continue
        rid = raw[id_field]
        unsafe = _check_safe_record_id(path, rid)
        if unsafe is not None:
            violations.append(unsafe)
            continue  # skipped whole by the reload too; a non-string id is unhashable as well
        all_paths_by_id.setdefault(rid, []).append(path)
        mismatch = _check_filename_matches_id(path, rid)
        if mismatch is not None:
            violations.append(mismatch)
        if rid not in raw_by_id:
            raw_by_id[rid] = raw
            path_by_id[rid] = path
        try:
            model.model_validate(raw).model_dump_json()  # see _check_temporal_dir
        except ValueError as e:  # ValidationError, or a payload that cannot be serialised
            violations.append(_violation(PARSE_ERROR, path, str(e)))
    # A readable payload under that id (a stem-skipped file) wins over an unknown one.
    skipped = {**undecodable, **skipped_by_id}
    return _WalkResult(raw_by_id, path_by_id, all_paths_by_id, skipped, violations)


def _check_drift_checks(store_dir: Path, decision_ids: set[str]) -> list[Violation]:
    """Lint auxiliary assertions, including terminal/archived subjects as valid history."""
    folder = store_dir / VERIFICATIONS_SUBDIR
    try:
        directory_stat = folder.stat()
    except FileNotFoundError:
        return []
    if not stat.S_ISDIR(directory_stat.st_mode):
        return [_violation(PARSE_ERROR, folder, "verification history is not a directory")]
    links = [path for path in _iter_json_files(folder) if path.is_symlink()]
    if links:
        return [
            _violation(SYMLINKED_STORE_ENTRY, path, "verification file is a symlink")
            for path in links
        ]
    raw, paths, all_paths, _, violations = _check_plain_dir(folder, DriftCheck, "id")
    violations += _check_duplicate_ulids(all_paths, {})
    for rid, payload in raw.items():
        target = payload.get("decision_id")
        if isinstance(target, str) and target not in decision_ids:
            violations.append(
                _violation(DANGLING_DRIFT_CHECK, paths[rid], "checked decision does not exist")
            )
    return violations


def _check_bindings_dir(
    dir_path: Path,
) -> tuple[dict[str, list[dict]], dict[str, Path], list[Violation]]:
    """``bindings/<record_id>.json``: a JSON list of ``{entity_id, tier, relation, weight}``
    (see ``store._binding_identity_payload``) — ``record_id`` itself is implied by the
    filename, not a key inside each item, so there is no internal id to compare it against
    (no :data:`FILENAME_ID_MISMATCH` check here)."""
    by_record: dict[str, list[dict]] = {}
    path_by_record: dict[str, Path] = {}
    violations: list[Violation] = []
    for path in _iter_json_files(dir_path):
        unsafe = _check_safe_stem(path)
        if unsafe is not None:
            violations.append(unsafe)
            continue  # the store's reload skips this file whole: no follow-on checks on it
        raw, err = _load_raw_json(path)
        if err is not None:
            violations.append(err)
            continue
        if not isinstance(raw, list):
            violations.append(_violation(PARSE_ERROR, path, "bindings file must be a JSON list"))
            continue
        record_id = path.stem
        items: list[dict] = []
        for i, item in enumerate(raw):
            payload = item if isinstance(item, dict) else {}
            try:
                AnchorBinding.model_validate({**payload, "record_id": record_id}).model_dump_json()
            except ValueError as e:  # ValidationError, or a payload that cannot be serialised
                violations.append(_violation(PARSE_ERROR, f"{path}#{i}", str(e)))
                continue
            items.append(payload)
        by_record[record_id] = items
        path_by_record[record_id] = path
    return by_record, path_by_record, violations


@dataclass
class _ArchiveIndex:
    """Everything read out of ``archive/*.jsonl``. ``*_entries`` keeps EVERY occurrence
    (path + payload) an id was seen in (unlike ``store._archived_records``'s dedup-to-first),
    because :func:`_check_duplicate_ulids` needs to compare payloads across occurrences, not
    just count them — see that function's docstring for the byte-identical exemption."""

    decisions: dict[str, dict]
    domains: dict[str, dict]
    decision_entries: dict[str, list[tuple[Path, dict]]]
    domain_entries: dict[str, list[tuple[Path, dict]]]
    violations: list[Violation]


_ARCHIVE_TERMINAL_STATUSES = {
    "decision": frozenset(status.value for status in _TERMINAL_DECISION_STATUSES),
    "domain": frozenset(status.value for status in _TERMINAL_DOMAIN_STATUSES),
}


def _archive_compaction_problem(kind: object, payload: dict) -> str | None:
    """Recognized archive records must be eligible for the Store's compaction API."""
    if not isinstance(kind, str):
        return None  # the archive reader also skips non-string future kind markers
    terminal = _ARCHIVE_TERMINAL_STATUSES.get(kind)
    if terminal is None:
        return None  # unknown future kinds retain the loader's forward compatibility
    status = payload.get("status")
    if not isinstance(status, str) or status not in terminal:
        return "record status is not eligible for compaction"
    return None


def _check_archive_dir(dir_path: Path) -> _ArchiveIndex:
    """``archive/*.jsonl`` — one JSON object per line (see
    ``docs/reference/store-format.md#archive-segments-sidegraph-compact`` and
    ``store._archive_record_line``). A line that fails to decode, parse or validate is
    :data:`BAD_ARCHIVE_SEGMENT`, scoped separately from :data:`PARSE_ERROR` (which is
    reserved for the one-file-per-record canonical directories) since "this JSONL segment has
    a bad line" and "this record file is corrupt" are different failure shapes worth
    distinguishing in a report. Decode/schema failures match the lines the store's reload
    leaves out and lists (design/superpowers/specs/2026-10-03-store-survives-a-bad-file-design.md
    D4); the verifier additionally rejects recognized nonterminal records, which the reload
    still reads. Only compaction-eligible records belong in an archive."""
    decisions: dict[str, dict] = {}
    domains: dict[str, dict] = {}
    decision_entries: dict[str, list[tuple[Path, dict]]] = {}
    domain_entries: dict[str, list[tuple[Path, dict]]] = {}
    violations: list[Violation] = []
    if not dir_path.is_dir():
        return _ArchiveIndex(decisions, domains, decision_entries, domain_entries, violations)
    for path in sorted(dir_path.glob("*.jsonl")):
        try:
            if not is_regular_file(stat_entry(path)):  # not a segment; a FIFO would block the read
                violations.append(
                    _violation(BAD_ARCHIVE_SEGMENT, path, f"unreadable: {NOT_A_FILE_REASON}")
                )
                continue
            data = path.read_bytes()
        except OSError as e:
            violations.append(_violation(BAD_ARCHIVE_SEGMENT, path, f"unreadable: {e}"))
            continue
        # Split and decode the way the store's reader does (``Store._read_archive_segment``):
        # ``bytes.splitlines`` breaks only at \n and \r, so a title holding U+2028 or U+0085
        # stays whole, and one line of invalid UTF-8 costs only that line.
        for lineno, raw_line in enumerate(data.splitlines(), start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line.decode("utf-8"))
            except (ValueError, RecursionError) as e:  # not UTF-8, not JSON, or nested too deep
                violations.append(_violation(BAD_ARCHIVE_SEGMENT, path, f"line {lineno}: {e}"))
                continue
            if not isinstance(obj, dict):
                violations.append(
                    _violation(BAD_ARCHIVE_SEGMENT, path, f"line {lineno}: not a JSON object")
                )
                continue
            record_type = obj.get("record_type")
            payload = {k: v for k, v in obj.items() if k != "record_type"}
            # Same predicate the store and the catalog skip by: a recognised record_type
            # whose id is missing, not a string or not a safe filename stem is not indexed.
            problem = _archive_line_problem(record_type, payload)
            if problem is not None:
                violations.append(
                    _violation(
                        UNSAFE_RECORD_ID,
                        path,
                        f"line {lineno}: archive record cannot be indexed: {problem}",
                    )
                )
                continue
            # The same payload check the store's reload runs on every archive line: a record
            # the model rejects is left out of the index. It stays in the id maps below, like
            # a hot file that fails validation (see ``_check_temporal_dir``): its id and
            # status are still real data for the cross-reference checks.
            problem = _archive_payload_problem(record_type, payload) or _archive_compaction_problem(
                record_type, payload
            )
            if problem is not None:
                violations.append(
                    _violation(BAD_ARCHIVE_SEGMENT, path, f"line {lineno}: {problem}")
                )
            if record_type == "decision" and "id" in payload:
                decisions.setdefault(payload["id"], payload)
                decision_entries.setdefault(payload["id"], []).append((path, payload))
            elif record_type == "domain" and "domain_id" in payload:
                domains.setdefault(payload["domain_id"], payload)
                domain_entries.setdefault(payload["domain_id"], []).append((path, payload))
            # An unrecognized record_type is silently skipped -- forward-compat with a future
            # record kind this version doesn't know how to load, mirroring
            # store.py's own ``_archived_records`` tolerance. The line still parsed as JSON,
            # so it is not a bad-archive-segment finding.
    return _ArchiveIndex(decisions, domains, decision_entries, domain_entries, violations)


def _check_format_marker(store_dir: Path) -> list[Violation]:
    """``<store_dir>/format``: ``sidegraph-store <version>``, and on a ``1.0.0`` marker exactly
    one ``requires-sidegraph >= X.Y.Z`` line (``store_layout.parse_format_marker``). Missing,
    unreadable, unrecognised or an unknown version: :data:`UNKNOWN_SCHEMA_VERSION`. A
    requirement line missing, unknown or unparsable: :data:`MALFORMED_REQUIREMENT`. A
    requirement above the running verifier (a CI pin older than the store):
    :data:`REQUIREMENT_ABOVE_VERIFIER`.
    # see design/superpowers/specs/2026-10-14-graphify-upgrade-design.md (D6)"""
    marker = store_dir / _FORMAT_MARKER_NAME
    if marker.is_symlink():
        # Reported as ``symlinked-store-entry`` by the caller; reading it would echo the link
        # target's text into a violation detail, and so into a CI log.
        return []
    if not marker.is_file():
        return [_violation(UNKNOWN_SCHEMA_VERSION, marker, "format marker missing")]
    try:
        with marker.open("rb") as stream:
            raw = stream.read(MAX_FORMAT_MARKER_BYTES + 1)
    except OSError as e:
        return [_violation(UNKNOWN_SCHEMA_VERSION, marker, f"unreadable: {e}")]
    if len(raw) > MAX_FORMAT_MARKER_BYTES:
        return [
            _violation(
                MALFORMED_REQUIREMENT,
                marker,
                f"format marker exceeds {MAX_FORMAT_MARKER_BYTES} bytes",
            )
        ]
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return [_violation(UNKNOWN_SCHEMA_VERSION, marker, "format marker is not UTF-8 text")]
    try:
        version, requirement = parse_format_marker(text, marker)
    except StoreRequirementError as e:
        code = MALFORMED_REQUIREMENT if e.kind == "malformed" else UNKNOWN_SCHEMA_VERSION
        return [_violation(code, marker, str(e))]
    if version not in _KNOWN_MARKER_VERSIONS:
        return [_violation(UNKNOWN_SCHEMA_VERSION, marker, f"unknown schema_version {version!r}")]
    if requirement is not None and not requirement_met(requirement):
        return [
            _violation(
                REQUIREMENT_ABOVE_VERIFIER,
                marker,
                f"the store requires Sidegraph >= {requirement}; this verifier is "
                f"{running_version()}: move the CI pin to at least {requirement}",
            )
        ]
    return []


def _check_shards_without_format2(store_dir: Path) -> list[Violation]:
    """A marker major below 2 (or no marker) beside at least one ``shards/*.shard`` file, found
    by the case-insensitive shard listing: every reader refuses such a store, since serving it
    would hide the packed records. An empty ``shards/``, or one holding only ``*.tmp``, is not
    packed data. A marker that does not parse is the marker check's finding.
    # see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§5.5, §7.1)"""
    try:
        found = store_marker(store_dir)
    except StoreRequirementError:
        return []
    if (found is not None and found.major >= 2) or not has_shard_files(store_dir):
        return []
    return [
        _violation(
            SHARDS_WITHOUT_FORMAT2,
            store_dir / SHARDS_SUBDIR,
            "shard files beside a format marker below major 2: run sidegraph-migrate, or "
            "restore the major-2 marker (a format conflict resolved toward the older branch)",
        )
    ]


def _check_domain_snapshot(store_dir: Path) -> list[Violation]:
    """``<store_dir>/domain-snapshot.json``, when present: it validates as a format-1
    snapshot, and the format marker requires at least the first Sidegraph that reads it (an
    older one would open the store and show the domains the snapshot exists to keep). A
    symlinked snapshot is reported as ``symlinked-store-entry``; a malformed marker by the
    marker check.
    # see design/superpowers/specs/2026-10-14-graphify-upgrade-design.md (D5.4)"""
    path = store_dir / DOMAIN_SNAPSHOT_NAME
    if path.is_symlink() or not path.exists():
        return []
    if not path.is_file():
        return [_violation(BAD_DOMAIN_SNAPSHOT, path, NOT_A_FILE_REASON)]
    out: list[Violation] = []
    try:
        domain_snapshot.parse_snapshot(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, domain_snapshot.SnapshotError) as e:
        out.append(_violation(BAD_DOMAIN_SNAPSHOT, path, str(e)))
    try:
        required = store_requirement(store_dir)
    except StoreRequirementError:
        return out
    floor = domain_snapshot.SNAPSHOT_MIN_SIDEGRAPH
    # requirement_met(a, b) is "b >= a" by release.
    if required is None or not requirement_met(floor, required):
        out.append(
            _violation(
                SNAPSHOT_WITHOUT_REQUIREMENT,
                path,
                f"the format marker must require Sidegraph >= {floor} (it requires "
                f"{required or 'nothing'}); run `sidegraph-require --sidegraph {floor}` and commit "
                "the marker",
            )
        )
    return out


# -- cross-reference checks -----------------------------------------------------------------


def _check_supersedes(
    combined: dict[str, dict],
    path_by_id: Mapping[str, Path | str],
    skipped: dict[str, dict],
    extra_successors: frozenset[str] | set[str] = frozenset(),
) -> list[Violation]:
    """Generic across decisions/facts/domains — mirrors the real write-path rule
    (``store.add_decision``/``add_fact``/``supersede_domain``): a successor's ``supersedes``
    must reference a record that exists (:data:`DANGLING_SUPERSEDES`), and a record whose
    ``status`` is ``superseded`` must be the target of some OTHER record's ``supersedes``
    (:data:`SUPERSEDED_WITHOUT_SUCCESSOR`) — the same "close + link, atomically" guarantee
    ``add_decision``'s docstring describes. ``combined`` is the union of hot and archived
    payloads for one record type (a superseded record's successor may itself have since been
    compacted, so both pools count as "exists"). ``skipped`` holds the files the reload skips for
    their name (:class:`_WalkResult`): a reference to one, or from one, raises no finding."""
    violations: list[Violation] = []
    # Only a string can name a record: a hand-edited list/dict ``supersedes`` is unhashable
    # (it would crash the set and the ``in combined`` test) and is already reported by the
    # model check (parse-error, or bad-archive-segment for an archived line).
    successors = {
        payload["supersedes"]
        for payload in (*combined.values(), *skipped.values())
        if isinstance(payload.get("supersedes"), str) and payload["supersedes"]
    } | set(extra_successors)  # a packed store: links of every readable occurrence
    for rid, payload in combined.items():
        supersedes = payload.get("supersedes")
        if (
            supersedes
            and isinstance(supersedes, str)
            and supersedes not in combined
            and supersedes not in skipped
        ):
            violations.append(
                _violation(
                    DANGLING_SUPERSEDES,
                    path_by_id.get(rid, rid),
                    f"supersedes unknown record {supersedes!r}",
                )
            )
        if payload.get("status") == _SUPERSEDED and rid not in successors:
            violations.append(
                _violation(
                    SUPERSEDED_WITHOUT_SUCCESSOR,
                    path_by_id.get(rid, rid),
                    f"status is superseded but no record's supersedes references {rid!r}",
                )
            )
    return violations


_CYCLE_TEXT_IDS = 10


def _cycle_text(cycle: list[str]) -> str:
    """``a -> b -> a``; past :data:`_CYCLE_TEXT_IDS` ids the middle is counted, not listed."""
    shown = cycle if len(cycle) <= _CYCLE_TEXT_IDS else cycle[:_CYCLE_TEXT_IDS]
    tail = [] if len(cycle) <= _CYCLE_TEXT_IDS else [f"... ({len(cycle)} domains)"]
    return " -> ".join([*shown, *tail, cycle[0]])


def _check_domain_parents(
    combined: dict[str, dict], path_by_id: Mapping[str, Path | str], skipped: dict[str, dict]
) -> list[Violation]:
    """Mirrors ``Store._check_domain_parent_acyclic``: a domain's ``parent_id`` must name an
    existing domain (:data:`DANGLING_PARENT`) and the parent chain must not loop
    (:data:`PARENT_CYCLE`). ``parent_id`` is immutable and the write path refuses both shapes, so
    only a hand edit can produce them. Like the write path, existence is all that counts: a
    superseded, dropped or archived parent is still a parent, so ``combined`` is the hot plus
    archived pool, and ``skipped`` (:class:`_WalkResult`) holds the files the reload skips for
    their name: a parent reference to one raises no finding. Only a string can name a domain: a
    hand-edited list/dict ``parent_id`` is unhashable and is already reported by the model check."""
    violations: list[Violation] = []
    for did, payload in combined.items():
        parent = payload.get("parent_id")
        if isinstance(parent, str) and parent not in combined and parent not in skipped:
            violations.append(
                _violation(
                    DANGLING_PARENT,
                    path_by_id.get(did, did),
                    f"parent_id references unknown domain {parent!r}",
                )
            )
    # Each domain has at most one parent, so a walk from every unvisited id, in sorted order,
    # meets each cycle exactly once; the finding sits at the cycle's smallest id.
    done: set[str] = set()
    for start in sorted(combined):
        walk: dict[str, int] = {}
        current: object = start
        while isinstance(current, str) and current in combined and current not in done:
            if current in walk:
                cycle = list(walk)[walk[current] :]
                first = min(cycle)
                cycle = cycle[cycle.index(first) :] + cycle[: cycle.index(first)]
                violations.append(
                    _violation(
                        PARENT_CYCLE,
                        path_by_id.get(first, first),
                        "parent_id chain loops back on itself: " + _cycle_text(cycle),
                    )
                )
                break
            walk[current] = len(walk)
            current = combined[current].get("parent_id")
        done.update(walk)
    return violations


def _check_binding_entities(
    bindings_by_record: dict[str, list[dict]],
    path_by_record: Mapping[str, Path | str],
    entity_ids: set[str],
) -> list[Violation]:
    """Every ``bindings/<record_id>.json`` entry's ``entity_id`` must resolve to a real
    ``entities/<id>.json`` (mirrors ``store.add_binding``'s own existence check)."""
    violations: list[Violation] = []
    for record_id, items in bindings_by_record.items():
        path = path_by_record[record_id]
        for item in items:
            entity_id = item.get("entity_id")
            if entity_id not in entity_ids:
                violations.append(
                    _violation(
                        DANGLING_BINDING_ENTITY,
                        path,
                        f"binding references unknown entity {entity_id!r}",
                    )
                )
    return violations


def _check_fact_supports(
    facts_raw: dict[str, dict], path_by_id: Mapping[str, Path | str], decision_ids: set[str]
) -> list[Violation]:
    """Every id in a ``Fact.supports`` list must resolve to a real decision (hot or archived)
    — mirrors ``store.add_fact``'s own existence check."""
    violations: list[Violation] = []
    for fid, payload in facts_raw.items():
        # A hand-edited non-list ``supports`` (int, str, dict) or a non-string item (list/dict,
        # unhashable) is already reported by the model check as parse-error; only a list of
        # strings is checkable here.
        supports = payload.get("supports")
        if not isinstance(supports, list):
            continue
        for did in supports:
            if isinstance(did, str) and did not in decision_ids:
                violations.append(
                    _violation(
                        DANGLING_FACT_SUPPORT, path_by_id[fid], f"supports unknown decision {did!r}"
                    )
                )
    return violations


def _check_duplicate_ulids(
    hot_paths_by_id: Mapping[str, Sequence[Path | str]],
    archive_entries_by_id: dict[str, list[tuple[Path, dict]]],
    *,
    strict: bool = False,
) -> list[Violation]:
    """An id's canonical locations must be unique, with exactly ONE sanctioned exception
    (design amendment, Task-2 review): two or more archive segments carrying byte-IDENTICAL
    payloads for the same id are the store's own tolerated cross-branch merge shape
    (independent ``sidegraph-compact`` runs on two branches, later merged — see
    ``store._archived_records``'s "guaranteed byte-identical by design") and are exempt.
    Every other multi-location shape is :data:`DUPLICATE_ULID`: two+ hot files sharing an id
    (the store's file-per-record model allows at most one — ``hot_paths_by_id`` tracks every
    occurrence precisely so this can't be masked by a silent last-write-wins dict merge), a
    hot file coexisting with ANY archive copy, or archive segments whose payloads for the same
    id actually DIFFER (corruption-shaped, not a legitimate merge, regardless of how it arose).
    The exemption is byte-equality (payload-dict equality, mirroring
    ``store._reload_index_from_canonical``'s own ``json.loads(...) != payload`` comparison),
    never id-equality alone. On a packed store (``strict``) the comparison keeps JSON types, so
    ``1``, ``1.0`` and ``true`` differ; format 1 keeps Python's ``==``.
    """
    violations: list[Violation] = []
    all_ids = set(hot_paths_by_id) | set(archive_entries_by_id)
    for rid in sorted(all_ids):
        hot_locs = hot_paths_by_id.get(rid, [])
        archive_locs = archive_entries_by_id.get(rid, [])
        if len(hot_locs) + len(archive_locs) <= 1:
            continue
        if not hot_locs:
            payloads = [payload for _, payload in archive_locs]
            same = _strict_equal if strict else operator.eq  # format 1 keeps Python's ``==``
            if all(same(payload, payloads[0]) for payload in payloads):
                continue  # sanctioned cross-branch archive merge -- exempt
        locations = list(hot_locs) + [path for path, _ in archive_locs]
        detail = "present in " + "; ".join(str(p) for p in locations)
        violations.append(_violation(DUPLICATE_ULID, locations[0], detail))
    return violations


# -- packed (format 2) snapshot layer ---------------------------------------------------------
#
# One occurrence inventory of the store (``store_format.inventory_from_disk``) read beside the
# per-record and archive walks above, which stay as they are so a format-1 report is unchanged.
# Shard lines join the reference pools, and the findings below are the ones only a packed store
# can have. Verify keeps one severity: every finding is an error.
# see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§7.1)

_Identity = tuple[str, str]
_POOLED_TYPES = ("decision", "fact", "domain", "entity", "bindings")
_REFERENCED_TYPES = ("decision", "fact", "domain")


@dataclass
class _Packed:
    """The inventory of a format-2 store and what the reference pools take from it.

    ``reps[record_type][id]`` is ``(body, where)`` for a record with no per-record file: the
    body of its effective READABLE shard line, ``where`` being ``shards/NN.shard:<line>``. An
    unreadable line never enters a pool (its model failure is ``bad-shard-line``; a body that
    failed the model may hold ids of the wrong type), but its identity is kept in ``held`` so a
    reference to it does not dangle. ``pooled`` holds the ids of the occurrences the pools carry.
    ``eff`` is the effective record of every identity, computed once."""

    inv: Inventory
    eff: dict[_Identity, Effective]
    by_id: dict[_Identity, list[Occurrence]]
    reps: dict[str, dict[str, tuple[object, str]]]
    held: dict[str, dict[str, dict]]
    entity_ids: set[str]
    pooled: set[int]
    archive_carried: set[int]
    unreadable: list[tuple[str, OSError]]

    def bodies(self, record_type: str) -> dict[str, dict]:
        return {i: b for i, (b, _) in self.reps[record_type].items() if isinstance(b, dict)}

    def paths(self, record_type: str) -> dict[str, str]:
        return {i: w for i, (_, w) in self.reps[record_type].items()}

    def successor_links(self) -> dict[str, set[str]]:
        """The ids some READABLE occurrence names in ``supersedes`` (a file, a shard line, an
        archive line), per record type: the one successor set of every per-record check of a
        packed snapshot, so a link held by a shadowed occurrence still counts."""
        links: dict[str, set[str]] = {t: set() for t in _REFERENCED_TYPES}
        for occ in self.inv.occurrences:
            if occ.readable and occ.record_type in links and isinstance(occ.raw, dict):
                target = occ.raw.get("supersedes")
                if isinstance(target, str) and target:
                    links[str(occ.record_type)].add(target)
        return links

    def uncovered(self, file_ids: dict[str, set[str]]) -> list[Occurrence]:
        """Readable occurrences no pool carries, so the per-record checks of the walks above
        never saw them: a shard line shadowed by a per-record file or a second copy, and an
        archive line other than the one the archive pool kept for its id, or one a file or
        shard line overrides in the pools. ``file_ids`` are the ids the per-record walk holds."""
        out: list[Occurrence] = []
        for occ in self.inv.occurrences:
            if not occ.readable or occ.in_conflict or occ.record_type is None:
                continue
            if occ.kind == "shard":
                if occ.record_type in self.reps and id(occ) not in self.pooled:
                    out.append(occ)
            elif occ.kind == "archive" and occ.record_id is not None:
                carried = (
                    id(occ) in self.archive_carried
                    and occ.record_id not in file_ids.get(occ.record_type, set())
                    and occ.record_id not in self.reps.get(occ.record_type, {})
                )
                if not carried:
                    out.append(occ)
        return out


def _where(occ: Occurrence) -> str:
    return f"{occ.path}:{occ.line_no}"


def _right_shape(record_type: str, raw: object) -> bool:
    return isinstance(raw, list) if record_type == "bindings" else isinstance(raw, dict)


def _packed_inventory(store_dir: Path) -> _Packed | None:
    """The inventory when the marker is major 2, else ``None``: a store below major 2 has no
    shard layer to read (``shards-without-format2`` is the marker check's finding). A marker
    that does not parse is the marker check's finding too. A shard file that cannot be read is
    kept in ``unreadable`` and becomes a finding; the other checks still run."""
    unreadable: list[tuple[str, OSError]] = []
    try:
        found = store_marker(store_dir)
        if found is None or found.major != 2:
            return None
        inv = inventory_from_disk(
            store_dir,
            validate=validate_record_body,
            on_unreadable=lambda rel, exc: unreadable.append((rel, exc)),
        )
    except (StoreRequirementError, ValueError):
        return None
    eff = effective_all(inv)
    by_id = inv.by_identity()
    reps: dict[str, dict[str, tuple[object, str]]] = {t: {} for t in _POOLED_TYPES}
    held: dict[str, dict[str, dict]] = {t: {} for t in _REFERENCED_TYPES}
    entity_ids: set[str] = set()
    pooled: set[int] = set()
    for (record_type, record_id), occs in by_id.items():
        if record_type not in reps:
            continue
        shards = [o for o in occs if o.kind == "shard"]
        if record_type == "entity" and shards:
            entity_ids.add(record_id)
        if any(o.kind == "file" for o in occs):
            continue  # the file wins; its pool entry comes from the per-record walk
        usable = [o for o in shards if o.readable and not o.in_conflict]
        if not usable:
            if shards and record_type in held:
                first = shards[0].raw
                held[record_type][record_id] = first if isinstance(first, dict) else {}
            continue
        chosen = eff[(record_type, record_id)]
        if chosen.source is not None and chosen.source.kind == "archive":
            continue  # the archive pool already holds the effective body
        rep = chosen.source if chosen.source is not None else usable[0]
        reps[record_type][record_id] = (rep.raw, _where(rep))
        pooled.add(id(rep))
    # The archive walk pools the first PARSED line of an id (a line that failed its model too),
    # so the occurrence it carries is the first one with an identity, readable or not.
    archive_carried: set[int] = set()
    archive_seen: set[_Identity] = set()
    for occ in inv.occurrences:
        if occ.kind == "archive" and occ.archive_type_known and occ.record_id is not None:
            identity = (str(occ.record_type), occ.record_id)
            if identity not in archive_seen:
                archive_seen.add(identity)
                archive_carried.add(id(occ))
    return _Packed(inv, eff, by_id, reps, held, entity_ids, pooled, archive_carried, unreadable)


def _shard_line_findings(packed: _Packed) -> list[Violation]:
    """``bad-shard-line`` for a line that is not a readable record (envelope, JSON, model or
    body-id failure: the test ``parse-error`` applies to a file), ``shard-conflict`` for a line
    inside a git conflict region."""
    out: list[Violation] = [
        _violation(BAD_SHARD_LINE, rel, f"unreadable: {exc}")
        for rel, exc in packed.unreadable
        if rel.startswith(f"{SHARDS_SUBDIR}/")
    ]  # an unreadable record file or archive segment is the per-record walk's finding
    for occ in packed.inv.occurrences:
        if occ.kind != "shard":
            continue
        if occ.in_conflict:
            out.append(_violation(SHARD_CONFLICT, _where(occ), "line inside a git conflict region"))
        elif occ.error is not None:
            out.append(_violation(BAD_SHARD_LINE, _where(occ), occ.error))
    return out


def _distinct_shards(occs: list[Occurrence]) -> list[Occurrence]:
    """Readable shard lines of one identity, one per differing parsed body."""
    distinct: list[Occurrence] = []
    for occ in occs:
        readable = occ.kind == "shard" and occ.readable and not occ.in_conflict
        if readable and not any(_same_body(occ, seen) for seen in distinct):
            distinct.append(occ)
    return distinct


def _duplicate_packed_findings(packed: _Packed) -> list[Violation]:
    out: list[Violation] = []
    for (record_type, record_id), occs in packed.by_id.items():
        distinct = _distinct_shards(occs)
        if len(distinct) > 1:
            where = ", ".join(str(_where(o)) for o in distinct)
            out.append(
                _violation(
                    DUPLICATE_PACKED_RECORD,
                    _where(distinct[0]),
                    f"{record_type} {record_id!r} has differing shard lines: {where}",
                )
            )
    return out


def _shadow_findings(store_dir: Path, packed: _Packed) -> list[Violation]:
    """A per-record file whose record is not a legal transition of its shard copy
    (``classify_transition``). Bindings are machine-managed lists and exempt, as they are from
    transition checks."""
    out: list[Violation] = []
    for (record_type, _record_id), occs in packed.by_id.items():
        if record_type == "bindings":
            continue
        files = [o for o in occs if o.kind == "file" and o.readable and not o.in_conflict]
        distinct = _distinct_shards(occs)
        if not files or len(distinct) != 1:
            continue
        old, new = distinct[0].raw, files[0].raw
        if not isinstance(old, dict) or not isinstance(new, dict):
            continue
        problems = classify_transition(kind_of(record_type), old, new)
        if problems:
            detail = "; ".join(sorted({p.code for p in problems}))
            out.append(
                _violation(
                    SHADOW_DIVERGENCE,
                    store_dir / files[0].path,
                    f"not a legal successor of the shard copy at {_where(distinct[0])}: {detail}",
                )
            )
    return out


def _entries(body: object) -> list[dict]:
    return [e for e in body if isinstance(e, dict)] if isinstance(body, list) else []


def _entity_of(entry: dict) -> str | None:
    value = entry.get("entity_id")
    return value if isinstance(value, str) else None


def _binding_entry_findings(store_dir: Path, packed: _Packed) -> list[Violation]:
    """A bindings file that lacks an entity its owner's shard line has. Judged on the tree
    itself, so it fires on a merge result whatever the merge base was. Entries of a derived
    entity (``is_derived_entity_body``) are excluded; an entity that does not resolve is not
    derived.
    # see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§7.1)"""
    eff = packed.eff

    def derived(entity_id: str) -> bool:
        found = eff.get(("entity", entity_id))
        return found is not None and is_derived_entity_body(found.raw)

    out: list[Violation] = []
    for (record_type, _record_id), occs in packed.by_id.items():
        if record_type != "bindings":
            continue
        files = [o for o in occs if o.kind == "file" and o.readable and not o.in_conflict]
        distinct = _distinct_shards(occs)
        if not files or not distinct:
            continue
        have = {_entity_of(e) for e in _entries(files[0].raw)}
        # the union over the owner's differing lines: a dispute does not hide a dropped entry
        packed_at: dict[str, str] = {}
        for line in reversed(distinct):
            for entry in _entries(line.raw):
                entity_id = _entity_of(entry)
                if entity_id is not None:
                    packed_at[entity_id] = _where(line)
        for entity_id in sorted(set(packed_at) - have):
            if derived(entity_id):
                continue
            out.append(
                _violation(
                    BINDING_ENTRY_DROPPED,
                    store_dir / files[0].path,
                    f"lacks entity {entity_id!r} that the shard line at "
                    f"{packed_at[entity_id]} binds: add the anchor back",
                )
            )
    return out


def _check_shard_name_case(listing: ShardListing | None) -> list[Violation]:
    """Two files of ``shards/`` whose names are equal lower-cased, or a ``*.shard`` file whose
    lower-cased name is a bucket name while the name is not: git writes both into one file on
    a case-insensitive filesystem, and the other's records go missing."""
    if listing is None:
        return []
    return [
        _violation(
            SHARD_NAME_CASE,
            f"{SHARDS_SUBDIR}/{name}",
            "shard file name differs only in case from a bucket name or another shard file; "
            "a case-insensitive checkout merges them: rename or remove one",
        )
        for name in listing.case_collisions
    ]


def _own_parent_cycle(own_id: str, parent: str, bodies: Mapping[str, dict]) -> list[str]:
    """The cycle the parent chain of one occurrence of domain ``own_id`` closes: the chain starts
    at ``parent``, follows the pooled bodies, and loops when it reaches ``own_id`` again (the
    occurrence stands for that id's own edge). Empty when it does not loop through ``own_id``."""
    path = [own_id]
    current: object = parent
    while isinstance(current, str):
        if current == own_id:
            first = min(path)
            at = path.index(first)
            return path[at:] + path[:at]
        if current in path or current not in bodies:
            return []
        path.append(current)
        current = bodies[current].get("parent_id")
    return []


def _unpooled_reference_findings(
    store_dir: Path,
    occurrences: list[Occurrence],
    *,
    exist: dict[str, set[str]],
    successors: dict[str, set[str]],
    entities: set[str],
    domain_bodies: Mapping[str, dict],
) -> list[Violation]:
    """The per-record reference checks of the walks above on every readable occurrence the pools
    do not carry (:meth:`_Packed.uncovered`), each reported where it lies: a shard line at
    ``shards/NN.shard:<line>``, an archive line at its segment. ``exist`` holds the ids of each
    record type that resolve, ``successors`` the ids some record's ``supersedes`` names."""
    out: list[Violation] = []
    for occ in occurrences:
        record_type = str(occ.record_type)
        where = _where(occ) if occ.kind == "shard" else str(store_dir / occ.path)
        body = occ.raw
        if record_type == "bindings":
            for entry in _entries(body):
                entity_id = _entity_of(entry)
                if entity_id not in entities:
                    out.append(
                        _violation(
                            DANGLING_BINDING_ENTITY,
                            where,
                            f"binding references unknown entity {entity_id!r}",
                        )
                    )
            continue
        if not isinstance(body, dict) or record_type not in exist:
            continue
        target = body.get("supersedes")
        if isinstance(target, str) and target and target not in exist[record_type]:
            out.append(
                _violation(DANGLING_SUPERSEDES, where, f"supersedes unknown record {target!r}")
            )
        if body.get("status") == _SUPERSEDED and occ.record_id not in successors[record_type]:
            out.append(
                _violation(
                    SUPERSEDED_WITHOUT_SUCCESSOR,
                    where,
                    f"status is superseded but no record's supersedes references {occ.record_id!r}",
                )
            )
        parent = body.get("parent_id")
        if record_type == "domain" and isinstance(parent, str) and parent not in exist["domain"]:
            out.append(
                _violation(
                    DANGLING_PARENT, where, f"parent_id references unknown domain {parent!r}"
                )
            )
        if record_type == "domain" and isinstance(parent, str):
            cycle = _own_parent_cycle(str(occ.record_id), parent, domain_bodies)
            if cycle:
                out.append(
                    _violation(
                        PARENT_CYCLE,
                        where,
                        "parent_id chain loops back on itself: " + _cycle_text(cycle),
                    )
                )
        supports = body.get("supports")
        if record_type == "fact" and isinstance(supports, list):
            out.extend(
                _violation(DANGLING_FACT_SUPPORT, where, f"supports unknown decision {did!r}")
                for did in supports
                if isinstance(did, str) and did not in exist["decision"]
            )
    return out


def _packed_archive_overlap(
    kind: str,
    packed: _Packed,
    files_by_id: Mapping[str, Sequence[Path | str]],
    file_raw: dict[str, dict],
    archive_by_id: dict[str, list[tuple[Path, dict]]],
) -> tuple[dict[str, list[Path | str]], dict[str, list[tuple[Path, dict]]]]:
    """The hot and archive location maps :func:`_check_duplicate_ulids` should judge on a
    packed store. A per-record file beside an archive line is legal when the file holds the
    line's state or a legal successor of it (``compatible``); a shard line beside an archive
    line when it is ``compatible`` or the line is ``superseding`` it. A legal archive copy is
    dropped from the map; an illegal one beside a shard line gets that line added as a hot
    location, so the overlap is reported. Differing archive lines of one identity are left
    alone: they are ``duplicate-ulid`` as before. A file and a shard line of one identity are
    the normal pre-maintenance pair (``shadow-divergence`` judges them) and never count.
    # see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§7.1)"""
    hot = {rid: list(paths) for rid, paths in files_by_id.items()}
    archive = dict(archive_by_id)
    for rid, entries in archive_by_id.items():
        payloads = [payload for _, payload in entries]
        if any(not _strict_equal(payload, payloads[0]) for payload in payloads):
            continue
        a = payloads[0]
        if hot.get(rid):
            raw = file_raw.get(rid)
            if len(hot[rid]) == 1 and raw is not None and compatible(kind, a, raw):
                del archive[rid]
            continue
        distinct = _distinct_shards(packed.by_id.get((kind, rid), []))
        if len(distinct) != 1 or not isinstance(distinct[0].raw, dict):
            continue
        s = distinct[0].raw
        if compatible(kind, a, s) or superseding(kind, a, s):
            del archive[rid]
        else:
            hot[rid] = [_where(distinct[0])]
    return hot, archive


# -- entry point ------------------------------------------------------------------------


def _require_store_dir(store_dir: Path) -> None:
    """Raise unless ``store_dir`` is a directory the format marker can be looked up in.

    Python 3.13's ``Path.is_dir``/``is_file``/``is_symlink`` re-raise EACCES while 3.14's return
    False, so a store directory (or a parent) without the search bit made 3.14 report
    ``store directory not found`` or, worse, ``unknown-schema-version`` / "format marker
    missing" for what is a permission problem. This probes with ``os.stat`` and ``os.lstat``
    instead, which raise ``PermissionError`` on every Python, so the CLI's ``store not
    readable`` path catches it before any predicate runs. The probe is the search bit (the one
    the predicates need), not the read bit: a directory that is searchable but not listable is
    still checked normally. Only a path that is missing or not a directory is the caller's
    ``NotADirectoryError``; a missing marker is left to :func:`_check_format_marker`."""
    try:
        is_dir = stat.S_ISDIR(os.stat(store_dir).st_mode)
    except (FileNotFoundError, NotADirectoryError):
        is_dir = False
    if not is_dir:
        raise NotADirectoryError(f"store directory not found: {store_dir}")
    try:
        os.lstat(store_dir / _FORMAT_MARKER_NAME)
    except FileNotFoundError:
        pass
    except PermissionError as e:
        # The marker's own mode is fine; the directory's search bit is what is missing, and
        # the message must name the thing a user has to chmod.
        raise PermissionError(e.errno, e.strerror, str(store_dir)) from e


def verify_snapshot(store_dir: str | Path) -> list[Violation]:
    """Lint every canonical file under ``store_dir`` against the invariants ``store.py``'s
    write API enforces at write time (see this module's docstring for why this reads the
    files directly rather than opening a ``Store``). Returns ``[]`` for a clean store.

    Raises ``NotADirectoryError`` if ``store_dir`` does not exist or is not a directory, and
    ``PermissionError`` if it, or a record directory in it, cannot be searched or listed —
    the CLI (``verify_main``) treats those as the operational-error exit path (1), distinct
    from a violation (exit 2): an unreadable store is not itself an integrity finding
    about the store's contents.
    """
    store_dir = Path(store_dir)
    _require_store_dir(store_dir)

    violations: list[Violation] = []
    violations += _check_format_marker(store_dir)
    # ``Store()`` refuses these at open, so the CI gate must report them here: this layer
    # never builds a ``Store`` and would otherwise read straight through the link.
    violations += [
        _violation(
            SYMLINKED_STORE_ENTRY,
            store_dir / name,
            "store-owned entry is a symlink; the store refuses to use one",
        )
        for name in symlinked_internals(store_dir)
    ]
    if any(v.code == SYMLINKED_STORE_ENTRY for v in violations):
        # A store ``Store()`` refuses to open has no records worth linting: walking through the
        # link lints foreign files (or the same files as the wrong type), and a pool that is
        # cut off makes every cross-reference into it dangle. Stop at the links.
        return violations
    violations += _check_shards_without_format2(store_dir)
    violations += _check_domain_snapshot(store_dir)
    require_listable_record_dirs(store_dir)

    decisions_raw, decision_paths, decision_all_paths, decisions_skipped, v = _check_temporal_dir(
        store_dir / "decisions", Decision
    )
    violations += v
    facts_raw, fact_paths, fact_all_paths, facts_skipped, v = _check_temporal_dir(
        store_dir / "facts", Fact
    )
    violations += v
    domains_raw, domain_paths, domain_all_paths, domains_skipped, v = _check_plain_dir(
        store_dir / "domains", Domain, "domain_id"
    )
    violations += v
    entities_raw, _entity_paths, entity_all_paths, entities_skipped, v = _check_plain_dir(
        store_dir / "entities", Entity, "entity_id"
    )
    violations += v
    _initiatives_raw, _initiative_paths, initiative_all_paths, _initiatives_skipped, v = (
        _check_plain_dir(store_dir / "initiatives", Initiative, "id")
    )
    violations += v
    bindings_by_record, binding_paths, v = _check_bindings_dir(store_dir / "bindings")
    violations += v
    archive = _check_archive_dir(store_dir / "archive")
    violations += archive.violations

    # A format-2 store: shard lines join the reference pools below as the records that have no
    # per-record file; on any other store ``packed`` is None and every pool is as it was.
    packed = _packed_inventory(store_dir)
    file_ids = {
        "decision": set(decisions_raw),
        "fact": set(facts_raw),
        "domain": set(domains_raw),
        "bindings": set(bindings_by_record),
    }
    fact_paths_all: dict[str, Path | str] = {**fact_paths}
    binding_paths_all: dict[str, Path | str] = {**binding_paths}
    decision_paths_all: dict[str, Path | str] = {}
    domain_paths_all: dict[str, Path | str] = {}
    if packed is not None:
        # an identity known only from an unreadable shard line still resolves a reference to it
        decisions_skipped = {**packed.held["decision"], **decisions_skipped}
        facts_skipped = {**packed.held["fact"], **facts_skipped}
        domains_skipped = {**packed.held["domain"], **domains_skipped}
        facts_raw = {**packed.bodies("fact"), **facts_raw}
        fact_paths_all = {**packed.paths("fact"), **fact_paths_all}
        bindings_by_record = {
            **{
                rid: _entries(body)
                for rid, (body, _) in packed.reps["bindings"].items()
                if isinstance(body, list)
            },
            **bindings_by_record,
        }
        binding_paths_all = {**packed.paths("bindings"), **binding_paths_all}
        decision_paths_all = {**packed.paths("decision")}
        domain_paths_all = {**packed.paths("domain")}

    entity_ids = set(entities_raw) | set(entities_skipped)
    if packed is not None:
        entity_ids |= packed.entity_ids

    # Decisions/domains: a superseded record's successor (or the dangling-supersedes target
    # itself) may live only in the archive, so the cross-reference checks run against the
    # union of hot + archived payloads; hot wins on an id present in both (mirrors
    # ``store._reload_index_from_canonical``'s own hot-always-wins precedent) -- the
    # id itself is ALSO reported separately as ``duplicate-ulid`` below.
    shard_decisions = packed.bodies("decision") if packed is not None else {}
    shard_domains = packed.bodies("domain") if packed is not None else {}
    combined_decisions = {**archive.decisions, **shard_decisions, **decisions_raw}
    decision_path_by_id: Mapping[str, Path | str] = {
        **{did: entries[0][0] for did, entries in archive.decision_entries.items()},
        **decision_paths_all,
        **decision_paths,
    }
    links = packed.successor_links() if packed is not None else {}
    violations += _check_supersedes(
        combined_decisions, decision_path_by_id, decisions_skipped, links.get("decision", set())
    )
    violations += _check_drift_checks(store_dir, set(combined_decisions) | set(decisions_skipped))

    # Facts are never archived (see docs/reference/store-format.md) -- hot only.
    violations += _check_supersedes(
        facts_raw, fact_paths_all, facts_skipped, links.get("fact", set())
    )

    combined_domains = {**archive.domains, **shard_domains, **domains_raw}
    domain_path_by_id: Mapping[str, Path | str] = {
        **{dmid: entries[0][0] for dmid, entries in archive.domain_entries.items()},
        **domain_paths_all,
        **domain_paths,
    }
    violations += _check_supersedes(
        combined_domains, domain_path_by_id, domains_skipped, links.get("domain", set())
    )
    violations += _check_domain_parents(combined_domains, domain_path_by_id, domains_skipped)

    violations += _check_binding_entities(bindings_by_record, binding_paths_all, entity_ids)
    violations += _check_fact_supports(
        facts_raw, fact_paths_all, set(combined_decisions) | set(decisions_skipped)
    )

    # duplicate-ulid: decisions/domains compare hot against BOTH archive pools (the only two
    # archived record types); facts/entities/initiatives are never archived, so only hot-hot
    # duplication is possible for them.
    decision_hot: Mapping[str, Sequence[Path | str]]
    domain_hot: Mapping[str, Sequence[Path | str]]
    if packed is not None:
        # A file or shard line beside a compatible archive line is the state a packed store
        # legitimately holds; the relaxation is format 2 only.
        decision_hot, decision_archive = _packed_archive_overlap(
            "decision", packed, decision_all_paths, decisions_raw, archive.decision_entries
        )
        domain_hot, domain_archive = _packed_archive_overlap(
            "domain", packed, domain_all_paths, domains_raw, archive.domain_entries
        )
    else:
        decision_hot, decision_archive = decision_all_paths, archive.decision_entries
        domain_hot, domain_archive = domain_all_paths, archive.domain_entries
    strict = packed is not None
    violations += _check_duplicate_ulids(decision_hot, decision_archive, strict=strict)
    violations += _check_duplicate_ulids(domain_hot, domain_archive, strict=strict)
    violations += _check_duplicate_ulids(fact_all_paths, {})
    violations += _check_duplicate_ulids(entity_all_paths, {})
    violations += _check_duplicate_ulids(initiative_all_paths, {})

    if packed is not None:
        violations += _shard_line_findings(packed)
        violations += _duplicate_packed_findings(packed)
        violations += _shadow_findings(store_dir, packed)
        violations += _binding_entry_findings(store_dir, packed)
        violations += _check_shard_name_case(packed.inv.listing)
        pools = {
            "decision": (combined_decisions, decisions_skipped),
            "fact": (facts_raw, facts_skipped),
            "domain": (combined_domains, domains_skipped),
        }
        successors = {
            t: links[t]
            | {
                body["supersedes"]
                for body in (*pool.values(), *skipped.values())
                if isinstance(body.get("supersedes"), str) and body["supersedes"]
            }
            for t, (pool, skipped) in pools.items()
        }  # the one set the pooled checks use, too
        violations += _unpooled_reference_findings(
            store_dir,
            packed.uncovered(file_ids),
            exist={
                "decision": set(combined_decisions) | set(decisions_skipped),
                "fact": set(facts_raw) | set(facts_skipped),
                "domain": set(combined_domains) | set(domains_skipped),
            },
            successors=successors,
            entities=entity_ids,
            domain_bodies=combined_domains,
        )

    return violations


def _classify_archive_transfer(kind: str, old: dict, archived: dict) -> list[Violation]:
    """Check a hot record's relocation without losing raw fields or legal lifecycle moves."""
    path = _synthetic_path(kind, old)
    if _archive_payload_problem(kind, archived) or _archive_compaction_problem(kind, archived):
        return [
            _violation(ILLEGAL_DELETION, path, "archive copy is not a valid compactable record")
        ]

    findings = classify_transition(kind, old, archived)
    mutable = DECISION_MUTABLE_FIELDS if kind == "decision" else DOMAIN_MUTABLE_FIELDS
    for field in sorted(old):
        if field in mutable or not same_modulo_absent(old[field], archived.get(field)):
            continue  # actual changes already follow classify_transition's lifecycle rules
        # The general classifier tolerates absent/default keys symmetrically. Compaction's
        # destructive removal requires every baseline key to survive, even a nested empty
        # key which a model dump could otherwise discard. Older absent defaults still expand.
        archive_field = {field: archived[field]} if field in archived else {}
        if not hot_matches_archive({field: old[field]}, archive_field):
            findings.append(
                _violation(ILLEGAL_FIELD_CHANGE, path, f"field {field!r} lost raw keys")
            )
    return findings


class _UnparsableContent(Exception):
    """One side of a changed file's diff failed to parse as a JSON object. Caught by
    :func:`verify_against` to skip just that one file's transition check — the ALWAYS-ON
    snapshot layer already reports a corrupt hot file as :data:`PARSE_ERROR`; the
    transition layer piling on a second, less specific finding for the same root cause
    would be noise, and one unparsable file must never abort the whole ``--against`` pass."""


@overload
def _run_git(
    args: list[str], cwd: Path, *, timeout: float | None = None, text: Literal[True] = True
) -> subprocess.CompletedProcess[str]: ...


@overload
def _run_git(
    args: list[str], cwd: Path, *, timeout: float | None = None, text: Literal[False]
) -> subprocess.CompletedProcess[bytes]: ...


def _run_git(
    args: list[str], cwd: Path, *, timeout: float | None = None, text: bool = True
) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]:
    """``timeout`` (drift→supersede D1) bounds the subprocess for hook-path callers; the
    default ``None`` is byte-for-byte today's behavior for every existing call site. A
    ``TimeoutExpired`` maps onto the same ``ValueError`` contract as ``OSError`` — every
    caller already treats that as "git unavailable".

    ``text=False`` preserves raw pathname bytes for NUL-framed diff output; existing
    callers keep decoded text by default.

    Git runs with its repository-local variables (``GIT_DIR`` and friends) dropped, so it
    answers about the repository containing ``cwd``, never an ambient one. These
    noninteractive commands receive EOF on stdin, keeping subprocesses off the MCP pipe."""
    try:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=text,
            encoding="utf-8" if text else None,
            errors="surrogateescape" if text else None,  # lossless for paths (os.fsencode)
            timeout=timeout,
            env=git_env(),
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ValueError(f"could not run git ({' '.join(args)}): {e}") from e


def _find_repo_root(store_dir: Path) -> Path:
    """The git repository that PHYSICALLY contains ``store_dir`` — may be an ancestor of it,
    not ``store_dir`` itself. Raises ``ValueError`` (the CLI's operational-error exit path, 1 —
    never a violation, design ruling 2: "Not-a-git-repo ... -> operational error") when
    ``store_dir`` isn't inside any git working tree.

    Physical on purpose: ``git`` resolves a symlinked store through the link, and the store's
    own files are committed in the repository they sit in, so ``verify_against`` (and
    ``doctor --against``) diff that repository's history. The repository of the project that
    HOLDS a symlinked store is :func:`find_store_project_repo`'s answer, not this one's.
    # see design/superpowers/specs/2026-10-02-store-git-root-symlink-design.md (D5)"""
    result = _run_git(["rev-parse", "--show-toplevel"], cwd=store_dir)
    if result.returncode != 0:
        raise ValueError(f"{store_dir} is not inside a git repository: {result.stderr.strip()}")
    return Path(result.stdout.strip()).resolve()


def find_store_project_repo(store_dir: Path, *, timeout: float | None = None) -> Path | None:
    """The git repository of the project a store belongs to, or ``None``. Never raises.

    The project is the store path's logical parent (``config._store_project_root``, lexical,
    never ``resolve()``d), so a symlinked ``.sidegraph`` belongs to the project that holds the
    link, not to the repository the link points into: the anchored code lives in the project.
    Only when that parent is inside no repository does it retry from ``store_dir`` itself, which
    keeps a store directory that is its own repository working. ``--show-toplevel`` succeeds in
    a repository with no commits, so an empty project is still chosen over a link target's
    repository. Git runs through ``_run_git`` (``git_env()``, repository-local variables
    dropped). ``timeout`` is ONE budget in seconds for both attempts; a failure to run git, or
    a spent budget, gives ``None`` without a retry.
    # see design/superpowers/specs/2026-10-02-store-git-root-symlink-design.md (D1)"""
    deadline = None if timeout is None else time.monotonic() + timeout
    for cwd in (_store_project_root(store_dir), Path(store_dir)):
        remaining = None if deadline is None else deadline - time.monotonic()
        if remaining is not None and remaining <= 0:
            return None
        try:
            result = _run_git(["rev-parse", "--show-toplevel"], cwd=cwd, timeout=remaining)
            if result.returncode == 0:
                return Path(result.stdout.strip()).resolve()
        except (ValueError, OSError):
            return None
    return None


def _resolve_commit_oid(repo_root: Path, ref: str) -> str:
    """Resolve one baseline commit without interpreting user input as options."""
    if not isinstance(ref, str) or not ref or ref.startswith("-") or "\0" in ref:
        raise ValueError("Invalid Git baseline revision.")
    result = _run_git(
        ["rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"],
        cwd=repo_root,
    )
    if result.returncode != 0:
        raise ValueError("Could not resolve Git baseline to one commit.")
    match = re.fullmatch(r"([0-9a-f]{40}|[0-9a-f]{64})\n?", result.stdout)
    if match is None:
        raise ValueError("Git returned an invalid baseline commit ID.")
    return match.group(1)


def _parse_git_name_status_z(stdout: bytes) -> list[tuple[str, str]]:
    """Validate the whole no-rename diff stream before exposing filesystem paths."""
    error = "Git returned malformed or unsupported NUL-framed diff output."
    if not isinstance(stdout, bytes):
        raise ValueError(error)
    if not stdout:
        return []
    if not stdout.endswith(b"\0"):
        raise ValueError(error)
    fields = stdout[:-1].split(b"\0")
    if len(fields) % 2:
        raise ValueError(error)
    changed: list[tuple[str, str]] = []
    for index in range(0, len(fields), 2):
        status, path = fields[index : index + 2]
        if status not in {b"A", b"D", b"M", b"T", b"U"} or not path:
            raise ValueError(error)
        changed.append((status.decode("ascii"), os.fsdecode(path)))
    return changed


def _git_diff_name_status(repo_root: Path, ref: str, store_dir: Path) -> list[tuple[str, str]]:
    """``git diff --no-renames --name-status -z <ref> -- <store_dir>``, run from ``repo_root``
    (design ruling 2: "git plumbing ... ``git diff --name-status <ref> -- <store-dir>``").

    ``--no-renames`` is deliberate: a renamed-but-identical record file would otherwise
    report as one ``R100`` line instead of a delete+add pair — but the store's own
    invariant is that a file is always named after its own id (:data:`FILENAME_ID_MISMATCH`),
    so a genuine rename is never legitimate, and surfacing it as a deletion (illegal, absent
    from any archive) alongside an unrelated addition is the MORE informative shape, not a
    loss of information.

    ``ref`` is the validated full commit OID resolved once by ``verify_against``.
    Compares that frozen baseline against the CURRENT WORKING TREE (git's default for a single-ref
    ``diff`` — no second ref, no ``--cached``), matching the CI use case this exists for:
    ``origin/main`` vs. whatever the PR branch has checked out right now. Untracked files
    never appear in ``git diff`` output at all (git diff semantics, not a bug here) — a
    normal CI checkout has none.
    """
    result = _run_git(
        ["diff", "--no-renames", "--name-status", "-z", ref, "--", str(store_dir)],
        cwd=repo_root,
        text=False,
    )
    if result.returncode != 0:
        raise ValueError(f"git diff against {ref!r} failed: {os.fsdecode(result.stderr).strip()}")
    return _parse_git_name_status_z(result.stdout)


def _git_show_json(repo_root: Path, ref: str, repo_path: str) -> dict:
    """Read a record from the same validated full commit OID used by the diff."""
    result = _run_git(["show", f"{ref}:{repo_path}"], cwd=repo_root, text=False)
    if result.returncode != 0:
        err = result.stderr.decode("utf-8", "replace").strip()
        raise ValueError(f"git show {ref}:{repo_path} failed: {err}")
    try:
        # strict: the store writes UTF-8, and mojibake must not be compared as a record
        obj = json.loads(result.stdout.decode("utf-8"))
    except (ValueError, RecursionError) as e:
        raise _UnparsableContent(str(e)) from e
    if not isinstance(obj, dict):
        raise _UnparsableContent(f"{repo_path}@{ref} is not a JSON object")
    return obj


def _read_json_object(path: Path) -> dict:
    try:
        # A FIFO (or a symlink to one) put in a tracked file's place would block the read; the
        # snapshot layer already reports the entry.
        if not is_regular_file(stat_entry(path)):
            raise _UnparsableContent(NOT_A_FILE_REASON)
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError) as e:
        raise _UnparsableContent(str(e)) from e
    if not isinstance(obj, dict):
        raise _UnparsableContent(f"{path} is not a JSON object")
    return obj


_DIR_TO_KIND = {
    "decisions": "decision",
    "facts": "fact",
    "domains": "domain",
    "entities": "entity",
    "bindings": "binding",
    "initiatives": "initiative",
    VERIFICATIONS_SUBDIR: "verification",
}


def _classify_store_relative_path(store_relative: str) -> str | None:
    """``<dir>/<id>.json`` (a path already relative to the store dir) -> a
    :func:`classify_transition` ``kind``, or one of two kinds :func:`verify_against` handles
    directly rather than through :func:`classify_transition` (see its docstring):
    ``"initiative"`` (no append-only guard in store.py at all -- deletion-only check) and
    ``"archive"`` (``archive/*.jsonl`` segments — write-once by design, see
    ``store._write_archive_segment``'s docstring "Segments are write-once ... and this
    always creates a brand-new file" and ``docs/reference/store-format.md``'s "immutable
    segments"; a segment is a multi-record file, not a one-record-per-file canonical shape,
    so it is never diffed field-by-field like a decision/fact/domain/entity — any change to
    an already-published segment's bytes at all is illegal, full stop).

    ``None`` for anything else changed under the store dir this layer doesn't classify: the
    ``format`` marker, the gitignored ``index.db``, or any unexpected nesting.
    ``domain-snapshot.json`` is replaced whole by ``sidegraph-domains snapshot --refresh``
    and validated by the snapshot layer."""
    parts = store_relative.split("/")
    if len(parts) == 2 and parts[0] == "archive" and parts[1].endswith(".jsonl"):
        return "archive"
    if len(parts) != 2 or not parts[1].endswith(".json"):
        return None
    return _DIR_TO_KIND.get(parts[0])


# -- the packed (format 2) transition layer ----------------------------------------------
#
# ``--against REF`` on a store that is, or was, packed: both trees are read as occurrence
# inventories and judged identity by identity, in the dispatch order of the spec (rule 8, the
# path-status rules 6 and 7, provenance for every added shard line, then per identity rule 2
# unchanged, rule 1 file-only bindings, rule 3 edit, rule 5 sanctions, rule 4 relocation). A
# store that is format 1 on both sides keeps the path-status walk of :func:`verify_against`
# unchanged.
# see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§7.1)

_Shaped = dict | list


def _loc(prefix: str, occ: Occurrence) -> str:
    where = f"{prefix}{occ.path}"
    return f"{where}:{occ.line_no}" if occ.line_no else where


def _readable(occ: Occurrence) -> bool:
    return occ.readable and not occ.in_conflict


def _body_of(occ: Occurrence) -> _Shaped | None:
    """The parsed body of an occurrence, readable or not (a model-invalid but parseable body is
    a raw body for the transition rules), when it has the shape of its record type."""
    if not occ.parseable or occ.record_type is None:
        return None
    return occ.raw if _right_shape(occ.record_type, occ.raw) else None  # type: ignore[return-value]


def _dict_body(occ: Occurrence) -> dict | None:
    body = _body_of(occ)
    return body if isinstance(body, dict) else None


class _Against:
    """One ``--against`` pass over two inventories (the work is in the methods; ``run`` is the
    dispatch order)."""

    def __init__(self, prefix: str, ref_inv: Inventory, new_inv: Inventory) -> None:
        self.prefix = prefix
        self.ref_inv, self.new_inv = ref_inv, new_inv
        self.ref_eff = effective_all(ref_inv)
        self.new_eff = effective_all(new_inv)
        self.diff = diff_occurrences(ref_inv, new_inv)
        self.out: list[Violation] = []
        # REF occurrences whose removal a rule 5 sanction covers (a deleted segment's line)
        self.covered: set[int] = set()
        self._ref_ids = {id(o) for o in ref_inv.occurrences}

    # -- helpers ---------------------------------------------------------------------------

    def loc(self, occ: Occurrence) -> str:
        """Where an occurrence is: ``<store>/<path>:<line>`` for the new tree; a baseline
        occurrence has the path only, as REF's line number means nothing in the work tree."""
        if id(occ) in self._ref_ids:
            return f"{self.prefix}{occ.path}"
        return _loc(self.prefix, occ)

    def flag(self, code: str, where: str, detail: str) -> None:
        self.out.append(Violation(code, where, detail))

    def _derived(self, entity_id: str) -> bool:
        return any(
            (found := eff.get(("entity", entity_id))) is not None
            and is_derived_entity_body(found.raw)
            for eff in (self.ref_eff, self.new_eff)
        )

    def _all_derived(self, occs: list[Occurrence]) -> bool:
        for occ in occs:
            body = _body_of(occ)
            if not isinstance(body, list):
                return False
            if any(not (isinstance(e, dict) and _derived_entry_id(e, self._derived)) for e in body):
                return False
        return True

    def _survives(self, record_type: str, x: _Shaped, d: IdentityDiff, y: object) -> bool:
        """``keeps_keys(x, y)``, or ``keeps_keys(x, S)`` for some shard occurrence S of the
        identity in the new tree, added or unchanged (a verbatim survivor keeps the keys)."""
        bind = record_type == "bindings"
        kind = kind_of(record_type)

        def kept(target: object) -> bool:
            # a legacy default the target lacks is no lost key (store_rules.deletable agrees)
            body = without_legacy_defaults(kind, x, target)
            return keeps_keys(body, target, bindings=bind, is_derived=self._derived)

        if kept(y):
            return True
        for occ in d.new:
            body = _body_of(occ)
            if occ.kind == "shard" and body is not None and kept(body):
                return True
        return False

    def _shadow_pair(self, d: IdentityDiff) -> tuple[Occurrence, Occurrence] | None:
        """(file, shard line) of a REF ``shadow-divergence`` of this identity, else ``None``."""
        record_type = d.identity[0]
        if record_type == "bindings":
            return None
        files = [o for o in d.ref if o.kind == "file" and _readable(o) and _dict_body(o)]
        shards = _distinct_shards(d.ref)
        if len(files) != 1 or len(shards) != 1 or _dict_body(shards[0]) is None:
            return None
        if classify_transition(kind_of(record_type), shards[0].raw, files[0].raw):  # type: ignore[arg-type]
            return files[0], shards[0]
        return None

    def _ref_duplicate_ulid(self, d: IdentityDiff) -> bool:
        """The REF identity is a ``duplicate-ulid`` of a packed store: differing archive lines,
        or an archive line beside a file it is not ``compatible`` with, or beside a shard line
        it is neither ``compatible`` nor ``superseding``."""
        kind = kind_of(d.identity[0])
        lines = [
            o
            for o in d.ref
            if o.kind == "archive" and o.archive_type_known and _readable(o) and _dict_body(o)
        ]
        if not lines:
            return False
        if any(not same_raw_body(lines[0], o) for o in lines[1:]):
            return True
        a = lines[0].raw
        assert isinstance(a, dict)
        files = [o for o in d.ref if o.kind == "file" and _readable(o) and _dict_body(o)]
        if files:
            return not compatible(kind, a, files[0].raw)  # type: ignore[arg-type]
        shards = _distinct_shards(d.ref)
        if len(shards) == 1 and _dict_body(shards[0]) is not None:
            s = shards[0].raw
            assert isinstance(s, dict)
            return not (compatible(kind, a, s) or superseding(kind, a, s))
        return False

    # -- the dispatch ----------------------------------------------------------------------

    def run(self) -> list[Violation]:
        self.provenance()
        for d in self.diff.identities.values():
            self.judge(d)
        kept = {o.path for o in self.new_inv.occurrences if o.kind == "archive"}
        gone = {o.path for o in self.ref_inv.occurrences if o.kind == "archive"} - kept
        for path in sorted(gone):
            self.deleted_segment(path)
        return self.out

    def provenance(self) -> None:
        """Decision 14: every added or modified shard line holds the raw body of some REF
        occurrence of the same identity (a file, a shard line, or an archive line compared
        without ``record_type``), new identities included."""
        for d in self.diff.identities.values():
            for occ in d.added:
                if occ.kind != "shard" or not occ.parseable:
                    continue
                if any(o.parseable and same_raw_body(o, occ) for o in d.ref):
                    continue
                self.flag(
                    SHARD_BODY_WITHOUT_SOURCE,
                    self.loc(occ),
                    f"{d.identity[0]} {d.identity[1]!r}: no occurrence of it in the baseline "
                    "holds this body (maintenance only writes bodies it read)",
                )

    def judge(self, d: IdentityDiff) -> None:
        record_type = d.identity[0]
        kind = kind_of(record_type)
        if not d.changed:
            return  # rule 2
        if d.new and all(o.error in (NOT_A_FILE_REASON, SYMLINK_REASON) for o in d.new):
            return  # only non-regular or unreadable entries: the snapshot layer reports them
        file_only = all(o.kind == "file" for o in d.ref)
        if record_type == "bindings" and not d.new and (file_only or self._all_derived(d.ref)):
            return  # rule 1: a file-only (or all-derived) bindings deletion
        if not d.ref:
            return  # a new identity: provenance was its only rule
        ref_eff = self.ref_eff[d.identity]
        new_eff = self.new_eff.get(d.identity)
        if ref_eff.raw is not None and (new_eff is None or new_eff.raw is None):
            gone = ref_eff.source if ref_eff.source is not None else d.ref[0]
            self.flag(
                ILLEGAL_DELETION,
                self.loc(gone),
                f"{record_type} {d.identity[1]!r}: the effective record of the baseline has no "
                "effective record in the new tree",
            )
            return  # rule 1
        if ref_eff.raw is None:
            if not self._repaired_file_edit(d, kind):
                self._no_effective(d, kind, new_eff)
            return
        if not d.removed and d.added and all(o.kind == "file" for o in d.added):
            self._edit(d, kind, ref_eff.raw)  # rule 3
            return
        if self._sanctioned(d, new_eff):  # rule 5
            return
        self._relocation(d, kind, ref_eff, new_eff)  # rule 4

    # -- rule 3 ----------------------------------------------------------------------------

    def _edit(self, d: IdentityDiff, kind: str, base: object) -> None:
        """Each added or modified per-record file against ``base``, as an edited file is judged
        on format 1; key survival is not checked."""
        if not isinstance(base, dict) and kind != "binding":
            return
        for occ in d.added:
            new = _dict_body(occ)
            if new is None or occ.kind != "file":
                continue
            for v in classify_transition(kind, base if isinstance(base, dict) else None, new):
                self.flag(v.code, self.loc(occ), v.detail)

    def _repaired_file_edit(self, d: IdentityDiff, kind: str) -> bool:
        """A REF identity whose only unreadable occurrence is a conflicted or unparseable
        per-record file, beside readable shard lines of one body, repaired by rewriting the file:
        a rule 3 edit against that shard body. The destroyed file body keeps its keys only when it
        parsed, and only over keys whose value has the shape the shard body gives them.
        # see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§28 Opus 3)"""
        unreadable = [o for o in d.ref if not _readable(o)]
        if len(unreadable) != 1 or unreadable[0].kind != "file":
            return False
        damaged = unreadable[0]
        if not (damaged.in_conflict or not damaged.parseable):
            return False
        if d.removed or not d.added or any(o.kind != "file" for o in d.added):
            return False
        shards = _distinct_shards(d.ref)
        if len(shards) != 1 or _body_of(shards[0]) is None:
            return False
        base = shards[0].raw
        self._edit(d, kind, base)
        old = _dict_body(damaged)
        if old is not None and isinstance(base, dict):
            shaped = {k: v for k, v in old.items() if k in base and type(v) is type(base[k])}
            for occ in d.added:
                new = _dict_body(occ)
                if new is not None and not keeps_keys(
                    without_legacy_defaults(kind, shaped, new), new
                ):
                    self.flag(ILLEGAL_FIELD_CHANGE, self.loc(occ), "repair lost raw keys")
        return True

    # -- rule 5 ----------------------------------------------------------------------------

    def _sanctioned(self, d: IdentityDiff, new_eff: Effective | None) -> bool:
        """The sanctioned removals for an identity that was a ``shadow-divergence`` or a
        ``duplicate-ulid`` in REF (the no-effective resolution is :meth:`_no_effective`)."""
        if new_eff is None or new_eff.raw is None:
            return False
        pair = self._shadow_pair(d)
        if pair is not None:
            file_occ, shard = pair
            if any(o is file_occ for o in d.removed) and _strict_equal(new_eff.raw, shard.raw):
                return True
        if self._ref_duplicate_ulid(d) and len(d.removed) == 1 and not d.modified:
            gone = d.removed[0]
            if any(
                o is not gone and o.parseable and _strict_equal(o.raw, new_eff.raw) for o in d.ref
            ):
                if gone.kind == "archive":
                    self.covered.add(id(gone))
                return True
        return False

    # -- rule 4 ----------------------------------------------------------------------------

    def _relocation(
        self, d: IdentityDiff, kind: str, ref_eff: Effective, new_eff: Effective | None
    ) -> None:
        record_type = d.identity[0]
        new_raw = new_eff.raw if new_eff is not None else None
        where = self.loc(new_eff.source if new_eff and new_eff.source else d.new[0])
        if isinstance(ref_eff.raw, dict) and isinstance(new_raw, dict):  # rule 1
            for v in classify_transition(kind, ref_eff.raw, new_raw):
                self.flag(v.code, where, v.detail)
        for occ in d.removed:
            if occ.kind == "file":
                for v in self._file_removal(d, kind, occ, new_eff):
                    self.flag(v.code, self.loc(occ), v.detail)
        self._added_archive(d, kind, ref_eff, new_eff)
        if new_raw is not None:  # an identity left without a record is the snapshot layer's
            for occ in d.destroyed:
                body = _body_of(occ)
                if body is not None and not self._survives(record_type, body, d, new_raw):
                    self.flag(
                        ILLEGAL_FIELD_CHANGE,
                        self.loc(occ),
                        f"{record_type} {d.identity[1]!r}: the body loses raw keys",
                    )
        if record_type != "bindings" and isinstance(ref_eff.raw, dict):
            for occ in d.added:
                body = _dict_body(occ)
                if occ.kind != "shard" or body is None:
                    continue
                if any(o.kind == "shard" and same_raw_body(o, occ) for o in d.ref):
                    continue  # a verbatim copy between shards is judged against its source
                for v in classify_transition(kind, ref_eff.raw, body):
                    self.flag(v.code, self.loc(occ), v.detail)
        pair = self._shadow_pair(d)
        if pair is not None and isinstance(new_raw, dict):
            shard = pair[1]
            if any(o is shard for o in d.removed):  # the evidence survives its own removal
                for v in classify_transition(kind, shard.raw, new_raw):  # type: ignore[arg-type]
                    self.flag(v.code, where, v.detail)

    def _file_removal(
        self, d: IdentityDiff, kind: str, file_occ: Occurrence, new_eff: Effective | None
    ) -> list[Violation]:
        """A removed per-record file is legal onto an equal shard line (the fold), or, for a
        decision or domain, onto an archive line that passes the transfer check and is the
        effective record (or the only copy left). Otherwise it is an ``illegal-deletion``."""
        body = _dict_body(file_occ)
        for occ in d.new:
            if occ.kind == "shard" and occ.parseable and same_raw_body(file_occ, occ):
                return []
            # an older copy folded onto its backfilled twin loses nothing (legacy defaults)
            if (
                occ.kind == "shard"
                and occ.parseable
                and body is not None
                and same_modulo_legacy_defaults(kind, body, _dict_body(occ))
            ):
                return []
        if kind in ("decision", "domain") and body is not None:
            lines = [
                o for o in d.new if o.kind == "archive" and o.archive_type_known and _dict_body(o)
            ]
            archive_effective = (
                new_eff is not None
                and new_eff.source is not None
                and new_eff.source.kind == "archive"
            )
            if lines and (archive_effective or not any(o.kind == "shard" for o in d.new)):
                found: list[Violation] = []
                for line in lines:
                    found += _classify_archive_transfer(kind, body, line.raw)  # type: ignore[arg-type]
                return found
        return [
            _violation(
                ILLEGAL_DELETION,
                file_occ.path,
                f"{d.identity[0]} {d.identity[1]!r}: file removed and no shard line or archive "
                "copy of the same body takes its place",
            )
        ]

    def _added_archive(
        self, d: IdentityDiff, kind: str, ref_eff: Effective, new_eff: Effective | None
    ) -> None:
        """An added archive line: judged as a transfer when it becomes the effective record
        (the file-removal check judges one that replaces a removed file); beside a per-record
        file that stays it still gets the payload and terminal checks."""
        if kind not in ("decision", "domain") or any(o.kind == "file" for o in d.removed):
            return
        for occ in d.added:
            body = _dict_body(occ)
            if occ.kind != "archive" or not occ.archive_type_known or body is None:
                continue
            if new_eff is not None and new_eff.source is occ and isinstance(ref_eff.raw, dict):
                found = _classify_archive_transfer(kind, ref_eff.raw, body)
                for v in found:
                    self.flag(v.code, self.loc(occ), v.detail)
            elif _archive_payload_problem(kind, body) or _archive_compaction_problem(kind, body):
                self.flag(
                    ILLEGAL_DELETION,
                    self.loc(occ),
                    "archive copy is not a valid compactable record",
                )

    # -- an identity with no effective record in REF ------------------------------------------

    def _no_effective(self, d: IdentityDiff, kind: str, new_eff: Effective | None) -> None:
        """A REF identity with no effective record (a ``duplicate-packed-record``, a conflict
        region, an unreadable copy): its new effective record must be the raw body of one
        readable REF occurrence X, no destroyed parseable occurrence may be a legal successor of
        X that X is not a successor of, and every destroyed readable body keeps its keys.
        # see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§7.1 rule 4)"""
        record_type = d.identity[0]
        readable = [o for o in d.ref if _readable(o) and _body_of(o) is not None]
        destroyed = d.destroyed
        new_raw = new_eff.raw if new_eff is not None else None
        if not readable:
            self._sole_unreadable(d, kind, new_raw, new_eff)
            return
        if not d.new:
            self.flag(
                ILLEGAL_DELETION,
                self.loc(destroyed[0]),
                f"{record_type} {d.identity[1]!r}: no occurrence is left in the new tree",
            )
            return
        if new_raw is None:
            return  # still without a record: the snapshot layer reports it
        where = self.loc(new_eff.source if new_eff and new_eff.source else d.new[0])
        matches = [o for o in readable if _strict_equal(o.raw, new_raw)]
        if not matches and not self._union(d, new_eff, readable):
            self.flag(
                ILLEGAL_FIELD_CHANGE,
                where,
                f"{record_type} {d.identity[1]!r}: the resolution is a body no baseline copy holds",
            )
        if matches and isinstance(matches[0].raw, dict):
            chosen = matches[0].raw
            for occ in d.ref:  # every parseable copy, the retained ones too
                other = _dict_body(occ)
                if other is None or classify_transition(kind, chosen, other):
                    continue
                for v in classify_transition(kind, other, chosen):
                    self.flag(v.code, where, v.detail)
        for occ in destroyed:
            body = _body_of(occ)
            if (
                _readable(occ)
                and body is not None
                and not self._survives(record_type, body, d, new_raw)
            ):
                self.flag(ILLEGAL_FIELD_CHANGE, self.loc(occ), "the resolution loses raw keys")
        self.covered.update(id(o) for o in destroyed if o.kind == "archive")

    def _union(
        self, d: IdentityDiff, new_eff: Effective | None, readable: list[Occurrence]
    ) -> bool:
        """A bindings resolution: a per-record file that keeps the keys of every readable REF
        copy, beside at most one shard line, which is a REF line."""
        if d.identity[0] != "bindings" or new_eff is None or new_eff.source is None:
            return False
        if new_eff.source.kind != "file":
            return False
        shards = [o for o in d.new if o.kind == "shard"]
        if len(shards) > 1 or any(not any(o is s for o in d.unchanged) for s in shards):
            return False
        return all(
            keeps_keys(o.raw, new_eff.raw, bindings=True, is_derived=self._derived)
            for o in readable
        )

    def _sole_unreadable(
        self, d: IdentityDiff, kind: str, new_raw: object, new_eff: Effective | None
    ) -> None:
        """No readable REF occurrence at all. Unparseable content is waived; a model-invalid
        object is judged as a parseable baseline is: ``classify_transition`` and ``keeps_keys``.
        A sole unreadable archive line stays (rule 6 keeps its segment)."""
        record_type = d.identity[0]
        parsed = [o for o in d.destroyed if o.kind != "archive" and _body_of(o) is not None]
        if not d.new:
            # an unreadable copy may be repaired INTO a per-record file; removing it alone is
            # granted only to a shard line with no recoverable identity, which is in no diff
            for occ in [o for o in d.destroyed if o.kind != "archive"]:
                if record_type == "bindings" and self._all_derived([occ]):
                    continue
                self.flag(
                    ILLEGAL_DELETION,
                    self.loc(occ),
                    f"{record_type} {d.identity[1]!r}: removed and no copy is left",
                )
            return
        if new_raw is None:
            return
        where = self.loc(new_eff.source if new_eff and new_eff.source else d.new[0])
        damaged = [o for o in d.destroyed if o.kind != "archive"]
        if damaged and (new_eff is None or new_eff.source is None or new_eff.source.kind != "file"):
            # the exception repairs an unreadable copy INTO a per-record file
            self.flag(
                ILLEGAL_DELETION,
                self.loc(damaged[0]),
                f"{record_type} {d.identity[1]!r}: an unreadable copy is repaired into a "
                "per-record file, not into a shard line or an archive segment",
            )
        for occ in parsed:
            body = _body_of(occ)
            if isinstance(body, dict) and isinstance(new_raw, dict):
                for v in classify_transition(kind, body, new_raw):
                    self.flag(v.code, where, v.detail)
            if body is not None and not self._survives(record_type, body, d, new_raw):
                self.flag(ILLEGAL_FIELD_CHANGE, where, "the repair loses raw keys")

    # -- rule 6: a deleted segment -----------------------------------------------------------

    def deleted_segment(self, path: str) -> None:
        """A segment is deleted only when every line is readable, of a known ``record_type``
        and either ``deletable`` against a shard line of its identity in the new tree or covered
        by a rule 5 sanction."""
        lines = [o for o in self.ref_inv.occurrences if o.kind == "archive" and o.path == path]
        for occ in lines:
            body = _dict_body(occ)
            if not (_readable(occ) and occ.archive_type_known and occ.record_id and body):
                self.flag(  # no sanction covers a line that is not a readable record
                    ILLEGAL_DELETION,
                    self.loc(occ),
                    "archive segment deleted while a line is not a readable record of a known "
                    "type (the segment stays)",
                )
                continue
            if id(occ) in self.covered:
                continue
            identity = (str(occ.record_type), occ.record_id)
            diff = self.diff.identities.get(identity)
            kind = kind_of(identity[0])
            shards = [] if diff is None else [o for o in diff.new if o.kind == "shard"]
            if not any(
                (s_body := _dict_body(s)) is not None and deletable(kind, body, s_body)
                for s in shards
            ):
                self.flag(
                    ILLEGAL_DELETION,
                    self.loc(occ),
                    f"archive segment deleted but {identity[0]} {identity[1]!r} has no shard "
                    "line that is compatible with it and keeps every key",
                )


def _derived_entry_id(entry: dict, is_derived: Callable[[str], bool]) -> bool:
    entity_id = entry.get("entity_id")
    return isinstance(entity_id, str) and is_derived(entity_id)


def _marker_at_ref(repo_root: Path, oid: str, store_rel: str) -> Marker | None:
    """The parsed ``format`` marker of the store in REF's tree, or ``None`` when it is absent or
    does not parse."""
    name = f"{store_rel}/{_FORMAT_MARKER_NAME}" if store_rel else _FORMAT_MARKER_NAME
    result = _run_git(["show", f"{oid}:{name}"], cwd=repo_root, text=False)
    if result.returncode != 0:
        return None
    try:
        return parse_marker(
            result.stdout.decode("utf-8"), Path(name), malformed=lambda _p, d: ValueError(d)
        )
    except ValueError:
        return None


def _marker_on_disk(store_dir: Path) -> Marker | None:
    try:
        return store_marker(store_dir)
    except (StoreRequirementError, ValueError, OSError):
        return None


def _baseline_initiative_loaded(repo_root: Path, ref: str, repo_path: str, inner: str) -> bool:
    """Whether the baseline initiative file counts as a record: anything but a stray the reload
    never loaded (an unsafe name, or an object with no id or an unsafe one), whose deletion is
    the cleanup. A file that does not decode is a damaged record, as for the other kinds."""
    if _check_safe_stem(Path(inner)) is not None:
        return False
    try:
        old = _git_show_json(repo_root, ref, repo_path)
    except _UnparsableContent:
        return True  # a damaged record, not a stray: its loss is judged
    return not isinstance(old, dict) or is_safe_record_id(old.get("id"))


def _never_loaded_file(occ: Occurrence) -> bool:
    """A per-record file of a classified kind whose body decodes to an object holding no id, or
    an unsafe (or non-string) one: the reload skips it whole, as :func:`verify_against` does
    for format 1. A file that does not decode is not one of these: rule 1 quarantines its
    identity, so on a packed store it stands for a damaged record whose loss is judged."""
    if occ.kind != "file" or occ.record_type not in _KIND_ID_FIELDS:
        return False
    if not isinstance(occ.raw, dict):
        return False
    return not is_safe_record_id(occ.raw.get(_KIND_ID_FIELDS[occ.record_type]))


def _packed_against(
    store_dir: Path,
    repo_root: Path,
    store_rel: str,
    baseline_oid: str,
    changed: list[tuple[str, str]],
    ref_marker: Marker | None,
    new_marker: Marker | None,
) -> list[Violation]:
    """The format-2 transition layer: ``verify_against`` for a store that is packed in REF or in
    the work tree."""
    prefix = f"{store_rel}/" if store_rel else ""
    ref_inv = inventory_from_git_tree(
        repo_root, baseline_oid, store_rel, validate=validate_record_body
    )
    violations: list[Violation] = []
    # A committed symlink named like a record is read by git as the link's text, not as a
    # record: it is the snapshot layer's symlinked-store-entry, never a deletion.
    links = [o for o in ref_inv.occurrences if o.error == SYMLINK_REASON]
    if links:
        ref_inv.occurrences = [o for o in ref_inv.occurrences if o.error != SYMLINK_REASON]
        for occ in links:
            if (store_dir / occ.path).is_symlink():
                violations.append(
                    _violation(
                        SYMLINKED_STORE_ENTRY,
                        f"{prefix}{occ.path}",
                        "store-owned entry is a symlink; the store refuses to use one",
                    )
                )
    # A per-record file whose body id is missing or unsafe is never loaded by the reload, so it
    # is no record: deleting or repairing it is no transition (format 1 skips it the same way).
    ref_never_loaded = {o.path for o in ref_inv.occurrences if _never_loaded_file(o)}
    ref_inv.occurrences = [o for o in ref_inv.occurrences if not _never_loaded_file(o)]
    shard_count = (
        new_marker.shards
        if new_marker is not None and new_marker.major == 2
        else ref_marker.shards
        if ref_marker is not None
        else None
    )
    packed_now = (new_marker is not None and new_marker.major == 2) or has_shard_files(store_dir)
    marker_problem: str | None = None
    read = dict(
        validate=validate_record_body,
        assume_major=2 if packed_now else None,
        assume_shards=shard_count,
        on_unreadable=lambda _rel, _exc: None,  # the snapshot layer reports an unreadable file
    )
    try:
        new_inv = inventory_from_disk(store_dir, **read)  # type: ignore[arg-type]
    except MalformedMarker as exc:
        # a marker that does not parse is the snapshot layer's finding; under a major-2 baseline
        # it is also a downgrade (rule 8). The independent checks go on over an inventory read
        # as major 2.
        marker_problem = str(exc)
        read.update(assume_major=2, assume_shards=shard_count or 64)
        new_inv = inventory_from_disk(store_dir, lenient_marker=True, **read)  # type: ignore[arg-type]
    # A work-tree file the reload never loads is dropped too, but only where the baseline held
    # the same kind of never-loaded file at that path: unchanged (or still stray) on both sides,
    # it changes nothing. A new one, or a real record edited into one, is judged.
    new_inv.occurrences = [
        o for o in new_inv.occurrences if not (_never_loaded_file(o) and o.path in ref_never_loaded)
    ]
    engine = _Against(prefix, ref_inv, new_inv)
    # rules 6 and 7: path rules outside the walk (a segment's deletion is judged in the walk)
    for status, repo_path in changed:
        inner = repo_path[len(prefix) :] if prefix and repo_path.startswith(prefix) else repo_path
        kind = _classify_store_relative_path(inner)
        if (
            kind == "initiative"
            and status == "D"
            and _baseline_initiative_loaded(repo_root, baseline_oid, repo_path, inner)
        ):
            violations.append(
                _violation(
                    ILLEGAL_DELETION,
                    repo_path,
                    "initiative deleted (no compaction/archive path exists for initiatives)",
                )
            )
        elif kind == "verification" and status != "A":
            violations.append(
                _violation(
                    ILLEGAL_DELETION if status == "D" else ILLEGAL_FIELD_CHANGE,
                    repo_path,
                    (
                        "verification deleted (write-once; no pruning path exists)"
                        if status == "D"
                        else "write-once verification modified"
                    ),
                )
            )
        elif kind == "archive" and status not in ("A", "D"):
            violations.append(
                _violation(ILLEGAL_FIELD_CHANGE, repo_path, "write-once archive segment modified")
            )
    violations += engine.run()
    # rule 8: a marker that went from major 2 to below it, unless every record is back in a
    # per-record file (the rollback the guide prescribes): no shard file left, every rule passing
    downgraded = ref_marker is not None and ref_marker.major == 2
    downgraded = downgraded and (new_marker is None or new_marker.major < 2)
    if downgraded and (violations or marker_problem or has_shard_files(store_dir)):
        violations.append(
            _violation(
                FORMAT_DOWNGRADE,
                f"{prefix}{_FORMAT_MARKER_NAME}",
                marker_problem
                or "the format marker went from major 2 to below it; only the full rollback "
                "(no shard file left, every record in its per-record file) is legal",
            )
        )
    seen: set[tuple[str, str, str]] = set()
    unique: list[Violation] = []
    for v in violations:
        if (v.code, v.path, v.detail) not in seen:
            seen.add((v.code, v.path, v.detail))
            unique.append(v)
    return unique


def verify_against(store_dir: str | Path, ref: str) -> list[Violation]:
    """Classify every store file that differs between ``ref`` and the current working tree
    against the store's OWN write-path rules (design ruling 2, "Transition layer") — the
    CI-consumable half of ``sidegraph-verify --against <git-ref>``. Runs alongside
    :func:`verify_snapshot` (which always runs — see ``cli.verify_main``); this function
    alone never checks a single snapshot in isolation for schema/referential correctness,
    only whether what CHANGED was legal.

    Raises ``NotADirectoryError`` for a missing ``store_dir`` (mirrors
    :func:`verify_snapshot`) and ``ValueError`` for every other operational failure —
    ``store_dir`` not inside a git working tree, an unknown/unresolvable ``ref``, or ``git``
    itself unavailable. All of these are the CLI's exit-1 path, never a violation (design
    ruling 2: "Not-a-git-repo / unknown ref -> operational error").

    Deletion legality (design ruling 2's "Deletions" bullet): :func:`classify_transition`
    alone always reports a decision/domain deletion as illegal (it has no I/O access to the
    archive). This function applies the one sanctioned exception: every archive copy of
    the same kind and id in the new tree must be a schema-valid compactable record whose
    raw payload is a legal transition from the baseline, preserving its immutable keys.
    An ID alone never authorizes deletion. Entity/initiative
    deletions have no such exception (no compact path exists for either kind) and stay
    illegal unconditionally. Binding-file changes of any shape, including deletion, are
    never classified at all here, matching :func:`classify_transition`. An already-
    published ``archive/*.jsonl`` segment is write-once: a NEW segment is legal, but any
    modification or deletion of an existing one is always illegal (see
    :func:`_classify_store_relative_path`'s docstring) — checked directly here, not via
    :func:`classify_transition` (a segment is a multi-record file, not a per-record shape).

    ``Violation.path`` here is the changed file's path RELATIVE TO THE REPO ROOT (exactly as
    ``git diff`` itself reports it) — not the ``<dir>/<id>.json``-relative-to-store guess
    :func:`classify_transition` produces internally, and not the absolute filesystem path
    :func:`verify_snapshot` uses; this function always corrects it to the real changed path.
    """
    store_dir = Path(store_dir)
    _require_store_dir(store_dir)

    repo_root = _find_repo_root(store_dir)
    store_dir_abs = store_dir.resolve()
    store_rel_path = store_dir_abs.relative_to(repo_root)
    store_rel = "" if str(store_rel_path) == "." else store_rel_path.as_posix()

    # Fix-round review (Important-2): the git subprocess always runs with cwd=repo_root
    # (see _find_repo_root/_git_diff_name_status), NOT the caller's process cwd -- a
    # pathspec built from a store_dir that was itself relative to the CALLER's cwd (e.g.
    # the caller is running from a repo subdirectory and passed a relative --db) would be
    # silently re-interpreted relative to repo_root instead, mis-scoping the diff to a path
    # that doesn't exist and yielding an EMPTY diff -- a silently "clean" verdict over real
    # tampering, the worst possible failure mode for a CI lint. git accepts an absolute
    # pathspec regardless of cwd, so always pass the already-resolved absolute path here.
    baseline_oid = _resolve_commit_oid(repo_root, ref)
    changed = _git_diff_name_status(repo_root, baseline_oid, store_dir_abs)
    if symlinked_internals(store_dir):
        # The snapshot layer stops at the links; a transition diff would read through them. The
        # repo and the ref are resolved first, so a bad ref stays an operational error.
        return []
    require_listable_record_dirs(store_dir)

    ref_marker = _marker_at_ref(repo_root, baseline_oid, store_rel)
    new_marker = _marker_on_disk(store_dir_abs)
    if (ref_marker is not None and ref_marker.major == 2) or (
        new_marker is not None and new_marker.major == 2
    ):
        return _packed_against(
            store_dir_abs, repo_root, store_rel, baseline_oid, changed, ref_marker, new_marker
        )

    # The NEW tree's archive contents (on-disk, right now) -- what deletion legality
    # consults. Preserve every raw occurrence: selecting the first ID match could hide an
    # altered second copy. The snapshot layer separately reports malformed/duplicate lines.
    new_tree_archive = _check_archive_dir(store_dir_abs / "archive")
    archived_entries = {
        "decision": new_tree_archive.decision_entries,
        "domain": new_tree_archive.domain_entries,
    }

    violations: list[Violation] = []
    for status, repo_path in changed:
        if store_rel:
            if not (repo_path == store_rel or repo_path.startswith(store_rel + "/")):
                continue  # defensive: git's own pathspec already scopes this
            inner = repo_path[len(store_rel) + 1 :]
        else:
            inner = repo_path
        kind = _classify_store_relative_path(inner)
        if kind is None or kind == "binding":
            continue

        if kind == "initiative":
            if status == "D" and _baseline_initiative_loaded(
                repo_root, baseline_oid, repo_path, inner
            ):
                violations.append(
                    _violation(
                        ILLEGAL_DELETION,
                        repo_path,
                        "initiative deleted (no compaction/archive path exists for initiatives)",
                    )
                )
            continue

        if kind in {"archive", "verification"}:
            # Segments are write-once (store._write_archive_segment: "Segments are
            # write-once ... and this always creates a brand-new file"). A new segment
            # (status "A") is the only legal shape; any other change to an already-
            # published segment's bytes is illegal -- deleted (no pruning path exists, so
            # this is never a legitimate compaction shape the way a hot record's deletion
            # can be) or modified (content diffed at the byte level, not field-by-field --
            # a segment is a multi-record file, not this layer's per-record comparison).
            if status == "D":
                violations.append(
                    _violation(
                        ILLEGAL_DELETION,
                        repo_path,
                        (
                            "archive segment deleted (write-once; no pruning path exists)"
                            if kind == "archive"
                            else "verification deleted (write-once; no pruning path exists)"
                        ),
                    )
                )
            elif status != "A":
                violations.append(
                    _violation(
                        ILLEGAL_FIELD_CHANGE,
                        repo_path,
                        (
                            "write-once archive segment modified"
                            if kind == "archive"
                            else "write-once verification modified"
                        ),
                    )
                )
            continue

        if _check_safe_stem(Path(inner)) is not None:
            continue  # never a record: the reload skips it, the snapshot layer reports it
        try:
            old = None if status == "A" else _git_show_json(repo_root, baseline_oid, repo_path)
            new = None if status == "D" else _read_json_object(repo_root / repo_path)
        except _UnparsableContent:
            continue  # the always-on snapshot layer already reports this as parse-error
        if old is not None and not is_safe_record_id(old.get(_KIND_ID_FIELDS[kind])):
            continue  # the baseline held no record either: the reload skips an unsafe id

        found = classify_transition(kind, old, new)
        if new is None and found and kind in ("decision", "domain"):
            rid_field = "id" if kind == "decision" else "domain_id"
            old_id = old.get(rid_field) if old is not None else None
            # Only a string id can be an archived one (a list/dict id is unhashable).
            if isinstance(old_id, str) and (entries := archived_entries[kind].get(old_id)):
                assert old is not None  # old_id came from this baseline payload
                found = [
                    violation
                    for _, archived in entries
                    for violation in _classify_archive_transfer(kind, old, archived)
                ]
        violations.extend(replace(v, path=repo_path) for v in found)

    return violations
