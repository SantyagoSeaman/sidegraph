"""Pure-read, deterministic record-to-ADR projection; no engine or host integration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from string import Template
from urllib.parse import quote

from pydantic import BaseModel

from .adr_identity import escape_markers
from .profiles import AdrExport
from .schema import (
    AnchorBinding,
    Decision,
    DecisionKind,
    DecisionStatus,
    Domain,
    DomainStatus,
    Entity,
    EntityKind,
    Fact,
)
from .store_view import EffectiveRecord, effective_records
from .verify import verify_snapshot

DOC_DERIVED_SOURCES = frozenset({"doc-import", "import", "doc", "rationale", "graphify"})

_PUBLISHED = frozenset(
    {DecisionStatus.ACCEPTED, DecisionStatus.SUPERSEDED, DecisionStatus.DEPRECATED}
)
_KINDS = frozenset({DecisionKind.ADR, DecisionKind.CONSTRAINT})


@dataclass(frozen=True)
class AdrSnapshot:
    """Owned record values and canonical source locations, not volatile graph mappings."""

    root: Path
    decisions: dict[str, Decision]
    facts: dict[str, Fact]
    domains: dict[str, Domain]
    entities: dict[str, Entity]
    bindings: dict[str, list[AnchorBinding]]
    record_paths: dict[str, Path]
    fact_paths: dict[str, Path]
    domain_paths: dict[str, Path]

    @property
    def exported(self) -> dict[str, Decision]:
        return {
            rid: d
            for rid, d in sorted(self.decisions.items())
            if d.status in _PUBLISHED and d.kind in _KINDS
        }


def read_snapshot(path: Path) -> AdrSnapshot:
    """Admit and read canonical files without opening Store, index.db or graph.json.

    Admission is deliberately strict: the verifier's findings refuse the store. Identical
    archive/archive rows deduplicate; on a per-record store any hot/archive coexistence is
    refused, even if identical. A packed store admits the pre-maintenance pairs the verifier
    calls legal. Every record is then its inventory's effective record, never an archived body
    laid over a live one.

    # see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§7.2)
    """
    # The project holding a store symlink owns its relative descriptors.
    root = path.absolute()
    if not root.is_dir() or (root / "decisions.db").exists():
        raise ValueError(
            "ADR export needs a canonical store directory; "
            "migrate legacy stores with sidegraph-init first"
        )
    violations = verify_snapshot(root)
    if violations:
        first = violations[0]
        raise ValueError(f"store verification failed: {first.code} at {first.path}")
    packed = effective_records(root, any_format=True)
    assert packed is not None
    return _snapshot_of(root, packed)


def _snapshot_of(root: Path, packed: dict[tuple[str, str], EffectiveRecord]) -> AdrSnapshot:
    """The snapshot of a store: every record is its inventory's effective record
    (``store_rules.effective``), whichever file, shard line or archive segment holds it, so an
    archived body never overwrites a live one. A record's path is where it is stored.

    # see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§7.2)"""

    def of[Model: BaseModel](record_type: str, model: type[Model]) -> tuple[dict[str, Model], dict]:
        values: dict[str, Model] = {}
        paths: dict[str, Path] = {}
        for (kind, rid), rec in packed.items():
            if kind == record_type:
                values[rid] = model.model_validate(rec.raw)
                paths[rid] = rec.source_path(root)
        return values, paths

    decisions, record_paths = of("decision", Decision)
    facts, fact_paths = of("fact", Fact)
    domains, domain_paths = of("domain", Domain)
    entities, _ = of("entity", Entity)
    bindings: dict[str, list[AnchorBinding]] = {}
    for (kind, rid), rec in packed.items():
        if kind == "bindings":
            if not isinstance(rec.raw, list):
                raise ValueError(f"cannot read canonical binding file: {rid}.json")
            bindings[rid] = [
                AnchorBinding.model_validate({**item, "record_id": rid}) for item in rec.raw
            ]
    return AdrSnapshot(
        root, decisions, facts, domains, entities, bindings, record_paths, fact_paths, domain_paths
    )


def _label(text: str) -> str:
    return " ".join(text.splitlines()).replace("[", "\\[").replace("]", "\\]")


def file_link(label: str, target: Path, out: Path) -> str:
    """Link from a flat ADR directory, retaining archive ID in the label where needed."""
    try:
        path = os.path.relpath(target, out).replace(os.sep, "/")
    except ValueError:  # Windows cross-drive output has no relative spelling.
        path = target.resolve().as_uri()
        return f"[{_label(label)}]({path})"
    return f"[{_label(label)}]({quote(path, safe='/')})"


def local_source(snapshot: AdrSnapshot, ref: str | None) -> Path | None:
    """Only safe project-local paths are link targets; arbitrary provenance stays prose."""
    if not ref:
        return None
    value = ref.split("#", 1)[0]
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or "\\" in value or ":" in value:
        return None
    candidate = snapshot.root.parent / value
    if not candidate.resolve().is_relative_to(snapshot.root.parent.resolve()):
        return None
    return candidate


def record_domains(snapshot: AdrSnapshot, record_id: str) -> list[Domain]:
    """Explicit accepted domain identity bindings only; never claim volatile liveness."""
    current = {
        d.slug: d
        for d in sorted(snapshot.domains.values(), key=lambda d: d.domain_id)
        if d.status is DomainStatus.ACCEPTED
    }
    slugs = set()
    for binding in snapshot.bindings.get(record_id, []):
        entity = snapshot.entities.get(binding.entity_id)
        if entity and entity.canonical_name.startswith("domain:"):
            slugs.add(entity.canonical_name.removeprefix("domain:"))
    return [current[slug] for slug in sorted(slugs) if slug in current]


def _ref(snapshot: AdrSnapshot, rid: str, paths: dict[str, str], out: Path) -> str:
    if rid in paths:
        return f"[{_label(rid)}]({quote(paths[rid], safe='/')})"
    return file_link(rid, snapshot.record_paths[rid], out)


def render_record(
    snapshot: AdrSnapshot, record_id: str, paths: dict[str, str], config: AdrExport, out: Path
) -> str:
    """Render complete authored prose plus a mandatory source/status/evidence footer."""
    decision = snapshot.exported[record_id]
    record_link = file_link(record_id, snapshot.record_paths[record_id], out)
    evidence_lines = []
    for fact in sorted(snapshot.facts.values(), key=lambda f: f.id):
        if (
            fact.status is DecisionStatus.ACCEPTED
            and fact.valid_to is None
            and record_id in fact.supports
        ):
            evidence_lines.append(
                f"- {file_link(fact.id, snapshot.fact_paths[fact.id], out)}: {fact.statement}\n"
                f"  Source: {fact.source}"
            )
    anchors = []
    for binding in snapshot.bindings.get(record_id, []):
        entity = snapshot.entities.get(binding.entity_id)
        if (
            entity is None
            or entity.kind is not EntityKind.CONCRETE
            or entity.canonical_name.startswith("community:")
        ):
            continue
        descriptor_path = entity.descriptor.file_path if entity.descriptor else None
        path = local_source(snapshot, descriptor_path)
        if path is not None:
            anchors.append(
                f"- {file_link(entity.canonical_name, path, out)} "
                "(durable descriptor; current resolution not certified)"
            )
        else:
            detail = _label(descriptor_path) if descriptor_path else "no durable file path"
            anchors.append(f"- {_label(entity.canonical_name)} ({detail}; unlinked, not verified)")
    domains = [
        file_link(d.title, snapshot.domain_paths[d.domain_id], out)
        for d in record_domains(snapshot, record_id)
    ]
    source = f"Recorded source: {decision.provenance.source}."
    if decision.provenance.ref:
        original = local_source(snapshot, decision.provenance.ref)
        source += " Ref: " + (
            file_link(decision.provenance.ref, original, out)
            if original
            else _label(decision.provenance.ref)
        )
    if decision.provenance.source in DOC_DERIVED_SOURCES:
        source += (
            " Document-derived orientation; consult the original artifact "
            "and current implementation."
        )
    successors = [
        d
        for d in snapshot.decisions.values()
        if d.supersedes == record_id and d.status in _PUBLISHED
    ]
    previous = decision.supersedes
    fields = {
        "title": decision.title,
        "context": decision.context,
        "choice": decision.choice,
        "rejected": decision.rejected or "Not recorded.",
        "consequences": decision.consequences or "Not recorded.",
        "kind": decision.kind.value,
        "status": decision.status.value,
        "valid_from": decision.valid_from.isoformat(),
        "valid_to": decision.valid_to.isoformat() if decision.valid_to else "Open",
        "evidence": "\n".join(evidence_lines) or "None recorded.",
        "anchors": "\n".join(sorted(set(anchors))) or "None recorded.",
        "domains": ", ".join(domains) or "Unassigned",
        "supersedes": _ref(snapshot, previous, paths, out)
        if previous in snapshot.decisions
        else "None",
        "successors": ", ".join(
            _ref(snapshot, d.id, paths, out) for d in sorted(successors, key=lambda d: d.id)
        )
        or "None",
        "record_link": record_link,
        "source": source,
    }
    prose = (
        Template(config.template)
        .substitute(fields)
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .rstrip()
    )
    footer = f"""---

Record: {record_link}
Kind: {fields["kind"]}; status: {fields["status"]}
Valid from: {fields["valid_from"]}; valid to: {fields["valid_to"]}
Supersedes: {fields["supersedes"]}
Successors: {fields["successors"]}
Domains (explicit intent): {fields["domains"]}
{source}

Anchors:
{fields["anchors"]}

Supporting evidence:
{fields["evidence"]}
"""
    return escape_markers(prose + "\n\n" + footer)


def render_index(snapshot: AdrSnapshot, paths: dict[str, str]) -> str:
    """Group by explicit domain intent; a partial store is not complete architecture."""
    groups: dict[str, list[str]] = {}
    for rid, decision in snapshot.exported.items():
        if rid not in paths:
            continue
        domains = record_domains(snapshot, rid)
        for title in [d.title for d in domains] or ["Unassigned"]:
            groups.setdefault(title, []).append(
                f"- [{_label(decision.title)}]({quote(paths[rid], safe='/')}) "
                f"— {decision.status.value}"
            )
    lines = [
        "# ADR projection",
        "",
        "Record-derived documentation; coverage is limited to the available store.",
        "",
    ]
    if not groups:
        lines += ["No eligible ADR or constraint records.", ""]
    for title, entries in sorted(groups.items()):
        lines += [f"## {_label(title)}", "", *entries, ""]
    return escape_markers("\n".join(lines))
