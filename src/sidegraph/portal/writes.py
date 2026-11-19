"""Portal verdict and version writes (D6–D8)."""

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from typing import Literal

from pydantic import JsonValue, ValidationError
from ulid import ULID

from sidegraph import server
from sidegraph.capture import redact
from sidegraph.engine.reader import GraphifyReader
from sidegraph.input_limits import InputLimitError, preflight_direct
from sidegraph.retrieval import TOC_CACHE_KEY, build_toc
from sidegraph.schema import AnchorBinding, Decision, Domain, Fact
from sidegraph.store import (
    Store,
    StoreBusyError,
    SupersessionConflictError,
    UnknownSupportError,
    _is_derived_entity,
    settled_ratify_result,
)
from sidegraph.sync import ensure_domain_membership
from sidegraph.validation_errors import SafeWriteError

from .contract import ApplyResult, OperationPart, OperationResult
from .refusals import portal_message

_BUSY = "Store is busy. Earlier steps may have written records; inspect the store before retrying."


def _is_nested_evidence(store: Store, fact: Fact) -> bool:
    return fact.status == "proposed" and any(
        (parent := store.get_decision(sid)) is not None and parent.status == "proposed"
        for sid in fact.supports
    )


def _nested_marks(store: Store, marked: set[str]) -> set[str]:
    return {
        fact.id
        for fact in store.iter_proposed_facts()
        if fact.id in marked and _is_nested_evidence(store, fact)
    }


def _exclusive_evidence(store: Store, fact: Fact, decision_id: str) -> bool:
    others = [id_ for id_ in fact.supports if id_ != decision_id]
    return not others or store._all_supporters_rejected(
        fact.model_copy(update={"supports": others})
    )


def _sqlite_busy(exc: sqlite3.OperationalError) -> bool:
    code = getattr(exc, "sqlite_errorcode", 0)
    return isinstance(exc, StoreBusyError) or code & 0xFF in (
        sqlite3.SQLITE_BUSY,
        sqlite3.SQLITE_LOCKED,
    )


def _batch_evidence(store: Store, marked: set[str]) -> dict[str, list[str]]:
    return {
        fact.id: list(dict.fromkeys(id_ for id_ in fact.supports if id_ in marked))
        for fact in store.iter_proposed_facts()
        if marked.intersection(fact.supports)
    }


class _RatificationRefused(Exception):
    """Roll back a settled refusal within a reserved verdict."""


def apply_verdicts(
    store: Store, reader: GraphifyReader | None, accept: list[str], reject: list[str]
) -> ApplyResult:
    """Apply the whole batch using the human verdict path.

    See design/superpowers/specs/2026-11-17-portal-design.md (D6).
    """
    nested = _nested_marks(store, set(accept) | set(reject))
    accept = [id_ for id_ in accept if id_ not in nested]
    reject = [id_ for id_ in reject if id_ not in nested]
    actions = dict.fromkeys(reject, "drop") | dict.fromkeys(accept, "accept")
    before = {id_: _get_record(store, id_) or store.get_domain(id_) for id_ in actions}
    evidence: dict[str, list[str]] = {}
    interrupted: set[str] = set()
    reads_blocked = False
    domain_ids = {id_ for id_, item in before.items() if isinstance(item, Domain)}
    busy = {"outcome": "busy", "error": {"code": "busy", "message": _BUSY, "details": {}}}
    outcomes: dict[str, dict] = {id_: {"outcome": "stays"} for id_ in sorted(nested)}
    raw: dict[str, str] = {}
    # Match the ratifier's decision-first accept order. Reserve separately for each
    # verdict so a later busy item leaves the settled prefix in place.
    first = [id_ for id_ in accept if isinstance(before[id_], Decision)]
    try:
        for id_ in dict.fromkeys([*first, *accept, *reject]):
            interrupted = set()
            if id_ in raw:
                continue
            with _reservation(store):
                current = _get_record(store, id_) or store.get_domain(id_)
                if isinstance(current, Fact) and _is_nested_evidence(store, current):
                    outcomes[id_] = {"outcome": "stays"}
                    continue
                action = actions[id_]
                if isinstance(current, Domain):
                    domain_ids.add(id_)
                else:
                    domain_ids.discard(id_)
                if isinstance(current, Decision) and current.status == "proposed":
                    for fact in store.facts_for_decision(id_):
                        if fact.status != "proposed":
                            continue
                        evidence[fact.id] = list(
                            dict.fromkeys(s for s in fact.supports if s in actions)
                        )
                        if action == "accept" or _exclusive_evidence(store, fact, id_):
                            interrupted.add(fact.id)
                try:
                    with store._savepoint():
                        verdict = server._ratify_impl(
                            store,
                            [id_] if action == "accept" else [],
                            [id_] if action == "drop" else [],
                            reader,
                            relayed=False,
                            drop_needs_verdict=True,
                        )
                        result = verdict[id_]
                        if (
                            current is not None
                            and current.status == "proposed"
                            and result.startswith("already ")
                        ):
                            # A nested store write can raise after updating the index. Its
                            # settled re-read saw that uncommitted change under our reservation.
                            verdict[id_] = result = server._RATIFY_FAILED
                        if result.startswith("error:"):
                            raise _RatificationRefused
                except _RatificationRefused:
                    pass
                raw.update(verdict)
                if verdict[id_].startswith("already ") and isinstance(current, Domain):
                    store.set_meta(TOC_CACHE_KEY, json.dumps(build_toc(store, reader)))
    except sqlite3.OperationalError as exc:
        if not _sqlite_busy(exc):
            raise
        try:
            store.refresh_if_stale()
        except sqlite3.OperationalError as refresh_error:
            if not _sqlite_busy(refresh_error):
                raise
            reads_blocked = True
        domains_changed = any(
            id_ in domain_ids and result.startswith(("accepted", "dropped"))
            for id_, result in raw.items()
        )
        for id_, action in actions.items():
            if id_ in raw or id_ in outcomes:
                continue
            if not reads_blocked:
                try:
                    record = _get_record(store, id_) or store.get_domain(id_)
                except sqlite3.OperationalError as read_error:
                    if not _sqlite_busy(read_error):
                        raise
                    reads_blocked = True
            if reads_blocked:
                outcomes[id_] = busy.copy()
                continue
            original = before[id_]
            if record is None:
                raw[id_] = "error: unknown id"
            elif record.status == "proposed":
                outcomes[id_] = busy.copy()
            else:
                kind = (
                    "domain"
                    if isinstance(record, Domain)
                    else ("fact" if isinstance(record, Fact) else "decision")
                )
                if kind == "domain":
                    domain_ids.add(id_)
                    if isinstance(original, Domain) and record.status != original.status:
                        domains_changed = True
                if kind == "domain" and record.status == "accepted" and action == "drop":
                    raw[id_] = "error: domain was accepted in the meantime"
                else:
                    settled = settled_ratify_result(kind, record.status, action)
                    raw[id_] = (
                        settled
                        if original is not None and original.status == record.status
                        else settled.removeprefix("already ")
                    )
        if domains_changed and not reads_blocked:
            if reader is not None:
                with suppress(Exception):
                    ensure_domain_membership(store, reader)
            try:
                store.set_meta(TOC_CACHE_KEY, json.dumps(build_toc(store, reader)))
            except sqlite3.OperationalError as cache_error:
                if not _sqlite_busy(cache_error):
                    raise
    else:
        interrupted.clear()
    for id_, result in raw.items():
        if result.startswith("error:"):
            reason, message = portal_message(result)
            if (
                reason in {"not_current", "pending_proposal", "pending_successor"}
                and result.startswith("error: fact ")
                and isinstance(before.get(id_), Decision)
            ):
                reason = "evidence_conflict"
            outcomes[id_] = {
                "outcome": "refused",
                "error": {"code": "refused", "message": message, "details": {"reason": reason}},
            }
            continue
        outcome = result.split(" (", 1)[0].replace(" ", "_")
        if outcome == "dropped" and id_ not in domain_ids:
            outcome = "rejected"
        outcomes[id_] = {"outcome": outcome}
    for id_, supporters in evidence.items():
        if id_ not in outcomes:
            if id_ in interrupted:
                persisted_fact = None
                if not reads_blocked:
                    try:
                        persisted_fact = store.get_fact(id_)
                    except sqlite3.OperationalError as read_error:
                        if not _sqlite_busy(read_error):
                            raise
                        reads_blocked = True
                outcomes[id_] = (
                    {"outcome": persisted_fact.status}
                    if persisted_fact and persisted_fact.status in ("accepted", "rejected")
                    else busy.copy()
                )
            else:
                outcomes[id_] = {"outcome": "stays"}
        outcomes[id_]["evidence_of"] = supporters
    return ApplyResult.model_validate({"outcomes": outcomes})


class EditRefusal(ValueError):
    """A display-safe refusal before any version-write step (D7–D8)."""

    def __init__(self, code: str, message: str, **details: JsonValue) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


_DECISION_FIELDS = {"title", "kind", "layer", "context", "choice", "rejected", "consequences"}
_FACT_FIELDS = {"statement", "source"}
_OPTIONAL_FIELDS = {"layer", "rejected", "consequences"}
_CONFLICT_MESSAGES = {
    "not_current": "This record is no longer current.",
    "pending_successor": "Decide on the pending replacement in the queue first.",
    "pending_proposal": "This record is still a proposal. Decide on it in the queue first.",
    "evidence_conflict": "The evidence changed. Review it before trying again.",
}
_Mode = Literal["edit", "edit-and-accept", "save-as-new"]
_IN_PROGRESS = "Another save is already in progress for this draft."
_REUSED_KEY = "This save was already used for a different request."


def _redacted_values(values: dict[str, str | None]) -> dict[str, str | None]:
    return {
        name: redact(value)[0] if value is not None and name not in {"kind", "layer"} else value
        for name, value in values.items()
    }


def _fields(
    store: Store,
    record: Decision | Fact,
    changes: dict,
    *,
    carry_facts: list[str] | None = None,
) -> dict[str, str | None]:
    """Resolve validated fields and check for a change when the carry list is supplied."""
    allowed = _DECISION_FIELDS if isinstance(record, Decision) else _FACT_FIELDS
    for name, value in changes.items():
        if name not in allowed:
            raise EditRefusal("invalid", "A field cannot be edited on this record.")
        if value is None and name in _OPTIONAL_FIELDS:
            continue
        if not isinstance(value, str) or (name not in _OPTIONAL_FIELDS and not value.strip()):
            raise EditRefusal("invalid", "Enter a value for this field.", field=name)
        if name == "layer" and value not in ("business", "technical"):
            raise EditRefusal("invalid", "Choose a valid layer.", field=name)
    current = record.model_dump(mode="json", include=allowed)
    values = current | changes
    try:
        preflight_direct(*values.values())
        redacted = _redacted_values(values)
        type(record).model_validate(record.model_dump() | redacted)
    except InputLimitError:
        raise EditRefusal("invalid", "Input exceeds resource limits.") from None
    except ValidationError as exc:
        field = next(
            (error["loc"][0] for error in exc.errors(include_input=False) if error["loc"]), None
        )
        details = {"field": field} if isinstance(field, str) and field in allowed else {}
        raise EditRefusal("invalid", "Enter a valid value.", **details) from None
    if (
        carry_facts is not None
        and record.status == "accepted"
        and record.valid_to is None
        and redacted == current
    ):
        facts = store.facts_for_decision(record.id) if isinstance(record, Decision) else []
        if set(carry_facts) == {f.id for f in facts if f.status == "accepted"} and not any(
            f.status == "proposed" for f in facts
        ):
            _check_predecessor(store, record)
            raise EditRefusal(
                "refused",
                "Nothing changed. To confirm that this record still holds, "
                "record a drift check instead.",
                reason="no_change",
            )
    return values


@dataclass
class _Version:
    id: str
    source: Decision | Fact
    predecessor_id: str | None
    fields: dict[str, str | None]
    supports: list[str] | None = None
    inherit: bool = False


def _bindings(store: Store, record_id: str) -> list[AnchorBinding]:
    bindings = store.bindings_for_record(record_id)
    if store.is_skipped("bindings", record_id) or any(
        store.get_entity(binding.entity_id) is None for binding in bindings
    ):
        raise EditRefusal("refused", "The record's anchors are unavailable.", reason="bindings")
    return bindings


def _check_predecessor(store: Store, predecessor: Decision | Fact, *, excluding: str = "") -> None:
    fact = isinstance(predecessor, Fact)
    store._check_supersede_target(
        "facts" if fact else "decisions",
        "id",
        "fact" if fact else "decision",
        excluding,
        "accepted",
        predecessor.id,
        predecessor.status,
        predecessor.valid_to is None,
    )


def _get_record(store: Store, record_id: str) -> Decision | Fact | None:
    return store.get_decision(record_id) or store.get_fact(record_id)


def _check_fact_supports(
    store: Store,
    supports: list[str],
    *,
    inherited_binding: bool,
    planned_decision: str | None = None,
) -> None:
    existing = [sid for sid in supports if sid != planned_decision]
    if server._unknown_support_positions(store, existing):
        raise EditRefusal("invalid", "A supporting record is unavailable.", field="supports")
    if not inherited_binding and planned_decision not in supports:
        try:
            server._require_fact_reachability(store, None, supports)
        except (SafeWriteError, UnknownSupportError):
            raise EditRefusal("invalid", "The fact needs an anchor or live support.") from None


def _plan_versions(
    store: Store,
    record: Decision | Fact,
    values: dict,
    evidence: list[str],
    planned: list[str],
    mode: _Mode,
    *,
    started: bool,
    author: str | None,
    completed: bool = False,
) -> list[_Version]:
    draft_edit = mode == "edit-and-accept"
    inherit = mode != "edit"
    if draft_edit and not (
        record.status == "proposed" or (started and record.status == "rejected")
    ):
        details = {"offer_save_as_new": True} if record.status == "rejected" else {}
        raise EditRefusal("not_current", "This draft is no longer proposed.", **details)
    if (
        draft_edit
        and isinstance(record, Fact)
        and any(
            (parent := store.get_decision(sid)) is not None and parent.status == "proposed"
            for sid in record.supports
        )
    ):
        raise EditRefusal(
            "refused",
            "Decide on this evidence with its proposal in the queue.",
            reason="nested_evidence",
        )
    if mode == "save-as-new" and record.status != "rejected":
        raise EditRefusal("not_current", "Only a rejected draft can be saved as a new record.")

    predecessor_id = record.id if mode == "edit" else record.supersedes if draft_edit else None
    supports = record.supports if isinstance(record, Fact) else None
    versions = [_Version(planned[0], record, predecessor_id, values, supports, inherit)]
    _check_version(store, versions[0], excluding=record.id if draft_edit else "", author=author)
    if (
        evidence
        and isinstance(record, Decision)
        and (published := store.get_decision(planned[0])) is not None
        and (published.status != "accepted" or published.valid_to is not None)
        and any(store.get_fact(id_) is None for id_ in planned[1:])
    ):
        _check_predecessor(store, published)

    if mode == "edit" and isinstance(record, Decision) and not completed:
        pending = [f.id for f in store.facts_for_decision(record.id) if f.status == "proposed"]
        if pending:
            raise EditRefusal(
                "pending_evidence",
                "Decide on this record's pending evidence in the queue first.",
                ids=list[JsonValue](pending),
                queue=True,
            )

    for fact_id, version_id in zip(evidence, planned[1:], strict=True):
        fact = store.get_fact(fact_id)
        message = _CONFLICT_MESSAGES["evidence_conflict"]
        if not isinstance(record, Decision) or fact is None or record.id not in fact.supports:
            raise EditRefusal("evidence_conflict", message, id=fact_id)
        try:
            if draft_edit:
                expected = "rejected" if record.status == "rejected" else "proposed"
                if not _exclusive_evidence(store, fact, record.id) or (
                    fact.status != expected
                    and not (expected == "rejected" and fact.status == "proposed")
                ):
                    raise EditRefusal("evidence_conflict", message)
                version = _Version(
                    version_id, fact, fact.supersedes, _fields(store, fact, {}), [planned[0]], True
                )
            else:
                supports = [planned[0] if sid == record.id else sid for sid in fact.supports]
                version = _Version(version_id, fact, fact.id, _fields(store, fact, {}), supports)
            _check_version(
                store,
                version,
                excluding=fact.id if draft_edit else "",
                planned_decision=planned[0],
                author=author,
            )
            versions.append(version)
        except SupersessionConflictError as exc:
            detail = {"pending": exc.pending} if exc.pending is not None else {}
            raise EditRefusal("evidence_conflict", message, id=fact_id, **detail) from None
        except EditRefusal:
            raise EditRefusal("evidence_conflict", message, id=fact_id) from None
    return versions


def _check_version(
    store: Store,
    version: _Version,
    *,
    excluding: str,
    author: str | None,
    planned_decision: str | None = None,
) -> None:
    bindings = _bindings(store, version.source.id)
    fact = isinstance(version.source, Fact)
    if version.predecessor_id is not None:
        siblings = store.iter_facts() if fact else store.iter_decisions()
        for sibling in siblings:
            if sibling.supersedes != version.predecessor_id or sibling.id == version.id:
                continue
            head = None
            if sibling.status == "accepted" and sibling.valid_to is None:
                head = sibling.id
            elif sibling.status == "superseded":
                head = store._live_head_after("facts" if fact else "decisions", "id", sibling.id)
            if head is not None:
                raise EditRefusal(
                    "not_current", "There is a competing version of this record.", head=head
                )
    if (published := _get_record(store, version.id)) is not None:
        values = published.model_dump(mode="json", include=set(version.fields))
        if (
            type(published) is not type(version.source)
            or published.supersedes != version.predecessor_id
            or published.provenance.source != "human"
            or published.status not in {"accepted", "superseded", "deprecated"}
            or values != _redacted_values(version.fields)
            or (isinstance(published, Fact) and published.supports != version.supports)
            or (
                isinstance(published, Decision)
                and isinstance(version.source, Decision)
                and published.scope != version.source.scope
            )
        ):
            raise EditRefusal("not_current", "The planned version changed on this branch.")
        return
    # Match the impl functions' admission of inherited values as well as edited text.
    inherit = version.source.id if version.inherit else None
    arguments: tuple[object, ...] = (
        *version.fields.values(),
        author,
        None,
        None,
        version.id,
        inherit,
    )
    if fact:
        arguments += (version.supports,)
    else:
        assert isinstance(version.source, Decision)
        arguments += (version.source.scope,)
        arguments += (None, None, None) if version.predecessor_id is None else ("human",)
    if version.predecessor_id is not None:
        arguments += (version.predecessor_id,)
    try:
        preflight_direct(*arguments)
    except InputLimitError:
        raise EditRefusal("invalid", "Input exceeds resource limits.") from None
    if version.predecessor_id is not None:
        get = store.get_fact if isinstance(version.source, Fact) else store.get_decision
        predecessor = get(version.predecessor_id)
        if predecessor is None:
            raise EditRefusal("not_current", "The record's predecessor is unavailable.")
        _check_predecessor(store, predecessor, excluding=excluding)
    if version.supports is not None:
        has_binding = bool(bindings)
        if version.inherit:
            has_binding = any(
                entity is not None and not _is_derived_entity(entity)
                for binding in bindings
                if (entity := store.get_entity(binding.entity_id)) is not None
            )
        _check_fact_supports(
            store,
            version.supports,
            inherited_binding=has_binding,
            planned_decision=planned_decision,
        )


def _publish_version(
    store: Store, reader: GraphifyReader | None, version: _Version, author: str | None
) -> None:
    arguments: dict = dict(version.fields, record_id=version.id, author=author)
    if version.inherit:
        arguments["inherit_from"] = version.source.id
    if isinstance(version.source, Decision):
        arguments["scope"] = version.source.scope
        if version.predecessor_id is None:
            server._add_decision_impl(store, reader, **arguments)
        else:
            server._supersede_decision_impl(
                store, reader, version.predecessor_id, **arguments, source="human"
            )
    elif version.predecessor_id is None:
        server._add_fact_impl(store, reader, **arguments, supports=version.supports)
    else:
        server._supersede_fact_impl(
            store, reader, version.predecessor_id, **arguments, supports=version.supports
        )


def _pending_cascade(store: Store, draft_id: str) -> list[Fact]:
    return [
        fact
        for fact in store.facts_for_decision(draft_id)
        if fact.status == "proposed" and store._all_supporters_rejected(fact)
    ]


def _reconcile_drop(store: Store, reader: GraphifyReader | None, draft: Decision | Fact) -> None:
    if draft.status == "proposed":
        result = server._ratify_impl(store, drop=[draft.id], reader=reader)
        if result.get(draft.id) != "dropped":
            raise RuntimeError("The draft was not rejected.")
    if isinstance(draft, Decision):
        for fact in _pending_cascade(store, draft.id):
            store.drop_fact(fact.id)


def _reconcile_version(
    store: Store, reader: GraphifyReader | None, version: _Version, author: str | None
) -> None:
    if _get_record(store, version.id) is None:
        _publish_version(store, reader, version, author)
        return
    if version.predecessor_id is not None:
        predecessor = _get_record(store, version.predecessor_id)
        if predecessor is not None and predecessor.status == "accepted":
            store.close_superseded_predecessor(version.id)
    server._copy_bindings(store, version.source.id, version.id)


def _operation_parts(
    store: Store, versions: list[_Version], draft_id: str | None
) -> dict[str, tuple[str, bool]]:
    parts: dict[str, tuple[str, bool]] = {}
    if draft_id is not None:
        draft = _get_record(store, draft_id)
        dropped = draft is not None and draft.status == "rejected"
        parts["drop_draft"] = (draft_id, dropped)
        if isinstance(versions[0].source, Decision):
            parts["cascade"] = (draft_id, dropped and not _pending_cascade(store, draft_id))
    for index, version in enumerate(versions):
        published = _get_record(store, version.id) is not None
        predecessor_id = version.predecessor_id
        closed = False
        if predecessor_id is not None and (previous := _get_record(store, predecessor_id)):
            closed = previous.status == "superseded"
        source_bindings = {
            (b.entity_id, b.tier, b.weight, b.relation)
            for b in store.bindings_for_record(version.source.id)
        }
        target_bindings = {
            (b.entity_id, b.tier, b.weight, b.relation)
            for b in store.bindings_for_record(version.id)
        }
        copied = published and source_bindings <= target_bindings
        if index == 0:
            parts["version"] = (version.id, published)
            if predecessor_id is not None:
                parts["close_predecessor"] = (predecessor_id, closed)
            parts["bindings"] = (version.id, copied)
        else:
            parts[f"fact:{version.id}"] = (version.id, copied and (not predecessor_id or closed))
    return parts


def edit_record(
    store: Store,
    reader: GraphifyReader | None,
    *,
    op_key: str,
    record_id: str,
    fields: dict,
    carry_facts: list[str],
    author: str | None,
) -> OperationResult:
    """Save a current record and selected accepted evidence as new versions.

    See design/superpowers/specs/2026-11-17-portal-design.md (D7).
    """
    return _edit(store, reader, op_key, record_id, fields, carry_facts, author, "edit")


def edit_and_accept(
    store: Store,
    reader: GraphifyReader | None,
    *,
    op_key: str,
    draft_id: str,
    fields: dict,
    keep_facts: list[str],
    author: str | None,
) -> OperationResult:
    """Reject a proposal and publish the human version and kept evidence.

    See design/superpowers/specs/2026-11-17-portal-design.md (D8).
    """
    return _edit(store, reader, op_key, draft_id, fields, keep_facts, author, "edit-and-accept")


def save_as_new(
    store: Store,
    reader: GraphifyReader | None,
    *,
    op_key: str,
    draft_id: str,
    fields: dict,
    author: str | None,
) -> OperationResult:
    """Publish the person's text separately when a draft rejection is unattributed.

    See design/superpowers/specs/2026-11-17-portal-design.md (D8).
    """
    return _edit(store, reader, op_key, draft_id, fields, [], author, "save-as-new")


@contextmanager
def _reservation(store: Store) -> Iterator[None]:
    # A writer we waited for may have published files and rolled its index changes back.
    # Refresh outside the reservation because the Store rebuild owns its own transaction.
    while True:
        with store._mutation():
            digest, _ = store._compute_canonical_digest()
            if digest == store.canonical_digest():
                yield
                return
        store.refresh_if_stale()


def _edit(
    store: Store,
    reader: GraphifyReader | None,
    op_key: str,
    record_id: str,
    fields: dict,
    evidence: list[str],
    author: str | None,
    mode: _Mode,
) -> OperationResult:
    store.refresh_if_stale()
    try:
        preflight_direct(op_key, record_id, fields, evidence, author)
    except InputLimitError:
        raise EditRefusal("invalid", "Input exceeds resource limits.") from None
    digest = hashlib.sha256(
        json.dumps([mode, record_id, fields, evidence], sort_keys=True).encode()
    ).hexdigest()
    evidence = list(dict.fromkeys(evidence))
    carry_facts = evidence if mode == "edit" else None
    draft_id = record_id if mode == "edit-and-accept" else None
    row = store.get_operation(op_key)
    if row is not None and row["request_digest"] != digest:
        raise EditRefusal("op_key_reused", _REUSED_KEY)
    if row is None:
        if carry_facts is not None:
            # Read the record and evidence together without reserving the writer.
            with store._lock:
                own_snapshot = not store._conn.in_transaction
                if own_snapshot:
                    store._conn.execute("BEGIN DEFERRED")
                try:
                    if (current := _get_record(store, record_id)) is not None:
                        _fields(store, current, fields, carry_facts=carry_facts)
                finally:
                    if own_snapshot:
                        store._conn.rollback()
        try:
            with _reservation(store):
                if store.get_operation(op_key) is None:
                    record = _get_record(store, record_id)
                    if record is None:
                        if store.get_domain(record_id) is not None:
                            raise EditRefusal(
                                "invalid", "Domains have verdicts and cannot be edited."
                            )
                        raise EditRefusal("not_found", "The record is no longer available.")
                    values = _fields(store, record, fields, carry_facts=carry_facts)
                    planned = [str(ULID()) for _ in range(len(evidence) + 1)]
                    if draft_id is None or record.status == "proposed":
                        _plan_versions(
                            store,
                            record,
                            values,
                            evidence,
                            planned,
                            mode,
                            started=False,
                            author=author,
                        )
                    try:
                        store.put_operation(
                            op_key,
                            digest,
                            draft_id,
                            record_id if draft_id is None else None,
                            planned,
                        )
                    except SupersessionConflictError as exc:
                        details: dict[str, JsonValue] = {}
                        if exc.head is not None:
                            details["head"] = exc.head
                        elif record.status == "rejected":
                            details["offer_save_as_new"] = True
                        raise EditRefusal(
                            "not_current", "This draft is no longer proposed.", **details
                        ) from None
        except sqlite3.IntegrityError:
            raise EditRefusal("operation_in_progress", _IN_PROGRESS) from None

    parts: list[OperationPart] = []
    state: Literal["started", "done"] = "started"
    committed_to_write = False
    try:
        with _reservation(store):
            row = store.get_operation(op_key)
            if row is None:
                raise EditRefusal("operation_in_progress", "Another save replaced this operation.")
            if row["request_digest"] != digest:
                raise EditRefusal("op_key_reused", _REUSED_KEY)
            planned = row["planned"]
            record = _get_record(store, record_id)
            if record is None:
                raise EditRefusal("not_found", "The record is no longer available.")
            if row["state"] == "done":
                version = _get_record(store, planned[0])
                if version is None:
                    raise EditRefusal(
                        "not_found", "The saved version is unavailable on this branch."
                    )
                if any(store.get_fact(id_) is None for id_ in planned[1:]):
                    raise EditRefusal(
                        "not_found", "The saved evidence is unavailable on this branch."
                    )
                replay = _plan_versions(
                    store,
                    record,
                    _fields(store, record, fields),
                    evidence,
                    planned,
                    mode,
                    started=True,
                    author=author,
                    completed=True,
                )
                for part, (id_, complete) in _operation_parts(store, replay, draft_id).items():
                    if not complete:
                        raise EditRefusal(
                            "not_current", "The saved operation changed on this branch."
                        )
                    parts.append(OperationPart(part=part, outcome="done", id=id_))
                return OperationResult(
                    op_key=op_key,
                    state="done",
                    parts=parts,
                    new_record_id=version.id,
                    offer_save_as_new=False,
                )
            published = _get_record(store, planned[0]) is not None
            values = _fields(store, record, fields, carry_facts=None if published else carry_facts)
            versions = _plan_versions(
                store,
                record,
                values,
                evidence,
                planned,
                mode,
                started=row["state"] == "started" or published,
                author=author,
            )
            before = _operation_parts(store, versions, draft_id)
            store.set_operation_state(op_key, "started")
            committed_to_write = True
            try:
                # Roll back derived writes while retaining the started row under the same lock.
                with store._savepoint():
                    if draft_id:
                        _reconcile_drop(store, reader, record)
                    for step in versions:
                        _reconcile_version(store, reader, step, author)
                    after = _operation_parts(store, versions, draft_id)
                    if not all(complete for _, complete in after.values()):
                        raise RuntimeError("The operation is incomplete.")
                    store.set_operation_state(op_key, "done")
                state = "done"
            except Exception:
                # Canonical files may have advanced inside the rolled-back savepoint.
                pass
    except Exception as exc:
        if not committed_to_write:
            if isinstance(exc, sqlite3.OperationalError) and _sqlite_busy(exc):
                operation = row
                safe_new_key = False
                try:
                    operation = store.get_operation(op_key)
                    if operation is not None and operation["state"] == "planned":
                        safe_new_key = _get_record(store, operation["planned"][0]) is None
                except sqlite3.OperationalError as read_error:
                    if not _sqlite_busy(read_error):
                        raise
                if operation is not None:
                    if safe_new_key:
                        raise EditRefusal(
                            "busy",
                            "The store is busy. A changed edit can use a new save key.",
                            retry_with_new_key=True,
                        ) from None
                    raise EditRefusal(
                        "busy",
                        "The store is busy. Retry this save with the same key.",
                        retry_with_same_key=True,
                    ) from None
            raise
        state = "started"
    if state == "started":
        # The savepoint keeps started on routine faults. SQLite can also roll back the
        # whole transaction, including at COMMIT. Restore only after a successful start.
        operation = store.get_operation(op_key)
        if operation is None or operation["state"] == "planned":
            with store._mutation():
                operation = store.get_operation(op_key)
                if operation is None:
                    raise EditRefusal(
                        "operation_in_progress", "Another save replaced this operation."
                    )
                if operation["state"] == "planned":
                    store.set_operation_state(op_key, "started")
        if operation["state"] == "done":
            state = "done"
        store.refresh_if_stale()
        after = _operation_parts(store, versions, draft_id)
    failed = False
    for part, (id_, complete) in after.items():
        outcome: Literal["done", "done_now", "failed", "skipped"]
        message = None
        if complete:
            outcome = "done" if before[part][1] else "done_now"
        elif failed:
            outcome = "skipped"
        else:
            failed = True
            outcome = "failed"
            message = "This step could not finish. Retry to resume the save."
        parts.append(OperationPart(part=part, outcome=outcome, id=id_, message=message))
    return OperationResult(
        op_key=op_key,
        state=state,
        parts=parts,
        new_record_id=planned[0] if after["version"][1] else None,
        offer_save_as_new=False,
    )
