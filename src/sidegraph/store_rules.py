"""Pure, pydantic-free store rules: transition classification, key survival and the archive
relations. Shared by verify (``--against``), inventory and the maintenance planner.

# see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§5.3)
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast

from .store_format import Inventory, Occurrence
from .store_layout import LEGACY_DEFAULTS, hot_matches_archive, project_shared, same_modulo_absent

# Transition-layer codes (design ruling 2, "Transition layer" — see that section below).
ILLEGAL_FIELD_CHANGE = "illegal-field-change"
ILLEGAL_STATUS_JUMP = "illegal-status-jump"
VALID_TO_UNSET = "valid-to-unset"
VALID_TO_CHANGED = "valid-to-changed"
ILLEGAL_DELETION = "illegal-deletion"


@dataclass(frozen=True)
class Violation:
    """One integrity finding. ``path`` is always a string (a file path, or a synthetic
    ``dir/<id>.json`` for an id that only exists inside an archive segment)."""

    code: str
    path: str
    detail: str


def _violation(code: str, path: Path | str, detail: str) -> Violation:
    return Violation(code=code, path=str(path), detail=detail)


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
#   :func:`verify.verify_against` handles the type directly, deletion-only — see its docstring).
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
# drop/supersede all do this incidentally, not because the field itself changed.
# :func:`same_modulo_absent` treats an absent key as equal to ``null``/``[]``/``{}``,
# recursing into nested objects and into the items of a list, so this never reads as an
# ``illegal-field-change`` on its own; a key that actually changes value, or that is removed
# while holding a non-empty value, still does. A field whose default is a non-empty value is
# not covered by that rule (see :func:`same_modulo_absent`'s own docstring); it has its own
# narrow table, :data:`LEGACY_DEFAULTS`.

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
    file after its own id — see :data:`verify.FILENAME_ID_MISMATCH`); :func:`verify.verify_against`
    overwrites it with the actual changed git path regardless, so a mismatch here is never
    user-visible."""
    rid = payload.get(_KIND_ID_FIELDS[kind], "?")
    return f"{_KIND_DIRS[kind]}/{rid}.json"


def _check_status_transition(
    old: dict, new: dict, legal: frozenset[tuple[str, str]], path: str
) -> list[Violation]:
    old_status, new_status = old.get("status"), new.get("status")
    # A malformed NEW status is the snapshot layer's parse-error, so stay silent. A malformed
    # OLD status that is now a string is a repair of a value the ref never legitimised, so it
    # is a jump like any other unlisted pair (as for title or valid_to) -- but an unhashable
    # old value must never reach the ``legal`` lookup.
    if not isinstance(new_status, str):
        return []
    if old_status == new_status or (
        isinstance(old_status, str) and (old_status, new_status) in legal
    ):
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


def _absent_vs_default(old: dict, new: dict, key: str, default: object) -> bool:
    """One side lacks ``key`` and the other holds exactly its legacy ``default``: the reader
    loads both as that default, so neither a writer's backfill nor an older copy of the record
    beside a backfilled one is a change."""
    if key not in old:
        return key in new and new[key] == default
    if key not in new:
        return old[key] == default
    return False


def _check_immutable_fields(
    old: dict,
    new: dict,
    mutable: frozenset[str],
    path: str,
    exempt: tuple[str, ...] = (),
    defaults: dict[str, str] | None = None,
) -> list[Violation]:
    """Every key present in ``old`` or ``new`` but not in ``mutable`` or ``exempt`` must be
    the same value on both sides (:func:`same_modulo_absent`, Rule B — an absent key counts as
    equal to ``null`` or an empty list/object) — covers a changed value, a field that
    disappeared, AND a field that newly appeared (all three are "this immutable field
    changed"). One violation per offending field (design ruling 2: "detail names the
    field"), in sorted key order for a deterministic report.
    ``exempt`` (the ratifier stamp, for decisions/facts/domains) is
    skipped here entirely — its own rule is :func:`_check_ratifier_stamp`, run alongside this
    one by :func:`classify_transition`, never by this function.
    ``defaults`` (:data:`LEGACY_DEFAULTS` for the kind) allows a key absent on one side to hold
    exactly its schema default on the other (:func:`_absent_vs_default`)."""
    violations: list[Violation] = []
    for key in sorted(set(old) | set(new)):
        if key in mutable or key in exempt:
            continue
        if defaults and key in defaults and _absent_vs_default(old, new, key, defaults[key]):
            continue
        if not same_modulo_absent(old.get(key), new.get(key)):
            violations.append(_violation(ILLEGAL_FIELD_CHANGE, path, f"field {key!r} changed"))
    return violations


def classify_transition(kind: str, old: dict | None, new: dict | None) -> list[Violation]:
    """Classify one record's before/after payload against the store's OWN write-path rules
    (design ruling 2, "Transition layer"). Pure: no filesystem/git access, so every case is
    directly unit-testable without a git repo — :func:`verify.verify_against` is the thin,
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
      tree) is applied by :func:`verify.verify_against` as a post-hoc filter over this function's
      raw output, never by this function itself. A pure test exercising ``new=None``
      directly is therefore always the "id absent from any archive" scenario, by
      construction.

    Neither payload is ever a real filesystem/git path, so a returned :class:`Violation`'s
    ``path`` is a best-effort ``<dir>/<id>.json`` guess (:func:`_synthetic_path`) that
    :func:`verify.verify_against` overwrites with the actual changed path.
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
            + _check_immutable_fields(
                old, new, mutable, path, exempt=_RATIFIER_STAMP, defaults=LEGACY_DEFAULTS.get(kind)
            )
            + _check_ratifier_stamp(old, new, path)
        )
    if kind == "domain":
        return (
            _check_status_transition(old, new, _DOMAIN_STATUS_TRANSITIONS, path)
            + _check_immutable_fields(old, new, DOMAIN_MUTABLE_FIELDS, path, exempt=_RATIFIER_STAMP)
            + _check_ratifier_stamp(old, new, path)
        )
    # entity
    return _check_immutable_fields(
        old, new, ENTITY_MUTABLE_FIELDS, path, defaults=LEGACY_DEFAULTS["entity"]
    )


def kind_of(record_type: str) -> str:
    """The :func:`classify_transition` ``kind`` of a store ``record_type`` (``bindings`` is
    the one plural spelling); every other type is its own kind.

    # see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§5.3)"""
    return "binding" if record_type == "bindings" else record_type


def is_derived_entity_body(body: object) -> bool:
    """True iff an entity body is engine-volatile derived state: a Tier-1 community abstract
    entity (``kind`` ``abstract``, ``canonical_name`` starting ``community:``). Pure-dict twin
    of ``Store._is_derived_entity``.

    # see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§5.3)"""
    if not isinstance(body, dict):
        return False
    name = body.get("canonical_name")
    return (
        body.get("kind") == "abstract" and isinstance(name, str) and name.startswith("community:")
    )


# Sentinel for "no surviving body" in :func:`keeps_keys` (``None`` is a legal JSON value).
ABSENT: Final = object()


def _never_derived(_entity_id: str) -> bool:
    return False


def keeps_keys(
    x: object,
    y: object,
    *,
    bindings: bool = False,
    is_derived: Callable[[str], bool] = _never_derived,
) -> bool:
    """Whether destroying body ``x`` while ``y`` survives loses no key: objects by key at any
    depth, lists by position (scalar positions count), a bindings list by ``entity_id`` against
    the LAST entry of ``y`` with that id, derived entries excluded. Values may change; keys,
    objects inside lists and bindings entries may not disappear.

    # see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§5.3)"""
    if bindings and isinstance(x, list):
        return _bindings_keep(x, y, is_derived)
    if y is ABSENT:
        return False
    return _walk(x, y)


def _bindings_keep(x: list, y: object, is_derived: Callable[[str], bool]) -> bool:
    entries = [e for e in x if not (isinstance(e, dict) and _derived_entry(e, is_derived))]
    if not entries:
        return True
    if not isinstance(y, list):  # ABSENT, or a body of the wrong shape
        return False
    last: dict[str, object] = {}
    for entry in y:
        if isinstance(entry, dict) and isinstance(entry.get("entity_id"), str):
            last[entry["entity_id"]] = entry
    for entry in entries:
        eid = entry.get("entity_id") if isinstance(entry, dict) else None
        if not isinstance(eid, str) or eid not in last or not _walk(entry, last[eid]):
            return False
    return True


def _derived_entry(entry: dict, is_derived: Callable[[str], bool]) -> bool:
    eid = entry.get("entity_id")
    return isinstance(eid, str) and is_derived(eid)


def _walk(x: object, y: object) -> bool:
    """Every key and list position of ``x`` is in ``y`` at any depth (iterative); a scalar may
    change value."""
    stack = [(x, y)]
    while stack:
        a, b = stack.pop()
        if isinstance(a, dict):
            if not isinstance(b, dict):
                return False
            for k, v in a.items():
                if k not in b:
                    return False
                if isinstance(v, (dict, list)):
                    stack.append((v, b[k]))
        elif isinstance(a, list):
            if not isinstance(b, list) or len(b) < len(a):
                return False
            stack.extend((v, b[i]) for i, v in enumerate(a) if isinstance(v, (dict, list)))
    return True


def _empty(value: object) -> bool:
    return value is None or value == [] or value == {}


def compatible(kind: str, a: dict, b: dict) -> bool:
    """``b`` holds archive payload ``a``'s state or a legal successor of it. A legacy-default
    key ``a`` lacks is not a newer version's extra key in ``b``: ``a`` holds that default, so
    ``b`` holding any other value is a real change, never projected away.

    # see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§5.3)"""
    for key, default in LEGACY_DEFAULTS.get(kind, {}).items():
        if key not in a and key in b and b[key] != default:
            return False
    shared = cast(dict, project_shared(b, a))
    if hot_matches_archive(shared, a, LEGACY_DEFAULTS.get(kind)):
        return True
    return not classify_transition(kind, a, shared) and all(
        k in b for k, v in a.items() if not _empty(v)
    )


def superseding(kind: str, a: dict, s: dict) -> bool:
    """Archive payload ``a`` is a strict legal successor of shard body ``s``.

    # see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§5.3)"""
    return not classify_transition(kind, s, a) and not compatible(kind, a, s)


def without_legacy_defaults(kind: str, a: object, s: object) -> object:
    """``a`` minus each top-level key ``s`` lacks while ``a`` holds exactly its
    :data:`LEGACY_DEFAULTS` value: removing such a key loses nothing, because the reader loads
    the absent key as that same default. Anything but two dicts comes back unchanged."""
    if not isinstance(a, dict) or not isinstance(s, dict):
        return a
    defaults = LEGACY_DEFAULTS.get(kind, {})
    return {k: v for k, v in a.items() if not (k not in s and k in defaults and defaults[k] == v)}


def same_modulo_legacy_defaults(kind: str, a: object, b: object) -> bool:
    """Equal bodies once a :data:`LEGACY_DEFAULTS` key one side lacks is read as the default the
    other side holds (strict JSON equality otherwise): an older copy and its backfilled twin."""
    if not isinstance(a, dict) or not isinstance(b, dict):
        return _strict_equal(a, b)
    defaults = LEGACY_DEFAULTS.get(kind, {})
    a2 = {**{k: v for k, v in defaults.items() if k not in a and b.get(k) == v}, **a}
    b2 = {**{k: v for k, v in defaults.items() if k not in b and a.get(k) == v}, **b}
    return _strict_equal(a2, b2)


def deletable(kind: str, a: dict, s: dict) -> bool:
    """Removing archive payload ``a`` loses no key: ``s`` is compatible and keeps every key
    (:func:`without_legacy_defaults`).

    # see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§5.3)"""
    return compatible(kind, a, s) and keeps_keys(without_legacy_defaults(kind, a, s), s)


# -- the effective record ------------------------------------------------------------------


@dataclass(frozen=True)
class Effective:
    """The effective record of one identity. ``raw`` is its parsed body, ``source`` the
    occurrence it came from. ``raw`` is ``None`` when there is no record: ``reason`` is then
    ``unreadable``, ``conflict`` or ``duplicate-packed-record`` when rule 1 refused the
    identity, and ``None`` when nothing readable exists at all.

    # see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§5.3)"""

    raw: object | None
    source: Occurrence | None
    reason: str | None


def _strict_equal(a: object, b: object) -> bool:
    """Parsed JSON values equal with their JSON types: ``1``, ``1.0`` and ``true`` differ
    (Python's ``==`` would equate them). Needs no UTF-8 encoding, so a body holding an escaped
    lone surrogate compares like any other. Iterative: any depth the JSON reader accepts."""
    stack = [(a, b)]
    while stack:
        x, y = stack.pop()
        if type(x) is not type(y):
            return False
        if isinstance(x, dict):
            assert isinstance(y, dict)
            if x.keys() != y.keys():
                return False
            stack.extend((v, y[k]) for k, v in x.items())
        elif isinstance(x, list):
            assert isinstance(y, list)
            if len(x) != len(y):
                return False
            stack.extend(zip(x, y, strict=True))
        elif x != y:
            return False
    return True


def _same_body(a: Occurrence, b: Occurrence) -> bool:
    """Equal parsed bodies, whatever the line's whitespace or key order."""
    return _strict_equal(a.raw, b.raw)


def archive_candidate(occs: list[Occurrence]) -> Occurrence | None:
    """The archive candidate of an identity: the first segment's readable line of a known
    type, segments in sorted path order and lines in file order, as ``Store._archived_records``
    ("first occurrence wins").

    # see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§5.3 rule 3)"""
    lines = [o for o in occs if o.kind == "archive" and o.archive_type_known and o.readable]
    lines.sort(key=lambda o: (o.path, o.line_no or 0))
    return lines[0] if lines else None


def _effective_of(record_type: str, occs: list[Occurrence]) -> Effective:
    files = [o for o in occs if o.kind == "file"]
    shards = [o for o in occs if o.kind == "shard"]
    # Rule 1: an unreadable or conflicted file or shard line, or two differing shard bodies,
    # leaves the identity with no record; there is no fall-through to a stale copy.
    packed_or_file = files + shards
    if any(o.in_conflict for o in packed_or_file):
        return Effective(None, None, "conflict")
    if any(not o.readable for o in packed_or_file):
        return Effective(None, None, "unreadable")
    distinct: list[Occurrence] = []
    for occ in shards:
        if not any(_same_body(occ, seen) for seen in distinct):
            distinct.append(occ)
    if len(distinct) > 1:
        return Effective(None, None, "duplicate-packed-record")
    # Rules 2-3: unreadable and unknown-type archive lines are dropped; the first segment's
    # readable line is the candidate.
    candidate = archive_candidate(occs)
    # Rule 4: a file wins; else a shard line, unless the archive candidate supersedes it.
    if files:
        return Effective(files[0].raw, files[0], None)
    if distinct:
        shard = distinct[0]
        if candidate is not None and _archive_supersedes(record_type, occs, candidate, shard):
            return Effective(candidate.raw, candidate, None)
        return Effective(shard.raw, shard, None)
    if candidate is not None:
        return Effective(candidate.raw, candidate, None)
    return Effective(None, None, None)


def _archive_supersedes(
    record_type: str, occs: list[Occurrence], candidate: Occurrence, shard: Occurrence
) -> bool:
    """Whether the archive candidate is ``superseding`` the shard body. No test is run when
    readable archive lines of the identity differ (rule 3)."""
    for other in occs:
        if (
            other is not candidate
            and other.kind == "archive"
            and other.archive_type_known
            and other.readable
            and not _same_body(other, candidate)
        ):
            return False
    if not isinstance(candidate.raw, dict) or not isinstance(shard.raw, dict):
        return False
    return superseding(kind_of(record_type), candidate.raw, shard.raw)


def effective(inv: Inventory, record_type: str, record_id: str) -> Effective:
    """The effective record of ``(record_type, record_id)``: rules 1-4 of the spec, in order.
    Scans the inventory once; use :func:`effective_all` for many identities.

    # see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§5.3)"""
    occs = [o for o in inv.occurrences if o.record_type == record_type and o.record_id == record_id]
    return _effective_of(record_type, occs)


def effective_all(inv: Inventory) -> dict[tuple[str, str], Effective]:
    """The effective record of every identity in the inventory, quarantined ones (``raw`` is
    ``None``) included.

    # see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§5.3)"""
    return {
        identity: _effective_of(identity[0], occs) for identity, occs in inv.by_identity().items()
    }


# -- the occurrence diff (``--against``) ---------------------------------------------------


def same_raw_body(a: Occurrence, b: Occurrence) -> bool:
    """Two occurrences hold the same raw body: equal parsed JSON with its types, or, when
    neither parsed, equal bytes. Whitespace and key order of a line do not matter, and an
    archive line is compared without its ``record_type`` (the inventory already drops it).

    # see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§7.1)"""
    if a.parseable and b.parseable:
        return _strict_equal(a.raw, b.raw)
    return a.parseable == b.parseable and a.raw_bytes == b.raw_bytes


@dataclass(frozen=True)
class IdentityDiff:
    """What changed for one ``(record_type, record_id)`` between REF and the new tree.

    ``ref`` and ``new`` are all of its occurrences on each side. ``removed`` are REF occurrences
    with no counterpart: a deleted file, or a shard or archive line whose body is gone from its
    ``(path, type, id)``. ``modified`` pairs a per-record file with its new body (same path,
    different raw body). ``added`` are the new occurrences with no counterpart, the new side of
    each modified file included. ``unchanged`` are the new occurrences that have one.
    ``destroyed`` is every body that stopped existing: ``removed`` plus the REF side of each
    modified file.

    # see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§7.1)"""

    identity: tuple[str, str]
    ref: list[Occurrence]
    new: list[Occurrence]
    removed: list[Occurrence]
    added: list[Occurrence]
    modified: list[tuple[Occurrence, Occurrence]]
    unchanged: list[Occurrence]

    @property
    def destroyed(self) -> list[Occurrence]:
        return [*self.removed, *(old for old, _ in self.modified)]

    @property
    def changed(self) -> bool:
        return bool(self.removed or self.added)


@dataclass(frozen=True)
class OccurrenceDiff:
    """The per-identity diffs of two inventories, in REF-then-new order. Occurrences with no
    identity (an unrecoverable shard line, an archive line of an unknown type) are in no diff.

    # see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§7.1)"""

    identities: dict[tuple[str, str], IdentityDiff]


def diff_occurrences(ref_inv: Inventory, new_inv: Inventory) -> OccurrenceDiff:
    """Compare two inventories. A file is keyed by ``path``; a shard or archive line by
    ``(path, type, id)`` and compared as a multiset of raw bodies, so two lines of one identity
    in one file are two occurrences (the ``k`` of ``(path, type, id, k)`` only orders them).
    Bodies in both are unchanged, the rest removed or added.

    # see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§7.1)"""
    ref_by = ref_inv.by_identity()
    new_by = new_inv.by_identity()
    out: dict[tuple[str, str], IdentityDiff] = {}
    for identity in [*ref_by, *(i for i in new_by if i not in ref_by)]:
        ref_occs = ref_by.get(identity, [])
        new_occs = new_by.get(identity, [])
        removed: list[Occurrence] = []
        added: list[Occurrence] = []
        modified: list[tuple[Occurrence, Occurrence]] = []
        unchanged: list[Occurrence] = []
        slots: dict[tuple[str, str], tuple[list[Occurrence], list[Occurrence]]] = {}
        for occ in ref_occs:
            slots.setdefault((occ.kind, occ.path), ([], []))[0].append(occ)
        for occ in new_occs:
            slots.setdefault((occ.kind, occ.path), ([], []))[1].append(occ)
        for (kind, _path), (olds, news) in slots.items():
            if kind == "file":
                if olds and news and same_raw_body(olds[0], news[0]):
                    unchanged.append(news[0])
                elif olds and news:
                    modified.append((olds[0], news[0]))
                else:
                    (removed if olds else added).extend(olds or news)
                continue
            pending = list(olds)
            for occ in news:
                match = next((o for o in pending if same_raw_body(o, occ)), None)
                if match is None:
                    added.append(occ)
                else:
                    pending.remove(match)
                    unchanged.append(occ)
            removed.extend(pending)
        added.extend(new for _, new in modified)
        out[identity] = IdentityDiff(
            identity, ref_occs, new_occs, removed, added, modified, unchanged
        )
    return OccurrenceDiff(out)
