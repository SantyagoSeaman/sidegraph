"""Advisory curation lint — the advisory half of ``sidegraph-doctor``.

See design/superpowers/specs/2026-07-23-sidegraph-doctor-design.md. Strict conformance
lives in ``verify.py`` (pinned ``Violation`` codes, CI-gateable); this module is the
curation-quality complement: findings are advice, not contract breaches, and the CLI exit
code ignores them unless ``--check`` escalates.

**PURE READ**, same reasoning as ``verify.py``'s module docstring: never construct
:class:`sidegraph.store.Store` (its ``__init__`` can write — index refresh, legacy
migration). Canonical files are read directly via ``verify``'s loading helpers. The one
non-canonical source is ``index.db``, opened read-only for binding statuses only (Task 2)
— statuses are volatile, index-only state that canonical ``bindings/*.json`` files
deliberately do not carry (docs/reference/store-format.md).

A file that fails to parse yields NO finding here: surfacing corruption is
``verify_snapshot``'s job (``parse-error``), ``sidegraph-doctor`` always runs both
halves, and a second copy of the same complaint would be noise.
"""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, NamedTuple

from ulid import ULID

from .schema import DecisionStatus, canonicalize
from .store import _STAMPING_MARKER_NAME, _TERMINAL_DECISION_STATUSES
from .verify import _check_archive_dir, _iter_json_files, _load_raw_json, _run_git

# Finding codes — pinned constants (design: "Advisory checks — pinned finding codes").
# ``--json`` output and every test assert against these exact strings.
DANGLING_RECORD = "dangling-record"
DEGRADED_BINDING = "degraded-binding"
ORPHANED_BINDING = "orphaned-binding"
STALE_PROPOSAL = "stale-proposal"
UNREFERENCED_ENTITY = "unreferenced-entity"
EXPIRED_OPEN_VALIDITY = "expired-open-validity"
DUPLICATE_ENTITY = "duplicate-entity"
CODE_DRIFT = "code-drift"
STALE_INSTRUCTIONS = "stale-instructions"
UNRATIFIED_ACCEPT = "unratified-accept"

# Name reported in ``CurationReport.skipped`` when index.db is absent or unusable — the
# binding-status check has no canonical source to fall back on (statuses are index-only).
BINDING_STATUS_CHECK = "binding-status"

NEVER_SURFACED = "never-surfaced"

# Same name as the finding code (matching BINDING_STATUS_CHECK's own precedent) — reported
# in ``CurationReport.skipped`` when index.db is absent or unusable: shows/queries are
# index-only derived state (design/superpowers/specs/2026-07-25-retrieval-telemetry-design.md)
# with no canonical fallback.
NEVER_SURFACED_CHECK = "never-surfaced"

# Terminal decision statuses, as strings -- imported from store.py rather than hand-rolled
# here so D4's "live" complement always matches _TERMINAL_DECISION_STATUSES exactly (it
# includes DEPRECATED; an earlier draft of the design spec got this wrong by hand-listing
# "superseded/rejected" and missing it). DecisionStatus is a StrEnum, so its members compare
# equal to the plain strings this module reads out of raw JSON.
_TERMINAL_DECISION_STATUS_VALUES = frozenset(s.value for s in _TERMINAL_DECISION_STATUSES)

# Statuses under which a decision is still "open" memory, i.e. a curation target. A
# superseded/rejected/deprecated record awaiting compaction is deliberately NOT flagged
# by dangling/expired checks (design: narrower than viz's dangling flag, which marks
# every record node). DERIVED as the terminal complement, never hand-listed (drift→supersede
# review M1: the hand-listed pair would silently diverge on a sixth DecisionStatus, the
# exact hazard the comment above already warns about).
_OPEN_DECISION_STATUSES = frozenset(s.value for s in DecisionStatus) - (
    _TERMINAL_DECISION_STATUS_VALUES
)

# Entity-name prefixes exempt from unreferenced-entity: ``domain:*`` is the structural
# pair of a Domain row, minted at acceptance and legitimately bindingless until a Tier-1
# decision binds there; ``community:*`` is derived and no longer committed, but legacy
# stores may still carry decayed leftovers (store-format: "legacy committed entries decay
# lazily") and flagging those would tell users to hand-delete what sync retires itself.
_STRUCTURAL_ENTITY_PREFIXES = ("domain:", "community:")


@dataclass(frozen=True)
class Finding:
    """One curation finding. Same field shape as ``verify.Violation``, a distinct type on
    purpose: findings are advice, violations are contract breaches — the CLI must never
    mix the two lists."""

    code: str
    path: str
    detail: str


@dataclass(frozen=True)
class CurationReport:
    """Everything one ``curate`` pass produced: findings plus the names of checks that
    could not run (``skipped`` — e.g. ``binding-status`` without a usable index.db)."""

    findings: list[Finding] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


# -- canonical-file loading (silent-skip: parse errors are verify's territory) -------------


def _iter_records(store_dir: Path, subdir: str) -> list[tuple[Path, dict]]:
    """(path, payload) for every parseable JSON-object file in ``<store>/<subdir>``,
    sorted by path (findings inherit this order — deterministic output)."""
    out: list[tuple[Path, dict]] = []
    for path in _iter_json_files(store_dir / subdir):
        payload, err = _load_raw_json(path)
        if err is None and isinstance(payload, dict):
            out.append((path, payload))
    return out


def _binding_entries(store_dir: Path) -> dict[str, list[dict]]:
    """record_id -> committed anchor entries, from ``bindings/<record_id>.json`` (each a
    JSON LIST of identity dicts, no status — see store-format). Non-list payloads and
    non-dict entries are skipped under the same silent-skip policy."""
    out: dict[str, list[dict]] = {}
    for path in _iter_json_files(store_dir / "bindings"):
        payload, err = _load_raw_json(path)
        if err is None and isinstance(payload, list):
            out[path.stem] = [e for e in payload if isinstance(e, dict)]
    return out


# -- individual checks ---------------------------------------------------------------------


def _check_dangling_records(
    decisions: list[tuple[Path, dict]],
    facts: list[tuple[Path, dict]],
    bindings: dict[str, list[dict]],
    decision_status_by_id: dict[str, str],
) -> list[Finding]:
    """dangling-record: an OPEN record (decision proposed|accepted; fact valid_to null)
    whose committed anchor set is absent or empty -- AND, for a fact only, whose
    ``supports`` also fails to resolve to a LIVE decision (design D4/D5/D6, external review
    Defect B).

    Decisions (D7, unchanged): a decision has no ``supports`` field, so "an anchor alone is
    reachability" is still the whole test for that branch.

    Facts are narrower, because the OLD anchor-only rule contradicted the write path's own
    gate (``capture.py``'s reachability check accepts a standalone fact with EITHER an
    anchor OR a supports id) and retrieval renders such a fact as inline evidence for any
    decision that reaches a bucket -- i.e. every LIVE one (see ``retrieval.rank_decisions``
    and its ``evidence=False`` calls for the terminal exception, D6). An anchorless fact is
    dangling unless at least one ``supports`` id resolves to a LIVE (``proposed``/
    ``accepted``) decision:

    - no supports at all -> reported, generic wording.
    - every supports id fails to resolve to ANY known decision (hot or archived) -> reported
      with D5's distinct wording -- a genuinely broken reference, a different problem from
      simply having none, and the message says so instead of leaving the reader to open the
      file.
    - every supports id resolves, but only to TERMINAL decisions -> reported with D6's
      wording: such a fact survives only in ``list_facts``/a direct ``facts_for_decision``
      lookup, never in task-seeded retrieval, so "was reachable once, decayed" is exactly
      what a curation check should say.
    - a MIX of a missing id and a terminal id (no live id at all): the missing-id wording
      wins -- a broken reference is worth naming over "merely" terminal.
    """
    open_records: list[tuple[Path, dict, str]] = [
        (p, d, "decision") for p, d in decisions if d.get("status") in _OPEN_DECISION_STATUSES
    ] + [(p, f, "fact") for p, f in facts if f.get("valid_to") is None]
    findings: list[Finding] = []
    for path, rec, kind in open_records:
        rid = rec.get("id")
        if not isinstance(rid, str):
            continue
        if bindings.get(rid):
            continue  # an anchor set alone is reachability, for either kind
        if kind == "decision":
            findings.append(
                Finding(
                    DANGLING_RECORD,
                    str(path),
                    f"open {kind} has no committed anchor set "
                    f"(bindings/{rid}.json absent or empty)",
                )
            )
            continue

        raw_supports = rec.get("supports")
        supports = (
            [s for s in raw_supports if isinstance(s, str)]
            if isinstance(raw_supports, list)
            else []
        )
        statuses = [decision_status_by_id.get(sid) for sid in supports]
        # D4's "live" is the complement of _TERMINAL_DECISION_STATUS_VALUES, checked
        # directly against it (not a second, separately-maintained "open" set) so the two
        # can never drift apart: a resolved, non-terminal status IS live by definition.
        if any(s is not None and s not in _TERMINAL_DECISION_STATUS_VALUES for s in statuses):
            continue  # D4: reachable via a live supporting decision
        if not supports:
            detail = "no anchor set and no supporting decision"
        elif any(s is None for s in statuses):
            detail = "supports a decision that does not exist"
        else:
            detail = (
                "supporting decision is superseded/rejected/deprecated -- reachable only "
                "via list_facts, not task-seeded retrieval"
            )
        findings.append(Finding(DANGLING_RECORD, str(path), detail))
    return findings


def _ulid_datetime(rid: str) -> datetime | None:
    """Creation time embedded in a ULID id, or None when the id isn't a parseable ULID
    (hand-edited store; age is unknowable and the record silently doesn't flag)."""
    try:
        return ULID.from_str(rid).datetime
    except (ValueError, TypeError):
        return None


def _check_stale_proposals(
    decisions: list[tuple[Path, dict]],
    domains: list[tuple[Path, dict]],
    stale_days: int,
    now: datetime,
) -> list[Finding]:
    """stale-proposal: a proposed decision/domain whose ULID id timestamp is STRICTLY
    older than ``stale_days`` — a rotting ratification queue."""
    cutoff = now - timedelta(days=stale_days)
    findings: list[Finding] = []

    def check(path: Path, rec: dict, id_key: str, kind: str) -> None:
        if rec.get("status") != "proposed":
            return
        rid = rec.get(id_key)
        created = _ulid_datetime(rid) if isinstance(rid, str) else None
        if created is None or created >= cutoff:
            return
        findings.append(
            Finding(
                STALE_PROPOSAL,
                str(path),
                f"{kind} proposed {(now - created).days} days ago (threshold {stale_days} days)",
            )
        )

    for path, rec in decisions:
        check(path, rec, "id", "decision")
    for path, rec in domains:
        check(path, rec, "domain_id", "domain")
    return findings


def _check_unreferenced_entities(
    entities: list[tuple[Path, dict]], bindings: dict[str, list[dict]]
) -> list[Finding]:
    """unreferenced-entity: a hot entity no committed anchor set references (bindings
    files persist when their record is archived, so historical references still count).
    Structural ``domain:*`` / ``community:*`` entities are exempt."""
    referenced = {e.get("entity_id") for entries in bindings.values() for e in entries}
    findings: list[Finding] = []
    for path, ent in entities:
        eid = ent.get("entity_id")
        name = ent.get("canonical_name", "")
        if not isinstance(eid, str) or not isinstance(name, str):
            continue
        if name.startswith(_STRUCTURAL_ENTITY_PREFIXES) or eid in referenced:
            continue
        findings.append(
            Finding(
                UNREFERENCED_ENTITY,
                str(path),
                f"entity {name!r} is referenced by no committed anchor set",
            )
        )
    return findings


def _entity_identity_key(ent: dict) -> tuple[str, str, str | None] | None:
    """The logical identity a duplicate-entity group is keyed on. MUST mirror
    ``Store.find_entity`` exactly for concrete entities (``store.py:1365-1367`` at the time
    of writing): ``canonicalize(descriptor.name if descriptor else canonical_name)`` paired
    with ``descriptor.file_path if descriptor else None`` — not a looser
    ``canonicalize(canonical_name)``, or this check and the lookup it describes would
    disagree about what a duplicate even is (design D5).

    Abstract identity is ``kind`` + ``canonical_name`` (mirrors ``find_abstract_entity``).
    One edge accepted rather than redesigned (design §3's own note): ``find_entity`` itself
    is kind-blind, so a stray abstract row can satisfy a ``file_path=None`` concrete lookup
    at runtime — but grouping here is kind-partitioned, so that pair would NOT be reported
    as a duplicate-entity group. Not this check's problem to solve; named so it isn't
    mistaken for a bug later."""
    kind = ent.get("kind")
    canonical_name = ent.get("canonical_name", "")
    if not isinstance(canonical_name, str):
        return None  # malformed -- see below
    if kind == "abstract":
        return ("abstract", canonical_name, None)
    descriptor = ent.get("descriptor")
    if isinstance(descriptor, dict):
        name = descriptor.get("name", canonical_name)
        file_path = descriptor.get("file_path")
    else:
        name = canonical_name
        file_path = None
    # ``None`` for any shape whose identity cannot be computed, so the caller skips it.
    # Returning here rather than letting canonicalize() raise is the module's stated policy
    # -- one corrupt file must never abort the whole pass -- and it is what the sibling
    # _check_unreferenced_entities enforces with its own isinstance guard. Without it an
    # entity whose canonical_name is null took every other check down with it (branch
    # review, Low-1). ``verify`` reports the malformed file itself; this check stays quiet.
    if not isinstance(name, str) or not isinstance(file_path, str | None):
        return None
    return ("concrete", canonicalize(name), file_path)


def _check_duplicate_entities(
    entities: list[tuple[Path, dict]], bindings: dict[str, list[dict]]
) -> list[Finding]:
    """duplicate-entity: two or more committed entity files share one logical identity.

    LEGAL, not corruption (design D2): two branches each minting the same name produce two
    ULIDs -> two files -> a clean git merge, and a UNIQUE index would brick the store on
    exactly that path (see ``test_store_opens_with_merged_duplicate.py``) rather than guard
    it. But it IS ambiguous: lookups now deterministically resolve to the lowest
    ``entity_id`` (D4), so every OTHER id in the group is reliably unreachable by name --
    reported once per group, at the WINNER's file, naming every id and its binding count
    (the count is what makes "which one should absorb the others" an answerable question,
    design D5).

    ``bindings`` is keyed by RECORD id (``bindings/<record_id>.json``, each a list of anchor
    entries — see ``_binding_entries``), not by entity id, so counting "how many bindings
    does this entity have" means flattening every record's entries first (same precondition
    ``_check_unreferenced_entities`` computes for its own ``referenced`` set)."""
    binding_count_by_entity: dict[str, int] = {}
    for entries in bindings.values():
        for entry in entries:
            eid = entry.get("entity_id")
            if isinstance(eid, str):
                binding_count_by_entity[eid] = binding_count_by_entity.get(eid, 0) + 1

    groups: dict[tuple[str, str, str | None], list[tuple[Path, dict]]] = {}
    for path, ent in entities:
        eid = ent.get("entity_id")
        if not isinstance(eid, str):
            continue
        key = _entity_identity_key(ent)
        if key is None:
            continue
        groups.setdefault(key, []).append((path, ent))

    findings: list[Finding] = []
    for members in groups.values():
        if len(members) < 2:
            continue
        members_sorted = sorted(members, key=lambda pair: pair[1]["entity_id"])
        winner_path, _winner = members_sorted[0]
        detail_ids = ", ".join(
            f"{e['entity_id']} ({binding_count_by_entity.get(e['entity_id'], 0)} bindings)"
            for _p, e in members_sorted
        )
        findings.append(
            Finding(
                DUPLICATE_ENTITY,
                # The real path, root included -- every other file-anchored finding in this
                # module reports str(path), and a hand-built "entities/<id>.json" made this
                # one the odd row out for --json consumers (branch review, Low-2).
                str(winner_path),
                f"{len(members_sorted)} entities share one logical identity; lookups "
                f"resolve to the lowest id: {detail_ids}",
            )
        )
    return sorted(findings, key=lambda f: f.path)


def _parse_aware_iso(ts: object) -> datetime | None:
    """Parse an ISO timestamp; None for non-strings, unparseable strings, and NAIVE
    timestamps (schema requires AwareDatetime — a naive one is verify's territory, and
    comparing it against an aware ``now`` would raise)."""
    if not isinstance(ts, str):
        return None
    try:
        parsed = datetime.fromisoformat(ts)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _check_expired_open(decisions: list[tuple[Path, dict]], now: datetime) -> list[Finding]:
    """expired-open-validity: a decision closed-by-time (valid_to STRICTLY past) whose
    status is still open — never superseded/rejected. Decisions only: Fact.status is
    deprecated-unused and a fact with valid_to set is closed by convention."""
    findings: list[Finding] = []
    for path, d in decisions:
        if d.get("status") not in _OPEN_DECISION_STATUSES:
            continue
        valid_to = _parse_aware_iso(d.get("valid_to"))
        if valid_to is None or valid_to >= now:
            continue
        findings.append(
            Finding(
                EXPIRED_OPEN_VALIDITY,
                str(path),
                f"valid_to {d['valid_to']} is past but status is still {d['status']!r}",
            )
        )
    return findings


def _check_binding_statuses(store_dir: Path) -> tuple[list[Finding], list[str]]:
    """degraded-binding / orphaned-binding: binding rows whose last RECORDED status has
    decayed. Statuses live only in the derived ``index.db`` (canonical bindings files
    carry identity, no status; sync flips statuses; a reload resets them to live), so
    this check reads the index — strictly read-only — and reports "as of last sync".

    ``mode=ro`` cannot write and, on this store's DELETE journal, creates no sidecar files.
    The old ``immutable`` connection flag was dropped deliberately: the coverage journal
    makes ``index.db`` a write target on every tool call, and that flag disables locking
    and change detection — sound only on a quiescent file. See the coverage-telemetry
    design, D8.

    An absent, corrupt, or pre-``anchor_bindings`` index.db is a SKIPPED check, never an
    operational error — only the canonical store gates exit 1 (design ruling)."""
    index_path = store_dir / "index.db"
    if not index_path.is_file():
        return [], [BINDING_STATUS_CHECK]
    try:
        conn = sqlite3.connect(f"file:{index_path}?mode=ro", uri=True)
        try:
            rows = conn.execute("SELECT record_id, entity_id, data FROM anchor_bindings").fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return [], [BINDING_STATUS_CHECK]

    code_by_status = {"degraded": DEGRADED_BINDING, "orphaned": ORPHANED_BINDING}
    findings: list[Finding] = []
    for record_id, entity_id, data in rows:
        try:
            payload = json.loads(data)
        except (json.JSONDecodeError, TypeError):
            continue
        status = payload.get("status") if isinstance(payload, dict) else None
        if not isinstance(status, str):
            continue
        code = code_by_status.get(status)
        if code is None:
            continue
        findings.append(
            Finding(
                code,
                f"bindings/{record_id}.json",
                f"binding {record_id} -> {entity_id} is {status} as of last sync",
            )
        )
    return findings, []


def _check_never_surfaced(store_dir: Path) -> tuple[list[Finding], list[str]]:
    """never-surfaced: decisions anchored where people repeatedly work that have never once
    reached a render (spec D5).

    Reads the index — strictly read-only, same ``mode=ro`` contract as
    ``_check_binding_statuses`` — because show and query counts are derived state and live
    nowhere else. An absent or unusable index.db is SKIPPED, never an error: a store nobody
    has read yet has no evidence of dead memory, and saying otherwise would flag a healthy
    store on its first day.

    Only ``0 shows / N>0 queries`` is reported. ``0 shows / 0 queries`` means nobody worked
    in that area — an absence of occasion, not a defect — and reporting it would bury the
    real signal under every decision in the store.
    """
    index_path = store_dir / "index.db"
    if not index_path.is_file():
        return [], [NEVER_SURFACED_CHECK]
    try:
        conn = sqlite3.connect(f"file:{index_path}?mode=ro", uri=True)
        try:
            shows = dict(conn.execute("SELECT record_id, shows FROM retrieval_shows").fetchall())
            queries = dict(conn.execute("SELECT seed, queries FROM retrieval_seeds").fetchall())
        finally:
            conn.close()
    except sqlite3.Error:
        return [], [NEVER_SURFACED_CHECK]

    # entity_id -> file path, resolved from the CANONICAL entities (no index needed) — same
    # source _check_unreferenced_entities already reads.
    file_by_entity: dict[str, str] = {}
    for _path, ent in _iter_records(store_dir, "entities"):
        eid = ent.get("entity_id")
        descriptor = ent.get("descriptor")
        file_path = descriptor.get("file_path") if isinstance(descriptor, dict) else None
        if isinstance(eid, str) and isinstance(file_path, str):
            file_by_entity[eid] = file_path

    bindings = _binding_entries(store_dir)
    findings: list[Finding] = []
    for path, decision in _iter_records(store_dir, "decisions"):
        if decision.get("status") != "accepted":
            continue
        rid = decision.get("id")
        if not isinstance(rid, str) or shows.get(rid, 0) != 0:
            continue
        anchored_paths = sorted(
            {
                file_by_entity[entry["entity_id"]]
                for entry in bindings.get(rid, [])
                if entry.get("entity_id") in file_by_entity
            }
        )
        if not anchored_paths:
            continue
        total_queries = sum(queries.get(p, 0) for p in anchored_paths)
        if total_queries == 0:
            continue
        findings.append(
            Finding(
                NEVER_SURFACED,
                str(path),
                f"anchored to {', '.join(anchored_paths)}, queried {total_queries} times, "
                "never surfaced — check its anchors or the ranking",
            )
        )
    return findings, []


def _file_by_entity(entities: list[tuple[Path, dict]]) -> dict[str, str]:
    """entity_id -> descriptor.file_path, for every concrete entity that has one. Abstract
    entities carry ``descriptor: null`` and drop out naturally (design D5) -- exactly the
    same shape ``_check_never_surfaced`` builds for its own file-coverage join, but taking
    an already-loaded ``entities`` list (``curate`` loads it once, up front) rather than
    re-reading the canonical directory a second time."""
    out: dict[str, str] = {}
    for _path, ent in entities:
        eid = ent.get("entity_id")
        descriptor = ent.get("descriptor")
        file_path = descriptor.get("file_path") if isinstance(descriptor, dict) else None
        if isinstance(eid, str) and isinstance(file_path, str):
            out[eid] = file_path
    return out


def _binding_status_by_key(store_dir: Path) -> dict[tuple[str, str], str]:
    """(record_id, entity_id) -> last-recorded binding status, read from index.db —
    read-only, same ``mode=ro`` contract as ``_check_binding_statuses`` (design D5: "copy
    the _check_binding_statuses precedent", doctor.py:405 at spec-writing time). Binding
    status has no canonical form (store-format) so this is the only place it can be read
    from. Returns ``{}`` when index.db is absent/unusable — code-drift's orphaned-binding
    exclusion simply doesn't filter anything in that case; a missing/corrupt index does NOT
    by itself skip the code-drift check (git availability is what gates that, see
    ``_check_code_drift``), so no separate ``skipped`` entry is added here."""
    index_path = store_dir / "index.db"
    if not index_path.is_file():
        return {}
    try:
        conn = sqlite3.connect(f"file:{index_path}?mode=ro", uri=True)
        try:
            rows = conn.execute("SELECT record_id, entity_id, data FROM anchor_bindings").fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return {}
    out: dict[tuple[str, str], str] = {}
    for record_id, entity_id, data in rows:
        try:
            payload = json.loads(data)
        except (json.JSONDecodeError, TypeError):
            continue
        status = payload.get("status") if isinstance(payload, dict) else None
        if isinstance(status, str):
            out[(record_id, entity_id)] = status
    return out


def _git_diff_name_only(
    repo_root: Path, commit: str, paths: list[str], *, timeout: float | None = None
) -> list[str]:
    """``git diff --name-only <commit>..HEAD -- <paths>``, from ``repo_root`` (design D5).
    Raises ``ValueError`` on a non-zero exit -- mirrors ``verify._git_diff_name_status``'s
    own raise-on-failure contract -- so every call site wraps this the same way that
    module's callers wrap ITS git helpers; ``_scan_code_drift`` is the one caller here, and
    it turns any failure into a failed batch (rendered as a skipped-note finding) rather
    than letting it propagate. ``timeout`` (drift→supersede D1) bounds the subprocess; the
    curate()/CLI path passes none, exactly today's behavior."""
    result = _run_git(
        ["diff", "--name-only", f"{commit}..HEAD", "--", *paths], cwd=repo_root, timeout=timeout
    )
    if result.returncode != 0:
        raise ValueError(f"git diff {commit}..HEAD failed: {result.stderr.strip()}")
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


_CODE_DRIFT_GIT_NOTE = "code-drift: git unavailable — check skipped"


class DriftEntry(NamedTuple):
    """One live, commit-stamped decision whose anchored file(s) changed since capture —
    the fields ``_check_code_drift`` needs to render today's finding exactly, plus the
    ``record_id`` the drift cache (sync.refresh_code_drift_cache) keys on.
    # see design/superpowers/specs/2026-07-30-drift-supersede-affordance-design.md (D1)
    """

    record_path: str
    record_id: str
    files: list[str]


class DriftBatch(NamedTuple):
    """One distinct ``provenance.commit``'s diff outcome, in ``by_commit`` iteration order
    — findings order IS this order, including a failed batch's git-note emitted in place
    (the mid-list interleaving the round-1 review pinned as a refactor constraint)."""

    commit: str
    failed: bool
    entries: list[DriftEntry]


class CodeDriftScan(NamedTuple):
    """The structured result the drift→supersede surfaces consume (D1). ``active`` False
    reproduces the check's silent contract (nothing commit-stamped AND anchored);
    ``repo_root_failed`` is the no-batch-ran case (one note, distinct from per-batch
    failures); ``head`` is resolved ONLY by the load-inclusive :func:`scan_code_drift`
    wrapper (never on the ``curate()`` path — its git-call count must not change) and is
    cache provenance only, it gates nothing."""

    batches: list[DriftBatch]
    unstamped: int
    active: bool
    repo_root_failed: bool
    head: str | None


def _scan_code_drift(
    decisions: list[tuple[Path, dict]],
    bindings: dict[str, list[dict]],
    entities: list[tuple[Path, dict]],
    store_dir: Path,
    repo_root: Path | None,
    *,
    deadline: float | None = None,
    resolve_head: bool = False,
) -> CodeDriftScan:
    """The pure scan half of the code-drift check (D1): record → committed bindings →
    entity ``file_path`` join, orphaned-binding exclusion, one batched diff per distinct
    commit. Never raises; every git failure lands in a batch's ``failed`` flag (or
    ``repo_root_failed``). ``deadline`` is a TOTAL budget in seconds (round-2 N2, never
    per-call): each subprocess gets the remaining time, and once exhausted every un-run
    batch is marked ``failed`` without spawning anything — hook cost is bounded by the
    deadline itself, not by ``distinct_commits × per-call-timeout``. EVERY git call this
    scan makes — root resolution, the batch diffs, and (``resolve_head=True``, the
    :func:`scan_code_drift` wrapper only — never the ``curate()`` path, N6) the final
    ``rev-parse HEAD`` — draws from the ONE budget (code review round 1, I-1/M-1: the
    root used to be resolved through ``verify._find_repo_root`` with no timeout at all,
    then re-resolved a second time in the wrapper)."""
    file_by_entity = _file_by_entity(entities)
    status_by_key = _binding_status_by_key(store_dir)

    unstamped = 0
    by_commit: dict[str, list[tuple[Path, str, set[str]]]] = {}
    for path, d in decisions:
        if d.get("status") not in _OPEN_DECISION_STATUSES:
            continue
        prov = d.get("provenance")
        commit = prov.get("commit") if isinstance(prov, dict) else None
        rid = d.get("id")
        if not isinstance(commit, str) or not commit:
            unstamped += 1
            continue
        if not isinstance(rid, str):
            continue
        paths: set[str] = set()
        for entry in bindings.get(rid, []):
            eid = entry.get("entity_id")
            if not isinstance(eid, str):
                continue
            if status_by_key.get((rid, eid)) == "orphaned":
                continue
            fp = file_by_entity.get(eid)
            if fp:
                paths.add(fp)
        if paths:
            by_commit.setdefault(commit, []).append((path, rid, paths))

    if not by_commit:
        return CodeDriftScan([], unstamped, False, False, None)

    started = time.monotonic()

    def _remaining() -> float | None:
        if deadline is None:
            return None
        return deadline - (time.monotonic() - started)

    if repo_root is None:
        # Inline rather than `verify._find_repo_root` so the call draws on the scan
        # budget — the helper has no timeout seam, and this used to be the one unbounded
        # git call on the hook paths (review I-1). Same command, same cwd, same
        # `.resolve()`; a non-zero exit or any failure is the same repo_root_failed scan
        # the helper's raise produced.
        try:
            result = _run_git(["rev-parse", "--show-toplevel"], cwd=store_dir, timeout=_remaining())
            if result.returncode != 0:
                return CodeDriftScan([], unstamped, True, True, None)
            repo_root = Path(result.stdout.strip()).resolve()
        except (ValueError, OSError, subprocess.TimeoutExpired):
            return CodeDriftScan([], unstamped, True, True, None)

    batches: list[DriftBatch] = []
    for commit, items in by_commit.items():
        remaining = _remaining()
        if remaining is not None and remaining <= 0:
            batches.append(DriftBatch(commit, True, []))
            continue
        all_paths = sorted({p for _path, _rid, paths in items for p in paths})
        try:
            changed = set(_git_diff_name_only(repo_root, commit, all_paths, timeout=remaining))
        except (ValueError, OSError, subprocess.TimeoutExpired):
            batches.append(DriftBatch(commit, True, []))
            continue
        entries = [
            DriftEntry(str(path), rid, drifted)
            for path, rid, paths in items
            if (drifted := sorted(paths & changed))
        ]
        batches.append(DriftBatch(commit, False, entries))

    head: str | None = None
    if resolve_head:
        remaining = _remaining()
        if remaining is None or remaining > 0:
            try:
                result = _run_git(["rev-parse", "HEAD"], cwd=repo_root, timeout=remaining)
                if result.returncode == 0:
                    head = result.stdout.strip() or None
            except (ValueError, OSError, subprocess.TimeoutExpired):
                head = None
    return CodeDriftScan(batches, unstamped, True, False, head)


def scan_code_drift(
    store_dir: str | Path,
    repo_root: Path | None = None,
    *,
    deadline: float | None = None,
) -> CodeDriftScan:
    """Load-inclusive public entry point for the drift→supersede surfaces (D1): loads the
    three record sets itself (the same helpers ``curate()`` uses — no ``Store``, this
    module stays pure-read) and asks the scan to resolve ``head`` (``git rev-parse
    HEAD``) for cache provenance. ``resolve_head`` is set ONLY here, never on the
    ``curate()`` path (round-2 N6), and an inactive store still makes zero git calls
    (N5). Every git call — root resolution, diffs, head — draws on the ONE ``deadline``
    (review I-1/M-1), so hook cost is bounded by the deadline itself."""
    root = Path(store_dir)
    decisions = _iter_records(root, "decisions")
    entities = _iter_records(root, "entities")
    bindings = _binding_entries(root)
    return _scan_code_drift(
        decisions, bindings, entities, root, repo_root, deadline=deadline, resolve_head=True
    )


def _check_code_drift(
    decisions: list[tuple[Path, dict]],
    bindings: dict[str, list[dict]],
    entities: list[tuple[Path, dict]],
    store_dir: Path,
    repo_root: Path | None,
) -> list[Finding]:
    """code-drift: a LIVE decision's anchored file has changed since its capture-time HEAD
    (design D5) -- the UPDATE-half curation candidate list this whole wave exists for.

    "Live" is the same open-record set every other check in this module reasons about
    (:data:`_OPEN_DECISION_STATUSES` -- proposed/accepted; a superseded/rejected/deprecated
    decision is historical, not a drift target). For each live decision whose
    ``provenance.commit`` is set, this resolves record -> committed bindings -> entities ->
    ``descriptor.file_path`` (a pure in-memory join over already-loaded canonical JSON),
    excluding any binding index.db records as ``orphaned`` (:func:`_binding_status_by_key`
    -- a degraded binding is still included, same as ``valid_decisions_for_entity``'s own
    live/degraded-vs-orphaned split). Records with NO commit (every pre-wave record, by
    design -- commits are never backfilled) are counted once, not silently dropped:
    ``"N record(s) predate commit stamping — not checkable"``.

    That unstamped-count note (and the whole check) stays SILENT when there is not one
    single commit-stamped, checkable decision anywhere in the store: every decision written
    before this wave shipped lacks ``provenance.commit`` by construction (§5/§6, "pre-wave
    records are never code-drift-checkable"), so an unconditional note would surface on
    every pre-existing store forever, permanently costing it a clean ``sidegraph-doctor``
    run for a wave it never opted into. The check only starts talking once it has at least
    one commit-stamped, anchored decision to actually check — at which point the unstamped
    count rides alongside as informational context about the rest of the store.

    Anchored file sets are grouped by their decision's ``provenance.commit`` so ``git diff``
    runs ONCE per distinct commit (batched), never once per decision. Never raises and never
    silently reports zero on a git failure (design ruling; C1's silent-zero shape rejected):
    a failed batch yields NO drift findings for that commit plus one
    :data:`_CODE_DRIFT_GIT_NOTE` finding, IN PLACE in the batch order, so a git-less run
    reads as "check skipped", never as a clean pass.

    Since the drift→supersede wave this is a pure renderer over :func:`_scan_code_drift`
    (D1) — the scan owns the join and the git mechanics; this function owns only today's
    exact finding shapes and order, which the pre-wave test suite pins unchanged.
    """
    scan = _scan_code_drift(decisions, bindings, entities, store_dir, repo_root)
    if not scan.active:
        # Nothing in the store is commit-stamped AND anchored -- the check has literally
        # nothing to say, unstamped count included (see this function's docstring).
        return []

    findings: list[Finding] = []
    if scan.unstamped:
        findings.append(
            Finding(
                CODE_DRIFT,
                str(store_dir),
                f"{scan.unstamped} record(s) predate commit stamping — not checkable",
            )
        )
    if scan.repo_root_failed:
        findings.append(Finding(CODE_DRIFT, str(store_dir), _CODE_DRIFT_GIT_NOTE))
        return findings
    for batch in scan.batches:
        if batch.failed:
            findings.append(Finding(CODE_DRIFT, str(store_dir), _CODE_DRIFT_GIT_NOTE))
            continue
        for entry in batch.entries:
            findings.append(
                Finding(
                    CODE_DRIFT,
                    entry.record_path,
                    f"anchored file(s) changed since capture (commit {batch.commit[:8]}): "
                    + ", ".join(entry.files),
                )
            )
    return findings


# -- entry point ---------------------------------------------------------------------------


# Files a host auto-injects into every session's turn-zero context. The whitepaper's
# §12.6 finding is why this check exists: that file wins by position, so a superseded
# rule surviving in it outranks the store's correction by default.
_INSTRUCTION_FILES = ("CLAUDE.md", "AGENTS.md", "GEMINI.md", ".cursorrules")

# A phrase must be this distinctive before a match means anything: short titles ("Use
# SQLite", "Retry policy") collide with ordinary prose and would make the check noise.
_MIN_PHRASE_CHARS = 28
_MIN_PHRASE_WORDS = 5


def _normalize_phrase(text: str) -> str:
    """Lowercased, whitespace-collapsed, punctuation-light form for substring matching.

    Deliberately NOT a semantic comparison: this check reports a VERBATIM-ish quotation
    of a superseded record's own wording, which is a fact a script can establish. Whether
    the file still *teaches* the abandoned rule (rather than narrating its history) is the
    human judgment the finding asks for — see the finding's wording.
    """
    lowered = re.sub(r"[`*_#>\[\]()]", " ", text.lower())
    return re.sub(r"\s+", " ", lowered).strip()


def _check_stale_instructions(
    decisions: list[tuple[Path, dict]], repo_root: Path | None
) -> list[Finding]:
    """stale-instructions: an auto-injected agent-instructions file still carries the
    distinctive wording of a SUPERSEDED decision.

    Advisory and deliberately conservative: title-length and word-count floors, superseded
    records only (an accepted record's wording SHOULD appear there), and a wording that
    names the possibility rather than asserting a contradiction. ``repo_root`` is the
    directory the instructions files are looked up in — the store's parent in the standard
    layout; no git call, no search.
    """
    if repo_root is None:
        return []
    phrases: list[tuple[str, str]] = []  # (normalized phrase, record id)
    for _path, rec in decisions:
        if rec.get("status") != "superseded":
            continue
        rid = rec.get("id")
        title = rec.get("title")
        if not isinstance(rid, str) or not isinstance(title, str):
            continue
        norm = _normalize_phrase(title)
        if len(norm) >= _MIN_PHRASE_CHARS and len(norm.split()) >= _MIN_PHRASE_WORDS:
            phrases.append((norm, rid))
    if not phrases:
        return []

    findings: list[Finding] = []
    for name in _INSTRUCTION_FILES:
        f = repo_root / name
        try:
            if not f.is_file():
                continue
            haystack = _normalize_phrase(f.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue
        for norm, rid in phrases:
            if norm in haystack:
                findings.append(
                    Finding(
                        STALE_INSTRUCTIONS,
                        str(f),
                        f"quotes the wording of superseded decision {rid} "
                        f'("{norm[:60]}…") — an auto-injected file reaches the agent '
                        "before retrieval does, so verify it no longer teaches the "
                        "abandoned rule",
                    )
                )
    return findings


def _read_stamping_marker(store_dir: Path) -> datetime | None:
    """The store's creation marker (``store.py``'s ``_STAMPING_MARKER_NAME``, written
    once — and only once — by ``Store._ensure_stamping_marker`` the moment a store is
    found genuinely new), or ``None`` when it's absent: every store predating that
    feature, and any store that already held a record of any kind (hot or archived) the
    first time a stamping version opened it, never has this file. Read directly off disk
    (this module never constructs ``Store`` — see the module docstring).

    A missing, unreadable, or unparseable marker degrades to ``None`` the same silent-skip
    way every other read in this module does. Unlike most such reads, this one is NOT
    covered elsewhere: ``verify_snapshot`` has no check on this file at all (measured,
    review round 2, Minor 4 — a prior version of this docstring claimed otherwise; it was
    wrong). A hand-edited, corrupt, or future-dated marker therefore silently re-opens the
    exact blind window this whole mechanism exists to close — ``_check_unratified_accepts``
    falls back to stamp-only scoping (or ``[]`` with no stamps either) with no finding, no
    warning, anywhere, ever pointing at the marker itself as the reason. Recorded here as a
    known gap, not fixed in this round: closing it is a `verify.py` change (a new pinned
    ``Violation`` code for an unparseable/naive/future marker), a different module's
    contract, out of scope for this fix."""
    try:
        text = (store_dir / _STAMPING_MARKER_NAME).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    return _parse_aware_iso(text.strip())


# Tier-0 entity kinds only the propose path ever binds fresh onto a superseding record --
# review Major 1 (fix round 2). ``supersede_decision``/``supersede_fact`` never creates
# either: with anchors omitted it copies the predecessor's bindings verbatim (never a NEW
# one), and with explicit anchors it has no ``tags``/``initiative`` parameter at all to bind
# one from. Only ``add_decision`` (source ``"human"``), ``doc_import`` (source
# ``"doc-import"``) and the propose/``auto_accept`` path (source ``"agent"``) ever mint
# these, so for an agent-sourced, stamp-less, ``supersedes``-bearing record, one of these
# present and NOT already on the predecessor is unambiguous: the propose path wrote it.
_PROPOSE_ONLY_TIER0_PREFIXES = ("tag:", "initiative:")


def _tier0_tag_or_initiative_entity_ids(
    record_id: str | None,
    bindings: dict[str, list[dict]],
    entity_canonical_by_id: dict[str, str],
) -> set[str]:
    """``entity_id``s of ``record_id``'s own committed tier-0 ``tag:``/``initiative:``
    bindings -- ``set()`` for ``None``/an unknown id/a record with no bindings at all,
    which correctly makes "absent from the predecessor" true by default when the
    predecessor itself cannot be resolved (never a false negative on the propose-path
    signal)."""
    if not isinstance(record_id, str):
        return set()
    out: set[str] = set()
    for b in bindings.get(record_id, []):
        if not isinstance(b, dict) or b.get("tier") != 0:
            continue
        entity_id = b.get("entity_id")
        if not isinstance(entity_id, str):
            continue
        name = entity_canonical_by_id.get(entity_id)
        if isinstance(name, str) and name.startswith(_PROPOSE_ONLY_TIER0_PREFIXES):
            out.add(entity_id)
    return out


def _has_propose_only_signal(
    rec: dict,
    bindings: dict[str, list[dict]],
    entity_canonical_by_id: dict[str, str],
) -> bool:
    """True iff ``rec`` (a ``supersedes``-bearing, accepted, agent-sourced, stamp-less
    record) carries at least one field only the propose/``auto_accept`` path can produce —
    review Major 1: ``layer``, a ``provenance.ref``, or a tier-0 ``tag:``/``initiative:``
    binding not already on its predecessor. One-directional by construction (see the
    constant above's comment): presence proves propose wrote it; absence does NOT prove
    ``supersede_decision``/``supersede_fact`` wrote it -- a minimal propose draft (no
    layer/ref/tags) is genuinely indistinguishable from a genuine successor, and stays
    excluded either way (see the check's own docstring)."""
    if rec.get("layer"):
        return True
    provenance = rec.get("provenance")
    if isinstance(provenance, dict) and provenance.get("ref"):
        return True
    successor_tags = _tier0_tag_or_initiative_entity_ids(
        rec.get("id"), bindings, entity_canonical_by_id
    )
    if not successor_tags:
        return False
    predecessor_tags = _tier0_tag_or_initiative_entity_ids(
        rec.get("supersedes"), bindings, entity_canonical_by_id
    )
    return bool(successor_tags - predecessor_tags)


def _check_unratified_accepts(
    decisions: list[tuple[Path, dict]],
    facts: list[tuple[Path, dict]],
    store_dir: Path,
    bindings: dict[str, list[dict]],
    entities: list[tuple[Path, dict]],
) -> list[Finding]:
    """unratified-accept: an ACCEPTED, agent-sourced record carrying no ratifier stamp —
    the signature `SIDEGRAPH_AUTO_ACCEPT=on` leaves behind.

    That switch is per-environment (one developer's shell) but writes into the SHARED
    committed store, so the ratification gate can be bypassed for everyone by a setting
    nobody else sees. Three of four practitioner reviewers flagged it independently; this
    makes it auditable in a PR diff and in CI instead of trust-based.

    The signature is genuinely ambiguous for records ratified before the stamp existed, so
    the check is SCOPED to the period where it can discriminate. The scope start is the
    EARLIER of: the store's earliest ``ratified_at`` stamp, and its creation marker
    (``stamping_live_since`` — see ``_read_stamping_marker``/``Store._ensure_stamping_marker``),
    when either exists. Records older than that scope start are skipped entirely.

    Without a creation marker (every store predating that feature), this is exactly the
    original rule: a store with no stamps at all produces no findings, because nothing
    in it can discriminate. Measured before shipping — unscoped, this check emitted 239
    findings on this project's own store (every agent-sourced record predating the
    feature), which is not a signal, it is a wall; that wall stays shut for any store
    that has never been opened by a marker-writing version.

    With a creation marker, "a store with no stamps at all produces no findings" is no
    longer universally true, by design: the marker records the instant stamping became
    live for THAT store, so records created at/after it are judgeable whether or not any
    ratification has happened yet. This closes a blind window the stamp-only rule left
    open — a store adopting an auto-ratification policy from scratch has never ratified
    anything, so it had no stamp to scope from and produced zero findings forever, even
    after a later ratification finally armed the check (every existing record already
    predated that first stamp). Taking the EARLIER of the two bounds when both exist
    means a store that was created new and only later ratified something does not lose
    coverage of its own first window either.

    The finding's wording still says the signature is not proof, because an advisory that
    overstates is one people learn to skip.

    Deliberately DOES NOT see a supersession successor (a record with ``supersedes`` set)
    UNLESS it carries a propose-only signal — fix round 1, 2026-09-15, measured live: both
    of this project's own store's findings at the time were ``supersede_decision``/
    ``supersede_fact`` successors, not ``SIDEGRAPH_AUTO_ACCEPT=on``. (Precisely: all of
    them were DECISION successors. The fact half of this exclusion is defensive only --
    ``supersede_fact`` always stamps ``source="human"``, so a fact successor never reaches
    this agent-sourced check at all, and ``DraftFact`` carries no ``supersedes`` field, so
    the propose path cannot write one either. No current writer produces the shape the
    fact half excludes; it is kept so the rule does not silently depend on that staying
    true -- fix round 2 re-review, Minor B.) Reversal
    (``supersede_decision``/``supersede_fact``) has no ratification queue to bypass in the
    first place — it writes its successor ``accepted`` immediately, unconditionally, by
    design (CLAUDE.md invariant #2's "reversal = new record with supersedes"), and its
    ``provenance.source`` is legitimately ``"agent"`` (that tool's own docstring names an
    agent-initiated caller as an anticipated shape, and every real successor in this
    project's own store already is exactly that). Left unscoped, that reproduces this
    check's whole signature — accepted, agent-sourced, no ratifier stamp — on every
    superseding record, with no relation to the env var this check exists to catch.

    Narrowed in fix round 2, 2026-09-15 (review Major 1): fix round 1 excluded EVERY
    ``supersedes``-bearing record on the claim that a ``supersede_decision`` successor and
    an auto-accepted superseding ``propose_decisions`` draft are "genuinely
    indistinguishable" — measurement refuted that for a real, non-trivial share of drafts.
    ``_has_propose_only_signal`` checks three fields only the propose/``auto_accept`` path
    can produce and ``supersede_decision``/``supersede_fact`` structurally cannot: a
    ``layer`` (no parameter on either supersede tool), a ``provenance.ref`` (same), or a
    tier-0 ``tag:``/``initiative:`` binding not already on the predecessor (the supersede
    tools either inherit the predecessor's bindings verbatim or resolve explicit anchors
    with no ``tags``/``initiative`` parameter to mint one from). A ``supersedes``-bearing
    record is now excluded ONLY when none of the three is present — one-directional and
    zero-false-positive by construction: presence proves the propose path wrote it, but a
    MINIMAL superseding draft (no layer/ref/tags) still carries none of the three and stays
    excluded, correctly indistinguishable from a genuine successor, exactly as before.

    Known, accepted gap this still leaves (record the trade-off, don't pretend it isn't
    one): a minimal superseding ``propose_decisions`` draft — no ``layer``, no ``ref``, no
    new tier-0 binding — landed via ``SIDEGRAPH_AUTO_ACCEPT=on`` is still excluded, because
    it is genuinely indistinguishable, from the canonical record's own fields, from a
    legitimate ``supersede_decision``/``supersede_fact`` successor (neither carries any
    field naming which MCP tool wrote it). This is a real, deliberate, NARROWER blind spot
    than fix round 1 shipped, not an oversight — the alternative (still flagging every
    supersession) is the measured, guaranteed-noisy status quo this fix round exists to
    remove, and an advisory nobody trusts protects nothing.
    """
    stamps = [
        parsed
        for _p, rec in [*decisions, *facts]
        if (parsed := _parse_aware_iso(rec.get("ratified_at"))) is not None
    ]
    marker_stamp = _read_stamping_marker(store_dir)
    scope_candidates = [s for s in (marker_stamp, min(stamps) if stamps else None) if s is not None]
    if not scope_candidates:
        return []  # neither a creation marker nor any stamp exists; nothing to scope from
    scope_start = min(scope_candidates)
    entity_canonical_by_id = {
        e["entity_id"]: e["canonical_name"]
        for _p, e in entities
        if isinstance(e.get("entity_id"), str) and isinstance(e.get("canonical_name"), str)
    }

    findings: list[Finding] = []
    for path, rec in [*decisions, *facts]:
        if rec.get("status") != DecisionStatus.ACCEPTED.value:
            continue
        provenance = rec.get("provenance")
        source = provenance.get("source") if isinstance(provenance, dict) else None
        if source != "agent" or rec.get("ratified_at"):
            continue
        if rec.get("supersedes") and not _has_propose_only_signal(
            rec, bindings, entity_canonical_by_id
        ):
            continue  # a reversal's successor -- no ratify queue exists for it to bypass
        created = _parse_aware_iso(rec.get("valid_from"))
        if created is None or created < scope_start:
            continue  # predates observable stamping (or the store's own creation) here
        findings.append(
            Finding(
                UNRATIFIED_ACCEPT,
                str(path),
                "accepted with provenance.source='agent' and no ratifier stamp — the "
                "signature of SIDEGRAPH_AUTO_ACCEPT=on (the ratification gate bypassed "
                "for the whole shared store by one environment's setting). Records "
                "ratified before the ratifier stamp existed look identical, so verify "
                "rather than assume; if the switch is on, decide whether that is the "
                "policy you want committed",
            )
        )
    return findings


# -- D5: auto-ratification audit (informational lines, not findings; design D5) ------------


def _hot_plus_archive_records(store_dir: Path) -> dict[str, dict[str, dict]]:
    """{"decisions": {id: payload}, "facts": {id: payload}, "domains": {domain_id: payload}}:
    hot canonical files plus ``archive/*.jsonl``, HOT winning on an id present in both
    (mirrors ``verify.py``'s own ``{**archive.decisions, **decisions_raw}`` precedent, also
    mirrored at this module's own ``curate()`` above). Facts are never archived
    (``Store.compact``: "facts/ stays entirely hot regardless of status"), so they are hot
    only. Read-only: archive violations are ``verify``'s territory and are silently ignored
    here, the same silent-skip policy ``_iter_records`` already uses. No ``Finding``, no
    ``Store()``, no write.
    # see design/superpowers/specs/2026-09-11-auto-ratification-policy-design.md D5
    """
    archive = _check_archive_dir(store_dir / "archive")

    def hot(subdir: str, key: str) -> dict[str, dict]:
        return {
            rec[key]: rec
            for _p, rec in _iter_records(store_dir, subdir)
            if isinstance(rec.get(key), str)
        }

    return {
        "decisions": {**archive.decisions, **hot("decisions", "id")},
        "facts": hot("facts", "id"),
        "domains": {**archive.domains, **hot("domains", "domain_id")},
    }


@dataclass(frozen=True)
class _AutoShareTally:
    """One kind's classification tallies (D5, the pre-flight review's Q5 table).
    ``auto_total``/``human_total`` are the share line's numerator/denominator (records
    EVER ratified, whatever their current status); ``*_retired`` are the supersede-rate
    numerators (auto/human records whose CURRENT status is in the kind's retired set);
    ``excluded`` counts stamp-less records created after the store's earliest stamp --
    neither numerator nor denominator, but named in the line."""

    auto_total: int = 0
    auto_retired: int = 0
    human_total: int = 0
    human_retired: int = 0
    excluded: int = 0


# The three record kinds the D5 audit classifies (mirrors the store's own dirs). Private
# and prefixed on purpose: this codebase's own "kind" already means `DecisionKind`
# (adr/lesson/constraint/gotcha), and `capture._auto_ratify(kind=...)` uses a third
# vocabulary again ("fact"/"domain"/a decision kind) -- an unprefixed `Kind` here would
# read as a fourth, public meaning (review round 1, Minor 3).
_RecordKind = Literal["decisions", "facts", "domains"]

# Per-kind "retired" statuses for the supersede-rate numerator (design D5, Ruling Z):
# decisions/facts retire only by supersession; a domain retires via EITHER
# ratify_domains(drop=...) OR Store.supersede_domain (rev 10 corrected rev 9's claim that
# drop was the only one -- both are legitimate retirements of an accepted domain, so the
# line's group label is "domains retired", not "domains dropped").
_RETIRED_STATUSES: dict[_RecordKind, frozenset[str]] = {
    "decisions": frozenset({DecisionStatus.SUPERSEDED.value}),
    "facts": frozenset({DecisionStatus.SUPERSEDED.value}),
    "domains": frozenset({"dropped", "superseded"}),
}


def _is_auto_stamped(rec: dict) -> bool:
    """True iff ``rec`` carries a genuine ``auto:``-prefixed ratifier stamp: a parseable
    ``ratified_at`` AND a ``ratified_by`` starting ``auto:`` (design D5, Q5's "auto"
    classification). Both the print gate in ``_auto_share_lines`` and the classifier
    below use this SAME predicate, so a record can never open the gate without also
    feeding the ``min()`` that computes the earliest stamp -- ``min()`` is therefore
    never called over an empty generator."""
    return (
        _parse_aware_iso(rec.get("ratified_at")) is not None
        and isinstance(rb := rec.get("ratified_by"), str)
        and rb.startswith("auto:")
    )


def _tally_kind(kind: _RecordKind, records: dict[str, dict], earliest: datetime) -> _AutoShareTally:
    """Classify every record of one kind (D5, Q5's table) by ``ratified_at`` PRESENCE,
    never by ``ratified_by`` -- a real human ratify can legitimately store
    ``ratified_by=None`` (no resolvable git identity, ``store.py:521-545``). ``earliest``
    is the store's earliest stamp taken over hot PLUS archive, ALL three kinds (mirrors
    ``_check_unratified_accepts``'s scoping rule, widened): a stamp-less accepted/retired
    record created BEFORE ``earliest`` is legacy-human (a numerator hit when retired) --
    residual contamination the design accepts, since a pre-stamp retired record might
    never have been accepted at all and looks identical by canonical fields. Created ON
    OR AFTER ``earliest``, it is excluded instead: stamping was already live when the
    record was created, so a stamp-less accepted record bypassed the ratify path
    entirely and is neither auto- nor human-ratified.

    Deliberately NOT widened by the store's creation marker (``stamping_live_since``),
    unlike ``_check_unratified_accepts`` -- considered and rejected for this doctor-blind-
    window fix, and left as a KNOWN, RECORDED inconsistency, not a closed question (review
    round 2, Minor 3 -- the round-1 justification below was incomplete, not wrong: it only
    covered PRINTING, not CLASSIFICATION).

    For PRINTING: this audit's own print gate already requires at least one genuine
    ``auto:``-stamped record to exist anywhere before it prints a single line
    (``_auto_share_lines``), so a store that has merely adopted stamping without ever
    actually stamping anything stays silent either way -- there is no blind window in
    WHETHER the lines print.

    For CLASSIFICATION, there is one, and it is live today: measured (fix round 2) -- take
    a new store (marker present, `_check_unratified_accepts` armed from creation), one
    ``propose(auto_accept=True)`` bypass record, and one LATER decision that gets
    auto-ratified (arming this audit's own print gate). The bypass record predates the
    store's earliest actual stamp (that later ratification), so THIS function classifies
    it as legacy-human -- `auto share: decisions 1/2 (50%) ... vs human 0/1`, where it
    lands in ``human_total``, the supersede rate's DENOMINATOR (fix round 2 re-review,
    Minor C: an earlier wording called this "a numerator hit", which it is not -- the
    record is accepted, not retired, and only a retired record reaches a numerator) -- in
    the exact same ``curate()`` run where `_check_unratified_accepts` flags that
    SAME record as an unratified accept. Two checks in one report disagreeing about the
    same record is a real inconsistency, not a hypothetical one.

    The ruling stands anyway: folding the marker into `earliest` here would additionally
    misdescribe this function's own "excluded" bucket, whose printed wording is anchored to
    a real, OBSERVED event ("after the first stamp excluded") -- the marker only proves
    stamping CAPABILITY existed, not that any stamping ever ran, so reusing it here needs
    new wording, not just a wider `earliest`, which stays out of scope for a fix this
    function did not have a BUG in (D5's numbers are informational, not a security signal).
    The inconsistency above is the documented cost of that scope decision, not evidence
    against it."""
    retired = _RETIRED_STATUSES[kind]
    accepted_or_retired = {DecisionStatus.ACCEPTED.value} | retired
    # _AutoShareTally is frozen -- accumulate in plain locals first, build it once at the
    # end.
    auto_total = 0
    auto_retired = 0
    human_total = 0
    human_retired = 0
    excluded = 0
    for record_id, rec in records.items():
        status = rec.get("status")
        if _parse_aware_iso(rec.get("ratified_at")) is not None:
            if _is_auto_stamped(rec):
                auto_total += 1
                if status in retired:
                    auto_retired += 1
            else:
                human_total += 1
                if status in retired:
                    human_retired += 1
            continue
        if status not in accepted_or_retired:
            continue  # ignored: proposed / rejected / deprecated -- not classifiable
        created = (
            _ulid_datetime(record_id)
            if kind == "domains"
            else _parse_aware_iso(rec.get("valid_from"))
        )
        if created is None or created >= earliest:
            excluded += 1
            continue
        human_total += 1  # legacy-human: unstamped, predates observable stamping
        if status in retired:
            human_retired += 1
    return _AutoShareTally(
        auto_total=auto_total,
        auto_retired=auto_retired,
        human_total=human_total,
        human_retired=human_retired,
        excluded=excluded,
    )


def _fmt_share(n: int, d: int) -> str:
    """``n/d (p%)`` with an integer floor percentage, or ``n/a`` for a zero denominator
    (design D5: "zero records of a kind prints n/a, never a division")."""
    return f"{n}/{d} ({100 * n // d}%)" if d else "n/a"


def _auto_share_lines(store_dir: str | Path) -> list[str]:
    """The two D5 informational lines (``auto share`` / ``auto supersede rate``), or
    ``[]`` when no ``auto:``-prefixed stamp exists anywhere (hot plus archive, any kind)
    -- the print gate that keeps a manual deployment's doctor output byte-identical (D4:
    "manual deployments see zero render diff"). Pure read: no ``Finding``, no
    ``Store()``, no write.
    # see design/superpowers/specs/2026-09-11-auto-ratification-policy-design.md D5
    """
    records = _hot_plus_archive_records(Path(store_dir))
    all_records = [rec for kind_records in records.values() for rec in kind_records.values()]
    if not any(_is_auto_stamped(rec) for rec in all_records):
        return []
    earliest = min(
        parsed
        for rec in all_records
        if (parsed := _parse_aware_iso(rec.get("ratified_at"))) is not None
    )
    tallies = {kind: _tally_kind(kind, records[kind], earliest) for kind in _RETIRED_STATUSES}
    excluded = sum(t.excluded for t in tallies.values())

    share = ", ".join(
        f"{kind} {_fmt_share(t.auto_total, t.auto_total + t.human_total)}"
        for kind, t in tallies.items()
    )
    df_auto_n = tallies["decisions"].auto_retired + tallies["facts"].auto_retired
    df_auto_d = tallies["decisions"].auto_total + tallies["facts"].auto_total
    df_human_n = tallies["decisions"].human_retired + tallies["facts"].human_retired
    df_human_d = tallies["decisions"].human_total + tallies["facts"].human_total
    dom = tallies["domains"]

    return [
        f"auto share: {share} ({excluded} stamp-less record(s) after the first stamp excluded)",
        "auto supersede rate: decisions+facts superseded "
        f"{_fmt_share(df_auto_n, df_auto_d)} vs human {_fmt_share(df_human_n, df_human_d)}; "
        f"domains retired {_fmt_share(dom.auto_retired, dom.auto_total)} vs human "
        f"{_fmt_share(dom.human_retired, dom.human_total)} "
        f"({excluded} stamp-less record(s) after the first stamp excluded)",
    ]


def curate(
    store_dir: str | Path,
    *,
    stale_days: int = 30,
    now: datetime | None = None,
    repo_root: Path | None = None,
) -> CurationReport:
    """Run every advisory curation check over the store at ``store_dir``.

    Pure read (module docstring). ``now`` is injectable for tests and defaults to
    ``datetime.now(UTC)``. Findings are ordered by check (the spec's listing order),
    then by file path within each check — deterministic output.

    ``repo_root`` (design D5, additive): the git repository ``code-drift`` diffs against.
    ``None`` (the default -- ``sidegraph-doctor`` passes nothing) resolves it the same way
    ``verify._find_repo_root`` does, from ``store_dir`` itself (the store may be nested
    inside the repo). Passing it explicitly is mainly a test seam -- it skips that
    resolution and any git-availability failure it could raise.
    # see design/superpowers/specs/2026-07-23-sidegraph-doctor-design.md
    # see design/superpowers/specs/2026-07-30-staleness-machinery-design.md (D5)
    """
    root = Path(store_dir)
    now = now or datetime.now(UTC)
    decisions = _iter_records(root, "decisions")
    facts = _iter_records(root, "facts")
    domains = _iter_records(root, "domains")
    entities = _iter_records(root, "entities")
    bindings = _binding_entries(root)

    # decision id -> status, HOT ids winning over archived ones (mirrors verify.py's own
    # `{**archive.decisions, **decisions_raw}` precedent) -- resolution must include archived
    # decisions too, not just hot ones: `compact` moves terminal decisions into
    # `archive/*.jsonl`, and without this a supports-only fact whose decision was archived
    # would misreport "does not exist" (D5's wording) about a decision that is sitting right
    # there. The verdict doesn't change (an archived decision is terminal by construction, so
    # such a fact is correctly reported either way) -- only the WORDING does, which is
    # exactly the failure D5's distinct wording exists to prevent.
    archived = _check_archive_dir(root / "archive")
    decision_status_by_id: dict[str, str] = {
        did: status
        for did, payload in archived.decisions.items()
        if isinstance(status := payload.get("status"), str)
    }
    decision_status_by_id.update(
        {
            d["id"]: status
            for _, d in decisions
            if isinstance(d.get("id"), str) and isinstance(status := d.get("status"), str)
        }
    )

    findings: list[Finding] = []
    findings += _check_dangling_records(decisions, facts, bindings, decision_status_by_id)
    status_findings, skipped = _check_binding_statuses(root)
    findings += status_findings
    findings += _check_stale_proposals(decisions, domains, stale_days, now)
    findings += _check_unreferenced_entities(entities, bindings)
    findings += _check_duplicate_entities(entities, bindings)
    findings += _check_expired_open(decisions, now)
    surfaced_findings, surfaced_skipped = _check_never_surfaced(root)
    findings += surfaced_findings
    skipped += surfaced_skipped
    findings += _check_unratified_accepts(decisions, facts, root, bindings, entities)
    # Appended LAST (design D5: "appended at the END of curate()'s listing order").
    findings += _check_code_drift(decisions, bindings, entities, root, repo_root)
    # Instructions files sit beside the store in the standard layout (`<repo>/.sidegraph`),
    # so the store's own parent is the lookup root. Deliberately NOT a git call: curate()'s
    # git-call count is pinned by test_curate_git_call_count_unchanged_by_refactor, and
    # this advisory check is not worth a subprocess on the hook paths. A store configured
    # outside its repository simply finds no instructions file — the check stays silent
    # rather than guessing where one might live.
    findings += _check_stale_instructions(decisions, repo_root or root.parent)
    return CurationReport(findings=findings, skipped=skipped)
