"""Pure, immutable snapshot of canonical Sidegraph record files."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

from pydantic import BaseModel, ValidationError

from sidegraph.bootstrap.model import FrozenModel
from sidegraph.schema import Decision, DecisionStatus, Domain, Fact
from sidegraph.store import _archive_line_problem, _record_identity_problem
from sidegraph.store_format import SYMLINK_REASON, Occurrence
from sidegraph.store_layout import NOT_A_FILE_REASON, is_regular_file, stat_entry
from sidegraph.store_view import effective_bodies, inventory_of, is_packed_store


class CanonicalCatalog(FrozenModel):
    decisions: tuple[Decision, ...] = ()
    facts: tuple[Fact, ...] = ()
    domains: tuple[Domain, ...] = ()

    def find_by_ref(
        self,
        source: str,
        ref: str,
        statuses: tuple[DecisionStatus, ...],
    ) -> tuple[Decision, ...]:
        return tuple(
            decision
            for decision in self.decisions
            if decision.provenance.source == source
            and decision.provenance.ref == ref
            and decision.status in statuses
        )


def _is_regular(path: Path) -> bool:
    """False only for an entry that exists and is not a regular file (a directory or FIFO named
    like a record, which a read would crash on or block). A path that cannot be statted is left
    to the read, which reports it as before."""
    try:
        return is_regular_file(stat_entry(path))
    except OSError:
        return True


def _load_identified[ModelT: BaseModel](
    path: Path, model: type[ModelT], id_field: str
) -> ModelT | None:
    """Read one canonical file as ``model``. A file that is not a record by the store's identity
    rule (``store._record_identity_problem``: id present, safe, equal to the filename stem)
    is skipped with a warning. Keying it by its JSON id would mint a fresh ULID for a missing
    id on every load, so the fingerprint would differ between two loads of one store (design/
    superpowers/specs/2026-09-29-record-identity-design.md D12)."""
    if not _is_regular(path):
        print(f"sidegraph: WARNING skipping {path}: {NOT_A_FILE_REASON}.", file=sys.stderr)
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError(f"invalid canonical file {path}: {exc}") from exc
    problem = _record_identity_problem(data, id_field, path.stem)
    if problem is not None:
        print(f"sidegraph: WARNING skipping {path}: {problem}.", file=sys.stderr)
        return None
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise ValueError(f"invalid canonical file {path}: {exc}") from exc


def _identity_problem(error: str) -> bool:
    """Whether an occurrence error is the kind format 1 skips with a warning (the record is not
    one by the store's identity rule, or is not a regular file), rather than refuses."""
    return (
        error.startswith(("missing ", "unsafe ", NOT_A_FILE_REASON, SYMLINK_REASON))
        or error == "line is not a JSON object"
        or "does not match the identity" in error
    )


def _refuse_or_warn(store_dir: Path, occ: Occurrence) -> None:
    """The format-1 rules for one unreadable occurrence of a packed store: an identity problem
    is skipped with a warning, anything else (not JSON, a model error, a conflict) refuses the
    catalog, naming the file (and the line of a shard or archive line). An archive line of a
    record type this version does not know is not an error.

    # see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§7.2)"""
    where = store_dir / occ.path
    if occ.kind == "archive" and not occ.archive_type_known and occ.error is None:
        return
    error = (
        occ.error if occ.error is not None else ("in a merge conflict" if occ.in_conflict else None)
    )
    if error is None:
        return
    if _identity_problem(error):
        print(f"sidegraph: WARNING skipping {where}: {error}.", file=sys.stderr)
        return
    what = "file" if occ.kind == "file" else ("archive" if occ.kind == "archive" else "shard")
    at = where if occ.kind == "file" else f"{where}:{occ.line_no}"
    raise ValueError(f"invalid canonical {what} {at}: {error}")


def load_canonical_catalog(store_dir: Path) -> CanonicalCatalog:
    """Read canonical files directly, without constructing ``Store`` or derived state.

    A format-2 store is read through the inventory and ``effective()`` like every other reader;
    an identity the rules quarantine (unreadable, conflicted) has no record here.
    # see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§7.2)"""
    if not store_dir.is_dir():
        return CanonicalCatalog()
    if is_packed_store(store_dir):

        def unreadable(rel: str, exc: OSError) -> None:
            kind = {"shards": "shard", "archive": "archive"}.get(rel.split("/", 1)[0], "file")
            raise ValueError(f"invalid canonical {kind} {store_dir / rel}: {exc}") from exc

        inv = inventory_of(store_dir, unreadable)
        for occ in inv.occurrences:
            _refuse_or_warn(store_dir, occ)
        for segment in sorted((store_dir / "archive").glob("*.jsonl")):
            if not _is_regular(segment):  # the inventory drops it without an occurrence
                print(
                    f"sidegraph: WARNING skipping {segment}: {NOT_A_FILE_REASON}.",
                    file=sys.stderr,
                )
        packed = effective_bodies(inv)
        return CanonicalCatalog(
            decisions=tuple(
                Decision.model_validate(rec.raw)
                for (kind, _), rec in packed.items()
                if kind == "decision"
            ),
            facts=tuple(
                Fact.model_validate(rec.raw) for (kind, _), rec in packed.items() if kind == "fact"
            ),
            domains=tuple(
                Domain.model_validate(rec.raw)
                for (kind, _), rec in packed.items()
                if kind == "domain"
            ),
        )

    decisions: dict[str, Decision] = {}
    domains: dict[str, Domain] = {}
    for segment in sorted((store_dir / "archive").glob("*.jsonl")):
        if not _is_regular(segment):
            print(f"sidegraph: WARNING skipping {segment}: {NOT_A_FILE_REASON}.", file=sys.stderr)
            continue
        try:
            # ``\n`` only: a U+2028 or U+0085 inside a JSON string is data, not a line end
            lines = segment.read_text(encoding="utf-8").split("\n")
        except (OSError, UnicodeError) as exc:
            raise ValueError(f"invalid canonical archive {segment}: {exc}") from exc
        for lineno, line in enumerate(lines, 1):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
                if not isinstance(raw, dict):
                    print(
                        f"sidegraph: WARNING skipping a line of archive segment {segment.name}: "
                        "not a JSON object.",
                        file=sys.stderr,
                    )
                    continue
                payload = {key: value for key, value in raw.items() if key != "record_type"}
                problem = _archive_line_problem(raw.get("record_type"), payload)
                if problem is not None:
                    print(
                        f"sidegraph: WARNING skipping a line of archive segment {segment.name}: "
                        f"{problem}.",
                        file=sys.stderr,
                    )
                    continue
                if raw.get("record_type") == "decision":
                    decision = Decision.model_validate(payload)
                    decisions.setdefault(decision.id, decision)
                elif raw.get("record_type") == "domain":
                    domain = Domain.model_validate(payload)
                    domains.setdefault(domain.domain_id, domain)
            except (json.JSONDecodeError, TypeError, ValidationError) as exc:
                raise ValueError(f"invalid canonical archive {segment}:{lineno}: {exc}") from exc

    for path in sorted((store_dir / "decisions").glob("*.json")):
        hot_decision = _load_identified(path, Decision, "id")
        if hot_decision is not None:
            decisions[hot_decision.id] = hot_decision
    loaded_facts = (
        _load_identified(path, Fact, "id") for path in sorted((store_dir / "facts").glob("*.json"))
    )
    facts = tuple(fact for fact in loaded_facts if fact is not None)
    for path in sorted((store_dir / "domains").glob("*.json")):
        hot_domain = _load_identified(path, Domain, "domain_id")
        if hot_domain is not None:
            domains[hot_domain.domain_id] = hot_domain

    return CanonicalCatalog(
        decisions=tuple(decisions[key] for key in sorted(decisions)),
        facts=facts,
        domains=tuple(domains[key] for key in sorted(domains)),
    )


def fingerprint_catalog(catalog: CanonicalCatalog) -> str:
    material = json.dumps(
        catalog.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode()).hexdigest()
