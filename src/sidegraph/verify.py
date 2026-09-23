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
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path

from pydantic import BaseModel, ValidationError

from .schema import SCHEMA_VERSION, AnchorBinding, Decision, Domain, Entity, Fact, Initiative
from .store import (
    _FORMAT_MARKER_NAME,
    _FORMAT_MARKER_PREFIX,
    _MIGRATABLE_SCHEMA_VERSIONS,
    _RELOADABLE_SCHEMA_VERSIONS,
)

# Violation codes — pinned constants (design ruling 2). The CLI's --json output and every
# test assert against these exact strings, never a free-form message.
PARSE_ERROR = "parse-error"
UNKNOWN_SCHEMA_VERSION = "unknown-schema-version"
BAD_VALIDITY_WINDOW = "bad-validity-window"
SUPERSEDED_WITHOUT_SUCCESSOR = "superseded-without-successor"
DANGLING_SUPERSEDES = "dangling-supersedes"
DANGLING_BINDING_ENTITY = "dangling-binding-entity"
DANGLING_FACT_SUPPORT = "dangling-fact-support"
DUPLICATE_ULID = "duplicate-ulid"
BAD_ARCHIVE_SEGMENT = "bad-archive-segment"
FILENAME_ID_MISMATCH = "filename-id-mismatch"

# Transition-layer codes (design ruling 2, "Transition layer" — see that section below).
ILLEGAL_FIELD_CHANGE = "illegal-field-change"
ILLEGAL_STATUS_JUMP = "illegal-status-jump"
VALID_TO_UNSET = "valid-to-unset"
VALID_TO_CHANGED = "valid-to-changed"
ILLEGAL_DELETION = "illegal-deletion"

# A store's committed ``format`` marker (see ``store._ensure_format_marker``) is only ever
# stamped with the running code's CURRENT ``SCHEMA_VERSION`` at write time — it is never
# rewritten by a mere reload. A store this code can actually OPEN (see
# ``store._RELOADABLE_SCHEMA_VERSIONS``/``_MIGRATABLE_SCHEMA_VERSIONS``) may therefore still
# carry an older marker; all three sets together are exactly "known to this running code",
# mirroring what ``Store.__init__`` itself tolerates without raising.
_KNOWN_SCHEMA_VERSIONS = frozenset(
    {SCHEMA_VERSION} | _RELOADABLE_SCHEMA_VERSIONS | _MIGRATABLE_SCHEMA_VERSIONS
)

# Substring pydantic's wrapped ``ValueError`` carries for the ``Decision``/``Fact``
# validity-window ``model_validator`` (see schema.py) — see ``_check_temporal_dir``.
_VALIDITY_WINDOW_MSG = "valid_to must be >= valid_from"

# Both DecisionStatus.SUPERSEDED and DomainStatus.SUPERSEDED serialize to this same literal
# (see schema.py) — the supersedes-chain check is generic across decisions/facts/domains, so
# it compares against the raw string rather than importing either enum.
_SUPERSEDED = "superseded"


@dataclass(frozen=True)
class Violation:
    """One integrity finding. ``path`` is always a string (a file path, or a synthetic
    ``dir/<id>.json`` for an id that only exists inside an archive segment)."""

    code: str
    path: str
    detail: str


def _violation(code: str, path: Path | str, detail: str) -> Violation:
    return Violation(code=code, path=str(path), detail=detail)


# -- generic file loading -----------------------------------------------------------------


def _iter_json_files(dir_path: Path) -> list[Path]:
    if not dir_path.is_dir():
        return []
    return sorted(dir_path.glob("*.json"))


def _load_raw_json(path: Path) -> tuple[object | None, Violation | None]:
    """Read+parse one file as JSON. Never raises — a decode/read failure becomes a
    :data:`PARSE_ERROR` violation instead, so one corrupt file never aborts the whole
    snapshot pass."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        return None, _violation(PARSE_ERROR, path, f"unreadable: {e}")
    try:
        return json.loads(text), None
    except json.JSONDecodeError as e:
        return None, _violation(PARSE_ERROR, path, f"invalid JSON: {e}")


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


# -- per-directory checks ------------------------------------------------------------------


def _check_temporal_dir(
    dir_path: Path, model: type[BaseModel]
) -> tuple[dict[str, dict], dict[str, Path], dict[str, list[Path]], list[Violation]]:
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
    fails to decode at all contributes no id anywhere.
    """
    raw_by_id: dict[str, dict] = {}
    path_by_id: dict[str, Path] = {}
    all_paths_by_id: dict[str, list[Path]] = {}
    violations: list[Violation] = []
    for path in _iter_json_files(dir_path):
        raw, err = _load_raw_json(path)
        if err is not None:
            violations.append(err)
            continue
        if not isinstance(raw, dict) or "id" not in raw:
            violations.append(_violation(PARSE_ERROR, path, "not a record object (missing 'id')"))
            continue
        rid = raw["id"]
        all_paths_by_id.setdefault(rid, []).append(path)
        mismatch = _check_filename_matches_id(path, rid)
        if mismatch is not None:
            violations.append(mismatch)
        if rid not in raw_by_id:
            raw_by_id[rid] = raw
            path_by_id[rid] = path
        try:
            model.model_validate(raw)
        except ValidationError as e:
            errors = e.errors()
            if len(errors) == 1 and _VALIDITY_WINDOW_MSG in errors[0]["msg"]:
                violations.append(_violation(BAD_VALIDITY_WINDOW, path, errors[0]["msg"]))
            else:
                violations.append(_violation(PARSE_ERROR, path, str(e)))
    return raw_by_id, path_by_id, all_paths_by_id, violations


def _check_plain_dir(
    dir_path: Path, model: type[BaseModel], id_field: str
) -> tuple[dict[str, dict], dict[str, Path], dict[str, list[Path]], list[Violation]]:
    """``domains/``, ``entities/``, ``initiatives/`` — no validity window, straightforward
    schema validation. ``id_field`` is ``"domain_id"``/``"entity_id"``/``"id"``. See
    :func:`_check_temporal_dir` for the ``path_by_id`` vs. ``all_paths_by_id`` split."""
    raw_by_id: dict[str, dict] = {}
    path_by_id: dict[str, Path] = {}
    all_paths_by_id: dict[str, list[Path]] = {}
    violations: list[Violation] = []
    for path in _iter_json_files(dir_path):
        raw, err = _load_raw_json(path)
        if err is not None:
            violations.append(err)
            continue
        if not isinstance(raw, dict) or id_field not in raw:
            violations.append(
                _violation(PARSE_ERROR, path, f"not a record object (missing {id_field!r})")
            )
            continue
        rid = raw[id_field]
        all_paths_by_id.setdefault(rid, []).append(path)
        mismatch = _check_filename_matches_id(path, rid)
        if mismatch is not None:
            violations.append(mismatch)
        if rid not in raw_by_id:
            raw_by_id[rid] = raw
            path_by_id[rid] = path
        try:
            model.model_validate(raw)
        except ValidationError as e:
            violations.append(_violation(PARSE_ERROR, path, str(e)))
    return raw_by_id, path_by_id, all_paths_by_id, violations


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
                AnchorBinding.model_validate({**payload, "record_id": record_id})
            except ValidationError as e:
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


def _check_archive_dir(dir_path: Path) -> _ArchiveIndex:
    """``archive/*.jsonl`` — one JSON object per line (see
    ``docs/reference/store-format.md#archive-segments-sidegraph-compact`` and
    ``store._archive_record_line``). A line that fails to parse is
    :data:`BAD_ARCHIVE_SEGMENT`, scoped separately from :data:`PARSE_ERROR` (which is
    reserved for the one-file-per-record canonical directories) since "this JSONL segment has
    a bad line" and "this record file is corrupt" are different failure shapes worth
    distinguishing in a report."""
    decisions: dict[str, dict] = {}
    domains: dict[str, dict] = {}
    decision_entries: dict[str, list[tuple[Path, dict]]] = {}
    domain_entries: dict[str, list[tuple[Path, dict]]] = {}
    violations: list[Violation] = []
    if not dir_path.is_dir():
        return _ArchiveIndex(decisions, domains, decision_entries, domain_entries, violations)
    for path in sorted(dir_path.glob("*.jsonl")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as e:
            violations.append(_violation(BAD_ARCHIVE_SEGMENT, path, f"unreadable: {e}"))
            continue
        for lineno, raw_line in enumerate(text.splitlines(), start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                violations.append(_violation(BAD_ARCHIVE_SEGMENT, path, f"line {lineno}: {e}"))
                continue
            if not isinstance(obj, dict):
                violations.append(
                    _violation(BAD_ARCHIVE_SEGMENT, path, f"line {lineno}: not a JSON object")
                )
                continue
            record_type = obj.get("record_type")
            payload = {k: v for k, v in obj.items() if k != "record_type"}
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
    """``<store_dir>/format`` — a single committed line, ``sidegraph-store <version>`` (see
    ``store._ensure_format_marker``). Missing, malformed, or an unrecognized version are all
    reported the same way: :data:`UNKNOWN_SCHEMA_VERSION`."""
    marker = store_dir / _FORMAT_MARKER_NAME
    if not marker.is_file():
        return [_violation(UNKNOWN_SCHEMA_VERSION, marker, "format marker missing")]
    try:
        text = marker.read_text(encoding="utf-8").strip()
    except OSError as e:
        return [_violation(UNKNOWN_SCHEMA_VERSION, marker, f"unreadable: {e}")]
    if not text.startswith(_FORMAT_MARKER_PREFIX):
        return [_violation(UNKNOWN_SCHEMA_VERSION, marker, f"unrecognized format marker {text!r}")]
    version = text[len(_FORMAT_MARKER_PREFIX) :]
    if version not in _KNOWN_SCHEMA_VERSIONS:
        return [_violation(UNKNOWN_SCHEMA_VERSION, marker, f"unknown schema_version {version!r}")]
    return []


# -- cross-reference checks -----------------------------------------------------------------


def _check_supersedes(combined: dict[str, dict], path_by_id: dict[str, Path]) -> list[Violation]:
    """Generic across decisions/facts/domains — mirrors the real write-path rule
    (``store.add_decision``/``add_fact``/``supersede_domain``): a successor's ``supersedes``
    must reference a record that exists (:data:`DANGLING_SUPERSEDES`), and a record whose
    ``status`` is ``superseded`` must be the target of some OTHER record's ``supersedes``
    (:data:`SUPERSEDED_WITHOUT_SUCCESSOR`) — the same "close + link, atomically" guarantee
    ``add_decision``'s docstring describes. ``combined`` is the union of hot and archived
    payloads for one record type (a superseded record's successor may itself have since been
    compacted, so both pools count as "exists")."""
    violations: list[Violation] = []
    successors = {
        payload["supersedes"] for payload in combined.values() if payload.get("supersedes")
    }
    for rid, payload in combined.items():
        supersedes = payload.get("supersedes")
        if supersedes and supersedes not in combined:
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


def _check_binding_entities(
    bindings_by_record: dict[str, list[dict]],
    path_by_record: dict[str, Path],
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
    facts_raw: dict[str, dict], path_by_id: dict[str, Path], decision_ids: set[str]
) -> list[Violation]:
    """Every id in a ``Fact.supports`` list must resolve to a real decision (hot or archived)
    — mirrors ``store.add_fact``'s own existence check."""
    violations: list[Violation] = []
    for fid, payload in facts_raw.items():
        for did in payload.get("supports") or []:
            if did not in decision_ids:
                violations.append(
                    _violation(
                        DANGLING_FACT_SUPPORT, path_by_id[fid], f"supports unknown decision {did!r}"
                    )
                )
    return violations


def _check_duplicate_ulids(
    hot_paths_by_id: dict[str, list[Path]],
    archive_entries_by_id: dict[str, list[tuple[Path, dict]]],
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
    never id-equality alone.
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
            if all(payload == payloads[0] for payload in payloads):
                continue  # sanctioned cross-branch archive merge -- exempt
        locations = list(hot_locs) + [path for path, _ in archive_locs]
        detail = "present in " + "; ".join(str(p) for p in locations)
        violations.append(_violation(DUPLICATE_ULID, locations[0], detail))
    return violations


# -- entry point ------------------------------------------------------------------------


def verify_snapshot(store_dir: str | Path) -> list[Violation]:
    """Lint every canonical file under ``store_dir`` against the invariants ``store.py``'s
    write API enforces at write time (see this module's docstring for why this reads the
    files directly rather than opening a ``Store``). Returns ``[]`` for a clean store.

    Raises ``NotADirectoryError`` if ``store_dir`` does not exist or is not a directory —
    the CLI (``verify_main``) treats that as the operational-error exit path (1), distinct
    from a violation (exit 2): an unreadable store dir is not itself an integrity finding
    about the store's contents.
    """
    store_dir = Path(store_dir)
    if not store_dir.is_dir():
        raise NotADirectoryError(f"store directory not found: {store_dir}")

    violations: list[Violation] = []
    violations += _check_format_marker(store_dir)

    decisions_raw, decision_paths, decision_all_paths, v = _check_temporal_dir(
        store_dir / "decisions", Decision
    )
    violations += v
    facts_raw, fact_paths, fact_all_paths, v = _check_temporal_dir(store_dir / "facts", Fact)
    violations += v
    domains_raw, domain_paths, domain_all_paths, v = _check_plain_dir(
        store_dir / "domains", Domain, "domain_id"
    )
    violations += v
    entities_raw, _entity_paths, entity_all_paths, v = _check_plain_dir(
        store_dir / "entities", Entity, "entity_id"
    )
    violations += v
    _initiatives_raw, _initiative_paths, initiative_all_paths, v = _check_plain_dir(
        store_dir / "initiatives", Initiative, "id"
    )
    violations += v
    bindings_by_record, binding_paths, v = _check_bindings_dir(store_dir / "bindings")
    violations += v
    archive = _check_archive_dir(store_dir / "archive")
    violations += archive.violations

    entity_ids = set(entities_raw)

    # Decisions/domains: a superseded record's successor (or the dangling-supersedes target
    # itself) may live only in the archive, so the cross-reference checks run against the
    # union of hot + archived payloads; hot wins on an id present in both (mirrors
    # ``store._reload_index_from_canonical``'s own hot-always-wins precedent) -- the
    # id itself is ALSO reported separately as ``duplicate-ulid`` below.
    combined_decisions = {**archive.decisions, **decisions_raw}
    decision_path_by_id = {
        **{did: entries[0][0] for did, entries in archive.decision_entries.items()},
        **decision_paths,
    }
    violations += _check_supersedes(combined_decisions, decision_path_by_id)

    # Facts are never archived (see docs/reference/store-format.md) -- hot only.
    violations += _check_supersedes(facts_raw, fact_paths)

    combined_domains = {**archive.domains, **domains_raw}
    domain_path_by_id = {
        **{dmid: entries[0][0] for dmid, entries in archive.domain_entries.items()},
        **domain_paths,
    }
    violations += _check_supersedes(combined_domains, domain_path_by_id)

    violations += _check_binding_entities(bindings_by_record, binding_paths, entity_ids)
    violations += _check_fact_supports(facts_raw, fact_paths, set(combined_decisions))

    # duplicate-ulid: decisions/domains compare hot against BOTH archive pools (the only two
    # archived record types); facts/entities/initiatives are never archived, so only hot-hot
    # duplication is possible for them.
    violations += _check_duplicate_ulids(decision_all_paths, archive.decision_entries)
    violations += _check_duplicate_ulids(domain_all_paths, archive.domain_entries)
    violations += _check_duplicate_ulids(fact_all_paths, {})
    violations += _check_duplicate_ulids(entity_all_paths, {})
    violations += _check_duplicate_ulids(initiative_all_paths, {})

    return violations


# -- transition layer (--against <git-ref>) ------------------------------------------------
#
# design/superpowers/specs/2026-07-11-ci-integrity-design.md ruling 2, "Transition layer":
# for each store file changed vs a git ref, classify the modification against the STORE'S
# OWN write rules rather than a blanket "committed files never change" lint (which would be
# wrong -- supersede legally closes a predecessor, ratify legally flips statuses, sync
# legally refreshes entity/domain engine-mapping fields, compact legally deletes a hot file
# once its content is durably archived). The mutable-field tables below are DERIVED from
# store.py's write methods, not invented -- see each table's own comment for exactly which
# write path was read. Two disagreements surfaced between the design ruling's plan and the
# actual code while deriving these tables (code wins, per the task brief):
#
# 1. ``Domain`` has NO ``valid_to`` field at all (see schema.py's ``Domain`` -- only
#    ``Decision``/``Fact`` carry temporal validity). The design ruling's domain table lists
#    ``valid_to`` as mutable; dropped here -- there is no such key to ever diff.
# 2. The design ruling's domain table names ``parent_slug`` as an immutable field; the
#    actual schema field is ``parent_id`` (``parent_slug`` is an MCP-tool PARAMETER name in
#    ``server.supersede_domain``/``_add_domain_impl``, resolved to ``parent_id`` before a
#    ``Domain`` is ever constructed). Corrected here — immutable by omission either way
#    (this module classifies via a mutable allow-list, so the exact immutable spelling only
#    matters for this comment, not for behavior), but worth flagging as a real inaccuracy.
#
# Two further store.py facts worth flagging even though they don't change the tables below:
#
# - ``Entity.last_seen_node_id``/``last_seen_graph_version``/``last_seen_community`` and
#   ``Domain.communities`` are *index-only* — ``_entity_identity_payload``/
#   ``_domain_canonical_payload`` (store.py) strip them before a canonical file is ever
#   written, so sync's routine refresh of these fields never touches a committed file at all
#   (design §1: "sync never writes a committed file again" — confirmed by inspection: a
#   freshly upserted entity's ``entities/<id>.json`` contains only ``entity_id``/
#   ``canonical_name``/``kind``/``descriptor``; a domain's ``domains/<id>.json`` never
#   contains ``communities``). They stay in the MUTABLE tables below anyway, matching the
#   design ruling's intent (these are engine-mapping, never content, so a hand-edit touching
#   them should be tolerated, not flagged) — but in practice no LEGITIMATE diff ever
#   exercises that branch: a real committed file never carries these keys to begin with, so
#   classifying them mutable is inert bookkeeping, not a load-bearing behavior.
# - ``Initiative`` has NO append-only guard at all (``Store.upsert_initiative`` is a bare,
#   unconditional upsert — see store.py) — so field-level mutation is never checked for
#   initiatives here (:func:`classify_transition` has no ``"initiative"`` kind;
#   :func:`verify_against` handles the type directly, deletion-only — see its docstring).
#
# Two more legal rewrites, beyond the mutable-field tables, that this module must not flag
# (design/superpowers/specs/2026-09-23-verify-ratifier-stamp-design.md): ``Store.ratify``,
# ``Store.ratify_fact`` and ``Store.ratify_domains`` stamp ``ratified_at``/``ratified_by`` on a
# decision, fact or domain the moment it is accepted — see :func:`_check_ratifier_stamp`, which
# allows exactly that one move (unset on a still-``proposed`` record, landing on any status
# reached through ``accepted`` within the diffed range) and flags every other change to those
# two fields. Separately, a field added to a schema after some records were already written
# (``Provenance.commit``, or ``Domain.seed_anchors``/``path_prefixes``, whose defaults are the
# empty list) is absent from an old file's JSON and appears back in as that default the next
# time anything rewrites the file — a ratify, a drop, a supersede-close, or a domain
# drop/supersede all do this incidentally, not because the field itself changed. :func:`_same`
# treats an absent key as equal to ``null``/``[]``/``{}``, recursing into nested objects and
# into the items of a list, so this never reads as an ``illegal-field-change`` on its own; a
# key that actually changes value, or that is removed while holding a non-empty value, still
# does. A field whose default is a non-empty value is not covered by this (see :func:`_same`'s
# own docstring).

# Decisions/facts (store.py's ``add_decision``, ``add_fact``, ``ratify``, ``drop``,
# ``ratify_fact``, ``drop_fact``): the only fields any write path ever changes on an EXISTING
# record are ``status`` (along a real transition) and ``valid_to`` (null -> a value, once,
# when a record closes). Everything else is written once, at creation, and never touched
# again by any of those methods.
_DECISION_STATUS_TRANSITIONS = frozenset(
    {
        ("proposed", "accepted"),  # ratify
        ("proposed", "rejected"),  # drop
        ("proposed", "superseded"),  # add_decision/ratify closing a still-proposed predecessor
        ("accepted", "superseded"),  # add_decision/ratify closing an accepted predecessor
        # No live write path sets DEPRECATED today -- store.py's own comment on
        # _TERMINAL_DECISION_STATUSES calls it a "dead enum value ... included for
        # forward-compat rather than silently left hot forever the day something does start
        # setting it." Allowed here for that same documented forward-compat reason, not a
        # gap this module papers over.
        ("accepted", "deprecated"),
    }
)
_FACT_STATUS_TRANSITIONS = frozenset(
    {
        ("proposed", "accepted"),  # ratify_fact, or ratify's decision-supports cascade
        ("proposed", "rejected"),  # drop_fact, or drop's all-supporters-rejected cascade
        ("proposed", "superseded"),  # add_fact closing a still-proposed predecessor
        ("accepted", "superseded"),  # add_fact closing an accepted predecessor
        # schema.py: "DEPRECATED unused for facts" -- no forward-compat carve-out here,
        # unlike decisions above.
    }
)
DECISION_MUTABLE_FIELDS = frozenset({"status", "valid_to"})
FACT_MUTABLE_FIELDS = frozenset({"status", "valid_to"})

# Domains (store.py's ``add_domain`` [creation only], ``supersede_domain``,
# ``ratify_domains``, ``refresh_domain_communities``): status moves along
# ratify_domains'/supersede_domain's transitions; ``communities`` is
# sync's engine-mapping refresh (see the module-level note above -- never actually present
# in the canonical file).
_DOMAIN_STATUS_TRANSITIONS = frozenset(
    {
        ("proposed", "accepted"),  # ratify_domains(accept=...)
        ("proposed", "dropped"),  # ratify_domains(drop=...)
        ("accepted", "dropped"),  # ratify_domains(drop=...) -- extended beyond proposed-only
        # (review round 3, design §6: cross-branch slug-conflict resolution)
        ("proposed", "superseded"),  # supersede_domain
        ("accepted", "superseded"),  # supersede_domain
        # Fix-round review (Important-1): supersede_domain (store.py) flips
        # old.status to SUPERSEDED with NO gate on old's current status at all -- unlike
        # ratify_domains, it never checks old is proposed/accepted first. find_domain_by_slug
        # deliberately keeps a DROPPED domain resolvable by slug ("accepted > proposed >
        # dropped" preference order, store.py's own docstring) specifically so a dropped
        # domain can still be superseded/revived later. Reviewer reproduced dropped ->
        # superseded via the real MCP entry points (ratify drop, then supersede_domain) --
        # code wins over my earlier {proposed,accepted}-only restriction.
        ("dropped", "superseded"),  # supersede_domain, from a previously-dropped domain
    }
)
DOMAIN_MUTABLE_FIELDS = frozenset({"status", "communities"})

# Entities (store.py's ``upsert_entity``, ``_entity_identity_payload``):
# the canonical file holds ONLY entity_id/canonical_name/kind/descriptor -- upsert_entity
# rewrites the file solely when the IDENTITY payload changes (i.e. ``descriptor`` -- a
# "moved" rebind, see ``sync._adopt``). ``entity_id``/``canonical_name``/``kind`` are minted
# once (or, for ``kind``, fixed at construction) and never rewritten by any write path.
ENTITY_MUTABLE_FIELDS = frozenset(
    {"descriptor", "last_seen_node_id", "last_seen_graph_version", "last_seen_community"}
)

_KIND_DIRS = {
    "decision": "decisions",
    "fact": "facts",
    "domain": "domains",
    "entity": "entities",
    "binding": "bindings",
}
_KIND_ID_FIELDS = {"decision": "id", "fact": "id", "domain": "domain_id", "entity": "entity_id"}


def _synthetic_path(kind: str, payload: dict) -> str:
    """A best-guess ``<dir>/<id>.json`` path built from the payload alone —
    :func:`classify_transition` is pure and never receives the real filesystem/git path.
    In every legitimate case this matches the real path exactly (the store always names a
    file after its own id — see :data:`FILENAME_ID_MISMATCH`); :func:`verify_against`
    overwrites it with the actual changed git path regardless, so a mismatch here is never
    user-visible."""
    rid = payload.get(_KIND_ID_FIELDS[kind], "?")
    return f"{_KIND_DIRS[kind]}/{rid}.json"


def _check_status_transition(
    old: dict, new: dict, legal: frozenset[tuple[str, str]], path: str
) -> list[Violation]:
    old_status, new_status = old.get("status"), new.get("status")
    if old_status == new_status or (old_status, new_status) in legal:
        return []
    return [
        _violation(
            ILLEGAL_STATUS_JUMP,
            path,
            f"status {old_status!r} -> {new_status!r} is not a real transition",
        )
    ]


def _check_valid_to_transition(old: dict, new: dict, path: str) -> list[Violation]:
    """``valid_to``: null -> a value, exactly once, is the only legal move — mirrors every
    ``add_decision``/``add_fact``/``ratify``/``drop`` call site's own
    ``predecessor.valid_to = predecessor.valid_to or max(...)`` idiom (store.py): it is
    ALWAYS an or-assignment onto a currently-``None`` field, never onto an already-set one.
    """
    old_vt, new_vt = old.get("valid_to"), new.get("valid_to")
    if old_vt == new_vt:
        return []
    if old_vt is None:
        return []  # null -> value: legal
    if new_vt is None:
        return [_violation(VALID_TO_UNSET, path, "valid_to reverted from a value to null")]
    return [_violation(VALID_TO_CHANGED, path, f"valid_to changed {old_vt!r} -> {new_vt!r}")]


# The ratifier stamp (store.py's ``ratify``/``ratify_fact``/``ratify_domains``, design D4) —
# see the module comment above the mutable-field tables for why this pair needs its own rule
# rather than a plain mutable-field entry: unlike ``status``/``valid_to``, a stamp may be set
# only once, and only alongside a specific status move.
_RATIFIER_STAMP = ("ratified_at", "ratified_by")

# An absent key (``dict.get`` already turns it into ``None``) and these two defaults are the
# other information-free shapes a field can serialize back in as, once a rewrite touches the
# record it lives on — see :func:`_same`.
_EMPTY_DEFAULTS: tuple[dict, list] = ({}, [])


def _absent_or_empty(v: object) -> bool:
    return v is None or v in _EMPTY_DEFAULTS


def _same(a: object, b: object) -> bool:
    """Equality where an absent key counts as the same value as ``null`` or an empty
    list/object (Rule B) — see the module comment above the mutable-field tables: a field
    added to a model after a record was written (``Provenance.commit``, the ratifier stamp,
    ``Domain.seed_anchors``/``path_prefixes``) is missing from an old file and appears as its
    default — ``null``, ``[]``, or ``{}`` — the next time anything rewrites it. Recurses into
    nested dicts key-by-key and into lists element-by-element (so a change buried inside a
    ``seed_anchors`` entry is still caught), and treats two lists of different length as
    different. A field whose default is a NON-empty value (``Decision.scope``, ``"repo"``) is
    not covered: an absent key there still reads as changed. That is a known, accepted gap —
    no file in this store lacks ``scope`` today — not something this function papers over.
    """
    if isinstance(a, dict) and isinstance(b, dict):
        return all(_same(a.get(k), b.get(k)) for k in set(a) | set(b))
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b, strict=True))
    if _absent_or_empty(a) or _absent_or_empty(b):
        return _absent_or_empty(a) and _absent_or_empty(b)
    return a == b


def _check_ratifier_stamp(old: dict, new: dict, path: str) -> list[Violation]:
    """Rule A: ``ratified_at``/``ratified_by`` may change in exactly one way — both unset on
    a still-``proposed`` old side, ``ratified_at`` newly set, and the new status anything
    reached through ``accepted`` within the diffed range (not ``proposed`` or ``rejected`` —
    ``drop``/``drop_fact`` stamp nothing). Any other change to either field — appearing on a
    record that wasn't proposed, appearing alongside a drop, ``ratified_by`` set without
    ``ratified_at``, or an edit/removal of an already-set stamp — stays
    :data:`ILLEGAL_FIELD_CHANGE`."""
    old_stamp = {k: old.get(k) for k in _RATIFIER_STAMP}
    new_stamp = {k: new.get(k) for k in _RATIFIER_STAMP}
    if old_stamp == new_stamp:
        return []
    stamping = (
        old.get("status") == "proposed"
        and all(v is None for v in old_stamp.values())
        and new_stamp["ratified_at"] is not None
        and new.get("status") not in ("proposed", "rejected")
    )
    if stamping:
        return []
    return [
        _violation(ILLEGAL_FIELD_CHANGE, path, f"field {k!r} changed")
        for k in _RATIFIER_STAMP
        if old_stamp[k] != new_stamp[k]
    ]


def _check_immutable_fields(
    old: dict, new: dict, mutable: frozenset[str], path: str, exempt: tuple[str, ...] = ()
) -> list[Violation]:
    """Every key present in ``old`` or ``new`` but not in ``mutable`` or ``exempt`` must be
    the same value on both sides (:func:`_same`, Rule B — an absent key counts as equal to
    ``null`` or an empty list/object) — covers a changed value, a field that disappeared, AND
    a field that newly appeared (all three are "this immutable field changed"). One violation
    per offending field (design ruling 2: "detail names the field"), in sorted key order for a
    deterministic report. ``exempt`` (the ratifier stamp, for decisions/facts/domains) is
    skipped here entirely — its own rule is :func:`_check_ratifier_stamp`, run alongside this
    one by :func:`classify_transition`, never by this function."""
    violations: list[Violation] = []
    for key in sorted(set(old) | set(new)):
        if key in mutable or key in exempt:
            continue
        if not _same(old.get(key), new.get(key)):
            violations.append(_violation(ILLEGAL_FIELD_CHANGE, path, f"field {key!r} changed"))
    return violations


def classify_transition(kind: str, old: dict | None, new: dict | None) -> list[Violation]:
    """Classify one record's before/after payload against the store's OWN write-path rules
    (design ruling 2, "Transition layer"). Pure: no filesystem/git access, so every case is
    directly unit-testable without a git repo — :func:`verify_against` is the thin,
    I/O-doing wrapper that feeds this real ``(old, new)`` pairs pulled from ``git show``/the
    working tree.

    ``kind`` is one of ``{"decision", "fact", "domain", "entity", "binding"}``.

    ``old=None`` means the file was ADDED — always legal (a brand-new record can carry any
    content; every write-path invariant on ITS shape is the snapshot layer's job, not this
    one's).

    ``new=None`` means the file was DELETED:

    - ``kind == "binding"``: always legal, unconditionally (see the ``kind == "binding"``
      branch below — bindings are never classified here at all, added/modified/deleted
      alike; referential integrity of the new state is the snapshot layer's job).
    - every other kind: this function ALONE always reports :data:`ILLEGAL_DELETION` — it
      has no I/O access to check whether the id is safely archived. The one sanctioned
      exception (a decision/domain compacted into an archive segment present in the new
      tree) is applied by :func:`verify_against` as a post-hoc filter over this function's
      raw output, never by this function itself. A pure test exercising ``new=None``
      directly is therefore always the "id absent from any archive" scenario, by
      construction.

    Neither payload is ever a real filesystem/git path, so a returned :class:`Violation`'s
    ``path`` is a best-effort ``<dir>/<id>.json`` guess (:func:`_synthetic_path`) that
    :func:`verify_against` overwrites with the actual changed path.
    """
    if kind not in _KIND_DIRS:
        raise ValueError(f"unknown kind {kind!r}")

    if kind == "binding":
        # Referential-only, machine-managed payload (design ruling 2): a dropped entry or a
        # deleted bindings/<record_id>.json file is derived-community decay or record
        # compaction, never flagged here regardless of shape.
        return []

    if old is None:
        return []  # added -- always legal

    if new is None:
        return [
            _violation(
                ILLEGAL_DELETION,
                _synthetic_path(kind, old),
                "record deleted (no archive segment consulted at this pure layer)",
            )
        ]

    path = _synthetic_path(kind, new)
    if kind in ("decision", "fact"):
        legal = _DECISION_STATUS_TRANSITIONS if kind == "decision" else _FACT_STATUS_TRANSITIONS
        mutable = DECISION_MUTABLE_FIELDS if kind == "decision" else FACT_MUTABLE_FIELDS
        return (
            _check_status_transition(old, new, legal, path)
            + _check_valid_to_transition(old, new, path)
            + _check_immutable_fields(old, new, mutable, path, exempt=_RATIFIER_STAMP)
            + _check_ratifier_stamp(old, new, path)
        )
    if kind == "domain":
        return (
            _check_status_transition(old, new, _DOMAIN_STATUS_TRANSITIONS, path)
            + _check_immutable_fields(old, new, DOMAIN_MUTABLE_FIELDS, path, exempt=_RATIFIER_STAMP)
            + _check_ratifier_stamp(old, new, path)
        )
    # entity
    return _check_immutable_fields(old, new, ENTITY_MUTABLE_FIELDS, path)


class _UnparsableContent(Exception):
    """One side of a changed file's diff failed to parse as a JSON object. Caught by
    :func:`verify_against` to skip just that one file's transition check — the ALWAYS-ON
    snapshot layer already reports a corrupt hot file as :data:`PARSE_ERROR`; the
    transition layer piling on a second, less specific finding for the same root cause
    would be noise, and one unparsable file must never abort the whole ``--against`` pass."""


def _run_git(
    args: list[str], cwd: Path, *, timeout: float | None = None
) -> subprocess.CompletedProcess[str]:
    """``timeout`` (drift→supersede D1) bounds the subprocess for hook-path callers; the
    default ``None`` is byte-for-byte today's behavior for every existing call site. A
    ``TimeoutExpired`` maps onto the same ``ValueError`` contract as ``OSError`` — every
    caller already treats that as "git unavailable"."""
    try:
        return subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ValueError(f"could not run git ({' '.join(args)}): {e}") from e


def _find_repo_root(store_dir: Path) -> Path:
    """The git repository containing ``store_dir`` — may be an ancestor of it, not
    ``store_dir`` itself. Raises ``ValueError`` (the CLI's operational-error exit path, 1 —
    never a violation, design ruling 2: "Not-a-git-repo ... -> operational error") when
    ``store_dir`` isn't inside any git working tree."""
    result = _run_git(["rev-parse", "--show-toplevel"], cwd=store_dir)
    if result.returncode != 0:
        raise ValueError(f"{store_dir} is not inside a git repository: {result.stderr.strip()}")
    return Path(result.stdout.strip()).resolve()


def _git_diff_name_status(repo_root: Path, ref: str, store_dir: Path) -> list[tuple[str, str]]:
    """``git diff --no-renames --name-status <ref> -- <store_dir>``, run from ``repo_root``
    (design ruling 2: "git plumbing ... ``git diff --name-status <ref> -- <store-dir>``").

    ``--no-renames`` is deliberate: a renamed-but-identical record file would otherwise
    report as one ``R100`` line instead of a delete+add pair — but the store's own
    invariant is that a file is always named after its own id (:data:`FILENAME_ID_MISMATCH`),
    so a genuine rename is never legitimate, and surfacing it as a deletion (illegal, absent
    from any archive) alongside an unrelated addition is the MORE informative shape, not a
    loss of information.

    Compares ``ref`` against the CURRENT WORKING TREE (git's default for a single-ref
    ``diff`` — no second ref, no ``--cached``), matching the CI use case this exists for:
    ``origin/main`` vs. whatever the PR branch has checked out right now. Untracked files
    never appear in ``git diff`` output at all (git diff semantics, not a bug here) — a
    normal CI checkout has none.
    """
    result = _run_git(
        ["diff", "--no-renames", "--name-status", ref, "--", str(store_dir)], cwd=repo_root
    )
    if result.returncode != 0:
        raise ValueError(f"git diff against {ref!r} failed: {result.stderr.strip()}")
    changed: list[tuple[str, str]] = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        status, _, path = line.partition("\t")
        changed.append((status[:1], path))
    return changed


def _git_show_json(repo_root: Path, ref: str, repo_path: str) -> dict:
    result = _run_git(["show", f"{ref}:{repo_path}"], cwd=repo_root)
    if result.returncode != 0:
        raise ValueError(f"git show {ref}:{repo_path} failed: {result.stderr.strip()}")
    try:
        obj = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise _UnparsableContent(str(e)) from e
    if not isinstance(obj, dict):
        raise _UnparsableContent(f"{repo_path}@{ref} is not a JSON object")
    return obj


def _read_json_object(path: Path) -> dict:
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
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
    ``format`` marker, the gitignored ``index.db``, or any unexpected nesting."""
    parts = store_relative.split("/")
    if len(parts) == 2 and parts[0] == "archive" and parts[1].endswith(".jsonl"):
        return "archive"
    if len(parts) != 2 or not parts[1].endswith(".json"):
        return None
    return _DIR_TO_KIND.get(parts[0])


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
    archive). This function applies the one sanctioned exception — a deleted record whose id
    appears in an ``archive/*.jsonl`` segment PRESENT IN THE NEW TREE (i.e. on disk right
    now, under ``store_dir/archive`` — a legitimate ``sidegraph-compact``) — by dropping
    that specific :data:`ILLEGAL_DELETION` finding after the fact. Entity/initiative
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
    if not store_dir.is_dir():
        raise NotADirectoryError(f"store directory not found: {store_dir}")

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
    changed = _git_diff_name_status(repo_root, ref, store_dir_abs)

    # The NEW tree's archive contents (on-disk, right now) -- what deletion legality
    # consults. Only the id sets matter here; any BAD_ARCHIVE_SEGMENT-shaped finding is the
    # always-on snapshot layer's job, not re-reported by this pass.
    new_tree_archive = _check_archive_dir(store_dir_abs / "archive")
    archived_ids = {
        "decision": set(new_tree_archive.decisions),
        "domain": set(new_tree_archive.domains),
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
            if status == "D":
                violations.append(
                    _violation(
                        ILLEGAL_DELETION,
                        repo_path,
                        "initiative deleted (no compaction/archive path exists for initiatives)",
                    )
                )
            continue

        if kind == "archive":
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
                        "archive segment deleted (write-once; no pruning path exists)",
                    )
                )
            elif status != "A":
                violations.append(
                    _violation(
                        ILLEGAL_FIELD_CHANGE,
                        repo_path,
                        "write-once archive segment modified",
                    )
                )
            continue

        try:
            old = None if status == "A" else _git_show_json(repo_root, ref, repo_path)
            new = None if status == "D" else _read_json_object(repo_root / repo_path)
        except _UnparsableContent:
            continue  # the always-on snapshot layer already reports this as parse-error

        found = classify_transition(kind, old, new)
        if new is None and found and kind in ("decision", "domain"):
            rid_field = "id" if kind == "decision" else "domain_id"
            if old is not None and old.get(rid_field) in archived_ids[kind]:
                found = []  # legitimate sidegraph-compact -- archived in the new tree
        violations.extend(replace(v, path=repo_path) for v in found)

    return violations
