"""Pure, immutable snapshot of canonical Sidegraph record files."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pydantic import BaseModel, ValidationError

from sidegraph.bootstrap.model import FrozenModel
from sidegraph.schema import Decision, DecisionStatus, Domain, Fact


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


def _load_model[ModelT: BaseModel](path: Path, model: type[ModelT]) -> ModelT:
    try:
        return model.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValidationError) as exc:
        raise ValueError(f"invalid canonical file {path}: {exc}") from exc


def load_canonical_catalog(store_dir: Path) -> CanonicalCatalog:
    """Read canonical files directly, without constructing ``Store`` or derived state."""
    if not store_dir.is_dir():
        return CanonicalCatalog()

    decisions: dict[str, Decision] = {}
    domains: dict[str, Domain] = {}
    for segment in sorted((store_dir / "archive").glob("*.jsonl")):
        try:
            lines = segment.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as exc:
            raise ValueError(f"invalid canonical archive {segment}: {exc}") from exc
        for lineno, line in enumerate(lines, 1):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
                if not isinstance(raw, dict):
                    raise TypeError("archive record must be a JSON object")
                payload = {key: value for key, value in raw.items() if key != "record_type"}
                if raw.get("record_type") == "decision":
                    decision = Decision.model_validate(payload)
                    decisions.setdefault(decision.id, decision)
                elif raw.get("record_type") == "domain":
                    domain = Domain.model_validate(payload)
                    domains.setdefault(domain.domain_id, domain)
            except (json.JSONDecodeError, TypeError, ValidationError) as exc:
                raise ValueError(f"invalid canonical archive {segment}:{lineno}: {exc}") from exc

    for path in sorted((store_dir / "decisions").glob("*.json")):
        decision = _load_model(path, Decision)
        decisions[decision.id] = decision
    facts = tuple(_load_model(path, Fact) for path in sorted((store_dir / "facts").glob("*.json")))
    for path in sorted((store_dir / "domains").glob("*.json")):
        domain = _load_model(path, Domain)
        domains[domain.domain_id] = domain

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
